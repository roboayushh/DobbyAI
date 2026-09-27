"""PRD 3-5 terminal and headless commands.

Registered on the main Typer app by :mod:`harness.cli`. Every command reads
host-owned state through the same services the pipeline uses. ``--json``
prints exactly one object to stdout; human output goes to stderr. Exit codes
follow the canonical release table (PRD 6 section 6.4); the JSON status stays
authoritative.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

import typer
from rich import box
from rich.prompt import Confirm
from rich.table import Table

from .application.composition import (
    HarnessServices,
    build_controller,
    build_services,
    ensure_runtime,
    scaled_budget,
)
from .cli_renderer import sanitize_terminal_text
from .config import HarnessConfig, get_config
from .contracts.execution import PermissionProfile
from .persistence import ArtifactStore, RunStore

# Canonical release exit codes (PRD 6 section 6.4). Earlier PRD tables are adapters into this one.
RELEASE_EXIT_CODES: Dict[str, int] = {
    "COMPLETED_ALL": 0,
    "PASS": 0,
    "READY_FOR_REVIEW": 0,
    "PLAN_READY": 0,
    "ACTION_PROPOSED": 0,
    "VERIFICATION_REQUIRED": 0,
    "INVALID": 2,
    "PARTIAL_SUCCESS": 3,
    "UNVERIFIED": 3,
    "BLOCKED_ENVIRONMENT": 3,
    "NEEDS_CAPABILITY": 3,
    "FAILED": 4,
    "VERIFICATION_FAILED": 4,
    "BUDGET_EXHAUSTED": 5,
    "NEEDS_INPUT": 6,
    "NEEDS_APPROVAL": 6,
    "PENDING_APPROVAL": 6,
    "INTEGRATION_UNCERTAIN": 7,
    "ACTION_UNKNOWN": 7,
    "CANCELLED": 130,
}

# Test seam: a callable returning a model adapter (never set in production).
ADAPTER_FACTORY: Optional[Callable[[], Any]] = None


def release_exit_code(status: str) -> int:
    from .release.status_mapping import exit_code

    return exit_code(status)


@dataclass
class Stack:
    config: HarnessConfig
    run_store: RunStore
    artifact_store: ArtifactStore
    services: HarnessServices
    controller: Any
    coordinator: Any


def build_stack(run_id: Optional[str] = None, *, config: Optional[HarnessConfig] = None) -> Stack:
    from .cli import _get_services
    from .queue import QueueCoordinator

    config = config or get_config()
    _, run_store, artifact_store = _get_services(config)
    try:
        profile = PermissionProfile(config.permission_profile)
    except ValueError as exc:
        raise ValueError(f"Unknown permission profile {config.permission_profile!r}; use guided, sandbox, or delegated") from exc
    services = build_services(run_store, artifact_store, config.data_dir, permission_profile=profile,
                              dependency_setup=config.dependency_setup)
    count = len(run_store.get_tasks(run_id)) if run_id else 1
    controller = build_controller(
        services,
        config.model_profiles_path,
        adapter=ADAPTER_FACTORY() if ADAPTER_FACTORY else None,
        profile_id=config.model_profile,
        budget_limits=scaled_budget(count, max_calls=config.max_model_calls, max_wall_seconds=config.max_run_wall_seconds),
    )
    coordinator = QueueCoordinator(run_store=run_store, artifact_store=artifact_store, services=services, controller=controller)
    return Stack(config, run_store, artifact_store, services, controller, coordinator)


# ------------------------------------------------------------------ output
def _console():
    from .cli import _err_console

    return _err_console


def _emit(payload: Any, json_mode: bool, render: Optional[Callable[[], None]] = None) -> None:
    from .cli import _renderer

    if json_mode:
        _renderer.render_json(payload)
    elif render is not None:
        render()
    else:
        data = payload.model_dump(mode="json") if hasattr(payload, "model_dump") else payload
        _console().print_json(json.dumps(data, default=str))


def _fail(exc: BaseException, json_mode: bool, run_id: Optional[str] = None) -> None:
    from .cli import _handle_cli_error

    _handle_cli_error(exc, json_mode, run_id)


def _safe(text: Any, limit: int = 120) -> str:
    value = sanitize_terminal_text(str(text if text is not None else ""))
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _short(oid: Optional[str]) -> str:
    return (oid or "-")[:10]


def _require_run(stack: Stack, run_id: str) -> Dict[str, Any]:
    run = stack.run_store.get_run(run_id)
    if not run:
        raise ValueError(f"Run not found: {run_id}")
    return run


def _rows(stack: Stack, sql: str, params: tuple) -> List[Dict[str, Any]]:
    with stack.run_store.get_connection() as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _row(stack: Stack, sql: str, params: tuple) -> Optional[Dict[str, Any]]:
    rows = _rows(stack, sql, params)
    return rows[0] if rows else None


def _artifact_bytes(stack: Stack, artifact_id: Optional[str]) -> Optional[bytes]:
    if not artifact_id:
        return None
    artifact = stack.artifact_store.get_artifact_by_id(artifact_id)
    if not artifact:
        return None
    path = stack.config.data_dir / artifact["relative_path"]
    if not path.is_file():
        return None
    return path.read_bytes()


def _artifact_json(stack: Stack, artifact_id: Optional[str]) -> Optional[Any]:
    data = _artifact_bytes(stack, artifact_id)
    return json.loads(data) if data else None


def _artifact_path(stack: Stack, artifact_id: Optional[str]) -> Optional[str]:
    artifact = stack.artifact_store.get_artifact_by_id(artifact_id) if artifact_id else None
    return str(stack.config.data_dir / artifact["relative_path"]) if artifact else None


# ------------------------------------------------------------- pipeline
def run_pipeline(run_id: str, json_mode: bool, stack: Optional[Stack] = None, *, interactive: bool = False) -> None:
    """PREPARED run -> queue (one or more tasks) -> truthful final result.

    ``interactive`` (terminal `harness run` only) lets a verified result offer to
    bring its final patch back to the original source; machine modes never ask.
    """
    stack = stack or build_stack(run_id)
    log = None if json_mode else (lambda message: _console().print(f"[dim]{_safe(message, 400)}[/dim]"))
    blocked = ensure_runtime(stack.services, auto_build=stack.config.auto_build_runtime, log=log)
    if blocked:
        payload = {
            "schema_version": "1.0",
            "run_id": run_id,
            "status": "BLOCKED_ENVIRONMENT",
            "reason_code": blocked,
            "message": "The sandbox runtime is unavailable; the harness never falls back to host execution. "
                       "Start Docker (or run `harness sandbox build`) and then `harness resume " + run_id + "`.",
            "publication_authorized": False,
        }
        _emit(payload, json_mode, lambda: _console().print(f"[bold yellow]BLOCKED_ENVIRONMENT[/bold yellow] ({blocked}): {payload['message']}"))
        raise typer.Exit(release_exit_code("BLOCKED_ENVIRONMENT"))
    try:
        result = stack.coordinator.run(run_id)
    except KeyboardInterrupt as exc:
        try:
            stack.coordinator.request_cancel(run_id)
        except Exception:
            pass
        _fail(exc, json_mode, run_id)
    except Exception as exc:
        _fail(exc, json_mode, run_id)
    _render_queue_outcome(stack, run_id, result, json_mode, interactive=interactive and not json_mode)


def _render_queue_outcome(stack: Stack, run_id: str, result: Any, json_mode: bool, *, interactive: bool = False) -> None:
    from .contracts.queue import QueueFinalResultV1

    if isinstance(result, QueueFinalResultV1):
        _emit(result, json_mode, lambda: _render_final(stack, result, interactive=interactive))
        raise typer.Exit(release_exit_code(result.status))
    # Paused (approval or explicit pause): report progress truthfully.
    pending = stack.services.actions.approvals.pending(run_id)
    status = "NEEDS_APPROVAL" if pending else "PAUSED"
    payload = result.model_dump(mode="json")
    payload["pending_approvals"] = [row["approval_request_id"] for row in pending]

    def render() -> None:
        _render_progress(stack, run_id, result)
        for row in pending:
            _console().print(f"Approval required: [bold]{row['approval_request_id']}[/bold]  (harness approval show {row['approval_request_id']})")

    _emit(payload, json_mode, render)
    raise typer.Exit(release_exit_code(status) if status == "NEEDS_APPROVAL" else 0)


def _render_final(stack: Stack, final: Any, *, interactive: bool = False) -> None:
    console = _console()
    colour = {"COMPLETED_ALL": "green", "PARTIAL_SUCCESS": "yellow"}.get(final.status, "red")
    console.print(f"\nRun: [bold]{final.run_id}[/bold]   Result: [bold {colour}]{final.status}[/bold {colour}]")
    console.print(
        f"Baseline {_short(final.baseline.commit)} -> integration {_short(final.final_integration.commit)} "
        f"(sequence {final.final_integration.sequence}); aggregate verification: {final.aggregate_verification.status}"
    )
    table = Table(box=box.SIMPLE, show_header=True)
    for column in ("Task", "State", "Commit", "Summary"):
        table.add_column(column)
    tasks = {row["task_id"]: json.loads(row["task_spec_json"]) for row in stack.run_store.get_tasks(final.run_id)}
    for entry in final.task_results:
        spec = tasks.get(entry.task_id, {})
        table.add_row(entry.task_id, entry.status, _short(entry.commit), _safe(spec.get("title", ""), 60))
    console.print(table)
    counts = final.counts
    console.print(f"Tasks: {counts.integrated} integrated | {counts.failed} failed | {counts.blocked} blocked | {counts.remaining} remaining")
    patch = _artifact_path(stack, final.final_patch_artifact_id)
    if patch:
        console.print(f"Final patch (baseline -> candidate): [cyan]{patch}[/cyan]")
    report = _artifact_json(stack, final.queue_report_artifact_id) or {}
    for partial in report.get("best_partial_candidates", [])[:20]:
        console.print(
            f"[yellow]Best unverified attempt[/yellow] for {partial['task_id']} ({partial['status']}, not integrated): "
            f"[cyan]{_artifact_path(stack, partial['patch_artifact_id'])}[/cyan]"
        )
    applied = False
    if interactive and patch and final.status in APPLY_OFFER_STATUSES:
        # The prepared RunRequestV1 names the original source the workspace was copied from.
        run = stack.run_store.get_run(final.run_id) or {}
        repository = json.loads(run.get("request_json") or "{}").get("repository") or {}
        applied = offer_apply_to_original(console, repository.get("kind"), repository.get("locator"), patch, final.run_id)
    if not applied:
        console.print("[dim]Private result only: nothing was pushed, merged, or applied to the original repository.[/dim]\n")


# ------------------------------------------------ opt-in apply to original
# Final statuses whose verified patch may be offered back to the original source.
APPLY_OFFER_STATUSES = frozenset({"PASS", "COMPLETED_ALL", "PARTIAL_SUCCESS"})
_APPLY_GIT_TIMEOUT_SECONDS = 120
_APPLY_LISTED_FILES = 50


def offer_apply_to_original(
    console: Any,
    kind: Optional[str],
    locator: Optional[str],
    patch_path: Any,
    run_id: str,
    ask: Callable[..., bool] = Confirm.ask,
) -> bool:
    """Ask (default No) whether to bring the verified final patch back to the original source.

    Local Git checkouts and folders get ``git apply`` in their working tree only: never a
    commit, a push, or a write outside that directory. ZIP archives and GitHub sources are
    never modified; the patch can only be saved as a file. Returns True only when the
    original directory was changed.
    """
    patch = Path(patch_path)
    if not patch.is_file() or patch.stat().st_size == 0:
        return False
    kind = str(getattr(kind, "value", kind) or "")
    if kind in ("local_git", "local_folder"):
        return _apply_to_directory(console, Path(locator or ""), patch, run_id, ask)
    if kind == "local_zip":
        archive = Path(locator or "")
        if not archive.is_file():
            console.print(f"[yellow]Not saved:[/yellow] the original archive {_safe(locator, 400)} no longer exists. Nothing was changed.")
            return False
        destination = archive.parent / f"{_file_part(archive.stem)}-{_file_part(run_id)}.patch"
        console.print("The original is a ZIP archive; it is never modified in place.")
        _save_patch_copy(console, patch, destination, ask)
        return False
    if kind == "public_https":
        name = _file_part(urlparse(locator or "").path.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git"))
        destination = Path.cwd() / f"{name}-{_file_part(run_id)}.patch"
        console.print("The original is a remote repository; the harness never pushes or opens pull requests.")
        if _save_patch_copy(console, patch, destination, ask):
            console.print(f"Apply it in your own clone of {_safe(locator, 300)} with: git apply --binary {_safe(destination, 400)}")
        return False
    return False


def _file_part(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(value or "")).strip(".") or "repository"


def _confirm(ask: Callable[..., bool], console: Any, prompt: str) -> bool:
    """No input (closed or non-TTY stdin) or Ctrl-C at the prompt means No."""
    try:
        return bool(ask(prompt, console=console, default=False))
    except (EOFError, KeyboardInterrupt):
        console.print()
        return False


def _original_git(root: Path, *args: str) -> subprocess.CompletedProcess:
    """Host ``git`` in the original directory: no hooks, no system/global config, no prompts."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin"),
        "LC_ALL": "C",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        # Never discover (and so never write through) a repository above the original directory.
        "GIT_CEILING_DIRECTORIES": str(root.parent),
    }
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.quotePath=false", *args],
        cwd=str(root), env=env, stdin=subprocess.DEVNULL, capture_output=True,
        timeout=_APPLY_GIT_TIMEOUT_SECONDS, check=False,
    )


