"""Stable machine-mode boundary (PRD 6 EvaluatorGateway, section 6).

``harness run --input request.json --non-interactive``:

1. load and strictly parse the request (bounded, duplicate keys and non-finite
   numbers rejected, secrets rejected, unsupported major versions rejected);
2. resolve and bind the pinned plugin set, the release profile, the one model
   profile, and the credential presence; validate output paths;
   -> everything above happens BEFORE any repository mutation or model call;
3. idempotency: the same request returns its recorded result; the same request
   ID with different content is a conflict;
4. PRD 1 preparation, PRD 2-5 queue pipeline, PRD 6 export + round trip,
   reproducibility manifest;
5. one canonical ``EvaluatorResultV1`` (also written atomically to result_path).
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from harness.config import HarnessConfig
from harness.contracts.release import EvaluatorRequestV1, EvaluatorResultV1, ResultExportV1
from harness.evaluator.interface import EvaluatorInputError
from harness.evaluator.native_json import NativeJsonEvaluatorAdapter
from harness.evaluator.official_adapter_placeholder import OfficialAdapterPlaceholder
from harness.persistence import canonical_json
from harness.persistence.events import append_event_sql
from harness.release.status_mapping import exit_code

RUNTIME_ALIASES = {"python312_docker_v1": "python-default", "python-default": "python-default"}
MIN_MODEL_CALLS = 3  # one working call plus the two calls always reserved for verification/reporting
RESULT_LIMITATION = "PASS describes declared observed checks and does not guarantee hidden-test success."


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def minimal_result(status: str, *, code: str, message: str, request_id: Optional[str] = None, run_id: Optional[str] = None) -> EvaluatorResultV1:
    return EvaluatorResultV1(request_id=request_id, run_id=run_id, status=status, exit_code=exit_code(status),
                             error={"code": code, "message": message[:2000]}, limitations=[RESULT_LIMITATION], settled_at=_now())


def write_result_atomic(result: EvaluatorResultV1, path: str | Path) -> None:
    """Temp file + fsync + rename: a partial result is never visible at ``path``."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.parent / f".{target.name}.tmp-{uuid.uuid4().hex[:8]}"
    data = (json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True) + "\n").encode()
    with open(temp, "xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, target)


class EvaluatorGateway:
    def __init__(self, config: HarnessConfig, *, adapter_factory: Optional[Callable[[], Any]] = None,
                 log: Callable[[str], None] = lambda message: None) -> None:
        self.config = config
        self.adapter_factory = adapter_factory
        self.log = log

    # ----------------------------------------------------------------- load
    def load_request(self, path: str | Path) -> Tuple[bytes, EvaluatorRequestV1]:
        source = Path(path)
        if not source.is_file():
            raise EvaluatorInputError("INVALID_REQUEST", f"Request file not found: {path}")
        if source.stat().st_size > 4 * 1024 * 1024:
            raise EvaluatorInputError("REQUEST_TOO_LARGE", "Request file exceeds 4 MiB")
        raw = source.read_bytes()
        try:
            name = json.loads(raw).get("adapter", {}).get("name", "native_json_v1")
        except (ValueError, AttributeError):
            name = "native_json_v1"
        adapter = self.resolve_adapter(name)
        return raw, adapter.parse(raw)

    @staticmethod
    def resolve_adapter(name: str):
        if name == "native_json_v1":
            return NativeJsonEvaluatorAdapter()
        if isinstance(name, str) and name.startswith("official"):
            return OfficialAdapterPlaceholder()
        raise EvaluatorInputError("UNKNOWN_ADAPTER", f"Unknown evaluator adapter {name!r}; supported: native_json_v1")

    # -------------------------------------------------------------- execute
    def execute(self, raw: bytes, request: EvaluatorRequestV1) -> EvaluatorResultV1:
        from harness.release.profiles import ReleaseProfileError, ensure_profile, load_profile

        # ---- validation phase: no mutation, no model call ----------------
        try:
            profile = load_profile(request.profile)
        except ReleaseProfileError as exc:
            raise EvaluatorInputError("INVALID_RELEASE_PROFILE", str(exc)) from exc
        if request.runtime_profile not in RUNTIME_ALIASES:
            raise EvaluatorInputError("UNKNOWN_RUNTIME_PROFILE", f"Unsupported runtime_profile {request.runtime_profile!r}")
        for effect in request.requested_effects:
            if effect in ("APPLY_LOCAL", "PUSH_NEW_BRANCH") and not profile["effects"].get(effect):
                raise EvaluatorInputError("CAPABILITY_DISABLED", f"{effect} is a P1 effect disabled in release profile {profile['name']}")
        if request.budgets.model_calls is not None and request.budgets.model_calls < MIN_MODEL_CALLS:
            raise EvaluatorInputError("INVALID_REQUEST", f"budgets.model_calls must be at least {MIN_MODEL_CALLS}: two calls are "
                                      "always reserved for verification and reporting")
        if request.execution_mode == "evaluation" and request.task_mode != "single_issue":
            raise EvaluatorInputError("INVALID_REQUEST", "Evaluation accepts one independent task (task_mode=single_issue)")
        model_profile_id = request.model_config_ref or self.config.model_profile
        self._validate_model(model_profile_id)
        repo_path = request.repository.locator
        for field_name in ("result_path", "export_path"):
            value = getattr(request, field_name)
            if value is None:
                continue
            if not os.path.isabs(value):
                raise EvaluatorInputError("INVALID_OUTPUT_PATH", f"{field_name} must be an absolute path")
            if request.repository.kind in ("local_git", "local_folder"):
                resolved = Path(value).resolve()
                root = Path(repo_path).resolve()
                if resolved == root or root in resolved.parents:
                    raise EvaluatorInputError("INVALID_OUTPUT_PATH", f"{field_name} must not be inside the source repository")
            if Path(self.config.data_dir).resolve() / "runs" in Path(value).resolve().parents:
                raise EvaluatorInputError("INVALID_OUTPUT_PATH", f"{field_name} must not be inside harness run storage")
        plugins = self._resolve_plugins(profile)
        run_request = self._to_run_request(request, profile)

        # ---- execution phase --------------------------------------------
        from harness.application.composition import build_controller, build_services, ensure_runtime, scaled_budget
        from harness.cli import _get_services
        from harness.contracts.execution import PermissionProfile
        from harness.export.service import ExportError, ExportService
        from harness.queue import QueueCoordinator
        from harness.release.reproducibility import ReproducibilityService, effective_configuration
        from harness.release.result_builder import build_result
        from harness.release.run_facts import gather

        prep, run_store, artifact_store = _get_services(self.config)
        profile_row = ensure_profile(run_store, profile["name"], plugins.lock_sha256)
        request_sha = hashlib.sha256(canonical_json(json.loads(raw)).encode()).hexdigest()
        replay = self._existing_session(run_store, artifact_store, request, request_sha)
        if replay is not None:
            return replay
        prep.reconcile_interrupted()
        self.log(f"Preparing {request.repository.kind} source ...")
        try:
            prepared = prep.prepare(run_request)
        except Exception as exc:
            code = getattr(exc, "code", "PREPARATION_FAILED")
            status = "INVALID" if any(t in code for t in ("INVALID", "IDEMPOTENCY", "MISMATCH", "POLICY", "LIMITS")) else "FAILED"
            if isinstance(exc, KeyboardInterrupt):
                raise
            return minimal_result(status, code=code, message=str(exc), request_id=request.request_id)
        run_id = prepared.run_id
        session_id = self._record_session(run_store, artifact_store, run_id, profile_row, request, raw, request_sha)
        from harness.plugins.registry import PluginRegistry

        PluginRegistry(allowed_capabilities=profile["allowed_plugin_capabilities"]).bind_run(run_store, run_id, plugins)
        budgets = request.budgets
        limits = scaled_budget(len(prepared.tasks), max_calls=budgets.model_calls or self.config.max_model_calls,
                               max_wall_seconds=budgets.wall_seconds or self.config.max_run_wall_seconds)
        if budgets.input_tokens:
            limits = replace(limits, max_input_tokens=budgets.input_tokens)
        if budgets.output_tokens:
            limits = replace(limits, max_output_tokens=budgets.output_tokens)
        services = build_services(run_store, artifact_store, self.config.data_dir,
                                  permission_profile=PermissionProfile(profile["permission_profile"]),
                                  dependency_setup=self.config.dependency_setup)
        controller = build_controller(services, self.config.model_profiles_path, adapter=self.adapter_factory() if self.adapter_factory else None,
                                      budget_limits=limits, profile_id=model_profile_id)
        coordinator = QueueCoordinator(run_store=run_store, artifact_store=artifact_store, services=services, controller=controller)
        effective = effective_configuration(self.config, model_profile_id=model_profile_id, permission_profile=profile["permission_profile"],
                                            budgets={k: getattr(limits, k) for k in ("max_calls", "max_input_tokens", "max_output_tokens", "max_wall_seconds")},
                                            release_profile=profile["name"])
        adapter_ref = {"name": request.adapter.name, "version": request.adapter.version,
                       "content_sha256": plugins.plugins["evaluator"].manifest.plugin.content_sha256}
        repro = ReproducibilityService(run_store=run_store, artifact_store=artifact_store, services=services,
                                       coordinator=coordinator, plugin_lock_sha256=plugins.lock_sha256)
        self._session_state(run_store, session_id, "RUNNING")
        blocked = ensure_runtime(services, auto_build=self.config.auto_build_runtime, log=self.log)
        limitations: List[str] = []
        status_override: Optional[str] = None
        if blocked:
            status_override = "BLOCKED_ENVIRONMENT"
            limitations.append(f"{blocked}: the sandbox runtime is unavailable; the harness never falls back to host execution.")
        else:
            try:
                self.log(f"Run {run_id}: planning, sandboxed execution, verification ...")
                coordinator.run(run_id)
            except KeyboardInterrupt:
                coordinator.request_cancel(run_id)
                status_override = "CANCELLED"
            except Exception as exc:  # internal failure: settle truthfully, keep evidence
                status_override = "INTERNAL_ERROR"
                limitations.append(f"INTERNAL_ERROR: {getattr(exc, 'code', type(exc).__name__)}: {str(exc)[:300]}")
        manifest = repro.manifest(run_id, effective_config=effective, adapter=adapter_ref)
        recorded = repro.record(run_id, manifest, effective)
        export_view: Optional[ResultExportV1] = ResultExportV1(status="NOT_REQUESTED")
        facts = gather(run_id, run_store=run_store, artifact_store=artifact_store, services=services, coordinator=coordinator)
        git = services.workspaces.git(run_id)
        extra_pending: List[Dict[str, Any]] = []
        if "EXPORT" in request.requested_effects and request.export_path and status_override not in ("CANCELLED",):
            exporter = ExportService(run_store=run_store, artifact_store=artifact_store, services=services, coordinator=coordinator,
                                     data_root=self.config.data_dir)
            provenance = manifest.model_dump(mode="json")
            try:
                self.log(f"Exporting bundle to {request.export_path} (with patch round-trip) ...")
                outcome = exporter.export(
                    run_id, request.export_path, replace=False,
                    extra_result={"request_id": request.request_id, "provenance": provenance,
                                  "reproducibility_manifest_artifact_id": recorded["artifact_id"]},
                    result_builder=lambda f, **kw: build_result(f, git=git, status_override=status_override,
                                                                extra_limitations=limitations, extra_pending=extra_pending, **kw),
                )
                export_view = ResultExportV1(status="VALID" if outcome.manifest.status == "VALID" else "EXPORT_INVALID",
                                             bundle_path=str(outcome.bundle_path), manifest_sha256=outcome.manifest.manifest_sha256,
                                             patch_sha256=outcome.patch_sha256)
                if outcome.manifest.status != "VALID":
                    limitations.append("EXPORT_INVALID: the patch did not round-trip to the exact candidate; do not apply it.")
            except ExportError as exc:
                export_view = ResultExportV1(status="FAILED")
                limitations.append(f"{exc.code}: {str(exc)[:300]}")
        if "CLEANUP_RUN" in request.requested_effects:
            limitations.append("CLEANUP_RUN requires an exact approval grant; a pending capability request was recorded (no effect).")
            from harness.approvals.capabilities import KernelPrincipal
            from harness.retention.cleanup import CleanupService

            cleaner = CleanupService(run_store=run_store, artifact_store=artifact_store, data_root=self.config.data_dir)
            pending = cleaner.request(run_id, cleaner.plan(run_id), KernelPrincipal.headless())
            extra_pending.append({"capability_request_id": pending["capability_request_id"], "operation": "CLEANUP_RUN",
                                  "summary": "Remove transient registered run data (awaiting approval)"})
            status_override = status_override or "PENDING_APPROVAL"
            facts = gather(run_id, run_store=run_store, artifact_store=artifact_store, services=services, coordinator=coordinator)
        result = build_result(facts, git=git, request_id=request.request_id, export=export_view, status_override=status_override,
                              extra_limitations=limitations, reproducibility_manifest_artifact_id=recorded["artifact_id"],
                              extra_pending=extra_pending)
        self._settle_session(run_store, artifact_store, session_id, run_id, result, request)
        return result

    # --------------------------------------------------------------- helpers
    def _validate_model(self, profile_id: str) -> None:
        from harness.model import CredentialProvider, ModelProfileResolver

        if self.adapter_factory is not None:
            return  # deterministic test seam: no provider is contacted
        resolver = ModelProfileResolver(self.config.model_profiles_path)
        try:
            resolved = resolver.resolve(profile_id)
            resolver.validate_live(resolved)
        except Exception as exc:
            raise EvaluatorInputError(getattr(exc, "code", "INVALID_MODEL_PROFILE"), str(exc)) from exc
        try:
            CredentialProvider().get_ai_api_key()
        except Exception as exc:
            raise EvaluatorInputError("MODEL_AUTH_MISSING", "AI_API_KEY is not set in the host environment") from exc

    def _resolve_plugins(self, profile: Dict[str, Any]):
        from harness.plugins.registry import PluginError, PluginRegistry

        try:
            return PluginRegistry(allowed_capabilities=profile["allowed_plugin_capabilities"]).resolve()
        except PluginError as exc:
            raise EvaluatorInputError(exc.code, str(exc)) from exc

    def _to_run_request(self, request: EvaluatorRequestV1, profile: Dict[str, Any]):
        from pydantic import ValidationError

        from harness.contracts import (
            ExecutionMode, LimitsV1, RepositoryKind, RepositoryQueryV1, RepositoryRefV1, RunRequestV1, TaskInputV1, TaskMode,
        )

        task = request.task
        if task.source_type == "direct_text":
            task_input = TaskInputV1(text=task.text)
        elif task.source_type == "issue_url":
            task_input = TaskInputV1(issue_url=task.issue_url)
        elif task.source_type == "issue_number":
            owner = task.repository_owner
            if not owner and request.repository.kind == "public_https":
                owner = request.repository.locator.removesuffix(".git").rstrip("/").split("github.com/")[-1]
            if not owner:
                raise EvaluatorInputError("INVALID_REQUEST", "issue_number needs repository_owner (owner/repo) for non-GitHub sources")
            task_input = TaskInputV1(issue_url=f"https://github.com/{owner}/issues/{task.issue_number}")
        else:
            task_input = TaskInputV1(repository_query=RepositoryQueryV1(state=task.state or "open", include_labels=task.labels))
        key = "".join(ch if ch.isalnum() or ch in "_.-" else "-" for ch in request.idempotency_key)[:120]
        try:
            return RunRequestV1(
                idempotency_key=(key + "-eval")[:128] if len(key) < 8 else key,
                task_mode=TaskMode(request.task_mode),
                execution_mode=ExecutionMode(request.execution_mode),
                repository=RepositoryRefV1(kind=RepositoryKind(request.repository.kind), locator=request.repository.locator,
                                           revision=request.repository.revision),
                task=task_input,
                limits=LimitsV1(max_tasks=1 if request.task_mode == "single_issue" else (task.max_tasks or 3)),
                runtime_profile=RUNTIME_ALIASES[request.runtime_profile],
                metadata={"evaluator_request_id": request.request_id, "release_profile": profile["name"]},
            )
        except ValidationError as exc:
            raise EvaluatorInputError("INVALID_REQUEST", "; ".join(e["msg"] for e in exc.errors()[:4])) from exc

    def _existing_session(self, run_store, artifact_store, request: EvaluatorRequestV1, request_sha: str) -> Optional[EvaluatorResultV1]:
        with run_store.get_connection() as conn:
            row = conn.execute("SELECT * FROM h_evaluator_sessions WHERE adapter_name = ? AND adapter_version = ? AND external_request_id = ?",
                               (request.adapter.name, request.adapter.version, request.request_id)).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != request_sha:
            raise EvaluatorInputError("IDEMPOTENCY_CONFLICT", "This request_id was already used with different content")
        if row["state"] == "RESULT_WRITTEN" and row["result_artifact_id"]:
            artifact = artifact_store.get_artifact_by_id(row["result_artifact_id"])
            return EvaluatorResultV1.model_validate_json((artifact_store.data_root / artifact["relative_path"]).read_text(encoding="utf-8"))
        return None  # unsettled: the idempotent PRD 1 key resumes the same run

    def _record_session(self, run_store, artifact_store, run_id: str, profile_row: str, request: EvaluatorRequestV1,
                        raw: bytes, request_sha: str) -> str:
        with run_store.get_connection() as conn:
            row = conn.execute("SELECT evaluator_session_id FROM h_evaluator_sessions WHERE run_id = ?", (run_id,)).fetchone()
        if row:
            return row["evaluator_session_id"]
        artifact_store.write_bytes(run_id, "prd6/evaluator/request.json", raw, "application/json", "evaluator_request")
        artifact = artifact_store.get_artifact_by_path(run_id, "prd6/evaluator/request.json")
        session_id = f"esess_{uuid.uuid4().hex[:16]}"
        result_hash = hashlib.sha256((request.result_path or "").encode()).hexdigest()
        with run_store.get_connection() as conn:
            with conn:
                conn.execute("INSERT INTO h_evaluator_sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 'ACCEPTED', ?, NULL)",
                             (session_id, run_id, profile_row, request.request_id, request.adapter.name, request.adapter.version,
                              request.schema_version, "1.0", artifact["artifact_id"], request_sha, result_hash, _now()))
                append_event_sql(conn, run_id, "EVALUATOR_REQUEST_ACCEPTED", {"evaluator_session_id": session_id,
                                                                               "request_id": request.request_id}, "PRD6", "PRD6", _now())
        return session_id

    @staticmethod
    def _session_state(run_store, session_id: str, state: str) -> None:
        with run_store.get_connection() as conn:
            with conn:
                conn.execute("UPDATE h_evaluator_sessions SET state = ? WHERE evaluator_session_id = ?", (state, session_id))

    def _settle_session(self, run_store, artifact_store, session_id: str, run_id: str, result: EvaluatorResultV1,
                        request: EvaluatorRequestV1) -> None:
        path = "prd6/evaluator/result.json"
        data = (json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True) + "\n").encode()
        version = 1
        while artifact_store.get_artifact_by_path(run_id, path):
            version += 1
            path = f"prd6/evaluator/result-v{version}.json"
        artifact_store.write_bytes(run_id, path, data, "application/json", "evaluator_result")
        artifact = artifact_store.get_artifact_by_path(run_id, path)
        if request.result_path:
            write_result_atomic(result, request.result_path)
        with run_store.get_connection() as conn:
            with conn:
                conn.execute("UPDATE h_evaluator_sessions SET state = 'RESULT_WRITTEN', result_artifact_id = ?, result_sha256 = ?, settled_at = ? "
                             "WHERE evaluator_session_id = ?", (artifact["artifact_id"], artifact["sha256"], _now(), session_id))
                append_event_sql(conn, run_id, "EVALUATOR_RESULT_WRITTEN", {"status": result.status, "exit_code": result.exit_code},
                                 "PRD6", "PRD6", _now())
