"""PRD 4 unit tests: result parsing, regression comparison, completion gate, diff scope."""
from __future__ import annotations

from typing import Dict, Optional

import pytest

from harness.contracts.verification import CheckOrigin, CheckStatus, ContractCheckV1, ContractCriterionV1, TaskOutcome
from harness.verification.comparator import CheckEvidence, compare
from harness.verification.completion_gate import GateInputs, evaluate
from harness.verification.diff_review import DiffScopeReviewer
from harness.verification.parsers import failure_signature, parse_pytest_junit


def junit(cases: str) -> bytes:
    return f'<?xml version="1.0"?><testsuites><testsuite name="pytest">{cases}</testsuite></testsuites>'.encode()


# ---------------------------------------------------------------- parsers
def test_junit_parses_pass_fail_skip() -> None:
    report = junit(
        '<testcase classname="tests.test_a" name="test_ok" time="0.01"/>'
        '<testcase classname="tests.test_a" name="test_bad"><failure message="assert 1 == 2">E assert 1 == 2</failure></testcase>'
        '<testcase classname="tests.test_a" name="test_skip"><skipped message="later"/></testcase>'
    )
    parsed = parse_pytest_junit(report, 1)
    assert parsed.status == CheckStatus.FAIL
    assert (parsed.discovered, parsed.passed, parsed.failed, parsed.skipped) == (3, 1, 1, 1)
    assert parsed.failing_ids() == ["tests.test_a::test_bad"]


@pytest.mark.parametrize("payload", [
    b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><testsuite><testcase name="t">&a;</testcase></testsuite>',
    b'<testsuite><testcase name="t"><failure>' + b"<!ENTITY x SYSTEM 'file:///etc/passwd'>" + b"</failure></testcase></testsuite>",
    b"<testsuite><testcase name='t'>",
    b"<html><body>not junit</body></html>",
])
def test_hostile_or_malformed_reports_are_unparsable(payload: bytes) -> None:
    assert parse_pytest_junit(payload, 0).status == CheckStatus.UNPARSABLE


def test_green_report_with_nonzero_exit_is_not_a_pass() -> None:
    parsed = parse_pytest_junit(junit('<testcase classname="t" name="a"/>'), 2)
    assert parsed.status == CheckStatus.UNPARSABLE


def test_zero_and_all_skipped_are_not_passes() -> None:
    assert parse_pytest_junit(junit(""), 5).status == CheckStatus.ZERO_TESTS
    assert parse_pytest_junit(None, 5).status == CheckStatus.ZERO_TESTS
    skipped = junit('<testcase classname="t" name="a"><skipped/></testcase>')
    assert parse_pytest_junit(skipped, 0).status == CheckStatus.ALL_SKIPPED


def test_missing_third_party_module_is_blocked_environment_but_local_is_failure() -> None:
    report = junit('<testcase classname="t" name="a"><error message="ModuleNotFoundError: No module named \'numpy\'">E ModuleNotFoundError: No module named \'numpy\'</error></testcase>')
    assert parse_pytest_junit(report, 2, local_modules=["src"]).status == CheckStatus.BLOCKED_ENVIRONMENT
    local = junit('<testcase classname="t" name="a"><error message="ModuleNotFoundError: No module named \'src.missing\'">E ModuleNotFoundError: No module named \'src.missing\'</error></testcase>')
    assert parse_pytest_junit(local, 2, local_modules=["src"]).status == CheckStatus.FAIL


def test_failure_signature_ignores_addresses_and_line_numbers() -> None:
    a = failure_signature("obj at 0x7f00aa\nfile.py:12: AssertionError")
    b = failure_signature("obj at 0x7f99bb\nfile.py:98: AssertionError")
    assert a == b
    assert a != failure_signature("file.py:12: KeyError")


# ------------------------------------------------------------- comparator
def check(check_id: str, tier: str = "focused", required: bool = True, kind: str = "test") -> ContractCheckV1:
    return ContractCheckV1(
        check_id=check_id, origin=CheckOrigin.RUNTIME_ADAPTER, kind=kind, tier=tier, required=required,
        baseline_policy="required", argv=["python", "-m", "pytest"], timeout_seconds=60, parser="pytest-junit@1",
    )


def evidence(item: ContractCheckV1, status: CheckStatus, cases: Dict[str, str], baseline: Optional[Dict[str, str]]) -> CheckEvidence:
    return CheckEvidence(item, status, cases=cases, baseline_cases=baseline, baseline_status="FAIL" if baseline else None,
                         signatures={k: "s" for k, v in cases.items() if v != "PASS"},
                         baseline_signatures={k: "s" for k, v in (baseline or {}).items() if v != "PASS"})


def test_pre_existing_unrelated_failure_does_not_block_resolved_target() -> None:
    focused, broad = check("focused"), check("broad", tier="broad")
    result = compare([
        evidence(focused, CheckStatus.PASS, {"t::target": "PASS"}, {"t::target": "FAIL"}),
        evidence(broad, CheckStatus.FAIL, {"t::target": "PASS", "u::old": "FAIL"}, {"t::target": "FAIL", "u::old": "FAIL"}),
    ])
    assert result.verdicts["focused"].verdict == "SATISFIED"
    assert result.verdicts["broad"].verdict == "SATISFIED"
    assert result.verdicts["broad"].pre_existing_unchanged == ["u::old"]
    assert "t::target" in result.targeted_baseline_failures


