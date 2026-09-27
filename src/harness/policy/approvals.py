"""Exact, expiring, one-use approvals for guided/delegated actions.

A grant is bound to the full approval binding hash. Any change to code,
paths, capabilities, workspace version, runtime, limits, or policy produces a
different hash, so the grant no longer matches. Consumption happens inside the
same transaction that persists the action intent.
"""
from __future__ import annotations

import datetime
import hashlib
import sqlite3
import uuid
from typing import Any, Dict, List, Optional

from harness.persistence import ArtifactStore, RunStore, canonical_json


class ApprovalError(RuntimeError):
    code = "APPROVAL_INVALID"


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _sha(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class ApprovalService:
    DEFAULT_TTL_SECONDS = 3600
    CONSEQUENCES = (
        "Runs untrusted code only inside the private Docker workspace with network disabled; "
        "it cannot modify the original repository, read host credentials, or publish anything. "
        "The grant is single-use and bound to the exact code hash, paths, capabilities, "
        "workspace version, runtime, limits, and policy shown."
    )

    def __init__(self, run_store: RunStore, artifact_store: ArtifactStore) -> None:
        self.run_store = run_store
        self.artifact_store = artifact_store

    def create(
        self,
        *,
        run_id: str,
        task_id: str,
        action_id: str,
        purpose: str,
        binding: Dict[str, Any],
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        max_uses: int = 1,
    ) -> Dict[str, Any]:
        with self.run_store.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM h_action_approval_requests WHERE action_id = ?", (action_id,)
            ).fetchone()
        if row:
            return dict(row)
        request_id = f"apr_{uuid.uuid4().hex[:16]}"
        expires = (_now() + datetime.timedelta(seconds=ttl_seconds)).isoformat()
        request = {
            "schema_version": "1.0",
            "approval_request_id": request_id,
            "run_id": run_id,
            "task_id": task_id,
            "operation": "execute_workspace_action",
            "purpose": purpose[:2000] or "Execute a proposed workspace action",
            "action_proposal_id": binding["action_proposal_id"],
            "code_sha256": binding["code_sha256"],
            "workspace_version": binding["workspace_version"],
            "capabilities": binding["capabilities"],
            "paths": binding["paths"],
            "runtime_profile_fingerprint": binding["runtime_profile_fingerprint"],
            "network_policy_fingerprint": binding["network_policy_fingerprint"],
            "limits_fingerprint": binding["limits_fingerprint"],
            "policy_version": binding["policy_version"],
            "max_uses": max_uses,
            "expires_at": expires,
            "consequences": self.CONSEQUENCES,
        }
        consequence_path = f"prd3/approvals/{request_id}.json"
        self.artifact_store.write_json(run_id, consequence_path, request, "approval_consequence", task_id)
        artifact = self.artifact_store.get_artifact_by_path(run_id, consequence_path)
        assert artifact is not None
        now = _now().isoformat()
        with self.run_store.get_connection() as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO h_action_approval_requests(
                        approval_request_id, run_id, task_id, action_id, binding_json,
                        binding_sha256, consequence_artifact_id, state, max_uses,
                        used_count, expires_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, 0, ?, ?, ?)
                    """,
                    (
                        request_id, run_id, task_id, action_id, canonical_json(binding), _sha(binding),
                        artifact["artifact_id"], max_uses, expires, now, now,
                    ),
                )
                self._event(conn, run_id, "APPROVAL_REQUESTED", {"approval_request_id": request_id, "action_id": action_id})
        return self.get(request_id)

    def get(self, request_id: str) -> Dict[str, Any]:
        with self.run_store.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM h_action_approval_requests WHERE approval_request_id = ?", (request_id,)
            ).fetchone()
        if not row:
            raise KeyError(f"Unknown approval request: {request_id}")
        return dict(row)

    def show(self, request_id: str) -> Dict[str, Any]:
        row = self.get(request_id)
        artifact = self.artifact_store.get_artifact_by_id(row["consequence_artifact_id"])
        import json

        relative = artifact["relative_path"].split("/artifacts/", 1)[-1]
        request = json.loads(self.artifact_store.open_readonly(row["run_id"], relative))
        return {"request": request, "state": row["state"], "used_count": row["used_count"], "binding_sha256": row["binding_sha256"]}

    def for_action(self, action_id: str) -> Optional[Dict[str, Any]]:
        with self.run_store.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM h_action_approval_requests WHERE action_id = ?", (action_id,)
            ).fetchone()
        return dict(row) if row else None

    def approve(self, request_id: str, approved_by: str = "user_interactive") -> Dict[str, Any]:
        now = _now()
        with self.run_store.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM h_action_approval_requests WHERE approval_request_id = ?", (request_id,)
                ).fetchone()
                if not row:
                    raise ApprovalError(f"Unknown approval request: {request_id}")
                if row["state"] != "PENDING":
                    raise ApprovalError(f"Approval request is {row['state']}, not PENDING")
                if datetime.datetime.fromisoformat(row["expires_at"]) <= now:
                    conn.execute(
                        "UPDATE h_action_approval_requests SET state = 'EXPIRED', updated_at = ? WHERE approval_request_id = ?",
                        (now.isoformat(), request_id),
                    )
                    conn.commit()
                    raise ApprovalError("Approval request has expired")
                conn.execute(
                    "UPDATE h_action_approval_requests SET state = 'APPROVED', updated_at = ? WHERE approval_request_id = ?",
                    (now.isoformat(), request_id),
                )
                conn.execute(
                    """
                    INSERT INTO h_action_approval_grants(
                        grant_id, approval_request_id, binding_sha256, approved_by, state, approved_at
                    ) VALUES (?, ?, ?, ?, 'ACTIVE', ?)
                    """,
                    (f"grant_{uuid.uuid4().hex[:16]}", request_id, row["binding_sha256"], approved_by, now.isoformat()),
                )
                self._event(conn, row["run_id"], "APPROVAL_GRANTED", {"approval_request_id": request_id})
                conn.commit()
            except ApprovalError:
                if conn.in_transaction:
                    conn.rollback()
                raise
            except BaseException:
                conn.rollback()
                raise
        return self.get(request_id)

    def deny(self, request_id: str, reason: str) -> Dict[str, Any]:
        now = _now().isoformat()
        with self.run_store.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM h_action_approval_requests WHERE approval_request_id = ?", (request_id,)
            ).fetchone()
            if not row or row["state"] not in ("PENDING", "APPROVED"):
                conn.rollback()
                raise ApprovalError("Only a pending or unconsumed approval can be denied")
            conn.execute(
                "UPDATE h_action_approval_requests SET state = 'DENIED', denial_reason = ?, updated_at = ? WHERE approval_request_id = ?",
                (reason[:500], now, request_id),
            )
            conn.execute(
                "UPDATE h_action_approval_grants SET state = 'REVOKED' WHERE approval_request_id = ? AND state = 'ACTIVE'",
                (request_id,),
            )
            self._event(conn, row["run_id"], "APPROVAL_DENIED", {"approval_request_id": request_id, "reason": reason[:500]})
            conn.commit()
        return self.get(request_id)

    def revoke(self, request_id: str) -> None:
        with self.run_store.get_connection() as conn:
            with conn:
                conn.execute(
                    "UPDATE h_action_approval_requests SET state = 'REVOKED', updated_at = ? WHERE approval_request_id = ? AND state IN ('PENDING', 'APPROVED')",
                    (_now().isoformat(), request_id),
                )
                conn.execute(
                    "UPDATE h_action_approval_grants SET state = 'REVOKED' WHERE approval_request_id = ? AND state = 'ACTIVE'",
                    (request_id,),
                )

    def expire(self) -> int:
        now = _now().isoformat()
        with self.run_store.get_connection() as conn:
            with conn:
                changed = conn.execute(
                    "UPDATE h_action_approval_requests SET state = 'EXPIRED', updated_at = ? WHERE state IN ('PENDING', 'APPROVED') AND expires_at <= ?",
                    (now, now),
                ).rowcount
                conn.execute(
                    """
                    UPDATE h_action_approval_grants SET state = 'EXPIRED'
                    WHERE state = 'ACTIVE' AND approval_request_id IN (
                        SELECT approval_request_id FROM h_action_approval_requests WHERE state = 'EXPIRED'
                    )
                    """
                )
        return changed

    def active_binding(self, action_id: str) -> Optional[str]:
        """Binding hash of a live, unexpired, unconsumed grant for ``action_id``."""
        now = _now()
        with self.run_store.get_connection() as conn:
            row = conn.execute(
                """
                SELECT r.*, g.state AS grant_state, g.binding_sha256 AS grant_binding
                FROM h_action_approval_requests r
                JOIN h_action_approval_grants g ON g.approval_request_id = r.approval_request_id
                WHERE r.action_id = ? AND r.state = 'APPROVED' AND g.state = 'ACTIVE'
                """,
                (action_id,),
            ).fetchone()
        if not row or datetime.datetime.fromisoformat(row["expires_at"]) <= now:
            return None
        if row["used_count"] >= row["max_uses"]:
            return None
        return row["grant_binding"]

    @staticmethod
    def consume_sql(conn: sqlite3.Connection, action_id: str, binding_sha256: str) -> str:
        """Consume a grant inside the caller's intent transaction. Returns the request ID."""
        now = _now()
        row = conn.execute(
            """
            SELECT r.*, g.grant_id, g.state AS grant_state, g.binding_sha256 AS grant_binding
            FROM h_action_approval_requests r
            JOIN h_action_approval_grants g ON g.approval_request_id = r.approval_request_id
            WHERE r.action_id = ?
            """,
            (action_id,),
        ).fetchone()
        if not row or row["state"] != "APPROVED" or row["grant_state"] != "ACTIVE":
            raise ApprovalError("No active grant exists for this action")
        if datetime.datetime.fromisoformat(row["expires_at"]) <= now:
            raise ApprovalError("Grant expired before consumption")
        if row["grant_binding"] != binding_sha256 or row["binding_sha256"] != binding_sha256:
            raise ApprovalError("Grant binding differs from the action being executed")
        if row["used_count"] >= row["max_uses"]:
            raise ApprovalError("Grant use count is exhausted")
        used = row["used_count"] + 1
        conn.execute(
            "UPDATE h_action_approval_requests SET used_count = ?, state = ?, updated_at = ? WHERE approval_request_id = ?",
            (used, "CONSUMED" if used >= row["max_uses"] else "APPROVED", now.isoformat(), row["approval_request_id"]),
        )
        if used >= row["max_uses"]:
            conn.execute(
                "UPDATE h_action_approval_grants SET state = 'CONSUMED', consumed_at = ? WHERE grant_id = ?",
                (now.isoformat(), row["grant_id"]),
            )
        ApprovalService._event(conn, row["run_id"], "APPROVAL_CONSUMED", {"approval_request_id": row["approval_request_id"], "action_id": action_id})
        return row["approval_request_id"]

    def pending(self, run_id: str) -> List[Dict[str, Any]]:
        with self.run_store.get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM h_action_approval_requests WHERE run_id = ? AND state IN ('PENDING', 'APPROVED') ORDER BY created_at",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _event(conn: sqlite3.Connection, run_id: str, event_type: str, payload: Dict[str, Any]) -> None:
        from harness.persistence.events import append_event_sql

        append_event_sql(conn, run_id, event_type, payload, "PRD3", "PRD3", _now().isoformat())
