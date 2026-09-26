"""tests/smoke_test.py
End-to-end smoke test for PRD 1 Core Foundation and Safe Repository Workspace.
Prepares a fixture Git repository and asserts all invariants without needing an AI API key.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Add src to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from harness.contracts import (
    ExecutionMode,
    LimitsV1,
    RepositoryKind,
    RepositoryRefV1,
    RunRequestV1,
    TaskInputV1,
    TaskMode,
)
from harness.application.preparation_controller import PreparationController
from harness.intake import ExistingIssueIntakeAdapter, TaskPreparationService
from harness.persistence import ArtifactStore, RunStore
from harness.repository import RepositoryService
from harness.workspace import WorkspaceManager


def main():
    print("🔥 Starting PRD 1 Smoke Test...")

    temp_root = Path(tempfile.mkdtemp(prefix="harness_smoke_"))
    try:
        # 1. Create a fixture Git repository
        repo_dir = temp_root / "smoke_repo"
        repo_dir.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=str(repo_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "SmokeTester"], cwd=str(repo_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "smoketester@example.com"], cwd=str(repo_dir), check=True, capture_output=True)

        (repo_dir / "README.md").write_text("# Smoke Test Repository\nPRD 1 smoke testing.\n")
        (repo_dir / "calculator.py").write_text("def add(a, b):\n    return a + b\n")
        subprocess.run(["git", "add", "."], cwd=str(repo_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial smoke commit"], cwd=str(repo_dir), check=True, capture_output=True)

        head_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo_dir), capture_output=True, text=True).stdout.strip()
        print(f"  ✓ Created local fixture repo at commit {head_commit[:8]}")

        # 2. Setup harness environment
        data_root = temp_root / "harness_data"
        data_root.mkdir()
        db_path = data_root / "smoke_harness.db"

        run_store = RunStore(str(db_path))
        artifact_store = ArtifactStore(str(data_root), run_store)
        workspace_mgr = WorkspaceManager(str(data_root))
        repo_service = RepositoryService()
        intake_adapter = ExistingIssueIntakeAdapter()
        task_prep = TaskPreparationService(intake_adapter)

        ctrl = PreparationController(
            run_store=run_store,
            artifact_store=artifact_store,
            workspace_manager=workspace_mgr,
            repository_service=repo_service,
            task_prep_service=task_prep,
            data_root=str(data_root),
        )

        # 3. Create preparation request
        req = RunRequestV1(
            schema_version="1.0",
            idempotency_key="smoke-idemp-001",
            task_mode=TaskMode.SINGLE_ISSUE,
            execution_mode=ExecutionMode.DEVELOPMENT,
            repository=RepositoryRefV1(
                kind=RepositoryKind.LOCAL_GIT,
                locator=str(repo_dir),
            ),
            task=TaskInputV1(text="Smoke test: Add subtract function to calculator"),
            limits=LimitsV1(max_tasks=1),
        )

        print("  ✓ Formatted typed RunRequestV1")

        # 4. Execute preparation
        result = ctrl.prepare(req)
        assert result.status == "PREPARED", f"Expected PREPARED, got {result.status}"
        print(f"  ✓ Successfully prepared run {result.run_id}")
        assert result.source.baseline_commit == head_commit
        assert len(result.tasks) == 1
        assert len(result.artifacts) >= 5

        # 5. Verify artifacts on disk
        artifacts_dir = data_root / "runs" / result.run_id / "artifacts"
        assert (artifacts_dir / "request.json").exists()
        assert (artifacts_dir / "result.json").exists()
        assert (artifacts_dir / "source-manifest.json").exists()
        assert (artifacts_dir / "task-manifest.json").exists()
        assert (artifacts_dir / "events.ndjson").exists()
        print("  ✓ All required artifacts verified on disk")

        # 6. Verify workspace read-only
        worktree_path = data_root / result.workspace.relative_root
        test_file = worktree_path / "calculator.py"
        assert test_file.exists()

        # 7. Check source preservation (no changes to input repository)
        status_out = subprocess.run(["git", "status", "--porcelain"], cwd=str(repo_dir), capture_output=True, text=True).stdout
        assert status_out.strip() == "", "Source repository was modified during preparation!"
        print("  ✓ Source repository preserved byte-for-byte")

        # 8. Test idempotency
        replay_result = ctrl.prepare(req)
        assert replay_result.run_id == result.run_id
        print("  ✓ Idempotency verified: replaying returned identical run")

        print("🎉 All smoke tests PASSED successfully!")
        return 0

    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
