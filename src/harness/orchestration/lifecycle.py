"""Additive PRD 2 run/task lifecycle with optimistic concurrency."""
from __future__ import annotations

import datetime
import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional, Set

from harness.contracts import OrchestrationState
from harness.persistence import RunStore, canonical_json


class InvalidLifecycleTransitionError(RuntimeError):
    code = "INVALID_LIFECYCLE_TRANSITION"


class StaleLifecycleError(RuntimeError):
    code = "STALE_LIFECYCLE_VERSION"


_S = OrchestrationState

ALLOWED_LIFECYCLE_TRANSITIONS: Dict[OrchestrationState, Set[OrchestrationState]] = {
    _S.PREPARED: {_S.INDEXING, _S.CANCELLED},
    _S.INDEXING: {_S.PLANNING, _S.FAILED, _S.CANCELLED, _S.BLOCKED_ENVIRONMENT},
    _S.PLANNING: {
        _S.PLAN_READY,
        _S.NEEDS_INPUT,
        _S.NEEDS_CAPABILITY,
        _S.BUDGET_EXHAUSTED,
        _S.FAILED,
        _S.CANCELLED,
    },
    _S.PLAN_READY: {_S.CODING, _S.CANCELLED, _S.BLOCKED_ENVIRONMENT, _S.BUDGET_EXHAUSTED, _S.FAILED},
    _S.CODING: {
        _S.ACTION_PROPOSED,
        _S.REPLANNING,
        _S.VERIFICATION_REQUIRED,
        _S.NEEDS_CAPABILITY,
        _S.BUDGET_EXHAUSTED,
        _S.FAILED,
        _S.CANCELLED,
    },
    _S.REPLANNING: {_S.PLANNING, _S.BUDGET_EXHAUSTED, _S.FAILED, _S.CANCELLED},
    _S.NEEDS_INPUT: {_S.PLANNING, _S.CANCELLED, _S.QUEUE_SETTLED},
    _S.NEEDS_CAPABILITY: {_S.CANCELLED, _S.QUEUE_SETTLED},
    # PRD 3: an admitted proposal executes; a stale/denied one returns the
    # decision to the coder; guided mode waits for an exact approval.
    _S.ACTION_PROPOSED: {
        _S.ACTION_EXECUTING,
        _S.NEEDS_APPROVAL,
        _S.CODING,
        _S.BLOCKED_ENVIRONMENT,
        _S.BUDGET_EXHAUSTED,
        _S.FAILED,
        _S.CANCELLED,
    },
    _S.NEEDS_APPROVAL: {_S.ACTION_EXECUTING, _S.CODING, _S.BUDGET_EXHAUSTED, _S.CANCELLED},
    _S.ACTION_EXECUTING: {
        _S.CODING,
        _S.ACTION_PROPOSED,
        _S.NEEDS_APPROVAL,
        _S.REPLANNING,
        _S.ACTION_UNKNOWN,
        _S.BLOCKED_ENVIRONMENT,
        _S.BUDGET_EXHAUSTED,
        _S.FAILED,
        _S.CANCELLED,
    },
    _S.ACTION_UNKNOWN: {_S.CODING, _S.FAILED, _S.CANCELLED},
    _S.BLOCKED_ENVIRONMENT: {
        _S.ACTION_PROPOSED,
        _S.VERIFICATION_REQUIRED,
        _S.PLAN_READY,
        _S.CANCELLED,
        _S.QUEUE_SETTLED,
    },
    # PRD 4: fresh verification of the frozen candidate and bounded repair.
    _S.VERIFICATION_REQUIRED: {_S.VERIFYING, _S.CANCELLED, _S.FAILED},
    _S.VERIFYING: {
        _S.READY_FOR_REVIEW,
        _S.REPAIRING,
        _S.VERIFICATION_FAILED,
        _S.UNVERIFIED,
        _S.BLOCKED_ENVIRONMENT,
        _S.BUDGET_EXHAUSTED,
        _S.NEEDS_INPUT,
        _S.CANCELLED,
        _S.FAILED,
    },
    _S.REPAIRING: {_S.CODING, _S.REPLANNING, _S.BUDGET_EXHAUSTED, _S.CANCELLED, _S.FAILED},
    _S.READY_FOR_REVIEW: {_S.QUEUE_SETTLED},
    _S.VERIFICATION_FAILED: {_S.QUEUE_SETTLED},
    _S.UNVERIFIED: {_S.QUEUE_SETTLED},
    _S.BUDGET_EXHAUSTED: {_S.QUEUE_SETTLED},
    _S.FAILED: {_S.QUEUE_SETTLED},
    _S.CANCELLED: set(),
    _S.QUEUE_SETTLED: set(),
}

