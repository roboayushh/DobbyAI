# PRD 3 Gap Report — Secure Action Sandbox and Tools

Assessed 2026-09-27 on branch `Dev` against `PRD_3_Secure_Action_Sandbox_and_Tools.md`.
Status legend: **done** (implemented and tested), **partial** (implemented with a named gap), **deferred**.

| Req | Pri | Implementation | Status | Evidence |
|---|---|---|---|---|
| SAT-001 | P0 | `ActionService.admit` binds proposal, plan/task revision, lifecycle version, workspace version, policy SHA, code hash (`execution/action_service.py`, `policy/engine.py`) | done | stale-revision rejections in `test_prd2_controller.py`; E2E admission in `test_prd345_e2e.py` |
| SAT-002 | P0 | `guided` / `sandbox` / `delegated` profiles, immutable `PolicySnapshotV1` (`policy/engine.py`), `HARNESS_PERMISSION_PROFILE` | done | `test_guided_profile_pauses_for_one_use_approval_then_resumes` |
| SAT-003 | P0 | `ApprovalService` binding hash, one-use grant consumed in the intent transaction (`policy/approvals.py`); `harness approval show`, `approve`, `deny` | done | guided E2E asserts `used_count == 1`, nothing runs unapproved |
| SAT-004 | P0 | `DockerBackend.availability`, `harness sandbox doctor` | partial | engine required and verified; no minimum engine-version floor enforced |
| SAT-005 | P0 | Pinned base digest, per-machine `config/runtime.lock.json` image ID, label + tool-library hash + architecture verification (`sandbox/runtime_profile.py`) | done | runtime auto-rebuilds when the worker hash changes (observed); `sandbox doctor` |
| SAT-006 | P0 | `--read-only`, `--cap-drop=ALL`, no-new-privileges, non-root numeric UID, pids/memory/cpu/ulimits, `--init`, created-container inspection fails closed (`sandbox/docker_backend.py`) | done | `harness sandbox probe` 14/14; `test_sandbox_adversarial_battery` |
| SAT-007 | P0 | Env allowlist, secret-marker rejection, no engine socket, no home/original repo mounts | done | battery asserts `AI_API_KEY` absent and value never in output |
| SAT-008 | P0 | `--network=none` for all coding and verification containers | done | battery: egress fails; probe: no up interfaces, no routes |
| SAT-009 | P0 | `DependencyEnvironmentService`: separate setup container on an `--internal` network whose only peer is a PyPI-allowlist CONNECT proxy; manifests parsed on host, never executed; project/VCS/URL/local requirements dropped | done | `test_setup_network_enforces_destination_allowlist`, `test_third_party_dependency_repo_verifies_instead_of_blocking` |
| SAT-010 | P0 | Fresh container per action; accepted files persist as checkpoint commits | done | multi-action E2E flows |
| SAT-011 | P0 | `search`, `read_file`, `symbols`, `apply_patch`, `run`, `read_artifact`, `emit_result` (`worker/harness_tools.py`) | done | `test_prd3_units.py` (patch forms, atomicity, path safety, symlinks) |
| SAT-012 | P0 | One Python action composes tools; events in `tool-events.ndjson` | done | `harness action inspect` shows read → patch → run → emit |
| SAT-013 | P0 | Wall time, stdout/stderr caps, workspace growth, new files, output dir watchdog | done | `test_timeout_output_bomb_and_reserved_write_are_rolled_back` |
| SAT-014 | P0 | Process-group kill in `run()`, `--init`, forced container removal | done | battery background `sleep` never outlives the action |
| SAT-015 | P0 | `WorkspaceLockService` single writer with lease tokens | done | lock acquire/release in every action |
| SAT-016 | P0 | Byte-exact checkpoint commit before mutation | done | `test_task_workspace_restore_is_byte_exact` |
| SAT-017 | P0 | Host rescans and rehashes every file after each action (`workspace/manifest.py`) | done | `test_manifest_detects_every_change_kind` |
| SAT-018 | P0 | ALLOWED/UNEXPECTED/DENIED/DISPOSABLE classification; reserved `.git` writes → `POLICY_VIOLATION_ROLLED_BACK` | done | reserved-write E2E |
| SAT-019 | P0 | Single settlement transaction; replay of terminal actions is idempotent | done | E2E settlements |
| SAT-020 | P0 | Failed/violating actions restored to the exact pre-action manifest; rollback diff retained | done | timeout/bomb/violation E2E asserts rollback |
| SAT-021 | P0 | `ActionService.reconcile` (container stop/remove, verified restore, UNKNOWN + quarantine); Ctrl-C settles `CANCELLED_ROLLED_BACK` | partial | reconcile paths exercised by `harness recover`; no forced mid-action process-kill test yet |
| SAT-022 | P0 | Incremental re-index per accepted version (`retrieval/indexer.py` reuse) | done | E2E index events |
| SAT-023 | P0 | Action/result pairs in coder history, ranked above evidence | done | repair and multi-action E2E |
| SAT-024 | P0 | Turn limit, consecutive-rejection limit, failure-signature replan, max 2 replans | done | repair-loop trace |
| SAT-025 | P0 | Coder `run()` output never counts as verification; PRD 4 re-verifies in fresh environments | done | gate tests |
| SAT-026 | P0 | `COMPLETE` freezes an immutable one-commit candidate; `COMPLETE` without changes is rejected (`COMPLETE_WITHOUT_CHANGES`) | done | `test_complete_without_changes_is_rejected_not_passed` |
| SAT-027 | P0 | Original repository never mounted or written | done | `repo_integrity` equality in every E2E |
| SAT-028 | P0 | `--json` prints exactly one object; logs on stderr | done | `test_cli_run_headless_json_and_exit_code` (18 commands) |
| SAT-029 | P1 | Dependency cache key = image digest, arch, adapter, requirements, network policy | done | cache reuse across tasks |
| SAT-030 | P1 | Commit messages sanitized (URLs, control chars stripped) with trailers | done | `test_candidate_commit_is_single_parent_with_sanitized_message` |
| SAT-031 | P1 | Read-only actions mount `/workspace` read-only | done | admission `read_only` flag |
| SAT-032 | P1 | `SandboxBackend` protocol; only `python-default` profile shipped | partial | Node.js profile not added (PRD allows only after containment tests) |

## Security decisions

- There is no host-execution fallback anywhere. A missing engine or image yields `SANDBOX_UNAVAILABLE`/`RUNTIME_IMAGE_INVALID` → `BLOCKED_ENVIRONMENT`.
- The `.gitattributes` smudge/eol filters never touch workspace bytes (hash-object `--no-filters`, `cat-file --batch`).
- `apply_patch` requires `expected_hashes` only for whole-file overwrites; edit lists and diffs are anchored by their content.
- Newline/CR in tree paths is rejected. This closes an `update-index --index-info` injection vector found by tests.
- A symlink to a directory is removed during restore, never followed. This was a bug found by tests and is now fixed.
