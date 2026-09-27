"""Canonical ``EvaluatorResultV1`` from durable run facts (PRD 6 ResultBuilder)."""
from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional

from harness.contracts.release import (
    EvaluatorResultV1,
    PendingApprovalV1,
    ResultCandidateV1,
    ResultExportV1,
    ResultInputV1,
    ResultTaskV1,
    ResultUsageV1,
    ResultVerificationV1,
)
from harness.release.identity import commit_content_sha256
from harness.release.run_facts import RunFacts, wall_seconds
from harness.release.status_mapping import exit_code


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def verification_summary(facts: RunFacts) -> ResultVerificationV1:
    total = passed = failed = inconclusive = skipped = 0
    for task in facts.tasks:
        counts = (task.decision or {}).get("required_checks") or {}
        total += int(counts.get("total", 0))
        passed += int(counts.get("passed", 0))
        failed += int(counts.get("failed", 0))
        inconclusive += int(counts.get("inconclusive", 0))
        skipped += sum(1 for reason in (task.decision or {}).get("reason_codes", []) if "SKIPPED" in reason or "NOT_RUN" in reason)
    status = "NOT_RUN"
    report_id: Optional[str] = None
    if facts.final is not None and facts.final.aggregate_verification.status != "NOT_RUN":
        status = facts.final.aggregate_verification.status
        report_id = facts.final.aggregate_verification.report_artifact_id
    elif len(facts.tasks) == 1 and facts.tasks[0].decision:
        status = facts.tasks[0].decision.get("status", "NOT_RUN")
        report_id = facts.tasks[0].decision.get("report_artifact_id")
    elif any(task.decision for task in facts.tasks):
        statuses = {task.decision.get("status") for task in facts.tasks if task.decision}
        status = "PASS" if statuses == {"PASS"} else ("FAILED" if "FAILED" in statuses else "UNVERIFIED")
    return ResultVerificationV1(status=status, required_checks=total, passed_checks=passed, failed_checks=failed,
                                skipped_checks=skipped, unavailable_checks=inconclusive, aggregate_report_artifact_id=report_id)


def usage(facts: RunFacts) -> ResultUsageV1:
    budget = facts.budget or {}
    estimated = sorted({"input_tokens", "output_tokens"} if any(
        call["usage_source"] != "provider_reported" for call in facts.model_calls if call["state"] == "SUCCEEDED") else set())
    return ResultUsageV1(
        model_calls=int(budget.get("used_calls", len(facts.model_calls)) or 0),
        input_tokens=int(budget.get("used_input_tokens", 0) or 0),
        output_tokens=int(budget.get("used_output_tokens", 0) or 0),
        wall_seconds=wall_seconds(facts),
        estimated_fields=estimated,
    )


def build_result(facts: RunFacts, *, git=None, request_id: Optional[str] = None, export: Optional[ResultExportV1] = None,
                 status_override: Optional[str] = None, extra_limitations: List[str] = (),
                 reproducibility_manifest_artifact_id: Optional[str] = None,
                 extra_pending: List[Dict[str, Any]] = ()) -> EvaluatorResultV1:
    status = status_override or facts.status
    candidate = None
    if git is not None and facts.candidate.head:
        candidate = ResultCandidateV1(commit=facts.candidate.head, tree=git.commit_tree_of(facts.candidate.head),
                                      content_sha256=commit_content_sha256(git, facts.candidate.head))
    pending = [PendingApprovalV1(**item) for item in [*facts.pending_approvals, *extra_pending]]
    return EvaluatorResultV1(
        request_id=request_id,
        run_id=facts.run_id,
        status=status,
        exit_code=exit_code(status),
        input=ResultInputV1(baseline_commit=facts.source.get("baseline_commit"), baseline_tree=facts.source.get("baseline_tree"),
                            content_sha256=facts.source.get("content_tree_sha256")),
        candidate=candidate,
        tasks=[ResultTaskV1(task_id=task.task_id, status=task.external_status, changed_paths=task.changed_paths[:2000],
                            verification_report_artifact_id=(task.outcome or {}).get("report_artifact_id")) for task in facts.tasks],
        verification=verification_summary(facts),
        usage=usage(facts),
        export=export,
        pending_approvals=pending,
        limitations=list(dict.fromkeys([*facts.limitations, *extra_limitations]))[:50],
        reproducibility_manifest_artifact_id=reproducibility_manifest_artifact_id,
        settled_at=_now(),
    )
