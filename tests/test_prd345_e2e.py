"""PRD 3-5 end-to-end tests against the real Docker sandbox.

Model responses are scripted (bound to the host-advertised revisions), but
every action, check, commit, and ref move is real. Skipped when Docker or the
pinned runtime image is unavailable.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Dict, List, Tuple

import pytest
from typer.testing import CliRunner

from harness.application.composition import build_controller, build_services, ensure_runtime
from harness.orchestration import BudgetLimits
from harness.queue import QueueCoordinator

from tests.support.harness_fixtures import docker_available, prepare_run, repo_integrity
from tests.support.scripted_model import (
    RoutedAdapter,
    ScriptedAdapter,
    code_step,
    complete_step,
    needs_input_step,
    patch_action,
    plan_step,
    validator_step,
)

BIG_BUDGET = BudgetLimits(max_calls=80, max_input_tokens=4_000_000, max_output_tokens=600_000, max_wall_seconds=3600)


def _runtime_ready() -> bool:
    if not docker_available():
        return False
    from harness.persistence import ArtifactStore, RunStore
    import tempfile

    with tempfile.TemporaryDirectory() as temp:
        store = RunStore(os.path.join(temp, "h.db"))
        services = build_services(store, ArtifactStore(temp, store), temp)
        return ensure_runtime(services, auto_build=True) is None


pytestmark = pytest.mark.skipif(not _runtime_ready(), reason="Docker sandbox runtime unavailable")

CALC = {
    "src/__init__.py": "",
    "src/calc.py": "def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n",
    "tests/test_calc.py": "from src.calc import add, mul\n\n\ndef test_add():\n    assert add(2, 3) == 5\n\n\ndef test_mul():\n    assert mul(2, 3) == 6\n",
}
TASK = "add(2, 3) should return 5 but returns -1. See tests/test_calc.py::test_add"


def stack(env, adapter, budget: BudgetLimits = BIG_BUDGET):
    services = build_services(env.run_store, env.artifact_store, env.data_root)
    controller = build_controller(services, env.profile_path, adapter=adapter, budget_limits=budget)
    queue = QueueCoordinator(run_store=env.run_store, artifact_store=env.artifact_store, services=services, controller=controller)
    return services, controller, queue


def results(env) -> List[Dict[str, object]]:
    with env.run_store.get_connection() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT r.settlement, r.reason_codes_json, r.stdout_artifact_id FROM h_execution_results r JOIN h_actions a ON a.action_id = r.action_id WHERE a.run_id = ? ORDER BY a.created_at",
            (env.run_id,),
        ).fetchall()]


def artifact_text(env, artifact_id: str) -> str:
    artifact = env.artifact_store.get_artifact_by_id(artifact_id)
    return (Path(env.data_root) / artifact["relative_path"]).read_text(encoding="utf-8", errors="replace")


FIX = patch_action("src/calc.py", "return a - b", "return a + b", "tests/test_calc.py")


# ---------------------------------------------------------------- PRD 3
def test_single_issue_pipeline_completes_and_never_touches_original(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_API_KEY", "sk-test-must-never-reach-the-sandbox")
    env = prepare_run(tmp_path, CALC, TASK)
    before = repo_integrity(env.repo)
    adapter = ScriptedAdapter([plan_step(), code_step(FIX), complete_step(), validator_step()])
    services, _, queue = stack(env, adapter)
    final = queue.run(env.run_id)
    assert final.status == "COMPLETED_ALL"
    assert final.aggregate_verification.status == "PASS"
    assert final.publication_authorized is False
    git = services.workspaces.git(env.run_id)
    assert git.commit_parents(final.final_integration.commit) == [final.baseline.commit]
    assert b"return a + b" in git.diff(final.baseline.commit, final.final_integration.commit)
    assert repo_integrity(env.repo) == before
    settled = results(env)
    assert [row["settlement"] for row in settled] == ["ACCEPTED"]


def test_sandbox_adversarial_battery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_API_KEY", "sk-test-secret-value")
    env = prepare_run(tmp_path, CALC, TASK)
    probe = r'''
import os, socket, json, subprocess
out = {"uid": os.getuid(), "secret": [k for k in os.environ if "API_KEY" in k or "TOKEN" in k]}
try:
    socket.create_connection(("1.1.1.1", 53), timeout=2); out["net"] = True
except OSError:
    out["net"] = False
for path in ("/etc/probe", "/usr/probe", "/opt/harness/probe"):
    try:
        open(path, "w").write("x"); out[path] = True
    except OSError:
        out[path] = False
out["docker_sock"] = os.path.exists("/var/run/docker.sock")
out["home_files"] = sorted(os.listdir("/tmp"))[:5]
out["original_repo_visible"] = any(os.path.exists(p) for p in ("/Users", "/home/runner", "/root/.ssh"))
r = run(["sh", "-c", "sleep 300 & echo started"], timeout_seconds=5)
print("PROBE=" + json.dumps(out))
emit_result("ACTION_COMPLETED", "probe", ["done"])
'''
    adapter = ScriptedAdapter([plan_step(), code_step(probe), code_step(FIX), complete_step(), validator_step()])
    _, controller, _ = stack(env, adapter)
    result = controller.continue_run(env.run_id, stop_at="complete")
    assert result.status.value == "READY_FOR_REVIEW"
    first = results(env)[0]
    stdout = artifact_text(env, first["stdout_artifact_id"])
    observed = json.loads(re.search(r"PROBE=(\{.*\})", stdout).group(1))
    assert observed["uid"] != 0
    assert observed["secret"] == []
    assert observed["net"] is False
    assert not observed["/etc/probe"] and not observed["/usr/probe"] and not observed["/opt/harness/probe"]
    assert observed["docker_sock"] is False
    assert "sk-test-secret-value" not in stdout


def test_timeout_output_bomb_and_reserved_write_are_rolled_back(tmp_path: Path) -> None:
    env = prepare_run(tmp_path, CALC, TASK)
    hang = "apply_patch([{'path': 'src/calc.py', 'old': 'return a - b', 'new': 'return 0'}])\nimport time\nwhile True:\n    time.sleep(1)\n"
    bomb = "import sys\nchunk = 'x' * 65536\nfor _ in range(100000):\n    sys.stdout.write(chunk)\n"
    reserved = "import os\nos.makedirs('.git', exist_ok=True)\nopen('.git/config', 'w').write('[core]\\n')\nemit_result('ACTION_COMPLETED', 'wrote git', [])\n"
    adapter = ScriptedAdapter([
        plan_step(), code_step(hang, seconds=5), code_step(bomb, seconds=60), code_step(reserved, declared=(".",)),
        code_step(FIX), complete_step(), validator_step(),
    ])
    services, controller, _ = stack(env, adapter)
    result = controller.continue_run(env.run_id, stop_at="complete")
    assert result.status.value == "READY_FOR_REVIEW"
    settled = results(env)
    assert settled[0]["settlement"] == "FAILED_ROLLED_BACK" and "TIMEOUT" in settled[0]["reason_codes_json"]
    assert settled[1]["settlement"] == "FAILED_ROLLED_BACK" and "OUTPUT_LIMIT" in settled[1]["reason_codes_json"]
    assert settled[2]["settlement"] == "POLICY_VIOLATION_ROLLED_BACK"
    assert settled[3]["settlement"] == "ACCEPTED"
    root = services.workspaces.workspace_root(env.run_id, env.tasks()[0])
    assert not (root / ".git").exists()


# ---------------------------------------------------------------- PRD 4
def test_repair_loop_turns_failed_candidate_into_pass(tmp_path: Path) -> None:
    env = prepare_run(tmp_path, CALC, TASK)
    wrong = patch_action("src/calc.py", "return a - b", "return b - a + 2", "tests/test_calc.py", note="wrong fix")
    repair = patch_action("src/calc.py", "return b - a + 2", "return a + b", "tests/test_calc.py", note="repair")
    adapter = ScriptedAdapter([plan_step(), code_step(wrong), complete_step(), code_step(repair), complete_step(), validator_step()])
    services, controller, _ = stack(env, adapter)
    result = controller.continue_run(env.run_id, stop_at="complete")
    assert result.status.value == "READY_FOR_REVIEW"
    with env.run_store.get_connection() as conn:
        decisions = [row["status"] for row in conn.execute("SELECT status FROM h_completion_decisions ORDER BY decided_at")]
        repairs = conn.execute("SELECT COUNT(*) FROM h_repair_attempts").fetchone()[0]
    assert decisions == ["FAILED", "PASS"]
    assert repairs == 1
    # The repair packet carried host-observed failure evidence, not the coder's claim.
    repair_view = adapter.views[3]
    assert "verification_repair_feedback" in repair_view.text or "repair" in repair_view.text.lower()


def test_complete_without_changes_is_rejected_not_passed(tmp_path: Path) -> None:
    env = prepare_run(tmp_path, CALC, TASK)
    adapter = ScriptedAdapter([plan_step(), complete_step(), code_step(FIX), complete_step(), validator_step()])
    _, controller, _ = stack(env, adapter)
    result = controller.continue_run(env.run_id, stop_at="complete")
    assert result.status.value == "READY_FOR_REVIEW"
    with env.run_store.get_connection() as conn:
        rejected = [json.loads(r[0])["reason_code"] for r in conn.execute("SELECT payload_json FROM h_events WHERE event_type = 'CODER_DECISION_REJECTED'")]
        candidates = conn.execute("SELECT COUNT(*) FROM h_candidate_snapshots").fetchone()[0]
    assert "COMPLETE_WITHOUT_CHANGES" in rejected
    assert candidates == 1


def test_test_tampering_never_passes(tmp_path: Path) -> None:
    env = prepare_run(tmp_path, CALC, TASK)
    tamper = patch_action("tests/test_calc.py", "assert add(2, 3) == 5", "assert True", "tests/test_calc.py", note="tamper")
    adapter = ScriptedAdapter([plan_step(), code_step(tamper, declared=("src/", "tests/")), complete_step(), validator_step()])
    _, controller, _ = stack(env, adapter)
    try:
        controller.continue_run(env.run_id, stop_at="complete")
    except Exception:
        pass  # the script ends at the repair turn; only the first verdict matters here
    with env.run_store.get_connection() as conn:
        decisions = [row[0] for row in conn.execute("SELECT status FROM h_completion_decisions ORDER BY decided_at")]
        review = conn.execute("SELECT review_artifact_id FROM h_diff_scope_reviews").fetchone()
    assert decisions and decisions[0] == "FAILED" and "PASS" not in decisions
    findings = json.loads(artifact_text(env, review[0]))["findings"]
    assert {"trivial_assertion_added", "oracle_test_modified"} <= {f["category"] for f in findings}


# ---------------------------------------------------------------- PRD 5
QUEUE_FILES = {
    **CALC,
    "src/text.py": "def shout(s):\n    return s.lower()\n",
    "tests/test_text.py": "from src.text import shout\n\n\ndef test_shout():\n    assert shout('hi') == 'HI'\n",
    "tests/test_total.py": "from src import calc\n\n\ndef test_total():\n    assert calc.total([1, 2, 3]) == 6\n",
}
ISSUES: List[Tuple[int, str, str]] = [
    (1, "add returns wrong result", TASK),
    (2, "shout should uppercase", "shout('hi') should return 'HI' but returns 'hi'. See tests/test_text.py::test_shout"),
    (3, "Implement total", "Depends on #1. Implement total(xs) in src/calc.py summing values with add. See tests/test_total.py::test_total"),
    (4, "Improve performance somewhat", "Make things faster in some way please."),
    (5, "Follow-up tuning", "Blocked by #4. Tune the cache size after the performance work lands."),
]
TOTAL = "    return a + b\n\n\ndef total(xs):\n    result = 0\n    for x in xs:\n        result = add(result, x)\n    return result\n"


def issue_ids(env) -> Dict[int, str]:
    mapping = {}
    for row in env.run_store.get_tasks(env.run_id):
        spec = json.loads(row["task_spec_json"])
        mapping[int(re.search(r"(\d+)$", spec["source_key"]).group(1))] = row["task_id"]
    return mapping


def queue_routes(num: Dict[int, str]):
    return {
        num[1]: [plan_step(), code_step(FIX), complete_step(), validator_step()],
        num[2]: [plan_step(edit_paths=("src/text.py",)), code_step(patch_action("src/text.py", "s.lower()", "s.upper()", "tests/test_text.py")), complete_step(), validator_step()],
        num[3]: [plan_step(), code_step(patch_action("src/calc.py", "    return a + b\n", TOTAL, "tests/test_total.py")), complete_step(), validator_step()],
        num[4]: [needs_input_step()],
        num[5]: [],
    }


def test_development_queue_integrates_dependency_chain_and_blocks_dependents(tmp_path: Path) -> None:
    env = prepare_run(tmp_path, QUEUE_FILES, issues=ISSUES, max_tasks=5)
    before = repo_integrity(env.repo)
    num = issue_ids(env)
    services, _, queue = stack(env, RoutedAdapter(queue_routes(num)))
    final = queue.run(env.run_id)
    states = {entry.task_id: entry.status for entry in final.task_results}
    assert final.status == "PARTIAL_SUCCESS"
    assert [states[num[n]] for n in (1, 2, 3)] == ["INTEGRATED"] * 3
    assert states[num[4]] == "NEEDS_INPUT" and states[num[5]] == "BLOCKED_DEPENDENCY"
    assert final.aggregate_verification.status == "PASS"
    git = services.workspaces.git(env.run_id)
    # One exact commit per integrated issue, linear on the private integration ref.
    chain, cursor = [], final.final_integration.commit
    while cursor != final.baseline.commit:
        chain.append(cursor)
        parents = git.commit_parents(cursor)
        assert len(parents) == 1
        cursor = parents[0]
    assert len(chain) == 3
    assert git.read_ref(queue.integration_ref(env.run_id)) == final.final_integration.commit
    with env.run_store.get_connection() as conn:
        ops = [tuple(r) for r in conn.execute("SELECT operation_kind, state FROM h_git_ref_operations ORDER BY created_at")]
    assert ops == [("CREATE", "APPLIED")] + [("ADVANCE", "APPLIED")] * 3
    assert repo_integrity(env.repo) == before
    # Re-running a settled queue returns the same recorded result, no new work.
    again = queue.run(env.run_id)
    assert again.final_integration.commit == final.final_integration.commit


def test_evaluation_mode_case_is_isolated_and_never_integrated(tmp_path: Path) -> None:
    from harness.contracts import ExecutionMode

    env = prepare_run(tmp_path, CALC, TASK, execution_mode=ExecutionMode.EVALUATION)
    services, _, queue = stack(env, ScriptedAdapter([plan_step(), code_step(FIX), complete_step(), validator_step()]))
    final = queue.run(env.run_id)
    assert final.status == "COMPLETED_ALL"
    assert [entry.status for entry in final.task_results] == ["VERIFIED_NOT_INTEGRATED"]
    candidate = services.workspaces.latest_candidate(env.run_id, env.tasks()[0])
    assert candidate["task_start_commit"] == final.baseline.commit
    assert services.workspaces.git(env.run_id).read_ref(queue.integration_ref(env.run_id)) is None
    with env.run_store.get_connection() as conn:
        cases = [tuple(r) for r in conn.execute("SELECT status, carried_state_from_case_id FROM h_evaluation_cases")]
    assert cases == [("PASS", None)]


def test_cli_run_headless_json_and_exit_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from harness import cli, cli_execution, config as config_module
    from harness.config import HarnessConfig
    from tests.support.harness_fixtures import PROFILE_TOML, make_repo

    repo = make_repo(tmp_path, CALC)
    profiles = tmp_path / "profiles.toml"
    profiles.write_text(PROFILE_TOML, encoding="utf-8")
    data = tmp_path / "data"
    monkeypatch.setattr(config_module, "_config", HarnessConfig(data_dir=data, model_profiles_path=profiles))
    monkeypatch.setattr(cli, "get_config", lambda: config_module._config)
    monkeypatch.setattr(cli_execution, "get_config", lambda: config_module._config)
    monkeypatch.setattr(cli_execution, "ADAPTER_FACTORY", lambda: ScriptedAdapter([plan_step(), code_step(FIX), complete_step(), validator_step()]))
    request = tmp_path / "request.json"
    request.write_text(json.dumps({
        "schema_version": "1.0",
        "idempotency_key": "cli-e2e-0001",
        "task_mode": "single_issue",
        "execution_mode": "development",
        "repository": {"kind": "local_git", "locator": str(repo)},
        "task": {"text": TASK},
        "limits": {"max_tasks": 1},
    }), encoding="utf-8")
    invocation = CliRunner().invoke(cli.app, ["run", "--request", str(request), "--json"])
    payload = json.loads(invocation.stdout)
    assert invocation.exit_code == 0, invocation.stdout[-2000:]
    assert payload["status"] == "COMPLETED_ALL"
    assert payload["publication_authorized"] is False
    status = CliRunner().invoke(cli.app, ["queue", "status", payload["run_id"], "--json"])
    assert status.exit_code == 0 and json.loads(status.stdout)["counts"]["integrated"] == 1
    graph = CliRunner().invoke(cli.app, ["git", "graph", payload["run_id"], "--json"])
    assert json.loads(graph.stdout)["integration"] == payload["final_integration"]["commit"]

    run_id = payload["run_id"]
    store = config_module._config.data_dir / "harness.db"
    import sqlite3

    with sqlite3.connect(store) as conn:
        task_id = conn.execute("SELECT task_id FROM h_tasks WHERE run_id = ?", (run_id,)).fetchone()[0]
        attempt_id = conn.execute("SELECT attempt_id FROM h_verification_attempts WHERE task_id = ?", (task_id,)).fetchone()[0]
        action_id = conn.execute("SELECT action_id FROM h_actions WHERE task_id = ?", (task_id,)).fetchone()[0]
        candidate_id = conn.execute("SELECT candidate_id FROM h_candidate_snapshots WHERE task_id = ?", (task_id,)).fetchone()[0]
    commands = [
        ["report", run_id], ["report", run_id, "--task", task_id], ["task", "inspect", run_id, task_id],
        ["queue", "show", run_id, "--graph", "--artifacts"], ["queue", "plan", run_id],
        ["verification", "contract", run_id, "--task", task_id], ["verification", "baseline", run_id, "--task", task_id],
        ["verification", "checks", attempt_id], ["verification", "compare", attempt_id], ["validator", "review", attempt_id],
        ["action", "inspect", action_id], ["workspace", "status", run_id, "--task", task_id], ["policy", "show", run_id],
        ["repair", run_id, "--task", task_id], ["verify", run_id, "--candidate", candidate_id],
        ["recover", run_id, "--dry-run"], ["approval", "list", run_id], ["sandbox", "doctor"],
    ]
    for command in commands:
        result = CliRunner().invoke(cli.app, [*command, "--json"])
        assert result.exit_code == 0, (command, result.stdout[-1500:])
        assert isinstance(json.loads(result.stdout), dict), command
    logs = CliRunner().invoke(cli.app, ["action", "logs", action_id, "--stream", "stdout"])
    assert logs.exit_code == 0 and "passed" in logs.stdout
    resumed = CliRunner().invoke(cli.app, ["resume", run_id, "--json"])  # settled run: returns the recorded result
    assert resumed.exit_code == 0 and json.loads(resumed.stdout)["status"] == "COMPLETED_ALL"


def test_guided_profile_pauses_for_one_use_approval_then_resumes(tmp_path: Path) -> None:
    from harness.contracts.execution import PermissionProfile

    env = prepare_run(tmp_path, CALC, TASK)
    adapter = ScriptedAdapter([plan_step(), code_step(FIX), complete_step(), validator_step()])
    services = build_services(env.run_store, env.artifact_store, env.data_root, permission_profile=PermissionProfile.GUIDED)
    controller = build_controller(services, env.profile_path, adapter=adapter, budget_limits=BIG_BUDGET)
    queue = QueueCoordinator(run_store=env.run_store, artifact_store=env.artifact_store, services=services, controller=controller)
    paused = queue.run(env.run_id)
    assert paused.queue_state == "PAUSED"
    pending = services.actions.approvals.pending(env.run_id)
    assert len(pending) == 1
    shown = services.actions.approvals.show(pending[0]["approval_request_id"])
    assert shown["request"]["code_sha256"] and shown["state"] == "PENDING"
    with env.run_store.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM h_execution_results").fetchone()[0] == 0  # nothing ran unapproved
    services.actions.approvals.approve(pending[0]["approval_request_id"])
    final = queue.run(env.run_id)
    assert final.status == "COMPLETED_ALL"
    with env.run_store.get_connection() as conn:
        grant = conn.execute("SELECT used_count, state FROM h_action_approval_requests").fetchone()
    assert grant[0] == 1  # one-use grant consumed exactly once
