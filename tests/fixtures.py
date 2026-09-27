"""
tests/fixtures.py – shared test data factories.
"""
from __future__ import annotations

from datetime import datetime, timezone

REPO_JSON = {
    "id": 123456789,
    "name": "hello-world",
    "full_name": "octocat/hello-world",
    "owner": {"login": "octocat", "id": 1},
    "html_url": "https://github.com/octocat/hello-world",
    "visibility": "public",
    "default_branch": "main",
}

ISSUE_JSON = {
    "id": 111222333,
    "number": 42,
    "title": "Fix the flux capacitor",
    "body": "The flux capacitor is **broken**. Please fix it.\n\nSteps to reproduce:\n1. Run `time-machine --go-back`",
    "state": "open",
    "user": {"login": "marty"},
    "labels": [{"name": "bug"}, {"name": "urgent"}],
    "assignees": [{"login": "doc"}],
    "created_at": "2024-01-15T10:00:00Z",
    "updated_at": "2024-06-20T15:30:00Z",
    "html_url": "https://github.com/octocat/hello-world/issues/42",
    "comments": 3,
}

PR_JSON = {
    "id": 999000111,
    "number": 99,
    "title": "Feat: add flux capacitor v2",
    "body": "This PR adds flux capacitor v2.",
    "state": "open",
    "user": {"login": "einstein"},
    "labels": [],
    "assignees": [],
    "created_at": "2024-02-01T08:00:00Z",
    "updated_at": "2024-06-21T09:00:00Z",
    "html_url": "https://github.com/octocat/hello-world/pull/99",
    "comments": 0,
    "pull_request": {"url": "https://api.github.com/repos/octocat/hello-world/pulls/99"},
}

NULL_BODY_ISSUE_JSON = {
    **ISSUE_JSON,
    "id": 222333444,
    "number": 55,
    "body": None,
    "title": "Issue with no body",
}

DELETED_USER_ISSUE_JSON = {
    **ISSUE_JSON,
    "id": 333444555,
    "number": 77,
    "user": None,
    "title": "Issue from deleted user",
}
