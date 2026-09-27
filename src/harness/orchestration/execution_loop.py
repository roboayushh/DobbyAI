"""PRD 3-4 execution loop for the deterministic controller.

The model never controls this loop. Each iteration reads the durable
lifecycle state, performs exactly one host-owned step, and persists the next
state before continuing, so a crash at any point resumes from durable facts:

    PREPARED -> INDEXING (task workspace at its start commit, index at that tree)
    -> PLANNING -> PLAN_READY (PRD 4 contract + baseline before any mutation)
    -> CODING <-> ACTION_PROPOSED -> ACTION_EXECUTING (sandbox; settle; re-index)
    -> VERIFICATION_REQUIRED (frozen one-commit candidate)
    -> VERIFYING -> READY_FOR_REVIEW | REPAIRING -> CODING | truthful non-PASS state
"""
from __future__ import annotations

import datetime
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from harness.contracts import (
    ActionReferenceV1,
    CandidateReferenceV1,
    CoderCompleteV1,
    CoderDecisionV1,
    CoderReplanV1,
    EvidenceQueryV1,
    NeedCapabilityV1,
    OrchestrationPhaseResultV1,
    OrchestrationState,
    PhaseUsageV1,
    PlanReferenceV1,
    ProposalReferenceV1,
    Role,
    ValidatorReviewV1,
    VerificationReferenceV1,
)
from harness.contracts.execution import (
    HandoffAcceptanceContractV1,
    HandoffBaselineV1,
    HandoffCandidateV1,
    HandoffEnvironmentV1,
    HandoffRemainingBudgetV1,
    PermissionProfile,
    VerificationCandidateHandoffV1,
)
from harness.execution import (
    ActionService,
    ActionUnknownError,
    ExecutionBudgetExhaustedError,
    ExecutionBudgetLedger,
    ExecutionBudgetLimits,
)
from harness.execution.feedback import ExecutionFeedbackService
from harness.orchestration.budget_ledger import BudgetExhaustedError
from harness.orchestration.handoff import VerifiedHandoff
from harness.orchestration.lifecycle import TASK_TERMINAL_STATES, LifecycleSnapshot
from harness.orchestration.store import ProposalRecord
from harness.persistence import canonical_json
from harness.policy import PolicyEngine
from harness.sandbox import RuntimeImageInvalidError, SandboxError, SandboxUnavailableError
from harness.workspace.task_workspace import TaskWorkspaceService, WorkspaceLockService

S = OrchestrationState
EXECUTION_BOUNDARIES = {"verification-required", "complete"}
RETURN_STATES = TASK_TERMINAL_STATES | {S.CANCELLED, S.QUEUE_SETTLED}
# The model never produced a usable plan or decision within its bounded retries. The task
# FAILS with this typed stop reason (the queue settles it and moves on); any other
# exception is a harness fault and still surfaces as INTERNAL_ERROR.
MODEL_BEHAVIOUR_FAILURES = frozenset({
    "PLANNER_EVIDENCE_ROUND_LIMIT", "PLANNER_DECISION_INVALID", "PLAN_REVISION_INVALID",
    "REPLAN_LIMIT_EXCEEDED", "ROLE_SCHEMA_RETRY_EXHAUSTED", "CODER_DECISION_INVALID",
    "CODER_TASK_MISMATCH", "CODER_TASK_REVISION_STALE", "CODER_PLAN_REVISION_STALE",
    "STALE_WORKSPACE_VERSION", "EXECUTION_LOOP_BOUND",
})


@dataclass
class ExecutionServices:
    workspaces: TaskWorkspaceService
    actions: ActionService
    policy: PolicyEngine
    budget_limits: ExecutionBudgetLimits = field(default_factory=ExecutionBudgetLimits)
    verifier: Any = None  # PRD 4 VerificationService (optional)
    permission_profile: PermissionProfile = PermissionProfile.SANDBOX
    max_coder_turns_per_task: int = 30
    max_consecutive_rejections: int = 3
    dependencies: Any = None
    history_limit: int = 4


