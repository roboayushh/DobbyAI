"""harness/repository/repository_service.py
RepositoryService – resolves revisions, measures repositories, and delegates import.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from harness.contracts import LimitsV1, RepositoryKind, RepositoryRefV1, SourceIdentityV1
from harness.repository.git_runner import GitRunner
from harness.repository.source_importer import SourceImporter


class RepositoryService:
    def __init__(
        self,
        git_runner: Optional[GitRunner] = None,
        importer: Optional[SourceImporter] = None,
    ):
        self.git = git_runner or GitRunner()
        self.importer = importer or SourceImporter(self.git)

    def resolve(self, ref: RepositoryRefV1) -> str:
        """Resolve revision for repository ref."""
        target_revision = ref.revision or "HEAD"
        if ref.kind == RepositoryKind.LOCAL_GIT:
            out, _ = self.git.run(["rev-parse", target_revision], cwd=ref.locator)
            return out.strip()
        elif ref.kind == RepositoryKind.PUBLIC_HTTPS:
            # Remote ls-remote
            out, _ = self.git.run(["ls-remote", ref.locator, target_revision])
            lines = out.strip().splitlines()
            if lines:
                return lines[0].split()[0]
            # Try HEAD
            out, _ = self.git.run(["ls-remote", ref.locator, "HEAD"])
            lines = out.strip().splitlines()
            if lines:
                return lines[0].split()[0]
            raise ValueError(f"Could not resolve revision {target_revision} for {ref.locator}")
        else:
            raise ValueError(f"Unsupported repository kind: {ref.kind}")

    def acquire(
        self,
        ref: RepositoryRefV1,
        private_bare_repo: Path,
        staging_dir: Path,
        run_id: str,
        limits: LimitsV1,
    ) -> Tuple[SourceIdentityV1, Dict[str, Any], int, int]:
        """Acquire repository into private bare repository."""
        if ref.kind == RepositoryKind.LOCAL_GIT:
            return self.importer.import_local(
                source_path=ref.locator,
                private_bare_repo=private_bare_repo,
                run_id=run_id,
                limits=limits,
                revision=ref.revision,
            )
        elif ref.kind == RepositoryKind.PUBLIC_HTTPS:
            return self.importer.import_remote(
                clone_url=ref.locator,
                private_bare_repo=private_bare_repo,
                staging_dir=staging_dir,
                run_id=run_id,
                limits=limits,
                revision=ref.revision,
            )
        else:
            raise ValueError(f"Unknown repository kind: {ref.kind}")
