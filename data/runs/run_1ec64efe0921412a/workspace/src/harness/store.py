"""
harness/store.py
────────────────
IssueStore – SQLite-backed persistence for pages and snapshots.

Rules (from PRD):
  • Writes are atomic (BEGIN IMMEDIATE + commit).
  • Cached pages use a composite key: repo_id + filter_hash + page.
  • Snapshots are immutable; refresh creates a new snapshot, never overwrites.
  • Anonymous responses may be cached; authenticated responses stay in memory
    (caller decides whether to persist authenticated pages).
  • Cache clear deletes cached data, not saved snapshots.
  • File permissions are set to 0o600 (owner read/write only).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import HarnessConfig, get_config
from .models import IssuePage, IssueRecord, IssueSnapshot, Repository

logger = logging.getLogger(__name__)

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS repositories (
    repository_id  INTEGER PRIMARY KEY,
    owner          TEXT    NOT NULL,
    name           TEXT    NOT NULL,
    full_name      TEXT    NOT NULL,
    html_url       TEXT    NOT NULL,
    visibility     TEXT    NOT NULL,
    default_branch TEXT    NOT NULL,
    fetched_at     TEXT    NOT NULL,
    data_json      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS issue_records (
    issue_id      INTEGER PRIMARY KEY,
    repository_id INTEGER NOT NULL,
    number        INTEGER NOT NULL,
    title         TEXT    NOT NULL,
    state         TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    data_json     TEXT    NOT NULL,
    UNIQUE(repository_id, number)
);

CREATE TABLE IF NOT EXISTS cached_pages (
    cache_key   TEXT    PRIMARY KEY,
    data_json   TEXT    NOT NULL,
    fetched_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id     TEXT    PRIMARY KEY,
    repository_id   INTEGER NOT NULL,
    issue_number    INTEGER NOT NULL,
    schema_version  TEXT    NOT NULL,
    content_hash    TEXT    NOT NULL,
    fetched_at      TEXT    NOT NULL,
    data_json       TEXT    NOT NULL
);
"""


class IssueStore:
    def __init__(self, config: HarnessConfig | None = None) -> None:
        self._cfg = config or get_config()
        self._db_path = self._cfg.db_path
        self._conn: sqlite3.Connection | None = None
        self._ensure_db()

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def _ensure_db(self) -> None:
        db_path = self._db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        conn.executescript(_SCHEMA)
        conn.commit()
        conn.close()
        # Restrict permissions to owner only
        try:
            os.chmod(db_path, 0o600)
        except OSError:
            pass
        logger.debug("SQLite store ready at %s", db_path)

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._db_path))
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    # ── repository ────────────────────────────────────────────────────────────

    def save_repository(self, repo: Repository) -> None:
        conn = self._get_conn()
        with conn:
            conn.execute(
                """
                INSERT INTO repositories
                    (repository_id, owner, name, full_name, html_url,
                     visibility, default_branch, fetched_at, data_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(repository_id) DO UPDATE SET
                    visibility     = excluded.visibility,
                    default_branch = excluded.default_branch,
                    fetched_at     = excluded.fetched_at,
                    data_json      = excluded.data_json
                """,
                (
                    repo.repository_id,
                    repo.owner,
                    repo.name,
                    repo.full_name,
                    repo.html_url,
                    repo.visibility,
                    repo.default_branch,
                    repo.fetched_at.isoformat(),
                    repo.model_dump_json(),
                ),
            )

    def load_repository(self, repository_id: int) -> Repository | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT data_json FROM repositories WHERE repository_id = ?",
            (repository_id,),
        ).fetchone()
        if row:
            return Repository.model_validate_json(row["data_json"])
        return None

    # ── pages ─────────────────────────────────────────────────────────────────

    def save_page(self, repo_id: int, page: IssuePage) -> None:
        key = self._page_key(repo_id, page.filters, page.next_cursor)
        conn = self._get_conn()
        with conn:
            conn.execute(
                """
                INSERT INTO cached_pages (cache_key, data_json, fetched_at)
                VALUES (?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    data_json  = excluded.data_json,
                    fetched_at = excluded.fetched_at
                """,
                (key, page.model_dump_json(), _utcnow_str()),
            )

    def load_page(self, repo_id: int, filters: dict, cursor: str | None) -> IssuePage | None:
        key = self._page_key(repo_id, filters, cursor)
        conn = self._get_conn()
        row = conn.execute(
            "SELECT data_json FROM cached_pages WHERE cache_key = ?",
            (key,),
        ).fetchone()
        if row:
            return IssuePage.model_validate_json(row["data_json"])
        return None

    # ── snapshots ─────────────────────────────────────────────────────────────

    def save_snapshot(self, snapshot: IssueSnapshot) -> Path:
        """
        Persist snapshot to SQLite and write a JSON file to snapshots_dir.
        Returns the path to the JSON file.
        Never overwrites an existing snapshot (immutable by design).
        """
        conn = self._get_conn()
        with conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO snapshots
                    (snapshot_id, repository_id, issue_number, schema_version,
                     content_hash, fetched_at, data_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.snapshot_id,
                    snapshot.repository.repository_id,
                    snapshot.issue.number,
                    snapshot.schema_version,
                    snapshot.content_hash,
                    snapshot.fetched_at.isoformat(),
                    snapshot.model_dump_json(),
                ),
            )

        # Write JSON file
        snap_dir = self._cfg.snapshots_dir
        snap_dir.mkdir(parents=True, exist_ok=True)
        fname = f"{snapshot.snapshot_id}.json"
        path = snap_dir / fname
        path.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        logger.info("Snapshot saved: %s", path)
        return path

    def load_snapshot(self, snapshot_id: str) -> IssueSnapshot | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT data_json FROM snapshots WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
        if row:
            return IssueSnapshot.model_validate_json(row["data_json"])
        return None

    def list_snapshots(self) -> list[dict]:
        """Return minimal metadata for listing saved snapshots."""
        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT snapshot_id, repository_id, issue_number, content_hash, fetched_at
            FROM snapshots ORDER BY fetched_at DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]

    # ── cache management ──────────────────────────────────────────────────────

    def clear_cache(self) -> int:
        """Delete cached pages (not snapshots). Returns number of rows deleted."""
        conn = self._get_conn()
        with conn:
            cursor = conn.execute("DELETE FROM cached_pages")
            return cursor.rowcount

    # ── helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _page_key(repo_id: int, filters: dict, cursor: str | None) -> str:
        stable = json.dumps(
            {"repo_id": repo_id, "filters": filters, "cursor": cursor},
            sort_keys=True,
        )
        return hashlib.sha256(stable.encode()).hexdigest()[:32]


def _utcnow_str() -> str:
    return datetime.now(tz=timezone.utc).isoformat()
