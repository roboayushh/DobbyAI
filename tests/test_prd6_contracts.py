"""PRD 6 contracts, schemas, migrations 1-7, and status mapping."""
from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

import harness.contracts.release as release
from harness.persistence import RunStore
from harness.persistence.migrator import apply_migrations
from harness.release.status_mapping import EXIT_CODES, ITEM_TO_EXTERNAL, LIFECYCLE_TO_EXTERNAL, exit_code

EXAMPLES = sorted((Path(__file__).parent / "prd_examples_prd6").glob("*.json"))
MIGRATIONS = sorted((Path(__file__).resolve().parents[1] / "src" / "harness" / "persistence" / "migrations").glob("*.sql"))


def test_all_fourteen_prd6_examples_are_present() -> None:
    assert len(EXAMPLES) == 14


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_prd6_example_validates(path: Path) -> None:
    getattr(release, path.stem).model_validate(json.loads(path.read_text()))


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_future_major_version_and_unknown_fields_fail_closed(path: Path) -> None:
    model = getattr(release, path.stem)
    data = json.loads(path.read_text())
    with pytest.raises(Exception, match="UNSUPPORTED_SCHEMA_VERSION"):
        model.model_validate({**data, "schema_version": "2.0"})
    with pytest.raises(Exception):
        model.model_validate({**data, "unexpected_field": 1})
    model.model_validate({**data, "schema_version": "1.7"})  # minor bumps stay readable


def test_grant_uses_are_bounded() -> None:
    data = json.loads((Path(__file__).parent / "prd_examples_prd6" / "ApprovalGrantV1.json").read_text())
    with pytest.raises(Exception):
        release.ApprovalGrantV1.model_validate({**data, "remaining_uses": 5})


def test_every_internal_terminal_status_has_one_exit_code() -> None:
    for status in {*ITEM_TO_EXTERNAL.values(), *LIFECYCLE_TO_EXTERNAL.values(), *release.EVALUATOR_STATUSES}:
        assert status in EXIT_CODES, status
    assert exit_code("PASS") == 0 and exit_code("COMPLETED_ALL") == 0
    assert exit_code("INVALID") == 2
    assert {exit_code(s) for s in ("PARTIAL_SUCCESS", "UNVERIFIED", "BLOCKED_ENVIRONMENT")} == {3}
    assert exit_code("FAILED") == 4 and exit_code("BUDGET_EXHAUSTED") == 5
    assert exit_code("NEEDS_INPUT") == 6 and exit_code("PENDING_APPROVAL") == 6
    assert exit_code("INTERNAL_ERROR") == 7 and exit_code("CANCELLED") == 130


def test_migrations_one_to_seven_apply_clean_and_idempotently(tmp_path: Path) -> None:
    store = RunStore(str(tmp_path / "h.db"))
    with store.get_connection() as conn:
        versions = [row[0] for row in conn.execute("SELECT version FROM h_schema_migrations ORDER BY version")]
        assert versions == list(range(1, len(MIGRATIONS) + 1))
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert apply_migrations(conn) == []  # idempotent startup at the latest version
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in ("h_evaluator_sessions", "h_plugin_set_locks", "h_exports", "h_export_round_trips", "h_capability_requests",
                  "h_approval_grants", "h_external_effect_intents", "h_cleanup_plans", "h_reproducibility_manifests", "h_release_gates"):
        assert table in tables


def test_populated_prd5_database_upgrades_without_losing_rows(tmp_path: Path) -> None:
    from tests.support.harness_fixtures import prepare_run

    env = prepare_run(tmp_path, {"a.py": "x = 1\n"}, "Change x in a.py to two please")
    db = tmp_path / "harness_data" / "harness.db"
    copy = tmp_path / "old.db"
    source = sqlite3.connect(db)
    conn = sqlite3.connect(copy)
    source.backup(conn)  # WAL-safe copy
    source.close()
    conn.execute("DELETE FROM h_schema_migrations WHERE version >= 6")
    conn.commit()
    # Simulate a pre-PRD 6 database by dropping the PRD 6 tables, then upgrade.
    prd6 = (MIGRATIONS[5]).read_text()
    for name in [line.split()[2] for line in prd6.splitlines() if line.startswith("CREATE TABLE")]:
        conn.execute(f"DROP TABLE IF EXISTS {name}")
    conn.commit()
    before = conn.execute("SELECT COUNT(*) FROM h_source_snapshots").fetchone()[0]
    conn.row_factory = sqlite3.Row
    applied = apply_migrations(conn)
    assert applied[0].startswith("006") and applied[-1].startswith("007")
    assert conn.execute("SELECT COUNT(*) FROM h_source_snapshots").fetchone()[0] == before == 1
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
