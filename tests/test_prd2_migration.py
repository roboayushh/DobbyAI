from __future__ import annotations

import datetime
import hashlib
import sqlite3
from pathlib import Path

import pytest

from harness.persistence import ArtifactStore, IntegrityError, RunStore, apply_migrations


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def test_migration_002_preserves_legacy_artifact(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    migration_1 = (
        Path(__file__).parents[1]
        / "src/harness/persistence/migrations/001_prd1_initial.sql"
    ).read_text(encoding="utf-8")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(migration_1)
    conn.execute(
        "INSERT INTO h_schema_migrations(version, name, applied_at) VALUES (1, ?, ?)",
        ("001_prd1_initial.sql", _now()),
    )
    conn.execute(
        """
        INSERT INTO h_runs(
            run_id, schema_version, idempotency_key, task_mode, execution_mode,
            state, request_json, request_sha256, runtime_profile,
            next_event_seq, created_at, updated_at
        ) VALUES ('run_legacy', '1.0', 'legacy-key', 'single_issue', 'development',
                  'PREPARED', '{}', ?, 'local-default', 1, ?, ?)
        """,
        (hashlib.sha256(b"{}").hexdigest(), _now(), _now()),
    )
    content = b"legacy"
    conn.execute(
        """
        INSERT INTO h_artifacts(
            artifact_id, run_id, kind, relative_path, media_type,
            byte_size, sha256, created_at
        ) VALUES ('art_legacy', 'run_legacy', 'diagnostic',
                  'runs/run_legacy/artifacts/legacy.txt', 'text/plain', ?, ?, ?)
        """,
        (len(content), hashlib.sha256(content).hexdigest(), _now()),
    )
    conn.commit()

    applied = apply_migrations(conn)
    # 002 applies first; later additive PRD 3-5 migrations follow in order.
    assert applied[0] == "002_prd2_orchestration.sql"
    assert applied == sorted(applied)
    artifact = conn.execute(
        "SELECT artifact_id, relative_path, byte_size, sha256 FROM h_artifacts"
    ).fetchone()
    assert artifact == (
        "art_legacy",
        "runs/run_legacy/artifacts/legacy.txt",
        len(content),
        hashlib.sha256(content).hexdigest(),
    )
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"h_run_lifecycle", "h_budget_ledgers", "h_model_calls", "h_action_proposals"} <= tables
    conn.close()


def test_artifacts_are_application_write_once(tmp_path: Path) -> None:
    store = RunStore(str(tmp_path / "harness.db"))
    now = _now()
    with store.get_connection() as conn:
        with conn:
            conn.execute(
                """
                INSERT INTO h_runs(
                    run_id, schema_version, idempotency_key, task_mode, execution_mode,
                    state, request_json, request_sha256, runtime_profile,
                    next_event_seq, created_at, updated_at
                ) VALUES ('run_1', '1.0', 'immutable-key', 'single_issue', 'development',
                          'PREPARED', '{}', ?, 'local-default', 1, ?, ?)
                """,
                (hashlib.sha256(b"{}").hexdigest(), now, now),
            )
    artifacts = ArtifactStore(str(tmp_path), store)
    first = artifacts.write_bytes("run_1", "x.txt", b"one", "text/plain", "diagnostic")
    replay = artifacts.write_bytes("run_1", "x.txt", b"one", "text/plain", "diagnostic")
    assert replay.sha256 == first.sha256
    with pytest.raises(IntegrityError):
        artifacts.write_bytes("run_1", "x.txt", b"two", "text/plain", "diagnostic")
    with pytest.raises(ValueError, match="Unregistered"):
        artifacts.write_bytes("run_1", "y.txt", b"x", "text/plain", "repository_defined_kind")
