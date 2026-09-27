"""Streaming, bounded, validating ZIP extraction into private staging (FR05 / PRD 1 AT03).

Rejected before anything is written outside the private staging directory:
absolute/drive/traversal paths, NUL/control characters, duplicate normalized
names (NFC + case-fold), symlinks, special files, embedded ``.git`` metadata,
encrypted entries, and file-count / per-file / total / expansion-ratio limits
(enforced while streaming, not trusted from headers). The original ZIP is only
read. A single common top-level directory (``repo-main/``) is stripped.
"""
from __future__ import annotations

import hashlib
import shutil
import stat
import unicodedata
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List

from harness.contracts import LimitsV1

MAX_ENTRIES = 50_000
MAX_TOTAL_BYTES = 500 * 1024 * 1024
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_RATIO = 100


class ZipPolicyError(ValueError):
    code = "ZIP_POLICY_VIOLATION"


def _normalize(name: str) -> str:
    if "\x00" in name or any(ord(ch) < 32 for ch in name):
        raise ZipPolicyError(f"Control character in ZIP entry name: {name!r}")
    value = name.replace("\\", "/")
    if value.startswith("/") or (len(value) > 1 and value[1] == ":"):
        raise ZipPolicyError(f"Absolute path in ZIP: {name!r}")
    parts = [part for part in value.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ZipPolicyError(f"Path traversal in ZIP: {name!r}")
    if any(part.lower() == ".git" for part in parts):
        raise ZipPolicyError(f"Embedded Git metadata in ZIP: {name!r}")
    return "/".join(parts)


def extract_zip(archive: Path, target: Path, limits: LimitsV1) -> Dict[str, Any]:
    archive = Path(archive)
    if not archive.is_file() or archive.is_symlink():
        raise ZipPolicyError(f"ZIP input is not a regular file: {archive}")
    if not zipfile.is_zipfile(archive):
        raise ZipPolicyError(f"Not a ZIP archive: {archive}")
    zip_sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    max_total = min(MAX_TOTAL_BYTES, limits.max_repo_bytes)
    max_entries = min(MAX_ENTRIES, limits.max_file_count)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    root = target.resolve()
    try:
        with zipfile.ZipFile(archive) as bundle:
            infos = bundle.infolist()
            if len(infos) > max_entries:
                raise ZipPolicyError(f"ZIP has {len(infos)} entries (limit {max_entries})")
            files: List[tuple] = []
            seen: Dict[str, str] = {}
            for info in infos:
                name = _normalize(info.filename)
                if not name:
                    continue
                mode = (info.external_attr >> 16) & 0xFFFF
                if info.flag_bits & 0x1:
                    raise ZipPolicyError(f"Encrypted ZIP entry: {info.filename!r}")
                if stat.S_ISLNK(mode):
                    raise ZipPolicyError(f"Symlink in ZIP: {info.filename!r}")
                is_dir = info.is_dir()
                # Many writers record permission bits only (no file-type bits): that is a regular file.
                if mode & 0o170000 and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)) and not is_dir:
                    raise ZipPolicyError(f"Special file in ZIP: {info.filename!r}")
                key = unicodedata.normalize("NFC", name).casefold()
                if key in seen and not is_dir:
                    raise ZipPolicyError(f"Duplicate normalized ZIP path: {name!r} vs {seen[key]!r}")
                seen[key] = name
                if not is_dir:
                    files.append((info, name, bool(mode & 0o111)))
            # Strip one common top-level directory (e.g. GitHub "repo-main/").
            firsts = {PurePosixPath(name).parts[0] for _, name, _ in files}
            strip = next(iter(firsts)) if len(firsts) == 1 and all(len(PurePosixPath(n).parts) > 1 for _, n, _ in files) else None
            total = 0
            compressed = max(1, archive.stat().st_size)
            for info, name, executable in files:
                relative = "/".join(PurePosixPath(name).parts[1:]) if strip else name
                destination = (root / relative).resolve()
                if root not in destination.parents:
                    raise ZipPolicyError(f"ZIP entry escapes staging: {name!r}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                with bundle.open(info) as source, open(destination, "xb") as sink:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        total += len(chunk)
                        if written > MAX_FILE_BYTES:
                            raise ZipPolicyError(f"ZIP entry exceeds {MAX_FILE_BYTES} bytes: {name!r}")
                        if total > max_total:
                            raise ZipPolicyError(f"ZIP expands beyond {max_total} bytes")
                        if total > MAX_RATIO * compressed and total > 10 * 1024 * 1024:
                            raise ZipPolicyError("ZIP expansion ratio exceeds 100:1")
                        sink.write(chunk)
                if executable:
                    destination.chmod(0o755)
    except (ZipPolicyError, zipfile.BadZipFile, OSError) as exc:
        shutil.rmtree(target, ignore_errors=True)  # remove only the failed private import
        if isinstance(exc, ZipPolicyError):
            raise
        raise ZipPolicyError(f"ZIP could not be extracted safely: {exc}") from exc
    return {"root": root, "zip_sha256": zip_sha, "entries": len(files), "stripped_root": strip}