def _apply_to_directory(console: Any, root: Path, patch: Path, run_id: str, ask: Callable[..., bool]) -> bool:
    shown = _safe(root, 400)
    if not root.is_dir():
        console.print(f"[yellow]Not applied:[/yellow] the original repository {shown} no longer exists. Nothing was changed.")
        return False
    if not _confirm(ask, console, f"Apply these verified changes to the original repository {shown}?"):
        return False
    patch_arg = str(patch.resolve())
    try:
        check = _original_git(root, "apply", "--check", "--binary", patch_arg)
        stats = _original_git(root, "apply", "--numstat", "--binary", patch_arg) if check.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired) as exc:
        console.print(f"[red]Could not check the patch with git apply:[/red] {_safe(exc, 300)}\nNothing was changed.")
        return False
    if check.returncode != 0:
        console.print(f"[red]The verified patch does not apply cleanly to {shown}:[/red]")
        console.print(_safe(check.stderr.decode("utf-8", "replace").strip(), 2000))
        console.print("Nothing was changed.")
        return False
    try:
        applied = _original_git(root, "apply", "--binary", patch_arg)
    except (OSError, subprocess.TimeoutExpired) as exc:
        console.print(f"[red]git apply did not finish:[/red] {_safe(exc, 300)}. Review {shown} before continuing.")
        return False
    if applied.returncode != 0:
        console.print(f"[red]git apply failed after a clean check:[/red] {_safe(applied.stderr.decode('utf-8', 'replace').strip(), 2000)}")
        console.print(f"Review {shown} before continuing.")
        return False
    # --numstat only reads the patch: "<added>\t<deleted>\t<path>", "-" counts for binary files.
    rows = [line.split("\t", 2) for line in (stats.stdout if stats else b"").decode("utf-8", "replace").splitlines() if line.count("\t") >= 2]
    console.print(f"Changed files in {shown}:")
    for added, deleted, path in rows[:_APPLY_LISTED_FILES]:
        lines = "binary" if added == "-" else f"+{added} -{deleted}"
        console.print(f"  {lines:<12} {_safe(path, 300)}")
    if len(rows) > _APPLY_LISTED_FILES:
        console.print(f"  … and {len(rows) - _APPLY_LISTED_FILES} more file(s)")
    added_total = sum(int(row[0]) for row in rows if row[0].isdigit())
    deleted_total = sum(int(row[1]) for row in rows if row[1].isdigit())
    digest = hashlib.sha256(patch.read_bytes()).hexdigest()
    console.print(f"Receipt: run {_safe(run_id)} | patch sha256 {digest[:16]} | {len(rows)} file(s), +{added_total} -{deleted_total} | {shown}")
    console.print(f"[bold green]Applied[/bold green] to the working tree of {shown} only: nothing was committed or pushed.\n")
    return True


