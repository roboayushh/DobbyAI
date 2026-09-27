"""Reviewed built-in tools available to generated Python actions.

This module runs *inside* the sandbox container. It is stdlib-only and must
never import host modules. Tool checks improve usability and feedback, but
they are not the security boundary: mounts, namespaces, cgroups, and the
host's post-action manifest scan are authoritative.
"""
from __future__ import annotations

import base64
import datetime
import fnmatch
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

TOOL_LIBRARY_VERSION = "1.0.0"
TOOL_VERSION = "1.0"

WORKSPACE = Path(os.environ.get("HARNESS_WORKSPACE_ROOT", "/workspace"))
CONTEXT = Path(os.environ.get("HARNESS_CONTEXT_ROOT", "/context"))
OUTPUT = Path(os.environ.get("HARNESS_OUTPUT_DIR", "/output"))

RESULT_STATUSES = ("ACTION_COMPLETED", "ACTION_NEEDS_FOLLOWUP", "ACTION_BLOCKED")
READ_DEFAULT_LINES = 400
READ_MAX_LINES = 2000
READ_MAX_BYTES = 1024 * 1024
PATCH_MAX_BYTES = 1024 * 1024
PATCH_MAX_FILES = 100
PATCH_MAX_HUNKS = 1000
RUN_EXCERPT_BYTES = 16 * 1024
RUN_LOG_BYTES = 1024 * 1024
DENIED_ENV_PATTERNS = (
    re.compile(r"^LD_"),
    re.compile(r"PROXY", re.I),
    re.compile(r"TOKEN|SECRET|PASSWORD|CREDENTIAL|API_?KEY", re.I),
    re.compile(r"^(AWS|GCP|AZURE|GITHUB|GIT|SSH|DOCKER|AI)_"),
    re.compile(r"^HARNESS_"),
)


def _is_workspace_root(path: Any) -> bool:
    """True for every spelling of the workspace root: "", ".", "./", "/workspace", "/workspace/." ..."""
    if not isinstance(path, str):
        return False
    value = path.replace("\\", "/").strip()
    root = str(WORKSPACE).rstrip("/")
    if value == root or value.startswith(root + "/"):
        value = value[len(root):]
    return not [part for part in value.split("/") if part not in ("", ".")]


