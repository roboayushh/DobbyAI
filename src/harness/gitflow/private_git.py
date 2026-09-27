"""Narrow, hardened access to a run's private Git repository.

Every invocation:

* uses an explicit ``--git-dir`` and an argument array (never a shell);
* runs with an environment constructed from scratch (no inherited ``GIT_*``,
  ``HOME``, SSH, or credential variables);
* disables hooks, credential helpers, external diff/textconv, filters, LFS,
  fsmonitor, automatic GC, and every network transport;
* bounds stdout, stderr, and wall time.

Trees are built byte-exactly with ``hash-object --no-filters`` and
``update-index --index-info``; trees are materialized with ``cat-file --batch``.
Neither path consults ``.gitattributes``, so repository content cannot change
bytes on the way in or out of the object database.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from harness.repository.git_runner import GitCommandError, redact_secrets

HARDENING_CONFIG: Tuple[str, ...] = (
    "core.hooksPath=/dev/null",
    "core.autocrlf=false",
    "core.safecrlf=false",
    "core.fsmonitor=false",
    "core.untrackedCache=false",
    "core.attributesFile=/dev/null",
    "core.quotePath=false",
    "core.pager=cat",
    "core.symlinks=true",
    "credential.helper=",
    "diff.external=",
    "diff.noprefix=false",
    "diff.renames=false",
    "color.ui=false",
    "gc.auto=0",
    "maintenance.auto=false",
    "protocol.allow=never",
    "submodule.recurse=false",
    "filter.lfs.smudge=",
    "filter.lfs.clean=",
    "filter.lfs.process=",
    "filter.lfs.required=false",
    "advice.detachedHead=false",
    "commit.gpgSign=false",
    "tag.gpgSign=false",
)

HARNESS_IDENTITY = ("AI Harness", "harness@invalid")
ZERO_OIDS = {"sha1": "0" * 40, "sha256": "0" * 64}
_OID_RE = {"sha1": re.compile(r"^[0-9a-f]{40}$"), "sha256": re.compile(r"^[0-9a-f]{64}$")}
_REF_PREFIX = "refs/harness/"


class RefCASError(RuntimeError):
    code = "GIT_REF_CAS_FAILED"

    def __init__(self, ref: str, expected: Optional[str], observed: Optional[str]) -> None:
        super().__init__(f"Ref {ref} expected {expected or '<absent>'} but observed {observed or '<absent>'}")
        self.ref = ref
        self.expected = expected
        self.observed = observed


class UnsafeTreeError(RuntimeError):
    code = "UNSAFE_TREE_ENTRY"


@dataclass(frozen=True)
class TreeEntry:
    mode: str
    object_type: str
    oid: str
    path: str


def git_blob_oid(content: bytes, object_format: str = "sha1") -> str:
    """Return the Git blob object ID for ``content`` without touching the repo."""
    header = f"blob {len(content)}\0".encode("ascii")
    return hashlib.new(object_format, header + content).hexdigest()


def validate_tree_path(path: str) -> str:
    """Reject any tree path that could escape or shadow repository metadata."""
    if not path or any(ch in path for ch in ("\x00", "\n", "\r")) or path.startswith("/") or "\\" in path:
        raise UnsafeTreeError(f"Unsafe tree path: {path!r}")
    parts = PurePosixPath(path).parts
    for part in parts:
        if part in ("..", ".") or part.lower() == ".git":
            raise UnsafeTreeError(f"Unsafe tree path component in {path!r}")
    return path


def validate_ref_name(ref: str) -> str:
    if not ref.startswith(_REF_PREFIX):
        raise ValueError(f"Harness may only manage refs under {_REF_PREFIX}: {ref}")
    if not re.fullmatch(r"refs/harness/[A-Za-z0-9_./-]{1,400}", ref):
        raise ValueError(f"Invalid managed ref name: {ref}")
    if ".." in ref or "//" in ref or ref.endswith((".", "/", ".lock")) or "@{" in ref:
        raise ValueError(f"Invalid managed ref name: {ref}")
    return ref


def safe_ref_component(value: str) -> str:
    """Internal identifiers become ref components; raw issue text never does."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
        raise ValueError(f"Identifier is not a safe ref component: {value!r}")
    return value


