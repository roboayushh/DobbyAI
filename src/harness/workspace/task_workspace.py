"""Registered writable task workspaces backed by the run's private Git repository.

Layout (all beneath the registered run root)::

    runs/<run>/repo.git                      host-only Git metadata (never mounted)
    runs/<run>/workspace                     PRD 1 read-only baseline (never mutated)
    runs/<run>/tasks/<task>/workspace        writable task checkout (mounted at /workspace)
    runs/<run>/tasks/<task>/actions/<act>    per-action context/output staging

Each accepted workspace state is a private checkpoint commit. The commit OID is
also the PRD 2 ``source_revision`` used for indexing, evidence, and coder
proposals, so every model-visible fact is bound to exact bytes.
"""
from __future__ import annotations

import datetime
import hashlib
import os
import secrets
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from harness.gitflow.private_git import (
    PrivateGit,
    RefCASError,
    make_writable_tree,
    safe_ref_component,
    validate_tree_path,
)
from harness.persistence import ArtifactStore, RunStore, canonical_json
from harness.workspace.manifest import (
    WorkspaceManifest,
    diff_manifests,
    scan_workspace,
)


class WorkspaceIntegrityError(RuntimeError):
    code = "WORKSPACE_INTEGRITY_FAILURE"


class RestoreVerificationError(WorkspaceIntegrityError):
    code = "WORKSPACE_RESTORE_UNVERIFIED"


class WorkspaceLockError(RuntimeError):
    code = "WORKSPACE_LOCKED"


class StaleWorkspaceVersionError(RuntimeError):
    code = "STALE_WORKSPACE_VERSION"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


