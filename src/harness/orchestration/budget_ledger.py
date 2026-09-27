"""Atomic reserve-before-call accounting shared by every model role."""
from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

from harness.contracts import Role
from harness.orchestration.lifecycle import LifecycleService
from harness.persistence import RunStore


class BudgetExhaustedError(RuntimeError):
    code = "MODEL_BUDGET_EXHAUSTED"
    retryable = False


class BudgetSettlementError(RuntimeError):
    code = "MODEL_BUDGET_SETTLEMENT_FAILED"


@dataclass(frozen=True)
class BudgetLimits:
    max_calls: int = 30
    max_input_tokens: int = 150_000
    max_output_tokens: int = 20_000
    max_wall_seconds: int = 1_800
    reserved_future_calls: int = 2
    reserved_future_output_tokens: int = 2_000
    reserved_future_wall_seconds: int = 120


@dataclass(frozen=True)
class Reservation:
    reservation_id: str
    call_id: str
    run_id: str
    input_tokens: int
    output_tokens: int


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class BudgetLedgerService:
    def __init__(self, run_store: RunStore) -> None:
        self.run_store = run_store

    def initialize(self, run_id: str, limits: BudgetLimits = BudgetLimits()) -> Dict[str, Any]:
        if limits.reserved_future_calls < 2:
            raise ValueError("At least two future verification calls must be reserved")
        if limits.max_calls < limits.reserved_future_calls:
            raise ValueError("Future call reserve exceeds total call budget")
        if limits.reserved_future_output_tokens > limits.max_output_tokens:
            raise ValueError("Future output-token reserve exceeds total output budget")
        if limits.reserved_future_wall_seconds >= limits.max_wall_seconds:
            raise ValueError("Future wall-time reserve leaves no orchestration time")
        now = _now()
        deadline = now + datetime.timedelta(seconds=limits.max_wall_seconds)
        with self.run_store.get_connection() as conn:
            with conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO h_budget_ledgers(
                        run_id, max_calls, max_input_tokens, max_output_tokens,
                        max_wall_seconds, reserved_future_calls,
                        reserved_future_output_tokens, reserved_future_wall_seconds,
                        started_at, deadline_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        limits.max_calls,
                        limits.max_input_tokens,
                        limits.max_output_tokens,
                        limits.max_wall_seconds,
                        limits.reserved_future_calls,
                        limits.reserved_future_output_tokens,
                        limits.reserved_future_wall_seconds,
                        now.isoformat(),
                        deadline.isoformat(),
                        now.isoformat(),
                    ),
                )
        return self.get(run_id)

    def get(self, run_id: str) -> Dict[str, Any]:
        with self.run_store.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM h_budget_ledgers WHERE run_id = ?", (run_id,)
            ).fetchone()
        if not row:
            raise KeyError(f"Budget ledger not initialized for run: {run_id}")
        result = dict(row)
        result["remaining_calls"] = result["max_calls"] - result["used_calls"] - result["reserved_calls"]
        result["remaining_input_tokens"] = (
            result["max_input_tokens"]
            - result["used_input_tokens"]
            - result["reserved_input_tokens"]
        )
        result["remaining_output_tokens"] = (
            result["max_output_tokens"]
            - result["used_output_tokens"]
            - result["reserved_output_tokens"]
        )
        return result

    def reserve(
        self,
        *,
        run_id: str,
        task_id: str,
        packet_id: str,
        call_id: str,
        role: Role,
        attempt_no: int,
        profile_fingerprint: str,
        request_sha256: str,
        input_tokens: int,
        output_tokens: int,
        protect_future: bool = True,
    ) -> Reservation:
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("Token reservations cannot be negative")
        now = _now()
        reservation_id = f"bres_{uuid.uuid4().hex[:16]}"
        with self.run_store.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            ledger = conn.execute(
                "SELECT * FROM h_budget_ledgers WHERE run_id = ?", (run_id,)
            ).fetchone()
            if not ledger:
                conn.rollback()
                raise KeyError(f"Budget ledger not initialized for run: {run_id}")
            deadline = datetime.datetime.fromisoformat(ledger["deadline_at"])
            future_calls = ledger["reserved_future_calls"] if protect_future else 0
            future_output = ledger["reserved_future_output_tokens"] if protect_future else 0
            future_wall = ledger["reserved_future_wall_seconds"] if protect_future else 0
            if now + datetime.timedelta(seconds=future_wall) >= deadline:
                conn.rollback()
                raise BudgetExhaustedError("Wall-time reserve would be consumed")
            if ledger["used_calls"] + ledger["reserved_calls"] + 1 > ledger["max_calls"] - future_calls:
                conn.rollback()
                raise BudgetExhaustedError("Model call budget exhausted or verification reserve reached")
            if (
                ledger["used_input_tokens"]
                + ledger["reserved_input_tokens"]
                + input_tokens
                > ledger["max_input_tokens"]
            ):
                conn.rollback()
                raise BudgetExhaustedError("Model input-token budget exhausted")
            if (
                ledger["used_output_tokens"]
                + ledger["reserved_output_tokens"]
                + output_tokens
                > ledger["max_output_tokens"] - future_output
            ):
                conn.rollback()
                raise BudgetExhaustedError("Model output-token budget exhausted or verification reserve reached")
            conn.execute(
                """
                INSERT INTO h_model_calls(
                    call_id, run_id, task_id, packet_id, role, attempt_no, state,
                    profile_fingerprint, request_sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'INTENT', ?, ?, ?)
                """,
                (
                    call_id,
                    run_id,
                    task_id,
                    packet_id,
                    role.value,
                    attempt_no,
                    profile_fingerprint,
                    request_sha256,
                    now.isoformat(),
                ),
            )
            conn.execute(
                """
                INSERT INTO h_budget_reservations(
                    reservation_id, run_id, call_id, reserved_calls,
                    reserved_input_tokens, reserved_output_tokens, state, created_at
                ) VALUES (?, ?, ?, 1, ?, ?, 'RESERVED', ?)
                """,
                (reservation_id, run_id, call_id, input_tokens, output_tokens, now.isoformat()),
            )
            conn.execute(
                """
                UPDATE h_budget_ledgers
                SET reserved_calls = reserved_calls + 1,
                    reserved_input_tokens = reserved_input_tokens + ?,
                    reserved_output_tokens = reserved_output_tokens + ?,
                    updated_at = ?
                WHERE run_id = ?
                """,
                (input_tokens, output_tokens, now.isoformat(), run_id),
            )
            LifecycleService._append_event(
                conn,
                run_id,
                "MODEL_CALL_REQUESTED",
                {
                    "call_id": call_id,
                    "role": role.value,
                    "reservation_id": reservation_id,
                    "reserved_input_tokens": input_tokens,
                    "reserved_output_tokens": output_tokens,
                },
                "MODEL_CALL_INTENT",
                "MODEL_CALL_INTENT",
                now.isoformat(),
            )
            conn.commit()
        return Reservation(reservation_id, call_id, run_id, input_tokens, output_tokens)

    def mark_in_flight(self, call_id: str) -> None:
        now = _now().isoformat()
        with self.run_store.get_connection() as conn:
            with conn:
                changed = conn.execute(
                    "UPDATE h_model_calls SET state = 'IN_FLIGHT', started_at = ? WHERE call_id = ? AND state = 'INTENT'",
                    (now, call_id),
                ).rowcount
        if changed != 1:
            raise BudgetSettlementError(f"Call {call_id} is not in INTENT state")

    def settle(
        self,
        call_id: str,
        *,
        state: str,
        input_tokens: int,
        output_tokens: int,
        usage_source: str,
        response_artifact_id: Optional[str] = None,
        parsed_output_sha256: Optional[str] = None,
        provider_request_id: Optional[str] = None,
        latency_ms: Optional[int] = None,
        error_code: Optional[str] = None,
    ) -> None:
        if state not in {"SUCCEEDED", "FAILED", "CANCELLED", "UNKNOWN"}:
            raise ValueError(f"Invalid terminal model-call state: {state}")
        now = _now().isoformat()
        with self.run_store.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT r.*, c.state AS call_state
                FROM h_budget_reservations r
                JOIN h_model_calls c ON c.call_id = r.call_id
                WHERE r.call_id = ?
                """,
                (call_id,),
            ).fetchone()
            if not row:
                conn.rollback()
                raise KeyError(f"Unknown model call: {call_id}")
            if row["state"] != "RESERVED" or row["call_state"] in {
                "SUCCEEDED",
                "FAILED",
                "CANCELLED",
                "UNKNOWN",
            }:
                conn.rollback()
                raise BudgetSettlementError(f"Call {call_id} has already been settled")

            if state == "UNKNOWN":
                settled_input = row["reserved_input_tokens"]
                settled_output = row["reserved_output_tokens"]
                reservation_state = "CONSUMED_UNKNOWN"
                usage_source = "unknown"
            else:
                settled_input = max(0, input_tokens)
                settled_output = max(0, output_tokens)
                reservation_state = "SETTLED"
            # The call already happened, so its provider-reported usage is recorded as is
            # (a provider tokenizer can be denser than the conservative estimate). The ledger
            # absorbs an overage only up to its limits: a saturated dimension makes the next
            # reservation fail with MODEL_BUDGET_EXHAUSTED instead of crashing this settlement.
            ledger = conn.execute(
                "SELECT * FROM h_budget_ledgers WHERE run_id = ?", (row["run_id"],)
            ).fetchone()
            overage = {
                "input_tokens": max(0, settled_input - row["reserved_input_tokens"]),
                "output_tokens": max(0, settled_output - row["reserved_output_tokens"]),
            }
            charged_input = min(
                settled_input,
                ledger["max_input_tokens"] - ledger["used_input_tokens"]
                - (ledger["reserved_input_tokens"] - row["reserved_input_tokens"]),
            )
            charged_output = min(
                settled_output,
                ledger["max_output_tokens"] - ledger["used_output_tokens"]
                - (ledger["reserved_output_tokens"] - row["reserved_output_tokens"]),
            )
            conn.execute(
                """
                UPDATE h_budget_ledgers
                SET reserved_calls = reserved_calls - 1,
                    reserved_input_tokens = reserved_input_tokens - ?,
                    reserved_output_tokens = reserved_output_tokens - ?,
                    used_calls = used_calls + 1,
                    used_input_tokens = used_input_tokens + ?,
                    used_output_tokens = used_output_tokens + ?,
                    updated_at = ?
                WHERE run_id = ?
                """,
                (
                    row["reserved_input_tokens"],
                    row["reserved_output_tokens"],
                    charged_input,
                    charged_output,
                    now,
                    row["run_id"],
                ),
            )
            conn.execute(
                "UPDATE h_budget_reservations SET state = ?, settled_at = ? WHERE call_id = ?",
                (reservation_state, now, call_id),
            )
            conn.execute(
                """
                UPDATE h_model_calls
                SET state = ?, response_artifact_id = ?, parsed_output_sha256 = ?,
                    provider_request_id = ?, input_tokens = ?, output_tokens = ?,
                    usage_source = ?, latency_ms = ?, error_code = ?, settled_at = ?
                WHERE call_id = ?
                """,
                (
                    state,
                    response_artifact_id,
                    parsed_output_sha256,
                    provider_request_id,
                    settled_input,
                    settled_output,
                    usage_source,
                    latency_ms,
                    error_code,
                    now,
                    call_id,
                ),
            )
            LifecycleService._append_event(
                conn,
                row["run_id"],
                "MODEL_CALL_SETTLED",
                {
                    "call_id": call_id,
                    "state": state,
                    "input_tokens": settled_input,
                    "output_tokens": settled_output,
                    "usage_source": usage_source,
                    **({"reservation_exceeded": overage} if any(overage.values()) else {}),
                },
                "MODEL_CALL_IN_FLIGHT",
                f"MODEL_CALL_{state}",
                now,
            )
            conn.commit()

    def release_intent(self, call_id: str, error_code: str) -> None:
        now = _now().isoformat()
        with self.run_store.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT r.*, c.state AS call_state
                FROM h_budget_reservations r
                JOIN h_model_calls c ON c.call_id = r.call_id
                WHERE r.call_id = ?
                """,
                (call_id,),
            ).fetchone()
            if not row or row["state"] != "RESERVED" or row["call_state"] != "INTENT":
                conn.rollback()
                raise BudgetSettlementError("Only a non-started intent can release its reservation")
            conn.execute(
                """
                UPDATE h_budget_ledgers
                SET reserved_calls = reserved_calls - 1,
                    reserved_input_tokens = reserved_input_tokens - ?,
                    reserved_output_tokens = reserved_output_tokens - ?,
                    updated_at = ?
                WHERE run_id = ?
                """,
                (
                    row["reserved_input_tokens"],
                    row["reserved_output_tokens"],
                    now,
                    row["run_id"],
                ),
            )
            conn.execute(
                "UPDATE h_budget_reservations SET state = 'RELEASED', settled_at = ? WHERE call_id = ?",
                (now, call_id),
            )
            conn.execute(
                "UPDATE h_model_calls SET state = 'FAILED', error_code = ?, settled_at = ? WHERE call_id = ?",
                (error_code, now, call_id),
            )
            conn.commit()

    def reconcile(self, run_id: str) -> Dict[str, int]:
        """Reconcile abandoned intents and in-flight calls conservatively."""
        released = 0
        unknown = 0
        with self.run_store.get_connection() as conn:
            calls = conn.execute(
                "SELECT call_id, state FROM h_model_calls WHERE run_id = ? AND state IN ('INTENT', 'IN_FLIGHT')",
                (run_id,),
            ).fetchall()
        for call in calls:
            if call["state"] == "INTENT":
                self.release_intent(call["call_id"], "RECONCILED_BEFORE_NETWORK")
                released += 1
            else:
                self.settle(
                    call["call_id"],
                    state="UNKNOWN",
                    input_tokens=0,
                    output_tokens=0,
                    usage_source="unknown",
                    error_code="RECONCILED_IN_FLIGHT_UNKNOWN",
                )
                unknown += 1
        return {"released_intents": released, "unknown_calls": unknown}
