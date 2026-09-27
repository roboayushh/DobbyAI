"""Versioned contracts for PRD 5: queue, private Git workflow, integration, results."""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

SHA256 = r"^[0-9a-f]{64}$"
ID = Field(min_length=1, max_length=128)
OID = Field(min_length=4, max_length=128)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())


QueueItemClassification = Literal["ACTIONABLE", "DUPLICATE", "DEPENDENT", "AMBIGUOUS", "UNSUPPORTED", "INVALID"]
QueueStatus = Literal[
    "COMPLETED_ALL", "PARTIAL_SUCCESS", "FAILED", "UNVERIFIED", "BLOCKED_ENVIRONMENT",
    "BUDGET_EXHAUSTED", "NEEDS_INPUT", "CANCELLED", "INTEGRATION_UNCERTAIN", "INVALID",
]


class FinalReserveV1(_Strict):
    model_calls: int = Field(ge=0)
    wall_seconds: int = Field(ge=0)
    check_runs: int = Field(ge=0)
    output_bytes: int = Field(ge=0)


class QueuePolicyV1(_Strict):
    schema_version: Literal["1.0"] = "1.0"
    policy_id: str = ID
    max_tasks_default: int = Field(ge=1, le=20)
    max_tasks_hard: int = Field(ge=1, le=20)
    max_edges: int = Field(ge=0, le=400)
    single_writer: Literal[True] = True
    continue_independent_after_failure: bool = True
    fail_fast: bool = False
    one_commit_per_task: Literal[True] = True
    require_post_advance_verification: bool = True
    require_final_aggregate_verification: bool = True
    checkpoint_limit_per_task: int = Field(ge=1, le=1000)
    final_reserve: FinalReserveV1
    policy_sha256: str = Field(pattern=SHA256)


class QueuePlanItemV1(_Strict):
    queue_item_id: str = ID
    task_id: str = ID
    ordinal: int = Field(ge=0)
    classification: QueueItemClassification
    canonical_item_id: Optional[str] = None
    priority: int
    reason_codes: List[str] = Field(default_factory=list, max_length=32)


class QueuePlanEdgeV1(_Strict):
    edge_id: str = ID
    predecessor_item_id: str = ID
    dependent_item_id: str = ID
    kind: Literal["REQUIRES"] = "REQUIRES"
    source: Literal["USER_DECLARED", "SOURCE_METADATA", "PLANNER_PROPOSED_HOST_VALIDATED"]


class QueuePlanV1(_Strict):
    schema_version: Literal["1.0"] = "1.0"
    queue_id: str = ID
    queue_version_id: str = ID
    run_id: str = ID
    version: int = Field(ge=1)
    task_snapshot_sha256: str = Field(pattern=SHA256)
    policy_id: str = ID
    state: Literal["DRAFT", "FROZEN", "SUPERSEDED", "INVALID"]
    items: List[QueuePlanItemV1] = Field(max_length=20)
    edges: List[QueuePlanEdgeV1] = Field(max_length=400)
    plan_sha256: str = Field(pattern=SHA256)
    frozen_at: Optional[str] = None


class IntegrationStateV1(_Strict):
    sequence: int = Field(ge=0)
    commit: str = OID
    tree: str = OID
    aggregate_verification: Literal["NOT_RUN", "PASS", "FAILED", "UNVERIFIED", "BLOCKED_ENVIRONMENT", "BUDGET_EXHAUSTED"]


class QueueCountsV1(_Strict):
    selected: int = Field(ge=0)
    actionable: int = Field(ge=0)
    integrated: int = Field(ge=0)
    running: int = Field(ge=0)
    failed: int = Field(ge=0)
    blocked: int = Field(ge=0)
    remaining: int = Field(ge=0)


class BlockedItemV1(_Strict):
    queue_item_id: str = ID
    reason: str = Field(min_length=1, max_length=128)
    blocking_item_ids: List[str] = Field(default_factory=list, max_length=20)


class QueueBudgetViewV1(_Strict):
    model_calls_remaining: int
    wall_seconds_remaining: int
    final_reserve_intact: bool


class QueueProgressResultV1(_Strict):
    schema_version: Literal["1.0"] = "1.0"
    run_id: str = ID
    queue_id: str = ID
    queue_version_id: str = ID
    queue_state: Literal[
        "DRAFT", "FROZEN", "RUNNING", "PAUSED", "FINALIZING", "CANCELLING",
        "RECOVERING", "UNCERTAIN", "SETTLED", "INVALID",
    ]
    integration: IntegrationStateV1
    counts: QueueCountsV1
    active_item_id: Optional[str] = None
    blocked: List[BlockedItemV1] = Field(default_factory=list, max_length=20)
    budget: QueueBudgetViewV1
    last_event_sequence: int = Field(ge=0)
    updated_at: str


class GitIdentityV1(_Strict):
    commit: str = OID
    tree: str = OID
    object_format: Literal["sha1", "sha256"] = "sha1"


class TaskRefsV1(_Strict):
    start: str = Field(min_length=1, max_length=512)
    working: str = Field(min_length=1, max_length=512)
    candidate: str = Field(min_length=1, max_length=512)
    verified: str = Field(min_length=1, max_length=512)


class TaskExecutionStartV1(_Strict):
    schema_version: Literal["1.0"] = "1.0"
    task_execution_id: str = ID
    run_id: str = ID
    queue_version_id: str = ID
    queue_item_id: str = ID
    task_id: str = ID
    attempt_number: int = Field(ge=1)
    integration_sequence: int = Field(ge=0)
    start: GitIdentityV1
    refs: TaskRefsV1
    worktree_id: str = ID
    index_version_id: Optional[str] = None
    budget_allocation_id: str = ID
    started_at: str


