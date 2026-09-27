"""Run-wide execution budget ledger (actions, container wall time, output, growth).

Reservations happen inside the intent transaction before a container starts
and are settled exactly once in the settlement transaction. The table's CHECK
constraints make over-reservation impossible even under concurrent writers.
"""
from __future__ import annotations

import datetime
import sqlite3
from dataclasses import dataclass
from typing import Any, Dict

from harness.persistence import RunStore

MiB = 1024 * 1024


class ExecutionBudgetExhaustedError(RuntimeError):
    code = "EXECUTION_BUDGET_EXHAUSTED"
    retryable = False


@dataclass(frozen=True)
class ExecutionBudgetLimits:
    actions_per_task: int = 12
    wall_seconds_per_task: int = 1200
    output_bytes_per_task: int = 64 * MiB
    max_workspace_growth_bytes: int = 2 * 1024 * MiB


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class ExecutionBudgetLedger:
    def __init__(self, run_store: RunStore) -> None:
        self.run_store = run_store

    def initialize(self, run_id: str, task_count: int, limits: ExecutionBudgetLimits = ExecutionBudgetLimits()) -> Dict[str, Any]:
        count = max(1, task_count)
        with self.run_store.get_connection() as conn:
            with conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO h_execution_budgets(
                        run_id, max_actions, max_wall_seconds, max_output_bytes,
                        max_workspace_growth_bytes, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        limits.actions_per_task * count,
                        limits.wall_seconds_per_task * count,
                        limits.output_bytes_per_task * count,
                        limits.max_workspace_growth_bytes,
                        _now(),
                    ),
                )
        return self.get(run_id)

    def get(self, run_id: str) -> Dict[str, Any]:
        with self.run_store.get_connection() as conn:
            row = conn.execute("SELECT * FROM h_execution_budgets WHERE run_id = ?", (run_id,)).fetchone()
        if not row:
            raise KeyError(f"Execution budget not initialized for run {run_id}")
        data = dict(row)
        data["remaining_actions"] = data["max_actions"] - data["used_actions"] - data["reserved_actions"]
        data["remaining_wall_seconds"] = data["max_wall_seconds"] - data["used_wall_seconds"] - data["reserved_wall_seconds"]
        data["remaining_output_bytes"] = data["max_output_bytes"] - data["used_output_bytes"] - data["reserved_output_bytes"]
        return data

    @staticmethod
    def reserve_sql(conn: sqlite3.Connection, run_id: str, wall_seconds: int, output_bytes: int) -> None:
        row = conn.execute("SELECT * FROM h_execution_budgets WHERE run_id = ?", (run_id,)).fetchone()
        if not row:
            raise ExecutionBudgetExhaustedError("Execution budget is not initialized")
        if row["used_actions"] + row["reserved_actions"] + 1 > row["max_actions"]:
            raise ExecutionBudgetExhaustedError("Action budget exhausted")
        if row["used_wall_seconds"] + row["reserved_wall_seconds"] + wall_seconds > row["max_wall_seconds"]:
            raise ExecutionBudgetExhaustedError("Container wall-time budget exhausted")
        if row["used_output_bytes"] + row["reserved_output_bytes"] + output_bytes > row["max_output_bytes"]:
            raise ExecutionBudgetExhaustedError("Output budget exhausted")
        conn.execute(
            """
            UPDATE h_execution_budgets
            SET reserved_actions = reserved_actions + 1,
                reserved_wall_seconds = reserved_wall_seconds + ?,
                reserved_output_bytes = reserved_output_bytes + ?,
                updated_at = ?
            WHERE run_id = ?
            """,
            (wall_seconds, output_bytes, _now(), run_id),
        )

    @staticmethod
    def settle_sql(
        conn: sqlite3.Connection,
        run_id: str,
        *,
        reserved_wall: int,
        reserved_output: int,
        used_wall: int,
        used_output: int,
        growth_bytes: int,
        charge_action: bool = True,
    ) -> None:
        used_wall = min(max(0, used_wall), reserved_wall)
        used_output = min(max(0, used_output), reserved_output)
        row = conn.execute("SELECT * FROM h_execution_budgets WHERE run_id = ?", (run_id,)).fetchone()
        growth = max(0, growth_bytes)
        growth = min(growth, row["max_workspace_growth_bytes"] - row["used_workspace_growth_bytes"])
        conn.execute(
            """
            UPDATE h_execution_budgets
            SET reserved_actions = reserved_actions - 1,
                reserved_wall_seconds = reserved_wall_seconds - ?,
                reserved_output_bytes = reserved_output_bytes - ?,
                used_actions = used_actions + ?,
                used_wall_seconds = used_wall_seconds + ?,
                used_output_bytes = used_output_bytes + ?,
                used_workspace_growth_bytes = used_workspace_growth_bytes + ?,
                updated_at = ?
            WHERE run_id = ?
            """,
            (
                reserved_wall,
                reserved_output,
                1 if charge_action else 0,
                used_wall,
                used_output,
                growth,
                _now(),
                run_id,
            ),
        )
