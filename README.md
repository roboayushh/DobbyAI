# DobbyAI — AI Coding Harness

Terminal-first AI coding harness. One command takes a repository and an issue (or a bounded set of open issues) through the whole pipeline:

1. **Prepare**: an exact private baseline from a Git repo, a GitHub URL, a plain folder, or a ZIP, plus immutable tasks .
2. **Plan**: plan with one prescribed model over versioned repository evidence .
3. **Act**: act through a hardened, network-less Docker sandbox with seven built-in tools .
4. **Verify**: verify every candidate on a fresh copy against a frozen contract and a baseline, with an independent validator and bounded repair.
5. **Integrate**: integrate verified work as one exact commit per issue on a private Git ref, then run a final aggregate check .
6. **Release**: return exactly one JSON result, export a patch bundle that has been round-tripped against the baseline, a truthful report, and a reproducibility manifest.

The original repository is never mounted or modified, nothing is pushed, and the only status that means success is a host-computed `PASS`.

---

## Requirements

| Item | Version |
|------|---------|
| Python | 3.12 or newer (`make setup` picks the first `python3.12`, `python3.13`, `python3.14`, or `python3` that qualifies) |
| OS | macOS or Linux (Windows through WSL 2) |
| git | 2.30+ |
| Docker | Engine reachable by the current user (as root, the sandbox runs as an unprivileged UID). Model-generated code runs only inside it, never on the host. |
| Network | HTTPS to the model endpoint; PyPI for isolated dependency setup; GitHub only for `public_https` or issue sources. |

## Quick start

This is the standard evaluator flow; nothing has to be edited:

```bash
git clone <this repository> dobbyai && cd dobbyai
export AI_API_KEY="<provided key>"          # the ONLY credential, read from the environment
make setup                                  # hash-locked user-space install + pinned sandbox image (no sudo)
make run                                    # interactive; see the prompts below
```

`make run` asks three questions:

1. **Model**, asked only when no model is configured: `1` DeepSeek (`deepseek-chat`),
   `2` Qwen (`qwen-plus`), Qwen on Groq, and the other bundled profiles. A Groq key
   (`gsk_…`) makes Groq the default answer. The choice is used for every
   role of the run. Skip the question with `make run MODEL=deepseek` (or `qwen`), or
   `export HARNESS_MODEL_PROFILE=qwen`.
2. **Repository**: a GitHub URL (`https://github.com/owner/repo`), a local folder, a Git
   checkout, or a ZIP file.
3. **Task**: a GitHub issue URL (`https://github.com/owner/repo/issues/123`), `#123`, or
   plain text describing the problem or failing test.

Pasting an issue URL at the repository prompt answers both questions 2 and 3.

While it works, `make run` shows a **live feed**: which agent is working (🧭 Planner,
🛠 Coder, 🔍 Validator) and what it decided, each sandbox action and whether it was
accepted, every test check with pass/fail counts, the completion gate, token use per call,
and any rate-limit waits.

After a verified run, `make run` asks whether to **apply the fix to the original
repository** (default No). For a local folder or checkout it runs `git apply --check`, then
applies the patch to the working tree only: nothing is committed or pushed. For a GitHub
source it saves `<repo>-<run>.patch` in the current directory instead; the harness never
pushes or opens pull requests. Headless and `--non-interactive` runs never apply anything.

Other entry points:

```bash
make doctor ARGS=--live                     # prerequisites + one tiny probe request to the model
make run MODEL=qwen ARGS='--repo https://github.com/owner/repo --task https://github.com/owner/repo/issues/7'
make run INPUT=/abs/path/request.json       # headless evaluator mode: one JSON object on stdout
make test                                   # the harness's own test suite
```

The result names the baseline and the verified candidate commit. The export bundle
(`patch.diff`, `report.md`, `result.json`, `manifest.json`, checksums, evidence) goes
to the requested `export_path`. Apply it to your own checkout with `git apply --binary patch.diff`.

## Make targets