@dataclass(frozen=True)
class WorkspaceVersion:
    version_id: str
    run_id: str
    task_id: str
    parent_version_id: Optional[str]
    baseline_commit: str
    commit: str
    tree: str
    content_tree_sha256: str
    manifest_artifact_id: str
    created_by_action_id: Optional[str]
    state: str
    created_at: str

    @classmethod
    def from_row(cls, row: Any) -> "WorkspaceVersion":
        return cls(
            row["workspace_version_id"],
            row["run_id"],
            row["task_id"],
            row["parent_version_id"],
            row["baseline_commit"],
            row["private_checkpoint_commit"],
            row["git_tree"],
            row["content_tree_sha256"],
            row["manifest_artifact_id"],
            row["created_by_action_id"],
            row["state"],
            row["created_at"],
        )

    def contract(self) -> Dict[str, Any]:
        return {
            "schema_version": "1.0",
            "workspace_version_id": self.version_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "parent_version_id": self.parent_version_id,
            "baseline_commit": self.baseline_commit,
            "private_checkpoint_commit": self.commit,
            "git_tree": self.tree,
            "content_tree_sha256": self.content_tree_sha256,
            "manifest_artifact_id": self.manifest_artifact_id,
            "created_by_action_id": self.created_by_action_id,
            "state": self.state,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class PreparedCheckpoint:
    """Git objects written for a new version; the DB row is inserted by the caller."""

    version_id: str
    commit: str
    tree: str
    content_tree_sha256: str
    manifest_artifact_id: str
    manifest: WorkspaceManifest


@dataclass(frozen=True)
class CandidateRecord:
    candidate_id: str
    run_id: str
    task_id: str
    workspace_version_id: str
    baseline_commit: str
    task_start_commit: str
    commit: str
    tree: str
    candidate_sha256: str
    manifest_artifact_id: str
    diff_artifact_id: str
    changed_paths: List[str]
    ordinal: int
    created_at: str


def task_ref(run_id: str, task_id: str, kind: str) -> str:
    return f"refs/harness/runs/{safe_ref_component(run_id)}/tasks/{safe_ref_component(task_id)}/{kind}"


class TaskWorkspaceService:
    def __init__(self, run_store: RunStore, artifact_store: ArtifactStore, data_root: str | Path) -> None:
        self.run_store = run_store
        self.artifact_store = artifact_store
        self.data_root = Path(data_root).resolve()

    # ------------------------------------------------------------ locations
    def run_root(self, run_id: str) -> Path:
        safe_ref_component(run_id)
        root = (self.data_root / "runs" / run_id).resolve()
        root.relative_to((self.data_root / "runs").resolve())
        return root

    def git(self, run_id: str) -> PrivateGit:
        workspace = self.run_store.get_workspace(run_id)
        if not workspace:
            raise WorkspaceIntegrityError(f"Run {run_id} has no prepared workspace")
        git_dir = (self.data_root / workspace["bare_repo_relpath"]).resolve()
        git_dir.relative_to(self.run_root(run_id))
        return PrivateGit(git_dir)

    def task_dir(self, run_id: str, task_id: str) -> Path:
        safe_ref_component(task_id)
        path = self.run_root(run_id) / "tasks" / task_id
        path.resolve().relative_to(self.run_root(run_id))
        return path

    def workspace_root(self, run_id: str, task_id: str) -> Path:
        return self.task_dir(run_id, task_id) / "workspace"

    # ------------------------------------------------------------- queries
    def get(self, run_id: str, task_id: str) -> Optional[Dict[str, Any]]:
        with self.run_store.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM h_task_workspaces WHERE task_id = ? AND run_id = ?", (task_id, run_id)
            ).fetchone()
        return dict(row) if row else None

    def active_task_root(self, run_id: str) -> Optional[Path]:
        """Root of the lifecycle's active task workspace, if one exists."""
        with self.run_store.get_connection() as conn:
            row = conn.execute(
                """
                SELECT w.root_relpath FROM h_task_workspaces w
                JOIN h_run_lifecycle l ON l.active_task_id = w.task_id AND l.run_id = w.run_id
                WHERE w.run_id = ? AND w.state = 'ACTIVE'
                """,
                (run_id,),
            ).fetchone()
        if not row:
            return None
        root = (self.data_root / row["root_relpath"]).resolve()
        root.relative_to(self.run_root(run_id))
        return root

    def current_version(self, run_id: str, task_id: str) -> WorkspaceVersion:
        with self.run_store.get_connection() as conn:
            row = conn.execute(
                """
                SELECT v.* FROM h_workspace_versions v
                WHERE v.run_id = ? AND v.task_id = ? AND v.state = 'ACTIVE'
                """,
                (run_id, task_id),
            ).fetchone()
        if not row:
            raise WorkspaceIntegrityError(f"Task {task_id} has no active workspace version")
        return WorkspaceVersion.from_row(row)

    def get_version(self, version_id: str) -> WorkspaceVersion:
        with self.run_store.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM h_workspace_versions WHERE workspace_version_id = ?", (version_id,)
            ).fetchone()
        if not row:
            raise KeyError(f"Unknown workspace version {version_id}")
        return WorkspaceVersion.from_row(row)

    def version_by_commit(self, run_id: str, task_id: str, commit: str) -> Optional[WorkspaceVersion]:
        with self.run_store.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM h_workspace_versions WHERE run_id = ? AND task_id = ? AND private_checkpoint_commit = ?",
                (run_id, task_id, commit),
            ).fetchone()
        return WorkspaceVersion.from_row(row) if row else None

    def versions(self, run_id: str, task_id: str) -> List[WorkspaceVersion]:
        with self.run_store.get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM h_workspace_versions WHERE run_id = ? AND task_id = ? ORDER BY created_at, rowid",
                (run_id, task_id),
            ).fetchall()
        return [WorkspaceVersion.from_row(row) for row in rows]

    def load_manifest(self, version: WorkspaceVersion) -> WorkspaceManifest:
        artifact = self.artifact_store.get_artifact_by_id(version.manifest_artifact_id)
        if not artifact:
            raise WorkspaceIntegrityError("Workspace manifest artifact is missing")
        relative = artifact["relative_path"].split("/artifacts/", 1)[-1]
        if not self.artifact_store.verify(version.run_id, relative):
            raise WorkspaceIntegrityError("Workspace manifest artifact failed verification")
        import json

        manifest = WorkspaceManifest.from_json(json.loads(self.artifact_store.open_readonly(version.run_id, relative)))
        if manifest.content_tree_sha256() != version.content_tree_sha256:
            raise WorkspaceIntegrityError("Workspace manifest hash differs from its version row")
        return manifest

    def scan(self, run_id: str, task_id: str, **limits: Any) -> WorkspaceManifest:
        git = self.git(run_id)
        return scan_workspace(self.workspace_root(run_id, task_id), object_format=git.object_format, **limits)

    def verify_current(self, run_id: str, task_id: str) -> WorkspaceManifest:
        """Scan the workspace and require it to equal the active version exactly."""
        version = self.current_version(run_id, task_id)
        expected = self.load_manifest(version)
        actual = self.scan(run_id, task_id)
        if actual.special or actual.reserved or actual.unsafe_names:
            raise WorkspaceIntegrityError("Workspace contains special, reserved, or unsafe entries")
        if actual.core_view() != expected.core_view():
            raise WorkspaceIntegrityError(
                f"Workspace bytes differ from active version {version.version_id}"
            )
        return actual

    # ------------------------------------------------------------ creation
    def ensure(self, run_id: str, task_id: str, start_commit: str, baseline_commit: str) -> WorkspaceVersion:
        """Create (or verify) the task workspace at ``start_commit``. Idempotent."""
        existing = self.get(run_id, task_id)
        if existing:
            if existing["task_start_commit"] != start_commit:
                raise WorkspaceIntegrityError("Task workspace exists with a different start commit")
            return self.current_version(run_id, task_id)

        git = self.git(run_id)
        commit = git.rev_parse(f"{start_commit}^{{commit}}")
        if not commit:
            raise WorkspaceIntegrityError(f"Task start commit is not in the private repository: {start_commit}")
        tree = git.commit_tree_of(commit)
        root = self.workspace_root(run_id, task_id)
        if root.exists():
            # A previous attempt crashed before its row was committed.
            shutil.rmtree(root)
        root.parent.mkdir(parents=True, exist_ok=True)
        skipped = git.materialize(tree, root)
        manifest = scan_workspace(root, object_format=git.object_format)
        expected_entries = {
            entry.path: entry for entry in git.ls_tree(tree) if entry.object_type == "blob"
        }
        if {path: entry.oid for path, entry in manifest.entries.items()} != {
            path: entry.oid for path, entry in expected_entries.items()
        }:
            raise WorkspaceIntegrityError("Materialized workspace differs from its start tree")

        version_id = f"wsv_{uuid.uuid4().hex[:16]}"
        manifest_artifact = self._write_manifest(run_id, task_id, version_id, manifest)
        start_ref = task_ref(run_id, task_id, "start")
        working_ref = task_ref(run_id, task_id, "working")
        for ref in (start_ref, working_ref):
            current = git.read_ref(ref)
            if current is None:
                git.update_ref_cas(ref, commit, None)
            elif current != commit:
                raise WorkspaceIntegrityError(f"Managed ref {ref} already points elsewhere")
        now = _now()
        rel_root = str(root.relative_to(self.data_root))
        with self.run_store.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    INSERT INTO h_workspace_versions(
                        workspace_version_id, run_id, task_id, parent_version_id, baseline_commit,
                        private_checkpoint_commit, git_tree, content_tree_sha256,
                        manifest_artifact_id, created_by_action_id, state, created_at
                    ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, NULL, 'ACTIVE', ?)
                    """,
                    (
                        version_id, run_id, task_id, baseline_commit, commit, tree,
                        manifest.content_tree_sha256(), manifest_artifact, now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO h_task_workspaces(
                        task_id, run_id, root_relpath, task_start_commit, task_start_tree,
                        object_format, current_version_id, state, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)
                    """,
                    (task_id, run_id, rel_root, commit, tree, git.object_format, version_id, now, now),
                )
                self._event(
                    conn,
                    run_id,
                    "TASK_WORKSPACE_CREATED",
                    {
                        "task_id": task_id,
                        "workspace_version_id": version_id,
                        "start_commit": commit,
                        "content_tree_sha256": manifest.content_tree_sha256(),
                        "skipped_gitlinks": skipped,
                    },
                )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return self.current_version(run_id, task_id)

    def _write_manifest(self, run_id: str, task_id: str, version_id: str, manifest: WorkspaceManifest) -> str:
        path = f"prd3/workspace/{task_id}/{version_id}-manifest.json"
        self.artifact_store.write_json(run_id, path, manifest.to_json(), "workspace_manifest", task_id)
        artifact = self.artifact_store.get_artifact_by_path(run_id, path)
        if not artifact:
            raise WorkspaceIntegrityError("Manifest artifact metadata was not persisted")
        return artifact["artifact_id"]

    # ---------------------------------------------------------- checkpoints
    def prepare_checkpoint(
        self,
        run_id: str,
        task_id: str,
        parent: WorkspaceVersion,
        manifest: WorkspaceManifest,
        message: str,
    ) -> PreparedCheckpoint:
        """Write exact blobs/tree/commit for ``manifest``. No DB version row yet."""
        git = self.git(run_id)
        root = self.workspace_root(run_id, task_id)
        parent_manifest = self.load_manifest(parent)
        known = {entry.oid for entry in parent_manifest.entries.values()}
        regular = [
            entry for entry in manifest.entries.values() if entry.oid not in known and entry.mode != "120000"
        ]
        written = git.hash_files([root / entry.path for entry in regular])
        for entry, oid in zip(regular, written):
            if oid != entry.oid:
                raise WorkspaceIntegrityError(f"Blob identity mismatch while checkpointing {entry.path}")
        for entry in manifest.entries.values():
            if entry.oid not in known and entry.mode == "120000":
                target = os.readlink(root / entry.path).encode("utf-8", "surrogateescape")
                if git.hash_bytes(target) != entry.oid:
                    raise WorkspaceIntegrityError(f"Symlink identity mismatch for {entry.path}")
        for mode, _, path in manifest.tree_entries():
            validate_tree_path(path)
        tree = git.write_tree(manifest.tree_entries())
        commit = git.commit_tree(tree, [parent.commit], message)
        version_id = f"wsv_{uuid.uuid4().hex[:16]}"
        manifest_artifact = self._write_manifest(run_id, task_id, version_id, manifest)
        return PreparedCheckpoint(version_id, commit, tree, manifest.content_tree_sha256(), manifest_artifact, manifest)

    def commit_checkpoint_sql(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        task_id: str,
        parent: WorkspaceVersion,
        checkpoint: PreparedCheckpoint,
        action_id: Optional[str],
    ) -> None:
        """Insert the new ACTIVE version inside the caller's settlement transaction."""
        now = _now()
        changed = conn.execute(
            """
            UPDATE h_workspace_versions SET state = 'SUPERSEDED'
            WHERE workspace_version_id = ? AND state = 'ACTIVE'
            """,
            (parent.version_id,),
        ).rowcount
        if changed != 1:
            raise StaleWorkspaceVersionError("Parent workspace version is no longer active")
        conn.execute(
            """
            INSERT INTO h_workspace_versions(
                workspace_version_id, run_id, task_id, parent_version_id, baseline_commit,
                private_checkpoint_commit, git_tree, content_tree_sha256,
                manifest_artifact_id, created_by_action_id, state, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?)
            """,
            (
                checkpoint.version_id, run_id, task_id, parent.version_id, parent.baseline_commit,
                checkpoint.commit, checkpoint.tree, checkpoint.content_tree_sha256,
                checkpoint.manifest_artifact_id, action_id, now,
            ),
        )
        conn.execute(
            "UPDATE h_task_workspaces SET current_version_id = ?, updated_at = ? WHERE task_id = ?",
            (checkpoint.version_id, now, task_id),
        )

    def publish_checkpoint_refs(self, run_id: str, task_id: str, parent: WorkspaceVersion, checkpoint: PreparedCheckpoint) -> None:
        """Advance the working ref and record a hidden checkpoint ref (idempotent)."""
        git = self.git(run_id)
        working = task_ref(run_id, task_id, "working")
        current = git.read_ref(working)
        if current == checkpoint.commit:
            return
        try:
            git.update_ref_cas(working, checkpoint.commit, parent.commit)
        except RefCASError:
            if git.read_ref(working) != checkpoint.commit:
                raise
        sequence = len(self.versions(run_id, task_id))
        checkpoint_ref = task_ref(run_id, task_id, f"checkpoints/{sequence:04d}")
        if git.read_ref(checkpoint_ref) is None:
            git.update_ref_cas(checkpoint_ref, checkpoint.commit, None)

    # --------------------------------------------------------------- restore
    def restore(self, run_id: str, task_id: str, target: WorkspaceManifest) -> WorkspaceManifest:
        """Restore the registered task workspace to exactly ``target``; verify."""
        git = self.git(run_id)
        root = self.workspace_root(run_id, task_id).resolve()
        root.relative_to(self.run_root(run_id))
        make_writable_tree(root)
        # 1. Remove everything the target does not contain (bottom-up).
        for current_root, dirs, files in os.walk(root, topdown=False, followlinks=False):
            current = Path(current_root)
            for name in files + [d for d in dirs if (current / d).is_symlink()]:
                full = current / name
                relative = full.relative_to(root).as_posix()
                entry = target.entries.get(relative)
                wants_link = entry is not None and entry.mode == "120000"
                is_link = full.is_symlink()
                is_regular = full.is_file() and not is_link
                keep = entry is not None and ((wants_link and is_link) or (not wants_link and is_regular))
                if not keep:
                    full.unlink()
            for name in dirs:
                full = current / name
                relative = full.relative_to(root).as_posix()
                if full.is_symlink() or not full.exists():
                    continue  # symlinked directories were settled with files above; never followed
                if relative not in target.directories:
                    if relative in target.entries:
                        shutil.rmtree(full)
                        continue
                    try:
                        full.rmdir()
                    except OSError:
                        shutil.rmtree(full)
        # 2. Recreate every missing or differing entry from the object database.
        current = scan_workspace(root, object_format=git.object_format)
        needed = [
            entry for path, entry in target.entries.items()
            if path not in current.entries
            or current.entries[path].sha256 != entry.sha256
            or current.entries[path].mode != entry.mode
        ]
        blobs = git.cat_blobs(entry.oid for entry in needed)
        for entry in needed:
            validate_tree_path(entry.path)
            full = root / entry.path
            for parent in reversed(full.relative_to(root).parents):
                if str(parent) == ".":
                    continue
                directory = root / parent
                if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
                    directory.unlink()
                directory.mkdir(exist_ok=True)
            if full.is_symlink() or full.exists():
                if full.is_dir() and not full.is_symlink():
                    shutil.rmtree(full)
                else:
                    full.unlink()
            data = blobs[entry.oid]
            if entry.mode == "120000":
                os.symlink(data.decode("utf-8", "surrogateescape"), full)
            else:
                with open(full, "xb") as handle:
                    handle.write(data)
                os.chmod(full, 0o755 if entry.mode == "100755" else 0o644)
        for directory in sorted(target.directories):
            (root / directory).mkdir(parents=True, exist_ok=True)
        # 3. Verify the exact result.
        restored = scan_workspace(root, object_format=git.object_format)
        if (
            restored.core_view() != target.core_view()
            or restored.special
            or restored.reserved
            or restored.unsafe_names
            or restored.directories != target.directories
        ):
            raise RestoreVerificationError("Workspace restore could not be proven exact")
        return restored

    # ------------------------------------------------------------ candidate
    def freeze_candidate(
        self,
        run_id: str,
        task_id: str,
        *,
        summary: str,
        trailers: Dict[str, str],
    ) -> CandidateRecord:
        """Create the immutable one-commit candidate (parent = task start)."""
        workspace = self.get(run_id, task_id)
        if not workspace:
            raise WorkspaceIntegrityError("Task workspace does not exist")
        version = self.current_version(run_id, task_id)
        manifest = self.verify_current(run_id, task_id)
        git = self.git(run_id)
        start = workspace["task_start_commit"]
        candidate_id = f"cand_{uuid.uuid4().hex[:16]}"
        message = build_commit_message(summary, {**trailers, "Harness-Candidate-ID": candidate_id})
        tree = version.tree
        commit = git.commit_tree(tree, [start], message)
        if git.commit_tree_of(commit) != tree or git.commit_parents(commit) != [start]:
            raise WorkspaceIntegrityError("Candidate commit shape is invalid")
        diff = git.diff(start, commit)
        changed = [path for _, path in git.diff_paths(start, commit)]
        with self.run_store.get_connection() as conn:
            ordinal = conn.execute(
                "SELECT COALESCE(MAX(candidate_ordinal), 0) + 1 FROM h_candidate_snapshots WHERE run_id = ? AND task_id = ?",
                (run_id, task_id),
            ).fetchone()[0]
        manifest_path = f"prd3/candidates/{task_id}/{candidate_id}-manifest.json"
        diff_path = f"prd3/candidates/{task_id}/{candidate_id}.diff"
        self.artifact_store.write_json(run_id, manifest_path, manifest.to_json(), "candidate_manifest", task_id)
        self.artifact_store.write_bytes(run_id, diff_path, diff, "text/x-diff", "candidate_diff", task_id)
        manifest_artifact = self.artifact_store.get_artifact_by_path(run_id, manifest_path)
        diff_artifact = self.artifact_store.get_artifact_by_path(run_id, diff_path)
        assert manifest_artifact and diff_artifact
        candidate_ref = task_ref(run_id, task_id, "candidate")
        previous = git.read_ref(candidate_ref)
        git.update_ref_cas(candidate_ref, commit, previous)
        record = CandidateRecord(
            candidate_id=candidate_id,
            run_id=run_id,
            task_id=task_id,
            workspace_version_id=version.version_id,
            baseline_commit=version.baseline_commit,
            task_start_commit=start,
            commit=commit,
            tree=tree,
            candidate_sha256=manifest.content_tree_sha256(),
            manifest_artifact_id=manifest_artifact["artifact_id"],
            diff_artifact_id=diff_artifact["artifact_id"],
            changed_paths=changed,
            ordinal=int(ordinal),
            created_at=_now(),
        )
        return record

    def insert_candidate_sql(self, conn: sqlite3.Connection, record: CandidateRecord, handoff_artifact_id: Optional[str]) -> None:
        conn.execute(
            """
            INSERT INTO h_candidate_snapshots(
                candidate_id, run_id, task_id, workspace_version_id, baseline_commit,
                task_start_commit, candidate_commit, candidate_tree, candidate_sha256,
                manifest_artifact_id, diff_artifact_id, handoff_artifact_id, frozen,
                verification_status, candidate_ordinal, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'NOT_RUN', ?, ?)
            """,
            (
                record.candidate_id, record.run_id, record.task_id, record.workspace_version_id,
                record.baseline_commit, record.task_start_commit, record.commit, record.tree,
                record.candidate_sha256, record.manifest_artifact_id, record.diff_artifact_id,
                handoff_artifact_id, record.ordinal, record.created_at,
            ),
        )

    def get_candidate(self, candidate_id: str) -> Optional[Dict[str, Any]]:
        with self.run_store.get_connection() as conn:
            row = conn.execute("SELECT * FROM h_candidate_snapshots WHERE candidate_id = ?", (candidate_id,)).fetchone()
        return dict(row) if row else None

    def latest_candidate(self, run_id: str, task_id: str) -> Optional[Dict[str, Any]]:
        with self.run_store.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM h_candidate_snapshots WHERE run_id = ? AND task_id = ? ORDER BY candidate_ordinal DESC LIMIT 1",
                (run_id, task_id),
            ).fetchone()
        return dict(row) if row else None

    def verify_candidate(self, candidate: Dict[str, Any]) -> None:
        """Rehash a frozen candidate from the object database (immutability check)."""
        git = self.git(candidate["run_id"])
        if git.commit_tree_of(candidate["candidate_commit"]) != candidate["candidate_tree"]:
            raise WorkspaceIntegrityError("Candidate tree identity changed")
        if git.commit_parents(candidate["candidate_commit"]) != [candidate["task_start_commit"]]:
            raise WorkspaceIntegrityError("Candidate parent identity changed")
        entries = [entry for entry in git.ls_tree(candidate["candidate_tree"]) if entry.object_type == "blob"]
        blobs = git.cat_blobs(entry.oid for entry in entries)
        lines = []
        for entry in sorted(entries, key=lambda item: item.path):
            lines.append(f"{entry.mode} {hashlib.sha256(blobs[entry.oid]).hexdigest()} {entry.path}")
        digest = hashlib.sha256(("\n".join(lines) + ("\n" if lines else "")).encode()).hexdigest()
        if digest != candidate["candidate_sha256"]:
            raise WorkspaceIntegrityError("Candidate content hash changed")

    def quarantine(self, run_id: str, task_id: str, reason: str) -> None:
        with self.run_store.get_connection() as conn:
            with conn:
                conn.execute(
                    "UPDATE h_task_workspaces SET state = 'QUARANTINED', updated_at = ? WHERE task_id = ?",
                    (_now(), task_id),
                )
                self._event(conn, run_id, "WORKSPACE_QUARANTINED", {"task_id": task_id, "reason": reason})

    @staticmethod
    def _event(conn: sqlite3.Connection, run_id: str, event_type: str, payload: Dict[str, Any]) -> None:
        from harness.persistence.events import append_event_sql

        append_event_sql(conn, run_id, event_type, payload, "PRD3", "PRD3", _now())