def _utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class ExecutionLoopMixin:
    """Mixed into OrchestrationController; relies on its PRD 2 services."""

    execution: Optional[ExecutionServices]

    # ------------------------------------------------------------ entry
    def _continue_execution(self, run_id: str, stop_at: str) -> OrchestrationPhaseResultV1:
        ex = self.execution
        if ex is None:
            from harness.orchestration.controller import OrchestrationError

            raise OrchestrationError(
                "EXECUTION_NOT_CONFIGURED",
                "Execution boundaries require the PRD 3 sandbox services to be configured",
            )
        try:
            handoff = self.handoff_verifier.verify(run_id)
        except Exception as exc:
            if self.run_store.get_run(run_id):
                try:
                    self.run_store.append_event(
                        run_id,
                        "ORCHESTRATION_HANDOFF_REJECTED",
                        {"error_code": getattr(exc, "code", "PREPARED_HANDOFF_INTEGRITY_FAILURE"), "message": str(exc)[:1000]},
                    )
                except Exception:
                    pass
            raise
        self.lifecycle.initialize(run_id)
        lease = self.leases.acquire(run_id, self.owner_id, ttl_seconds=300)
        self._current_lease = lease
        started = time.monotonic()
        self._evidence_cache: Dict[str, Any] = {}
        try:
            resolved = self.profile_resolver.resolve(self.profile_id)
            adapter = self._adapter(resolved)
            self.model_configs.freeze(run_id, resolved, adapter_name=adapter.name, adapter_version=adapter.version)
            self.budgets.initialize(run_id, self._effective_model_limits(resolved))
            self._reconcile_model_calls(run_id)
            ex.policy.ensure_snapshot(run_id, profile=ex.permission_profile)
            ex.actions.budgets.initialize(run_id, len(handoff.task_ids), ex.budget_limits)
            return self._execution_loop(run_id, stop_at, handoff, resolved, adapter, started)
        except (BudgetExhaustedError, ExecutionBudgetExhaustedError) as exc:
            self._move_to_budget_exhausted(run_id)
            return self._execution_result(run_id, handoff, started)
        except (SandboxUnavailableError, RuntimeImageInvalidError) as exc:
            self._move_to_blocked_environment(run_id, getattr(exc, "code", "SANDBOX_UNAVAILABLE"))
            return self._execution_result(run_id, handoff, started)
        except KeyboardInterrupt:
            self._move_to_cancelled(run_id)
            raise
        except Exception as exc:
            code = getattr(exc, "code", "ORCHESTRATION_FAILED")
            self._move_to_failed(run_id, code)
            if code in MODEL_BEHAVIOUR_FAILURES and self.lifecycle.get(run_id).state == OrchestrationState.FAILED:
                return self._execution_result(run_id, handoff, started)
            raise
        finally:
            try:
                self._assert_workspace_unchanged(handoff)
            finally:
                self._current_lease = None
                self.leases.release(lease)

    def _effective_model_limits(self, resolved: Any) -> Any:
        return replace(
            self.budget_limits,
            reserved_future_output_tokens=min(
                max(
                    self.budget_limits.reserved_future_output_tokens,
                    self.budget_limits.reserved_future_calls * resolved.contract.max_output_tokens,
                ),
                self.budget_limits.max_output_tokens // 2,
            ),
            reserved_future_wall_seconds=min(
                max(
                    self.budget_limits.reserved_future_wall_seconds,
                    self.budget_limits.reserved_future_calls * resolved.contract.request_timeout_seconds,
                ),
                self.budget_limits.max_wall_seconds // 2,
            ),
        )

    # ------------------------------------------------------------- loop
    def _execution_loop(
        self,
        run_id: str,
        stop_at: str,
        handoff: VerifiedHandoff,
        resolved: Any,
        adapter: Any,
        started: float,
    ) -> OrchestrationPhaseResultV1:
        ex = self.execution
        assert ex is not None
        plan_record = None
        for _ in range(10_000):
            if self._current_lease is not None:
                self._current_lease = self.leases.renew(self._current_lease, ttl_seconds=300)
            snap = self.lifecycle.get(run_id)
            task_id, state = snap.active_task_id, snap.state

            if state in RETURN_STATES and state != S.BLOCKED_ENVIRONMENT:
                return self._execution_result(run_id, handoff, started)
            if state == S.PREPARED:
                self._transition(run_id, snap, S.INDEXING, "INDEXING_STARTED")
                continue
            if state == S.INDEXING:
                version = self._ensure_task_workspace(run_id, task_id, handoff)
                self._task_dependencies(run_id, task_id, version.commit)
                reuse = handoff.source_revision if version.commit != handoff.source_revision else None
                index = self.indexer.build(run_id, version.commit, reuse_from_revision=reuse)
                self._transition(
                    run_id, snap, S.PLANNING, "INDEX_READY",
                    {
                        "indexed_files": index.indexed_files,
                        "excluded_files": index.excluded_files,
                        "symbols": index.symbol_count,
                        "reused": index.reused,
                        "source_revision": version.commit,
                    },
                )
                continue

            version = ex.workspaces.current_version(run_id, task_id)
            revision = version.commit
            task_handoff = replace(handoff, source_revision=revision)

            if state == S.BLOCKED_ENVIRONMENT:
                if not self._resume_blocked(run_id, snap):
                    return self._execution_result(run_id, handoff, started)
                continue

            if state == S.PLANNING:
                replan_count = self._replan_count(run_id, task_id)
                existing = self._safe_active_plan(task_id)
                if existing and existing.plan.plan_revision > replan_count:
                    plan_record = existing
                else:
                    plan_record, stop = self._planning_phase(
                        run_id=run_id,
                        snapshot=snap,
                        handoff=task_handoff,
                        resolved=resolved,
                        adapter=adapter,
                        evidence=self._evidence(run_id, task_id, revision),
                        previous_plan=existing,
                        cycle=replan_count,
                    )
                    if stop is not None:
                        return self._execution_result(run_id, handoff, started)
                snap = self.lifecycle.get(run_id)
                if snap.state == S.PLANNING:
                    self._transition(
                        run_id, snap, S.PLAN_READY, "PLAN_READY",
                        {"plan_id": plan_record.plan_id, "plan_revision": plan_record.plan.plan_revision},
                    )
                continue

            if state == S.PLAN_READY:
                plan_record = plan_record or self.records.get_active_plan(task_id)
                if ex.verifier is not None:
                    try:
                        ex.verifier.ensure_contract(run_id, task_id, plan_record=plan_record, version=version)
                    except (SandboxUnavailableError, RuntimeImageInvalidError) as exc:
                        self._transition(run_id, snap, S.BLOCKED_ENVIRONMENT, "VERIFICATION_ENVIRONMENT_BLOCKED",
                                         {"resume_state": "PLAN_READY"}, stop_reason=f"{getattr(exc, 'code', 'SANDBOX_UNAVAILABLE')}@PLAN_READY")
                        return self._execution_result(run_id, handoff, started)
                self._transition(run_id, snap, S.CODING, "CODING_STARTED")
                continue

            if state == S.CODING:
                plan_record = self.records.get_active_plan(task_id)
                if self._unexecuted_proposal(run_id, task_id):
                    self._transition(run_id, snap, S.ACTION_PROPOSED, "ACTION_PROPOSAL_RECONCILED")
                    continue
                pending = self._pending_candidate(run_id, task_id, version.version_id)
                if pending:
                    self._transition(run_id, snap, S.VERIFICATION_REQUIRED, "VERIFICATION_REQUIRED",
                                     {"candidate_id": pending["candidate_id"], "reconciled": True})
                    continue
                outcome = self._coder_turn(run_id, snap, task_handoff, resolved, adapter, plan_record, version)
                if outcome == "return":
                    return self._execution_result(run_id, handoff, started)
                continue

            if state == S.ACTION_PROPOSED:
                proposal = self._unexecuted_proposal(run_id, task_id)
                self._transition(
                    run_id, snap, S.ACTION_EXECUTING, "ACTION_ADMISSION_REQUESTED",
                    {"action_proposal_id": proposal["action_proposal_id"] if proposal else None},
                )
                continue

            if state == S.ACTION_EXECUTING:
                if self._execute_pending(run_id, snap, revision) == "return":
                    return self._execution_result(run_id, handoff, started)
                continue

            if state == S.NEEDS_APPROVAL:
                decision = self._approval_state(run_id, task_id)
                if decision == "PENDING":
                    return self._execution_result(run_id, handoff, started)
                self._transition(run_id, snap, S.ACTION_EXECUTING, "APPROVAL_RESOLVED", {"approval_state": decision})
                continue

            if state == S.ACTION_UNKNOWN:
                report = ex.actions.reconcile(run_id)
                if any(item.get("outcome") == "UNKNOWN" for item in report) or ex.actions.unsettled(run_id):
                    return self._execution_result(run_id, handoff, started)
                self._transition(run_id, snap, S.CODING, "ACTION_UNKNOWN_RECONCILED", {"report": report})
                continue

            if state == S.VERIFICATION_REQUIRED:
                if stop_at == "verification-required" or ex.verifier is None:
                    return self._execution_result(run_id, handoff, started)
                self._transition(run_id, snap, S.VERIFYING, "VERIFICATION_REQUESTED")
                continue

            if state == S.VERIFYING:
                if self._verify_candidate(run_id, snap, plan_record or self.records.get_active_plan(task_id), resolved, adapter) == "return":
                    return self._execution_result(run_id, handoff, started)
                continue

            if state == S.REPAIRING:
                self._transition(run_id, snap, S.CODING, "REPAIR_STARTED")
                continue

            if state == S.REPLANNING:
                self._transition(run_id, snap, S.PLANNING, "REPLANNING_STARTED")
                continue

            return self._execution_result(run_id, handoff, started)
        from harness.orchestration.controller import OrchestrationError

        raise OrchestrationError("EXECUTION_LOOP_BOUND", "Execution loop exceeded its iteration bound")

    # --------------------------------------------------------- helpers
    def _transition(
        self,
        run_id: str,
        snap: LifecycleSnapshot,
        new_state: OrchestrationState,
        event: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        stop_reason: Optional[str] = None,
    ) -> LifecycleSnapshot:
        current = self.lifecycle.get(run_id)
        return self.lifecycle.transition(
            run_id, current.state, current.version, new_state,
            event_type=event, payload=payload or {}, stop_reason_code=stop_reason,
        )

    def _task_dependencies(self, run_id: str, task_id: str, commit: str) -> Any:
        """Isolated dependency environment for the task (None when not needed/unavailable)."""
        ex = self.execution
        if ex.dependencies is None:
            return None
        try:
            env = ex.dependencies.ensure(run_id, task_id, commit)
        except Exception as exc:  # setup trouble must never fall back to host execution
            self.run_store.append_event(run_id, "DEPENDENCY_ENVIRONMENT_UNAVAILABLE", {
                "task_id": task_id, "reason": getattr(exc, "code", type(exc).__name__), "message": str(exc)[:300],
            })
            return None
        return env if env.state == "READY" else None

    def _ensure_task_workspace(self, run_id: str, task_id: str, handoff: VerifiedHandoff) -> Any:
        ex = self.execution
        existing = ex.workspaces.get(run_id, task_id)
        if existing:
            return ex.workspaces.current_version(run_id, task_id)
        return ex.workspaces.ensure(run_id, task_id, handoff.source_revision, handoff.source_revision)

    def _evidence(self, run_id: str, task_id: str, revision: str) -> List[Any]:
        key = f"{task_id}:{revision}"
        cached = self._evidence_cache.get(key)
        if cached is not None:
            return cached
        results = self._seed_evidence(run_id, task_id, revision)
        seen = {item.reference.evidence_id for item in results}
        # Carry forward planner-requested evidence to the current revision.
        with self.run_store.get_connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT provenance_json FROM h_evidence WHERE task_id = ? AND run_id = ?",
                (task_id, run_id),
            ).fetchall()
        queries: List[tuple] = []
        for row in rows:
            try:
                provenance = json.loads(row["provenance_json"])
            except ValueError:
                continue
            item = (provenance.get("query_type"), provenance.get("query"))
            if all(item) and item not in queries:
                queries.append(item)
        for query_type, query in queries[:30]:
            try:
                query_model = EvidenceQueryV1(query_type=query_type, query=query, max_results=6, max_bytes=24000)
            except Exception:
                continue
            for result in self.retriever.query(run_id, task_id, revision, query_model):
                if result.reference.evidence_id not in seen:
                    seen.add(result.reference.evidence_id)
                    results.append(result)
        self._evidence_cache = {key: results}
        return results

    def _unexecuted_proposal(self, run_id: str, task_id: str) -> Optional[Dict[str, Any]]:
        with self.run_store.get_connection() as conn:
            row = conn.execute(
                """
                SELECT p.* FROM h_action_proposals p
                LEFT JOIN h_actions a ON a.action_proposal_id = p.action_proposal_id
                WHERE p.run_id = ? AND p.task_id = ?
                  AND (p.state = 'UNEXECUTED' OR (p.state = 'ADMITTED' AND (a.state IS NULL OR a.state NOT IN
                       ('SUCCEEDED', 'FAILED', 'POLICY_VIOLATION', 'CANCELLED', 'DENIED', 'STALE'))))
                ORDER BY p.created_at DESC LIMIT 1
                """,
                (run_id, task_id),
            ).fetchone()
        return dict(row) if row else None

    def _pending_candidate(self, run_id: str, task_id: str, version_id: str) -> Optional[Dict[str, Any]]:
        """Latest candidate of the current version that has not been verified yet."""
        ex = self.execution
        candidate = ex.workspaces.latest_candidate(run_id, task_id)
        if not candidate or candidate["workspace_version_id"] != version_id:
            return None
        if ex.verifier is not None and ex.verifier.has_decision(candidate["candidate_id"]):
            return None
        return candidate

    def _coder_turn_index(self, run_id: str, task_id: str) -> int:
        with self.run_store.get_connection() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM h_events WHERE run_id = ? AND event_type = 'CODER_DECISION_CONSUMED'",
                (run_id,),
            ).fetchall()
        return sum(1 for row in rows if json.loads(row["payload_json"]).get("task_id") == task_id)

    def _consume_turn(self, run_id: str, task_id: str, call_id: str, kind: str, extra: Optional[Dict[str, Any]] = None) -> None:
        try:
            self.run_store.append_event(
                run_id,
                "CODER_DECISION_CONSUMED",
                {"task_id": task_id, "call_id": call_id, "decision": kind, **(extra or {})},
                dedupe_key=f"coder-decision:{call_id}",
            )
        except Exception:
            # A replayed call was already consumed; the unique dedupe key keeps this idempotent.
            pass

    def _consecutive_rejections(self, run_id: str, task_id: str) -> int:
        with self.run_store.get_connection() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM h_events WHERE run_id = ? AND event_type = 'CODER_DECISION_CONSUMED' ORDER BY seq",
                (run_id,),
            ).fetchall()
        count = 0
        for row in rows:
            payload = json.loads(row["payload_json"])
            if payload.get("task_id") != task_id:
                continue
            count = count + 1 if payload.get("rejected") else 0
        return count

    def _history_sections(self, run_id: str, task_id: str) -> List[Dict[str, Any]]:
        ex = self.execution
        records = ex.actions.feedback_history(run_id, task_id, limit=ex.history_limit)
        with self.run_store.get_connection() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM h_events WHERE run_id = ? AND event_type = 'CODER_DECISION_REJECTED' ORDER BY seq DESC LIMIT 2",
                (run_id,),
            ).fetchall()
        rejected = [json.loads(row["payload_json"]) for row in rows]
        rejected = [item for item in rejected if item.get("task_id") == task_id]
        sections: List[Dict[str, Any]] = []
        total = len(records)
        # Under a small provider tokens-per-minute cap, the untrimmed newest result does not fit
        # beside the pinned context and would be dropped: the coder would never see what its last
        # action read, and would read again forever. Trim it to fit instead.
        from harness.model.adapter import provider_tpm_limit

        limits = provider_tpm_limit(getattr(self, "_tpm_origin", "")) or {}
        tight = int(limits.get("tpm", 0) or 0)
        newest_limits = ((("stdout_excerpt", max(1500, tight // 3)), ("stderr_excerpt", 600), ("diff_excerpt", 900))
                         if 0 < tight < 20_000 else None)
        for position, record in enumerate(records):
            newest = position == total - 1
            trimmed = dict(record)
            if not newest or newest_limits:
                for key, limit in (newest_limits if newest and newest_limits else
                                   (("stdout_excerpt", 1500), ("stderr_excerpt", 1000), ("diff_excerpt", 1500))):
                    value = trimmed.get(key) or ""
                    if len(value) > limit:
                        trimmed[key] = value[: limit // 3] + "\n…[older excerpt trimmed]…\n" + value[-(limit - limit // 3):]
            sections.append({
                "section": "history",
                "label": f"action_result_{position}",
                "content": trimmed,
                "host_observed": False,
                "rank": 6_000 + position * 10,
                "kind": "action_result",
                "pair_id": record.get("pair_id"),
            })
        for position, item in enumerate(reversed(rejected)):
            sections.append({
                "section": "history",
                "label": f"rejected_decision_{position}",
                "content": item,
                "host_observed": True,
                "rank": 6_500 + position,
                "kind": "decision_rejection",
            })
        if ex.verifier is not None:
            feedback = ex.verifier.latest_repair_feedback(run_id, task_id)
            if feedback:
                sections.append({
                    "section": "repair",
                    "label": "verification_repair_feedback",
                    "content": feedback,
                    "host_observed": False,
                    "rank": 7_000,
                    "pinned": False,
                    "kind": "repair_feedback",
                })
        return sections

    def _remaining_budget_view(self, run_id: str) -> Dict[str, Any]:
        ledger = self.budgets.get(run_id)
        view: Dict[str, Any] = {
            "model_calls_remaining": ledger["remaining_calls"] - ledger["reserved_future_calls"],
            "input_tokens_remaining": ledger["remaining_input_tokens"],
            "output_tokens_remaining": ledger["remaining_output_tokens"] - ledger["reserved_future_output_tokens"],
            "verification_calls_reserved": ledger["reserved_future_calls"],
            "deadline_at": ledger["deadline_at"],
        }
        if self.execution is not None:
            try:
                execution = self.execution.actions.budgets.get(run_id)
                view.update({
                    "actions_remaining": execution["remaining_actions"],
                    "container_seconds_remaining": execution["remaining_wall_seconds"],
                })
            except KeyError:
                pass
        return view

    # ------------------------------------------------------ coder turn
    def _coder_turn(
        self,
        run_id: str,
        snap: LifecycleSnapshot,
        handoff: VerifiedHandoff,
        resolved: Any,
        adapter: Any,
        plan_record: Any,
        version: Any,
    ) -> str:
        from harness.orchestration.controller import OrchestrationError

        ex = self.execution
        task_id = snap.active_task_id
        turn = self._coder_turn_index(run_id, task_id)
        if turn >= ex.max_coder_turns_per_task:
            self._transition(run_id, snap, S.BUDGET_EXHAUSTED, "BUDGET_EXHAUSTED", {"reason": "CODER_TURN_LIMIT"},
                             stop_reason="CODER_TURN_LIMIT")
            return "return"
        if self._consecutive_rejections(run_id, task_id) >= ex.max_consecutive_rejections:
            return self._no_progress_replan(run_id, snap, "REPEATED_REJECTED_DECISIONS")
        policy = ex.policy.active_snapshot(run_id)
        outcome = self._call_role(
            run_id=run_id,
            task_id=task_id,
            role=Role.CODER,
            purpose=f"coder-exec-p{plan_record.plan.plan_revision}-t{turn}",
            resolved=resolved,
            adapter=adapter,
            source_revision=version.commit,
            evidence=self._evidence(run_id, task_id, version.commit),
            plan=plan_record.plan,
            phase="EXECUTION",
            authorization_policy=policy.model_visible(),
            extra_sections=self._history_sections(run_id, task_id),
            candidate_version=version.commit,
        )
        decision = outcome.parsed
        try:
            if isinstance(decision, CoderDecisionV1):
                self._validate_coder_identity(decision, task_id, version.commit, plan_record)
            elif isinstance(decision, (CoderCompleteV1, CoderReplanV1)):
                self._validate_coder_common(decision, task_id, plan_record)
        except OrchestrationError as exc:
            self._reject_decision(run_id, task_id, outcome.call_id, exc.code, str(exc), decision)
            return "continue"

        if isinstance(decision, CoderDecisionV1):
            existing = self._proposal_for_call(outcome.call_id)
            if existing is None:
                try:
                    record = self.records.put_action_proposal(
                        run_id=run_id,
                        task_id=task_id,
                        lifecycle_version=self.lifecycle.get(run_id).version + 1,
                        plan_record=plan_record,
                        coder_call_id=outcome.call_id,
                        decision=decision,
                        authorization_policy_sha256=policy.contract.policy_sha256,
                    )
                except Exception as exc:  # ActionProposalError: invalid capability/syntax
                    self._reject_decision(run_id, task_id, outcome.call_id, getattr(exc, "code", "ACTION_PROPOSAL_INVALID"), str(exc), decision)
                    return "continue"
            else:
                record = existing
            self._consume_turn(run_id, task_id, outcome.call_id, "CODE", {"action_proposal_id": record.action_proposal_id})
            snap = self.lifecycle.get(run_id)
            self._transition(run_id, snap, S.ACTION_PROPOSED, "ACTION_PROPOSED",
                             {"action_proposal_id": record.action_proposal_id, "executed": False})
            return "continue"

        if isinstance(decision, CoderCompleteV1):
            last = ex.workspaces.latest_candidate(run_id, task_id)
            if last and last["workspace_version_id"] == version.version_id and ex.verifier is not None and ex.verifier.has_decision(last["candidate_id"]):
                self._consume_turn(run_id, task_id, outcome.call_id, "COMPLETE", {"rejected": True})
                self.run_store.append_event(run_id, "CODER_DECISION_REJECTED", {
                    "task_id": task_id,
                    "reason_code": "NO_CHANGE_SINCE_FAILED_VERIFICATION",
                    "message": "COMPLETE was requested without any accepted change since the last verified candidate failed.",
                })
                return self._no_progress_replan(run_id, self.lifecycle.get(run_id), "COMPLETE_WITHOUT_PROGRESS")
            workspace = ex.workspaces.get(run_id, task_id)
            git = ex.workspaces.git(run_id)
            if workspace and git.commit_tree_of(version.commit) == git.commit_tree_of(workspace["task_start_commit"]):
                self._reject_decision(
                    run_id, task_id, outcome.call_id, "COMPLETE_WITHOUT_CHANGES",
                    "COMPLETE was rejected: the private workspace is byte-identical to the task start, so there is nothing to verify.",
                    decision,
                    instruction="Apply the fix with a CODE action (apply_patch) and run the focused tests first; "
                                "return REPLAN or NEEDS_INPUT only if no source change is appropriate.",
                )
                return "continue"
            candidate = self._freeze_candidate(run_id, task_id, plan_record, version, decision)
            self._consume_turn(run_id, task_id, outcome.call_id, "COMPLETE", {"candidate_id": candidate.candidate_id})
            snap = self.lifecycle.get(run_id)
            self._transition(run_id, snap, S.VERIFICATION_REQUIRED, "VERIFICATION_REQUIRED",
                             {"candidate_id": candidate.candidate_id, "claimed_outcome": decision.claimed_outcome[:500]})
            return "continue"

        if isinstance(decision, CoderReplanV1):
            try:
                self.records.validate_evidence_references(
                    task_id=task_id, source_revision=version.commit, evidence_ids=set(decision.evidence_ids)
                )
            except Exception as exc:
                self._reject_decision(run_id, task_id, outcome.call_id, "REPLAN_EVIDENCE_INVALID", str(exc), decision)
                return "continue"
            self._consume_turn(run_id, task_id, outcome.call_id, "REPLAN")
            if self._replan_count(run_id, task_id) >= 2:
                self._transition(run_id, self.lifecycle.get(run_id), S.FAILED, "ORCHESTRATION_FAILED",
                                 {"reason": "REPLAN_LIMIT_EXCEEDED"}, stop_reason="REPLAN_LIMIT_EXCEEDED")
                return "return"
            self._transition(run_id, self.lifecycle.get(run_id), S.REPLANNING, "REPLAN_REQUESTED",
                             {"evidence_ids": decision.evidence_ids, "reason": decision.requested_change[:500]})
            return "continue"

        if isinstance(decision, NeedCapabilityV1):
            self._consume_turn(run_id, task_id, outcome.call_id, "NEED_CAPABILITY")
            self._transition(run_id, self.lifecycle.get(run_id), S.NEEDS_CAPABILITY, "CAPABILITY_REQUIRED",
                             {"capability": decision.capability}, stop_reason="NEEDS_CAPABILITY")
            return "return"
        raise OrchestrationError("CODER_DECISION_INVALID", "Unsupported coder decision")

    def _proposal_for_call(self, call_id: str) -> Optional[ProposalRecord]:
        with self.run_store.get_connection() as conn:
            row = conn.execute("SELECT * FROM h_action_proposals WHERE coder_call_id = ?", (call_id,)).fetchone()
        if not row:
            return None
        return ProposalRecord(row["action_proposal_id"], row["proposal_artifact_id"], row["code_artifact_id"], row["code_sha256"])

    def _reject_decision(self, run_id: str, task_id: str, call_id: str, code: str, message: str, decision: Any,
                         instruction: str = "Return a corrected decision; workspace_version must equal the host source_revision.") -> None:
        self._consume_turn(run_id, task_id, call_id, getattr(decision, "decision", "UNKNOWN"), {"rejected": True, "reason_code": code})
        self.run_store.append_event(run_id, "CODER_DECISION_REJECTED", {
            "task_id": task_id,
            "call_id": call_id,
            "reason_code": code,
            "message": message[:1000],
            "instruction": instruction,
        })

    def _no_progress_replan(self, run_id: str, snap: LifecycleSnapshot, reason: str) -> str:
        task_id = snap.active_task_id
        if self._replan_count(run_id, task_id) >= 2:
            self._transition(run_id, snap, S.FAILED, "ORCHESTRATION_FAILED", {"reason": f"{reason}_AFTER_REPLANS"},
                             stop_reason="NO_PROGRESS_AFTER_REPLANS")
            return "return"
        current = self.lifecycle.get(run_id)
        target = current.state
        if target not in (S.CODING, S.ACTION_EXECUTING, S.REPAIRING):
            return "continue"
        self.lifecycle.transition(
            run_id, current.state, current.version, S.REPLANNING,
            event_type="REPLAN_REQUESTED", payload={"reason": reason, "host_triggered": True},
        )
        return "continue"

    # --------------------------------------------------------- execute
    def _execute_pending(self, run_id: str, snap: LifecycleSnapshot, revision: str) -> str:
        ex = self.execution
        task_id = snap.active_task_id
        proposal = self._unexecuted_proposal(run_id, task_id)
        if proposal is None:
            with self.run_store.get_connection() as conn:
                row = conn.execute(
                    """
                    SELECT p.* FROM h_action_proposals p JOIN h_actions a ON a.action_proposal_id = p.action_proposal_id
                    WHERE p.run_id = ? AND p.task_id = ? AND a.state = 'NEEDS_APPROVAL' ORDER BY p.created_at DESC LIMIT 1
                    """,
                    (run_id, task_id),
                ).fetchone()
            proposal = dict(row) if row else None
        if proposal is None:
            self._transition(run_id, snap, S.CODING, "ACTION_EXECUTION_RECONCILED", {"reason": "no pending proposal"})
            return "continue"
        try:
            deps = self._task_dependencies(run_id, task_id, ex.workspaces.get(run_id, task_id)["task_start_commit"])
            execution = ex.actions.execute_proposal(
                run_id,
                task_id,
                proposal["action_proposal_id"],
                owner_id=self.owner_id,
                stage_artifact_ids=self._stage_artifacts(run_id, task_id),
                dependency_site=deps.site_root if deps is not None else None,
                dependency_environment_id=deps.environment_id if deps is not None and deps.state == "READY" else None,
            )
        except ActionUnknownError as exc:
            self._transition(run_id, snap, S.ACTION_UNKNOWN, "ACTION_UNKNOWN", {"reason": str(exc)[:500]},
                             stop_reason="ACTION_UNKNOWN")
            return "return"
        except ExecutionBudgetExhaustedError as exc:
            self._transition(run_id, snap, S.BUDGET_EXHAUSTED, "BUDGET_EXHAUSTED", {"reason": str(exc)[:300]},
                             stop_reason="EXECUTION_BUDGET_EXHAUSTED")
            return "return"
        except (SandboxUnavailableError, RuntimeImageInvalidError) as exc:
            self._transition(run_id, snap, S.BLOCKED_ENVIRONMENT, "SANDBOX_UNAVAILABLE",
                             {"resume_state": "ACTION_PROPOSED", "error": str(exc)[:300]},
                             stop_reason=f"{getattr(exc, 'code', 'SANDBOX_UNAVAILABLE')}@ACTION_PROPOSED")
            return "return"
        except SandboxError as exc:
            self._transition(run_id, snap, S.BLOCKED_ENVIRONMENT, "SANDBOX_ERROR",
                             {"resume_state": "ACTION_PROPOSED", "error": str(exc)[:300]},
                             stop_reason=f"{getattr(exc, 'code', 'SANDBOX_ERROR')}@ACTION_PROPOSED")
            return "return"
        if execution.kind == "NEEDS_APPROVAL":
            self._transition(run_id, snap, S.NEEDS_APPROVAL, "APPROVAL_REQUESTED",
                             {"approval_request_id": execution.approval_request_id, "action_id": execution.action_id},
                             stop_reason="NEEDS_APPROVAL")
            return "return"
        if execution.kind in ("STALE", "DENIED"):
            self._mark_last_turn_rejected(run_id, task_id, proposal["action_proposal_id"], execution.kind)
            self._transition(run_id, snap, S.CODING, f"ACTION_{execution.kind}_RETURNED_TO_CODER",
                             {"action_id": execution.action_id, "reason_codes": execution.reason_codes})
            return "continue"
        result = execution.result
        if result is not None and result.settlement.value == "ACCEPTED":
            self._evidence_cache = {}
            ExecutionFeedbackService(self.run_store, self.indexer, self.evidence_store).refresh_context(
                run_id, task_id,
                old_revision=execution.old_version or revision,
                new_revision=execution.new_version or revision,
                changed_paths=execution.changed_paths,
                action_id=execution.action_id,
            )
        if execution.repeated_failure:
            self.lifecycle.transition(
                run_id, self.lifecycle.get(run_id).state, self.lifecycle.get(run_id).version, S.CODING,
                event_type="ACTION_SETTLED_TO_CODER", payload={"action_id": execution.action_id},
            )
            return self._no_progress_replan(run_id, self.lifecycle.get(run_id), "REPEATED_FAILURE_SIGNATURE")
        self._transition(run_id, snap, S.CODING, "ACTION_SETTLED_TO_CODER", {
            "action_id": execution.action_id,
            "settlement": result.settlement.value if result else None,
        })
        return "continue"

    def _mark_last_turn_rejected(self, run_id: str, task_id: str, proposal_id: str, kind: str) -> None:
        self.run_store.append_event(run_id, "CODER_DECISION_REJECTED", {
            "task_id": task_id,
            "action_proposal_id": proposal_id,
            "reason_code": f"PROPOSAL_{kind}",
            "message": f"The proposal was {kind.lower()} by host policy and was not executed.",
        })
        # Record the rejection on the consumed-turn stream for the consecutive-rejection guard.
        self.run_store.append_event(run_id, "CODER_DECISION_CONSUMED", {
            "task_id": task_id, "decision": "POLICY", "rejected": True, "action_proposal_id": proposal_id,
        }, dedupe_key=f"policy-rejection:{proposal_id}")

    def _stage_artifacts(self, run_id: str, task_id: str) -> List[str]:
        ex = self.execution
        ids = ex.actions.recent_log_artifacts(run_id, task_id)
        try:
            plan = self.records.get_active_plan(task_id)
            ids.append(plan.artifact_id)
        except KeyError:
            pass
        return ids

    def _approval_state(self, run_id: str, task_id: str) -> str:
        with self.run_store.get_connection() as conn:
            row = conn.execute(
                """
                SELECT r.state FROM h_action_approval_requests r JOIN h_actions a ON a.action_id = r.action_id
                WHERE a.run_id = ? AND a.task_id = ? AND a.state = 'NEEDS_APPROVAL'
                ORDER BY r.created_at DESC LIMIT 1
                """,
                (run_id, task_id),
            ).fetchone()
        if not row:
            return "MISSING"
        self.execution.actions.approvals.expire()
        return row["state"]

    def _resume_blocked(self, run_id: str, snap: LifecycleSnapshot) -> bool:
        """Retry the step that was blocked by the environment when Docker is back."""
        ex = self.execution
        ok, _, _ = ex.actions.backend.availability()
        if not ok:
            return False
        try:
            ex.actions._runtime = None
            ex.actions.runtime()
        except SandboxError:
            return False
        reason = snap.stop_reason_code or ""
        resume = reason.split("@", 1)[1] if "@" in reason else ""
        target = {
            "ACTION_PROPOSED": S.ACTION_PROPOSED,
            "VERIFICATION_REQUIRED": S.VERIFICATION_REQUIRED,
            "PLAN_READY": S.PLAN_READY,
        }.get(resume)
        if target is None:
            return False
        self._transition(run_id, snap, target, "ENVIRONMENT_RESTORED", {"resume_state": resume})
        return True

    # -------------------------------------------------------- candidate
    def _freeze_candidate(self, run_id: str, task_id: str, plan_record: Any, version: Any, decision: CoderCompleteV1) -> Any:
        ex = self.execution
        locks = WorkspaceLockService(self.run_store)
        token = locks.acquire(run_id, task_id, version.version_id, self.owner_id, purpose="candidate-freeze", ttl_seconds=300)
        try:
            if ex.actions.unsettled(run_id):
                from harness.orchestration.controller import OrchestrationError

                raise OrchestrationError("UNSETTLED_ACTION", "Cannot freeze a candidate while an action is unsettled")
            with self.run_store.get_connection() as conn:
                task = conn.execute("SELECT task_spec_json FROM h_tasks WHERE task_id = ?", (task_id,)).fetchone()
            spec = json.loads(task["task_spec_json"])
            trailers = {
                "Harness-Task-ID": task_id,
                "Harness-Plan-Revision": str(plan_record.plan.plan_revision),
                "Harness-Source-Locator-SHA256": hashlib.sha256(str(spec.get("source_key", "")).encode()).hexdigest(),
            }
            contract_sha = ex.verifier.contract_sha(run_id, task_id) if ex.verifier is not None else None
            if contract_sha:
                trailers["Harness-Contract-SHA256"] = contract_sha
            record = ex.workspaces.freeze_candidate(run_id, task_id, summary=f"Fix: {spec.get('title', 'task')}", trailers=trailers)
            handoff = self._verification_handoff(run_id, task_id, plan_record, record)
            path = f"prd3/candidates/{task_id}/{record.candidate_id}-handoff.json"
            self.artifact_store.write_json(run_id, path, handoff.model_dump(mode="json"), "verification_handoff", task_id)
            artifact = self.artifact_store.get_artifact_by_path(run_id, path)
            with self.run_store.get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    ex.workspaces.insert_candidate_sql(conn, record, artifact["artifact_id"] if artifact else None)
                    from harness.persistence.events import append_event_sql

                    append_event_sql(conn, run_id, "CANDIDATE_FROZEN", {
                        "task_id": task_id,
                        "candidate_id": record.candidate_id,
                        "candidate_commit": record.commit,
                        "candidate_sha256": record.candidate_sha256,
                        "changed_paths": record.changed_paths[:200],
                        "verification_status": "NOT_RUN",
                    }, "PRD3", "PRD3", _utc())
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
            return record
        finally:
            locks.release(task_id, token)

    def _verification_handoff(self, run_id: str, task_id: str, plan_record: Any, record: Any) -> VerificationCandidateHandoffV1:
        ex = self.execution
        runtime = ex.actions.runtime()
        source = self.run_store.get_source_snapshot(run_id)
        ledger = self.budgets.get(run_id)
        deadline = datetime.datetime.fromisoformat(ledger["deadline_at"])
        remaining_wall = max(0, int((deadline - datetime.datetime.now(datetime.timezone.utc)).total_seconds()))
        with self.run_store.get_connection() as conn:
            results = [row["execution_result_id"] for row in conn.execute(
                """
                SELECT r.execution_result_id FROM h_execution_results r JOIN h_actions a ON a.action_id = r.action_id
                WHERE a.run_id = ? AND a.task_id = ? ORDER BY r.settled_at
                """,
                (run_id, task_id),
            ).fetchall()]
            task = conn.execute("SELECT task_revision FROM h_task_lifecycle WHERE task_id = ?", (task_id,)).fetchone()
        policy = ex.policy.active_snapshot(run_id)
        return VerificationCandidateHandoffV1(
            run_id=run_id,
            task_id=task_id,
            task_revision=task["task_revision"] if task else 1,
            plan_id=plan_record.plan_id,
            plan_revision=plan_record.plan.plan_revision,
            baseline=HandoffBaselineV1(commit=source["baseline_commit"], content_tree_sha256=source["content_tree_sha256"]),
            candidate=HandoffCandidateV1(
                candidate_id=record.candidate_id,
                commit=record.commit,
                tree=record.tree,
                candidate_sha256=record.candidate_sha256,
                manifest_artifact_id=record.manifest_artifact_id,
                diff_artifact_id=record.diff_artifact_id,
            ),
            changed_paths=record.changed_paths,
            action_result_ids=results,
            acceptance_contract=HandoffAcceptanceContractV1(plan_artifact_id=plan_record.artifact_id, plan_sha256=plan_record.sha256),
            environment=HandoffEnvironmentV1(
                runtime_profile_fingerprint=runtime.fingerprint,
                image_digest=runtime.contract.image_digest,
                dependency_environment_sha256=None,
                worker_version=runtime.contract.worker_version,
                tool_library_version=runtime.contract.tool_library_version,
            ),
            remaining_budget=HandoffRemainingBudgetV1(
                model_calls=max(0, ledger["remaining_calls"]),
                input_tokens=max(0, ledger["remaining_input_tokens"]),
                output_tokens=max(0, ledger["remaining_output_tokens"]),
                wall_seconds=remaining_wall,
                verification_calls_reserved=ledger["reserved_future_calls"],
            ),
            policy_sha256=policy.contract.policy_sha256,
        )

    # ----------------------------------------------------- verification
    def _verify_candidate(self, run_id: str, snap: LifecycleSnapshot, plan_record: Any, resolved: Any, adapter: Any) -> str:
        ex = self.execution
        task_id = snap.active_task_id
        candidate = ex.workspaces.latest_candidate(run_id, task_id)
        if candidate is None:
            self._transition(run_id, snap, S.CODING, "VERIFICATION_RECONCILED", {"reason": "no candidate"})
            return "continue"

        def validator(sections: Sequence[Mapping[str, Any]], purpose: str) -> ValidatorReviewV1:
            outcome = self._call_role(
                run_id=run_id,
                task_id=task_id,
                role=Role.VALIDATOR,
                purpose=purpose,
                resolved=resolved,
                adapter=adapter,
                source_revision=candidate["candidate_commit"],
                evidence=[],
                plan=plan_record.plan,
                phase="EXECUTION",
                authorization_policy=ex.policy.active_snapshot(run_id).model_visible(),
                extra_sections=sections,
                candidate_version=candidate["candidate_sha256"],
                protect_future=False,
            )
            return outcome.parsed

        outcome = ex.verifier.verify(run_id, task_id, candidate["candidate_id"], plan_record=plan_record, validator=validator)
        status = outcome.status
        payload = {"candidate_id": candidate["candidate_id"], "status": status, "decision_id": outcome.decision_id}
        if status == "PASS":
            self._transition(run_id, snap, S.READY_FOR_REVIEW, "TASK_READY_FOR_REVIEW", payload)
            return "return"
        if outcome.repair_allowed:
            self._transition(run_id, snap, S.REPAIRING, "REPAIR_FEEDBACK_CREATED", {**payload, "repair_number": outcome.repair_number})
            if outcome.replan:
                snap = self.lifecycle.get(run_id)
                self.lifecycle.transition(run_id, snap.state, snap.version, S.REPLANNING, event_type="REPLAN_REQUESTED",
                                          payload={"reason": "VERIFICATION_CONTRADICTS_PLAN", "host_triggered": True})
            return "continue"
        target = {
            "FAILED": (S.VERIFICATION_FAILED, "TASK_FAILED"),
            "UNVERIFIED": (S.UNVERIFIED, "TASK_UNVERIFIED"),
            "BLOCKED_ENVIRONMENT": (S.BLOCKED_ENVIRONMENT, "TASK_BLOCKED_ENVIRONMENT"),
            "BUDGET_EXHAUSTED": (S.BUDGET_EXHAUSTED, "TASK_BUDGET_EXHAUSTED"),
            "NEEDS_INPUT": (S.NEEDS_INPUT, "USER_INPUT_REQUIRED"),
            "CANCELLED": (S.CANCELLED, "VERIFICATION_CANCELLED"),
        }.get(status, (S.UNVERIFIED, "TASK_UNVERIFIED"))
        stop_reason = f"{status}@VERIFICATION_REQUIRED" if status == "BLOCKED_ENVIRONMENT" else status
        self._transition(run_id, snap, target[0], target[1], payload, stop_reason=stop_reason)
        return "return"

    # ---------------------------------------------------------- results
    def _move_to_blocked_environment(self, run_id: str, code: str) -> None:
        try:
            snapshot = self.lifecycle.get(run_id)
            if S.BLOCKED_ENVIRONMENT in self._allowed(snapshot.state):
                resume = {
                    S.ACTION_PROPOSED: "ACTION_PROPOSED",
                    S.ACTION_EXECUTING: "ACTION_PROPOSED",
                    S.VERIFYING: "VERIFICATION_REQUIRED",
                    S.PLAN_READY: "PLAN_READY",
                }.get(snapshot.state, "")
                self.lifecycle.transition(
                    run_id, snapshot.state, snapshot.version, S.BLOCKED_ENVIRONMENT,
                    event_type="SANDBOX_UNAVAILABLE", stop_reason_code=f"{code}@{resume}" if resume else code,
                )
        except Exception:
            return

    def _execution_result(self, run_id: str, handoff: VerifiedHandoff, started: float) -> OrchestrationPhaseResultV1:
        ex = self.execution
        snap = self.lifecycle.get(run_id)
        task_id = snap.active_task_id
        budget = self.budgets.get(run_id)
        plan = self._safe_active_plan(task_id)
        with self.run_store.get_connection() as conn:
            remaining = conn.execute(
                "SELECT COUNT(*) FROM h_tasks WHERE run_id = ? AND task_id <> ?", (run_id, task_id)
            ).fetchone()[0]
            executed = conn.execute(
                "SELECT COUNT(*) FROM h_execution_results r JOIN h_actions a ON a.action_id = r.action_id WHERE a.run_id = ? AND a.task_id = ?",
                (run_id, task_id),
            ).fetchone()[0]
            last_action = conn.execute(
                """
                SELECT a.action_id, a.state, r.settlement, r.reason_codes_json FROM h_actions a
                LEFT JOIN h_execution_results r ON r.action_id = a.action_id
                WHERE a.run_id = ? AND a.task_id = ? ORDER BY a.updated_at DESC, a.rowid DESC LIMIT 1
                """,
                (run_id, task_id),
            ).fetchone()
            latest_proposal = conn.execute(
                "SELECT * FROM h_action_proposals WHERE run_id = ? AND task_id = ? ORDER BY created_at DESC LIMIT 1",
                (run_id, task_id),
            ).fetchone()
            approval = conn.execute(
                "SELECT approval_request_id FROM h_action_approval_requests WHERE run_id = ? AND task_id = ? AND state IN ('PENDING', 'APPROVED') ORDER BY created_at DESC LIMIT 1",
                (run_id, task_id),
            ).fetchone()
            latest_event = conn.execute(
                "SELECT event_type, payload_json FROM h_events WHERE run_id = ? AND event_type IN ('USER_INPUT_REQUIRED', 'CAPABILITY_REQUIRED') ORDER BY seq DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        version = None
        try:
            version = ex.workspaces.current_version(run_id, task_id).commit if ex and ex.workspaces.get(run_id, task_id) else None
        except Exception:
            version = None
        candidate_ref = None
        candidate = ex.workspaces.latest_candidate(run_id, task_id) if ex else None
        if candidate:
            import json as _json

            changed = []
            try:
                changed = [path for _, path in ex.workspaces.git(run_id).diff_paths(candidate["task_start_commit"], candidate["candidate_commit"])]
            except Exception:
                changed = []
            candidate_ref = CandidateReferenceV1(
                candidate_id=candidate["candidate_id"],
                commit=candidate["candidate_commit"],
                candidate_sha256=candidate["candidate_sha256"],
                changed_paths=changed[:500],
            )
        verification_ref = None
        if ex and ex.verifier is not None and candidate:
            verification_ref = ex.verifier.reference(run_id, task_id)
        questions: List[Any] = []
        required_capability = None
        if latest_event:
            payload = json.loads(latest_event["payload_json"])
            if latest_event["event_type"] == "USER_INPUT_REQUIRED" and snap.state == S.NEEDS_INPUT:
                questions = payload.get("questions", [])
            if latest_event["event_type"] == "CAPABILITY_REQUIRED" and snap.state == S.NEEDS_CAPABILITY:
                required_capability = payload.get("capability")
        next_prd = None
        if snap.state in (S.ACTION_PROPOSED,):
            next_prd = 3
        elif snap.state == S.VERIFICATION_REQUIRED:
            next_prd = 4
        elif snap.state == S.READY_FOR_REVIEW:
            next_prd = 5
        proposal_ref = None
        if latest_proposal and latest_proposal["state"] == "UNEXECUTED":
            proposal_ref = ProposalReferenceV1(
                action_proposal_id=latest_proposal["action_proposal_id"],
                proposal_artifact_id=latest_proposal["proposal_artifact_id"],
                decision="CODE",
            )
        return OrchestrationPhaseResultV1(
            run_id=run_id,
            task_id=task_id,
            status=snap.state,
            source_revision=version or handoff.source_revision,
            plan=PlanReferenceV1(plan_id=plan.plan_id, revision=plan.plan.plan_revision) if plan else None,
            proposal=proposal_ref,
            usage=PhaseUsageV1(
                model_calls_used=budget["used_calls"],
                input_tokens_used=budget["used_input_tokens"],
                output_tokens_used=budget["used_output_tokens"],
                elapsed_seconds=max(0, int(time.monotonic() - started)),
                verification_calls_reserved=max(2, budget["reserved_future_calls"]),
            ),
            remaining_repository_tasks=remaining,
            questions=questions,
            required_capability=required_capability,
            next_required_prd=next_prd,
            workspace_version=version,
            actions_executed=executed,
            last_action=(
                ActionReferenceV1(
                    action_id=last_action["action_id"],
                    settlement=last_action["settlement"] or last_action["state"],
                    reason_codes=json.loads(last_action["reason_codes_json"] or "[]"),
                )
                if last_action
                else None
            ),
            candidate=candidate_ref,
            verification=verification_ref,
            approval_request_id=approval["approval_request_id"] if approval else None,
            stop_reason_code=snap.stop_reason_code,
            created_at=_utc(),
        )
