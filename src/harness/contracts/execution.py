"""Versioned contracts for PRD 3: policy admission, sandbox execution, and settlement.

All contracts are strict (``extra="forbid"``) and bounded. Identifier fields are
free-form bounded strings because the PRD examples use illustrative IDs; content
hashes are always 64-character lowercase SHA-256 strings. Git object IDs are
validated at runtime by the private Git adapter, not by these schemas, so that
SHA-1 and SHA-256 repositories are both representable.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SHA256 = r"^[0-9a-f]{64}$"
ID = Field(min_length=1, max_length=128)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())


def _safe_relative(path: str) -> str:
    if not path or len(path) > 1024 or "\x00" in path:
        raise ValueError(f"unsafe relative path: {path!r}")
    normalized = path.replace("\\", "/")
    if normalized.startswith("/") or (len(normalized) > 1 and normalized[1] == ":"):
        raise ValueError(f"absolute path is not allowed: {path}")
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ValueError(f"path traversal is not allowed: {path}")
    return normalized


class PermissionProfile(str, Enum):
    GUIDED = "guided"
    SANDBOX = "sandbox"
    DELEGATED = "delegated"


class NetworkMode(str, Enum):
    NONE = "none"
    SETUP_SCOPED = "setup_scoped"
    ACTION_SCOPED = "action_scoped"


class NetworkPolicyV1(_Strict):
    mode: NetworkMode = NetworkMode.NONE
    destinations: List[str] = Field(default_factory=list, max_length=32)


class PolicySnapshotV1(_Strict):
    schema_version: Literal["1.0"] = "1.0"
    policy_id: str = ID
    run_id: str = ID
    version: int = Field(ge=1)
    profile: PermissionProfile
    allowed_capabilities: List[str] = Field(max_length=32)
    path_scopes: List[str] = Field(max_length=64)
    network: NetworkPolicyV1
    runtime_profile_id: str = ID
    limits_fingerprint: str = Field(pattern=SHA256)
    revoked: bool = False
    expires_at: Optional[str] = None
    policy_sha256: str = Field(pattern=SHA256)


class ActionAdmissionRequestV1(_Strict):
    schema_version: Literal["1.0"] = "1.0"
    request_id: str = ID
    action_proposal_id: str = ID
    run_id: str = ID
    task_id: str = ID
    task_revision: int = Field(ge=1)
    lifecycle_version: int = Field(ge=1)
    plan_id: str = ID
    plan_revision: int = Field(ge=1)
    workspace_version: str = ID
    code_artifact_id: str = ID
    code_sha256: str = Field(pattern=SHA256)
    requested_capabilities: List[str] = Field(max_length=32)
    declared_paths: List[str] = Field(max_length=256)
    requested_timeout_seconds: int = Field(gt=0, le=3600)
    policy_sha256: str = Field(pattern=SHA256)
    runtime_profile_id: str = ID

    @field_validator("declared_paths")
    @classmethod
    def _paths(cls, values: List[str]) -> List[str]:
        return [_safe_relative(value) for value in values]


class EffectiveLimitsV1(_Strict):
    wall_seconds: int = Field(gt=0, le=1200)
    cpus: float = Field(gt=0, le=4)
    memory_bytes: int = Field(gt=0, le=8 * 1024**3)
    pids: int = Field(gt=0, le=512)
    stdout_bytes: int = Field(gt=0, le=10 * 1024**2)
    stderr_bytes: int = Field(gt=0, le=10 * 1024**2)
    workspace_growth_bytes: int = Field(ge=0, le=2 * 1024**3)
    new_files: int = Field(ge=0, le=25_000)


class PolicyDecisionKind(str, Enum):
    ADMITTED = "ADMITTED"
    NEEDS_APPROVAL = "NEEDS_APPROVAL"
    DENIED = "DENIED"
    STALE = "STALE"


class PolicyDecisionV1(_Strict):
    schema_version: Literal["1.0"] = "1.0"
    decision_id: str = ID
    request_id: str = ID
    decision: PolicyDecisionKind
    reason_codes: List[str] = Field(default_factory=list, max_length=32)
    normalized_capabilities: List[str] = Field(max_length=32)
    normalized_paths: List[str] = Field(max_length=256)
    effective_limits: EffectiveLimitsV1
    network_mode: NetworkMode = NetworkMode.NONE
    approval_request_id: Optional[str] = None
    decision_sha256: str = Field(pattern=SHA256)


class ApprovalRequestV1(_Strict):
    schema_version: Literal["1.0"] = "1.0"
    approval_request_id: str = ID
    run_id: str = ID
    task_id: str = ID
    operation: Literal["execute_workspace_action"] = "execute_workspace_action"
    purpose: str = Field(min_length=1, max_length=2000)
    action_proposal_id: str = ID
    code_sha256: str = Field(pattern=SHA256)
    workspace_version: str = ID
    capabilities: List[str] = Field(max_length=32)
    paths: List[str] = Field(max_length=256)
    runtime_profile_fingerprint: str = Field(pattern=SHA256)
    network_policy_fingerprint: str = Field(pattern=SHA256)
    limits_fingerprint: str = Field(pattern=SHA256)
    policy_version: int = Field(ge=1)
    max_uses: int = Field(default=1, ge=1, le=10)
    expires_at: str
    consequences: str = Field(min_length=1, max_length=2000)


class RuntimeProfileV1(_Strict):
    schema_version: Literal["1.0"] = "1.0"
    runtime_profile_id: str = ID
    runtime: Literal["python"] = "python"
    runtime_version: str = Field(min_length=1, max_length=32)
    image_reference: str = Field(min_length=1, max_length=512)
    image_digest: str = Field(pattern=SHA256)
    worker_version: str = Field(min_length=1, max_length=32)
    tool_library_version: str = Field(min_length=1, max_length=32)
    non_root_uid: int = Field(ge=1, le=2**31 - 1)
    non_root_gid: int = Field(ge=1, le=2**31 - 1)
    default_network_mode: NetworkMode = NetworkMode.NONE
    shell_path: str = "/bin/bash"
    limits_profile: str = Field(min_length=1, max_length=64)
    profile_fingerprint: str = Field(pattern=SHA256)

    @model_validator(mode="after")
    def _immutable_reference(self) -> "RuntimeProfileV1":
        if "@sha256:" not in self.image_reference and not self.image_reference.startswith("sha256:"):
            raise ValueError("runtime image reference must be pinned by immutable digest")
        if not self.image_reference.endswith(self.image_digest):
            raise ValueError("image_reference digest differs from image_digest")
        return self


class HashedPathV1(_Strict):
    container_path: str = Field(min_length=1, max_length=256)
    sha256: str = Field(pattern=SHA256)


class SandboxLimitsV1(_Strict):
    wall_seconds: int = Field(gt=0, le=1200)
    cpus: float = Field(gt=0, le=4)
    memory_bytes: int = Field(gt=0, le=8 * 1024**3)
    pids: int = Field(gt=0, le=512)
    scratch_bytes: int = Field(gt=0, le=4 * 1024**3)
    stdout_bytes: int = Field(gt=0, le=10 * 1024**2)
    stderr_bytes: int = Field(gt=0, le=10 * 1024**2)
    tool_calls: int = Field(gt=0, le=256)
    run_calls: int = Field(ge=0, le=32)
    workspace_growth_bytes: int = Field(ge=0, le=2 * 1024**3)
    new_files: int = Field(ge=0, le=25_000)


class SandboxExecutionRequestV1(_Strict):
    schema_version: Literal["1.0"] = "1.0"
    action_id: str = ID
    run_id: str = ID
    task_id: str = ID
    action_proposal_id: str = ID
    workspace_version: str = ID
    checkpoint_id: str = ID
    code: HashedPathV1
    context_manifest: HashedPathV1
    capabilities: List[str] = Field(max_length=32)
    declared_paths: List[str] = Field(max_length=256)
    network_mode: NetworkMode = NetworkMode.NONE
    runtime_profile_fingerprint: str = Field(pattern=SHA256)
    policy_decision_sha256: str = Field(pattern=SHA256)
    limits: SandboxLimitsV1


class ToolCallState(str, Enum):
    STARTED = "STARTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TRUNCATED = "TRUNCATED"
    REJECTED = "REJECTED"


class ToolCallEventV1(_Strict):
    schema_version: Literal["1.0"] = "1.0"
    action_id: str = ID
    tool_call_id: str = ID
    sequence: int = Field(ge=1, le=100_000)
    tool: Literal[
        "search", "read_file", "symbols", "apply_patch", "run", "read_artifact", "emit_result"
    ]
    tool_version: str = Field(min_length=1, max_length=16)
    capability: str = Field(min_length=1, max_length=64)
    state: ToolCallState
    arguments_summary: Dict[str, Any] = Field(default_factory=dict)
    result_summary: Dict[str, Any] = Field(default_factory=dict)
    started_at: str
    finished_at: Optional[str] = None


class Settlement(str, Enum):
    ACCEPTED = "ACCEPTED"
    NO_CHANGE = "NO_CHANGE"
    FAILED_ROLLED_BACK = "FAILED_ROLLED_BACK"
    POLICY_VIOLATION_ROLLED_BACK = "POLICY_VIOLATION_ROLLED_BACK"
    CANCELLED_ROLLED_BACK = "CANCELLED_ROLLED_BACK"
    UNKNOWN = "UNKNOWN"


class ContainerIdentityV1(_Strict):
    runtime_profile_fingerprint: str = Field(pattern=SHA256)
    image_digest: str = Field(pattern=SHA256)
    container_id_hash: Optional[str] = Field(default=None, pattern=SHA256)


class ProcessOutcomeV1(_Strict):
    exit_code: Optional[int] = None
    signal: Optional[int] = None
    timed_out: bool = False
    oom_killed: bool = False
    output_limit_exceeded: bool = False
    elapsed_ms: int = Field(ge=0)


class WorkerResultSummaryV1(_Strict):
    status: Optional[Literal["ACTION_COMPLETED", "ACTION_NEEDS_FOLLOWUP", "ACTION_BLOCKED"]] = None
    summary: str = Field(default="", max_length=4000)
    trusted_as: Literal["advisory"] = "advisory"


class WorkspaceChangeSummaryV1(_Strict):
    before_version: str = ID
    after_version: Optional[str] = None
    change_set_sha256: Optional[str] = Field(default=None, pattern=SHA256)
    created: List[str] = Field(default_factory=list)
    modified: List[str] = Field(default_factory=list)
    deleted: List[str] = Field(default_factory=list)
    unexpected: List[str] = Field(default_factory=list)


class ExecutionArtifactsV1(_Strict):
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    tool_events: Optional[str] = None
    diff: Optional[str] = None
    result: Optional[str] = None


class ExecutionBudgetChargeV1(_Strict):
    action_units_charged: int = Field(ge=0)
    wall_seconds_charged: int = Field(ge=0)
    output_bytes_charged: int = Field(ge=0)


class ExecutionResultV1(_Strict):
    schema_version: Literal["1.0"] = "1.0"
    action_id: str = ID
    run_id: str = ID
    task_id: str = ID
    settlement: Settlement
    container: ContainerIdentityV1
    process: ProcessOutcomeV1
    worker_result: WorkerResultSummaryV1
    workspace: WorkspaceChangeSummaryV1
    artifacts: ExecutionArtifactsV1
    budget: ExecutionBudgetChargeV1
    observed_at: str


class WorkspaceVersionState(str, Enum):
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    ROLLED_BACK = "ROLLED_BACK"
    FROZEN = "FROZEN"
    QUARANTINED = "QUARANTINED"


class WorkspaceVersionV1(_Strict):
    schema_version: Literal["1.0"] = "1.0"
    workspace_version_id: str = ID
    run_id: str = ID
    task_id: str = ID
    parent_version_id: Optional[str] = None
    baseline_commit: str = Field(min_length=1, max_length=128)
    private_checkpoint_commit: str = Field(min_length=1, max_length=128)
    git_tree: str = Field(min_length=1, max_length=128)
    content_tree_sha256: str = Field(pattern=SHA256)
    manifest_artifact_id: str = ID
    created_by_action_id: Optional[str] = None
    state: WorkspaceVersionState
    created_at: str


class CandidateSnapshotV1(_Strict):
    schema_version: Literal["1.0"] = "1.0"
    candidate_id: str = ID
    run_id: str = ID
    task_id: str = ID
    workspace_version_id: str = ID
    baseline_commit: str = Field(min_length=1, max_length=128)
    candidate_commit: str = Field(min_length=1, max_length=128)
    candidate_tree: str = Field(min_length=1, max_length=128)
    candidate_sha256: str = Field(pattern=SHA256)
    manifest_artifact_id: str = ID
    diff_artifact_id: str = ID
    frozen: Literal[True] = True
    verification_status: Literal["NOT_RUN"] = "NOT_RUN"
    created_at: str


class HandoffBaselineV1(_Strict):
    commit: str = Field(min_length=1, max_length=128)
    content_tree_sha256: str = Field(pattern=SHA256)


class HandoffCandidateV1(_Strict):
    candidate_id: str = ID
    commit: str = Field(min_length=1, max_length=128)
    tree: str = Field(min_length=1, max_length=128)
    candidate_sha256: str = Field(pattern=SHA256)
    manifest_artifact_id: str = ID
    diff_artifact_id: str = ID
    verification_status: Literal["NOT_RUN"] = "NOT_RUN"


class HandoffAcceptanceContractV1(_Strict):
    plan_artifact_id: str = ID
    plan_sha256: str = Field(pattern=SHA256)


class HandoffEnvironmentV1(_Strict):
    runtime_profile_fingerprint: str = Field(pattern=SHA256)
    image_digest: str = Field(pattern=SHA256)
    dependency_environment_sha256: Optional[str] = Field(default=None, pattern=SHA256)
    worker_version: str = Field(min_length=1, max_length=32)
    tool_library_version: str = Field(min_length=1, max_length=32)


class HandoffRemainingBudgetV1(_Strict):
    model_calls: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    wall_seconds: int = Field(ge=0)
    verification_calls_reserved: int = Field(ge=0)


class VerificationCandidateHandoffV1(_Strict):
    schema_version: Literal["1.0"] = "1.0"
    run_id: str = ID
    task_id: str = ID
    task_revision: int = Field(ge=1)
    plan_id: str = ID
    plan_revision: int = Field(ge=1)
    baseline: HandoffBaselineV1
    candidate: HandoffCandidateV1
    changed_paths: List[str] = Field(max_length=25_000)
    action_result_ids: List[str] = Field(max_length=1024)
    acceptance_contract: HandoffAcceptanceContractV1
    environment: HandoffEnvironmentV1
    remaining_budget: HandoffRemainingBudgetV1
    policy_sha256: str = Field(pattern=SHA256)
