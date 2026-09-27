# PRD 6 Gap Report: Evaluator Integration, Plugins, Export, and Release Hardening

Assessed 2026-09-27 on branch `Dev` against `PRD_6_Evaluator_Plugins_Export_and_Release_Hardening.md`.
P0 is implemented. P1 (local apply and publish) is **disabled by design** and returns
`CAPABILITY_DISABLED`. P2 is not implemented.

Status key:

- **done**: implemented, with automated evidence.
- **evidence pending**: implemented, but the gate needs an environment this release was not run in.
- **disabled**: intentionally off (P1).

| Req | Pri | Implementation | Status | Evidence |
|---|---|---|---|---|
| REL-001 | P0 | `native_json_v1` adapter → `EvaluatorGateway`. `harness run --input … --non-interactive` prints exactly one JSON object on stdout. | done | `test_headless_evaluation_run_exports_a_round_tripped_patch`; `test_invalid_requests_fail_before_any_mutation` (one stdout line); live runs below |
| REL-002 | P0 | `official_adapter_placeholder.py`: an isolated, version-pinned slot that returns `OFFICIAL_ADAPTER_NOT_CONFIGURED` and never guesses at the protocol. `doctor --profile submission` blocks on it. | done (slot) | `test_invalid_requests…[official]`, `test_version_plugins_and_doctor_emit_single_json_objects` |
| REL-003 | P0 | Full validation before any effect: schema, profile, runtime, P1 effects, mode, model, key, output paths, plugins, minimum call budget | done | 9 parametrized invalid cases assert 0 run rows and an untouched repo; `test_output_inside_source_repository_is_rejected`; `test_placeholder_model_and_missing_key_are_prerequisite_failures` |
| REL-004 | P0 | `release/status_mapping.py`: one external status and exit code per terminal internal status | done | `test_every_internal_terminal_status_has_one_exit_code` |
| REL-005 | P0 | `write_result_atomic` (temp file, fsync, rename). A fallback INVALID result is never written inside the source. | done | e2e asserts `result.json` equals stdout; `test_malformed_and_missing_request_files_are_typed` |
| REL-006 | P0 | Root Makefile: `setup`, `run` (interactive or `INPUT=`), `test`, `doctor`, `clean` (harness caches only) | done | clean-machine run below |
| REL-007 | P0 | `requirements.lock` with hashes, installed with `pip --require-hashes`. User space only, no sudo. | done | clean-machine `make setup` |
| REL-008 | P0 | `ensure_runtime` → `BLOCKED_ENVIRONMENT` (exit 3). There is no host execution path. | done | `test_unreachable_sandbox_is_blocked_environment_never_host_execution` (no model call, source untouched) |
| REL-009 | P0 | `export/patch_adapter.py`: `unified_git_patch_v1` from hardened `diff-tree`, bound to `B` and to the candidate commit, tree, and content hash | done | `test_bundle_round_trips_every_file_type` |
| REL-010 | P0 | `export/round_trip.py`: apply to a fresh materialization of `B` and require exact tree and content equality | done | same, plus `test_malicious_diff_driver_and_filters_never_execute` |
| REL-011 | P0 | Bundle staged in a sibling temp dir, fsynced, renamed in one step; checksums file; `verify_bundle` | done | `test_failed_export_leaves_no_partial_bundle`, `test_existing_output_is_never_overwritten_without_replace`, `test_tampered_bundle_fails_verification_and_is_rebuilt`, `test_protected_and_symlinked_destinations_are_refused` |
| REL-012 | P0 | `report_builder.py`: executed, missing, and skipped checks; baseline failures; incomplete tasks; usage; limitations; "nothing was pushed" | done | report assertions in the headless e2e test |
| REL-013 | P0 | Export reads only private state. The source is never opened for writing. | done | `repo_integrity` before and after in the e2e and export tests |
| REL-014 | P0 | `plugins/registry.py`: lock hash, module content hash checked **before import**, reviewed built-ins only | done | `test_pinned_builtin_set_resolves…`, `test_tampered_lock_hash_fails`, `test_content_hash_mismatch_fails_before_import`, `test_checked_in_lock_is_current` |
| REL-015 | P0 | Interface major version and configuration JSON-schema validation at startup | done | `test_interface_major_mismatch_fails`, `test_invalid_configuration_fails`, `test_failing_self_check_blocks_startup` |
| REL-016 | P0 | Dependency DAG with a stable cycle path | done | `test_missing_dependency_fails`, `test_dependency_cycle_is_reported_stably`, `test_cycle_in_manifests_fails` |
| REL-017 | P0 | `plugins/kernel.py` `KernelGateway`: identical policy, budget, and approval denials for any controller | done | `test_replacement_controller_gets_the_same_denials_as_builtin`, `test_completion_authority_stays_with_the_host_gate` |
| REL-018 | P0 | The plugin root and lock come from the harness install only. A repository-supplied lock is rejected. | done | `test_repository_supplied_lock_is_rejected` |
| REL-019 | P0 | One frozen profile fingerprint for every role; `AI_API_KEY` only; no fallback; typed provider errors | done | `test_live_adapter_fails_closed_without_fallback`, `test_profile_fingerprint_is_deterministic_and_secret_free`, `test_bundled_profiles_send_json_object_mode_and_no_secret` |
| REL-020 | P0 | `_Strict` contracts reject other majors (`UNSUPPORTED_SCHEMA_VERSION`) and unknown fields. Migrations 1–7. | done | `test_future_major_version_and_unknown_fields_fail_closed`, `test_migrations_one_to_seven_apply_clean_and_idempotently`, `test_populated_prd5_database_upgrades_without_losing_rows` |
| REL-021 | P0 | `release/reproducibility.py`: model, adapter, config, image, source, dependencies, checks, plugins, schemas | done | provenance assertions in the headless e2e test |
| REL-022 | P0 | `replay --mode audit` rebuilds the terminal state from events and artifacts with no effects | done | e2e (`artifacts_verified > 10`) |
| REL-023 | P0 | `reverify` reruns checks without the model; `recorded` → `REPLAY_MODE_UNSUPPORTED`; a live rerun is a new run | done | e2e |
| REL-024 | P0 | `KernelPrincipal` minted only through a private token. Text never creates a grant. | done | `test_fake_approval_output_creates_no_grant`, `test_p1_effects_are_disabled_and_principals_cannot_be_forged`, `test_plugins_cannot_forge_principals` |
| REL-025 | P0 | Grant bound to the request SHA-256 (operation, target, base, artifact, run, policy, expiry); `max_uses=1` | done | `test_grant_is_bound_single_use_and_revocable`, `test_expired_requests_cannot_be_granted` |
| REL-026 | P0 | A headless `CLEANUP_RUN` records a request, performs nothing, and exits 6 | done | `test_requested_cleanup_is_pending_until_approved` |
| REL-027 | P0 | `retention/cleanup.py`: only registered resources of one run are eligible | done | `test_cleanup_plan_lists_only_registered_resources` |
| REL-028 | P0 | `retain_default_v1`: evidence, refs, and DB are kept; exports and originals are never targets | done | `test_cleanup_requires_exact_approval_and_keeps_evidence` |
| REL-029 | P0 | Plan → approval → tombstone → delete → receipt; a changed plan invalidates the grant | done | `test_resource_change_after_approval_invalidates_the_plan` |
| REL-030 | P0 | `scripts/clean_machine.sh`: fresh `python:3.12-bookworm` container → `make setup`, `make test`, `make run INPUT=` | see [release evidence](#release-evidence) | `release-evidence/clean-machine/*.json` |
| REL-031 | P0 | `release/evidence.py`: unexecuted gates are `NOT_RUN`; bridge and baseline records never satisfy model gates | done | `test_unexecuted_gates_are_never_pass`, `test_development_bridge_runs_never_satisfy…`, `test_baseline_records_and_bridge_comparisons_are_not_release_evidence` |
| REL-032 | P0 | CycloneDX SBOM, third-party notices, both hash locks, provenance doc | done | `test_provenance_files_ship_with_the_release` |
| REL-033 | P0 | README plus `docs/{configuration,profiles,evaluator,security,limitations,provenance,release-evidence}.md` | done | `documentation` gate |
| REL-034 | P0 | `baseline_loop.py` (single-role shell loop, same model, budget, runtime, and oracle), `compare_eval.py` (N trials, development and held-out reported separately) | methodology done; prescribed-model run pending | [release evidence](#release-evidence) |
| REL-035–044 | P1 | Local apply and publish | disabled | `test_disabled_p1_commands_are_typed_not_simulated`; `publication_authorized` is always false |
| REL-045 | P2 | Read-only release viewer | not implemented | — |

## Additional changes made during PRD 6

- **Providers:** DeepSeek and Qwen profiles; `json_object` mode; Qwen `enable_thinking=false`; `Retry-After` backoff; `reasoning_content`, null content, and truncation handling; loopback HTTP for local servers; environment overrides recorded in the fingerprint; `harness doctor --live` probe.
- **Sources (PRD 1 extension):** plain-folder and ZIP import (streaming, bounded); byte-exact manifests; disposable caches excluded; dirty detection by content.
- **Robustness from live runs:**
  - span-end off-by-one in retrieval;
  - `cwd="/workspace"` accepted by the sandbox tools;
  - repair of triple-quoted `python_action` values;
  - a planner query that names a file path returns that file;
  - a planner request that yields no new evidence gets bounded feedback instead of aborting;
  - wall and output-token reserves are capped at half the budget, so evaluator budgets below `2 × timeout` no longer crash with `INTERNAL_ERROR`;
  - `budgets.model_calls < 3` is `INVALID`.
- **Security:** only the harness's own `.env` is read. Explicit `HarnessConfig` arguments now take precedence over `HARNESS_*` variables. The suite is hermetic against an exported live configuration (`tests/conftest.py`).

## Release evidence

_Filled in from `harness release evidence` at release time; see below._
