"""Versioned public contracts for PRD 6: evaluator I/O, plugins, export, approvals,
external effects, cleanup, reproducibility, and doctor reports.

Unknown fields and enum values fail closed (``extra="forbid"``). Major-version
bumps of ``schema_version`` are rejected before any model call or mutation.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SHA256 = r"^[0-9a-f]{64}$"
OID = r"^[0-9a-f]{4,64}$"
SUPPORTED_MAJOR = "1"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    @field_validator("schema_version", check_fields=False)
    @classmethod
    def _supported_version(cls, value: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+", value):
            raise ValueError("schema_version must look like MAJOR.MINOR")
        if value.split(".")[0] != SUPPORTED_MAJOR:
            raise ValueError(f"UNSUPPORTED_SCHEMA_VERSION: major version {value.split('.')[0]} is not supported")
        return value


# ------------------------------------------------------------------ evaluator
class AdapterRefV1(_Strict):
    name: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9_.-]+$")
    version: str = Field(min_length=1, max_length=32)


class EvaluatorRepositoryV1(_Strict):
    kind: Literal["local_git", "public_https", "local_folder", "local_zip"]
    locator: str = Field(min_length=1, max_length=4096)
    revision: Optional[str] = Field(default=None, max_length=256)


class EvaluatorTaskV1(_Strict):
    source_type: Literal["direct_text", "issue_url", "issue_number", "repository_query"]
    text: Optional[str] = Field(default=None, max_length=200_000)
    issue_url: Optional[str] = Field(default=None, max_length=2048)
    issue_number: Optional[int] = Field(default=None, ge=1)
    repository_owner: Optional[str] = Field(default=None, max_length=200)
    state: Optional[Literal["open", "closed", "all"]] = None
    labels: List[str] = Field(default_factory=list, max_length=20)
    max_tasks: Optional[int] = Field(default=None, ge=1, le=20)

    @model_validator(mode="after")
    def _one_source(self) -> "EvaluatorTaskV1":
        needed = {"direct_text": self.text, "issue_url": self.issue_url, "issue_number": self.issue_number}
        if self.source_type in needed and not needed[self.source_type]:
            raise ValueError(f"task.source_type={self.source_type} requires its value")
        return self


class EvaluatorBudgetsV1(_Strict):
    model_calls: Optional[int] = Field(default=None, ge=1, le=2000)
    input_tokens: Optional[int] = Field(default=None, ge=1000)
    output_tokens: Optional[int] = Field(default=None, ge=100)
    wall_seconds: Optional[int] = Field(default=None, ge=30, le=86_400)


class EvaluatorRequestV1(_Strict):
    schema_version: str = "1.0"
    request_id: str = Field(min_length=1, max_length=128)
    adapter: AdapterRefV1
    repository: EvaluatorRepositoryV1
    task_mode: Literal["single_issue", "repository"]
    execution_mode: Literal["development", "evaluation"]
    task: EvaluatorTaskV1
    profile: str = Field(default="evaluation_strict_v1", min_length=1, max_length=128)
    runtime_profile: str = Field(default="python312_docker_v1", min_length=1, max_length=128)
    model_config_ref: Optional[str] = Field(default=None, max_length=128)
    budgets: EvaluatorBudgetsV1 = Field(default_factory=EvaluatorBudgetsV1)
    result_path: Optional[str] = Field(default=None, max_length=4096)
    export_path: Optional[str] = Field(default=None, max_length=4096)
    requested_effects: List[Literal["EXPORT", "APPLY_LOCAL", "PUSH_NEW_BRANCH", "CLEANUP_RUN"]] = Field(
        default_factory=lambda: ["EXPORT"], max_length=4
    )
    idempotency_key: str = Field(min_length=1, max_length=256)


class ResultInputV1(_Strict):
    baseline_commit: Optional[str] = None
    baseline_tree: Optional[str] = None
    content_sha256: Optional[str] = Field(default=None, pattern=SHA256)


class ResultCandidateV1(_Strict):
    commit: str = Field(pattern=OID)
    tree: str = Field(pattern=OID)
    content_sha256: str = Field(pattern=SHA256)


class ResultTaskV1(_Strict):
    task_id: str
    status: str = Field(min_length=1, max_length=64)
    changed_paths: List[str] = Field(default_factory=list, max_length=2000)
    verification_report_artifact_id: Optional[str] = None


class ResultVerificationV1(_Strict):
    status: str = Field(min_length=1, max_length=64)
    required_checks: int = Field(ge=0)
    passed_checks: int = Field(ge=0)
    failed_checks: int = Field(ge=0)
    skipped_checks: int = Field(ge=0)
    unavailable_checks: int = Field(ge=0)
    aggregate_report_artifact_id: Optional[str] = None


class ResultUsageV1(_Strict):
    model_calls: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    wall_seconds: int = Field(ge=0)
    estimated_fields: List[str] = Field(default_factory=list)


class ResultExportV1(_Strict):
    status: Literal["VALID", "INVALID", "FAILED", "NOT_REQUESTED", "EXPORT_INVALID"]
    bundle_path: Optional[str] = None
    manifest_sha256: Optional[str] = Field(default=None, pattern=SHA256)
    patch_sha256: Optional[str] = Field(default=None, pattern=SHA256)


class PendingApprovalV1(_Strict):
    capability_request_id: str
    operation: str
    summary: str = Field(max_length=2000)


EVALUATOR_STATUSES = (
    "PASS", "COMPLETED_ALL", "PARTIAL_SUCCESS", "FAILED", "UNVERIFIED", "BLOCKED_ENVIRONMENT",
    "BUDGET_EXHAUSTED", "NEEDS_INPUT", "PENDING_APPROVAL", "CANCELLED", "INVALID",
    "INTEGRATION_UNCERTAIN", "INTERNAL_ERROR",
)


class EvaluatorResultV1(_Strict):
    schema_version: str = "1.0"
    request_id: Optional[str] = None
    run_id: Optional[str] = None
    status: Literal[
        "PASS", "COMPLETED_ALL", "PARTIAL_SUCCESS", "FAILED", "UNVERIFIED", "BLOCKED_ENVIRONMENT",
        "BUDGET_EXHAUSTED", "NEEDS_INPUT", "PENDING_APPROVAL", "CANCELLED", "INVALID",
        "INTEGRATION_UNCERTAIN", "INTERNAL_ERROR",
    ]
    exit_code: int
    input: ResultInputV1 = Field(default_factory=ResultInputV1)
    candidate: Optional[ResultCandidateV1] = None
    tasks: List[ResultTaskV1] = Field(default_factory=list, max_length=20)
    verification: Optional[ResultVerificationV1] = None
    usage: Optional[ResultUsageV1] = None
    export: Optional[ResultExportV1] = None
    pending_approvals: List[PendingApprovalV1] = Field(default_factory=list)
    limitations: List[str] = Field(default_factory=list, max_length=50)
    error: Optional[Dict[str, Any]] = None
    reproducibility_manifest_artifact_id: Optional[str] = None
    settled_at: str


# -------------------------------------------------------------------- plugins
class PluginIdentityV1(_Strict):
    name: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9_.-]+$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    distribution: str = Field(min_length=1, max_length=128)
    module: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.]*$")
    entry_point: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    content_sha256: str = Field(pattern=SHA256)


class PluginInterfaceRefV1(_Strict):
    name: Literal[
        "ControllerPlugin", "ContextBuilderPlugin", "ModelAdapterPlugin", "RepositoryPlugin", "RetrieverPlugin",
        "EnvironmentPlugin", "VerifierPlugin", "EvaluatorAdapterPlugin", "ExporterPlugin", "PublisherPlugin",
        "ReportRendererPlugin",
    ]
    api_version: str = Field(pattern=r"^\d+\.\d+$")


class PluginDependencyV1(_Strict):
    name: str
    version_constraint: str = Field(min_length=1, max_length=64)
    optional: bool = False


class PluginRuntimeV1(_Strict):
    python: str = Field(min_length=1, max_length=64)
    platforms: List[str] = Field(min_length=1)


class PluginProvenanceV1(_Strict):
    source: str
    license: str
    review_status: Literal["REVIEWED", "REJECTED", "EXPIRED"]
    review_artifact_id: Optional[str] = None


class PluginManifestV1(_Strict):
    schema_version: str = "1.0"
    plugin: PluginIdentityV1
    interfaces: List[PluginInterfaceRefV1] = Field(min_length=1)
    configuration_schema_sha256: str = Field(pattern=SHA256)
    required_capabilities: List[str] = Field(default_factory=list)
    optional_capabilities: List[str] = Field(default_factory=list)
    dependencies: List[PluginDependencyV1] = Field(default_factory=list)
    runtime: PluginRuntimeV1
    provenance: PluginProvenanceV1
    in_process: bool = True
    self_check: str = Field(min_length=1, max_length=128)


class PluginLockEntryV1(_Strict):
    slot: str = Field(pattern=r"^[a-z_]+$")
    name: str
    version: str
    content_sha256: str = Field(pattern=SHA256)
    configuration_sha256: str = Field(pattern=SHA256)


class PluginSetLockV1(_Strict):
    schema_version: str = "1.0"
    plugin_set_id: str
    kernel_api_version: str = Field(pattern=r"^\d+\.\d+$")
    plugins: List[PluginLockEntryV1] = Field(min_length=1)
    lock_sha256: str = Field(pattern=SHA256)
    created_at: str


# --------------------------------------------------------------------- export
class ExportCandidateRefV1(_Strict):
    base_commit: str = Field(pattern=OID)
    head_commit: str = Field(pattern=OID)
    head_tree: str = Field(pattern=OID)
    head_content_sha256: str = Field(pattern=SHA256)


class ExportRequestV1(_Strict):
    schema_version: str = "1.0"
    export_request_id: str
    run_id: str
    release_handoff_artifact_id: Optional[str] = None
    candidate: ExportCandidateRefV1
    format: Literal["unified_git_patch_v1"] = "unified_git_patch_v1"
    output_path: str = Field(min_length=1, max_length=4096)
    replace_existing: bool = False
    include_artifact_kinds: List[Literal["verification_summary", "task_results", "usage", "provenance"]] = Field(
        default_factory=lambda: ["verification_summary", "task_results", "usage", "provenance"]
    )
    max_bundle_bytes: int = Field(default=25_000_000, gt=0, le=512 * 1024 * 1024)
    request_sha256: str = Field(pattern=SHA256)


class GitPointV1(_Strict):
    commit: str = Field(pattern=OID)
    tree: str = Field(pattern=OID)


class ExportCandidateV1(_Strict):
    commit: str = Field(pattern=OID)
    tree: str = Field(pattern=OID)
    content_sha256: str = Field(pattern=SHA256)


class ExportFileV1(_Strict):
    path: str = Field(min_length=1, max_length=512)
    media_type: str
    bytes: int = Field(ge=0)
    sha256: str = Field(pattern=SHA256)


class RoundTripV1(_Strict):
    status: Literal["PASS", "FAIL", "BLOCKED_ENVIRONMENT", "CANCELLED"]
    result_tree: Optional[str] = None
    report_artifact_id: Optional[str] = None


class ExportManifestV1(_Strict):
    schema_version: str = "1.0"
    export_id: str
    run_id: str
    status: Literal["VALID", "INVALID", "FAILED"]
    format: Literal["unified_git_patch_v1"] = "unified_git_patch_v1"
    base: GitPointV1
    candidate: ExportCandidateV1
    files: List[ExportFileV1]
    round_trip: RoundTripV1
    manifest_sha256: str = Field(pattern=SHA256)
    created_at: str


# ------------------------------------------------------------------ approvals
class CapabilityTargetV1(_Strict):
    repository_identity: Optional[str] = None
    remote_name: Optional[str] = None
    remote_url_sha256: Optional[str] = Field(default=None, pattern=SHA256)
    ref: Optional[str] = None
    expected_oid: Optional[str] = None
    local_root_identity: Optional[str] = None
    run_id: Optional[str] = None


class CapabilityPolicyV1(_Strict):
    policy_id: str
    policy_sha256: str = Field(pattern=SHA256)
    permission_profile: Literal["guided", "sandbox", "delegated"]


class CapabilityRequestV1(_Strict):
    schema_version: str = "1.0"
    capability_request_id: str
    run_id: str
    operation: Literal["APPLY_LOCAL", "PUSH_NEW_BRANCH", "CREATE_PULL_REQUEST", "MERGE_TARGET", "CLEANUP_RUN", "DELETE_REMOTE_BRANCH"]
    candidate: Optional[ExportCandidateV1] = None
    artifact_sha256: Optional[str] = Field(default=None, pattern=SHA256)
    target: CapabilityTargetV1
    policy: CapabilityPolicyV1
    requested_uses: int = Field(default=1, ge=1, le=10)
    expires_at: str
    summary_artifact_id: Optional[str] = None
    request_sha256: str = Field(pattern=SHA256)


class PrincipalV1(_Strict):
    principal_id: str
    channel: Literal["interactive_terminal", "headless_pregrant", "admin_policy"]


class ApprovalGrantV1(_Strict):
    schema_version: str = "1.0"
    approval_grant_id: str
    capability_request_id: str
    principal: PrincipalV1
    bound_request_sha256: str = Field(pattern=SHA256)
    credential_scope_id: Optional[str] = None
    max_uses: int = Field(ge=1)
    remaining_uses: int = Field(ge=0)
    state: Literal["ACTIVE", "CONSUMED", "REVOKED", "EXPIRED", "INVALIDATED"]
    created_at: str
    expires_at: str
    grant_sha256: str = Field(pattern=SHA256)

    @model_validator(mode="after")
    def _uses(self) -> "ApprovalGrantV1":
        if self.remaining_uses > self.max_uses:
            raise ValueError("remaining_uses cannot exceed max_uses")
        return self


class LocalApplyPathV1(_Strict):
    path: str
    operation: Literal["CREATE", "MODIFY", "DELETE", "MODE_CHANGE", "RENAME"]
    expected_before_sha256: Optional[str] = Field(default=None, pattern=SHA256)
    desired_after_sha256: Optional[str] = Field(default=None, pattern=SHA256)
    expected_before_mode: Optional[str] = None
    desired_after_mode: Optional[str] = None


class LocalApplyPlanV1(_Strict):
    schema_version: str = "1.0"
    application_plan_id: str
    run_id: str
    target_root_identity: str
    base_commit: str = Field(pattern=OID)
    candidate_commit: str = Field(pattern=OID)
    patch_sha256: str = Field(pattern=SHA256)
    paths: List[LocalApplyPathV1]
    concurrent_change_policy: Literal["STOP", "STOP_AND_REVERIFY"]
    backup_bytes: int = Field(ge=0)
    preflight_status: Literal["PASS", "FAIL", "NEEDS_REVERIFY"]
    plan_sha256: str = Field(pattern=SHA256)


class EffectPluginRefV1(_Strict):
    name: str
    version: str
    content_sha256: str = Field(pattern=SHA256)


class EffectTargetV1(_Strict):
    repository_identity: Optional[str] = None
    ref: Optional[str] = None
    expected_oid: Optional[str] = None
    desired_oid: Optional[str] = None
    local_root_identity: Optional[str] = None


class ExternalEffectIntentV1(_Strict):
    schema_version: str = "1.0"
    effect_intent_id: str
    run_id: str
    operation: Literal["APPLY_LOCAL", "PUSH_NEW_BRANCH", "CLEANUP_RUN"]
    approval_grant_id: str
    approval_consumption_id: str
    plugin: EffectPluginRefV1
    source_commit: Optional[str] = None
    target: EffectTargetV1
    credential_scope_id: Optional[str] = None
    idempotency_key: str
    state: Literal["PREPARED", "DISPATCHED", "OBSERVING", "SETTLED", "NOT_APPLIED", "FAILED", "UNCERTAIN"]
    intent_sha256: str = Field(pattern=SHA256)
    created_at: str


class EffectDispatchV1(_Strict):
    started_at: str
    response_received: bool
    provider_receipt_redacted: Optional[str] = None


class EffectObservationV1(_Strict):
    observed_at: str
    target_ref: Optional[str] = None
    observed_oid: Optional[str] = None
    matches_desired: bool


class ExternalEffectReceiptV1(_Strict):
    schema_version: str = "1.0"
    effect_receipt_id: str
    effect_intent_id: str
    status: Literal[
        "APPLIED", "PUBLISHED", "CLEANED", "NOT_APPLIED", "PARTIAL_CLEANUP", "FAILED", "APPLICATION_UNCERTAIN",
        "PUBLICATION_UNCERTAIN", "CLEANUP_UNCERTAIN", "REMOTE_CONFLICT",
    ]
    dispatch: EffectDispatchV1
    observation: EffectObservationV1
    reconciliation: str
    receipt_sha256: str = Field(pattern=SHA256)


# -------------------------------------------------------------------- cleanup
class CleanupTargetV1(_Strict):
    resource_id: str
    kind: Literal[
        "TEMP_CONTAINER", "VERIFICATION_WORKTREE", "TASK_WORKTREE", "CACHE", "PRIVATE_REF", "RAW_LOG",
        "RUN_ARTIFACT", "RUN_DATABASE", "RUN_ROOT",
    ]
    registered_path_hash: str = Field(pattern=SHA256)
    estimated_bytes: int = Field(ge=0)
    eligibility: Literal["ELIGIBLE", "RETAIN", "PROTECTED", "ACTIVE_REFERENCE"]


class CleanupExclusionV1(_Strict):
    resource_id: str
    reason: str


class CleanupPlanV1(_Strict):
    schema_version: str = "1.0"
    cleanup_plan_id: str
    run_id: str
    retention_policy_id: str
    targets: List[CleanupTargetV1]
    excluded: List[CleanupExclusionV1] = Field(default_factory=list)
    estimated_reclaimed_bytes: int = Field(ge=0)
    requires_approval: bool
    plan_sha256: str = Field(pattern=SHA256)


# ------------------------------------------------------------- reproducibility
class HarnessBuildV1(_Strict):
    version: str
    source_commit: str
    build_id: str


class SchemaVersionsV1(_Strict):
    request: str
    event: str
    result: str
    database_migration: int = Field(ge=1)


class ReproAdapterV1(_Strict):
    name: str
    version: str
    content_sha256: str = Field(pattern=SHA256)


class ReproModelV1(_Strict):
    provider: str
    model: str
    adapter_version: str
    config_sha256: str = Field(pattern=SHA256)
    sampling: Dict[str, Any]


class ReproSourceV1(_Strict):
    baseline_commit: str
    baseline_tree: str
    candidate_commit: Optional[str] = None
    candidate_tree: Optional[str] = None


class ReproRuntimeV1(_Strict):
    image_digest: str
    architecture: str
    dependency_lock_sha256: str = Field(pattern=SHA256)


class ReproVerificationV1(_Strict):
    contract_set_sha256: Optional[str] = Field(default=None, pattern=SHA256)
    test_command_set_sha256: Optional[str] = Field(default=None, pattern=SHA256)
    environment_set_sha256: Optional[str] = Field(default=None, pattern=SHA256)


class ReproducibilityManifestV1(_Strict):
    schema_version: str = "1.0"
    run_id: str
    harness: HarnessBuildV1
    schemas: SchemaVersionsV1
    evaluator_adapter: ReproAdapterV1
    plugin_set_lock_sha256: str = Field(pattern=SHA256)
    model: ReproModelV1
    source: ReproSourceV1
    runtime: ReproRuntimeV1
    verification: ReproVerificationV1
    effective_configuration_sha256: str = Field(pattern=SHA256)
    manifest_sha256: str = Field(pattern=SHA256)
    created_at: str


# --------------------------------------------------------------------- doctor
class DoctorCheckV1(_Strict):
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    status: Literal["PASS", "FAIL", "WARN", "SKIP"]
    observed: str = Field(max_length=500)
    required: str = Field(max_length=500)


class DoctorReportV1(_Strict):
    schema_version: str = "1.0"
    profile: str
    status: Literal["READY", "BLOCKED", "WARNING"]
    checks: List[DoctorCheckV1]
    blocking_check_ids: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    report_sha256: str = Field(pattern=SHA256)
    created_at: str
