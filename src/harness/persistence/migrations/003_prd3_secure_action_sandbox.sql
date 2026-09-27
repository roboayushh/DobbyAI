-- PRD 3: policy admission, sandbox execution, workspace versions, settlement.
-- Executed by the migrator inside one explicit transaction with foreign keys
-- disabled; the migrator runs PRAGMA foreign_key_check before committing and
-- verifies that rebuilt lifecycle tables preserve every prior row exactly.

-- Lifecycle projections gain the execution, verification, and queue states
-- used by PRD 3-5. SQLite CHECK constraints cannot be altered in place.
CREATE TABLE h_run_lifecycle_v3 (
    run_id TEXT PRIMARY KEY REFERENCES h_runs(run_id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK (state IN (
        'PREPARED', 'INDEXING', 'PLANNING', 'PLAN_READY', 'CODING',
        'REPLANNING', 'ACTION_PROPOSED', 'VERIFICATION_REQUIRED',
        'NEEDS_INPUT', 'NEEDS_CAPABILITY', 'BUDGET_EXHAUSTED',
        'FAILED', 'CANCELLED',
        'ACTION_EXECUTING', 'NEEDS_APPROVAL', 'BLOCKED_ENVIRONMENT', 'ACTION_UNKNOWN',
        'VERIFYING', 'REPAIRING', 'READY_FOR_REVIEW', 'VERIFICATION_FAILED', 'UNVERIFIED',
        'QUEUE_SETTLED'
    )),
    active_task_id TEXT REFERENCES h_tasks(task_id),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    stop_reason_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
INSERT INTO h_run_lifecycle_v3 (run_id, state, active_task_id, version, stop_reason_code, created_at, updated_at)
SELECT run_id, state, active_task_id, version, stop_reason_code, created_at, updated_at FROM h_run_lifecycle;
DROP TABLE h_run_lifecycle;
ALTER TABLE h_run_lifecycle_v3 RENAME TO h_run_lifecycle;

DROP INDEX IF EXISTS h_task_lifecycle_state_idx;
CREATE TABLE h_task_lifecycle_v3 (
    task_id TEXT PRIMARY KEY REFERENCES h_tasks(task_id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK (state IN (
        'QUEUED', 'INDEXING', 'PLANNING', 'PLANNED', 'CODING',
        'ACTION_PROPOSED', 'VERIFICATION_REQUIRED', 'NEEDS_INPUT',
        'NEEDS_CAPABILITY', 'BUDGET_EXHAUSTED', 'FAILED', 'CANCELLED',
        'ACTION_EXECUTING', 'NEEDS_APPROVAL', 'BLOCKED_ENVIRONMENT', 'ACTION_UNKNOWN',
        'VERIFYING', 'REPAIRING', 'READY_FOR_REVIEW', 'VERIFICATION_FAILED', 'UNVERIFIED',
        'SKIPPED'
    )),
    task_revision INTEGER NOT NULL DEFAULT 1 CHECK (task_revision >= 1),
    active_plan_revision INTEGER,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    updated_at TEXT NOT NULL
);
INSERT INTO h_task_lifecycle_v3 (task_id, state, task_revision, active_plan_revision, version, updated_at)
SELECT task_id, state, task_revision, active_plan_revision, version, updated_at FROM h_task_lifecycle;
DROP TABLE h_task_lifecycle;
ALTER TABLE h_task_lifecycle_v3 RENAME TO h_task_lifecycle;
CREATE INDEX h_task_lifecycle_state_idx ON h_task_lifecycle(state, updated_at);

CREATE TABLE h_policy_snapshots (
    policy_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES h_runs(run_id) ON DELETE CASCADE,
    version INTEGER NOT NULL CHECK (version >= 1),
    profile TEXT NOT NULL CHECK (profile IN ('guided', 'sandbox', 'delegated')),
    policy_json TEXT NOT NULL,
    policy_sha256 TEXT NOT NULL CHECK (length(policy_sha256) = 64),
    revoked INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0, 1)),
    expires_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, version),
    UNIQUE (run_id, policy_sha256)
);

