"""tests/test_schemas.py
Verify that example objects and contracts match published JSON Schemas in schemas/v1/.
"""
import json
from pathlib import Path

import pytest

from harness.contracts import (
    ErrorDetailsV1,
    ErrorResultV1,
    ExecutionMode,
    LimitsV1,
    PreparedRunResultV1,
    QueueSummaryV1,
    RepositoryKind,
    RepositoryRefV1,
    RunRequestV1,
    SourceIdentityV1,
    SourceSummaryV1,
    TaskInputV1,
    TaskMode,
    TaskSpecV1,
    TaskSummaryV1,
    WorkspaceSummaryV1,
)


def get_schema(name: str) -> dict:
    schema_path = Path(__file__).parent.parent / "schemas" / "v1" / name
    assert schema_path.is_file(), f"Schema file not found: {schema_path}"
    return json.loads(schema_path.read_text(encoding="utf-8"))


def test_run_request_schema():
    schema = get_schema("run_request.json")
    assert schema["title"] == "RunRequestV1"
    req = RunRequestV1(
        schema_version="1.0",
        idempotency_key="idemp-schema-check",
        task_mode=TaskMode.SINGLE_ISSUE,
        execution_mode=ExecutionMode.DEVELOPMENT,
        repository=RepositoryRefV1(
            kind=RepositoryKind.LOCAL_GIT,
            locator="/absolute/path/to/repo",
        ),
        task=TaskInputV1(text="Hello task"),
        limits=LimitsV1(max_tasks=1),
    )
    req_dict = req.model_dump(mode="json")
    for req_field in schema["required"]:
        assert req_field in req_dict


def test_task_spec_schema():
    schema = get_schema("task_spec.json")
    assert schema["title"] == "TaskSpecV1"
    task = TaskSpecV1(
        schema_version="1.0",
        task_id="tsk_01JTEST",
        ordinal=0,
        source_type="direct_text",
        source_key="direct_text:abc",
        title="Test Task",
        body="Body text",
        labels=["bug"],
        remote_state="open",
        raw_content_sha256="a" * 64,
        normalized_content_sha256="b" * 64,
        dependencies=[],
        trust="untrusted_input",
    )
    task_dict = task.model_dump(mode="json")
    for req_field in schema["required"]:
        assert req_field in task_dict


def test_source_identity_schema():
    schema = get_schema("source_identity.json")
    assert schema["title"] == "SourceIdentityV1"
    src = SourceIdentityV1(
        schema_version="1.0",
        source_kind="local_git",
        canonical_locator="/path/to/repo",
        baseline_commit="40hex" + "0" * 35,
        baseline_tree="40hex" + "0" * 35,
        content_tree_sha256="c" * 64,
        import_manifest_sha256="d" * 64,
        dirty_source_imported=False,
        created_at="2026-09-27T00:00:00Z",
    )
    src_dict = src.model_dump(mode="json")
    for req_field in schema["required"]:
        assert req_field in src_dict


def test_prepared_result_schema():
    schema = get_schema("prepared_result.json")
    assert schema["title"] == "PreparedRunResultV1"
    res = PreparedRunResultV1(
        schema_version="1.0",
        run_id="run_01JTEST",
        status="PREPARED",
        task_mode=TaskMode.SINGLE_ISSUE,
        execution_mode=ExecutionMode.DEVELOPMENT,
        source=SourceSummaryV1(
            baseline_commit="40hex" + "0" * 35,
            content_tree_sha256="e" * 64,
        ),
        workspace=WorkspaceSummaryV1(
            workspace_id="wsp_01JTEST",
            relative_root="runs/run_01JTEST/workspace",
            writable=False,
        ),
        tasks=[TaskSummaryV1(task_id="tsk_01JTEST", ordinal=0, state="QUEUED")],
        queue_summary=QueueSummaryV1(
            discovered=1, excluded=0, duplicates=0, selected=1, remaining=0
        ),
        artifacts=[],
        created_at="2026-09-27T00:00:00Z",
    )
    res_dict = res.model_dump(mode="json")
    for req_field in schema["required"]:
        assert req_field in res_dict


def test_error_result_schema():
    schema = get_schema("error_result.json")
    assert schema["title"] == "ErrorResultV1"
    err = ErrorResultV1(
        schema_version="1.0",
        status="FAILED",
        run_id=None,
        error=ErrorDetailsV1(
            code="REPOSITORY_REVISION_NOT_FOUND",
            message="The requested revision could not be resolved.",
            retryable=False,
            details={},
        ),
    )
    err_dict = err.model_dump(mode="json")
    for req_field in schema["required"]:
        assert req_field in err_dict
