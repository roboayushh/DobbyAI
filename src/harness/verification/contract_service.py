"""Derive and freeze the verification contract before any mutation (PRD 4 section 6).

Checks are built by trusted host code from: user-explicit test references in
the task text, the plan's edit locations and test paths, repository test
inventory (treated as untrusted data), and harness invariants. Model output can
influence *which* repository tests are relevant, never how they are executed.
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from harness.contracts.verification import (
    CheckOrigin,
    ContractBaselineV1,
    ContractCheckV1,
    ContractCriterionV1,
    VerificationContractV1,
)
from harness.persistence import canonical_json

JUNIT = "--junitxml=/output/junit.xml"
PYTEST = ["python", "-m", "pytest", "-q", "-rfE", "-o", "junit_family=xunit2"]
_NODE_ID = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*test_[\w.-]*\.py(?:::[\w\[\]:.,-]+)?|(?:[\w.-]+/)*[\w.-]*_test\.py(?:::[\w\[\]:.,-]+)?)")
IGNORED = {".git", ".venv", "venv", "node_modules", "__pycache__", ".tox", "build", "dist", ".mypy_cache", ".pytest_cache"}
PYTEST_CONFIGS = ("pytest.ini", "conftest.py", "tox.ini", "setup.cfg", "pyproject.toml")

PROHIBITED_SHORTCUTS = [
    "disable_required_test",
    "add_unconditional_skip_or_xfail",
    "weaken_assertions_to_pass",
    "hide_test_discovery",
    "unconditional_task_specific_hardcoding",
    "mute_exceptions_or_return_fixed_values",
    "model_narrative_as_evidence",
    "zero_collected_tests_as_success",
]


def sha(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def is_test_path(path: str) -> bool:
    """True for files pytest collects by default (test_*.py / *_test.py)."""
    name = PurePosixPath(path).name
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def in_test_area(path: str) -> bool:
    """Test files plus test support (conftest, tests/ helpers) for integrity review."""
    pure = PurePosixPath(path)
    return is_test_path(path) or pure.name == "conftest.py" or any(part in ("tests", "test") for part in pure.parts[:-1])


def inventory(root: Path) -> Tuple[List[str], Set[str]]:
    """(python test files, local top-level module names) under ``root``."""
    tests: List[str] = []
    modules: Set[str] = set()
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in IGNORED)
        relative = Path(current).relative_to(root)
        if relative.parts == ():
            modules.update(d for d in dirs)
            modules.update(Path(name).stem for name in files if name.endswith(".py"))
        if relative.parts and relative.parts[0] in ("src", "lib") and len(relative.parts) == 1:
            modules.update(d for d in dirs)
            modules.update(Path(name).stem for name in files if name.endswith(".py"))
        for name in sorted(files):
            path = (relative / name).as_posix()
            if is_test_path(path) and (Path(current) / name).is_file():
                tests.append(path)
    return sorted(tests), modules


def module_names(path: str) -> Set[str]:
    """Import names a test would use for implementation file ``path``."""
    pure = PurePosixPath(path)
    if pure.suffix != ".py":
        return set()
    parts = list(pure.with_suffix("").parts)
    names = {parts[-1]} if parts[-1] != "__init__" else ({parts[-2]} if len(parts) > 1 else set())
    if parts[-1] == "__init__":
        parts = parts[:-1]
    for start in range(len(parts)):
        names.add(".".join(parts[start:]))
    return {name for name in names if name}


def related_tests(root: Path, tests: Sequence[str], edit_paths: Sequence[str], limit: int = 20) -> List[str]:
    implementation = [path for path in edit_paths if path.endswith(".py") and not is_test_path(path)]
    wanted: Set[str] = set()
    names: Set[str] = set()
    for path in implementation:
        names |= module_names(path)
    stems = {PurePosixPath(path).stem for path in implementation if PurePosixPath(path).stem != "__init__"}
    for test in tests:
        name = PurePosixPath(test).name
        if any(stem and (f"test_{stem}" in name or f"{stem}_test" in name) for stem in stems):
            wanted.add(test)
            continue
        try:
            text = (root / test).read_text(encoding="utf-8", errors="replace")[:200_000]
        except OSError:
            continue
        for module in names:
            pattern = rf"(^|\n)\s*(from\s+[\w.]*\b{re.escape(module)}\b[\w.]*\s+import|import\s+[\w.]*\b{re.escape(module)}\b)"
            if re.search(pattern, text):
                wanted.add(test)
                break
    return sorted(wanted)[:limit]


def explicit_references(text: str, root: Path) -> List[str]:
    found: List[str] = []
    for match in _NODE_ID.finditer(text or ""):
        value = match.group(1).rstrip(".,)")
        path = value.split("::", 1)[0]
        if ".." in path.split("/"):
            continue
        if value not in found:
            found.append(value)
    return found[:10]


class ContractBuilder:
    """Pure derivation of a draft contract from host-observed inputs."""

    def __init__(self, *, test_batch_seconds: int = 600, focused_seconds: int = 180) -> None:
        self.test_batch_seconds = test_batch_seconds
        self.focused_seconds = focused_seconds

    def build(
        self,
        *,
        contract_id: str,
        run_id: str,
        task_id: str,
        task_revision: int,
        plan: Any,
        plan_id: str,
        task_text: str,
        root: Path,
        baseline_commit: str,
        baseline_content_sha256: str,
        runtime_profile_fingerprint: str,
        policy_sha256: str,
    ) -> VerificationContractV1:
        tests, _ = inventory(root)
        checks: List[ContractCheckV1] = []
        criteria_checks: List[str] = []
        existing = set(tests)

        explicit = explicit_references(f"{task_text}", root)
        for index, reference in enumerate(explicit):
            path = reference.split("::", 1)[0]
            checks.append(
                ContractCheckV1(
                    check_id=f"check_focused_{index + 1}",
                    origin=CheckOrigin.USER_EXPLICIT,
                    kind="test",
                    tier="focused",
                    required=True,
                    baseline_policy="required" if path in existing else "optional",
                    argv=[*PYTEST, reference, JUNIT],
                    timeout_seconds=self.focused_seconds,
                    parser="pytest-junit@1",
                    minimum_tests=1,
                )
            )
            criteria_checks.append(checks[-1].check_id)

        edit_paths = [location.path for location in getattr(plan, "likely_edit_locations", [])]
        strategy_text = " ".join(getattr(plan, "verification_strategy", []))
        planned_tests = [path for path in edit_paths if is_test_path(path)]
        planned_tests += [ref.split("::", 1)[0] for ref in explicit_references(strategy_text, root)]
        relevant = sorted(set(related_tests(root, tests, edit_paths)) | {path for path in planned_tests if path in existing})
        relevant = [path for path in relevant if all(not ref.startswith(path) for ref in explicit)]
        if relevant:
            checks.append(
                ContractCheckV1(
                    check_id="check_relevant",
                    origin=CheckOrigin.PLANNER_PROPOSED,
                    kind="test",
                    tier="relevant",
                    required=True,
                    baseline_policy="required",
                    argv=[*PYTEST, *relevant, JUNIT],
                    timeout_seconds=self.test_batch_seconds,
                    parser="pytest-junit@1",
                    minimum_tests=1,
                )
            )
            criteria_checks.append("check_relevant")

        checks.append(
            ContractCheckV1(
                check_id="check_broad",
                origin=CheckOrigin.RUNTIME_ADAPTER,
                kind="test",
                tier="broad",
                required=True,
                baseline_policy="required",
                argv=[*PYTEST, JUNIT],
                timeout_seconds=self.test_batch_seconds,
                parser="pytest-junit@1",
                minimum_tests=1,
            )
        )
        criteria_checks.append("check_broad")
        checks.append(
            ContractCheckV1(
                check_id="check_syntax",
                origin=CheckOrigin.HARNESS_INVARIANT,
                kind="invariant",
                tier="invariant",
                required=True,
                baseline_policy="not_applicable",
                argv=["python", "-I", "-B", "/opt/harness/checks/syntax_check.py", "/context/changed_paths.json"],
                timeout_seconds=120,
                parser="exit-code@1",
                minimum_tests=0,
            )
        )
        focused_or_relevant = [cid for cid in criteria_checks if cid != "check_broad"]
        mapped = focused_or_relevant + ["check_broad"] if focused_or_relevant else ["check_broad"]
        criteria = [
            ContractCriterionV1(
                criterion_id=criterion.criterion_id,
                statement=criterion.statement[:2000],
                required=True,
                check_ids=mapped,
            )
            for criterion in getattr(plan, "acceptance_criteria", [])
        ] or [ContractCriterionV1(criterion_id="ac_task", statement="The task is resolved.", required=True, check_ids=mapped)]
        core = {
            "schema_version": "1.0",
            "contract_id": contract_id,
            "run_id": run_id,
            "task_id": task_id,
            "task_revision": task_revision,
            "plan_id": plan_id,
            "plan_revision": plan.plan_revision,
            "baseline": ContractBaselineV1(commit=baseline_commit, content_tree_sha256=baseline_content_sha256).model_dump(),
            "criteria": [item.model_dump() for item in criteria],
            "checks": [item.model_dump(mode="json") for item in checks],
            "prohibited_shortcuts": PROHIBITED_SHORTCUTS,
            "validator_required": True,
            "max_validator_overlay_files": 10,
            "max_validator_overlay_bytes": 200_000,
            "flake_retries": 1,
            "max_repair_attempts": 2,
            "runtime_profile_fingerprint": runtime_profile_fingerprint,
            "policy_sha256": policy_sha256,
            "state": "FROZEN",
        }
        return VerificationContractV1(**core, contract_sha256=sha(core))


def test_set_sha256(contract: VerificationContractV1) -> str:
    return sha([check.model_dump(mode="json") for check in contract.checks])


def command_sha256(check: ContractCheckV1) -> str:
    return sha({"argv": check.argv, "cwd": check.cwd, "parser": check.parser, "timeout": check.timeout_seconds})
