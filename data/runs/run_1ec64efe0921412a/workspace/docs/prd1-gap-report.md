# PRD 1 Gap Report

Generated: 2026-09-27  
Inspected by: Antigravity implementation agent  
Basis: Existing issue-intake implementation vs. PRD 1 requirements

---

## Existing Capabilities (Pre-PRD 1)

- GitHub repository/issue URL parsing and validation (`validator.py`)
- Paginated issue fetching with PR exclusion (`provider.py`)
- Auth-injecting, retry-capable HTTPS transport (`transport.py`)
- SQLite-backed snapshot + page cache storage (`store.py`)
- IssueSnapshot model with content hash (`models.py`)
- Terminal rendering with ANSI-escape sanitization (`renderer.py`)
- Interactive CLI (`harness run`) and non-interactive commands (`cli.py`)
- IssueIntakeService orchestrating provider/store/recorder (`service.py`)

---

## Requirement Gap Table

| Requirement | ID | Existing component/file | Status | Planned change |
|---|---|---|---|---|
| Preserve existing issue intake | FND-001 | `service.py`, `store.py`, `provider.py`, `cli.py` | reuse | Add `ExistingIssueIntakePort` adapter; existing tests remain green |
| Explicit task-mode selection | FND-002 | None | add | `RunRequestV1.task_mode` in `contracts/run_request.py`; wizard step in CLI |
| Explicit execution mode | FND-003 | None | add | `RunRequestV1.execution_mode`; persisted in `h_runs` |
| Typed request validation | FND-004 | `validator.py` (URL only) | adapt | `RequestValidator` with Pydantic + cross-field rules |
| Local Git acquisition | FND-005 | None | add | `RepositoryService` + `SourceImporter` + `GitRunner` in `repository/` |
| Public HTTPS Git acquisition | FND-006 | None | add | `RepositoryService.acquire()` for public HTTPS |
| Exact source identity | FND-007 | None | add | `SourceIdentityV1` contract + `h_source_snapshots` table |
| Private workspace | FND-008 | None | add | `WorkspaceManager` in `workspace/`; `h_workspaces` table |
| Single task snapshot | FND-009 | `IssueSnapshot` (partial) | adapt | `TaskNormalizer` -> `TaskSpecV1`; `h_tasks` table |
| Bounded repository queue | FND-010 | `provider.list_issues` (pagination) | adapt | `TaskPreparationService.prepare_queue()` with selection/dedup/ordering |
| Evaluation isolation | FND-011 | None | add | Cross-field validation in `RequestValidator`; reject repo+default-eval |
| Persistent state machine | FND-012 | None | add | `RunStore` with `h_runs`/`h_events`; state transition enforcement |
| Idempotent preparation | FND-013 | None | add | `idempotency_key` UNIQUE in `h_runs`; `RunStore.create_run()` |
| Artifact integrity | FND-014 | `store.save_snapshot()` (no hash) | adapt | `ArtifactStore` with SHA-256, atomic rename, metadata row |
| Safe Git invocation | FND-015 | None | add | `GitRunner` with arg-array, controlled env, hooks disabled |
| Secret hygiene | FND-016 | `transport.py` (partial) | adapt | URL credential rejection; env-dump guard; redaction in `GitRunner` |
| Terminal-safe rendering | FND-017 | `renderer._safe()` | reuse | Extend `ResultRenderer`; add bidi/C1 stripping |
| Headless result contract | FND-018 | `--json` (envelope, not typed) | adapt | `ResultRenderer.render_json()` emits `PreparedRunResultV1` to stdout only |
| Task dependency representation | FND-019 | None | add | `h_task_dependencies` table; `validate_dependencies()` cycle detection |
| Crash-safe acquisition | FND-020 | None | add | `WorkspaceManager.recover()`; `reconcile_interrupted()` |
| Storage migration discipline | FND-021 | `executescript` (no versioning) | adapt | `h_schema_migrations` table; forward-only numbered migrations |
| Inspectability | FND-022 | None | add | `harness status RUN_ID` and `harness inspect RUN_ID` commands |
| Clean setup + smoke target | FND-023 | `make setup`, `make test` | adapt | Add `make smoke` target |
| Read-only queue preview | FND-024 | None | add | P2 deferred; tracking issue: deferred until PRD 2 |

---

## Compatibility Decisions

1. Existing `snapshots` table is NOT renamed; `h_tasks.source_snapshot_id` references by text without FK.
2. `IssueIntakeService` is not replaced; `ExistingIssueIntakePort` wraps it via composition.
3. `JSONEnvelope` is kept for existing commands; new commands emit `PreparedRunResultV1`.
4. `data/harness.db` is the existing database; additive migrations run at startup.
5. Exit codes 2-4 are inherited; PRD 1 adds 5 (integrity) and 10 (internal).
6. `HarnessConfig` is extended, not replaced; new fields have safe defaults.

---

## Status Summary

- P0 requirements (18): 4 reuse, 6 adapt, 8 add
- P1 requirements (6): 0 reuse, 2 adapt, 4 add
- P2 requirements (1): 1 add (deferred)
- Existing test suite: 76 tests passing; no destructive changes
