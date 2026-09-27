"""harness/intake/existing_intake_port.py
Abstract interface / port isolating PRD 1 from the underlying intake storage and provider implementation.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple

from harness.models import IssueRecord, IssueSnapshot, Repository


class ExistingIssueIntakePort(ABC):
    @abstractmethod
    def get_snapshot(self, snapshot_id: str) -> Optional[IssueSnapshot]:
        """Retrieve an existing issue snapshot by its ID."""
        raise NotImplementedError

    @abstractmethod
    def resolve_url(self, issue_url: str) -> Tuple[IssueRecord, IssueSnapshot, Repository]:
        """Fetch and persist an issue from its GitHub URL."""
        raise NotImplementedError

    @abstractmethod
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
        """List candidate issues for repository queue discovery."""
        raise NotImplementedError
