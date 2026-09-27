"""Host-owned policy snapshots and deterministic action admission.

The policy engine is pure with respect to its inputs: it evaluates an exact
admission request against an immutable policy snapshot and host-observed
facts. It never executes anything and cannot be influenced by model text
beyond the structured request fields it validates.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional, Sequence, Tuple

from harness.contracts.execution import (
    ActionAdmissionRequestV1,
    EffectiveLimitsV1,
    NetworkMode,
    NetworkPolicyV1,
    PermissionProfile,
    PolicyDecisionKind,
    PolicyDecisionV1,
    PolicySnapshotV1,
)
from harness.persistence import RunStore, canonical_json
from harness.policy.capabilities import DEFAULT_REGISTRY, CapabilityRegistry
from harness.policy.limits import SandboxLimits

WHOLE_WORKSPACE = "."
RESERVED_PREFIXES = (".git/", ".harness/")


def _sha(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class PolicyError(RuntimeError):
    code = "POLICY_ERROR"


def normalize_declared_path(path: str) -> str:
    """Canonical repository-relative form; directory prefixes keep a trailing slash."""
    if not isinstance(path, str) or not path or "\x00" in path or len(path) > 1024:
        raise PolicyError(f"Unsafe declared path: {path!r}")
    value = path.replace("\\", "/")
    if value.startswith("/") or (len(value) > 1 and value[1] == ":"):
        raise PolicyError(f"Absolute declared path: {path}")
    trailing = value.endswith("/")
    parts = [part for part in value.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise PolicyError(f"Traversal in declared path: {path}")
    if not parts:
        return WHOLE_WORKSPACE
    if any(part.lower() == ".git" for part in parts) or parts[0] == ".harness":
        raise PolicyError(f"Declared path targets reserved harness metadata: {path}")
    normalized = "/".join(parts)
    return normalized + "/" if trailing else normalized


def path_within(path: str, scopes: Sequence[str]) -> bool:
    """True when concrete ``path`` is covered by any declared/scoped entry."""
    for scope in scopes:
        if scope == WHOLE_WORKSPACE:
            return True
        if scope.endswith("/"):
            if path.startswith(scope):
                return True
        elif path == scope or path.startswith(scope + "/"):
            return True
    return False


@dataclass(frozen=True)
class PolicySnapshot:
    contract: PolicySnapshotV1

    @property
    def core(self) -> Dict[str, Any]:
        data = self.contract.model_dump(mode="json")
        data.pop("policy_sha256")
        return data

    def model_visible(self) -> Dict[str, Any]:
        """The exact host authorization policy pinned in model context packets."""
        return self.core


@dataclass(frozen=True)
class AdmissionFacts:
    """Host-observed facts the admission request must match exactly."""

    proposal_state: str
    task_revision: int
    lifecycle_state: str
    lifecycle_version: int
    active_plan_id: Optional[str]
    active_plan_revision: Optional[int]
    active_workspace_version_id: str
    active_workspace_commit: str
    code_bytes_sha256: Optional[str]
    runtime_profile_id: str
    runtime_profile_fingerprint: str
    remaining_actions: int
    remaining_wall_seconds: int
    approved_binding_sha256: Optional[str] = None


class PolicyEngine:
    def __init__(
        self,
        run_store: RunStore,
        *,
        registry: CapabilityRegistry = DEFAULT_REGISTRY,
        limits: SandboxLimits = SandboxLimits(),
    ) -> None:
        self.run_store = run_store
        self.registry = registry
        self.limits = limits

    # ------------------------------------------------------------ snapshots
    def default_allowed(self, profile: PermissionProfile) -> List[str]:
        names = [
            "source.search",
            "source.read",
            "source.symbols",
            "context.artifact.read",
            "action.result.emit",
            "workspace.patch",
            "sandbox.command.argv",
        ]
        if profile in (PermissionProfile.SANDBOX, PermissionProfile.DELEGATED):
            names.append("sandbox.command.shell")
        return sorted(names)

    def ensure_snapshot(
        self,
        run_id: str,
        *,
        profile: PermissionProfile = PermissionProfile.SANDBOX,
        runtime_profile_id: str = "python-default",
        path_scopes: Optional[Sequence[str]] = None,
        allowed_capabilities: Optional[Sequence[str]] = None,
    ) -> PolicySnapshot:
        existing = self.active_snapshot(run_id)
        if existing is not None:
            return existing
        return self.create_snapshot(
            run_id,
            profile=profile,
            runtime_profile_id=runtime_profile_id,
            path_scopes=path_scopes,
            allowed_capabilities=allowed_capabilities,
        )

    def create_snapshot(
        self,
        run_id: str,
        *,
        profile: PermissionProfile,
        runtime_profile_id: str,
        path_scopes: Optional[Sequence[str]] = None,
        allowed_capabilities: Optional[Sequence[str]] = None,
        expires_at: Optional[str] = None,
    ) -> PolicySnapshot:
        """Snapshots are immutable; any change creates a new version."""
        scopes = [normalize_declared_path(scope) for scope in (path_scopes or [WHOLE_WORKSPACE])]
        allowed = sorted(set(allowed_capabilities or self.default_allowed(profile)))
        for name in allowed:
            self.registry.get(name)
        with self.run_store.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                version = conn.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 FROM h_policy_snapshots WHERE run_id = ?", (run_id,)
                ).fetchone()[0]
                policy_id = f"pol_{uuid.uuid4().hex[:16]}"
                core = {
                    "schema_version": "1.0",
                    "policy_id": policy_id,
                    "run_id": run_id,
                    "version": int(version),
                    "profile": profile.value,
                    "allowed_capabilities": allowed,
                    "path_scopes": sorted(set(scopes)),
                    "network": {"mode": NetworkMode.NONE.value, "destinations": []},
                    "runtime_profile_id": runtime_profile_id,
                    "limits_fingerprint": self.limits.fingerprint(),
                    "revoked": False,
                    "expires_at": expires_at,
                }
                contract = PolicySnapshotV1(**core, policy_sha256=_sha(core))
                conn.execute(
                    "UPDATE h_policy_snapshots SET revoked = 1 WHERE run_id = ? AND revoked = 0", (run_id,)
                )
                conn.execute(
                    """
                    INSERT INTO h_policy_snapshots(
                        policy_id, run_id, version, profile, policy_json, policy_sha256,
                        revoked, expires_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        policy_id, run_id, int(version), profile.value,
                        canonical_json(contract.model_dump(mode="json")), contract.policy_sha256,
                        expires_at, _now().isoformat(),
                    ),
                )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return PolicySnapshot(contract)

    def active_snapshot(self, run_id: str) -> Optional[PolicySnapshot]:
        with self.run_store.get_connection() as conn:
            row = conn.execute(
                "SELECT policy_json FROM h_policy_snapshots WHERE run_id = ? AND revoked = 0 ORDER BY version DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        if not row:
            return None
        contract = PolicySnapshotV1.model_validate_json(row["policy_json"])
        core = contract.model_dump(mode="json")
        core.pop("policy_sha256")
        if _sha(core) != contract.policy_sha256:
            raise PolicyError("Stored policy snapshot hash does not match its content")
        return PolicySnapshot(contract)

    def get_snapshot(self, policy_id: str) -> PolicySnapshot:
        with self.run_store.get_connection() as conn:
            row = conn.execute("SELECT policy_json FROM h_policy_snapshots WHERE policy_id = ?", (policy_id,)).fetchone()
        if not row:
            raise KeyError(policy_id)
        return PolicySnapshot(PolicySnapshotV1.model_validate_json(row["policy_json"]))

    def revoke(self, run_id: str) -> None:
        with self.run_store.get_connection() as conn:
            with conn:
                conn.execute("UPDATE h_policy_snapshots SET revoked = 1 WHERE run_id = ?", (run_id,))

    # ------------------------------------------------------------ decisions
    def effective_limits(self, requested_timeout: int) -> EffectiveLimitsV1:
        wall = max(1, min(int(requested_timeout), self.limits.max_action_seconds))
        return EffectiveLimitsV1(
            wall_seconds=wall,
            cpus=self.limits.cpus,
            memory_bytes=self.limits.memory_bytes,
            pids=self.limits.pids,
            stdout_bytes=self.limits.stdout_bytes,
            stderr_bytes=self.limits.stderr_bytes,
            workspace_growth_bytes=self.limits.workspace_growth_bytes,
            new_files=self.limits.new_files,
        )

    def evaluate(
        self,
        request: ActionAdmissionRequestV1,
        snapshot: PolicySnapshot,
        facts: AdmissionFacts,
    ) -> Tuple[PolicyDecisionV1, Dict[str, Any]]:
        """Return the decision and the exact approval binding it implies."""
        policy = snapshot.contract
        stale: List[str] = []
        denied: List[str] = []
        if facts.proposal_state != "UNEXECUTED":
            stale.append(f"PROPOSAL_STATE_{facts.proposal_state}")
        if request.task_revision != facts.task_revision:
            stale.append("TASK_REVISION_CHANGED")
        if facts.lifecycle_state not in ("ACTION_PROPOSED", "NEEDS_APPROVAL", "ACTION_EXECUTING") or (
            request.lifecycle_version > facts.lifecycle_version
        ):
            stale.append("LIFECYCLE_CHANGED")
        if request.plan_id != facts.active_plan_id or request.plan_revision != facts.active_plan_revision:
            stale.append("PLAN_CHANGED")
        if request.workspace_version != facts.active_workspace_commit:
            stale.append("WORKSPACE_VERSION_CHANGED")
        if facts.code_bytes_sha256 is None or facts.code_bytes_sha256 != request.code_sha256:
            stale.append("CODE_HASH_MISMATCH")
        if request.policy_sha256 != policy.policy_sha256:
            stale.append("POLICY_CHANGED")
        if request.runtime_profile_id != facts.runtime_profile_id or policy.runtime_profile_id != facts.runtime_profile_id:
            stale.append("RUNTIME_PROFILE_CHANGED")
        if policy.revoked:
            stale.append("POLICY_REVOKED")
        if policy.expires_at and datetime.datetime.fromisoformat(policy.expires_at) <= _now():
            stale.append("POLICY_EXPIRED")

        denied.extend(self.registry.validate_request(request.requested_capabilities, policy.profile.value))
        capabilities, _unknown = self.registry.normalize(request.requested_capabilities)
        not_allowed = sorted(set(capabilities) - set(policy.allowed_capabilities))
        denied.extend(f"CAPABILITY_NOT_IN_POLICY:{name}" for name in not_allowed)

        paths: List[str] = []
        for raw in request.declared_paths:
            try:
                normalized = normalize_declared_path(raw)
            except PolicyError:
                denied.append("DECLARED_PATH_UNSAFE")
                continue
            if not path_within(normalized.rstrip("/") or WHOLE_WORKSPACE, policy.path_scopes) and not (
                normalized == WHOLE_WORKSPACE and WHOLE_WORKSPACE in policy.path_scopes
            ):
                denied.append("DECLARED_PATH_OUTSIDE_SCOPE")
            paths.append(normalized)
        paths = sorted(set(paths))

        if policy.network.mode != NetworkMode.NONE:
            denied.append("NETWORK_POLICY_UNAVAILABLE")

        limits = self.effective_limits(request.requested_timeout_seconds)
        if facts.remaining_actions < 1:
            denied.append("EXECUTION_BUDGET_ACTIONS_EXHAUSTED")
        if facts.remaining_wall_seconds < limits.wall_seconds:
            if facts.remaining_wall_seconds >= 10:
                limits = limits.model_copy(update={"wall_seconds": facts.remaining_wall_seconds})
            else:
                denied.append("EXECUTION_BUDGET_WALL_EXHAUSTED")

        mutating = self.registry.mutating(capabilities)
        binding = self.approval_binding(request, snapshot, facts, capabilities, paths, limits)
        binding_sha = _sha(binding)

        if stale:
            kind, reasons = PolicyDecisionKind.STALE, stale
        elif denied:
            kind, reasons = PolicyDecisionKind.DENIED, sorted(set(denied))
        elif mutating and policy.profile == PermissionProfile.GUIDED:
            if facts.approved_binding_sha256 == binding_sha:
                kind, reasons = PolicyDecisionKind.ADMITTED, ["APPROVED_ONE_USE_GRANT"]
            else:
                kind, reasons = PolicyDecisionKind.NEEDS_APPROVAL, ["GUIDED_MUTATION_REQUIRES_APPROVAL"]
        elif mutating and policy.profile == PermissionProfile.DELEGATED and (
            WHOLE_WORKSPACE in policy.path_scopes or not paths
        ):
            # Delegation authorizes only an explicit private-workspace scope.
            if facts.approved_binding_sha256 == binding_sha:
                kind, reasons = PolicyDecisionKind.ADMITTED, ["APPROVED_ONE_USE_GRANT"]
            else:
                kind, reasons = PolicyDecisionKind.NEEDS_APPROVAL, ["DELEGATED_SCOPE_NOT_EXPLICIT"]
        else:
            kind, reasons = PolicyDecisionKind.ADMITTED, []

        decision_core = {
            "schema_version": "1.0",
            "decision_id": f"pdec_{uuid.uuid4().hex[:16]}",
            "request_id": request.request_id,
            "decision": kind.value,
            "reason_codes": reasons,
            "normalized_capabilities": capabilities,
            "normalized_paths": paths,
            "effective_limits": limits.model_dump(mode="json"),
            "network_mode": NetworkMode.NONE.value,
            "approval_request_id": None,
        }
        decision = PolicyDecisionV1(**decision_core, decision_sha256=_sha({**decision_core, "binding_sha256": binding_sha}))
        return decision, binding

    def approval_binding(
        self,
        request: ActionAdmissionRequestV1,
        snapshot: PolicySnapshot,
        facts: AdmissionFacts,
        capabilities: Sequence[str],
        paths: Sequence[str],
        limits: EffectiveLimitsV1,
    ) -> Dict[str, Any]:
        """Every value a one-use grant is bound to (PRD 3 section 6.4)."""
        return {
            "run_id": request.run_id,
            "task_id": request.task_id,
            "action_proposal_id": request.action_proposal_id,
            "code_sha256": request.code_sha256,
            "plan_id": request.plan_id,
            "plan_revision": request.plan_revision,
            "workspace_version": request.workspace_version,
            "capabilities": sorted(capabilities),
            "paths": sorted(paths),
            "runtime_profile_fingerprint": facts.runtime_profile_fingerprint,
            "network_policy_fingerprint": _sha(snapshot.contract.network.model_dump(mode="json")),
            "limits_fingerprint": _sha(limits.model_dump(mode="json")),
            "policy_version": snapshot.contract.version,
            "policy_sha256": snapshot.contract.policy_sha256,
        }

    @staticmethod
    def binding_sha(binding: Dict[str, Any]) -> str:
        return _sha(binding)
