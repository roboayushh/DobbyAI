# PRD 2 Gap Report

Baseline assessed against `docs/prd2.md` on 2026-09-27. PRD 1 is green (98 tests) and the PRD 2 implementation starts from commit `3095263`.

| Requirement | Existing component/file | Status | Migration impact | Planned test |
|---|---|---|---|---|
| MCO-001 | `PreparationController`, `RunStore`, `ArtifactStore`, `WorkspaceManager` | adapt | Add lifecycle projection; no PRD 1 state changes | AT2-001/002 handoff integrity and containment |
| MCO-002 | `HarnessConfig` deliberately does not read model credentials | add | Persist credential name only | AT2-003/004 and secret scans |
| MCO-003 | `RunRequestV1.runtime_profile` | add | Add frozen `h_run_model_config` | AT2-003/005/006 fingerprint invariants |
| MCO-004 | Existing `httpx` transport patterns | add | Add model-call rows | Adapter unit tests and AT2-040 gated smoke |
| MCO-005 | Pydantic strict-contract convention | reuse/adapt | Model-call parse status | Schema negative tests and AT2-007/008 |
| MCO-006 | PRD 1 source manifest/import policy | adapt | Add `h_file_index` | AT2-009 inventory exclusions |
| MCO-007 | None | add | Add `h_symbols` | AT2-010/011 Python/JS/TS and fallback |
| MCO-008 | None | add | Evidence/query metadata | AT2-012/013 deterministic retrieval |
| MCO-009 | Source hashes and immutable baseline | adapt | Add `h_evidence` | AT2-014 revision/hash rejection |
| MCO-010 | None | add | Add packet/item tables | AT2-015 role separation |
| MCO-011 | None | add | Packet token metadata | AT2-016/017 capacity and pinned overflow |
| MCO-012 | None | add | Summary/context records | AT2-018/019 deterministic compaction |
| MCO-013 | Task trust labels only | add | Truth status and invalidation | AT2-020 provenance invariants |
| MCO-014 | `ArtifactStore` hashes content but currently permits replacement | adapt | Extensible artifact kinds | Exact reconstruction and tamper tests |
| MCO-015 | None | add | Add ledger and reservations | AT2-023/025 atomic race and settlement |
| MCO-016 | None | add | Ledger reserve fields | AT2-024 future verification reserve |
| MCO-017 | `PreparationController` host-owned PRD 1 state | adapt | Separate run/task lifecycle | State routing unit tests |
| MCO-018 | None | add | Evidence/call records | AT2-029 bounded/deduplicated loop |
| MCO-019 | None | add | Add revisioned `h_plans` | Plan persistence and supersession tests |
| MCO-020 | Read-only workspace and PRD 2 stub | adapt | Add proposal records/artifacts | AT2-031/038 no execution or source writes |
| MCO-021 | None | add | Lifecycle state | AT2-032 COMPLETE maps to verification only |
| MCO-022 | Existing bounded GitHub transport | adapt | Call error/usage fields | Failure-classification and retry tests |
| MCO-023 | PRD 1 transaction/event conventions | adapt | Intent + reservation tables | AT2-026/027 crash reconciliation |
| MCO-024 | None | add | Add lease table/version CAS | AT2-028 concurrent controller |
| MCO-025 | Terminal sanitization and task trust label | adapt | Persist filtered packets | AT2-021/022 injection and secret corpus |
| MCO-026 | Existing JSON renderer/CLI pattern | adapt | None | AT2-036 stdout/stderr separation |

## P1 mapping

| Requirement | Existing component/file | Status | Migration impact | Planned test |
|---|---|---|---|---|
| MCO-027 | None | add, disabled by default | Reuses calls/summaries | Same-profile and ledger tests |
| MCO-028 | Source/file hashes exist | add | Cache keys in file index | Corrupt cache rebuild test |
| MCO-029 | `inspect` command | adapt | Read-only queries | Packet/evidence/budget inspection tests |
| MCO-030 | Ordered task rows and dependencies | adapt | Task lifecycle | AT2-035 first-unblocked selection |

## Compatibility decisions

- Keep `h_runs.state` and `RunState` as PRD 1 preparation projections; PRD 2 uses additive lifecycle tables.
- Name the next migration `002_prd2_orchestration.sql` to match the repository's numeric migration loader.
- Replace the restrictive artifact-kind check in migration 002 and validate kinds through a trusted application registry.
- PRD 2 artifacts use unique versioned paths and fail on replacement. Existing PRD 1 idempotent replay remains readable.
- No live model call is part of deterministic tests; the release smoke is explicitly gated on configuration and `AI_API_KEY`.
