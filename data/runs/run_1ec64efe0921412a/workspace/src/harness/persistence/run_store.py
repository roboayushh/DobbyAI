"""harness/persistence/run_store.py
SQLite-backed persistence for runs, events, tasks, source snapshots, workspaces, and dependencies.
Follows all transactional and state invariant requirements.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import sqlite3
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple

from harness.contracts import (
    ALLOWED_TRANSITIONS,
    RunRequestV1,
    RunState,
    SourceIdentityV1,
    TaskSpecV1,
)
from harness.persistence.migrator import apply_migrations


def canonical_json(data: Any) -> str:
    """Return UTF-8 canonical JSON with sorted keys and minimal whitespace."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class IdempotencyConflictError(Exception):
    """Raised when an idempotency key is replayed with a different request."""
    pass


class InvalidStateTransitionError(Exception):
    """Raised when an illegal state machine transition is attempted."""
    pass


class DependencyCycleError(Exception):
    """Raised when a task dependency cycle is detected."""
    pass


class RunStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA busy_timeout = 30000;")
        return conn

    def _init_db(self) -> None:
        with self.get_connection() as conn:
            apply_migrations(conn)

    def create_run(
        self,
        run_id: str,
        request: RunRequestV1,
    ) -> Tuple[str, bool]:
        """Create a new run and record the RUN_CREATED event atomically.
        If idempotency_key already exists:
          - If request_json matches, returns (existing_run_id, False).
          - If request_json differs, raises IdempotencyConflictError.
        Returns: (run_id, is_new)
        """
        request_dict = request.model_dump(mode="json")
        req_json = canonical_json(request_dict)
        req_sha256 = compute_sha256(req_json)
        now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()

        with self.get_connection() as conn:
            with conn:
                # Check idempotency
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT run_id, request_json FROM h_runs WHERE idempotency_key = ?",
                    (request.idempotency_key,),
                )
                row = cursor.fetchone()
                if row:
                    existing_run_id, existing_json = row["run_id"], row["request_json"]
                    if existing_json == req_json:
                        return existing_run_id, False
                    else:
                        raise IdempotencyConflictError(
                            f"Idempotency key '{request.idempotency_key}' was previously used with a different request."
                        )

                # Insert run
                conn.execute(
                    """
                    INSERT INTO h_runs (
                        run_id, schema_version, idempotency_key, task_mode, execution_mode,
                        state, request_json, request_sha256, runtime_profile, evaluation_profile,
                        next_event_seq, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        request.schema_version,
                        request.idempotency_key,
                        request.task_mode.value,
                        request.execution_mode.value,
                        RunState.NEW.value,
                        req_json,
                        req_sha256,
                        request.runtime_profile,
                        request.evaluation_profile,
                        2,  # next event will be 2
                        now_utc,
                        now_utc,
                    ),
                )

                # Record RUN_CREATED event (seq=1)
                event_id = f"evt_{uuid.uuid4().hex[:16]}"
                payload = {
                    "run_id": run_id,
                    "idempotency_key": request.idempotency_key,
                    "task_mode": request.task_mode.value,
                    "execution_mode": request.execution_mode.value,
                    "request_sha256": req_sha256,
                }
                payload_json = canonical_json(payload)
                payload_sha256 = compute_sha256(payload_json)

                conn.execute(
                    """
                    INSERT INTO h_events (
                        event_id, run_id, seq, event_type, from_state, to_state,
                        payload_json, payload_sha256, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        run_id,
                        1,
                        "RUN_CREATED",
                        None,
                        RunState.NEW.value,
                        payload_json,
                        payload_sha256,
                        now_utc,
                    ),
                )

        return run_id, True

    def transition(
        self,
        run_id: str,
        from_state: RunState,
        to_state: RunState,
        event_type: str,
        payload: Dict[str, Any],
        error_code: Optional[str] = None,
        dedupe_key: Optional[str] = None,
    ) -> int:
        """Perform a state machine transition atomically with event logging.
        Returns the allocated event sequence number.
        """
        allowed = ALLOWED_TRANSITIONS.get(from_state, set())
        if to_state not in allowed:
            raise InvalidStateTransitionError(
                f"Illegal transition from {from_state.value} to {to_state.value}"
            )

        now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
        payload_json = canonical_json(payload)
        payload_sha256 = compute_sha256(payload_json)

        with self.get_connection() as conn:
            with conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT state, next_event_seq FROM h_runs WHERE run_id = ?",
                    (run_id,),
                )
                row = cursor.fetchone()
                if not row:
                    raise KeyError(f"Run {run_id} not found")

                current_state, seq = row["state"], row["next_event_seq"]
                if current_state != from_state.value:
                    raise InvalidStateTransitionError(
                        f"Run {run_id} is in state {current_state}, cannot transition from {from_state.value} to {to_state.value}"
                    )

                event_id = f"evt_{uuid.uuid4().hex[:16]}"

                conn.execute(
                    """
                    UPDATE h_runs
                    SET state = ?, error_code = ?, updated_at = ?, next_event_seq = ?
                    WHERE run_id = ?
                    """,
                    (to_state.value, error_code, now_utc, seq + 1, run_id),
                )

                conn.execute(
                    """
                    INSERT INTO h_events (
                        event_id, run_id, seq, event_type, from_state, to_state,
                        payload_json, payload_sha256, dedupe_key, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        run_id,
                        seq,
                        event_type,
                        from_state.value,
                        to_state.value,
                        payload_json,
                        payload_sha256,
                        dedupe_key,
                        now_utc,
                    ),
                )
                return seq

    def append_event(
        self,
        run_id: str,
        event_type: str,
        payload: Dict[str, Any],
        from_state: Optional[str] = None,
        to_state: Optional[str] = None,
        dedupe_key: Optional[str] = None,
    ) -> int:
        """Append an event without changing run state.
        Uses h_runs.next_event_seq atomically.
        """
        now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
        payload_json = canonical_json(payload)
        payload_sha256 = compute_sha256(payload_json)

        with self.get_connection() as conn:
            with conn:
                cursor = conn.cursor()
                cursor.execute("SELECT state, next_event_seq FROM h_runs WHERE run_id = ?", (run_id,))
                row = cursor.fetchone()
                if not row:
                    raise KeyError(f"Run {run_id} not found")

                current_state, seq = row["state"], row["next_event_seq"]
                fs = from_state if from_state is not None else current_state
                ts = to_state if to_state is not None else current_state

                event_id = f"evt_{uuid.uuid4().hex[:16]}"
                conn.execute(
                    "UPDATE h_runs SET next_event_seq = ?, updated_at = ? WHERE run_id = ?",
                    (seq + 1, now_utc, run_id),
                )
                conn.execute(
                    """
                    INSERT INTO h_events (
                        event_id, run_id, seq, event_type, from_state, to_state,
                        payload_json, payload_sha256, dedupe_key, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        run_id,
                        seq,
                        event_type,
                        fs,
                        ts,
                        payload_json,
                        payload_sha256,
                        dedupe_key,
                        now_utc,
                    ),
                )
                return seq

    def put_source_snapshot(
        self,
        snapshot: SourceIdentityV1,
        run_id: str,
        repo_bytes: int,
        file_count: int,
    ) -> str:
        snapshot_id = f"src_{uuid.uuid4().hex[:16]}"
        with self.get_connection() as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO h_source_snapshots (
                        source_snapshot_id, run_id, source_kind, canonical_locator,
                        upstream_commit, baseline_commit, baseline_tree,
                        content_tree_sha256, import_manifest_sha256, dirty_source_imported,
                        repo_bytes, file_count, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        run_id,
                        snapshot.source_kind,
                        snapshot.canonical_locator,
                        snapshot.upstream_commit,
                        snapshot.baseline_commit,
                        snapshot.baseline_tree,
                        snapshot.content_tree_sha256,
                        snapshot.import_manifest_sha256,
                        1 if snapshot.dirty_source_imported else 0,
                        repo_bytes,
                        file_count,
                        snapshot.created_at,
                    ),
                )
        return snapshot_id

    def put_workspace(
        self,
        run_id: str,
        workspace_id: str,
        source_snapshot_id: str,
        root_relpath: str,
        bare_repo_relpath: str,
        worktree_relpath: str,
        state: str = "READY",
        writable: bool = False,
    ) -> None:
        now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self.get_connection() as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO h_workspaces (
                        workspace_id, run_id, source_snapshot_id, root_relpath,
                        bare_repo_relpath, worktree_relpath, state, writable,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        workspace_id,
                        run_id,
                        source_snapshot_id,
                        root_relpath,
                        bare_repo_relpath,
                        worktree_relpath,
                        state,
                        1 if writable else 0,
                        now_utc,
                        now_utc,
                    ),
                )

    def put_tasks(self, run_id: str, tasks: List[TaskSpecV1]) -> None:
        """Insert tasks in a single transaction."""
        now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self.get_connection() as conn:
            with conn:
                for task in tasks:
                    spec_json = canonical_json(task.model_dump(mode="json"))
                    conn.execute(
                        """
                        INSERT INTO h_tasks (
                            task_id, run_id, ordinal, source_type, source_key,
                            source_snapshot_id, raw_content_sha256, normalized_content_sha256,
                            task_spec_json, state, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            task.task_id,
                            run_id,
                            task.ordinal,
                            task.source_type,
                            task.source_key,
                            task.source_snapshot_id,
                            task.raw_content_sha256,
                            task.normalized_content_sha256,
                            spec_json,
                            "QUEUED",
                            now_utc,
                        ),
                    )

    def put_task_dependencies(
        self,
        dependencies: List[Tuple[str, str, str]],  # (task_id, depends_on_task_id, type)
    ) -> None:
        """Validate DAG (no cycles) and insert dependencies."""
        if not dependencies:
            return

        # Check self-dependency
        for tid, dep_id, _ in dependencies:
            if tid == dep_id:
                raise DependencyCycleError(f"Task {tid} cannot depend on itself")

        # Cycle detection using DFS
        adj: Dict[str, Set[str]] = {}
        for tid, dep_id, _ in dependencies:
            adj.setdefault(tid, set()).add(dep_id)

        visited: Dict[str, int] = {}  # 0: visiting, 1: visited

        def dfs(node: str) -> None:
            visited[node] = 0
            for neighbor in adj.get(node, set()):
                if neighbor in visited:
                    if visited[neighbor] == 0:
                        raise DependencyCycleError(f"Cycle detected involving {node} -> {neighbor}")
                else:
                    dfs(neighbor)
            visited[node] = 1

        for node in list(adj.keys()):
            if node not in visited:
                dfs(node)

        now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self.get_connection() as conn:
            with conn:
                for tid, dep_id, dep_type in dependencies:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO h_task_dependencies (
                            task_id, depends_on_task_id, dependency_type, created_at
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (tid, dep_id, dep_type, now_utc),
                    )

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM h_runs WHERE run_id = ?", (run_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_run_by_idempotency_key(self, key: str) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM h_runs WHERE idempotency_key = ?", (key,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_source_snapshot(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM h_source_snapshots WHERE run_id = ?", (run_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_workspace(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM h_workspaces WHERE run_id = ?", (run_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_tasks(self, run_id: str) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM h_tasks WHERE run_id = ? ORDER BY ordinal ASC", (run_id,))
            return [dict(r) for r in cursor.fetchall()]

    def get_events(self, run_id: str) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM h_events WHERE run_id = ? ORDER BY seq ASC", (run_id,))
            return [dict(r) for r in cursor.fetchall()]

    def get_nonterminal_runs(self) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM h_runs WHERE state IN ('NEW', 'VALIDATING', 'ACQUIRING', 'PREPARING') ORDER BY created_at ASC"
            )
            return [dict(r) for r in cursor.fetchall()]

    def get_latest_run(self) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM h_runs ORDER BY created_at DESC LIMIT 1")
            row = cursor.fetchone()
            return dict(row) if row else None
