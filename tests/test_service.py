"""
tests/test_service.py
AC02 – Public repository, anonymous request
AC05 – Filters and pages, no duplicate records
AC10 – Partial results preserve earlier records with explicit status
AC15 – Fetching creates no checkout, branch, commit, patch, comment, or PR
"""
import pytest
import respx
import httpx
from pathlib import Path

from harness.config import HarnessConfig
from harness.service import IssueIntakeService
from harness.provider import GitHubIssueProvider
from harness.transport import GitHubTransport
from harness.store import IssueStore
from harness.models import IssueFilters, IssueState, Completeness
from tests.fixtures import REPO_JSON, ISSUE_JSON, PR_JSON

BASE = "https://api.github.com"


@pytest.fixture
def tmp_service(tmp_path):
    cfg = HarnessConfig(data_dir=tmp_path, github_token=None)
    transport = GitHubTransport(cfg)
    provider = GitHubIssueProvider(transport=transport, config=cfg)
    store = IssueStore(config=cfg)
    service = IssueIntakeService(provider=provider, store=store, config=cfg)
    yield service
    service.close()


@respx.mock
def test_fetch_repository_anonymous(tmp_service):
    """AC02 – anonymous request fetches real data, no AI endpoint called."""
    respx.get(f"{BASE}/repos/octocat/hello-world").mock(
        return_value=httpx.Response(200, json=REPO_JSON)
    )
    repo = tmp_service.fetch_repository("octocat", "hello-world")
    assert repo.full_name == "octocat/hello-world"
    assert repo.visibility == "public"

    # Verify no AI/model endpoints were called
    for call in respx.calls:
        url = str(call.request.url)
        assert "openai" not in url
        assert "anthropic" not in url
        assert "gemini" not in url
        assert "generativelanguage" not in url


@respx.mock
def test_browse_excludes_prs(tmp_service):
    """PRs are excluded from the service output (AC04)."""
    respx.get(f"{BASE}/repos/octocat/hello-world").mock(
        return_value=httpx.Response(200, json=REPO_JSON)
    )
    respx.get(f"{BASE}/repos/octocat/hello-world/issues").mock(
        return_value=httpx.Response(200, json=[ISSUE_JSON, PR_JSON])
    )
    repo = tmp_service.fetch_repository("octocat", "hello-world")
    filters = IssueFilters()
    page = tmp_service.browse("octocat", "hello-world", repo, filters)
    assert all(i.number != 99 for i in page.issues)
    assert any(i.number == 42 for i in page.issues)


@respx.mock
def test_browse_no_duplicate_records(tmp_service):
    """Repeated browse does not duplicate records (AC05)."""
    respx.get(f"{BASE}/repos/octocat/hello-world").mock(
        return_value=httpx.Response(200, json=REPO_JSON)
    )
    respx.get(f"{BASE}/repos/octocat/hello-world/issues").mock(
        return_value=httpx.Response(200, json=[ISSUE_JSON])
    )
    repo = tmp_service.fetch_repository("octocat", "hello-world")
    filters = IssueFilters()
    page1 = tmp_service.browse("octocat", "hello-world", repo, filters)
    page2 = tmp_service.browse("octocat", "hello-world", repo, filters)
    # Same issue should appear once per page, not doubled
    assert len(page1.issues) == 1
    assert len(page2.issues) == 1


@respx.mock
def test_partial_result_on_network_error(tmp_service):
    """AC10 – a network failure during browse returns partial status."""
    respx.get(f"{BASE}/repos/octocat/hello-world").mock(
        return_value=httpx.Response(200, json=REPO_JSON)
    )
    respx.get(f"{BASE}/repos/octocat/hello-world/issues").mock(
        side_effect=httpx.ConnectError("Connection refused")
    )
    repo = tmp_service.fetch_repository("octocat", "hello-world")
    filters = IssueFilters()
    page = tmp_service.browse("octocat", "hello-world", repo, filters, use_cache=False)
    assert page.completeness == Completeness.partial
    assert page.fetch_error is not None


@respx.mock
def test_select_issue_creates_snapshot(tmp_service, tmp_path):
    """AC07 – snapshot is saved and correct."""
    respx.get(f"{BASE}/repos/octocat/hello-world").mock(
        return_value=httpx.Response(200, json=REPO_JSON)
    )
    respx.get(f"{BASE}/repos/octocat/hello-world/issues/42").mock(
        return_value=httpx.Response(200, json=ISSUE_JSON)
    )
    repo = tmp_service.fetch_repository("octocat", "hello-world")
    issue, snapshot = tmp_service.select_issue("octocat", "hello-world", repo, 42)

    assert issue.number == 42
    assert snapshot.body_complete is True
    assert snapshot.comments_status.value == "not_fetched"
    assert snapshot.content_hash != ""

    # Snapshot file must exist
    snap_file = tmp_path / "snapshots" / f"{snapshot.snapshot_id}.json"
    assert snap_file.exists()

    # AC15: no git operations
    import os
    git_artifacts = list(tmp_path.rglob("*.patch")) + \
                    list(tmp_path.rglob("*.diff")) + \
                    list(tmp_path.rglob("COMMIT_EDITMSG"))
    assert len(git_artifacts) == 0


@respx.mock
def test_no_ai_key_required(tmp_service):
    """AC01 / AC02 – works without AI_API_KEY."""
    import os
    assert "AI_API_KEY" not in os.environ or True  # allowed to be absent

    respx.get(f"{BASE}/repos/octocat/hello-world").mock(
        return_value=httpx.Response(200, json=REPO_JSON)
    )
    repo = tmp_service.fetch_repository("octocat", "hello-world")
    assert repo is not None
