"""
tests/test_models.py
Model round-trip, null-body coercion, content hash, and schema validation.
"""
import pytest
from datetime import datetime, timezone

from harness.models import (
    IssueRecord,
    IssueSnapshot,
    IssuePage,
    Repository,
    IssueFilters,
    IssueState,
    JSONEnvelope,
    EnvelopeStatus,
    Source,
    CommentsStatus,
)
from tests.fixtures import REPO_JSON, ISSUE_JSON
from harness.provider import GitHubIssueProvider


def make_repo():
    return GitHubIssueProvider._parse_repository(REPO_JSON)


def make_issue():
    return GitHubIssueProvider._parse_issue(ISSUE_JSON)


# ── null body coercion ────────────────────────────────────────────────────────

def test_null_body_becomes_empty_string():
    issue = IssueRecord(
        issue_id=1,
        repository_id=0,
        number=1,
        title="Test",
        body=None,
        state="open",
        author=None,
        created_at=datetime.now(tz=timezone.utc),
        updated_at=datetime.now(tz=timezone.utc),
        html_url="https://github.com/o/r/issues/1",
        comments_count=0,
    )
    assert issue.body == ""


# ── snapshot round-trip ───────────────────────────────────────────────────────

def test_snapshot_roundtrip_json():
    repo = make_repo()
    issue = make_issue()
    snap = IssueSnapshot(
        repository=repo,
        issue=issue,
        source_updated_at=issue.updated_at,
        source=Source.live,
        body_complete=True,
    )
    json_str = snap.model_dump_json()
    restored = IssueSnapshot.model_validate_json(json_str)
    assert restored.snapshot_id == snap.snapshot_id
    assert restored.content_hash == snap.content_hash
    assert restored.comments_status == CommentsStatus.not_fetched
    assert restored.schema_version == "1.0"


def test_snapshot_content_hash_is_deterministic():
    repo = make_repo()
    issue = make_issue()
    snap1 = IssueSnapshot(
        repository=repo,
        issue=issue,
        source_updated_at=issue.updated_at,
        source=Source.live,
        body_complete=True,
    )
    snap2 = IssueSnapshot(
        repository=repo,
        issue=issue,
        source_updated_at=issue.updated_at,
        source=Source.live,
        body_complete=True,
    )
    # Both should produce the same content hash (same input)
    assert snap1.content_hash == snap2.content_hash


# ── filter params ─────────────────────────────────────────────────────────────

def test_issue_filters_api_params_open():
    f = IssueFilters(state=IssueState.open, labels=["bug"], per_page=30, page=1)
    params = f.as_api_params()
    assert params["state"] == "open"
    assert params["labels"] == "bug"
    assert params["per_page"] == 30
    assert params["sort"] == "updated"
    assert params["direction"] == "desc"


def test_issue_filters_no_labels_omits_param():
    f = IssueFilters()
    params = f.as_api_params()
    assert "labels" not in params


def test_filters_reset_page():
    f = IssueFilters(page=5)
    reset = f.reset_page()
    assert reset.page == 1


# ── JSON envelope ─────────────────────────────────────────────────────────────

def test_json_envelope_ok():
    env = JSONEnvelope(status=EnvelopeStatus.ok, data={"result": "ok"})
    assert env.schema_version == "1.0"
    assert env.error is None


def test_json_envelope_error():
    env = JSONEnvelope(status=EnvelopeStatus.error, error="Something failed")
    assert env.data is None
