"""Baseline-vs-candidate comparison at test-case granularity (PRD 4 section 9.4).

Targeted checks (focused/relevant) must end fully green except for failures
that already existed on the baseline *and* are not the reproduction of the
task; the broad tier tolerates pre-existing unrelated failures but never a
new regression or lost coverage. Same normalized evidence always produces the
same verdict (NFR4-011).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set

from harness.contracts.verification import CheckStatus, ContractCheckV1

FAILING = {"FAIL", "ERROR"}
INCONCLUSIVE_STATUSES = {
    CheckStatus.TIMEOUT, CheckStatus.OOM, CheckStatus.OUTPUT_LIMIT, CheckStatus.UNPARSABLE,
    CheckStatus.UNEXPECTED_MUTATION, CheckStatus.CANCELLED, CheckStatus.INTERNAL_ERROR,
}


@dataclass
class CheckEvidence:
    check: ContractCheckV1
    status: Optional[CheckStatus]  # None when not run
    cases: Dict[str, str] = field(default_factory=dict)
    signatures: Dict[str, Optional[str]] = field(default_factory=dict)
    baseline_status: Optional[str] = None
    baseline_cases: Optional[Dict[str, str]] = None
    baseline_signatures: Dict[str, Optional[str]] = field(default_factory=dict)
    check_run_id: Optional[str] = None
    baseline_run_id: Optional[str] = None
    not_run_reason: Optional[str] = None
    flaky: bool = False


@dataclass
class CheckVerdict:
    check_id: str
    required: bool
    tier: str
    verdict: str  # SATISFIED, NOT_SATISFIED, INCONCLUSIVE, BLOCKED_ENVIRONMENT, NOT_RUN
    reasons: List[str] = field(default_factory=list)
    resolved_targets: List[str] = field(default_factory=list)
    new_regressions: List[str] = field(default_factory=list)
    pre_existing_unchanged: List[str] = field(default_factory=list)
    changed_failures: List[str] = field(default_factory=list)
    coverage_lost: List[str] = field(default_factory=list)
    candidate_only_passes: List[str] = field(default_factory=list)
    unresolved_targets: List[str] = field(default_factory=list)


@dataclass
class ComparisonResult:
    verdicts: Dict[str, CheckVerdict]
    targeted_baseline_failures: List[str]

    def totals(self) -> Dict[str, int]:
        keys = ("resolved_targets", "new_regressions", "pre_existing_unchanged", "changed_failures", "coverage_lost")
        totals = {key: sum(len(getattr(verdict, key)) for verdict in self.verdicts.values()) for key in keys}
        totals["inconclusive"] = sum(1 for verdict in self.verdicts.values() if verdict.verdict == "INCONCLUSIVE")
        return totals


def compare(evidence: Sequence[CheckEvidence]) -> ComparisonResult:
    targeted: Set[str] = set()
    for item in evidence:
        if item.check.tier in ("focused", "relevant") and item.baseline_cases:
            targeted |= {test for test, status in item.baseline_cases.items() if status in FAILING}
    verdicts: Dict[str, CheckVerdict] = {}
    for item in evidence:
        verdicts[item.check.check_id] = _verdict(item, targeted)
    return ComparisonResult(verdicts, sorted(targeted))


def _verdict(item: CheckEvidence, targeted: Set[str]) -> CheckVerdict:
    check = item.check
    verdict = CheckVerdict(check.check_id, check.required, check.tier, "SATISFIED")
    if item.status is None:
        verdict.verdict = "NOT_RUN"
        verdict.reasons.append(item.not_run_reason or "NOT_RUN")
        return verdict
    if item.flaky:
        verdict.verdict = "INCONCLUSIVE"
        verdict.reasons.append("FLAKY")
        return verdict
    if item.status == CheckStatus.BLOCKED_ENVIRONMENT:
        verdict.verdict = "BLOCKED_ENVIRONMENT"
        verdict.reasons.append("BLOCKED_ENVIRONMENT")
        return verdict
    if item.status in INCONCLUSIVE_STATUSES:
        verdict.verdict = "INCONCLUSIVE"
        verdict.reasons.append(item.status.value)
        return verdict
    if item.status in (CheckStatus.ZERO_TESTS, CheckStatus.ALL_SKIPPED):
        verdict.verdict = "INCONCLUSIVE"
        verdict.reasons.append(item.status.value)
        return verdict
    if check.kind != "test":
        if item.status != CheckStatus.PASS:
            verdict.verdict = "NOT_SATISFIED"
            verdict.reasons.append(f"{check.kind.upper()}_FAILED")
        return verdict

    baseline = item.baseline_cases
    candidate = item.cases
    for test_id in sorted(set(candidate) | set(baseline or {})):
        before = (baseline or {}).get(test_id)
        after = candidate.get(test_id)
        if baseline is None:
            if after == "PASS":
                verdict.candidate_only_passes.append(test_id)
            elif after in FAILING:
                verdict.new_regressions.append(test_id)
            continue
        if before in FAILING and after == "PASS":
            verdict.resolved_targets.append(test_id)
        elif before == "PASS" and after in FAILING:
            verdict.new_regressions.append(test_id)
        elif before in FAILING and after in FAILING:
            if item.baseline_signatures.get(test_id) and item.baseline_signatures.get(test_id) == item.signatures.get(test_id):
                verdict.pre_existing_unchanged.append(test_id)
            else:
                verdict.changed_failures.append(test_id)
        elif before == "PASS" and (after is None or after == "SKIP"):
            verdict.coverage_lost.append(test_id)
        elif before is None and after == "PASS":
            verdict.candidate_only_passes.append(test_id)
        elif before is None and after in FAILING:
            verdict.new_regressions.append(test_id)
    still_failing = [test for test, status in candidate.items() if status in FAILING]
    if verdict.new_regressions:
        verdict.verdict = "NOT_SATISFIED"
        verdict.reasons.append("NEW_REGRESSION")
    if verdict.coverage_lost:
        verdict.verdict = "NOT_SATISFIED"
        verdict.reasons.append("COVERAGE_LOST")
    if check.tier == "focused" and still_failing:
        verdict.verdict = "NOT_SATISFIED"
        verdict.unresolved_targets = sorted(still_failing)
        verdict.reasons.append("FOCUSED_TEST_FAILING")
    elif check.tier == "relevant":
        unresolved = sorted(test for test in still_failing if test in targeted or test not in (baseline or {}))
        if unresolved:
            verdict.verdict = "NOT_SATISFIED"
            verdict.unresolved_targets = unresolved
            verdict.reasons.append("UNRESOLVED_TARGET")
    elif check.tier == "broad":
        unresolved = sorted(test for test in still_failing if test in targeted)
        if unresolved:
            verdict.verdict = "NOT_SATISFIED"
            verdict.unresolved_targets = unresolved
            verdict.reasons.append("UNRESOLVED_TARGET")
    if item.status == CheckStatus.FAIL and baseline is None and still_failing:
        verdict.verdict = "NOT_SATISFIED"
        verdict.reasons.append("FAILING_WITHOUT_BASELINE")
    verdict.reasons = sorted(set(verdict.reasons))
    return verdict
