"""Host-side workspace manifests and change sets.

The host never trusts helper-tool reports about what an action changed. After a
container stops, the host rescans the workspace and derives the change set from
two manifests. Every regular file is re-hashed on every scan; stat metadata is
never used as a cache key because a sandboxed process can forge mtimes.
"""
from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Set, Tuple

from harness.gitflow.private_git import git_blob_oid

# Paths the harness reserves inside a task workspace. A sandboxed action must
# never create repository metadata that the host Git service could later read.
RESERVED_COMPONENTS = frozenset({".git"})

# Caches produced by ordinary development commands. They are removed after an
# action and never enter a workspace version or candidate.
DISPOSABLE_DIR_NAMES = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".hypothesis",
        ".tox",
        ".nox",
    }
)
DISPOSABLE_SUFFIXES = (".pyc", ".pyo")
DISPOSABLE_FILE_NAMES = frozenset({".coverage"})


class ManifestLimitError(RuntimeError):
    code = "WORKSPACE_LIMIT_EXCEEDED"


class ManifestPolicyError(RuntimeError):
    code = "WORKSPACE_POLICY_VIOLATION"


@dataclass(frozen=True)
class ManifestEntry:
    path: str
    mode: str
    size: int
    sha256: str
    oid: str

    def as_dict(self) -> Dict[str, Any]:
        return {"path": self.path, "mode": self.mode, "size": self.size, "sha256": self.sha256, "oid": self.oid}


@dataclass
class WorkspaceManifest:
    entries: Dict[str, ManifestEntry] = field(default_factory=dict)
    directories: Set[str] = field(default_factory=set)
    special: List[Tuple[str, str]] = field(default_factory=list)
    reserved: List[str] = field(default_factory=list)
    unsafe_names: List[str] = field(default_factory=list)
    object_format: str = "sha1"

    @property
    def total_bytes(self) -> int:
        return sum(entry.size for entry in self.entries.values())

    @property
    def file_count(self) -> int:
        return len(self.entries)

    def content_tree_sha256(self) -> str:
        """Identical format to the PRD 1 source-manifest content-tree hash."""
        lines = [
            f"{self.entries[path].mode} {self.entries[path].sha256} {path}"
            for path in sorted(self.entries)
        ]
        text = "\n".join(lines) + ("\n" if lines else "")
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def core_view(self) -> Dict[str, Dict[str, Any]]:
        return {
            path: {"path": entry.path, "mode": entry.mode, "size": entry.size, "sha256": entry.sha256}
            for path, entry in self.entries.items()
        }

    def to_json(self) -> Dict[str, Any]:
        return {
            "schema_version": "1.0",
            "object_format": self.object_format,
            "content_tree_sha256": self.content_tree_sha256(),
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "entries": [self.entries[path].as_dict() for path in sorted(self.entries)],
            "directories": sorted(self.directories),
        }

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "WorkspaceManifest":
        manifest = cls(object_format=data.get("object_format", "sha1"))
        for item in data.get("entries", []):
            manifest.entries[item["path"]] = ManifestEntry(
                item["path"], item["mode"], int(item["size"]), item["sha256"], item["oid"]
            )
        manifest.directories = set(data.get("directories", []))
        return manifest

    def tree_entries(self) -> List[Tuple[str, str, str]]:
        return [(entry.mode, entry.oid, entry.path) for entry in self.entries.values()]


def is_disposable(path: str) -> bool:
    parts = PurePosixPath(path).parts
    if any(part in DISPOSABLE_DIR_NAMES for part in parts):
        return True
    name = parts[-1] if parts else ""
    return name in DISPOSABLE_FILE_NAMES or name.endswith(DISPOSABLE_SUFFIXES)


def scan_workspace(
    root: Path,
    *,
    object_format: str = "sha1",
    max_files: Optional[int] = None,
    max_bytes: Optional[int] = None,
    include_disposable: bool = True,
) -> WorkspaceManifest:
    root = Path(root)
    manifest = WorkspaceManifest(object_format=object_format)
    total = 0
    for current_root, dirs, files in os.walk(root, topdown=True, followlinks=False):
        current = Path(current_root)
        relative_root = current.relative_to(root)
        kept_dirs = []
        for name in sorted(dirs):
            relative = (relative_root / name).as_posix()
            full = current / name
            if full.is_symlink():
                files.append(name)  # classified below as a symlink entry
                continue
            if name.lower() in RESERVED_COMPONENTS:
                manifest.reserved.append(relative)
                continue
            if not include_disposable and is_disposable(relative):
                continue
            manifest.directories.add(relative)
            kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in sorted(set(files)):
            relative = (relative_root / name).as_posix()
            full = current / name
            if "\n" in relative or "\r" in relative or "\x00" in relative:
                manifest.unsafe_names.append(relative.replace("\n", "\\n").replace("\r", "\\r"))
                continue
            if name.lower() in RESERVED_COMPONENTS:
                manifest.reserved.append(relative)
                continue
            if not include_disposable and is_disposable(relative):
                continue
            info = full.lstat()
            if stat.S_ISLNK(info.st_mode):
                target = os.readlink(full).encode("utf-8", "surrogateescape")
                manifest.entries[relative] = ManifestEntry(
                    relative,
                    "120000",
                    len(target),
                    hashlib.sha256(target).hexdigest(),
                    git_blob_oid(target, object_format),
                )
                continue
            if not stat.S_ISREG(info.st_mode):
                kind = (
                    "fifo" if stat.S_ISFIFO(info.st_mode)
                    else "socket" if stat.S_ISSOCK(info.st_mode)
                    else "device"
                )
                manifest.special.append((relative, kind))
                continue
            data = _read_bounded(full, info.st_size)
            total += len(data)
            if max_bytes is not None and total > max_bytes:
                raise ManifestLimitError(f"Workspace exceeds {max_bytes} bytes")
            mode = "100755" if info.st_mode & 0o111 else "100644"
            manifest.entries[relative] = ManifestEntry(
                relative,
                mode,
                len(data),
                hashlib.sha256(data).hexdigest(),
                git_blob_oid(data, object_format),
            )
            if max_files is not None and len(manifest.entries) > max_files:
                raise ManifestLimitError(f"Workspace exceeds {max_files} files")
    return manifest


