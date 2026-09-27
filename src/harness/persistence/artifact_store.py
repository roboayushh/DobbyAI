"""harness/persistence/artifact_store.py
Atomic, content-hashed artifact storage with database metadata tracking and integrity verification.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from harness.contracts import ArtifactSummaryV1
from harness.persistence.run_store import RunStore, canonical_json


class PathTraversalError(Exception):
    pass


class IntegrityError(Exception):
    pass


ALLOWED_ARTIFACT_KINDS = frozenset(
    {
        "request",
        "result",
        "source_manifest",
        "task_manifest",
        "event_export",
        "diagnostic",
        "repository_map",
        "index_diagnostic",
        "evidence",
        "context_packet",
        "model_request",
        "model_response",
        "plan",
        "summary",
        "action_proposal",
        "generated_action",
        "role_schema_error",
        # PRD 3: secure action sandbox
        "action_code",
        "execution_request",
        "context_manifest",
        "action_stdout",
        "action_stderr",
        "action_result",
        "tool_events",
        "workspace_manifest",
        "workspace_diff",
        "sandbox_diagnostic",
        "runtime_setup_log",
        "approval_consequence",
        "candidate_manifest",
        "candidate_diff",
        "execution_result",
        "verification_handoff",
        "policy_snapshot",
        # PRD 4: verification, feedback, recovery
        "verification_contract",
        "baseline_stdout",
        "baseline_stderr",
        "baseline_report",
        "check_stdout",
        "check_stderr",
        "check_report",
        "verification_environment_manifest",
        "test_overlay",
        "validator_review",
        "diff_scope_review",
        "regression_comparison",
        "repair_feedback",
        "verification_report",
        "completion_decision",
        "verified_task_handoff",
        # PRD 5: queue and private Git workflow
        "queue_policy",
        "queue_classification_proposal",
        "queue_plan",
        "queue_dag",
        "queue_cycle_report",
        "queue_progress",
        "task_start_manifest",
        "task_ref_manifest",
        "task_checkpoint_manifest",
        "task_commit_manifest",
        "task_commit_diff",
        "integration_intent",
        "integration_diff",
        "integration_result",
        "affected_contract_set",
        "post_advance_verification_report",
        "aggregate_contract_set",
        "aggregate_verification_report",
        "queue_final_result",
        "queue_report",
        "final_patch_input",
        "evaluation_case_result",
        "recovery_report",
        "source_integrity_report",
        "release_candidate_handoff",
        # PRD 6
        "evaluator_request",
        "evaluator_result",
        "release_profile",
        "effective_configuration",
        "plugin_manifest",
        "plugin_configuration_schema",
        "plugin_set_lock",
        "plugin_review",
        "plugin_self_check",
        "export_request",
        "export_patch",
        "export_manifest",
        "export_round_trip_report",
        "release_report",
        "capability_request",
        "approval_summary",
        "approval_grant",
        "application_plan",
        "application_journal",
        "application_backup",
        "publication_candidate_report",
        "external_effect_intent",
        "external_effect_observation",
        "external_effect_receipt",
        "retention_policy",
        "cleanup_plan",
        "cleanup_report",
        "reproducibility_manifest",
        "replay_report",
        "doctor_report",
        "release_gate_evidence",
        "sbom",
        "third_party_notices",
        "comparative_evaluation_report",
        "export_evidence",
    }
)


class ArtifactStore:
    def __init__(self, data_root: str, run_store: RunStore):
        self.data_root = Path(data_root).resolve()
        self.run_store = run_store

    def _safe_artifact_path(self, run_id: str, relative_path: str) -> Path:
        if "\0" in relative_path or ".." in relative_path or os.path.isabs(relative_path):
            raise PathTraversalError(f"Unsafe artifact relative path: {relative_path}")

        run_artifacts_dir = (self.data_root / "runs" / run_id / "artifacts").resolve()
        target = (run_artifacts_dir / relative_path).resolve()

        try:
            target.relative_to(run_artifacts_dir)
        except ValueError:
            raise PathTraversalError(f"Path escapes artifact root: {relative_path}")

        return target

    def write_bytes(
        self,
        run_id: str,
        relative_path: str,
        content: bytes,
        media_type: str,
        kind: str,
        task_id: Optional[str] = None,
    ) -> ArtifactSummaryV1:
        if kind not in ALLOWED_ARTIFACT_KINDS:
            raise ValueError(f"Unregistered artifact kind: {kind}")

        target_path = self._safe_artifact_path(run_id, relative_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)

        temp_dir = (self.data_root / "runs" / run_id / "temp").resolve()
        temp_dir.mkdir(parents=True, exist_ok=True)

        # Compute SHA-256 and byte size
        sha256_hash = hashlib.sha256(content).hexdigest()
        byte_size = len(content)

        # Database relative path relative to data_root
        db_relpath = str(target_path.relative_to(self.data_root))

        # Application-level write-once rule. An identical replay is idempotent,
        # while any attempt to replace bytes or metadata fails closed.
        with self.run_store.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM h_artifacts WHERE run_id = ? AND relative_path = ?",
                (run_id, db_relpath),
            ).fetchone()
        if row:
            existing = dict(row)
            if (
                existing["sha256"] != sha256_hash
                or existing["byte_size"] != byte_size
                or existing["kind"] != kind
                or existing["media_type"] != media_type
                or existing["task_id"] != task_id
                or not self.verify(run_id, relative_path)
            ):
                raise IntegrityError(f"Artifact path is immutable: {relative_path}")
            return ArtifactSummaryV1(
                kind=existing["kind"],
                sha256=existing["sha256"],
                relative_path=existing["relative_path"],
            )

        # Persist bytes through a same-filesystem temporary file and atomic hard
        # link. Unlike os.replace(), this cannot overwrite an existing artifact.
        temp_file = temp_dir / f"{uuid.uuid4().hex}.tmp"
        try:
            with open(temp_file, "xb") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.link(temp_file, target_path)
            except FileExistsError:
                existing_bytes = target_path.read_bytes()
                if existing_bytes != content:
                    raise IntegrityError(f"Untracked artifact conflicts with {relative_path}")
        finally:
            try:
                temp_file.unlink()
            except FileNotFoundError:
                pass

        artifact_id = f"art_{uuid.uuid4().hex[:16]}"
        now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()

        try:
            with self.run_store.get_connection() as conn:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO h_artifacts (
                            artifact_id, run_id, task_id, kind, relative_path,
                            media_type, byte_size, sha256, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            artifact_id,
                            run_id,
                            task_id,
                            kind,
                            db_relpath,
                            media_type,
                            byte_size,
                            sha256_hash,
                            now_utc,
                        ),
                    )
        except sqlite3.IntegrityError as exc:
            with self.run_store.get_connection() as conn:
                raced = conn.execute(
                    "SELECT * FROM h_artifacts WHERE run_id = ? AND relative_path = ?",
                    (run_id, db_relpath),
                ).fetchone()
            if not raced or raced["sha256"] != sha256_hash:
                raise IntegrityError(f"Artifact metadata conflict: {relative_path}") from exc

        return ArtifactSummaryV1(
            kind=kind,
            sha256=sha256_hash,
            relative_path=db_relpath,
        )

    def write_json(
        self,
        run_id: str,
        relative_path: str,
        data: Any,
        kind: str,
        task_id: Optional[str] = None,
    ) -> ArtifactSummaryV1:
        json_str = canonical_json(data)
        return self.write_bytes(
            run_id=run_id,
            relative_path=relative_path,
            content=json_str.encode("utf-8"),
            media_type="application/json",
            kind=kind,
            task_id=task_id,
        )

    def verify(self, run_id: str, relative_path: str) -> bool:
        """Verify the integrity of a stored artifact against database metadata."""
        target_path = self._safe_artifact_path(run_id, relative_path)
        if not target_path.exists():
            return False

        db_relpath = str(target_path.relative_to(self.data_root))
        with self.run_store.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT byte_size, sha256 FROM h_artifacts WHERE run_id = ? AND relative_path = ?",
                (run_id, db_relpath),
            )
            row = cursor.fetchone()
            if not row:
                return False

            expected_size, expected_hash = row["byte_size"], row["sha256"]

        with open(target_path, "rb") as f:
            actual_content = f.read()

        if len(actual_content) != expected_size:
            return False

        actual_hash = hashlib.sha256(actual_content).hexdigest()
        return actual_hash.lower() == expected_hash.lower()

    def open_readonly(self, run_id: str, relative_path: str) -> bytes:
        target_path = self._safe_artifact_path(run_id, relative_path)
        if not target_path.exists():
            raise FileNotFoundError(f"Artifact not found: {relative_path}")
        with open(target_path, "rb") as f:
            return f.read()

    def get_artifacts(self, run_id: str) -> List[Dict[str, Any]]:
        with self.run_store.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM h_artifacts WHERE run_id = ? ORDER BY created_at ASC",
                (run_id,),
            )
            return [dict(r) for r in cursor.fetchall()]

    def get_artifact_by_id(self, artifact_id: str) -> Optional[Dict[str, Any]]:
        with self.run_store.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM h_artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_artifact_by_path(self, run_id: str, relative_path: str) -> Optional[Dict[str, Any]]:
        target_path = self._safe_artifact_path(run_id, relative_path)
        db_relpath = str(target_path.relative_to(self.data_root))
        with self.run_store.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM h_artifacts WHERE run_id = ? AND relative_path = ?",
                (run_id, db_relpath),
            ).fetchone()
            return dict(row) if row else None
