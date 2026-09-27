#!/usr/bin/env python3
"""Comparative evaluation driver (PRD 6 section 15, REL-034).

Runs the full harness (``live_eval.py``) and the single-role shell-loop baseline
(``baseline_loop.py``) on the same cases, starting commits, runtime, model
profile, sampling settings, and call/wall budgets, for N trials, then writes
``release-evidence/comparison/summary.{json,md}`` with development and held-out
results reported separately.

  AI_API_KEY=... python scripts/compare_eval.py run cases.json --work /tmp/cmp --profile deepseek --trials 2
  python scripts/compare_eval.py summarize --profile deepseek
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
CONFIGS = ("baseline_single_role", "harness_full")


def run(args: argparse.Namespace) -> None:
    evidence = Path(args.evidence)
    common = ["--work", args.work, "--profile", args.profile, "--model-calls", str(args.model_calls),
              "--wall-seconds", str(args.wall_seconds), "--evidence", str(evidence)]
    cases = json.loads(Path(args.cases).read_text())
    for trial in range(1, args.trials + 1):
        for case in cases:
            if args.only and case["name"] != args.only:
                continue
            for script in ("baseline_loop.py", "live_eval.py"):
                subprocess.run([PY, str(ROOT / "scripts" / script), args.cases, *common, "--only", case["name"],
                                "--trial", str(trial)], check=False, stdout=subprocess.DEVNULL)
    (evidence / "cases.json").write_text(json.dumps(
        [{k: c[k] for k in ("name", "split", "issue", "hidden_test")} for c in cases], indent=2) + "\n")
    summarize(args)


def _records(evidence: Path, profile: str) -> list[dict]:
    rows = []
    for path in sorted(evidence.glob(f"*-{profile}-t*.json")):
        record = json.loads(path.read_text())
        if record.get("configuration") in CONFIGS:
            rows.append(record)
    return rows


def _aggregate(rows: list[dict]) -> dict:
    solved = [r for r in rows if r.get("hidden_pass")]
    calls = [((r.get("usage") or {}).get("model_calls") or 0) for r in rows]
    tokens = [((r.get("usage") or {}).get("input_tokens") or 0) + ((r.get("usage") or {}).get("output_tokens") or 0) for r in rows]
    walls = [r.get("wall_seconds") or 0 for r in rows]
    by_trial: dict[int, int] = {}
    for r in rows:
        by_trial[r["trial"]] = by_trial.get(r["trial"], 0) + bool(r.get("hidden_pass"))
    # A run is truthful when it did not claim success it lacks: the harness claims PASS only
    # through its host gate; the baseline claims success whenever the model says done.
    claimed = [r for r in rows if r.get("status") in ("PASS", "DONE")]
    false_claims = [r for r in claimed if not r.get("hidden_pass")]
    return {
        "attempted": len(rows),
        "solved": len(solved),
        "solve_rate": round(len(solved) / len(rows), 3) if rows else None,
        "solved_per_trial": dict(sorted(by_trial.items())),
        "new_regression_failures": sum(len(r.get("new_regressions") or []) for r in rows),
        "claimed_success_but_hidden_failed": len(false_claims),
        "truthful_incomplete_or_blocked": sum(1 for r in rows if r.get("status") not in ("PASS", "DONE") and not r.get("hidden_pass")),
        "model_calls_total": sum(calls),
        "tokens_total": sum(tokens),
        "tokens_per_solved_task": round(sum(tokens) / len(solved)) if solved else None,
        "wall_seconds_mean": round(statistics.mean(walls), 1) if walls else None,
        "wall_seconds_stdev": round(statistics.stdev(walls), 1) if len(walls) > 1 else 0.0,
        "repeated_identical_failed_actions": sum(r.get("repeated_identical_failed_commands", 0) for r in rows),
    }


def summarize(args: argparse.Namespace) -> None:
    evidence = Path(args.evidence)
    rows = _records(evidence, args.profile)
    cases = {c["name"]: c for c in json.loads((evidence / "cases.json").read_text())} if (evidence / "cases.json").exists() else {}
    summary: dict = {
        "schema_version": "1.0",
        "model_profile": args.profile,
        "prescribed_model_evidence": args.profile not in ("claude-bridge",),
        "method": ("Same model profile/adapter/sampling, same tasks and starting commits, same python:3.12 runtime, same "
                   "model-call and wall budgets, same oracle (project test suite with original tests restored)."),
        "results": {},
        "runs": [{k: r.get(k) for k in ("case", "configuration", "trial", "status", "hidden_pass", "new_regressions",
                                        "usage", "wall_seconds", "changed_paths")} for r in rows],
    }
    for split in ("development", "held_out"):
        for config in CONFIGS:
            picked = [r for r in rows if r["configuration"] == config and cases.get(r["case"], {}).get("split") == split]
            if picked:
                summary["results"].setdefault(split, {})[config] = _aggregate(picked)
    out = evidence.parent / "comparison"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"summary-{args.profile}.json").write_text(json.dumps(summary, indent=2) + "\n")
    lines = [f"# Comparative evaluation ({args.profile})", "",
             "Development cases were used while tuning the harness; held-out cases were not.", "",
             "| Split | Configuration | Solved | Claimed success but hidden tests failed | New regressions | Calls | Tokens | Tokens/solved | Mean wall s |",
             "|---|---|---|---|---|---|---|---|---|"]
    for split, configs in summary["results"].items():
        for config, agg in configs.items():
            lines.append(f"| {split} | {config} | {agg['solved']}/{agg['attempted']} | {agg['claimed_success_but_hidden_failed']} | "
                         f"{agg['new_regression_failures']} | {agg['model_calls_total']} | {agg['tokens_total']} | "
                         f"{agg['tokens_per_solved_task']} | {agg['wall_seconds_mean']} |")
    if not summary["prescribed_model_evidence"]:
        lines += ["", "Development bridge profile: these numbers are NOT prescribed-model (DeepSeek/Qwen) evidence."]
    (out / f"summary-{args.profile}.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    runner = sub.add_parser("run")
    runner.add_argument("cases")
    runner.add_argument("--work", required=True)
    runner.add_argument("--trials", type=int, default=2)
    runner.add_argument("--model-calls", type=int, default=20)
    runner.add_argument("--wall-seconds", type=int, default=1200)
    runner.add_argument("--only", default=None)
    for command in (runner, sub.add_parser("summarize")):
        command.add_argument("--profile", required=True)
        command.add_argument("--evidence", default=str(ROOT / "release-evidence" / "live"))
    args = parser.parse_args()
    run(args) if args.command == "run" else summarize(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
