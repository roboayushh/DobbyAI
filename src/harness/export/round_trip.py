"""Patch round-trip gate (PRD 6 section 8.3): fresh B + patch must equal exact C."""
from __future__ import annotations

import datetime
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from harness.export.patch_adapter import PatchAdapter, PatchRejected
from harness.gitflow.private_git import PrivateGit, make_writable_tree, secure_rmtree
from harness.release.identity import commit_content_sha256
from harness.workspace.manifest import diff_manifests, scan_workspace


@dataclass
class RoundTripResult:
    status: str  # PASS | FAIL
    expected_tree: str
    observed_tree: Optional[str]
    expected_content_sha256: str
    observed_content_sha256: Optional[str]
    changed_path_set_sha256: str
    environment_sha256: str
    reasons: List[str] = field(default_factory=list)
    started_at: str = ""
    settled_at: str = ""

    def report(self) -> Dict[str, Any]:
        return {
            "schema_version": "1.0",
            "status": self.status,
            "expected_tree": self.expected_tree,
            "observed_tree": self.observed_tree,
            "expected_content_sha256": self.expected_content_sha256,
            "observed_content_sha256": self.observed_content_sha256,
            "changed_path_set_sha256": self.changed_path_set_sha256,
            "environment_sha256": self.environment_sha256,
            "reasons": self.reasons,
            "adapter": "unified_git_patch_v1 via git apply (no fuzz, repository discovery disabled)",
            "started_at": self.started_at,
            "settled_at": self.settled_at,
        }


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def round_trip(git: PrivateGit, base: str, head: str, patch: bytes, work_root: Path,
               adapter: Optional[PatchAdapter] = None) -> RoundTripResult:
    adapter = adapter or PatchAdapter()
    started = _now()
    expected_tree = git.commit_tree_of(head)
    expected_content = commit_content_sha256(git, head)
    expected_paths = sorted(path for _, path in git.diff_paths(base, head))
    path_set_sha = hashlib.sha256(json.dumps(expected_paths).encode()).hexdigest()
    environment_sha = hashlib.sha256(json.dumps({
        "adapter": adapter.format, "base_tree": git.commit_tree_of(base), "object_format": git.object_format,
    }, sort_keys=True).encode()).hexdigest()
    result = RoundTripResult("FAIL", expected_tree, None, expected_content, None, path_set_sha, environment_sha, started_at=started)
    work = Path(work_root) / f"rt-{uuid.uuid4().hex[:12]}"
    workspace = work / "ws"
    work.mkdir(parents=True, exist_ok=False)
    try:
        git.materialize(git.commit_tree_of(base), workspace)
        make_writable_tree(workspace)
        before = scan_workspace(workspace, object_format=git.object_format)
        try:
            adapter.apply(patch, workspace)
        except PatchRejected as exc:
            result.reasons.append(str(exc)[:500])
            return result
        after = scan_workspace(workspace, object_format=git.object_format)
        result.observed_content_sha256 = after.content_tree_sha256()
        try:
            result.observed_tree = git.write_tree(after.tree_entries())
        except Exception as exc:  # missing objects mean the bytes differ from C
            result.reasons.append(f"TREE_NOT_REPRODUCIBLE: {type(exc).__name__}")
        changed_set = set()
        for change in diff_manifests(before, after).changes:
            changed_set.add(change.path)
            if change.old_path:
                changed_set.add(change.old_path)
        changed = sorted(changed_set)
        if changed != expected_paths:
            result.reasons.append("CHANGED_PATH_SET_DIFFERS")
        if result.observed_content_sha256 != expected_content:
            result.reasons.append("CONTENT_DIFFERS")
        if result.observed_tree != expected_tree:
            result.reasons.append("TREE_DIFFERS")
        if not result.reasons:
            result.status = "PASS"
        return result
    finally:
        result.settled_at = _now()
        try:
            make_writable_tree(work)
            secure_rmtree(work, Path(work_root))
        except Exception:
            pass
