# Headless evaluator interface

```bash
harness run --input /abs/path/request.json --non-interactive
# or
make run INPUT=/abs/path/request.json
```

- **stdout** carries exactly one JSON object: the final `EvaluatorResultV1`. Progress
  and diagnostics go to stderr.
- If the request names a `result_path`, the same object is written there atomically
  (temp file, fsync, rename). A partial result file is never left behind.
- `--non-interactive` never prompts. Missing input is `INVALID` (exit 2).
- The process exit code follows the canonical table below. The JSON `status` is
  authoritative. `make` reports any failing recipe as exit 2, so automation that needs
  the canonical code should call `.venv/bin/harness run --input …` directly. `make run
  INPUT=…` prints the identical JSON result.

## Request (`native_json_v1`)

```json
{
  "schema_version": "1.0",
  "request_id": "ereq_case_0001",
  "adapter": {"name": "native_json_v1", "version": "1.0.0"},
  "repository": {"kind": "local_git", "locator": "/evaluator/input/repository"},
  "task_mode": "single_issue",
  "execution_mode": "evaluation",
  "task": {"source_type": "direct_text", "text": "ordinal(11) returns '11st'; it must return '11th' ..."},
  "profile": "evaluation_strict_v1",
  "runtime_profile": "python312_docker_v1",
  "model_config_ref": "deepseek",
  "budgets": {"model_calls": 30, "input_tokens": 150000, "output_tokens": 20000, "wall_seconds": 1800},
  "result_path": "/evaluator/output/result.json",
  "export_path": "/evaluator/output/bundle",
  "requested_effects": ["EXPORT"],
  "idempotency_key": "eval-case-0001"
}
```

