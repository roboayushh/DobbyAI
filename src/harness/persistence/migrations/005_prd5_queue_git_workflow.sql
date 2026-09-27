-- PRD 5: bounded queue, dependency DAG, task executions, task commits, journaled
-- integration-ref compare-and-swap, cumulative verification, queue results.
-- Additive: PRD 1-4 records keep their meaning; these tables reference them.

CREATE TABLE h_task_queues (
    queue_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES h_runs(run_id) ON DELETE CASCADE,
    task_snapshot_sha256 TEXT NOT NULL CHECK (length(task_snapshot_sha256) = 64),
    active_version_id TEXT,
    mode TEXT NOT NULL CHECK (mode IN ('development', 'evaluation')),
    state TEXT NOT NULL CHECK (state IN (
        'DRAFT', 'FROZEN', 'RUNNING', 'PAUSED', 'FINALIZING',
        'CANCELLING', 'RECOVERING', 'UNCERTAIN', 'SETTLED', 'INVALID'
    )),
    state_version INTEGER NOT NULL DEFAULT 1 CHECK (state_version >= 1),
    pause_requested INTEGER NOT NULL DEFAULT 0 CHECK (pause_requested IN (0, 1)),
    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE h_queue_versions (
    queue_version_id TEXT PRIMARY KEY,
    queue_id TEXT NOT NULL REFERENCES h_task_queues(queue_id) ON DELETE CASCADE,
    version INTEGER NOT NULL CHECK (version >= 1),
    policy_id TEXT NOT NULL,
    policy_sha256 TEXT NOT NULL CHECK (length(policy_sha256) = 64),
    plan_artifact_id TEXT NOT NULL REFERENCES h_artifacts(artifact_id),
    plan_sha256 TEXT NOT NULL CHECK (length(plan_sha256) = 64),
    state TEXT NOT NULL CHECK (state IN ('DRAFT', 'FROZEN', 'SUPERSEDED', 'INVALID')),
    created_at TEXT NOT NULL,
    frozen_at TEXT,
    UNIQUE (queue_id, version)
);

CREATE TABLE h_queue_items (
    queue_item_id TEXT PRIMARY KEY,
    queue_version_id TEXT NOT NULL REFERENCES h_queue_versions(queue_version_id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES h_tasks(task_id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    priority INTEGER NOT NULL DEFAULT 0,
    classification TEXT NOT NULL CHECK (classification IN (
        'ACTIONABLE', 'DUPLICATE', 'DEPENDENT', 'AMBIGUOUS', 'UNSUPPORTED', 'INVALID'
    )),
    canonical_item_id TEXT REFERENCES h_queue_items(queue_item_id),
    state TEXT NOT NULL CHECK (state IN (
        'PENDING', 'READY', 'STARTING', 'RUNNING', 'PASS_VERIFIED',
        'INTEGRATING', 'POST_INTEGRATION_VERIFYING', 'INTEGRATED',
        'BLOCKED_DEPENDENCY', 'NEEDS_INPUT', 'UNSUPPORTED', 'DUPLICATE',
        'SKIPPED', 'FAILED', 'UNVERIFIED', 'BLOCKED_ENVIRONMENT',
        'BUDGET_EXHAUSTED', 'CANCELLED', 'INTEGRATION_FAILED',
        'INTEGRATION_UNCERTAIN', 'REMAINING_BUDGET', 'NEEDS_APPROVAL', 'VERIFIED_NOT_INTEGRATED'
    )),
    reason_codes_json TEXT NOT NULL,
    classification_artifact_id TEXT NOT NULL REFERENCES h_artifacts(artifact_id),
    classification_sha256 TEXT NOT NULL CHECK (length(classification_sha256) = 64),
    state_version INTEGER NOT NULL DEFAULT 1 CHECK (state_version >= 1),
    updated_at TEXT NOT NULL,
    UNIQUE (queue_version_id, task_id),
    UNIQUE (queue_version_id, ordinal),
    CHECK (canonical_item_id IS NULL OR canonical_item_id <> queue_item_id)
);

CREATE TABLE h_queue_edges (
    edge_id TEXT PRIMARY KEY,
    queue_version_id TEXT NOT NULL REFERENCES h_queue_versions(queue_version_id) ON DELETE CASCADE,
    predecessor_item_id TEXT NOT NULL REFERENCES h_queue_items(queue_item_id) ON DELETE CASCADE,
    dependent_item_id TEXT NOT NULL REFERENCES h_queue_items(queue_item_id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('REQUIRES')),
    source TEXT NOT NULL CHECK (source IN (
        'USER_DECLARED', 'SOURCE_METADATA', 'PLANNER_PROPOSED_HOST_VALIDATED'
    )),
    evidence_artifact_id TEXT REFERENCES h_artifacts(artifact_id),
    created_at TEXT NOT NULL,
    UNIQUE (queue_version_id, predecessor_item_id, dependent_item_id, kind),
    CHECK (predecessor_item_id <> dependent_item_id)
);

CREATE TABLE h_queue_leases (
    queue_lease_id TEXT PRIMARY KEY,
    queue_id TEXT NOT NULL REFERENCES h_task_queues(queue_id) ON DELETE CASCADE,
    owner_id TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose IN ('EXECUTE', 'FINALIZE', 'RECOVER')),
    fencing_token INTEGER NOT NULL CHECK (fencing_token >= 1),
    state TEXT NOT NULL CHECK (state IN ('ACTIVE', 'RELEASED', 'EXPIRED', 'REVOKED')),
    acquired_at TEXT NOT NULL,
    renewed_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    released_at TEXT,
    UNIQUE (queue_id, fencing_token)
);

CREATE UNIQUE INDEX h_queue_leases_one_active_idx
    ON h_queue_leases(queue_id)
    WHERE state = 'ACTIVE';

CREATE TABLE h_queue_budget_allocations (
    budget_allocation_id TEXT PRIMARY KEY,
    queue_id TEXT NOT NULL REFERENCES h_task_queues(queue_id) ON DELETE CASCADE,
    queue_item_id TEXT REFERENCES h_queue_items(queue_item_id),
    allocation_kind TEXT NOT NULL CHECK (allocation_kind IN ('TASK', 'FINAL_RESERVE', 'RECOVERY')),
    model_calls_reserved INTEGER NOT NULL CHECK (model_calls_reserved >= 0),
    input_tokens_reserved INTEGER NOT NULL CHECK (input_tokens_reserved >= 0),
    output_tokens_reserved INTEGER NOT NULL CHECK (output_tokens_reserved >= 0),
    wall_seconds_reserved INTEGER NOT NULL CHECK (wall_seconds_reserved >= 0),
    check_runs_reserved INTEGER NOT NULL CHECK (check_runs_reserved >= 0),
    output_bytes_reserved INTEGER NOT NULL CHECK (output_bytes_reserved >= 0),
    state TEXT NOT NULL CHECK (state IN ('RESERVED', 'ACTIVE', 'SETTLED', 'RELEASED', 'EXHAUSTED')),
    usage_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    settled_at TEXT
);

CREATE TABLE h_task_executions (
    task_execution_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES h_runs(run_id) ON DELETE CASCADE,
    queue_item_id TEXT NOT NULL REFERENCES h_queue_items(queue_item_id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES h_tasks(task_id) ON DELETE CASCADE,
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    integration_sequence_at_start INTEGER NOT NULL CHECK (integration_sequence_at_start >= 0),
    start_commit_oid TEXT NOT NULL,
    start_tree_oid TEXT NOT NULL,
    object_format TEXT NOT NULL CHECK (object_format IN ('sha1', 'sha256')),
    index_version_id TEXT,
    budget_allocation_id TEXT NOT NULL REFERENCES h_queue_budget_allocations(budget_allocation_id),
    state TEXT NOT NULL CHECK (state IN (
        'STARTING', 'RUNNING', 'CANDIDATE_READY', 'PASS_VERIFIED',
        'INTEGRATING', 'INTEGRATED', 'FAILED', 'CANCELLED', 'RECOVERING', 'UNCERTAIN', 'SETTLED'
    )),
    outcome TEXT,
    start_artifact_id TEXT REFERENCES h_artifacts(artifact_id),
    started_at TEXT NOT NULL,
    settled_at TEXT,
    UNIQUE (queue_item_id, attempt_number)
);

CREATE TABLE h_task_git_refs (
    task_git_ref_id TEXT PRIMARY KEY,
    task_execution_id TEXT NOT NULL REFERENCES h_task_executions(task_execution_id) ON DELETE CASCADE,
    ref_kind TEXT NOT NULL CHECK (ref_kind IN ('START', 'WORKING', 'CANDIDATE', 'VERIFIED', 'CHECKPOINT')),
    ref_name TEXT NOT NULL UNIQUE,
    sequence INTEGER,
    target_oid TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('ACTIVE', 'RETAINED', 'RETIRED', 'MISSING', 'UNCERTAIN')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (ref_kind = 'CHECKPOINT' AND sequence IS NOT NULL AND sequence >= 1) OR
        (ref_kind <> 'CHECKPOINT' AND sequence IS NULL)
    )
);

CREATE UNIQUE INDEX h_task_git_refs_one_kind_idx
    ON h_task_git_refs(task_execution_id, ref_kind)
    WHERE ref_kind <> 'CHECKPOINT';

CREATE TABLE h_task_worktrees (
    task_worktree_id TEXT PRIMARY KEY,
    task_execution_id TEXT NOT NULL REFERENCES h_task_executions(task_execution_id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES h_tasks(task_id) ON DELETE CASCADE,
    absolute_path_hash TEXT NOT NULL CHECK (length(absolute_path_hash) = 64),
    start_commit_oid TEXT NOT NULL,
    observed_head_oid TEXT,
    observed_tree_oid TEXT,
    before_manifest_artifact_id TEXT NOT NULL REFERENCES h_artifacts(artifact_id),
    after_manifest_artifact_id TEXT REFERENCES h_artifacts(artifact_id),
    state TEXT NOT NULL CHECK (state IN (
        'REGISTERED', 'CREATED', 'ACTIVE', 'PAUSED', 'RETIRED',
        'MISSING', 'DIRTY_UNRECORDED', 'UNCERTAIN'
    )),
    created_at TEXT NOT NULL,
    retired_at TEXT
);

CREATE TABLE h_task_commits (
    task_commit_id TEXT PRIMARY KEY,
    task_execution_id TEXT NOT NULL REFERENCES h_task_executions(task_execution_id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES h_tasks(task_id) ON DELETE CASCADE,
    candidate_id TEXT NOT NULL REFERENCES h_candidate_snapshots(candidate_id),
    completion_decision_id TEXT REFERENCES h_completion_decisions(completion_decision_id),
    commit_oid TEXT NOT NULL,
    tree_oid TEXT NOT NULL,
    parent_oid TEXT NOT NULL,
    object_format TEXT NOT NULL CHECK (object_format IN ('sha1', 'sha256')),
    shape TEXT NOT NULL CHECK (shape IN ('SINGLE_DIRECT_PARENT', 'LEGACY_VERIFIED_RANGE')),
    metadata_sha256 TEXT NOT NULL CHECK (length(metadata_sha256) = 64),
    verification_state TEXT NOT NULL CHECK (verification_state IN ('NOT_RUN', 'PASS', 'NON_PASS', 'INVALIDATED')),
    created_at TEXT NOT NULL,
    verified_at TEXT,
    UNIQUE (task_execution_id, candidate_id),
    UNIQUE (commit_oid, object_format),
    CHECK (
        (verification_state = 'PASS' AND completion_decision_id IS NOT NULL) OR
        (verification_state <> 'PASS')
    )
);

CREATE TABLE h_integration_heads (
    integration_head_id TEXT PRIMARY KEY,
    queue_id TEXT NOT NULL REFERENCES h_task_queues(queue_id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL CHECK (sequence >= 0),
    commit_oid TEXT NOT NULL,
    tree_oid TEXT NOT NULL,
    object_format TEXT NOT NULL CHECK (object_format IN ('sha1', 'sha256')),
    source_kind TEXT NOT NULL CHECK (source_kind IN ('BASELINE', 'TASK', 'COMPENSATION')),
    source_task_commit_id TEXT REFERENCES h_task_commits(task_commit_id),
    prior_head_id TEXT REFERENCES h_integration_heads(integration_head_id),
    created_at TEXT NOT NULL,
    UNIQUE (queue_id, sequence)
);

CREATE TABLE h_git_ref_operations (
    ref_operation_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES h_runs(run_id) ON DELETE CASCADE,
    queue_id TEXT NOT NULL REFERENCES h_task_queues(queue_id) ON DELETE CASCADE,
    operation_kind TEXT NOT NULL CHECK (operation_kind IN ('CREATE', 'ADVANCE', 'COMPENSATE', 'SET_VERIFIED')),
    ref_name TEXT NOT NULL,
    expected_old_oid TEXT,
    desired_new_oid TEXT NOT NULL,
    observed_oid TEXT,
    lease_fencing_token INTEGER NOT NULL CHECK (lease_fencing_token >= 1),
    state TEXT NOT NULL CHECK (state IN (
        'PREPARED', 'DISPATCHED', 'APPLIED', 'NOT_APPLIED', 'FAILED', 'UNCERTAIN'
    )),
    error_code TEXT,
    intent_sha256 TEXT NOT NULL CHECK (length(intent_sha256) = 64),
    created_at TEXT NOT NULL,
    dispatched_at TEXT,
    settled_at TEXT
);

CREATE TABLE h_integration_intents (
    integration_intent_id TEXT PRIMARY KEY,
    queue_id TEXT NOT NULL REFERENCES h_task_queues(queue_id) ON DELETE CASCADE,
    queue_item_id TEXT NOT NULL REFERENCES h_queue_items(queue_item_id),
    task_commit_id TEXT REFERENCES h_task_commits(task_commit_id),
    ref_operation_id TEXT NOT NULL UNIQUE REFERENCES h_git_ref_operations(ref_operation_id),
    strategy TEXT NOT NULL CHECK (strategy IN ('FAST_FORWARD_EXACT', 'REVERIFIED_REPLAY', 'COMPENSATE')),
    expected_head_oid TEXT NOT NULL,
    desired_head_oid TEXT NOT NULL,
    desired_tree_oid TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'PREPARED', 'APPLYING', 'APPLIED', 'VERIFYING',
        'SETTLED', 'REJECTED', 'FAILED', 'UNCERTAIN'
    )),
    intent_artifact_id TEXT NOT NULL REFERENCES h_artifacts(artifact_id),
    intent_sha256 TEXT NOT NULL CHECK (length(intent_sha256) = 64),
    created_at TEXT NOT NULL,
    settled_at TEXT
);

CREATE TABLE h_integration_results (
    integration_result_id TEXT PRIMARY KEY,
    integration_intent_id TEXT NOT NULL UNIQUE REFERENCES h_integration_intents(integration_intent_id) ON DELETE CASCADE,
    before_head_id TEXT NOT NULL REFERENCES h_integration_heads(integration_head_id),
    after_head_id TEXT REFERENCES h_integration_heads(integration_head_id),
    status TEXT NOT NULL CHECK (status IN (
        'APPLIED_PENDING_VERIFICATION', 'APPLIED_AND_VERIFIED', 'NOT_APPLIED',
        'COMPENSATED', 'FAILED', 'UNCERTAIN'
    )),
    result_artifact_id TEXT NOT NULL REFERENCES h_artifacts(artifact_id),
    result_sha256 TEXT NOT NULL CHECK (length(result_sha256) = 64),
    settled_at TEXT NOT NULL
);

CREATE TABLE h_integration_verifications (
    integration_verification_id TEXT PRIMARY KEY,
    queue_id TEXT NOT NULL REFERENCES h_task_queues(queue_id) ON DELETE CASCADE,
    integration_result_id TEXT REFERENCES h_integration_results(integration_result_id),
    integration_head_id TEXT NOT NULL REFERENCES h_integration_heads(integration_head_id),
    scope TEXT NOT NULL CHECK (scope IN ('POST_ADVANCE', 'FINAL_AGGREGATE')),
    contract_set_artifact_id TEXT NOT NULL REFERENCES h_artifacts(artifact_id),
    contract_set_sha256 TEXT NOT NULL CHECK (length(contract_set_sha256) = 64),
    status TEXT NOT NULL CHECK (status IN (
        'PASS', 'FAILED', 'UNVERIFIED', 'BLOCKED_ENVIRONMENT',
        'BUDGET_EXHAUSTED', 'NEEDS_INPUT', 'CANCELLED'
    )),
    report_artifact_id TEXT NOT NULL REFERENCES h_artifacts(artifact_id),
    report_sha256 TEXT NOT NULL CHECK (length(report_sha256) = 64),
    started_at TEXT NOT NULL,
    settled_at TEXT NOT NULL
);

CREATE UNIQUE INDEX h_integration_verification_post_idx
    ON h_integration_verifications(integration_result_id, scope)
    WHERE scope = 'POST_ADVANCE';

CREATE UNIQUE INDEX h_integration_verification_final_idx
    ON h_integration_verifications(queue_id, integration_head_id, scope)
    WHERE scope = 'FINAL_AGGREGATE';

CREATE TABLE h_evaluation_cases (
    evaluation_case_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES h_runs(run_id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES h_tasks(task_id) ON DELETE CASCADE,
    batch_id TEXT,
    case_ordinal INTEGER NOT NULL CHECK (case_ordinal >= 0),
    baseline_commit_oid TEXT NOT NULL,
    baseline_tree_oid TEXT NOT NULL,
    context_isolation_id TEXT NOT NULL,
    workspace_id TEXT,
    candidate_id TEXT REFERENCES h_candidate_snapshots(candidate_id),
    completion_decision_id TEXT REFERENCES h_completion_decisions(completion_decision_id),
    carried_state_from_case_id TEXT,
    status TEXT NOT NULL CHECK (status IN (
        'PENDING', 'RUNNING', 'PASS', 'FAILED', 'UNVERIFIED',
        'BLOCKED_ENVIRONMENT', 'BUDGET_EXHAUSTED', 'NEEDS_INPUT', 'CANCELLED'
    )),
    result_artifact_id TEXT REFERENCES h_artifacts(artifact_id),
    created_at TEXT NOT NULL,
    settled_at TEXT,
    CHECK (carried_state_from_case_id IS NULL),
    UNIQUE (run_id, case_ordinal)
);

CREATE TABLE h_queue_results (
    queue_result_id TEXT PRIMARY KEY,
    queue_id TEXT NOT NULL UNIQUE REFERENCES h_task_queues(queue_id) ON DELETE CASCADE,
    queue_version_id TEXT NOT NULL REFERENCES h_queue_versions(queue_version_id),
    final_integration_head_id TEXT REFERENCES h_integration_heads(integration_head_id),
    final_verification_id TEXT REFERENCES h_integration_verifications(integration_verification_id),
    status TEXT NOT NULL CHECK (status IN (
        'COMPLETED_ALL', 'PARTIAL_SUCCESS', 'FAILED', 'UNVERIFIED',
        'BLOCKED_ENVIRONMENT', 'BUDGET_EXHAUSTED', 'NEEDS_INPUT',
        'CANCELLED', 'INTEGRATION_UNCERTAIN', 'INVALID'
    )),
    selected_count INTEGER NOT NULL CHECK (selected_count >= 0),
    integrated_count INTEGER NOT NULL CHECK (integrated_count >= 0),
    failed_count INTEGER NOT NULL CHECK (failed_count >= 0),
    blocked_count INTEGER NOT NULL CHECK (blocked_count >= 0),
    remaining_count INTEGER NOT NULL CHECK (remaining_count >= 0),
    final_patch_input_artifact_id TEXT REFERENCES h_artifacts(artifact_id),
    report_artifact_id TEXT NOT NULL REFERENCES h_artifacts(artifact_id),
    result_sha256 TEXT NOT NULL CHECK (length(result_sha256) = 64),
    publication_authorized INTEGER NOT NULL DEFAULT 0 CHECK (publication_authorized = 0),
    settled_at TEXT NOT NULL
);

CREATE INDEX h_queue_items_state_idx ON h_queue_items(queue_version_id, state, priority, ordinal);
CREATE INDEX h_queue_edges_dependent_idx ON h_queue_edges(queue_version_id, dependent_item_id);
CREATE INDEX h_task_executions_task_idx ON h_task_executions(task_id, attempt_number, state);
CREATE INDEX h_task_worktrees_state_idx ON h_task_worktrees(task_execution_id, state);
CREATE INDEX h_git_ref_operations_state_idx ON h_git_ref_operations(queue_id, state, created_at);
CREATE INDEX h_integration_heads_queue_idx ON h_integration_heads(queue_id, sequence);
CREATE INDEX h_integration_intents_state_idx ON h_integration_intents(queue_id, state, created_at);
CREATE INDEX h_integration_verifications_status_idx ON h_integration_verifications(queue_id, scope, status);
CREATE INDEX h_evaluation_cases_status_idx ON h_evaluation_cases(run_id, status, case_ordinal);
