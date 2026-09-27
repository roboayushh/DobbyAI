"""
tests/test_store.py
AC07 – Snapshot survives restart, round-trips schema, records provenance.
"""
import pytest
from pathlib import Path
import tempfile

from harness.config import HarnessConfig
from harness.store import IssueStore
from harness.models import IssueSnapshot, Repository, IssueRecord, Source
from tests.fixtures import REPO_JSON, ISSUE_JSON
from harness.provider import GitHubIssueProvider


def make_repo() -> Repository:
    from harness.provider import GitHubIssueProvider
    return GitHubIssueProvider._parse_repository(REPO_JSON)


def make_issue() -> IssueRecord:
    return GitHubIssueProvider._parse_issue(ISSUE_JSON)


@pytest.fixture
def tmp_store(tmp_path):
    cfg = HarnessConfig(data_dir=tmp_path)
    store = IssueStore(config=cfg)
    yield store
    store.close()


def test_save_and_load_snapshot(tmp_store):
    repo = make_repo()
    issue = make_issue()
    snap = IssueSnapshot(
        repository=repo,
        issue=issue,
        source_updated_at=issue.updated_at,
        source=Source.live,
        body_complete=True,
    )
    path = tmp_store.save_snapshot(snap)
    assert path.exists()

    # Load back from DB
    loaded = tmp_store.load_snapshot(snap.snapshot_id)
    assert loaded is not None
    assert loaded.snapshot_id == snap.snapshot_id
    assert loaded.issue.number == 42
    assert loaded.body_complete is True
    assert loaded.comments_status.value == "not_fetched"
    assert loaded.content_hash != ""


def test_snapshot_is_immutable(tmp_store):
    """A second save of same snapshot_id must not overwrite (INSERT OR IGNORE)."""
    repo = make_repo()
    issue = make_issue()
    snap = IssueSnapshot(
        repository=repo,
        issue=issue,
        source_updated_at=issue.updated_at,
        source=Source.live,
        body_complete=True,
    )
    tmp_store.save_snapshot(snap)

    # Mutate and attempt re-save
    snap2 = snap.model_copy(update={"body_complete": False})
    tmp_store.save_snapshot(snap2)

    # Original should be preserved
    loaded = tmp_store.load_snapshot(snap.snapshot_id)
    assert loaded.body_complete is True


def test_snapshot_json_file_has_no_base_commit(tmp_store, tmp_path):
    """AC07: snapshot must not invent a base commit or checkout."""
    repo = make_repo()
    issue = make_issue()
    snap = IssueSnapshot(
        repository=repo,
        issue=issue,
        source_updated_at=issue.updated_at,
        source=Source.live,
        body_complete=True,
    )
    path = tmp_store.save_snapshot(snap)
    content = path.read_text()
    # These fields must NOT appear in a Phase 1 snapshot
    assert "base_commit" not in content
    assert "checkout" not in content
    assert "git_branch" not in content
    assert "patch" not in content
    # 'default_branch' is a legitimate repo metadata field, so we only
    # check that we haven't added novel git-workspace fields


def test_snapshot_file_permissions(tmp_store):
    repo = make_repo()
    issue = make_issue()
    snap = IssueSnapshot(
        repository=repo,
        issue=issue,
        source_updated_at=issue.updated_at,
        source=Source.live,
        body_complete=True,
    )
    path = tmp_store.save_snapshot(snap)
    import stat
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600


def test_cache_clear_preserves_snapshots(tmp_store):
    repo = make_repo()
    issue = make_issue()
    snap = IssueSnapshot(
        repository=repo,
        issue=issue,
        source_updated_at=issue.updated_at,
        source=Source.live,
        body_complete=True,
    )
    tmp_store.save_snapshot(snap)

    from harness.models import IssuePage, IssueFilters
    page = IssuePage(issues=[], filters={})
    tmp_store.save_page(repo.repository_id, page)

    n = tmp_store.clear_cache()
    assert n >= 1

    # Snapshot still exists
    loaded = tmp_store.load_snapshot(snap.snapshot_id)
    assert loaded is not None


def test_list_snapshots(tmp_store):
    repo = make_repo()
    issue = make_issue()
    for _ in range(3):
        snap = IssueSnapshot(
            repository=repo,
            issue=issue,
            source_updated_at=issue.updated_at,
            source=Source.live,
            body_complete=True,
        )
        tmp_store.save_snapshot(snap)
    snaps = tmp_store.list_snapshots()
    assert len(snaps) >= 3
