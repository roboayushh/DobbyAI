"""Append one ordered, hashed event inside the caller's open transaction.

State projections and their events must commit together (FR24). Lower layers
(policy, workspace, execution, verification) use this primitive so they never
depend on the orchestration package.
"""
from __future__ import annotations

import hashlib
import sqlite3
import uuid
from typing import Any, Dict, Optional

from harness.persistence.run_store import canonical_json


def append_event_sql(
    conn: sqlite3.Connection,
    run_id: str,
    event_type: str,
    payload: Dict[str, Any],
    from_state: Optional[str],
    to_state: Optional[str],
    created_at: str,
    dedupe_key: Optional[str] = None,
) -> int:
    run = conn.execute("SELECT next_event_seq FROM h_runs WHERE run_id = ?", (run_id,)).fetchone()
    if not run:
        raise KeyError(f"Run not found: {run_id}")
    seq = run[0]
    payload_json = canonical_json(payload)
    conn.execute(
        "UPDATE h_runs SET next_event_seq = ?, updated_at = ? WHERE run_id = ?",
        (seq + 1, created_at, run_id),
    )
    conn.execute(
        """
        INSERT INTO h_events(
            event_id, run_id, seq, event_type, from_state, to_state,
            payload_json, payload_sha256, dedupe_key, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"evt_{uuid.uuid4().hex[:16]}",
            run_id,
            seq,
            event_type,
            from_state,
            to_state,
            payload_json,
            hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
            dedupe_key,
            created_at,
        ),
    )
    return seq
