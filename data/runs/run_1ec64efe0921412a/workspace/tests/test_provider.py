"""
tests/test_provider.py
AC04 – PR exclusion (mixed pages preserve pagination)
AC05 – Filters and pages
AC06 – Selected issue: full body, null bodies, missing author
AC08 – 401/403/404 produce distinct errors
"""
import pytest
import respx
import httpx

from harness.provider import GitHubIssueProvider
from harness.models import IssueFilters, IssueState
from harness.transport import GitHubTransport, AuthError, AccessError, RateLimitError
from harness.config import HarnessConfig
from tests.fixtures import REPO_JSON, ISSUE_JSON, PR_JSON, NULL_BODY_ISSUE_JSON, DELETED_USER_ISSUE_JSON

BASE = "https://api.github.com"


def make_transport(config=None):
    cfg = config or HarnessConfig(github_token=None)
    return GitHubTransport(cfg)


# ── repository ────────────────────────────────────────────────────────────────

@respx.mock
def test_get_repository_success():
    respx.get(f"{BASE}/repos/octocat/hello-world").mock(
        return_value=httpx.Response(200, json=REPO_JSON)
    )
    provider = GitHubIssueProvider(transport=make_transport())
    repo = provider.get_repository("octocat", "hello-world")
    assert repo.full_name == "octocat/hello-world"
    assert repo.repository_id == 123456789
    assert repo.visibility == "public"


# ── issue list – PR filtering (AC04) ─────────────────────────────────────────

@respx.mock
def test_list_issues_filters_pull_requests():
    """PRs in the response must be excluded; issues remain (AC04)."""
    respx.get(f"{BASE}/repos/octocat/hello-world/issues").mock(
        return_value=httpx.Response(200, json=[ISSUE_JSON, PR_JSON])
    )
    provider = GitHubIssueProvider(transport=make_transport())
    filters = IssueFilters()
    page = provider.list_issues("octocat", "hello-world", filters, cursor=None)
    assert len(page.issues) == 1
    assert page.issues[0].number == 42


@respx.mock
def test_list_issues_all_pr_page_preserves_pagination():
    """A page containing only PRs should NOT end pagination (AC04)."""
    link_header = f'<{BASE}/repos/octocat/hello-world/issues?page=2>; rel="next"'
    respx.get(f"{BASE}/repos/octocat/hello-world/issues").mock(
        return_value=httpx.Response(
            200,
            json=[PR_JSON],
            headers={"Link": link_header},
        )
    )
    provider = GitHubIssueProvider(transport=make_transport())
    filters = IssueFilters()
    page = provider.list_issues("octocat", "hello-world", filters, cursor=None)
    assert page.issues == []            # filtered
    assert page.has_more is True        # pagination preserved
    assert page.next_cursor is not None


# ── filters reach the API (AC05) ─────────────────────────────────────────────

@respx.mock
def test_list_issues_state_label_params():
    """State and labels must be passed as API query params."""
    route = respx.get(f"{BASE}/repos/octocat/hello-world/issues").mock(
        return_value=httpx.Response(200, json=[ISSUE_JSON])
    )
    provider = GitHubIssueProvider(transport=make_transport())
    filters = IssueFilters(state=IssueState.closed, labels=["bug", "urgent"])
    provider.list_issues("octocat", "hello-world", filters, cursor=None)

    request = route.calls.last.request
    assert "state=closed" in str(request.url)
    assert "labels=bug%2Curgent" in str(request.url) or "labels=bug,urgent" in str(request.url)


# ── null body and missing author (AC06) ───────────────────────────────────────

@respx.mock
def test_get_issue_null_body_becomes_empty_string():
    respx.get(f"{BASE}/repos/octocat/hello-world/issues/55").mock(
        return_value=httpx.Response(200, json=NULL_BODY_ISSUE_JSON)
    )
    provider = GitHubIssueProvider(transport=make_transport())
    issue = provider.get_issue("octocat", "hello-world", 55)
    assert issue.body == ""


@respx.mock
def test_get_issue_deleted_user():
    respx.get(f"{BASE}/repos/octocat/hello-world/issues/77").mock(
        return_value=httpx.Response(200, json=DELETED_USER_ISSUE_JSON)
    )
    provider = GitHubIssueProvider(transport=make_transport())
    issue = provider.get_issue("octocat", "hello-world", 77)
    assert issue.author is None


@respx.mock
def test_get_issue_full_fields():
    respx.get(f"{BASE}/repos/octocat/hello-world/issues/42").mock(
        return_value=httpx.Response(200, json=ISSUE_JSON)
    )
    provider = GitHubIssueProvider(transport=make_transport())
    issue = provider.get_issue("octocat", "hello-world", 42)
    assert issue.number == 42
    assert issue.title == "Fix the flux capacitor"
    assert "flux capacitor" in issue.body
    assert issue.author == "marty"
    assert "bug" in issue.labels
    assert "doc" in issue.assignees
    assert issue.comments_count == 3


@respx.mock
def test_get_issue_rejects_pull_request():
    """Direct PR URL must be rejected (FR03)."""
    respx.get(f"{BASE}/repos/octocat/hello-world/issues/99").mock(
        return_value=httpx.Response(200, json=PR_JSON)
    )
    provider = GitHubIssueProvider(transport=make_transport())
    from harness.transport import AccessError
    with pytest.raises(AccessError, match="pull request"):
        provider.get_issue("octocat", "hello-world", 99)


# ── error handling (AC08) ─────────────────────────────────────────────────────

@respx.mock
def test_401_raises_auth_error():
    respx.get(f"{BASE}/repos/octocat/hello-world").mock(
        return_value=httpx.Response(401, json={"message": "Bad credentials"})
    )
    provider = GitHubIssueProvider(transport=make_transport())
    with pytest.raises(AuthError):
        provider.get_repository("octocat", "hello-world")


@respx.mock
def test_403_permission_raises_access_error():
    respx.get(f"{BASE}/repos/octocat/private-repo").mock(
        return_value=httpx.Response(
            403,
            json={"message": "Forbidden"},
            headers={"x-ratelimit-remaining": "60"},
        )
    )
    provider = GitHubIssueProvider(transport=make_transport())
    with pytest.raises(AccessError):
        provider.get_repository("octocat", "private-repo")


@respx.mock
def test_403_rate_limit_raises_rate_limit_error():
    respx.get(f"{BASE}/repos/octocat/hello-world").mock(
        return_value=httpx.Response(
            403,
            json={"message": "API rate limit exceeded"},
            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "9999999999"},
        )
    )
    provider = GitHubIssueProvider(transport=make_transport())
    with pytest.raises(RateLimitError):
        provider.get_repository("octocat", "hello-world")


@respx.mock
def test_404_raises_access_error():
    respx.get(f"{BASE}/repos/ghost/nonexistent").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    provider = GitHubIssueProvider(transport=make_transport())
    with pytest.raises(AccessError):
        provider.get_repository("ghost", "nonexistent")


@respx.mock
def test_429_raises_rate_limit_error():
    respx.get(f"{BASE}/repos/octocat/hello-world").mock(
        return_value=httpx.Response(
            429,
            json={"message": "Too many requests"},
            headers={"retry-after": "60"},
        )
    )
    provider = GitHubIssueProvider(transport=make_transport())
    with pytest.raises(RateLimitError) as exc_info:
        provider.get_repository("octocat", "hello-world")
    assert exc_info.value.retry_after == 60.0
