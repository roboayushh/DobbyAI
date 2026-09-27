"""PRD 5 recovery and compensation tests (Docker-backed).

These exercise the journaled integration protocol across the crash and
failure boundaries that ordinary happy-path runs never hit.
"""
from __future__ import annotations

import datetime
import subprocess
import sys
from pathlib import Path

import pytest

from harness.gitflow import ref_journal
from harness.queue import QueueCoordinator, QueueError
from harness.verification import service as verification_service

from tests.support.harness_fixtures import prepare_run, repo_integrity
from tests.support.scripted_model import RoutedAdapter, code_step, complete_step, patch_action, plan_step, validator_step
from tests.test_prd345_e2e import CALC, FIX, TASK, _runtime_ready, issue_ids, stack

pytestmark = pytest.mark.skipif(not _runtime_ready(), reason="Docker sandbox runtime unavailable")

FILES = {
    **CALC,
    "src/text.py": "def shout(s):\n    return s.lower()\n",
    "tests/test_text.py": "from src.text import shout\n\n\ndef test_shout():\n    assert shout('hi') == 'HI'\n",
}
ISSUES = [
    (1, "add returns wrong result", TASK),
    (2, "shout should uppercase", "shout('hi') should return 'HI' but returns 'hi'. See tests/test_text.py::test_shout"),
]


def routes(num):
    return {
        num[1]: [plan_step(), code_step(FIX), complete_step(), validator_step()],
        num[2]: [plan_step(edit_paths=("src/text.py",)), code_step(patch_action("src/text.py", "s.lower()", "s.upper()", "tests/test_text.py")),
                 complete_step(), validator_step()],
    }


def test_post_advance_failure_compensates_exactly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = prepare_run(tmp_path, FILES, issues=ISSUES, max_tasks=2)
    before = repo_integrity(env.repo)
    num = issue_ids(env)
    original = verification_service.verify_commit
    calls = {"post": 0}

    def flaky_post_advance(service, run_id, **kwargs):
        result = original(service, run_id, **kwargs)
        if "post-advance" in kwargs["artifact_prefix"]:
            calls["post"] += 1
            if calls["post"] == 2:  # the second integrated task fails cumulative verification
                result.status = "FAILED"
        return result

    monkeypatch.setattr(verification_service, "verify_commit", flaky_post_advance)
    services, _, queue = stack(env, RoutedAdapter(routes(num)))
    final = queue.run(env.run_id)
    heads = queue.heads(queue.queue(env.run_id)["queue_id"])
    assert [h["source_kind"] for h in heads] == ["BASELINE", "TASK", "TASK", "COMPENSATION"]
    assert heads[3]["commit_oid"] == heads[1]["commit_oid"]  # back to exactly the first accepted task
    git = services.workspaces.git(env.run_id)
    assert git.read_ref(queue.integration_ref(env.run_id)) == heads[1]["commit_oid"]
    states = sorted(entry.status for entry in final.task_results)
    assert states == ["INTEGRATED", "INTEGRATION_FAILED"]
    assert final.status == "PARTIAL_SUCCESS"
    assert final.final_integration.commit == heads[1]["commit_oid"]
    assert final.aggregate_verification.status == "PASS"
    assert repo_integrity(env.repo) == before
    # The compensated task's attempt is still exported, clearly labeled and never integrated.
    report_artifact = env.artifact_store.get_artifact_by_id(final.queue_report_artifact_id)
    report = __import__("json").loads((Path(env.data_root) / report_artifact["relative_path"]).read_text())
    partials = report["best_partial_candidates"]
    assert len(partials) == 1 and partials[0]["integrated"] is False
    assert partials[0]["task_id"] == next(e.task_id for e in final.task_results if e.status == "INTEGRATION_FAILED")


def test_crash_between_ref_move_and_settlement_recovers_without_duplication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = prepare_run(tmp_path, FILES, issues=ISSUES, max_tasks=2)
    num = issue_ids(env)
    adapter = RoutedAdapter(routes(num))
    original_observe = ref_journal.RefOperationJournal.observe
    state = {"crashed": False}

    def crashing_observe(self, git, operation_id):
        op = self.get(operation_id)
        if not state["crashed"] and op["operation_kind"] == "ADVANCE" and op["state"] == "DISPATCHED":
            state["crashed"] = True
            raise RuntimeError("simulated crash after update-ref, before observation was recorded")
        return original_observe(self, git, operation_id)

    monkeypatch.setattr(ref_journal.RefOperationJournal, "observe", crashing_observe)
    services, controller, queue = stack(env, adapter)
    with pytest.raises(RuntimeError):
        queue.run(env.run_id)
    git = services.workspaces.git(env.run_id)
    moved_to = git.read_ref(queue.integration_ref(env.run_id))
    with env.run_store.get_connection() as conn:
        assert conn.execute("SELECT state FROM h_git_ref_operations WHERE operation_kind = 'ADVANCE'").fetchone()[0] == "DISPATCHED"
    monkeypatch.setattr(ref_journal.RefOperationJournal, "observe", original_observe)
    resumed = QueueCoordinator(run_store=env.run_store, artifact_store=env.artifact_store, services=services, controller=controller)
    final = resumed.run(env.run_id)
    assert final.status == "COMPLETED_ALL"
    heads = resumed.heads(resumed.queue(env.run_id)["queue_id"])
    assert [h["sequence"] for h in heads] == [0, 1, 2]
    assert heads[1]["commit_oid"] == moved_to  # the crashed advance was observed as applied, never re-applied
    with env.run_store.get_connection() as conn:
        ops = [tuple(r) for r in conn.execute("SELECT operation_kind, state FROM h_git_ref_operations ORDER BY created_at")]
    assert ops == [("CREATE", "APPLIED"), ("ADVANCE", "APPLIED"), ("ADVANCE", "APPLIED")]


def test_live_lease_blocks_and_dead_owner_is_taken_over(tmp_path: Path) -> None:
    env = prepare_run(tmp_path, FILES, issues=ISSUES, max_tasks=2)
    services, controller, queue = stack(env, RoutedAdapter({}))
    queue.prepare(env.run_id)
    queue_id = queue.queue(env.run_id)["queue_id"]
    live = QueueCoordinator(run_store=env.run_store, artifact_store=env.artifact_store, services=services, controller=controller)
    live._acquire(queue_id)
    with pytest.raises(QueueError):
        queue._acquire(queue_id)
    live._release(queue_id)
    proc = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    dead_owner = f"queue:{__import__('socket').gethostname()[:40]}:{proc.stdout.strip()}:deadbeef"
    now = datetime.datetime.now(datetime.timezone.utc)
    with env.run_store.get_connection() as conn:
        with conn:
            conn.execute(
                "INSERT INTO h_queue_leases VALUES ('ql_dead', ?, ?, 'EXECUTE', 99, 'ACTIVE', ?, ?, ?, NULL)",
                (queue_id, dead_owner, now.isoformat(), now.isoformat(), (now + datetime.timedelta(hours=1)).isoformat()),
            )
    token = queue._acquire(queue_id)
    assert token == 100
    with env.run_store.get_connection() as conn:
        assert conn.execute("SELECT state FROM h_queue_leases WHERE queue_lease_id = 'ql_dead'").fetchone()[0] == "EXPIRED"