def _save_patch_copy(console: Any, patch: Path, destination: Path, ask: Callable[..., bool]) -> bool:
    shown = _safe(destination, 400)
    if not _confirm(ask, console, f"Save the verified patch as {shown}?"):
        return False
    try:
        with destination.open("xb") as handle:  # never overwrite an existing file
            handle.write(patch.read_bytes())
    except FileExistsError:
        console.print(f"[yellow]Not saved:[/yellow] {shown} already exists. Nothing was changed.")
        return False
    except OSError as exc:
        console.print(f"[red]Could not save the patch:[/red] {_safe(exc, 300)}")
        return False
    console.print(f"[green]Saved[/green] the verified patch: [cyan]{shown}[/cyan]")
    return True


def _render_progress(stack: Stack, run_id: str, progress: Any) -> None:
    console = _console()
    counts = progress.counts
    console.print(
        f"Run: [bold]{run_id}[/bold]   Queue: [bold]{progress.queue_state}[/bold]   Integration: {_short(progress.integration.commit)}"
    )
    console.print(
        f"Tasks: {counts.integrated} integrated | {counts.running} running | {counts.blocked} blocked | "
        f"{counts.failed} failed | {counts.remaining} remaining"
    )
    budget = progress.budget
    console.print(
        f"Budget: {budget.model_calls_remaining} model calls left | {max(0, budget.wall_seconds_remaining) // 60}m left | "
        f"final reserve {'intact' if budget.final_reserve_intact else 'AT RISK'}"
    )
    queue = stack.coordinator.queue(run_id)
    items = stack.coordinator.items(queue)
    edges = stack.coordinator.edges(queue)
    ordinal = {item["task_id"]: item["ordinal"] + 1 for item in items}
    table = Table(box=box.SIMPLE, show_header=True)
    for column in ("#", "State", "Depends", "Start", "Candidate", "Summary"):
        table.add_column(column)
    for item in items:
        execution = _row(stack, "SELECT start_commit_oid FROM h_task_executions WHERE queue_item_id = ? ORDER BY attempt_number DESC LIMIT 1", (item["queue_item_id"],))
        candidate = stack.services.workspaces.latest_candidate(run_id, item["task_id"])
        depends = ",".join(f"#{ordinal[e.predecessor]}" for e in edges if e.dependent == item["task_id"]) or "-"
        table.add_row(
            str(ordinal[item["task_id"]]), item["state"], depends,
            _short(execution["start_commit_oid"] if execution else None),
            _short(candidate["candidate_commit"] if candidate else None),
            _safe(json.loads(item["task_spec_json"]).get("title", ""), 50),
        )
    console.print(table)


def continue_with_execution(run_id: str, until: str, json_mode: bool) -> None:
    stack = build_stack(run_id)
    blocked = ensure_runtime(stack.services, auto_build=stack.config.auto_build_runtime,
                             log=None if json_mode else (lambda m: _console().print(f"[dim]{_safe(m, 400)}[/dim]")))
    if blocked:
        _fail(_coded(blocked, "The sandbox runtime is unavailable; start Docker or run `harness sandbox build`."), json_mode, run_id)
    try:
        result = stack.controller.continue_run(run_id, stop_at=until)
    except KeyboardInterrupt as exc:
        _fail(exc, json_mode, run_id)
    except Exception as exc:
        _fail(exc, json_mode, run_id)

    def render() -> None:
        console = _console()
        console.print(f"Run: [bold]{run_id}[/bold]  Task: {result.task_id}")
        console.print(f"State: [bold]{result.status.value}[/bold]  Actions executed: {result.actions_executed}")
        if result.candidate:
            console.print(f"Candidate: {result.candidate.candidate_id} ({_short(result.candidate.commit)}) changed: {', '.join(result.candidate.changed_paths[:10])}")
        if result.verification:
            console.print(f"Verification: {result.verification.status}")
        if result.approval_request_id:
            console.print(f"Approval required: harness approval show {result.approval_request_id}")
        if result.stop_reason_code:
            console.print(f"Stop reason: {result.stop_reason_code}")

    _emit(result, json_mode, render)
    raise typer.Exit(release_exit_code(result.status.value))


class _CodedError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _coded(code: str, message: str) -> _CodedError:
    return _CodedError(code, message)


# =================================================================== apps
action_app = typer.Typer(help="Inspect sandboxed actions (PRD 3)")
approval_app = typer.Typer(help="Show and decide one-use action approvals (PRD 3)")
policy_app = typer.Typer(help="Inspect the immutable run policy snapshot (PRD 3)")
sandbox_app = typer.Typer(help="Sandbox runtime diagnostics (PRD 3)")
workspace_app = typer.Typer(help="Private task workspace state (PRD 3)")
verification_app = typer.Typer(help="Verification contracts, baselines, and check evidence (PRD 4)")
validator_app = typer.Typer(help="Independent validator reviews (PRD 4)")
queue_app = typer.Typer(help="Whole-repository task queue (PRD 5)")
task_app = typer.Typer(help="Per-task inspection (PRD 5)")
git_app = typer.Typer(help="Private Git history inspection (PRD 5)")

JsonOpt = Annotated[bool, typer.Option("--json", help="Emit exactly one JSON object to stdout")]


def register(app: typer.Typer) -> None:
    for name, sub in (
        ("action", action_app), ("approval", approval_app), ("policy", policy_app), ("sandbox", sandbox_app),
        ("workspace", workspace_app), ("verification", verification_app), ("validator", validator_app),
        ("queue", queue_app), ("task", task_app), ("git", git_app),
    ):
        app.add_typer(sub, name=name)
    app.command("approve")(cmd_approve)
    app.command("deny")(cmd_deny)
    app.command("resume")(cmd_resume)
    app.command("verify")(cmd_verify)
    app.command("repair")(cmd_repair)
    app.command("report")(cmd_report)
    app.command("recover")(cmd_recover)


