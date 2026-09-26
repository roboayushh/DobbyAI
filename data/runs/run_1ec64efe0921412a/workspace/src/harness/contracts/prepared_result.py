"""harness/contracts/prepared_result.py
PreparedRunResultV1 and related result/state contracts.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional, Set

from pydantic import BaseModel, ConfigDict, Field

from .run_request import ExecutionMode, TaskMode
from .task_spec import TaskState


class RunState(str, Enum):
    NEW = "NEW"
    VALIDATING = "VALIDATING"
    ACQUIRING = "ACQUIRING"
    PREPARING = "PREPARING"
    PREPARED = "PREPARED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


ALLOWED_TRANSITIONS: Dict[RunState, Set[RunState]] = {
    RunState.NEW: {RunState.VALIDATING, RunState.CANCELLED},
    RunState.VALIDATING: {RunState.ACQUIRING, RunState.BLOCKED, RunState.CANCELLED},
    RunState.ACQUIRING: {RunState.PREPARING, RunState.BLOCKED, RunState.FAILED, RunState.CANCELLED},
    RunState.PREPARING: {RunState.PREPARED, RunState.FAILED, RunState.CANCELLED},
    RunState.PREPARED: set(),
    RunState.BLOCKED: set(),
    RunState.FAILED: set(),
    RunState.CANCELLED: set(),
}


class SourceSummaryV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    upstream_commit: Optional[str] = None
    baseline_commit: str
    content_tree_sha256: str


class WorkspaceSummaryV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    relative_root: str
    writable: bool = False


class TaskSummaryV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    ordinal: int
    state: TaskState


class QueueSummaryV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    discovered: int = Field(ge=0)
    excluded: int = Field(ge=0)
    duplicates: int = Field(ge=0)
    selected: int = Field(ge=0)
    remaining: int = Field(ge=0)


class ArtifactSummaryV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    sha256: str
    relative_path: str


class PreparedRunResultV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    run_id: str
    status: str = "PREPARED"
    task_mode: TaskMode
    execution_mode: ExecutionMode
    source: SourceSummaryV1
    workspace: WorkspaceSummaryV1
    tasks: List[TaskSummaryV1]
    queue_summary: QueueSummaryV1
    artifacts: List[ArtifactSummaryV1]
    created_at: str


class ErrorDetailsV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    retryable: bool = False
    details: Dict[str, Any] = Field(default_factory=dict)


class ErrorResultV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    status: str
    run_id: Optional[str] = None
    error: ErrorDetailsV1
