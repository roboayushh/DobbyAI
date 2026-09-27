#!/usr/bin/env python3
"""Live end-to-end evaluation through the exact headless evaluator path.

For each case (a repository with a known defect and a symptom-only issue text):
  1. submit a native_json_v1 request via ``harness run --input ... --non-interactive``
     (the configured prescribed model; locally the claude-bridge profile);
  2. apply the exported patch.diff to a clean copy of the original repository;
  3. run the project's own full test suite ("hidden tests") in a fresh
     python:3.12-slim container, before and after the patch;
  4. record metrics to release-evidence/live/<case>.json.

Usage:
  AI_API_KEY=... HARNESS_MODEL_PROFILE=claude-bridge python scripts/live_eval.py cases.json --work /tmp/live
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / ".venv" / "bin" / "harness"


def hidden_tests(repo: Path, install: str, test_cmd: str) -> dict:
    """Run the project's own suite in a clean container (network only for pip)."""
    script = f"set -e; cd /w; {install} >/tmp/install.log 2>&1 || (tail -20 /tmp/install.log; exit 3); {test_cmd} -rfE --color=no"
    proc = subprocess.run(["docker", "run", "--rm", "-e", "SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0", "-v", f"{repo}:/w", "python:3.12-slim", "sh", "-c", script],
                          capture_output=True, text=True, timeout=1200)
    lines = (proc.stdout + proc.stderr).strip().splitlines()
    summary = next((line for line in reversed(lines[-3:]) if " passed" in line or " failed" in line or " error" in line),
                   " | ".join(lines[-3:]))
    failing = sorted({line.split()[1] for line in lines if line.startswith(("FAILED ", "ERROR ")) and len(line.split()) > 1})
    return {"exit_code": proc.returncode, "summary": summary.strip(), "failing": failing}


def score_patch(case: dict, case_dir: Path, patch: Path) -> dict:
    """Apply ``patch`` to a clean copy, restore the original test files, and run the hidden suite.

    Restoring the tests means a configuration cannot pass by editing the oracle; the
    same scorer is used for the harness and for the baseline.
    """
    if not case.get("hidden_install") or not patch.is_file():
        return {}
    source = Path(case["repo"])
    scored: dict = {}
    clean = case_dir / "hidden-before"
    if not clean.exists():
        shutil.copytree(source, clean, ignore=shutil.ignore_patterns(".git"))
    before = hidden_tests(clean, case["hidden_install"], case["hidden_test"])
    patched = case_dir / "hidden-after"
    if patched.exists():
        shutil.rmtree(patched)
    shutil.copytree(source, patched, ignore=shutil.ignore_patterns(".git"))
    empty = not patch.read_text().strip()
    applied = subprocess.run(["git", "apply", "--whitespace=nowarn", str(patch)], cwd=patched, capture_output=True, text=True)
    scored["patch_empty"] = empty
    scored["patch_applies"] = applied.returncode == 0 and not empty
    for rel in case.get("test_paths", ["tests"]):
        if (patched / rel).exists():
            shutil.rmtree(patched / rel) if (patched / rel).is_dir() else (patched / rel).unlink()
        if (source / rel).is_dir():
            shutil.copytree(source / rel, patched / rel, ignore=shutil.ignore_patterns("__pycache__"))
        elif (source / rel).exists():
            shutil.copy2(source / rel, patched / rel)
    after = hidden_tests(patched, case["hidden_install"], case["hidden_test"]) if scored["patch_applies"] else None
    scored["hidden_before"] = {k: before[k] for k in ("exit_code", "summary")} | {"failing_count": len(before["failing"])}
    scored["hidden_after"] = after and {k: after[k] for k in ("exit_code", "summary")} | {"failing_count": len(after["failing"])}
    scored["hidden_pass"] = bool(after and after["exit_code"] == 0)
    scored["new_regressions"] = sorted(set(after["failing"]) - set(before["failing"])) if after else []
    return scored


