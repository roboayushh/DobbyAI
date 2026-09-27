"""PRD 6 release evidence truthfulness, provenance artifacts, and secret hygiene (REL-031..REL-033)."""
from __future__ import annotations

import json
from pathlib import Path

from harness.config import HarnessConfig
from harness.release.evidence import ReleaseEvidenceBuilder
from harness.release import secrets

ROOT = Path(__file__).resolve().parents[1]


def test_unexecuted_gates_are_never_pass(tmp_path: Path) -> None:
    report = ReleaseEvidenceBuilder(HarnessConfig(data_dir=tmp_path), evidence_dir=tmp_path / "empty").evaluate()
    by_gate = {g["gate"]: g for g in report["gates"]}
    assert report["status"] == "NOT_QUALIFIED"
    assert by_gate["model"]["status"] == "NOT_RUN" and "LIVE_MODEL_NOT_RUN" in by_gate["model"]["evidence"]
    assert by_gate["setup_clean_machine"]["status"] == "NOT_RUN"
    assert by_gate["containment"]["status"] == "NOT_RUN"
    assert by_gate["optional_apply_publish"]["status"] == "NOT_APPLICABLE"


def test_development_bridge_runs_never_satisfy_the_prescribed_model_gate(tmp_path: Path) -> None:
    live = tmp_path / "ev" / "live"
    live.mkdir(parents=True)
    (live / "a.json").write_text(json.dumps({"model_profile": "claude-bridge", "status": "PASS"}))
    report = ReleaseEvidenceBuilder(HarnessConfig(data_dir=tmp_path), evidence_dir=tmp_path / "ev").evaluate()
    model = next(g for g in report["gates"] if g["gate"] == "model")
    assert model["status"] == "NOT_RUN" and "1/1 passed" in model["evidence"]
    (live / "b.json").write_text(json.dumps({"model_profile": "deepseek", "status": "PASS"}))
    report = ReleaseEvidenceBuilder(HarnessConfig(data_dir=tmp_path), evidence_dir=tmp_path / "ev").evaluate()
    assert next(g for g in report["gates"] if g["gate"] == "model")["status"] == "PASS"


def test_baseline_records_and_bridge_comparisons_are_not_release_evidence(tmp_path: Path) -> None:
    ev = tmp_path / "ev"
    (ev / "live").mkdir(parents=True)
    (ev / "comparison").mkdir()
    (ev / "live" / "b.json").write_text(json.dumps({"model_profile": "deepseek", "status": "PASS",
                                                    "configuration": "baseline_single_role"}))
    (ev / "comparison" / "summary-claude-bridge.json").write_text(json.dumps(
        {"model_profile": "claude-bridge", "prescribed_model_evidence": False}))
    gates = {g["gate"]: g for g in ReleaseEvidenceBuilder(HarnessConfig(data_dir=tmp_path), evidence_dir=ev).evaluate()["gates"]}
    assert gates["model"]["status"] == "NOT_RUN" and gates["comparative_evaluation"]["status"] == "NOT_RUN"
    (ev / "comparison" / "summary-qwen.json").write_text(json.dumps({"model_profile": "qwen", "prescribed_model_evidence": True}))
    gates = {g["gate"]: g for g in ReleaseEvidenceBuilder(HarnessConfig(data_dir=tmp_path), evidence_dir=ev).evaluate()["gates"]}
    assert gates["comparative_evaluation"]["status"] == "PASS"


def test_test_gates_read_the_recorded_junit(tmp_path: Path) -> None:
    ev = tmp_path / "ev" / "tests"
    ev.mkdir(parents=True)
    (ev / "junit.xml").write_text(
        '<testsuites><testsuite>'
        '<testcase classname="tests.test_prd4_units" name="test_a"/>'
        '<testcase classname="tests.test_prd345_e2e" name="test_test_tampering_never_passes"/>'
        '<testcase classname="tests.test_prd345_e2e" name="test_repair_loop_turns_failed_candidate_into_pass"><skipped/></testcase>'
        '</testsuite></testsuites>')
    report = ReleaseEvidenceBuilder(HarnessConfig(data_dir=tmp_path), evidence_dir=tmp_path / "ev").evaluate()
    verification = next(g for g in report["gates"] if g["gate"] == "verification")
    assert verification["status"] == "NOT_RUN" and "skipped" in verification["evidence"]


def test_provenance_files_ship_with_the_release() -> None:
    sbom = json.loads((ROOT / "licenses" / "sbom.cdx.json").read_text())
    names = {c["name"] for c in sbom["components"]}
    assert {"httpx", "pydantic", "pytest"} <= names and sbom["bomFormat"] == "CycloneDX"
    assert (ROOT / "licenses" / "THIRD_PARTY_NOTICES.md").is_file()
    lock = (ROOT / "requirements.lock").read_text()
    assert "--hash=sha256:" in lock


def test_secret_scanner_flags_sentinels_but_not_counters() -> None:
    assert secrets.scan({"budgets": {"input_tokens": 10}}) == []
    assert secrets.scan({"auth_token": "x"}) == ["$.auth_token"]
    assert secrets.scan({"note": "key sk-abcdefghijklmnopqrstuvwx"}) == ["$.note"]
    assert secrets.scan_bytes(b"ghp_" + b"a" * 30)
    assert secrets.scan({"text": "SENTINELVALUE12345"}, known=["SENTINELVALUE12345"]) == ["$.text"]


def test_model_profiles_file_contains_no_credentials() -> None:
    text = (ROOT / "config" / "model_profiles.toml").read_text()
    assert not secrets.scan_bytes(text.encode(), known=[])
    assert "api_key" not in text.lower().replace("ai_api_key", "")
