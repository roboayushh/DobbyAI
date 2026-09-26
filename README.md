# AI Coding Harness — Phase 1: GitHub Issue Intake

> **Phase 1 milestone** — Terminal-first intake module. Accepts a GitHub repository or issue URL, fetches real issues, displays their contents, and saves a structured snapshot. Makes **zero model calls** and modifies **zero repository code**.

---

## Requirements

| Item | Version |
|------|---------|
| Python | 3.12+ |
| OS | macOS / Linux |
| Network | HTTPS to `api.github.com` |

## Quick Start

```bash
# 1. Clone this repository
git clone https://github.com/your-org/ai-coding-harness.git
cd ai-coding-harness

# 2. Install dependencies (creates .venv)
make setup

# 3. Launch the interactive intake
make run
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GITHUB_TOKEN` | No | Fine-grained PAT with **Issues: read** scope. Enables private repos and higher rate limits. |
| `AI_API_KEY` | **Never** | Phase 1 never reads or sends this. It is intentionally ignored. |

### Recommended token permissions

```
Repository access: Selected repositories
Permissions:
  Issues: Read-only
```

> **Never commit your token.** Use `.env` (git-ignored) or export it in your shell.

## Usage

### Interactive mode

```bash
make run
# Prompts: Enter repository (owner/repo or GitHub URL)
```

Accepted inputs:
- `octocat/hello-world`
- `https://github.com/octocat/hello-world`
- `https://github.com/octocat/hello-world.git`
- `https://github.com/octocat/hello-world/issues/42` — skips list, opens issue directly

### Non-interactive / JSON mode

```bash
# List issues
harness issues --repo octocat/hello-world --state open --json

# Single issue
harness issue --url https://github.com/octocat/hello-world/issues/42 --json

# Clear page cache (snapshots are preserved)
harness cache clear
```

JSON output goes to **stdout**; progress and errors go to **stderr**.

### JSON output envelope

```json
{
  "schema_version": "1.0",
  "status": "ok",
  "data": { ... },
  "error": null
}
```

`status` values: `ok`, `partial`, `error`.

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Successful operation |
| 2 | Invalid input |
| 3 | Authentication or access failure |
| 4 | Temporary API or network failure |
| 130 | Cancelled (Ctrl-C) |

## Running Tests

```bash
make test
```

- Tests use **mocked GitHub responses** — no live network required.
- No `AI_API_KEY` needed.
- Coverage report printed to terminal.

```bash
# With custom pytest args
make test ARGS="-k test_validator -v"
```

## Project Structure

```
.
├── Makefile
├── pyproject.toml
├── requirements.txt
├── requirements-dev.txt
├── src/
│   └── harness/
│       ├── __init__.py
│       ├── cli.py          # CLIController (Typer)
│       ├── config.py       # HarnessConfig (pydantic-settings)
│       ├── models.py       # Pydantic data contracts
│       ├── provider.py     # GitHubIssueProvider
│       ├── recorder.py     # EventRecorder (local telemetry)
│       ├── renderer.py     # TerminalRenderer (Rich, safe)
│       ├── service.py      # IssueIntakeService
│       ├── store.py        # IssueStore (SQLite)
│       ├── transport.py    # GitHubTransport (HTTPX)
│       └── validator.py    # InputValidator
├── tests/
│   ├── fixtures.py
│   ├── test_models.py
│   ├── test_provider.py
│   ├── test_renderer.py
│   ├── test_service.py
│   ├── test_store.py
│   ├── test_transport.py
│   └── test_validator.py
└── data/               # Created at runtime (git-ignored)
    ├── harness.db
    └── snapshots/      # Immutable JSON snapshots (preserved by make clean)
```

## Data & Persistence

- **SQLite** (`data/harness.db`) — cached pages and snapshots. File permissions: `0o600`.
- **Snapshots** (`data/snapshots/*.json`) — immutable intake records. Preserved by `make clean`.
- Anonymous pages may be cached; authenticated responses stay in-process only.
- `make clean` removes `.venv`, build artifacts — **not** `data/snapshots/`.

## Security & Guardrails

- Only HTTPS requests to `api.github.com` — host allowlist enforced in transport.
- All user-supplied content treated as untrusted: ANSI sequences stripped, Rich markup escaped.
- `GITHUB_TOKEN` never logged, never included in exceptions.
- `AI_API_KEY` is never read.
- TLS verification always enabled.
- Response size capped at 5 MiB.
- Max 2 retries with jitter for 5xx/network errors.

## Acceptance Criteria Coverage

| ID | Scenario | Module |
|----|----------|--------|
| AC01 | Fresh environment setup | `Makefile` |
| AC02 | Anonymous public repository | `test_service.py` |
| AC03 | Input normalization | `test_validator.py` |
| AC04 | PR filtering / mixed page | `test_provider.py` |
| AC05 | Filters and pages | `test_provider.py`, `test_service.py` |
| AC06 | Selected issue fields | `test_provider.py` |
| AC07 | Snapshot correctness | `test_store.py`, `test_service.py` |
| AC08 | Credential failures | `test_provider.py`, `test_transport.py` |
| AC09 | Limits and outages | `test_transport.py` |
| AC10 | Partial results | `test_service.py` |
| AC11 | Untrusted content | `test_renderer.py` |
| AC12 | Output boundaries | `test_renderer.py`, `test_transport.py` |
| AC13 | Cancellation | `cli.py` (Ctrl-C → exit 130) |
| AC14 | Privacy and redirects | `test_transport.py` |
| AC15 | Zero code side effects | `test_service.py` |

## Relationship to Full Harness

Phase 1 covers issue context, retrieval, and recovery from fetch failures. Future phases add:
- Model interactions and planning
- Repository cloning, code indexing, and agent orchestration  
- Code changes, Git operations, and patch verification
- Execution sandboxes and test verification

`IssueProvider` is intentionally replaceable for future text/file input adapters.
