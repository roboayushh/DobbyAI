"""harness/persistence/artifact_store.py
Atomic, content-hashed artifact storage with database metadata tracking and integrity verification.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from harness.contracts import ArtifactSummaryV1
from harness.persistence.run_store import RunStore, canonical_json


class PathTraversalError(Exception):
    pass


class IntegrityError(Exception):
    pass


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
        target_path = self._safe_artifact_path(run_id, relative_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)

        temp_dir = (self.data_root / "runs" / run_id / "temp").resolve()
        temp_dir.mkdir(parents=True, exist_ok=True)

        # Compute SHA-256 and byte size
        sha256_hash = hashlib.sha256(content).hexdigest()
        byte_size = len(content)

        # Atomic write
        temp_file = temp_dir / f"{uuid.uuid4().hex}.tmp"
        with open(temp_file, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())

        os.replace(temp_file, target_path)

        # Record in database
        artifact_id = f"art_{uuid.uuid4().hex[:16]}"
        now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()

        # Database relative path relative to data_root
        db_relpath = str(target_path.relative_to(self.data_root))

        with self.run_store.get_connection() as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO h_artifacts (
                        artifact_id, run_id, task_id, kind, relative_path,
                        media_type, byte_size, sha256, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id, relative_path) DO UPDATE SET
                        byte_size = excluded.byte_size,
                        sha256 = excluded.sha256,
                        created_at = excluded.created_at
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
