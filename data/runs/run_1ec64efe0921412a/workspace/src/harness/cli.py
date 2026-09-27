"""harness/cli.py
CLIController – Typer application.

Commands:
  harness prepare --request request.json --json
  harness run [--request request.json] [--json]
  harness status RUN_ID [--json]
  harness inspect RUN_ID
  harness intake list --repo OWNER/REPO [--json]
  harness intake show SNAPSHOT_ID [--json]
  harness issues ...
  harness issue ...
  harness cache clear
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from typing import Annotated, Any, Dict, Optional, Tuple

import typer
from pydantic import ValidationError
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from .application.preparation_controller import PreparationController, PreparationError
from .cli_renderer import ResultRenderer, sanitize_terminal_text
from .config import HarnessConfig, get_config
from .contracts import (
    ErrorDetailsV1,
    ErrorResultV1,
    ExecutionMode,
    LimitsV1,
    PreparedRunResultV1,
    RepositoryKind,
    RepositoryQueryV1,
    RepositoryRefV1,
    RunRequestV1,
    RunState,
    TaskInputV1,
    TaskMode,
)
from .intake import ExistingIssueIntakeAdapter, TaskPreparationService
from .models import (
    EnvelopeStatus,
    IssueFilters,
    IssueRecord,
    IssueSnapshot,
    JSONEnvelope,
    Repository,
)
from .persistence import ArtifactStore, RunStore
from .renderer import TerminalRenderer, _safe
from .repository import (
    GitCommandError,
    LimitsExceededError,
    RepositoryService,
    SourcePolicyError,
)
from .service import IssueIntakeService
from .transport import (
    AccessError,
    AuthError,
    HarnessError,
    NetworkError,
    OversizedResponseError,
)
from .validator import InputError as ValidatorInputError
from .validator import InputValidator
from .workspace import WorkspaceManager, WorkspacePolicyError

app = typer.Typer(
    name="harness",
    help="AI Coding Harness – Terminal-first AI coding harness",
    add_completion=False,
    no_args_is_help=True,
)

intake_app = typer.Typer(help="Issue intake operations")
app.add_typer(intake_app, name="intake")

cache_app = typer.Typer(help="Cache management")
app.add_typer(cache_app, name="cache")

_err_console = Console(file=sys.stderr, highlight=False, markup=True)
_renderer = ResultRenderer(_err_console)


def _get_services(
    cfg: Optional[HarnessConfig] = None,
) -> Tuple[PreparationController, RunStore, ArtifactStore]:
    config = cfg or get_config()
    data_root = config.data_dir
    data_root.mkdir(parents=True, exist_ok=True)
    db_path = data_root / "harness.db"

    run_store = RunStore(str(db_path))
    artifact_store = ArtifactStore(str(data_root), run_store)
    workspace_mgr = WorkspaceManager(str(data_root))
    repo_service = RepositoryService()
    intake_adapter = ExistingIssueIntakeAdapter(config=config)
    task_prep = TaskPreparationService(intake_adapter)

    controller = PreparationController(
        run_store=run_store,
        artifact_store=artifact_store,
        workspace_manager=workspace_mgr,
        repository_service=repo_service,
        task_prep_service=task_prep,
        data_root=str(data_root),
    )
    return controller, run_store, artifact_store


def _map_exit_code_and_status(exc: Exception) -> Tuple[int, str, str]:
    """Map exception to (exit_code, status, code_name)."""
    if isinstance(exc, (ValidationError, ValueError, json.JSONDecodeError)):
        return 2, "BLOCKED", "INVALID_REQUEST"

    code = getattr(exc, "code", "")
    if "IDEMPOTENCY" in code or "INVALID" in code:
        return 2, "FAILED", code or "INVALID_REQUEST"
    if "POLICY" in code or "LIMITS" in code or isinstance(exc, (SourcePolicyError, LimitsExceededError, WorkspacePolicyError)):
        return 3, "BLOCKED", code or "POLICY_BLOCKED"
    if "ACQUISITION" in code or "REVISION" in code or isinstance(exc, GitCommandError):
        return 4, "FAILED", code or "REPOSITORY_ACQUISITION_FAILED"
    if "INTEGRITY" in code:
        return 5, "FAILED", code or "INTEGRITY_FAILURE"
    if isinstance(exc, KeyboardInterrupt):
        return 130, "CANCELLED", "CANCELLED_BY_USER"
    return 10, "FAILED", code or "INTERNAL_FAILURE"


def _handle_cli_error(exc: Exception, json_mode: bool, run_id: Optional[str] = None) -> None:
    exit_code, status, code_name = _map_exit_code_and_status(exc)
    msg = str(exc)

    if json_mode:
        err_obj = ErrorResultV1(
            schema_version="1.0",
            status=status,  # type: ignore[arg-type]
            run_id=run_id,
            error=ErrorDetailsV1(
                code=code_name,
                message=msg,
                retryable=getattr(exc, "retryable", False),
                details=getattr(exc, "details", {}),
            ),
        )
        _renderer.render_json(err_obj)
    else:
        _err_console.print(f"[bold red]✗ [{code_name}] Error:[/bold red] {msg}")

    sys.exit(exit_code)


# ── prepare command ───────────────────────────────────────────────────────────

@app.command("prepare")
def cmd_prepare(
    request: Annotated[
        str,
        typer.Option("--request", "-r", help="Path to request JSON file"),
    ],
    json_mode: Annotated[
        bool,
        typer.Option("--json", help="Emit single schema-valid JSON object to stdout"),
    ] = False,
) -> None:
    """Prepare a private Git workspace and immutable tasks from request specification."""
    req_path = Path(request)
    if not req_path.is_file():
        _handle_cli_error(ValueError(f"Request file not found: {request}"), json_mode)

    try:
        raw_text = req_path.read_text(encoding="utf-8")
        parsed_dict = json.loads(raw_text)
        run_request = RunRequestV1.model_validate(parsed_dict)
    except Exception as exc:
        _handle_cli_error(exc, json_mode)

    controller, _, _ = _get_services()
    try:
        # Reconcile any prior interrupted runs
        controller.reconcile_interrupted()
        result = controller.prepare(run_request)
    except Exception as exc:
        _handle_cli_error(exc, json_mode)

    if json_mode:
        _renderer.render_json(result)
    else:
        _renderer.render_terminal(result)
    sys.exit(0)


# ── run command ───────────────────────────────────────────────────────────────

@app.command("run")
def cmd_run(
    target: Annotated[
        Optional[str],
        typer.Argument(help="GitHub repo (owner/repo or URL), issue URL, or local path"),
    ] = None,
    prompt: Annotated[
        Optional[str],
        typer.Option("--prompt", "-p", help="Task prompt or issue description"),
    ] = None,
    mode: Annotated[
        Optional[str],
        typer.Option("--mode", "-m", help="Task mode: single_issue or repository"),
    ] = None,
    request: Annotated[
        Optional[str],
        typer.Option("--request", "-r", help="Path to request JSON file (headless)"),
    ] = None,
    json_mode: Annotated[
        bool,
        typer.Option("--json", help="Emit single schema-valid JSON object to stdout"),
    ] = False,
) -> None:
    """Launch preparation run (automated, fast, minimal prompts)."""
    if request:
        cmd_prepare(request=request, json_mode=json_mode)
        return

    _err_console.print("\n[bold cyan]AI Coding Harness[/bold cyan] [dim]– Automated Preparation[/dim]\n")

    # 1. Mode prompt: single issue vs whole repo
    task_mode: Optional[TaskMode] = None
    if mode:
        task_mode = TaskMode(mode)
    elif prompt:
        task_mode = TaskMode.SINGLE_ISSUE

    if not target and not task_mode:
        _err_console.print("[bold]Select Mode:[/bold]")
        _err_console.print("   1) Single issue / task (default)")
        _err_console.print("   2) Whole repository")
        mode_choice = Prompt.ask("Choose mode", choices=["1", "2"], default="1")
        task_mode = TaskMode.SINGLE_ISSUE if mode_choice == "1" else TaskMode.REPOSITORY

    # 2. Target repository or issue URL
    if not target:
        target = Prompt.ask("\n[bold]Enter GitHub repo link (or issue URL, or local path)[/bold]", default=".").strip()

    # Automatically detect if target is a GitHub issue URL
    issue_ref = InputValidator.parse_issue_ref(target)
    if issue_ref:
        task_mode = TaskMode.SINGLE_ISSUE
        repo_locator = f"https://github.com/{issue_ref.owner}/{issue_ref.repo}.git"
        repo_kind = RepositoryKind.PUBLIC_HTTPS
        task_input = TaskInputV1(issue_url=target)
        _err_console.print(f"  [green]✓[/green] Detected issue #{issue_ref.number} on [cyan]{issue_ref.owner}/{issue_ref.repo}[/cyan]")
    else:
        if not task_mode:
            _err_console.print("[bold]Select Mode:[/bold]")
            _err_console.print("   1) Single issue / task (default)")
            _err_console.print("   2) Whole repository")
            mode_choice = Prompt.ask("Choose mode", choices=["1", "2"], default="1")
            task_mode = TaskMode.SINGLE_ISSUE if mode_choice == "1" else TaskMode.REPOSITORY

        # Determine repository locator and kind
        if target.startswith("http://") or target.startswith("https://"):
            repo_locator = target if target.endswith(".git") else f"{target.rstrip('/')}.git"
            repo_kind = RepositoryKind.PUBLIC_HTTPS
        elif "/" in target and not Path(target).exists():
            parts = target.split("/")
            if len(parts) == 2:
                repo_locator = f"https://github.com/{parts[0]}/{parts[1]}.git"
                repo_kind = RepositoryKind.PUBLIC_HTTPS
            else:
                repo_locator = str(Path(target).resolve())
                repo_kind = RepositoryKind.LOCAL_GIT
        else:
            repo_locator = str(Path(target).resolve())
            repo_kind = RepositoryKind.LOCAL_GIT

        # 3. User prompt
        if task_mode == TaskMode.SINGLE_ISSUE:
            if not prompt:
                prompt = Prompt.ask("\n[bold]Enter user prompt (or issue #)[/bold]").strip()
            if prompt.lstrip("#").isdigit() and repo_kind == RepositoryKind.PUBLIC_HTTPS:
                clean_base = repo_locator.removesuffix(".git")
                task_input = TaskInputV1(issue_url=f"{clean_base}/issues/{prompt.lstrip('#')}")
            else:
                task_input = TaskInputV1(text=prompt)
        else:
            task_input = TaskInputV1(repository_query=RepositoryQueryV1(state="open"))

    idempotency_key = f"run-{uuid.uuid4().hex[:12]}"
    run_req = RunRequestV1(
        schema_version="1.0",
        idempotency_key=idempotency_key,
        task_mode=task_mode,
        execution_mode=ExecutionMode.DEVELOPMENT,
        repository=RepositoryRefV1(
            kind=repo_kind,
            locator=repo_locator,
            revision=None,
        ),
        task=task_input,
        limits=LimitsV1(max_tasks=1 if task_mode == TaskMode.SINGLE_ISSUE else 3),
    )

    controller, _, _ = _get_services()
    try:
        controller.reconcile_interrupted()
        result = controller.prepare(run_req)
    except Exception as exc:
        _handle_cli_error(exc, json_mode=json_mode)

    if json_mode:
        _renderer.render_json(result)
    else:
        _renderer.render_terminal(result)
        _err_console.print("\n[dim]Run [bold]harness status[/bold] or [bold]harness inspect[/bold] anytime to review.[/dim]\n")
    sys.exit(0)


# ── status command ────────────────────────────────────────────────────────────

@app.command("status")
def cmd_status(
    run_id: Annotated[
        Optional[str],
        typer.Argument(help="Run ID to inspect (optional; defaults to most recent run)"),
    ] = None,
    json_mode: Annotated[
        bool,
        typer.Option("--json", help="Emit single schema-valid JSON object to stdout"),
    ] = False,
) -> None:
    """Display the current state and summary of a run (defaults to latest run)."""
    _, run_store, artifact_store = _get_services()
    if not run_id:
        latest = run_store.get_latest_run()
        if not latest:
            _handle_cli_error(ValueError("No runs found in harness database."), json_mode)
        run = latest
        run_id = run["run_id"]
    else:
        run = run_store.get_run(run_id)
        if not run:
            _handle_cli_error(ValueError(f"Run not found: {run_id}"), json_mode, run_id=run_id)

    if run["state"] == RunState.PREPARED.value:
        try:
            res_bytes = artifact_store.open_readonly(run_id, "result.json")
            if json_mode:
                print(res_bytes.decode("utf-8"), file=sys.stdout)
            else:
                res_obj = PreparedRunResultV1.model_validate_json(res_bytes)
                _renderer.render_terminal(res_obj)
            sys.exit(0)
        except Exception:
            pass

    status_data = {
        "schema_version": "1.0",
        "run_id": run["run_id"],
        "status": run["state"],
        "task_mode": run["task_mode"],
        "execution_mode": run["execution_mode"],
        "error_code": run.get("error_code"),
        "created_at": run["created_at"],
        "updated_at": run["updated_at"],
    }
    if json_mode:
        _renderer.render_json(status_data)
    else:
        _err_console.print(f"Run ID: [bold]{run_id}[/bold]")
        _err_console.print(f"Status: [bold green]{run['state']}[/bold green]")
        if run.get("error_code"):
            _err_console.print(f"Error Code: [red]{run['error_code']}[/red]")
    sys.exit(0)


# ── inspect command ───────────────────────────────────────────────────────────

@app.command("inspect")
def cmd_inspect(
    run_id: Annotated[
        Optional[str],
        typer.Argument(help="Run ID to inspect (optional; defaults to most recent run)"),
    ] = None,
) -> None:
    """Show detailed state, source identity, tasks, artifacts, and events (defaults to latest run)."""
    _, run_store, artifact_store = _get_services()
    if not run_id:
        latest = run_store.get_latest_run()
        if not latest:
            _err_console.print("[bold red]No runs found in harness database.[/bold red]")
            sys.exit(2)
        run = latest
        run_id = run["run_id"]
    else:
        run = run_store.get_run(run_id)
        if not run:
            _err_console.print(f"[bold red]Run not found: {run_id}[/bold red]")
            sys.exit(2)

    source = run_store.get_source_snapshot(run_id)
    workspace = run_store.get_workspace(run_id)
    tasks = run_store.get_tasks(run_id)
    artifacts = run_store.get_connection().execute(
        "SELECT * FROM h_artifacts WHERE run_id = ?", (run_id,)
    ).fetchall()
    events = run_store.get_events(run_id)

    _renderer.render_inspect(
        run=run,
        source=source,
        workspace=workspace,
        tasks=tasks,
        artifacts=[dict(a) for a in artifacts],
        events=events,
    )
    sys.exit(0)


# ── intake commands ───────────────────────────────────────────────────────────

@intake_app.command("list")
def cmd_intake_list(
    repo: Annotated[
        str,
        typer.Option("--repo", "-r", help="Repository in owner/repo format"),
    ],
    state: Annotated[str, typer.Option("--state", help="Issue state: open, closed, all")] = "open",
    json_mode: Annotated[
        bool,
        typer.Option("--json", help="Output as JSON"),
    ] = False,
) -> None:
    """List candidate issues for a repository."""
    cfg = get_config()
    adapter = ExistingIssueIntakeAdapter(config=cfg)
    parts = repo.split("/")
    if len(parts) != 2:
        _handle_cli_error(ValueError("Repository must be in owner/repo format"), json_mode)

    owner, repo_name = parts[0], parts[1]
    try:
        candidates, _ = adapter.list_candidates(owner=owner, repo=repo_name, state=state)
    except Exception as exc:
        _handle_cli_error(exc, json_mode)

    if json_mode:
        data = [
            {
                "number": c.number,
                "title": c.title,
                "state": c.state,
                "labels": [lbl.name if hasattr(lbl, "name") else str(lbl) for lbl in c.labels],
            }
            for c in candidates
        ]
        _renderer.render_json(data)
    else:
        _err_console.print(f"\n[bold]Issues for {repo}:[/bold]")
        for c in candidates:
            _err_console.print(f"  #{c.number}: {sanitize_terminal_text(c.title)} ({c.state})")
    sys.exit(0)


@intake_app.command("show")
def cmd_intake_show(
    snapshot_id: Annotated[
        Optional[str],
        typer.Argument(help="Snapshot ID to show (defaults to latest snapshot)"),
    ] = None,
    json_mode: Annotated[
        bool,
        typer.Option("--json", help="Output as JSON"),
    ] = False,
) -> None:
    """Show details of a saved issue snapshot (defaults to latest snapshot)."""
    cfg = get_config()
    adapter = ExistingIssueIntakeAdapter(config=cfg)
    if not snapshot_id:
        snap_files = sorted(cfg.snapshots_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not snap_files:
            _err_console.print("[bold red]No saved snapshots found.[/bold red]")
            sys.exit(2)
        snapshot_id = snap_files[0].stem

    snapshot = adapter.get_snapshot(snapshot_id)
    if not snapshot:
        _handle_cli_error(ValueError(f"Snapshot not found: {snapshot_id}"), json_mode)

    if json_mode:
        _renderer.render_json(snapshot.model_dump(mode="json"))
    else:
        _err_console.print(f"\n[bold]Snapshot {snapshot_id}:[/bold]")
        _err_console.print(f"Repository: {snapshot.repository.full_name}")
        _err_console.print(f"Issue #{snapshot.issue.number}: {sanitize_terminal_text(snapshot.issue.title)}")
        _err_console.print(f"Content Hash: {snapshot.content_hash}")
    sys.exit(0)


# ── Backwards-compatible commands (issues, issue, cache clear) ─────────────────

@app.command("issues")
def cmd_issues(
    repo: Annotated[
        Optional[str],
        typer.Option("--repo", "-r", help="Repository in owner/name format"),
    ] = None,
    url: Annotated[
        Optional[str],
        typer.Option("--url", "-u", help="Full GitHub repository URL"),
    ] = None,
    state: Annotated[
        str,
        typer.Option("--state", "-s", help="Issue state: open, closed, all"),
    ] = "open",
    labels: Annotated[
        Optional[list[str]],
        typer.Option("--label", "-l", help="Filter by label (repeatable)"),
    ] = None,
    page: Annotated[
        int,
        typer.Option("--page", "-p", min=1, help="Page number (1-based)"),
    ] = 1,
    per_page: Annotated[
        int,
        typer.Option("--per-page", min=1, max=100, help="Results per page"),
    ] = 25,
    cursor: Annotated[
        Optional[str],
        typer.Option("--cursor", help="Opaque pagination cursor from previous page"),
    ] = None,
    no_cache: Annotated[
        bool,
        typer.Option("--no-cache", help="Bypass cached pages; force fresh fetch"),
    ] = False,
    json_mode: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON envelope to stdout"),
    ] = False,
) -> None:
    """Non-interactive issue listing with filter, pagination, and JSON support."""
    cfg = get_config()
    renderer = TerminalRenderer(Console(file=sys.stderr, highlight=False, markup=True))
    try:
        if url:
            repo_ref = InputValidator.parse_repo_ref(url)
        elif repo:
            repo_ref = InputValidator.parse_repo_ref(repo)
        else:
            raise InputValidator("Provide either --repo owner/repo or --url https://github.com/owner/repo")
    except Exception as exc:
        if json_mode:
            _renderer.render_json(JSONEnvelope(status=EnvelopeStatus.error, error=str(exc)).model_dump(mode="json"))
        else:
            renderer.render_error(str(exc))
        sys.exit(2)

    service = IssueIntakeService(config=cfg)
    try:
        repository = service.fetch_repository(repo_ref.owner, repo_ref.name)
        filters = IssueFilters(
            state=state,  # type: ignore[arg-type]
            labels=labels or [],
            page=page,
            per_page=per_page,
        )
        issue_page = service.browse(
            owner=repo_ref.owner,
            repo=repo_ref.name,
            repository=repository,
            filters=filters,
            cursor=cursor,
            use_cache=not no_cache,
        )
    except Exception as exc:
        if json_mode:
            _renderer.render_json(JSONEnvelope(status=EnvelopeStatus.error, error=str(exc)).model_dump(mode="json"))
        else:
            renderer.render_error(str(exc))
        sys.exit(4)
    finally:
        service.close()

    if json_mode:
        _renderer.render_json(JSONEnvelope(status=EnvelopeStatus.ok, data=issue_page.model_dump(mode="json")).model_dump(mode="json"))
    else:
        renderer.render_issue_list(issue_page, page_num=page)
    sys.exit(0)


@app.command("issue")
def cmd_issue(
    url: Annotated[
        Optional[str],
        typer.Option("--url", "-u", help="Full GitHub issue URL"),
    ] = None,
    repo: Annotated[
        Optional[str],
        typer.Option("--repo", "-r", help="Repository in owner/name format"),
    ] = None,
    number: Annotated[
        Optional[int],
        typer.Option("--number", "-n", min=1, help="Issue number"),
    ] = None,
    json_mode: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON envelope to stdout"),
    ] = False,
) -> None:
    """Non-interactive single issue fetch and snapshot."""
    cfg = get_config()
    renderer = TerminalRenderer(Console(file=sys.stderr, highlight=False, markup=True))
    try:
        if url:
            parsed = InputValidator.parse_issue_ref(url)
            if not parsed:
                raise ValidatorInputError(f"URL is not a GitHub issue: {url}")
            owner, repo_name, issue_number = parsed.owner, parsed.name, parsed.number
        elif repo and number:
            repo_ref = InputValidator.parse_repo_ref(repo)
            owner, repo_name, issue_number = repo_ref.owner, repo_ref.name, number
        else:
            raise ValidatorInputError("Provide --url https://github.com/owner/repo/issues/N or --repo owner/repo --number N")
    except Exception as exc:
        if json_mode:
            _renderer.render_json(JSONEnvelope(status=EnvelopeStatus.error, error=str(exc)).model_dump(mode="json"))
        else:
            renderer.render_error(str(exc))
        sys.exit(2)

    service = IssueIntakeService(config=cfg)
    try:
        repository = service.fetch_repository(owner, repo_name)
        issue, snapshot = service.select_issue(owner, repo_name, repository, issue_number)
    except Exception as exc:
        if json_mode:
            _renderer.render_json(JSONEnvelope(status=EnvelopeStatus.error, error=str(exc)).model_dump(mode="json"))
        else:
            renderer.render_error(str(exc))
        sys.exit(4)
    finally:
        service.close()

    if json_mode:
        _renderer.render_json(JSONEnvelope(status=EnvelopeStatus.ok, data=snapshot.model_dump(mode="json")).model_dump(mode="json"))
    else:
        renderer.render_issue_detail(issue, source=snapshot.source.value)
    sys.exit(0)


@cache_app.command("clear")
def cmd_cache_clear(
    force: Annotated[
        bool,
        typer.Option("--force", "-f", "--yes", "-y", help="Skip confirmation prompt"),
    ] = False,
) -> None:
    """Delete cached page data (snapshots are preserved)."""
    if not force:
        confirm = Confirm.ask(
            "[bold yellow]⚠️  Are you sure you want to clear the intake cache?[/bold yellow]",
            default=False,
        )
        if not confirm:
            Console(file=sys.stderr).print("[dim]Operation cancelled.[/dim]")
            sys.exit(0)
    cfg = get_config()
    service = IssueIntakeService(config=cfg)
    try:
        n = service.clear_cache()
        Console(file=sys.stderr).print(f"[green]✓ Cache cleared:[/green] {n} page(s) removed.")
    finally:
        service.close()
    sys.exit(0)


@app.command("clean")
def cmd_clean(
    force: Annotated[
        bool,
        typer.Option("--force", "-f", "--yes", "-y", help="Skip confirmation prompt"),
    ] = False,
) -> None:
    """Clean all run workspaces, temporary clones, and quarantine data."""
    if not force:
        confirm = Confirm.ask(
            "[bold red]⚠️  Are you sure you want to clean all harness workspaces and temporary runs?[/bold red]",
            default=False,
        )
        if not confirm:
            _err_console.print("[dim]Operation cancelled.[/dim]")
            sys.exit(0)

    import shutil
    cfg = get_config()
    cleaned_items = 0
    if cfg.runs_dir.exists():
        for item in cfg.runs_dir.iterdir():
            try:
                for root, dirs, files in os.walk(item):
                    for d in dirs:
                        try:
                            os.chmod(os.path.join(root, d), 0o755)
                        except OSError:
                            pass
                    for f in files:
                        try:
                            os.chmod(os.path.join(root, f), 0o644)
                        except OSError:
                            pass
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink(missing_ok=True)
                cleaned_items += 1
            except Exception:
                pass
    if cfg.quarantine_dir.exists():
        for item in cfg.quarantine_dir.iterdir():
            try:
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink(missing_ok=True)
                cleaned_items += 1
            except Exception:
                pass
    if cfg.db_path.exists():
        try:
            cfg.db_path.unlink(missing_ok=True)
            cleaned_items += 1
        except Exception:
            pass
    _err_console.print(f"[bold green]✓ Cleaned {cleaned_items} workspace/temporary run items and database.[/bold green] [dim](data/snapshots/ preserved)[/dim]")
    sys.exit(0)


if __name__ == "__main__":
    app()
