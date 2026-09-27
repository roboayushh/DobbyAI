"""PRD 5 unit tests: deterministic queue planning and the journaled ref protocol."""
from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from harness.gitflow.ref_journal import RefOperationJournal, StaleFencingTokenError
from harness.queue import graph
from harness.workspace.task_workspace import TaskWorkspaceService

from tests.support.harness_fixtures import prepare_run


def facts(number: int, title: str, body: str, labels=(), digest: str = "") -> graph.TaskFacts:
    return graph.TaskFacts(
        task_id=f"tsk_{number}", ordinal=number - 1, title=title, body=body, labels=tuple(labels),
        source_key=f"github:o/r:issue:{number}", normalized_sha256=digest or f"{number:064d}",
    )


def test_classification_is_deterministic() -> None:
    tasks = [
        facts(1, "Fix add", "add returns the wrong value for positive numbers"),
        facts(2, "Fix add again", "add returns the wrong value for positive numbers", digest="1".zfill(64)),
        facts(3, "Question about API", "How should I call the API from scripts?", labels=["question"]),
        facts(4, "Bug", "broken"),
        facts(5, "Implement total", "Depends on #1. Add total() built on add."),
    ]
    items, edges = graph.classify(tasks)
    by_id = {item.task_id: item for item in items}
    assert by_id["tsk_1"].classification == "ACTIONABLE"
    assert by_id["tsk_2"].classification == "DUPLICATE" and by_id["tsk_2"].canonical_task_id == "tsk_1"
    assert by_id["tsk_3"].classification == "UNSUPPORTED"
    assert by_id["tsk_4"].classification == "AMBIGUOUS"
    assert by_id["tsk_5"].classification == "DEPENDENT"
    assert edges == [graph.Edge("tsk_1", "tsk_5")]
    again, again_edges = graph.classify(list(reversed(tasks)))
    assert [(i.task_id, i.classification) for i in again] == [(i.task_id, i.classification) for i in items]
    assert again_edges == edges


def test_cycles_are_rejected_with_a_stable_path() -> None:
    tasks = [facts(1, "A task here", "Blocked by #2 and more words"), facts(2, "B task here", "Depends on #1 for sure")]
    items, edges = graph.classify(tasks)
    with pytest.raises(graph.QueuePlanError) as raised:
        graph.validate(items, edges, max_tasks=20, max_edges=80)
    assert "tsk_1 -> tsk_2 -> tsk_1" in str(raised.value)


def test_limits_are_enforced() -> None:
    items, edges = graph.classify([facts(n, f"Task number {n}", f"Do the thing {n} carefully") for n in range(1, 5)])
    with pytest.raises(graph.QueuePlanError):
        graph.validate(items, edges, max_tasks=3, max_edges=80)


def test_ready_set_and_order() -> None:
    items = [
        {"task_id": "a", "classification": "ACTIONABLE", "priority": 0, "ordinal": 0},
        {"task_id": "b", "classification": "ACTIONABLE", "priority": 300, "ordinal": 1},
        {"task_id": "c", "classification": "DEPENDENT", "priority": 0, "ordinal": 2},
        {"task_id": "d", "classification": "ACTIONABLE", "priority": 0, "ordinal": 3},
    ]
    edges = [graph.Edge("d", "c")]
    states = {"a": "PENDING", "b": "PENDING", "c": "PENDING", "d": "PENDING"}
    ready = graph.ready_items(items, edges, states)
    assert ready == ["a", "b", "d"]
    by_id = {item["task_id"]: item for item in items}
    # explicit priority first, then criticality (d unblocks c), then ordinal
    assert graph.order(ready, by_id, edges) == ["b", "d", "a"]
    states["d"] = "FAILED"
    assert graph.blocked_reason("c", edges, states) == ("DEPENDENCY_NOT_INTEGRATED", ["d"])
    states["d"] = "INTEGRATED"
    assert "c" in graph.ready_items(items, edges, states)


# ------------------------------------------------------------- ref journal
@pytest.fixture()
def journal_env(tmp_path: Path):
    env = prepare_run(tmp_path, {"src/__init__.py": "", "src/a.py": "x = 1\n"}, "Change x in src/a.py to two")
    workspaces = TaskWorkspaceService(env.run_store, env.artifact_store, env.data_root)
    git = workspaces.git(env.run_id)
    baseline = env.run_store.get_source_snapshot(env.run_id)["baseline_commit"]
    tree = git.commit_tree_of(baseline)
    child = git.commit_tree(tree, [baseline], "child\n")
    other = git.commit_tree(tree, [baseline], "other\n")
    now = datetime.datetime.now(datetime.timezone.utc)
    with env.run_store.get_connection() as conn:
        with conn:
            conn.execute(
                "INSERT INTO h_task_queues(queue_id, run_id, task_snapshot_sha256, active_version_id, mode, state, created_at, updated_at) VALUES ('que_t', ?, ?, NULL, 'development', 'RUNNING', ?, ?)",
                (env.run_id, "0" * 64, now.isoformat(), now.isoformat()),
            )
            conn.execute(
                "INSERT INTO h_queue_leases VALUES ('ql1', 'que_t', 'me', 'EXECUTE', 1, 'ACTIVE', ?, ?, ?, NULL)",
                (now.isoformat(), now.isoformat(), (now + datetime.timedelta(hours=1)).isoformat()),
            )
    return env, git, baseline, child, other


def test_ref_journal_applied_not_applied_uncertain(journal_env) -> None:
    env, git, baseline, child, other = journal_env
    journal = RefOperationJournal(env.run_store)
    ref = f"refs/harness/runs/{env.run_id}/integration"
    create = journal.prepare(run_id=env.run_id, queue_id="que_t", kind="CREATE", ref=ref, expected_old=None, desired_new=baseline, fencing_token=1)
    assert journal.dispatch(git, create).state == "APPLIED"
    # A stale expected-old loses the CAS: observed == expected? no -> recorded truthfully.
    stale = journal.prepare(run_id=env.run_id, queue_id="que_t", kind="ADVANCE", ref=ref, expected_old=other, desired_new=child, fencing_token=1)
    assert journal.dispatch(git, stale).state == "UNCERTAIN"
    advance = journal.prepare(run_id=env.run_id, queue_id="que_t", kind="ADVANCE", ref=ref, expected_old=baseline, desired_new=child, fencing_token=1)
    # Crash after PREPARED but before dispatch: observation says not applied.
    assert journal.observe(git, advance).state == "NOT_APPLIED"
    replay = journal.prepare(run_id=env.run_id, queue_id="que_t", kind="ADVANCE", ref=ref, expected_old=baseline, desired_new=child, fencing_token=1)
    # Crash after the ref moved but before settlement: observation says applied, never re-applied.
    git.update_ref_cas(ref, child, baseline)
    assert journal.observe(git, replay).state == "APPLIED"
    assert git.read_ref(ref) == child


def test_stale_fencing_token_cannot_move_refs(journal_env) -> None:
    env, git, baseline, child, _ = journal_env
    journal = RefOperationJournal(env.run_store)
    ref = f"refs/harness/runs/{env.run_id}/integration"
    op = journal.prepare(run_id=env.run_id, queue_id="que_t", kind="CREATE", ref=ref, expected_old=None, desired_new=baseline, fencing_token=7)
    with pytest.raises(StaleFencingTokenError):
        journal.dispatch(git, op)
    assert git.read_ref(ref) is None
