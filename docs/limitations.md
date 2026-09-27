# Known limitations

This release states what it does not do, so that no result or document overclaims.

## Release scope

- **P1 effects are disabled.** Local apply (`harness apply`), remote publish
  (`harness publish`), and push, merge, PR, or issue mutation return
  `CAPABILITY_DISABLED`. The output is a verified private commit plus an export
  bundle. Apply it yourself with `git apply --binary bundle/patch.diff`.
- **Official evaluator adapter.** No official hackathon protocol was published at
  release time. The isolated `official_hackathon_v1` slot returns
  `OFFICIAL_ADAPTER_NOT_CONFIGURED`, and `harness doctor --profile submission` reports
  it as blocking. The native `native_json_v1` contract is complete.
- **Runtime profile.** Only `python312_docker_v1` is supported: Python 3.12 projects
  whose tests run under pytest or unittest. Other ecosystems (Node, Go, Java) are
  indexed and can be edited, but have no verification runtime. They end as
  `UNVERIFIED` or `BLOCKED_ENVIRONMENT`, never `PASS`.
- **Docker is required.** There is no host-execution fallback. Without a reachable
  Docker engine, runs return `BLOCKED_ENVIRONMENT` (exit 3).
- **Dependencies** are installed only from PyPI, and only from test or dev groups and
  requirements files the project declares. Projects that need system packages,
  services (databases, browsers), or private indexes report `BLOCKED_ENVIRONMENT`.
  The harness does not guess.

## Model evidence

- **Live prescribed-model runs were not executed by the authors.** DeepSeek and Qwen
  are supported through the OpenAI-compatible adapter, with bundled profiles, JSON-mode
  handling, `enable_thinking=false` for Qwen, and `Retry-After` backoff. All live
  end-to-end testing was done with Claude through the local development bridge,
  which emulates DeepSeek's `json_object`-only behavior and injects rate limits. The
  `model` and `comparative_evaluation` release gates stay `NOT_RUN` until a
  prescribed-model run is recorded. See [release-evidence.md](release-evidence.md).
- **Smaller models** may need more schema-repair rounds. The coder prompt forbids
  Python triple-quoted strings inside JSON, and a tested normalizer repairs that one
  common mistake. Other malformed output is reported, not guessed at.
- Live model output is stochastic. Replaying a run (`audit`, `reverify`) reproduces
  its recorded state and checks, not new model output.

## Verification

- `PASS` means the *declared, observed* checks passed on a fresh copy with no new
  regressions, and the validator and diff review raised no blocking finding. It does
  not guarantee that the evaluator's hidden tests pass. Every result says so in
  `limitations`.
- Tests that already fail at the baseline are reported as baseline failures, not as
  regressions. The harness does not try to fix unrelated failures.
- Tests that modify or weaken the oracle are rerun in their original form, and
  always-true assertion swaps block `PASS`. This is heuristic hardening, not a proof.

## Plugins

- Plugins are reviewed built-ins that run in process. The hash lock controls **which
  code loads**, but plugin code is not sandboxed. Adding a third-party plugin means
  reviewing it and regenerating the lock (`make plugins-lock`).

## Scale

- A repository queue is capped at 20 tasks and 80 dependency edges per run.
- Retrieval indexes text files up to 512 KiB each. Larger files, and files with
  secret-like paths, are recorded in the inventory as `EXCLUDED` with a reason, and
  are never shown to the model.
- Single-task wall time defaults to about 35 minutes. Budgets are global per run.
  An evaluator request may set them explicitly, within the schema bounds.

## Platform

- Supported: macOS (Docker Desktop) and Linux (Docker Engine, non-root user in the
  `docker` group). Windows is untested and should run under WSL 2.
- The sandbox image is built per machine. `config/runtime.lock.json` records the
  local image ID and architecture (arm64 or amd64).
