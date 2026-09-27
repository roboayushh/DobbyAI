# PRD 4 Gap Report — Verification, Feedback, and Recovery

Assessed 2026-09-27 on branch `Dev` against `PRD_4_Verification_Feedback_and_Recovery.md`.

| Req | Pri | Implementation | Status | Evidence |
|---|---|---|---|---|
| VFR-001 | P0 | Contract frozen at `PLAN_READY` before `CODING` (`verification/contract_service.py`, `service.ensure_contract`) | done | `VERIFICATION_CONTRACT_FROZEN` precedes first action in every E2E |
| VFR-002 | P0 | Every criterion maps to check IDs; unmapped criteria block PASS | done | `test_gate_unmapped_criterion_blocks_pass` |
| VFR-003 | P0 | Baseline on the task start commit in fresh sandboxes | done | `verification baseline` shows `EXPECTED_REPRODUCTION_FAILURE` |
| VFR-004 | P0 | Missing baselines recorded as limitations, never fabricated | done | gate `REQUIRED_BASELINE_NOT_CAPTURED` limitation |
| VFR-005 | P0 | Candidate identity re-verified before checks (`verify_candidate`) | done | gate matrix `candidate_intact=False` → UNVERIFIED |
| VFR-006 | P0 | Fresh copy per check from the object store | done | `h_verification_environments` one row per run |
| VFR-007 | P0 | Same hardened sandbox, `--network=none`, read-only dependency mount | done | check containers use PRD 3 spec builder |
| VFR-008 | P0 | Tiers: focused (task-named node IDs), relevant, broad, invariant syntax | done | contract inspection output |
| VFR-009 | P0 | JUnit parsing with DTD/entity rejection; zero tests, all-skipped, and nonzero exit on a green report are non-pass; missing third-party modules → BLOCKED_ENVIRONMENT | done | `test_prd4_units.py` parser tests |
| VFR-010 | P0 | False-pass matrix in the completion gate; **protected oracle tests**; always-true assertion swaps BLOCKING | done | `test_gate_false_pass_matrix`, `test_test_tampering_never_passes` |
| VFR-011 | P0 | Per-test baseline vs candidate comparison (resolved targets, regressions, pre-existing, coverage lost) | done | `test_pre_existing_unrelated_failure_does_not_block_resolved_target` |
| VFR-012 | P0 | One fresh rerun on a new regression; disagreement → INCONCLUSIVE (FLAKY) | done | comparator `flaky` unit test |
| VFR-013 | P0 | Deterministic diff-scope review (deleted/skipped/weakened tests, discovery config, secrets, hardcoding, scope) | done | diff review unit tests |
| VFR-014 | P0 | Separate validator call and context; coder narrative excluded | done | validator packet sections verified in E2E |
| VFR-015 | P0 | Validator overlay tests only under `tests_overlay/`, run outside the candidate | partial | implemented; no dedicated overlay E2E yet |
| VFR-016 | P0 | Only the host gate emits PASS; model output is a proposal | done | gate is the sole writer of PASS decisions |
| VFR-017 | P0 | Results bound to candidate, contract, test-set, environment, command, overlay hashes | done | `CheckRunResultV1` / `CompletionDecisionV1` fields |
| VFR-018 | P0 | Plan revision supersedes the contract; stale decisions not reused | done | `VERIFICATION_CONTRACT_INVALIDATED` on replan |
| VFR-019 | P0 | PASS / FAILED / UNVERIFIED / BLOCKED_ENVIRONMENT / BUDGET_EXHAUSTED / NEEDS_INPUT / CANCELLED with priority ordering | done | gate matrix |
| VFR-020 | P0 | Host-generated repair feedback from observed failures (not coder claims) | done | `test_repair_loop_turns_failed_candidate_into_pass` |
| VFR-021 | P0 | Max 2 repairs; same failure signature → replan; replan limit 2 | done | repair-loop trace |
| VFR-022 | P0 | Each repair freezes a new candidate | done | decisions `FAILED`, then `PASS` on distinct candidates |
| VFR-023 | P0 | Verification budget reserve/settle per check | done | `h_verification_budgets` |
| VFR-024 | P0 | Attempts and check intents persisted; recorded decisions replayed | partial | no forced crash-mid-attempt test |
| VFR-025 | P0 | Cancellation settles and reports CANCELLED | partial | Ctrl-C path implemented; not covered by an automated test |
| VFR-026 | P0 | Original repository unchanged | done | `repo_integrity` in all E2E |
| VFR-027 | P0 | `VerificationReportV1` plus `harness report` with verdict and pre-existing detail | done | CLI E2E |
| VFR-028 | P0 | Pure JSON on stdout | done | CLI E2E |
| VFR-029 | P1 | Deterministic best-partial candidate | done | `best_partial` ordering |
| VFR-030 | P1 | Parser/runtime adapters (`pytest-junit@1`, unittest, exit-code) | done | parser units |
| VFR-031 | P1 | Contract amendment only through plan revision (supersede + new baseline) | done | replan trace |
| VFR-032 | P1 | Read-only report contract | partial | JSON report exists; export bundle belongs to PRD 6 |

## Notable fix found by testing

Replacing `assert add(2, 3) == 5` with `assert True` in the task's own test used to produce PASS. Two guards now close this:

1. Oracle test files are the files named by focused checks plus files holding baseline-failing tests. When the candidate modifies one, focused and relevant checks run it in its original task-start form.
2. Diff review raises a BLOCKING `trivial_assertion_added` finding and an `oracle_test_modified` warning.
