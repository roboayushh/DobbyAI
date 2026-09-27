"""Evidence-backed release qualification (PRD 6 section 15.3, REL-031).

A gate is PASS only when durable evidence proves it. Missing evidence is
``NOT_RUN``; P1 gates are ``NOT_APPLICABLE`` while P1 effects are disabled. The
live-model gate distinguishes the *prescribed* evaluation model from the local
development bridge: bridge runs are reported, but they never satisfy the gate.
"""
from __future__ import annotations

import datetime
import json
import re
import subprocess
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from typing import Any, Dict, List, Tuple

from harness.config import HARNESS_ROOT, HarnessConfig
from harness.release import secrets

EVIDENCE_DIR = HARNESS_ROOT / "release-evidence"
DEV_BRIDGE_PROFILES = {"claude-bridge"}

GATE_TESTS = {
    "contracts_migrations": ("tests/test_schemas.py", "tests/test_prd345_schemas.py", "tests/test_prd2_migration.py", "tests/test_prd6_contracts.py"),
    "source_safety": ("tests/test_prd1_acceptance.py", "tests/test_prd6_sources.py"),
    "containment": ("tests/test_prd345_e2e.py::test_sandbox_adversarial_battery", "tests/test_prd345_e2e.py::test_timeout_output_bomb_and_reserved_write_are_rolled_back",
                    "tests/test_prd3_dependencies.py::test_setup_network_enforces_destination_allowlist"),
    "verification": ("tests/test_prd4_units.py", "tests/test_prd345_e2e.py::test_test_tampering_never_passes",
                     "tests/test_prd345_e2e.py::test_repair_loop_turns_failed_candidate_into_pass"),
    "queue_git": ("tests/test_prd5_units.py", "tests/test_prd5_recovery.py"),
    "evaluator": ("tests/test_prd6_evaluator.py",),
    "export": ("tests/test_prd6_export.py",),
    "plugins": ("tests/test_prd6_plugins.py",),
}


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _junit_cases(path: Path) -> Dict[str, str]:
    """Map 'tests/test_x.py::test_name' -> PASS/FAIL/SKIP from a pytest JUnit report."""
    cases: Dict[str, str] = {}
    root = ElementTree.parse(path).getroot()
    for case in root.iter("testcase"):
        classname = case.get("classname", "")
        name = case.get("name", "")
        module = classname.replace(".", "/") + ".py" if classname else ""
        status = "PASS"
        for child in case:
            if child.tag in ("failure", "error"):
                status = "FAIL"
            elif child.tag == "skipped" and status == "PASS":
                status = "SKIP"
        cases[f"{module}::{name.split('[')[0]}"] = status if cases.get(f"{module}::{name.split('[')[0]}") != "FAIL" else "FAIL"
    return cases


