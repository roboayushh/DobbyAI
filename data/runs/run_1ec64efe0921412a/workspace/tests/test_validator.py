"""
tests/test_validator.py
AC03 – Input normalization: supported formats resolve; bad inputs fail before HTTP.
"""
import pytest
from harness.validator import InputValidator, InputError, RepoRef, IssueRef


# ── valid repository inputs ───────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected_owner,expected_name", [
    ("octocat/hello-world", "octocat", "hello-world"),
    ("octocat/hello-world/", "octocat", "hello-world"),
    ("https://github.com/octocat/hello-world", "octocat", "hello-world"),
    ("https://github.com/octocat/hello-world/", "octocat", "hello-world"),
    ("https://github.com/octocat/hello-world.git", "octocat", "hello-world"),
    ("https://github.com/octocat/hello-world/issues", "octocat", "hello-world"),
    ("https://github.com/octocat/hello-world/tree/main", "octocat", "hello-world"),
])
def test_parse_repo_ref_valid(raw, expected_owner, expected_name):
    ref = InputValidator.parse_repo_ref(raw)
    assert ref.owner == expected_owner
    assert ref.name == expected_name
    assert ref.full_name == f"{expected_owner}/{expected_name}"


# ── invalid repository inputs ─────────────────────────────────────────────────

@pytest.mark.parametrize("raw", [
    "",
    "    ",
    "just-a-name",
    "https://gitlab.com/octocat/repo",         # wrong host
    "http://github.com/octocat/repo",           # http (not https)
    "https://user:pass@github.com/o/r",         # embedded credentials
    "https://github.com/",                      # no owner/repo
    "ftp://github.com/o/r",                     # bad scheme
])
def test_parse_repo_ref_invalid(raw):
    with pytest.raises((InputError, Exception)):
        InputValidator.parse_repo_ref(raw)


# ── direct issue URLs ─────────────────────────────────────────────────────────

def test_parse_issue_ref_valid():
    url = "https://github.com/octocat/hello-world/issues/42"
    ref = InputValidator.parse_issue_ref(url)
    assert isinstance(ref, IssueRef)
    assert ref.owner == "octocat"
    assert ref.repo == "hello-world"
    assert ref.number == 42


def test_parse_issue_ref_returns_none_for_plain_repo():
    ref = InputValidator.parse_issue_ref("octocat/hello-world")
    assert ref is None


def test_parse_issue_ref_returns_none_for_repo_url():
    ref = InputValidator.parse_issue_ref("https://github.com/octocat/hello-world")
    assert ref is None


# ── pull request URLs rejected before HTTP ────────────────────────────────────

def test_parse_issue_ref_rejects_pr_url():
    with pytest.raises(InputError, match="[Pp]ull [Rr]equest"):
        InputValidator.parse_issue_ref("https://github.com/octocat/hello-world/pull/99")


# ── invalid issue numbers ─────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "https://github.com/octocat/hello-world/issues/0",
    "https://github.com/octocat/hello-world/issues/abc",
    "https://github.com/octocat/hello-world/issues/-1",
])
def test_parse_issue_ref_bad_numbers(url):
    with pytest.raises(InputError):
        InputValidator.parse_issue_ref(url)


# ── unsupported hosts rejected ────────────────────────────────────────────────

def test_parse_repo_ref_rejects_enterprise():
    with pytest.raises(InputError, match="Unsupported host"):
        InputValidator.parse_repo_ref("https://github.enterprise.com/owner/repo")
