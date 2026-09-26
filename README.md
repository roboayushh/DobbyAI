# DobbyAI – AI Coding Harness · Phase 1

> **Terminal-first GitHub issue intake.** Fetches real issues, displays their
> contents, and saves a structured snapshot ready for a future agent to consume.
> Makes **zero model calls** and changes **no repository code**.

---

## Quick Start

```bash
# 1. Install dependencies
make setup

# 2. Launch interactive intake
make run

# 3. Run the test suite (no live API required)
make test
```

---

## Requirements

| Item | Detail |
|---|---|
| Python | 3.11 or later |
| OS | macOS, Linux (or WSL on Windows) |
| Network | HTTPS access to `api.github.com` |
| AI API key | **Not required** for Phase 1 |

---

## Authentication

| Mode | How |
|---|---|
| Anonymous | Works out-of-the-box for public repos |
| Authenticated | Set `GITHUB_TOKEN` in your environment |

```bash
export GITHUB_TOKEN=ghp_your_token_here
make run
```

Recommended token scopes: **Fine-grained token** with **Issues: Read** on the
target repository. `AI_API_KEY` is intentionally ignored – Phase 1 must start
when it is absent.

---

## Interactive Usage (`make run`)

```
Enter repository: octocat/hello-world
          – or –
Enter repository: https://github.com/octocat/hello-world
          – or –
Enter repository: https://github.com/octocat/hello-world/issues/42
```

The last form skips the issue list and opens that specific issue directly.

### Issue list actions

| Key | Action |
|---|---|
| `n` | Next page |
| `p` | Previous page |
| `s` | Select an issue by number |
| `f` | Change filters (state, labels, per-page) |
| `r` | Refresh (live fetch, bypass cache) |
| `q` | Quit |

After selecting an issue you will be offered the option to **save a snapshot**.

---

## Non-interactive / Automation

```bash
# List issues as JSON
.venv/bin/python -m harness.cli issues --repo octocat/hello-world \
    --state open --json

# Fetch a single issue as JSON
.venv/bin/python -m harness.cli issue \
    --url https://github.com/octocat/hello-world/issues/1 --json

# Clear cached pages (snapshots are preserved)
.venv/bin/python -m harness.cli cache clear
```

### JSON output envelope

```json
{
  "schema_version": "1.0",
  "status": "ok",
  "data": { ... },
  "error": null
}
```

`status` is `ok`, `partial`, or `error`. Progress and errors always go to
**stderr**; the JSON result goes to **stdout** only.

---

## Exit Codes

| Code | Meaning |
|---|---|
| 0 | Successful operation |
| 2 | Invalid input |
| 3 | Authentication or access failure |
| 4 | Temporary API or network failure |
| 130 | Cancelled (Ctrl-C) |

---

## Environment Variables

| Variable | Required | Purpose |
|---|---|---|
| `GITHUB_TOKEN` | No | Enables private repos and higher rate limits |
| `AI_API_KEY` | **Ignored** | Not used in Phase 1; must not be sent to GitHub |

---

## Data Directory

| Path | Contains |
|---|---|
| `data/harness.db` | SQLite cache: repos, pages (anonymous only) |
| `data/snapshots/` | Immutable issue snapshots (JSON, `chmod 600`) |

`make clean` removes build artefacts. **Saved snapshots in `data/` are always
preserved.**

---

## Architecture

```
harness/
├── cli.py          CLIController    – Typer commands, state machine, exit codes
├── validator.py    InputValidator   – Parse/reject repo and issue refs pre-HTTP
├── service.py      IssueIntakeService – Coordinates provider + cache + snapshots
├── provider.py     GitHubIssueProvider – GitHub REST reads, PR exclusion, pagination
├── transport.py    GitHubTransport  – Auth, TLS, timeouts, retry, error classification
├── store.py        IssueStore       – SQLite cache, immutable snapshots
├── renderer.py     Safe rendering   – ANSI/markup stripping, Rich display
├── recorder.py     EventRecorder    – Local telemetry (no credentials, no bodies)
├── models.py       Pydantic models  – Repository, IssueRecord, IssuePage, IssueSnapshot
└── config.py       Settings         – Env vars, resource limits
```

---

## Security

- All remote content is treated as **untrusted data**.
- Issue text cannot authorise tool actions.
- ANSI escape sequences and Rich markup are stripped before display.
- Only HTTPS requests to `api.github.com` are permitted.
- Pagination URLs are validated before use; credentials are never forwarded to
  other hosts.
- `GITHUB_TOKEN` is never logged, printed, or included in error messages.
- Anonymous responses may be cached; authenticated responses stay in process
  memory only.
- Snapshot files are written `chmod 600` (owner read/write only).

---

## Acceptance Criteria Coverage

| AC | Scenario | Where tested |
|---|---|---|
| AC01 | Fresh environment | `make setup && make run` |
| AC02 | Public repo anonymous | `test_provider.py::TestGitHubIssueProviderRepository` |
| AC03 | Input normalization | `test_validator.py` |
| AC04 | Mixed API page / all-PR page | `test_provider.py::TestGitHubIssueProviderListIssues` |
| AC05 | Filters and pages | `test_provider.py::test_filters_sent_to_api` |
| AC06 | Selected issue / null body | `test_provider.py::test_get_issue_detail` |
| AC07 | Snapshot correctness | `test_snapshot.py` |
| AC08 | Credential failures 401/403/404 | `test_transport.py` |
| AC09 | Limits and outages 429/5xx | `test_transport.py` |
| AC10 | Partial results / cache clear | `test_snapshot.py::test_cache_clear_preserves_snapshots` |
| AC11 | Untrusted content | `test_renderer.py` |
| AC12 | Output boundaries / oversized | `test_transport.py::test_oversized_response_rejected` |
| AC13 | Cancellation Ctrl-C → 130 | `harness/cli.py` (KeyboardInterrupt handler) |
| AC14 | Privacy and redirects | `test_transport.py::test_absolute_url_wrong_host_rejected` |
| AC15 | Zero code side effects | `test_snapshot.py`, no checkout/branch/commit created |

---

## Phase 1 Scope

**Included:** Repository and issue intake, filters, pagination, safe display,
snapshot persistence, JSON output, error handling, test suite.

**Deferred to later phases:** Repository cloning, agent planning, code changes,
Git branches/commits, model calls, web UI, push/PR creation.

This phase ends when an issue is saved and marked *"ready for later
processing."* No agent is started.
