"""PRD 6 evaluator boundary and CLI contract (REL-001..REL-008, AT6-001..AT6-016, AT6-053)."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from tests.support.cli_env import configure, evaluator_request
from tests.support.harness_fixtures import make_repo
from tests.support.scripted_model import ScriptedAdapter, code_step, complete_step, patch_action, plan_step, validator_step

CALC = {
    "src/__init__.py": "",
    "src/calc.py": "def add(a, b):\n    return a - b\n",
    "tests/test_calc.py": "from src.calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
}
FIX = patch_action("src/calc.py", "return a - b", "return a + b", "tests/test_calc.py")


def script():
    return ScriptedAdapter([plan_step(), code_step(FIX), complete_step(), validator_step()])


def run_count(env) -> int:
    db = env.config.data_dir / "harness.db"
    if not db.exists():
        return 0
    with sqlite3.connect(db) as conn:
        return conn.execute("SELECT COUNT(*) FROM h_runs").fetchone()[0]


def write_request(env, request) -> Path:
    path = env.root / f"request-{abs(hash(json.dumps(request, sort_keys=True))) % 10**8}.json"
    path.write_text(json.dumps(request))
    return path


@pytest.mark.parametrize("mutate, code", [
    (lambda r: r.update(schema_version="2.0"), "UNSUPPORTED_SCHEMA_VERSION"),
    (lambda r: r["task"].update(text="use sk-abcdefghijklmnopqrstuvwxyz to call"), "SECRET_IN_REQUEST"),
    (lambda r: r.update(adapter={"name": "official_hackathon_v1", "version": "1.0.0"}), "OFFICIAL_ADAPTER_NOT_CONFIGURED"),
    (lambda r: r.update(requested_effects=["EXPORT", "PUSH_NEW_BRANCH"]), "CAPABILITY_DISABLED"),
    (lambda r: r.update(task_mode="repository", task={"source_type": "repository_query"}), "INVALID_REQUEST"),
    (lambda r: r.update(profile="nonexistent_profile"), "INVALID_RELEASE_PROFILE"),
    (lambda r: r.update(runtime_profile="node20_v1"), "UNKNOWN_RUNTIME_PROFILE"),
    (lambda r: r.update(unexpected=True), "INVALID_REQUEST"),
    (lambda r: r.update(budgets={"model_calls": 2}), "INVALID_REQUEST"),  # cannot fit the verification reserve
])
def test_invalid_requests_fail_before_any_mutation(tmp_path: Path, monkeypatch, mutate, code: str) -> None:
    env = configure(monkeypatch, tmp_path, adapter_factory=script)
    repo = make_repo(tmp_path, CALC)
    (tmp_path / "out").mkdir()
    request = evaluator_request(repo, tmp_path / "out")
    mutate(request)
    out = env.json("run", "--input", str(write_request(env, request)), "--non-interactive")
    assert out["exit_code"] == 2
    assert out["body"]["status"] == "INVALID" and out["body"]["error"]["code"] == code
    assert len(out["stdout"].strip().splitlines()) == 1  # exactly one JSON object on stdout
    assert run_count(env) == 0  # no preparation, no repository mutation, no model call
    assert json.loads((tmp_path / "out" / "result.json").read_text())["status"] == "INVALID"


def test_output_inside_source_repository_is_rejected(tmp_path: Path, monkeypatch) -> None:
    env = configure(monkeypatch, tmp_path, adapter_factory=script)
    repo = make_repo(tmp_path, CALC)
    request = evaluator_request(repo, repo / "out")
    out = env.json("run", "--input", str(write_request(env, request)), "--non-interactive")
    assert out["body"]["error"]["code"] == "INVALID_OUTPUT_PATH" and run_count(env) == 0
    assert not (repo / "out").exists()


def test_placeholder_model_and_missing_key_are_prerequisite_failures(tmp_path: Path, monkeypatch) -> None:
    env = configure(monkeypatch, tmp_path, adapter_factory=None)
    monkeypatch.delenv("AI_API_KEY", raising=False)
    repo = make_repo(tmp_path, CALC)
    (tmp_path / "out").mkdir()
    out = env.json("run", "--input", str(write_request(env, evaluator_request(repo, tmp_path / "out"))), "--non-interactive")
    assert out["exit_code"] == 2 and out["body"]["error"]["code"] == "MODEL_PROFILE_INCOMPLETE"
    good = (Path(__file__).resolve().parents[1] / "config" / "model_profiles.toml").read_text()
    env = configure(monkeypatch, tmp_path, adapter_factory=None, model_profile="deepseek", profiles_text=good)
    out = env.json("run", "--input", str(write_request(env, evaluator_request(repo, tmp_path / "out", request_id="ereq_2"))), "--non-interactive")
    assert out["body"]["error"]["code"] == "MODEL_AUTH_MISSING" and run_count(env) == 0


def test_malformed_and_missing_request_files_are_typed(tmp_path: Path, monkeypatch) -> None:
    env = configure(monkeypatch, tmp_path, adapter_factory=script)
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    for path in (bad, tmp_path / "missing.json"):
        out = env.json("run", "--input", str(path), "--non-interactive")
        assert out["exit_code"] == 2 and out["body"]["status"] == "INVALID"


def test_disabled_p1_commands_are_typed_not_simulated(tmp_path: Path, monkeypatch) -> None:
    env = configure(monkeypatch, tmp_path)
    for args in (("apply", "run_x", "--target", str(tmp_path)), ("publish", "run_x", "--remote", "origin", "--branch", "b")):
        out = env.json(*args, "--json")
        assert out["exit_code"] == 2 and out["body"]["error"]["code"] == "CAPABILITY_DISABLED"


def test_version_plugins_and_doctor_emit_single_json_objects(tmp_path: Path, monkeypatch) -> None:
    env = configure(monkeypatch, tmp_path)
    version = env.json("version", "--json")
    assert version["exit_code"] == 0 and version["body"]["build_id"].startswith("build_")
    plugins = env.json("plugins", "list", "--json")
    assert plugins["exit_code"] == 0 and len(plugins["body"]["plugins"]) == 10
    monkeypatch.delenv("AI_API_KEY", raising=False)
    doctor = env.json("doctor", "--json")
    assert doctor["exit_code"] == 2 and doctor["body"]["status"] == "BLOCKED"
    assert {"model_profile", "api_key"} <= set(doctor["body"]["blocking_check_ids"])
    assert "sk-" not in doctor["stdout"]
    submission = env.json("doctor", "--profile", "submission", "--json")
    assert "official_adapter" in submission["body"]["blocking_check_ids"]


def test_unreachable_sandbox_is_blocked_environment_never_host_execution(tmp_path: Path, monkeypatch) -> None:
    """REL-008: without a container engine the run is BLOCKED_ENVIRONMENT (exit 3); nothing runs on the host."""
    adapter = script()
    env = configure(monkeypatch, tmp_path, adapter_factory=lambda: adapter)
    monkeypatch.setenv("DOCKER_HOST", f"unix://{tmp_path}/no-engine.sock")
    repo = make_repo(tmp_path, CALC)
    before = (repo / "src" / "calc.py").read_bytes()
    (tmp_path / "out").mkdir()
    out = env.json("run", "--input", str(write_request(env, evaluator_request(repo, tmp_path / "out"))), "--non-interactive")
    body = out["body"]
    assert out["exit_code"] == 3 and body["status"] == "BLOCKED_ENVIRONMENT"
    assert any("never falls back to host execution" in item for item in body["limitations"])
    assert adapter.calls == []  # no model call is spent on a run that cannot execute
    assert (repo / "src" / "calc.py").read_bytes() == before
    # Nothing ran, so the only exportable state is the unchanged baseline: an empty patch.
    assert body["candidate"] is None or body["candidate"]["commit"] == body["input"]["baseline_commit"]
    assert body["export"]["patch_sha256"] == hashlib.sha256(b"").hexdigest()
    assert json.loads((tmp_path / "out" / "result.json").read_text())["status"] == "BLOCKED_ENVIRONMENT"


def test_non_interactive_run_never_prompts(tmp_path: Path, monkeypatch) -> None:
    env = configure(monkeypatch, tmp_path, adapter_factory=script)
    result = env.invoke("run", "--non-interactive", "--json")
    assert result.exit_code == 2
