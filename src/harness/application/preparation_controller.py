"""harness/application/preparation_controller.py
PreparationController – orchestrates the full preparation lifecycle deterministically.
"""
from __future__ import annotations

import datetime
import json
import logging
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from harness.contracts import (
    ArtifactSummaryV1,
    ErrorDetailsV1,
    ErrorResultV1,
    PreparedRunResultV1,
    QueueSummaryV1,
    RepositoryKind,
    RunRequestV1,
    RunState,
    SourceIdentityV1,
    SourceSummaryV1,
    TaskMode,
    TaskSpecV1,
    TaskSummaryV1,
    WorkspaceSummaryV1,
)
from harness.intake import ExistingIssueIntakePort, TaskPreparationService
from harness.persistence import (
    ArtifactStore,
    IdempotencyConflictError,
    RunStore,
    canonical_json,
)
from harness.repository import (
    GitCommandError,
    LimitsExceededError,
    RepositoryService,
    SourceChangedDuringImportError,
    SourcePolicyError,
)
from harness.workspace import WorkspaceManager, WorkspacePolicyError

logger = logging.getLogger(__name__)


class PreparationError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}


class PreparationController:
    def __init__(
        self,
        run_store: RunStore,
        artifact_store: ArtifactStore,
        workspace_manager: WorkspaceManager,
        repository_service: RepositoryService,
        task_prep_service: TaskPreparationService,
        data_root: str,
    ):
        self.run_store = run_store
        self.artifact_store = artifact_store
        self.workspace_mgr = workspace_manager
        self.repo_service = repository_service
        self.task_prep_service = task_prep_service
        self.data_root = Path(data_root).resolve()

    def _extract_repo_owner_name(self, request: RunRequestV1) -> Tuple[str, str]:
        """Extract owner and repo name from locator."""
        loc = request.repository.locator
        if request.repository.kind == RepositoryKind.PUBLIC_HTTPS:
            parsed = urlparse(loc)
            parts = [p for p in parsed.path.strip("/").split("/") if p]
            if len(parts) >= 2:
                repo_name = parts[1].removesuffix(".git")
                return parts[0], repo_name
        # Fallback for local git
        loc_path = Path(loc)
        return "local", loc_path.name

    def prepare(self, request: RunRequestV1) -> PreparedRunResultV1:
        run_id = f"run_{uuid.uuid4().hex[:16]}"
        is_new = False

        # Step 1: Create or fetch run (idempotent)
        try:
            actual_run_id, is_new = self.run_store.create_run(run_id, request)
            run_id = actual_run_id
        except IdempotencyConflictError as e:
            raise PreparationError(
                code="IDEMPOTENCY_CONFLICT",
                message=str(e),
                retryable=False,
            )

        # If not new, check if already completed
        if not is_new:
            run_row = self.run_store.get_run(run_id)
            if run_row and run_row["state"] == RunState.PREPARED.value:
                # Load result.json artifact
                try:
                    result_bytes = self.artifact_store.open_readonly(run_id, "result.json")
                    return PreparedRunResultV1.model_validate_json(result_bytes)
                except Exception:
                    logger.warning("Run %s recorded as PREPARED but result artifact missing; re-preparing", run_id)
            elif run_row and run_row["state"] in (
                RunState.BLOCKED.value,
                RunState.FAILED.value,
                RunState.CANCELLED.value,
            ):
                raise PreparationError(
                    code=run_row.get("error_code") or "RUN_FAILED",
                    message=f"Replayed run {run_id} is in terminal failure state: {run_row['state']}",
                    retryable=False,
                )

        current_state = RunState.NEW
        try:
            # Step 2: Transition to VALIDATING
            self.run_store.transition(
                run_id=run_id,
                from_state=RunState.NEW,
                to_state=RunState.VALIDATING,
                event_type="REQUEST_VALIDATED",
                payload={"run_id": run_id, "task_mode": request.task_mode.value},
            )
            current_state = RunState.VALIDATING

            # Step 3: Freeze task(s)
            tasks: List[TaskSpecV1] = []
            queue_summary: QueueSummaryV1
            task_manifest_meta: Dict[str, Any] = {}

            if request.task_mode == TaskMode.SINGLE_ISSUE:
                single_task, queue_summary = self.task_prep_service.prepare_single(request.task)
                tasks = [single_task]
                task_manifest_meta = {
                    "mode": "single_issue",
                    "task_id": single_task.task_id,
                    "source_key": single_task.source_key,
                }
                self.run_store.append_event(
                    run_id=run_id,
                    event_type="TASK_INPUT_RESOLVED",
                    payload={"task_id": single_task.task_id, "source_key": single_task.source_key},
                )
            else:
                owner, repo_name = self._extract_repo_owner_name(request)
                tasks, queue_summary, task_manifest_meta = self.task_prep_service.prepare_queue(
                    owner=owner,
                    repo=repo_name,
                    query=request.task.repository_query,  # type: ignore[arg-type]
                    limits=request.limits,
                )
                self.run_store.append_event(
                    run_id=run_id,
                    event_type="QUEUE_SELECTED",
                    payload={
                        "selected_count": queue_summary.selected,
                        "discovered_count": queue_summary.discovered,
                    },
                )

            # Step 4: Transition to ACQUIRING
            self.run_store.transition(
                run_id=run_id,
                from_state=RunState.VALIDATING,
                to_state=RunState.ACQUIRING,
                event_type="SOURCE_ACQUISITION_STARTED",
                payload={"locator": request.repository.locator, "kind": request.repository.kind.value},
            )
            current_state = RunState.ACQUIRING

            run_dir, bare_repo_dir, worktree_dir = self.workspace_mgr.allocate(run_id)
            staging_dir = run_dir / "temp" / "staging"

            source_identity, import_manifest, total_bytes, file_count = self.repo_service.acquire(
                ref=request.repository,
                private_bare_repo=bare_repo_dir,
                staging_dir=staging_dir,
                run_id=run_id,
                limits=request.limits,
            )

            # Verify expected_tree_sha256 if specified
            if request.repository.expected_tree_sha256:
                if source_identity.content_tree_sha256 != request.repository.expected_tree_sha256:
                    raise PreparationError(
                        code="TREE_SHA_MISMATCH",
                        message=f"Tree SHA mismatch: expected {request.repository.expected_tree_sha256}, got {source_identity.content_tree_sha256}",
                    )

            self.run_store.append_event(
                run_id=run_id,
                event_type="SOURCE_ACQUIRED",
                payload={
                    "baseline_commit": source_identity.baseline_commit,
                    "content_tree_sha256": source_identity.content_tree_sha256,
                    "dirty_source_imported": source_identity.dirty_source_imported,
                },
            )

            # Step 5: Transition to PREPARING
            self.run_store.transition(
                run_id=run_id,
                from_state=RunState.ACQUIRING,
                to_state=RunState.PREPARING,
                event_type="SOURCE_IMPORTED",
                payload={"baseline_commit": source_identity.baseline_commit},
            )
            current_state = RunState.PREPARING

            # Finalize workspace (checkout and make read-only)
            self.workspace_mgr.finalize(run_id, source_identity.baseline_commit)

            snapshot_id = self.run_store.put_source_snapshot(
                snapshot=source_identity,
                run_id=run_id,
                repo_bytes=total_bytes,
                file_count=file_count,
            )

            workspace_id = f"wsp_{uuid.uuid4().hex[:16]}"
            rel_worktree = str(worktree_dir.relative_to(self.data_root))
            rel_bare = str(bare_repo_dir.relative_to(self.data_root))
            rel_root = str(run_dir.relative_to(self.data_root))

            self.run_store.put_workspace(
                run_id=run_id,
                workspace_id=workspace_id,
                source_snapshot_id=snapshot_id,
                root_relpath=rel_root,
                bare_repo_relpath=rel_bare,
                worktree_relpath=rel_worktree,
                state="READY",
                writable=False,
            )

            self.run_store.append_event(
                run_id=run_id,
                event_type="WORKSPACE_READY",
                payload={"workspace_id": workspace_id, "relative_root": rel_worktree},
            )

            # Persist tasks
            self.run_store.put_tasks(run_id, tasks)

            # Write required artifacts
            # 1. request.json
            art_req = self.artifact_store.write_json(
                run_id=run_id,
                relative_path="request.json",
                data=request.model_dump(mode="json"),
                kind="request",
            )
            self.run_store.append_event(
                run_id=run_id,
                event_type="ARTIFACT_WRITTEN",
                payload={"kind": "request", "sha256": art_req.sha256},
            )

            # 2. source-manifest.json
            art_src = self.artifact_store.write_json(
                run_id=run_id,
                relative_path="source-manifest.json",
                data=import_manifest,
                kind="source_manifest",
            )
            self.run_store.append_event(
                run_id=run_id,
                event_type="ARTIFACT_WRITTEN",
                payload={"kind": "source_manifest", "sha256": art_src.sha256},
            )

            # 3. task-manifest.json
            task_manifest = {
                "run_id": run_id,
                "task_mode": request.task_mode.value,
                "metadata": task_manifest_meta,
                "summary": queue_summary.model_dump(mode="json"),
                "tasks": [t.model_dump(mode="json") for t in tasks],
            }
            art_tsk = self.artifact_store.write_json(
                run_id=run_id,
                relative_path="task-manifest.json",
                data=task_manifest,
                kind="task_manifest",
            )
            self.run_store.append_event(
                run_id=run_id,
                event_type="ARTIFACT_WRITTEN",
                payload={"kind": "task_manifest", "sha256": art_tsk.sha256},
            )

            # 4. events.ndjson
            all_events = self.run_store.get_events(run_id)
            events_ndjson_text = "\n".join(canonical_json(e) for e in all_events) + "\n"
            art_evt = self.artifact_store.write_bytes(
                run_id=run_id,
                relative_path="events.ndjson",
                content=events_ndjson_text.encode("utf-8"),
                media_type="application/x-ndjson",
                kind="event_export",
            )

            # Build result contract
            task_summaries = [
                TaskSummaryV1(task_id=t.task_id, ordinal=t.ordinal, state="QUEUED")  # type: ignore[arg-type]
                for t in tasks
            ]
            now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()

            artifacts_list = [art_req, art_src, art_tsk, art_evt]

            # 5. result.json
            result_obj = PreparedRunResultV1(
                schema_version="1.0",
                run_id=run_id,
                status="PREPARED",
                task_mode=request.task_mode,
                execution_mode=request.execution_mode,
                source=SourceSummaryV1(
                    upstream_commit=source_identity.upstream_commit,
                    baseline_commit=source_identity.baseline_commit,
                    content_tree_sha256=source_identity.content_tree_sha256,
                ),
                workspace=WorkspaceSummaryV1(
                    workspace_id=workspace_id,
                    relative_root=rel_worktree,
                    writable=False,
                ),
                tasks=task_summaries,
                queue_summary=queue_summary,
                artifacts=artifacts_list,
                created_at=now_utc,
            )

            art_res = self.artifact_store.write_json(
                run_id=run_id,
                relative_path="result.json",
                data=result_obj.model_dump(mode="json"),
                kind="result",
            )
            artifacts_list.append(art_res)
            result_obj.artifacts = artifacts_list

            # Verify all artifacts
            for art in artifacts_list:
                rel_name = Path(art.relative_path).name
                if not self.artifact_store.verify(run_id, rel_name):
                    raise PreparationError(
                        code="ARTIFACT_INTEGRITY_FAILURE",
                        message=f"Artifact failed integrity verification: {art.relative_path}",
                    )

            # Step 6: Transition to PREPARED
            self.run_store.transition(
                run_id=run_id,
                from_state=RunState.PREPARING,
                to_state=RunState.PREPARED,
                event_type="RUN_PREPARED",
                payload={"run_id": run_id, "status": "PREPARED"},
            )

            return result_obj

        except KeyboardInterrupt:
            logger.warning("Preparation interrupted by user")
            self.workspace_mgr.destroy_partial(run_id)
            if current_state in (RunState.NEW, RunState.VALIDATING, RunState.ACQUIRING, RunState.PREPARING):
                self.run_store.transition(
                    run_id=run_id,
                    from_state=current_state,
                    to_state=RunState.CANCELLED,
                    event_type="RUN_CANCELLED",
                    payload={"reason": "User interrupt"},
                )
            raise

        except (SourcePolicyError, LimitsExceededError, WorkspacePolicyError) as e:
            logger.error("Preparation blocked by policy/limits: %s", e)
            self.workspace_mgr.destroy_partial(run_id)
            err_code = "POLICY_VIOLATION" if isinstance(e, (SourcePolicyError, WorkspacePolicyError)) else "LIMITS_EXCEEDED"
            if current_state in (RunState.VALIDATING, RunState.ACQUIRING):
                self.run_store.transition(
                    run_id=run_id,
                    from_state=current_state,
                    to_state=RunState.BLOCKED,
                    event_type="RUN_BLOCKED",
                    payload={"error": str(e), "code": err_code},
                    error_code=err_code,
                )
            raise PreparationError(code=err_code, message=str(e), retryable=False)

        except Exception as e:
            logger.exception("Preparation failed: %s", e)
            self.workspace_mgr.quarantine(run_id)
            err_code = getattr(e, "code", "INTERNAL_FAILURE")
            if current_state in (RunState.ACQUIRING, RunState.PREPARING):
                self.run_store.transition(
                    run_id=run_id,
                    from_state=current_state,
                    to_state=RunState.FAILED,
                    event_type="RUN_FAILED",
                    payload={"error": str(e), "code": err_code},
                    error_code=err_code,
                )
            elif current_state == RunState.VALIDATING:
                self.run_store.transition(
                    run_id=run_id,
                    from_state=current_state,
                    to_state=RunState.BLOCKED,
                    event_type="RUN_BLOCKED",
                    payload={"error": str(e), "code": err_code},
                    error_code=err_code,
                )
            raise PreparationError(code=err_code, message=str(e), retryable=getattr(e, "retryable", False))

    def reconcile_interrupted(self) -> List[Dict[str, Any]]:
        """Reconcile nonterminal runs after crash/restart per PRD section 13.3."""
        nonterminal = self.run_store.get_nonterminal_runs()
        reconciled = []

        for run in nonterminal:
            run_id = run["run_id"]
            state = run["state"]
            logger.info("Reconciling interrupted run %s in state %s", run_id, state)

            if state in (RunState.NEW.value, RunState.VALIDATING.value):
                # Mark FAILED with INTERRUPTED_BEFORE_ACQUISITION
                self.run_store.transition(
                    run_id=run_id,
                    from_state=RunState(state),
                    to_state=RunState.FAILED,
                    event_type="RUN_RECONCILED",
                    payload={"outcome": "FAILED", "code": "INTERRUPTED_BEFORE_ACQUISITION"},
                    error_code="INTERRUPTED_BEFORE_ACQUISITION",
                )
                self.workspace_mgr.destroy_partial(run_id)
                reconciled.append({"run_id": run_id, "action": "marked_failed"})

            elif state == RunState.ACQUIRING.value:
                # Quarantine staging content
                self.workspace_mgr.quarantine(run_id)
                self.run_store.transition(
                    run_id=run_id,
                    from_state=RunState.ACQUIRING,
                    to_state=RunState.FAILED,
                    event_type="RUN_RECONCILED",
                    payload={"outcome": "FAILED", "code": "INTERRUPTED_DURING_ACQUISITION"},
                    error_code="INTERRUPTED_DURING_ACQUISITION",
                )
                reconciled.append({"run_id": run_id, "action": "quarantined_and_failed"})

            elif state == RunState.PREPARING.value:
                # Check if everything was written and verified
                has_snapshot = self.run_store.get_source_snapshot(run_id) is not None
                has_workspace = self.run_store.get_workspace(run_id) is not None
                has_tasks = len(self.run_store.get_tasks(run_id)) > 0
                has_result_art = self.artifact_store.verify(run_id, "result.json")

                if has_snapshot and has_workspace and has_tasks and has_result_art:
                    # Advance to PREPARED
                    self.run_store.transition(
                        run_id=run_id,
                        from_state=RunState.PREPARING,
                        to_state=RunState.PREPARED,
                        event_type="RUN_RECONCILED",
                        payload={"outcome": "PREPARED"},
                    )
                    reconciled.append({"run_id": run_id, "action": "advanced_to_prepared"})
                else:
                    self.workspace_mgr.quarantine(run_id)
                    self.run_store.transition(
                        run_id=run_id,
                        from_state=RunState.PREPARING,
                        to_state=RunState.FAILED,
                        event_type="RUN_RECONCILED",
                        payload={"outcome": "FAILED", "code": "INCOMPLETE_PREPARATION"},
                        error_code="INCOMPLETE_PREPARATION",
                    )
                    reconciled.append({"run_id": run_id, "action": "quarantined_and_failed"})

        return reconciled

    def continue_prepared_run(
        self,
        run_id: str,
        orchestration_controller: Any,
        stop_at: str = "action-proposed",
    ) -> Any:
        """Delegate a verified PREPARED run into the PRD 2 controller."""
        run = self.run_store.get_run(run_id)
        if not run:
            raise KeyError(f"Run {run_id} not found")
        if run["state"] != RunState.PREPARED.value:
            raise ValueError(f"Run {run_id} is not PREPARED (state={run['state']})")

        if orchestration_controller is None:
            raise PreparationError(
                code="ORCHESTRATION_CONTROLLER_REQUIRED",
                message="A configured PRD 2 orchestration controller is required",
            )
        return orchestration_controller.continue_run(run_id, stop_at=stop_at)