# ------------------------------------------------------------------ action
@action_app.command("inspect")
def cmd_action_inspect(action_id: Annotated[str, typer.Argument(help="Action ID")], json_mode: JsonOpt = False) -> None:
    """Show an action's admission, sandbox, settlement, and changes."""
    stack = build_stack()
    action = stack.services.actions.get_action(action_id)
    if not action:
        _fail(ValueError(f"Unknown action: {action_id}"), json_mode)
    result = stack.services.actions.result_for_action(action_id)
    changes = _rows(stack, """SELECT c.relative_path AS path, c.change_type, c.policy_state FROM h_file_changes c
                               JOIN h_execution_results r ON r.execution_result_id = c.execution_result_id
                               WHERE r.action_id = ? ORDER BY c.relative_path""", (action_id,))
    reasons = _row(stack, "SELECT reason_codes_json FROM h_execution_results WHERE action_id = ?", (action_id,))
    reason_codes = json.loads(reasons["reason_codes_json"] or "[]") if reasons else []
    tools = _rows(stack, "SELECT sequence, tool_name, state, arguments_summary_json FROM h_tool_calls WHERE action_id = ? ORDER BY sequence", (action_id,))
    sandbox = _rows(stack, "SELECT engine, engine_version, image_digest, network_mode, state, settings_sha256 FROM h_sandbox_instances WHERE action_id = ?", (action_id,))
    payload = {
        "schema_version": "1.0",
        "action": {key: action[key] for key in ("action_id", "run_id", "task_id", "state", "code_sha256", "read_only", "checkpoint_commit", "policy_id")},
        "decision": json.loads(action["decision_json"]) if action.get("decision_json") else None,
        "result": result.model_dump(mode="json") if result else None,
        "reason_codes": reason_codes,
        "file_changes": changes[:500],
        "tool_calls": [{**row, "arguments_summary_json": json.loads(row["arguments_summary_json"] or "{}")} for row in tools[:200]],
        "sandbox": sandbox,
    }

    def render() -> None:
        console = _console()
        console.print(f"Action [bold]{action_id}[/bold]  state {action['state']}  code {action['code_sha256'][:12]}")
        if result:
            console.print(f"Settlement: [bold]{result.settlement.value}[/bold]  exit {result.process.exit_code}  {result.process.elapsed_ms} ms")
            for reason in reason_codes[:10]:
                console.print(f"  reason: {reason}")
        for change in changes[:30]:
            console.print(f"  {change['change_type']:<9} {change['policy_state']:<11} {_safe(change['path'])}")
        for tool in tools[:30]:
            console.print(f"  tool #{tool['sequence']} {tool['tool_name']} {tool['state']}")

    _emit(payload, json_mode, render)
    raise typer.Exit(0)


@action_app.command("logs")
def cmd_action_logs(
    action_id: Annotated[str, typer.Argument(help="Action ID")],
    stream: Annotated[str, typer.Option("--stream", help="stdout or stderr")] = "stdout",
) -> None:
    """Print an action's captured, bounded output stream (sanitized)."""
    stack = build_stack()
    if stream not in ("stdout", "stderr"):
        _fail(ValueError("--stream must be stdout or stderr"), False)
    row = _row(stack, "SELECT stdout_artifact_id, stderr_artifact_id FROM h_execution_results WHERE action_id = ?", (action_id,))
    if not row:
        _fail(ValueError(f"No settled result for action {action_id}"), False)
    data = _artifact_bytes(stack, row[f"{stream}_artifact_id"]) or b""
    sys.stdout.write(sanitize_terminal_text(data.decode("utf-8", "replace")))
    raise typer.Exit(0)


# ---------------------------------------------------------------- approval
@approval_app.command("show")
def cmd_approval_show(request_id: Annotated[str, typer.Argument(help="Approval request ID")], json_mode: JsonOpt = False) -> None:
    """Show the exact binding (code hash, capabilities, paths, limits, expiry) of an approval request."""
    stack = build_stack()
    if request_id.startswith("capreq_"):
        from .approvals.capabilities import CapabilityError, CapabilityService

        try:
            shown = CapabilityService(stack.run_store, stack.artifact_store).show(request_id)
        except CapabilityError as exc:
            _fail(ValueError(str(exc)), json_mode)
        _emit(shown, json_mode, lambda: _console().print(
            f"Capability request [bold]{request_id}[/bold] {shown['request']['operation']} state {shown['state']} "
            f"expires {shown['expires_at']}\n  {shown['summary']['summary']}\n  request_sha256 {shown['request']['request_sha256']}"))
        raise typer.Exit(0)
    try:
        shown = stack.services.actions.approvals.show(request_id)
    except KeyError as exc:
        _fail(ValueError(str(exc)), json_mode)

    def render() -> None:
        request = shown["request"]
        console = _console()
        console.print(f"Approval [bold]{request_id}[/bold]  state {shown['state']}  uses {shown['used_count']}/1")
        for key in ("action_id", "purpose", "code_sha256", "capabilities", "declared_paths", "workspace_version", "policy_sha256", "limits", "expires_at", "consequence"):
            if key in request:
                console.print(f"  {key}: {_safe(json.dumps(request[key]) if not isinstance(request[key], str) else request[key], 300)}")
        console.print(f"  binding_sha256: {shown['binding_sha256']}")

    _emit(shown, json_mode, render)
    raise typer.Exit(0)


@approval_app.command("list")
def cmd_approval_list(run_id: Annotated[str, typer.Argument(help="Run ID")], json_mode: JsonOpt = False) -> None:
    """List pending approval requests for a run."""
    stack = build_stack()
    pending = stack.services.actions.approvals.pending(run_id)
    payload = {"schema_version": "1.0", "run_id": run_id, "pending": [
        {key: row[key] for key in ("approval_request_id", "action_id", "task_id", "state", "expires_at")} for row in pending
    ]}
    _emit(payload, json_mode, lambda: [_console().print(f"{r['approval_request_id']}  action {r['action_id']}  expires {r['expires_at']}") for r in payload["pending"]] or _console().print("No pending approvals."))
    raise typer.Exit(0)


def cmd_approve(request_id: Annotated[str, typer.Argument(help="Approval request ID")], json_mode: JsonOpt = False) -> None:
    """Grant a one-use approval bound to the exact action (or capability request) shown by `approval show`."""
    stack = build_stack()
    if request_id.startswith("capreq_"):
        from .approvals.capabilities import CapabilityError, CapabilityService, KernelPrincipal

        try:
            grant = CapabilityService(stack.run_store, stack.artifact_store).grant(request_id, KernelPrincipal.interactive())
        except CapabilityError as exc:
            _fail(ValueError(f"{exc.code}: {exc}"), json_mode)
        _emit(grant, json_mode, lambda: _console().print(f"[green]Granted[/green] {request_id} ({grant.max_uses} use, expires {grant.expires_at})"))
        raise typer.Exit(0)
    try:
        granted = stack.services.actions.approvals.approve(request_id, approved_by="user_cli")
    except Exception as exc:
        _fail(exc, json_mode)
    _emit({"schema_version": "1.0", "approval_request_id": request_id, "state": "APPROVED", "grant": granted}, json_mode,
          lambda: _console().print(f"[green]Approved[/green] {request_id} (single use). Run `harness resume <run-id>` to continue."))
    raise typer.Exit(0)


def cmd_deny(
    request_id: Annotated[str, typer.Argument(help="Approval request ID")],
    reason: Annotated[str, typer.Option("--reason", help="Why the action is denied")] = "denied by user",
    json_mode: JsonOpt = False,
) -> None:
    """Deny an approval request; the coder receives the denial as feedback."""
    stack = build_stack()
    if request_id.startswith("capreq_"):
        from .approvals.capabilities import CapabilityError, CapabilityService, KernelPrincipal

        try:
            CapabilityService(stack.run_store, stack.artifact_store).deny(request_id, KernelPrincipal.interactive(), reason)
        except CapabilityError as exc:
            _fail(ValueError(f"{exc.code}: {exc}"), json_mode)
        _emit({"schema_version": "1.0", "capability_request_id": request_id, "state": "DENIED"}, json_mode,
              lambda: _console().print(f"[yellow]Denied[/yellow] {request_id}."))
        raise typer.Exit(0)
    try:
        denied = stack.services.actions.approvals.deny(request_id, reason)
    except Exception as exc:
        _fail(exc, json_mode)
    _emit({"schema_version": "1.0", "approval_request_id": request_id, "state": "DENIED", "result": denied}, json_mode,
          lambda: _console().print(f"[yellow]Denied[/yellow] {request_id}."))
    raise typer.Exit(0)


