"""Untrusted test-report parsing with false-pass protection (PRD 4 section 9).

Exit codes alone never pass a test check: discovery counts and per-case
results must be parsed. Reports are size-bounded and any DTD/entity
declaration is rejected before XML parsing (no XXE, no entity expansion).
"""
from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from harness.contracts.verification import CheckStatus

MAX_REPORT_BYTES = 16 * 1024 * 1024
_ADDRESS = re.compile(r"0x[0-9a-fA-F]{4,}")
_LINE_NO = re.compile(r"(line |:)\d+")
_MISSING_MODULE = re.compile(r"ModuleNotFoundError: No module named '([A-Za-z0-9_.]+)'")
_IMPORT_ERROR = re.compile(r"ImportError: (?:cannot import name|No module named) ['\"]?([A-Za-z0-9_.]+)")


@dataclass(frozen=True)
class CaseResult:
    test_id: str
    status: str  # PASS, FAIL, SKIP, ERROR
    duration_ms: Optional[int]
    failure_signature: Optional[str]
    message: str = ""

    @property
    def raw_hash(self) -> str:
        return hashlib.sha256(self.test_id.encode("utf-8")).hexdigest()


@dataclass
class ParsedReport:
    parser: str
    status: CheckStatus
    cases: List[CaseResult] = field(default_factory=list)
    discovered: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0
    detail: str = ""
    missing_modules: List[str] = field(default_factory=list)

    def counts(self) -> Dict[str, int]:
        return {
            "discovered": self.discovered,
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "errors": self.errors,
        }

    def failing_ids(self) -> List[str]:
        return sorted(case.test_id for case in self.cases if case.status in ("FAIL", "ERROR"))

    def case_map(self) -> Dict[str, str]:
        return {case.test_id: case.status for case in self.cases}


def failure_signature(text: str) -> str:
    """Stable signature of a failure message with volatile parts removed."""
    lines = [line.strip() for line in (text or "").strip().splitlines() if line.strip()]
    relevant = [line for line in lines if not line.startswith(("self =", "@", "def "))][-3:]
    normalized = _LINE_NO.sub(r"\1?", _ADDRESS.sub("0x?", "\n".join(relevant)))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _safe_xml(data: bytes) -> Optional[ElementTree.Element]:
    if len(data) > MAX_REPORT_BYTES:
        return None
    head = data[:4096].upper()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in data.upper():
        return None
    try:
        return ElementTree.fromstring(data)
    except ElementTree.ParseError:
        return None


def _local_modules(local_roots: Sequence[str]) -> Set[str]:
    return {root.split(".")[0] for root in local_roots if root}


def parse_pytest_junit(
    report: Optional[bytes],
    exit_code: Optional[int],
    *,
    stdout: str = "",
    stderr: str = "",
    local_modules: Sequence[str] = (),
    minimum_tests: int = 1,
) -> ParsedReport:
    parser = "pytest-junit@1"
    combined = f"{stdout}\n{stderr}"
    if report is None or not report.strip():
        if exit_code == 5:
            return ParsedReport(parser, CheckStatus.ZERO_TESTS, detail="pytest collected no tests")
        if exit_code == 4 and ("not found" in combined or "no match" in combined or "ERROR: file" in combined):
            return ParsedReport(parser, CheckStatus.ZERO_TESTS, detail="requested test path does not exist")
        missing = _missing_third_party(combined, local_modules)
        if missing:
            return ParsedReport(parser, CheckStatus.BLOCKED_ENVIRONMENT, detail="missing dependency", missing_modules=missing)
        return ParsedReport(parser, CheckStatus.UNPARSABLE, detail=f"no JUnit report (exit {exit_code})")
    root = _safe_xml(report)
    if root is None:
        return ParsedReport(parser, CheckStatus.UNPARSABLE, detail="JUnit report rejected (size, DTD/entity, or malformed XML)")
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    if root.tag not in ("testsuite", "testsuites"):
        return ParsedReport(parser, CheckStatus.UNPARSABLE, detail="unexpected JUnit root element")
    cases: List[CaseResult] = []
    seen: Set[str] = set()
    for suite in suites:
        for case in suite.iter("testcase"):
            classname = (case.get("classname") or "").strip()
            name = (case.get("name") or "").strip()
            if not name:
                continue
            test_id = f"{classname}::{name}" if classname else name
            if test_id in seen:
                test_id = f"{test_id}#{len(seen)}"
            seen.add(test_id)
            status = "PASS"
            message = ""
            for child in case:
                tag = child.tag.lower()
                text = (child.get("message") or "") + "\n" + (child.text or "")
                if tag == "failure":
                    status, message = "FAIL", text
                elif tag == "error":
                    status, message = "ERROR", text
                elif tag == "skipped" and status == "PASS":
                    status, message = "SKIP", text
            try:
                duration = int(float(case.get("time", "0") or 0) * 1000)
            except ValueError:
                duration = None
            cases.append(
                CaseResult(
                    test_id=test_id[:1000],
                    status=status,
                    duration_ms=duration,
                    failure_signature=failure_signature(message) if status in ("FAIL", "ERROR") else None,
                    message=message[:4000],
                )
            )
    parsed = ParsedReport(parser, CheckStatus.PASS, cases=cases)
    parsed.discovered = len(cases)
    parsed.passed = sum(1 for case in cases if case.status == "PASS")
    parsed.failed = sum(1 for case in cases if case.status == "FAIL")
    parsed.errors = sum(1 for case in cases if case.status == "ERROR")
    parsed.skipped = sum(1 for case in cases if case.status == "SKIP")
    if parsed.discovered == 0:
        parsed.status = CheckStatus.ZERO_TESTS
        parsed.detail = "report lists no test cases"
        return parsed
    if parsed.skipped == parsed.discovered:
        parsed.status = CheckStatus.ALL_SKIPPED
        parsed.detail = "every test was skipped"
        return parsed
    if parsed.failed or parsed.errors:
        failing_messages = "\n".join(case.message for case in cases if case.status in ("FAIL", "ERROR"))
        missing = _missing_third_party(failing_messages + "\n" + combined, local_modules)
        only_import_errors = all(
            _MISSING_MODULE.search(case.message) for case in cases if case.status in ("FAIL", "ERROR")
        )
        if missing and only_import_errors:
            parsed.status = CheckStatus.BLOCKED_ENVIRONMENT
            parsed.missing_modules = missing
            parsed.detail = "tests could not import third-party modules"
        else:
            parsed.status = CheckStatus.FAIL
        return parsed
    if exit_code not in (0, None):
        parsed.status = CheckStatus.UNPARSABLE
        parsed.detail = f"report shows no failures but pytest exited {exit_code}"
        return parsed
    if parsed.passed < max(1, minimum_tests):
        parsed.status = CheckStatus.ZERO_TESTS
        parsed.detail = f"fewer than {minimum_tests} passing tests were executed"
    return parsed