# States in which the active task has settled and a queue coordinator may
# select the next task. CANCELLED is run-level and never advances.
TASK_TERMINAL_STATES: Set[OrchestrationState] = {
    _S.READY_FOR_REVIEW,
    _S.VERIFICATION_FAILED,
    _S.UNVERIFIED,
    _S.BLOCKED_ENVIRONMENT,
    _S.NEEDS_INPUT,
    _S.NEEDS_CAPABILITY,
    _S.BUDGET_EXHAUSTED,
    _S.FAILED,
}


TASK_STATE_FOR_RUN = {
    _S.INDEXING: "INDEXING",
    _S.PLANNING: "PLANNING",
    _S.PLAN_READY: "PLANNED",
    _S.CODING: "CODING",
    _S.ACTION_PROPOSED: "ACTION_PROPOSED",
    _S.VERIFICATION_REQUIRED: "VERIFICATION_REQUIRED",
    _S.NEEDS_INPUT: "NEEDS_INPUT",
    _S.NEEDS_CAPABILITY: "NEEDS_CAPABILITY",
    _S.BUDGET_EXHAUSTED: "BUDGET_EXHAUSTED",
    _S.FAILED: "FAILED",
    _S.CANCELLED: "CANCELLED",
    _S.ACTION_EXECUTING: "ACTION_EXECUTING",
    _S.NEEDS_APPROVAL: "NEEDS_APPROVAL",
    _S.BLOCKED_ENVIRONMENT: "BLOCKED_ENVIRONMENT",
    _S.ACTION_UNKNOWN: "ACTION_UNKNOWN",
    _S.VERIFYING: "VERIFYING",
    _S.REPAIRING: "REPAIRING",
    _S.READY_FOR_REVIEW: "READY_FOR_REVIEW",
    _S.VERIFICATION_FAILED: "VERIFICATION_FAILED",
    _S.UNVERIFIED: "UNVERIFIED",
}


def utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


@dataclass(frozen=True)
class LifecycleSnapshot:
    run_id: str
    state: OrchestrationState
    active_task_id: str
    version: int
    stop_reason_code: Optional[str]


