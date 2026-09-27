-- PRD 2: model, context, retrieval, budget, and orchestration persistence.
PRAGMA foreign_keys = OFF;

DROP INDEX IF EXISTS h_artifacts_run_kind_idx;

CREATE TABLE h_artifacts_v2 (
    artifact_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES h_runs(run_id) ON DELETE CASCADE,
    task_id TEXT REFERENCES h_tasks(task_id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (length(kind) BETWEEN 1 AND 64),
    relative_path TEXT NOT NULL,
    media_type TEXT NOT NULL,
    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    created_at TEXT NOT NULL,
    UNIQUE (run_id, relative_path)
);

INSERT INTO h_artifacts_v2 (
    artifact_id, run_id, task_id, kind, relative_path,
    media_type, byte_size, sha256, created_at
)
SELECT
    artifact_id, run_id, task_id, kind, relative_path,
    media_type, byte_size, sha256, created_at
FROM h_artifacts;

DROP TABLE h_artifacts;
ALTER TABLE h_artifacts_v2 RENAME TO h_artifacts;
CREATE INDEX h_artifacts_run_kind_idx ON h_artifacts(run_id, kind);

CREATE TABLE h_run_lifecycle (
    run_id TEXT PRIMARY KEY REFERENCES h_runs(run_id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK (state IN (
        'PREPARED', 'INDEXING', 'PLANNING', 'PLAN_READY', 'CODING',
        'REPLANNING', 'ACTION_PROPOSED', 'VERIFICATION_REQUIRED',
        'NEEDS_INPUT', 'NEEDS_CAPABILITY', 'BUDGET_EXHAUSTED',
        'FAILED', 'CANCELLED'
    )),
    active_task_id TEXT REFERENCES h_tasks(task_id),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    stop_reason_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE h_task_lifecycle (
    task_id TEXT PRIMARY KEY REFERENCES h_tasks(task_id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK (state IN (
        'QUEUED', 'INDEXING', 'PLANNING', 'PLANNED', 'CODING',
        'ACTION_PROPOSED', 'VERIFICATION_REQUIRED', 'NEEDS_INPUT',
        'NEEDS_CAPABILITY', 'BUDGET_EXHAUSTED', 'FAILED', 'CANCELLED'
    )),
    task_revision INTEGER NOT NULL DEFAULT 1 CHECK (task_revision >= 1),
    active_plan_revision INTEGER,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    updated_at TEXT NOT NULL
);

CREATE TABLE h_orchestration_leases (
    run_id TEXT PRIMARY KEY REFERENCES h_runs(run_id) ON DELETE CASCADE,
    owner_id TEXT NOT NULL,
    lease_token_sha256 TEXT NOT NULL CHECK (length(lease_token_sha256) = 64),
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    lifecycle_version INTEGER NOT NULL CHECK (lifecycle_version >= 1)
);

CREATE TABLE h_run_model_config (
    run_id TEXT PRIMARY KEY REFERENCES h_runs(run_id) ON DELETE CASCADE,
    profile_id TEXT NOT NULL,
    protocol TEXT NOT NULL,
    endpoint_origin TEXT NOT NULL,
    model_id TEXT NOT NULL,
    profile_fingerprint TEXT NOT NULL CHECK (length(profile_fingerprint) = 64),
    adapter_name TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    tokenizer_id TEXT NOT NULL,
    context_window_tokens INTEGER NOT NULL CHECK (context_window_tokens > 0),
    max_output_tokens INTEGER NOT NULL CHECK (max_output_tokens > 0),
    safety_margin_tokens INTEGER NOT NULL CHECK (safety_margin_tokens >= 0),
    sampling_json TEXT NOT NULL,
    credential_env_name TEXT NOT NULL CHECK (credential_env_name = 'AI_API_KEY'),
    created_at TEXT NOT NULL
);

CREATE TABLE h_budget_ledgers (
    run_id TEXT PRIMARY KEY REFERENCES h_runs(run_id) ON DELETE CASCADE,
    max_calls INTEGER NOT NULL CHECK (max_calls >= 0),
    max_input_tokens INTEGER NOT NULL CHECK (max_input_tokens >= 0),
    max_output_tokens INTEGER NOT NULL CHECK (max_output_tokens >= 0),
    max_wall_seconds INTEGER NOT NULL CHECK (max_wall_seconds >= 0),
    reserved_future_calls INTEGER NOT NULL CHECK (reserved_future_calls >= 0),
    reserved_future_output_tokens INTEGER NOT NULL CHECK (reserved_future_output_tokens >= 0),
    reserved_future_wall_seconds INTEGER NOT NULL CHECK (reserved_future_wall_seconds >= 0),
    used_calls INTEGER NOT NULL DEFAULT 0 CHECK (used_calls >= 0),
    used_input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (used_input_tokens >= 0),
    used_output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (used_output_tokens >= 0),
    reserved_calls INTEGER NOT NULL DEFAULT 0 CHECK (reserved_calls >= 0),
    reserved_input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (reserved_input_tokens >= 0),
    reserved_output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (reserved_output_tokens >= 0),
    started_at TEXT NOT NULL,
    deadline_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (used_calls + reserved_calls <= max_calls),
    CHECK (used_input_tokens + reserved_input_tokens <= max_input_tokens),
    CHECK (used_output_tokens + reserved_output_tokens <= max_output_tokens)
);

CREATE TABLE h_file_index (
    file_index_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES h_runs(run_id) ON DELETE CASCADE,
    source_revision TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
    language TEXT,
    parser_name TEXT,
    parser_version TEXT,
    parse_state TEXT NOT NULL CHECK (parse_state IN ('PARSED', 'TEXT_ONLY', 'EXCLUDED', 'ERROR')),
    exclusion_reason TEXT,
    valid INTEGER NOT NULL DEFAULT 1 CHECK (valid IN (0, 1)),
    indexed_at TEXT NOT NULL,
    UNIQUE (run_id, source_revision, relative_path)
);

CREATE TABLE h_symbols (
    symbol_id TEXT PRIMARY KEY,
    file_index_id TEXT NOT NULL REFERENCES h_file_index(file_index_id) ON DELETE CASCADE,
    symbol_kind TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    signature TEXT,
    start_line INTEGER NOT NULL CHECK (start_line >= 1),
    end_line INTEGER NOT NULL CHECK (end_line >= start_line),
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    UNIQUE (file_index_id, symbol_kind, qualified_name, start_line)
);

CREATE TABLE h_evidence (
    evidence_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES h_runs(run_id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES h_tasks(task_id) ON DELETE CASCADE,
    source_revision TEXT NOT NULL,
    relative_path TEXT,
    start_line INTEGER,
    end_line INTEGER,
    symbol TEXT,
    evidence_type TEXT NOT NULL,
    retrieval_reason TEXT NOT NULL,
    truth_status TEXT NOT NULL CHECK (truth_status IN ('observed', 'reported', 'hypothesis')),
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    provenance_json TEXT NOT NULL,
    artifact_id TEXT REFERENCES h_artifacts(artifact_id),
    valid INTEGER NOT NULL DEFAULT 1 CHECK (valid IN (0, 1)),
    invalidated_at TEXT,
    invalidation_reason TEXT,
    created_at TEXT NOT NULL,
    CHECK ((start_line IS NULL AND end_line IS NULL) OR
           (start_line >= 1 AND end_line >= start_line))
);

CREATE TABLE h_summaries (
    summary_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES h_runs(run_id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES h_tasks(task_id) ON DELETE CASCADE,
    source_revision TEXT NOT NULL,
    method TEXT NOT NULL CHECK (method IN ('deterministic', 'model_assisted')),
    method_version TEXT NOT NULL,
    input_refs_json TEXT NOT NULL,
    artifact_id TEXT NOT NULL REFERENCES h_artifacts(artifact_id),
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    valid INTEGER NOT NULL DEFAULT 1 CHECK (valid IN (0, 1)),
    invalidated_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE h_context_packets (
    packet_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES h_runs(run_id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES h_tasks(task_id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('planner', 'coder', 'validator', 'summarizer')),
    purpose TEXT NOT NULL,
    packet_revision INTEGER NOT NULL CHECK (packet_revision >= 1),
    source_revision TEXT NOT NULL,
    candidate_hash TEXT,
    artifact_id TEXT NOT NULL REFERENCES h_artifacts(artifact_id),
    packet_sha256 TEXT NOT NULL CHECK (length(packet_sha256) = 64),
    estimated_input_tokens INTEGER NOT NULL CHECK (estimated_input_tokens >= 0),
    reserved_output_tokens INTEGER NOT NULL CHECK (reserved_output_tokens >= 0),
    safety_margin_tokens INTEGER NOT NULL CHECK (safety_margin_tokens >= 0),
    counter_mode TEXT NOT NULL CHECK (counter_mode IN ('tokenizer', 'conservative_estimate')),
    profile_fingerprint TEXT NOT NULL CHECK (length(profile_fingerprint) = 64),
    created_at TEXT NOT NULL,
    UNIQUE (run_id, task_id, role, purpose, packet_revision)
);

CREATE TABLE h_context_items (
    context_item_id TEXT PRIMARY KEY,
    packet_id TEXT NOT NULL REFERENCES h_context_packets(packet_id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    section_name TEXT NOT NULL,
    content_kind TEXT NOT NULL,
    evidence_id TEXT REFERENCES h_evidence(evidence_id),
    summary_id TEXT REFERENCES h_summaries(summary_id),
    artifact_id TEXT REFERENCES h_artifacts(artifact_id),
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    source_revision TEXT,
    estimated_tokens INTEGER NOT NULL CHECK (estimated_tokens >= 0),
    pinned INTEGER NOT NULL CHECK (pinned IN (0, 1)),
    UNIQUE (packet_id, ordinal),
    CHECK (
        (CASE WHEN evidence_id IS NOT NULL THEN 1 ELSE 0 END) +
        (CASE WHEN summary_id IS NOT NULL THEN 1 ELSE 0 END) +
        (CASE WHEN artifact_id IS NOT NULL THEN 1 ELSE 0 END) <= 1
    )
);

CREATE TABLE h_model_calls (
    call_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES h_runs(run_id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES h_tasks(task_id) ON DELETE CASCADE,
    packet_id TEXT NOT NULL REFERENCES h_context_packets(packet_id),
    role TEXT NOT NULL CHECK (role IN ('planner', 'coder', 'validator', 'summarizer')),
    attempt_no INTEGER NOT NULL CHECK (attempt_no >= 1),
    state TEXT NOT NULL CHECK (state IN (
        'INTENT', 'IN_FLIGHT', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'UNKNOWN'
    )),
    profile_fingerprint TEXT NOT NULL CHECK (length(profile_fingerprint) = 64),
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    response_artifact_id TEXT REFERENCES h_artifacts(artifact_id),
    parsed_output_sha256 TEXT,
    provider_request_id TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    usage_source TEXT CHECK (usage_source IN ('provider_reported', 'estimated', 'unknown')),
    latency_ms INTEGER CHECK (latency_ms IS NULL OR latency_ms >= 0),
    error_code TEXT,
    started_at TEXT,
    settled_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (packet_id, attempt_no)
);

CREATE TABLE h_budget_reservations (
    reservation_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES h_runs(run_id) ON DELETE CASCADE,
    call_id TEXT NOT NULL UNIQUE REFERENCES h_model_calls(call_id) ON DELETE CASCADE,
    reserved_calls INTEGER NOT NULL CHECK (reserved_calls = 1),
    reserved_input_tokens INTEGER NOT NULL CHECK (reserved_input_tokens >= 0),
    reserved_output_tokens INTEGER NOT NULL CHECK (reserved_output_tokens >= 0),
    state TEXT NOT NULL CHECK (state IN ('RESERVED', 'SETTLED', 'RELEASED', 'CONSUMED_UNKNOWN')),
    created_at TEXT NOT NULL,
    settled_at TEXT
);

CREATE TABLE h_plans (
    plan_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES h_runs(run_id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES h_tasks(task_id) ON DELETE CASCADE,
    task_revision INTEGER NOT NULL CHECK (task_revision >= 1),
    plan_revision INTEGER NOT NULL CHECK (plan_revision >= 1),
    source_revision TEXT NOT NULL,
    planner_call_id TEXT NOT NULL REFERENCES h_model_calls(call_id),
    artifact_id TEXT NOT NULL REFERENCES h_artifacts(artifact_id),
    plan_sha256 TEXT NOT NULL CHECK (length(plan_sha256) = 64),
    state TEXT NOT NULL CHECK (state IN ('ACTIVE', 'SUPERSEDED', 'INVALIDATED')),
    created_at TEXT NOT NULL,
    UNIQUE (task_id, task_revision, plan_revision)
);

CREATE TABLE h_action_proposals (
    action_proposal_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES h_runs(run_id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES h_tasks(task_id) ON DELETE CASCADE,
    task_revision INTEGER NOT NULL CHECK (task_revision >= 1),
    lifecycle_version INTEGER NOT NULL CHECK (lifecycle_version >= 1),
    plan_id TEXT NOT NULL REFERENCES h_plans(plan_id),
    plan_revision INTEGER NOT NULL CHECK (plan_revision >= 1),
    workspace_version TEXT NOT NULL,
    coder_call_id TEXT NOT NULL UNIQUE REFERENCES h_model_calls(call_id),
    proposal_artifact_id TEXT NOT NULL REFERENCES h_artifacts(artifact_id),
    code_artifact_id TEXT NOT NULL REFERENCES h_artifacts(artifact_id),
    code_sha256 TEXT NOT NULL CHECK (length(code_sha256) = 64),
    requested_capabilities_json TEXT NOT NULL,
    declared_paths_json TEXT NOT NULL,
    requested_timeout_seconds INTEGER NOT NULL CHECK (requested_timeout_seconds > 0),
    authorization_policy_sha256 TEXT NOT NULL CHECK (length(authorization_policy_sha256) = 64),
    state TEXT NOT NULL CHECK (state IN ('UNEXECUTED', 'ADMITTED', 'REJECTED', 'STALE')),
    created_at TEXT NOT NULL
);

CREATE INDEX h_task_lifecycle_state_idx ON h_task_lifecycle(state, updated_at);
CREATE INDEX h_file_index_lookup_idx ON h_file_index(run_id, source_revision, valid, relative_path);
CREATE INDEX h_symbols_name_idx ON h_symbols(qualified_name, symbol_kind);
CREATE INDEX h_evidence_lookup_idx ON h_evidence(task_id, valid, evidence_type, source_revision);
CREATE INDEX h_context_packets_role_idx ON h_context_packets(run_id, task_id, role, created_at);
CREATE INDEX h_model_calls_state_idx ON h_model_calls(run_id, state, created_at);
CREATE INDEX h_plans_active_idx ON h_plans(task_id, state, plan_revision);
CREATE INDEX h_action_proposals_state_idx ON h_action_proposals(run_id, task_id, state, created_at);

PRAGMA foreign_keys = ON;