class ReleaseEvidenceBuilder:
    def __init__(self, config: HarnessConfig, evidence_dir: Path = EVIDENCE_DIR) -> None:
        self.config = config
        self.evidence_dir = Path(evidence_dir)

    def _test_gate(self, cases: Dict[str, str], selectors: Tuple[str, ...]) -> Tuple[str, str]:
        matched: List[str] = []
        for selector in selectors:
            hits = {k: v for k, v in cases.items() if k == selector or k.startswith(selector.split("::")[0] + "::") and "::" not in selector
                    or ("::" in selector and k == selector)}
            if not hits:
                return "NOT_RUN", f"{selector} not in the recorded test report"
            if any(v == "FAIL" for v in hits.values()):
                return "FAIL", f"{selector} failed"
            if all(v == "SKIP" for v in hits.values()):
                return "NOT_RUN", f"{selector} was skipped (environment unavailable)"
            matched.append(f"{selector} ({sum(1 for v in hits.values() if v == 'PASS')} passed)")
        return "PASS", "; ".join(matched)

    def evaluate(self) -> Dict[str, Any]:
        gates: List[Dict[str, Any]] = []

        def gate(name: str, status: str, evidence: str, required: bool = True) -> None:
            gates.append({"gate": name, "status": status, "evidence": evidence[:400], "required": required})

        junit = self.evidence_dir / "tests" / "junit.xml"
        cases = _junit_cases(junit) if junit.is_file() else {}
        for name, selectors in GATE_TESTS.items():
            if not cases:
                gate(name, "NOT_RUN", "no recorded test report (run `make test`)")
            else:
                gate(name, *self._test_gate(cases, selectors))
        # Live model: only the prescribed (non-bridge) model satisfies the gate.
        live_dir = self.evidence_dir / "live"
        prescribed, bridge = [], []
        for path in sorted(live_dir.glob("*.json")) if live_dir.is_dir() else []:
            try:
                record = json.loads(path.read_text())
            except ValueError:
                continue
            if record.get("configuration") == "baseline_single_role":
                continue  # comparison baseline, not harness evidence
            (bridge if record.get("model_profile") in DEV_BRIDGE_PROFILES else prescribed).append(record)
        passing = [r for r in prescribed if r.get("status") in ("PASS", "COMPLETED_ALL")]
        if passing:
            gate("model", "PASS", f"{len(passing)}/{len(prescribed)} prescribed-model runs passed ({', '.join(sorted({r['model_profile'] for r in passing}))})")
        else:
            note = f"LIVE_MODEL_NOT_RUN for the prescribed model; development-bridge runs: {sum(1 for r in bridge if r.get('status') in ('PASS', 'COMPLETED_ALL'))}/{len(bridge)} passed"
            gate("model", "NOT_RUN", note)
        # REL-034: a comparison counts only when it used the prescribed model; bridge comparisons are methodology checks.
        summaries = sorted((self.evidence_dir / "comparison").glob("summary-*.json")) if (self.evidence_dir / "comparison").is_dir() else []
        qualifying = []
        for path in summaries:
            try:
                summary = json.loads(path.read_text())
            except ValueError:
                continue
            if summary.get("prescribed_model_evidence") and summary.get("model_profile") not in DEV_BRIDGE_PROFILES:
                qualifying.append(summary["model_profile"])
        if qualifying:
            gate("comparative_evaluation", "PASS", f"baseline vs harness recorded for {', '.join(sorted(qualifying))}")
        else:
            gate("comparative_evaluation", "NOT_RUN",
                 f"no prescribed-model comparison; development-bridge comparisons recorded: {len(summaries)}")
        clean = sorted((self.evidence_dir / "clean-machine").glob("*.json")) if (self.evidence_dir / "clean-machine").is_dir() else []
        if clean:
            record = json.loads(clean[-1].read_text())
            gate("setup_clean_machine", "PASS" if record.get("status") == "PASS" else "FAIL",
                 f"{record.get('environment')}: {record.get('summary', '')}")
        else:
            gate("setup_clean_machine", "NOT_RUN", "no clean-machine setup/test/headless-run evidence")
        docs = ["README.md", "docs/prd6-gap-report.md", "docs/configuration.md", "docs/evaluator.md", "docs/security.md",
                "docs/limitations.md", "docs/provenance.md", "docs/profiles.md"]
        missing = [d for d in docs if not (HARNESS_ROOT / d).is_file()]
        gate("documentation", "PASS" if not missing else "FAIL", "all present" if not missing else "missing: " + ", ".join(missing))
        provenance = ["licenses/THIRD_PARTY_NOTICES.md", "licenses/sbom.cdx.json", "requirements.lock", "runtime/python/requirements.lock"]
        missing = [d for d in provenance if not (HARNESS_ROOT / d).is_file()]
        gate("provenance_licenses", "PASS" if not missing else "FAIL", "SBOM, notices, locks present" if not missing else "missing: " + ", ".join(missing))
        gate("secret_scan", *self._secret_scan())
        gate("optional_apply_publish", "NOT_APPLICABLE", "P1 apply/publish disabled in this release", required=False)
        required = [g for g in gates if g["required"]]
        status = "QUALIFIED" if all(g["status"] == "PASS" for g in required) else "NOT_QUALIFIED"
        return {"schema_version": "1.0", "status": status, "gates": gates, "created_at": _now(),
                "note": "A gate not executed is NOT_RUN, never PASS."}

    def _secret_scan(self) -> Tuple[str, str]:
        try:
            files = subprocess.run(["git", "ls-files"], cwd=HARNESS_ROOT, capture_output=True, text=True, timeout=30).stdout.splitlines()
        except (OSError, subprocess.SubprocessError):
            return "NOT_RUN", "git ls-files unavailable"
        untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"], cwd=HARNESS_ROOT,
                                   capture_output=True, text=True, timeout=30).stdout.splitlines()
        flagged = []
        for name in files + untracked:
            path = HARNESS_ROOT / name
            if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024 or name.startswith(("data/", "release-evidence/live/")):
                continue
            if name.startswith("tests/") or "secrets.py" in name:
                continue  # fixtures deliberately contain fake sentinels
            try:
                if secrets.scan_bytes(path.read_bytes(), known=secrets.known_secret_values()):
                    flagged.append(name)
            except OSError:
                continue
        return ("PASS", f"{len(files) + len(untracked)} files scanned, none flagged") if not flagged else ("FAIL", "flagged: " + ", ".join(flagged[:10]))
