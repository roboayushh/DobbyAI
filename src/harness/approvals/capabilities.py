"""PRD 6 capability requests, approval grants, consumption, and effect journaling (section 9).

Authority comes only from a ``KernelPrincipal`` minted by the CLI (interactive
terminal), a user-owned headless pregrant, or deployment admin policy. Repository
text, issue text, model output, generated scripts, subprocess output, test
reports, and plugins cannot create, approve, expand, or consume a grant: they have
no principal, and forged "approved" JSON has no grant ID bound to a request hash.
Every grant binds the exact request hash; changing any bound value invalidates it.
"""
from __future__ import annotations

import datetime
import hashlib
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from harness.contracts.release import ApprovalGrantV1, CapabilityRequestV1
from harness.persistence import canonical_json
from harness.persistence.events import append_event_sql

_KERNEL = object()
P1_OPERATIONS = {"APPLY_LOCAL", "PUSH_NEW_BRANCH", "CREATE_PULL_REQUEST", "MERGE_TARGET", "DELETE_REMOTE_BRANCH"}


class CapabilityError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class KernelPrincipal:
    principal_id: str
    channel: str  # INTERACTIVE_TERMINAL | HEADLESS_PREGRANT | ADMIN_POLICY
    _token: object = None

    def __post_init__(self) -> None:
        if self._token is not _KERNEL:
            raise CapabilityError("PRINCIPAL_FORGED", "Principals are minted only by the trusted kernel entry points")

    @classmethod
    def interactive(cls, name: str = "user_cli") -> "KernelPrincipal":
        return cls(f"{name}", "INTERACTIVE_TERMINAL", _KERNEL)

    @classmethod
    def headless(cls, name: str = "user_headless_request") -> "KernelPrincipal":
        return cls(f"{name}", "HEADLESS_PREGRANT", _KERNEL)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _sha(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class CapabilityService:
    def __init__(self, run_store, artifact_store, *, p1_enabled: bool = False) -> None:
        self.run_store = run_store
        self.artifact_store = artifact_store
        self.p1_enabled = p1_enabled

    # ------------------------------------------------------------ requests
    def create(self, run_id: str, operation: str, *, principal: KernelPrincipal, target: Dict[str, Any],
               summary: str, candidate: Optional[Dict[str, Any]] = None, artifact_sha256: Optional[str] = None,
               permission_profile: str = "sandbox", uses: int = 1, ttl_seconds: int = 3600,
               export_id: Optional[str] = None, candidate_id: Optional[str] = None) -> CapabilityRequestV1:
        if not isinstance(principal, KernelPrincipal):
            raise CapabilityError("PRINCIPAL_REQUIRED", "Only a trusted kernel principal may create a capability request")
        if operation in P1_OPERATIONS and not self.p1_enabled:
            raise CapabilityError("CAPABILITY_DISABLED", f"{operation} is a P1 effect that is disabled in this release profile")
        with self.run_store.get_connection() as conn:
            policy = conn.execute("SELECT policy_id, policy_sha256 FROM h_policy_snapshots WHERE run_id = ? AND revoked = 0 ORDER BY version DESC LIMIT 1",
                                  (run_id,)).fetchone()
        policy_view = {"policy_id": policy["policy_id"] if policy else "policy_release_v1",
                       "policy_sha256": policy["policy_sha256"] if policy else _sha({"release_policy": "v1"}),
                       "permission_profile": permission_profile}
        request_id = f"capreq_{uuid.uuid4().hex[:16]}"
        expires = (_now() + datetime.timedelta(seconds=ttl_seconds)).isoformat()
        core = {"schema_version": "1.0", "capability_request_id": request_id, "run_id": run_id, "operation": operation,
                "candidate": candidate, "artifact_sha256": artifact_sha256, "target": target, "policy": policy_view,
                "requested_uses": uses, "expires_at": expires, "summary_artifact_id": None}
        bound = {k: v for k, v in core.items() if k not in ("capability_request_id", "summary_artifact_id", "expires_at")}
        base = f"prd6/capabilities/{request_id}"
        self.artifact_store.write_json(run_id, f"{base}/summary.json", {"operation": operation, "summary": summary[:2000], "target": target},
                                       "approval_summary")
        summary_artifact = self.artifact_store.get_artifact_by_path(run_id, f"{base}/summary.json")
        core["summary_artifact_id"] = summary_artifact["artifact_id"]
        request = CapabilityRequestV1(**core, request_sha256=_sha(bound))
        self.artifact_store.write_json(run_id, f"{base}/target.json", target, "capability_request")
        target_artifact = self.artifact_store.get_artifact_by_path(run_id, f"{base}/target.json")
        self.artifact_store.write_json(run_id, f"{base}/request.json", request.model_dump(mode="json"), "capability_request")
        request_artifact = self.artifact_store.get_artifact_by_path(run_id, f"{base}/request.json")
        created_by = {"INTERACTIVE_TERMINAL": "USER_INTERACTIVE", "HEADLESS_PREGRANT": "USER_HEADLESS_REQUEST"}.get(principal.channel, "ADMIN_POLICY")
        now = _now().isoformat()
        with self.run_store.get_connection() as conn:
            with conn:
                conn.execute(
                    "INSERT INTO h_capability_requests VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)",
                    (request_id, run_id, operation, candidate_id, export_id, created_by, target_artifact["artifact_id"], _sha(target),
                     policy_view["policy_id"], policy_view["policy_sha256"], permission_profile, uses, expires,
                     summary_artifact["artifact_id"], request_artifact["artifact_id"], request.request_sha256, now, now),
                )
                append_event_sql(conn, run_id, "CAPABILITY_REQUEST_CREATED", {"capability_request_id": request_id, "operation": operation},
                                 "PRD6", "PRD6", now)
        return request

    def get(self, request_id: str) -> Dict[str, Any]:
        with self.run_store.get_connection() as conn:
            row = conn.execute("SELECT * FROM h_capability_requests WHERE capability_request_id = ?", (request_id,)).fetchone()
        if not row:
            raise CapabilityError("UNKNOWN_CAPABILITY_REQUEST", f"Unknown capability request {request_id}")
        return dict(row)

    def show(self, request_id: str) -> Dict[str, Any]:
        row = self.get(request_id)
        request = self._load_json(row["request_artifact_id"])
        summary = self._load_json(row["summary_artifact_id"])
        return {"request": request, "summary": summary, "state": row["state"], "expires_at": row["expires_at"]}

    def pending(self, run_id: str) -> List[Dict[str, Any]]:
        self.expire()
        with self.run_store.get_connection() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM h_capability_requests WHERE run_id = ? AND state = 'PENDING' ORDER BY created_at",
                                                  (run_id,)).fetchall()]

    def expire(self) -> int:
        now = _now().isoformat()
        with self.run_store.get_connection() as conn:
            with conn:
                changed = conn.execute("UPDATE h_capability_requests SET state = 'EXPIRED', updated_at = ? WHERE state IN ('PENDING', 'APPROVED') AND expires_at <= ?",
                                       (now, now)).rowcount
                conn.execute("UPDATE h_approval_grants SET state = 'EXPIRED' WHERE state = 'ACTIVE' AND expires_at <= ?", (now,))
        return changed

    # --------------------------------------------------------------- grants
    def grant(self, request_id: str, principal: KernelPrincipal) -> ApprovalGrantV1:
        if not isinstance(principal, KernelPrincipal):
            raise CapabilityError("PRINCIPAL_REQUIRED", "Only a trusted kernel principal may grant approval")
        self.expire()
        row = self.get(request_id)
        if row["state"] != "PENDING":
            raise CapabilityError("CAPABILITY_REQUEST_NOT_PENDING", f"Request is {row['state']}")
        grant_id = f"grant_{uuid.uuid4().hex[:16]}"
        now = _now().isoformat()
        core = {"schema_version": "1.0", "approval_grant_id": grant_id, "capability_request_id": request_id,
                "principal": {"principal_id": principal.principal_id, "channel": principal.channel.lower()},
                "bound_request_sha256": row["request_sha256"], "credential_scope_id": None,
                "max_uses": row["requested_uses"], "remaining_uses": row["requested_uses"], "state": "ACTIVE",
                "created_at": now, "expires_at": row["expires_at"]}
        grant = ApprovalGrantV1(**core, grant_sha256=_sha(core))
        path = f"prd6/capabilities/{request_id}/grant-{grant_id}.json"
        self.artifact_store.write_json(row["run_id"], path, grant.model_dump(mode="json"), "approval_grant")
        artifact = self.artifact_store.get_artifact_by_path(row["run_id"], path)
        with self.run_store.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                current = conn.execute("SELECT state FROM h_capability_requests WHERE capability_request_id = ?", (request_id,)).fetchone()
                if current["state"] != "PENDING":
                    raise CapabilityError("CAPABILITY_REQUEST_NOT_PENDING", "Request changed state concurrently")
                conn.execute("INSERT INTO h_approval_grants VALUES (?, ?, ?, ?, ?, NULL, ?, ?, 'ACTIVE', ?, ?, ?, ?, NULL)",
                             (grant_id, request_id, principal.principal_id, principal.channel, row["request_sha256"],
                              grant.max_uses, grant.remaining_uses, artifact["artifact_id"], grant.grant_sha256, now, row["expires_at"]))
                conn.execute("UPDATE h_capability_requests SET state = 'APPROVED', updated_at = ? WHERE capability_request_id = ?", (now, request_id))
                append_event_sql(conn, row["run_id"], "APPROVAL_GRANTED", {"capability_request_id": request_id, "grant_id": grant_id,
                                                                            "channel": principal.channel}, "PRD6", "PRD6", now)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return grant

    def deny(self, request_id: str, principal: KernelPrincipal, reason: str) -> None:
        if not isinstance(principal, KernelPrincipal):
            raise CapabilityError("PRINCIPAL_REQUIRED", "Only a trusted kernel principal may deny a request")
        row = self.get(request_id)
        now = _now().isoformat()
        with self.run_store.get_connection() as conn:
            with conn:
                conn.execute("UPDATE h_capability_requests SET state = 'DENIED', updated_at = ? WHERE capability_request_id = ? AND state IN ('PENDING', 'APPROVED')",
                             (now, request_id))
                conn.execute("UPDATE h_approval_grants SET state = 'REVOKED', revoked_at = ? WHERE capability_request_id = ? AND state = 'ACTIVE'", (now, request_id))
                append_event_sql(conn, row["run_id"], "CAPABILITY_REQUEST_DENIED", {"capability_request_id": request_id, "reason": reason[:300]},
                                 "PRD6", "PRD6", now)

    def active_grant(self, request_id: str, current_request_sha256: str) -> Dict[str, Any]:
        """Re-check immediately before dispatch: active, unexpired, uses left, and bound to the current request."""
        self.expire()
        with self.run_store.get_connection() as conn:
            grant = conn.execute("SELECT * FROM h_approval_grants WHERE capability_request_id = ? AND state = 'ACTIVE'", (request_id,)).fetchone()
        if grant is None:
            raise CapabilityError("APPROVAL_MISSING", "No active grant for this request")
        if grant["bound_request_sha256"] != current_request_sha256:
            with self.run_store.get_connection() as conn:
                with conn:
                    conn.execute("UPDATE h_approval_grants SET state = 'INVALIDATED' WHERE approval_grant_id = ?", (grant["approval_grant_id"],))
            raise CapabilityError("APPROVAL_BINDING_CHANGED", "A bound value changed since approval; the grant is invalidated")
        if grant["remaining_uses"] < 1:
            raise CapabilityError("APPROVAL_EXHAUSTED", "Grant has no remaining uses")
        return dict(grant)

    def consume_sql(self, conn, grant: Dict[str, Any]) -> str:
        """Decrement and reserve one use inside the caller's effect-intent transaction."""
        changed = conn.execute(
            "UPDATE h_approval_grants SET remaining_uses = remaining_uses - 1, state = CASE WHEN remaining_uses - 1 = 0 THEN 'CONSUMED' ELSE state END "
            "WHERE approval_grant_id = ? AND state = 'ACTIVE' AND remaining_uses >= 1", (grant["approval_grant_id"],),
        ).rowcount
        if changed != 1:
            raise CapabilityError("APPROVAL_EXHAUSTED", "Grant was consumed concurrently")
        use_number = conn.execute("SELECT COUNT(*) + 1 FROM h_approval_consumptions WHERE approval_grant_id = ?", (grant["approval_grant_id"],)).fetchone()[0]
        consumption_id = f"guse_{uuid.uuid4().hex[:16]}"
        now = _now().isoformat()
        conn.execute("INSERT INTO h_approval_consumptions VALUES (?, ?, ?, ?, ?, 'RESERVED', ?, NULL, NULL)",
                     (consumption_id, grant["approval_grant_id"], grant["capability_request_id"], use_number,
                      grant["bound_request_sha256"], now))
        conn.execute("UPDATE h_capability_requests SET state = CASE WHEN ? = 1 THEN 'CONSUMED' ELSE state END, updated_at = ? WHERE capability_request_id = ?",
                     (1 if grant["remaining_uses"] - 1 == 0 else 0, now, grant["capability_request_id"]))
        return consumption_id

    def _load_json(self, artifact_id: Optional[str]) -> Any:
        import json

        if not artifact_id:
            return None
        artifact = self.artifact_store.get_artifact_by_id(artifact_id)
        return json.loads((self.artifact_store.data_root / artifact["relative_path"]).read_text(encoding="utf-8"))
