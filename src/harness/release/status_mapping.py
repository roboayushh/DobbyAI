"""Canonical external status and exit-code mapping (PRD 6 sections 6.4 and REL-004).

Every terminal internal status (PRD 2 lifecycle, PRD 4 task outcome, PRD 5 queue
item/queue result, PRD 6 effect) maps to exactly one evaluator status and one
process exit code. The JSON status is authoritative; the exit code is a coarse
automation aid.
"""
from __future__ import annotations

from typing import Dict, Optional

EXIT_CODES: Dict[str, int] = {
    "PASS": 0,
    "COMPLETED_ALL": 0,
    "READY_FOR_REVIEW": 0,
    "PLAN_READY": 0,
    "ACTION_PROPOSED": 0,
    "VERIFICATION_REQUIRED": 0,
    "VALID": 0,
    "INVALID": 2,
    "EXPORT_INVALID": 3,
    "CAPABILITY_DISABLED": 2,
    "PARTIAL_SUCCESS": 3,
    "UNVERIFIED": 3,
    "BLOCKED_ENVIRONMENT": 3,
    "NEEDS_CAPABILITY": 3,
    "NOT_APPLIED": 3,
    "PARTIAL_CLEANUP": 3,
    "FAILED": 4,
    "VERIFICATION_FAILED": 4,
    "INTEGRATION_FAILED": 4,
    "BUDGET_EXHAUSTED": 5,
    "REMAINING_BUDGET": 5,
    "NEEDS_INPUT": 6,
    "NEEDS_APPROVAL": 6,
    "PENDING_APPROVAL": 6,
    "INTEGRATION_UNCERTAIN": 7,
    "ACTION_UNKNOWN": 7,
    "CLEANUP_UNCERTAIN": 7,
    "INTERNAL_ERROR": 7,
    "CANCELLED": 130,
}

# PRD 5 queue item states -> external single-task status.
ITEM_TO_EXTERNAL: Dict[str, str] = {
    "INTEGRATED": "PASS",
    "VERIFIED_NOT_INTEGRATED": "PASS",
    "PASS_VERIFIED": "PASS",
    "FAILED": "FAILED",
    "INTEGRATION_FAILED": "FAILED",
    "UNVERIFIED": "UNVERIFIED",
    "BLOCKED_ENVIRONMENT": "BLOCKED_ENVIRONMENT",
    "BUDGET_EXHAUSTED": "BUDGET_EXHAUSTED",
    "REMAINING_BUDGET": "BUDGET_EXHAUSTED",
    "NEEDS_INPUT": "NEEDS_INPUT",
    "UNSUPPORTED": "NEEDS_INPUT",
    "DUPLICATE": "NEEDS_INPUT",
    "NEEDS_APPROVAL": "PENDING_APPROVAL",
    "CANCELLED": "CANCELLED",
    "SKIPPED": "CANCELLED",
    "BLOCKED_DEPENDENCY": "UNVERIFIED",
    "INTEGRATION_UNCERTAIN": "INTEGRATION_UNCERTAIN",
}

# PRD 2-4 lifecycle states -> external status (runs driven without a queue).
LIFECYCLE_TO_EXTERNAL: Dict[str, str] = {
    "READY_FOR_REVIEW": "PASS",
    "VERIFICATION_FAILED": "FAILED",
    "UNVERIFIED": "UNVERIFIED",
    "BLOCKED_ENVIRONMENT": "BLOCKED_ENVIRONMENT",
    "BUDGET_EXHAUSTED": "BUDGET_EXHAUSTED",
    "NEEDS_INPUT": "NEEDS_INPUT",
    "NEEDS_CAPABILITY": "NEEDS_INPUT",
    "NEEDS_APPROVAL": "PENDING_APPROVAL",
    "FAILED": "FAILED",
    "CANCELLED": "CANCELLED",
    "ACTION_UNKNOWN": "INTEGRATION_UNCERTAIN",
}


def exit_code(status: str) -> int:
    return EXIT_CODES.get(status, 4)


def single_task_status(item_state: str, queue_status: Optional[str] = None) -> str:
    """External status of a one-task run: a PASS item still needs the aggregate gate."""
    status = ITEM_TO_EXTERNAL.get(item_state, "FAILED")
    if status == "PASS" and queue_status not in (None, "COMPLETED_ALL"):
        return {"FAILED": "FAILED", "BLOCKED_ENVIRONMENT": "BLOCKED_ENVIRONMENT"}.get(queue_status, "UNVERIFIED")
    return status
