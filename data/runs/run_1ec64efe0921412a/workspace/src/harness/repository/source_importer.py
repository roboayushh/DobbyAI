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

            for fname in files:
                rel_path = (rel_root / fname if str(rel_root) != "." else Path(fname))
                rel_str = str(rel_path)

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

        if not is_dirty:
            # Clean checkout: baseline B is U
            baseline_commit = upstream_commit
            tree_out, _ = self.git.run(
                ["rev-parse", f"{baseline_commit}^{{tree}}"],
                cwd=str(private_bare_repo),
            )
            baseline_tree = tree_out.strip()
        else:
            # Dirty checkout: create synthetic commit B with parent U
            # We construct a temporary index to write the exact tree
            temp_index = private_bare_repo / f"index_{run_id}"
            env = {"GIT_INDEX_FILE": str(temp_index)}

            # Read base tree from upstream
            self.git.run(
                ["read-tree", upstream_commit],
                cwd=str(private_bare_repo),
                extra_env=env,
            )

            # Add all non-ignored modified/untracked files
            for rel_str, meta in manifest_entries.items():
                src_file = source_dir / rel_str
                if meta["mode"] == "120000":
                    continue  # symlinks handled separately if needed
                # Hash object into bare repo
                obj_id, _ = self.git.run(
                    ["hash-object", "-w", str(src_file)],
                    cwd=str(private_bare_repo),
                )
                obj_id = obj_id.strip()
                # Update index cacheinfo
                self.git.run(
                    ["update-index", "--add", "--cacheinfo", meta["mode"], obj_id, rel_str],
                    cwd=str(private_bare_repo),
                    extra_env=env,
                )

            # Write tree
            tree_id, _ = self.git.run(
                ["write-tree"],
                cwd=str(private_bare_repo),
                extra_env=env,
            )
            baseline_tree = tree_id.strip()

            # Create synthetic commit
            commit_msg = f"harness baseline import for run {run_id}"
            commit_id, _ = self.git.run(
                ["commit-tree", baseline_tree, "-p", upstream_commit, "-m", commit_msg],
                cwd=str(private_bare_repo),
            )
            baseline_commit = commit_id.strip()

            # Update ref
            self.git.run(
                ["update-ref", "refs/heads/main", baseline_commit],
                cwd=str(private_bare_repo),
            )

            if temp_index.exists():
                temp_index.unlink()

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

        # In bare repo, list files in tree to build manifest
        tree_files_out, _ = self.git.run(
            ["ls-tree", "-r", "--full-tree", "-l", baseline_tree],
            cwd=str(private_bare_repo),
        )

        manifest_entries: Dict[str, Dict[str, Any]] = {}
        total_bytes = 0
        file_count = 0

        for line in tree_files_out.splitlines():
            # Format: <mode> <type> <object> <size>\t<file>
            parts = line.split(maxsplit=4)
            if len(parts) < 5:
                continue
            mode, obj_type, obj_sha, size_str, rel_path = parts
            rel_path = rel_path.strip()
            size = int(size_str.strip()) if size_str.strip() != "-" else 0
            total_bytes += size
            file_count += 1

            if total_bytes > limits.max_repo_bytes:
                raise LimitsExceededError("Repository size exceeds limit")
            if file_count > limits.max_file_count:
                raise LimitsExceededError("File count exceeds limit")

            # Get file content sha256
            cat_out, _ = self.git.run(["cat-file", "-p", obj_sha], cwd=str(private_bare_repo))
            content_sha = compute_sha256(cat_out)

            manifest_entries[rel_path] = {
                "path": rel_path,
                "mode": mode,
                "size": size,
                "sha256": content_sha,
                "mtime_ns": 0,
            }

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
