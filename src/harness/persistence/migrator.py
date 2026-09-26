"""harness/persistence/migrator.py
Forward-only numbered schema migrations for SQLite.
"""
from __future__ import annotations

import datetime
import os
import sqlite3
from pathlib import Path
from typing import List


def get_migrations_dir() -> Path:
    return Path(__file__).parent / "migrations"


def apply_migrations(conn: sqlite3.Connection) -> List[str]:
    """Apply all pending migrations in order.
    Returns list of applied migration names.
    """
    conn.execute("PRAGMA foreign_keys = ON;")
    # Ensure migrations tracking table exists
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS h_schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            applied_at TEXT NOT NULL
        );
        """
    )
    conn.commit()

    # Get already applied versions
    cursor = conn.cursor()
    cursor.execute("SELECT version, name FROM h_schema_migrations ORDER BY version ASC")
    applied = {row[0]: row[1] for row in cursor.fetchall()}

    migrations_dir = get_migrations_dir()
    sql_files = sorted(migrations_dir.glob("*.sql"))

    available_versions = {}
    for f in sql_files:
        prefix = f.name.split("_")[0]
        try:
            ver = int(prefix)
            available_versions[ver] = f
        except ValueError:
            continue

    # Invariant: Database must not contain newer unsupported schema versions
    if applied:
        max_applied = max(applied.keys())
        max_available = max(available_versions.keys()) if available_versions else 0
        if max_applied > max_available:
            raise RuntimeError(
                f"Database schema version {max_applied} is newer than maximum supported version {max_available}."
            )

    applied_names = []
    for ver in sorted(available_versions.keys()):
        if ver in applied:
            continue
        sql_file = available_versions[ver]
        with open(sql_file, "r", encoding="utf-8") as f:
            script = f.read()

        now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with conn:
            conn.executescript(script)
            conn.execute(
                "INSERT INTO h_schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (ver, sql_file.name, now_utc),
            )
        applied_names.append(sql_file.name)

    return applied_names