| Field | Rules |
|---|---|
| `schema_version` | Must be `1.x`. Other majors fail with `UNSUPPORTED_SCHEMA_VERSION`. |
| `adapter` | `native_json_v1`. `official_hackathon_v1` is a pinned placeholder that returns `OFFICIAL_ADAPTER_NOT_CONFIGURED`: the harness does not guess at an unknown protocol. |
| `repository.kind` | `local_git` (optional `revision`), `public_https` (GitHub HTTPS URL), `local_folder` (absolute path to a plain directory), or `local_zip` (absolute path to a `.zip`; a single common root folder is stripped). The source is never modified. |
| `task` | `direct_text` (`text`), `issue_url`, or `issue_number` (with `repository_owner`). `repository_query` (with `state`, `labels`, `max_tasks` ≤ 20) is for `development` mode only. |
| `execution_mode` | `evaluation`: one independent case from its own baseline, never integrated into a queue. It requires `task_mode: single_issue`. `development` allows the repository queue. |
| `model_config_ref` | A profile id from `config/model_profiles.toml`. It defaults to `HARNESS_MODEL_PROFILE`. The key always comes from the host `AI_API_KEY`; secrets in the request are rejected with `SECRET_IN_REQUEST`. |
| `budgets` | Optional. Each field replaces the default for this run: `model_calls` 1–2000, `input_tokens` ≥ 1000, `output_tokens` ≥ 100, `wall_seconds` 30–86400 (defaults in [configuration.md](configuration.md#budgets)). |
| `result_path`, `export_path` | Absolute paths. They must not be inside the source repository or harness run storage. An existing export is never overwritten. |
| `requested_effects` | `EXPORT` (default). `CLEANUP_RUN` records a pending approval. `APPLY_LOCAL` and `PUSH_NEW_BRANCH` return `CAPABILITY_DISABLED`. |
| `idempotency_key` | Resubmitting the same request returns the recorded result without a new run. The same `request_id` with different content returns `IDEMPOTENCY_CONFLICT`. |

The whole request is validated **before any effect**: schema, release profile,
runtime profile, disabled effects, evaluation mode, model profile completeness, key
presence, output paths, and plugin resolution. An invalid request makes no model call,
creates no run row, and does not touch the repository.

## Result (`EvaluatorResultV1`)

```json
{
  "schema_version": "1.0",
  "request_id": "ereq_case_0001",
  "run_id": "run_bdf311b47c5a4812",
  "status": "PASS",
  "exit_code": 0,
  "input": {"baseline_commit": "3ca6ef6…", "baseline_tree": "048697b…", "content_sha256": "…"},
  "candidate": {"commit": "d8adf90…", "tree": "ef4287f…", "content_sha256": "…"},
  "tasks": [{"task_id": "tsk_…", "status": "PASS", "changed_paths": ["src/itsdangerous/encoding.py"],
             "verification_report_artifact_id": "art_…"}],
  "verification": {"status": "PASS", "required_checks": 3, "passed_checks": 3, "failed_checks": 0, "skipped_checks": 0,
                   "unavailable_checks": 0, "aggregate_report_artifact_id": "art_…"},
  "usage": {"model_calls": 7, "input_tokens": 122033, "output_tokens": 4666, "wall_seconds": 123, "estimated_fields": []},
  "export": {"status": "VALID", "bundle_path": "/evaluator/output/bundle", "manifest_sha256": "…", "patch_sha256": "…"},
  "pending_approvals": [],
  "limitations": ["PASS describes declared observed checks and does not guarantee hidden-test success."],
  "error": null,
  "reproducibility_manifest_artifact_id": "art_…",
  "settled_at": "2026-09-27T01:01:52Z"
}
```

`PASS` is set only by the host completion gate: the required checks passed on a fresh
copy, there were no new regressions against the baseline, the validator raised no
blocking finding, and the diff review was clean. Model output is only ever a proposal.

## Status and exit codes

| Exit | Status |
|---|---|
| 0 | `PASS`, `COMPLETED_ALL` |
| 2 | `INVALID` (request, schema, configuration, model profile, key, output path, plugin), `CAPABILITY_DISABLED` |
| 3 | `PARTIAL_SUCCESS`, `UNVERIFIED`, `BLOCKED_ENVIRONMENT` (for example no Docker; there is never a host fallback), `EXPORT_INVALID` |
| 4 | `FAILED`, `VERIFICATION_FAILED` |
| 5 | `BUDGET_EXHAUSTED` |
| 6 | `NEEDS_INPUT`, `PENDING_APPROVAL` |
| 7 | `INTERNAL_ERROR`, `INTEGRATION_UNCERTAIN`, unresolved `ACTION_UNKNOWN` |
| 130 | `CANCELLED` (SIGINT) |

The full internal-to-external mapping is in `src/harness/release/status_mapping.py`.
A test checks that every terminal internal status has exactly one entry.

## Export bundle

```
bundle/
  manifest.json            ExportManifestV1: baseline B, candidate identity, round-trip result, file list
  result.json              the same EvaluatorResultV1
  patch.diff               unified_git_patch_v1 from B to the candidate (binary-safe)
  report.md                human-readable report: checks run, missing, or skipped; baseline failures; usage; limitations
  checksums.sha256         sha256 of every file above
  evidence/
    provenance.json        reproducibility manifest (model, adapter, image, source, checks, plugins, schemas)
    verification-summary.json
    task-results.json
    usage.json
```

Before a bundle is published, the patch is applied with hardened `git apply` to a
fresh materialization of `B`. The resulting tree and file contents must equal the
candidate exactly, or the export is `EXPORT_INVALID`. The bundle is assembled in a
sibling temp directory and renamed into place in one step. `harness export RUN_ID
--output DIR` rebuilds a bundle later from recorded state. It is read-only and
valid for any settled run.

To apply the patch to your own checkout: `git apply --binary patch.diff`.

## Pending approvals

A requested `CLEANUP_RUN` is never performed without a grant. The run exits 6 with
`PENDING_APPROVAL` and lists `pending_approvals[].capability_request_id`. An operator
can grant it (`harness approve capreq_…`) and then run `harness clean RUN_ID`. Text
from the model, the repository, or a log that claims approval does not create a grant.

## Replay

- `harness replay RUN_ID --mode audit`: rebuilds the terminal state from recorded
  events and artifacts, verifies artifact hashes, and has no external effects.
- `harness replay RUN_ID --mode reverify`: reruns the recorded checks on the recorded
  candidate in fresh containers, with no model calls.
- `--mode recorded` returns `REPLAY_MODE_UNSUPPORTED`. A live rerun is a new run, and
  stochastic model output is not promised to be identical.

## Official adapter slot

`src/harness/evaluator/official_adapter_placeholder.py` holds the isolated,
version-pinned slot for an official hackathon protocol. Until the protocol is
published it rejects requests with `OFFICIAL_ADAPTER_NOT_CONFIGURED`, and
`harness doctor --profile submission` reports it as blocking. Implementing it means
translating the official request into the native contract and back, without any
change to the kernel.
