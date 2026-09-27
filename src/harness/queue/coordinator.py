"""Queue coordinator: the only owner of queue and integration-ref transitions (PRD 5).

Tasks run one at a time. Each starts from the recorded integration head, is
planned/edited/verified by the PRD 2-4 pipeline, and only an exact PASS
candidate (one commit whose parent is the task start) may advance the private
integration ref through a journaled compare-and-swap. Post-advance
verification can compensate precisely; failures never rewrite earlier
accepted work. A final aggregate gate verifies the cumulative result.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import socket
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from harness.contracts.queue import (
    AggregateVerificationV1,
    BlockedItemV1,
    FinalCountsV1,
    FinalIntegrationV1,
    FinalPatchInputV1,
    FinalReserveV1,
    GitIdentityV1,
    IntegrationIntentV1,
    IntegrationResultV1,
    IntegrationStateV1,
    PostAdvanceVerificationV1,
    QueueBudgetViewV1,
    QueueCountsV1,
    QueueFinalResultV1,
    QueuePlanEdgeV1,
    QueuePlanItemV1,
    QueuePlanV1,
    QueuePolicyV1,
    QueueProgressResultV1,
    ReleaseCandidateHandoffV1,
    TaskExecutionStartV1,
    TaskRefsV1,
    TaskResultEntryV1,
    EvaluationCaseResultV1,
    VerifiedTaskCommitV1,
    CommitVerificationV1,
)
from harness.contracts import OrchestrationState
from harness.gitflow.private_git import safe_ref_component
from harness.gitflow.ref_journal import RefOperationJournal, StaleFencingTokenError
from harness.orchestration.lifecycle import (
    ALLOWED_LIFECYCLE_TRANSITIONS,
    InvalidLifecycleTransitionError,
    LifecycleService,
    StaleLifecycleError,
)
from harness.persistence import ArtifactStore, RunStore, canonical_json
from harness.persistence.events import append_event_sql
from harness.queue import graph
from harness.workspace.task_workspace import task_ref

S = OrchestrationState
ACTIVE_ITEM_STATES = ("STARTING", "RUNNING", "PASS_VERIFIED", "INTEGRATING", "POST_INTEGRATION_VERIFYING", "NEEDS_APPROVAL")
TERMINAL_ITEM_STATES = {
    "INTEGRATED", "BLOCKED_DEPENDENCY", "NEEDS_INPUT", "UNSUPPORTED", "DUPLICATE", "SKIPPED", "FAILED",
    "UNVERIFIED", "BLOCKED_ENVIRONMENT", "BUDGET_EXHAUSTED", "CANCELLED", "INTEGRATION_FAILED",
    "INTEGRATION_UNCERTAIN", "REMAINING_BUDGET", "VERIFIED_NOT_INTEGRATED",
}
LIFECYCLE_TO_ITEM = {
    S.READY_FOR_REVIEW: "PASS_VERIFIED",
    S.VERIFICATION_FAILED: "FAILED",
    S.UNVERIFIED: "UNVERIFIED",
    S.BLOCKED_ENVIRONMENT: "BLOCKED_ENVIRONMENT",
    S.BUDGET_EXHAUSTED: "BUDGET_EXHAUSTED",
    S.NEEDS_INPUT: "NEEDS_INPUT",
    S.NEEDS_CAPABILITY: "UNSUPPORTED",
    S.FAILED: "FAILED",
    S.CANCELLED: "CANCELLED",
    S.NEEDS_APPROVAL: "NEEDS_APPROVAL",
    S.ACTION_UNKNOWN: "INTEGRATION_UNCERTAIN",
}
SHARED_SURFACE = {
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "conftest.py", "pytest.ini", "tox.ini",
    "package.json", "tsconfig.json",
}


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _sha(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def default_policy(max_tasks: int = 3) -> QueuePolicyV1:
    core = {
        "schema_version": "1.0",
        "policy_id": "qpol_default_v1",
        "max_tasks_default": 3,
        "max_tasks_hard": 20,
        "max_edges": 80,
        "single_writer": True,
        "continue_independent_after_failure": True,
        "fail_fast": False,
        "one_commit_per_task": True,
        "require_post_advance_verification": True,
        "require_final_aggregate_verification": True,
        "checkpoint_limit_per_task": 50,
        "final_reserve": FinalReserveV1(model_calls=0, wall_seconds=300, check_runs=8, output_bytes=2_000_000).model_dump(),
    }
    return QueuePolicyV1(**core, policy_sha256=_sha(core))


class QueueError(RuntimeError):
    code = "QUEUE_ERROR"


class QueueCoordinator:
    def __init__(
        self,
        *,
        run_store: RunStore,
        artifact_store: ArtifactStore,
        services: Any,
        controller: Any,
        policy: Optional[QueuePolicyV1] = None,
        owner_id: Optional[str] = None,
        min_task_model_calls: int = 3,
    ) -> None:
        self.run_store = run_store
        self.artifact_store = artifact_store
        self.services = services
        self.controller = controller
        self.policy = policy or default_policy()
        self.owner_id = owner_id or f"queue:{socket.gethostname()[:40]}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.lifecycle = LifecycleService(run_store)
        self.journal = RefOperationJournal(run_store)
        self.min_task_model_calls = min_task_model_calls
        self._token: Optional[int] = None

    # ------------------------------------------------------------- helpers
    def _event(self, conn, run_id: str, event_type: str, payload: Dict[str, Any]) -> None:
        append_event_sql(conn, run_id, event_type, payload, "PRD5", "PRD5", _now())

    def _append(self, run_id: str, event_type: str, payload: Dict[str, Any]) -> None:
        with self.run_store.get_connection() as conn:
            with conn:
                self._event(conn, run_id, event_type, payload)

    def queue(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self.run_store.get_connection() as conn:
            row = conn.execute("SELECT * FROM h_task_queues WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def items(self, queue: Mapping[str, Any]) -> List[Dict[str, Any]]:
        with self.run_store.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT i.*, t.task_spec_json FROM h_queue_items i JOIN h_tasks t ON t.task_id = i.task_id
                WHERE i.queue_version_id = ? ORDER BY i.ordinal
                """,
                (queue["active_version_id"],),
            ).fetchall()
        return [dict(row) for row in rows]

    def edges(self, queue: Mapping[str, Any]) -> List[graph.Edge]:
        with self.run_store.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT p.task_id AS predecessor, d.task_id AS dependent, e.source FROM h_queue_edges e
                JOIN h_queue_items p ON p.queue_item_id = e.predecessor_item_id
                JOIN h_queue_items d ON d.queue_item_id = e.dependent_item_id
                WHERE e.queue_version_id = ?
                """,
                (queue["active_version_id"],),
            ).fetchall()
        return [graph.Edge(row["predecessor"], row["dependent"], row["source"]) for row in rows]

    def _set_item(self, item_id: str, state: str, reasons: Optional[List[str]] = None, conn=None) -> None:
        def apply(c):
            if reasons is None:
                c.execute(
                    "UPDATE h_queue_items SET state = ?, state_version = state_version + 1, updated_at = ? WHERE queue_item_id = ?",
                    (state, _now(), item_id),
                )
            else:
                c.execute(
                    "UPDATE h_queue_items SET state = ?, reason_codes_json = ?, state_version = state_version + 1, updated_at = ? WHERE queue_item_id = ?",
                    (state, canonical_json(reasons), _now(), item_id),
                )
        if conn is not None:
            apply(conn)
            return
        with self.run_store.get_connection() as own:
            with own:
                apply(own)

    def _set_queue(self, queue_id: str, state: str) -> None:
        with self.run_store.get_connection() as conn:
            with conn:
                conn.execute(
                    "UPDATE h_task_queues SET state = ?, state_version = state_version + 1, updated_at = ? WHERE queue_id = ?",
                    (state, _now(), queue_id),
                )

    # ------------------------------------------------------------ prepare
    def prepare(self, run_id: str) -> QueuePlanV1:
        existing = self.queue(run_id)
        if existing and existing["active_version_id"]:
            return self.plan(existing)
        run = self.run_store.get_run(run_id)
        if not run or run["state"] != "PREPARED":
            raise QueueError("Queue preparation requires a PREPARED run")
        tasks = self.run_store.get_tasks(run_id)
        facts = []
        for row in tasks:
            spec = json.loads(row["task_spec_json"])
            facts.append(graph.TaskFacts(
                task_id=row["task_id"],
                ordinal=row["ordinal"],
                title=spec.get("title", ""),
                body=spec.get("body", ""),
                labels=tuple(spec.get("labels", [])),
                source_key=spec.get("source_key", ""),
                normalized_sha256=spec.get("normalized_content_sha256", ""),
                created_at=spec.get("remote_created_at"),
            ))
        snapshot_sha = _sha([row["task_spec_json"] for row in tasks])
        queue_id = existing["queue_id"] if existing else f"que_{uuid.uuid4().hex[:16]}"
        version_id = f"qver_{uuid.uuid4().hex[:16]}"
        items, edges = graph.classify(facts)
        mode = run["execution_mode"]
        if mode == "evaluation":
            edges = []  # evaluation cases never chain
            for item in items:
                if item.classification == "DEPENDENT":
                    item.classification = "ACTIONABLE"
        plan_error: Optional[str] = None
        try:
            graph.validate(items, edges, max_tasks=self.policy.max_tasks_hard, max_edges=self.policy.max_edges)
        except graph.QueuePlanError as exc:
            plan_error = str(exc)
        item_ids = {item.task_id: f"qitem_{uuid.uuid4().hex[:16]}" for item in items}
        plan_items = [
            QueuePlanItemV1(
                queue_item_id=item_ids[item.task_id],
                task_id=item.task_id,
                ordinal=item.ordinal,
                classification=item.classification,
                canonical_item_id=item_ids.get(item.canonical_task_id) if item.canonical_task_id else None,
                priority=item.priority,
                reason_codes=item.reasons,
            )
            for item in items
        ]
        plan_edges = [
            QueuePlanEdgeV1(
                edge_id=f"qedge_{uuid.uuid4().hex[:16]}",
                predecessor_item_id=item_ids[edge.predecessor],
                dependent_item_id=item_ids[edge.dependent],
                source=edge.source,
            )
            for edge in edges
        ]
        core = {
            "schema_version": "1.0",
            "queue_id": queue_id,
            "queue_version_id": version_id,
            "run_id": run_id,
            "version": 1,
            "task_snapshot_sha256": snapshot_sha,
            "policy_id": self.policy.policy_id,
            "state": "INVALID" if plan_error else "FROZEN",
            "items": [item.model_dump() for item in plan_items],
            "edges": [edge.model_dump() for edge in plan_edges],
            "frozen_at": None if plan_error else _now(),
        }
        stable = {key: value for key, value in core.items() if key not in ("queue_id", "queue_version_id", "frozen_at")}
        stable["items"] = [{k: v for k, v in item.items() if k not in ("queue_item_id", "canonical_item_id")} for item in core["items"]]
        stable["edges"] = [{"predecessor": edge.predecessor, "dependent": edge.dependent} for edge in edges]
        plan = QueuePlanV1(**core, plan_sha256=_sha(stable))
        self.artifact_store.write_json(run_id, "prd5/queue/policy.json", self.policy.model_dump(mode="json"), "queue_policy")
        plan_path = f"prd5/queue/{version_id}-plan.json"
        self.artifact_store.write_json(run_id, plan_path, plan.model_dump(mode="json"), "queue_plan")
        plan_artifact = self.artifact_store.get_artifact_by_path(run_id, plan_path)
        if plan_error:
            self.artifact_store.write_json(run_id, f"prd5/queue/{version_id}-cycle-report.json", {"error": plan_error}, "queue_cycle_report")
        initial_state = {
            "DUPLICATE": "DUPLICATE",
            "UNSUPPORTED": "UNSUPPORTED",
            "AMBIGUOUS": "NEEDS_INPUT",
            "INVALID": "UNSUPPORTED",
        }
        now = _now()
        with self.run_store.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if not existing:
                    conn.execute(
                        "INSERT INTO h_task_queues(queue_id, run_id, task_snapshot_sha256, active_version_id, mode, state, created_at, updated_at) VALUES (?, ?, ?, NULL, ?, 'DRAFT', ?, ?)",
                        (queue_id, run_id, snapshot_sha, mode, now, now),
                    )
                    self._event(conn, run_id, "QUEUE_CREATED", {"queue_id": queue_id, "mode": mode})
                conn.execute(
                    "INSERT INTO h_queue_versions VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?)",
                    (version_id, queue_id, self.policy.policy_id, self.policy.policy_sha256, plan_artifact["artifact_id"],
                     plan.plan_sha256, "INVALID" if plan_error else "FROZEN", now, None if plan_error else now),
                )
                for item in plan_items:
                    conn.execute(
                        """
                        INSERT INTO h_queue_items(queue_item_id, queue_version_id, task_id, ordinal, priority, classification,
                            canonical_item_id, state, reason_codes_json, classification_artifact_id, classification_sha256, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
                        """,
                        (item.queue_item_id, version_id, item.task_id, item.ordinal, item.priority, item.classification,
                         initial_state.get(item.classification, "PENDING"), canonical_json(item.reason_codes),
                         plan_artifact["artifact_id"], _sha(item.model_dump()), now),
                    )
                for item in plan_items:
                    if item.canonical_item_id:
                        conn.execute("UPDATE h_queue_items SET canonical_item_id = ? WHERE queue_item_id = ?", (item.canonical_item_id, item.queue_item_id))
                for edge in plan_edges:
                    conn.execute(
                        "INSERT INTO h_queue_edges VALUES (?, ?, ?, ?, 'REQUIRES', ?, NULL, ?)",
                        (edge.edge_id, version_id, edge.predecessor_item_id, edge.dependent_item_id, edge.source, now),
                    )
                conn.execute(
                    "UPDATE h_task_queues SET active_version_id = ?, state = ?, updated_at = ? WHERE queue_id = ?",
                    (version_id, "INVALID" if plan_error else "FROZEN", now, queue_id),
                )
                self._event(conn, run_id, "QUEUE_PLAN_REJECTED" if plan_error else "QUEUE_PLAN_VALIDATED", {
                    "queue_version_id": version_id, "error": plan_error,
                })
                if not plan_error:
                    self._event(conn, run_id, "QUEUE_PLAN_FROZEN", {"queue_version_id": version_id, "plan_sha256": plan.plan_sha256})
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return plan

    def plan(self, queue: Mapping[str, Any]) -> QueuePlanV1:
        path = f"prd5/queue/{queue['active_version_id']}-plan.json"
        if not self.artifact_store.verify(queue["run_id"], path):
            raise QueueError("Queue plan artifact failed integrity verification")
        return QueuePlanV1.model_validate_json(self.artifact_store.open_readonly(queue["run_id"], path))

    # -------------------------------------------------------------- lease
    LEASE_TTL = 900

    @staticmethod
    def owner_state(owner_id: str) -> str:
        """``alive``/``dead`` for owners on this host (by PID), else ``unknown``."""
        parts = owner_id.split(":")
        if len(parts) != 4 or parts[0] != "queue" or parts[1] != socket.gethostname()[:40]:
            return "unknown"
        try:
            os.kill(int(parts[2]), 0)
        except ProcessLookupError:
            return "dead"
        except (PermissionError, ValueError):
            return "unknown"
        return "alive"

    def _acquire(self, queue_id: str, purpose: str = "EXECUTE", ttl: Optional[int] = None) -> int:
        ttl = ttl or self.LEASE_TTL
        now = datetime.datetime.now(datetime.timezone.utc)
        with self.run_store.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                active = conn.execute("SELECT * FROM h_queue_leases WHERE queue_id = ? AND state = 'ACTIVE'", (queue_id,)).fetchone()
                if active and active["owner_id"] != self.owner_id:
                    state = self.owner_state(active["owner_id"])
                    expired = datetime.datetime.fromisoformat(active["expires_at"]) <= now
                    # A live local owner is never preempted; an unknown owner only after expiry.
                    if state == "alive" or (state == "unknown" and not expired):
                        raise QueueError(f"Queue {queue_id} is leased by {active['owner_id']} ({state})")
                if active:
                    conn.execute("UPDATE h_queue_leases SET state = 'EXPIRED', released_at = ? WHERE queue_lease_id = ?", (now.isoformat(), active["queue_lease_id"]))
                token = conn.execute("SELECT COALESCE(MAX(fencing_token), 0) + 1 FROM h_queue_leases WHERE queue_id = ?", (queue_id,)).fetchone()[0]
                conn.execute(
                    "INSERT INTO h_queue_leases VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?, NULL)",
                    (f"qlease_{uuid.uuid4().hex[:16]}", queue_id, self.owner_id, purpose, token, now.isoformat(), now.isoformat(),
                     (now + datetime.timedelta(seconds=ttl)).isoformat()),
                )
                run_id = conn.execute("SELECT run_id FROM h_task_queues WHERE queue_id = ?", (queue_id,)).fetchone()["run_id"]
                self._event(conn, run_id, "QUEUE_LEASE_ACQUIRED", {"queue_id": queue_id, "fencing_token": token, "purpose": purpose})
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        self._token = int(token)
        return int(token)

    def _renew(self, queue_id: str) -> None:
        now = datetime.datetime.now(datetime.timezone.utc)
        with self.run_store.get_connection() as conn:
            with conn:
                changed = conn.execute(
                    "UPDATE h_queue_leases SET renewed_at = ?, expires_at = ? WHERE queue_id = ? AND owner_id = ? AND state = 'ACTIVE' AND fencing_token = ?",
                    (now.isoformat(), (now + datetime.timedelta(seconds=self.LEASE_TTL)).isoformat(), queue_id, self.owner_id, self._token),
                ).rowcount
        if changed != 1:
            raise StaleFencingTokenError("Queue lease was lost; stopping before any further queue mutation")

    def _release(self, queue_id: str) -> None:
        with self.run_store.get_connection() as conn:
            with conn:
                conn.execute(
                    "UPDATE h_queue_leases SET state = 'RELEASED', released_at = ? WHERE queue_id = ? AND owner_id = ? AND state = 'ACTIVE'",
                    (_now(), queue_id, self.owner_id),
                )
        self._token = None

    # ----------------------------------------------------------- integration
    def integration_ref(self, run_id: str) -> str:
        return f"refs/harness/runs/{safe_ref_component(run_id)}/integration"

    def heads(self, queue_id: str) -> List[Dict[str, Any]]:
        with self.run_store.get_connection() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM h_integration_heads WHERE queue_id = ? ORDER BY sequence", (queue_id,)
            ).fetchall()]

    def current_head(self, queue_id: str) -> Optional[Dict[str, Any]]:
        heads = self.heads(queue_id)
        return heads[-1] if heads else None

    def _ensure_integration(self, queue: Mapping[str, Any]) -> Dict[str, Any]:
        run_id = queue["run_id"]
        head = self.current_head(queue["queue_id"])
        if head:
            return head
        git = self.services.workspaces.git(run_id)
        source = self.run_store.get_source_snapshot(run_id)
        baseline = source["baseline_commit"]
        ref = self.integration_ref(run_id)
        observed = git.read_ref(ref)
        if observed not in (None, baseline):
            raise QueueError("Integration ref already exists at an unexpected commit")
        if observed is None:
            op = self.journal.prepare(run_id=run_id, queue_id=queue["queue_id"], kind="CREATE", ref=ref,
                                      expected_old=None, desired_new=baseline, fencing_token=self._token or 1)
            observation = self.journal.dispatch(git, op)
            if observation.state != "APPLIED":
                raise QueueError(f"Could not create the integration ref ({observation.state})")
        with self.run_store.get_connection() as conn:
            with conn:
                conn.execute(
                    "INSERT OR IGNORE INTO h_integration_heads VALUES (?, ?, 0, ?, ?, ?, 'BASELINE', NULL, NULL, ?)",
                    (f"ihead_{uuid.uuid4().hex[:16]}", queue["queue_id"], baseline, git.commit_tree_of(baseline), git.object_format, _now()),
                )
        return self.current_head(queue["queue_id"])

    def _reconcile_ref_operations(self, queue: Mapping[str, Any]) -> bool:
        """Settle every unsettled ref intent. Returns False when uncertainty remains."""
        run_id = queue["run_id"]
        git = self.services.workspaces.git(run_id)
        certain = True
        for op in self.journal.unsettled(queue["queue_id"]):
            observation = self.journal.observe(git, op["ref_operation_id"])
            if observation.state == "UNCERTAIN":
                certain = False
                continue
            if observation.state == "APPLIED" and op["operation_kind"] in ("ADVANCE", "COMPENSATE"):
                self._record_head_for_operation(queue, op)
        self._append(run_id, "RECOVERY_RECONCILED", {"queue_id": queue["queue_id"], "certain": certain})
        return certain

    def _record_head_for_operation(self, queue: Mapping[str, Any], op: Mapping[str, Any]) -> Dict[str, Any]:
        git = self.services.workspaces.git(queue["run_id"])
        with self.run_store.get_connection() as conn:
            intent = conn.execute("SELECT * FROM h_integration_intents WHERE ref_operation_id = ?", (op["ref_operation_id"],)).fetchone()
        heads = self.heads(queue["queue_id"])
        if heads and heads[-1]["commit_oid"] == op["desired_new_oid"]:
            return heads[-1]
        head_id = f"ihead_{uuid.uuid4().hex[:16]}"
        with self.run_store.get_connection() as conn:
            with conn:
                conn.execute(
                    "INSERT INTO h_integration_heads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (head_id, queue["queue_id"], (heads[-1]["sequence"] + 1) if heads else 0, op["desired_new_oid"],
                     git.commit_tree_of(op["desired_new_oid"]), git.object_format,
                     "COMPENSATION" if op["operation_kind"] == "COMPENSATE" else "TASK",
                     intent["task_commit_id"] if intent else None, heads[-1]["integration_head_id"] if heads else None, _now()),
                )
                self._event(conn, queue["run_id"], "INTEGRATION_REF_ADVANCED" if op["operation_kind"] == "ADVANCE" else "INTEGRATION_COMPENSATION_RECORDED", {
                    "commit": op["desired_new_oid"], "operation_id": op["ref_operation_id"],
                })
        return self.current_head(queue["queue_id"])

    # --------------------------------------------------------------- run
    def run(self, run_id: str) -> Any:
        plan = self.prepare(run_id)
        queue = self.queue(run_id)
        if queue["state"] == "INVALID":
            return self._finalize(queue, forced_status="INVALID")
        if queue["state"] == "SETTLED":
            return self.final_result(run_id)
        token = self._acquire(queue["queue_id"])
        try:
            if queue["state"] in ("FROZEN", "PAUSED", "RECOVERING", "RUNNING"):
                self._set_queue(queue["queue_id"], "RUNNING")
                self._append(run_id, "QUEUE_STARTED" if queue["state"] == "FROZEN" else "QUEUE_RESUMED", {"queue_id": queue["queue_id"]})
            if not self._reconcile_ref_operations(queue):
                self._set_queue(queue["queue_id"], "UNCERTAIN")
                return self._finalize(self.queue(run_id), forced_status="INTEGRATION_UNCERTAIN")
            if queue["mode"] == "development":
                self._ensure_integration(queue)
            for _ in range(200):
                self._renew(queue["queue_id"])
                queue = self.queue(run_id)
                if queue["cancel_requested"]:
                    self._cancel_pending(queue)
                    return self._finalize(queue, forced_status="CANCELLED")
                if queue["pause_requested"]:
                    self._set_queue(queue["queue_id"], "PAUSED")
                    self._append(run_id, "QUEUE_PAUSED", {"queue_id": queue["queue_id"]})
                    return self.progress(run_id)
                items = self.items(queue)
                active = [item for item in items if item["state"] in ACTIVE_ITEM_STATES]
                if active:
                    signal = self._drive(queue, active[0])
                    if signal == "pause":
                        return self.progress(run_id)
                    if signal == "uncertain":
                        self._set_queue(queue["queue_id"], "UNCERTAIN")
                        return self._finalize(self.queue(run_id), forced_status="INTEGRATION_UNCERTAIN")
                    if signal == "stop":
                        self._mark_remaining(queue, "REMAINING_BUDGET")
                        break
                    continue
                self._block_dependents(queue)
                items = self.items(queue)
                states = {item["task_id"]: item["state"] for item in items}
                edges = self.edges(queue)
                ready = graph.ready_items(items, edges, states)
                if not ready:
                    break
                if self.policy.fail_fast and any(item["state"] in ("FAILED", "INTEGRATION_FAILED") for item in items):
                    self._mark_remaining(queue, "SKIPPED")
                    break
                if not self._can_start_next(queue):
                    self._mark_remaining(queue, "REMAINING_BUDGET")
                    break
                by_task = {item["task_id"]: {**item, "created_at": json.loads(item["task_spec_json"]).get("remote_created_at")} for item in items}
                chosen = graph.order(ready, by_task, edges)[0]
                self._start(queue, by_task[chosen])
            return self._finalize(self.queue(run_id))
        except StaleFencingTokenError:
            raise
        finally:
            self._release(queue["queue_id"])

    def _can_start_next(self, queue: Mapping[str, Any]) -> bool:
        run_id = queue["run_id"]
        try:
            ledger = self.controller.budgets.get(run_id)
        except KeyError:
            return True
        remaining_calls = ledger["remaining_calls"] - ledger["reserved_future_calls"]
        if remaining_calls < self.min_task_model_calls:
            return False
        verifier = self.services.verifier
        if verifier is not None:
            budget = verifier.budget(run_id)
            if budget["remaining_check_runs"] < self.policy.final_reserve.check_runs + 2:
                return False
        try:
            execution = self.services.actions.budgets.get(run_id)
            if execution["remaining_actions"] < 1:
                return False
        except KeyError:
            pass
        return True

    def _mark_remaining(self, queue: Mapping[str, Any], state: str) -> None:
        for item in self.items(queue):
            if item["state"] in ("PENDING", "READY"):
                self._set_item(item["queue_item_id"], state, [state])

    def _cancel_pending(self, queue: Mapping[str, Any]) -> None:
        for item in self.items(queue):
            if item["state"] in ("PENDING", "READY", "STARTING"):
                self._set_item(item["queue_item_id"], "CANCELLED", ["QUEUE_CANCELLED"])

    def _block_dependents(self, queue: Mapping[str, Any]) -> None:
        items = self.items(queue)
        states = {item["task_id"]: item["state"] for item in items}
        edges = self.edges(queue)
        changed = True
        while changed:
            changed = False
            for item in items:
                if states[item["task_id"]] not in ("PENDING", "READY"):
                    continue
                for pred in graph.predecessors_of(item["task_id"], edges):
                    if states.get(pred) in TERMINAL_ITEM_STATES and states.get(pred) != graph.INTEGRATED:
                        states[item["task_id"]] = "BLOCKED_DEPENDENCY"
                        self._set_item(item["queue_item_id"], "BLOCKED_DEPENDENCY", ["DEPENDENCY_NOT_INTEGRATED", pred])
                        self._append(queue["run_id"], "QUEUE_ITEM_BLOCKED", {"queue_item_id": item["queue_item_id"], "blocking_task_id": pred})
                        changed = True
                        break

    # ------------------------------------------------------------ start
    def _start(self, queue: Mapping[str, Any], item: Mapping[str, Any]) -> None:
        run_id, task_id = queue["run_id"], item["task_id"]
        workspaces = self.services.workspaces
        git = workspaces.git(run_id)
        source = self.run_store.get_source_snapshot(run_id)
        if queue["mode"] == "development":
            head = self._ensure_integration(queue)
            start_commit, sequence = head["commit_oid"], head["sequence"]
            if git.read_ref(self.integration_ref(run_id)) != start_commit:
                raise QueueError("Integration ref differs from the durable integration head")
        else:
            start_commit, sequence = source["baseline_commit"], 0
        tree = git.commit_tree_of(start_commit)
        allocation_id = f"qba_{uuid.uuid4().hex[:16]}"
        execution_id = f"texe_{uuid.uuid4().hex[:16]}"
        with self.run_store.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                attempt = conn.execute("SELECT COALESCE(MAX(attempt_number), 0) + 1 FROM h_task_executions WHERE queue_item_id = ?", (item["queue_item_id"],)).fetchone()[0]
                conn.execute(
                    "INSERT INTO h_queue_budget_allocations VALUES (?, ?, ?, 'TASK', ?, 0, 0, 0, 0, 0, 'ACTIVE', '{}', ?, NULL)",
                    (allocation_id, queue["queue_id"], item["queue_item_id"], self.min_task_model_calls, _now()),
                )
                conn.execute(
                    """
                    INSERT INTO h_task_executions(task_execution_id, run_id, queue_item_id, task_id, attempt_number,
                        integration_sequence_at_start, start_commit_oid, start_tree_oid, object_format,
                        budget_allocation_id, state, started_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'STARTING', ?)
                    """,
                    (execution_id, run_id, item["queue_item_id"], task_id, attempt, sequence, start_commit, tree, git.object_format, allocation_id, _now()),
                )
                self._set_item(item["queue_item_id"], "STARTING", conn=conn)
                self._event(conn, run_id, "QUEUE_ITEM_SELECTED", {"queue_item_id": item["queue_item_id"], "task_id": task_id})
                self._event(conn, run_id, "TASK_EXECUTION_START_RECORDED", {
                    "task_execution_id": execution_id, "task_id": task_id, "start_commit": start_commit, "integration_sequence": sequence,
                })
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        version = workspaces.ensure(run_id, task_id, start_commit, source["baseline_commit"])
        refs = TaskRefsV1(
            start=task_ref(run_id, task_id, "start"),
            working=task_ref(run_id, task_id, "working"),
            candidate=task_ref(run_id, task_id, "candidate"),
            verified=task_ref(run_id, task_id, "verified"),
        )
        start_record = TaskExecutionStartV1(
            task_execution_id=execution_id,
            run_id=run_id,
            queue_version_id=queue["active_version_id"],
            queue_item_id=item["queue_item_id"],
            task_id=task_id,
            attempt_number=1,
            integration_sequence=sequence,
            start=GitIdentityV1(commit=start_commit, tree=tree, object_format=git.object_format),
            refs=refs,
            worktree_id=f"wt_{task_id}",
            budget_allocation_id=allocation_id,
            started_at=_now(),
        )
        path = f"prd5/tasks/{task_id}/{execution_id}-start.json"
        self.artifact_store.write_json(run_id, path, start_record.model_dump(mode="json"), "task_start_manifest", task_id)
        manifest_artifact = version.manifest_artifact_id
        with self.run_store.get_connection() as conn:
            with conn:
                for kind, ref in (("START", refs.start), ("WORKING", refs.working)):
                    conn.execute(
                        "INSERT OR IGNORE INTO h_task_git_refs VALUES (?, ?, ?, ?, NULL, ?, 'ACTIVE', ?, ?)",
                        (f"tref_{uuid.uuid4().hex[:16]}", execution_id, kind, ref, start_commit, _now(), _now()),
                    )
                conn.execute(
                    "INSERT INTO h_task_worktrees VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 'ACTIVE', ?, NULL)",
                    (f"wt_{uuid.uuid4().hex[:16]}", execution_id, task_id,
                     hashlib.sha256(str(workspaces.workspace_root(run_id, task_id)).encode()).hexdigest(),
                     start_commit, version.commit, version.tree, manifest_artifact, _now()),
                )
                self._event(conn, run_id, "TASK_WORKTREE_CREATED", {"task_id": task_id, "start_commit": start_commit})
        snapshot = self.lifecycle.get(run_id) if self._lifecycle_exists(run_id) else self.lifecycle.initialize(run_id)
        if snapshot.state == S.PREPARED:
            self.lifecycle.select_initial_task(run_id, task_id)
        elif snapshot.active_task_id != task_id:
            self.lifecycle.advance_task(run_id, snapshot.version, task_id, payload={"queue_item_id": item["queue_item_id"]})
        self._set_item(item["queue_item_id"], "RUNNING")
        with self.run_store.get_connection() as conn:
            with conn:
                conn.execute("UPDATE h_task_executions SET state = 'RUNNING' WHERE task_execution_id = ?", (execution_id,))

    def _lifecycle_exists(self, run_id: str) -> bool:
        with self.run_store.get_connection() as conn:
            return bool(conn.execute("SELECT 1 FROM h_run_lifecycle WHERE run_id = ?", (run_id,)).fetchone())

    def _execution(self, item: Mapping[str, Any]) -> Dict[str, Any]:
        with self.run_store.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM h_task_executions WHERE queue_item_id = ? ORDER BY attempt_number DESC LIMIT 1", (item["queue_item_id"],)
            ).fetchone()
        return dict(row)

    # ------------------------------------------------------------ drive
    def _drive(self, queue: Mapping[str, Any], item: Dict[str, Any]) -> str:
        run_id = queue["run_id"]
        if item["state"] == "STARTING":
            execution = self._execution(item)
            self.services.workspaces.ensure(run_id, item["task_id"], execution["start_commit_oid"], self.run_store.get_source_snapshot(run_id)["baseline_commit"])
            snapshot = self.lifecycle.get(run_id) if self._lifecycle_exists(run_id) else self.lifecycle.initialize(run_id)
            if snapshot.state == S.PREPARED:
                self.lifecycle.select_initial_task(run_id, item["task_id"])
            elif snapshot.active_task_id != item["task_id"]:
                self.lifecycle.advance_task(run_id, snapshot.version, item["task_id"])
            self._set_item(item["queue_item_id"], "RUNNING")
            item["state"] = "RUNNING"
        if item["state"] in ("RUNNING", "NEEDS_APPROVAL"):
            result = self.controller.continue_run(run_id, stop_at="complete")
            snapshot = self.lifecycle.get(run_id)
            if snapshot.active_task_id != item["task_id"]:
                raise QueueError("Lifecycle active task diverged from the queue's active item")
            state = LIFECYCLE_TO_ITEM.get(snapshot.state)
            if state is None:
                raise QueueError(f"Task stopped in a non-terminal lifecycle state {snapshot.state.value}")
            if state == "NEEDS_APPROVAL":
                self._set_item(item["queue_item_id"], "NEEDS_APPROVAL", ["NEEDS_APPROVAL"])
                self._set_queue(queue["queue_id"], "PAUSED")
                self._append(run_id, "QUEUE_PAUSED", {"queue_id": queue["queue_id"], "reason": "NEEDS_APPROVAL"})
                return "pause"
            if state == "INTEGRATION_UNCERTAIN":
                self._set_item(item["queue_item_id"], "INTEGRATION_UNCERTAIN", ["ACTION_UNKNOWN"])
                return "uncertain"
            verifier = self.services.verifier
            if verifier is not None and state != "PASS_VERIFIED":
                verifier.record_terminal_outcome(run_id, item["task_id"], _outcome_status(state), snapshot.stop_reason_code or state)
            if state != "PASS_VERIFIED":
                self._settle_execution(item, state)
                self._set_item(item["queue_item_id"], state, [snapshot.stop_reason_code or state])
                self._append(run_id, "TASK_NON_PASS_SETTLED", {"task_id": item["task_id"], "state": state})
                if queue["mode"] == "evaluation":
                    self._record_case(queue, item, state)
                if state == "BUDGET_EXHAUSTED":
                    return "stop"
                return "continue"
            self._set_item(item["queue_item_id"], "PASS_VERIFIED")
            item["state"] = "PASS_VERIFIED"
        if item["state"] == "PASS_VERIFIED":
            if queue["mode"] == "evaluation":
                self._settle_execution(item, "SETTLED")
                self._set_item(item["queue_item_id"], "VERIFIED_NOT_INTEGRATED", ["EVALUATION_CASE_PASSED"])
                self._record_case(queue, item, "PASS")
                return "continue"
            return self._integrate(queue, item)
        if item["state"] in ("INTEGRATING", "POST_INTEGRATION_VERIFYING"):
            return self._integrate(queue, item)
        return "continue"

    def _settle_execution(self, item: Mapping[str, Any], outcome: str) -> None:
        execution = self._execution(item)
        with self.run_store.get_connection() as conn:
            with conn:
                conn.execute(
                    "UPDATE h_task_executions SET state = ?, outcome = ?, settled_at = ? WHERE task_execution_id = ?",
                    ("INTEGRATED" if outcome == "INTEGRATED" else ("FAILED" if outcome not in ("SETTLED", "PASS_VERIFIED") else "SETTLED"), outcome, _now(), execution["task_execution_id"]),
                )
                conn.execute(
                    "UPDATE h_queue_budget_allocations SET state = 'SETTLED', settled_at = ? WHERE budget_allocation_id = ?",
                    (_now(), execution["budget_allocation_id"]),
                )

    # --------------------------------------------------------- integrate
    def _integrate(self, queue: Mapping[str, Any], item: Dict[str, Any]) -> str:
        run_id, task_id = queue["run_id"], item["task_id"]
        git = self.services.workspaces.git(run_id)
        verifier = self.services.verifier
        execution = self._execution(item)
        outcome = verifier.task_outcome(task_id) if verifier else None
        if not outcome or outcome["status"] != "PASS" or not outcome["accepted_candidate_id"]:
            self._set_item(item["queue_item_id"], "FAILED", ["INTEGRATION_REJECTED:NO_PASS_OUTCOME"])
            return "continue"
        candidate = self.services.workspaces.get_candidate(outcome["accepted_candidate_id"])
        with self.run_store.get_connection() as conn:
            decision = conn.execute("SELECT * FROM h_completion_decisions WHERE completion_decision_id = ?", (outcome["completion_decision_id"],)).fetchone()
            intent = conn.execute(
                "SELECT i.* FROM h_integration_intents i WHERE i.queue_item_id = ? ORDER BY created_at DESC LIMIT 1", (item["queue_item_id"],)
            ).fetchone()
        reasons = self._admission_problems(queue, item, execution, candidate, decision, git)
        if reasons and intent is None:
            self._set_item(item["queue_item_id"], "FAILED", ["INTEGRATION_REJECTED", *reasons])
            self._append(run_id, "INTEGRATION_REJECTED", {"task_id": task_id, "reasons": reasons})
            return "continue"
        if intent is None:
            intent = self._prepare_intent(queue, item, execution, candidate, decision, git)
        op = self.journal.get(intent["ref_operation_id"])
        if op["state"] in ("PREPARED", "DISPATCHED"):
            observation = self.journal.dispatch(git, op["ref_operation_id"])
        else:
            observation = self.journal.observe(git, op["ref_operation_id"]) if op["state"] == "UNCERTAIN" else None
        op = self.journal.get(intent["ref_operation_id"])
        if op["state"] == "UNCERTAIN":
            self._set_item(item["queue_item_id"], "INTEGRATION_UNCERTAIN", ["REF_STATE_UNEXPLAINED"])
            self._append(run_id, "INTEGRATION_UNCERTAIN", {"task_id": task_id, "observed": op["observed_oid"]})
            return "uncertain"
        if op["state"] == "NOT_APPLIED":
            self._set_item(item["queue_item_id"], "INTEGRATION_FAILED", ["CAS_NOT_APPLIED"])
            return "continue"
        before_head = self._head_by_commit(queue["queue_id"], op["expected_old_oid"])
        after_head = self._record_head_for_operation(queue, op)
        self._set_item(item["queue_item_id"], "POST_INTEGRATION_VERIFYING")
        with self.run_store.get_connection() as conn:
            existing_result = conn.execute("SELECT * FROM h_integration_results WHERE integration_intent_id = ?", (intent["integration_intent_id"],)).fetchone()
        if existing_result and existing_result["status"] in ("APPLIED_AND_VERIFIED", "COMPENSATED"):
            return "continue"
        verification = self._post_advance(queue, item, candidate, after_head)
        status = "APPLIED_AND_VERIFIED" if verification.status == "PASS" else "COMPENSATED"
        final_head = after_head
        if verification.status != "PASS":
            final_head = self._compensate(queue, item, op, before_head)
            if final_head is None:
                return "uncertain"
        result_id = f"ires_{uuid.uuid4().hex[:16]}"
        post = PostAdvanceVerificationV1(
            status=verification.status if verification.status in ("PASS", "FAILED", "UNVERIFIED", "BLOCKED_ENVIRONMENT") else "UNVERIFIED",
            verification_id=f"iver_{uuid.uuid4().hex[:16]}",
            contract_set_sha256=verification.contract_set_sha256,
            report_artifact_id=verification.report_artifact_id,
        )
        result = IntegrationResultV1(
            integration_result_id=result_id,
            integration_intent_id=intent["integration_intent_id"],
            status=status,
            observed_before_commit=op["expected_old_oid"],
            observed_after_commit=final_head["commit_oid"],
            integration_sequence=final_head["sequence"],
            post_advance_verification=post,
            settled_at=_now(),
        )
        path = f"prd5/integration/{task_id}/{result_id}.json"
        self.artifact_store.write_json(run_id, path, result.model_dump(mode="json"), "integration_result", task_id)
        artifact = self.artifact_store.get_artifact_by_path(run_id, path)
        with self.run_store.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO h_integration_results VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (result_id, intent["integration_intent_id"], before_head["integration_head_id"], final_head["integration_head_id"],
                     status, artifact["artifact_id"], artifact["sha256"], _now()),
                )
                conn.execute(
                    "INSERT INTO h_integration_verifications VALUES (?, ?, ?, ?, 'POST_ADVANCE', ?, ?, ?, ?, ?, ?, ?)",
                    (post.verification_id, queue["queue_id"], result_id, after_head["integration_head_id"],
                     verification.contract_set_artifact_id, verification.contract_set_sha256, post.status,
                     verification.report_artifact_id, self.artifact_store.get_artifact_by_id(verification.report_artifact_id)["sha256"],
                     _now(), _now()),
                )
                conn.execute("UPDATE h_integration_intents SET state = 'SETTLED', settled_at = ? WHERE integration_intent_id = ?", (_now(), intent["integration_intent_id"]))
                self._set_item(item["queue_item_id"], "INTEGRATED" if status == "APPLIED_AND_VERIFIED" else "INTEGRATION_FAILED",
                               None if status == "APPLIED_AND_VERIFIED" else ["POST_ADVANCE_VERIFICATION_" + verification.status], conn=conn)
                self._event(conn, run_id, "POST_ADVANCE_VERIFICATION_SETTLED", {
                    "task_id": task_id, "status": verification.status, "reused_checks": verification.reused_checks,
                    "executed_checks": verification.executed_checks,
                })
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        self._settle_execution(item, "INTEGRATED" if status == "APPLIED_AND_VERIFIED" else "FAILED")
        return "continue"

    def _admission_problems(self, queue, item, execution, candidate, decision, git) -> List[str]:
        problems = []
        if decision is None or decision["status"] != "PASS" or decision["candidate_id"] != candidate["candidate_id"]:
            problems.append("DECISION_NOT_PASS_FOR_CANDIDATE")
        if candidate["task_start_commit"] != execution["start_commit_oid"]:
            problems.append("CANDIDATE_START_DIFFERS")
        if git.commit_parents(candidate["candidate_commit"]) != [execution["start_commit_oid"]]:
            problems.append("CANDIDATE_NOT_SINGLE_DIRECT_PARENT")
        if git.commit_tree_of(candidate["candidate_commit"]) != candidate["candidate_tree"]:
            problems.append("CANDIDATE_TREE_CHANGED")
        head = self.current_head(queue["queue_id"])
        if head is None or head["commit_oid"] != execution["start_commit_oid"]:
            problems.append("INTEGRATION_HEAD_STALE")
        if git.read_ref(self.integration_ref(queue["run_id"])) != execution["start_commit_oid"]:
            problems.append("INTEGRATION_REF_STALE")
        if self.services.actions.unsettled(queue["run_id"]):
            problems.append("UNSETTLED_ACTION")
        try:
            self.services.workspaces.verify_candidate(candidate)
        except Exception:
            problems.append("CANDIDATE_INTEGRITY_FAILURE")
        return problems

    def _prepare_intent(self, queue, item, execution, candidate, decision, git) -> Dict[str, Any]:
        run_id, task_id = queue["run_id"], item["task_id"]
        commit_id = f"tcmt_{uuid.uuid4().hex[:16]}"
        message = git.commit_message(candidate["candidate_commit"])
        metadata_sha = hashlib.sha256(message.encode()).hexdigest()
        decision_json = json.loads(decision["decision_json"])
        verified_commit = VerifiedTaskCommitV1(
            task_commit_id=commit_id,
            run_id=run_id,
            task_id=task_id,
            candidate_id=candidate["candidate_id"],
            commit=candidate["candidate_commit"],
            tree=candidate["candidate_tree"],
            parent=execution["start_commit_oid"],
            object_format=git.object_format,
            shape="SINGLE_DIRECT_PARENT",
            completion_decision_id=decision["completion_decision_id"],
            verification=CommitVerificationV1(
                status="PASS",
                contract_sha256=decision["contract_sha256"],
                test_set_sha256=decision["test_set_sha256"],
                environment_set_sha256=decision["environment_set_sha256"],
                report_artifact_id=decision["report_artifact_id"],
            ),
            metadata_sha256=metadata_sha,
        )
        self.artifact_store.write_json(run_id, f"prd5/tasks/{task_id}/{commit_id}-commit.json", verified_commit.model_dump(mode="json"), "task_commit_manifest", task_id)
        diff = git.diff(execution["start_commit_oid"], candidate["candidate_commit"])
        self.artifact_store.write_bytes(run_id, f"prd5/integration/{task_id}/{commit_id}.diff", diff, "text/x-diff", "integration_diff", task_id)
        intent_id = f"iint_{uuid.uuid4().hex[:16]}"
        ref = self.integration_ref(run_id)
        with self.run_store.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    INSERT INTO h_task_commits(task_commit_id, task_execution_id, task_id, candidate_id, completion_decision_id,
                        commit_oid, tree_oid, parent_oid, object_format, shape, metadata_sha256, verification_state, created_at, verified_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'SINGLE_DIRECT_PARENT', ?, 'PASS', ?, ?)
                    ON CONFLICT(task_execution_id, candidate_id) DO NOTHING
                    """,
                    (commit_id, execution["task_execution_id"], task_id, candidate["candidate_id"], decision["completion_decision_id"],
                     candidate["candidate_commit"], candidate["candidate_tree"], execution["start_commit_oid"], git.object_format,
                     metadata_sha, _now(), decision["decided_at"]),
                )
                stored = conn.execute("SELECT task_commit_id FROM h_task_commits WHERE task_execution_id = ? AND candidate_id = ?",
                                      (execution["task_execution_id"], candidate["candidate_id"])).fetchone()
                op_id = self.journal.prepare(
                    run_id=run_id, queue_id=queue["queue_id"], kind="ADVANCE", ref=ref,
                    expected_old=execution["start_commit_oid"], desired_new=candidate["candidate_commit"],
                    fencing_token=self._token or 1, conn=conn,
                )
                model = IntegrationIntentV1(
                    integration_intent_id=intent_id,
                    ref_operation_id=op_id,
                    run_id=run_id,
                    queue_item_id=item["queue_item_id"],
                    task_commit_id=stored["task_commit_id"],
                    strategy="FAST_FORWARD_EXACT",
                    integration_ref=ref,
                    expected_old_commit=execution["start_commit_oid"],
                    desired_new_commit=candidate["candidate_commit"],
                    desired_tree=candidate["candidate_tree"],
                    lease_fencing_token=self._token or 1,
                    state="PREPARED",
                    intent_sha256="0" * 64,
                    created_at=_now(),
                )
                intent_payload = model.model_dump(mode="json")
                intent_payload["intent_sha256"] = _sha({k: v for k, v in intent_payload.items() if k != "intent_sha256"})
                path = f"prd5/integration/{task_id}/{intent_id}-intent.json"
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        self.artifact_store.write_json(run_id, path, intent_payload, "integration_intent", task_id)
        artifact = self.artifact_store.get_artifact_by_path(run_id, path)
        with self.run_store.get_connection() as conn:
            with conn:
                conn.execute(
                    "INSERT INTO h_integration_intents VALUES (?, ?, ?, ?, ?, 'FAST_FORWARD_EXACT', ?, ?, ?, 'PREPARED', ?, ?, ?, NULL)",
                    (intent_id, queue["queue_id"], item["queue_item_id"], stored["task_commit_id"], op_id,
                     execution["start_commit_oid"], candidate["candidate_commit"], candidate["candidate_tree"],
                     artifact["artifact_id"], intent_payload["intent_sha256"], _now()),
                )
                self._set_item(item["queue_item_id"], "INTEGRATING", conn=conn)
                self._event(conn, run_id, "INTEGRATION_INTENT_RECORDED", {"integration_intent_id": intent_id, "task_id": task_id})
                self._event(conn, run_id, "TASK_COMMIT_VERIFIED", {"task_commit_id": stored["task_commit_id"], "commit": candidate["candidate_commit"]})
        with self.run_store.get_connection() as conn:
            return dict(conn.execute("SELECT * FROM h_integration_intents WHERE integration_intent_id = ?", (intent_id,)).fetchone())

    def _head_by_commit(self, queue_id: str, commit: str) -> Dict[str, Any]:
        for head in reversed(self.heads(queue_id)):
            if head["commit_oid"] == commit:
                return head
        raise QueueError(f"No integration head recorded for {commit}")

    def _integrated_contracts(self, queue: Mapping[str, Any], exclude_task: Optional[str] = None) -> List[Tuple[str, Any]]:
        from harness.verification.service import load_contract_by_id

        verifier = self.services.verifier
        contracts = []
        for other in self.items(queue):
            if other["state"] != "INTEGRATED" or other["task_id"] == exclude_task:
                continue
            with self.run_store.get_connection() as conn:
                row = conn.execute(
                    "SELECT a.contract_id FROM h_completion_decisions d JOIN h_task_outcomes o ON o.completion_decision_id = d.completion_decision_id JOIN h_verification_attempts a ON a.attempt_id = d.attempt_id WHERE o.task_id = ?",
                    (other["task_id"],),
                ).fetchone()
            if row:
                contracts.append((other["task_id"], load_contract_by_id(verifier, row["contract_id"])))
        return contracts

    def _post_advance(self, queue, item, candidate, head) -> Any:
        from harness.verification.service import load_contract_by_id, verify_commit

        verifier = self.services.verifier
        run_id, task_id = queue["run_id"], item["task_id"]
        git = self.services.workspaces.git(run_id)
        changed = {path for _, path in git.diff_paths(candidate["task_start_commit"], candidate["candidate_commit"])}
        own = verifier.contract_row(run_id, task_id)
        contracts = [load_contract_by_id(verifier, own["contract_id"])] if own else []
        base = self.run_store.get_source_snapshot(run_id)["baseline_commit"]
        for other_task, contract in self._integrated_contracts(queue, exclude_task=task_id):
            other_candidate = verifier.task_outcome(other_task)
            other = self.services.workspaces.get_candidate(other_candidate["accepted_candidate_id"]) if other_candidate else None
            other_paths = {path for _, path in git.diff_paths(other["task_start_commit"], other["candidate_commit"])} if other else set()
            overlap = bool(changed & other_paths)
            shared = any(PurePosixPath(path).name in SHARED_SURFACE for path in changed | other_paths)
            dependent = any(edge.predecessor == other_task and edge.dependent == task_id for edge in self.edges(queue))
            if overlap or shared or dependent:
                contracts.append(contract)
        return verify_commit(
            verifier, run_id,
            commit=head["commit_oid"],
            contracts=contracts,
            artifact_prefix=f"prd5/integration/{task_id}/post-advance-{head['sequence']}",
            changed_paths=sorted({path for _, path in git.diff_paths(base, head["commit_oid"])}),
        )

    def _compensate(self, queue, item, op, before_head) -> Optional[Dict[str, Any]]:
        run_id = queue["run_id"]
        git = self.services.workspaces.git(run_id)
        ref = self.integration_ref(run_id)
        if git.read_ref(ref) != op["desired_new_oid"]:
            self._set_item(item["queue_item_id"], "INTEGRATION_UNCERTAIN", ["COMPENSATION_REF_MOVED"])
            self._append(run_id, "INTEGRATION_UNCERTAIN", {"task_id": item["task_id"], "reason": "ref moved before compensation"})
            return None
        comp = self.journal.prepare(run_id=run_id, queue_id=queue["queue_id"], kind="COMPENSATE", ref=ref,
                                    expected_old=op["desired_new_oid"], desired_new=before_head["commit_oid"],
                                    fencing_token=self._token or 1)
        observation = self.journal.dispatch(git, comp)
        if observation.state != "APPLIED":
            self._set_item(item["queue_item_id"], "INTEGRATION_UNCERTAIN", ["COMPENSATION_" + observation.state])
            return None
        return self._record_head_for_operation(queue, self.journal.get(comp))

    # ---------------------------------------------------------- evaluation
    def _record_case(self, queue: Mapping[str, Any], item: Mapping[str, Any], state: str) -> None:
        run_id, task_id = queue["run_id"], item["task_id"]
        verifier = self.services.verifier
        outcome = verifier.task_outcome(task_id) if verifier else None
        candidate = self.services.workspaces.latest_candidate(run_id, task_id)
        source = self.run_store.get_source_snapshot(run_id)
        case_id = f"ecase_{uuid.uuid4().hex[:16]}"
        status = "PASS" if state == "PASS" else _outcome_status(state)
        result = EvaluationCaseResultV1(
            batch_id=f"ebat_{queue['queue_id']}",
            case_id=case_id,
            task_id=task_id,
            baseline_commit=source["baseline_commit"],
            candidate_commit=candidate["candidate_commit"] if candidate else None,
            outcome=status,
            context_isolation_id=f"iso_{task_id}",
            workspace_id=f"wt_{task_id}",
            completion_decision_id=outcome["completion_decision_id"] if outcome else None,
            result_artifact_id="pending",
        )
        path = f"prd5/evaluation/{task_id}/{case_id}.json"
        self.artifact_store.write_json(run_id, path, result.model_dump(mode="json"), "evaluation_case_result", task_id)
        artifact = self.artifact_store.get_artifact_by_path(run_id, path)
        git = self.services.workspaces.git(run_id)
        with self.run_store.get_connection() as conn:
            with conn:
                conn.execute(
                    "INSERT INTO h_evaluation_cases VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
                    (case_id, run_id, task_id, result.batch_id, item["ordinal"], source["baseline_commit"],
                     git.commit_tree_of(source["baseline_commit"]), result.context_isolation_id, None,
                     candidate["candidate_id"] if candidate else None, result.completion_decision_id, status,
                     artifact["artifact_id"], _now(), _now()),
                )
                self._event(conn, run_id, "EVALUATION_CASE_SETTLED", {"case_id": case_id, "task_id": task_id, "status": status})

    # ------------------------------------------------------------ finalize
    def _finalize(self, queue: Mapping[str, Any], forced_status: Optional[str] = None) -> QueueFinalResultV1:
        from harness.verification.service import verify_commit

        run_id = queue["run_id"]
        with self.run_store.get_connection() as conn:
            done = conn.execute("SELECT * FROM h_queue_results WHERE queue_id = ?", (queue["queue_id"],)).fetchone()
        if done:
            return self.final_result(run_id)
        self._set_queue(queue["queue_id"], "FINALIZING")
        self._append(run_id, "FINAL_AGGREGATE_STARTED", {"queue_id": queue["queue_id"]})
        git = self.services.workspaces.git(run_id)
        source = self.run_store.get_source_snapshot(run_id)
        baseline = source["baseline_commit"]
        items = self.items(queue)
        head = self.current_head(queue["queue_id"]) if queue["mode"] == "development" else None
        final_commit = head["commit_oid"] if head else baseline
        integrated = [item for item in items if item["state"] == "INTEGRATED"]
        passed_cases = [item for item in items if item["state"] == "VERIFIED_NOT_INTEGRATED"]
        aggregate = AggregateVerificationV1(status="NOT_RUN")
        aggregate_row_id = None
        if integrated and forced_status is None and self.policy.require_final_aggregate_verification:
            contracts = [contract for _, contract in self._integrated_contracts(queue)]
            verification = verify_commit(
                self.services.verifier, run_id,
                commit=final_commit,
                contracts=contracts,
                artifact_prefix=f"prd5/aggregate/{head['sequence']}",
                changed_paths=sorted({path for _, path in git.diff_paths(baseline, final_commit)}),
            )
            aggregate_row_id = f"iver_final_{uuid.uuid4().hex[:12]}"
            status = verification.status if verification.status in ("PASS", "FAILED", "UNVERIFIED", "BLOCKED_ENVIRONMENT") else "UNVERIFIED"
            aggregate = AggregateVerificationV1(
                status=status,
                verification_id=aggregate_row_id,
                contract_set_sha256=verification.contract_set_sha256,
                report_artifact_id=verification.report_artifact_id,
            )
            with self.run_store.get_connection() as conn:
                with conn:
                    conn.execute(
                        "INSERT INTO h_integration_verifications VALUES (?, ?, NULL, ?, 'FINAL_AGGREGATE', ?, ?, ?, ?, ?, ?, ?)",
                        (aggregate_row_id, queue["queue_id"], head["integration_head_id"], verification.contract_set_artifact_id,
                         verification.contract_set_sha256, status, verification.report_artifact_id,
                         self.artifact_store.get_artifact_by_id(verification.report_artifact_id)["sha256"], _now(), _now()),
                    )
                    self._event(conn, run_id, "FINAL_AGGREGATE_SETTLED", {"status": status, "reused": verification.reused_checks, "executed": verification.executed_checks})
            if git.read_ref(task_ref_aggregate(run_id)) is None and status == "PASS":
                git.update_ref_cas(task_ref_aggregate(run_id), final_commit, None)
        status = forced_status or self._derive_status(queue, items, aggregate.status)
        counts = self._counts(items)
        patch = git.diff(baseline, final_commit)
        patch_path = "prd5/final/final-patch.diff"
        self.artifact_store.write_bytes(run_id, patch_path, patch, "text/x-diff", "final_patch_input")
        patch_artifact = self.artifact_store.get_artifact_by_path(run_id, patch_path)
        task_results = []
        for item in items:
            commit = None
            if item["state"] == "INTEGRATED":
                with self.run_store.get_connection() as conn:
                    row = conn.execute("SELECT commit_oid FROM h_task_commits WHERE task_id = ? AND verification_state = 'PASS' ORDER BY created_at DESC LIMIT 1", (item["task_id"],)).fetchone()
                commit = row["commit_oid"] if row else None
            elif item["state"] == "VERIFIED_NOT_INTEGRATED":
                candidate = self.services.workspaces.latest_candidate(run_id, item["task_id"])
                commit = candidate["candidate_commit"] if candidate else None
            task_results.append(TaskResultEntryV1(task_id=item["task_id"], status=item["state"], commit=commit))
        report = {
            "queue_id": queue["queue_id"],
            "status": status,
            "items": [
                {
                    "task_id": item["task_id"],
                    "ordinal": item["ordinal"],
                    "classification": item["classification"],
                    "state": item["state"],
                    "reasons": json.loads(item["reason_codes_json"] or "[]"),
                    "title": json.loads(item["task_spec_json"]).get("title", "")[:200],
                }
                for item in items
            ],
            "integration_heads": [
                {"sequence": h["sequence"], "commit": h["commit_oid"], "source": h["source_kind"]}
                for h in (self.heads(queue["queue_id"]) if queue["mode"] == "development" else [])
            ],
            "aggregate_verification": aggregate.model_dump(mode="json"),
            "best_partial_candidates": self._best_partials(queue, items),
            "publication_authorized": False,
        }
        report_path = "prd5/final/queue-report.json"
        self.artifact_store.write_json(run_id, report_path, report, "queue_report")
        report_artifact = self.artifact_store.get_artifact_by_path(run_id, report_path)
        final = QueueFinalResultV1(
            run_id=run_id,
            queue_id=queue["queue_id"],
            queue_version_id=queue["active_version_id"],
            status=status,
            baseline=GitIdentityV1(commit=baseline, tree=git.commit_tree_of(baseline), object_format=git.object_format),
            final_integration=FinalIntegrationV1(commit=final_commit, tree=git.commit_tree_of(final_commit), sequence=head["sequence"] if head else 0),
            aggregate_verification=aggregate,
            counts=counts,
            task_results=task_results,
            final_patch_artifact_id=patch_artifact["artifact_id"],
            queue_report_artifact_id=report_artifact["artifact_id"],
            settled_at=_now(),
        )
        final_path = "prd5/final/queue-final-result.json"
        self.artifact_store.write_json(run_id, final_path, final.model_dump(mode="json"), "queue_final_result")
        final_artifact = self.artifact_store.get_artifact_by_path(run_id, final_path)
        source_artifact = self.artifact_store.get_artifact_by_path(run_id, "source-manifest.json")
        commit_ids = []
        with self.run_store.get_connection() as conn:
            commit_ids = [row["task_commit_id"] for row in conn.execute(
                "SELECT c.task_commit_id FROM h_task_commits c JOIN h_queue_items i ON i.task_id = c.task_id WHERE i.queue_version_id = ? AND i.state = 'INTEGRATED' ORDER BY c.created_at",
                (queue["active_version_id"],),
            ).fetchall()]
        handoff = ReleaseCandidateHandoffV1(
            run_id=run_id,
            source_identity_artifact_id=source_artifact["artifact_id"] if source_artifact else "unknown",
            queue_final_result_artifact_id=final_artifact["artifact_id"],
            baseline_commit=baseline,
            final_candidate_commit=final_commit,
            final_candidate_tree=git.commit_tree_of(final_commit),
            aggregate_status=aggregate.status,
            task_commit_ids=commit_ids,
            final_patch_input=FinalPatchInputV1(base=baseline, head=final_commit, artifact_id=patch_artifact["artifact_id"]),
            dirty_or_synthetic_baseline=bool(source["dirty_source_imported"]),
            allowed_next_actions=["EXPORT_PATCH", "EXPORT_RESULT_BUNDLE"],
        )
        self.artifact_store.write_json(run_id, "prd5/final/release-candidate-handoff.json", handoff.model_dump(mode="json"), "release_candidate_handoff")
        with self.run_store.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    INSERT INTO h_queue_results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                    """,
                    (f"qres_{uuid.uuid4().hex[:16]}", queue["queue_id"], queue["active_version_id"],
                     head["integration_head_id"] if head else None, aggregate_row_id, status,
                     counts.selected, counts.integrated, counts.failed, counts.blocked, counts.remaining,
                     patch_artifact["artifact_id"], report_artifact["artifact_id"], final_artifact["sha256"], _now()),
                )
                conn.execute("UPDATE h_task_queues SET state = 'SETTLED', updated_at = ? WHERE queue_id = ?", (_now(), queue["queue_id"]))
                self._event(conn, run_id, "QUEUE_SETTLED", {"status": status, "counts": counts.model_dump()})
                self._event(conn, run_id, "SOURCE_INTEGRITY_CONFIRMED", {"original_repository_writes": 0})
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        if queue["mode"] == "development" or len(items) > 1:
            try:
                snapshot = self.lifecycle.get(run_id)
                if S.QUEUE_SETTLED in ALLOWED_LIFECYCLE_TRANSITIONS.get(snapshot.state, set()):
                    self.lifecycle.transition(run_id, snapshot.state, snapshot.version, S.QUEUE_SETTLED,
                                              event_type="QUEUE_SETTLED_LIFECYCLE", payload={"status": status})
            except (KeyError, InvalidLifecycleTransitionError, StaleLifecycleError):
                pass
        return final

    def _best_partials(self, queue: Mapping[str, Any], items: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        """Best non-PASS candidate per unintegrated task (PRD 4 VFR-029), exported as a labeled patch.

        These are never integrated and never change the truthful status; they let a
        reviewer or external evaluator inspect the strongest attempt.
        """
        verifier = self.services.verifier
        if verifier is None:
            return []
        run_id = queue["run_id"]
        git = self.services.workspaces.git(run_id)
        exported: List[Dict[str, Any]] = []
        for item in items:
            if item["state"] in ("INTEGRATED", "VERIFIED_NOT_INTEGRATED"):
                continue
            outcome = verifier.task_outcome(item["task_id"])
            candidate_id = (outcome or {}).get("best_partial_candidate_id")
            if not candidate_id:
                latest = self.services.workspaces.latest_candidate(run_id, item["task_id"])
                candidate_id = latest["candidate_id"] if latest else None
            candidate = self.services.workspaces.get_candidate(candidate_id) if candidate_id else None
            if candidate is None:
                continue
            patch = git.diff(candidate["task_start_commit"], candidate["candidate_commit"])
            if not patch.strip():
                continue
            path = f"prd5/final/best-partial/{item['task_id']}.diff"
            self.artifact_store.write_bytes(run_id, path, patch, "text/x-diff", "final_patch_input", item["task_id"])
            artifact = self.artifact_store.get_artifact_by_path(run_id, path)
            exported.append({
                "task_id": item["task_id"],
                "status": (outcome or {}).get("status") or item["state"],
                "candidate_id": candidate_id,
                "candidate_commit": candidate["candidate_commit"],
                "base_commit": candidate["task_start_commit"],
                "patch_artifact_id": artifact["artifact_id"],
                "integrated": False,
            })
        return exported

    def _derive_status(self, queue: Mapping[str, Any], items: Sequence[Mapping[str, Any]], aggregate: str) -> str:
        states = [item["state"] for item in items]
        if any(state == "INTEGRATION_UNCERTAIN" for state in states):
            return "INTEGRATION_UNCERTAIN"
        canonical = [item for item in items if item["classification"] in ("ACTIONABLE", "DEPENDENT")]
        if queue["mode"] == "evaluation":
            passed = [item for item in canonical if item["state"] == "VERIFIED_NOT_INTEGRATED"]
            if canonical and len(passed) == len(canonical):
                return "COMPLETED_ALL"
            if passed:
                return "PARTIAL_SUCCESS"
            return self._no_success_status(states)
        integrated = [item for item in canonical if item["state"] == "INTEGRATED"]
        if integrated:
            if aggregate != "PASS":
                return {"FAILED": "FAILED", "BLOCKED_ENVIRONMENT": "BLOCKED_ENVIRONMENT"}.get(aggregate, "UNVERIFIED")
            if len(integrated) == len(canonical) and all(item["state"] in TERMINAL_ITEM_STATES for item in items):
                return "COMPLETED_ALL"
            return "PARTIAL_SUCCESS"
        return self._no_success_status(states)

    @staticmethod
    def _no_success_status(states: Sequence[str]) -> str:
        if not states:
            return "INVALID"
        if "CANCELLED" in states:
            return "CANCELLED"
        if "NEEDS_INPUT" in states or "NEEDS_APPROVAL" in states:
            return "NEEDS_INPUT"
        if any(state in ("BUDGET_EXHAUSTED", "REMAINING_BUDGET") for state in states):
            return "BUDGET_EXHAUSTED"
        if "BLOCKED_ENVIRONMENT" in states:
            return "BLOCKED_ENVIRONMENT"
        if any(state in ("FAILED", "INTEGRATION_FAILED") for state in states):
            return "FAILED"
        return "UNVERIFIED"

    @staticmethod
    def _counts(items: Sequence[Mapping[str, Any]]) -> FinalCountsV1:
        states = [item["state"] for item in items]
        return FinalCountsV1(
            selected=len(items),
            integrated=sum(1 for s in states if s in ("INTEGRATED", "VERIFIED_NOT_INTEGRATED")),
            failed=sum(1 for s in states if s in ("FAILED", "INTEGRATION_FAILED", "UNVERIFIED")),
            blocked=sum(1 for s in states if s in ("BLOCKED_DEPENDENCY", "BLOCKED_ENVIRONMENT", "NEEDS_INPUT", "UNSUPPORTED", "DUPLICATE", "NEEDS_APPROVAL")),
            remaining=sum(1 for s in states if s in ("PENDING", "READY", "REMAINING_BUDGET", "SKIPPED", "BUDGET_EXHAUSTED", "CANCELLED")),
        )

    # ------------------------------------------------------------ views
    def final_result(self, run_id: str) -> Optional[QueueFinalResultV1]:
        artifact = self.artifact_store.get_artifact_by_path(run_id, "prd5/final/queue-final-result.json")
        if not artifact:
            return None
        return QueueFinalResultV1.model_validate_json(self.artifact_store.open_readonly(run_id, "prd5/final/queue-final-result.json"))

    def progress(self, run_id: str) -> QueueProgressResultV1:
        queue = self.queue(run_id)
        if queue is None:
            raise QueueError("Run has no queue")
        items = self.items(queue)
        edges = self.edges(queue)
        states = {item["task_id"]: item["state"] for item in items}
        head = self.current_head(queue["queue_id"])
        git = self.services.workspaces.git(run_id)
        baseline = self.run_store.get_source_snapshot(run_id)["baseline_commit"]
        commit = head["commit_oid"] if head else baseline
        with self.run_store.get_connection() as conn:
            aggregate = conn.execute(
                "SELECT status FROM h_integration_verifications WHERE queue_id = ? AND scope = 'FINAL_AGGREGATE' ORDER BY settled_at DESC LIMIT 1",
                (queue["queue_id"],),
            ).fetchone()
            last_seq = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM h_events WHERE run_id = ?", (run_id,)).fetchone()[0]
        blocked = []
        for item in items:
            if item["state"] in ("PENDING", "BLOCKED_DEPENDENCY"):
                reason, blocking = graph.blocked_reason(item["task_id"], edges, states)
                if reason:
                    task_to_item = {i["task_id"]: i["queue_item_id"] for i in items}
                    blocked.append(BlockedItemV1(queue_item_id=item["queue_item_id"], reason=reason,
                                                 blocking_item_ids=[task_to_item[t] for t in blocking if t in task_to_item]))
        counts = self._counts(items)
        try:
            ledger = self.controller.budgets.get(run_id)
            calls = ledger["remaining_calls"]
            deadline = datetime.datetime.fromisoformat(ledger["deadline_at"])
            wall = int((deadline - datetime.datetime.now(datetime.timezone.utc)).total_seconds())
            reserve = ledger["remaining_calls"] >= ledger["reserved_future_calls"]
        except KeyError:
            calls, wall, reserve = 0, 0, True
        active = [item for item in items if item["state"] in ACTIVE_ITEM_STATES]
        return QueueProgressResultV1(
            run_id=run_id,
            queue_id=queue["queue_id"],
            queue_version_id=queue["active_version_id"],
            queue_state=queue["state"],
            integration=IntegrationStateV1(
                sequence=head["sequence"] if head else 0,
                commit=commit,
                tree=git.commit_tree_of(commit),
                aggregate_verification=(aggregate["status"] if aggregate and aggregate["status"] in ("PASS", "FAILED", "UNVERIFIED", "BLOCKED_ENVIRONMENT", "BUDGET_EXHAUSTED") else "NOT_RUN"),
            ),
            counts=QueueCountsV1(
                selected=counts.selected,
                actionable=sum(1 for item in items if item["classification"] in ("ACTIONABLE", "DEPENDENT")),
                integrated=counts.integrated,
                running=len(active),
                failed=counts.failed,
                blocked=counts.blocked,
                remaining=counts.remaining,
            ),
            active_item_id=active[0]["queue_item_id"] if active else None,
            blocked=blocked[:20],
            budget=QueueBudgetViewV1(model_calls_remaining=max(0, calls), wall_seconds_remaining=max(0, wall), final_reserve_intact=reserve),
            last_event_sequence=last_seq,
            updated_at=_now(),
        )

    # ----------------------------------------------------------- controls
    def request_pause(self, run_id: str) -> None:
        with self.run_store.get_connection() as conn:
            with conn:
                conn.execute("UPDATE h_task_queues SET pause_requested = 1, updated_at = ? WHERE run_id = ?", (_now(), run_id))

    def request_resume(self, run_id: str) -> None:
        with self.run_store.get_connection() as conn:
            with conn:
                conn.execute("UPDATE h_task_queues SET pause_requested = 0, updated_at = ? WHERE run_id = ?", (_now(), run_id))

    def request_cancel(self, run_id: str) -> None:
        with self.run_store.get_connection() as conn:
            with conn:
                conn.execute("UPDATE h_task_queues SET cancel_requested = 1, updated_at = ? WHERE run_id = ?", (_now(), run_id))

    def skip(self, run_id: str, task_id: str, reason: str) -> None:
        queue = self.queue(run_id)
        for item in self.items(queue):
            if item["task_id"] == task_id:
                if item["state"] not in ("PENDING", "READY"):
                    raise QueueError("Only an unstarted item can be skipped")
                self._set_item(item["queue_item_id"], "SKIPPED", [f"USER_SKIPPED:{reason[:64]}"])
                self.lifecycle.set_task_state(task_id, "SKIPPED")
                self._append(run_id, "QUEUE_ITEM_SKIPPED", {"task_id": task_id, "reason": reason[:200]})
                return
        raise QueueError(f"Task {task_id} is not in the queue")


def task_ref_aggregate(run_id: str) -> str:
    return f"refs/harness/runs/{safe_ref_component(run_id)}/aggregate/verified"


def _outcome_status(state: str) -> str:
    return {
        "FAILED": "FAILED",
        "UNVERIFIED": "UNVERIFIED",
        "BLOCKED_ENVIRONMENT": "BLOCKED_ENVIRONMENT",
        "BUDGET_EXHAUSTED": "BUDGET_EXHAUSTED",
        "NEEDS_INPUT": "NEEDS_INPUT",
        "UNSUPPORTED": "NEEDS_INPUT",
        "CANCELLED": "CANCELLED",
    }.get(state, "UNVERIFIED")
