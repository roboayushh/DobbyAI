#!/usr/bin/env python3
"""Single-role shell-loop baseline for comparative evaluation (PRD 6 section 15.1).

The deliberately simple configuration the harness is compared against:

- one role, one conversation, no planner, retrieval, validator, repair, or queue;
- the SAME prescribed model profile and adapter (``OpenAICompatibleModelAdapter``,
  same sampling settings, same ``AI_API_KEY``);
- the same task text, the same starting commit, the same runtime image, and the
  same call/token/wall budgets;
- the same success oracle: ``scripts/live_eval.py`` applies the diff to a clean
  copy and runs the project's own suite with the original test files restored.

Each turn the model returns one JSON object: ``{"command": "<shell>"}`` to run in
the container, ``{"write": {"path": ..., "content": ...}}`` to replace a file, or
``{"done": true}``. The container has network only for the one-time dependency
install, then it is disconnected before the first model call.

Local development only; results are recorded next to the harness results with
``configuration: "baseline_single_role"``.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from harness.contracts import Role  # noqa: E402
from harness.model.adapter import ModelAdapterError, ModelCallRequest, OpenAICompatibleModelAdapter  # noqa: E402
from harness.model.profile import ModelProfileResolver  # noqa: E402

import live_eval  # noqa: E402

IMAGE = "python:3.12-slim"
OUTPUT_LIMIT = 6_000
SYSTEM = """You are a software engineer fixing one issue in the repository mounted at /w.
Reply with exactly ONE json object per turn and nothing else:
  {"thought": "<short>", "command": "<one shell command, run with sh -c in /w>"}
  {"thought": "<short>", "write": {"path": "<repo-relative path>", "content": "<entire new file content>"}}
  {"thought": "<short>", "done": true}
