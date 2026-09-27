"""
harness/service.py
──────────────────
IssueIntakeService – coordinates provider, store, and recorder.
Implements browse() and select_issue() as the PRD requires.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from .config import HarnessConfig, get_config
from .models import (
    IssueFilters,
    IssueRecord,
    IssuePage,
    IssueSnapshot,
    Repository,
    Source,
)
from .provider import GitHubIssueProvider, IssueProvider
from .recorder import EventRecorder
from .store import IssueStore

logger = logging.getLogger(__name__)


class IssueIntakeService:
    """
    High-level service used by both the interactive CLI and JSON mode.
    Coordinates provider, store, and recorder; never makes model calls.
    """

    def __init__(
        self,
        provider: IssueProvider | None = None,
        store: IssueStore | None = None,
        recorder: EventRecorder | None = None,
        config: HarnessConfig | None = None,
    ) -> None:
        self._cfg = config or get_config()
        self._provider = provider or GitHubIssueProvider(config=self._cfg)
        self._store = store or IssueStore(config=self._cfg)
        self._recorder = recorder or EventRecorder()

    # ── public API ─────────────────────────────────────────────────────────────

    def fetch_repository(self, owner: str, repo: str) -> Repository:
        """Fetch and cache repository metadata."""
        t0 = time.monotonic()
        try:
            result = self._provider.get_repository(owner, repo)
            self._store.save_repository(result)
            self._recorder.record_event("fetch_repository", t0, "ok", cache_outcome="saved")
            return result
        except Exception:
            self._recorder.record_event("fetch_repository", t0, "error")
            raise

    def browse(
        self,
        owner: str,
        repo: str,
        repository: Repository,
        filters: IssueFilters,
        cursor: str | None = None,
        use_cache: bool = True,
    ) -> IssuePage:
        """
        Fetch one page of issues.
        Checks cache first when use_cache=True (anonymous responses only).
        Never silently crawls all pages (FR05).
        """
        t0 = time.monotonic()
        cache_key_filters = filters.model_dump()

        # Cache lookup
        if use_cache and not self._cfg.has_github_token:
            cached = self._store.load_page(repository.repository_id, cache_key_filters, cursor)
            if cached:
                self._recorder.record_event("browse", t0, "ok", cache_outcome="hit")
                return cached

        # Live fetch
        page = self._provider.list_issues(owner, repo, filters, cursor)

        # Cache only anonymous responses
        if not self._cfg.has_github_token and page.fetch_error is None:
            self._store.save_page(repository.repository_id, page)
            self._recorder.record_event("browse", t0, "ok", cache_outcome="saved")
        else:
            outcome = "miss" if page.fetch_error else "skip_auth"
            self._recorder.record_event(
                "browse", t0,
                "partial" if page.fetch_error else "ok",
                cache_outcome=outcome,
            )

        return page

    def select_issue(
        self,
        owner: str,
        repo: str,
        repository: Repository,
        number: int,
    ) -> tuple[IssueRecord, IssueSnapshot]:
        """
        Fetch full issue details and create an immutable intake snapshot.
        Returns (issue_record, snapshot).
        """
        t0 = time.monotonic()
        issue = self._provider.get_issue(owner, repo, number)
        body_complete = len(issue.body) > 0 or True  # True = body received (possibly empty)

        snapshot = IssueSnapshot(
            repository=repository,
            issue=issue,
            source_updated_at=issue.updated_at,
            source=Source.live,
            body_complete=body_complete,
        )
        path = self._store.save_snapshot(snapshot)
        self._recorder.record_event("select_issue", t0, "ok", cache_outcome="saved")
        logger.info("Snapshot %s saved for issue #%d", snapshot.snapshot_id, number)
        return issue, snapshot

    def refresh(
        self,
        owner: str,
        repo: str,
        repository: Repository,
        filters: IssueFilters,
    ) -> IssuePage:
        """Force-fetch fresh data (ignore cache)."""
        return self.browse(owner, repo, repository, filters, use_cache=False)

    def clear_cache(self) -> int:
        n = self._store.clear_cache()
        logger.info("Cache cleared: %d page(s) removed.", n)
        return n

    def close(self) -> None:
        if isinstance(self._provider, GitHubIssueProvider):
            self._provider.close()
        self._store.close()