# ------------------------------------------------------------------ policy
@policy_app.command("show")
def cmd_policy_show(run_id: Annotated[str, typer.Argument(help="Run ID")], json_mode: JsonOpt = False) -> None:
    """Show the active immutable policy snapshot for a run."""
    stack = build_stack(run_id)
    snapshot = stack.services.policy.active_snapshot(run_id)
    if snapshot is None:
        payload = {"schema_version": "1.0", "run_id": run_id, "policy": None, "profile_default": stack.config.permission_profile}
    else:
        payload = {"schema_version": "1.0", "run_id": run_id, "policy": snapshot.contract.model_dump(mode="json")}
    _emit(payload, json_mode)
    raise typer.Exit(0)


# ----------------------------------------------------------------- sandbox
@sandbox_app.command("doctor")
def cmd_sandbox_doctor(
    profile: Annotated[str, typer.Option("--profile", help="Runtime profile")] = "python-default",
    json_mode: JsonOpt = False,
) -> None:
    """Check Docker availability, the pinned runtime image, and tool-library integrity."""
    stack = build_stack()
    backend, resolver = stack.services.backend, stack.services.runtime_resolver
    ok, version, error = backend.availability()
    checks: List[Dict[str, Any]] = [{"name": "engine_available", "passed": ok, "detail": version or error}]
    runtime_ok = False
    detail: Any = None
    if ok:
        try:
            runtime = resolver.resolve(profile)
            runtime_ok = True
            detail = {"image_id": runtime.image_id, "architecture": runtime.architecture, "fingerprint": runtime.fingerprint[:16], "user": runtime.user}
        except Exception as exc:
            detail = f"{getattr(exc, 'code', type(exc).__name__)}: {exc}"
    checks.append({"name": "runtime_image_pinned_and_verified", "passed": runtime_ok, "detail": detail})
    if ok:
        import uuid as _uuid

        probe_net = f"{backend.SETUP_NETWORK_PREFIX}doctor-{_uuid.uuid4().hex[:8]}"
        try:
            backend.network_create_internal(probe_net, {"org.dobby.harness": "1"})
            internal = (backend.network_inspect(probe_net) or {}).get("Internal") is True
            detail_net: Any = "internal no-route network available; setup egress limited to the PyPI allowlist proxy"
        except Exception as exc:
            internal, detail_net = False, f"NETWORK_POLICY_UNAVAILABLE: {str(exc)[:200]}"
        finally:
            backend.network_remove(probe_net)
        checks.append({"name": "dependency_setup_scoped_network", "passed": internal or not stack.config.dependency_setup,
                       "detail": detail_net if stack.config.dependency_setup else "dependency setup disabled (offline only)"})
    healthy = all(check["passed"] for check in checks)
    payload = {"schema_version": "1.0", "profile": profile, "status": "READY" if healthy else "BLOCKED_ENVIRONMENT", "checks": checks,
               "host_fallback": False}

    def render() -> None:
        for check in checks:
            mark = "[green]✓[/green]" if check["passed"] else "[red]✗[/red]"
            _console().print(f"{mark} {check['name']}: {_safe(json.dumps(check['detail']) if not isinstance(check['detail'], str) else check['detail'], 300)}")
        if not healthy:
            _console().print("Fix: start Docker, then `harness sandbox build`. The harness never runs model code on the host.")

    _emit(payload, json_mode, render)
    raise typer.Exit(0 if healthy else 3)


@sandbox_app.command("probe")
def cmd_sandbox_probe(
    profile: Annotated[str, typer.Option("--profile", help="Runtime profile")] = "python-default",
    json_mode: JsonOpt = False,
) -> None:
    """Run the isolation probe inside a fresh container (non-root, no network, no socket, no secrets...)."""
    from .sandbox.probe import run_probe

    stack = build_stack()
    try:
        report = run_probe(stack.services.backend, stack.services.runtime_resolver, stack.config.data_dir / "runs" / "_probe")
    except Exception as exc:
        _fail(exc, json_mode)

    def render() -> None:
        for check in report.get("checks", []):
            mark = "[green]✓[/green]" if check["passed"] else "[red]✗[/red]"
            _console().print(f"{mark} {check['name']}: {_safe(json.dumps(check['observed']), 200)}")
        _console().print("[bold green]Sandbox isolation verified[/bold green]" if report["ok"] else "[bold red]Sandbox isolation check FAILED[/bold red]")

    _emit(report, json_mode, render)
    raise typer.Exit(0 if report.get("ok") else 3)


@sandbox_app.command("build")
def cmd_sandbox_build(json_mode: JsonOpt = False) -> None:
    """Build the pinned runtime image and write the per-machine runtime lock."""
    stack = build_stack()
    try:
        lock = stack.services.runtime_resolver.build()
    except Exception as exc:
        _fail(exc, json_mode)
    _emit(lock, json_mode, lambda: _console().print(f"[green]Runtime image built[/green] {lock['image_id']} ({lock['architecture']})"))
    raise typer.Exit(0)


# --------------------------------------------------------------- workspace
@workspace_app.command("status")
def cmd_workspace_status(
    run_id: Annotated[str, typer.Argument(help="Run ID")],
    task: Annotated[Optional[str], typer.Option("--task", help="Task ID (defaults to the active task)")] = None,
    json_mode: JsonOpt = False,
) -> None:
    """Show the private task workspace version chain and live integrity."""
    stack = build_stack(run_id)
    task_id = task or _active_task(stack, run_id)
    workspaces = stack.services.workspaces
    record = workspaces.get(run_id, task_id)
    if not record:
        _fail(ValueError(f"Task {task_id} has no private workspace yet"), json_mode, run_id)
    versions = workspaces.versions(run_id, task_id)
    integrity = "VERIFIED"
    try:
        workspaces.verify_current(run_id, task_id)
    except Exception as exc:
        integrity = f"DIVERGED: {str(exc)[:200]}"
    candidate = workspaces.latest_candidate(run_id, task_id)
    decided = _row(stack, "SELECT status FROM h_completion_decisions WHERE candidate_id = ?", (candidate["candidate_id"],)) if candidate else None
    candidate_status = decided["status"] if decided else (candidate["verification_status"] if candidate else None)
    payload = {
        "schema_version": "1.0",
        "run_id": run_id,
        "task_id": task_id,
        "state": record["state"],
        "task_start_commit": record["task_start_commit"],
        "integrity": integrity,
        "versions": [version.contract() for version in versions][-50:],
        "latest_candidate": {"candidate_id": candidate["candidate_id"], "candidate_commit": candidate["candidate_commit"],
                             "verification": candidate_status} if candidate else None,
    }

    def render() -> None:
        console = _console()
        console.print(f"Task {task_id}  workspace {record['state']}  integrity {integrity}")
        for version in versions[-15:]:
            console.print(f"  {version.version_id}  {_short(version.commit)}  {version.state}  by {version.created_by_action_id or 'start'}")
        if candidate:
            console.print(f"  candidate {candidate['candidate_id']} {_short(candidate['candidate_commit'])} verification {candidate_status}")

    _emit(payload, json_mode, render)
    raise typer.Exit(0)


def _active_task(stack: Stack, run_id: str) -> str:
    row = _row(stack, "SELECT active_task_id FROM h_run_lifecycle WHERE run_id = ?", (run_id,))
    if row:
        return row["active_task_id"]
    tasks = stack.run_store.get_tasks(run_id)
    if not tasks:
        raise ValueError(f"Run {run_id} has no tasks")
    return tasks[0]["task_id"]


# ------------------------------------------------------------ verification
@verification_app.command("contract")
def cmd_verification_contract(
    run_id: Annotated[str, typer.Argument(help="Run ID")],
    task: Annotated[Optional[str], typer.Option("--task")] = None,
    json_mode: JsonOpt = False,
) -> None:
    """Show the frozen verification contract for a task."""
    stack = build_stack(run_id)
    task_id = task or _active_task(stack, run_id)
    row = stack.services.verifier.contract_row(run_id, task_id)
    if not row:
        _fail(ValueError(f"No verification contract for {task_id}"), json_mode, run_id)
    contract = stack.services.verifier.load_contract(row)

    def render() -> None:
        console = _console()
        console.print(f"Contract {contract.contract_id} ({row['state']}) for {task_id}; baseline {_short(contract.baseline.commit)}")
        for check in contract.checks:
            console.print(f"  {check.check_id:<18} {check.tier:<9} required={check.required} {_safe(' '.join(check.argv), 90)}")
        for criterion in contract.criteria:
            console.print(f"  criterion {criterion.criterion_id}: {_safe(criterion.statement, 90)}")

    _emit(contract, json_mode, render)
    raise typer.Exit(0)


