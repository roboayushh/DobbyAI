from __future__ import annotations

import datetime
import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from harness.contracts import OrchestrationState, Role
from harness.orchestration import (
    BudgetExhaustedError,
    BudgetLedgerService,
    BudgetLimits,
    LeaseUnavailableError,
    LifecycleService,
    RunLeaseService,
    StaleLifecycleError,
)
from harness.persistence import RunStore


H = "a" * 64


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _prepared_store(tmp_path: Path, *, packets: int = 2) -> RunStore:
    store = RunStore(str(tmp_path / "harness.db"))
    now = _now()
    with store.get_connection() as conn:
        with conn:
            conn.execute(
                """
                INSERT INTO h_runs(
                    run_id, schema_version, idempotency_key, task_mode, execution_mode,
                    state, request_json, request_sha256, runtime_profile,
                    next_event_seq, created_at, updated_at
                ) VALUES ('run_1', '1.0', 'control-key', 'single_issue', 'development',
                          'PREPARED', '{}', ?, 'local-default', 1, ?, ?)
                """,
                (hashlib.sha256(b"{}").hexdigest(), now, now),
            )
            conn.execute(
                """
                INSERT INTO h_tasks(
                    task_id, run_id, ordinal, source_type, source_key,
                    raw_content_sha256, normalized_content_sha256,
                    task_spec_json, state, created_at
                ) VALUES ('task_1', 'run_1', 0, 'direct_text', 'direct:1', ?, ?, '{}', 'QUEUED', ?)
                """,
                (H, H, now),
            )
            for index in range(packets):
                artifact_id = f"art_{index}"
                packet_id = f"pkt_{index}"
                conn.execute(
                    """
                    INSERT INTO h_artifacts(
                        artifact_id, run_id, task_id, kind, relative_path,
                        media_type, byte_size, sha256, created_at
                    ) VALUES (?, 'run_1', 'task_1', 'context_packet', ?,
                              'application/json', 2, ?, ?)
                    """,
                    (
                        artifact_id,
                        f"runs/run_1/artifacts/packet-{index}.json",
                        hashlib.sha256(b"{}").hexdigest(),
                        now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO h_context_packets(
                        packet_id, run_id, task_id, role, purpose, packet_revision,
                        source_revision, artifact_id, packet_sha256,
                        estimated_input_tokens, reserved_output_tokens,
                        safety_margin_tokens, counter_mode, profile_fingerprint, created_at
                    ) VALUES (?, 'run_1', 'task_1', 'planner', ?, 1, 'B', ?, ?,
                              100, 50, 10, 'conservative_estimate', ?, ?)
                    """,
                    (
                        packet_id,
                        f"purpose-{index}",
                        artifact_id,
                        hashlib.sha256(b"{}").hexdigest(),
                        H,
                        now,
                    ),
                )
    return store


def test_lifecycle_is_additive_and_compare_and_swap(tmp_path: Path) -> None:
    store = _prepared_store(tmp_path)
    service = LifecycleService(store)
    snapshot = service.initialize("run_1")
    assert snapshot.state == OrchestrationState.PREPARED
    indexed = service.transition(
        "run_1",
        OrchestrationState.PREPARED,
        snapshot.version,
        OrchestrationState.INDEXING,
        event_type="INDEXING_STARTED",
    )
    assert indexed.version == snapshot.version + 1
    assert store.get_run("run_1")["state"] == "PREPARED"
    with pytest.raises(StaleLifecycleError):
        service.transition(
            "run_1",
            OrchestrationState.PREPARED,
            snapshot.version,
            OrchestrationState.INDEXING,
            event_type="STALE",
        )


def test_repository_mode_selects_only_first_unblocked_task(tmp_path: Path) -> None:
    store = _prepared_store(tmp_path)
    now = _now()
    with store.get_connection() as conn:
        with conn:
            conn.execute("UPDATE h_runs SET task_mode = 'repository' WHERE run_id = 'run_1'")
            conn.execute(
                """
                INSERT INTO h_tasks(
                    task_id, run_id, ordinal, source_type, source_key,
                    raw_content_sha256, normalized_content_sha256,
                    task_spec_json, state, created_at
                ) VALUES ('task_2', 'run_1', 1, 'direct_text', 'direct:2',
                          ?, ?, '{}', 'QUEUED', ?)
                """,
                (H, H, now),
            )
    service = LifecycleService(store)
    snapshot = service.initialize("run_1")
    assert snapshot.active_task_id == "task_1"
    service.transition(
        "run_1",
        OrchestrationState.PREPARED,
        snapshot.version,
        OrchestrationState.INDEXING,
        event_type="INDEXING_STARTED",
    )
    with store.get_connection() as conn:
        later = conn.execute(
            """
            SELECT t.state AS preparation_state, l.state AS orchestration_state,
                   l.version AS orchestration_version
            FROM h_tasks t JOIN h_task_lifecycle l ON l.task_id = t.task_id
            WHERE t.task_id = 'task_2'
            """
        ).fetchone()
    assert dict(later) == {
        "preparation_state": "QUEUED",
        "orchestration_state": "QUEUED",
        "orchestration_version": 1,
    }


def test_only_one_controller_lease_is_active(tmp_path: Path) -> None:
    store = _prepared_store(tmp_path)
    LifecycleService(store).initialize("run_1")
    leases = RunLeaseService(store)
    first = leases.acquire("run_1", "owner-a")
    with pytest.raises(LeaseUnavailableError):
        leases.acquire("run_1", "owner-b")
    leases.assert_valid(first)
    leases.release(first)
    second = leases.acquire("run_1", "owner-b")
    assert second.owner_id == "owner-b"


def test_atomic_budget_reservation_protects_future_calls(tmp_path: Path) -> None:
    store = _prepared_store(tmp_path)
    budget = BudgetLedgerService(store)
    budget.initialize(
        "run_1",
        BudgetLimits(
            max_calls=3,
            max_input_tokens=1_000,
            max_output_tokens=500,
            max_wall_seconds=300,
            reserved_future_calls=2,
            reserved_future_output_tokens=100,
            reserved_future_wall_seconds=30,
        ),
    )

    def reserve(index: int) -> str:
        try:
            budget.reserve(
                run_id="run_1",
                task_id="task_1",
                packet_id=f"pkt_{index}",
                call_id=f"call_{index}",
                role=Role.PLANNER,
                attempt_no=1,
                profile_fingerprint=H,
                request_sha256=H,
                input_tokens=100,
                output_tokens=100,
            )
            return "admitted"
        except BudgetExhaustedError:
            return "denied"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(reserve, [0, 1]))
    assert sorted(outcomes) == ["admitted", "denied"]
    ledger = budget.get("run_1")
    assert ledger["reserved_calls"] == 1
    assert ledger["used_calls"] == 0


def test_unknown_call_consumes_conservative_reservation_once(tmp_path: Path) -> None:
    store = _prepared_store(tmp_path)
    budget = BudgetLedgerService(store)
    budget.initialize("run_1")
    reservation = budget.reserve(
        run_id="run_1",
        task_id="task_1",
        packet_id="pkt_0",
        call_id="call_unknown",
        role=Role.PLANNER,
        attempt_no=1,
        profile_fingerprint=H,
        request_sha256=H,
        input_tokens=200,
        output_tokens=300,
    )
    budget.mark_in_flight(reservation.call_id)
    budget.settle(
        reservation.call_id,
        state="UNKNOWN",
        input_tokens=0,
        output_tokens=0,
        usage_source="unknown",
        error_code="TIMEOUT_UNCERTAIN",
    )
    ledger = budget.get("run_1")
    assert ledger["used_calls"] == 1
    assert ledger["used_input_tokens"] == 200
    assert ledger["used_output_tokens"] == 300
    with pytest.raises(Exception, match="already been settled"):
        budget.settle(
            reservation.call_id,
            state="UNKNOWN",
            input_tokens=0,
            output_tokens=0,
            usage_source="unknown",
        )


def test_provider_usage_above_the_estimate_settles_instead_of_crashing(tmp_path: Path) -> None:
    """A denser provider tokenizer is recorded truthfully; a saturated budget stops the next call."""
    store = _prepared_store(tmp_path)
    budget = BudgetLedgerService(store)
    budget.initialize("run_1", BudgetLimits(max_calls=6, max_input_tokens=1_000, max_output_tokens=1_000,
                                            reserved_future_calls=2, reserved_future_output_tokens=100))

    def call(index: int, reported_input: int) -> None:
        reservation = budget.reserve(run_id="run_1", task_id="task_1", packet_id=f"pkt_{index}",
                                     call_id=f"call_{index}", role=Role.PLANNER, attempt_no=1,
                                     profile_fingerprint=H, request_sha256=H, input_tokens=300, output_tokens=100)
        budget.mark_in_flight(reservation.call_id)
        budget.settle(reservation.call_id, state="SUCCEEDED", input_tokens=reported_input, output_tokens=50,
                      usage_source="provider_reported")

    call(0, 450)  # over its 300-token reservation, still within the run budget
    assert budget.get("run_1")["used_input_tokens"] == 450
    with store.get_connection() as conn:
        assert conn.execute("SELECT input_tokens FROM h_model_calls WHERE call_id = 'call_0'").fetchone()[0] == 450
    call(1, 900)  # the provider reports more than the budget can absorb: the ledger saturates at its limit
    ledger = budget.get("run_1")
    assert ledger["used_input_tokens"] == 1_000 and ledger["reserved_input_tokens"] == 0
    with store.get_connection() as conn:
        assert conn.execute("SELECT input_tokens FROM h_model_calls WHERE call_id = 'call_1'").fetchone()[0] == 900
    with pytest.raises(BudgetExhaustedError):
        budget.reserve(run_id="run_1", task_id="task_1", packet_id="pkt_0", call_id="call_2", role=Role.PLANNER,
                       attempt_no=2, profile_fingerprint=H, request_sha256=H, input_tokens=10, output_tokens=10)
