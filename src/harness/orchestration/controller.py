"""Deterministic PRD 2 controller: prepare -> plan -> unexecuted proposal."""
from __future__ import annotations

import datetime
import hashlib
import json
import re
import time
import threading
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from pydantic import BaseModel

from harness.context import BuiltContext, ContextBuilder
from harness.context.builder import DEFAULT_AUTHORIZATION_POLICY
from harness.contracts import (
    CoderCompleteV1,
    CoderDecisionV1,
    CoderReplanV1,
    EvidenceQueryV1,
    NeedCapabilityV1,
    OrchestrationPhaseResultV1,
    OrchestrationState,
    PhaseUsageV1,
    PlanReferenceV1,
    PlanV1,
    PlannerNeedsEvidenceV1,
    PlannerNeedsInputV1,
    ProposalReferenceV1,
    Role,
)
from harness.model import (
    FakeModelAdapter,
    ModelAdapter,
    ModelAdapterError,
    ModelCallRequest,
    ModelConfigStore,
    ModelProfileResolver,
    OpenAICompatibleModelAdapter,
    RecordedModelAdapter,
    ResolvedProfile,
)
from harness.model.credentials import ModelAuthMissingError
from harness.orchestration.budget_ledger import (
    BudgetExhaustedError,
    BudgetLedgerService,
    BudgetLimits,
)
from harness.orchestration.execution_loop import EXECUTION_BOUNDARIES, ExecutionLoopMixin, ExecutionServices
from harness.orchestration.handoff import PreparedHandoffVerifier, VerifiedHandoff
from harness.orchestration.lease_service import Lease, RunLeaseService
from harness.orchestration.lifecycle import LifecycleService, LifecycleSnapshot
from harness.orchestration.store import OrchestrationRecordStore, PlanRecord, PlanRevisionError, ProposalRecord
from harness.persistence import ArtifactStore, RunStore, canonical_json
from harness.retrieval import EvidenceResult, EvidenceStore, RepositoryIndexer, Retriever
from harness.roles import RoleSchemaError, RoleSchemaRegistry


class OrchestrationError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class CallOutcome:
    parsed: Any
    call_id: str
    context: Optional[BuiltContext]