@verification_app.command("baseline")
def cmd_verification_baseline(
    run_id: Annotated[str, typer.Argument(help="Run ID")],
    task: Annotated[Optional[str], typer.Option("--task")] = None,
    json_mode: JsonOpt = False,
) -> None:
    """Show baseline check results captured on the task start commit."""
    stack = build_stack(run_id)
    task_id = task or _active_task(stack, run_id)
    capture = _row(stack, "SELECT * FROM h_baseline_captures WHERE run_id = ? AND task_id = ? ORDER BY created_at DESC LIMIT 1", (run_id, task_id))
    runs = _rows(stack, """
        SELECT v.external_check_id, b.status, b.exit_code, b.discovered_count, b.passed_count, b.failed_count, b.error_count, b.baseline_commit
        FROM h_baseline_runs b JOIN h_verification_checks v ON v.verification_check_id = b.verification_check_id
        WHERE b.run_id = ? AND b.task_id = ? ORDER BY v.ordinal, b.attempt_no""", (run_id, task_id))
    payload = {"schema_version": "1.0", "run_id": run_id, "task_id": task_id, "capture_state": capture["state"] if capture else "NOT_CAPTURED", "runs": runs}
    _emit(payload, json_mode, lambda: [_console().print(f"  {r['external_check_id']:<18} {r['status']:<32} {r['passed_count']}/{r['discovered_count']} passed") for r in runs] or _console().print("No baseline captured."))
    raise typer.Exit(0)


@verification_app.command("checks")
def cmd_verification_checks(attempt_id: Annotated[str, typer.Argument(help="Verification attempt ID")], json_mode: JsonOpt = False) -> None:
    """List check runs of a verification attempt."""
    stack = build_stack()
    runs = _rows(stack, """
        SELECT r.check_run_id, COALESCE(v.external_check_id, 'overlay') AS check_id, r.run_number, r.status, r.exit_code,
               r.discovered_count, r.passed_count, r.failed_count, r.error_count, r.elapsed_ms
        FROM h_check_runs r LEFT JOIN h_verification_checks v ON v.verification_check_id = r.verification_check_id
        WHERE r.attempt_id = ? ORDER BY r.settled_at""", (attempt_id,))
    _emit({"schema_version": "1.0", "attempt_id": attempt_id, "check_runs": runs}, json_mode,
          lambda: [_console().print(f"  {r['check_run_id']}  {r['check_id']:<18} #{r['run_number']} {r['status']:<14} {r['passed_count']}/{r['discovered_count']}") for r in runs] or _console().print("No check runs."))
    raise typer.Exit(0)


@verification_app.command("logs")
def cmd_verification_logs(
    check_run_id: Annotated[str, typer.Argument(help="Check run ID")],
    stream: Annotated[str, typer.Option("--stream")] = "stdout",
) -> None:
    """Print a check run's captured output (sanitized)."""
    stack = build_stack()
    if stream not in ("stdout", "stderr"):
        _fail(ValueError("--stream must be stdout or stderr"), False)
    row = _row(stack, "SELECT stdout_artifact_id, stderr_artifact_id FROM h_check_runs WHERE check_run_id = ?", (check_run_id,))
    if not row:
        row = _row(stack, "SELECT stdout_artifact_id, stderr_artifact_id FROM h_baseline_runs WHERE baseline_run_id = ?", (check_run_id,))
    if not row:
        _fail(ValueError(f"Unknown check run {check_run_id}"), False)
    sys.stdout.write(sanitize_terminal_text((_artifact_bytes(stack, row[f"{stream}_artifact_id"]) or b"").decode("utf-8", "replace")))
    raise typer.Exit(0)


@verification_app.command("compare")
def cmd_verification_compare(attempt_id: Annotated[str, typer.Argument(help="Verification attempt ID")], json_mode: JsonOpt = False) -> None:
    """Show the baseline-vs-candidate regression comparison of an attempt."""
    stack = build_stack()
    row = _row(stack, "SELECT * FROM h_regression_comparisons WHERE attempt_id = ?", (attempt_id,))
    if not row:
        _fail(ValueError(f"No comparison for attempt {attempt_id}"), json_mode)
    comparison = _artifact_json(stack, row["comparison_artifact_id"])
    _emit(comparison, json_mode, lambda: _console().print(
        f"resolved targets {row['resolved_target_count']} | new regressions {row['new_regression_count']} | "
        f"coverage lost {row['coverage_lost_count']} | inconclusive {row['inconclusive_count']}"))
    raise typer.Exit(0)


@validator_app.command("review")
def cmd_validator_review(attempt_id: Annotated[str, typer.Argument(help="Verification attempt ID")], json_mode: JsonOpt = False) -> None:
    """Show the independent validator review recorded for an attempt."""
    stack = build_stack()
    row = _row(stack, "SELECT * FROM h_validator_reviews WHERE attempt_id = ?", (attempt_id,))
    if not row:
        _fail(ValueError(f"No validator review for attempt {attempt_id}"), json_mode)
    review = _artifact_json(stack, row["review_artifact_id"]) or {}
    payload = {"schema_version": "1.0", "attempt_id": attempt_id, "decision": row["decision"], "skip_reason": row["skip_reason"],
               "blocking_findings": row["blocking_finding_count"], "review": review}
    _emit(payload, json_mode, lambda: _console().print(f"Validator: {row['decision']} ({row['blocking_finding_count']} blocking findings){' skip: ' + row['skip_reason'] if row['skip_reason'] else ''}"))
    raise typer.Exit(0)


def cmd_verify(
    run_id: Annotated[str, typer.Argument(help="Run ID")],
    candidate: Annotated[str, typer.Option("--candidate", help="Candidate ID")],
    json_mode: JsonOpt = False,
) -> None:
    """Verify a frozen candidate (or show its recorded completion decision)."""
    stack = build_stack(run_id)
    record = stack.services.workspaces.get_candidate(candidate)
    if not record or record["run_id"] != run_id:
        _fail(ValueError(f"Unknown candidate {candidate} for run {run_id}"), json_mode, run_id)
    decision = _row(stack, "SELECT * FROM h_completion_decisions WHERE candidate_id = ?", (candidate,))
    if decision is None:
        lifecycle = _row(stack, "SELECT state, active_task_id FROM h_run_lifecycle WHERE run_id = ?", (run_id,))
        if not lifecycle or lifecycle["active_task_id"] != record["task_id"] or lifecycle["state"] not in ("VERIFICATION_REQUIRED", "VERIFYING"):
            _fail(ValueError("Candidate is not awaiting verification; it was superseded or its task is not active"), json_mode, run_id)
        continue_with_execution(run_id, "complete", json_mode)
    payload = json.loads(decision["decision_json"])
    _emit(payload, json_mode, lambda: _console().print(f"Candidate {candidate}: [bold]{decision['status']}[/bold] {', '.join(json.loads(decision['reason_codes_json'])[:8])}"))
    raise typer.Exit(release_exit_code(decision["status"]))


