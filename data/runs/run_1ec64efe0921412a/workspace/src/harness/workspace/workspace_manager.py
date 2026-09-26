"""harness/workspace/workspace_manager.py
WorkspaceManager – handles contained directory allocation, staging, atomic checkout,
read-only finalization, quarantine, and recovery.
"""
from __future__ import annotations

import datetime
import os
import shutil
import stat
from pathlib import Path
from typing import Optional, Tuple

from harness.repository.git_runner import GitRunner


class WorkspacePolicyError(Exception):
    pass


class WorkspaceManager:
    def __init__(self, data_root: str, git_runner: Optional[GitRunner] = None):
        self.data_root = Path(data_root).resolve()
        self.git = git_runner or GitRunner()

    def _safe_run_dir(self, run_id: str) -> Path:
        if "\0" in run_id or ".." in run_id or "/" in run_id or "\\" in run_id:
            raise WorkspacePolicyError(f"Unsafe run_id: {run_id}")
        run_dir = (self.data_root / "runs" / run_id).resolve()
        try:
            run_dir.relative_to(self.data_root / "runs")
        except ValueError:
            raise WorkspacePolicyError(f"Run dir escapes root: {run_id}")
        return run_dir

    def allocate(self, run_id: str) -> Tuple[Path, Path, Path]:
        """Allocate private bare repo, workspace, and temp dirs."""
        run_dir = self._safe_run_dir(run_id)
        bare_repo_dir = run_dir / "repo.git"
        worktree_dir = run_dir / "workspace"
        artifacts_dir = run_dir / "artifacts"
        temp_dir = run_dir / "temp"

        run_dir.mkdir(parents=True, exist_ok=True)
        bare_repo_dir.mkdir(parents=True, exist_ok=True)
        worktree_dir.mkdir(parents=True, exist_ok=True)
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        temp_dir.mkdir(parents=True, exist_ok=True)

        return run_dir, bare_repo_dir, worktree_dir

    def finalize(self, run_id: str, baseline_commit: str) -> Path:
        """Checkout baseline_commit into workspace and make read-only in PRD 1."""
        run_dir = self._safe_run_dir(run_id)
        bare_repo_dir = run_dir / "repo.git"
        worktree_dir = run_dir / "workspace"

        worktree_dir.mkdir(parents=True, exist_ok=True)

        # Checkout baseline_commit into workspace
        self.git.run(
            [
                f"--git-dir={str(bare_repo_dir)}",
                f"--work-tree={str(worktree_dir)}",
                "checkout",
                "-f",
                baseline_commit,
            ]
        )

        # Make worktree read-only for PRD 1 (section 10.5 & 14.3)
        self.make_tree_readonly(worktree_dir)
        return worktree_dir

    def make_tree_readonly(self, path: Path) -> None:
        """Remove write permissions recursively."""
        for root, dirs, files in os.walk(path):
            for f in files:
                fpath = Path(root) / f
                if not fpath.is_symlink():
                    try:
                        current = fpath.stat().st_mode
                        os.chmod(fpath, current & ~0o222)
                    except OSError:
                        pass
            for d in dirs:
                dpath = Path(root) / d
                if not dpath.is_symlink():
                    try:
                        current = dpath.stat().st_mode
                        os.chmod(dpath, current & ~0o222)
                    except OSError:
                        pass
        try:
            current = path.stat().st_mode
            os.chmod(path, current & ~0o222)
        except OSError:
            pass

    def make_tree_writable(self, path: Path) -> None:
        """Restore write permissions recursively (used during cleanup/removal)."""
        if not path.exists():
            return
        for root, dirs, files in os.walk(path):
            for f in files:
                fpath = Path(root) / f
                if not fpath.is_symlink():
                    try:
                        os.chmod(fpath, stat.S_IRUSR | stat.S_IWUSR)
                    except OSError:
                        pass
            for d in dirs:
                dpath = Path(root) / d
                if not dpath.is_symlink():
                    try:
                        os.chmod(dpath, stat.S_IRWXU)
                    except OSError:
                        pass
        try:
            os.chmod(path, stat.S_IRWXU)
        except OSError:
            pass

    def destroy_partial(self, run_id: str) -> None:
        run_dir = self._safe_run_dir(run_id)
        if run_dir.exists():
            self.make_tree_writable(run_dir)
            shutil.rmtree(run_dir, ignore_errors=True)

    def quarantine(self, run_id: str) -> Optional[Path]:
        run_dir = self._safe_run_dir(run_id)
        if not run_dir.exists():
            return None

        quarantine_dir = self.data_root / "quarantine"
        quarantine_dir.mkdir(parents=True, exist_ok=True)

        now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = quarantine_dir / f"{run_id}-{now_str}"

        self.make_tree_writable(run_dir)
        try:
            shutil.move(str(run_dir), str(dest))
            return dest
        except Exception:
            return None