class PrivateGit:
    def __init__(
        self,
        git_dir: str | Path,
        *,
        timeout_seconds: int = 300,
        max_output_bytes: int = 256 * 1024 * 1024,
    ) -> None:
        self.git_dir = Path(git_dir).resolve()
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes
        self._object_format: Optional[str] = None
        if not (self.git_dir / "objects").is_dir():
            raise FileNotFoundError(f"Private Git repository not found: {self.git_dir}")

    # ------------------------------------------------------------------ core
    def _env(self, extra: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin"),
            "LC_ALL": "C",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_PROTOCOL_FROM_USER": "0",
        }
        if extra:
            env.update(extra)
        return env

    def run(
        self,
        args: Sequence[str],
        *,
        input_bytes: Optional[bytes] = None,
        extra_env: Optional[Mapping[str, str]] = None,
        check: bool = True,
        max_output_bytes: Optional[int] = None,
    ) -> Tuple[int, bytes, bytes]:
        cmd: List[str] = ["git"]
        for item in HARDENING_CONFIG:
            cmd.extend(["-c", item])
        cmd.append(f"--git-dir={self.git_dir}")
        cmd.extend(args)
        limit = max_output_bytes or self.max_output_bytes
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._env(extra_env),
                cwd=str(self.git_dir),
            )
        except OSError as exc:
            raise GitCommandError(f"Failed to execute git: {exc}", -1, "", str(exc)) from exc

        out_chunks: List[bytes] = []
        err_chunks: List[bytes] = []
        overflow = {"stdout": False, "stderr": False}

        def _drain(stream, sink: List[bytes], key: str, cap: int) -> None:
            size = 0
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > cap:
                    overflow[key] = True
                    continue
                sink.append(chunk)

        readers = [
            threading.Thread(target=_drain, args=(proc.stdout, out_chunks, "stdout", limit), daemon=True),
            threading.Thread(target=_drain, args=(proc.stderr, err_chunks, "stderr", 1024 * 1024), daemon=True),
        ]
        for reader in readers:
            reader.start()
        try:
            if input_bytes is not None and proc.stdin is not None:
                try:
                    proc.stdin.write(input_bytes)
                finally:
                    proc.stdin.close()
            proc.wait(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise GitCommandError(f"Git command timed out after {self.timeout_seconds}s", -1, "", "")
        finally:
            for reader in readers:
                reader.join(timeout=5)
        stdout = b"".join(out_chunks)
        stderr = b"".join(err_chunks)
        if overflow["stdout"]:
            raise GitCommandError("Git output exceeded the configured bound", -1, "", "")
        if check and proc.returncode != 0:
            raise GitCommandError(
                f"Git command failed: {' '.join(args[:2])}",
                proc.returncode,
                redact_secrets(stdout[:4000].decode("utf-8", "replace")),
                redact_secrets(stderr[:4000].decode("utf-8", "replace")),
            )
        return proc.returncode, stdout, stderr

    def _text(self, args: Sequence[str], **kwargs) -> str:
        return self.run(args, **kwargs)[1].decode("utf-8", "replace").strip()

    # --------------------------------------------------------------- objects
    @property
    def object_format(self) -> str:
        if self._object_format is None:
            value = self._text(["rev-parse", "--show-object-format"]) or "sha1"
            if value not in ZERO_OIDS:
                raise GitCommandError(f"Unsupported object format {value}", -1, "", "")
            self._object_format = value
        return self._object_format

    @property
    def zero_oid(self) -> str:
        return ZERO_OIDS[self.object_format]

    def is_oid(self, value: Optional[str]) -> bool:
        return bool(value) and bool(_OID_RE[self.object_format].fullmatch(value or ""))

    def rev_parse(self, rev: str) -> Optional[str]:
        code, out, _ = self.run(["rev-parse", "--verify", "--quiet", "--end-of-options", rev], check=False)
        value = out.decode().strip()
        return value if code == 0 and self.is_oid(value) else None

    def commit_tree_of(self, commit: str) -> str:
        tree = self.rev_parse(f"{commit}^{{tree}}")
        if not tree:
            raise GitCommandError(f"Commit has no tree: {commit}", -1, "", "")
        return tree

    def commit_parents(self, commit: str) -> List[str]:
        out = self._text(["rev-list", "--parents", "-n", "1", "--end-of-options", commit])
        parts = out.split()
        return parts[1:] if parts else []

    def object_exists(self, oid: str) -> bool:
        code, _, _ = self.run(["cat-file", "-e", oid], check=False)
        return code == 0

    def hash_files(self, paths: Sequence[Path]) -> List[str]:
        """Write blobs for ``paths`` byte-exactly and return their object IDs."""
        if not paths:
            return []
        oids: List[str] = []
        for start in range(0, len(paths), 500):
            batch = paths[start : start + 500]
            data = "".join(f"{str(path)}\n" for path in batch).encode("utf-8")
            out = self.run(
                ["hash-object", "-w", "--no-filters", "--stdin-paths"], input_bytes=data
            )[1].decode().split()
            if len(out) != len(batch):
                raise GitCommandError("hash-object returned an unexpected object count", -1, "", "")
            oids.extend(out)
        return oids

    def hash_bytes(self, content: bytes) -> str:
        return self.run(["hash-object", "-w", "--no-filters", "--stdin"], input_bytes=content)[1].decode().strip()

    def ls_tree(self, tree: str) -> List[TreeEntry]:
        out = self.run(["ls-tree", "-r", "-z", "--full-tree", "--end-of-options", tree])[1]
        entries: List[TreeEntry] = []
        for record in out.split(b"\x00"):
            if not record:
                continue
            meta, _, raw_path = record.partition(b"\t")
            mode, object_type, oid = meta.decode().split()
            entries.append(TreeEntry(mode, object_type, oid, raw_path.decode("utf-8", "surrogateescape")))
        return entries

    def cat_blobs(self, oids: Iterable[str]) -> Dict[str, bytes]:
        wanted = list(dict.fromkeys(oids))
        result: Dict[str, bytes] = {}
        for start in range(0, len(wanted), 200):
            batch = wanted[start : start + 200]
            out = self.run(
                ["cat-file", "--batch"], input_bytes="".join(f"{oid}\n" for oid in batch).encode()
            )[1]
            offset = 0
            for oid in batch:
                newline = out.index(b"\n", offset)
                header = out[offset:newline].decode().split()
                offset = newline + 1
                if len(header) == 2 and header[1] == "missing":
                    raise GitCommandError(f"Missing object {oid}", -1, "", "")
                size = int(header[2])
                result[oid] = out[offset : offset + size]
                offset += size + 1
        return result

    def write_tree(self, entries: Sequence[Tuple[str, str, str]]) -> str:
        """Build a tree from ``(mode, blob_oid, path)`` without a working tree."""
        index_dir = self.git_dir / "harness-index"
        index_dir.mkdir(exist_ok=True)
        index_file = index_dir / f"idx-{uuid.uuid4().hex}"
        env = {"GIT_INDEX_FILE": str(index_file)}
        try:
            lines = []
            for mode, oid, path in sorted(entries, key=lambda item: item[2]):
                validate_tree_path(path)
                if "\n" in path:
                    raise UnsafeTreeError(f"Newline in tree path: {path!r}")
                lines.append(f"{mode} {oid}\t{path}\n")
            if lines:
                self.run(["update-index", "--index-info"], input_bytes="".join(lines).encode("utf-8"), extra_env=env)
            else:
                self.run(["read-tree", "--empty"], extra_env=env)
            return self._text(["write-tree"], extra_env=env)
        finally:
            try:
                index_file.unlink()
            except FileNotFoundError:
                pass

    def commit_tree(self, tree: str, parents: Sequence[str], message: str) -> str:
        args = ["commit-tree", tree]
        for parent in parents:
            args.extend(["-p", parent])
        name, email = HARNESS_IDENTITY
        env = {
            "GIT_AUTHOR_NAME": name,
            "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": name,
            "GIT_COMMITTER_EMAIL": email,
        }
        return self.run(args, input_bytes=message.encode("utf-8"), extra_env=env)[1].decode().strip()

    def commit_message(self, commit: str) -> str:
        return self.run(["log", "-1", "--format=%B", "--end-of-options", commit])[1].decode("utf-8", "replace")

    # ------------------------------------------------------------------ refs
    def read_ref(self, ref: str) -> Optional[str]:
        validate_ref_name(ref)
        return self.rev_parse(ref)

    def update_ref_cas(self, ref: str, new: str, expected_old: Optional[str]) -> None:
        """Atomic compare-and-swap. ``expected_old=None`` means the ref must not exist."""
        validate_ref_name(ref)
        if not self.is_oid(new):
            raise ValueError(f"Invalid desired OID for {ref}")
        old = expected_old if expected_old is not None else self.zero_oid
        code, _, _ = self.run(["update-ref", "--no-deref", ref, new, old], check=False)
        if code != 0:
            raise RefCASError(ref, expected_old, self.read_ref(ref))

    def list_refs(self, prefix: str) -> Dict[str, str]:
        validate_ref_name(prefix.rstrip("/") + "/x")
        out = self._text(["for-each-ref", "--format=%(refname) %(objectname)", prefix])
        refs: Dict[str, str] = {}
        for line in out.splitlines():
            name, _, oid = line.partition(" ")
            refs[name] = oid
        return refs

    # ------------------------------------------------------------------ diff
    def diff(self, base: str, head: str, *, max_bytes: int = 32 * 1024 * 1024) -> bytes:
        return self.run(
            [
                "diff-tree", "-p", "--binary", "--full-index", "--no-color",
                "--no-ext-diff", "--no-textconv", "--no-renames", "-r",
                "--end-of-options", base, head,
            ],
            max_output_bytes=max_bytes,
        )[1]

    def diff_paths(self, base: str, head: str) -> List[Tuple[str, str]]:
        out = self.run(
            ["diff-tree", "-r", "-z", "--name-status", "--no-renames", "--end-of-options", base, head]
        )[1]
        parts = [part for part in out.split(b"\x00") if part]
        pairs: List[Tuple[str, str]] = []
        for index in range(0, len(parts) - 1, 2):
            pairs.append((parts[index].decode(), parts[index + 1].decode("utf-8", "surrogateescape")))
        return pairs

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        code, _, _ = self.run(["merge-base", "--is-ancestor", ancestor, descendant], check=False)
        return code == 0

    # ----------------------------------------------------------- materialize
    def materialize(self, tree: str, destination: Path) -> List[str]:
        """Write the exact blobs of ``tree`` into an empty ``destination``.

        Returns skipped gitlink (submodule) paths, which are never fetched.
        """
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        if any(destination.iterdir()):
            raise UnsafeTreeError(f"Materialization target is not empty: {destination}")
        root = destination.resolve()
        entries = self.ls_tree(tree)
        skipped: List[str] = []
        blobs = [entry for entry in entries if entry.object_type == "blob"]
        contents: Dict[str, bytes] = {}
        for start in range(0, len(blobs), 500):
            contents.update(self.cat_blobs(entry.oid for entry in blobs[start : start + 500]))
        for entry in entries:
            validate_tree_path(entry.path)
            if entry.object_type == "commit":
                skipped.append(entry.path)
                continue
            if entry.object_type != "blob":
                continue
            target = root / entry.path
            _ensure_real_parent(root, target.parent)
            data = contents[entry.oid]
            if entry.mode == "120000":
                link_target = data.decode("utf-8", "surrogateescape")
                resolved = (target.parent / link_target).resolve()
                try:
                    resolved.relative_to(root)
                except ValueError as exc:
                    raise UnsafeTreeError(f"Symlink escapes workspace: {entry.path}") from exc
                os.symlink(link_target, target)
                continue
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(target, flags, 0o755 if entry.mode == "100755" else 0o644)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
            except BaseException:
                raise
            os.chmod(target, 0o755 if entry.mode == "100755" else 0o644)
        return skipped


def _ensure_real_parent(root: Path, directory: Path) -> None:
    relative = directory.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise UnsafeTreeError(f"Path traverses a symlink: {current}")
        if not current.exists():
            current.mkdir(mode=0o755)
        elif not current.is_dir():
            raise UnsafeTreeError(f"Path component is not a directory: {current}")


def make_writable_tree(path: Path) -> None:
    """Restore owner write permission (PRD 1 checkouts are read-only)."""
    if not path.exists():
        return
    for current_root, dirs, files in os.walk(path, followlinks=False):
        for name in dirs:
            item = Path(current_root) / name
            if not item.is_symlink():
                os.chmod(item, (item.stat().st_mode | stat.S_IRWXU) & 0o7777)
        for name in files:
            item = Path(current_root) / name
            if not item.is_symlink():
                os.chmod(item, (item.lstat().st_mode | stat.S_IRUSR | stat.S_IWUSR) & 0o7777)
    os.chmod(path, (path.stat().st_mode | stat.S_IRWXU) & 0o7777)


def secure_rmtree(path: Path, allowed_root: Path) -> None:
    """Remove ``path`` only when it is strictly inside ``allowed_root``."""
    import shutil

    resolved = Path(path).resolve()
    root = Path(allowed_root).resolve()
    if resolved == root:
        raise ValueError("Refusing to remove the registered root itself")
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Refusing to remove path outside registered root: {path}") from exc
    if not resolved.exists():
        return
    make_writable_tree(resolved)
    shutil.rmtree(resolved)


def new_temp_dir(parent: Path, prefix: str) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=str(parent)))
