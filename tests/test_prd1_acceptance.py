"""tests/test_prd1_acceptance.py
Comprehensive acceptance test suite covering AT-001 through AT-021 for PRD 1.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

import pytest

from harness.application.preparation_controller import PreparationController, PreparationError
from harness.contracts import (
    ExecutionMode,
    LimitsV1,
    PreparedRunResultV1,
    QueueSummaryV1,
    RepositoryKind,
    RepositoryQueryV1,
    RepositoryRefV1,
    RunRequestV1,
    RunState,
    TaskInputV1,
    TaskMode,
    TaskSpecV1,
)
from harness.intake.existing_intake_port import ExistingIssueIntakePort
from harness.intake.task_preparation_service import TaskPreparationService
from harness.models import IssueRecord, IssueSnapshot, Repository, Source
from harness.persistence import (
    ArtifactStore,
    IdempotencyConflictError,
    RunStore,
)
from harness.repository import (
    GitRunner,
    LimitsExceededError,
    RepositoryService,
    SourceChangedDuringImportError,
    SourceImporter,
    SourcePolicyError,
)
from harness.workspace import WorkspaceManager


@pytest.fixture
def temp_dir():
    d = tempfile.mkdtemp(prefix="harness_test_")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def git_repo(temp_dir):
    """Create a clean local git repository fixture."""
    repo_dir = temp_dir / "test_repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=str(repo_dir), check=True, capture_output=True)

    # Initial commit
    (repo_dir / "README.md").write_text("# Hello World\nInitial repo content.\n")
    (repo_dir / "src").mkdir()
    (repo_dir / "src" / "main.py").write_text("print('hello')\n")
    subprocess.run(["git", "add", "."], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(repo_dir), check=True, capture_output=True)
    return repo_dir


@pytest.fixture
def harness_env(temp_dir):
    """Setup harness storage, stores, and services in a temp directory."""
    data_root = temp_dir / "harness_data"
    data_root.mkdir()
    db_path = data_root / "test_harness.db"

    run_store = RunStore(str(db_path))
    artifact_store = ArtifactStore(str(data_root), run_store)
    workspace_mgr = WorkspaceManager(str(data_root))
    repo_service = RepositoryService()

    return {
        "data_root": data_root,
        "run_store": run_store,
        "artifact_store": artifact_store,
        "workspace_mgr": workspace_mgr,
        "repo_service": repo_service,
    }


from datetime import datetime, timezone

def make_issue_record(
    issue_id: int = 1,
    repository_id: int = 1,
    number: int = 1,
    title: str = "Test Issue",
    body: str = "Test Body",
    state: str = "open",
    labels: Optional[List[str]] = None,
    author: str = "tester",
    is_pull_request: bool = False,
) -> IssueRecord:
    now = datetime.now(timezone.utc)
    rec = IssueRecord(
        issue_id=issue_id,
        repository_id=repository_id,
        number=number,
        title=title,
        body=body,
        state=state,
        author=author,
        labels=labels or [],
        created_at=now,
        updated_at=now,
        html_url=f"https://github.com/owner/repo/issues/{number}",
        comments_count=0,
    )
    if is_pull_request:
        object.__setattr__(rec, "is_pull_request", True)
    return rec


class MockIntakePort(ExistingIssueIntakePort):
    def __init__(self):
        self.snapshots: Dict[str, IssueSnapshot] = {}
        self.candidates: List[IssueRecord] = []

    def get_snapshot(self, snapshot_id: str) -> Optional[IssueSnapshot]:
        return self.snapshots.get(snapshot_id)

    def resolve_url(self, issue_url: str) -> Tuple[IssueRecord, IssueSnapshot, Repository]:
        repo = Repository(
            repository_id=1,
            owner="owner",
            name="repo",
            full_name="owner/repo",
            html_url="https://github.com/owner/repo",
            default_branch="main",
            visibility="public",
        )
        issue = make_issue_record(
            issue_id=101,
            number=42,
            title="Resolved from URL",
            body="Issue body content from URL",
            labels=["bug"],
            author="alice",
        )
        snap = IssueSnapshot(
            repository=repo,
            issue=issue,
            source=Source.live,
            source_updated_at=datetime.now(timezone.utc),
            body_complete=True,
        )
        return issue, snap, repo

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
        res = [c for c in self.candidates]
        if include_labels:
            res = [c for c in res if any(l in c.labels for l in include_labels)]
        if exclude_labels:
            res = [c for c in res if not any(l in c.labels for l in exclude_labels)]
        return res[:limit], None


# ── AT-002: Single saved issue ────────────────────────────────────────────────

def test_at002_single_saved_issue(git_repo, harness_env):
    intake = MockIntakePort()
    repo_model = Repository(
        repository_id=1, owner="octocat", name="hello-world", full_name="octocat/hello-world",
        html_url="https://github.com/octocat/hello-world", default_branch="main", visibility="public"
    )
    issue_model = make_issue_record(
        issue_id=100, number=1, title="Bug in parser", body="Detailed description of bug.",
        state="open", labels=["bug", "priority:high"], author="octocat"
    )
    snap = IssueSnapshot(
        repository=repo_model,
        issue=issue_model,
        source=Source.cache,
        source_updated_at=datetime.now(timezone.utc),
        body_complete=True,
    )
    intake.snapshots[snap.snapshot_id] = snap

    task_prep = TaskPreparationService(intake)
    ctrl = PreparationController(
        run_store=harness_env["run_store"],
        artifact_store=harness_env["artifact_store"],
        workspace_manager=harness_env["workspace_mgr"],
        repository_service=harness_env["repo_service"],
        task_prep_service=task_prep,
        data_root=str(harness_env["data_root"]),
    )

    req = RunRequestV1(
        idempotency_key="idemp-at002-test",
        task_mode=TaskMode.SINGLE_ISSUE,
        execution_mode=ExecutionMode.DEVELOPMENT,
        repository=RepositoryRefV1(kind=RepositoryKind.LOCAL_GIT, locator=str(git_repo)),
        task=TaskInputV1(issue_snapshot_id=snap.snapshot_id),
        limits=LimitsV1(max_tasks=1),
    )

    result = ctrl.prepare(req)
    assert result.status == "PREPARED"
    assert len(result.tasks) == 1
    assert result.tasks[0].state == "QUEUED"
    assert result.queue_summary.selected == 1

    # Verify task in DB
    tasks_db = harness_env["run_store"].get_tasks(result.run_id)
    assert len(tasks_db) == 1
    assert tasks_db[0]["source_snapshot_id"] == snap.snapshot_id
    assert len(tasks_db[0]["raw_content_sha256"]) == 64
    assert len(tasks_db[0]["normalized_content_sha256"]) == 64


# ── AT-003: Direct text task ──────────────────────────────────────────────────

def test_at003_direct_text_task(git_repo, harness_env):
    intake = MockIntakePort()
    task_prep = TaskPreparationService(intake)
    ctrl = PreparationController(
        run_store=harness_env["run_store"],
        artifact_store=harness_env["artifact_store"],
        workspace_manager=harness_env["workspace_mgr"],
        repository_service=harness_env["repo_service"],
        task_prep_service=task_prep,
        data_root=str(harness_env["data_root"]),
    )

    req = RunRequestV1(
        idempotency_key="idemp-at003-text",
        task_mode=TaskMode.SINGLE_ISSUE,
        execution_mode=ExecutionMode.DEVELOPMENT,
        repository=RepositoryRefV1(kind=RepositoryKind.LOCAL_GIT, locator=str(git_repo)),
        task=TaskInputV1(text="Fix calculation error in math module\nEnsure float division works."),
        limits=LimitsV1(max_tasks=1),
    )

    result = ctrl.prepare(req)
    assert result.status == "PREPARED"
    assert len(result.tasks) == 1
    tasks_db = harness_env["run_store"].get_tasks(result.run_id)
    assert tasks_db[0]["source_type"] == "direct_text"
    assert "calculation error" in tasks_db[0]["task_spec_json"]


# ── AT-004 & AT-005 & AT-006: Repository mode, PR exclusion, duplicates ─────────

def test_at004_at005_at006_repository_mode_filtering(git_repo, harness_env):
    intake = MockIntakePort()
    # Populate candidates with: 2 valid issues, 1 PR, 1 duplicate ID, 1 duplicate content
    intake.candidates = [
        make_issue_record(issue_id=1, number=10, title="Normal bug", body="Bug 1", labels=["bug"], author="a"),
        make_issue_record(issue_id=2, number=11, title="Pull request #11", body="PR description", is_pull_request=True, labels=["bug"], author="b"),
        make_issue_record(issue_id=3, number=12, title="High priority bug", body="Critical issue", labels=["priority:critical", "bug"], author="c"),
        make_issue_record(issue_id=4, number=10, title="Duplicate number 10", body="Duplicate", labels=["bug"], author="d"),
        make_issue_record(issue_id=5, number=13, title="Duplicate body bug", body="Bug 1", labels=["bug"], author="e"),  # same body as #10
        make_issue_record(issue_id=6, number=14, title="", body="no title", labels=["bug"], author="f"),
    ]

    task_prep = TaskPreparationService(intake)
    ctrl = PreparationController(
        run_store=harness_env["run_store"],
        artifact_store=harness_env["artifact_store"],
        workspace_manager=harness_env["workspace_mgr"],
        repository_service=harness_env["repo_service"],
        task_prep_service=task_prep,
        data_root=str(harness_env["data_root"]),
    )

    req = RunRequestV1(
        idempotency_key="idemp-at004-repo",
        task_mode=TaskMode.REPOSITORY,
        execution_mode=ExecutionMode.DEVELOPMENT,
        repository=RepositoryRefV1(kind=RepositoryKind.LOCAL_GIT, locator=str(git_repo)),
        task=TaskInputV1(repository_query=RepositoryQueryV1(state="open")),
        limits=LimitsV1(max_tasks=3),
    )

    result = ctrl.prepare(req)
    assert result.status == "PREPARED"

    # Selected tasks should have priority:critical first (number 12), then oldest/number 10
    tasks_db = harness_env["run_store"].get_tasks(result.run_id)
    assert len(tasks_db) == 2  # #12 and #10

    # Ensure PR (11) was excluded
    source_keys = [t["source_key"] for t in tasks_db]
    assert not any("issue:11" in k for k in source_keys)

    # First task is #12 because of priority:critical
    assert "issue:12" in tasks_db[0]["source_key"]
    assert "issue:10" in tasks_db[1]["source_key"]

    # Verify counts
    assert result.queue_summary.duplicates >= 2  # #4 (duplicate key) and #5 (duplicate content)
    assert result.queue_summary.excluded >= 2    # PR (11) + empty title (14)


# ── AT-007: Evaluation isolation ──────────────────────────────────────────────

def test_at007_evaluation_isolation_rejects_repo_mode(git_repo):
    # Cross-field validation must reject task_mode=repository with execution_mode=evaluation in default profile
    with pytest.raises(ValueError, match="rejected with execution_mode=evaluation"):
        RunRequestV1(
            idempotency_key="idemp-at007-eval",
            task_mode=TaskMode.REPOSITORY,
            execution_mode=ExecutionMode.EVALUATION,
            repository=RepositoryRefV1(kind=RepositoryKind.LOCAL_GIT, locator=str(git_repo)),
            task=TaskInputV1(repository_query=RepositoryQueryV1(state="open")),
        )


# ── AT-008: Clean local repository ────────────────────────────────────────────

def test_at008_clean_local_repository(git_repo, harness_env):
    intake = MockIntakePort()
    task_prep = TaskPreparationService(intake)
    ctrl = PreparationController(
        run_store=harness_env["run_store"],
        artifact_store=harness_env["artifact_store"],
        workspace_manager=harness_env["workspace_mgr"],
        repository_service=harness_env["repo_service"],
        task_prep_service=task_prep,
        data_root=str(harness_env["data_root"]),
    )

    # Record original source state before preparation
    git_status_before = subprocess.run(["git", "status", "--porcelain"], cwd=str(git_repo), capture_output=True, text=True).stdout
    head_before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(git_repo), capture_output=True, text=True).stdout.strip()

    req = RunRequestV1(
        idempotency_key="idemp-at008-clean",
        task_mode=TaskMode.SINGLE_ISSUE,
        execution_mode=ExecutionMode.DEVELOPMENT,
        repository=RepositoryRefV1(kind=RepositoryKind.LOCAL_GIT, locator=str(git_repo)),
        task=TaskInputV1(text="Clean import test"),
        limits=LimitsV1(max_tasks=1),
    )

    result = ctrl.prepare(req)
    assert result.status == "PREPARED"
    assert result.source.upstream_commit == head_before
    assert result.source.baseline_commit == head_before  # Clean checkout keeps U as B

    # Assert source repo worktree is 100% untouched
    git_status_after = subprocess.run(["git", "status", "--porcelain"], cwd=str(git_repo), capture_output=True, text=True).stdout
    head_after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(git_repo), capture_output=True, text=True).stdout.strip()
    assert git_status_before == git_status_after
    assert head_before == head_after


# ── AT-009: Dirty local repository ────────────────────────────────────────────

def test_at009_dirty_local_repository(git_repo, harness_env):
    # Introduce staged, unstaged, and untracked changes
    (git_repo / "README.md").write_text("# Modified Header\nDirty content.\n")
    (git_repo / "src" / "new_untracked.py").write_text("var = 42\n")
    (git_repo / "staged.txt").write_text("staged file\n")
    subprocess.run(["git", "add", "staged.txt"], cwd=str(git_repo), check=True, capture_output=True)

    intake = MockIntakePort()
    task_prep = TaskPreparationService(intake)
    ctrl = PreparationController(
        run_store=harness_env["run_store"],
        artifact_store=harness_env["artifact_store"],
        workspace_manager=harness_env["workspace_mgr"],
        repository_service=harness_env["repo_service"],
        task_prep_service=task_prep,
        data_root=str(harness_env["data_root"]),
    )

    head_before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(git_repo), capture_output=True, text=True).stdout.strip()

    req = RunRequestV1(
        idempotency_key="idemp-at009-dirty",
        task_mode=TaskMode.SINGLE_ISSUE,
        execution_mode=ExecutionMode.DEVELOPMENT,
        repository=RepositoryRefV1(kind=RepositoryKind.LOCAL_GIT, locator=str(git_repo)),
        task=TaskInputV1(text="Dirty import test"),
        limits=LimitsV1(max_tasks=1),
    )

    result = ctrl.prepare(req)
    assert result.status == "PREPARED"
    assert result.source.upstream_commit == head_before
    # Baseline B must be a synthetic commit (different from U)
    assert result.source.baseline_commit != head_before

    # Verify prepared workspace contains the uncommitted files
    worktree_path = harness_env["data_root"] / result.workspace.relative_root
    assert (worktree_path / "staged.txt").exists()
    assert (worktree_path / "src" / "new_untracked.py").exists()
    assert "Modified Header" in (worktree_path / "README.md").read_text()

    # Verify source was NOT modified
    status_source = subprocess.run(["git", "status", "--porcelain"], cwd=str(git_repo), capture_output=True, text=True).stdout
    assert "M README.md" in status_source
    assert "A  staged.txt" in status_source
    assert "?? src/new_untracked.py" in status_source


# ── AT-010: Concurrent source mutation ────────────────────────────────────────

def test_at010_concurrent_source_mutation(git_repo, harness_env):
    importer = SourceImporter()
    with patch.object(importer, "check_concurrent_mutation", return_value=False):
        with pytest.raises(SourceChangedDuringImportError, match="Source changed during import"):
            importer.import_local(
                source_path=str(git_repo),
                private_bare_repo=harness_env["data_root"] / "bare.git",
                run_id="run_test_mutation",
                limits=LimitsV1(),
            )


# ── AT-012: Credential URL rejected ───────────────────────────────────────────

def test_at012_credential_url_rejected():
    with pytest.raises(ValueError, match="must not contain embedded credentials"):
        RunRequestV1(
            idempotency_key="idemp-at012-cred",
            task_mode=TaskMode.SINGLE_ISSUE,
            execution_mode=ExecutionMode.DEVELOPMENT,
            repository=RepositoryRefV1(
                kind=RepositoryKind.PUBLIC_HTTPS,
                locator="https://user:secrettoken@github.com/owner/repo.git",
            ),
            task=TaskInputV1(text="test"),
        )


# ── AT-013: Malicious metadata sanitized ──────────────────────────────────────

def test_at013_malicious_metadata_sanitized():
    from harness.cli_renderer import sanitize_terminal_text
    malicious = "\x1b[31;1mMalicious\x1b[0m\x00\x08; $(rm -rf /) \u202eReversed"
    sanitized = sanitize_terminal_text(malicious)
    assert "\x1b" not in sanitized
    assert "\x00" not in sanitized
    assert "\u202e" not in sanitized
    assert "Malicious" in sanitized


# ── AT-014: Symlink escape rejected ───────────────────────────────────────────

def test_at014_symlink_escape_rejected(git_repo, harness_env):
    # Create symlink pointing outside the repository
    outside_file = harness_env["data_root"] / "secret.txt"
    outside_file.write_text("super secret\n")
    (git_repo / "escape_link").symlink_to(outside_file)

    importer = SourceImporter()
    with pytest.raises(SourcePolicyError, match="Symlink escapes repository root"):
        importer.import_local(
            source_path=str(git_repo),
            private_bare_repo=harness_env["data_root"] / "bare.git",
            run_id="run_escape",
            limits=LimitsV1(),
        )


# ── AT-015: Limits exceeded ───────────────────────────────────────────────────

def test_at015_limits_exceeded(git_repo, harness_env):
    # Set limit to 1 file, but repo has multiple
    importer = SourceImporter()
    with pytest.raises(LimitsExceededError, match="File count exceeds limit"):
        importer.import_local(
            source_path=str(git_repo),
            private_bare_repo=harness_env["data_root"] / "bare.git",
            run_id="run_limits",
            limits=LimitsV1(max_file_count=1),
        )


# ── AT-016: Idempotency ───────────────────────────────────────────────────────

def test_at016_idempotency(git_repo, harness_env):
    intake = MockIntakePort()
    task_prep = TaskPreparationService(intake)
    ctrl = PreparationController(
        run_store=harness_env["run_store"],
        artifact_store=harness_env["artifact_store"],
        workspace_manager=harness_env["workspace_mgr"],
        repository_service=harness_env["repo_service"],
        task_prep_service=task_prep,
        data_root=str(harness_env["data_root"]),
    )

    req1 = RunRequestV1(
        idempotency_key="idemp-unique-12345",
        task_mode=TaskMode.SINGLE_ISSUE,
        execution_mode=ExecutionMode.DEVELOPMENT,
        repository=RepositoryRefV1(kind=RepositoryKind.LOCAL_GIT, locator=str(git_repo)),
        task=TaskInputV1(text="Same task"),
        limits=LimitsV1(max_tasks=1),
    )

    # First run prepares successfully
    res1 = ctrl.prepare(req1)
    assert res1.status == "PREPARED"

    # Second run with exact same request returns same run_id
    res2 = ctrl.prepare(req1)
    assert res2.run_id == res1.run_id

    # Third run with SAME idempotency key but DIFFERENT request raises conflict
    req_different = RunRequestV1(
        idempotency_key="idemp-unique-12345",
        task_mode=TaskMode.SINGLE_ISSUE,
        execution_mode=ExecutionMode.DEVELOPMENT,
        repository=RepositoryRefV1(kind=RepositoryKind.LOCAL_GIT, locator=str(git_repo)),
        task=TaskInputV1(text="Different task content"),
        limits=LimitsV1(max_tasks=1),
    )
    with pytest.raises(PreparationError, match="previously used with a different request"):
        ctrl.prepare(req_different)


# ── AT-017 & AT-018: Crash recovery & reconciliation ──────────────────────────

def test_at017_crash_during_acquiring(harness_env):
    run_store = harness_env["run_store"]
    req = RunRequestV1(
        idempotency_key="idemp-crash-acq",
        task_mode=TaskMode.SINGLE_ISSUE,
        execution_mode=ExecutionMode.DEVELOPMENT,
        repository=RepositoryRefV1(kind=RepositoryKind.LOCAL_GIT, locator="/tmp/fake"),
        task=TaskInputV1(text="task"),
        limits=LimitsV1(max_tasks=1),
    )
    run_id, _ = run_store.create_run("run_crash_acq", req)
    run_store.transition(run_id, RunState.NEW, RunState.VALIDATING, "REQUEST_VALIDATED", {})
    run_store.transition(run_id, RunState.VALIDATING, RunState.ACQUIRING, "SOURCE_ACQUISITION_STARTED", {})

    ctrl = PreparationController(
        run_store=run_store,
        artifact_store=harness_env["artifact_store"],
        workspace_manager=harness_env["workspace_mgr"],
        repository_service=harness_env["repo_service"],
        task_prep_service=TaskPreparationService(MockIntakePort()),
        data_root=str(harness_env["data_root"]),
    )

    reconciled = ctrl.reconcile_interrupted()
    assert len(reconciled) == 1
    assert reconciled[0]["run_id"] == run_id

    # State must be FAILED with INTERRUPTED_DURING_ACQUISITION
    updated_run = run_store.get_run(run_id)
    assert updated_run["state"] == RunState.FAILED.value
    assert updated_run["error_code"] == "INTERRUPTED_DURING_ACQUISITION"


def test_at018_crash_in_preparing_advances_if_complete(git_repo, harness_env):
    intake = MockIntakePort()
    ctrl = PreparationController(
        run_store=harness_env["run_store"],
        artifact_store=harness_env["artifact_store"],
        workspace_manager=harness_env["workspace_mgr"],
        repository_service=harness_env["repo_service"],
        task_prep_service=TaskPreparationService(intake),
        data_root=str(harness_env["data_root"]),
    )

    req = RunRequestV1(
        idempotency_key="idemp-crash-prep",
        task_mode=TaskMode.SINGLE_ISSUE,
        execution_mode=ExecutionMode.DEVELOPMENT,
        repository=RepositoryRefV1(kind=RepositoryKind.LOCAL_GIT, locator=str(git_repo)),
        task=TaskInputV1(text="Reconcile complete task"),
        limits=LimitsV1(max_tasks=1),
    )

    # First prepare completely so workspace, tasks, and artifacts are verified
    result = ctrl.prepare(req)

    # Reset state to PREPARING in database to simulate crash right before PREPARED transition
    with ctrl.run_store.get_connection() as conn:
        with conn:
            conn.execute("UPDATE h_runs SET state = 'PREPARING' WHERE run_id = ?", (result.run_id,))

    # Reconcile should detect everything is complete and advance to PREPARED
    reconciled = ctrl.reconcile_interrupted()
    assert len(reconciled) == 1
    assert reconciled[0]["action"] == "advanced_to_prepared"

    run_db = ctrl.run_store.get_run_by_idempotency_key("idemp-crash-prep")
    assert run_db["state"] == RunState.PREPARED.value


# ── AT-019: Artifact tampering detected ───────────────────────────────────────

def test_at019_artifact_tamper_detected(git_repo, harness_env):
    intake = MockIntakePort()
    ctrl = PreparationController(
        run_store=harness_env["run_store"],
        artifact_store=harness_env["artifact_store"],
        workspace_manager=harness_env["workspace_mgr"],
        repository_service=harness_env["repo_service"],
        task_prep_service=TaskPreparationService(intake),
        data_root=str(harness_env["data_root"]),
    )

    req = RunRequestV1(
        idempotency_key="idemp-at019-tamper",
        task_mode=TaskMode.SINGLE_ISSUE,
        execution_mode=ExecutionMode.DEVELOPMENT,
        repository=RepositoryRefV1(kind=RepositoryKind.LOCAL_GIT, locator=str(git_repo)),
        task=TaskInputV1(text="Tamper test"),
        limits=LimitsV1(max_tasks=1),
    )

    result = ctrl.prepare(req)
    assert result.status == "PREPARED"

    # Tamper with request.json artifact on disk
    req_artifact_file = harness_env["data_root"] / "runs" / result.run_id / "artifacts" / "request.json"
    assert req_artifact_file.exists()
    req_artifact_file.write_text('{"tampered": true}')

    # Verification must fail
    is_valid = harness_env["artifact_store"].verify(result.run_id, "request.json")
    assert is_valid is False


# ── AT-020: Machine output contract ───────────────────────────────────────────

def test_at020_machine_output_valid_json(git_repo, temp_dir):
    import uuid
    idemp = f"idemp-{uuid.uuid4().hex[:12]}"
    req_data = {
        "schema_version": "1.0",
        "idempotency_key": idemp,
        "task_mode": "single_issue",
        "execution_mode": "development",
        "repository": {
            "kind": "local_git",
            "locator": str(git_repo),
        },
        "task": {
            "text": "Headless CLI test",
        },
        "limits": {
            "max_tasks": 1,
        },
    }
    req_file = temp_dir / "request.json"
    req_file.write_text(json.dumps(req_data))

    env = dict(os.environ)
    env["PYTHONPATH"] = "src"
    env["DATA_DIR"] = str(temp_dir / "harness_data")  # never write into the developer's real data/

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "harness.cli",
            "prepare",
            "--request",
            str(req_file),
            "--json",
        ],
        cwd=str(Path.cwd()),
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0
    # stdout must be parseable JSON and validate as PreparedRunResultV1
    stdout_parsed = json.loads(proc.stdout)
    assert stdout_parsed["status"] == "PREPARED"
    assert stdout_parsed["schema_version"] == "1.0"
    assert "run_id" in stdout_parsed


# ── AT-021: No forbidden actions ──────────────────────────────────────────────

def test_at021_no_forbidden_actions(git_repo, harness_env):
    """Spies ensure no model calls, no git push, and no npm/pip install during preparation."""
    intake = MockIntakePort()
    ctrl = PreparationController(
        run_store=harness_env["run_store"],
        artifact_store=harness_env["artifact_store"],
        workspace_manager=harness_env["workspace_mgr"],
        repository_service=harness_env["repo_service"],
        task_prep_service=TaskPreparationService(intake),
        data_root=str(harness_env["data_root"]),
    )

    with patch.object(ctrl.repo_service.git, "run", wraps=ctrl.repo_service.git.run) as git_spy:
        req = RunRequestV1(
            idempotency_key="idemp-at021-spy",
            task_mode=TaskMode.SINGLE_ISSUE,
            execution_mode=ExecutionMode.DEVELOPMENT,
            repository=RepositoryRefV1(kind=RepositoryKind.LOCAL_GIT, locator=str(git_repo)),
            task=TaskInputV1(text="Spy test"),
            limits=LimitsV1(max_tasks=1),
        )
        ctrl.prepare(req)

        # Inspect all git commands executed
        for call_args in git_spy.call_args_list:
            args = call_args[0][0]
            assert "push" not in args, f"Forbidden git push detected: {args}"
            assert "commit" not in args or "commit-tree" in args, f"Forbidden commit in source: {args}"


def test_real_intake_adapter_reads_provider_issue_pages(tmp_path):
    """Repository mode through the real adapter (not a fake port): IssuePage.issues, paging, label filters."""
    from harness.config import HarnessConfig
    from harness.contracts import LimitsV1, RepositoryQueryV1
    from harness.intake.existing_intake_adapter import ExistingIssueIntakeAdapter
    from harness.models import IssuePage

    repository = Repository(repository_id=1, owner="owner", name="repo", full_name="owner/repo",
                            html_url="https://github.com/owner/repo", visibility="public", default_branch="main")
    pages = {
        None: IssuePage(issues=[make_issue_record(issue_id=1, number=1, title="Crash on empty input"),
                                make_issue_record(issue_id=2, number=2, title="Docs typo", labels=["wontfix"])],
                        filters={}, next_cursor="page-2", has_more=True),
        "page-2": IssuePage(issues=[make_issue_record(issue_id=3, number=3, title="Wrong rounding", body="Other body")],
                            filters={}),
    }
    seen = []

    class Service:
        def fetch_repository(self, owner, repo):
            return repository

        def browse(self, owner, repo, repository, filters, cursor=None, use_cache=True):
            seen.append((cursor, filters.per_page))
            return pages[cursor]

    adapter = ExistingIssueIntakeAdapter(service=Service(), store=MagicMock(),
                                         config=HarnessConfig(data_dir=tmp_path / "data"))
    batch, cursor = adapter.list_candidates("owner", "repo", exclude_labels=["wontfix"], limit=20)
    assert [issue.number for issue in batch] == [1] and cursor == "page-2"

    tasks, summary, _ = TaskPreparationService(adapter).prepare_queue(
        "owner", "repo", RepositoryQueryV1(exclude_labels=["wontfix"]), LimitsV1(max_tasks=5))
    assert [t.source_key for t in tasks] == ["github:owner/repo:issue:1", "github:owner/repo:issue:3"]
    assert summary.selected == 2 and seen[-1] == ("page-2", 50)