CREATE TABLE h_runtime_profiles (
    runtime_profile_row_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES h_runs(run_id) ON DELETE CASCADE,
    runtime_profile_id TEXT NOT NULL,
    runtime_name TEXT NOT NULL,
    runtime_version TEXT NOT NULL,
    image_reference TEXT NOT NULL,
    image_digest TEXT NOT NULL CHECK (length(image_digest) = 64),
    worker_version TEXT NOT NULL,
    tool_library_version TEXT NOT NULL,
    limits_json TEXT NOT NULL,
    profile_fingerprint TEXT NOT NULL CHECK (length(profile_fingerprint) = 64),
    created_at TEXT NOT NULL,
    UNIQUE (run_id, runtime_profile_id),
    UNIQUE (run_id, profile_fingerprint)
);

CREATE TABLE h_dependency_environments (
    dependency_environment_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES h_runs(run_id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES h_tasks(task_id) ON DELETE CASCADE,
    runtime_profile_row_id TEXT NOT NULL REFERENCES h_runtime_profiles(runtime_profile_row_id),
    source_revision TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
    lockfile_sha256 TEXT,
    setup_policy_sha256 TEXT NOT NULL CHECK (length(setup_policy_sha256) = 64),
    cache_key_sha256 TEXT NOT NULL CHECK (length(cache_key_sha256) = 64),
    environment_sha256 TEXT,
    environment_relpath TEXT,
    setup_log_artifact_id TEXT REFERENCES h_artifacts(artifact_id),
    state TEXT NOT NULL CHECK (state IN ('REQUESTED', 'BUILDING', 'READY', 'FAILED', 'BLOCKED', 'DISCARDED')),
    failure_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (run_id, task_id, cache_key_sha256)
);

CREATE TABLE h_execution_budgets (
    run_id TEXT PRIMARY KEY REFERENCES h_runs(run_id) ON DELETE CASCADE,
    max_actions INTEGER NOT NULL CHECK (max_actions >= 0),
    max_wall_seconds INTEGER NOT NULL CHECK (max_wall_seconds >= 0),
    max_output_bytes INTEGER NOT NULL CHECK (max_output_bytes >= 0),
    max_workspace_growth_bytes INTEGER NOT NULL CHECK (max_workspace_growth_bytes >= 0),
    used_actions INTEGER NOT NULL DEFAULT 0 CHECK (used_actions >= 0),
    reserved_actions INTEGER NOT NULL DEFAULT 0 CHECK (reserved_actions >= 0),
    used_wall_seconds INTEGER NOT NULL DEFAULT 0 CHECK (used_wall_seconds >= 0),
    reserved_wall_seconds INTEGER NOT NULL DEFAULT 0 CHECK (reserved_wall_seconds >= 0),
    used_output_bytes INTEGER NOT NULL DEFAULT 0 CHECK (used_output_bytes >= 0),
    reserved_output_bytes INTEGER NOT NULL DEFAULT 0 CHECK (reserved_output_bytes >= 0),
    used_workspace_growth_bytes INTEGER NOT NULL DEFAULT 0 CHECK (used_workspace_growth_bytes >= 0),
    updated_at TEXT NOT NULL,
    CHECK (used_actions + reserved_actions <= max_actions),
    CHECK (used_wall_seconds + reserved_wall_seconds <= max_wall_seconds),
    CHECK (used_output_bytes + reserved_output_bytes <= max_output_bytes),
    CHECK (used_workspace_growth_bytes <= max_workspace_growth_bytes)
);

-- Writable, host-registered task workspace. Git metadata stays in the run's
-- private bare repository; only the checkout directory is ever mounted.
CREATE TABLE h_task_workspaces (
    task_id TEXT PRIMARY KEY REFERENCES h_tasks(task_id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES h_runs(run_id) ON DELETE CASCADE,
    root_relpath TEXT NOT NULL UNIQUE,
    task_start_commit TEXT NOT NULL,
    task_start_tree TEXT NOT NULL,
    object_format TEXT NOT NULL CHECK (object_format IN ('sha1', 'sha256')),
    current_version_id TEXT,
    state TEXT NOT NULL CHECK (state IN ('ACTIVE', 'QUARANTINED', 'RETIRED')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE h_workspace_versions (
    workspace_version_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES h_runs(run_id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES h_tasks(task_id) ON DELETE CASCADE,
    parent_version_id TEXT REFERENCES h_workspace_versions(workspace_version_id),
    baseline_commit TEXT NOT NULL,
    private_checkpoint_commit TEXT NOT NULL,
    git_tree TEXT NOT NULL,
    content_tree_sha256 TEXT NOT NULL CHECK (length(content_tree_sha256) = 64),
    manifest_artifact_id TEXT NOT NULL REFERENCES h_artifacts(artifact_id),
    created_by_action_id TEXT,
    state TEXT NOT NULL CHECK (state IN ('ACTIVE', 'SUPERSEDED', 'ROLLED_BACK', 'FROZEN', 'QUARANTINED')),
    created_at TEXT NOT NULL,
    UNIQUE (run_id, task_id, private_checkpoint_commit)
);

CREATE UNIQUE INDEX h_workspace_versions_one_active_idx
    ON h_workspace_versions(run_id, task_id)
    WHERE state = 'ACTIVE';

CREATE TABLE h_workspace_locks (
    task_id TEXT PRIMARY KEY REFERENCES h_tasks(task_id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES h_runs(run_id) ON DELETE CASCADE,
    workspace_version_id TEXT NOT NULL REFERENCES h_workspace_versions(workspace_version_id),
    owner_id TEXT NOT NULL,
    purpose TEXT NOT NULL,
    lease_token_sha256 TEXT NOT NULL CHECK (length(lease_token_sha256) = 64),
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE h_actions (
    action_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES h_runs(run_id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES h_tasks(task_id) ON DELETE CASCADE,
    action_proposal_id TEXT NOT NULL UNIQUE REFERENCES h_action_proposals(action_proposal_id),
    coder_call_id TEXT NOT NULL REFERENCES h_model_calls(call_id),
    plan_id TEXT NOT NULL REFERENCES h_plans(plan_id),
    task_revision INTEGER NOT NULL CHECK (task_revision >= 1),
    plan_revision INTEGER NOT NULL CHECK (plan_revision >= 1),
    lifecycle_version INTEGER NOT NULL CHECK (lifecycle_version >= 1),
    before_workspace_version_id TEXT NOT NULL REFERENCES h_workspace_versions(workspace_version_id),
    checkpoint_commit TEXT,
    code_artifact_id TEXT NOT NULL REFERENCES h_artifacts(artifact_id),
    code_sha256 TEXT NOT NULL CHECK (length(code_sha256) = 64),
    policy_id TEXT NOT NULL REFERENCES h_policy_snapshots(policy_id),
    policy_decision_sha256 TEXT NOT NULL CHECK (length(policy_decision_sha256) = 64),
    decision_json TEXT NOT NULL,
    runtime_profile_row_id TEXT NOT NULL REFERENCES h_runtime_profiles(runtime_profile_row_id),
    dependency_environment_id TEXT REFERENCES h_dependency_environments(dependency_environment_id),
    read_only INTEGER NOT NULL DEFAULT 0 CHECK (read_only IN (0, 1)),
    state TEXT NOT NULL CHECK (state IN (
        'PROPOSED', 'NEEDS_APPROVAL', 'ADMITTED', 'INTENT', 'RUNNING',
        'SETTLING', 'SUCCEEDED', 'FAILED', 'POLICY_VIOLATION',
        'CANCELLED', 'UNKNOWN', 'DENIED', 'STALE'
    )),
    failure_signature_sha256 TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE h_action_capabilities (
    action_id TEXT NOT NULL REFERENCES h_actions(action_id) ON DELETE CASCADE,
    capability_name TEXT NOT NULL,
    capability_version TEXT NOT NULL,
    mutates_workspace INTEGER NOT NULL CHECK (mutates_workspace IN (0, 1)),
    parameters_sha256 TEXT NOT NULL CHECK (length(parameters_sha256) = 64),
    PRIMARY KEY (action_id, capability_name)
);

-- Named with an h_action_ prefix so the PRD 6 publication approval tables can
-- use the generic names without a conflicting schema.
CREATE TABLE h_action_approval_requests (
    approval_request_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES h_runs(run_id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES h_tasks(task_id) ON DELETE CASCADE,
    action_id TEXT NOT NULL UNIQUE REFERENCES h_actions(action_id) ON DELETE CASCADE,
    binding_json TEXT NOT NULL,
    binding_sha256 TEXT NOT NULL CHECK (length(binding_sha256) = 64),
    consequence_artifact_id TEXT NOT NULL REFERENCES h_artifacts(artifact_id),
    state TEXT NOT NULL CHECK (state IN ('PENDING', 'APPROVED', 'DENIED', 'EXPIRED', 'REVOKED', 'CONSUMED')),
    max_uses INTEGER NOT NULL DEFAULT 1 CHECK (max_uses >= 1),
    used_count INTEGER NOT NULL DEFAULT 0 CHECK (used_count >= 0 AND used_count <= max_uses),
    denial_reason TEXT,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE h_action_approval_grants (
    grant_id TEXT PRIMARY KEY,
    approval_request_id TEXT NOT NULL UNIQUE REFERENCES h_action_approval_requests(approval_request_id) ON DELETE CASCADE,
    binding_sha256 TEXT NOT NULL CHECK (length(binding_sha256) = 64),
    approved_by TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('ACTIVE', 'CONSUMED', 'REVOKED', 'EXPIRED')),
    approved_at TEXT NOT NULL,
    consumed_at TEXT
);

CREATE TABLE h_sandbox_instances (
    sandbox_instance_id TEXT PRIMARY KEY,
    action_id TEXT NOT NULL REFERENCES h_actions(action_id) ON DELETE CASCADE,
    attempt_no INTEGER NOT NULL CHECK (attempt_no >= 1),
    engine TEXT NOT NULL,
    engine_version TEXT NOT NULL,
    engine_object_id TEXT,
    container_name TEXT NOT NULL,
    host_architecture TEXT NOT NULL,
    image_digest TEXT NOT NULL CHECK (length(image_digest) = 64),
    container_id_hash TEXT CHECK (container_id_hash IS NULL OR length(container_id_hash) = 64),
    settings_sha256 TEXT NOT NULL CHECK (length(settings_sha256) = 64),
    network_mode TEXT NOT NULL CHECK (network_mode IN ('none', 'setup_scoped', 'action_scoped')),
    state TEXT NOT NULL CHECK (state IN ('CREATED', 'RUNNING', 'STOPPING', 'STOPPED', 'REMOVED', 'UNKNOWN')),
    started_at TEXT,
    stopped_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (action_id, attempt_no)
);

CREATE TABLE h_tool_calls (
    tool_call_id TEXT PRIMARY KEY,
    action_id TEXT NOT NULL REFERENCES h_actions(action_id) ON DELETE CASCADE,
    sandbox_instance_id TEXT NOT NULL REFERENCES h_sandbox_instances(sandbox_instance_id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    tool_name TEXT NOT NULL,
    tool_version TEXT NOT NULL,
    capability_name TEXT NOT NULL,
    arguments_summary_json TEXT NOT NULL,
    result_summary_json TEXT,
    state TEXT NOT NULL CHECK (state IN ('STARTED', 'SUCCEEDED', 'FAILED', 'TRUNCATED', 'REJECTED')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE (action_id, sequence)
);

CREATE TABLE h_execution_results (
    execution_result_id TEXT PRIMARY KEY,
    action_id TEXT NOT NULL UNIQUE REFERENCES h_actions(action_id) ON DELETE CASCADE,
    settlement TEXT NOT NULL CHECK (settlement IN (
        'ACCEPTED', 'NO_CHANGE', 'FAILED_ROLLED_BACK',
        'POLICY_VIOLATION_ROLLED_BACK', 'CANCELLED_ROLLED_BACK', 'UNKNOWN'
    )),
    before_workspace_version_id TEXT NOT NULL REFERENCES h_workspace_versions(workspace_version_id),
    after_workspace_version_id TEXT REFERENCES h_workspace_versions(workspace_version_id),
    exit_code INTEGER,
    signal INTEGER,
    timed_out INTEGER NOT NULL CHECK (timed_out IN (0, 1)),
    oom_killed INTEGER NOT NULL CHECK (oom_killed IN (0, 1)),
    output_limit_exceeded INTEGER NOT NULL CHECK (output_limit_exceeded IN (0, 1)),
    elapsed_ms INTEGER NOT NULL CHECK (elapsed_ms >= 0),
    change_set_sha256 TEXT CHECK (change_set_sha256 IS NULL OR length(change_set_sha256) = 64),
    stdout_artifact_id TEXT REFERENCES h_artifacts(artifact_id),
    stderr_artifact_id TEXT REFERENCES h_artifacts(artifact_id),
    tool_events_artifact_id TEXT REFERENCES h_artifacts(artifact_id),
    diff_artifact_id TEXT REFERENCES h_artifacts(artifact_id),
    worker_result_artifact_id TEXT REFERENCES h_artifacts(artifact_id),
    result_artifact_id TEXT REFERENCES h_artifacts(artifact_id),
    reason_codes_json TEXT NOT NULL DEFAULT '[]',
    result_json TEXT NOT NULL,
    settled_at TEXT NOT NULL
);

CREATE TABLE h_file_changes (
    file_change_id TEXT PRIMARY KEY,
    execution_result_id TEXT NOT NULL REFERENCES h_execution_results(execution_result_id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL,
    change_type TEXT NOT NULL CHECK (change_type IN ('CREATED', 'MODIFIED', 'DELETED', 'RENAMED', 'MODE_CHANGED', 'SYMLINK_CHANGED', 'SPECIAL', 'DISPOSABLE')),
    old_path TEXT,
    before_sha256 TEXT,
    after_sha256 TEXT,
    before_mode TEXT,
    after_mode TEXT,
    byte_delta INTEGER NOT NULL,
    declared INTEGER NOT NULL CHECK (declared IN (0, 1)),
    policy_state TEXT NOT NULL CHECK (policy_state IN ('ALLOWED', 'UNEXPECTED', 'DENIED', 'DISPOSABLE')),
    UNIQUE (execution_result_id, relative_path, change_type)
);

CREATE TABLE h_candidate_snapshots (
    candidate_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES h_runs(run_id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES h_tasks(task_id) ON DELETE CASCADE,
    workspace_version_id TEXT NOT NULL REFERENCES h_workspace_versions(workspace_version_id),
    baseline_commit TEXT NOT NULL,
    task_start_commit TEXT NOT NULL,
    candidate_commit TEXT NOT NULL,
    candidate_tree TEXT NOT NULL,
    candidate_sha256 TEXT NOT NULL CHECK (length(candidate_sha256) = 64),
    manifest_artifact_id TEXT NOT NULL REFERENCES h_artifacts(artifact_id),
    diff_artifact_id TEXT NOT NULL REFERENCES h_artifacts(artifact_id),
    handoff_artifact_id TEXT REFERENCES h_artifacts(artifact_id),
    frozen INTEGER NOT NULL CHECK (frozen IN (0, 1)),
    verification_status TEXT NOT NULL CHECK (verification_status IN ('NOT_RUN', 'INVALIDATED')),
    candidate_ordinal INTEGER NOT NULL CHECK (candidate_ordinal >= 1),
    created_at TEXT NOT NULL,
    UNIQUE (run_id, task_id, candidate_ordinal)
);

CREATE INDEX h_policy_snapshots_active_idx ON h_policy_snapshots(run_id, revoked, version);
CREATE INDEX h_dependency_environments_state_idx ON h_dependency_environments(run_id, task_id, state);
CREATE INDEX h_workspace_versions_active_idx ON h_workspace_versions(run_id, task_id, state, created_at);
CREATE INDEX h_actions_state_idx ON h_actions(run_id, task_id, state, created_at);
CREATE INDEX h_action_approval_requests_state_idx ON h_action_approval_requests(run_id, state, expires_at);
CREATE INDEX h_sandbox_instances_state_idx ON h_sandbox_instances(action_id, state);
CREATE INDEX h_tool_calls_action_idx ON h_tool_calls(action_id, sequence);
CREATE INDEX h_file_changes_path_idx ON h_file_changes(relative_path, change_type);
CREATE INDEX h_candidate_snapshots_task_idx ON h_candidate_snapshots(task_id, created_at);
