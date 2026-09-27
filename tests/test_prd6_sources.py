"""Source safety: ordinary folders, safe ZIPs, dirty checkouts (FR02/FR03/FR05, AT01-AT03)."""
from __future__ import annotations

import hashlib
import os
import zipfile
from pathlib import Path

import pytest

from harness.application.preparation_controller import PreparationController
from harness.contracts import ExecutionMode, LimitsV1, RepositoryKind, RepositoryRefV1, RunRequestV1, TaskInputV1, TaskMode
from harness.intake.task_preparation_service import TaskPreparationService
from harness.persistence import ArtifactStore, RunStore
from harness.repository import RepositoryService
from harness.workspace import WorkspaceManager

from tests.support.harness_fixtures import ListIntakePort, make_repo, repo_integrity


@pytest.fixture()
def controller(tmp_path: Path):
    data = tmp_path / "data"
    data.mkdir()
    store = RunStore(str(data / "h.db"))
    arts = ArtifactStore(str(data), store)
    ctl = PreparationController(run_store=store, artifact_store=arts, workspace_manager=WorkspaceManager(str(data)),
                                repository_service=RepositoryService(), task_prep_service=TaskPreparationService(ListIntakePort([])),
                                data_root=str(data))
    return ctl, store, data


def prepare(ctl, kind, locator, key):
    return ctl.prepare(RunRequestV1(idempotency_key=key, task_mode=TaskMode.SINGLE_ISSUE, execution_mode=ExecutionMode.DEVELOPMENT,
                                    repository=RepositoryRefV1(kind=kind, locator=str(locator)),
                                    task=TaskInputV1(text="Fix the defect described here"), limits=LimitsV1(max_tasks=1)))


def workspace_files(store, data, run_id):
    root = data / store.get_workspace(run_id)["worktree_relpath"]
    return {p.relative_to(root).as_posix(): (os.readlink(p) if p.is_symlink() else p.read_bytes())
            for p in root.rglob("*") if p.is_file() or p.is_symlink()}


def tree_digest(root: Path):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}


def test_ordinary_folder_import_never_touches_the_original(tmp_path: Path, controller) -> None:
    ctl, store, data = controller
    folder = tmp_path / "plain dir ü"  # spaces and Unicode are data
    (folder / "pkg").mkdir(parents=True)
    (folder / "pkg" / "a.py").write_text("x = 1\n")
    (folder / "__pycache__").mkdir()
    (folder / "__pycache__" / "a.cpython-312.pyc").write_bytes(b"\0stale")
    before = tree_digest(folder)
    result = prepare(ctl, RepositoryKind.LOCAL_FOLDER, folder, "src-folder-0001")
    assert result.status == "PREPARED"
    assert store.get_source_snapshot(result.run_id)["source_kind"] == "local_folder"
    assert set(workspace_files(store, data, result.run_id)) == {"pkg/a.py"}  # caches never imported
    assert tree_digest(folder) == before and not (folder / ".git").exists()


def test_zip_import_strips_single_root_and_leaves_zip_unchanged(tmp_path: Path, controller) -> None:
    ctl, store, data = controller
    archive = tmp_path / "project.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("project-main/src/app.py", "print('hi')\n")
        bundle.writestr("project-main/tests/test_app.py", "def test(): pass\n")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    result = prepare(ctl, RepositoryKind.LOCAL_ZIP, archive, "src-zip-00001")
    assert set(workspace_files(store, data, result.run_id)) == {"src/app.py", "tests/test_app.py"}
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == digest


def _zip(path: Path, entries):
    with zipfile.ZipFile(path, "w") as bundle:
        for name, data, attr in entries:
            info = zipfile.ZipInfo(name)
            if attr is not None:
                info.external_attr = attr << 16
            bundle.writestr(info, data)
    return path


@pytest.mark.parametrize("entries, reason", [
    ([("../escape.py", "x", None)], "traversal"),
    ([("/abs/evil.py", "x", None)], "Absolute"),
    ([("x/link", "/etc/passwd", 0o120777)], "Symlink"),
    ([("a/.git/config", "[core]", None)], "Git metadata"),
    ([("a/File.py", "1", None), ("a/file.py", "2", None)], "Duplicate"),
    ([("a/fifo", "", 0o010644)], "Special"),
])
def test_unsafe_zips_are_rejected_and_nothing_escapes(tmp_path: Path, controller, entries, reason: str) -> None:
    ctl, store, data = controller
    archive = _zip(tmp_path / "bad.zip", entries)
    with pytest.raises(Exception, match=reason):
        prepare(ctl, RepositoryKind.LOCAL_ZIP, archive, f"src-bad-{abs(hash(reason)) % 10**8:08d}")
    assert not (tmp_path / "escape.py").exists()


def test_zip_expansion_bomb_is_bounded(tmp_path: Path, controller) -> None:
    ctl, store, data = controller
    archive = tmp_path / "bomb.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("a/huge.txt", b"\0" * (30 * 1024 * 1024))
    with pytest.raises(Exception, match="exceeds|ratio"):
        prepare(ctl, RepositoryKind.LOCAL_ZIP, archive, "src-bomb-0001")


def test_dirty_checkout_baseline_is_byte_exact_and_original_untouched(tmp_path: Path, controller) -> None:
    ctl, store, data = controller
    repo = make_repo(tmp_path, {"a.py": "a = 1\n", "b.py": "b = 1\n", ".gitattributes": "* text eol=crlf\n"})
    (repo / "a.py").write_text("a = 2\n")
    (repo / "b.py").unlink()
    os.symlink("a.py", repo / "link.py")
    (repo / "new.py").write_text("n = 1\n")
    (repo / ".pytest_cache").mkdir()
    (repo / ".pytest_cache" / "v").write_text("cache")
    before = repo_integrity(repo)
    result = prepare(ctl, RepositoryKind.LOCAL_GIT, repo, "src-dirty-0001")
    files = workspace_files(store, data, result.run_id)
    assert store.get_source_snapshot(result.run_id)["dirty_source_imported"] == 1
    assert files["a.py"] == b"a = 2\n"  # no eol smudging from the repository's own attributes
    assert "b.py" not in files and files["link.py"] == "a.py" and files["new.py"] == b"n = 1\n"
    assert not any(path.startswith(".pytest_cache") for path in files)
    assert repo_integrity(repo) == before


def test_checkout_with_only_caches_is_not_dirty(tmp_path: Path, controller) -> None:
    ctl, store, data = controller
    repo = make_repo(tmp_path, {"a.py": "a = 1\n"})
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "a.cpython-312.pyc").write_bytes(b"\0")
    result = prepare(ctl, RepositoryKind.LOCAL_GIT, repo, "src-cache-0001")
    snapshot = store.get_source_snapshot(result.run_id)
    assert snapshot["dirty_source_imported"] == 0 and snapshot["baseline_commit"] == snapshot["upstream_commit"]
