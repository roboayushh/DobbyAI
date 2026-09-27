# Configuration reference

All configuration is host-side and trusted. Nothing in a target repository (its `.env`,
config files, `.gitattributes`, plugin files, or issue text) can change these values.

## Where settings come from

1. Process environment variables.
2. `<harness checkout>/.env` — only the harness's own `.env` is read, never the current
   directory's or a target repository's. A target repository's `.env` can therefore not
   redirect the model endpoint or capture the key.
3. Built-in defaults.

Evaluator requests (`harness run --input request.json`) may configure a run (budgets,
`model_config_ref`, output paths), but they cannot enable disabled effects, widen permissions,
add plugins, or introduce a second model.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `AI_API_KEY` | unset | The **only** model credential. It is read immediately before each host-side model request. It is never persisted, logged, exported, or passed to a sandbox. |
| `HARNESS_MODEL_PROFILE` | unset (`designated` placeholder) | Profile id in `config/model_profiles.toml`. All roles (planner, coder, validator) use it. `make run MODEL=<id>` sets it for one invocation. When it names no real model, interactive `make run` asks once at launch (DeepSeek, Qwen, …) and uses the answer for the whole run; `--non-interactive` flag runs fail with instructions instead. |
| `HARNESS_MODEL_ENDPOINT` | profile value | Override the profile's base URL. It must be HTTPS, or HTTP to loopback only. |
| `HARNESS_MODEL_NAME` | profile value | Override the model id. |
| `HARNESS_MODEL_CONTEXT_WINDOW` | profile value | Context window in tokens. |
| `HARNESS_MODEL_MAX_OUTPUT_TOKENS` | profile value | Per-call output cap. |
| `HARNESS_MODEL_RESPONSE_FORMAT` | profile value | `json_schema`, `json_object` or `none`. |
| `HARNESS_MODEL_TIMEOUT_SECONDS` | profile value | Per-request timeout. |
| `HARNESS_MODEL_TEMPERATURE` | profile value | Sampling temperature. |
| `MODEL_PROFILES_PATH` | `config/model_profiles.toml` | Trusted profile file, relative to the harness checkout. |
| `DATA_DIR` | `data/` | Run database, artifacts, and private repositories. |
| `HARNESS_PERMISSION_PROFILE` | `sandbox` | `sandbox` runs private-workspace actions automatically. `guided` requires a one-use approval per writable action. `delegated` is also available. |
| `HARNESS_MAX_MODEL_CALLS` | `40 × tasks + 4` | Run-wide model-call budget. |
| `HARNESS_MAX_RUN_WALL_SECONDS` | `1800 × tasks + 300` | Run-wide wall-clock budget. |
| `HARNESS_DEPENDENCY_SETUP` | `true` | Installs declared test dependencies in an isolated setup container that can reach only PyPI through an allowlist proxy. `false` means offline only. |
| `HARNESS_AUTO_BUILD_RUNTIME` | `true` | Builds the pinned runtime image once when it is missing or stale. |
| `GITHUB_TOKEN` | unset | Optional. Read-only issue access for private repositories or higher rate limits. It is never given to the model or the sandbox. |

Every environment override is recorded in the profile fingerprint and in the
reproducibility manifest, so a run always states the exact effective configuration.

## Budgets

A single task gets one global budget sized by `scaled_budget()`:

| Budget | Single task | Per extra task |
|---|---|---|
| Model calls | 44 | +40 (cap 400) |
| Input tokens | calls × 48 000 | — |
| Output tokens | calls × 6 000 | — |
| Wall seconds | 2 100 | +1 800 (cap 6 h) |
| Reserved for final verification/report | 2 calls, 4 000 output tokens, 180 s | — |

An evaluator request can set these explicitly through `budgets` (see [evaluator.md](evaluator.md)).
Exhausting a budget ends the run with `BUDGET_EXHAUSTED` (exit 5). The best verified
partial work is still exported.

## Files

| Path | Trust | Contents |
|---|---|---|
| `config/model_profiles.toml` | trusted, non-secret | Model profiles ([profiles.md](profiles.md)) |
| `config/profiles/*.json` | trusted | Release profiles `evaluation_strict_v1` and `development_sandbox_v1` |
| `config/retention/default_v1.json` | trusted | What cleanup may remove ([security.md](security.md#cleanup-and-retention)) |
| `config/plugins/builtin_release_v1.lock.json` | trusted, hash-locked | The reviewed plugin set |
| `config/runtime.lock.json` | per machine | Content-addressed sandbox image ID and labels |
| `runtime/python/Dockerfile`, `runtime/python/requirements.lock` | trusted | Pinned sandbox image (base image by digest) |
| `requirements.lock` | trusted | Hash-locked harness dependencies for `make setup` |
| `schemas/v1/**` | generated | Public JSON Schemas (`make schemas`) |

## Doctor

`make doctor` (or `harness doctor --json`) checks Python, git, SQLite, storage, the
database, Docker, the pinned runtime image and its architecture, dependency network,
the model profile, `AI_API_KEY`, the evaluator adapter, the plugin set, and the
dependency lock. `harness doctor --live` also sends one probe request to the model
(it asks for `{"ok":true,"sum":42}`) and reports latency, the JSON mode used, and any
wrapper that was stripped. `--profile submission` also blocks when the official
evaluator adapter is not configured.
