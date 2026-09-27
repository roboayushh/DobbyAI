"""harness/intake/task_normalizer.py
TaskNormalizer converts raw intake (IssueSnapshot, IssueRecord, direct text)
into an immutable, terminal-safe TaskSpecV1 with both raw and normalized content hashes.
"""
from __future__ import annotations

import hashlib
import uuid
from typing import List, Optional

from harness.contracts import (
    TaskSpecV1,
    compute_sha256,
    normalize_content,
)
from harness.models import IssueRecord, IssueSnapshot


class TaskNormalizer:
    @staticmethod
    def from_issue(snapshot: IssueSnapshot, ordinal: int = 0) -> TaskSpecV1:
        issue = snapshot.issue
        repo = snapshot.repository
        raw_body = issue.body or ""
        normalized_body = normalize_content(raw_body)

        source_key = f"github:{repo.owner}/{repo.name}:issue:{issue.number}"
        task_id = f"tsk_{uuid.uuid4().hex[:16]}"

        labels = [lbl.name if hasattr(lbl, "name") else str(lbl) for lbl in issue.labels]

        return TaskSpecV1(
            schema_version="1.0",
            task_id=task_id,
            ordinal=ordinal,
            source_type="github_issue",
            source_key=source_key,
            source_snapshot_id=snapshot.snapshot_id,
            title=issue.title,
            body=normalized_body,
            labels=labels,
            remote_state=issue.state,
            remote_created_at=issue.created_at.isoformat() if issue.created_at else None,
            remote_updated_at=issue.updated_at.isoformat() if issue.updated_at else None,
            raw_content_sha256=compute_sha256(raw_body),
            normalized_content_sha256=compute_sha256(normalized_body),
            dependencies=[],
            trust="untrusted_input",
        )

    @staticmethod
    def from_issue_record(
        issue: IssueRecord,
        owner: str,
        repo: str,
        ordinal: int = 0,
        snapshot_id: Optional[str] = None,
    ) -> TaskSpecV1:
        raw_body = issue.body or ""
        normalized_body = normalize_content(raw_body)

        source_key = f"github:{owner}/{repo}:issue:{issue.number}"
        task_id = f"tsk_{uuid.uuid4().hex[:16]}"

        labels = [lbl.name if hasattr(lbl, "name") else str(lbl) for lbl in issue.labels]

        return TaskSpecV1(
            schema_version="1.0",
            task_id=task_id,
            ordinal=ordinal,
            source_type="github_issue",
            source_key=source_key,
            source_snapshot_id=snapshot_id,
            title=issue.title,
            body=normalized_body,
            labels=labels,
            remote_state=issue.state,
            remote_created_at=issue.created_at.isoformat() if issue.created_at else None,
            remote_updated_at=issue.updated_at.isoformat() if issue.updated_at else None,
            raw_content_sha256=compute_sha256(raw_body),
            normalized_content_sha256=compute_sha256(normalized_body),
            dependencies=[],
            trust="untrusted_input",
        )

    @staticmethod
    def from_text(
        text: str,
        title: Optional[str] = None,
        ordinal: int = 0,
    ) -> TaskSpecV1:
        raw_body = text or ""
        normalized_body = normalize_content(raw_body)
        raw_sha = compute_sha256(raw_body)
        norm_sha = compute_sha256(normalized_body)

        effective_title = title if title and title.strip() else (normalized_body.split("\n")[0][:80] or "Direct Task")
        source_key = f"direct_text:{norm_sha}"
        task_id = f"tsk_{uuid.uuid4().hex[:16]}"

        return TaskSpecV1(
            schema_version="1.0",
            task_id=task_id,
            ordinal=ordinal,
            source_type="direct_text",
            source_key=source_key,
            source_snapshot_id=None,
            title=effective_title,
            body=normalized_body,
            labels=[],
            remote_state="open",
            remote_created_at=None,
            remote_updated_at=None,
            raw_content_sha256=raw_sha,
            normalized_content_sha256=norm_sha,
            dependencies=[],
            trust="untrusted_input",
        )
