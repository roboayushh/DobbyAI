"""Deterministic, atomic export bundle (PRD 6 section 8).

``harness export RUN_ID --output PATH`` is read-only toward the original source and
every external system. The bundle binds exact B -> C identities, passes the fresh
baseline round-trip gate before it may be labeled VALID, and is committed with an
atomic same-filesystem rename so a crash never exposes a partial bundle.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from harness.contracts.release import ExportManifestV1, ExportRequestV1
from harness.export.patch_adapter import PatchAdapter, PatchRejected
from harness.export.round_trip import round_trip
from harness.persistence import canonical_json
from harness.persistence.events import append_event_sql
from harness.release.identity import commit_content_sha256, path_hash, sha256_json
from harness.release.report_builder import machine_report, markdown_report
from harness.release.result_builder import build_result, usage, verification_summary
from harness.release.run_facts import RunFacts, gather

BUNDLE_FILES = ("manifest.json", "result.json", "patch.diff", "report.md", "checksums.sha256")


class ExportError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


@dataclass
class ExportOutcome:
    manifest: ExportManifestV1
    bundle_path: Path
    patch_sha256: str
    replayed: bool = False


class ExportService:
    def __init__(self, *, run_store, artifact_store, services, coordinator, data_root: Path, adapter: Optional[PatchAdapter] = None) -> None:
        self.run_store = run_store
        self.artifact_store = artifact_store
        self.services = services
        self.coordinator = coordinator
        self.data_root = Path(data_root).resolve()
        self.adapter = adapter or PatchAdapter()

    # ------------------------------------------------------------ preflight
    def preflight_output(self, output: str | Path, facts: RunFacts, *, replace: bool = False) -> Path:
        raw = Path(output).expanduser()
        if not raw.is_absolute():
            raw = Path.cwd() / raw
        if any(ord(ch) < 32 for ch in str(raw)):
            raise ExportError("EXPORT_PATH_INVALID", "Export path contains control characters")
        parent = raw.parent
        try:
            parent = parent.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ExportError("EXPORT_PATH_INVALID", f"Export parent directory does not exist: {raw.parent}") from exc
        if not parent.is_dir():
            raise ExportError("EXPORT_PATH_INVALID", "Export parent is not a directory")
        target = parent / raw.name
        if target.is_symlink():
            raise ExportError("EXPORT_PATH_INVALID", "Export path is a symlink")
        if target.exists() and not target.is_dir():
            raise ExportError("EXPORT_PATH_INVALID", "Export path exists and is not a directory")
        protected: List[Path] = [self.data_root / "runs"]
        locator = facts.source.get("canonical_locator") or ""
        if facts.source.get("source_kind") in ("local_git", "local_folder") and locator:
            protected.append(Path(locator).resolve())
        for root in protected:
            if target == root or root in target.parents or target in root.parents:
                raise ExportError("EXPORT_PATH_PROTECTED", f"Export path overlaps a protected location ({root.name})")
        free = shutil.disk_usage(parent).free
        if free < 50 * 1024 * 1024:
            raise ExportError("EXPORT_INSUFFICIENT_SPACE", "Less than 50 MiB free at the export destination")
        return target

    # ---------------------------------------------------------------- build
    def export(self, run_id: str, output: str | Path, *, replace: bool = False, max_bundle_bytes: int = 25_000_000,
               extra_result: Optional[Dict[str, Any]] = None, result_builder=None) -> ExportOutcome:
        facts = gather(run_id, run_store=self.run_store, artifact_store=self.artifact_store,
                       services=self.services, coordinator=self.coordinator)
        if not facts.candidate.base:
            raise ExportError("EXPORT_NOT_READY", "Run has no imported baseline")
        target = self.preflight_output(output, facts, replace=replace)
        git = self.services.workspaces.git(run_id)
        base, head = facts.candidate.base, facts.candidate.head
        head_content = commit_content_sha256(git, head)
        request_core = {
            "schema_version": "1.0",
            "run_id": run_id,
            "candidate": {"base_commit": base, "head_commit": head, "head_tree": git.commit_tree_of(head), "head_content_sha256": head_content},
            "format": self.adapter.format,
            "output_path_sha256": path_hash(target),
            "max_bundle_bytes": max_bundle_bytes,
        }
        request_sha = sha256_json(request_core)
        replay = self._replay(run_id, request_sha, target)
        if replay is not None:
            return replay
        if target.exists() and any(target.iterdir()) and not replace:
            raise ExportError("EXPORT_OUTPUT_EXISTS", "Export path already contains files; pass --replace to replace the bundle")
        handoff = self.artifact_store.get_artifact_by_path(run_id, "prd5/final/release-candidate-handoff.json")
        request = ExportRequestV1(
            export_request_id=f"xreq_{uuid.uuid4().hex[:16]}",
            run_id=run_id,
            release_handoff_artifact_id=handoff["artifact_id"] if handoff else None,
            candidate=request_core["candidate"],
            output_path=str(target),
            replace_existing=replace,
            max_bundle_bytes=max_bundle_bytes,
            request_sha256=request_sha,
        )
        request_path = f"prd6/export/{request.export_request_id}/request.json"
        self.artifact_store.write_json(run_id, request_path, request.model_dump(mode="json"), "export_request")
        request_artifact = self.artifact_store.get_artifact_by_path(run_id, request_path)
        with self.run_store.get_connection() as conn:
            with conn:
                conn.execute(
                    "INSERT INTO h_export_requests VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'BUILDING', ?, NULL)",
                    (request.export_request_id, run_id, self._queue_result_id(run_id), self._candidate_id(facts),
                     self.adapter.format, request_core["output_path_sha256"], 1 if replace else 0, max_bundle_bytes,
                     request_artifact["artifact_id"], request_sha, _now()),
                )
                append_event_sql(conn, run_id, "EXPORT_REQUESTED", {"export_request_id": request.export_request_id,
                                                                     "candidate": head, "kind": facts.candidate.kind}, "PRD6", "PRD6", _now())
        try:
            return self._build(run_id, facts, request, target, git, head_content, extra_result or {}, result_builder)
        except BaseException as exc:
            code = getattr(exc, "code", "EXPORT_FAILED")
            with self.run_store.get_connection() as conn:
                with conn:
                    conn.execute("UPDATE h_export_requests SET state = ?, settled_at = ? WHERE export_request_id = ?",
                                 ("INVALID" if code == "EXPORT_INVALID" else ("CANCELLED" if isinstance(exc, KeyboardInterrupt) else "FAILED"),
                                  _now(), request.export_request_id))
                    append_event_sql(conn, run_id, "EXPORT_FAILED", {"export_request_id": request.export_request_id, "code": code,
                                                                      "message": str(exc)[:300]}, "PRD6", "PRD6", _now())
            raise

    def _build(self, run_id: str, facts: RunFacts, request: ExportRequestV1, target: Path, git, head_content: str,
               extra_result: Dict[str, Any], result_builder) -> ExportOutcome:
        base, head = facts.candidate.base, facts.candidate.head
        try:
            patch, changes = self.adapter.create(git, base, head, max_bytes=request.max_bundle_bytes)
            self.adapter.inspect(patch, changes)
        except PatchRejected as exc:
            raise ExportError("EXPORT_INVALID", str(exc)) from exc
        with self.run_store.get_connection() as conn:
            with conn:
                conn.execute("UPDATE h_export_requests SET state = 'ROUND_TRIP' WHERE export_request_id = ?", (request.export_request_id,))
                append_event_sql(conn, run_id, "EXPORT_BUILD_STARTED", {"export_request_id": request.export_request_id,
                                                                        "patch_bytes": len(patch)}, "PRD6", "PRD6", _now())
        work_root = self.data_root / "runs" / run_id / "export-work"
        work_root.mkdir(parents=True, exist_ok=True)
        trip = round_trip(git, base, head, patch, work_root, self.adapter)
        trip_report = trip.report()
        export_id = f"exp_{uuid.uuid4().hex[:16]}"
        prefix = f"prd6/export/{export_id}"
        self.artifact_store.write_json(run_id, f"{prefix}/round-trip.json", trip_report, "export_round_trip_report")
        trip_artifact = self.artifact_store.get_artifact_by_path(run_id, f"{prefix}/round-trip.json")
        with self.run_store.get_connection() as conn:
            with conn:
                append_event_sql(conn, run_id, "EXPORT_ROUND_TRIP_SETTLED", {"export_id": export_id, "status": trip.status,
                                                                              "reasons": trip.reasons}, "PRD6", "PRD6", _now())
        status = "VALID" if trip.status == "PASS" else "INVALID"
        patch_sha = hashlib.sha256(patch).hexdigest()
        provenance = extra_result.get("provenance") or {}
        # Bundle members.
        from harness.contracts.release import ResultExportV1

        export_view = ResultExportV1(status="VALID" if status == "VALID" else "EXPORT_INVALID", bundle_path=str(target),
                                     patch_sha256=patch_sha)
        builder = result_builder or (lambda f, **kw: build_result(f, git=git, **kw))
        result = builder(facts, export=export_view, request_id=extra_result.get("request_id"),
                         reproducibility_manifest_artifact_id=extra_result.get("reproducibility_manifest_artifact_id"))
        files: Dict[str, bytes] = {
            "patch.diff": patch,
            "result.json": (json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True) + "\n").encode(),
            "report.md": markdown_report(facts, round_trip=trip_report, provenance=provenance).encode("utf-8"),
            "evidence/verification-summary.json": _json_bytes({
                "verification": verification_summary(facts).model_dump(mode="json"),
                "tasks": machine_report(facts)["tasks"],
                "round_trip": trip_report,
            }),
            "evidence/task-results.json": _json_bytes([{
                "task_id": t.task_id, "ordinal": t.ordinal, "title": t.title[:200], "source_key": t.source_key,
                "queue_state": t.item_state, "status": t.external_status, "reasons": t.reasons[:20],
                "commit": t.commit, "changed_paths": t.changed_paths[:2000],
            } for t in facts.tasks]),
            "evidence/usage.json": _json_bytes({**usage(facts).model_dump(mode="json"), "calls_by_role": _calls_by_role(facts)}),
            "evidence/provenance.json": _json_bytes(provenance or {"note": "reproducibility manifest not recorded"}),
        }
        total = sum(len(data) for data in files.values())
        if total > request.max_bundle_bytes:
            raise ExportError("EXPORT_TOO_LARGE", f"Bundle would be {total} bytes (limit {request.max_bundle_bytes}); raw logs stay referenced")
        media = {"patch.diff": "text/x-diff", "report.md": "text/markdown"}
        file_entries = [{"path": name, "media_type": media.get(name, "application/json"), "bytes": len(data),
                         "sha256": hashlib.sha256(data).hexdigest()} for name, data in sorted(files.items())]
        manifest_core = {
            "schema_version": "1.0",
            "export_id": export_id,
            "run_id": run_id,
            "status": status,
            "format": self.adapter.format,
            "base": {"commit": base, "tree": git.commit_tree_of(base)},
            "candidate": {"commit": head, "tree": git.commit_tree_of(head), "content_sha256": head_content},
            "files": file_entries,
            "round_trip": {"status": trip.status, "result_tree": trip.observed_tree, "report_artifact_id": trip_artifact["artifact_id"]},
            "created_at": _now(),
        }
        manifest = ExportManifestV1(**manifest_core, manifest_sha256=sha256_json(manifest_core))
        files["manifest.json"] = _json_bytes(manifest.model_dump(mode="json"))
        checksum_lines = [f"{hashlib.sha256(data).hexdigest()}  {name}" for name, data in sorted(files.items())]
        files["checksums.sha256"] = ("\n".join(checksum_lines) + "\n").encode()
        # Durable artifacts for every member (h_export_files requires artifact identities).
        artifact_ids: Dict[str, str] = {}
        kinds = {"patch.diff": "export_patch", "manifest.json": "export_manifest", "report.md": "release_report",
                 "result.json": "evaluator_result"}
        for name, data in files.items():
            relative = f"{prefix}/bundle/{name}"
            self.artifact_store.write_bytes(run_id, relative, data, media.get(name, "application/json" if name.endswith(".json") else "text/plain"),
                                            kinds.get(name, "export_evidence"))
            artifact_ids[name] = self.artifact_store.get_artifact_by_path(run_id, relative)["artifact_id"]
        self._commit_output(target, files, replace=request.replace_existing)
        with self.run_store.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO h_exports VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (export_id, request.export_request_id, run_id, base, git.commit_tree_of(base), head, git.commit_tree_of(head),
                     head_content, artifact_ids["patch.diff"], patch_sha, artifact_ids["result.json"], artifact_ids["report.md"],
                     artifact_ids["manifest.json"], manifest.manifest_sha256, sum(len(d) for d in files.values()), status, _now()),
                )
                for ordinal, name in enumerate(sorted(files)):
                    conn.execute("INSERT INTO h_export_files VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                 (f"xfile_{uuid.uuid4().hex[:16]}", export_id, name, media.get(name, "application/json"),
                                  artifact_ids[name], len(files[name]), hashlib.sha256(files[name]).hexdigest(), ordinal))
                conn.execute(
                    "INSERT INTO h_export_round_trips VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (f"xrt_{uuid.uuid4().hex[:16]}", export_id, trip.environment_sha256, trip.expected_tree, trip.observed_tree,
                     trip.expected_content_sha256, trip.observed_content_sha256, trip.changed_path_set_sha256, trip.status,
                     trip_artifact["artifact_id"], trip.started_at, trip.settled_at),
                )
                conn.execute("UPDATE h_export_requests SET state = ?, settled_at = ? WHERE export_request_id = ?",
                             (status, _now(), request.export_request_id))
                append_event_sql(conn, run_id, "EXPORT_COMMITTED", {"export_id": export_id, "status": status,
                                                                     "manifest_sha256": manifest.manifest_sha256}, "PRD6", "PRD6", _now())
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return ExportOutcome(manifest, target, patch_sha)

    # ---------------------------------------------------------- atomic write
    @staticmethod
    def _commit_output(target: Path, files: Dict[str, bytes], *, replace: bool) -> None:
        parent = target.parent
        temp = parent / f".{target.name}.tmp-{uuid.uuid4().hex[:8]}"
        temp.mkdir()
        try:
            for name, data in sorted(files.items()):
                destination = temp / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                with open(destination, "xb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
            for directory in {temp, temp / "evidence"}:
                if directory.is_dir():
                    fd = os.open(directory, os.O_RDONLY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
            backup = None
            if target.exists():
                if any(target.iterdir()) and not replace:
                    raise ExportError("EXPORT_OUTPUT_EXISTS", "Export path gained files during export; refusing to overwrite")
                backup = parent / f".{target.name}.bak-{uuid.uuid4().hex[:8]}"
                os.rename(target, backup)
            try:
                os.rename(temp, target)
            except BaseException:
                if backup is not None:
                    os.rename(backup, target)
                raise
            if backup is not None:
                shutil.rmtree(backup, ignore_errors=True)
            fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            if temp.exists():
                shutil.rmtree(temp, ignore_errors=True)

    # ------------------------------------------------------------- helpers
    def _replay(self, run_id: str, request_sha: str, target: Path) -> Optional[ExportOutcome]:
        with self.run_store.get_connection() as conn:
            row = conn.execute(
                """SELECT e.* FROM h_exports e JOIN h_export_requests r ON r.export_request_id = e.export_request_id
                   WHERE r.run_id = ? AND r.request_sha256 = ? AND e.status = 'VALID' ORDER BY e.created_at DESC LIMIT 1""",
                (run_id, request_sha),
            ).fetchone()
        if not row or not verify_bundle(target):
            return None
        manifest = ExportManifestV1.model_validate_json((target / "manifest.json").read_text(encoding="utf-8"))
        if manifest.manifest_sha256 != row["manifest_sha256"]:
            return None
        return ExportOutcome(manifest, target, row["patch_sha256"], replayed=True)

    def _queue_result_id(self, run_id: str) -> Optional[str]:
        queue = self.coordinator.queue(run_id)
        if not queue:
            return None
        with self.run_store.get_connection() as conn:
            row = conn.execute("SELECT queue_result_id FROM h_queue_results WHERE queue_id = ?", (queue["queue_id"],)).fetchone()
        return row["queue_result_id"] if row else None

    def _candidate_id(self, facts: RunFacts) -> Optional[str]:
        for task in facts.tasks:
            if task.candidate and task.candidate["candidate_commit"] == facts.candidate.head:
                return task.candidate["candidate_id"]
        return None


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, default=str) + "\n").encode("utf-8")


def _calls_by_role(facts: RunFacts) -> Dict[str, Dict[str, int]]:
    roles: Dict[str, Dict[str, int]] = {}
    for call in facts.model_calls:
        entry = roles.setdefault(call["role"], {"calls": 0, "input_tokens": 0, "output_tokens": 0, "failed": 0})
        entry["calls"] += 1
        entry["input_tokens"] += int(call["input_tokens"] or 0)
        entry["output_tokens"] += int(call["output_tokens"] or 0)
        entry["failed"] += 0 if call["state"] == "SUCCEEDED" else 1
    return roles


def verify_bundle(path: Path) -> bool:
    """Recompute every member checksum; any mismatch or extra/missing file invalidates the bundle."""
    path = Path(path)
    checksums = path / "checksums.sha256"
    if not checksums.is_file():
        return False
    expected: Dict[str, str] = {}
    for line in checksums.read_text(encoding="utf-8").splitlines():
        digest, _, name = line.partition("  ")
        if not digest or not name or ".." in name.split("/") or name.startswith("/"):
            return False
        expected[name] = digest
    present = {p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file()} - {"checksums.sha256"}
    if present != set(expected):
        return False
    return all(hashlib.sha256((path / name).read_bytes()).hexdigest() == digest for name, digest in expected.items())