| Target | What it does |
|---|---|
| `make setup` | Finds Python ≥ 3.12 and git, creates `.venv`, runs `pip install --require-hashes -r requirements.lock`, and builds the pinned sandbox image when Docker is running. |
| `make run` | Interactive: model (only if none is set), repository, task. `MODEL=qwen` picks the model, `ARGS='…'` passes flags, `INPUT=request.json` runs headless. |
| `make test` | The harness's own deterministic suite. It writes `release-evidence/tests/junit.xml`. Docker end-to-end tests skip without Docker. |
| `make test-fast` | Unit and contract tests only. |
| `make doctor` | Prerequisite and configuration diagnosis. `ARGS=--live` adds a model probe; `ARGS='--profile submission'` adds the official-adapter check. |
| `make clean` | Removes the harness's build and test caches only. Runs, repositories and exports are never touched; use `harness clean RUN_ID` for a run (approval required). |
| `make release-evidence` | Evaluates every release gate. A gate that was not executed is `NOT_RUN`, never `PASS`. |
| `make runtime-image`, `sandbox-doctor`, `sandbox-probe` | Build, inspect, or probe the sandbox (14 isolation checks). |
| `make schemas`, `plugins-lock`, `sbom` | Regenerate public JSON Schemas, the plugin lock, and the SBOM and notices. |
| `make bridge` | **Local development only**: an OpenAI-compatible bridge to the `claude` CLI (see [docs/profiles.md](docs/profiles.md)). |

## Inputs

| Source | Interactive | Headless `repository.kind` |
|---|---|---|
| Local Git checkout (clean or dirty; the dirty state is snapshotted byte-exactly) | `./path/to/repo` | `local_git` (+ optional `revision`) |
| Public GitHub repository | `owner/repo`, `https://github.com/owner/repo` | `public_https` |
| Plain folder (no Git) | `./folder` | `local_folder` |
| ZIP archive (a single root folder is stripped; bounded and validated) | `./project.zip` | `local_zip` |

Tasks can be free text, a GitHub issue URL or number, or (in development mode) up to 20
open issues processed by a dependency-aware queue:
`harness run owner/repo --task-mode repository --max-tasks 5`.

## Model

All roles (planner, coder, validator) use **one** prescribed model through the
OpenAI-compatible adapter. `AI_API_KEY` is the only key, and there is no silent fallback
model. Bundled profiles in `config/model_profiles.toml`:

| Profile | Provider / model | Notes |
|---|---|---|
| `deepseek` | api.deepseek.com, `deepseek-chat` | JSON mode (`json_object`) |
| `deepseek-reasoner` | api.deepseek.com, `deepseek-reasoner` | Reasoning is discarded; only the final JSON is used |
| `qwen`, `qwen-coder`, `qwen-cn` | DashScope compatible mode, `qwen-plus` / `qwen3-coder-plus` | Sends `enable_thinking=false` |
| `groq-qwen` | api.groq.com, `qwen/qwen3.8-27b` (Groq key `gsk_…`) | Thinking off (`reasoning_effort=none`). Free tier works but is slow (requests are shrunk and paced to its tokens-per-minute cap) |
| `qwen-local` | vLLM or Ollama on `127.0.0.1:8000` | Loopback HTTP is allowed; remote HTTP is not |
| `openrouter-deepseek` | openrouter.ai | |
| `claude-bridge` | local `claude` CLI bridge | **Development only.** It never counts as prescribed-model evidence. |

Override any field without editing files: `HARNESS_MODEL_ENDPOINT`,
`HARNESS_MODEL_NAME`, `HARNESS_MODEL_RESPONSE_FORMAT`, and others (see
[docs/configuration.md](docs/configuration.md)). Overrides are recorded in the run's
fingerprint.

## Rate limits and token use

- **Small accounts do not stop the run.** When a provider reports a tokens-per-minute cap
  below one request (Groq free tier: HTTP 413), the harness learns the cap, rebuilds a
  smaller packet, and paces later calls to the one-minute window. HTTP 429s are waited
  out (up to 8 attempts, honoring `retry-after`). `HARNESS_MODEL_TPM_LIMIT=<n>` pre-sizes
  requests from the first call.
- **Token optimisation.** Duplicate evidence spans (the same lines returned by several
  queries) are sent once; the output JSON schema is sent without generated `title`
  annotations; the untrusted-data notice is a short per-item marker (the full rule is in
  the system policy). Replayed over the 35 model calls of a real GitHub-issue run, these
  cut input tokens by 28.6% (771,619 → 550,667 estimated).

## Headless evaluator mode

```json
{
  "schema_version": "1.0",
  "request_id": "ereq_case_0001",
  "adapter": {"name": "native_json_v1", "version": "1.0.0"},
  "repository": {"kind": "local_git", "locator": "/evaluator/input/repository"},
  "task_mode": "single_issue",
  "execution_mode": "evaluation",
  "task": {"source_type": "direct_text", "text": "Describe the bug or feature here."},
  "model_config_ref": "deepseek",
  "budgets": {"model_calls": 30, "wall_seconds": 1800},
  "result_path": "/evaluator/output/result.json",
  "export_path": "/evaluator/output/bundle",
  "requested_effects": ["EXPORT"],
  "idempotency_key": "eval-case-0001"
}
```

