"""harness/intake/existing_intake_adapter.py
Adapter implementing ExistingIssueIntakePort by delegating to existing IssueIntakeService and IssueStore.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from harness.config import HarnessConfig, get_config
from harness.intake.existing_intake_port import ExistingIssueIntakePort
from harness.models import IssueFilters, IssueRecord, IssueSnapshot, Repository
from harness.service import IssueIntakeService
from harness.store import IssueStore
from harness.validator import InputValidator, InputError


class ExistingIssueIntakeAdapter(ExistingIssueIntakePort):
    def __init__(
        self,
        service: Optional[IssueIntakeService] = None,
        store: Optional[IssueStore] = None,
        config: Optional[HarnessConfig] = None,
    ):
        self._cfg = config or get_config()
        self._service = service or IssueIntakeService(config=self._cfg)
        self._store = store or IssueStore(config=self._cfg)

    def get_snapshot(self, snapshot_id: str) -> Optional[IssueSnapshot]:
        return self._store.load_snapshot(snapshot_id)

    def resolve_url(self, issue_url: str) -> Tuple[IssueRecord, IssueSnapshot, Repository]:
        issue_ref = InputValidator.parse_issue_ref(issue_url)
        if not issue_ref:
            raise ValueError(f"URL is not a GitHub issue: {issue_url}")

        repo = self._service.fetch_repository(issue_ref.owner, issue_ref.repo)
        issue, snapshot = self._service.select_issue(
            issue_ref.owner, issue_ref.repo, repo, issue_ref.number
        )
        return issue, snapshot, repo

    def list_candidates(
        self,
        owner: str,
        repo: str,
        state: str = "open",
        include_labels: Optional[List[str]] = None,
        exclude_labels: Optional[List[str]] = None,
        cursor: Optional[str] = None,
        limit: int = 50,
    ) -> Tuple[List[IssueRecord], Optional[str]]:
        repo_obj = self._service.fetch_repository(owner, repo)
        filters = IssueFilters(
            state=state,
            labels=include_labels or [],
        )
        page = self._service.browse(
            owner=owner,
            repo=repo,
            repository=repo_obj,
            filters=filters,
            cursor=cursor,
            use_cache=True,
        )

        candidates: List[IssueRecord] = []
        exclude_set = set(exclude_labels or [])

        for item in page.items:
            # Exclude pull requests (PRD 8.1 item 2, AT-005)
            if item.is_pull_request:
                continue
            # Exclude items lacking a usable title
            if not item.title or not item.title.strip():
                continue
            # Apply exclude_labels
            if exclude_set and any(lbl in exclude_set for lbl in item.labels):
                continue
            candidates.append(item)

        return candidates, page.next_cursor
