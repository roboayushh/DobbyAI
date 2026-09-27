"""Turn a settled action into refreshed context for the next coder turn.

After an accepted mutation the old revision's evidence and summaries for the
changed paths are invalidated, the new workspace version is re-indexed
(unchanged files reuse their parse results), and a bounded proposal/result pair
is produced for the next coder packet. Raw ``run()`` writes are covered because
the change set comes from the host manifest, not from helper-tool reports.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from harness.persistence import RunStore
from harness.retrieval import EvidenceStore, RepositoryIndexer


@dataclass(frozen=True)
class RefreshResult:
    invalidated_evidence: int
    indexed_files: int
    symbols: int
    reused: bool


class ExecutionFeedbackService:
    def __init__(self, run_store: RunStore, indexer: RepositoryIndexer, evidence_store: EvidenceStore) -> None:
        self.run_store = run_store
        self.indexer = indexer
        self.evidence_store = evidence_store

    def refresh_context(
        self,
        run_id: str,
        task_id: str,
        *,
        old_revision: str,
        new_revision: str,
        changed_paths: Sequence[str],
        action_id: Optional[str] = None,
    ) -> RefreshResult:
        invalidated = 0
        if old_revision != new_revision and changed_paths:
            invalidated = self.evidence_store.invalidate(list(changed_paths), old_revision, new_revision)
            self.run_store.append_event(
                run_id,
                "CONTEXT_INVALIDATED",
                {
                    "task_id": task_id,
                    "action_id": action_id,
                    "old_revision": old_revision,
                    "new_revision": new_revision,
                    "changed_paths": sorted(changed_paths)[:200],
                    "invalidated_evidence": invalidated,
                },
            )
        result = self.indexer.build(run_id, new_revision, reuse_from_revision=old_revision)
        self.run_store.append_event(
            run_id,
            "CONTEXT_REINDEXED",
            {
                "task_id": task_id,
                "source_revision": new_revision,
                "indexed_files": result.indexed_files,
                "symbols": result.symbol_count,
                "reused": result.reused,
            },
        )
        return RefreshResult(invalidated, result.indexed_files, result.symbol_count, result.reused)

    @staticmethod
    def history(records: Sequence[Dict[str, Any]], limit: int = 6) -> List[Dict[str, Any]]:
        """Most recent complete action-result pairs, oldest first."""
        return [dict(record) for record in list(records)[-limit:]]
