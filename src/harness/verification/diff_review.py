"""Deterministic diff-scope and test-integrity review (PRD 4 section 11).

Heuristics produce evidence, not a model verdict. BLOCKING findings prevent
PASS; WARN findings are reported and shown to the validator.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Dict, List, Optional, Sequence, Set

from harness.context.prompt_firewall import PromptFirewall
from harness.verification.contract_service import in_test_area, is_test_path

SKIP_MARKERS = re.compile(
    r"(@pytest\.mark\.(skip|skipif|xfail)|pytest\.skip\(|pytest\.xfail\(|@unittest\.skip|unittest\.skip\(|"
    r"@unittest\.expectedFailure|self\.skipTest\(|raise\s+unittest\.SkipTest|__test__\s*=\s*False)"
)
ASSERTION = re.compile(r"^\s*(assert\b|self\.assert\w*\(|pytest\.raises\(|with\s+pytest\.raises)")
DISCOVERY_FILES = {"pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", "conftest.py", ".coveragerc", "noxfile.py"}
DISCOVERY_KEYS = re.compile(r"(testpaths|python_files|python_classes|python_functions|addopts|norecursedirs|collect_ignore|pytest_collection_modifyitems|pytest_ignore_collect|\[tool\.pytest|\[pytest\]|\[tool:pytest\])")
DEPENDENCY_FILES = {
    "requirements.txt", "requirements-dev.txt", "setup.py", "pipfile", "pipfile.lock", "poetry.lock",
    "uv.lock", "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
}
HARDCODE = re.compile(r"^\s*(if|elif)\s+.*==\s*['\"\d].*:\s*return\b")
# Assertions that can never fail: replacing a real check with one of these weakens the test.
TRIVIAL_ASSERT = re.compile(
    r"^\s*(assert\s+(True|1|not\s+(False|0|None)|(['\"]).*\4)\s*(,.*)?(#.*)?$|"
    r"self\.assertTrue\(\s*(True|1)\s*\)|self\.assertFalse\(\s*(False|0|None)\s*\))"
)


@dataclass
class FileDiff:
    path: str
    old_path: Optional[str]
    status: str  # added, deleted, modified
    binary: bool = False
    added: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)


def parse_git_diff(diff: str) -> List[FileDiff]:
    files: List[FileDiff] = []
    current: Optional[FileDiff] = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            match = re.match(r"diff --git a/(.*) b/(.*)", line)
            if not match:
                continue
            current = FileDiff(path=match.group(2), old_path=match.group(1), status="modified")
            files.append(current)
            continue
        if current is None:
            continue
        if line.startswith("new file mode"):
            current.status = "added"
        elif line.startswith("deleted file mode"):
            current.status = "deleted"
        elif line.startswith("GIT binary patch") or line.startswith("Binary files "):
            current.binary = True
        elif line.startswith("+++") or line.startswith("---"):
            continue
        elif line.startswith("+"):
            current.added.append(line[1:])
        elif line.startswith("-"):
            current.removed.append(line[1:])
    return files


@dataclass
class Finding:
    severity: str  # WARN or BLOCKING
    category: str
    path: Optional[str]
    detail: str

    def as_dict(self) -> Dict[str, Optional[str]]:
        return {"severity": self.severity, "category": self.category, "path": self.path, "detail": self.detail[:1000]}


@dataclass
class DiffReview:
    status: str
    changed_paths: List[str]
    findings: List[Finding]
    existing_tests_deleted: int
    skip_markers_added: int
    assertions_weakened_suspected: int
    discovery_config_changed: bool


class DiffScopeReviewer:
    def __init__(self) -> None:
        self.firewall = PromptFirewall()

    def review(
        self,
        diff: str,
        *,
        baseline_tests: Sequence[str],
        plan_paths: Sequence[str],
        max_changed_lines: int = 2000,
        max_changed_files: int = 50,
    ) -> DiffReview:
        files = parse_git_diff(diff)
        findings: List[Finding] = []
        baseline_set: Set[str] = set(baseline_tests)
        deleted_tests = skip_added = weakened = 0
        discovery_changed = False
        plan_dirs = {str(PurePosixPath(path).parent) for path in plan_paths} | set(plan_paths)
        total_lines = 0
        for item in files:
            path = item.path
            name = PurePosixPath(path).name
            total_lines += len(item.added) + len(item.removed)
            existing_test = path in baseline_set or (item.old_path in baseline_set if item.old_path else False)
            if item.status == "deleted" and (existing_test or (is_test_path(path) and item.old_path in baseline_set)):
                deleted_tests += 1
                findings.append(Finding("BLOCKING", "existing_test_deleted", path, "An existing test file was deleted."))
            if in_test_area(path):
                added_skips = [line for line in item.added if SKIP_MARKERS.search(line)]
                removed_skips = [line for line in item.removed if SKIP_MARKERS.search(line)]
                net_skips = len(added_skips) - len(removed_skips)
                if net_skips > 0 and item.status != "added":
                    skip_added += net_skips
                    findings.append(Finding("BLOCKING", "skip_marker_added", path, f"{net_skips} skip/xfail marker(s) added to an existing test file."))
                trivial = [line for line in item.added if TRIVIAL_ASSERT.search(line)]
                if trivial and item.status != "added":
                    findings.append(Finding("BLOCKING", "trivial_assertion_added", path,
                                            f"{len(trivial)} always-true assertion(s) added to an existing test file."))
                removed_asserts = sum(1 for line in item.removed if ASSERTION.search(line) and not TRIVIAL_ASSERT.search(line))
                added_asserts = sum(1 for line in item.added if ASSERTION.search(line) and not TRIVIAL_ASSERT.search(line))
                if item.status == "modified" and removed_asserts > added_asserts:
                    weakened += removed_asserts - added_asserts
                    findings.append(Finding("WARN", "assertions_weakened_suspected", path, f"{removed_asserts - added_asserts} assertion line(s) removed."))
            if name in DISCOVERY_FILES:
                changed_discovery = [line for line in item.added + item.removed if DISCOVERY_KEYS.search(line)]
                if changed_discovery or (name == "conftest.py" and item.status != "added"):
                    discovery_changed = True
                    findings.append(Finding("BLOCKING", "test_discovery_config_changed", path, "Test discovery configuration changed."))
            if name.lower() in DEPENDENCY_FILES:
                findings.append(Finding("WARN", "dependency_manifest_changed", path, "Dependency manifest or lockfile changed."))
            if item.binary:
                findings.append(Finding("WARN", "binary_change", path, "Binary content added or modified."))
            if not in_test_area(path) and any(HARDCODE.search(line) for line in item.added):
                findings.append(Finding("WARN", "possible_task_specific_hardcoding", path, "An added branch returns a value for a literal input."))
            secrets = [line for line in item.added if self.firewall.filter_text(line) != line and "[REDACTED" in self.firewall.filter_text(line)]
            if secrets:
                findings.append(Finding("BLOCKING", "secret_like_content", path, "Added lines contain secret-like content."))
            if plan_paths and not in_test_area(path) and path not in plan_dirs and str(PurePosixPath(path).parent) not in plan_dirs:
                findings.append(Finding("WARN", "outside_planned_scope", path, "Changed file is outside the plan's likely edit locations."))
        if total_lines > max_changed_lines or len(files) > max_changed_files:
            findings.append(Finding("WARN", "large_diff", None, f"{len(files)} files / {total_lines} changed lines."))
        status = "BLOCKING" if any(f.severity == "BLOCKING" for f in findings) else ("WARN" if findings else "CLEAN")
        return DiffReview(
            status=status,
            changed_paths=sorted({item.path for item in files}),
            findings=findings,
            existing_tests_deleted=deleted_tests,
            skip_markers_added=skip_added,
            assertions_weakened_suspected=weakened,
            discovery_config_changed=discovery_changed,
        )
