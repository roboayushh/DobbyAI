"""One read-only view of a settled (or paused) run, shared by result, report, and export.

Everything here is read from durable host state; nothing trusts model narrative.
The candidate choice rule (PRD 6 section 8.2): the final verified candidate
(PRD 5 integration head, or the evaluation case candidate), otherwise the
truthful best partial candidate of a single-task run, otherwise the baseline
itself (an empty patch).
"""
from __future__ import annotations

import datetime
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from harness.release.status_mapping import LIFECYCLE_TO_EXTERNAL, ITEM_TO_EXTERNAL, single_task_status

LIMITATION_PASS = "PASS describes declared observed checks and does not guarantee hidden-test success."


@dataclass
class CandidateChoice:
    base: str
    head: str
    kind: str  # INTEGRATED | CASE_CANDIDATE | BEST_PARTIAL | EMPTY
    task_id: Optional[str] = None
    verified: bool = False


@dataclass
class TaskFacts:
    task_id: str
    ordinal: int
    title: str
    source_key: str
    item_state: Optional[str]
    external_status: str
    reasons: List[str]
    outcome: Optional[Dict[str, Any]]
    decision: Optional[Dict[str, Any]]
    candidate: Optional[Dict[str, Any]]
    changed_paths: List[str]
    commit: Optional[str]


@dataclass
class RunFacts:
    run: Dict[str, Any]
    source: Dict[str, Any]
    queue: Optional[Dict[str, Any]]
    final: Any
    report: Dict[str, Any]
    lifecycle: Optional[Dict[str, Any]]
    budget: Optional[Dict[str, Any]]
    model_calls: List[Dict[str, Any]]
    tasks: List[TaskFacts]
    candidate: CandidateChoice
    status: str
    pending_approvals: List[Dict[str, Any]] = field(default_factory=list)
    limitations: List[str] = field(default_factory=list)

    @property
    def run_id(self) -> str:
        return self.run["run_id"]


