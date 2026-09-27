"""``unified_git_patch_v1``: deterministic, bounded B -> C patch creation and application.

Creation uses the hardened private Git (no external diff, no textconv, no color,
no renames, full blob IDs, binary patches). Application uses ``git apply`` as a
plain patch tool in a directory with Git repository discovery disabled, so no
enclosing repository, hook, attribute, or filter can influence the bytes. There
is no fuzz: context must match exactly or the whole patch is rejected.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Sequence, Tuple

from harness.gitflow.private_git import PrivateGit, validate_tree_path

FORMAT = "unified_git_patch_v1"
MAX_PATCH_BYTES = 64 * 1024 * 1024


class PatchRejected(ValueError):
    code = "EXPORT_INVALID"


@dataclass
class PatchInspection:
    paths: List[str]
    created: List[str] = field(default_factory=list)
    deleted: List[str] = field(default_factory=list)
    modified: List[str] = field(default_factory=list)
    mode_changed: List[str] = field(default_factory=list)
    binary: List[str] = field(default_factory=list)
    bytes: int = 0


class PatchAdapter:
    format = FORMAT

    def create(self, git: PrivateGit, base: str, head: str, *, max_bytes: int = MAX_PATCH_BYTES) -> Tuple[bytes, List[Tuple[str, str]]]:
        changes = git.diff_paths(base, head)
        for _, path in changes:
            validate_tree_path(path)
        patch = git.diff(base, head, max_bytes=max_bytes)
        if len(patch) > max_bytes:
            raise PatchRejected(f"Patch exceeds {max_bytes} bytes")
        return patch, changes

    def inspect(self, patch: bytes, expected: Sequence[Tuple[str, str]]) -> PatchInspection:
        """Require the patch to touch exactly the expected paths, in canonical form."""
        if b"\x00" in patch:
            raise PatchRejected("Patch contains NUL bytes")
        text = patch.decode("utf-8", "surrogateescape")
        headers = re.findall(r"^diff --git (.*)$", text, flags=re.MULTILINE)
        wanted = [path for _, path in expected]
        if len(headers) != len(wanted):
            raise PatchRejected(f"Patch has {len(headers)} file sections; expected {len(wanted)}")
        inspection = PatchInspection(paths=list(wanted), bytes=len(patch))
        for header, (status, path) in zip(headers, expected):
            if header.startswith('"'):
                raise PatchRejected(f"Ambiguously quoted path in patch header: {header[:120]}")
            if header != f"a/{path} b/{path}":
                raise PatchRejected(f"Unexpected patch header for {path!r}")
            if path.startswith("/") or ".." in path.split("/") or "\\" in path:
                raise PatchRejected(f"Unsafe patch path: {path!r}")
            {"A": inspection.created, "D": inspection.deleted, "M": inspection.modified, "T": inspection.mode_changed}.get(status, inspection.modified).append(path)
        if re.search(r"^(new|deleted) file mode 160000|^index [0-9a-f]+\.\.[0-9a-f]+ 160000", text, flags=re.MULTILINE):
            raise PatchRejected("Patch changes a submodule gitlink; submodule content is never exported")
        for section in re.split(r"^diff --git ", text, flags=re.MULTILINE)[1:]:
            if "\nGIT binary patch\n" in section:
                inspection.binary.append(section.split("\n", 1)[0].split(" b/", 1)[-1])
            if re.search(r"^old mode \d+\nnew mode \d+", section, flags=re.MULTILINE):
                name = section.split("\n", 1)[0].split(" b/", 1)[-1]
                if name not in inspection.mode_changed:
                    inspection.mode_changed.append(name)
        return inspection

    def apply(self, patch: bytes, workspace: Path, *, timeout: int = 300) -> None:
        """Apply atomically (no ``--reject``, no fuzz) into ``workspace``."""
        workspace = Path(workspace).resolve()
        if not patch.strip():
            return
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(workspace.parent),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CEILING_DIRECTORIES": str(workspace.parent),
            "GIT_ATTR_NOSYSTEM": "1",
            "LC_ALL": "C",
        }
        command = [
            "git", "-c", "core.hooksPath=/dev/null", "-c", "core.autocrlf=false", "-c", "core.attributesFile=/dev/null",
            "-c", "core.symlinks=true", "-c", "apply.whitespace=nowarn",
            "apply", "--binary", "--whitespace=nowarn", "--recount", "-",
        ]
        # --recount only re-derives hunk line counts; context still has to match exactly.
        command.remove("--recount")
        proc = subprocess.run(command, input=patch, cwd=str(workspace), env=env, capture_output=True, timeout=timeout)
        if proc.returncode != 0:
            raise PatchRejected("git apply rejected the patch: " + proc.stderr.decode("utf-8", "replace")[-1000:])
