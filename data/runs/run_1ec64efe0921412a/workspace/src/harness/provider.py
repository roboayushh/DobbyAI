"""
harness/provider.py
───────────────────
IssueProvider protocol + GitHubIssueProvider implementation.
Responsible for:
  • Fetching repository metadata (GET /repos/{owner}/{repo})
  • Fetching paginated issue lists (GET /repos/{owner}/{repo}/issues)
  • Fetching issue details (GET /repos/{owner}/{repo}/issues/{number})
  • Filtering out pull_request objects (FR03)
  • Following validated Link-header pagination (FR04)
  • Returning structured IssuePage with has_more and next_cursor (FR05)
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

import httpx
from pydantic import ValidationError

from .config import HarnessConfig, get_config
from .models import (
    Completeness,
    IssueFilters,
    IssueRecord,
    IssuePage,
    Repository,
    Source,
)
from .transport import GitHubTransport, NetworkError

logger = logging.getLogger(__name__)

_LINK_NEXT_RE = re.compile(r'<([^>]+)>;\s*rel="next"')


# ── protocol ──────────────────────────────────────────────────────────────────

@runtime_checkable
class IssueProvider(Protocol):
    def get_repository(self, owner: str, repo: str) -> Repository: ...
    def list_issues(self, owner: str, repo: str, filters: IssueFilters, cursor: str | None) -> IssuePage: ...
    def get_issue(self, owner: str, repo: str, number: int) -> IssueRecord: ...


# ── GitHub implementation ─────────────────────────────────────────────────────

class GitHubIssueProvider:
    """Reads from GitHub REST API; never modifies any repository."""

    def __init__(
        self,
        transport: GitHubTransport | None = None,
        config: HarnessConfig | None = None,
    ) -> None:
        self._cfg = config or get_config()
        self._transport = transport or GitHubTransport(self._cfg)

    # ── IssueProvider interface ──────────────────────────────────────────────

    def get_repository(self, owner: str, repo: str) -> Repository:
        """Fetch and normalize repository metadata."""
        data, _ = self._transport.get_json(f"/repos/{owner}/{repo}")
        assert isinstance(data, dict)
        return self._parse_repository(data)

    def list_issues(
        self,
        owner: str,
        repo: str,
        filters: IssueFilters,
        cursor: str | None = None,
    ) -> IssuePage:
        """
        Fetch one page of issues.
        If cursor (a next-page URL) is provided, follow it instead of
        rebuilding params from scratch, so pagination is stable.
        """
        try:
            if cursor:
                raw, headers = self._transport.get_page_from_url(cursor)
            else:
                params = filters.as_api_params()
                raw, headers = self._transport.get_json(
                    f"/repos/{owner}/{repo}/issues", params=params
                )
        except NetworkError as exc:
            return IssuePage(
                issues=[],
                filters=filters.model_dump(),
                completeness=Completeness.partial,
                fetch_error=str(exc),
            )

        assert isinstance(raw, list)

        issues: list[IssueRecord] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            # FR03: exclude pull requests
            if "pull_request" in item:
                logger.debug("Skipping PR #%s", item.get("number"))
                continue
            record = self._parse_issue(item)
            if record:
                issues.append(record)

        next_url = self._extract_next_link(headers)
        return IssuePage(
            issues=issues,
            filters=filters.model_dump(),
            next_cursor=next_url,
            has_more=next_url is not None,
            source=Source.live,
            completeness=Completeness.complete,
        )

    def get_issue(self, owner: str, repo: str, number: int) -> IssueRecord:
        """Fetch and normalize a single issue's details (FR06)."""
        data, _ = self._transport.get_json(f"/repos/{owner}/{repo}/issues/{number}")
        assert isinstance(data, dict)
        # Reject if it is a pull request
        if "pull_request" in data:
            from .transport import AccessError
            raise AccessError(
                f"#{number} is a pull request, not an issue. "
                "Pull request inputs are not supported."
            )
        record = self._parse_issue(data)
        if record is None:
            raise NetworkError(f"Failed to parse issue #{number} from API response.")
        return record

    def close(self) -> None:
        self._transport.close()

    # ── parsers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_repository(data: dict) -> Repository:
        owner = data.get("owner", {}) or {}
        return Repository(
            repository_id=int(data["id"]),
            owner=owner.get("login", ""),
            name=data.get("name", ""),
            full_name=data.get("full_name", ""),
            html_url=data.get("html_url", ""),
            visibility=data.get("visibility", "public"),
            default_branch=data.get("default_branch", "main"),
        )

    @staticmethod
    def _parse_issue(data: dict) -> IssueRecord | None:
        try:
            labels = [
                (lbl.get("name", "") if isinstance(lbl, dict) else str(lbl))
                for lbl in (data.get("labels") or [])
            ]
            assignees = [
                (a.get("login", "") if isinstance(a, dict) else str(a))
                for a in (data.get("assignees") or [])
            ]
            user = data.get("user") or {}
            author: str | None = user.get("login") if isinstance(user, dict) else None

            return IssueRecord(
                issue_id=int(data["id"]),
                repository_id=int(data.get("repository", {}).get("id", 0)) if isinstance(data.get("repository"), dict) else 0,
                number=int(data["number"]),
                title=data.get("title", ""),
                body=data.get("body"),  # null bodies → "" via model validator
                state=data.get("state", "open"),
                author=author,
                labels=labels,
                assignees=assignees,
                created_at=_parse_dt(data.get("created_at")),
                updated_at=_parse_dt(data.get("updated_at")),
                html_url=data.get("html_url", ""),
                comments_count=int(data.get("comments", 0)),
            )
        except (KeyError, ValueError, ValidationError) as exc:
            logger.warning("Failed to parse issue: %s", exc)
            return None

    @staticmethod
    def _extract_next_link(headers: httpx.Headers) -> str | None:
        """Parse RFC 5988 Link header for rel="next" URL."""
        link = headers.get("link", "")
        match = _LINK_NEXT_RE.search(link)
        if match:
            url = match.group(1)
            # Validate it stays on api.github.com (AC14 / guardrails)
            from urllib.parse import urlparse
            parsed = urlparse(url)
            if parsed.netloc.lower() in ("api.github.com", ""):
                return url
            logger.warning("Discarding next link with disallowed host: %s", parsed.netloc)
        return None


def _parse_dt(value: str | None) -> datetime:
    if not value:
        return datetime.now(tz=timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(tz=timezone.utc)