# ---------------------------------------------------------------- messages
def sanitize_summary(text: str, limit: int = 72) -> str:
    cleaned = "".join(ch if ch.isprintable() and ch not in "\r\n\t" else " " for ch in text or "")
    cleaned = " ".join(cleaned.split())
    for marker in ("http://", "https://", "@"):
        cleaned = cleaned.replace(marker, " ")
    cleaned = " ".join(cleaned.split())
    return (cleaned[:limit].rstrip() or "Harness task candidate")


def build_commit_message(summary: str, trailers: Dict[str, str]) -> str:
    lines = [sanitize_summary(summary), ""]
    for key, value in trailers.items():
        if not all(ch.isalnum() or ch == "-" for ch in key):
            raise ValueError(f"Invalid commit trailer key: {key}")
        safe_value = "".join(ch for ch in str(value) if ch.isalnum() or ch in "_-.:")[:128]
        lines.append(f"{key}: {safe_value}")
    return "\n".join(lines).rstrip() + "\n"


# ------------------------------------------------------------------- locks
class WorkspaceLockService:
    """Exactly one writer per task workspace, bound to an exact version."""

    def __init__(self, run_store: RunStore) -> None:
        self.run_store = run_store

    def acquire(
        self,
        run_id: str,
        task_id: str,
        version_id: str,
        owner_id: str,
        *,
        purpose: str,
        ttl_seconds: int = 900,
    ) -> str:
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        now = datetime.datetime.now(datetime.timezone.utc)
        expires = now + datetime.timedelta(seconds=ttl_seconds)
        with self.run_store.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute("SELECT * FROM h_workspace_locks WHERE task_id = ?", (task_id,)).fetchone()
                if existing and datetime.datetime.fromisoformat(existing["expires_at"]) > now:
                    raise WorkspaceLockError(f"Task workspace {task_id} is locked by {existing['owner_id']}")
                active = conn.execute(
                    "SELECT workspace_version_id FROM h_workspace_versions WHERE task_id = ? AND state = 'ACTIVE'",
                    (task_id,),
                ).fetchone()
                if not active or active["workspace_version_id"] != version_id:
                    raise StaleWorkspaceVersionError(
                        f"Requested version {version_id} is not the active version"
                    )
                conn.execute("DELETE FROM h_workspace_locks WHERE task_id = ?", (task_id,))
                conn.execute(
                    """
                    INSERT INTO h_workspace_locks(
                        task_id, run_id, workspace_version_id, owner_id, purpose,
                        lease_token_sha256, acquired_at, heartbeat_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (task_id, run_id, version_id, owner_id, purpose, digest, now.isoformat(), now.isoformat(), expires.isoformat()),
                )
                TaskWorkspaceService._event(
                    conn, run_id, "WORKSPACE_LOCK_ACQUIRED",
                    {"task_id": task_id, "workspace_version_id": version_id, "purpose": purpose},
                )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return token

    def assert_held(self, task_id: str, token: str) -> None:
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.run_store.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM h_workspace_locks WHERE task_id = ? AND lease_token_sha256 = ?",
                (task_id, digest),
            ).fetchone()
        if not row:
            raise WorkspaceLockError("Workspace lock is not held by this owner")

    def renew(self, task_id: str, token: str, ttl_seconds: int = 900) -> None:
        digest = hashlib.sha256(token.encode()).hexdigest()
        now = datetime.datetime.now(datetime.timezone.utc)
        with self.run_store.get_connection() as conn:
            with conn:
                changed = conn.execute(
                    "UPDATE h_workspace_locks SET heartbeat_at = ?, expires_at = ? WHERE task_id = ? AND lease_token_sha256 = ?",
                    (now.isoformat(), (now + datetime.timedelta(seconds=ttl_seconds)).isoformat(), task_id, digest),
                ).rowcount
        if changed != 1:
            raise WorkspaceLockError("Cannot renew a lock that is not held")

    def release(self, task_id: str, token: str) -> None:
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.run_store.get_connection() as conn:
            with conn:
                conn.execute(
                    "DELETE FROM h_workspace_locks WHERE task_id = ? AND lease_token_sha256 = ?",
                    (task_id, digest),
                )

    def break_expired(self, task_id: str) -> bool:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self.run_store.get_connection() as conn:
            with conn:
                changed = conn.execute(
                    "DELETE FROM h_workspace_locks WHERE task_id = ? AND expires_at <= ?", (task_id, now)
                ).rowcount
        return changed == 1

    def force_release(self, task_id: str) -> None:
        """Recovery-only: a crashed owner can never settle, so its lock is void."""
        with self.run_store.get_connection() as conn:
            with conn:
                conn.execute("DELETE FROM h_workspace_locks WHERE task_id = ?", (task_id,))
