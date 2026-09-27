"""Verify the complete immutable PRD 1 handoff before PRD 2 work starts."""
from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from harness.contracts import PreparedRunResultV1, RunRequestV1, TaskSpecV1
from harness.persistence import ArtifactStore, RunStore, canonical_json


class PreparedHandoffError(RuntimeError):
    code = "PREPARED_HANDOFF_INTEGRITY_FAILURE"
    retryable = False


@dataclass(frozen=True)
class VerifiedHandoff:
    run_id: str
    source_revision: str
    content_tree_sha256: str
    workspace_root: Path
    task_ids: List[str]
    manifest_sha256: str


class PreparedHandoffVerifier:
    REQUIRED_ARTIFACTS = {
        "request.json": "request",
        "source-manifest.json": "source_manifest",
        "task-manifest.json": "task_manifest",
        "result.json": "result",
    }

    def __init__(
        self,
        run_store: RunStore,
        artifact_store: ArtifactStore,
        data_root: str | Path,
    ) -> None:
        self.run_store = run_store
        self.artifact_store = artifact_store
        self.data_root = Path(data_root).resolve()

    def verify(self, run_id: str) -> VerifiedHandoff:
        try:
            return self._verify(run_id)
        except PreparedHandoffError:
            raise
        except Exception as exc:
            raise PreparedHandoffError(
                "Prepared handoff contains malformed or inconsistent data"
            ) from exc

    def _verify(self, run_id: str) -> VerifiedHandoff:
        run = self.run_store.get_run(run_id)
        source = self.run_store.get_source_snapshot(run_id)
        workspace = self.run_store.get_workspace(run_id)
        tasks = self.run_store.get_tasks(run_id)
        if not run or run["state"] != "PREPARED":
            raise PreparedHandoffError("Run is missing or is not PREPARED")
        if not source or not workspace or not tasks:
            raise PreparedHandoffError("Prepared source, workspace, or task rows are missing")
        if workspace["state"] != "READY" or workspace["writable"] != 0:
            raise PreparedHandoffError("Workspace is not a contained read-only READY workspace")

        root = (self.data_root / workspace["worktree_relpath"]).resolve()
        run_root = (self.data_root / "runs" / run_id).resolve()
        try:
            root.relative_to(run_root)
        except ValueError as exc:
            raise PreparedHandoffError("Workspace path escapes run root") from exc
        if not root.is_dir() or root.is_symlink():
            raise PreparedHandoffError("Workspace root is missing or unsafe")

        # Match the exact artifact path (runs/<run>/artifacts/<relative>): later phases may
        # legitimately store other files with the same basename in subdirectories.
        artifact_rows = {
            Path(row["relative_path"]).as_posix(): row
            for row in self.artifact_store.get_artifacts(run_id)
        }
        for relative_path, kind in self.REQUIRED_ARTIFACTS.items():
            row = artifact_rows.get((Path("runs") / run_id / "artifacts" / relative_path).as_posix())
            if not row or row["kind"] != kind or not self.artifact_store.verify(run_id, relative_path):
                raise PreparedHandoffError(f"Required artifact failed verification: {relative_path}")

        result_bytes = self.artifact_store.open_readonly(run_id, "result.json")
        result = PreparedRunResultV1.model_validate_json(result_bytes)
        if result.run_id != run_id:
            raise PreparedHandoffError("Prepared result targets a different run")
        if (
            result.source.baseline_commit != source["baseline_commit"]
            or result.source.content_tree_sha256 != source["content_tree_sha256"]
            or result.workspace.workspace_id != workspace["workspace_id"]
            or result.workspace.relative_root != workspace["worktree_relpath"]
            or result.workspace.writable
        ):
            raise PreparedHandoffError("Prepared result source or workspace projection differs")
        if [item.task_id for item in result.tasks] != [row["task_id"] for row in tasks]:
            raise PreparedHandoffError("Prepared result task order differs from task rows")
        for summary in result.artifacts:
            artifact = self.artifact_store.get_artifact_by_path(
                run_id,
                summary.relative_path.split("/artifacts/", 1)[-1],
            )
            if (
                not artifact
                or artifact["kind"] != summary.kind
                or artifact["sha256"] != summary.sha256
            ):
                raise PreparedHandoffError(
                    f"Prepared result artifact summary differs: {summary.relative_path}"
                )

        request_bytes = self.artifact_store.open_readonly(run_id, "request.json")
        request = RunRequestV1.model_validate_json(request_bytes)
        if hashlib.sha256(request_bytes).hexdigest() != run["request_sha256"]:
            # PRD 1 stores the hash of canonical JSON, which is exactly how the
            # artifact writer serializes this object.
            raise PreparedHandoffError("Request artifact hash does not match run projection")
        if request.task_mode.value != run["task_mode"] or request.execution_mode.value != run["execution_mode"]:
            raise PreparedHandoffError("Request mode differs from persisted run projection")

        source_bytes = self.artifact_store.open_readonly(run_id, "source-manifest.json")
        source_manifest = json.loads(source_bytes)
        if hashlib.sha256(source_bytes).hexdigest() != source["import_manifest_sha256"]:
            raise PreparedHandoffError("Source manifest hash differs from source identity")
        for field in ("baseline_commit", "baseline_tree", "content_tree_sha256"):
            if source_manifest.get(field) != source[field]:
                raise PreparedHandoffError(f"Source manifest {field} mismatch")

        task_bytes = self.artifact_store.open_readonly(run_id, "task-manifest.json")
        task_manifest = json.loads(task_bytes)
        task_objects = task_manifest.get("tasks")
        if not isinstance(task_objects, list) or len(task_objects) != len(tasks):
            raise PreparedHandoffError("Task manifest does not match task row count")
        for row, raw_task in zip(tasks, task_objects):
            task = TaskSpecV1.model_validate(raw_task)
            if task.task_id != row["task_id"] or task.ordinal != row["ordinal"]:
                raise PreparedHandoffError("Task identity/order differs from manifest")
            if canonical_json(task.model_dump(mode="json")) != row["task_spec_json"]:
                raise PreparedHandoffError(f"Task snapshot mismatch: {task.task_id}")
            if (
                task.raw_content_sha256 != row["raw_content_sha256"]
                or task.normalized_content_sha256 != row["normalized_content_sha256"]
            ):
                raise PreparedHandoffError(f"Task hash mismatch: {task.task_id}")

        actual_manifest = self.compute_workspace_manifest(root)
        expected_files = source_manifest.get("files")
        if not isinstance(expected_files, dict):
            raise PreparedHandoffError("Source manifest file inventory is invalid")
        expected_core = {
            path: {
                "path": meta["path"],
                "mode": meta["mode"],
                "size": meta["size"],
                "sha256": meta["sha256"],
            }
            for path, meta in expected_files.items()
        }
        if actual_manifest != expected_core:
            raise PreparedHandoffError("Workspace bytes differ from prepared source manifest")
        manifest_sha = self.manifest_sha(actual_manifest)
        if manifest_sha != source["content_tree_sha256"]:
            raise PreparedHandoffError("Workspace content-tree hash differs from source identity")

        # Permission bits are an additional guardrail and evidence for the PRD 2
        # no-write boundary. The controller also compares manifests before/after.
        for current_root, dirs, files in os.walk(root, followlinks=False):
            for name in [*dirs, *files]:
                path = Path(current_root) / name
                if not path.is_symlink() and path.stat().st_mode & 0o222:
                    raise PreparedHandoffError(f"Workspace entry is writable: {path.relative_to(root)}")
        if root.stat().st_mode & 0o222:
            raise PreparedHandoffError("Workspace root is writable")

        return VerifiedHandoff(
            run_id=run_id,
            source_revision=source["baseline_commit"],
            content_tree_sha256=source["content_tree_sha256"],
            workspace_root=root,
            task_ids=[row["task_id"] for row in tasks],
            manifest_sha256=manifest_sha,
        )

    @staticmethod
    def compute_workspace_manifest(root: Path) -> Dict[str, Dict[str, Any]]:
        manifest: Dict[str, Dict[str, Any]] = {}
        for current_root, dirs, files in os.walk(root, topdown=True, followlinks=False):
            if ".git" in dirs:
                dirs.remove(".git")
            dirs.sort()
            for name in sorted(files):
                path = Path(current_root) / name
                relative = path.relative_to(root).as_posix()
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode):
                    target = os.readlink(path).encode("utf-8")
                    mode = "120000"
                    content = target
                elif stat.S_ISREG(info.st_mode):
                    content = path.read_bytes()
                    mode = "100755" if info.st_mode & 0o111 else "100644"
                else:
                    raise PreparedHandoffError(f"Special workspace object is not allowed: {relative}")
                manifest[relative] = {
                    "path": relative,
                    "mode": mode,
                    "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
        return manifest

    @staticmethod
    def manifest_sha(manifest: Dict[str, Dict[str, Any]]) -> str:
        lines = [
            f"{manifest[path]['mode']} {manifest[path]['sha256']} {path}"
            for path in sorted(manifest)
        ]
        text = "\n".join(lines) + ("\n" if lines else "")
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
