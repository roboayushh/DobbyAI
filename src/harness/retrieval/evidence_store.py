"""Versioned evidence records with read-time integrity verification."""
from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from harness.contracts import EvidenceRefV1, TruthStatus
from harness.persistence import ArtifactStore, RunStore, canonical_json


class EvidenceIntegrityError(RuntimeError):
    code = "EVIDENCE_INTEGRITY_FAILURE"


class EvidenceStore:
    def __init__(
        self,
        run_store: RunStore,
        artifact_store: ArtifactStore,
        workspace_root_resolver: Any,
    ) -> None:
        self.run_store = run_store
        self.artifact_store = artifact_store
        self._workspace_root_resolver = workspace_root_resolver

    def put(
        self,
        *,
        evidence_id: str,
        run_id: str,
        task_id: str,
        source_revision: str,
        relative_path: Optional[str],
        start_line: Optional[int],
        end_line: Optional[int],
        symbol: Optional[str],
        evidence_type: str,
        retrieval_reason: str,
        truth_status: TruthStatus,
        content: str,
        parser_version: Optional[str],
        provenance: Dict[str, Any],
    ) -> EvidenceRefV1:
        content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        evidence_payload = {
            "schema_version": "1.0",
            "evidence_id": evidence_id,
            "run_id": run_id,
            "task_id": task_id,
            "source_revision": source_revision,
            "relative_path": relative_path,
            "start_line": start_line,
            "end_line": end_line,
            "content": content,
            "content_sha256": content_sha256,
            "provenance": provenance,
            "parser_version": parser_version,
        }
        artifact_path = f"prd2/evidence/{task_id}/{evidence_id}.json"
        self.artifact_store.write_json(
            run_id,
            artifact_path,
            evidence_payload,
            "evidence",
            task_id,
        )
        artifact = self.artifact_store.get_artifact_by_path(run_id, artifact_path)
        if not artifact:
            raise RuntimeError("Evidence artifact metadata was not persisted")
        provenance_json = canonical_json(
            {**provenance, "parser_version": parser_version, "artifact_sha256": artifact["sha256"]}
        )
        with self.run_store.get_connection() as conn:
            with conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO h_evidence(
                        evidence_id, run_id, task_id, source_revision, relative_path,
                        start_line, end_line, symbol, evidence_type, retrieval_reason,
                        truth_status, content_sha256, provenance_json, artifact_id,
                        valid, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                    """,
                    (
                        evidence_id,
                        run_id,
                        task_id,
                        source_revision,
                        relative_path,
                        start_line,
                        end_line,
                        symbol,
                        evidence_type,
                        retrieval_reason,
                        truth_status.value,
                        content_sha256,
                        provenance_json,
                        artifact["artifact_id"],
                        now,
                    ),
                )
        return EvidenceRefV1(
            evidence_id=evidence_id,
            run_id=run_id,
            task_id=task_id,
            source_revision=source_revision,
            path=relative_path,
            start_line=start_line,
            end_line=end_line,
            symbol=symbol,
            evidence_type=evidence_type,
            retrieval_reason=retrieval_reason,
            truth_status=truth_status,
            content_sha256=content_sha256,
            parser_version=parser_version,
            valid=True,
        )

    def get(self, evidence_id: str, *, verify: bool = True) -> Dict[str, Any]:
        with self.run_store.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM h_evidence WHERE evidence_id = ?", (evidence_id,)
            ).fetchone()
        if not row:
            raise KeyError(f"Evidence not found: {evidence_id}")
        result = dict(row)
        provenance = json.loads(result["provenance_json"])
        content = ""
        if result["artifact_id"]:
            artifact = self.artifact_store.get_artifact_by_id(result["artifact_id"])
            if not artifact:
                raise EvidenceIntegrityError("Evidence artifact metadata is missing")
            relative = artifact["relative_path"].split("/artifacts/", 1)[-1]
            if not self.artifact_store.verify(result["run_id"], relative):
                raise EvidenceIntegrityError("Evidence artifact failed integrity verification")
            artifact_payload = json.loads(
                self.artifact_store.open_readonly(result["run_id"], relative)
            )
            content = artifact_payload.get("content", "")
            artifact_provenance = artifact_payload.get("provenance")
            if artifact_provenance != {
                key: value
                for key, value in provenance.items()
                if key not in {"parser_version", "artifact_sha256"}
            }:
                raise EvidenceIntegrityError("Evidence artifact provenance mismatch")
        else:
            # Backward-compatible read path for early development fixtures.
            content = provenance.get("content", "")
        if not isinstance(content, str):
            raise EvidenceIntegrityError("Evidence provenance content is invalid")
        if hashlib.sha256(content.encode("utf-8")).hexdigest() != result["content_sha256"]:
            raise EvidenceIntegrityError("Evidence content hash mismatch")
        if verify and result["truth_status"] == "observed" and result["relative_path"]:
            self._verify_source(result, content, provenance)
        result["content"] = content
        result["provenance"] = provenance
        return result

    def list(self, run_id: str, task_id: str) -> List[Dict[str, Any]]:
        with self.run_store.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM h_evidence
                WHERE run_id = ? AND task_id = ?
                ORDER BY created_at, evidence_id
                """,
                (run_id, task_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def invalidate(
        self,
        changed_paths: List[str],
        old_workspace_version: str,
        new_workspace_version: str,
    ) -> int:
        if old_workspace_version == new_workspace_version:
            return 0
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if not changed_paths:
            return 0
        placeholders = ",".join("?" for _ in changed_paths)
        with self.run_store.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                f"""
                SELECT evidence_id FROM h_evidence
                WHERE source_revision = ? AND relative_path IN ({placeholders}) AND valid = 1
                """,
                (old_workspace_version, *changed_paths),
            ).fetchall()
            changed = conn.execute(
                f"""
                UPDATE h_evidence
                SET valid = 0, invalidated_at = ?, invalidation_reason = ?
                WHERE source_revision = ? AND relative_path IN ({placeholders}) AND valid = 1
                """,
                (
                    now,
                    f"workspace advanced to {new_workspace_version}",
                    old_workspace_version,
                    *changed_paths,
                ),
            ).rowcount
            if rows:
                evidence_ids = [row["evidence_id"] for row in rows]
                summaries = conn.execute(
                    "SELECT summary_id, input_refs_json FROM h_summaries WHERE valid = 1"
                ).fetchall()
                invalid_summaries = [
                    row["summary_id"]
                    for row in summaries
                    if any(evidence_id in row["input_refs_json"] for evidence_id in evidence_ids)
                ]
                for summary_id in invalid_summaries:
                    conn.execute(
                        "UPDATE h_summaries SET valid = 0, invalidated_at = ? WHERE summary_id = ?",
                        (now, summary_id),
                    )
            conn.commit()
        return changed

    def _verify_source(
        self, row: Dict[str, Any], content: str, provenance: Dict[str, Any]
    ) -> None:
        root: Path = self._workspace_root_resolver(row["run_id"])
        path = (root / row["relative_path"]).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise EvidenceIntegrityError("Evidence path escapes workspace") from exc
        if not path.is_file():
            raise EvidenceIntegrityError("Evidence source file is missing")
        file_bytes = path.read_bytes()
        expected_file_hash = provenance.get("file_sha256")
        if expected_file_hash and hashlib.sha256(file_bytes).hexdigest() != expected_file_hash:
            raise EvidenceIntegrityError("Evidence source file hash changed")
        if row["start_line"] is not None:
            lines = file_bytes.decode("utf-8").splitlines()
            current = "\n".join(lines[row["start_line"] - 1 : row["end_line"]])
            if current != content:
                raise EvidenceIntegrityError("Evidence source span changed")