```bash
harness run --input request.json --non-interactive     # == make run INPUT=request.json
```

- The request is validated completely **before any effect**. An invalid request makes
  no model call and does not touch the repository.
- Resubmitting the same request is idempotent.
- stdout carries exactly one `EvaluatorResultV1` JSON object, and the same object is
  written atomically to `result_path`.

Full contract: [docs/evaluator.md](docs/evaluator.md).

## Exit codes

The JSON `status` is authoritative. The exit code is a coarse automation aid, returned by
`harness run`. `make` collapses any failure to exit 2, so use `.venv/bin/harness run --input …`
when you need the canonical code.

| Code | Status |
|------|---------|
| 0 | `PASS`, `COMPLETED_ALL`; a requested boundary was reached or a read-only command succeeded |
| 2 | `INVALID` input, schema, configuration, model profile, missing key, or output path; `CAPABILITY_DISABLED` |
| 3 | `PARTIAL_SUCCESS`, `UNVERIFIED`, `BLOCKED_ENVIRONMENT` (e.g. no Docker: there is never a host fallback) |
| 4 | `FAILED`, `VERIFICATION_FAILED` |
| 5 | `BUDGET_EXHAUSTED` |
| 6 | `NEEDS_INPUT`, `PENDING_APPROVAL` |
| 7 | `INTERNAL_ERROR`, `INTEGRATION_UNCERTAIN`, unresolved `ACTION_UNKNOWN` |
| 130 | Cancelled (Ctrl-C) |

## Commands

```bash
# Release (PRD 6)
harness run --input request.json --non-interactive
harness export RUN_ID --output DIR [--json]          # read-only; rebuilds and round-trips the bundle
harness replay RUN_ID --mode audit|reverify          # audit: no effects; reverify: rerun checks, no model
harness clean RUN_ID [--dry-run]                     # registered resources only; asks for an approval
harness doctor [--live] [--profile submission] [--json]
harness plugins list | doctor
harness release evidence
harness version --json
harness apply | publish                              # P1: disabled (CAPABILITY_DISABLED)

# Pipeline stages (PRD 1-5)
harness prepare --request request.json --json
harness continue RUN_ID --until plan|action-proposed|verification-required|complete --json
harness approval list RUN_ID | approval show REQUEST_ID | approve REQUEST_ID | deny REQUEST_ID
harness verification contract|baseline|checks|logs|compare ...
harness report RUN_ID [--task TASK_ID] --json
harness queue plan|start|status|show|pause|resume|cancel|skip RUN_ID
harness git graph RUN_ID --max-count 50
harness resume RUN_ID | recover RUN_ID --dry-run
harness sandbox doctor | probe | build
harness model doctor [--profile ID] [--live]
```

JSON goes to **stdout**; progress and errors go to **stderr**.

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `AI_API_KEY` | live runs | The sole model credential. It is read only immediately before a request and never persisted or given to a sandbox. |
| `HARNESS_MODEL_PROFILE` | no | Profile id, e.g. `deepseek` or `qwen` (or `make run MODEL=…`). When unset, interactive `make run` asks once at launch; headless runs use the request's `model_config_ref`, and `--non-interactive` flag runs fail with instructions. |
| `HARNESS_MODEL_ENDPOINT`, `HARNESS_MODEL_NAME`, … | no | Per-field profile overrides. |
| `GITHUB_TOKEN` | no | Read-only issue access for private repositories and higher rate limits. |
| `HARNESS_PERMISSION_PROFILE` | no | `sandbox` (default), `guided`, or `delegated`. |
| `HARNESS_MAX_MODEL_CALLS`, `HARNESS_MAX_RUN_WALL_SECONDS` | no | Run budgets (default 44 calls and 2100 s for one task). |
| `HARNESS_DEPENDENCY_SETUP` | no | `true` (default): PyPI-only isolated dependency install. `false`: offline only. |
| `DATA_DIR` | no | Run storage (default `data/`). |

Only the harness's own `.env` is read (template: [.env.example](.env.example), which holds no key). A target repository's `.env` is never loaded.

## Running tests

