"""
harness/cli.py
──────────────
CLIController – Typer application.

Commands:
  harness run              Interactive intake prompt (make run)
  harness issues           Non-interactive issue list (JSON capable)
  harness issue            Non-interactive single issue (JSON capable)
  harness cache clear      Clear cached pages (not snapshots)

Exit codes (per PRD):
  0    Successful operation
  2    Invalid input
  3    Authentication / access failure
  4    Temporary API or network failure
  130  Cancelled (Ctrl-C)
"""
from __future__ import annotations

import json
import signal
import sys
import time
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.prompt import Prompt

from .config import get_config
from .models import (
    EnvelopeStatus,
    IssueFilters,
    IssueState,
    JSONEnvelope,
)
from .renderer import TerminalRenderer
from .service import IssueIntakeService
from .transport import (
    AccessError,
    AuthError,
    CancelledError,
    HarnessError,
    InputError,
    NetworkError,
    OversizedResponseError,
    RateLimitError,
)
from .validator import InputError as ValidatorInputError
from .validator import InputValidator

app = typer.Typer(
    name="harness",
    help="AI Coding Harness – Phase 1: GitHub Issue Intake",
    add_completion=False,
    no_args_is_help=True,
)

# stdout console (for JSON output); stderr console (for progress/errors)
_out = Console(file=sys.stdout, highlight=False, markup=False)
_err = Console(file=sys.stderr, highlight=False, markup=True)


# ── helpers ───────────────────────────────────────────────────────────────────

def _exit_for(exc: Exception) -> int:
    if isinstance(exc, HarnessError):
        return exc.exit_code
    if isinstance(exc, (ValidatorInputError, KeyboardInterrupt)):
        return 130 if isinstance(exc, KeyboardInterrupt) else 2
    return 1


def _emit_json(envelope: JSONEnvelope) -> None:
    """Write JSON envelope to stdout; never mix with progress messages."""
    _out.print(envelope.model_dump_json(indent=2), markup=False, highlight=False)


def _emit_error_json(message: str, status: EnvelopeStatus = EnvelopeStatus.error) -> None:
    _emit_json(JSONEnvelope(status=status, error=message))


def _handle_exc(exc: Exception, renderer: TerminalRenderer | None = None) -> int:
    msg = str(exc)
    if renderer:
        renderer.render_error(msg)
    else:
        _err.print(f"[bold red]✗ Error:[/bold red] {msg}", markup=True)
    return _exit_for(exc)


# ── run command (interactive) ─────────────────────────────────────────────────

@app.command("run")
def cmd_run() -> None:
    """Launch the interactive GitHub issue intake prompt."""
    cfg = get_config()
    renderer = TerminalRenderer(Console(highlight=False, markup=True))
    service = IssueIntakeService(config=cfg)

    renderer.console.print(
        "\n[bold cyan]AI Coding Harness[/bold cyan] [dim]– Phase 1: Issue Intake[/dim]\n",
        markup=True,
    )
    renderer.render_auth_status(cfg.has_github_token)
    renderer.console.print()

    # ── Step 1: accept repository input ────────────────────────────────────
    repo_ref = None
    issue_ref = None
    while repo_ref is None:
        raw = Prompt.ask("[bold]Enter repository[/bold] (owner/repo or GitHub URL)")
        if raw.lower() in ("q", "quit", "exit"):
            renderer.render_info("Goodbye.")
            raise typer.Exit(0)
        try:
            # Check if it's a direct issue URL first
            issue_ref = InputValidator.parse_issue_ref(raw)
            repo_ref = InputValidator.parse_repo_ref(raw)
        except (ValidatorInputError, InputError) as exc:
            renderer.render_error(str(exc))
            continue

    owner, repo_name = repo_ref.owner, repo_ref.name

    # ── Step 2: fetch repository metadata ──────────────────────────────────
    renderer.render_info(f"Fetching repository {owner}/{repo_name} …")
    try:
        repository = service.fetch_repository(owner, repo_name)
    except (AuthError, AccessError) as exc:
        renderer.render_error(str(exc))
        service.close()
        raise typer.Exit(_exit_for(exc))
    except (NetworkError, OversizedResponseError) as exc:
        renderer.render_error(str(exc))
        service.close()
        raise typer.Exit(_exit_for(exc))
    except KeyboardInterrupt:
        renderer.render_info("\nCancelled.")
        service.close()
        raise typer.Exit(130)

    renderer.render_repository(repository)

    # ── Step 3: if direct issue URL, skip list ──────────────────────────────
    if issue_ref:
        renderer.render_info(f"Direct issue URL detected — fetching #{issue_ref.number} …")
        _interactive_show_issue(
            service, renderer, owner, repo_name, repository, issue_ref.number
        )
        service.close()
        return

    # ── Step 4: issue list loop ─────────────────────────────────────────────
    filters = IssueFilters(per_page=cfg.default_page_size)
    cursor: str | None = None
    current_page = None

    while True:
        renderer.render_info("Fetching issues …")
        try:
            current_page = service.browse(owner, repo_name, repository, filters, cursor)
        except KeyboardInterrupt:
            renderer.render_info("\nCancelled during fetch.")
            service.close()
            raise typer.Exit(130)

        renderer.render_issue_list(current_page)

        # Build menu
        choices = []
        if current_page.issues:
            choices.append("Select issue number to view detail")
        if current_page.has_more:
            choices.append("[n] Next page")
        choices.extend([
            "[f] Change filters (state/labels)",
            "[r] Refresh",
            "[q] Quit",
        ])

        for c in choices:
            renderer.console.print(f"  {c}", markup=False)
        renderer.console.print()

        try:
            cmd = Prompt.ask("Command").strip().lower()
        except KeyboardInterrupt:
            renderer.render_info("\nCancelled.")
            service.close()
            raise typer.Exit(130)

        if cmd in ("q", "quit", "exit"):
            renderer.render_info("Goodbye.")
            service.close()
            return

        if cmd == "n":
            if current_page.has_more:
                cursor = current_page.next_cursor
                filters = IssueFilters(
                    state=filters.state,
                    labels=filters.labels,
                    page=filters.page + 1,
                    per_page=filters.per_page,
                )
            else:
                renderer.render_warning("No more pages.")
            continue

        if cmd == "r":
            cursor = None
            filters = filters.reset_page()
            current_page = service.refresh(owner, repo_name, repository, filters)
            renderer.render_issue_list(current_page)
            continue

        if cmd == "f":
            filters, cursor = _prompt_filters(renderer, cfg.default_page_size)
            continue

        # Treat numeric input as issue selection
        if cmd.isdigit():
            number = int(cmd)
            _interactive_show_issue(
                service, renderer, owner, repo_name, repository, number
            )
        else:
            renderer.render_warning(f"Unknown command: {cmd!r}")

    service.close()


