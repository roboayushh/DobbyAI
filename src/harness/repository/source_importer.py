"""harness/repository/source_importer.py
SourceImporter – captures canonical manifests, imports local and public HTTPS sources
into a private bare Git repository, and establishes immutable baseline B.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
import stat
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from harness.contracts import (
    LimitsV1,
    SourceIdentityV1,
    compute_sha256,
)
from harness.persistence.run_store import canonical_json
from harness.repository.git_runner import GitCommandError, GitRunner


def is_disposable(path: str) -> bool:
    # Imported lazily: harness.workspace.manifest -> harness.gitflow -> harness.repository would cycle.
    from harness.workspace.manifest import is_disposable as _is_disposable

    return _is_disposable(path)


class SourceChangedDuringImportError(Exception):
    pass


class SourcePolicyError(Exception):
    pass


class LimitsExceededError(Exception):
    pass


class SourceImporter:
    def __init__(self, git_runner: Optional[GitRunner] = None):
        self.git = git_runner or GitRunner()

    def scan_worktree(
        self,
        source_dir: Path,
        limits: LimitsV1,
        *,
        use_git_ignores: bool = True,
    ) -> Tuple[Dict[str, Dict[str, Any]], int, int]:
        """Scan worktree files (excluding .git), enforce limits and policies,
        and generate canonical file manifest.
        Returns: (manifest_entries, total_bytes, file_count)
        """
        source_resolved = source_dir.resolve()
        entries: Dict[str, Dict[str, Any]] = {}
        total_bytes = 0
        file_count = 0

        # Discover ignored files via git if available
        ignored_relpaths: Set[str] = set()
        try:
            if not use_git_ignores:
                raise LookupError("git ignore rules do not apply to a non-Git folder")
            out, _ = self.git.run(
                ["status", "--ignored", "--porcelain=v1"],
                cwd=str(source_resolved),
                check=False,
            )
            for line in out.splitlines():
                if line.startswith("!! "):
                    path_str = line[3:].strip().rstrip("/")
                    ignored_relpaths.add(path_str)
        except Exception:
            pass

        for root, dirs, files in os.walk(source_resolved, topdown=True, followlinks=False):
            # Exclude .git directory
            if ".git" in dirs:
                dirs.remove(".git")

            rel_root = Path(root).relative_to(source_resolved)

            # Skip ignored directories
            dirs[:] = [
                d for d in dirs
                if str(rel_root / d if str(rel_root) != "." else d) not in ignored_relpaths
            ]

            # Tool caches (__pycache__, .pytest_cache, ...) are never part of a baseline:
            # stale bytecode can shadow real sources inside the sandbox.
            dirs[:] = [d for d in dirs if not is_disposable(str(rel_root / d if str(rel_root) != "." else d) + "/x")]
            for fname in files:
                rel_path = (rel_root / fname if str(rel_root) != "." else Path(fname))
                rel_str = str(rel_path)
                if is_disposable(rel_str):
                    continue

                # Skip ignored files
                if rel_str in ignored_relpaths or any(
                    rel_str.startswith(f"{ig}/") for ig in ignored_relpaths
                ):
                    continue

                full_path = Path(root) / fname
                lstat = full_path.lstat()

                # Disallow special files (sockets, fifos, devices)
                if stat.S_ISFIFO(lstat.st_mode) or stat.S_ISSOCK(lstat.st_mode) or stat.S_ISBLK(lstat.st_mode) or stat.S_ISCHR(lstat.st_mode):
                    raise SourcePolicyError(f"Special filesystem object rejected: {rel_str}")

                # Symlinks
                if stat.S_ISLNK(lstat.st_mode):
                    link_target = os.readlink(full_path)
                    # Check symlink escape
                    resolved_target = (Path(root) / link_target).resolve()
                    try:
                        resolved_target.relative_to(source_resolved)
                    except ValueError:
                        raise SourcePolicyError(f"Symlink escapes repository root: {rel_str} -> {link_target}")

                    content_hash = hashlib.sha256(link_target.encode("utf-8")).hexdigest()
                    size = len(link_target.encode("utf-8"))
                    mode = "120000"
                else:
                    size = lstat.st_size
                    total_bytes += size
                    file_count += 1

                    if total_bytes > limits.max_repo_bytes:
                        raise LimitsExceededError(
                            f"Repository size exceeds limit of {limits.max_repo_bytes} bytes"
                        )
                    if file_count > limits.max_file_count:
                        raise LimitsExceededError(
                            f"File count exceeds limit of {limits.max_file_count} files"
                        )

                    with open(full_path, "rb") as f:
                        file_bytes = f.read()
                    content_hash = hashlib.sha256(file_bytes).hexdigest()
                    is_exec = bool(lstat.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
                    mode = "100755" if is_exec else "100644"

                entries[rel_str] = {
                    "path": rel_str,
                    "mode": mode,
                    "size": size,
                    "sha256": content_hash,
                    "mtime_ns": lstat.st_mtime_ns,
                }

        return entries, total_bytes, file_count

    def compute_manifest_sha(self, manifest_entries: Dict[str, Dict[str, Any]]) -> str:
        """Compute deterministic content_tree_sha256 from sorted entries."""
        canonical_items = [
            f"{manifest_entries[p]['mode']} {manifest_entries[p]['sha256']} {p}"
            for p in sorted(manifest_entries.keys())
        ]
        manifest_text = "\n".join(canonical_items) + ("\n" if canonical_items else "")
        return hashlib.sha256(manifest_text.encode("utf-8")).hexdigest()

    def check_concurrent_mutation(
        self,
        source_dir: Path,
        manifest_entries: Dict[str, Dict[str, Any]],
    ) -> bool:
        """Re-stat source files to verify no concurrent changes occurred."""
        for path_str, meta in manifest_entries.items():
            full_path = source_dir / path_str
            if not full_path.exists() and not full_path.is_symlink():
                return False
            lstat = full_path.lstat()
            if lstat.st_mtime_ns != meta["mtime_ns"]:
                return False
        return True

    def import_local(
        self,
        source_path: str,
        private_bare_repo: Path,
        run_id: str,
        limits: LimitsV1,
        revision: Optional[str] = None,
    ) -> Tuple[SourceIdentityV1, Dict[str, Any], int, int]:
        source_dir = Path(source_path).resolve()
        if not source_dir.is_dir():
            raise ValueError(f"Local source is not a directory: {source_path}")

        # Check git worktree
        out, _ = self.git.run(["rev-parse", "--is-inside-work-tree"], cwd=str(source_dir), check=False)
        if out.strip() != "true":
            raise ValueError(f"Path is not a git worktree: {source_path}")

        # Resolve upstream commit U
        ref = revision or "HEAD"
        upstream_commit, _ = self.git.run(["rev-parse", ref], cwd=str(source_dir))
        upstream_commit = upstream_commit.strip()

        # Check if dirty
        status_out, _ = self.git.run(
            ["status", "--porcelain=v1"], cwd=str(source_dir)
        )
        is_dirty = bool(status_out.strip())

        # Scan worktree and generate manifest
        manifest_entries, total_bytes, file_count = self.scan_worktree(source_dir, limits)
        content_tree_sha = self.compute_manifest_sha(manifest_entries)

        # Re-stat to verify no concurrent modification (AT-010)
        if not self.check_concurrent_mutation(source_dir, manifest_entries):
            # Retry once
            manifest_entries, total_bytes, file_count = self.scan_worktree(source_dir, limits)
            content_tree_sha = self.compute_manifest_sha(manifest_entries)
            if not self.check_concurrent_mutation(source_dir, manifest_entries):
                raise SourceChangedDuringImportError("Source changed during import")

        # Initialize private bare repository
        private_bare_repo.mkdir(parents=True, exist_ok=True)
        self.git.run(["init", "--bare"], cwd=str(private_bare_repo))

        # Fetch all objects from source into private bare repo
        self.git.run(
            ["fetch", str(source_dir), f"{upstream_commit}:refs/baselines/upstream"],
            cwd=str(private_bare_repo),
        )

        if is_dirty:
            # `git status` also reports ignored-by-us caches; the checkout is dirty only
            # when the imported content really differs from U's tree.
            upstream_tree = self.git.run(["rev-parse", f"{upstream_commit}^{{tree}}"], cwd=str(private_bare_repo))[0].strip()
            upstream_manifest, _, _ = self._manifest_from_tree(private_bare_repo, upstream_tree, limits)
            is_dirty = self.compute_manifest_sha(upstream_manifest) != content_tree_sha
        if not is_dirty:
            # Clean checkout: baseline B is U
            baseline_commit = upstream_commit
            tree_out, _ = self.git.run(
                ["rev-parse", f"{baseline_commit}^{{tree}}"],
                cwd=str(private_bare_repo),
            )
            baseline_tree = tree_out.strip()
        else:
            # Dirty checkout: synthetic commit B (parent U) built byte-exactly from the
            # scanned working tree, so deletions and symlinks are represented and no
            # .gitattributes filter can alter content.
            baseline_tree = self._tree_from_manifest(private_bare_repo, source_dir, manifest_entries)
            baseline_commit = self._commit(private_bare_repo, baseline_tree, [upstream_commit],
                                           f"harness baseline import for run {run_id}")

        # Build import manifest artifact
        now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
        import_manifest = {
            "run_id": run_id,
            "source_path": str(source_dir),
            "upstream_commit": upstream_commit,
            "baseline_commit": baseline_commit,
            "baseline_tree": baseline_tree,
            "content_tree_sha256": content_tree_sha,
            "dirty_source_imported": is_dirty,
            "file_count": file_count,
            "total_bytes": total_bytes,
            "files": manifest_entries,
            "created_at": now_utc,
        }
        manifest_json = canonical_json(import_manifest)
        import_manifest_sha256 = compute_sha256(manifest_json)

        source_identity = SourceIdentityV1(
            schema_version="1.0",
            source_kind="local_git",
            canonical_locator=str(source_dir),
            upstream_commit=upstream_commit,
            baseline_commit=baseline_commit,
            baseline_tree=baseline_tree,
            content_tree_sha256=content_tree_sha,
            import_manifest_sha256=import_manifest_sha256,
            dirty_source_imported=is_dirty,
            created_at=now_utc,
        )

        return source_identity, import_manifest, total_bytes, file_count

    def import_remote(
        self,
        clone_url: str,
        private_bare_repo: Path,
        staging_dir: Path,
        run_id: str,
        limits: LimitsV1,
        revision: Optional[str] = None,
    ) -> Tuple[SourceIdentityV1, Dict[str, Any], int, int]:
        """Clone public HTTPS git repo into staging, then import into private bare repo."""
        staging_dir.mkdir(parents=True, exist_ok=True)
        clone_dest = staging_dir / "clone"

        # Clone into staging
        self.git.run(["clone", "--bare", clone_url, str(clone_dest)])

        # Resolve revision
        ref = revision or "HEAD"
        commit_out, _ = self.git.run(["rev-parse", ref], cwd=str(clone_dest))
        resolved_commit = commit_out.strip()

        tree_out, _ = self.git.run(["rev-parse", f"{resolved_commit}^{{tree}}"], cwd=str(clone_dest))
        baseline_tree = tree_out.strip()

        # Copy/move bare repo to private_bare_repo
        if private_bare_repo.exists():
            shutil.rmtree(private_bare_repo)
        shutil.copytree(clone_dest, private_bare_repo)

        manifest_entries, total_bytes, file_count = self._manifest_from_tree(private_bare_repo, baseline_tree, limits)

        content_tree_sha = self.compute_manifest_sha(manifest_entries)
        now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()

        import_manifest = {
            "run_id": run_id,
            "source_path": clone_url,
            "upstream_commit": resolved_commit,
            "baseline_commit": resolved_commit,
            "baseline_tree": baseline_tree,
            "content_tree_sha256": content_tree_sha,
            "dirty_source_imported": False,
            "file_count": file_count,
            "total_bytes": total_bytes,
            "files": manifest_entries,
            "created_at": now_utc,
        }
        manifest_json = canonical_json(import_manifest)
        import_manifest_sha256 = compute_sha256(manifest_json)

        source_identity = SourceIdentityV1(
            schema_version="1.0",
            source_kind="public_https",
            canonical_locator=clone_url,
            upstream_commit=resolved_commit,
            baseline_commit=resolved_commit,
            baseline_tree=baseline_tree,
            content_tree_sha256=content_tree_sha,
            import_manifest_sha256=import_manifest_sha256,
            dirty_source_imported=False,
            created_at=now_utc,
        )

        return source_identity, import_manifest, total_bytes, file_count

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _private_git(private_bare_repo: Path):
        from harness.gitflow.private_git import PrivateGit

        return PrivateGit(private_bare_repo)

    def _manifest_from_tree(self, private_bare_repo: Path, tree: str, limits: LimitsV1) -> Tuple[Dict[str, Dict[str, Any]], int, int]:
        git = self._private_git(private_bare_repo)
        entries = [entry for entry in git.ls_tree(tree) if entry.object_type == "blob"]
        manifest: Dict[str, Dict[str, Any]] = {}
        total_bytes = 0
        for start in range(0, len(entries), 500):
            batch = entries[start:start + 500]
            blobs = git.cat_blobs(entry.oid for entry in batch)
            for entry in batch:
                data = blobs[entry.oid]
                total_bytes += len(data)
                if total_bytes > limits.max_repo_bytes:
                    raise LimitsExceededError("Repository size exceeds limit")
                manifest[entry.path] = {"path": entry.path, "mode": entry.mode, "size": len(data),
                                        "sha256": hashlib.sha256(data).hexdigest(), "mtime_ns": 0}
            if len(manifest) > limits.max_file_count:
                raise LimitsExceededError("File count exceeds limit")
        return manifest, total_bytes, len(manifest)

    def _tree_from_manifest(self, private_bare_repo: Path, source_dir: Path, entries: Dict[str, Dict[str, Any]]) -> str:
        git = self._private_git(private_bare_repo)
        regular = [path for path, meta in sorted(entries.items()) if meta["mode"] != "120000"]
        oids = dict(zip(regular, git.hash_files([source_dir / path for path in regular])))
        rows = []
        for path, meta in sorted(entries.items()):
            if meta["mode"] == "120000":
                oid = git.hash_bytes(os.readlink(source_dir / path).encode("utf-8", "surrogateescape"))
            else:
                oid = oids[path]
            rows.append((meta["mode"], oid, path))
        return git.write_tree(rows)

    def _commit(self, private_bare_repo: Path, tree: str, parents: List[str], message: str) -> str:
        git = self._private_git(private_bare_repo)
        commit = git.commit_tree(tree, parents, message + "\n")
        self.git.run(["update-ref", "refs/heads/main", commit], cwd=str(private_bare_repo))
        return commit

    def import_folder(
        self,
        source_path: str,
        private_bare_repo: Path,
        run_id: str,
        limits: LimitsV1,
        *,
        source_kind: str = "local_folder",
        canonical_locator: Optional[str] = None,
        extra_manifest: Optional[Dict[str, Any]] = None,
    ) -> Tuple[SourceIdentityV1, Dict[str, Any], int, int]:
        """Import an ordinary (non-Git) folder; only the private copy gets Git metadata (FR03)."""
        source_dir = Path(source_path).resolve()
        if not source_dir.is_dir():
            raise ValueError(f"Local source is not a directory: {source_path}")
        manifest_entries, total_bytes, file_count = self.scan_worktree(source_dir, limits, use_git_ignores=False)
        if not self.check_concurrent_mutation(source_dir, manifest_entries):
            manifest_entries, total_bytes, file_count = self.scan_worktree(source_dir, limits, use_git_ignores=False)
            if not self.check_concurrent_mutation(source_dir, manifest_entries):
                raise SourceChangedDuringImportError("Source changed during import")
        content_tree_sha = self.compute_manifest_sha(manifest_entries)
        private_bare_repo.mkdir(parents=True, exist_ok=True)
        self.git.run(["init", "--bare"], cwd=str(private_bare_repo))
        baseline_tree = self._tree_from_manifest(private_bare_repo, source_dir, manifest_entries)
        baseline_commit = self._commit(private_bare_repo, baseline_tree, [], f"harness baseline import ({source_kind}) for run {run_id}")
        now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
        import_manifest = {
            "run_id": run_id,
            "source_path": canonical_locator or str(source_dir),
            "source_kind": source_kind,
            "upstream_commit": None,
            "baseline_commit": baseline_commit,
            "baseline_tree": baseline_tree,
            "content_tree_sha256": content_tree_sha,
            "dirty_source_imported": False,
            "file_count": file_count,
            "total_bytes": total_bytes,
            "files": manifest_entries,
            "created_at": now_utc,
            **(extra_manifest or {}),
        }
        identity = SourceIdentityV1(
            schema_version="1.0",
            source_kind=source_kind,
            canonical_locator=canonical_locator or str(source_dir),
            upstream_commit=None,
            baseline_commit=baseline_commit,
            baseline_tree=baseline_tree,
            content_tree_sha256=content_tree_sha,
            import_manifest_sha256=compute_sha256(canonical_json(import_manifest)),
            dirty_source_imported=False,
            created_at=now_utc,
        )
        return identity, import_manifest, total_bytes, file_count

    def import_zip(
        self,
        zip_path: str,
        private_bare_repo: Path,
        staging_dir: Path,
        run_id: str,
        limits: LimitsV1,
    ) -> Tuple[SourceIdentityV1, Dict[str, Any], int, int]:
        """Stream a ZIP through a bounded, validating extractor into private staging (FR05)."""
        from harness.repository.zip_import import extract_zip

        archive = Path(zip_path).resolve()
        target = Path(staging_dir) / "zip-extract"
        info = extract_zip(archive, target, limits)
        return self.import_folder(
            str(info["root"]), private_bare_repo, run_id, limits, source_kind="local_zip",
            canonical_locator=str(archive),
            extra_manifest={"zip_sha256": info["zip_sha256"], "zip_entries": info["entries"],
                            "stripped_root": info["stripped_root"]},
        )

