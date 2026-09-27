"""Structured deterministic summaries that preserve truth labels and provenance."""
from __future__ import annotations

import datetime
import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Sequence

from harness.persistence import ArtifactStore, RunStore, canonical_json
from harness.retrieval import EvidenceStore


@dataclass(frozen=True)
class WorkingSummary:
    summary_id: str
    artifact_id: str
    content_sha256: str
    content: Dict[str, Any]


class WorkingMemoryStore:
    METHOD_VERSION = "deterministic-summary@1.0"

    def __init__(
        self,
        run_store: RunStore,
        artifact_store: ArtifactStore,
        evidence_store: EvidenceStore,
    ) -> None:
        self.run_store = run_store
        self.artifact_store = artifact_store
        self.evidence_store = evidence_store

    def summarize(
        self,
        *,
        run_id: str,
        task_id: str,
        source_revision: str,
        evidence_ids: Sequence[str],
        history: Sequence[Mapping[str, Any]] = (),
    ) -> WorkingSummary:
        partitions: Dict[str, List[Dict[str, Any]]] = {
            "observed_facts": [],
            "reported_facts": [],
            "hypotheses": [],
        }
        for evidence_id in sorted(set(evidence_ids)):
            row = self.evidence_store.get(evidence_id)
            if not row["valid"] or row["source_revision"] != source_revision:
                continue
            record = {
                "evidence_id": evidence_id,
                "path": row["relative_path"],
                "lines": [row["start_line"], row["end_line"]],
                "statement": row["content"][:2000],
                "content_sha256": row["content_sha256"],
            }
            key = {
                "observed": "observed_facts",
                "reported": "reported_facts",
                "hypothesis": "hypotheses",
            }[row["truth_status"]]
            partitions[key].append(record)
        failed = []
        questions = []
        for record in history:
            kind = record.get("kind")
            if kind in {"failed_approach", "failed_action"}:
                failed.append(dict(record))
            elif kind in {"open_question", "needs_input"}:
                questions.append(dict(record))
        content = {
            "schema_version": "1.0",
            "source_revision": source_revision,
            **partitions,
            "failed_approaches": failed,
            "open_questions": questions,
            "provenance": {
                "evidence_ids": sorted(set(evidence_ids)),
                "method": "deterministic",
                "method_version": self.METHOD_VERSION,
            },
        }
        content_json = canonical_json(content)
        content_sha = hashlib.sha256(content_json.encode("utf-8")).hexdigest()
        summary_id = f"sum_{uuid.uuid4().hex[:16]}"
        artifact_path = f"prd2/summaries/{task_id}/{summary_id}.json"
        self.artifact_store.write_bytes(
            run_id,
            artifact_path,
            content_json.encode("utf-8"),
            "application/json",
            "summary",
            task_id,
        )
        artifact = self.artifact_store.get_artifact_by_path(run_id, artifact_path)
        if not artifact:
            raise RuntimeError("Summary artifact metadata was not persisted")
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self.run_store.get_connection() as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO h_summaries(
                        summary_id, run_id, task_id, source_revision, method,
                        method_version, input_refs_json, artifact_id,
                        content_sha256, valid, created_at
                    ) VALUES (?, ?, ?, ?, 'deterministic', ?, ?, ?, ?, 1, ?)
                    """,
                    (
                        summary_id,
                        run_id,
                        task_id,
                        source_revision,
                        self.METHOD_VERSION,
                        canonical_json(sorted(set(evidence_ids))),
                        artifact["artifact_id"],
                        content_sha,
                        now,
                    ),
                )
        return WorkingSummary(summary_id, artifact["artifact_id"], content_sha, content)

    def get(self, summary_id: str) -> WorkingSummary:
        with self.run_store.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM h_summaries WHERE summary_id = ?", (summary_id,)
            ).fetchone()
        if not row or not row["valid"]:
            raise KeyError(f"Valid summary not found: {summary_id}")
        artifact = self.artifact_store.get_artifact_by_id(row["artifact_id"])
        if not artifact:
            raise RuntimeError("Summary artifact is missing")
        relative = artifact["relative_path"].split("/artifacts/", 1)[-1]
        if not self.artifact_store.verify(row["run_id"], relative):
            raise RuntimeError("Summary artifact failed verification")
        raw = self.artifact_store.open_readonly(row["run_id"], relative)
        if hashlib.sha256(raw).hexdigest() != row["content_sha256"]:
            raise RuntimeError("Summary content hash mismatch")
        return WorkingSummary(
            row["summary_id"], row["artifact_id"], row["content_sha256"], json.loads(raw)
        )