class OrchestrationController(ExecutionLoopMixin):
    def __init__(
        self,
        *,
        run_store: RunStore,
        artifact_store: ArtifactStore,
        data_root: str | Path,
        profile_resolver: ModelProfileResolver,
        adapter: Optional[ModelAdapter] = None,
        profile_id: str = "designated",
        budget_limits: BudgetLimits = BudgetLimits(),
        owner_id: Optional[str] = None,
        execution: Optional[ExecutionServices] = None,
    ) -> None:
        self.run_store = run_store
        self.artifact_store = artifact_store
        self.data_root = Path(data_root).resolve()
        self.profile_resolver = profile_resolver
        self.injected_adapter = adapter
        self.profile_id = profile_id
        self.budget_limits = budget_limits
        self.owner_id = owner_id or f"controller-{uuid.uuid4().hex[:12]}"
        self.schema_registry = RoleSchemaRegistry()
        self.lifecycle = LifecycleService(run_store)
        self.leases = RunLeaseService(run_store)
        self.budgets = BudgetLedgerService(run_store)
        self.model_configs = ModelConfigStore(run_store)
        self.handoff_verifier = PreparedHandoffVerifier(
            run_store, artifact_store, self.data_root
        )
        self.indexer = RepositoryIndexer(run_store, artifact_store, self.data_root)
        self.evidence_store = EvidenceStore(
            run_store, artifact_store, self.indexer.workspace_root
        )
        self.retriever = Retriever(run_store, self.indexer, self.evidence_store)
        self.context_builder = ContextBuilder(
            run_store, artifact_store, self.evidence_store, schema_registry=self.schema_registry
        )
        self.records = OrchestrationRecordStore(run_store, artifact_store)
        self.execution = execution
        if execution is not None:
            # PRD 3: index, retrieve, and verify evidence against the active
            # task's writable workspace; PRD 1's baseline stays untouched.
            self.indexer.root_override = execution.workspaces.active_task_root
        self._active_lock = threading.Lock()
        self._active_adapter: Optional[ModelAdapter] = None
        self._active_call_id: Optional[str] = None
        self._current_lease: Optional[Lease] = None

    def continue_run(
        self, run_id: str, stop_at: str = "action-proposed"
    ) -> OrchestrationPhaseResultV1:
        if stop_at in EXECUTION_BOUNDARIES:
            return self._continue_execution(run_id, stop_at)
        if stop_at not in {"plan", "action-proposed"}:
            raise OrchestrationError("INVALID_STOP_BOUNDARY", f"Unsupported stop boundary: {stop_at}")
        try:
            handoff = self.handoff_verifier.verify(run_id)
        except Exception as exc:
            if self.run_store.get_run(run_id):
                try:
                    self.run_store.append_event(
                        run_id,
                        "ORCHESTRATION_HANDOFF_REJECTED",
                        {
                            "error_code": getattr(
                                exc, "code", "PREPARED_HANDOFF_INTEGRITY_FAILURE"
                            ),
                            "message": str(exc)[:1000],
                            "model_called": False,
                        },
                    )
                except Exception:
                    pass
            raise
        snapshot = self.lifecycle.initialize(run_id)
        lease = self.leases.acquire(run_id, self.owner_id, ttl_seconds=300)
        self._current_lease = lease
        started = time.monotonic()
        try:
            resolved = self.profile_resolver.resolve(self.profile_id)
            adapter = self._adapter(resolved)
            self.model_configs.freeze(
                run_id,
                resolved,
                adapter_name=adapter.name,
                adapter_version=adapter.version,
            )
            effective_limits = replace(
                self.budget_limits,
                reserved_future_output_tokens=min(
                    max(
                        self.budget_limits.reserved_future_output_tokens,
                        self.budget_limits.reserved_future_calls
                        * resolved.contract.max_output_tokens,
                    ),
                    self.budget_limits.max_output_tokens // 2,
                ),
                # Capped so a small evaluator wall budget (or a slow profile) still leaves
                # orchestration time instead of failing initialization.
                reserved_future_wall_seconds=min(
                    max(
                        self.budget_limits.reserved_future_wall_seconds,
                        self.budget_limits.reserved_future_calls
                        * resolved.contract.request_timeout_seconds,
                    ),
                    self.budget_limits.max_wall_seconds // 2,
                ),
            )
            self.budgets.initialize(run_id, effective_limits)
            self._reconcile_model_calls(run_id)

            terminal = self._terminal_result_if_any(
                run_id, snapshot, handoff, started, stop_at=stop_at
            )
            if terminal is not None:
                self._assert_workspace_unchanged(handoff)
                return terminal

            snapshot = self.lifecycle.get(run_id)
            if snapshot.state == OrchestrationState.PREPARED:
                snapshot = self.lifecycle.transition(
                    run_id,
                    snapshot.state,
                    snapshot.version,
                    OrchestrationState.INDEXING,
                    event_type="INDEXING_STARTED",
                )
            if snapshot.state == OrchestrationState.INDEXING:
                index_result = self.indexer.build(run_id, handoff.source_revision)
                snapshot = self.lifecycle.transition(
                    run_id,
                    snapshot.state,
                    snapshot.version,
                    OrchestrationState.PLANNING,
                    event_type="INDEX_READY",
                    payload={
                        "indexed_files": index_result.indexed_files,
                        "excluded_files": index_result.excluded_files,
                        "symbols": index_result.symbol_count,
                        "reused": index_result.reused,
                    },
                )

            evidence = self._seed_evidence(
                run_id, snapshot.active_task_id, handoff.source_revision
            )
            plan_record: Optional[PlanRecord] = None
            replan_count = self._replan_count(run_id, snapshot.active_task_id)
            while True:
                snapshot = self.lifecycle.get(run_id)
                if snapshot.state == OrchestrationState.PLANNING:
                    existing = self._safe_active_plan(snapshot.active_task_id)
                    if existing and existing.plan.plan_revision > replan_count:
                        plan_record = existing
                    else:
                        plan_record, stop = self._planning_phase(
                            run_id=run_id,
                            snapshot=snapshot,
                            handoff=handoff,
                            resolved=resolved,
                            adapter=adapter,
                            evidence=evidence,
                            previous_plan=existing,
                            cycle=replan_count,
                        )
                        if stop is not None:
                            self._assert_workspace_unchanged(handoff)
                            return self._phase_result(
                                run_id,
                                handoff,
                                self.lifecycle.get(run_id),
                                started,
                                plan_record,
                                None,
                            )
                    snapshot = self.lifecycle.get(run_id)
                    if snapshot.state == OrchestrationState.PLANNING:
                        snapshot = self.lifecycle.transition(
                            run_id,
                            snapshot.state,
                            snapshot.version,
                            OrchestrationState.PLAN_READY,
                            event_type="PLAN_READY",
                            payload={
                                "plan_id": plan_record.plan_id,
                                "plan_revision": plan_record.plan.plan_revision,
                            },
                        )

                if snapshot.state == OrchestrationState.PLAN_READY and stop_at == "plan":
                    self._assert_workspace_unchanged(handoff)
                    return self._phase_result(
                        run_id, handoff, snapshot, started, plan_record, None
                    )
                if snapshot.state == OrchestrationState.PLAN_READY:
                    snapshot = self.lifecycle.transition(
                        run_id,
                        snapshot.state,
                        snapshot.version,
                        OrchestrationState.CODING,
                        event_type="CODING_STARTED",
                    )
                if snapshot.state != OrchestrationState.CODING:
                    terminal = self._terminal_result_if_any(
                        run_id, snapshot, handoff, started, stop_at=stop_at
                    )
                    if terminal is not None:
                        self._assert_workspace_unchanged(handoff)
                        return terminal
                    raise OrchestrationError(
                        "UNEXPECTED_LIFECYCLE_STATE",
                        f"Cannot continue from {snapshot.state.value}",
                    )

                if plan_record is None:
                    plan_record = self.records.get_active_plan(snapshot.active_task_id)
                existing_proposal = self.records.get_proposal(run_id, snapshot.active_task_id)
                if existing_proposal:
                    proposal_record = ProposalRecord(
                        existing_proposal["action_proposal_id"],
                        existing_proposal["proposal_artifact_id"],
                        existing_proposal["code_artifact_id"],
                        existing_proposal["code_sha256"],
                    )
                    snapshot = self.lifecycle.transition(
                        run_id,
                        snapshot.state,
                        snapshot.version,
                        OrchestrationState.ACTION_PROPOSED,
                        event_type="ACTION_PROPOSAL_RECONCILED",
                    )
                    self._assert_workspace_unchanged(handoff)
                    return self._phase_result(
                        run_id,
                        handoff,
                        snapshot,
                        started,
                        plan_record,
                        proposal_record,
                    )

                coder = self._call_role(
                    run_id=run_id,
                    task_id=snapshot.active_task_id,
                    role=Role.CODER,
                    purpose=f"coder-plan-{plan_record.plan.plan_revision}",
                    resolved=resolved,
                    adapter=adapter,
                    source_revision=handoff.source_revision,
                    evidence=evidence,
                    plan=plan_record.plan,
                )
                decision = coder.parsed
                if isinstance(decision, CoderDecisionV1):
                    self._validate_coder_identity(
                        decision,
                        snapshot.active_task_id,
                        handoff.source_revision,
                        plan_record,
                    )
                    auth_hash = hashlib.sha256(
                        canonical_json(DEFAULT_AUTHORIZATION_POLICY).encode("utf-8")
                    ).hexdigest()
                    proposal_record = self.records.put_action_proposal(
                        run_id=run_id,
                        task_id=snapshot.active_task_id,
                        lifecycle_version=snapshot.version + 1,
                        plan_record=plan_record,
                        coder_call_id=coder.call_id,
                        decision=decision,
                        authorization_policy_sha256=auth_hash,
                    )
                    snapshot = self.lifecycle.transition(
                        run_id,
                        snapshot.state,
                        snapshot.version,
                        OrchestrationState.ACTION_PROPOSED,
                        event_type="ACTION_PROPOSED",
                        payload={
                            "action_proposal_id": proposal_record.action_proposal_id,
                            "executed": False,
                        },
                    )
                    self._assert_workspace_unchanged(handoff)
                    return self._phase_result(
                        run_id,
                        handoff,
                        snapshot,
                        started,
                        plan_record,
                        proposal_record,
                    )
                if isinstance(decision, CoderCompleteV1):
                    self._validate_coder_common(decision, snapshot.active_task_id, plan_record)
                    snapshot = self.lifecycle.transition(
                        run_id,
                        snapshot.state,
                        snapshot.version,
                        OrchestrationState.VERIFICATION_REQUIRED,
                        event_type="VERIFICATION_REQUIRED",
                        payload={"claimed_outcome": decision.claimed_outcome},
                    )
                    self._assert_workspace_unchanged(handoff)
                    return self._phase_result(
                        run_id, handoff, snapshot, started, plan_record, None
                    )
                if isinstance(decision, CoderReplanV1):
                    self._validate_coder_common(decision, snapshot.active_task_id, plan_record)
                    self.records.validate_evidence_references(
                        task_id=snapshot.active_task_id,
                        source_revision=handoff.source_revision,
                        evidence_ids=set(decision.evidence_ids),
                    )
                    replan_count += 1
                    if replan_count > 2:
                        raise OrchestrationError(
                            "REPLAN_LIMIT_EXCEEDED", "Coder exceeded the bounded replan limit"
                        )
                    snapshot = self.lifecycle.transition(
                        run_id,
                        snapshot.state,
                        snapshot.version,
                        OrchestrationState.REPLANNING,
                        event_type="REPLAN_REQUESTED",
                        payload={"evidence_ids": decision.evidence_ids},
                    )
                    snapshot = self.lifecycle.transition(
                        run_id,
                        snapshot.state,
                        snapshot.version,
                        OrchestrationState.PLANNING,
                        event_type="REPLANNING_STARTED",
                    )
                    continue
                if isinstance(decision, NeedCapabilityV1):
                    snapshot = self.lifecycle.transition(
                        run_id,
                        snapshot.state,
                        snapshot.version,
                        OrchestrationState.NEEDS_CAPABILITY,
                        event_type="CAPABILITY_REQUIRED",
                        stop_reason_code="NEEDS_CAPABILITY",
                        payload={"capability": decision.capability},
                    )
                    self._assert_workspace_unchanged(handoff)
                    return self._phase_result(
                        run_id, handoff, snapshot, started, plan_record, None
                    )
                raise OrchestrationError("CODER_DECISION_INVALID", "Unsupported coder decision")
        except BudgetExhaustedError:
            self._move_to_budget_exhausted(run_id)
            raise
        except KeyboardInterrupt:
            self._move_to_cancelled(run_id)
            raise
        except Exception as exc:
            self._move_to_failed(run_id, getattr(exc, "code", "ORCHESTRATION_FAILED"))
            raise
        finally:
            try:
                self._assert_workspace_unchanged(handoff)
            finally:
                self._current_lease = None
                self.leases.release(lease)

    def next(self, run_id: str) -> str:
        return self.lifecycle.get(run_id).state.value

    def reconcile(self, run_id: str) -> Dict[str, int]:
        self.lifecycle.initialize(run_id)
        lease = self.leases.acquire(run_id, self.owner_id, ttl_seconds=30)
        try:
            return self._reconcile_model_calls(run_id)
        finally:
            self.leases.release(lease)

    def cancel(self, run_id: str) -> LifecycleSnapshot:
        snapshot = self.lifecycle.get(run_id)
        if OrchestrationState.CANCELLED not in self._allowed(snapshot.state):
            raise OrchestrationError(
                "CANCELLATION_NOT_ALLOWED", f"Cannot cancel state {snapshot.state.value}"
            )
        with self._active_lock:
            adapter = self._active_adapter
            call_id = self._active_call_id
        cancelled = self.lifecycle.transition(
            run_id,
            snapshot.state,
            snapshot.version,
            OrchestrationState.CANCELLED,
            event_type="ORCHESTRATION_CANCELLED",
            stop_reason_code="CANCELLED_BY_USER",
        )
        if adapter is not None and call_id is not None:
            adapter.cancel(call_id)
        return cancelled

    def _adapter(self, resolved: ResolvedProfile) -> ModelAdapter:
        if self.injected_adapter is not None:
            adapter_profile = getattr(self.injected_adapter, "profile", None)
            if adapter_profile is not None and (
                adapter_profile.profile_fingerprint
                != resolved.contract.profile_fingerprint
            ):
                raise OrchestrationError(
                    "MODEL_PROFILE_CONFLICT", "Injected adapter profile differs from run profile"
                )
            return self.injected_adapter
        self.profile_resolver.validate_live(resolved)
        return OpenAICompatibleModelAdapter(resolved)

    def _planning_phase(
        self,
        *,
        run_id: str,
        snapshot: LifecycleSnapshot,
        handoff: VerifiedHandoff,
        resolved: ResolvedProfile,
        adapter: ModelAdapter,
        evidence: List[EvidenceResult],
        previous_plan: Optional[PlanRecord],
        cycle: int,
    ) -> Tuple[Optional[PlanRecord], Optional[OrchestrationState]]:
        seen_queries: set[str] = set()
        evidence_rounds = 0
        plan_repairs = 0
        planner_feedback: List[Dict[str, Any]] = []
        # A replan must carry the host-owned identity of the NEXT revision; the previous plan
        # shown in the packet otherwise invites the model to echo its old revision number.
        identity_hint: List[Dict[str, Any]] = []
        if previous_plan is not None:
            identity = self.records.plan_identity(snapshot.active_task_id)
            identity_hint = [{
                "kind": "schema_feedback",
                "pair_id": f"plan-identity-cycle-{cycle}",
                "error": "REPLAN_IDENTITY",
                "instruction": (
                    f"This is a replan. A PLAN_READY decision must use task_id={identity['task_id']}, "
                    f"task_revision={identity['task_revision']} and plan_revision={identity['plan_revision']} "
                    "(the previous plan shown is an older revision)."
                ),
            }]
        while True:
            outcome = self._call_role(
                run_id=run_id,
                task_id=snapshot.active_task_id,
                role=Role.PLANNER,
                purpose=f"planner-cycle-{cycle}-round-{evidence_rounds}-repair-{plan_repairs}",
                resolved=resolved,
                adapter=adapter,
                source_revision=handoff.source_revision,
                evidence=evidence,
                plan=previous_plan.plan if previous_plan else None,
                history=identity_hint + planner_feedback,
            )
            decision = outcome.parsed
            if isinstance(decision, PlanV1):
                try:
                    record = self.records.put_plan(
                        run_id=run_id,
                        task_id=snapshot.active_task_id,
                        source_revision=handoff.source_revision,
                        planner_call_id=outcome.call_id,
                        plan=decision,
                    )
                except PlanRevisionError as exc:
                    # A wrong identity field or an unknown evidence ID is a correctable model
                    # mistake: bounded feedback (two repairs), then the typed error stands.
                    plan_repairs += 1
                    if plan_repairs > 2:
                        raise
                    identity = self.records.plan_identity(snapshot.active_task_id)
                    planner_feedback = [{
                        "kind": "schema_feedback",
                        "pair_id": f"plan-repair-{plan_repairs}",
                        "error": exc.code,
                        "instruction": (
                            f"The plan was rejected by the host: {exc}. Return the corrected PLAN_READY with "
                            f"task_id={identity['task_id']}, task_revision={identity['task_revision']}, "
                            f"plan_revision={identity['plan_revision']}, citing only evidence_ids shown in this packet."
                        ),
                    }]
                    continue
                return record, None
            if isinstance(decision, PlannerNeedsEvidenceV1):
                if evidence_rounds >= 3:
                    raise OrchestrationError(
                        "PLANNER_EVIDENCE_ROUND_LIMIT",
                        "Planner requested more than three initial evidence rounds",
                    )
                evidence_rounds += 1
                admitted = 0
                for query in decision.queries:
                    identity = canonical_json(query.model_dump(mode="json"))
                    if identity in seen_queries:
                        continue
                    seen_queries.add(identity)
                    for result in self.retriever.query(
                        run_id,
                        snapshot.active_task_id,
                        handoff.source_revision,
                        query,
                    ):
                        if all(
                            existing.reference.evidence_id != result.reference.evidence_id
                            for existing in evidence
                        ):
                            evidence.append(result)
                            admitted += 1
                if evidence_rounds >= 3:
                    # PRD 2 allows three initial evidence rounds. Say so before the final planner
                    # call, so it plans from what it has instead of asking again and aborting.
                    planner_feedback = [{
                        "kind": "schema_feedback",
                        "pair_id": f"evidence-round-{evidence_rounds}",
                        "error": "EVIDENCE_ROUNDS_EXHAUSTED",
                        "instruction": (
                            "All three evidence rounds are used; another evidence request will stop the task. "
                            "Return PLAN_READY now from the evidence shown (state uncertain points as hypotheses; "
                            "the coder can read further files while acting), or NEEDS_INPUT only if the task "
                            "cannot be planned at all."
                        ),
                    }]
                elif admitted == 0:
                    # Bounded recovery: tell the planner why nothing new came back instead of
                    # abandoning the task on one malformed or redundant request.
                    planner_feedback = [{
                        "kind": "schema_feedback",
                        "pair_id": f"evidence-round-{evidence_rounds}",
                        "error": "EVIDENCE_REQUEST_ADMITTED_NOTHING_NEW",
                        "instruction": (
                            "Those queries returned no new evidence (they were duplicates of evidence already shown, "
                            "matched nothing, or used the wrong query_type). Use PATH_GLOB with an exact path from the "
                            "repository map to read a file, IDENTIFIER/SYMBOL_DEFINITION for code names, EXACT_TEXT only "
                            "for literal text inside files. Otherwise return PLAN_READY using the evidence you already have."
                        ),
                    }]
                else:
                    planner_feedback = []
                continue
            if isinstance(decision, PlannerNeedsInputV1):
                self.lifecycle.transition(
                    run_id,
                    OrchestrationState.PLANNING,
                    self.lifecycle.get(run_id).version,
                    OrchestrationState.NEEDS_INPUT,
                    event_type="USER_INPUT_REQUIRED",
                    stop_reason_code="NEEDS_INPUT",
                    payload={"questions": [q.model_dump() for q in decision.questions]},
                )
                return None, OrchestrationState.NEEDS_INPUT
            if isinstance(decision, NeedCapabilityV1):
                self.lifecycle.transition(
                    run_id,
                    OrchestrationState.PLANNING,
                    self.lifecycle.get(run_id).version,
                    OrchestrationState.NEEDS_CAPABILITY,
                    event_type="CAPABILITY_REQUIRED",
                    stop_reason_code="NEEDS_CAPABILITY",
                    payload={"capability": decision.capability},
                )
                return None, OrchestrationState.NEEDS_CAPABILITY
            raise OrchestrationError("PLANNER_DECISION_INVALID", "Unsupported planner decision")

    def _call_role(
        self,
        *,
        run_id: str,
        task_id: str,
        role: Role,
        purpose: str,
        resolved: ResolvedProfile,
        adapter: ModelAdapter,
        source_revision: str,
        evidence: Sequence[EvidenceResult],
        plan: Optional[BaseModel] = None,
        phase: str = "PRD2",
        authorization_policy: Optional[Mapping[str, Any]] = None,
        history: Sequence[Mapping[str, Any]] = (),
        extra_sections: Sequence[Mapping[str, Any]] = (),
        candidate_version: Optional[str] = None,
        protect_future: bool = True,
    ) -> CallOutcome:
        replay = self._replay_settled(run_id, task_id, role, purpose)
        if replay is not None:
            return replay
        schema_errors: List[Mapping[str, Any]] = []
        for format_retry in range(3):
            if self._current_lease is None:
                raise OrchestrationError(
                    "RUN_LEASE_UNAVAILABLE", "Model call requires an active run lease"
                )
            self._current_lease = self.leases.renew(
                self._current_lease, ttl_seconds=300
            )
            built = self.context_builder.build(
                run_id=run_id,
                task_id=task_id,
                role=role,
                purpose=f"{purpose}-format-{format_retry}",
                profile=resolved.contract,
                source_revision=source_revision,
                evidence=evidence,
                plan=plan,
                history=[*history, *schema_errors],
                remaining_budget=self._remaining_budget_view(run_id),
                candidate_version=candidate_version,
                authorization_policy=authorization_policy or DEFAULT_AUTHORIZATION_POLICY,
                phase=phase,
                extra_sections=extra_sections,
            )
            call_id = f"mcall_{uuid.uuid4().hex[:16]}"
            schema = self.schema_registry.schema(role)
            request_payload = {
                "schema_version": "1.0",
                "call_id": call_id,
                "role": role.value,
                "profile_fingerprint": resolved.contract.profile_fingerprint,
                "packet_id": built.contract.packet_id,
                "messages": built.messages,
                "response_schema": schema,
                "max_output_tokens": resolved.contract.max_output_tokens,
            }
            request_json = canonical_json(request_payload)
            request_path = f"prd2/model/requests/{call_id}.json"
            self.artifact_store.write_bytes(
                run_id,
                request_path,
                request_json.encode("utf-8"),
                "application/json",
                "model_request",
                task_id,
            )
            self.budgets.reserve(
                run_id=run_id,
                task_id=task_id,
                packet_id=built.contract.packet_id,
                call_id=call_id,
                role=role,
                attempt_no=1,
                profile_fingerprint=resolved.contract.profile_fingerprint,
                request_sha256=hashlib.sha256(request_json.encode("utf-8")).hexdigest(),
                input_tokens=built.contract.budget.estimated_input_tokens,
                output_tokens=resolved.contract.max_output_tokens,
                protect_future=protect_future,
            )
            if isinstance(adapter, OpenAICompatibleModelAdapter):
                try:
                    adapter.credential_provider.get_ai_api_key()
                except ModelAuthMissingError:
                    self.budgets.release_intent(call_id, "MODEL_AUTH_MISSING")
                    raise
            self.budgets.mark_in_flight(call_id)
            try:
                with self._active_lock:
                    self._active_adapter = adapter
                    self._active_call_id = call_id
                response = adapter.generate(
                    ModelCallRequest(
                        call_id=call_id,
                        role=role,
                        messages=built.messages,
                        response_schema=schema,
                        max_output_tokens=resolved.contract.max_output_tokens,
                    )
                )
            except ModelAdapterError as exc:
                self.budgets.settle(
                    call_id,
                    state="UNKNOWN" if exc.uncertain_usage else "FAILED",
                    input_tokens=0,
                    output_tokens=0,
                    usage_source="unknown" if exc.uncertain_usage else "estimated",
                    error_code=exc.code,
                )
                raise
            except KeyboardInterrupt:
                try:
                    adapter.cancel(call_id)
                finally:
                    self.budgets.settle(
                        call_id,
                        state="UNKNOWN",
                        input_tokens=0,
                        output_tokens=0,
                        usage_source="unknown",
                        error_code="CANCELLED_BY_USER",
                    )
                raise
            finally:
                with self._active_lock:
                    if self._active_call_id == call_id:
                        self._active_adapter = None
                        self._active_call_id = None
            filtered_raw = self.context_builder.firewall.filter_text(response.raw_text)
            response_payload = {
                "schema_version": "1.0",
                "call_id": call_id,
                "role": role.value,
                "raw_text": filtered_raw,
                "filtering": "secret_and_control_character_filter_v1",
                "usage": {
                    "input_tokens": response.input_tokens,
                    "output_tokens": response.output_tokens,
                    "usage_source": response.usage_source,
                },
                "provider_request_id": response.provider_request_id,
                "latency_ms": response.latency_ms,
            }
            response_json = canonical_json(response_payload)
            response_path = f"prd2/model/responses/{call_id}.json"
            self.artifact_store.write_bytes(
                run_id,
                response_path,
                response_json.encode("utf-8"),
                "application/json",
                "model_response",
                task_id,
            )
            response_artifact = self.artifact_store.get_artifact_by_path(run_id, response_path)
            if not response_artifact:
                raise RuntimeError("Model response artifact metadata was not persisted")
            try:
                parsed = self.schema_registry.validate(role, filtered_raw)
            except RoleSchemaError as exc:
                self.budgets.settle(
                    call_id,
                    state="FAILED",
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                    usage_source=response.usage_source,
                    response_artifact_id=response_artifact["artifact_id"],
                    provider_request_id=response.provider_request_id,
                    latency_ms=response.latency_ms,
                    error_code="ROLE_SCHEMA_INVALID",
                )
                error_payload = {
                    "call_id": call_id,
                    "role": role.value,
                    "format_retry": format_retry,
                    "error": str(exc)[:4000],
                }
                self.artifact_store.write_json(
                    run_id,
                    f"prd2/model/schema-errors/{call_id}.json",
                    error_payload,
                    "role_schema_error",
                    task_id,
                )
                if format_retry >= 2:
                    raise OrchestrationError(
                        "ROLE_SCHEMA_RETRY_EXHAUSTED",
                        f"{role.value} returned invalid structured output three times",
                    ) from exc
                truncated = getattr(response, "finish_reason", None) == "length"
                schema_errors.append(
                    {
                        "kind": "schema_feedback",
                        "pair_id": f"format-{format_retry}",
                        "call_id": call_id,
                        "error": str(exc)[:1000],
                        "instruction": (
                            "Your previous reply was cut off at the output-token limit. Return one much shorter JSON "
                            "object matching the supplied schema (keep python_action small; no commentary)."
                            if truncated else
                            "Return one corrected JSON object matching the supplied schema: raw JSON only, "
                            "no Markdown fences, no prose, every required field present."
                        ),
                    }
                )
                continue
            parsed_json = canonical_json(parsed.model_dump(mode="json"))
            parsed_hash = hashlib.sha256(parsed_json.encode("utf-8")).hexdigest()
            self.budgets.settle(
                call_id,
                state="SUCCEEDED",
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                usage_source=response.usage_source,
                response_artifact_id=response_artifact["artifact_id"],
                parsed_output_sha256=parsed_hash,
                provider_request_id=response.provider_request_id,
                latency_ms=response.latency_ms,
            )
            return CallOutcome(parsed, call_id, built)
        raise AssertionError("Unreachable format retry loop")

    def _remaining_budget_view(self, run_id: str) -> Dict[str, Any]:
        return self.budgets.get(run_id)

    def _replay_settled(
        self, run_id: str, task_id: str, role: Role, purpose: str
    ) -> Optional[CallOutcome]:
        with self.run_store.get_connection() as conn:
            row = conn.execute(
                """
                SELECT c.call_id, c.response_artifact_id
                FROM h_model_calls c
                JOIN h_context_packets p ON p.packet_id = c.packet_id
                WHERE c.run_id = ? AND c.task_id = ? AND c.role = ?
                  AND p.purpose LIKE ? AND c.state = 'SUCCEEDED'
                ORDER BY c.created_at DESC LIMIT 1
                """,
                (run_id, task_id, role.value, f"{purpose}-format-%"),
            ).fetchone()
        if not row or not row["response_artifact_id"]:
            return None
        artifact = self.artifact_store.get_artifact_by_id(row["response_artifact_id"])
        if not artifact:
            return None
        relative = artifact["relative_path"].split("/artifacts/", 1)[-1]
        if not self.artifact_store.verify(run_id, relative):
            raise OrchestrationError(
                "MODEL_RESPONSE_INTEGRITY_FAILURE", "Settled model response artifact is corrupt"
            )
        envelope = json.loads(self.artifact_store.open_readonly(run_id, relative))
        parsed = self.schema_registry.validate(role, envelope["raw_text"])
        return CallOutcome(parsed, row["call_id"], None)

    def _seed_evidence(
        self, run_id: str, task_id: str, source_revision: str
    ) -> List[EvidenceResult]:
        with self.run_store.get_connection() as conn:
            task = conn.execute(
                "SELECT task_spec_json FROM h_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        if not task:
            raise KeyError(task_id)
        spec = json.loads(task["task_spec_json"])
        text = f"{spec.get('title', '')}\n{spec.get('body', '')}"
        queries: List[EvidenceQueryV1] = []
        # Files the task names explicitly (paths or pytest node IDs) are the strongest
        # evidence; fetch them up front instead of spending a planner round-trip.
        named_paths: List[str] = []
        for match in re.findall(r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.(?:py|pyi|js|jsx|ts|tsx|go|rs|java|rb|toml|cfg|ini|json|ya?ml|md))(?:::[\w\[\]-]+)*", text):
            path = match.removeprefix("./")
            if ".." in path.split("/") or path in named_paths:
                continue
            named_paths.append(path)
        for path in named_paths[:4]:
            queries.append(EvidenceQueryV1(query_type="PATH_GLOB", query=path, max_results=1, max_bytes=16000))
        stack_locations = re.findall(r"[A-Za-z0-9_./\\-]+:\d+", text)
        for location in stack_locations[:3]:
            queries.append(
                EvidenceQueryV1(
                    query_type="STACK_TRACE_LOCATION", query=location, max_results=5
                )
            )
        identifiers = []
        for token in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b", text):
            if token.lower() in {
                "the", "and", "for", "with", "this", "that", "from", "should",
                "when", "then", "fix", "issue", "task", "return",
            }:
                continue
            if token not in identifiers:
                identifiers.append(token)
        for identifier in identifiers[:6]:
            queries.append(
                EvidenceQueryV1(
                    query_type="IDENTIFIER", query=identifier, max_results=6, max_bytes=24000
                )
            )
            queries.append(
                EvidenceQueryV1(
                    query_type="ADJACENT_TESTS", query=identifier, max_results=3, max_bytes=12000
                )
            )
        queries.append(
            EvidenceQueryV1(
                query_type="MANIFEST_OR_CONFIG", query="runtime", max_results=8, max_bytes=24000
            )
        )
        results: List[EvidenceResult] = []
        seen: set[str] = set()
        for query in queries:
            for result in self.retriever.query(
                run_id, task_id, source_revision, query
            ):
                if result.reference.evidence_id not in seen:
                    results.append(result)
                    seen.add(result.reference.evidence_id)
        return results

    def _phase_result(
        self,
        run_id: str,
        handoff: VerifiedHandoff,
        snapshot: LifecycleSnapshot,
        started: float,
        plan: Optional[PlanRecord],
        proposal: Optional[ProposalRecord],
    ) -> OrchestrationPhaseResultV1:
        budget = self.budgets.get(run_id)
        with self.run_store.get_connection() as conn:
            remaining = conn.execute(
                "SELECT COUNT(*) FROM h_tasks WHERE run_id = ? AND task_id <> ?",
                (run_id, snapshot.active_task_id),
            ).fetchone()[0]
            latest_event = conn.execute(
                "SELECT event_type, payload_json FROM h_events WHERE run_id = ? ORDER BY seq DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        questions = []
        required_capability = None
        if latest_event:
            event_payload = json.loads(latest_event["payload_json"])
            if latest_event["event_type"] == "USER_INPUT_REQUIRED":
                questions = event_payload.get("questions", [])
            elif latest_event["event_type"] == "CAPABILITY_REQUIRED":
                required_capability = event_payload.get("capability")
        return OrchestrationPhaseResultV1(
            run_id=run_id,
            task_id=snapshot.active_task_id,
            status=snapshot.state,
            source_revision=handoff.source_revision,
            plan=(
                PlanReferenceV1(plan_id=plan.plan_id, revision=plan.plan.plan_revision)
                if plan
                else None
            ),
            proposal=(
                ProposalReferenceV1(
                    action_proposal_id=proposal.action_proposal_id,
                    proposal_artifact_id=proposal.proposal_artifact_id,
                    decision="CODE",
                )
                if proposal
                else None
            ),
            usage=PhaseUsageV1(
                model_calls_used=budget["used_calls"],
                input_tokens_used=budget["used_input_tokens"],
                output_tokens_used=budget["used_output_tokens"],
                elapsed_seconds=max(0, int(time.monotonic() - started)),
                verification_calls_reserved=budget["reserved_future_calls"],
            ),
            remaining_repository_tasks=remaining,
            questions=questions,
            required_capability=required_capability,
            next_required_prd=(
                3
                if snapshot.state
                in {
                    OrchestrationState.ACTION_PROPOSED,
                    OrchestrationState.VERIFICATION_REQUIRED,
                }
                else None
            ),
            created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        )

    def _terminal_result_if_any(
        self,
        run_id: str,
        snapshot: LifecycleSnapshot,
        handoff: VerifiedHandoff,
        started: float,
        *,
        stop_at: str,
    ) -> Optional[OrchestrationPhaseResultV1]:
        terminal_states = {
            OrchestrationState.ACTION_PROPOSED,
            OrchestrationState.VERIFICATION_REQUIRED,
            OrchestrationState.NEEDS_INPUT,
            OrchestrationState.NEEDS_CAPABILITY,
            OrchestrationState.BUDGET_EXHAUSTED,
            OrchestrationState.FAILED,
            OrchestrationState.CANCELLED,
        }
        if stop_at == "plan":
            terminal_states.add(OrchestrationState.PLAN_READY)
        if snapshot.state not in terminal_states:
            return None
        plan = self._safe_active_plan(snapshot.active_task_id)
        proposal_row = self.records.get_proposal(run_id, snapshot.active_task_id)
        proposal = (
            ProposalRecord(
                proposal_row["action_proposal_id"],
                proposal_row["proposal_artifact_id"],
                proposal_row["code_artifact_id"],
                proposal_row["code_sha256"],
            )
            if proposal_row
            else None
        )
        return self._phase_result(run_id, handoff, snapshot, started, plan, proposal)

    def _reconcile_model_calls(self, run_id: str) -> Dict[str, int]:
        """Settle durable responses exactly once; never repeat an uncertain call."""
        released = 0
        recovered = 0
        unknown = 0
        with self.run_store.get_connection() as conn:
            calls = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT * FROM h_model_calls
                    WHERE run_id = ? AND state IN ('INTENT', 'IN_FLIGHT')
                    ORDER BY created_at
                    """,
                    (run_id,),
                ).fetchall()
            ]
        for call in calls:
            if call["state"] == "INTENT":
                self.budgets.release_intent(
                    call["call_id"], "RECONCILED_BEFORE_NETWORK"
                )
                released += 1
                continue
            relative = f"prd2/model/responses/{call['call_id']}.json"
            artifact = self.artifact_store.get_artifact_by_path(run_id, relative)
            if not artifact:
                self.budgets.settle(
                    call["call_id"],
                    state="UNKNOWN",
                    input_tokens=0,
                    output_tokens=0,
                    usage_source="unknown",
                    error_code="RECONCILED_IN_FLIGHT_UNKNOWN",
                )
                unknown += 1
                continue
            if not self.artifact_store.verify(run_id, relative):
                raise OrchestrationError(
                    "MODEL_RESPONSE_INTEGRITY_FAILURE",
                    f"Durable response for {call['call_id']} failed verification",
                )
            envelope = json.loads(self.artifact_store.open_readonly(run_id, relative))
            raw_text = envelope.get("raw_text")
            usage = envelope.get("usage") or {}
            try:
                parsed = self.schema_registry.validate(Role(call["role"]), raw_text)
            except RoleSchemaError:
                self.budgets.settle(
                    call["call_id"],
                    state="FAILED",
                    input_tokens=int(usage.get("input_tokens", 0)),
                    output_tokens=int(usage.get("output_tokens", 0)),
                    usage_source=str(usage.get("usage_source", "estimated")),
                    response_artifact_id=artifact["artifact_id"],
                    provider_request_id=envelope.get("provider_request_id"),
                    latency_ms=int(envelope.get("latency_ms", 0)),
                    error_code="ROLE_SCHEMA_INVALID",
                )
                recovered += 1
                continue
            parsed_json = canonical_json(parsed.model_dump(mode="json"))
            self.budgets.settle(
                call["call_id"],
                state="SUCCEEDED",
                input_tokens=int(usage.get("input_tokens", 0)),
                output_tokens=int(usage.get("output_tokens", 0)),
                usage_source=str(usage.get("usage_source", "estimated")),
                response_artifact_id=artifact["artifact_id"],
                parsed_output_sha256=hashlib.sha256(parsed_json.encode("utf-8")).hexdigest(),
                provider_request_id=envelope.get("provider_request_id"),
                latency_ms=int(envelope.get("latency_ms", 0)),
            )
            recovered += 1
        return {
            "released_intents": released,
            "recovered_responses": recovered,
            "unknown_calls": unknown,
        }

    def _safe_active_plan(self, task_id: str) -> Optional[PlanRecord]:
        try:
            return self.records.get_active_plan(task_id)
        except KeyError:
            return None

    def _replan_count(self, run_id: str, task_id: str) -> int:
        with self.run_store.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT payload_json FROM h_events
                WHERE run_id = ? AND event_type = 'REPLAN_REQUESTED'
                ORDER BY seq
                """,
                (run_id,),
            ).fetchall()
        return sum(
            1
            for row in rows
            if json.loads(row["payload_json"]).get("active_task_id") == task_id
        )

    @staticmethod
    def _validate_coder_identity(
        decision: CoderDecisionV1,
        task_id: str,
        source_revision: str,
        plan: PlanRecord,
    ) -> None:
        OrchestrationController._validate_coder_common(decision, task_id, plan)
        if decision.workspace_version != source_revision:
            raise OrchestrationError(
                "STALE_WORKSPACE_VERSION", "Coder proposal targets a different workspace version"
            )

    @staticmethod
    def _validate_coder_common(decision: Any, task_id: str, plan: PlanRecord) -> None:
        if decision.task_id != task_id:
            raise OrchestrationError("CODER_TASK_MISMATCH", "Coder returned a different task ID")
        if decision.task_revision != plan.plan.task_revision:
            raise OrchestrationError("CODER_TASK_REVISION_STALE", "Coder task revision is stale")
        if decision.plan_revision != plan.plan.plan_revision:
            raise OrchestrationError("CODER_PLAN_REVISION_STALE", "Coder plan revision is stale")

    def _assert_workspace_unchanged(self, handoff: VerifiedHandoff) -> None:
        current = self.handoff_verifier.compute_workspace_manifest(handoff.workspace_root)
        if self.handoff_verifier.manifest_sha(current) != handoff.manifest_sha256:
            raise OrchestrationError(
                "PRD2_SOURCE_MUTATION_DETECTED",
                "PRD 2 changed the prepared repository, violating its stop boundary",
            )

    def _move_to_budget_exhausted(self, run_id: str) -> None:
        try:
            snapshot = self.lifecycle.get(run_id)
            if OrchestrationState.BUDGET_EXHAUSTED in self._allowed(snapshot.state):
                self.lifecycle.transition(
                    run_id,
                    snapshot.state,
                    snapshot.version,
                    OrchestrationState.BUDGET_EXHAUSTED,
                    event_type="BUDGET_EXHAUSTED",
                    stop_reason_code="MODEL_BUDGET_EXHAUSTED",
                )
        except Exception:
            return

    def _move_to_failed(self, run_id: str, code: str) -> None:
        try:
            snapshot = self.lifecycle.get(run_id)
            if OrchestrationState.FAILED in self._allowed(snapshot.state):
                self.lifecycle.transition(
                    run_id,
                    snapshot.state,
                    snapshot.version,
                    OrchestrationState.FAILED,
                    event_type="ORCHESTRATION_FAILED",
                    stop_reason_code=code,
                )
        except Exception:
            return

    def _move_to_cancelled(self, run_id: str) -> None:
        try:
            snapshot = self.lifecycle.get(run_id)
            if OrchestrationState.CANCELLED in self._allowed(snapshot.state):
                self.lifecycle.transition(
                    run_id,
                    snapshot.state,
                    snapshot.version,
                    OrchestrationState.CANCELLED,
                    event_type="ORCHESTRATION_CANCELLED",
                    stop_reason_code="CANCELLED_BY_USER",
                )
        except Exception:
            return

    @staticmethod
    def _allowed(state: OrchestrationState) -> set[OrchestrationState]:
        from harness.orchestration.lifecycle import ALLOWED_LIFECYCLE_TRANSITIONS

        return ALLOWED_LIFECYCLE_TRANSITIONS.get(state, set())
