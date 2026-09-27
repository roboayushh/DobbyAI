"""PRD 6 export fidelity (REL-009..REL-013, AT6-017..AT6-032). No Docker needed."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from harness.application.composition import build_controller, build_services
from harness.export.patch_adapter import PatchAdapter, PatchRejected
from harness.export.service import ExportError, ExportService, verify_bundle
from harness.queue import QueueCoordinator
from harness.workspace.task_workspace import TaskWorkspaceService

from tests.support.harness_fixtures import prepare_run, repo_integrity

BINARY = bytes(range(256)) * 4


def candidate_env(tmp_path: Path, *, malicious: bool = False):
    files = {
        "src/__init__.py": "",
        "src/keep.py": "keep = 1\n",
        "src/edit.py": "value = 1\n",
        "src/gone.py": "gone = 1\n",
        "scripts/run.sh": "#!/bin/sh\necho hi\n",
        "assets/logo.bin": "placeholder",
    }
    if malicious:
        files[".gitattributes"] = "*.py diff=evil\n*.py filter=evil\n* text eol=crlf\n"
    env = prepare_run(tmp_path, files, "Rework the modules as described in the issue")
    if malicious:
        marker = tmp_path / "PWNED"
        subprocess.run(["git", "config", "diff.evil.textconv", f"touch {marker}; cat"], cwd=env.repo, check=True)
        subprocess.run(["git", "config", "diff.evil.command", f"touch {marker}"], cwd=env.repo, check=True)
    task_id = env.tasks()[0]
    workspaces = TaskWorkspaceService(env.run_store, env.artifact_store, env.data_root)
    baseline = env.run_store.get_source_snapshot(env.run_id)["baseline_commit"]
    workspaces.ensure(env.run_id, task_id, baseline, baseline)
    root = workspaces.workspace_root(env.run_id, task_id)
    (root / "src" / "edit.py").write_text("value = 2\n")
    (root / "src" / "gone.py").unlink()
    (root / "src" / "new.py").write_text("new = 1\n")
    os.chmod(root / "scripts" / "run.sh", 0o755)
    (root / "assets" / "logo.bin").write_bytes(BINARY)
    os.symlink("keep.py", root / "src" / "alias.py")
    parent = workspaces.current_version(env.run_id, task_id)
    checkpoint = workspaces.prepare_checkpoint(env.run_id, task_id, parent, workspaces.scan(env.run_id, task_id), "edit")
    with env.run_store.get_connection() as conn:
        with conn:
            workspaces.commit_checkpoint_sql(conn, env.run_id, task_id, parent, checkpoint, None)
    workspaces.publish_checkpoint_refs(env.run_id, task_id, parent, checkpoint)
    record = workspaces.freeze_candidate(env.run_id, task_id, summary="Rework", trailers={"Harness-Task-ID": task_id})
    with env.run_store.get_connection() as conn:
        with conn:
            workspaces.insert_candidate_sql(conn, record, None)
    services = build_services(env.run_store, env.artifact_store, env.data_root)
    controller = build_controller(services, env.profile_path)
    coordinator = QueueCoordinator(run_store=env.run_store, artifact_store=env.artifact_store, services=services, controller=controller)
    exporter = ExportService(run_store=env.run_store, artifact_store=env.artifact_store, services=services,
                             coordinator=coordinator, data_root=env.data_root)
    return env, exporter, record


def test_bundle_round_trips_every_file_type(tmp_path: Path) -> None:
    env, exporter, record = candidate_env(tmp_path)
    before = repo_integrity(env.repo)
    out = tmp_path / "bundle"
    outcome = exporter.export(env.run_id, out)
    manifest = outcome.manifest
    assert manifest.status == "VALID" and manifest.round_trip.status == "PASS"
    assert manifest.round_trip.result_tree == manifest.candidate.tree == record.tree
    assert sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()) == sorted([
        "manifest.json", "result.json", "patch.diff", "report.md", "checksums.sha256",
        "evidence/verification-summary.json", "evidence/task-results.json", "evidence/usage.json", "evidence/provenance.json"])
    patch = (out / "patch.diff").read_text(errors="replace")
    assert "GIT binary patch" in patch and "new mode 100755" in patch and "deleted file mode" in patch and "120000" in patch
    assert verify_bundle(out)
    result = json.loads((out / "result.json").read_text())
    assert result["status"] != "PASS"  # an unverified attempt is never labeled PASS
    assert any("best UNVERIFIED" in item for item in result["limitations"])
    # The exported patch applies to a fresh clone of the original repository.
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(env.repo), str(clone)], check=True)
    assert subprocess.run(["git", "apply", "--check", str(out / "patch.diff")], cwd=clone).returncode == 0
    assert repo_integrity(env.repo) == before


def test_malicious_diff_driver_and_filters_never_execute(tmp_path: Path) -> None:
    env, exporter, _ = candidate_env(tmp_path, malicious=True)
    outcome = exporter.export(env.run_id, tmp_path / "bundle")
    assert outcome.manifest.status == "VALID"
    assert not (tmp_path / "PWNED").exists()


def test_existing_output_is_never_overwritten_without_replace(tmp_path: Path) -> None:
    env, exporter, _ = candidate_env(tmp_path)
    out = tmp_path / "bundle"
    out.mkdir()
    (out / "keep.txt").write_text("mine")
    with pytest.raises(ExportError) as raised:
        exporter.export(env.run_id, out)
    assert raised.value.code == "EXPORT_OUTPUT_EXISTS" and (out / "keep.txt").read_text() == "mine"
    outcome = exporter.export(env.run_id, out, replace=True)
    assert outcome.manifest.status == "VALID" and not (out / "keep.txt").exists()


def test_protected_and_symlinked_destinations_are_refused(tmp_path: Path) -> None:
    env, exporter, _ = candidate_env(tmp_path)
    for target in (env.repo / "bundle", env.data_root / "runs" / "x"):
        with pytest.raises(ExportError) as raised:
            exporter.export(env.run_id, target)
        assert raised.value.code in ("EXPORT_PATH_PROTECTED", "EXPORT_PATH_INVALID")
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    with pytest.raises(ExportError) as raised:
        exporter.export(env.run_id, tmp_path / "link")
    assert raised.value.code == "EXPORT_PATH_INVALID"


def test_tampered_bundle_fails_verification_and_is_rebuilt(tmp_path: Path) -> None:
    env, exporter, _ = candidate_env(tmp_path)
    out = tmp_path / "bundle"
    first = exporter.export(env.run_id, out)
    again = exporter.export(env.run_id, out)
    assert again.replayed and again.manifest.manifest_sha256 == first.manifest.manifest_sha256
    (out / "patch.diff").write_text("tampered")
    assert not verify_bundle(out)
    (out / "extra.txt").write_text("x")
    assert not verify_bundle(out)


def test_failed_export_leaves_no_partial_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, exporter, _ = candidate_env(tmp_path)

    def crash(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(ExportService, "_commit_output", staticmethod(crash))
    with pytest.raises(KeyboardInterrupt):
        exporter.export(env.run_id, tmp_path / "bundle")
    assert not (tmp_path / "bundle").exists()
    assert not list(tmp_path.glob(".bundle.tmp-*"))


def test_patch_inspection_rejects_unsafe_or_unexpected_paths() -> None:
    adapter = PatchAdapter()
    ok = b"diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-a\n+b\n"
    assert adapter.inspect(ok, [("M", "src/a.py")]).paths == ["src/a.py"]
    with pytest.raises(PatchRejected):
        adapter.inspect(ok, [("M", "src/other.py")])
    with pytest.raises(PatchRejected):
        adapter.inspect(ok.replace(b"src/a.py", b"../../etc/x"), [("M", "../../etc/x")])
    with pytest.raises(PatchRejected):
        adapter.inspect(b'diff --git "a/\\ttab" "b/\\ttab"\n', [("M", "\ttab")])
    with pytest.raises(PatchRejected):
        adapter.inspect(ok + b"\x00", [("M", "src/a.py")])