def _prompt_filters(renderer: TerminalRenderer, default_per_page: int) -> tuple[IssueFilters, None]:
    state_raw = Prompt.ask(
        "State", choices=["open", "closed", "all"], default="open"
    )
    labels_raw = Prompt.ask("Labels (comma-separated, blank for none)", default="")
    labels = [l.strip() for l in labels_raw.split(",") if l.strip()]
    size_raw = Prompt.ask(f"Per page (1-100)", default=str(default_per_page))
    try:
        per_page = max(1, min(100, int(size_raw)))
    except ValueError:
        per_page = default_per_page
    filters = IssueFilters(
        state=IssueState(state_raw),
        labels=labels,
        per_page=per_page,
    )
    return filters, None  # reset cursor


def _interactive_show_issue(
    service: IssueIntakeService,
    renderer: TerminalRenderer,
    owner: str,
    repo_name: str,
    repository,
    number: int,
) -> None:
    renderer.render_info(f"Fetching issue #{number} …")
    try:
        issue, snapshot = service.select_issue(owner, repo_name, repository, number)
    except (AuthError, AccessError) as exc:
        renderer.render_error(str(exc))
        return
    except (NetworkError, OversizedResponseError) as exc:
        renderer.render_error(str(exc))
        return
    except KeyboardInterrupt:
        renderer.render_info("\nCancelled.")
        return

    renderer.render_issue_detail(issue, source=snapshot.source.value)

    # Offer JSON output
    try:
        save = Prompt.ask(
            "\nSave snapshot? [Y/n]", default="y"
        ).strip().lower()
    except KeyboardInterrupt:
        renderer.render_info("\nCancelled.")
        return

    if save in ("y", "yes", ""):
        snap_path = service._store.save_snapshot(snapshot)
        renderer.render_snapshot_saved(str(snap_path))
    else:
        renderer.render_info("Snapshot not saved.")


# ── issues command (non-interactive) ─────────────────────────────────────────

