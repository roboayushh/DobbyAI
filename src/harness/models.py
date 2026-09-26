"""
harness/models.py
─────────────────
Pydantic data contracts for all normalized records.
Matches the PRD "Data memory and interface contracts" section.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator


# ── helpers ──────────────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _new_id() -> str:
    return str(uuid.uuid4())


# ── enums ────────────────────────────────────────────────────────────────────

class IssueState(str, Enum):
    open = "open"
    closed = "closed"
    all = "all"


class Source(str, Enum):
    live = "live"
    cache = "cache"


class CommentsStatus(str, Enum):
    not_fetched = "not_fetched"


class Completeness(str, Enum):
    complete = "complete"
    partial = "partial"
    empty = "empty"


class EnvelopeStatus(str, Enum):
    ok = "ok"
    partial = "partial"
    error = "error"


# ── core records ─────────────────────────────────────────────────────────────

class Repository(BaseModel):
    """Normalized repository record (FR requirement: Repository record)."""
    provider: str = "github"
    repository_id: int
    owner: str
    name: str
    full_name: str
    html_url: str
    visibility: str          # "public" | "private" | "internal"
    default_branch: str
    fetched_at: datetime = Field(default_factory=_utcnow)


class IssueRecord(BaseModel):
    """Normalized issue record."""
    issue_id: int
    repository_id: int
    number: int
    title: str
    body: str                # null bodies become empty string (FR06)
    state: str
    author: str | None       # login, may be null if user is deleted
    labels: list[str] = Field(default_factory=list)
    assignees: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    html_url: str
    comments_count: int

    @model_validator(mode="before")
    @classmethod
    def _coerce_null_body(cls, data: dict[str, Any]) -> dict[str, Any]:
        if isinstance(data, dict) and data.get("body") is None:
            data["body"] = ""
        return data


class IssuePage(BaseModel):
    """A single page of issues from the provider."""
    issues: list[IssueRecord]
    filters: dict[str, Any]          # state, labels, etc.
    next_cursor: str | None = None   # opaque pagination token (URL)
    has_more: bool = False
    fetched_at: datetime = Field(default_factory=_utcnow)
    source: Source = Source.live
    completeness: Completeness = Completeness.complete
    fetch_error: str | None = None


class IssueSnapshot(BaseModel):
    """Immutable intake snapshot saved to disk (FR/AC07)."""
    schema_version: str = "1.0"
    snapshot_id: str = Field(default_factory=_new_id)
    repository: Repository
    issue: IssueRecord
    fetched_at: datetime = Field(default_factory=_utcnow)
    source_updated_at: datetime
    source: Source = Source.live
    body_complete: bool
    comments_status: CommentsStatus = CommentsStatus.not_fetched
    content_hash: str = ""

    @model_validator(mode="after")
    def _compute_hash(self) -> "IssueSnapshot":
        if not self.content_hash:
            raw = (
                f"{self.issue.issue_id}:{self.issue.updated_at.isoformat()}"
                f":{len(self.issue.body)}"
            )
            self.content_hash = hashlib.sha256(raw.encode()).hexdigest()
        return self


# ── JSON output envelope ──────────────────────────────────────────────────────

class JSONEnvelope(BaseModel):
    """Machine-readable output wrapper."""
    schema_version: str = "1.0"
    status: EnvelopeStatus
    data: Any | None = None
    error: str | None = None


# ── filter params ─────────────────────────────────────────────────────────────

class IssueFilters(BaseModel):
    state: IssueState = IssueState.open
    labels: list[str] = Field(default_factory=list)
    page: int = 1
    per_page: int = 30

    def as_api_params(self) -> dict[str, str | int]:
        params: dict[str, str | int] = {
            "state": self.state.value,
            "sort": "updated",
            "direction": "desc",
            "per_page": self.per_page,
            "page": self.page,
        }
        if self.labels:
            params["labels"] = ",".join(self.labels)
        return params

    def reset_page(self) -> "IssueFilters":
        return self.model_copy(update={"page": 1})
