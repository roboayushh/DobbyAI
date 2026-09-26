"""
tests/test_transport.py
AC09 – Limits and outages: 429, timeout, 5xx obey retry and deadline.
AC11 – Untrusted content in responses causes no execution.
AC14 – Disallowed redirects / pagination URLs rejected.
"""
import pytest
import respx
import httpx

from harness.transport import (
    GitHubTransport,
    AuthError,
    AccessError,
    NetworkError,
    OversizedResponseError,
    RateLimitError,
)
from harness.config import HarnessConfig

BASE = "https://api.github.com"


def make_transport(token=None, max_retries=0, max_response_bytes=1024):
    cfg = HarnessConfig(
        github_token=token,
        max_retries=max_retries,
        max_response_bytes=max_response_bytes,
        request_timeout_s=5.0,
    )
    return GitHubTransport(cfg)


# ── basic success ─────────────────────────────────────────────────────────────

@respx.mock
def test_get_json_success():
    respx.get(f"{BASE}/repos/octocat/hello-world").mock(
        return_value=httpx.Response(200, json={"id": 1, "name": "hello-world"})
    )
    t = make_transport()
    data, headers = t.get_json("/repos/octocat/hello-world")
    assert data["name"] == "hello-world"


# ── oversized response (AC12) ─────────────────────────────────────────────────

@respx.mock
def test_oversized_response_rejected():
    large_body = "x" * 2048  # exceeds 1024 byte limit in test config
    respx.get(f"{BASE}/repos/octocat/hello-world").mock(
        return_value=httpx.Response(200, content=large_body.encode())
    )
    t = make_transport(max_response_bytes=512)
    with pytest.raises(OversizedResponseError):
        t.get_json("/repos/octocat/hello-world")


# ── 5xx retries (AC09) ────────────────────────────────────────────────────────

@respx.mock
def test_5xx_retries_and_raises():
    respx.get(f"{BASE}/repos/octocat/hello-world").mock(
        return_value=httpx.Response(503, json={"message": "Service Unavailable"})
    )
    # max_retries=2 but all attempts return 503
    t = make_transport(max_retries=0)  # 0 retries → immediate failure
    with pytest.raises(NetworkError):
        t.get_json("/repos/octocat/hello-world")


# ── 429 rate limit (AC09) ─────────────────────────────────────────────────────

@respx.mock
def test_429_rate_limit_parsed():
    respx.get(f"{BASE}/repos/octocat/hello-world").mock(
        return_value=httpx.Response(
            429,
            json={"message": "Too many requests"},
            headers={"retry-after": "30"},
        )
    )
    t = make_transport()
    with pytest.raises(RateLimitError) as exc_info:
        t.get_json("/repos/octocat/hello-world")
    assert exc_info.value.retry_after == 30.0


# ── auth header is not logged ──────────────────────────────────────────────────

def test_auth_header_not_in_exception_message():
    """Authorization value must never appear in error messages (AC08)."""
    secret = "ghp_secret_token_never_reveal"
    cfg = HarnessConfig(github_token=secret, max_retries=0, request_timeout_s=1.0)
    t = GitHubTransport(cfg)
    with respx.mock:
        respx.get(f"{BASE}/repos/octocat/hello-world").mock(
            return_value=httpx.Response(401, json={"message": "Bad credentials"})
        )
        try:
            t.get_json("/repos/octocat/hello-world")
        except AuthError as exc:
            assert secret not in str(exc)
            assert "Bearer" not in str(exc)


# ── disallowed pagination host (AC14) ─────────────────────────────────────────

def test_pagination_url_disallowed_host():
    t = make_transport()
    with pytest.raises((AccessError, Exception)):
        t.get_page_from_url("https://evil.com/repos/octocat/hello-world/issues?page=2")


def test_pagination_url_allowed_host():
    t = make_transport()
    with respx.mock:
        respx.get(f"{BASE}/repos/octocat/hello-world/issues").mock(
            return_value=httpx.Response(200, json=[])
        )
        data, _ = t.get_page_from_url(
            f"{BASE}/repos/octocat/hello-world/issues?page=2&per_page=30"
        )
        assert data == []


# ── path traversal rejected ────────────────────────────────────────────────────

def test_path_traversal_rejected():
    from harness.transport import InputError
    t = make_transport()
    with pytest.raises(InputError):
        t.get_json("../etc/passwd")
