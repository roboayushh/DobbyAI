"""Real git-backed fixtures shared by PRD 3-5 tests.

These build an actual local Git repository, prepare it through the PRD 1
PreparationController, and return handles to the stores. Nothing here executes
model-generated code on the host.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from harness.application.preparation_controller import PreparationController
from harness.contracts import (
    ExecutionMode,
    LimitsV1,
    RepositoryKind,
    RepositoryQueryV1,
    RepositoryRefV1,
    RunRequestV1,
    TaskInputV1,
    TaskMode,
)
from harness.intake.existing_intake_port import ExistingIssueIntakePort
from harness.intake.task_preparation_service import TaskPreparationService
from harness.models import IssueRecord
from harness.persistence import ArtifactStore, RunStore
from harness.repository import RepositoryService
from harness.workspace import WorkspaceManager

PROFILE_TOML = """
[profiles.designated]
protocol = "openai-compatible-chat"
adapter = "openai-compatible"
endpoint = "https://provider.example/v1"
model = "official-model-id"
context_window_tokens = 64000
max_output_tokens = 4000
safety_margin_tokens = 2000
tokenizer = "conservative-v1"
request_timeout_seconds = 120
temperature = 0.0
top_p = 1.0
supports_json_schema = true
allow_streaming = false
""".strip()


def git(repo: Path, *args: str) -> str:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


def make_repo(root: Path, files: Mapping[str, str], name: str = "fixture_repo") -> Path:
    repo = root / name
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    for relative, content in files.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fixture baseline")
    return repo


def repo_integrity(repo: Path) -> Dict[str, object]:
    """Byte manifest + Git status/HEAD/index identity of an original checkout."""
    files = {}
    for path in sorted(repo.rglob("*")):
        if ".git" in path.relative_to(repo).parts:
            continue
        if path.is_file() and not path.is_symlink():
            files[path.relative_to(repo).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    index = repo / ".git" / "index"
    return {
        "files": files,
        "head": git(repo, "rev-parse", "HEAD"),
        "branch": git(repo, "rev-parse", "--abbrev-ref", "HEAD"),
        "status": git(repo, "status", "--porcelain=v1", "--untracked-files=all"),
        "index_sha256": hashlib.sha256(index.read_bytes()).hexdigest() if index.exists() else None,
        "refs": git(repo, "for-each-ref", "--format=%(refname) %(objectname)"),
    }


class ListIntakePort(ExistingIssueIntakePort):
    """Offline intake returning a fixed issue list (no network)."""

    def __init__(self, issues: Sequence[Tuple[int, str, str]]) -> None:
        now = datetime.now(timezone.utc)
        self.records = [
            IssueRecord(
                issue_id=1000 + number,
                repository_id=1,
                number=number,
                title=title,
                body=body,
                state="open",
                author="fixture",
                labels=[],
                created_at=now,
                updated_at=now,
                html_url=f"https://github.com/fixture/repo/issues/{number}",
                comments_count=0,
            )
            for number, title, body in issues
        ]

    def get_snapshot(self, snapshot_id):  # pragma: no cover - unused
        return None

    def resolve_url(self, issue_url):  # pragma: no cover - unused
        raise NotImplementedError

    def list_candidates(self, owner, repo, state="open", include_labels=None, exclude_labels=None, cursor=None, limit=50):
        if cursor:
            return [], None
        return list(self.records), None


@dataclass
class PreparedEnv:
    data_root: Path
    repo: Path
    run_store: RunStore
    artifact_store: ArtifactStore
    run_id: str
    profile_path: Path

    def tasks(self) -> List[str]:
        return [row["task_id"] for row in self.run_store.get_tasks(self.run_id)]


def prepare_run(
    tmp_path: Path,
    files: Mapping[str, str],
    task_text: Optional[str] = "Fix the bug described in the repository.",
    *,
    issues: Optional[Sequence[Tuple[int, str, str]]] = None,
    execution_mode: ExecutionMode = ExecutionMode.DEVELOPMENT,
    max_tasks: int = 3,
    key: str = "fixture-run-0001",
    repo: Optional[Path] = None,
) -> PreparedEnv:
    repo = repo or make_repo(tmp_path, files)
    data_root = tmp_path / "harness_data"
    data_root.mkdir(exist_ok=True)
    run_store = RunStore(str(data_root / "harness.db"))
    artifact_store = ArtifactStore(str(data_root), run_store)
    intake = ListIntakePort(issues or [])
    controller = PreparationController(
        run_store=run_store,
        artifact_store=artifact_store,
        workspace_manager=WorkspaceManager(str(data_root)),
        repository_service=RepositoryService(),
        task_prep_service=TaskPreparationService(intake),
        data_root=str(data_root),
    )
    if issues:
        request = RunRequestV1(
            idempotency_key=key,
            task_mode=TaskMode.REPOSITORY,
            execution_mode=execution_mode,
            repository=RepositoryRefV1(kind=RepositoryKind.LOCAL_GIT, locator=str(repo.resolve())),
            task=TaskInputV1(repository_query=RepositoryQueryV1(state="open")),
            limits=LimitsV1(max_tasks=max_tasks),
        )
    else:
        request = RunRequestV1(
            idempotency_key=key,
            task_mode=TaskMode.SINGLE_ISSUE,
            execution_mode=execution_mode,
            repository=RepositoryRefV1(kind=RepositoryKind.LOCAL_GIT, locator=str(repo.resolve())),
            task=TaskInputV1(text=task_text),
            limits=LimitsV1(max_tasks=1),
        )
    result = controller.prepare(request)
    profile_path = tmp_path / "profiles.toml"
    profile_path.write_text(PROFILE_TOML, encoding="utf-8")
    return PreparedEnv(data_root, repo, run_store, artifact_store, result.run_id, profile_path)


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, timeout=20
        ).returncode == 0
    except Exception:
        return False


def json_response(payload: Mapping[str, object]) -> str:
    return json.dumps(payload)