def run_case(case: dict, work: Path, profile: str, trial: int = 1, budgets: dict | None = None) -> dict:
    name = case["name"]
    case_dir = work / f"{name}-harness-t{trial}"
    if case_dir.exists():
        subprocess.run(["chmod", "-R", "u+w", str(case_dir)])
        shutil.rmtree(case_dir)
    case_dir.mkdir(parents=True)
    out = case_dir / "out"
    out.mkdir()
    request = {
        "schema_version": "1.0", "request_id": f"ereq_{name}_{int(time.time())}",
        "adapter": {"name": "native_json_v1", "version": "1.0.0"},
        "repository": {"kind": case.get("kind", "local_git"), "locator": case["repo"]},
        "task_mode": "single_issue", "execution_mode": "evaluation",
        "task": {"source_type": "direct_text", "text": case["issue"]},
        "model_config_ref": profile, "budgets": budgets or case.get("budgets", {}),
        "result_path": str(out / "result.json"), "export_path": str(out / "bundle"),
        "requested_effects": ["EXPORT"], "idempotency_key": f"live-{name}-{int(time.time())}",
    }
    (case_dir / "request.json").write_text(json.dumps(request, indent=2))
    env = {**os.environ, "DATA_DIR": str(case_dir / "data")}
    started = time.monotonic()
    proc = subprocess.run([str(HARNESS), "run", "--input", str(case_dir / "request.json"), "--non-interactive"],
                          capture_output=True, text=True, env=env, timeout=case.get("timeout", 3600))
    elapsed = time.monotonic() - started
    (case_dir / "stderr.log").write_text(proc.stderr)
    lines = [line for line in proc.stdout.strip().splitlines() if line.strip()]
    result = json.loads(lines[-1]) if lines else {"status": "NO_OUTPUT"}
    record = {
        "case": name, "configuration": "harness_full", "trial": trial, "budget": budgets or case.get("budgets", {}),
        "model_profile": profile, "status": result.get("status"), "exit_code": proc.returncode,
        "stdout_json_objects": len(lines), "wall_seconds": round(elapsed, 1), "usage": result.get("usage"),
        "verification": (result.get("verification") or {}).get("status"), "export": (result.get("export") or {}).get("status"),
        "changed_paths": [p for t in result.get("tasks", []) for p in t.get("changed_paths", [])],
        "limitations": result.get("limitations", []), "run_id": result.get("run_id"),
    }
    record.update(score_patch(case, case_dir, out / "bundle" / "patch.diff"))
    return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("cases")
    parser.add_argument("--work", required=True)
    parser.add_argument("--profile", default=os.environ.get("HARNESS_MODEL_PROFILE", "designated"))
    parser.add_argument("--only", default=None)
    parser.add_argument("--trial", type=int, default=1)
    parser.add_argument("--model-calls", type=int, default=None, help="same call budget as the baseline for comparisons")
    parser.add_argument("--wall-seconds", type=int, default=None)
    parser.add_argument("--evidence", default=str(ROOT / "release-evidence" / "live"))
    args = parser.parse_args()
    cases = json.loads(Path(args.cases).read_text())
    work = Path(args.work)
    evidence = Path(args.evidence)
    evidence.mkdir(parents=True, exist_ok=True)
    records = []
    for case in cases:
        if args.only and case["name"] != args.only:
            continue
        print(f"== {case['name']} ...", file=sys.stderr, flush=True)
        budgets = {k: v for k, v in (("model_calls", args.model_calls), ("wall_seconds", args.wall_seconds)) if v}
        record = run_case(case, work, args.profile, args.trial, budgets or None)
        records.append(record)
        (evidence / f"{case['name']}-harness-{args.profile}-t{args.trial}.json").write_text(json.dumps(record, indent=2) + "\n")
        print(json.dumps(record, indent=2), file=sys.stderr, flush=True)
    print(json.dumps(records))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