def _rows(conn, sql: str, params: tuple) -> List[Dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def gather(run_id: str, *, run_store, artifact_store, services, coordinator) -> RunFacts:
    workspaces, verifier = services.workspaces, services.verifier
    run = run_store.get_run(run_id)
    if not run:
        raise KeyError(f"Run not found: {run_id}")
    source = run_store.get_source_snapshot(run_id) or {}
    queue = coordinator.queue(run_id)
    final = coordinator.final_result(run_id)
    report: Dict[str, Any] = {}
    if final is not None:
        artifact = artifact_store.get_artifact_by_id(final.queue_report_artifact_id)
        if artifact:
            try:
                report = json.loads((artifact_store.data_root / artifact["relative_path"]).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                report = {}
    with run_store.get_connection() as conn:
        lifecycle_row = conn.execute("SELECT * FROM h_run_lifecycle WHERE run_id = ?", (run_id,)).fetchone()
        budget_row = conn.execute("SELECT * FROM h_budget_ledgers WHERE run_id = ?", (run_id,)).fetchone()
        calls = _rows(conn, "SELECT role, state, input_tokens, output_tokens, usage_source, error_code FROM h_model_calls WHERE run_id = ? ORDER BY created_at", (run_id,))
        pending_actions = _rows(conn, "SELECT approval_request_id, action_id, task_id FROM h_action_approval_requests WHERE run_id = ? AND state = 'PENDING'", (run_id,))
        pending_caps = _rows(conn, "SELECT capability_request_id, operation FROM h_capability_requests WHERE run_id = ? AND state = 'PENDING'", (run_id,))
    items = {item["task_id"]: item for item in (coordinator.items(queue) if queue else [])}
    git = workspaces.git(run_id) if source else None
    baseline = source.get("baseline_commit")
    tasks: List[TaskFacts] = []
    for row in run_store.get_tasks(run_id):
        spec = json.loads(row["task_spec_json"])
        task_id = row["task_id"]
        item = items.get(task_id)
        outcome = verifier.task_outcome(task_id) if verifier else None
        decision = None
        if outcome and outcome.get("completion_decision_id"):
            with run_store.get_connection() as conn:
                found = conn.execute("SELECT decision_json FROM h_completion_decisions WHERE completion_decision_id = ?",
                                     (outcome["completion_decision_id"],)).fetchone()
            decision = json.loads(found["decision_json"]) if found else None
        candidate = None
        if outcome and outcome.get("accepted_candidate_id"):
            candidate = workspaces.get_candidate(outcome["accepted_candidate_id"])
        elif outcome and outcome.get("best_partial_candidate_id"):
            candidate = workspaces.get_candidate(outcome["best_partial_candidate_id"])
        else:
            candidate = workspaces.latest_candidate(run_id, task_id)
        changed: List[str] = []
        if candidate and git is not None:
            try:
                changed = [path for _, path in git.diff_paths(candidate["task_start_commit"], candidate["candidate_commit"])]
            except Exception:
                changed = []
        if item is not None:
            state = item["state"]
            external = ITEM_TO_EXTERNAL.get(state, "UNVERIFIED")
            reasons = json.loads(item["reason_codes_json"] or "[]")
        elif lifecycle_row is not None and lifecycle_row["active_task_id"] == task_id:
            state = lifecycle_row["state"]
            external = LIFECYCLE_TO_EXTERNAL.get(state, "UNVERIFIED")
            reasons = [lifecycle_row["stop_reason_code"]] if lifecycle_row["stop_reason_code"] else []
        else:
            state, external, reasons = None, "NOT_STARTED", []
        commit = None
        if state == "INTEGRATED" and final is not None:
            commit = next((entry.commit for entry in final.task_results if entry.task_id == task_id), None)
        elif candidate:
            commit = candidate["candidate_commit"]
        tasks.append(TaskFacts(task_id, row["ordinal"], spec.get("title", ""), spec.get("source_key", ""), state, external,
                               reasons, outcome, decision, candidate, changed, commit))

    choice = _choose_candidate(run, baseline, final, tasks)
    status = _status(run, queue, final, lifecycle_row, tasks)
    limitations = [LIMITATION_PASS]
    for task in tasks:
        for reason in (task.decision or {}).get("reason_codes", []):
            if reason.startswith(("NO_BASELINE", "REQUIRED_BASELINE")):
                limitations.append(f"{task.task_id}: {reason}")
    if choice.kind == "BEST_PARTIAL":
        limitations.append("The exported patch is the best UNVERIFIED/FAILED attempt, not a passing candidate.")
    if source.get("dirty_source_imported"):
        limitations.append("The baseline includes pre-existing uncommitted changes from the original checkout (synthetic baseline).")
    if any(call["usage_source"] != "provider_reported" for call in calls if call["state"] == "SUCCEEDED"):
        limitations.append("Some token usage is estimated because the provider did not report it.")
    pending = [{"capability_request_id": row["approval_request_id"], "operation": "SANDBOX_ACTION",
                "summary": f"Action {row['action_id']} for task {row['task_id']} awaits a one-use approval"} for row in pending_actions]
    pending += [{"capability_request_id": row["capability_request_id"], "operation": row["operation"],
                 "summary": f"{row['operation']} awaits an exact approval grant"} for row in pending_caps]
    return RunFacts(run=dict(run), source=dict(source), queue=queue, final=final, report=report,
                    lifecycle=dict(lifecycle_row) if lifecycle_row else None, budget=dict(budget_row) if budget_row else None,
                    model_calls=calls, tasks=tasks, candidate=choice, status=status,
                    pending_approvals=pending, limitations=limitations)


def _choose_candidate(run, baseline, final, tasks: List[TaskFacts]) -> CandidateChoice:
    if not baseline:
        return CandidateChoice(base="", head="", kind="EMPTY")
    if final is not None and final.final_integration.commit != baseline:
        return CandidateChoice(baseline, final.final_integration.commit, "INTEGRATED", verified=final.aggregate_verification.status == "PASS")
    passing = [t for t in tasks if t.item_state == "VERIFIED_NOT_INTEGRATED" and t.candidate]
    if len(passing) == 1 and passing[0].candidate["task_start_commit"] == baseline:
        return CandidateChoice(baseline, passing[0].candidate["candidate_commit"], "CASE_CANDIDATE", passing[0].task_id, verified=True)
    with_candidates = [t for t in tasks if t.candidate and t.candidate["task_start_commit"] == baseline]
    if len(tasks) == 1 and with_candidates:
        task = with_candidates[0]
        verified = (task.outcome or {}).get("status") == "PASS"
        return CandidateChoice(baseline, task.candidate["candidate_commit"], "BEST_PARTIAL" if not verified else "CASE_CANDIDATE",
                               task.task_id, verified=verified)
    return CandidateChoice(baseline, baseline, "EMPTY")


def _status(run, queue, final, lifecycle_row, tasks: List[TaskFacts]) -> str:
    single = len(tasks) == 1
    if final is not None:
        if single:
            return single_task_status(tasks[0].item_state or "FAILED", final.status)
        return final.status
    if queue is not None:
        if any(t.item_state == "NEEDS_APPROVAL" for t in tasks):
            return "PENDING_APPROVAL"
        if queue["state"] == "UNCERTAIN":
            return "INTEGRATION_UNCERTAIN"
        return "CANCELLED" if queue["cancel_requested"] else "UNVERIFIED"
    if lifecycle_row is not None:
        return LIFECYCLE_TO_EXTERNAL.get(lifecycle_row["state"], "UNVERIFIED")
    return "UNVERIFIED"


def wall_seconds(facts: RunFacts) -> int:
    try:
        start = datetime.datetime.fromisoformat(facts.run["created_at"])
        end_text = facts.final.settled_at if facts.final is not None else facts.run["updated_at"]
        end = datetime.datetime.fromisoformat(end_text)
        return max(0, int((end - start).total_seconds()))
    except (TypeError, ValueError):
        return 0
