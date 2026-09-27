"""PRD 6 end to end against the real Docker sandbox (model replies scripted, everything else real)."""
from __future__ import annotations

import json
import shutil
import sqlite3
import zipfile
from pathlib import Path

import pytest

from tests.support.cli_env import PROFILE_TOML, configure, evaluator_request
from tests.support.harness_fixtures import make_repo, repo_integrity
from tests.test_prd345_e2e import _runtime_ready
from tests.test_prd6_evaluator import CALC, script, write_request

pytestmark = pytest.mark.skipif(not _runtime_ready(), reason="Docker sandbox runtime unavailable")


def run_count(env) -> int:
    with sqlite3.connect(env.config.data_dir / "harness.db") as conn:
        return conn.execute("SELECT COUNT(*) FROM h_runs").fetchone()[0]


def test_headless_evaluation_run_exports_a_round_tripped_patch(tmp_path: Path, monkeypatch) -> None:
    env = configure(monkeypatch, tmp_path, adapter_factory=script)
    repo = make_repo(tmp_path, CALC)
    before = repo_integrity(repo)
    out = tmp_path / "out"
    out.mkdir()
    request = evaluator_request(repo, out)
    path = write_request(env, request)
    first = env.json("run", "--input", str(path), "--non-interactive")
    body = first["body"]
    assert first["exit_code"] == 0 and body["status"] == "PASS"
    assert len(first["stdout"].strip().splitlines()) == 1
    assert body["export"]["status"] == "VALID" and body["verification"]["status"] == "PASS"
    assert body["tasks"][0]["changed_paths"] == ["src/calc.py"]
    assert json.loads((out / "result.json").read_text()) == body
    patch = (out / "bundle" / "patch.diff").read_text()
    assert "+    return a + b" in patch
    manifest = json.loads((out / "bundle" / "manifest.json").read_text())
    assert manifest["round_trip"]["status"] == "PASS" and manifest["candidate"]["commit"] == body["candidate"]["commit"]
    assert repo_integrity(repo) == before
    # REL-012: the report states what was observed, the usage, and the limitations truthfully.
    report = (out / "bundle" / "report.md").read_text()
    assert "**Result:** `PASS`" in report and "Required checks:" in report and "failed 0" in report
    assert f"Model calls: {body['usage']['model_calls']}" in report and "## Limitations" in report
    assert "does not guarantee hidden-test success" in report and "Nothing was pushed" in report
    # REL-021: the reproducibility manifest binds model, adapter, image, source, checks, plugins, and schemas.
    provenance = json.loads((out / "bundle" / "evidence" / "provenance.json").read_text())
    assert provenance["source"]["candidate_commit"] == body["candidate"]["commit"]
    assert provenance["model"]["config_sha256"] and provenance["runtime"]["image_digest"].startswith("sha256:")
    assert provenance["plugin_set_lock_sha256"] and provenance["evaluator_adapter"]["name"] == "native_json_v1"
    assert provenance["verification"]["contract_set_sha256"] and provenance["schemas"]["result"] == "1.0"
    assert body["reproducibility_manifest_artifact_id"]
    # Idempotent: the same request returns the recorded result without a new run.
    again = env.json("run", "--input", str(path), "--non-interactive")
    assert again["body"]["run_id"] == body["run_id"] and run_count(env) == 1
    # Same request_id with different content is a conflict.
    changed = dict(request, task={"source_type": "direct_text", "text": "Something else entirely, please."})
    conflict = env.json("run", "--input", str(write_request(env, changed)), "--non-interactive")
    assert conflict["exit_code"] == 2 and conflict["body"]["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    # Audit replay reconstructs the terminal projection without effects; reverify reruns the checks.
    audit = env.json("replay", body["run_id"], "--mode", "audit", "--json")
    assert audit["exit_code"] == 0 and audit["body"]["state"] == "PASS" and audit["body"]["artifacts_verified"] > 10
    reverify = env.json("replay", body["run_id"], "--mode", "reverify", "--json")
    assert reverify["body"]["state"] == "PASS" and reverify["body"]["executed_checks"] >= 1
    unsupported = env.json("replay", body["run_id"], "--mode", "recorded", "--json")
    assert unsupported["body"]["reason"] == "REPLAY_MODE_UNSUPPORTED"
    # A second, separate export through the CLI is read-only and valid.
    exported = env.json("export", body["run_id"], "--output", str(tmp_path / "second"), "--json")
    assert exported["exit_code"] == 0 and exported["body"]["status"] == "VALID"


@pytest.mark.parametrize("kind", ["local_folder", "local_zip"])
def test_folder_and_zip_sources_run_end_to_end(tmp_path: Path, monkeypatch, kind: str) -> None:
    env = configure(monkeypatch, tmp_path, adapter_factory=script)
    source = tmp_path / "plain project"
    for name, content in CALC.items():
        (source / name).parent.mkdir(parents=True, exist_ok=True)
        (source / name).write_text(content)
    locator = source
    if kind == "local_zip":
        locator = tmp_path / "project.zip"
        with zipfile.ZipFile(locator, "w") as bundle:
            for name, content in CALC.items():
                bundle.writestr(f"project-main/{name}", content)
    out = tmp_path / "out"
    out.mkdir()
    request = evaluator_request(source, out, repository={"kind": kind, "locator": str(locator)}, request_id=f"ereq_{kind}",
                                idempotency_key=f"eval-{kind}-0001")
    result = env.json("run", "--input", str(write_request(env, request)), "--non-interactive")
    assert result["exit_code"] == 0 and result["body"]["status"] == "PASS"
    assert not (source / ".git").exists()


def test_requested_cleanup_is_pending_until_approved(tmp_path: Path, monkeypatch) -> None:
    env = configure(monkeypatch, tmp_path, adapter_factory=script)
    repo = make_repo(tmp_path, CALC)
    out = tmp_path / "out"
    out.mkdir()
    request = evaluator_request(repo, out, requested_effects=["EXPORT", "CLEANUP_RUN"], request_id="ereq_clean",
                                idempotency_key="eval-clean-0001")
    result = env.json("run", "--input", str(write_request(env, request)), "--non-interactive")
    body = result["body"]
    assert result["exit_code"] == 6 and body["status"] == "PENDING_APPROVAL"
    assert body["tasks"][0]["status"] == "PASS" and body["export"]["status"] == "VALID"
    request_id = next(p["capability_request_id"] for p in body["pending_approvals"] if p["operation"] == "CLEANUP_RUN")
    run_root = env.config.data_dir / "runs" / body["run_id"]
    assert (run_root / "workspace").exists()  # nothing happened without approval
    shown = env.json("approval", "show", request_id, "--json")
    assert shown["body"]["state"] == "PENDING"
    assert env.json("approve", request_id, "--json")["exit_code"] == 0
    cleaned = env.json("clean", body["run_id"], "--json")
    assert cleaned["exit_code"] == 0 and cleaned["body"]["status"] == "CLEANED"
    assert not (run_root / "workspace").exists() and (run_root / "artifacts").is_dir()
    assert (out / "bundle" / "patch.diff").is_file()  # exports are never cleanup targets


def test_small_evaluator_budgets_run_instead_of_crashing(tmp_path: Path, monkeypatch) -> None:
    """Budgets below the profile-derived reserves (slow timeout, large max output) still run truthfully."""
    slow = "\n".join(line if not line.startswith("request_timeout_seconds") else "request_timeout_seconds = 600"
                     for line in PROFILE_TOML.splitlines())
    env = configure(monkeypatch, tmp_path, adapter_factory=script, profiles_text=slow)
    repo = make_repo(tmp_path, CALC)
    out = tmp_path / "out"
    out.mkdir()
    request = evaluator_request(repo, out, budgets={"model_calls": 8, "wall_seconds": 600, "output_tokens": 12000},
                                request_id="ereq_small", idempotency_key="eval-small-0001")
    result = env.json("run", "--input", str(write_request(env, request)), "--non-interactive")
    assert result["body"]["status"] != "INTERNAL_ERROR", result["body"].get("limitations")
    assert result["exit_code"] == 0 and result["body"]["status"] == "PASS"


def test_a_planner_that_never_converges_fails_the_task_not_the_harness(tmp_path: Path, monkeypatch) -> None:
    """A model-behaviour limit is a truthful task FAILURE with its typed reason, never INTERNAL_ERROR."""
    from tests.support.scripted_model import ScriptedAdapter

    ask = lambda view: {"schema_version": "1.0", "decision": "NEEDS_EVIDENCE", "task_id": view.task_id,  # noqa: E731
                        "queries": [{"query_type": "PATH_GLOB", "query": "src/calc.py"}], "reason": "Read it again."}
    env = configure(monkeypatch, tmp_path, adapter_factory=lambda: ScriptedAdapter([ask] * 5))
    repo = make_repo(tmp_path, CALC)
    before = repo_integrity(repo)
    out = tmp_path / "out"
    out.mkdir()
    request = evaluator_request(repo, out, request_id="ereq_stuck", idempotency_key="eval-stuck-0001")
    result = env.json("run", "--input", str(write_request(env, request)), "--non-interactive")
    body = result["body"]
    assert body["status"] == "FAILED" and result["exit_code"] == 4, body.get("limitations")
    assert not any("INTERNAL_ERROR" in item for item in body["limitations"])
    assert body["export"]["status"] == "VALID" and repo_integrity(repo) == before
