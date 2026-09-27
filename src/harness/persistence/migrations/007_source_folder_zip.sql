-- Accept ordinary folders and ZIP archives as sources (requirements FR03/FR05).
-- Rebuilds h_source_snapshots with a wider source_kind CHECK; every row is
-- preserved (the migrator verifies row counts and foreign keys before commit).

CREATE TABLE h_source_snapshots_v7 (
    source_snapshot_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES h_runs(run_id) ON DELETE CASCADE,
    source_kind TEXT NOT NULL CHECK (source_kind IN ('local_git', 'public_https', 'local_folder', 'local_zip')),
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

INSERT INTO h_source_snapshots_v7 SELECT * FROM h_source_snapshots;
DROP TABLE h_source_snapshots;
ALTER TABLE h_source_snapshots_v7 RENAME TO h_source_snapshots;
