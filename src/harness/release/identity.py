"""Exact identities used by export, results, and reproducibility manifests."""
from __future__ import annotations

import hashlib
import subprocess
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict

from harness.config import HARNESS_ROOT
from harness.gitflow.private_git import PrivateGit
from harness.persistence import canonical_json


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def commit_content_sha256(git: PrivateGit, commit: str) -> str:
    """Content-tree hash of ``commit`` in the PRD 1 manifest format (mode sha256 path)."""
    entries = [entry for entry in git.ls_tree(git.commit_tree_of(commit)) if entry.object_type == "blob"]
    blobs = git.cat_blobs(entry.oid for entry in entries)
    lines = [f"{entry.mode} {hashlib.sha256(blobs[entry.oid]).hexdigest()} {entry.path}" for entry in sorted(entries, key=lambda e: e.path)]
    return hashlib.sha256(("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def harness_build() -> Dict[str, str]:
    """Harness version, source commit (``-dirty`` when the harness tree has local edits), and build ID."""
    try:
        version = tomllib.loads((HARNESS_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        version = "unknown"
    commit = "unknown"
    dirty = False
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=HARNESS_ROOT, capture_output=True, text=True, timeout=10).stdout.strip() or "unknown"
        status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no", "--", "src", "config", "runtime"],
                                cwd=HARNESS_ROOT, capture_output=True, text=True, timeout=10).stdout
        dirty = bool(status.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    source = hashlib.sha256()
    for path in sorted((HARNESS_ROOT / "src" / "harness").rglob("*.py")):
        source.update(path.relative_to(HARNESS_ROOT).as_posix().encode() + b"\0" + path.read_bytes())
    return {
        "version": version,
        "source_commit": commit + ("-dirty" if dirty else ""),
        "build_id": f"build_{source.hexdigest()[:20]}",
        "source_tree_sha256": source.hexdigest(),
    }


def path_hash(path: Path) -> str:
    return hashlib.sha256(str(Path(path).resolve()).encode("utf-8")).hexdigest()