def _read_bounded(path: Path, expected: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        with os.fdopen(fd, "rb") as handle:
            return handle.read()
    except OSError as exc:
        raise ManifestPolicyError(f"Unable to read workspace file {path}: {exc}") from exc


@dataclass(frozen=True)
class FileChange:
    path: str
    change_type: str  # CREATED, MODIFIED, DELETED, RENAMED, MODE_CHANGED, SYMLINK_CHANGED, SPECIAL
    before: Optional[ManifestEntry]
    after: Optional[ManifestEntry]
    old_path: Optional[str] = None

    @property
    def byte_delta(self) -> int:
        return (self.after.size if self.after else 0) - (self.before.size if self.before else 0)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "change_type": self.change_type,
            "old_path": self.old_path,
            "before_sha256": self.before.sha256 if self.before else None,
            "after_sha256": self.after.sha256 if self.after else None,
            "before_mode": self.before.mode if self.before else None,
            "after_mode": self.after.mode if self.after else None,
            "byte_delta": self.byte_delta,
        }


@dataclass
class ChangeSet:
    changes: List[FileChange]
    created_directories: List[str]
    special: List[Tuple[str, str]]
    reserved: List[str]
    unsafe_names: List[str]

    @property
    def empty(self) -> bool:
        return not (self.changes or self.special or self.reserved or self.unsafe_names)

    def paths(self) -> List[str]:
        result: List[str] = []
        for change in self.changes:
            result.append(change.path)
            if change.old_path:
                result.append(change.old_path)
        return sorted(set(result))

    def sha256(self) -> str:
        lines = [
            f"{c.change_type} {c.old_path or '-'} {c.path} "
            f"{c.before.sha256 if c.before else '-'} {c.after.sha256 if c.after else '-'}"
            for c in sorted(self.changes, key=lambda item: (item.path, item.change_type))
        ]
        lines.extend(f"SPECIAL {path} {kind}" for path, kind in sorted(self.special))
        lines.extend(f"RESERVED {path}" for path in sorted(self.reserved))
        return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()

    def by_type(self, *types: str) -> List[str]:
        return sorted(change.path for change in self.changes if change.change_type in types)


def diff_manifests(before: WorkspaceManifest, after: WorkspaceManifest) -> ChangeSet:
    changes: List[FileChange] = []
    created = {path: after.entries[path] for path in after.entries.keys() - before.entries.keys()}
    deleted = {path: before.entries[path] for path in before.entries.keys() - after.entries.keys()}
    for path in sorted(before.entries.keys() & after.entries.keys()):
        old, new = before.entries[path], after.entries[path]
        if old.sha256 == new.sha256 and old.mode == new.mode:
            continue
        if "120000" in (old.mode, new.mode):
            changes.append(FileChange(path, "SYMLINK_CHANGED", old, new))
        elif old.sha256 == new.sha256:
            changes.append(FileChange(path, "MODE_CHANGED", old, new))
        else:
            changes.append(FileChange(path, "MODIFIED", old, new))
    # A deletion plus a creation with identical content is reported as a rename.
    deleted_by_hash: Dict[str, List[str]] = {}
    for path, entry in deleted.items():
        deleted_by_hash.setdefault(entry.sha256, []).append(path)
    for path in sorted(created):
        entry = created[path]
        candidates = deleted_by_hash.get(entry.sha256)
        if candidates and entry.mode != "120000":
            old_path = candidates.pop(0)
            changes.append(FileChange(path, "RENAMED", deleted.pop(old_path), entry, old_path=old_path))
        else:
            change_type = "SYMLINK_CHANGED" if entry.mode == "120000" else "CREATED"
            changes.append(FileChange(path, change_type, None, entry))
    for path in sorted(deleted):
        changes.append(FileChange(path, "DELETED", deleted[path], None))
    return ChangeSet(
        changes=sorted(changes, key=lambda item: (item.path, item.change_type)),
        created_directories=sorted(after.directories - before.directories),
        special=list(after.special),
        reserved=list(after.reserved),
        unsafe_names=list(after.unsafe_names),
    )
