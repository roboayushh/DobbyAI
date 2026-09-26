-- PRD 1 Initial Schema Migration
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS h_schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS h_runs (
    run_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    task_mode TEXT NOT NULL CHECK (task_mode IN ('single_issue', 'repository')),
    execution_mode TEXT NOT NULL CHECK (execution_mode IN ('development', 'evaluation')),
    state TEXT NOT NULL CHECK (state IN (
        'NEW', 'VALIDATING', 'ACQUIRING', 'PREPARING', 'PREPARED',
        'BLOCKED', 'FAILED', 'CANCELLED'
    )),
    request_json TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    runtime_profile TEXT NOT NULL,
    evaluation_profile TEXT,
    next_event_seq INTEGER NOT NULL DEFAULT 1 CHECK (next_event_seq >= 1),
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS h_source_snapshots (
    source_snapshot_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES h_runs(run_id) ON DELETE CASCADE,
    source_kind TEXT NOT NULL CHECK (source_kind IN ('local_git', 'public_https')),
    canonical_locator TEXT NOT NULL,
    upstream_commit TEXT,
    baseline_commit TEXT NOT NULL,
    baseline_tree TEXT NOT NULL,
    content_tree_sha256 TEXT NOT NULL CHECK (length(content_tree_sha256) = 64),
    import_manifest_sha256 TEXT NOT NULL CHECK (length(import_manifest_sha256) = 64),
    dirty_source_imported INTEGER NOT NULL CHECK (dirty_source_imported IN (0, 1)),
    repo_bytes INTEGER NOT NULL CHECK (repo_bytes >= 0),
    file_count INTEGER NOT NULL CHECK (file_count >= 0),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS h_workspaces (
    workspace_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES h_runs(run_id) ON DELETE CASCADE,
    source_snapshot_id TEXT NOT NULL UNIQUE REFERENCES h_source_snapshots(source_snapshot_id),
    root_relpath TEXT NOT NULL UNIQUE,
    bare_repo_relpath TEXT NOT NULL UNIQUE,
    worktree_relpath TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (state IN ('STAGING', 'READY', 'QUARANTINED', 'REMOVED')),
    writable INTEGER NOT NULL DEFAULT 0 CHECK (writable IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS h_tasks (
    task_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES h_runs(run_id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    source_type TEXT NOT NULL CHECK (source_type IN ('github_issue', 'direct_text')),
    source_key TEXT NOT NULL,
    source_snapshot_id TEXT,
    raw_content_sha256 TEXT NOT NULL CHECK (length(raw_content_sha256) = 64),
    normalized_content_sha256 TEXT NOT NULL CHECK (length(normalized_content_sha256) = 64),
    task_spec_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('QUEUED', 'BLOCKED', 'CANCELLED')),
    created_at TEXT NOT NULL,
    UNIQUE (run_id, ordinal),
    UNIQUE (run_id, source_key)
);

CREATE TABLE IF NOT EXISTS h_task_dependencies (
    task_id TEXT NOT NULL REFERENCES h_tasks(task_id) ON DELETE CASCADE,
    depends_on_task_id TEXT NOT NULL REFERENCES h_tasks(task_id) ON DELETE CASCADE,
    dependency_type TEXT NOT NULL DEFAULT 'blocks' CHECK (dependency_type IN ('blocks', 'related')),
    created_at TEXT NOT NULL,
    PRIMARY KEY (task_id, depends_on_task_id),
    CHECK (task_id <> depends_on_task_id)
);

CREATE TABLE IF NOT EXISTS h_events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES h_runs(run_id) ON DELETE CASCADE,
    seq INTEGER NOT NULL CHECK (seq >= 1),
    event_type TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    dedupe_key TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, seq),
    UNIQUE (run_id, dedupe_key)
);

CREATE TABLE IF NOT EXISTS h_artifacts (
    artifact_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES h_runs(run_id) ON DELETE CASCADE,
    task_id TEXT REFERENCES h_tasks(task_id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN (
        'request', 'result', 'source_manifest', 'task_manifest',
        'event_export', 'diagnostic'
    )),
    relative_path TEXT NOT NULL,
    media_type TEXT NOT NULL,
    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    created_at TEXT NOT NULL,
    UNIQUE (run_id, relative_path)
);

CREATE INDEX IF NOT EXISTS h_events_run_type_idx ON h_events(run_id, event_type, seq);
CREATE INDEX IF NOT EXISTS h_tasks_run_state_idx ON h_tasks(run_id, state, ordinal);
CREATE INDEX IF NOT EXISTS h_artifacts_run_kind_idx ON h_artifacts(run_id, kind);