class ToolError(Exception):
    """Raised by a tool; generated code may catch it and adapt."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _bounded_text(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else str(value)
    return text if len(text) <= limit else text[: limit - 14] + "…[truncated]"


class Toolbox:
    def __init__(self, request: Dict[str, Any]) -> None:
        self.request = request
        self.action_id = str(request.get("action_id", "act_local"))
        self.capabilities = set(request.get("capabilities", []))
        self.declared = [str(item) for item in request.get("declared_paths", [])]
        limits = request.get("limits", {})
        self.max_tool_calls = int(limits.get("tool_calls", 64))
        self.max_run_calls = int(limits.get("run_calls", 8))
        self.max_read_bytes = int(limits.get("read_bytes", 4 * 1024 * 1024))
        self.frame_bytes = int(limits.get("frame_bytes", 1024 * 1024))
        self.deadline = time.monotonic() + float(limits.get("wall_seconds", 120)) - 2.0
        self.sequence = 0
        self.run_calls = 0
        self.read_bytes = 0
        self.result_emitted = False
        self.integrity_failure: Optional[str] = None
        self._manifest: Optional[Dict[str, Any]] = None
        self._symbols: Optional[Dict[str, Any]] = None
        OUTPUT.mkdir(parents=True, exist_ok=True)
        self.events_path = OUTPUT / "tool-events.ndjson"

    # ------------------------------------------------------------ plumbing
    def exports(self) -> Dict[str, Any]:
        return {
            "search": self.search,
            "read_file": self.read_file,
            "symbols": self.symbols,
            "apply_patch": self.apply_patch,
            "run": self.run,
            "read_artifact": self.read_artifact,
            "emit_result": self.emit_result,
            "ToolError": ToolError,
        }

    def _begin(self, tool: str, capability: str, arguments: Dict[str, Any]) -> Tuple[int, str]:
        self.sequence += 1
        started = _utc()
        if self.sequence > self.max_tool_calls:
            self._event(tool, capability, "REJECTED", arguments, {"error": "TOOL_CALL_LIMIT"}, started)
            raise ToolError("TOOL_CALL_LIMIT", f"More than {self.max_tool_calls} tool calls in one action")
        if capability not in self.capabilities:
            self._event(tool, capability, "REJECTED", arguments, {"error": "CAPABILITY_NOT_ADMITTED"}, started)
            raise ToolError("CAPABILITY_NOT_ADMITTED", f"{tool} requires admitted capability {capability}")
        return self.sequence, started

    def _event(
        self,
        tool: str,
        capability: str,
        state: str,
        arguments: Dict[str, Any],
        result: Dict[str, Any],
        started: str,
    ) -> None:
        frame = {
            "schema_version": "1.0",
            "action_id": self.action_id,
            "tool_call_id": f"tcall_{self.sequence:04d}",
            "sequence": self.sequence,
            "tool": tool,
            "tool_version": TOOL_VERSION,
            "capability": capability,
            "state": state,
            "arguments_summary": arguments,
            "result_summary": result,
            "started_at": started,
            "finished_at": _utc(),
        }
        line = json.dumps(frame, sort_keys=True, ensure_ascii=False)
        if len(line.encode("utf-8")) > self.frame_bytes:
            frame["arguments_summary"] = {"truncated": True}
            frame["result_summary"] = {"truncated": True}
            frame["state"] = "TRUNCATED"
            line = json.dumps(frame, sort_keys=True)
        with open(self.events_path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def _remaining(self) -> float:
        return self.deadline - time.monotonic()

    # ---------------------------------------------------------- path policy
    def _resolve(self, path: str, *, must_exist: bool = True, for_write: bool = False) -> Tuple[str, Path]:
        if not isinstance(path, str) or not path or "\x00" in path or len(path) > 1024:
            raise ToolError("PATH_INVALID", f"Invalid path: {path!r}")
        normalized = path.replace("\\", "/")
        if normalized.startswith("/"):
            if normalized == str(WORKSPACE) or normalized.startswith(str(WORKSPACE) + "/"):
                normalized = normalized[len(str(WORKSPACE)) :].lstrip("/")
            else:
                raise ToolError("PATH_ABSOLUTE", f"Absolute paths outside /workspace are not allowed: {path}")
        if len(normalized) > 1 and normalized[1] == ":":
            raise ToolError("PATH_ABSOLUTE", f"Drive paths are not allowed: {path}")
        parts = [part for part in normalized.split("/") if part not in ("", ".")]
        if not parts:
            raise ToolError("PATH_EMPTY", "Path resolves to the workspace root")
        if any(part == ".." for part in parts):
            raise ToolError("PATH_TRAVERSAL", f"Traversal is not allowed: {path}")
        if any(part.lower() == ".git" for part in parts):
            raise ToolError("PATH_RESERVED", f"Reserved path: {path}")
        relative = "/".join(parts)
        full = WORKSPACE.joinpath(*parts)
        # Refuse to traverse any symlinked directory component.
        current = WORKSPACE
        for part in parts[:-1]:
            current = current / part
            if current.is_symlink():
                raise ToolError("PATH_SYMLINK", f"Path traverses a symlink: {relative}")
        if full.is_symlink():
            if for_write:
                raise ToolError("PATH_SYMLINK", f"Refusing to write through a symlink: {relative}")
            target = full.resolve()
            try:
                target.relative_to(WORKSPACE.resolve())
            except ValueError:
                raise ToolError("PATH_ESCAPES", f"Symlink escapes the workspace: {relative}")
        if must_exist and not full.exists():
            raise ToolError("FILE_NOT_FOUND", f"No such file: {relative}")
        return relative, full

    def _declared(self, relative: str) -> bool:
        for scope in self.declared:
            if scope in (".", "./"):
                return True
            if scope.endswith("/") and relative.startswith(scope):
                return True
            if relative == scope or relative.startswith(scope.rstrip("/") + "/"):
                return True
        return False

    # --------------------------------------------------------------- search
    def search(
        self,
        query: str,
        paths: Optional[Sequence[str]] = None,
        glob: Optional[str] = None,
        regex: bool = False,
        max_results: int = 50,
        context_lines: int = 0,
    ) -> Dict[str, Any]:
        args = {"query": _bounded_text(query, 200), "paths": list(paths or [])[:20], "glob": glob, "regex": bool(regex)}
        seq, started = self._begin("search", "source.search", args)
        if not isinstance(query, str) or not query or len(query) > 1000:
            self._event("search", "source.search", "FAILED", args, {"error": "QUERY_INVALID"}, started)
            raise ToolError("QUERY_INVALID", "query must be a non-empty string up to 1000 characters")
        max_results = max(1, min(int(max_results), 500))
        context_lines = max(0, min(int(context_lines), 5))
        roots: List[Path] = []
        if isinstance(paths, str):
            paths = [paths]
        for item in paths or []:
            if _is_workspace_root(item):
                roots.append(WORKSPACE)
                continue
            _, resolved = self._resolve(item)
            roots.append(resolved)
        if not roots:
            roots = [WORKSPACE]
        try:
            pattern = re.compile(query if regex else re.escape(query))
        except re.error as exc:
            self._event("search", "source.search", "FAILED", args, {"error": "REGEX_INVALID"}, started)
            raise ToolError("REGEX_INVALID", str(exc))
        matches: List[Dict[str, Any]] = []
        truncated = False
        total_bytes = 0
        for root in roots:
            for file_path in self._walk(root):
                relative = file_path.relative_to(WORKSPACE).as_posix()
                if glob and not fnmatch.fnmatch(relative, glob) and not fnmatch.fnmatch(file_path.name, glob):
                    continue
                try:
                    data = file_path.read_bytes()
                except OSError:
                    continue
                if len(data) > 2 * 1024 * 1024 or b"\x00" in data[:8192]:
                    continue
                text = data.decode("utf-8", "replace")
                lines = text.splitlines()
                file_hash = _sha256(data)
                for number, line in enumerate(lines, start=1):
                    found = pattern.search(line)
                    if not found:
                        continue
                    excerpt = line[:500]
                    item: Dict[str, Any] = {
                        "path": relative,
                        "line": number,
                        "column": found.start() + 1,
                        "text": excerpt,
                        "sha256": file_hash,
                    }
                    if context_lines:
                        low = max(0, number - 1 - context_lines)
                        item["context"] = [l[:500] for l in lines[low : number + context_lines]]
                    total_bytes += len(excerpt)
                    matches.append(item)
                    if len(matches) >= max_results or total_bytes > 256 * 1024:
                        truncated = True
                        break
                if truncated:
                    break
            if truncated:
                break
        result = {"matches": matches, "truncated": truncated, "count": len(matches)}
        self._event("search", "source.search", "TRUNCATED" if truncated else "SUCCEEDED", args, {"count": len(matches), "truncated": truncated}, started)
        return result

    def _walk(self, root: Path) -> Iterable[Path]:
        if root.is_file():
            yield root
            return
        for current, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in {".git", "node_modules", "__pycache__", ".venv", "venv"})
            for name in sorted(files):
                path = Path(current) / name
                if path.is_symlink() or not path.is_file():
                    continue
                yield path

    # ------------------------------------------------------------ read_file
    def read_file(
        self, path: str, start_line: int = 1, end_line: Optional[int] = None, max_bytes: int = 200_000
    ) -> Dict[str, Any]:
        args = {"path": _bounded_text(path, 300), "start_line": start_line, "end_line": end_line}
        self._begin("read_file", "source.read", args)
        started = _utc()
        try:
            relative, full = self._resolve(path)
            if not full.is_file() or full.is_symlink() and not full.resolve().is_file():
                raise ToolError("NOT_A_REGULAR_FILE", f"Not a regular file: {relative}")
            data = full.read_bytes()
            max_bytes = max(1, min(int(max_bytes), READ_MAX_BYTES))
            if self.read_bytes + min(len(data), max_bytes) > self.max_read_bytes:
                raise ToolError("READ_BUDGET_EXHAUSTED", "Cumulative read budget for this action is exhausted")
            file_hash = _sha256(data)
            if b"\x00" in data[:8192]:
                result = {"path": relative, "binary": True, "sha256": file_hash, "size": len(data), "content": None}
                self._event("read_file", "source.read", "SUCCEEDED", args, {"binary": True}, started)
                return result
            try:
                text = data.decode("utf-8")
                encoding = "utf-8"
            except UnicodeDecodeError:
                result = {"path": relative, "binary": True, "encoding": "undecodable", "sha256": file_hash, "size": len(data), "content": None}
                self._event("read_file", "source.read", "SUCCEEDED", args, {"binary": True}, started)
                return result
            lines = text.splitlines(keepends=True)
            total = len(lines)
            start = max(1, int(start_line))
            end = int(end_line) if end_line is not None else start + READ_DEFAULT_LINES - 1
            end = min(end, start + READ_MAX_LINES - 1, total)
            selected = "".join(lines[start - 1 : end]) if total else ""
            truncated = end < total if end_line is None else False
            encoded = selected.encode("utf-8")
            if len(encoded) > max_bytes:
                selected = encoded[:max_bytes].decode("utf-8", "ignore")
                truncated = True
            self.read_bytes += len(selected.encode("utf-8"))
            result = {
                "path": relative,
                "binary": False,
                "encoding": encoding,
                "content": selected,
                "sha256": file_hash,
                "start_line": start,
                "end_line": end,
                "total_lines": total,
                "newline_at_eof": text.endswith("\n"),
                "truncated": truncated,
            }
            self._event("read_file", "source.read", "TRUNCATED" if truncated else "SUCCEEDED", args, {"bytes": len(selected), "truncated": truncated}, started)
            return result
        except ToolError as exc:
            self._event("read_file", "source.read", "FAILED", args, {"error": exc.code}, started)
            raise

    # -------------------------------------------------------------- symbols
    def symbols(self, query: str, path: Optional[str] = None, kind: Optional[str] = None, max_results: int = 50) -> Dict[str, Any]:
        args = {"query": _bounded_text(query, 200), "path": path, "kind": kind}
        _, started = self._begin("symbols", "source.symbols", args)
        snapshot = self._load_symbols()
        needle = (query or "").strip().lower()
        max_results = max(1, min(int(max_results), 200))
        exact: List[Dict[str, Any]] = []
        partial: List[Dict[str, Any]] = []
        for record in snapshot.get("symbols", []):
            if path and record.get("path") != path.lstrip("./"):
                continue
            if kind and record.get("kind") != kind:
                continue
            leaf = str(record.get("name", "")).split(".")[-1].lower()
            if needle and leaf == needle:
                exact.append(record)
            elif not needle or needle in str(record.get("name", "")).lower():
                partial.append(record)
        chosen = (exact + partial)[:max_results]
        stale_paths = set()
        for record in chosen:
            file_path = WORKSPACE / record.get("path", "")
            try:
                current = _sha256(file_path.read_bytes())
            except OSError:
                current = None
            if current != record.get("file_sha256"):
                stale_paths.add(record.get("path"))
        results = [dict(record, stale=record.get("path") in stale_paths) for record in chosen]
        response = {
            "symbols": results,
            "source_revision": snapshot.get("source_revision"),
            "parser_version": snapshot.get("parser_version"),
            "stale": bool(stale_paths),
            "truncated": len(exact) + len(partial) > max_results,
        }
        self._event("symbols", "source.symbols", "SUCCEEDED", args, {"count": len(results), "stale": bool(stale_paths)}, started)
        return response

    def _load_symbols(self) -> Dict[str, Any]:
        if self._symbols is None:
            path = CONTEXT / "symbols.json"
            try:
                self._symbols = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._symbols = {"symbols": [], "source_revision": None}
        return self._symbols

    # ---------------------------------------------------------- apply_patch
    def apply_patch(
        self,
        patch: Union[str, List[Dict[str, Any]], Dict[str, str]],
        expected_hashes: Optional[Dict[str, str]] = None,
        allow_create: bool = True,
        allow_delete: bool = False,
    ) -> Dict[str, Any]:
        args = {"form": type(patch).__name__, "expected_hashes": sorted((expected_hashes or {}).keys())[:50]}
        _, started = self._begin("apply_patch", "workspace.patch", args)
        try:
            plan = self._plan_patch(patch, dict(expected_hashes or {}), bool(allow_create), bool(allow_delete))
            written = self._commit_patch(plan)
        except ToolError as exc:
            self._event("apply_patch", "workspace.patch", "FAILED", args, {"error": exc.code, "message": exc.message[:300]}, started)
            raise
        summary = {
            "changed_paths": sorted(written),
            "created": sorted(path for path, item in plan.items() if item["before"] is None and item["after"] is not None),
            "deleted": sorted(path for path, item in plan.items() if item["after"] is None),
            "hashes": {path: _sha256(item["after"]) for path, item in plan.items() if item["after"] is not None},
        }
        self._event("apply_patch", "workspace.patch", "SUCCEEDED", args, {"changed_paths": summary["changed_paths"]}, started)
        return summary

    def _plan_patch(
        self,
        patch: Any,
        expected: Dict[str, str],
        allow_create: bool,
        allow_delete: bool,
    ) -> Dict[str, Dict[str, Any]]:
        plan: Dict[str, Dict[str, Any]] = {}

        def load(path: str) -> Tuple[str, Path, Optional[bytes]]:
            relative, full = self._resolve(path, must_exist=False, for_write=True)
            if not self._declared(relative):
                raise ToolError("PATH_NOT_DECLARED", f"{relative} is not in the action's declared_paths")
            if relative in plan:
                return relative, full, plan[relative]["after"]
            if full.exists():
                if not full.is_file():
                    raise ToolError("NOT_A_REGULAR_FILE", f"Refusing to patch a non-regular file: {relative}")
                data = full.read_bytes()
                if b"\x00" in data[:8192]:
                    raise ToolError("BINARY_TARGET", f"Refusing to text-patch a binary file: {relative}")
                plan[relative] = {"full": full, "before": data, "after": data}
                return relative, full, data
            plan[relative] = {"full": full, "before": None, "after": None}
            return relative, full, None

        def check_hash(relative: str, before: Optional[bytes], required: bool) -> None:
            if before is None:
                return
            if relative in expected:
                if expected[relative] != _sha256(before):
                    raise ToolError("EXPECTED_HASH_MISMATCH", f"{relative} changed since it was read")
            elif required:
                raise ToolError("EXPECTED_HASH_REQUIRED", f"expected_hashes must include {relative} (sha256 from read_file)")

        if isinstance(patch, str):
            if len(patch.encode("utf-8")) > PATCH_MAX_BYTES:
                raise ToolError("PATCH_TOO_LARGE", "Patch exceeds 1 MiB")
            files = _parse_unified_diff(patch)
            if not files:
                raise ToolError("PATCH_EMPTY", "No file patches found in unified diff")
            if len(files) > PATCH_MAX_FILES or sum(len(item["hunks"]) for item in files) > PATCH_MAX_HUNKS:
                raise ToolError("PATCH_TOO_LARGE", "Patch has too many files or hunks")
            for item in files:
                if item["old_path"] and item["new_path"] and item["old_path"] != item["new_path"]:
                    raise ToolError("RENAME_NOT_ALLOWED", "Renames are not supported by apply_patch")
                target = item["new_path"] or item["old_path"]
                relative, full, current = load(target)
                if item["new_path"] is None:
                    if not allow_delete:
                        raise ToolError("DELETE_NOT_ALLOWED", f"Deletion of {relative} requires allow_delete=True")
                    check_hash(relative, current, True)
                    if current is None:
                        raise ToolError("FILE_NOT_FOUND", relative)
                    plan[relative]["after"] = None
                    continue
                if item["old_path"] is None:
                    if current is not None:
                        raise ToolError("FILE_EXISTS", f"Patch creates {relative} but it already exists")
                    if not allow_create:
                        raise ToolError("CREATE_NOT_ALLOWED", f"Creating {relative} requires allow_create=True")
                    text = ""
                else:
                    if current is None:
                        raise ToolError("FILE_NOT_FOUND", relative)
                    # Hunk context anchors the edit; a hash is verified when supplied.
                    check_hash(relative, plan[relative]["before"], False)
                    text = current.decode("utf-8")
                plan[relative]["after"] = _apply_hunks(relative, text, item["hunks"]).encode("utf-8")
        elif isinstance(patch, list):
            if len(patch) > PATCH_MAX_HUNKS:
                raise ToolError("PATCH_TOO_LARGE", "Too many edits")
            for edit in patch:
                if not isinstance(edit, dict) or set(edit) - {"path", "old", "new"} or "path" not in edit:
                    raise ToolError("EDIT_INVALID", "Each edit must be {'path', 'old', 'new'}")
                old = edit.get("old", "")
                new = edit.get("new", "")
                if not isinstance(old, str) or not isinstance(new, str):
                    raise ToolError("EDIT_INVALID", "old/new must be strings")
                relative, full, current = load(edit["path"])
                check_hash(relative, plan[relative]["before"], False)
                if current is None:
                    if old:
                        raise ToolError("FILE_NOT_FOUND", relative)
                    if not allow_create:
                        raise ToolError("CREATE_NOT_ALLOWED", f"Creating {relative} requires allow_create=True")
                    plan[relative]["after"] = new.encode("utf-8")
                    continue
                text = current.decode("utf-8")
                if not old:
                    raise ToolError("EDIT_INVALID", f"'old' must be non-empty when editing existing {relative}")
                count = text.count(old)
                if count != 1:
                    raise ToolError(
                        "EDIT_NOT_UNIQUE" if count else "EDIT_NOT_FOUND",
                        f"'old' text occurs {count} times in {relative}; it must occur exactly once",
                    )
                plan[relative]["after"] = text.replace(old, new, 1).encode("utf-8")
        elif isinstance(patch, dict):
            if len(patch) > PATCH_MAX_FILES:
                raise ToolError("PATCH_TOO_LARGE", "Too many files")
            for path, content in patch.items():
                if not isinstance(content, str):
                    raise ToolError("EDIT_INVALID", "Whole-file content must be a string")
                relative, full, current = load(path)
                if current is None and not allow_create:
                    raise ToolError("CREATE_NOT_ALLOWED", f"Creating {relative} requires allow_create=True")
                check_hash(relative, plan[relative]["before"], True)
                plan[relative]["after"] = content.encode("utf-8")
        else:
            raise ToolError("PATCH_INVALID", "patch must be a unified diff string, an edit list, or a {path: content} dict")
        total = sum(len(item["after"] or b"") for item in plan.values())
        if total > 64 * 1024 * 1024:
            raise ToolError("PATCH_TOO_LARGE", "Resulting files exceed 64 MiB")
        return plan

    def _commit_patch(self, plan: Dict[str, Dict[str, Any]]) -> List[str]:
        """All-or-nothing: stage every file, then replace; undo on any failure."""
        staged: List[Tuple[str, Path, Optional[Path]]] = []
        applied: List[Tuple[str, Path, Optional[bytes], Optional[int]]] = []
        try:
            for relative, item in plan.items():
                if item["after"] == item["before"]:
                    continue
                full: Path = item["full"]
                if item["after"] is None:
                    staged.append((relative, full, None))
                    continue
                full.parent.mkdir(parents=True, exist_ok=True)
                temp = full.parent / f".harness-tmp-{os.getpid()}-{len(staged)}"
                with open(temp, "xb") as handle:
                    handle.write(item["after"])
                    handle.flush()
                    try:
                        os.fsync(handle.fileno())
                    except OSError:
                        pass
                mode = full.stat().st_mode & 0o777 if full.exists() else 0o644
                os.chmod(temp, mode)
                staged.append((relative, full, temp))
            for relative, full, temp in staged:
                previous = full.read_bytes() if full.exists() else None
                previous_mode = full.stat().st_mode & 0o777 if full.exists() else None
                if temp is None:
                    full.unlink()
                else:
                    os.replace(temp, full)
                applied.append((relative, full, previous, previous_mode))
        except BaseException as exc:
            for relative, full, previous, previous_mode in reversed(applied):
                try:
                    if previous is None:
                        full.unlink(missing_ok=True)
                    else:
                        full.write_bytes(previous)
                        if previous_mode is not None:
                            os.chmod(full, previous_mode)
                except OSError:
                    pass
            for _, _, temp in staged:
                if temp is not None and temp.exists():
                    temp.unlink()
            if isinstance(exc, ToolError):
                raise
            raise ToolError("PATCH_WRITE_FAILED", str(exc)[:300])
        return [relative for relative, _, _, _ in applied]

    # ------------------------------------------------------------------ run
    def run(
        self,
        argv: Union[Sequence[str], str],
        cwd: str = ".",
        timeout_seconds: float = 60,
        env: Optional[Dict[str, str]] = None,
        shell: bool = False,
    ) -> Dict[str, Any]:
        capability = "sandbox.command.shell" if shell else "sandbox.command.argv"
        summary_argv = [_bounded_text(a, 120) for a in (argv if isinstance(argv, (list, tuple)) else [argv])][:12]
        args = {"argv": summary_argv, "cwd": cwd, "shell": bool(shell)}
        _, started = self._begin("run", capability, args)
        self.run_calls += 1
        if self.run_calls > self.max_run_calls:
            self._event("run", capability, "REJECTED", args, {"error": "RUN_CALL_LIMIT"}, started)
            raise ToolError("RUN_CALL_LIMIT", f"More than {self.max_run_calls} run() calls in one action")
        if shell:
            if not isinstance(argv, str) or not argv or len(argv) > 32768:
                raise ToolError("ARGV_INVALID", "shell=True requires one command string")
            command: List[str] = [os.environ.get("HARNESS_SHELL", "/bin/bash"), "-c", argv]
        else:
            if isinstance(argv, str):
                raise ToolError("ARGV_INVALID", "argv must be a list of strings (use shell=True for a command string)")
            command = list(argv)
            if not command or len(command) > 256 or not all(isinstance(a, str) and len(a) <= 32768 for a in command):
                raise ToolError("ARGV_INVALID", "argv must be a non-empty list of up to 256 strings")
        if _is_workspace_root(cwd):
            work_dir = WORKSPACE
        else:
            _, work_dir = self._resolve(cwd)
            if not work_dir.is_dir():
                raise ToolError("CWD_INVALID", f"cwd is not a directory: {cwd}")
        child_env = {key: value for key, value in os.environ.items() if not key.startswith("HARNESS_")}
        deps = os.environ.get("HARNESS_DEPENDENCY_SITE")
        if deps and Path(deps).is_dir():
            child_env["PYTHONPATH"] = deps + (":" + child_env["PYTHONPATH"] if child_env.get("PYTHONPATH") else "")
        # src/-layout roots come first so the edited sources always win over any installed copy.
        roots = [r for r in os.environ.get("HARNESS_SOURCE_ROOTS", "").split(":") if r and Path(r).is_dir()]
        if roots:
            child_env["PYTHONPATH"] = ":".join(roots) + (":" + child_env["PYTHONPATH"] if child_env.get("PYTHONPATH") else "")
        for key, value in (env or {}).items():
            if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", str(key)) or any(p.search(key) for p in DENIED_ENV_PATTERNS):
                raise ToolError("ENV_NOT_ALLOWED", f"Environment key is not allowed: {key}")
            if not isinstance(value, str) or len(value) > 4096:
                raise ToolError("ENV_NOT_ALLOWED", f"Environment value for {key} is invalid")
            child_env[key] = value
        remaining = self._remaining()
        if remaining <= 1:
            raise ToolError("ACTION_TIME_EXHAUSTED", "No action time remains for run()")
        timeout = max(1.0, min(float(timeout_seconds), remaining))
        log_dir = OUTPUT / "runs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = log_dir / f"{self.run_calls:02d}.stdout"
        stderr_path = log_dir / f"{self.run_calls:02d}.stderr"
        begin = time.monotonic()
        timed_out = False
        with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
            try:
                proc = subprocess.Popen(
                    command,
                    cwd=str(work_dir),
                    env=child_env,
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    start_new_session=True,
                )
            except (OSError, ValueError) as exc:
                self._event("run", capability, "FAILED", args, {"error": "EXEC_FAILED"}, started)
                raise ToolError("EXEC_FAILED", str(exc)[:300])
            try:
                while True:
                    try:
                        proc.wait(timeout=0.2)
                        break
                    except subprocess.TimeoutExpired:
                        if time.monotonic() - begin > timeout:
                            timed_out = True
                            break
                        if out.tell() > RUN_LOG_BYTES * 4 or err.tell() > RUN_LOG_BYTES * 4:
                            break
            finally:
                _kill_group(proc)
        exit_code = proc.returncode
        signal_number = -exit_code if exit_code is not None and exit_code < 0 else None
        stdout_text, stdout_trunc = _excerpt(stdout_path)
        stderr_text, stderr_trunc = _excerpt(stderr_path)
        for path in (stdout_path, stderr_path):
            if path.stat().st_size > RUN_LOG_BYTES:
                with open(path, "r+b") as handle:
                    handle.truncate(RUN_LOG_BYTES)
        result = {
            "exit_code": None if signal_number else exit_code,
            "signal": signal_number,
            "timed_out": timed_out,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "stdout_truncated": stdout_trunc,
            "stderr_truncated": stderr_trunc,
            "elapsed_ms": int((time.monotonic() - begin) * 1000),
            "stdout_log": f"runs/{stdout_path.name}",
            "stderr_log": f"runs/{stderr_path.name}",
        }
        self._event(
            "run", capability, "SUCCEEDED", args,
            {"exit_code": result["exit_code"], "signal": signal_number, "timed_out": timed_out}, started,
        )
        return result

    # --------------------------------------------------------- read_artifact
    def read_artifact(self, artifact_id: str, offset: int = 0, max_bytes: int = 200_000) -> Dict[str, Any]:
        args = {"artifact_id": _bounded_text(artifact_id, 100), "offset": offset}
        _, started = self._begin("read_artifact", "context.artifact.read", args)
        manifest = self._load_manifest()
        entry = manifest.get("artifacts", {}).get(artifact_id) if isinstance(artifact_id, str) else None
        if not entry:
            self._event("read_artifact", "context.artifact.read", "REJECTED", args, {"error": "ARTIFACT_NOT_IN_SCOPE"}, started)
            raise ToolError("ARTIFACT_NOT_IN_SCOPE", f"Artifact is not staged for this action: {artifact_id}")
        path = CONTEXT / entry["path"]
        try:
            path.resolve().relative_to(CONTEXT.resolve())
            data = path.read_bytes()
        except (OSError, ValueError):
            self._event("read_artifact", "context.artifact.read", "FAILED", args, {"error": "ARTIFACT_INTEGRITY_ERROR"}, started)
            self.integrity_failure = artifact_id
            raise ToolError("ARTIFACT_INTEGRITY_ERROR", artifact_id)
        if _sha256(data) != entry["sha256"] or len(data) != entry["size"]:
            self.integrity_failure = artifact_id
            self._event("read_artifact", "context.artifact.read", "FAILED", args, {"error": "ARTIFACT_INTEGRITY_ERROR"}, started)
            raise ToolError("ARTIFACT_INTEGRITY_ERROR", f"Artifact hash mismatch: {artifact_id}")
        cap = min(int(max_bytes), int(entry.get("byte_cap", 200_000)), READ_MAX_BYTES)
        offset = max(0, int(offset))
        chunk = data[offset : offset + cap]
        if self.read_bytes + len(chunk) > self.max_read_bytes:
            raise ToolError("READ_BUDGET_EXHAUSTED", "Cumulative read budget for this action is exhausted")
        self.read_bytes += len(chunk)
        media = entry.get("media_type", "application/octet-stream")
        textual = media.startswith("text/") or media in ("application/json", "application/x-ndjson")
        result = {
            "artifact_id": artifact_id,
            "media_type": media,
            "sha256": entry["sha256"],
            "size": len(data),
            "offset": offset,
            "content": chunk.decode("utf-8", "replace") if textual else base64.b64encode(chunk).decode("ascii"),
            "encoding": "utf-8" if textual else "base64",
            "truncated": offset + len(chunk) < len(data),
        }
        self._event("read_artifact", "context.artifact.read", "SUCCEEDED", args, {"bytes": len(chunk)}, started)
        return result

    def _load_manifest(self) -> Dict[str, Any]:
        if self._manifest is None:
            try:
                self._manifest = json.loads((CONTEXT / "manifest.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._manifest = {"artifacts": {}}
        return self._manifest

    # ----------------------------------------------------------- emit_result
    def emit_result(
        self,
        status: str,
        summary: str,
        observations: Optional[Sequence[str]] = None,
        artifact_refs: Optional[Sequence[str]] = None,
        requests: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        args = {"status": _bounded_text(status, 40)}
        _, started = self._begin("emit_result", "action.result.emit", args)
        if self.result_emitted:
            self._event("emit_result", "action.result.emit", "REJECTED", args, {"error": "RESULT_ALREADY_EMITTED"}, started)
            raise ToolError("RESULT_ALREADY_EMITTED", "emit_result may be called once per action")
        if status not in RESULT_STATUSES:
            self._event("emit_result", "action.result.emit", "REJECTED", args, {"error": "STATUS_INVALID"}, started)
            raise ToolError("STATUS_INVALID", f"status must be one of {', '.join(RESULT_STATUSES)}")
        observations = [(_bounded_text(item, 1000)) for item in list(observations or [])[:20]]
        refs = [str(item)[:128] for item in list(artifact_refs or [])[:20]]
        clean_requests = []
        for item in list(requests or [])[:10]:
            if not isinstance(item, dict):
                continue
            clean_requests.append({
                "kind": _bounded_text(item.get("kind", "note"), 64),
                "detail": _bounded_text(item.get("detail", ""), 500),
            })
        payload = {
            "schema_version": "1.0",
            "action_id": self.action_id,
            "status": status,
            "summary": _bounded_text(summary or "", 4000),
            "observations": observations,
            "artifact_refs": refs,
            "requests": clean_requests,
            "trusted_as": "advisory",
        }
        temp = OUTPUT / ".result.json.tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
        os.replace(temp, OUTPUT / "result.json")
        self.result_emitted = True
        self._event("emit_result", "action.result.emit", "SUCCEEDED", args, {"status": status}, started)
        return {"accepted_as": "advisory", "status": status}


# ------------------------------------------------------------------ helpers
def _kill_group(proc: "subprocess.Popen[bytes]") -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _excerpt(path: Path) -> Tuple[str, bool]:
    size = path.stat().st_size
    with open(path, "rb") as handle:
        if size <= RUN_EXCERPT_BYTES:
            return handle.read().decode("utf-8", "replace"), False
        head = handle.read(RUN_EXCERPT_BYTES // 4)
        handle.seek(max(0, size - (RUN_EXCERPT_BYTES * 3) // 4))
        tail = handle.read()
    return head.decode("utf-8", "replace") + f"\n…[{size - len(head) - len(tail)} bytes omitted]…\n" + tail.decode("utf-8", "replace"), True


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _strip_prefix(raw: str) -> Optional[str]:
    value = raw.split("\t", 1)[0].strip()
    if value == "/dev/null":
        return None
    if value.startswith(("a/", "b/")):
        value = value[2:]
    return value


def _parse_unified_diff(text: str) -> List[Dict[str, Any]]:
    files: List[Dict[str, Any]] = []
    lines = text.splitlines(keepends=True)
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("--- ") and index + 1 < len(lines) and lines[index + 1].startswith("+++ "):
            old_path = _strip_prefix(line[4:])
            new_path = _strip_prefix(lines[index + 1][4:])
            index += 2
            hunks = []
            while index < len(lines) and lines[index].startswith("@@"):
                match = _HUNK_RE.match(lines[index])
                if not match:
                    raise ToolError("PATCH_INVALID", f"Malformed hunk header: {lines[index][:80]!r}")
                old_start = int(match.group(1))
                old_len = int(match.group(2) or "1")
                new_len = int(match.group(4) or "1")
                index += 1
                body: List[str] = []
                seen_old = seen_new = 0
                new_no_eol = False
                while index < len(lines) and (seen_old < old_len or seen_new < new_len):
                    current = lines[index]
                    if current.startswith("\\"):
                        if body and body[-1][:1] in (" ", "+"):
                            new_no_eol = True
                        index += 1
                        continue
                    marker = current[:1]
                    if marker not in (" ", "-", "+"):
                        if current.strip() == "":
                            current = " " + current
                            marker = " "
                        else:
                            raise ToolError("PATCH_INVALID", f"Unexpected line in hunk: {current[:80]!r}")
                    if marker in (" ", "-"):
                        seen_old += 1
                    if marker in (" ", "+"):
                        seen_new += 1
                    body.append(current)
                    index += 1
                while index < len(lines) and lines[index].startswith("\\"):
                    if body and body[-1][:1] in (" ", "+"):
                        new_no_eol = True
                    index += 1
                hunks.append({"old_start": old_start, "old_len": old_len, "lines": body, "new_no_eol": new_no_eol})
            files.append({"old_path": old_path, "new_path": new_path, "hunks": hunks})
            continue
        index += 1
    return files


def _apply_hunks(path: str, text: str, hunks: List[Dict[str, Any]]) -> str:
    """Exact hunk application. A hunk may move only to a unique exact offset."""
    source = text.splitlines(keepends=True)
    output: List[str] = []
    cursor = 0
    for hunk in hunks:
        old = [line[1:] for line in hunk["lines"] if line[:1] in (" ", "-")]
        new = [line[1:] for line in hunk["lines"] if line[:1] in (" ", "+")]
        preferred = max(0, hunk["old_start"] - 1) if hunk["old_len"] else hunk["old_start"]
        position = None
        if _block_matches(source, preferred, old):
            position = preferred
        else:
            hits = [i for i in range(cursor, len(source) - len(old) + 1) if _block_matches(source, i, old)]
            if len(hits) == 1:
                position = hits[0]
            elif not hits:
                raise ToolError("HUNK_FAILED", f"Hunk context not found in {path} near line {hunk['old_start']}")
            else:
                raise ToolError("HUNK_AMBIGUOUS", f"Hunk context matches {len(hits)} places in {path}")
        if position < cursor:
            raise ToolError("HUNK_FAILED", f"Overlapping hunks in {path}")
        output.extend(source[cursor:position])
        output.extend(new)
        cursor = position + len(old)
    output.extend(source[cursor:])
    result = "".join(output)
    if hunks and hunks[-1].get("new_no_eol") and cursor >= len(source) and result.endswith("\n"):
        result = result[:-1]
    return result


def _block_matches(source: List[str], start: int, block: List[str]) -> bool:
    if start < 0 or start + len(block) > len(source):
        return False
    for offset, expected in enumerate(block):
        if source[start + offset].rstrip("\r\n") != expected.rstrip("\r\n"):
            return False
    return True
