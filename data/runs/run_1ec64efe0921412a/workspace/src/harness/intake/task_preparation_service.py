"""harness/intake/task_preparation_service.py
TaskPreparationService – orchestrates single-issue and bounded repository queue preparation.
Implements deterministic selection, filtering, deduplication, and ordering per PRD section 8.1.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

from harness.contracts import (
    LimitsV1,
    QueueSummaryV1,
    RepositoryQueryV1,
    TaskInputV1,
    TaskMode,
    TaskSpecV1,
)
from harness.intake.existing_intake_port import ExistingIssueIntakePort
from harness.intake.task_normalizer import TaskNormalizer
from harness.models import IssueRecord


class TaskPreparationService:
    def __init__(self, intake_port: ExistingIssueIntakePort):
        self._intake = intake_port

    def prepare_single(self, task_input: TaskInputV1) -> Tuple[TaskSpecV1, QueueSummaryV1]:
        """Prepare exactly one task for single_issue mode."""
        if task_input.issue_snapshot_id:
            snapshot = self._intake.get_snapshot(task_input.issue_snapshot_id)
            if not snapshot:
                raise ValueError(f"Issue snapshot not found: {task_input.issue_snapshot_id}")
            task = TaskNormalizer.from_issue(snapshot, ordinal=0)
        elif task_input.issue_url:
            issue, snapshot, repo = self._intake.resolve_url(task_input.issue_url)
            task = TaskNormalizer.from_issue(snapshot, ordinal=0)
        elif task_input.text:
            task = TaskNormalizer.from_text(task_input.text, ordinal=0)
        else:
            raise ValueError("No valid task input provided for single_issue mode")

        summary = QueueSummaryV1(
            discovered=1,
            excluded=0,
            duplicates=0,
            selected=1,
            remaining=0,
        )
        return task, summary

    def prepare_queue(
        self,
        owner: str,
        repo: str,
        query: RepositoryQueryV1,
        limits: LimitsV1,
    ) -> Tuple[List[TaskSpecV1], QueueSummaryV1, Dict[str, Any]]:
        """Prepare a bounded, deduplicated, priority-ordered task queue per PRD Section 8.1."""
        discovered_count = 0
        excluded_count = 0
        duplicate_count = 0

        # 1. Fetch up to max_discovery_items
        candidates: List[IssueRecord] = []
        cursor: Optional[str] = None
        per_page = min(100, limits.max_discovery_items)

        while len(candidates) < limits.max_discovery_items:
            batch, next_cursor = self._intake.list_candidates(
                owner=owner,
                repo=repo,
                state=query.state,
                include_labels=query.include_labels,
                exclude_labels=query.exclude_labels,
                cursor=cursor,
                limit=per_page,
            )
            if not batch:
                break

            for item in batch:
                discovered_count += 1
                # 2. Exclude pull requests (already filtered by list_candidates or check here)
                if getattr(item, "is_pull_request", False):
                    excluded_count += 1
                    continue
                # Exclude entries lacking a usable title
                if not item.title or not item.title.strip():
                    excluded_count += 1
                    continue
                candidates.append(item)
                if len(candidates) >= limits.max_discovery_items:
                    break

            if not next_cursor or len(candidates) >= limits.max_discovery_items:
                break
            cursor = next_cursor

        # If candidates were returned directly without discovered_count incremented (e.g. mock)
        if discovered_count < len(candidates):
            discovered_count = len(candidates)

        # 3. Deduplicate by canonical remote identity, then by normalized-content hash
        seen_keys: Set[str] = set()
        seen_content_hashes: Set[str] = set()
        unique_tasks: List[TaskSpecV1] = []

        for issue in candidates:
            # Check body length limit
            body_bytes = len((issue.body or "").encode("utf-8"))
            if body_bytes > limits.max_issue_body_bytes:
                excluded_count += 1
                continue

            task = TaskNormalizer.from_issue_record(issue, owner, repo)

            if task.source_key in seen_keys:
                duplicate_count += 1
                continue
            if task.normalized_content_sha256 in seen_content_hashes:
                duplicate_count += 1
                continue

            seen_keys.add(task.source_key)
            seen_content_hashes.add(task.normalized_content_sha256)
            unique_tasks.append(task)

        # 4. Sort by explicit priority label mapping, then oldest creation time, then numeric issue identifier
        priority_map = {lbl.lower(): idx for idx, lbl in enumerate(query.priority_labels)}

        def sort_key(t: TaskSpecV1) -> Tuple[int, str, int]:
            # Priority label rank (lowest index wins)
            prio_rank = len(query.priority_labels)
            for lbl in t.labels:
                lbl_lower = lbl.lower()
                if lbl_lower in priority_map:
                    prio_rank = min(prio_rank, priority_map[lbl_lower])

            created_str = t.remote_created_at or ""
            # Extract numeric issue number from source_key if possible
            num = 0
            if ":issue:" in t.source_key:
                try:
                    num = int(t.source_key.split(":issue:")[-1])
                except ValueError:
                    num = 0
            return (prio_rank, created_str, num)

        unique_tasks.sort(key=sort_key)

        # 5. Select the first max_tasks items
        selected_tasks = unique_tasks[: limits.max_tasks]
        remaining_count = max(0, len(unique_tasks) - len(selected_tasks))

        # Re-assign ordinals
        for idx, task in enumerate(selected_tasks):
            task.ordinal = idx

        summary = QueueSummaryV1(
            discovered=discovered_count,
            excluded=excluded_count,
            duplicates=duplicate_count,
            selected=len(selected_tasks),
            remaining=remaining_count,
        )

        manifest_metadata = {
            "query": query.model_dump(mode="json"),
            "limits": limits.model_dump(mode="json"),
            "discovered": discovered_count,
            "excluded": excluded_count,
            "duplicates": duplicate_count,
            "selected": len(selected_tasks),
            "remaining": remaining_count,
        }

        return selected_tasks, summary, manifest_metadata