@app.command("issues")
def cmd_issues(
    repo: Annotated[str, typer.Option("--repo", help="owner/repo or GitHub URL")] = "",
    state: Annotated[str, typer.Option("--state", help="open|closed|all")] = "open",
    labels: Annotated[str, typer.Option("--labels", help="Comma-separated label names")] = "",
    page: Annotated[int, typer.Option("--page", help="Page number")] = 1,
    per_page: Annotated[int, typer.Option("--per-page", help="Records per page (max 100)")] = 30,
    json_mode: Annotated[bool, typer.Option("--json", help="Output JSON envelope to stdout")] = False,
) -> None:
    """Fetch a page of issues (non-interactive)."""
    cfg = get_config()
    renderer = TerminalRenderer(Console(file=sys.stderr, highlight=False, markup=True))

    try:
        repo_ref = InputValidator.parse_repo_ref(repo)
    except (ValidatorInputError, InputError) as exc:
        if json_mode:
            _emit_error_json(str(exc))
        else:
            renderer.render_error(str(exc))
        raise typer.Exit(2)

    label_list = [l.strip() for l in labels.split(",") if l.strip()]
    per_page = max(1, min(100, per_page))
    filters = IssueFilters(
        state=IssueState(state),
        labels=label_list,
        page=page,
        per_page=per_page,
    )

    service = IssueIntakeService(config=cfg)
    try:
        repository = service.fetch_repository(repo_ref.owner, repo_ref.name)
        page_result = service.browse(repo_ref.owner, repo_ref.name, repository, filters)
    except KeyboardInterrupt:
        if json_mode:
            _emit_error_json("Cancelled by user.", EnvelopeStatus.error)
        raise typer.Exit(130)
    except HarnessError as exc:
        if json_mode:
            _emit_error_json(str(exc))
        else:
            renderer.render_error(str(exc))
        raise typer.Exit(_exit_for(exc))
    finally:
        service.close()

    if json_mode:
        status = EnvelopeStatus.partial if page_result.fetch_error else EnvelopeStatus.ok
        envelope = JSONEnvelope(
            status=status,
            data=page_result.model_dump(mode="json"),
            error=page_result.fetch_error,
        )
        _emit_json(envelope)
    else:
        renderer.render_repository(repository)
        renderer.render_issue_list(page_result)


# ── issue command (non-interactive) ──────────────────────────────────────────

@app.command("issue")
def cmd_issue(
    url: Annotated[str, typer.Option("--url", help="https://github.com/owner/repo/issues/N")] = "",
    repo: Annotated[str, typer.Option("--repo", help="owner/repo")] = "",
    number: Annotated[int, typer.Option("--number", help="Issue number")] = 0,
    json_mode: Annotated[bool, typer.Option("--json", help="Output JSON envelope to stdout")] = False,
    save: Annotated[bool, typer.Option("--save-private", help="Save snapshot even for private repos")] = True,
) -> None:
    """Fetch a single issue's details (non-interactive)."""
    cfg = get_config()
    renderer = TerminalRenderer(Console(file=sys.stderr, highlight=False, markup=True))

    try:
        if url:
            issue_ref = InputValidator.parse_issue_ref(url)
            if issue_ref is None:
                raise InputError(f"Not a valid issue URL: {url!r}")
            owner, repo_name, issue_number = issue_ref.owner, issue_ref.repo, issue_ref.number
        elif repo and number > 0:
            repo_ref = InputValidator.parse_repo_ref(repo)
            owner, repo_name, issue_number = repo_ref.owner, repo_ref.name, number
        else:
            raise InputError(
                "Provide --url https://github.com/owner/repo/issues/N  "
                "or --repo owner/repo --number N"
            )
    except (ValidatorInputError, InputError) as exc:
        if json_mode:
            _emit_error_json(str(exc))
        else:
            renderer.render_error(str(exc))
        raise typer.Exit(2)

    service = IssueIntakeService(config=cfg)
    try:
        repository = service.fetch_repository(owner, repo_name)
        issue, snapshot = service.select_issue(owner, repo_name, repository, issue_number)
    except KeyboardInterrupt:
        if json_mode:
            _emit_error_json("Cancelled by user.")
        raise typer.Exit(130)
    except HarnessError as exc:
        if json_mode:
            _emit_error_json(str(exc))
        else:
            renderer.render_error(str(exc))
        raise typer.Exit(_exit_for(exc))
    finally:
        service.close()

    if json_mode:
        envelope = JSONEnvelope(
            status=EnvelopeStatus.ok,
            data=snapshot.model_dump(mode="json"),
        )
        _emit_json(envelope)
        snap_path = service._store.snapshots_dir / f"{snapshot.snapshot_id}.json"
        _err.print(
            f"[green]✓ Snapshot saved:[/green] {snap_path}", markup=True
        )
    else:
        renderer.render_issue_detail(issue, source=snapshot.source.value)
        snap_path = service._store.snapshots_dir / f"{snapshot.snapshot_id}.json"
        renderer.render_snapshot_saved(str(snap_path))


# ── cache command ─────────────────────────────────────────────────────────────

cache_app = typer.Typer(help="Cache management")
app.add_typer(cache_app, name="cache")


@cache_app.command("clear")
def cmd_cache_clear() -> None:
    """Delete cached page data (snapshots are preserved)."""
    cfg = get_config()
    service = IssueIntakeService(config=cfg)
    try:
        n = service.clear_cache()
        Console(file=sys.stderr).print(
            f"[green]✓ Cache cleared:[/green] {n} page(s) removed. "
            "Saved snapshots are preserved.",
            markup=True,
        )
    finally:
        service.close()
    raise typer.Exit(0)


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app()
