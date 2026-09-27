"""Intent / observe / settle protocol for every managed Git ref move (PRD 5 section 19).

SQLite and Git share no transaction, so each ref change is journaled first
with its expected-old and desired-new object IDs, dispatched as one atomic
``update-ref`` compare-and-swap, then observed and settled. Recovery never
guesses: it maps the observed ref to applied, not-applied, or uncertain.
"""
from __future__ import annotations

import datetime
import hashlib
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from harness.gitflow.private_git import PrivateGit, RefCASError
from harness.persistence import RunStore, canonical_json
from harness.persistence.events import append_event_sql


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class StaleFencingTokenError(RuntimeError):
    code = "STALE_QUEUE_LEASE"


@dataclass(frozen=True)
class RefObservation:
    operation_id: str
    state: str  # APPLIED, NOT_APPLIED, UNCERTAIN
    observed: Optional[str]


class RefOperationJournal:
    def __init__(self, run_store: RunStore) -> None:
        self.run_store = run_store

    def prepare(
        self,
        *,
        run_id: str,
        queue_id: str,
        kind: str,
        ref: str,
        expected_old: Optional[str],
        desired_new: str,
        fencing_token: int,
        conn=None,
    ) -> str:
        operation_id = f"grop_{uuid.uuid4().hex[:16]}"
        intent = {
            "operation_id": operation_id,
            "kind": kind,
            "ref": ref,
            "expected_old": expected_old,
            "desired_new": desired_new,
            "fencing_token": fencing_token,
        }
        statement = (
            """
            INSERT INTO h_git_ref_operations(
                ref_operation_id, run_id, queue_id, operation_kind, ref_name, expected_old_oid,
                desired_new_oid, lease_fencing_token, state, intent_sha256, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PREPARED', ?, ?)
            """,
            (
                operation_id, run_id, queue_id, kind, ref, expected_old, desired_new, fencing_token,
                hashlib.sha256(canonical_json(intent).encode()).hexdigest(), _now(),
            ),
        )
        if conn is not None:
            conn.execute(*statement)
            append_event_sql(conn, run_id, "GIT_REF_OPERATION_PREPARED", intent, "PRD5", "PRD5", _now())
            return operation_id
        with self.run_store.get_connection() as own:
            with own:
                own.execute(*statement)
                append_event_sql(own, run_id, "GIT_REF_OPERATION_PREPARED", intent, "PRD5", "PRD5", _now())
        return operation_id

    def get(self, operation_id: str) -> Dict[str, Any]:
        with self.run_store.get_connection() as conn:
            return dict(conn.execute("SELECT * FROM h_git_ref_operations WHERE ref_operation_id = ?", (operation_id,)).fetchone())

    def _assert_fence(self, conn, queue_id: str, token: int) -> None:
        row = conn.execute(
            "SELECT fencing_token FROM h_queue_leases WHERE queue_id = ? AND state = 'ACTIVE'", (queue_id,)
        ).fetchone()
        if not row or row["fencing_token"] != token:
            raise StaleFencingTokenError("Queue lease was replaced; a late owner cannot settle ref operations")

    def dispatch(self, git: PrivateGit, operation_id: str) -> RefObservation:
        op = self.get(operation_id)
        if op["state"] in ("APPLIED", "NOT_APPLIED", "UNCERTAIN", "FAILED"):
            return RefObservation(operation_id, op["state"], op["observed_oid"])
        with self.run_store.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._assert_fence(conn, op["queue_id"], op["lease_fencing_token"])
                conn.execute(
                    "UPDATE h_git_ref_operations SET state = 'DISPATCHED', dispatched_at = ? WHERE ref_operation_id = ?",
                    (_now(), operation_id),
                )
                append_event_sql(conn, op["run_id"], "GIT_REF_OPERATION_DISPATCHED", {"operation_id": operation_id}, "PRD5", "PRD5", _now())
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        try:
            git.update_ref_cas(op["ref_name"], op["desired_new_oid"], op["expected_old_oid"])
        except RefCASError:
            pass
        return self.observe(git, operation_id)

    def observe(self, git: PrivateGit, operation_id: str) -> RefObservation:
        """Map the observed ref onto the journaled intent (section 19.2 table)."""
        op = self.get(operation_id)
        observed = git.read_ref(op["ref_name"])
        if observed == op["desired_new_oid"]:
            state = "APPLIED"
        elif observed == op["expected_old_oid"] or (observed is None and op["expected_old_oid"] is None):
            state = "NOT_APPLIED"
        else:
            state = "UNCERTAIN"
        with self.run_store.get_connection() as conn:
            with conn:
                conn.execute(
                    "UPDATE h_git_ref_operations SET state = ?, observed_oid = ?, settled_at = ? WHERE ref_operation_id = ?",
                    (state, observed, _now(), operation_id),
                )
                append_event_sql(conn, op["run_id"], "GIT_REF_OPERATION_OBSERVED", {
                    "operation_id": operation_id, "observed": observed, "state": state,
                }, "PRD5", "PRD5", _now())
        return RefObservation(operation_id, state, observed)

    def unsettled(self, queue_id: str) -> List[Dict[str, Any]]:
        with self.run_store.get_connection() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM h_git_ref_operations WHERE queue_id = ? AND state IN ('PREPARED', 'DISPATCHED') ORDER BY created_at",
                (queue_id,),
            ).fetchall()]
