"""PRD 3 unit tests: path policy, in-container tools, workspace integrity, private Git refs.

No container is started here; Docker-backed behavior lives in
``test_prd3_sandbox.py``.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from harness.gitflow.private_git import RefCASError, validate_ref_name, validate_tree_path
from harness.policy.engine import PolicyError, normalize_declared_path, path_within
from harness.worker import harness_tools
from harness.workspace.manifest import diff_manifests, is_disposable, scan_workspace
from harness.workspace.task_workspace import TaskWorkspaceService

from tests.support.harness_fixtures import prepare_run


# ------------------------------------------------------------- path policy
@pytest.mark.parametrize("path", ["/etc/passwd", "C:/x", "../x", "a/../../b", ".git/config", "src/.git/hooks", "", "a\x00b"])
def test_declared_path_rejects_unsafe(path: str) -> None:
    with pytest.raises(PolicyError):
        normalize_declared_path(path)


def test_declared_path_normalizes_and_scopes() -> None:
    assert normalize_declared_path("./src//pkg/") == "src/pkg/"
    assert normalize_declared_path("src\\mod.py") == "src/mod.py"
    assert path_within("src/pkg/a.py", ["src/pkg/"])
    assert not path_within("src/pkgx/a.py", ["src/pkg/"])
    assert path_within("src/mod.py/inner", ["src/mod.py"])
    assert not path_within("tests/test_a.py", ["src/"])


# --------------------------------------------------------------- toolbox
@pytest.fixture()
def toolbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace, output, context = tmp_path / "workspace", tmp_path / "output", tmp_path / "context"
    for path in (workspace, output, context):
        path.mkdir()
    (workspace / "src").mkdir()
    (workspace / "src" / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (workspace / "README.md").write_text("readme\n", encoding="utf-8")
    monkeypatch.setattr(harness_tools, "WORKSPACE", workspace)
    monkeypatch.setattr(harness_tools, "OUTPUT", output)
    monkeypatch.setattr(harness_tools, "CONTEXT", context)
    box = harness_tools.Toolbox({
        "action_id": "act_test",
        "capabilities": ["workspace.patch", "source.read", "source.search", "sandbox.command.argv", "action.result.emit"],
        "declared_paths": ["src/"],
        "limits": {"wall_seconds": 60},
    })
    return box, workspace


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_apply_patch_edit_list_with_expected_hash(toolbox) -> None:
    box, workspace = toolbox
    before = (workspace / "src/calc.py").read_bytes()
    result = box.apply_patch([{"path": "src/calc.py", "old": "return a - b", "new": "return a + b"}], {"src/calc.py": _sha(before)})
    assert result["changed_paths"] == ["src/calc.py"]
    assert "return a + b" in (workspace / "src/calc.py").read_text()


def test_apply_patch_rejects_stale_hash_and_leaves_file(toolbox) -> None:
    box, workspace = toolbox
    before = (workspace / "src/calc.py").read_bytes()
    with pytest.raises(harness_tools.ToolError):
        box.apply_patch([{"path": "src/calc.py", "old": "return a - b", "new": "return a + b"}], {"src/calc.py": "0" * 64})
    assert (workspace / "src/calc.py").read_bytes() == before


def test_apply_patch_unified_diff(toolbox) -> None:
    box, workspace = toolbox
    diff = (
        "--- a/src/calc.py\n+++ b/src/calc.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n"
    )
    box.apply_patch(diff)
    assert (workspace / "src/calc.py").read_text() == "def add(a, b):\n    return a + b\n"


def test_apply_patch_is_all_or_nothing(toolbox) -> None:
    box, workspace = toolbox
    before = (workspace / "src/calc.py").read_bytes()
    with pytest.raises(harness_tools.ToolError) as raised:
        box.apply_patch([
            {"path": "src/calc.py", "old": "return a - b", "new": "return a + b"},
            {"path": "src/calc.py", "old": "does not exist", "new": "x"},
        ], {"src/calc.py": _sha(before)})
    assert raised.value.code == "EDIT_NOT_FOUND"
    assert (workspace / "src/calc.py").read_bytes() == before


@pytest.mark.parametrize("path", ["README.md", "../escape.py", ".git/config", "/etc/passwd"])
def test_apply_patch_refuses_undeclared_or_unsafe_paths(toolbox, path: str) -> None:
    box, workspace = toolbox
    with pytest.raises(harness_tools.ToolError):
        box.apply_patch({path: "owned\n"})
    assert (workspace / "README.md").read_text() == "readme\n"
    assert not (workspace.parent / "escape.py").exists()


def test_whole_file_overwrite_requires_read_hash(toolbox) -> None:
    box, workspace = toolbox
    with pytest.raises(harness_tools.ToolError) as raised:
        box.apply_patch({"src/calc.py": "clobbered\n"})
    assert raised.value.code == "EXPECTED_HASH_REQUIRED"
    assert "return a - b" in (workspace / "src/calc.py").read_text()


def test_apply_patch_refuses_symlink_escape(toolbox, tmp_path: Path) -> None:
    box, workspace = toolbox
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n")
    (workspace / "src" / "link.py").symlink_to(outside)
    with pytest.raises(harness_tools.ToolError):
        box.apply_patch({"src/link.py": "overwritten\n"})
    assert outside.read_text() == "secret\n"


def test_emit_result_only_once(toolbox) -> None:
    box, _ = toolbox
    box.emit_result("ACTION_COMPLETED", "done", ["obs"])
    with pytest.raises(harness_tools.ToolError):
        box.emit_result("ACTION_COMPLETED", "again", [])


# -------------------------------------------------------- workspace/manifest
def test_manifest_detects_every_change_kind(tmp_path: Path) -> None:
    root = tmp_path / "w"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "a.py").write_text("a = 1\n")
    (root / "pkg" / "b.py").write_text("b = 1\n")
    before = scan_workspace(root)
    (root / "pkg" / "a.py").write_text("a = 2\n")  # same size, different bytes
    (root / "pkg" / "b.py").unlink()
    (root / "pkg" / "c.py").write_text("c = 1\n")
    (root / "pkg" / "__pycache__").mkdir()
    (root / "pkg" / "__pycache__" / "a.cpython-312.pyc").write_bytes(b"\0")
    after = scan_workspace(root)
    changes = diff_manifests(before, after)
    kinds = {change.path: change.change_type for change in changes.changes}
    assert kinds.get("pkg/a.py") == "MODIFIED"
    assert kinds.get("pkg/b.py") == "DELETED"
    assert kinds.get("pkg/c.py") == "CREATED"
    cache = [path for path in kinds if path.startswith("pkg/__pycache__")]
    assert cache and all(is_disposable(path) for path in cache)
    assert not is_disposable("pkg/c.py")


def test_task_workspace_restore_is_byte_exact(tmp_path: Path) -> None:
    files = {
        ".gitattributes": "* text eol=crlf\n",
        "src/__init__.py": "",
        "src/mod.py": "x = 1\n",
    }
    env = prepare_run(tmp_path, files, "Fix mod.py so x equals two in src/mod.py")
    task_id = env.tasks()[0]
    service = TaskWorkspaceService(env.run_store, env.artifact_store, env.data_root)
    baseline = env.run_store.get_source_snapshot(env.run_id)["baseline_commit"]
    version = service.ensure(env.run_id, task_id, baseline, baseline)
    root = service.workspace_root(env.run_id, task_id)
    original = (root / "src/mod.py").read_bytes()
    assert b"\r\n" not in original  # no .gitattributes smudging in materialization
    (root / "src/mod.py").write_text("x = 999\n")
    (root / "injected").mkdir()
    (root / "injected" / "evil.py").write_text("boom\n")
    (root / "escape").symlink_to("/etc")
    service.restore(env.run_id, task_id, service.load_manifest(version))
    assert (root / "src/mod.py").read_bytes() == original
    assert not (root / "injected").exists()
    assert not (root / "escape").exists()
    service.verify_current(env.run_id, task_id)


def test_candidate_commit_is_single_parent_with_sanitized_message(tmp_path: Path) -> None:
    env = prepare_run(tmp_path, {"src/__init__.py": "", "src/mod.py": "x = 1\n"}, "Set x to two in src/mod.py please")
    task_id = env.tasks()[0]
    service = TaskWorkspaceService(env.run_store, env.artifact_store, env.data_root)
    baseline = env.run_store.get_source_snapshot(env.run_id)["baseline_commit"]
    service.ensure(env.run_id, task_id, baseline, baseline)
    root = service.workspace_root(env.run_id, task_id)
    (root / "src/mod.py").write_text("x = 2\n")
    parent = service.current_version(env.run_id, task_id)
    prepared = service.prepare_checkpoint(env.run_id, task_id, parent, service.scan(env.run_id, task_id), "manual checkpoint")
    with env.run_store.get_connection() as conn:
        with conn:
            service.commit_checkpoint_sql(conn, env.run_id, task_id, parent, prepared, "act_manual")
    service.publish_checkpoint_refs(env.run_id, task_id, parent, prepared)
    record = service.freeze_candidate(env.run_id, task_id, summary="Fix https://evil.example/x \x1b[31m now", trailers={"Harness-Task-ID": task_id})
    git = service.git(env.run_id)
    assert git.commit_parents(record.commit) == [baseline]
    message = git.commit_message(record.commit)
    assert "https://" not in message and "\x1b" not in message
    assert "Harness-Candidate-ID" in message


# ---------------------------------------------------------------- git refs
@pytest.mark.parametrize("ref", ["refs/heads/main", "HEAD", "refs/harness/../heads/main", "refs/harness/runs/x/@{1}", "refs/tags/v1"])
def test_managed_refs_must_stay_under_harness_namespace(ref: str) -> None:
    with pytest.raises(Exception):
        validate_ref_name(ref)


@pytest.mark.parametrize("path", ["../x", "/abs", ".git/config", "a/.git/b", "a\nb", ""])
def test_tree_paths_are_validated(path: str) -> None:
    with pytest.raises(Exception):
        validate_tree_path(path)


@pytest.mark.parametrize("cwd", [".", "", "./", "ABS", "ABS/"])
def test_run_accepts_every_workspace_root_spelling(toolbox, cwd: str) -> None:
    box, workspace = toolbox
    box.capabilities.add("sandbox.command.argv")
    cwd = cwd.replace("ABS", str(workspace))  # the container spells this /workspace
    result = box.run(["python3", "-c", "import os; print(os.getcwd())"], cwd=cwd)
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == str(workspace)


def test_search_accepts_root_and_string_paths(toolbox) -> None:
    box, _ = toolbox
    for paths in (["."], ".", [str(harness_tools.WORKSPACE)], None):
        found = box.search("return a - b", paths=paths)
        assert [m["path"] for m in found["matches"]] == ["src/calc.py"]