class LifecycleService:
    def __init__(self, run_store: RunStore) -> None:
        self.run_store = run_store

    def initialize(self, run_id: str) -> LifecycleSnapshot:
        now = utc_now().isoformat()
        with self.run_store.get_connection() as conn:
            with conn:
                run = conn.execute("SELECT * FROM h_runs WHERE run_id = ?", (run_id,)).fetchone()
                if not run:
                    raise KeyError(f"Run not found: {run_id}")
                if run["state"] != "PREPARED":
                    raise InvalidLifecycleTransitionError(
                        f"PRD 2 requires PREPARED handoff, got {run['state']}"
                    )
                tasks = conn.execute(
                    "SELECT task_id FROM h_tasks WHERE run_id = ? ORDER BY ordinal", (run_id,)
                ).fetchall()
                if not tasks:
                    raise InvalidLifecycleTransitionError("Prepared run has no tasks")
                for task in tasks:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO h_task_lifecycle(
                            task_id, state, task_revision, version, updated_at
                        ) VALUES (?, 'QUEUED', 1, 1, ?)
                        """,
                        (task["task_id"], now),
                    )
                active = conn.execute(
                    """
                    SELECT t.task_id
                    FROM h_tasks t
                    WHERE t.run_id = ? AND t.state = 'QUEUED'
                      AND NOT EXISTS (
                          SELECT 1 FROM h_task_dependencies d
                          WHERE d.task_id = t.task_id AND d.dependency_type = 'blocks'
                      )
                    ORDER BY t.ordinal
                    LIMIT 1
                    """,
                    (run_id,),
                ).fetchone()
                if not active:
                    raise InvalidLifecycleTransitionError("No unblocked queued task is available")
                conn.execute(
                    """
                    INSERT OR IGNORE INTO h_run_lifecycle(
                        run_id, state, active_task_id, version, created_at, updated_at
                    ) VALUES (?, 'PREPARED', ?, 1, ?, ?)
                    """,
                    (run_id, active["task_id"], now, now),
                )
        return self.get(run_id)

    def get(self, run_id: str) -> LifecycleSnapshot:
        with self.run_store.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM h_run_lifecycle WHERE run_id = ?", (run_id,)
            ).fetchone()
        if not row:
            raise KeyError(f"Lifecycle not initialized for run: {run_id}")
        if not row["active_task_id"]:
            raise InvalidLifecycleTransitionError("Lifecycle has no active task")
        return LifecycleSnapshot(
            run_id=run_id,
            state=OrchestrationState(row["state"]),
            active_task_id=row["active_task_id"],
            version=row["version"],
            stop_reason_code=row["stop_reason_code"],
        )

    def transition(
        self,
        run_id: str,
        expected_state: OrchestrationState,
        expected_version: int,
        new_state: OrchestrationState,
        *,
        event_type: str,
        payload: Optional[Dict[str, Any]] = None,
        stop_reason_code: Optional[str] = None,
    ) -> LifecycleSnapshot:
        allowed = ALLOWED_LIFECYCLE_TRANSITIONS.get(expected_state, set())
        if new_state not in allowed:
            raise InvalidLifecycleTransitionError(
                f"Illegal PRD 2 transition {expected_state.value} -> {new_state.value}"
            )
        now = utc_now().isoformat()
        with self.run_store.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM h_run_lifecycle WHERE run_id = ?", (run_id,)
            ).fetchone()
            if not row:
                conn.rollback()
                raise KeyError(f"Lifecycle not initialized for run: {run_id}")
            if row["state"] != expected_state.value or row["version"] != expected_version:
                conn.rollback()
                raise StaleLifecycleError(
                    f"Expected {expected_state.value}@{expected_version}, got {row['state']}@{row['version']}"
                )
            active_task_id = row["active_task_id"]
            changed = conn.execute(
                """
                UPDATE h_run_lifecycle
                SET state = ?, version = version + 1, stop_reason_code = ?, updated_at = ?
                WHERE run_id = ? AND state = ? AND version = ?
                """,
                (
                    new_state.value,
                    stop_reason_code,
                    now,
                    run_id,
                    expected_state.value,
                    expected_version,
                ),
            ).rowcount
            if changed != 1:
                conn.rollback()
                raise StaleLifecycleError("Lifecycle compare-and-swap failed")
            task_state = TASK_STATE_FOR_RUN.get(new_state)
            if task_state:
                conn.execute(
                    """
                    UPDATE h_task_lifecycle
                    SET state = ?, version = version + 1, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (task_state, now, active_task_id),
                )
            self._append_event(
                conn,
                run_id,
                event_type,
                {
                    **(payload or {}),
                    "lifecycle_version": expected_version + 1,
                    "active_task_id": active_task_id,
                },
                expected_state.value,
                new_state.value,
                now,
            )
            conn.commit()
        return self.get(run_id)

    def advance_task(
        self,
        run_id: str,
        expected_version: int,
        next_task_id: str,
        *,
        payload: Optional[Dict[str, Any]] = None,
    ) -> LifecycleSnapshot:
        """Queue-only transition: settle the active task and start the next one.

        Only a queue coordinator calls this after the previous task reached a
        task-terminal state. The new task always restarts at INDEXING so it is
        re-indexed and replanned against its own recorded start commit.
        """
        now = utc_now().isoformat()
        with self.run_store.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM h_run_lifecycle WHERE run_id = ?", (run_id,)
            ).fetchone()
            if not row:
                conn.rollback()
                raise KeyError(f"Lifecycle not initialized for run: {run_id}")
            current = OrchestrationState(row["state"])
            if row["version"] != expected_version:
                conn.rollback()
                raise StaleLifecycleError(
                    f"Expected version {expected_version}, got {row['version']}"
                )
            if current not in TASK_TERMINAL_STATES:
                conn.rollback()
                raise InvalidLifecycleTransitionError(
                    f"Cannot advance queue while active task is {current.value}"
                )
            task = conn.execute(
                "SELECT task_id FROM h_tasks WHERE task_id = ? AND run_id = ?",
                (next_task_id, run_id),
            ).fetchone()
            if not task:
                conn.rollback()
                raise InvalidLifecycleTransitionError("Next task does not belong to this run")
            conn.execute(
                """
                INSERT OR IGNORE INTO h_task_lifecycle(task_id, state, task_revision, version, updated_at)
                VALUES (?, 'QUEUED', 1, 1, ?)
                """,
                (next_task_id, now),
            )
            conn.execute(
                """
                UPDATE h_run_lifecycle
                SET state = 'INDEXING', active_task_id = ?, version = version + 1,
                    stop_reason_code = NULL, updated_at = ?
                WHERE run_id = ? AND version = ?
                """,
                (next_task_id, now, run_id, expected_version),
            )
            conn.execute(
                "UPDATE h_task_lifecycle SET state = 'INDEXING', version = version + 1, updated_at = ? WHERE task_id = ?",
                (now, next_task_id),
            )
            self._append_event(
                conn,
                run_id,
                "QUEUE_TASK_ADVANCED",
                {
                    **(payload or {}),
                    "previous_task_id": row["active_task_id"],
                    "previous_state": current.value,
                    "active_task_id": next_task_id,
                    "lifecycle_version": expected_version + 1,
                },
                current.value,
                OrchestrationState.INDEXING.value,
                now,
            )
            conn.commit()
        return self.get(run_id)

    def select_initial_task(self, run_id: str, task_id: str) -> LifecycleSnapshot:
        """Queue-only: choose the first scheduled task before any work starts."""
        now = utc_now().isoformat()
        with self.run_store.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM h_run_lifecycle WHERE run_id = ?", (run_id,)).fetchone()
            if not row:
                conn.rollback()
                raise KeyError(f"Lifecycle not initialized for run: {run_id}")
            if row["active_task_id"] == task_id:
                conn.rollback()
                return self.get(run_id)
            if row["state"] != "PREPARED":
                conn.rollback()
                raise InvalidLifecycleTransitionError("The first queue task can only be chosen before work starts")
            conn.execute(
                "UPDATE h_run_lifecycle SET active_task_id = ?, version = version + 1, updated_at = ? WHERE run_id = ?",
                (task_id, now, run_id),
            )
            self._append_event(conn, run_id, "QUEUE_FIRST_TASK_SELECTED", {"active_task_id": task_id}, "PREPARED", "PREPARED", now)
            conn.commit()
        return self.get(run_id)

    def set_task_state(self, task_id: str, state: str) -> None:
        """Record a queue-level task outcome (for example SKIPPED) without touching the run."""
        now = utc_now().isoformat()
        with self.run_store.get_connection() as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO h_task_lifecycle(task_id, state, task_revision, version, updated_at)
                    VALUES (?, ?, 1, 1, ?)
                    ON CONFLICT(task_id) DO UPDATE SET state = excluded.state,
                        version = h_task_lifecycle.version + 1, updated_at = excluded.updated_at
                    """,
                    (task_id, state, now),
                )

    @staticmethod
    def _append_event(
        conn: Any,
        run_id: str,
        event_type: str,
        payload: Dict[str, Any],
        from_state: str,
        to_state: str,
        created_at: str,
    ) -> int:
        from harness.persistence.events import append_event_sql

        return append_event_sql(conn, run_id, event_type, payload, from_state, to_state, created_at)