def cmd_repair(
    run_id: Annotated[str, typer.Argument(help="Run ID")],
    task: Annotated[Optional[str], typer.Option("--task")] = None,
    json_mode: JsonOpt = False,
) -> None:
    """Show repair attempts and the latest host-generated repair feedback for a task."""
    stack = build_stack(run_id)
    task_id = task or _active_task(stack, run_id)
    attempts = _rows(stack, "SELECT repair_attempt_id, repair_number, trigger_code, state, from_candidate_id, resulting_candidate_id FROM h_repair_attempts WHERE task_id = ? ORDER BY repair_number", (task_id,))
    feedback = stack.services.verifier.latest_repair_feedback(run_id, task_id)
    payload = {"schema_version": "1.0", "run_id": run_id, "task_id": task_id, "repair_attempts": attempts, "latest_feedback": feedback}
    _emit(payload, json_mode, lambda: [_console().print(f"  repair #{a['repair_number']} {a['trigger_code']} {a['state']}") for a in attempts] or _console().print("No repairs were needed."))
    raise typer.Exit(0)


def cmd_report(
    run_id: Annotated[str, typer.Argument(help="Run ID")],
    task: Annotated[Optional[str], typer.Option("--task", help="Task ID (omit for the whole-run report)")] = None,
    json_mode: JsonOpt = False,
) -> None:
    """Show the truthful task verification report or the whole-run queue result."""
    stack = build_stack(run_id)
    _require_run(stack, run_id)
    if task is None:
        final = stack.coordinator.final_result(run_id)
        if final is not None:
            _emit(final, json_mode, lambda: _render_final(stack, final))
            raise typer.Exit(release_exit_code(final.status))
        task = _active_task(stack, run_id)
    outcome = stack.services.verifier.task_outcome(task)
    if not outcome:
        _fail(ValueError(f"Task {task} has no settled outcome yet"), json_mode, run_id)
    report = _artifact_json(stack, outcome["report_artifact_id"]) or {}
    payload = {"schema_version": "1.0", "run_id": run_id, "task_id": task, "status": outcome["status"],
               "accepted_candidate_id": outcome["accepted_candidate_id"], "best_partial_candidate_id": outcome["best_partial_candidate_id"],
               "report": report}

    def render() -> None:
        console = _console()
        console.print(f"Task {task}: [bold]{outcome['status']}[/bold]")
        for check in (report.get("checks") or [])[:20]:
            if isinstance(check, dict):
                verdict = check.get("verdict") if isinstance(check.get("verdict"), dict) else {}
                note = ""
                if verdict.get("pre_existing_unchanged") and not verdict.get("new_regressions"):
                    note = f" (only pre-existing failures: {len(verdict['pre_existing_unchanged'])})"
                if verdict.get("resolved_targets"):
                    note += f" resolved {len(verdict['resolved_targets'])} target(s)"
                console.print(f"  {check.get('check_id', '?'):<18} run {check.get('status', '?'):<6} verdict {verdict.get('verdict', '?')}{note}")
        for limitation in (report.get("limitations") or [])[:10]:
            console.print(f"  limitation: {_safe(limitation, 150)}")

    _emit(payload, json_mode, render)
    raise typer.Exit(release_exit_code(outcome["status"]))


def cmd_resume(run_id: Annotated[str, typer.Argument(help="Run ID")], json_mode: JsonOpt = False) -> None:
    """Reconcile interrupted work exactly once, then continue the run to its final result."""
    stack = build_stack(run_id)
    try:
        _require_run(stack, run_id)
        stack.services.actions.reconcile(run_id)
        stack.coordinator.request_resume(run_id) if stack.coordinator.queue(run_id) else None
    except Exception as exc:
        _fail(exc, json_mode, run_id)
    has_lifecycle = _row(stack, "SELECT 1 AS present FROM h_run_lifecycle WHERE run_id = ?", (run_id,))
    if has_lifecycle and not stack.coordinator.queue(run_id):
        continue_with_execution(run_id, "complete", json_mode)
    run_pipeline(run_id, json_mode, stack)


def cmd_recover(
    run_id: Annotated[str, typer.Argument(help="Run ID")],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Report what recovery would do without changing anything")] = False,
    json_mode: JsonOpt = False,
) -> None:
    """Reconcile unsettled actions and journaled ref operations; never guesses."""
    stack = build_stack(run_id)
    _require_run(stack, run_id)
    actions = stack.services.actions.unsettled(run_id)
    queue = stack.coordinator.queue(run_id)
    ref_ops = stack.coordinator.journal.unsettled(queue["queue_id"]) if queue else []
    report: Dict[str, Any] = {
        "schema_version": "1.0",
        "run_id": run_id,
        "dry_run": dry_run,
        "unsettled_actions": [a["action_id"] for a in actions],
        "unsettled_ref_operations": [{"id": op["ref_operation_id"], "kind": op["operation_kind"], "state": op["state"]} for op in ref_ops],
    }
    if not dry_run:
        try:
            report["action_reconciliation"] = stack.services.actions.reconcile(run_id)
            if queue:
                report["ref_operations_certain"] = stack.coordinator._reconcile_ref_operations(queue)
        except Exception as exc:
            _fail(exc, json_mode, run_id)
        stack.artifact_store.write_json(run_id, f"prd5/recovery/recovery-{len(stack.artifact_store.get_artifacts(run_id))}.json", report, "recovery_report")
    uncertain = any(item.get("outcome") == "UNKNOWN" for item in report.get("action_reconciliation", [])) or report.get("ref_operations_certain") is False
    _emit(report, json_mode, lambda: _console().print(
        f"{'Would reconcile' if dry_run else 'Reconciled'} {len(actions)} action(s) and {len(ref_ops)} ref operation(s)"
        + ("; [red]uncertainty remains[/red]" if uncertain else "")))
    raise typer.Exit(7 if uncertain else 0)


# ------------------------------------------------------------------- queue
def _queue_or_fail(stack: Stack, run_id: str, json_mode: bool) -> Dict[str, Any]:
    queue = stack.coordinator.queue(run_id)
    if not queue:
        _fail(ValueError(f"Run {run_id} has no queue; run `harness queue plan {run_id}`"), json_mode, run_id)
    return queue


@queue_app.command("plan")
def cmd_queue_plan(run_id: Annotated[str, typer.Argument(help="Run ID")], json_mode: JsonOpt = False) -> None:
    """Classify tasks, validate the dependency DAG, and freeze the queue plan."""
    stack = build_stack(run_id)
    try:
        plan = stack.coordinator.prepare(run_id)
    except Exception as exc:
        _fail(exc, json_mode, run_id)

    def render() -> None:
        console = _console()
        console.print(f"Queue plan {plan.queue_version_id}: [bold]{plan.state}[/bold] ({len(plan.items)} items, {len(plan.edges)} edges)")
        for item in plan.items:
            console.print(f"  {item.ordinal + 1}. {item.task_id} {item.classification} {' '.join(item.reason_codes)}")

    _emit(plan, json_mode, render)
    raise typer.Exit(2 if plan.state == "INVALID" else 0)


@queue_app.command("start")
def cmd_queue_start(run_id: Annotated[str, typer.Argument(help="Run ID")], json_mode: JsonOpt = False) -> None:
    """Run the queue to a settled final result (or a pause/approval boundary)."""
    run_pipeline(run_id, json_mode)


@queue_app.command("status")
def cmd_queue_status(run_id: Annotated[str, typer.Argument(help="Run ID")], json_mode: JsonOpt = False) -> None:
    """Compact queue status: integration head, counts, budget, and items."""
    stack = build_stack(run_id)
    _queue_or_fail(stack, run_id, json_mode)
    progress = stack.coordinator.progress(run_id)
    _emit(progress, json_mode, lambda: _render_progress(stack, run_id, progress))
    raise typer.Exit(0)


