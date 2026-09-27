# PRD 5 Gap Report — Whole-Repository Queue and Git Workflow

Assessed 2026-09-27 on branch `Dev` against `PRD_5_Whole_Repository_Queue_and_Git_Workflow.md`.

| Req | Pri | Implementation | Status | Evidence |
|---|---|---|---|---|
| QGW-001 | P0 | Queue built only from PRD 1's frozen PREPARED task set (`queue/coordinator.py prepare`) | done | queue E2E |
| QGW-002 | P0 | PRD 1 `max_tasks` preserved; hard cap 20, 80 edges | done | `test_limits_are_enforced` |
| QGW-003 | P0 | Deterministic ACTIONABLE / DUPLICATE / DEPENDENT / AMBIGUOUS / UNSUPPORTED with reason codes (`queue/graph.py`) | done | `test_classification_is_deterministic` |
| QGW-004 | P0 | Duplicates must point at a non-duplicate canonical item | done | `graph.validate` |
| QGW-005 | P0 | DAG from "depends on / blocked by #N"; stable cycle path → INVALID queue | done | `test_cycles_are_rejected_with_a_stable_path` |
| QGW-006 | P0 | `harness queue status` / `show` list blocked items and blocking tasks | done | CLI E2E |
| QGW-007 | P0 | Order: priority label, criticality, source time, ordinal, task ID | done | `test_ready_set_and_order` |
| QGW-008 | P0 | Single active item; fenced queue lease with dead-owner takeover | done | `test_live_lease_blocks_and_dead_owner_is_taken_over` |
| QGW-009 | P0 | Task start recorded at the integration head before work (`TaskExecutionStartV1`) | done | queue E2E start commits chain |
| QGW-010 | P0 | Lifecycle `advance_task` → INDEXING; incremental re-index at the new start | done | queue E2E |
| QGW-011 | P0 | Private `start/working/candidate/verified` refs and a worktree row per task | done | `harness git graph` lists managed refs |
| QGW-012 | P0 | Exactly one commit per integrated issue, parent = task start | done | linear chain assertion in the queue E2E |
| QGW-013 | P0 | Checkpoints only under hidden `checkpoints/NNNN` refs | done | ref listing |
| QGW-014 | P0 | Admission re-checks commit, tree, parent, decision, candidate integrity | done | `_admission_problems` |
| QGW-015 | P0 | Only a PASS completion decision can be integrated | done | NEEDS_INPUT/FAILED items never integrate |
| QGW-016 | P0 | Journaled ref ops PREPARED → DISPATCHED → APPLIED/NOT_APPLIED/UNCERTAIN via `update-ref` CAS (`gitflow/ref_journal.py`) | done | `test_ref_journal_applied_not_applied_uncertain` |
| QGW-017 | P0 | Evidence reuse only on an identical environment hash (content tree + check + runtime + deps) | done | post-advance reports show reuse on identical trees only |
| QGW-018 | P0 | Post-advance failure compensates exactly to the prior head; earlier work kept | done | `test_post_advance_failure_compensates_exactly` |
| QGW-019 | P0 | Dependents of non-integrated items → BLOCKED_DEPENDENCY | done | queue E2E item 5 |
| QGW-020 | P0 | Independent items continue after failures | done | queue E2E |
| QGW-021 | P0 | Post-advance runs the task's contract plus overlapping, shared-surface, or dependent contracts | done | post-advance report for the dependent task includes 2 contracts |
| QGW-022 | P0 | Final aggregate verification of `B → I` with all integrated contracts; COMPLETED_ALL requires PASS | done | `FINAL_AGGREGATE` row PASS |
| QGW-023 | P0 | `_can_start_next` keeps model-call and check-run reserves before admitting a task | partial | no exact budget-boundary fixture test yet |
| QGW-024 | P0 | Untouched ready items → REMAINING_BUDGET; blocked stay blocked | partial | implemented; not covered by an automated test |
| QGW-025 | P0 | Startup reconciliation of unsettled ref ops; crash after `update-ref` observed as APPLIED, never re-applied | done | `test_crash_between_ref_move_and_settlement_recovers_without_duplication` |
| QGW-026 | P0 | Evaluation cases start from baseline, never integrate, no carried state | partial | single-issue evaluation tested; PRD 1's default profile rejects repository+evaluation |
| QGW-027 | P0 | Hardened private git config; no push/merge/remote; `publication_authorized` is always false | done | contract validation rejects `true` |
| QGW-028 | P0 | Managed refs only under `refs/harness/`, internal IDs only | done | ref-name unit tests |
| QGW-029 | P0 | Original checkout identical after every run | done | `repo_integrity` |
| QGW-030 | P0 | Final patch `B → I` artifact plus `ReleaseCandidateHandoffV1` | done | `prd5/final/*` artifacts |
| QGW-031 | P1 | Pause/resume flags; approval pause | done | guided E2E |
| QGW-032 | P1 | `harness queue skip` (unstarted items only) | done | CLI |
| QGW-033 | P1 | `harness git graph --max-count`, `queue status` | done | CLI E2E |
| QGW-034 | P1 | Versioned frozen plan artifact with `plan_sha256` | done | `queue plan` |
| QGW-035 | P1 | `QueuePolicyV1.fail_fast` supported | partial | not exposed as a CLI flag |
| QGW-036 | P1 | Re-running a queue resumes; a settled queue returns its recorded result | done | queue E2E re-run |
| QGW-037 | P1 | Commit trailers carry task ID, plan revision, and source-locator hash | done | commit messages |
| QGW-038 | P1 | Budget allocation rows per task | partial | per-task actual usage not yet settled into `usage_json` |
| QGW-039 | P1 | PARTIAL_SUCCESS with a verified retained candidate | done | queue E2E |
| QGW-040 | P2 | Read-only timeline | deferred | events exist in `h_events`; no timeline command |

## Decisions

- `harness run` always goes through the queue coordinator, including single-issue runs. Every run therefore ends with an integration ref, a final patch, and a release-candidate handoff for PRD 6.
- Exit codes follow PRD 6 section 6.4, which PRD 6 declares canonical: PARTIAL_SUCCESS → 3, INTEGRATION_UNCERTAIN → 7.
- The lease owner ID embeds host and PID. A live local owner is never preempted; a dead one is taken over immediately. Fencing tokens still reject late writers.