class CommitVerificationV1(_Strict):
    status: Literal["PASS"]
    contract_sha256: str = Field(pattern=SHA256)
    test_set_sha256: str = Field(pattern=SHA256)
    environment_set_sha256: str = Field(pattern=SHA256)
    report_artifact_id: str = ID


class VerifiedTaskCommitV1(_Strict):
    schema_version: Literal["1.0"] = "1.0"
    task_commit_id: str = ID
    run_id: str = ID
    task_id: str = ID
    candidate_id: str = ID
    commit: str = OID
    tree: str = OID
    parent: str = OID
    object_format: Literal["sha1", "sha256"] = "sha1"
    shape: Literal["SINGLE_DIRECT_PARENT", "LEGACY_VERIFIED_RANGE"]
    completion_decision_id: str = ID
    verification: CommitVerificationV1
    metadata_sha256: str = Field(pattern=SHA256)


class IntegrationIntentV1(_Strict):
    schema_version: Literal["1.0"] = "1.0"
    integration_intent_id: str = ID
    ref_operation_id: str = ID
    run_id: str = ID
    queue_item_id: str = ID
    task_commit_id: Optional[str] = None
    strategy: Literal["FAST_FORWARD_EXACT", "REVERIFIED_REPLAY", "COMPENSATE"]
    integration_ref: str = Field(min_length=1, max_length=512)
    expected_old_commit: str = OID
    desired_new_commit: str = OID
    desired_tree: str = OID
    lease_fencing_token: int = Field(ge=1)
    state: Literal["PREPARED", "APPLYING", "APPLIED", "VERIFYING", "SETTLED", "REJECTED", "FAILED", "UNCERTAIN"]
    intent_sha256: str = Field(pattern=SHA256)
    created_at: str


class PostAdvanceVerificationV1(_Strict):
    status: Literal["PASS", "FAILED", "UNVERIFIED", "BLOCKED_ENVIRONMENT", "BUDGET_EXHAUSTED", "NEEDS_INPUT", "CANCELLED"]
    verification_id: str = ID
    contract_set_sha256: str = Field(pattern=SHA256)
    report_artifact_id: str = ID


class IntegrationResultV1(_Strict):
    schema_version: Literal["1.0"] = "1.0"
    integration_result_id: str = ID
    integration_intent_id: str = ID
    status: Literal["APPLIED_PENDING_VERIFICATION", "APPLIED_AND_VERIFIED", "NOT_APPLIED", "COMPENSATED", "FAILED", "UNCERTAIN"]
    observed_before_commit: str = OID
    observed_after_commit: str = OID
    integration_sequence: int = Field(ge=0)
    post_advance_verification: Optional[PostAdvanceVerificationV1] = None
    settled_at: str


class FinalIntegrationV1(_Strict):
    commit: str = OID
    tree: str = OID
    sequence: int = Field(ge=0)


class AggregateVerificationV1(_Strict):
    status: Literal["PASS", "FAILED", "UNVERIFIED", "BLOCKED_ENVIRONMENT", "BUDGET_EXHAUSTED", "NEEDS_INPUT", "CANCELLED", "NOT_RUN"]
    verification_id: Optional[str] = None
    contract_set_sha256: Optional[str] = Field(default=None, pattern=SHA256)
    report_artifact_id: Optional[str] = None


class FinalCountsV1(_Strict):
    selected: int = Field(ge=0)
    integrated: int = Field(ge=0)
    failed: int = Field(ge=0)
    blocked: int = Field(ge=0)
    remaining: int = Field(ge=0)


class TaskResultEntryV1(_Strict):
    task_id: str = ID
    status: str = Field(min_length=1, max_length=64)
    commit: Optional[str] = None


class QueueFinalResultV1(_Strict):
    schema_version: Literal["1.0"] = "1.0"
    run_id: str = ID
    queue_id: str = ID
    queue_version_id: str = ID
    status: QueueStatus
    baseline: GitIdentityV1
    final_integration: FinalIntegrationV1
    aggregate_verification: AggregateVerificationV1
    counts: FinalCountsV1
    task_results: List[TaskResultEntryV1] = Field(max_length=20)
    final_patch_artifact_id: Optional[str] = None
    queue_report_artifact_id: str = ID
    publication_authorized: Literal[False] = False
    settled_at: str


class EvaluationCaseResultV1(_Strict):
    schema_version: Literal["1.0"] = "1.0"
    batch_id: str = ID
    case_id: str = ID
    task_id: str = ID
    baseline_commit: str = OID
    candidate_commit: Optional[str] = None
    outcome: Literal["PASS", "FAILED", "UNVERIFIED", "BLOCKED_ENVIRONMENT", "BUDGET_EXHAUSTED", "NEEDS_INPUT", "CANCELLED"]
    context_isolation_id: str = ID
    workspace_id: str = ID
    completion_decision_id: Optional[str] = None
    carried_state_from_case_id: Literal[None] = None
    result_artifact_id: str = ID


class FinalPatchInputV1(_Strict):
    base: str = OID
    head: str = OID
    artifact_id: str = ID


class ReleaseCandidateHandoffV1(_Strict):
    schema_version: Literal["1.0"] = "1.0"
    run_id: str = ID
    source_identity_artifact_id: str = ID
    queue_final_result_artifact_id: str = ID
    baseline_commit: str = OID
    final_candidate_commit: str = OID
    final_candidate_tree: str = OID
    aggregate_status: str = Field(min_length=1, max_length=64)
    task_commit_ids: List[str] = Field(max_length=20)
    final_patch_input: FinalPatchInputV1
    dirty_or_synthetic_baseline: bool
    allowed_next_actions: List[Literal["EXPORT_PATCH", "EXPORT_RESULT_BUNDLE"]]
    publication_authorized: Literal[False] = False
