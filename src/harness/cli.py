"""harness/cli.py
CLIController – Typer application.

Commands:
  harness prepare --request request.json --json
  harness run [--request request.json] [--json]
  harness continue RUN_ID --until plan|action-proposed|verification-required|complete --json
  harness resume RUN_ID / queue ... / verification ... / sandbox ... (see `harness --help`)
  harness plan RUN_ID --json
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
from .model import CredentialProvider, ModelProfileResolver
from .orchestration import BudgetLedgerService, OrchestrationController
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

context_app = typer.Typer(help="Inspect filtered model context packets")
app.add_typer(context_app, name="context")

evidence_app = typer.Typer(help="Inspect versioned repository evidence")
app.add_typer(evidence_app, name="evidence")

model_app = typer.Typer(help="Inspect designated model configuration")
app.add_typer(model_app, name="model")

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


def _get_orchestration_controller(
    cfg: Optional[HarnessConfig] = None,
) -> Tuple[OrchestrationController, RunStore, ArtifactStore]:
    config = cfg or get_config()
    _, run_store, artifact_store = _get_services(config)
    controller = OrchestrationController(
        run_store=run_store,
        artifact_store=artifact_store,
        data_root=config.data_dir,
        profile_resolver=ModelProfileResolver(config.model_profiles_path),
        profile_id=config.model_profile,
    )
    return controller, run_store, artifact_store


def _map_exit_code_and_status(exc: BaseException) -> Tuple[int, str, str]:
    """Map exception to (exit_code, status, code_name)."""
    if isinstance(exc, (ValidationError, json.JSONDecodeError)):
        return 2, "BLOCKED", "INVALID_REQUEST"

    code = getattr(exc, "code", "")
    if isinstance(exc, ValueError) and not code:
        return 2, "BLOCKED", "INVALID_REQUEST"
    if "INTEGRITY" in code or "PERSIST" in code or "SQLITE" in code:
        return 7, "FAILED", code or "INTEGRITY_FAILURE"
    if "BUDGET" in code:
        return 5, "BUDGET_EXHAUSTED", code or "MODEL_BUDGET_EXHAUSTED"
    if "NEEDS_INPUT" in code:
        return 6, "NEEDS_INPUT", code
    if (
        "POLICY" in code
        or "LIMITS" in code
        or "CONTEXT" in code
        or "CAPABILITY" in code
        or isinstance(exc, (SourcePolicyError, LimitsExceededError, WorkspacePolicyError))
    ):
        return 3, "BLOCKED", code or "POLICY_BLOCKED"
    if (
        "ACQUISITION" in code
        or "REVISION" in code
        or "ROLE_SCHEMA" in code
        or "PLANNER" in code
        or "CODER" in code
        or "ACTION_PROPOSAL" in code
        or isinstance(exc, GitCommandError)
    ):
        return 4, "FAILED", code or "REPOSITORY_ACQUISITION_FAILED"
    if (
        "IDEMPOTENCY" in code
        or "INVALID" in code
        or "MODEL_PROFILE" in code
        or "MODEL_AUTH" in code
        or "MODEL_QUOTA" in code
        or "MODEL_OR_ENDPOINT" in code
    ):
        return 2, "FAILED", code or "INVALID_REQUEST"
    if isinstance(exc, KeyboardInterrupt):
        return 130, "CANCELLED", "CANCELLED_BY_USER"
    return 7, "FAILED", code or "INTERNAL_FAILURE"


def _handle_cli_error(exc: BaseException, json_mode: bool, run_id: Optional[str] = None) -> None:
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


def _orchestration_exit_code(status: str) -> int:
    from .cli_execution import release_exit_code

    return release_exit_code(status)


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
    except KeyboardInterrupt as exc:
        _handle_cli_error(exc, json_mode)
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
        typer.Option("--mode", "--task-mode", "-m", help="Task mode: single_issue or repository"),
    ] = None,
    execution_mode: Annotated[
        str,
        typer.Option("--execution-mode", help="development (cumulative integration) or evaluation (independent cases)"),
    ] = "development",
    max_tasks: Annotated[
        Optional[int],
        typer.Option("--max-tasks", help="Repository mode: maximum selected issues (1-20, default 3)"),
    ] = None,
    until: Annotated[
        str,
        typer.Option("--until", help="Stop boundary: plan, action-proposed, verification-required, or complete"),
    ] = "complete",
    request: Annotated[
        Optional[str],
        typer.Option("--request", "-r", help="Path to an internal RunRequestV1 JSON file (headless)"),
    ] = None,
    input_path: Annotated[
        Optional[str],
        typer.Option("--input", "-i", help="Evaluator request (native_json_v1 EvaluatorRequestV1); implies machine mode"),
    ] = None,
    non_interactive: Annotated[
        bool,
        typer.Option("--non-interactive", help="Never prompt; fail with exit 2 if an input is missing"),
    ] = False,
    repo: Annotated[
        Optional[str],
        typer.Option("--repo", help="Repository: GitHub URL, local folder, local Git checkout, or ZIP path"),
    ] = None,
    task: Annotated[
        Optional[str],
        typer.Option("--task", "-t", help="Task text, issue URL/#number, or 'all open issues'"),
    ] = None,
    json_mode: Annotated[
        bool,
        typer.Option("--json", help="Emit single schema-valid JSON object to stdout"),
    ] = False,
) -> None:
    """Prepare, plan, act in the sandbox, verify, integrate, and report a truthful final result."""
    if input_path:
        from .cli_release import run_evaluator_request

        run_evaluator_request(input_path)
    if until not in {"plan", "action-proposed", "verification-required", "complete"}:
        _handle_cli_error(ValueError(f"Unsupported --until boundary: {until}"), json_mode)
    target = target or repo
    prompt = prompt or task
    if request:
        req_path = Path(request)
        if not req_path.is_file():
            _handle_cli_error(ValueError(f"Request file not found: {request}"), json_mode)
        try:
            run_req = RunRequestV1.model_validate_json(req_path.read_text(encoding="utf-8"))
        except Exception as exc:
            _handle_cli_error(exc, json_mode)
        prep_controller, _, _ = _get_services()
        try:
            prep_controller.reconcile_interrupted()
            prepared = prep_controller.prepare(run_req)
        except KeyboardInterrupt as exc:
            _handle_cli_error(exc, json_mode=json_mode)
        except Exception as exc:
            _handle_cli_error(exc, json_mode=json_mode)
        _run_after_prepare(prepared.run_id, until, json_mode)
        try:
            orchestration, _, _ = _get_orchestration_controller()
            result = prep_controller.continue_prepared_run(
                prepared.run_id, orchestration, stop_at=until
            )
        except KeyboardInterrupt as exc:
            _handle_cli_error(exc, json_mode=json_mode)
        except Exception as exc:
            _handle_cli_error(exc, json_mode=json_mode)
        if json_mode:
            _renderer.render_json(result)
        else:
            _err_console.print(f"[bold green]{result.status.value}[/bold green] for {result.task_id}")
            _err_console.print("[dim]The generated action is stored and has not been executed.[/dim]")
        raise typer.Exit(_orchestration_exit_code(result.status.value))

    if not json_mode:
        _err_console.print("\n[bold cyan]AI Coding Harness[/bold cyan] [dim]– plan, sandboxed fix, verification, private commit[/dim]\n")
    from .cli_release import build_interactive_request, ensure_model_profile, show_run_plan

    try:
        ensure_model_profile(get_config(), non_interactive or json_mode)
        run_req = build_interactive_request(target, prompt, mode, execution_mode, max_tasks, non_interactive or json_mode)
    except Exception as exc:
        _handle_cli_error(exc if isinstance(exc, ValueError) else ValueError(str(exc)), json_mode)
    if not json_mode:
        show_run_plan(run_req, get_config())

    controller, _, _ = _get_services()
    try:
        controller.reconcile_interrupted()
        prepared = controller.prepare(run_req)
    except KeyboardInterrupt as exc:
        _handle_cli_error(exc, json_mode=json_mode)
    except Exception as exc:
        _handle_cli_error(exc, json_mode=json_mode)
    if not json_mode:
        _err_console.print(f"  [green]✓[/green] Prepared run [bold]{prepared.run_id}[/bold] ({len(prepared.tasks)} task(s))")
    _run_after_prepare(prepared.run_id, until, json_mode)
    try:
        orchestration, _, _ = _get_orchestration_controller()
        result = controller.continue_prepared_run(
            prepared.run_id, orchestration, stop_at=until
        )
    except KeyboardInterrupt as exc:
        _handle_cli_error(exc, json_mode=json_mode)
    except Exception as exc:
        _handle_cli_error(exc, json_mode=json_mode)

    if json_mode:
        _renderer.render_json(result)
    else:
        _err_console.print(f"[bold green]{result.status.value}[/bold green] for {result.task_id}")
        _err_console.print("[dim]The generated action is stored and has not been executed.[/dim]")
        _err_console.print("\n[dim]Run [bold]harness status[/bold] or [bold]harness inspect[/bold] anytime to review.[/dim]\n")
    raise typer.Exit(_orchestration_exit_code(result.status.value))


# ── PRD 2 continuation commands ─────────────────────────────────────────────

@app.command("continue")
def cmd_continue(
    run_id: Annotated[str, typer.Argument(help="Prepared run ID")],
    until: Annotated[
        str,
        typer.Option("--until", help="Stop boundary: plan, action-proposed, verification-required, or complete"),
    ] = "action-proposed",
    json_mode: Annotated[
        bool,
        typer.Option("--json", help="Emit exactly one final JSON object to stdout"),
    ] = False,
) -> None:
    """Continue a prepared run through deterministic orchestration to a stop boundary."""
    if until in {"verification-required", "complete"}:
        from .cli_execution import continue_with_execution

        continue_with_execution(run_id, until, json_mode)
    controller, _, _ = _get_orchestration_controller()
    try:
        result = controller.continue_run(run_id, stop_at=until)
    except KeyboardInterrupt as exc:
        _handle_cli_error(exc, json_mode, run_id)
    except Exception as exc:
        _handle_cli_error(exc, json_mode, run_id)
    if json_mode:
        _renderer.render_json(result)
    else:
        _err_console.print(f"Run: [bold]{run_id}[/bold]")
        _err_console.print(f"State: [bold green]{result.status.value}[/bold green]")
        _err_console.print(f"Model calls used: {result.usage.model_calls_used}")
        if result.proposal:
            _err_console.print("[dim]Proposal persisted; no generated action was executed.[/dim]")
        if result.questions:
            for question in result.questions:
                _err_console.print(f"Question: {question.question}")
                _err_console.print(f"Impact: {question.impact}")
        if result.required_capability:
            _err_console.print(f"Required capability: {result.required_capability}")
    raise typer.Exit(_orchestration_exit_code(result.status.value))


@app.command("plan")
def cmd_plan(
    run_id: Annotated[str, typer.Argument(help="Prepared run ID")],
    json_mode: Annotated[
        bool,
        typer.Option("--json", help="Emit exactly one final JSON object to stdout"),
    ] = False,
) -> None:
    """Continue a prepared run only through PLAN_READY."""
    cmd_continue(run_id=run_id, until="plan", json_mode=json_mode)


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

    with run_store.get_connection() as conn:
        lifecycle = conn.execute(
            "SELECT * FROM h_run_lifecycle WHERE run_id = ?", (run_id,)
        ).fetchone()
        budget = conn.execute(
            "SELECT * FROM h_budget_ledgers WHERE run_id = ?", (run_id,)
        ).fetchone()
        remaining_tasks = (
            conn.execute(
                "SELECT COUNT(*) FROM h_tasks WHERE run_id = ? AND task_id <> ?",
                (run_id, lifecycle["active_task_id"]),
            ).fetchone()[0]
            if lifecycle
            else None
        )

    if lifecycle:
        status_data = {
            "schema_version": "1.0",
            "run_id": run_id,
            "status": lifecycle["state"],
            "preparation_status": run["state"],
            "active_task_id": lifecycle["active_task_id"],
            "remaining_repository_tasks": remaining_tasks,
            "lifecycle_version": lifecycle["version"],
            "stop_reason_code": lifecycle["stop_reason_code"],
            "usage": (
                {
                    "used_calls": budget["used_calls"],
                    "used_input_tokens": budget["used_input_tokens"],
                    "used_output_tokens": budget["used_output_tokens"],
                    "reserved_future_calls": budget["reserved_future_calls"],
                }
                if budget
                else None
            ),
        }
        if json_mode:
            _renderer.render_json(status_data)
        else:
            _err_console.print(f"Run ID: [bold]{run_id}[/bold]")
            _err_console.print(f"PRD 2 State: [bold green]{lifecycle['state']}[/bold green]")
            _err_console.print(f"Active task: {lifecycle['active_task_id']}")
            if lifecycle["stop_reason_code"]:
                _err_console.print(f"Stop reason: {lifecycle['stop_reason_code']}")
        raise typer.Exit(0)

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
    events = run_store.get_events(run_id)

    with run_store.get_connection() as conn:
        artifacts = conn.execute(
            "SELECT * FROM h_artifacts WHERE run_id = ?", (run_id,)
        ).fetchall()
        lifecycle = conn.execute(
            "SELECT * FROM h_run_lifecycle WHERE run_id = ?", (run_id,)
        ).fetchone()
        budget = conn.execute(
            "SELECT * FROM h_budget_ledgers WHERE run_id = ?", (run_id,)
        ).fetchone()
        plans = conn.execute(
            "SELECT * FROM h_plans WHERE run_id = ? ORDER BY plan_revision", (run_id,)
        ).fetchall()
        proposals = conn.execute(
            "SELECT * FROM h_action_proposals WHERE run_id = ? ORDER BY created_at", (run_id,)
        ).fetchall()

    _renderer.render_inspect(
        run=run,
        source=source,
        workspace=workspace,
        tasks=tasks,
        artifacts=[dict(a) for a in artifacts],
        events=events,
    )
    if lifecycle:
        _err_console.print("\n[bold]PRD 2 Orchestration[/bold]")
        _err_console.print(
            f"State: [bold green]{lifecycle['state']}[/bold green]  "
            f"Version: {lifecycle['version']}  Active task: {lifecycle['active_task_id']}"
        )
    if budget:
        _err_console.print(
            f"Budget: {budget['used_calls']}/{budget['max_calls']} calls, "
            f"{budget['used_input_tokens']}/{budget['max_input_tokens']} input tokens, "
            f"{budget['used_output_tokens']}/{budget['max_output_tokens']} output tokens"
        )
    if plans:
        _err_console.print(f"Plans: {len(plans)} (active revision {plans[-1]['plan_revision']})")
    if proposals:
        _err_console.print(
            f"Action proposals: {len(proposals)} (latest state {proposals[-1]['state']}; unexecuted by PRD 2)"
        )
    sys.exit(0)


# ── PRD 2 inspection commands ───────────────────────────────────────────────

@app.command("budget")
def cmd_budget(
    run_id: Annotated[str, typer.Argument(help="Run ID")],
    json_mode: Annotated[
        bool,
        typer.Option("--json", help="Emit exactly one JSON object to stdout"),
    ] = False,
) -> None:
    """Show shared model-call and token budget accounting."""
    _, run_store, _ = _get_services()
    try:
        data = BudgetLedgerService(run_store).get(run_id)
    except Exception as exc:
        _handle_cli_error(exc, json_mode, run_id)
    if json_mode:
        _renderer.render_json({"schema_version": "1.0", **data})
    else:
        _err_console.print(
            f"Calls: {data['used_calls']} used, {data['reserved_calls']} active, "
            f"{data['reserved_future_calls']} reserved for verification"
        )
        _err_console.print(
            f"Tokens: {data['used_input_tokens']} input / {data['used_output_tokens']} output"
        )
    raise typer.Exit(0)


@context_app.command("inspect")
def cmd_context_inspect(
    run_id: Annotated[str, typer.Argument(help="Run ID")],
    role: Annotated[str, typer.Option("--role", help="planner, coder, or validator")],
    packet_id: Annotated[str, typer.Option("--packet", help="Context packet ID")],
) -> None:
    """Print one exact filtered model-visible packet and its metadata."""
    _, run_store, artifact_store = _get_services()
    with run_store.get_connection() as conn:
        row = conn.execute(
            """
            SELECT p.*, a.relative_path, a.sha256 AS artifact_sha256
            FROM h_context_packets p JOIN h_artifacts a ON a.artifact_id = p.artifact_id
            WHERE p.run_id = ? AND p.packet_id = ? AND p.role = ?
            """,
            (run_id, packet_id, role),
        ).fetchone()
    if not row:
        _handle_cli_error(ValueError("Context packet not found"), True, run_id)
    relative = row["relative_path"].split("/artifacts/", 1)[-1]
    if not artifact_store.verify(run_id, relative):
        _handle_cli_error(RuntimeError("Context packet integrity verification failed"), True, run_id)
    exact = json.loads(artifact_store.open_readonly(run_id, relative))
    _renderer.render_json(
        {
            "schema_version": "1.0",
            "packet": {
                "packet_id": row["packet_id"],
                "role": row["role"],
                "purpose": row["purpose"],
                "source_revision": row["source_revision"],
                "estimated_input_tokens": row["estimated_input_tokens"],
                "counter_mode": row["counter_mode"],
                "sha256": row["packet_sha256"],
            },
            "exact_filtered_packet": exact,
        }
    )
    raise typer.Exit(0)


@evidence_app.command("list")
def cmd_evidence_list(
    run_id: Annotated[str, typer.Argument(help="Run ID")],
    task_id: Annotated[str, typer.Option("--task", help="Task ID")],
    json_mode: Annotated[
        bool,
        typer.Option("--json", help="Emit exactly one JSON object to stdout"),
    ] = False,
) -> None:
    """List versioned evidence metadata without mutating the run."""
    _, run_store, _ = _get_services()
    with run_store.get_connection() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT evidence_id, source_revision, relative_path, start_line,
                       end_line, symbol, evidence_type, retrieval_reason,
                       truth_status, content_sha256, valid, invalidated_at
                FROM h_evidence WHERE run_id = ? AND task_id = ?
                ORDER BY created_at, evidence_id
                """,
                (run_id, task_id),
            ).fetchall()
        ]
    payload = {
        "schema_version": "1.0",
        "run_id": run_id,
        "task_id": task_id,
        "evidence": rows,
    }
    if json_mode:
        _renderer.render_json(payload)
    else:
        for row in rows:
            _err_console.print(
                f"{row['evidence_id']}  {row['truth_status']}  "
                f"{row['relative_path']}:{row['start_line'] or '-'}  valid={bool(row['valid'])}"
            )
    raise typer.Exit(0)