```bash
make test                      # full suite; Docker end-to-end tests need Docker running
make test-fast                 # no Docker
make test ARGS="-k export -v"
```

- Tests use scripted model replies, so no `AI_API_KEY` is needed. Every sandbox
  action, check, commit, ref move, export and round-trip in the end-to-end tests is real.
- Live end-to-end evaluation against real open-source repositories with injected bugs
  and hidden tests, plus the single-role baseline comparison, lives in
  `scripts/live_eval.py`, `scripts/baseline_loop.py`, and `scripts/compare_eval.py`
  (see [docs/release-evidence.md](docs/release-evidence.md)).
- Clean-machine qualification in a fresh Linux container: `scripts/clean_machine.sh`.

## Project structure

```
Makefile  pyproject.toml  requirements.lock        # hash-locked install
config/
  model_profiles.toml                              # trusted, non-secret model profiles
  profiles/  retention/  plugins/                  # release profiles, retention, reviewed plugin lock
runtime/python/                                    # pinned sandbox image (base by digest) + tool lock
schemas/v1/                                        # public JSON Schemas (make schemas)
licenses/                                          # SBOM (CycloneDX) + third-party notices
docs/                                              # configuration, profiles, evaluator, security, limitations, provenance
scripts/                                           # schema/lock/SBOM generators, live eval, baseline, bridge, clean machine
src/harness/
  cli.py  cli_execution.py  cli_release.py         # Typer CLI
  config.py                                        # HarnessConfig (pydantic-settings)
  contracts/                                       # versioned PRD 1-6 contracts
  repository/  workspace/                          # sources (git/https/folder/zip), byte-exact manifests
  model/                                           # profiles, OpenAI-compatible adapter, probe
  retrieval/  context/  roles/  orchestration/     # evidence, role packets, planner/coder/validator loop
  sandbox/  worker/  execution/  policy/           # Docker backend, in-container tools, action settlement
  verification/                                    # contracts, baselines, parsers, validator, completion gate
  queue/  gitflow/                                 # DAG queue, hardened private git, journaled refs
  evaluator/  export/  release/  plugins/          # PRD 6: gateway, patch export, results/evidence, plugin kernel
  approvals/  retention/  doctor/                  # capability grants, registered cleanup, doctor
  persistence/                                     # SQLite migrations 1-7, artifact store, events
tests/                                             # 491 deterministic tests incl. Docker end-to-end
```

## Security and guardrails

In summary (details in [docs/security.md](docs/security.md)):

- One model, one key. The key never reaches a prompt, a log, an export, or a container.
- Generated code runs only in a `--read-only`, `--cap-drop=ALL`, non-root,
  `--network=none` container with resource limits. No Docker socket, home directory,
  credentials, or original repository is mounted.
- The original source is never modified. Imports are byte-exact, and the repository's
  git filters and hooks never run.
- Export, cleanup, and every future effect go through the kernel. Cleanup needs a
  one-use grant bound to the exact plan. Push, merge, and PR are disabled, and
  `publication_authorized` is always `false`.
- Only reviewed, hash-locked plugins load. A replacement controller gets exactly the
  same policy, budget, and approval denials.

## Documentation

| Document | Contents |
|---|---|
| [docs/configuration.md](docs/configuration.md) | Every setting, budget, and file |
| [docs/profiles.md](docs/profiles.md) | Model profiles (DeepSeek, Qwen, local), release profiles, the dev bridge |
| [docs/evaluator.md](docs/evaluator.md) | Headless request/result contract, export bundle, replay |
| [docs/security.md](docs/security.md) | Trust boundaries, sandbox, approvals, plugins, cleanup |
| [docs/limitations.md](docs/limitations.md) | What this release does not do |
| [docs/provenance.md](docs/provenance.md) | Locks, SBOM, licenses, per-run provenance |
| [docs/release-evidence.md](docs/release-evidence.md) | Release gates, live evaluation, baseline comparison |
| `docs/prd{1..6}-gap-report.md` | Requirement-by-requirement status and evidence |

## Limitations

In summary (details in [docs/limitations.md](docs/limitations.md)):

- Python 3.12 verification runtime only.
- Docker is required.
- P1 local apply and remote publish are disabled.
- The official hackathon adapter is a pinned placeholder until its protocol exists.
- `PASS` means the declared checks passed on a fresh copy; it does not guarantee
  hidden tests.
- Live testing so far used Claude through the local bridge. Prescribed-model
  (DeepSeek/Qwen) evidence is produced by running `scripts/compare_eval.py` with your key.
