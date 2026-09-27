# Security model

This document covers what the harness trusts, what it does not trust, and how each
boundary is enforced. Each boundary has a test, listed in [prd6-gap-report.md](prd6-gap-report.md)
and in the earlier PRD gap reports.

## Trust boundaries

| Trusted (host kernel) | Untrusted |
|---|---|
| Harness source, `config/**`, the plugin lock, the runtime Dockerfile and locks | Target repository content, including its `.env`, `.gitattributes`, hooks, config and tests |
| Host environment (`AI_API_KEY`, `GITHUB_TOKEN`) | Issue and task text |
| The SQLite run database and artifact store | All model output (plans, actions, validator findings) |
| The operator, through interactive approval commands | Anything a sandboxed process prints, writes, or claims |

The model **proposes**. The host kernel decides. Only the host completion gate can
emit `PASS`. Model output, repository content, logs, and plugins cannot create grants,
enable effects, widen permissions, change the model, or install plugins.

## One model, one key

- Every role uses the one frozen model profile. There is no silent fallback model. A
  failed provider produces a typed error.
- `AI_API_KEY` is read from the host environment immediately before each request. It
  never enters request JSON, prompts, the database, artifacts, logs, exports, commits,
  or containers.
- Only the harness's own `.env` is loaded. The current directory and target repository
  are never searched for one.
- Requests containing credential-like values are rejected with `SECRET_IN_REQUEST`.
  Export bundles and release evidence are secret-scanned, including for the literal
  values of the keys that are present.

## Sandbox (model-generated code)

Model-generated Python runs only in a fresh Docker container. If Docker is missing,
the result is `BLOCKED_ENVIRONMENT`: there is **no host fallback**.

- The image is pinned by content-addressed ID, never by a mutable tag. Its base image
  is pinned by digest.
- `--read-only`, `--cap-drop=ALL`, `no-new-privileges`, a non-root UID, `--network=none`,
  and limits on PIDs, memory (no swap), CPU and file size.
- An allowlisted environment only. No API key, Docker socket, home directory,
  credentials, host database, approval store, or original repository is mounted. Only
  the private task workspace and a read-only dependency directory are mounted.
- The effective settings are re-inspected after the container is created. Any mismatch
  fails closed.
- After every action the workspace is rescanned and rehashed. Undeclared, reserved, or
  oversized changes are rolled back to the exact checkpoint.
- Dependency setup is a separate, non-model container on an internal network. Its only
  route out is a locked-down CONNECT proxy that allows PyPI hosts only.

`harness sandbox probe` runs 14 isolation checks from inside a real container.

## Source safety

- The original repository is never mounted, modified, or used as a worktree. Git,
  folder, and ZIP sources are imported into a private bare repository with byte-exact
  manifests (`hash-object --no-filters`), so the repository's `.gitattributes`, filters,
  and hooks never run.
- ZIP import is streaming and bounded: at most 50 000 entries, 500 MB total, 20 MB per
  file, and a 100:1 compression ratio. Absolute paths, `..`, control characters,
  embedded `.git` metadata, symlinks, special files, encrypted entries, and duplicate
  normalized paths are all rejected.
- Hardened private git: no hooks, no filters or textconv, no fsmonitor, no remotes,
  `GIT_CEILING_DIRECTORIES` set, and system/global config ignored.

## Effects and approvals

| Effect | Status |
|---|---|
| Private workspace edits, private commits and refs | Automatic under the `sandbox` permission profile. The original repository is never touched. |
| `EXPORT` | Automatic. Writes only to a new directory the operator names, outside the source and run storage, atomically and with no overwrite. |
| `CLEANUP_RUN` | Requires an approval grant bound to the exact plan hash. |
| `APPLY_LOCAL`, `PUSH_NEW_BRANCH`, merge, pull request, issue comment | **Disabled** (`CAPABILITY_DISABLED`). `publication_authorized` is always `false`. |

Capability requests and grants:

- A grant is created only by a `KernelPrincipal`. It can be minted only through the
  interactive or headless kernel entry points, and a forged principal raises
  `PRINCIPAL_FORGED`.
- A grant is bound to the SHA-256 of the exact request (operation, target, base,
  artifact, run, policy, expiry) and has `max_uses = 1`.
- It is consumed in the same database transaction that records the effect intent.
- If the target changes after approval, the binding is invalidated
  (`APPROVAL_BINDING_CHANGED`).

## Plugins

Only plugins listed in the reviewed lock `config/plugins/builtin_release_v1.lock.json`
load, and each is checked before import:

- the lock's own hash;
- each module's content SHA-256;
- the interface major version;
- the configuration schema;
- dependency DAG and cycles;
- that the requested capabilities are a subset of the release profile's;
- the plugin's self-checks.

Plugins run **in process and are reviewed built-ins, not sandboxed code**. The lock
is what makes them trusted. Every proposal from a replacement controller passes
through the same `KernelGateway` policy, budget, and approval checks as the built-in
one. A lock file supplied from inside a target repository is rejected.

## Cleanup and retention

`harness clean RUN_ID` plans deletion only of **registered** resources of that one
run: temp containers, verification and task worktrees, caches. Run artifacts, the run
database, private refs, and raw logs are kept by `retain_default_v1`. The original
source, export bundles, the shared dependency cache, and other runs are never
eligible. Each run goes through plan, approval, tombstone, delete, and receipt; if the
resources change after approval, the plan is invalidated. `make clean` removes only
the harness's own build and test caches.

## Reporting a vulnerability

Please open a private security advisory on the repository rather than a public issue.
