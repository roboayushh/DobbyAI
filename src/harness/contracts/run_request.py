"""harness/contracts/run_request.py
Typed, validated preparation request contract.
Enforces all cross-field invariants before any acquisition starts.
"""
from __future__ import annotations

import json
import os
import re
from enum import Enum
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class TaskMode(str, Enum):
    SINGLE_ISSUE = "single_issue"
    REPOSITORY = "repository"


class ExecutionMode(str, Enum):
    DEVELOPMENT = "development"
    EVALUATION = "evaluation"


class RepositoryKind(str, Enum):
    LOCAL_GIT = "local_git"
    PUBLIC_HTTPS = "public_https"
    LOCAL_FOLDER = "local_folder"
    LOCAL_ZIP = "local_zip"


class RepositoryRefV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: RepositoryKind
    locator: str
    revision: Optional[str] = None
    expected_tree_sha256: Optional[str] = None

    @field_validator("locator")
    @classmethod
    def validate_locator(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Repository locator cannot be empty")
        return v

    @field_validator("expected_tree_sha256")
    @classmethod
    def validate_tree_sha(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            v = v.strip().lower()
            if not re.match(r"^[0-9a-f]{64}$", v):
                raise ValueError("expected_tree_sha256 must be a 64-character lowercase hex string")
        return v


class RepositoryQueryV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: str = "open"
    include_labels: List[str] = Field(default_factory=list)
    exclude_labels: List[str] = Field(default_factory=list)
    priority_labels: List[str] = Field(
        default_factory=lambda: ["priority:critical", "priority:high", "priority:medium"]
    )
    assignee: Optional[str] = None
    milestone: Optional[str] = None


class TaskInputV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issue_snapshot_id: Optional[str] = None
    issue_url: Optional[str] = None
    text: Optional[str] = None
    repository_query: Optional[RepositoryQueryV1] = None


class LimitsV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_tasks: int = Field(default=3, ge=1, le=20)
    max_discovery_items: int = Field(default=50, ge=1, le=500)
    max_issue_body_bytes: int = Field(default=200000, ge=1024, le=1000000)
    repo_acquire_timeout_seconds: int = Field(default=300, ge=10, le=1800)
    max_repo_bytes: int = Field(default=2147483648, ge=1048576, le=10737418240)
    max_file_count: int = Field(default=250000, ge=1, le=1000000)


class RunRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    idempotency_key: str = Field(min_length=8, max_length=128)
    task_mode: TaskMode
    execution_mode: ExecutionMode
    repository: RepositoryRefV1
    task: TaskInputV1
    limits: LimitsV1 = Field(default_factory=LimitsV1)
    runtime_profile: str = "local-default"
    evaluation_profile: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, v: str) -> str:
        if v != "1.0":
            raise ValueError(f"Unsupported schema_version: {v}. Must be exactly '1.0'")
        return v

    @field_validator("idempotency_key")
    @classmethod
    def validate_idempotency_key(cls, v: str) -> str:
        if not re.match(r"^[A-Za-z0-9_.-]+$", v):
            raise ValueError("idempotency_key must contain only URL-safe characters: [A-Za-z0-9_.-]")
        return v

    @model_validator(mode="after")
    def validate_cross_field_invariants(self) -> RunRequestV1:
        # 1. Metadata byte limit
        meta_canonical = json.dumps(self.metadata, sort_keys=True, separators=(",", ":"))
        if len(meta_canonical.encode("utf-8")) > 16384:
            raise ValueError("metadata exceeds 16 KiB limit when serialized")

        # 2. Repository validation
        if self.repository.kind in (RepositoryKind.LOCAL_GIT, RepositoryKind.LOCAL_FOLDER, RepositoryKind.LOCAL_ZIP):
            if not os.path.isabs(self.repository.locator):
                raise ValueError(f"{self.repository.kind.value} locator must be an absolute path, got: {self.repository.locator}")
            if self.repository.kind != RepositoryKind.LOCAL_GIT and self.repository.revision:
                raise ValueError(f"{self.repository.kind.value} sources have no revision; use expected_tree_sha256")
        elif self.repository.kind == RepositoryKind.PUBLIC_HTTPS:
            parsed = urlparse(self.repository.locator)
            if parsed.scheme.lower() != "https":
                raise ValueError(f"public_https locator must have https scheme, got: {parsed.scheme}")
            if parsed.username or parsed.password:
                raise ValueError("public_https locator must not contain embedded credentials/userinfo")
            if parsed.fragment:
                raise ValueError("public_https locator must not contain a URL fragment")
            if not parsed.netloc:
                raise ValueError("public_https locator must have a valid host")

        # 3. Task mode vs Task input
        if self.task_mode == TaskMode.SINGLE_ISSUE:
            if self.limits.max_tasks != 1:
                raise ValueError("single_issue mode requires limits.max_tasks to be 1")
            if self.task.repository_query is not None:
                raise ValueError("repository_query must be null in single_issue mode")

            single_inputs = [
                self.task.issue_snapshot_id is not None,
                self.task.issue_url is not None,
                self.task.text is not None,
            ]
            if sum(single_inputs) != 1:
                raise ValueError(
                    "single_issue mode requires exactly one of: issue_snapshot_id, issue_url, or text"
                )

        elif self.task_mode == TaskMode.REPOSITORY:
            if self.task.repository_query is None:
                raise ValueError("repository mode requires task.repository_query to be specified")
            if (
                self.task.issue_snapshot_id is not None
                or self.task.issue_url is not None
                or self.task.text is not None
            ):
                raise ValueError(
                    "repository mode cannot specify issue_snapshot_id, issue_url, or text"
                )
            if self.execution_mode == ExecutionMode.EVALUATION:
                # PRD Section 6.2: reject task_mode=repository with execution_mode=evaluation
                # unless an explicit evaluation profile declares cumulative queue is valid.
                allowed_eval_profiles = ["cumulative-evaluation"]
                if (
                    self.evaluation_profile is None
                    or self.evaluation_profile not in allowed_eval_profiles
                ):
                    raise ValueError(
                        "task_mode=repository is rejected with execution_mode=evaluation in default evaluation profile"
                    )

        return self