def test_new_regression_is_not_satisfied() -> None:
    broad = check("broad", tier="broad")
    result = compare([evidence(broad, CheckStatus.FAIL, {"a": "PASS", "b": "FAIL"}, {"a": "PASS", "b": "PASS"})])
    assert result.verdicts["broad"].verdict == "NOT_SATISFIED"
    assert result.verdicts["broad"].new_regressions == ["b"]


def test_unresolved_target_is_not_satisfied() -> None:
    focused = check("focused")
    result = compare([evidence(focused, CheckStatus.FAIL, {"t": "FAIL"}, {"t": "FAIL"})])
    assert result.verdicts["focused"].verdict == "NOT_SATISFIED"


def test_not_run_and_flaky_are_inconclusive_never_pass() -> None:
    a, b = check("a"), check("b")
    result = compare([CheckEvidence(a, None, not_run_reason="BUDGET"), CheckEvidence(b, CheckStatus.PASS, cases={"x": "PASS"}, flaky=True)])
    assert result.verdicts["a"].verdict == "NOT_RUN"
    assert result.verdicts["b"].verdict == "INCONCLUSIVE"


# ---------------------------------------------------------- completion gate
def gate(**overrides) -> GateInputs:
    focused = check("focused")
    comparison = compare([evidence(focused, CheckStatus.PASS, {"t": "PASS"}, {"t": "FAIL"})])
    values = dict(
        candidate_intact=True, contract_current=True, unsettled_work=False, comparison=comparison,
        criteria=[ContractCriterionV1(criterion_id="c1", statement="fixed", check_ids=["focused"])],
        baseline_required_missing=[], diff_review_status="CLEAN", validator_required=True, validator_ran=True,
        validator_decision="NO_OBJECTION", validator_blocking_findings=0,
    )
    values.update(overrides)
    return GateInputs(**values)


def test_gate_passes_only_with_complete_evidence() -> None:
    assert evaluate(gate()).status == TaskOutcome.PASS


@pytest.mark.parametrize("overrides, expected", [
    ({"candidate_intact": False}, TaskOutcome.UNVERIFIED),
    ({"contract_current": False}, TaskOutcome.UNVERIFIED),
    ({"unsettled_work": True}, TaskOutcome.UNVERIFIED),
    ({"validator_ran": False}, TaskOutcome.UNVERIFIED),
    ({"validator_blocking_findings": 1}, TaskOutcome.FAILED),
    ({"diff_review_status": "BLOCKING"}, TaskOutcome.FAILED),
    ({"overlay_verdicts": ["NOT_SATISFIED"]}, TaskOutcome.FAILED),
    ({"artifacts_durable": False}, TaskOutcome.UNVERIFIED),
    ({"cancelled": True}, TaskOutcome.CANCELLED),
    ({"validator_decision": "INSUFFICIENT_EVIDENCE"}, TaskOutcome.UNVERIFIED),
])
def test_gate_false_pass_matrix(overrides, expected) -> None:
    decision = evaluate(gate(**overrides))
    assert decision.status == expected
    assert decision.status != TaskOutcome.PASS


def test_gate_unmapped_criterion_blocks_pass() -> None:
    decision = evaluate(gate(criteria=[ContractCriterionV1(criterion_id="c9", statement="x", check_ids=["missing"])]))
    assert decision.status == TaskOutcome.UNVERIFIED
    assert "CRITERIA_NOT_ALL_SATISFIED" in decision.reasons


def test_gate_failed_required_check_is_repairable_failure() -> None:
    focused = check("focused")
    comparison = compare([evidence(focused, CheckStatus.FAIL, {"t": "FAIL"}, {"t": "FAIL"})])
    decision = evaluate(gate(comparison=comparison))
    assert decision.status == TaskOutcome.FAILED and decision.repairable


# ------------------------------------------------------------- diff scope
def test_diff_review_blocks_test_tampering() -> None:
    diff = (
        "diff --git a/tests/test_a.py b/tests/test_a.py\n--- a/tests/test_a.py\n+++ b/tests/test_a.py\n"
        "@@ -1,3 +1,4 @@\n+import pytest\n+@pytest.mark.skip\n def test_a():\n-    assert f() == 1\n+    pass\n"
        "diff --git a/tests/test_b.py b/tests/test_b.py\ndeleted file mode 100644\n--- a/tests/test_b.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-def test_b(): assert True\n"
    )
    review = DiffScopeReviewer().review(diff, baseline_tests=["tests/test_a.py", "tests/test_b.py"], plan_paths=["src/a.py"])
    categories = {finding.category for finding in review.findings}
    assert review.status == "BLOCKING"
    assert {"skip_marker_added", "existing_test_deleted"} <= categories


def test_diff_review_clean_source_fix() -> None:
    diff = "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-    return a - b\n+    return a + b\n"
    review = DiffScopeReviewer().review(diff, baseline_tests=["tests/test_a.py"], plan_paths=["src/a.py"])
    assert review.status == "CLEAN"


def test_diff_review_blocks_always_true_assertion_swap() -> None:
    diff = (
        "diff --git a/tests/test_a.py b/tests/test_a.py\n--- a/tests/test_a.py\n+++ b/tests/test_a.py\n"
        "@@ -1,2 +1,2 @@\n def test_a():\n-    assert add(2, 3) == 5\n+    assert True\n"
    )
    review = DiffScopeReviewer().review(diff, baseline_tests=["tests/test_a.py"], plan_paths=["src/a.py"])
    categories = {finding.category for finding in review.findings}
    assert review.status == "BLOCKING"
    assert {"trivial_assertion_added", "assertions_weakened_suspected"} <= categories