@queue_app.command("show")
def cmd_queue_show(
    run_id: Annotated[str, typer.Argument(help="Run ID")],
    graph: Annotated[bool, typer.Option("--graph", help="Include dependency edges")] = False,
    artifacts: Annotated[bool, typer.Option("--artifacts", help="Include queue artifacts")] = False,
    json_mode: JsonOpt = False,
) -> None:
    """Full queue detail: items with reasons, edges, integration heads, and results."""
    stack = build_stack(run_id)
    queue = _queue_or_fail(stack, run_id, json_mode)
    items = stack.coordinator.items(queue)
    payload: Dict[str, Any] = {
        "schema_version": "1.0",
        "queue": {k: queue[k] for k in ("queue_id", "run_id", "mode", "state", "active_version_id")},
        "items": [{k: item[k] for k in ("queue_item_id", "task_id", "ordinal", "priority", "classification", "state")}
                  | {"reasons": json.loads(item["reason_codes_json"] or "[]")} for item in items],
        "integration_heads": [{k: h[k] for k in ("sequence", "commit_oid", "source_kind", "created_at")} for h in stack.coordinator.heads(queue["queue_id"])],
    }
    if graph:
        payload["edges"] = [{"predecessor": e.predecessor, "dependent": e.dependent, "source": e.source} for e in stack.coordinator.edges(queue)]
    if artifacts:
        payload["artifacts"] = [{k: a[k] for k in ("artifact_id", "kind", "relative_path", "sha256")} for a in stack.artifact_store.get_artifacts(run_id) if a["relative_path"].split("/artifacts/")[-1].startswith("prd5/")]
    _emit(payload, json_mode)
    raise typer.Exit(0)


@queue_app.command("pause")
def cmd_queue_pause(run_id: Annotated[str, typer.Argument(help="Run ID")]) -> None:
    """Stop admitting new tasks; the active unit settles first."""
    stack = build_stack(run_id)
    _queue_or_fail(stack, run_id, False)
    stack.coordinator.request_pause(run_id)
    _console().print("Pause requested; no new task will start.")
    raise typer.Exit(0)


@queue_app.command("resume")
def cmd_queue_resume(run_id: Annotated[str, typer.Argument(help="Run ID")], json_mode: JsonOpt = False) -> None:
    """Clear a pause request and continue the queue."""
    stack = build_stack(run_id)
    _queue_or_fail(stack, run_id, json_mode)
    stack.coordinator.request_resume(run_id)
    run_pipeline(run_id, json_mode, stack)


@queue_app.command("skip")
def cmd_queue_skip(
    run_id: Annotated[str, typer.Argument(help="Run ID")],
    task: Annotated[str, typer.Option("--task", help="Task ID to skip")],
    reason: Annotated[str, typer.Option("--reason", help="Reason code")] = "USER_SKIPPED",
) -> None:
    """Skip an unstarted task (dependents become blocked)."""
    stack = build_stack(run_id)
    _queue_or_fail(stack, run_id, False)
    try:
        stack.coordinator.skip(run_id, task, reason)
    except Exception as exc:
        _fail(exc, False, run_id)
    _console().print(f"Skipped {task}.")
    raise typer.Exit(0)


@queue_app.command("cancel")
def cmd_queue_cancel(run_id: Annotated[str, typer.Argument(help="Run ID")]) -> None:
    """Request cancellation; retained work is reported, never deleted."""
    stack = build_stack(run_id)
    _queue_or_fail(stack, run_id, False)
    stack.coordinator.request_cancel(run_id)
    _console().print("Cancellation requested. Run `harness queue start` (or resume) to settle and report.")
    raise typer.Exit(0)


# -------------------------------------------------------------------- task
@task_app.command("inspect")
def cmd_task_inspect(
    run_id: Annotated[str, typer.Argument(help="Run ID")],
    task_id: Annotated[str, typer.Argument(help="Task ID")],
    json_mode: JsonOpt = False,
) -> None:
    """Everything recorded for one task: plan, actions, candidates, verification, integration."""
    stack = build_stack(run_id)
    spec_row = _row(stack, "SELECT task_spec_json FROM h_tasks WHERE task_id = ? AND run_id = ?", (task_id, run_id))
    if not spec_row:
        _fail(ValueError(f"Task {task_id} is not part of run {run_id}"), json_mode, run_id)
    spec = json.loads(spec_row["task_spec_json"])
    payload = {
        "schema_version": "1.0",
        "run_id": run_id,
        "task_id": task_id,
        "title": spec.get("title"),
        "source_key": spec.get("source_key"),
        "lifecycle": _row(stack, "SELECT state, task_revision FROM h_task_lifecycle WHERE task_id = ?", (task_id,)),
        "plans": _rows(stack, "SELECT plan_id, plan_revision, state FROM h_plans WHERE task_id = ? ORDER BY plan_revision", (task_id,)),
        "actions": _rows(stack, """SELECT a.action_id, a.state, r.settlement, r.exit_code FROM h_actions a
                                   LEFT JOIN h_execution_results r ON r.action_id = a.action_id WHERE a.task_id = ? ORDER BY a.created_at""", (task_id,)),
        "candidates": _rows(stack, "SELECT candidate_id, candidate_commit, task_start_commit, verification_status FROM h_candidate_snapshots WHERE task_id = ? ORDER BY candidate_ordinal", (task_id,)),
        "verification_attempts": _rows(stack, "SELECT attempt_id, candidate_id, attempt_number, state FROM h_verification_attempts WHERE task_id = ? ORDER BY attempt_number", (task_id,)),
        "decisions": _rows(stack, "SELECT d.completion_decision_id, d.candidate_id, d.status, d.reason_codes_json FROM h_completion_decisions d JOIN h_verification_attempts a ON a.attempt_id = d.attempt_id WHERE a.task_id = ?", (task_id,)),
        "outcome": stack.services.verifier.task_outcome(task_id) if stack.services.verifier else None,
        "executions": _rows(stack, "SELECT task_execution_id, attempt_number, integration_sequence_at_start, start_commit_oid, state, outcome FROM h_task_executions WHERE task_id = ?", (task_id,)),
        "task_commits": _rows(stack, "SELECT task_commit_id, commit_oid, parent_oid, verification_state FROM h_task_commits WHERE task_id = ?", (task_id,)),
    }
    _emit(payload, json_mode)
    raise typer.Exit(0)


# --------------------------------------------------------------------- git
@git_app.command("graph")
def cmd_git_graph(
    run_id: Annotated[str, typer.Argument(help="Run ID")],
    max_count: Annotated[int, typer.Option("--max-count", help="Maximum commits")] = 50,
    json_mode: JsonOpt = False,
) -> None:
    """Show the private integration history and managed refs (never the original repository)."""
    stack = build_stack(run_id)
    _require_run(stack, run_id)
    git = stack.services.workspaces.git(run_id)
    source = stack.run_store.get_source_snapshot(run_id)
    refs = git.list_refs(f"refs/harness/runs/{run_id}/")
    integration = git.read_ref(stack.coordinator.integration_ref(run_id))
    commits: List[Dict[str, Any]] = []
    cursor = integration
    while cursor and len(commits) < max(1, min(max_count, 500)):
        message = git.commit_message(cursor)
        parents = git.commit_parents(cursor)
        commits.append({"commit": cursor, "parents": parents, "subject": message.splitlines()[0] if message else ""})
        if cursor == source["baseline_commit"]:
            break
        cursor = parents[0] if parents else None
    payload = {"schema_version": "1.0", "run_id": run_id, "baseline": source["baseline_commit"], "integration": integration,
               "commits": commits, "refs": [{"ref": name, "target": target} for name, target in sorted(refs.items())][:500]}

    def render() -> None:
        console = _console()
        for commit in commits:
            marker = " (baseline)" if commit["commit"] == source["baseline_commit"] else ""
            console.print(f"* {_short(commit['commit'])} {_safe(commit['subject'], 80)}{marker}")
        console.print(f"[dim]{len(refs)} managed refs under refs/harness/runs/{run_id}/[/dim]")

    _emit(payload, json_mode, render)
    raise typer.Exit(0)
