# Profiles

The harness has two kinds of profile. A **model profile** says which single model every
role uses. A **release profile** says what a run may do.

## Model profiles (`config/model_profiles.toml`)

Every run freezes one profile. Its fingerprint (endpoint, model, sampling, response
format, extra body, overrides) is stored with the run. Planner, coder, and validator
all use that one fingerprint, and there is **no fallback to another model**. If the
provider fails, the run reports a typed error (`MODEL_RATE_LIMITED`,
`MODEL_CONTEXT_OVERFLOW`, `MODEL_RESPONSE_FORMAT_UNSUPPORTED`, and so on) instead of
switching model.

| Profile | Endpoint | Model | JSON mode | Notes |
|---|---|---|---|---|
| `deepseek` | `https://api.deepseek.com/v1` | `deepseek-chat` | `json_object` | DeepSeek JSON mode requires the word "json" in the prompt. The harness always includes it. |
| `deepseek-reasoner` | same | `deepseek-reasoner` | `json_object` | `reasoning_content` is discarded. Only the final JSON is used. |
| `qwen` | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` | `qwen-plus` | `json_object` | Sends `enable_thinking=false`, which Qwen3 requires for non-streaming calls. |
| `qwen-coder` | same | `qwen3-coder-plus` | `json_object` | |
| `qwen-cn` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` | `json_object` | Mainland China endpoint. |
| `groq-qwen` | `https://api.groq.com/openai/v1` | `qwen/qwen3.8-27b` (preview) | `json_object` | Groq key (`gsk_…`). Sends `reasoning_effort=none`. Needs a paid tier; see below. |
| `qwen-local` | `http://127.0.0.1:8000/v1` | `Qwen/Qwen2.5-Coder-32B-Instruct` | `json_object` | vLLM, Ollama, or LM Studio on loopback. |
| `openrouter-deepseek` | `https://openrouter.ai/api/v1` | `deepseek/deepseek-chat` | `json_object` | |
| `claude-bridge` | `http://127.0.0.1:8765/v1` | `claude-sonnet-4-6` | `json_object` | **Local development only.** See below. It never counts as prescribed-model evidence. |
| `designated` | placeholder | placeholder | `json_schema` | The unset default. Interactive `make run` asks which profile to use; headless runs fail with `MODEL_PROFILE_INCOMPLETE` unless `model_config_ref` or `HARNESS_MODEL_PROFILE` names a real one. |

### Using DeepSeek or Qwen

```bash
export AI_API_KEY="<provided key>"    # the provider key; the only credential the harness reads
make run                              # asks: 1 DeepSeek, 2 Qwen, ... (only when no profile is set)
make run MODEL=qwen                   # or pick the profile up front (same as HARNESS_MODEL_PROFILE=qwen)
make doctor MODEL=deepseek ARGS=--live  # one probe request: auth, JSON mode, latency
make run INPUT=/abs/path/request.json # headless evaluator run (model from model_config_ref)
```

Any other OpenAI-compatible provider works without code changes. Point the
`HARNESS_MODEL_ENDPOINT` and `HARNESS_MODEL_NAME` overrides at it, or add a
`[profiles.<id>]` table. The adapter:

- sends `response_format={"type":"json_object"}`, or `json_schema` when the profile
  says so. If a provider rejects `json_schema`, that is reported as
  `MODEL_RESPONSE_FORMAT_UNSUPPORTED` so you can switch the profile to `json_object`.
  The adapter never downgrades silently.
- retries 429 and 5xx responses up to 4 attempts. It honors `Retry-After`, capped at 30 s.
- treats `content: null`, `finish_reason: "length"`, and reasoning-only replies as typed,
  bounded schema failures. The role gets up to two repair rounds with the exact error, then `ROLE_SCHEMA_RETRY_EXHAUSTED`.
- accepts only the PRD 2 transport wrappers: `<think>…</think>` blocks and a single
  Markdown JSON fence. Prose around the JSON is a schema failure, not something to
  guess around.

### Qwen on Groq

GroqCloud serves Qwen through an OpenAI-compatible API, so a Groq key works as
`AI_API_KEY` with no other change:

```bash
export AI_API_KEY="gsk_..."           # a Groq key; make run then defaults to groq-qwen
make doctor MODEL=groq-qwen ARGS=--live
make run MODEL=groq-qwen
```

- **Account tier.** Groq's free tier allows 8K tokens per minute and 200K per day for this
  model. One harness request is typically 15–25K tokens, so the free tier cannot run a task.
  Groq then answers HTTP 413, which the harness reports as `MODEL_QUOTA_TPM_TOO_LOW` instead
  of retrying. Use the Developer (paid) tier.
- **Model id.** `qwen/qwen3.8-27b` is a Groq preview model (`qwen/qwen3-32b` was deprecated
  in July 2026). If Groq renames it, override it without editing files:
  `HARNESS_MODEL_NAME=<id from GET /models> make run MODEL=groq-qwen`. Groq no longer
  hosts a DeepSeek model.
- **JSON mode.** Groq rejects invalid JSON-mode output with HTTP 400 `json_validate_failed`
  and returns the text as `failed_generation`. The harness passes that text to the normal
  schema validation, so the role gets its bounded repair round (usage is labelled
  `estimated`).
- **Prescribed model.** Use Groq only if the organisers allow Qwen served by Groq. If they
  prescribe a specific model (for example `qwen-plus` on DashScope), use that profile.

### The local Claude bridge (development only)

`scripts/claude_openai_bridge.py` serves an OpenAI-compatible endpoint on
`127.0.0.1:8765`. Behind it is the local `claude` CLI, run with no tools and no
session persistence. It was used to develop and test the harness end to end when
no DeepSeek or Qwen key was available.

```bash
export AI_API_KEY=<any local value>                     # the bridge accepts only this bearer token
make bridge ARGS="--emulate deepseek --flaky-429 0.05"   # reject json_schema like DeepSeek, inject 429s
make run MODEL=claude-bridge INPUT=request.json
```

`--emulate deepseek` returns HTTP 400 for `json_schema` and requires "json" in the
prompt, so it exercises the same code paths as DeepSeek. Release evidence
labels every bridge run as development evidence. The `model` release gate stays
`NOT_RUN` until a prescribed-model run exists.

## Release profiles (`config/profiles/`)

| Profile | Default mode | Use |
|---|---|---|
| `evaluation_strict_v1` | `evaluation` | Headless evaluator runs. Each case starts from its own baseline, is never integrated into a queue, and is keep-and-export only. `--profile submission` also requires the official adapter. |
| `development_sandbox_v1` | `development` | Interactive and developer runs, including a cumulative repository queue. |

Both profiles share the same kernel guardrails:

- **Permissions:** the `sandbox` permission profile.
- **Network:** `none` for model-generated actions. Dependency setup can reach only PyPI, through an allowlist proxy.
- **Effects:** `EXPORT` is allowed. `CLEANUP_RUN` needs an approval grant. `APPLY_LOCAL` and `PUSH_NEW_BRANCH` are disabled (P1) and return `CAPABILITY_DISABLED`.
- **Plugins:** the reviewed, hash-locked set `builtin_release_v1`.
- **Retention:** policy `retain_default_v1`.
- **Runtime:** `python312_docker_v1`. Any other runtime profile is rejected with `UNKNOWN_RUNTIME_PROFILE`.

A release profile is frozen into the run when it starts (`h_release_profiles`,
bound to the plugin lock hash). A later edit to the file does not change a run
already in progress.
