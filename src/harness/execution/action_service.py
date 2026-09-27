"""End-to-end transaction boundaries for one proposed action (PRD 3 section 12).

``execute_proposal`` admits a PRD 2 proposal under policy, persists intent and
budget reservations atomically, stages scoped context, runs a fresh hardened
container, independently scans the workspace, and either checkpoints a new
workspace version or restores the exact pre-action state. Settlement, result,
file changes, tool calls, budget, and events commit in one transaction and the
unique proposal/action/result constraints make it idempotent.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from harness.contracts.execution import (
    ActionAdmissionRequestV1,
    ContainerIdentityV1,
    ExecutionArtifactsV1,
    ExecutionBudgetChargeV1,
    ExecutionResultV1,
    HashedPathV1,
    NetworkMode,
    PermissionProfile,
    PolicyDecisionKind,
    PolicyDecisionV1,
    ProcessOutcomeV1,
    SandboxExecutionRequestV1,
    SandboxLimitsV1,
    Settlement,
    ToolCallEventV1,
    WorkerResultSummaryV1,
    WorkspaceChangeSummaryV1,
)
from harness.execution.budgets import ExecutionBudgetExhaustedError, ExecutionBudgetLedger
from harness.execution.change_inspector import ChangeClassification, ChangeInspector
from harness.persistence.events import append_event_sql
from harness.persistence import ArtifactStore, RunStore, canonical_json
from harness.policy import (
    AdmissionFacts,
    ApprovalError,
    ApprovalService,
    PolicyEngine,
    PolicySnapshot,
)
from harness.policy.limits import SandboxLimits
from harness.sandbox import (
    ContainerLimits,
    ContainerOutcome,
    ContainerSpec,
    DockerBackend,
    Mount,
    ResolvedRuntime,
    RuntimeProfileResolver,
    SandboxCommunicationError,
    SandboxError,
    SandboxSettingsError,
    SandboxUnavailableError,
    host_architecture,
)
from harness.workspace.manifest import ManifestLimitError, WorkspaceManifest, diff_manifests, is_disposable
from harness.workspace.task_workspace import (
    RestoreVerificationError,
    TaskWorkspaceService,
    WorkspaceIntegrityError,
    WorkspaceLockService,
    WorkspaceVersion,
)

SANDBOX_ENV_BASE = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "HOME": "/tmp",
    "TMPDIR": "/tmp",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
    "PYTEST_ADDOPTS": "-p no:cacheprovider",
    "PIP_NO_INPUT": "1",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "HARNESS_CONTEXT_MANIFEST": "/context/manifest.json",
    "HARNESS_OUTPUT_DIR": "/output",
    "HARNESS_SHELL": "/bin/bash",
}
TERMINAL_ACTION_STATES = {"SUCCEEDED", "FAILED", "POLICY_VIOLATION", "CANCELLED", "DENIED", "STALE"}
EXIT_INTEGRITY = 97


class ActionServiceError(RuntimeError):
    code = "ACTION_SERVICE_ERROR"


class ActionUnknownError(ActionServiceError):
    code = "ACTION_UNKNOWN"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _sha(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _event(conn: sqlite3.Connection, run_id: str, event_type: str, payload: Dict[str, Any]) -> None:
    append_event_sql(conn, run_id, event_type, payload, "PRD3", "PRD3", _now())


@dataclass
class ActionExecution:
    kind: str  # EXECUTED, STALE, DENIED, NEEDS_APPROVAL, UNKNOWN
    action_id: str
    decision: Optional[PolicyDecisionV1]
    result: Optional[ExecutionResultV1] = None
    approval_request_id: Optional[str] = None
    reason_codes: List[str] = field(default_factory=list)
    failure_signature: Optional[str] = None
    repeated_failure: bool = False
    feedback: Dict[str, Any] = field(default_factory=dict)
    changed_paths: List[str] = field(default_factory=list)
    old_version: Optional[str] = None
    new_version: Optional[str] = None


class ActionService:
    def __init__(
        self,
        *,
        run_store: RunStore,
        artifact_store: ArtifactStore,
        data_root: Path,
        backend: DockerBackend,
        runtime_resolver: RuntimeProfileResolver,
        policy_engine: PolicyEngine,
        workspaces: TaskWorkspaceService,
        limits: SandboxLimits = SandboxLimits(),
        permission_profile: PermissionProfile = PermissionProfile.SANDBOX,
    ) -> None:
        self.run_store = run_store
        self.artifact_store = artifact_store
        self.data_root = Path(data_root).resolve()
        self.backend = backend
        self.runtime_resolver = runtime_resolver
        self.policy = policy_engine
        self.workspaces = workspaces
        self.locks = WorkspaceLockService(run_store)
        self.approvals = ApprovalService(run_store, artifact_store)
        self.budgets = ExecutionBudgetLedger(run_store)
        self.inspector = ChangeInspector()
        self.limits = limits
        self.permission_profile = permission_profile
        self._runtime: Optional[ResolvedRuntime] = None

    # ------------------------------------------------------------ runtime
    def runtime(self) -> ResolvedRuntime:
        if self._runtime is None:
            self._runtime = self.runtime_resolver.resolve()
        return self._runtime

    def ensure_runtime_row(self, run_id: str, runtime: ResolvedRuntime) -> str:
        with self.run_store.get_connection() as conn:
            row = conn.execute(
                "SELECT runtime_profile_row_id, profile_fingerprint FROM h_runtime_profiles WHERE run_id = ? AND runtime_profile_id = ?",
                (run_id, runtime.contract.runtime_profile_id),
            ).fetchone()
            if row:
                if row["profile_fingerprint"] != runtime.fingerprint:
                    raise SandboxSettingsError(
                        "Runtime profile changed during the run; the frozen run profile differs"
                    )
                return row["runtime_profile_row_id"]
            row_id = f"rtp_{uuid.uuid4().hex[:16]}"
            with conn:
                conn.execute(
                    """
                    INSERT INTO h_runtime_profiles(
                        runtime_profile_row_id, run_id, runtime_profile_id, runtime_name,
                        runtime_version, image_reference, image_digest, worker_version,
                        tool_library_version, limits_json, profile_fingerprint, created_at
                    ) VALUES (?, ?, ?, 'python', ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row_id, run_id, runtime.contract.runtime_profile_id, runtime.contract.runtime_version,
                        runtime.contract.image_reference, runtime.contract.image_digest,
                        runtime.contract.worker_version, runtime.contract.tool_library_version,
                        canonical_json(runtime.limits.as_dict()), runtime.fingerprint, _now(),
                    ),
                )
        return row_id

    # ------------------------------------------------------------ queries
    def get_action(self, action_id: str) -> Optional[Dict[str, Any]]:
        with self.run_store.get_connection() as conn:
            row = conn.execute("SELECT * FROM h_actions WHERE action_id = ?", (action_id,)).fetchone()
        return dict(row) if row else None

    def action_for_proposal(self, proposal_id: str) -> Optional[Dict[str, Any]]:
        with self.run_store.get_connection() as conn:
            row = conn.execute("SELECT * FROM h_actions WHERE action_proposal_id = ?", (proposal_id,)).fetchone()
        return dict(row) if row else None

    def result_for_action(self, action_id: str) -> Optional[ExecutionResultV1]:
        with self.run_store.get_connection() as conn:
            row = conn.execute("SELECT result_json FROM h_execution_results WHERE action_id = ?", (action_id,)).fetchone()
        return ExecutionResultV1.model_validate_json(row["result_json"]) if row else None

    def settled_actions(self, run_id: str, task_id: str) -> List[Dict[str, Any]]:
        with self.run_store.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT a.*, r.result_json, r.settlement, r.reason_codes_json
                FROM h_actions a JOIN h_execution_results r ON r.action_id = a.action_id
                WHERE a.run_id = ? AND a.task_id = ? ORDER BY r.settled_at, a.rowid
                """,
                (run_id, task_id),
            ).fetchall()
        return [dict(row) for row in rows]

    # ---------------------------------------------------------- admission
    def _facts(self, run_id: str, task_id: str, proposal: Dict[str, Any], runtime: ResolvedRuntime) -> AdmissionFacts:
        version = self.workspaces.current_version(run_id, task_id)
        workspace = self.workspaces.get(run_id, task_id)
        if not workspace or workspace["state"] != "ACTIVE":
            raise WorkspaceIntegrityError("Task workspace is not ACTIVE (quarantined or retired)")
        with self.run_store.get_connection() as conn:
            task = conn.execute("SELECT * FROM h_task_lifecycle WHERE task_id = ?", (task_id,)).fetchone()
            lifecycle = conn.execute("SELECT * FROM h_run_lifecycle WHERE run_id = ?", (run_id,)).fetchone()
            plan = conn.execute(
                "SELECT plan_id, plan_revision FROM h_plans WHERE task_id = ? AND state = 'ACTIVE'", (task_id,)
            ).fetchone()
        code_sha: Optional[str] = None
        artifact = self.artifact_store.get_artifact_by_id(proposal["code_artifact_id"])
        if artifact:
            relative = artifact["relative_path"].split("/artifacts/", 1)[-1]
            if self.artifact_store.verify(run_id, relative):
                code_sha = hashlib.sha256(self.artifact_store.open_readonly(run_id, relative)).hexdigest()
        budget = self.budgets.get(run_id)
        return AdmissionFacts(
            proposal_state=proposal["state"],
            task_revision=task["task_revision"] if task else 0,
            lifecycle_state=lifecycle["state"] if lifecycle else "UNKNOWN",
            lifecycle_version=lifecycle["version"] if lifecycle else 0,
            active_plan_id=plan["plan_id"] if plan else None,
            active_plan_revision=plan["plan_revision"] if plan else None,
            active_workspace_version_id=version.version_id,
            active_workspace_commit=version.commit,
            code_bytes_sha256=code_sha,
            runtime_profile_id=runtime.contract.runtime_profile_id,
            runtime_profile_fingerprint=runtime.fingerprint,
            remaining_actions=budget["remaining_actions"],
            remaining_wall_seconds=budget["remaining_wall_seconds"],
        )

    def _request(self, proposal: Dict[str, Any], snapshot_sha: str, runtime: ResolvedRuntime) -> ActionAdmissionRequestV1:
        return ActionAdmissionRequestV1(
            request_id=f"adm_{proposal['action_proposal_id']}",
            action_proposal_id=proposal["action_proposal_id"],
            run_id=proposal["run_id"],
            task_id=proposal["task_id"],
            task_revision=proposal["task_revision"],
            lifecycle_version=proposal["lifecycle_version"],
            plan_id=proposal["plan_id"],
            plan_revision=proposal["plan_revision"],
            workspace_version=proposal["workspace_version"],
            code_artifact_id=proposal["code_artifact_id"],
            code_sha256=proposal["code_sha256"],
            requested_capabilities=json.loads(proposal["requested_capabilities_json"]),
            declared_paths=json.loads(proposal["declared_paths_json"]),
            requested_timeout_seconds=proposal["requested_timeout_seconds"],
            policy_sha256=proposal["authorization_policy_sha256"],
            runtime_profile_id=runtime.contract.runtime_profile_id,
        )

    def admit(
        self, run_id: str, task_id: str, proposal_id: str, *, snapshot: PolicySnapshot
    ) -> Tuple[PolicyDecisionV1, Dict[str, Any], Dict[str, Any], ResolvedRuntime]:
        """Evaluate a proposal; persists nothing. Returns (decision, binding, proposal, runtime)."""
        runtime = self.runtime()
        with self.run_store.get_connection() as conn:
            proposal = conn.execute(
                "SELECT * FROM h_action_proposals WHERE action_proposal_id = ? AND run_id = ? AND task_id = ?",
                (proposal_id, run_id, task_id),
            ).fetchone()
        if not proposal:
            raise ActionServiceError(f"Unknown proposal {proposal_id}")
        proposal = dict(proposal)
        facts = self._facts(run_id, task_id, proposal, runtime)
        existing = self.action_for_proposal(proposal_id)
        if existing and existing["state"] == "NEEDS_APPROVAL":
            binding = self.approvals.active_binding(existing["action_id"])
            facts = AdmissionFacts(**{**facts.__dict__, "approved_binding_sha256": binding, "proposal_state": "UNEXECUTED"})
        request = self._request(proposal, snapshot.contract.policy_sha256, runtime)
        decision, binding = self.policy.evaluate(request, snapshot, facts)
        return decision, binding, proposal, runtime

    # ------------------------------------------------------------ execute
    def execute_proposal(
        self,
        run_id: str,
        task_id: str,
        proposal_id: str,
        *,
        owner_id: str,
        stage_artifact_ids: Sequence[str] = (),
        cancel_event: Optional[threading.Event] = None,
        dependency_site: Optional[Path] = None,
        dependency_environment_id: Optional[str] = None,
    ) -> ActionExecution:
        existing = self.action_for_proposal(proposal_id)
        if existing and existing["state"] in TERMINAL_ACTION_STATES:
            return self._replay(existing)
        if existing and existing["state"] in ("INTENT", "RUNNING", "SETTLING", "UNKNOWN"):
            self.reconcile(run_id)
            existing = self.action_for_proposal(proposal_id)
            if existing and existing["state"] in TERMINAL_ACTION_STATES:
                return self._replay(existing)
            raise ActionUnknownError(f"Action {existing['action_id'] if existing else '?'} remains uncertain")

        snapshot = self.policy.ensure_snapshot(run_id, profile=self.permission_profile)
        decision, binding, proposal, runtime = self.admit(run_id, task_id, proposal_id, snapshot=snapshot)
        runtime_row = self.ensure_runtime_row(run_id, runtime)
        action_id = existing["action_id"] if existing else f"act_{uuid.uuid4().hex[:16]}"
        version = self.workspaces.current_version(run_id, task_id)

        if decision.decision in (PolicyDecisionKind.STALE, PolicyDecisionKind.DENIED):
            self._record_rejection(action_id, proposal, snapshot, decision, runtime_row, version, existing is not None)
            return ActionExecution(
                kind=decision.decision.value,
                action_id=action_id,
                decision=decision,
                reason_codes=list(decision.reason_codes),
                feedback=self._rejection_feedback(proposal, decision),
            )
        if decision.decision == PolicyDecisionKind.NEEDS_APPROVAL and existing:
            approval = self.approvals.for_action(action_id)
            if approval and approval["state"] in ("DENIED", "EXPIRED", "REVOKED"):
                denied = decision.model_copy(update={
                    "decision": PolicyDecisionKind.DENIED,
                    "reason_codes": [f"APPROVAL_{approval['state']}"],
                })
                self._record_rejection(action_id, proposal, snapshot, denied, runtime_row, version, True)
                feedback = self._rejection_feedback(proposal, denied)
                if approval.get("denial_reason"):
                    feedback["user_denial_reason"] = approval["denial_reason"][:500]
                return ActionExecution(
                    kind="DENIED",
                    action_id=action_id,
                    decision=denied,
                    approval_request_id=approval["approval_request_id"],
                    reason_codes=list(denied.reason_codes),
                    feedback=feedback,
                )
        if decision.decision == PolicyDecisionKind.NEEDS_APPROVAL:
            if not existing:
                self._insert_action(action_id, proposal, snapshot, decision, runtime_row, version, "NEEDS_APPROVAL", read_only=False)
            approval = self.approvals.create(
                run_id=run_id,
                task_id=task_id,
                action_id=action_id,
                purpose=self._purpose(proposal),
                binding=binding,
            )
            return ActionExecution(
                kind="NEEDS_APPROVAL",
                action_id=action_id,
                decision=decision,
                approval_request_id=approval["approval_request_id"],
                reason_codes=list(decision.reason_codes),
            )
        return self._run_admitted(
            action_id=action_id,
            proposal=proposal,
            snapshot=snapshot,
            decision=decision,
            binding=binding,
            runtime=runtime,
            runtime_row=runtime_row,
            version=version,
            owner_id=owner_id,
            needs_grant=bool(existing and existing["state"] == "NEEDS_APPROVAL"),
            existing=existing is not None,
            stage_artifact_ids=stage_artifact_ids,
            cancel_event=cancel_event,
            dependency_site=dependency_site,
            dependency_environment_id=dependency_environment_id,
        )

    def _purpose(self, proposal: Dict[str, Any]) -> str:
        try:
            artifact = self.artifact_store.get_artifact_by_id(proposal["proposal_artifact_id"])
            relative = artifact["relative_path"].split("/artifacts/", 1)[-1]
            return json.loads(self.artifact_store.open_readonly(proposal["run_id"], relative)).get("purpose", "")
        except Exception:
            return ""

    def _insert_action(
        self,
        action_id: str,
        proposal: Dict[str, Any],
        snapshot: PolicySnapshot,
        decision: PolicyDecisionV1,
        runtime_row: str,
        version: WorkspaceVersion,
        state: str,
        *,
        read_only: bool,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        now = _now()
        statement = """
            INSERT INTO h_actions(
                action_id, run_id, task_id, action_proposal_id, coder_call_id, plan_id,
                task_revision, plan_revision, lifecycle_version, before_workspace_version_id,
                checkpoint_commit, code_artifact_id, code_sha256, policy_id, policy_decision_sha256,
                decision_json, runtime_profile_row_id, dependency_environment_id, read_only,
                state, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)
        """
        params = (
            action_id, proposal["run_id"], proposal["task_id"], proposal["action_proposal_id"],
            proposal["coder_call_id"], proposal["plan_id"], proposal["task_revision"],
            proposal["plan_revision"], proposal["lifecycle_version"], version.version_id,
            version.commit, proposal["code_artifact_id"], proposal["code_sha256"],
            snapshot.contract.policy_id, decision.decision_sha256,
            canonical_json(decision.model_dump(mode="json")), runtime_row, 1 if read_only else 0,
            state, now, now,
        )
        if conn is not None:
            conn.execute(statement, params)
            return
        with self.run_store.get_connection() as own:
            with own:
                own.execute(statement, params)

    def _record_rejection(
        self,
        action_id: str,
        proposal: Dict[str, Any],
        snapshot: PolicySnapshot,
        decision: PolicyDecisionV1,
        runtime_row: str,
        version: WorkspaceVersion,
        exists: bool,
    ) -> None:
        state = decision.decision.value  # STALE or DENIED
        with self.run_store.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if exists:
                    conn.execute(
                        "UPDATE h_actions SET state = ?, decision_json = ?, policy_decision_sha256 = ?, updated_at = ? WHERE action_id = ?",
                        (state, canonical_json(decision.model_dump(mode="json")), decision.decision_sha256, _now(), action_id),
                    )
                else:
                    self._insert_action(action_id, proposal, snapshot, decision, runtime_row, version, state, read_only=False, conn=conn)
                conn.execute(
                    "UPDATE h_action_proposals SET state = ? WHERE action_proposal_id = ? AND state = 'UNEXECUTED'",
                    ("STALE" if state == "STALE" else "REJECTED", proposal["action_proposal_id"]),
                )
                _event(conn, proposal["run_id"], f"ACTION_{state}", {
                    "action_id": action_id,
                    "action_proposal_id": proposal["action_proposal_id"],
                    "reason_codes": decision.reason_codes,
                    "executed": False,
                })
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def _rejection_feedback(self, proposal: Dict[str, Any], decision: PolicyDecisionV1) -> Dict[str, Any]:
        return {
            "kind": "action_result",
            "pair_id": proposal["action_proposal_id"],
            "action_proposal_id": proposal["action_proposal_id"],
            "purpose": self._purpose(proposal)[:500],
            "settlement": f"NOT_EXECUTED_{decision.decision.value}",
            "reason_codes": decision.reason_codes,
            "note": "The proposal was not executed. Propose a corrected action within policy.",
        }

    # ------------------------------------------------------ admitted path
    def _run_admitted(
        self,
        *,
        action_id: str,
        proposal: Dict[str, Any],
        snapshot: PolicySnapshot,
        decision: PolicyDecisionV1,
        binding: Dict[str, Any],
        runtime: ResolvedRuntime,
        runtime_row: str,
        version: WorkspaceVersion,
        owner_id: str,
        needs_grant: bool,
        existing: bool,
        stage_artifact_ids: Sequence[str],
        cancel_event: Optional[threading.Event],
        dependency_site: Optional[Path],
        dependency_environment_id: Optional[str],
    ) -> ActionExecution:
        run_id, task_id = proposal["run_id"], proposal["task_id"]
        limits = decision.effective_limits
        reserved_wall = limits.wall_seconds + 10
        reserved_output = limits.stdout_bytes + limits.stderr_bytes
        capabilities = list(decision.normalized_capabilities)
        read_only = not self.policy.registry.mutating(capabilities)
        token = self.locks.acquire(run_id, task_id, version.version_id, owner_id, purpose=f"action:{action_id}")
        try:
            before_manifest = self.workspaces.verify_current(run_id, task_id)
            with self.run_store.get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    if needs_grant:
                        ApprovalService.consume_sql(conn, action_id, PolicyEngine.binding_sha(binding))
                    ExecutionBudgetLedger.reserve_sql(conn, run_id, reserved_wall, reserved_output)
                    if existing:
                        conn.execute(
                            """
                            UPDATE h_actions SET state = 'INTENT', decision_json = ?, policy_decision_sha256 = ?,
                                read_only = ?, dependency_environment_id = ?, updated_at = ?
                            WHERE action_id = ?
                            """,
                            (canonical_json(decision.model_dump(mode="json")), decision.decision_sha256,
                             1 if read_only else 0, dependency_environment_id, _now(), action_id),
                        )
                    else:
                        self._insert_action(action_id, proposal, snapshot, decision, runtime_row, version, "INTENT", read_only=read_only, conn=conn)
                        if dependency_environment_id:
                            conn.execute("UPDATE h_actions SET dependency_environment_id = ? WHERE action_id = ?", (dependency_environment_id, action_id))
                    for name in capabilities:
                        capability = self.policy.registry.get(name)
                        conn.execute(
                            "INSERT OR IGNORE INTO h_action_capabilities VALUES (?, ?, ?, ?, ?)",
                            (action_id, capability.name, capability.version, 1 if capability.mutates_workspace else 0,
                             _sha({"paths": decision.normalized_paths, "limits": limits.model_dump(mode="json")})),
                        )
                    conn.execute(
                        "UPDATE h_action_proposals SET state = 'ADMITTED' WHERE action_proposal_id = ?",
                        (proposal["action_proposal_id"],),
                    )
                    _event(conn, run_id, "EXECUTION_BUDGET_RESERVED", {"action_id": action_id, "wall_seconds": reserved_wall, "output_bytes": reserved_output})
                    _event(conn, run_id, "WORKSPACE_CHECKPOINT_CREATED", {"action_id": action_id, "workspace_version_id": version.version_id, "checkpoint_commit": version.commit})
                    _event(conn, run_id, "ACTION_INTENT_PERSISTED", {
                        "action_id": action_id,
                        "action_proposal_id": proposal["action_proposal_id"],
                        "decision_sha256": decision.decision_sha256,
                        "code_sha256": proposal["code_sha256"],
                        "workspace_version_id": version.version_id,
                        "read_only": read_only,
                    })
                    conn.commit()
                except ExecutionBudgetExhaustedError:
                    conn.rollback()
                    raise
                except BaseException:
                    conn.rollback()
                    raise

            staging = self._stage(action_id, proposal, decision, runtime, version, stage_artifact_ids, dependency_site)
            spec = self._spec(action_id, proposal, runtime, version, staging, read_only, dependency_site, limits)
            instance_id = f"sbx_{uuid.uuid4().hex[:16]}"

            def on_created(container_id: str, settings_sha: str) -> None:
                with self.run_store.get_connection() as conn:
                    with conn:
                        conn.execute(
                            """
                            INSERT INTO h_sandbox_instances(
                                sandbox_instance_id, action_id, attempt_no, engine, engine_version,
                                engine_object_id, container_name, host_architecture, image_digest,
                                container_id_hash, settings_sha256, network_mode, state, created_at
                            ) VALUES (?, ?, 1, 'docker', ?, ?, ?, ?, ?, ?, ?, 'none', 'CREATED', ?)
                            """,
                            (
                                instance_id, action_id, self.backend.engine_version(), container_id, spec.name,
                                host_architecture(), runtime.contract.image_digest,
                                hashlib.sha256(container_id.encode()).hexdigest(), settings_sha, _now(),
                            ),
                        )
                        _event(conn, run_id, "SANDBOX_CREATED", {"action_id": action_id, "container": spec.name})
                        _event(conn, run_id, "SANDBOX_INSPECTED", {"action_id": action_id, "settings_sha256": settings_sha})

            def on_started() -> None:
                with self.run_store.get_connection() as conn:
                    with conn:
                        conn.execute("UPDATE h_actions SET state = 'RUNNING', updated_at = ? WHERE action_id = ?", (_now(), action_id))
                        conn.execute(
                            "UPDATE h_sandbox_instances SET state = 'RUNNING', started_at = ? WHERE sandbox_instance_id = ?",
                            (_now(), instance_id),
                        )
                        _event(conn, run_id, "SANDBOX_STARTED", {"action_id": action_id})

            try:
                outcome = self.backend.run(
                    spec,
                    stdout_path=staging["logs"] / "stdout.log",
                    stderr_path=staging["logs"] / "stderr.log",
                    cancel_event=cancel_event,
                    on_created=on_created,
                    on_started=on_started,
                )
            except KeyboardInterrupt:
                # User cancellation: stop/remove the container, restore the exact
                # pre-action workspace, settle CANCELLED_ROLLED_BACK, then re-raise.
                try:
                    with self.run_store.get_connection() as conn:
                        row = conn.execute("SELECT * FROM h_actions WHERE action_id = ?", (action_id,)).fetchone()
                    if row is not None:
                        self._reconcile_action(dict(row), cancelled=True)
                except Exception:
                    pass  # left unsettled; `harness resume` reconciles it exactly once
                raise
            except SandboxCommunicationError:
                self._mark_unknown(run_id, task_id, action_id, "SANDBOX_COMMUNICATION_LOST")
                raise ActionUnknownError(f"Sandbox communication lost during action {action_id}")
            except SandboxError as exc:
                self._settle_not_started(run_id, action_id, reserved_wall, reserved_output, getattr(exc, "code", "SANDBOX_ERROR"))
                raise
            return self._settle(
                action_id=action_id,
                proposal=proposal,
                decision=decision,
                runtime=runtime,
                version=version,
                before_manifest=before_manifest,
                outcome=outcome,
                staging=staging,
                instance_id=instance_id,
                reserved_wall=reserved_wall,
                reserved_output=reserved_output,
            )
        finally:
            self.locks.release(task_id, token)

    # ------------------------------------------------------------- staging
    def _action_dir(self, run_id: str, task_id: str, action_id: str) -> Path:
        return self.workspaces.task_dir(run_id, task_id) / "actions" / action_id

    def _stage(
        self,
        action_id: str,
        proposal: Dict[str, Any],
        decision: PolicyDecisionV1,
        runtime: ResolvedRuntime,
        version: WorkspaceVersion,
        artifact_ids: Sequence[str],
        dependency_site: Optional[Path],
    ) -> Dict[str, Any]:
        run_id, task_id = proposal["run_id"], proposal["task_id"]
        base = self._action_dir(run_id, task_id, action_id)
        if base.exists():
            from harness.gitflow import secure_rmtree

            secure_rmtree(base, self.workspaces.task_dir(run_id, task_id))
        context = base / "context"
        output = base / "output"
        logs = base / "logs"
        for directory in (context / "artifacts", output, logs):
            directory.mkdir(parents=True, exist_ok=True)
        code_artifact = self.artifact_store.get_artifact_by_id(proposal["code_artifact_id"])
        code = self.artifact_store.open_readonly(run_id, code_artifact["relative_path"].split("/artifacts/", 1)[-1])
        (context / "action.py").write_bytes(code)
        manifest = {"schema_version": "1.0", "action_id": action_id, "artifacts": {}}
        for artifact_id in list(dict.fromkeys(artifact_ids))[:32]:
            artifact = self.artifact_store.get_artifact_by_id(artifact_id)
            if not artifact or artifact["run_id"] != run_id:
                continue
            relative = artifact["relative_path"].split("/artifacts/", 1)[-1]
            if not self.artifact_store.verify(run_id, relative):
                continue
            data = self.artifact_store.open_readonly(run_id, relative)
            name = f"artifacts/{artifact_id}"
            (context / name).write_bytes(data)
            manifest["artifacts"][artifact_id] = {
                "path": name,
                "media_type": artifact["media_type"],
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "byte_cap": 1024 * 1024,
                "kind": artifact["kind"],
            }
        manifest_bytes = canonical_json(manifest).encode("utf-8")
        (context / "manifest.json").write_bytes(manifest_bytes)
        (context / "symbols.json").write_text(canonical_json(self._symbols_snapshot(run_id, version.commit)), encoding="utf-8")
        request = SandboxExecutionRequestV1(
            action_id=action_id,
            run_id=run_id,
            task_id=task_id,
            action_proposal_id=proposal["action_proposal_id"],
            workspace_version=version.version_id,
            checkpoint_id=version.commit,
            code=HashedPathV1(container_path="/context/action.py", sha256=hashlib.sha256(code).hexdigest()),
            context_manifest=HashedPathV1(container_path="/context/manifest.json", sha256=hashlib.sha256(manifest_bytes).hexdigest()),
            capabilities=decision.normalized_capabilities,
            declared_paths=decision.normalized_paths,
            network_mode=NetworkMode.NONE,
            runtime_profile_fingerprint=runtime.fingerprint,
            policy_decision_sha256=decision.decision_sha256,
            limits=SandboxLimitsV1(
                wall_seconds=decision.effective_limits.wall_seconds,
                cpus=decision.effective_limits.cpus,
                memory_bytes=decision.effective_limits.memory_bytes,
                pids=decision.effective_limits.pids,
                scratch_bytes=self.limits.scratch_bytes,
                stdout_bytes=decision.effective_limits.stdout_bytes,
                stderr_bytes=decision.effective_limits.stderr_bytes,
                tool_calls=self.limits.tool_calls,
                run_calls=self.limits.run_calls,
                workspace_growth_bytes=decision.effective_limits.workspace_growth_bytes,
                new_files=decision.effective_limits.new_files,
            ),
        )
        request_bytes = canonical_json(request.model_dump(mode="json")).encode("utf-8")
        (context / "execution-request.json").write_bytes(request_bytes)
        self.artifact_store.write_bytes(
            run_id, f"prd3/actions/{task_id}/{action_id}/execution-request.json", request_bytes,
            "application/json", "execution_request", task_id,
        )
        self.artifact_store.write_bytes(
            run_id, f"prd3/actions/{task_id}/{action_id}/context-manifest.json", manifest_bytes,
            "application/json", "context_manifest", task_id,
        )
        for path in context.rglob("*"):
            os.chmod(path, 0o555 if path.is_dir() else 0o444)
        os.chmod(context, 0o555)
        os.chmod(output, 0o777 if os.getuid() == 0 else 0o755)
        self.run_store.append_event(run_id, "CONTEXT_STAGED", {"action_id": action_id, "artifacts": sorted(manifest["artifacts"])})
        return {"base": base, "context": context, "output": output, "logs": logs, "request_sha": hashlib.sha256(request_bytes).hexdigest()}

    def _symbols_snapshot(self, run_id: str, source_revision: str) -> Dict[str, Any]:
        with self.run_store.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT f.relative_path, f.content_sha256, f.parser_version, s.symbol_kind,
                       s.qualified_name, s.signature, s.start_line, s.end_line
                FROM h_symbols s JOIN h_file_index f ON f.file_index_id = s.file_index_id
                WHERE f.run_id = ? AND f.source_revision = ? AND f.valid = 1
                  AND s.symbol_kind NOT IN ('reference', 'import')
                ORDER BY f.relative_path, s.start_line LIMIT 20000
                """,
                (run_id, source_revision),
            ).fetchall()
        return {
            "schema_version": "1.0",
            "source_revision": source_revision,
            "parser_version": rows[0]["parser_version"] if rows else None,
            "symbols": [
                {
                    "path": row["relative_path"],
                    "kind": row["symbol_kind"],
                    "name": row["qualified_name"],
                    "signature": row["signature"],
                    "start_line": row["start_line"],
                    "end_line": row["end_line"],
                    "file_sha256": row["content_sha256"],
                    "parser_version": row["parser_version"],
                    "source_revision": source_revision,
                }
                for row in rows
            ],
        }

    def _spec(
        self,
        action_id: str,
        proposal: Dict[str, Any],
        runtime: ResolvedRuntime,
        version: WorkspaceVersion,
        staging: Dict[str, Path],
        read_only: bool,
        dependency_site: Optional[Path],
        limits: Any,
    ) -> ContainerSpec:
        run_id, task_id = proposal["run_id"], proposal["task_id"]
        workspace = self.workspaces.workspace_root(run_id, task_id).resolve()
        mounts = [
            Mount(workspace, "/workspace", read_only),
            Mount(staging["context"].resolve(), "/context", True),
            Mount(staging["output"].resolve(), "/output", False),
        ]
        env = dict(SANDBOX_ENV_BASE)
        env.update(
            {
                "HARNESS_RUN_ID": run_id,
                "HARNESS_TASK_ID": task_id,
                "HARNESS_ACTION_ID": action_id,
                "HARNESS_WORKSPACE_VERSION": version.version_id,
                "HARNESS_SOURCE_VERSION": version.commit,
            }
        )
        if dependency_site is not None:
            mounts.append(Mount(Path(dependency_site).resolve(), "/deps", True))
            env["HARNESS_DEPENDENCY_SITE"] = "/deps/site-packages"
        from harness.execution.source_layout import source_roots

        roots = source_roots(workspace)
        if roots:
            env["HARNESS_SOURCE_ROOTS"] = ":".join(f"/workspace/{root}" for root in roots)
        return ContainerSpec(
            name=f"dobby-act-{action_id}",
            image_id=runtime.image_id,
            command=("python", "-I", "-B", "/opt/harness/action_runner.py", "/context/execution-request.json"),
            user=runtime.user,
            env=env,
            mounts=tuple(mounts),
            limits=ContainerLimits(
                cpus=limits.cpus,
                memory_bytes=limits.memory_bytes,
                pids=limits.pids,
                wall_seconds=limits.wall_seconds,
                stdout_bytes=limits.stdout_bytes,
                stderr_bytes=limits.stderr_bytes,
                scratch_bytes=self.limits.scratch_bytes,
                open_files=self.limits.open_files,
                max_file_bytes=min(self.limits.max_file_bytes, max(1024 * 1024, limits.workspace_growth_bytes)),
                workspace_growth_bytes=limits.workspace_growth_bytes,
                new_files=limits.new_files,
            ),
            labels={
                "org.dobby.harness": "1",
                "org.dobby.kind": "action",
                "org.dobby.run_id": run_id,
                "org.dobby.task_id": task_id,
                "org.dobby.action_id": action_id,
                "org.dobby.attempt": "1",
                "org.dobby.runtime_fingerprint": runtime.fingerprint[:32],
                "org.dobby.request_sha256": str(staging["request_sha"]),
            },
            watch_workspace=None if read_only else workspace,
            watch_output=staging["output"],
        )

    # ------------------------------------------------------------ settle
    def _settle(
        self,
        *,
        action_id: str,
        proposal: Dict[str, Any],
        decision: PolicyDecisionV1,
        runtime: ResolvedRuntime,
        version: WorkspaceVersion,
        before_manifest: WorkspaceManifest,
        outcome: ContainerOutcome,
        staging: Dict[str, Path],
        instance_id: Optional[str],
        reserved_wall: int,
        reserved_output: int,
        interrupted: bool = False,
    ) -> ActionExecution:
        run_id, task_id = proposal["run_id"], proposal["task_id"]
        with self.run_store.get_connection() as conn:
            with conn:
                conn.execute("UPDATE h_actions SET state = 'SETTLING', updated_at = ? WHERE action_id = ?", (_now(), action_id))
                if instance_id:
                    conn.execute(
                        "UPDATE h_sandbox_instances SET state = ?, stopped_at = ? WHERE sandbox_instance_id = ?",
                        ("REMOVED" if outcome.removed else "STOPPED", _now(), instance_id),
                    )
                _event(conn, run_id, "SANDBOX_STOPPED", {"action_id": action_id, "removed": outcome.removed, "limit_breach": outcome.limit_breach})
                if outcome.output_limit_exceeded:
                    _event(conn, run_id, "SANDBOX_OUTPUT_TRUNCATED", {"action_id": action_id})
        if not outcome.removed:
            self._mark_unknown(run_id, task_id, action_id, "CONTAINER_REMOVAL_UNPROVEN")
            raise ActionUnknownError("Container removal could not be confirmed; workspace quarantined")

        reasons: List[str] = []
        worker, worker_malformed = self._worker_result(staging["output"] / "result.json")
        tool_events, events_malformed = self._tool_events(staging["output"] / "tool-events.ndjson", action_id)
        if worker_malformed:
            reasons.append("WORKER_RESULT_MALFORMED")
        if events_malformed:
            reasons.append("TOOL_FRAMES_MALFORMED")

        declared = list(decision.normalized_paths)
        try:
            after_manifest = self.workspaces.scan(
                run_id, task_id,
                max_files=before_manifest.file_count + decision.effective_limits.new_files + 1000,
                max_bytes=before_manifest.total_bytes + decision.effective_limits.workspace_growth_bytes + 64 * 1024 * 1024,
            )
            change_set = diff_manifests(before_manifest, after_manifest)
            classification = self.inspector.classify(
                change_set,
                declared,
                max_growth_bytes=decision.effective_limits.workspace_growth_bytes,
                max_new_files=decision.effective_limits.new_files,
            )
        except ManifestLimitError:
            after_manifest = None
            change_set = None
            classification = ChangeClassification(reasons=["WORKSPACE_LIMIT_EXCEEDED"])
        if outcome.limit_breach in ("WORKSPACE_GROWTH", "NEW_FILES"):
            classification.reasons.append(f"{outcome.limit_breach}_LIMIT")

        # Remove disposable caches so they never enter a version.
        if change_set is not None and classification.disposable:
            self._revert_disposables(run_id, task_id, before_manifest, classification)
            after_manifest = self.workspaces.scan(run_id, task_id)
            change_set = diff_manifests(before_manifest, after_manifest)
            classification_real = self.inspector.classify(
                change_set,
                declared,
                max_growth_bytes=decision.effective_limits.workspace_growth_bytes,
                max_new_files=decision.effective_limits.new_files,
            )
            classification_real.items.extend(classification.disposable)
            classification = classification_real

        real_changes = [item for item in classification.items if item.policy_state != "DISPOSABLE"]
        exit_code = outcome.exit_code
        if exit_code == EXIT_INTEGRITY:
            reasons.append("ARTIFACT_OR_CODE_INTEGRITY_ERROR")
        if classification.is_violation:
            settlement = Settlement.POLICY_VIOLATION_ROLLED_BACK
            reasons.extend(classification.reasons)
        elif outcome.cancelled or interrupted and outcome.limit_breach == "CANCELLED":
            settlement = Settlement.CANCELLED_ROLLED_BACK
            reasons.append("CANCELLED")
        elif interrupted:
            settlement = Settlement.FAILED_ROLLED_BACK
            reasons.append("INTERRUPTED")
        elif outcome.timed_out or outcome.oom_killed or outcome.output_limit_exceeded or exit_code != 0 or worker_malformed or exit_code is None:
            settlement = Settlement.FAILED_ROLLED_BACK
            if outcome.timed_out:
                reasons.append("TIMEOUT")
            if outcome.oom_killed:
                reasons.append("OOM_KILLED")
            if outcome.output_limit_exceeded:
                reasons.append("OUTPUT_LIMIT")
            if exit_code not in (0, None):
                reasons.append(f"EXIT_{exit_code}")
        elif not real_changes:
            settlement = Settlement.NO_CHANGE
        else:
            settlement = Settlement.ACCEPTED

        git = self.workspaces.git(run_id)
        checkpoint = None
        diff_bytes = b""
        if settlement == Settlement.ACCEPTED:
            checkpoint = self.workspaces.prepare_checkpoint(
                run_id, task_id, version, after_manifest,
                f"harness checkpoint {task_id} action {action_id}\n",
            )
            diff_bytes = git.diff(version.commit, checkpoint.commit)
        elif change_set is not None and real_changes:
            diff_bytes = self._rollback_diff(run_id, task_id, version, after_manifest, real_changes)

        if settlement not in (Settlement.ACCEPTED, Settlement.NO_CHANGE):
            try:
                self.workspaces.restore(run_id, task_id, before_manifest)
            except (RestoreVerificationError, OSError, WorkspaceIntegrityError) as exc:
                self._mark_unknown(run_id, task_id, action_id, "ROLLBACK_UNVERIFIED")
                raise ActionUnknownError(f"Rollback could not be verified: {exc}")
        else:
            # Host re-verification: the scanned state is exactly what will be recorded.
            current = self.workspaces.scan(run_id, task_id)
            expected = after_manifest if settlement == Settlement.ACCEPTED else before_manifest
            if current.core_view() != expected.core_view():
                self._mark_unknown(run_id, task_id, action_id, "WORKSPACE_CHANGED_DURING_SETTLEMENT")
                raise ActionUnknownError("Workspace changed while settling")

        artifacts = self._write_artifacts(run_id, task_id, action_id, staging, diff_bytes, outcome)
        stdout_excerpt = _read_excerpt(outcome.stdout_path)
        stderr_excerpt = _read_excerpt(outcome.stderr_path)
        signature = self._failure_signature(version, proposal["code_sha256"], settlement, reasons, stderr_excerpt, real_changes)
        repeated = settlement != Settlement.ACCEPTED and self._signature_seen(task_id, signature, action_id)

        created = sorted(item.change.path for item in real_changes if item.change.change_type == "CREATED")
        modified = sorted(item.change.path for item in real_changes if item.change.change_type in ("MODIFIED", "MODE_CHANGED", "RENAMED"))
        deleted = sorted(
            {item.change.path for item in real_changes if item.change.change_type == "DELETED"}
            | {item.change.old_path for item in real_changes if item.change.change_type == "RENAMED" and item.change.old_path}
        )
        result = ExecutionResultV1(
            action_id=action_id,
            run_id=run_id,
            task_id=task_id,
            settlement=settlement,
            container=ContainerIdentityV1(
                runtime_profile_fingerprint=runtime.fingerprint,
                image_digest=runtime.contract.image_digest,
                container_id_hash=hashlib.sha256(outcome.container_id.encode()).hexdigest() if outcome.container_id else None,
            ),
            process=ProcessOutcomeV1(
                exit_code=exit_code,
                signal=outcome.signal,
                timed_out=outcome.timed_out,
                oom_killed=outcome.oom_killed,
                output_limit_exceeded=outcome.output_limit_exceeded,
                elapsed_ms=outcome.elapsed_ms,
            ),
            worker_result=WorkerResultSummaryV1(
                status=worker.get("status") if worker else None,
                summary=(worker or {}).get("summary", "")[:4000],
            ),
            workspace=WorkspaceChangeSummaryV1(
                before_version=version.version_id,
                after_version=checkpoint.version_id if checkpoint else version.version_id,
                change_set_sha256=change_set.sha256() if change_set is not None else None,
                created=created,
                modified=modified,
                deleted=deleted,
                unexpected=classification.unexpected_paths(),
            ),
            artifacts=ExecutionArtifactsV1(**artifacts),
            budget=ExecutionBudgetChargeV1(
                action_units_charged=1,
                wall_seconds_charged=min(reserved_wall, math.ceil(outcome.elapsed_ms / 1000)),
                output_bytes_charged=min(reserved_output, outcome.stdout_bytes + outcome.stderr_bytes),
            ),
            observed_at=_now(),
        )
        result_json = canonical_json(result.model_dump(mode="json"))
        result_path = f"prd3/actions/{task_id}/{action_id}/execution-result.json"
        self.artifact_store.write_bytes(run_id, result_path, result_json.encode(), "application/json", "execution_result", task_id)
        result_artifact = self.artifact_store.get_artifact_by_path(run_id, result_path)

        execution_result_id = f"xres_{uuid.uuid4().hex[:16]}"
        final_state = {
            Settlement.ACCEPTED: "SUCCEEDED",
            Settlement.NO_CHANGE: "SUCCEEDED",
            Settlement.FAILED_ROLLED_BACK: "FAILED",
            Settlement.POLICY_VIOLATION_ROLLED_BACK: "POLICY_VIOLATION",
            Settlement.CANCELLED_ROLLED_BACK: "CANCELLED",
        }[settlement]
        with self.run_store.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if conn.execute("SELECT 1 FROM h_execution_results WHERE action_id = ?", (action_id,)).fetchone():
                    conn.rollback()
                    return self._replay(self.get_action(action_id))
                conn.execute(
                    """
                    INSERT INTO h_execution_results(
                        execution_result_id, action_id, settlement, before_workspace_version_id,
                        after_workspace_version_id, exit_code, signal, timed_out, oom_killed,
                        output_limit_exceeded, elapsed_ms, change_set_sha256, stdout_artifact_id,
                        stderr_artifact_id, tool_events_artifact_id, diff_artifact_id,
                        worker_result_artifact_id, result_artifact_id, reason_codes_json,
                        result_json, settled_at
                    ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        execution_result_id, action_id, settlement.value, version.version_id,
                        exit_code, outcome.signal, int(outcome.timed_out), int(outcome.oom_killed),
                        int(outcome.output_limit_exceeded), outcome.elapsed_ms,
                        result.workspace.change_set_sha256, artifacts.get("stdout"), artifacts.get("stderr"),
                        artifacts.get("tool_events"), artifacts.get("diff"), artifacts.get("result"),
                        result_artifact["artifact_id"] if result_artifact else None,
                        canonical_json(sorted(set(reasons))), result_json, _now(),
                    ),
                )
                if checkpoint is not None:
                    self.workspaces.commit_checkpoint_sql(conn, run_id, task_id, version, checkpoint, action_id)
                    conn.execute(
                        "UPDATE h_execution_results SET after_workspace_version_id = ? WHERE execution_result_id = ?",
                        (checkpoint.version_id, execution_result_id),
                    )
                for item in classification.items:
                    change = item.change
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO h_file_changes(
                            file_change_id, execution_result_id, relative_path, change_type, old_path,
                            before_sha256, after_sha256, before_mode, after_mode, byte_delta, declared, policy_state
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            f"fchg_{uuid.uuid4().hex[:16]}", execution_result_id, change.path,
                            "DISPOSABLE" if item.policy_state == "DISPOSABLE" else change.change_type,
                            change.old_path, change.before.sha256 if change.before else None,
                            change.after.sha256 if change.after else None,
                            change.before.mode if change.before else None,
                            change.after.mode if change.after else None,
                            change.byte_delta, int(item.declared), item.policy_state,
                        ),
                    )
                for path in classification.special:
                    conn.execute(
                        "INSERT OR IGNORE INTO h_file_changes VALUES (?, ?, ?, 'SPECIAL', NULL, NULL, NULL, NULL, NULL, 0, 0, 'DENIED')",
                        (f"fchg_{uuid.uuid4().hex[:16]}", execution_result_id, path),
                    )
                if instance_id:
                    for frame in tool_events:
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO h_tool_calls(
                                tool_call_id, action_id, sandbox_instance_id, sequence, tool_name, tool_version,
                                capability_name, arguments_summary_json, result_summary_json, state, started_at, finished_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                f"{action_id}-{frame.tool_call_id}", action_id, instance_id, frame.sequence,
                                frame.tool, frame.tool_version, frame.capability,
                                canonical_json(frame.arguments_summary), canonical_json(frame.result_summary),
                                frame.state.value, frame.started_at, frame.finished_at,
                            ),
                        )
                conn.execute(
                    "UPDATE h_actions SET state = ?, failure_signature_sha256 = ?, updated_at = ? WHERE action_id = ?",
                    (final_state, signature, _now(), action_id),
                )
                ExecutionBudgetLedger.settle_sql(
                    conn, run_id,
                    reserved_wall=reserved_wall, reserved_output=reserved_output,
                    used_wall=result.budget.wall_seconds_charged,
                    used_output=result.budget.output_bytes_charged,
                    growth_bytes=classification.growth_bytes if settlement == Settlement.ACCEPTED else 0,
                )
                _event(conn, run_id, "ACTION_CHANGESET_COLLECTED", {
                    "action_id": action_id,
                    "change_set_sha256": result.workspace.change_set_sha256,
                    "changed_paths": len(real_changes),
                })
                if settlement in (Settlement.ACCEPTED, Settlement.NO_CHANGE):
                    _event(conn, run_id, "ACTION_SETTLED", {"action_id": action_id, "settlement": settlement.value})
                else:
                    _event(conn, run_id, "ACTION_ROLLED_BACK", {"action_id": action_id, "settlement": settlement.value, "reason_codes": sorted(set(reasons))})
                    _event(conn, run_id, "ACTION_SETTLED", {"action_id": action_id, "settlement": settlement.value})
                if checkpoint is not None:
                    _event(conn, run_id, "WORKSPACE_VERSION_CREATED", {
                        "action_id": action_id,
                        "workspace_version_id": checkpoint.version_id,
                        "commit": checkpoint.commit,
                        "content_tree_sha256": checkpoint.content_tree_sha256,
                    })
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        if checkpoint is not None:
            self.workspaces.publish_checkpoint_refs(run_id, task_id, version, checkpoint)
        feedback = self._feedback(proposal, result, reasons, stdout_excerpt, stderr_excerpt, diff_bytes, artifacts, repeated)
        return ActionExecution(
            kind="EXECUTED",
            action_id=action_id,
            decision=decision,
            result=result,
            reason_codes=sorted(set(reasons)),
            failure_signature=signature,
            repeated_failure=repeated,
            feedback=feedback,
            changed_paths=sorted({*created, *modified, *deleted}),
            old_version=version.commit,
            new_version=checkpoint.commit if checkpoint else version.commit,
        )

    def _revert_disposables(self, run_id: str, task_id: str, before: WorkspaceManifest, classification: ChangeClassification) -> None:
        root = self.workspaces.workspace_root(run_id, task_id)
        git = self.workspaces.git(run_id)
        restore_blobs = {}
        for item in classification.disposable:
            change = item.change
            for path in [change.path] + ([change.old_path] if change.old_path else []):
                target = root / path
                entry = before.entries.get(path)
                if entry is None:
                    if target.is_symlink() or target.is_file():
                        target.unlink()
                else:
                    restore_blobs[path] = entry
        if restore_blobs:
            blobs = git.cat_blobs(entry.oid for entry in restore_blobs.values())
            for path, entry in restore_blobs.items():
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() or target.is_symlink():
                    target.unlink()
                target.write_bytes(blobs[entry.oid])
        # Remove newly created disposable directories bottom-up.
        for current, dirs, files in os.walk(root, topdown=False):
            relative = Path(current).relative_to(root).as_posix()
            if relative != "." and is_disposable(relative) and relative not in before.directories:
                shutil.rmtree(current, ignore_errors=True)

    def _rollback_diff(self, run_id: str, task_id: str, version: WorkspaceVersion, after: WorkspaceManifest, changes: Sequence[Any]) -> bytes:
        total = sum(abs(item.change.byte_delta) for item in changes)
        if total > 16 * 1024 * 1024:
            lines = [f"# rolled-back change set too large to render ({total} bytes)"]
            lines.extend(f"# {item.change.change_type} {item.change.path}" for item in changes[:500])
            return ("\n".join(lines) + "\n").encode()
        git = self.workspaces.git(run_id)
        root = self.workspaces.workspace_root(run_id, task_id)
        regular = [entry for entry in after.entries.values() if entry.mode != "120000"]
        if regular:
            existing_oids = {entry.oid for entry in self.workspaces.load_manifest(version).entries.values()}
            fresh = [entry for entry in regular if entry.oid not in existing_oids]
            git.hash_files([root / entry.path for entry in fresh])
        for entry in after.entries.values():
            if entry.mode == "120000":
                git.hash_bytes(os.readlink(root / entry.path).encode("utf-8", "surrogateescape"))
        tree = git.write_tree(after.tree_entries())
        return git.diff(version.tree, tree)

    # ------------------------------------------------------------ helpers
    def _worker_result(self, path: Path) -> Tuple[Optional[Dict[str, Any]], bool]:
        if not path.exists():
            return None, False
        try:
            if path.is_symlink() or path.stat().st_size > self.limits.frame_bytes:
                return None, True
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None, True
        allowed = {"schema_version", "action_id", "status", "summary", "observations", "artifact_refs", "requests", "trusted_as"}
        if not isinstance(data, dict) or set(data) - allowed or data.get("status") not in (
            "ACTION_COMPLETED", "ACTION_NEEDS_FOLLOWUP", "ACTION_BLOCKED"
        ):
            return None, True
        return data, False

    def _tool_events(self, path: Path, action_id: str) -> Tuple[List[ToolCallEventV1], bool]:
        if not path.exists():
            return [], False
        frames: List[ToolCallEventV1] = []
        malformed = False
        try:
            if path.is_symlink():
                return [], True
            with open(path, "rb") as handle:
                for index, line in enumerate(handle):
                    if index >= self.limits.tool_calls + 16 or len(line) > self.limits.frame_bytes:
                        malformed = True
                        break
                    try:
                        frame = ToolCallEventV1.model_validate_json(line)
                    except Exception:
                        malformed = True
                        continue
                    if frame.action_id != action_id:
                        malformed = True
                        continue
                    frames.append(frame)
        except OSError:
            return [], True
        sequences = [frame.sequence for frame in frames]
        if sequences != sorted(sequences) or len(set(sequences)) != len(sequences):
            malformed = True
        return frames, malformed

    def _write_artifacts(self, run_id: str, task_id: str, action_id: str, staging: Dict[str, Path], diff: bytes, outcome: ContainerOutcome) -> Dict[str, Optional[str]]:
        base = f"prd3/actions/{task_id}/{action_id}"
        mapping: Dict[str, Optional[str]] = {}

        def put(name: str, data: bytes, media: str, kind: str) -> Optional[str]:
            path = f"{base}/{name}"
            self.artifact_store.write_bytes(run_id, path, data, media, kind, task_id)
            artifact = self.artifact_store.get_artifact_by_path(run_id, path)
            return artifact["artifact_id"] if artifact else None

        mapping["stdout"] = put("stdout.log", _read_bounded(outcome.stdout_path, self.limits.stdout_bytes), "text/plain", "action_stdout")
        mapping["stderr"] = put("stderr.log", _read_bounded(outcome.stderr_path, self.limits.stderr_bytes), "text/plain", "action_stderr")
        events = staging["output"] / "tool-events.ndjson"
        mapping["tool_events"] = put("tool-events.ndjson", _read_bounded(events, 8 * 1024 * 1024), "application/x-ndjson", "tool_events") if events.exists() and not events.is_symlink() else None
        mapping["diff"] = put("workspace.diff", diff, "text/x-diff", "workspace_diff") if diff else None
        result = staging["output"] / "result.json"
        mapping["result"] = put("worker-result.json", _read_bounded(result, self.limits.frame_bytes), "application/json", "action_result") if result.exists() and not result.is_symlink() else None
        return mapping

    def _failure_signature(
        self,
        version: WorkspaceVersion,
        code_sha: str,
        settlement: Settlement,
        reasons: Sequence[str],
        stderr: str,
        changes: Sequence[Any],
    ) -> str:
        lines = [line for line in stderr.strip().splitlines() if line.strip()]
        tail = lines[-1] if lines else ""
        tail = re.sub(r"0x[0-9a-fA-F]+", "0x?", tail)
        tail = re.sub(r"line \d+", "line ?", tail)
        return _sha({
            "workspace_version": version.commit,
            "code_sha256": code_sha,
            "error_class": [settlement.value, *sorted(set(reasons))],
            "stderr_signature": hashlib.sha256(tail.encode()).hexdigest(),
            "changed_paths": sorted(item.change.path for item in changes),
        })

    def _signature_seen(self, task_id: str, signature: str, action_id: str) -> bool:
        with self.run_store.get_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM h_actions WHERE task_id = ? AND failure_signature_sha256 = ? AND action_id <> ? LIMIT 1",
                (task_id, signature, action_id),
            ).fetchone()
        return bool(row)

    def _feedback(
        self,
        proposal: Dict[str, Any],
        result: ExecutionResultV1,
        reasons: Sequence[str],
        stdout: str,
        stderr: str,
        diff: bytes,
        artifacts: Dict[str, Optional[str]],
        repeated: bool,
    ) -> Dict[str, Any]:
        return {
            "kind": "action_result",
            "pair_id": result.action_id,
            "action_id": result.action_id,
            "action_proposal_id": proposal["action_proposal_id"],
            "purpose": self._purpose(proposal)[:500],
            "settlement": result.settlement.value,
            "reason_codes": sorted(set(reasons)),
            "exit_code": result.process.exit_code,
            "timed_out": result.process.timed_out,
            "oom_killed": result.process.oom_killed,
            "worker_result_advisory": result.worker_result.model_dump(mode="json"),
            "changed_paths_host_observed": {
                "created": result.workspace.created,
                "modified": result.workspace.modified,
                "deleted": result.workspace.deleted,
                "unexpected_rolled_back": result.workspace.unexpected,
            },
            "workspace_version_before": result.workspace.before_version,
            "workspace_version_after": result.workspace.after_version,
            "stdout_excerpt": _head_tail(stdout, 6000),
            "stderr_excerpt": _head_tail(stderr, 4000),
            "diff_excerpt": _head_tail(diff.decode("utf-8", "replace"), 6000) if result.settlement == Settlement.ACCEPTED else "",
            "log_artifact_ids": {key: value for key, value in artifacts.items() if value},
            "repeated_failure_signature": repeated,
            "trust": "host_observed_settlement_with_untrusted_output_excerpts",
        }

    def _settle_not_started(self, run_id: str, action_id: str, reserved_wall: int, reserved_output: int, code: str) -> None:
        with self.run_store.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                action = conn.execute("SELECT * FROM h_actions WHERE action_id = ?", (action_id,)).fetchone()
                if action and action["state"] in ("INTENT", "RUNNING"):
                    ExecutionBudgetLedger.settle_sql(
                        conn, run_id, reserved_wall=reserved_wall, reserved_output=reserved_output,
                        used_wall=0, used_output=0, growth_bytes=0, charge_action=False,
                    )
                    conn.execute(
                        "UPDATE h_action_proposals SET state = 'UNEXECUTED' WHERE action_proposal_id = ?",
                        (action["action_proposal_id"],),
                    )
                    conn.execute("DELETE FROM h_action_capabilities WHERE action_id = ?", (action_id,))
                    conn.execute("DELETE FROM h_sandbox_instances WHERE action_id = ?", (action_id,))
                    conn.execute("DELETE FROM h_actions WHERE action_id = ?", (action_id,))
                    _event(conn, run_id, "ACTION_START_ABORTED", {"action_id": action_id, "error_code": code})
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def _mark_unknown(self, run_id: str, task_id: str, action_id: str, reason: str) -> None:
        with self.run_store.get_connection() as conn:
            with conn:
                conn.execute("UPDATE h_actions SET state = 'UNKNOWN', updated_at = ? WHERE action_id = ?", (_now(), action_id))
                conn.execute("UPDATE h_sandbox_instances SET state = 'UNKNOWN' WHERE action_id = ? AND state <> 'REMOVED'", (action_id,))
                _event(conn, run_id, "ACTION_UNKNOWN", {"action_id": action_id, "reason": reason})
        self.workspaces.quarantine(run_id, task_id, reason)

    def _replay(self, action: Dict[str, Any]) -> ActionExecution:
        result = self.result_for_action(action["action_id"])
        decision = PolicyDecisionV1.model_validate_json(action["decision_json"])
        if result is None:
            return ActionExecution(
                kind=action["state"],
                action_id=action["action_id"],
                decision=decision,
                reason_codes=list(decision.reason_codes),
            )
        with self.run_store.get_connection() as conn:
            row = conn.execute("SELECT reason_codes_json FROM h_execution_results WHERE action_id = ?", (action["action_id"],)).fetchone()
        return ActionExecution(
            kind="EXECUTED",
            action_id=action["action_id"],
            decision=decision,
            result=result,
            reason_codes=json.loads(row["reason_codes_json"]) if row else [],
            failure_signature=action["failure_signature_sha256"],
        )

    # ---------------------------------------------------------- history
    def feedback_history(self, run_id: str, task_id: str, limit: int = 6) -> List[Dict[str, Any]]:
        """Rebuild bounded proposal/result pairs for the next coder packet from durable records."""
        with self.run_store.get_connection() as conn:
            rows = [dict(row) for row in conn.execute(
                """
                SELECT a.*, r.result_json, r.reason_codes_json, r.stdout_artifact_id, r.stderr_artifact_id,
                       r.diff_artifact_id, r.tool_events_artifact_id, r.worker_result_artifact_id
                FROM h_actions a
                LEFT JOIN h_execution_results r ON r.action_id = a.action_id
                WHERE a.run_id = ? AND a.task_id = ?
                  AND a.state IN ('SUCCEEDED', 'FAILED', 'POLICY_VIOLATION', 'CANCELLED', 'DENIED', 'STALE')
                ORDER BY a.created_at, a.rowid
                """,
                (run_id, task_id),
            ).fetchall()]
        records: List[Dict[str, Any]] = []
        for row in rows[-limit:]:
            with self.run_store.get_connection() as conn:
                proposal = dict(conn.execute(
                    "SELECT * FROM h_action_proposals WHERE action_proposal_id = ?", (row["action_proposal_id"],)
                ).fetchone())
            if not row.get("result_json"):
                decision = PolicyDecisionV1.model_validate_json(row["decision_json"])
                record = self._rejection_feedback(proposal, decision)
                approval = self.approvals.for_action(row["action_id"])
                if approval and approval.get("denial_reason"):
                    record["user_denial_reason"] = approval["denial_reason"][:500]
                records.append(record)
                continue
            result = ExecutionResultV1.model_validate_json(row["result_json"])
            artifacts = {
                "stdout": row["stdout_artifact_id"],
                "stderr": row["stderr_artifact_id"],
                "diff": row["diff_artifact_id"],
                "tool_events": row["tool_events_artifact_id"],
                "result": row["worker_result_artifact_id"],
            }
            stdout = self._artifact_text(run_id, row["stdout_artifact_id"])
            stderr = self._artifact_text(run_id, row["stderr_artifact_id"])
            diff = self._artifact_text(run_id, row["diff_artifact_id"]).encode("utf-8")
            reasons = json.loads(row["reason_codes_json"] or "[]")
            records.append(self._feedback(proposal, result, reasons, stdout, stderr, diff, artifacts, False))
        return records

    def recent_log_artifacts(self, run_id: str, task_id: str, limit: int = 3) -> List[str]:
        with self.run_store.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT r.stdout_artifact_id, r.stderr_artifact_id, r.diff_artifact_id
                FROM h_execution_results r JOIN h_actions a ON a.action_id = r.action_id
                WHERE a.run_id = ? AND a.task_id = ? ORDER BY r.settled_at DESC LIMIT ?
                """,
                (run_id, task_id, limit),
            ).fetchall()
        ids: List[str] = []
        for row in rows:
            ids.extend(value for value in (row["stdout_artifact_id"], row["stderr_artifact_id"], row["diff_artifact_id"]) if value)
        return ids

    def _artifact_text(self, run_id: str, artifact_id: Optional[str], limit: int = 64 * 1024) -> str:
        if not artifact_id:
            return ""
        artifact = self.artifact_store.get_artifact_by_id(artifact_id)
        if not artifact:
            return ""
        relative = artifact["relative_path"].split("/artifacts/", 1)[-1]
        if not self.artifact_store.verify(run_id, relative):
            return "[artifact failed integrity verification]"
        return self.artifact_store.open_readonly(run_id, relative)[:limit].decode("utf-8", "replace")

    def unsettled(self, run_id: str) -> List[Dict[str, Any]]:
        with self.run_store.get_connection() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM h_actions WHERE run_id = ? AND state IN ('INTENT', 'RUNNING', 'SETTLING', 'UNKNOWN')",
                (run_id,),
            ).fetchall()]

    # ------------------------------------------------------- reconcile
    def reconcile(self, run_id: str) -> List[Dict[str, Any]]:
        """Reconcile every unsettled action for ``run_id`` exactly once.

        Never repeats uncertain effects blindly: a container still present is
        stopped first; the workspace is compared with the pre-action version;
        partial changes are restored and verified; anything unprovable stays
        UNKNOWN with the workspace quarantined.
        """
        report: List[Dict[str, Any]] = []
        with self.run_store.get_connection() as conn:
            actions = [dict(row) for row in conn.execute(
                "SELECT * FROM h_actions WHERE run_id = ? AND state IN ('INTENT', 'RUNNING', 'SETTLING', 'UNKNOWN') ORDER BY created_at",
                (run_id,),
            ).fetchall()]
        for action in actions:
            report.append(self._reconcile_action(action))
        return report

    def _reconcile_action(self, action: Dict[str, Any], *, cancelled: bool = False) -> Dict[str, Any]:
        run_id, task_id, action_id = action["run_id"], action["task_id"], action["action_id"]
        ok, _, _ = self.backend.availability()
        if not ok:
            return {"action_id": action_id, "outcome": "UNKNOWN", "reason": "SANDBOX_UNAVAILABLE"}
        containers = self.backend.find_by_labels({"org.dobby.action_id": action_id})
        exited_state: Optional[Dict[str, Any]] = None
        for container in containers:
            state = self.backend.container_state(container)
            if state and (state.get("State") or {}).get("Running"):
                self.backend.stop(container, 2)
                state = self.backend.container_state(container)
            if state:
                exited_state = state.get("State") or {}
            if not self.backend.force_remove(container):
                return {"action_id": action_id, "outcome": "UNKNOWN", "reason": "CONTAINER_REMOVAL_UNPROVEN"}
        version = self.workspaces.get_version(action["before_workspace_version_id"])
        before = self.workspaces.load_manifest(version)
        decision = PolicyDecisionV1.model_validate_json(action["decision_json"])
        reserved_wall = decision.effective_limits.wall_seconds + 10
        reserved_output = decision.effective_limits.stdout_bytes + decision.effective_limits.stderr_bytes
        with self.run_store.get_connection() as conn:
            proposal = dict(conn.execute("SELECT * FROM h_action_proposals WHERE action_proposal_id = ?", (action["action_proposal_id"],)).fetchone())
            instance = conn.execute("SELECT * FROM h_sandbox_instances WHERE action_id = ? ORDER BY attempt_no DESC LIMIT 1", (action_id,)).fetchone()
        # Re-activate a quarantined workspace only for this bounded reconciliation.
        with self.run_store.get_connection() as conn:
            with conn:
                conn.execute("UPDATE h_task_workspaces SET state = 'ACTIVE' WHERE task_id = ? AND state = 'QUARANTINED'", (task_id,))
        self.locks.force_release(task_id)
        staging_base = self._action_dir(run_id, task_id, action_id)
        staging = {
            "base": staging_base,
            "context": staging_base / "context",
            "output": staging_base / "output",
            "logs": staging_base / "logs",
        }
        for key in ("output", "logs"):
            staging[key].mkdir(parents=True, exist_ok=True)
        stdout_path = staging["logs"] / "stdout.log"
        stderr_path = staging["logs"] / "stderr.log"
        for path in (stdout_path, stderr_path):
            if not path.exists():
                path.write_bytes(b"")
        never_started = action["state"] == "INTENT" and instance is None and not containers
        if never_started:
            current = self.workspaces.scan(run_id, task_id)
            if current.core_view() == before.core_view() and not (current.special or current.reserved):
                self._settle_not_started(run_id, action_id, reserved_wall, reserved_output, "INTERRUPTED_BEFORE_START")
                self.run_store.append_event(run_id, "ACTION_RECONCILED", {"action_id": action_id, "outcome": "RELEASED_BEFORE_START"})
                return {"action_id": action_id, "outcome": "RELEASED_BEFORE_START", "never_started": True}
        outcome = ContainerOutcome(
            container_name=f"dobby-act-{action_id}",
            container_id=instance["engine_object_id"] if instance else None,
            exit_code=(exited_state or {}).get("ExitCode") if exited_state else None,
            signal=None,
            oom_killed=bool((exited_state or {}).get("OOMKilled")),
            timed_out=False,
            cancelled=cancelled,
            output_limit_exceeded=False,
            limit_breach="CANCELLED" if cancelled else None,
            elapsed_ms=0,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            stdout_bytes=stdout_path.stat().st_size,
            stderr_bytes=stderr_path.stat().st_size,
            settings_sha256="0" * 64,
            engine_version=self.backend.engine_version(),
            removed=True,
            started=not never_started,
        )
        try:
            runtime = self.runtime()
            complete = exited_state is not None and (exited_state.get("Status") == "exited")
            execution = self._settle(
                action_id=action_id,
                proposal=proposal,
                decision=decision,
                runtime=runtime,
                version=version,
                before_manifest=before,
                outcome=outcome,
                staging=staging,
                instance_id=instance["sandbox_instance_id"] if instance else None,
                reserved_wall=reserved_wall,
                reserved_output=reserved_output,
                interrupted=not complete,
            )
        except ActionUnknownError:
            return {"action_id": action_id, "outcome": "UNKNOWN", "reason": "ROLLBACK_UNVERIFIED"}
        self.run_store.append_event(run_id, "ACTION_RECONCILED", {"action_id": action_id, "settlement": execution.result.settlement.value if execution.result else None})
        return {
            "action_id": action_id,
            "outcome": execution.result.settlement.value if execution.result else execution.kind,
            "never_started": never_started,
        }


def _read_bounded(path: Path, limit: int) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read(limit)
    except OSError:
        return b""


def _read_excerpt(path: Path, limit: int = 64 * 1024) -> str:
    return _read_bounded(path, limit).decode("utf-8", "replace")


def _head_tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = limit // 3
    tail = limit - head
    return text[:head] + f"\n…[{len(text) - limit} characters omitted]…\n" + text[-tail:]