@model_app.command("doctor")
def cmd_model_doctor(
    profile_id: Annotated[
        Optional[str],
        typer.Option("--profile", help="Trusted model profile ID (default: HARNESS_MODEL_PROFILE)"),
    ] = None,
    live: Annotated[
        bool,
        typer.Option("--live", help="Send one tiny structured probe request to the provider"),
    ] = False,
    json_mode: Annotated[
        bool,
        typer.Option("--json", help="Emit exactly one JSON object to stdout"),
    ] = False,
) -> None:
    """Validate the selected model profile and credential presence; optionally probe the provider live."""
    profile_id = profile_id or get_config().model_profile
    probe = None
    try:
        resolver = ModelProfileResolver(get_config().model_profiles_path)
        resolved = resolver.resolve(profile_id)
        resolver.validate_live(resolved)
        try:
            CredentialProvider().get_ai_api_key()
            credential_available = True
        except Exception:
            credential_available = False
        if live and credential_available:
            from .model.probe import probe_model

            probe = probe_model(resolved)
        status = "READY" if credential_available else "MODEL_AUTH_MISSING"
        if probe is not None and probe["status"] != "PASS":
            status = probe["error_code"] or "MODEL_PROBE_FAILED"
        payload = {
            "schema_version": "1.0",
            "status": status,
            "profile_id": profile_id,
            "profile": resolved.contract.model_dump(mode="json"),
            "structured_output": resolved.response_format,
            "environment_overrides": sorted(resolved.overrides),
            "adapter": resolved.adapter_name,
            "credential_env": "AI_API_KEY",
            "credential_available": credential_available,
            "live_probe": probe,
        }
    except Exception as exc:
        _handle_cli_error(exc, json_mode)
    if json_mode:
        _renderer.render_json(payload)
    else:
        _err_console.print(f"Profile: [bold]{profile_id}[/bold]")
        _err_console.print(f"Model: {payload['profile']['model']}")
        _err_console.print(f"Endpoint: {payload['profile']['endpoint_origin']}  (structured output: {payload['structured_output']})")
        _err_console.print(f"Credential (AI_API_KEY) available: {payload['credential_available']}")
        if probe is not None:
            _err_console.print(f"Live probe: {probe['status']} ({probe.get('latency_ms')} ms, usage reported: {probe.get('usage_reported')})"
                               + (f" error {probe['error_code']}: {probe.get('message', '')}" if probe['status'] != 'PASS' else ""))
    raise typer.Exit(0 if payload["status"] == "READY" else 2)


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


def legacy_clean_all(force: bool = False) -> None:
    """Developer reset (`harness clean --all`): remove ALL runs and the database (never original sources)."""
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


def _run_after_prepare(run_id: str, until: str, json_mode: bool) -> None:
    """Route a freshly prepared run: `complete` runs the full queue pipeline."""
    from .cli_execution import continue_with_execution, run_pipeline

    if until == "complete":
        run_pipeline(run_id, json_mode)
    if until == "verification-required":
        continue_with_execution(run_id, until, json_mode)


from .cli_execution import register as _register_execution_commands  # noqa: E402
from .cli_release import cmd_clean as _cmd_clean, register as _register_release_commands  # noqa: E402

_register_execution_commands(app)
_register_release_commands(app)
app.command("clean")(_cmd_clean)


if __name__ == "__main__":
    app()