def _missing_third_party(text: str, local_modules: Sequence[str]) -> List[str]:
    local = _local_modules(local_modules)
    missing = []
    for match in _MISSING_MODULE.finditer(text or ""):
        top = match.group(1).split(".")[0]
        if top not in local and top not in missing:
            missing.append(top)
    return missing


_UNITTEST_LINE = re.compile(r"^(?P<name>\w+) \((?P<cls>[\w.]+)\)(?: \.\.\.)? (?P<result>ok|FAIL|ERROR|skipped.*|expected failure|unexpected success)$")


def parse_unittest(output: str, exit_code: Optional[int], *, minimum_tests: int = 1) -> ParsedReport:
    """Parse ``python -m unittest -v`` output (written to stderr)."""
    parser = "unittest-verbose@1"
    cases: List[CaseResult] = []
    for line in (output or "").splitlines():
        match = _UNITTEST_LINE.match(line.strip())
        if not match:
            continue
        result = match.group("result")
        status = (
            "PASS" if result in ("ok", "expected failure")
            else "SKIP" if result.startswith("skipped")
            else "ERROR" if result == "ERROR"
            else "FAIL"
        )
        cases.append(CaseResult(f"{match.group('cls')}::{match.group('name')}", status, None, None))
    ran = re.search(r"^Ran (\d+) tests?", output or "", re.MULTILINE)
    parsed = ParsedReport(parser, CheckStatus.PASS, cases=cases)
    parsed.discovered = len(cases) if cases else (int(ran.group(1)) if ran else 0)
    parsed.passed = sum(1 for case in cases if case.status == "PASS")
    parsed.failed = sum(1 for case in cases if case.status == "FAIL")
    parsed.errors = sum(1 for case in cases if case.status == "ERROR")
    parsed.skipped = sum(1 for case in cases if case.status == "SKIP")
    if ran is None and not cases:
        parsed.status = CheckStatus.UNPARSABLE
        parsed.detail = "unittest summary not found"
        return parsed
    if parsed.discovered == 0 or "NO TESTS RAN" in (output or ""):
        parsed.status = CheckStatus.ZERO_TESTS
        return parsed
    if parsed.skipped == parsed.discovered:
        parsed.status = CheckStatus.ALL_SKIPPED
        return parsed
    failed_summary = re.search(r"^FAILED \(", output or "", re.MULTILINE)
    if parsed.failed or parsed.errors or failed_summary or exit_code not in (0, None):
        parsed.status = CheckStatus.FAIL
        return parsed
    if parsed.passed < max(1, minimum_tests):
        parsed.status = CheckStatus.ZERO_TESTS
    return parsed


def parse_exit_code(exit_code: Optional[int], *, stdout: str = "") -> ParsedReport:
    """Explicit non-test command semantics (invariants, builds): exit 0 passes."""
    parser = "exit-code@1"
    if exit_code is None:
        return ParsedReport(parser, CheckStatus.UNPARSABLE, detail="no exit code")
    status = CheckStatus.PASS if exit_code == 0 else CheckStatus.FAIL
    detail = ""
    try:
        payload = json.loads(stdout.strip().splitlines()[-1]) if stdout.strip() else None
        if isinstance(payload, dict) and payload.get("errors"):
            detail = json.dumps(payload["errors"])[:2000]
    except (ValueError, IndexError):
        pass
    return ParsedReport(parser, status, detail=detail)


PARSERS = {"pytest-junit@1", "unittest-verbose@1", "exit-code@1"}
