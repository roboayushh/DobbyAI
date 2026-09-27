"""The only component that can emit PASS (PRD 4 section 14).

Pure function of settled, host-observed evidence. The validator, the model,
the worker, and stdout have no input channel here except the validator's
recorded decision and blocking-finding count, which can only *block* PASS.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from harness.contracts.verification import ContractCriterionV1, TaskOutcome
from harness.verification.comparator import ComparisonResult


@dataclass
class GateInputs:
    candidate_intact: bool
    contract_current: bool
    unsettled_work: bool
    comparison: ComparisonResult
    criteria: Sequence[ContractCriterionV1]
    baseline_required_missing: List[str]
    diff_review_status: str
    validator_required: bool
    validator_ran: bool
    validator_decision: Optional[str]
    validator_blocking_findings: int
    overlay_verdicts: Sequence[str] = ()
    budget_exhausted: bool = False
    cancelled: bool = False
    artifacts_durable: bool = True
    needs_input: bool = False


@dataclass
class GateDecision:
    status: TaskOutcome
    reasons: List[str]
    criteria: List[Dict[str, object]]
    required_total: int
    required_passed: int
    required_failed: int
    required_inconclusive: int
    repairable: bool
    limitations: List[str] = field(default_factory=list)


def evaluate(inputs: GateInputs) -> GateDecision:
    reasons: List[str] = []
    limitations: List[str] = []
    verdicts = inputs.comparison.verdicts
    required = [verdict for verdict in verdicts.values() if verdict.required]
    passed = [v for v in required if v.verdict == "SATISFIED"]
    failed = [v for v in required if v.verdict == "NOT_SATISFIED"]
    blocked = [v for v in required if v.verdict == "BLOCKED_ENVIRONMENT"]
    inconclusive = [v for v in required if v.verdict in ("INCONCLUSIVE", "NOT_RUN")]

    criteria_results: List[Dict[str, object]] = []
    for criterion in inputs.criteria:
        mapped = [verdicts[cid] for cid in criterion.check_ids if cid in verdicts]
        if not mapped:
            status = "INCONCLUSIVE"
        elif any(v.verdict == "NOT_SATISFIED" for v in mapped):
            status = "NOT_SATISFIED"
        elif all(v.verdict == "SATISFIED" for v in mapped):
            status = "SATISFIED"
        elif any(v.verdict == "NOT_RUN" for v in mapped) and not any(v.verdict == "INCONCLUSIVE" for v in mapped):
            status = "NOT_RUN"
        else:
            status = "INCONCLUSIVE"
        criteria_results.append({
            "criterion_id": criterion.criterion_id,
            "status": status,
            "evidence_refs": [v.check_id for v in mapped],
        })

    if not inputs.candidate_intact:
        reasons.append("CANDIDATE_INTEGRITY_FAILURE")
    if not inputs.contract_current:
        reasons.append("CONTRACT_NOT_CURRENT")
    if inputs.unsettled_work:
        reasons.append("UNSETTLED_WORK")
    if not inputs.artifacts_durable:
        reasons.append("REPORT_ARTIFACTS_NOT_DURABLE")
    for verdict in failed:
        reasons.extend(f"{verdict.check_id}:{reason}" for reason in verdict.reasons)
    for verdict in blocked:
        reasons.append(f"{verdict.check_id}:BLOCKED_ENVIRONMENT")
    for verdict in inconclusive:
        reasons.extend(f"{verdict.check_id}:{reason}" for reason in (verdict.reasons or ["INCONCLUSIVE"]))
    if inputs.baseline_required_missing:
        limitations.append("REQUIRED_BASELINE_NOT_CAPTURED:" + ",".join(sorted(inputs.baseline_required_missing)))
    if inputs.diff_review_status == "BLOCKING":
        reasons.append("DIFF_REVIEW_BLOCKING")
    if inputs.validator_required and not inputs.validator_ran:
        reasons.append("VALIDATOR_NOT_RUN")
    if inputs.validator_ran and inputs.validator_blocking_findings > 0:
        reasons.append("VALIDATOR_BLOCKING_FINDING")
    if inputs.validator_ran and inputs.validator_decision == "INSUFFICIENT_EVIDENCE" and not inputs.overlay_verdicts:
        reasons.append("VALIDATOR_INSUFFICIENT_EVIDENCE")
    overlay_failed = [v for v in inputs.overlay_verdicts if v == "NOT_SATISFIED"]
    overlay_inconclusive = [v for v in inputs.overlay_verdicts if v in ("INCONCLUSIVE", "NOT_RUN", "BLOCKED_ENVIRONMENT")]
    if overlay_failed:
        reasons.append("VALIDATOR_OVERLAY_FAILED")
    if overlay_inconclusive:
        reasons.append("VALIDATOR_OVERLAY_INCONCLUSIVE")
    if any(result["status"] != "SATISFIED" for result in criteria_results):
        reasons.append("CRITERIA_NOT_ALL_SATISFIED")
    if not inputs.comparison.targeted_baseline_failures:
        limitations.append("NO_BASELINE_REPRODUCTION_FAILURE")

    conclusive_failure = bool(failed) or bool(overlay_failed) or inputs.diff_review_status == "BLOCKING" or (
        inputs.validator_ran and inputs.validator_blocking_findings > 0
    )
    integrity = not inputs.candidate_intact or not inputs.contract_current or not inputs.artifacts_durable
    if inputs.cancelled:
        status = TaskOutcome.CANCELLED
    elif integrity or inputs.unsettled_work:
        status = TaskOutcome.UNVERIFIED
    elif conclusive_failure:
        status = TaskOutcome.FAILED
    elif blocked:
        status = TaskOutcome.BLOCKED_ENVIRONMENT
    elif inputs.budget_exhausted:
        status = TaskOutcome.BUDGET_EXHAUSTED
    elif inputs.needs_input:
        status = TaskOutcome.NEEDS_INPUT
    elif reasons:
        status = TaskOutcome.UNVERIFIED
    else:
        status = TaskOutcome.PASS
        reasons = ["ALL_REQUIRED_CHECKS_PASSED", "NO_NEW_REGRESSIONS", "VALIDATOR_NO_BLOCKING_FINDINGS"]
    repairable = status == TaskOutcome.FAILED or (
        status == TaskOutcome.UNVERIFIED
        and not integrity
        and not inputs.unsettled_work
        and all(
            any(token in reason for token in ("ZERO_TESTS", "ALL_SKIPPED", "VALIDATOR_OVERLAY", "CRITERIA_NOT_ALL_SATISFIED", "VALIDATOR_INSUFFICIENT_EVIDENCE", "UNEXPECTED_MUTATION", "NOT_RUN"))
            for reason in reasons
        )
    )
    return GateDecision(
        status=status,
        reasons=sorted(set(reasons)),
        criteria=criteria_results,
        required_total=len(required),
        required_passed=len(passed),
        required_failed=len(failed),
        required_inconclusive=len(inconclusive) + len(blocked),
        repairable=repairable,
        limitations=limitations,
    )
