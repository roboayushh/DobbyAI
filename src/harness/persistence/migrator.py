"""harness/persistence/migrator.py
Forward-only numbered schema migrations for SQLite.
"""
from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path
from typing import List


def get_migrations_dir() -> Path:
    return Path(__file__).parent / "migrations"


# Tables that a migration rebuilds (to widen CHECK constraints) and whose rows
# must survive byte-for-byte. Keyed by migration version.
PRESERVED_TABLES = {
    3: ("h_run_lifecycle", "h_task_lifecycle"),
    7: ("h_source_snapshots",),
}

# Migrations from this version onward run as one explicit transaction that
# also records the schema version, so an interruption leaves the prior
# schema fully usable.
TRANSACTIONAL_FROM_VERSION = 3


def _snapshot_tables(conn: sqlite3.Connection, tables: tuple) -> dict:
    snapshot = {}
    for table in tables:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if exists:
            snapshot[table] = conn.execute(
                f"SELECT * FROM {table} ORDER BY rowid"
            ).fetchall()
    return snapshot


def _apply_transactional(
    conn: sqlite3.Connection, version: int, sql_file: Path, script: str
) -> None:
    """Apply one migration and its version row atomically.

    ``executescript`` commits any pending transaction before running, so the
    explicit BEGIN is issued inside the script and the transaction is left open
    for the integrity checks and version insert that follow.
    """
    preserved = PRESERVED_TABLES.get(version, ())
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF;")
    try:
        before = _snapshot_tables(conn, preserved)
        conn.executescript("BEGIN IMMEDIATE;\n" + script)
        after = _snapshot_tables(conn, preserved)
        for table, rows in before.items():
            if after.get(table) != rows:
                raise RuntimeError(
                    f"Migration {sql_file.name} did not preserve rows of {table} exactly"
                )
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(
                f"Migration {sql_file.name} produced foreign-key violations: {violations[:5]}"
            )
        now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO h_schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
            (version, sql_file.name, now_utc),
        )
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON;")


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
        if ver >= TRANSACTIONAL_FROM_VERSION:
            _apply_transactional(conn, ver, sql_file, sql_file.read_text(encoding="utf-8"))
            applied_names.append(sql_file.name)
            continue
        artifact_snapshot = None
        if ver == 2:
            table_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='h_artifacts'"
            ).fetchone()
            if table_exists:
                artifact_snapshot = conn.execute(
                    "SELECT * FROM h_artifacts ORDER BY artifact_id"
                ).fetchall()
        with open(sql_file, "r", encoding="utf-8") as f:
            script = f.read()

        now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with conn:
            conn.executescript(script)
            if artifact_snapshot is not None:
                migrated_artifacts = conn.execute(
                    "SELECT * FROM h_artifacts ORDER BY artifact_id"
                ).fetchall()
                if migrated_artifacts != artifact_snapshot:
                    raise RuntimeError(
                        f"Migration {sql_file.name} did not preserve legacy artifact rows exactly"
                    )
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(
                    f"Migration {sql_file.name} produced foreign-key violations: {violations[:5]}"
                )
            conn.execute(
                "INSERT INTO h_schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (ver, sql_file.name, now_utc),
            )
        applied_names.append(sql_file.name)

    return applied_names