Commands run without network access and time out after 120 seconds; output is truncated.
Python 3.12 and the project's dependencies are installed. Make the smallest correct change,
run the relevant tests, and reply done when the issue is fixed. Do not modify existing tests."""


def sh(container: str, command: str, timeout: int = 120) -> tuple[int, str]:
    try:
        proc = subprocess.run(["docker", "exec", "-w", "/w", container, "timeout", str(timeout), "sh", "-c", command],
                              capture_output=True, text=True, timeout=timeout + 15)
        out = (proc.stdout + proc.stderr)
        return proc.returncode, out[-OUTPUT_LIMIT:] if len(out) > OUTPUT_LIMIT else out
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT"


def parse(text: str) -> tuple[dict, str]:
    """Return the FIRST JSON object in the reply (a stop-sequence equivalent) and its text.

    Anything after it, such as the model continuing the conversation with
    imagined tool output, is discarded and never shown back to the model.
    """
    start = text.find("{")
    if start < 0:
        return {}, text
    try:
        value, end = json.JSONDecoder().raw_decode(text, start)
    except ValueError:
        return {}, text
    return (value, text[start:end]) if isinstance(value, dict) else ({}, text)


def run_case(case: dict, work: Path, profile: str, budget_calls: int, wall_limit: int, trial: int) -> dict:
    name = case["name"]
    case_dir = work / f"{name}-baseline-t{trial}"
    if case_dir.exists():
        subprocess.run(["chmod", "-R", "u+w", str(case_dir)])
        shutil.rmtree(case_dir)
    case_dir.mkdir(parents=True)
    repo = case_dir / "repo"
    shutil.copytree(case["repo"], repo, symlinks=True)
    resolved = ModelProfileResolver(ROOT / "config" / "model_profiles.toml").resolve(profile)
    adapter = OpenAICompatibleModelAdapter(resolved)
    container = f"baseline-{uuid.uuid4().hex[:10]}"
    record: dict = {"case": name, "configuration": "baseline_single_role", "model_profile": profile, "trial": trial,
                    "budget": {"model_calls": budget_calls, "wall_seconds": wall_limit}}
    subprocess.run(["docker", "run", "-d", "--name", container, "-e", "SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0", "-v", f"{repo}:/w", IMAGE, "sleep", "infinity"],
                   check=True, capture_output=True)
    try:
        code, out = sh(container, case["hidden_install"], timeout=600)
        if code != 0:
            record.update(status="SETUP_FAILED", setup_output=out[-2000:])
            return record
        subprocess.run(["docker", "network", "disconnect", "bridge", container], capture_output=True)
        files = subprocess.run(["git", "ls-files"], cwd=repo, capture_output=True, text=True).stdout.splitlines()
        messages = [{"role": "system", "content": SYSTEM},
                    {"role": "user", "content": f"Issue:\n{case['issue']}\n\nRepository files:\n" + "\n".join(files[:400])
                     + "\n\nReply with one json object."}]
        calls = input_tokens = output_tokens = commands = repeated_failures = 0
        failed_seen: set[str] = set()
        status = "BUDGET_EXHAUSTED"
        started = time.monotonic()
        while calls < budget_calls:
            if time.monotonic() - started > wall_limit:
                status = "WALL_BUDGET_EXHAUSTED"
                break
            try:
                response = adapter.generate(ModelCallRequest(call_id=f"call_{uuid.uuid4().hex}", role=Role.CODER, messages=messages,
                                                             response_schema={"type": "object"}, max_output_tokens=6000))
            except ModelAdapterError as exc:
                status = f"MODEL_ERROR:{exc.code}"
                break
            calls += 1
            input_tokens += response.input_tokens
            output_tokens += response.output_tokens
            reply, kept = parse(response.raw_text)
            messages.append({"role": "assistant", "content": kept})
            if reply.get("done"):
                status = "DONE"
                break
            if isinstance(reply.get("write"), dict) and reply["write"].get("path"):
                target = (repo / str(reply["write"]["path"])).resolve()
                if repo.resolve() not in target.parents:
                    observation = "error: path outside the repository"
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(str(reply["write"].get("content", "")))
                    observation = f"wrote {reply['write']['path']}"
            elif isinstance(reply.get("command"), str):
                commands += 1
                code, out = sh(container, reply["command"])
                if code != 0:
                    repeated_failures += reply["command"] in failed_seen
                    failed_seen.add(reply["command"])
                observation = f"exit code {code}\n{out}"
            else:
                observation = "error: reply was not one valid json object with command, write, or done"
            messages.append({"role": "user", "content": observation + "\n\nReply with one json object."})
        record.update(status=status, usage={"model_calls": calls, "input_tokens": input_tokens, "output_tokens": output_tokens},
                      wall_seconds=round(time.monotonic() - started, 1), commands=commands,
                      repeated_identical_failed_commands=repeated_failures)
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)
        if "messages" in locals():
            (case_dir / "transcript.json").write_text(json.dumps(messages, indent=1))
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)
    diff = subprocess.run(["git", "diff", "--cached", "--binary", "HEAD", "--", ".", ":(exclude)*.egg-info", ":(exclude)**/__pycache__/**"],
                          cwd=repo, capture_output=True, text=True).stdout
    (case_dir / "patch.diff").write_text(diff)
    record["changed_paths"] = subprocess.run(["git", "diff", "--cached", "--name-only", "HEAD"], cwd=repo,
                                             capture_output=True, text=True).stdout.split()
    record.update(live_eval.score_patch(case, case_dir, case_dir / "patch.diff"))
    return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("cases")
    parser.add_argument("--work", required=True)
    parser.add_argument("--profile", default=os.environ.get("HARNESS_MODEL_PROFILE", "designated"))
    parser.add_argument("--only", default=None)
    parser.add_argument("--model-calls", type=int, default=20)
    parser.add_argument("--wall-seconds", type=int, default=1200)
    parser.add_argument("--trial", type=int, default=1)
    parser.add_argument("--evidence", default=str(ROOT / "release-evidence" / "live"))
    args = parser.parse_args()
    evidence = Path(args.evidence)
    evidence.mkdir(parents=True, exist_ok=True)
    records = []
    for case in json.loads(Path(args.cases).read_text()):
        if args.only and case["name"] != args.only:
            continue
        print(f"== baseline {case['name']} trial {args.trial} ...", file=sys.stderr, flush=True)
        record = run_case(case, Path(args.work), args.profile, args.model_calls, args.wall_seconds, args.trial)
        records.append(record)
        (evidence / f"{case['name']}-baseline-{args.profile}-t{args.trial}.json").write_text(json.dumps(record, indent=2) + "\n")
        print(json.dumps(record, indent=2), file=sys.stderr, flush=True)
    print(json.dumps(records))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
