"""PRD 6 release commands: headless evaluator entry, export, doctor, cleanup, replay,
plugins, version, release evidence, and the P1 effect commands (disabled).

``--json`` prints exactly one JSON object on stdout; progress goes to stderr.
Exit codes follow the canonical release table (``harness.release.status_mapping``).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Any, Dict, Optional, Tuple

import typer

from .config import HarnessConfig, get_config
from .release.status_mapping import exit_code

JsonOpt = Annotated[bool, typer.Option("--json", help="Emit exactly one JSON object to stdout")]
REPOSITORY_WORDS = {"all", "all issues", "all open issues", "repository", "repo", "whole repository", "whole repo", "--all"}

plugins_app = typer.Typer(help="Reviewed pinned plugin registry (PRD 6)")
release_app = typer.Typer(help="Release qualification evidence (PRD 6)")


def _console():
    from .cli import _err_console

    return _err_console


def _emit(payload: Any, json_mode: bool, render=None) -> None:
    data = payload.model_dump(mode="json") if hasattr(payload, "model_dump") else payload
    if json_mode:
        sys.stdout.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
        sys.stdout.flush()
    elif render is not None:
        render()
    else:
        _console().print_json(json.dumps(data, default=str))


def _typed_error(code: str, message: str, json_mode: bool, status: str = "INVALID", extra: Optional[Dict[str, Any]] = None) -> None:
    payload = {"schema_version": "1.0", "status": status, "error": {"code": code, "message": message}, **(extra or {})}
    _emit(payload, json_mode, lambda: _console().print(f"[bold red]{status}[/bold red] [{code}] {message}"))
    raise typer.Exit(exit_code(status))


def _stack(run_id: Optional[str] = None):
    from .cli_execution import build_stack

    return build_stack(run_id)


# ============================================================ headless evaluator
def run_evaluator_request(input_path: str) -> None:
    """``harness run --input request.json --non-interactive``: one JSON object on stdout."""
    from .evaluator.gateway import EvaluatorGateway, minimal_result, write_result_atomic
    from .evaluator.interface import EvaluatorInputError
    from . import cli_execution

    config = get_config()
    gateway = EvaluatorGateway(config, adapter_factory=cli_execution.ADAPTER_FACTORY,
                               log=lambda message: _console().print(f"[dim]{message}[/dim]"))
    request = None
    try:
        raw, request = gateway.load_request(input_path)
        result = gateway.execute(raw, request)
    except EvaluatorInputError as exc:
        result = minimal_result("INVALID", code=exc.code, message=str(exc), request_id=getattr(request, "request_id", None))
    except KeyboardInterrupt:
        result = minimal_result("CANCELLED", code="CANCELLED_BY_USER", message="Cancelled by the user; the run is resumable",
                                request_id=getattr(request, "request_id", None))
    except Exception as exc:  # never a traceback on stdout
        result = minimal_result("INTERNAL_ERROR", code=getattr(exc, "code", type(exc).__name__), message=str(exc)[:1000],
                                request_id=getattr(request, "request_id", None))
    target = _safe_result_path(input_path, config)
    if target:
        try:
            write_result_atomic(result, target)
        except OSError as exc:
            _console().print(f"[red]Could not write result file: {exc}[/red]")
    sys.stdout.write(json.dumps(result.model_dump(mode="json"), sort_keys=True) + "\n")
    sys.stdout.flush()
    raise typer.Exit(result.exit_code)


def _safe_result_path(input_path: str, config: HarnessConfig) -> Optional[str]:
    """Where an evaluator result may be written: absolute, parent exists, and never inside the
    source repository or harness run storage (even for an INVALID request)."""
    try:
        raw = json.loads(Path(input_path).read_text(encoding="utf-8"))
        candidate = raw.get("result_path")
        locator = (raw.get("repository") or {}).get("locator")
        kind = (raw.get("repository") or {}).get("kind")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(candidate, str) or not Path(candidate).is_absolute():
        return None
    resolved = Path(candidate).resolve()
    if not resolved.parent.is_dir():
        return None
    protected = [Path(config.data_dir).resolve() / "runs"]
    if isinstance(locator, str) and kind in ("local_git", "local_folder") and Path(locator).is_absolute():
        protected.append(Path(locator).resolve())
    if any(resolved == root or root in resolved.parents for root in protected):
        return None
    return str(resolved)


# =============================================================== interactive run
def classify_target(target: str):
    """Repository URL, GitHub issue URL, local Git checkout, ordinary folder, or ZIP path."""
    from .contracts import RepositoryKind
    from .validator import InputValidator

    value = target.strip().strip('"').strip("'")
    issue = InputValidator.parse_issue_ref(value)
    if issue:
        return RepositoryKind.PUBLIC_HTTPS, f"https://github.com/{issue.owner}/{issue.repo}.git", value
    if value.startswith(("http://", "https://")):
        return RepositoryKind.PUBLIC_HTTPS, value if value.endswith(".git") else f"{value.rstrip('/')}.git", None
    path = Path(value).expanduser()
    if path.is_file() and path.suffix.lower() == ".zip":
        return RepositoryKind.LOCAL_ZIP, str(path.resolve()), None
    if path.is_dir():
        return (RepositoryKind.LOCAL_GIT if (path / ".git").exists() else RepositoryKind.LOCAL_FOLDER), str(path.resolve()), None
    parts = value.split("/")
    if len(parts) == 2 and all(parts):
        return RepositoryKind.PUBLIC_HTTPS, f"https://github.com/{parts[0]}/{parts[1]}.git", None
    raise ValueError(f"Repository not found: {value!r} (use a GitHub URL, a local folder, or a .zip path)")


# Prescribed text-only model profiles offered at launch, in menu order (DeepSeek and Qwen first).
LAUNCH_PROFILES = ("deepseek", "qwen", "qwen-coder", "groq-qwen", "deepseek-reasoner", "qwen-cn", "openrouter-deepseek")
# Unambiguous public key-format prefixes: they only preselect the menu default, never the model.
KEY_PREFIX_DEFAULTS = (("gsk_", "groq-qwen"), ("sk-or-", "openrouter-deepseek"))


def ensure_model_profile(config: HarnessConfig, non_interactive: bool) -> None:
    """Select the run's single model once, before any other prompt.

    The bundled default profile is a placeholder, so when no real profile is configured
    (``HARNESS_MODEL_PROFILE`` unset or pointing at a placeholder) the interactive launch
    asks which prescribed model to use. The answer is used for every role of the run and
    is never changed afterwards; there is no fallback. A configured profile is never
    second-guessed, and non-interactive runs fail with instructions instead of asking.
    """
    import os

    from rich.prompt import Prompt

    from . import cli_execution
    from .model import CredentialProvider, ModelProfileResolver
    from .model.profile import ModelProfileError

    if cli_execution.ADAPTER_FACTORY is not None:
        return  # an injected (test) adapter replaces the live provider: no profile or key is used
    resolver = ModelProfileResolver(config.model_profiles_path)
    try:
        resolver.validate_live(resolver.resolve(config.model_profile))
        configured = True
    except ModelProfileError as exc:
        if getattr(exc, "code", "") != "MODEL_PROFILE_INCOMPLETE":
            raise
        configured = False
    try:
        api_key = CredentialProvider().get_ai_api_key()
    except Exception as exc:
        raise ValueError("AI_API_KEY is not set: export AI_API_KEY=<provided key>, then make run") from exc
    if configured:
        return
    if non_interactive:
        raise ValueError("No model selected: set HARNESS_MODEL_PROFILE=deepseek or qwen "
                         "(or use make run MODEL=deepseek|qwen)")
    available = set(resolver.profile_ids())
    options = [profile_id for profile_id in LAUNCH_PROFILES if profile_id in available]
    console = _console()
    console.print("[bold]Model[/bold] (one text-only model for every role of this run):")
    for number, profile_id in enumerate(options, start=1):
        resolved = resolver.resolve(profile_id)
        console.print(f"  {number}. [cyan]{profile_id:<20}[/cyan] {resolved.contract.model} @ {resolved.contract.endpoint_origin}")
    suggested = next((profile_id for prefix, profile_id in KEY_PREFIX_DEFAULTS
                      if api_key.startswith(prefix) and profile_id in options), options[0])
    if suggested != options[0]:
        console.print(f"  [dim]AI_API_KEY has the {suggested} key format, so it is the default.[/dim]")
    while True:
        answer = Prompt.ask("Choose a model (number or profile name)", default=str(options.index(suggested) + 1),
                            console=console).strip()
        choice = options[int(answer) - 1] if answer.isdigit() and 1 <= int(answer) <= len(options) else answer
        if choice in options:
            break
        console.print(f"[red]Unknown choice {answer!r}[/red]")
    config.model_profile = choice
    os.environ["HARNESS_MODEL_PROFILE"] = choice
    console.print(f"  [green]✓[/green] Model profile [bold]{choice}[/bold] "
                  f"[dim](set HARNESS_MODEL_PROFILE={choice} or make run MODEL={choice} to skip this question)[/dim]\n")


def build_interactive_request(target: Optional[str], prompt: Optional[str], mode: Optional[str], execution_mode: str,
                              max_tasks: Optional[int], non_interactive: bool):
    """The two-prompt flow (FR01): 1) repository URL/folder/ZIP, 2) task text. Spaces/Unicode are data."""
    import uuid

    from rich.prompt import Prompt

    from .contracts import ExecutionMode, LimitsV1, RepositoryKind, RepositoryQueryV1, RepositoryRefV1, RunRequestV1, TaskInputV1, TaskMode

    if not target:
        if non_interactive:
            raise ValueError("--non-interactive needs --repo (or --input request.json)")
        target = Prompt.ask("[bold]Repository[/bold] (GitHub URL, local folder, or ZIP path)", default=".").strip()
    kind, locator, issue_url = classify_target(target)
    if issue_url and not prompt:
        prompt = issue_url
    if not prompt:
        if non_interactive:
            raise ValueError("--non-interactive needs --task (or --input request.json)")
        prompt = Prompt.ask("[bold]Task[/bold] (describe the problem, an issue URL or #number, or 'all open issues')").strip()
    text = prompt.strip()
    task_mode = TaskMode(mode) if mode else (TaskMode.REPOSITORY if text.lower() in REPOSITORY_WORDS else TaskMode.SINGLE_ISSUE)
    if task_mode == TaskMode.REPOSITORY:
        task_input = TaskInputV1(repository_query=RepositoryQueryV1(state="open"))
    elif text.startswith("https://github.com/") and "/issues/" in text:
        task_input = TaskInputV1(issue_url=text)
    elif text.lstrip("#").isdigit() and kind == RepositoryKind.PUBLIC_HTTPS:
        task_input = TaskInputV1(issue_url=f"{locator.removesuffix('.git')}/issues/{text.lstrip('#')}")
    else:
        task_input = TaskInputV1(text=text)
    return RunRequestV1(
        idempotency_key=f"run-{uuid.uuid4().hex[:12]}",
        task_mode=task_mode,
        execution_mode=ExecutionMode(execution_mode),
        repository=RepositoryRefV1(kind=kind, locator=locator),
        task=task_input,
        limits=LimitsV1(max_tasks=1 if task_mode == TaskMode.SINGLE_ISSUE else (max_tasks or 3)),
    )


def show_run_plan(request, config: HarnessConfig) -> None:
    from .application.composition import scaled_budget

    budget = scaled_budget(request.limits.max_tasks, max_calls=config.max_model_calls, max_wall_seconds=config.max_run_wall_seconds)
    console = _console()
    console.print(f"  Source: [cyan]{request.repository.kind.value}[/cyan] {request.repository.locator}")
    console.print(f"  Mode: {request.task_mode.value} / {request.execution_mode.value}   Model profile: [bold]{config.model_profile}[/bold]")
    console.print(f"  Permission profile: [bold]{config.permission_profile}[/bold] (actions run only in the network-less sandbox)")
    console.print(f"  Budget: {budget.max_calls} model calls, {budget.max_wall_seconds // 60} min; dependency setup: "
                  f"{'PyPI-only proxy' if config.dependency_setup else 'offline'}\n")


# ===================================================================== commands
def cmd_export(
    run_id: Annotated[str, typer.Argument(help="Run ID")],
    output: Annotated[str, typer.Option("--output", "-o", help="Bundle directory to create")],
    fmt: Annotated[str, typer.Option("--format", help="Patch format")] = "unified_git_patch_v1",
    replace: Annotated[bool, typer.Option("--replace", help="Replace an existing bundle at --output")] = False,
    json_mode: JsonOpt = False,
) -> None:
    """Write patch.diff, result.json, report.md, manifest and checksums for B -> C (round-trip checked)."""
    from .export.service import ExportError, ExportService

    if fmt != "unified_git_patch_v1":
        _typed_error("UNSUPPORTED_EXPORT_FORMAT", f"Unsupported format {fmt}", json_mode)
    stack = _stack(run_id)
    if not stack.run_store.get_run(run_id):
        _typed_error("RUN_NOT_FOUND", f"Run not found: {run_id}", json_mode)
    service = ExportService(run_store=stack.run_store, artifact_store=stack.artifact_store, services=stack.services,
                            coordinator=stack.coordinator, data_root=stack.config.data_dir)
    try:
        outcome = service.export(run_id, output, replace=replace)
    except ExportError as exc:
        _typed_error(exc.code, str(exc), json_mode, status="EXPORT_INVALID" if exc.code == "EXPORT_INVALID" else "INVALID")
    manifest = outcome.manifest
    payload = {**manifest.model_dump(mode="json"), "bundle_path": str(outcome.bundle_path), "replayed": outcome.replayed}

    def render() -> None:
        console = _console()
        colour = "green" if manifest.status == "VALID" else "red"
        console.print(f"Export [bold {colour}]{manifest.status}[/bold {colour}] -> [cyan]{outcome.bundle_path}[/cyan]"
                      + (" (recorded bundle reused)" if outcome.replayed else ""))
        console.print(f"  B {manifest.base.commit[:12]} -> C {manifest.candidate.commit[:12]}; round-trip {manifest.round_trip.status}")
        result = json.loads((outcome.bundle_path / "result.json").read_text())
        console.print(f"  Run status (authoritative): [bold]{result['status']}[/bold]")

    _emit(payload, json_mode, render)
    raise typer.Exit(0 if manifest.status == "VALID" else exit_code("EXPORT_INVALID"))


def cmd_doctor(
    profile: Annotated[str, typer.Option("--profile", help="evaluation_strict_v1 | development_sandbox_v1 | submission")] = "evaluation_strict_v1",
    live: Annotated[bool, typer.Option("--live", help="Also send one tiny probe request to the model provider")] = False,
    json_mode: JsonOpt = False,
) -> None:
    """Check Git, Python, storage, sandbox, runtime image, model config, key presence, adapter, plugins, locks."""
    from .doctor.service import DoctorService
    from .persistence import RunStore

    config = get_config()
    service = DoctorService(config)
    try:
        report, remediation = service.check(profile, live=live)
    except ValueError as exc:
        _typed_error("INVALID_PROFILE", str(exc), json_mode)
    try:
        service.persist(report, RunStore(str(Path(config.data_dir) / "harness.db")))
    except Exception:
        pass

    def render() -> None:
        console = _console()
        marks = {"PASS": "[green]✓[/green]", "FAIL": "[red]✗[/red]", "WARN": "[yellow]![/yellow]", "SKIP": "[dim]-[/dim]"}
        for check in report.checks:
            console.print(f"{marks[check.status]} {check.id:<20} {check.observed}")
            if check.id in remediation:
                console.print(f"    [dim]fix: {remediation[check.id]}[/dim]")
        colour = {"READY": "green", "WARNING": "yellow", "BLOCKED": "red"}[report.status]
        console.print(f"\nDoctor ({report.profile}): [bold {colour}]{report.status}[/bold {colour}]")

    _emit({**report.model_dump(mode="json"), "remediation": remediation}, json_mode, render)
    raise typer.Exit(0 if report.status in ("READY", "WARNING") else 2)


def cmd_clean(
    run_id: Annotated[Optional[str], typer.Argument(help="Run ID whose registered transient data to remove")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show the exact cleanup plan; change nothing")] = False,
    all_runs: Annotated[bool, typer.Option("--all", help="Developer reset: delete ALL harness runs and the database")] = False,
    force: Annotated[bool, typer.Option("--force", "-f", "--yes", "-y", help="Confirm --all without a prompt")] = False,
    json_mode: JsonOpt = False,
) -> None:
    """Remove only approved, registered run data (evidence, refs, originals, and exports are kept)."""
    if all_runs:
        from .cli import legacy_clean_all

        legacy_clean_all(force)
        return
    if not run_id:
        _typed_error("INVALID_REQUEST", "Pass a RUN_ID (or --all for a full developer reset)", json_mode)
    from .approvals.capabilities import CapabilityError, KernelPrincipal
    from .release.profiles import ensure_profile
    from .retention.cleanup import CleanupService

    stack = _stack(run_id)
    if not stack.run_store.get_run(run_id):
        _typed_error("RUN_NOT_FOUND", f"Run not found: {run_id}", json_mode)
    ensure_profile(stack.run_store, "development_sandbox_v1", "0" * 64)
    cleaner = CleanupService(run_store=stack.run_store, artifact_store=stack.artifact_store, data_root=stack.config.data_dir,
                             backend=stack.services.backend)
    try:
        plan = cleaner.plan(run_id)
    except CapabilityError as exc:
        _typed_error(exc.code, str(exc), json_mode)
    if dry_run:
        def render() -> None:
            console = _console()
            for target in plan.targets:
                console.print(f"  {target.eligibility:<16} {target.kind:<22} {target.resource_id:<32} {target.estimated_bytes} bytes")
            for item in plan.excluded:
                console.print(f"  {'EXCLUDED':<16} {item.reason:<22} {item.resource_id}")
            console.print(f"Would reclaim {plan.estimated_reclaimed_bytes} bytes (requires approval).")
        _emit(plan, json_mode, render)
        raise typer.Exit(0)
    with stack.run_store.get_connection() as conn:
        approved = conn.execute(
            """SELECT 1 FROM h_cleanup_plans p JOIN h_capability_requests c ON c.capability_request_id = p.capability_request_id
               WHERE p.run_id = ? AND c.state = 'APPROVED' AND p.state = 'PLANNED'""", (run_id,)).fetchone()
    if approved:
        try:
            report = cleaner.execute(run_id)
        except CapabilityError as exc:
            _typed_error(exc.code, str(exc), json_mode, status="CLEANUP_UNCERTAIN" if "UNCERTAIN" in exc.code else "INVALID")
        _emit(report, json_mode, lambda: _console().print(f"Cleanup [bold]{report['status']}[/bold]: removed {len(report['removed'])}, "
                                                          f"residual {len(report['residual'])}, uncertain {len(report['uncertain'])}"))
        raise typer.Exit(0 if report["status"] == "CLEANED" else exit_code(report["status"]))
    pending = cleaner.request(run_id, plan, KernelPrincipal.interactive())
    payload = {"schema_version": "1.0", "status": "PENDING_APPROVAL", "run_id": run_id, "plan": plan.model_dump(mode="json"), **pending}
    _emit(payload, json_mode, lambda: _console().print(
        f"Cleanup needs approval: [bold]{pending['capability_request_id']}[/bold]\n"
        f"  review: harness approval show {pending['capability_request_id']}\n"
        f"  approve: harness approve {pending['capability_request_id']}  then rerun: harness clean {run_id}"))
    raise typer.Exit(exit_code("PENDING_APPROVAL"))


def cmd_replay(
    run_id: Annotated[str, typer.Argument(help="Run ID")],
    mode: Annotated[str, typer.Option("--mode", help="audit | reverify | recorded | live-model")] = "audit",
    json_mode: JsonOpt = False,
) -> None:
    """Replay a run without overwriting it (audit: evidence only; reverify: fresh checks on the exact candidate)."""
    from .plugins.registry import PluginRegistry
    from .release.reproducibility import ReproducibilityService, effective_configuration

    stack = _stack(run_id)
    if not stack.run_store.get_run(run_id):
        _typed_error("RUN_NOT_FOUND", f"Run not found: {run_id}", json_mode)
    try:
        lock_sha = PluginRegistry().load_lock().lock_sha256
    except Exception:
        lock_sha = "0" * 64
    service = ReproducibilityService(run_store=stack.run_store, artifact_store=stack.artifact_store, services=stack.services,
                                     coordinator=stack.coordinator, plugin_lock_sha256=lock_sha)
    config = stack.config
    effective = effective_configuration(config, model_profile_id=config.model_profile, permission_profile=config.permission_profile,
                                        budgets={}, release_profile="development_sandbox_v1")
    from .plugins.registry import module_content_sha256

    report = service.replay(run_id, mode, effective_config=effective,
                            adapter={"name": "native_json_v1", "version": "1.0.0", "content_sha256": module_content_sha256("harness.evaluator.native_json")})
    _emit(report, json_mode, lambda: _console().print(f"Replay ({mode}): [bold]{report['state']}[/bold] "
                                                      + json.dumps({k: v for k, v in report.items() if k not in ('schema_version', 'note')}, default=str)[:600]))
    raise typer.Exit(0 if report["state"] == "PASS" else (2 if report["state"] == "BLOCKED" else 3))


def cmd_version(json_mode: JsonOpt = False) -> None:
    """Harness version, source commit, build ID, schemas, and plugin lock."""
    from .plugins.registry import PluginRegistry
    from .release.identity import harness_build

    build = harness_build()
    try:
        lock = PluginRegistry().load_lock().lock_sha256
    except Exception as exc:
        lock = f"unavailable ({getattr(exc, 'code', type(exc).__name__)})"
    payload = {"schema_version": "1.0", **build, "public_schema_versions": {"request": "1.0", "result": "1.0", "event": "1.0"},
               "evaluator_adapters": ["native_json_v1"], "plugin_set_lock_sha256": lock, "p1_effects": "disabled"}
    _emit(payload, json_mode, lambda: _console().print(f"harness {build['version']} ({build['source_commit'][:12]}, {build['build_id']})"))
    raise typer.Exit(0)


def cmd_apply(
    run_id: Annotated[str, typer.Argument(help="Run ID")],
    target: Annotated[str, typer.Option("--target", help="Original checkout to apply to")],
    json_mode: JsonOpt = False,
) -> None:
    """P1 guarded local application — disabled in this release (keep and export instead)."""
    _typed_error("CAPABILITY_DISABLED", "Local application is a P1 effect that is disabled in this release; use `harness export` "
                 "and apply patch.diff yourself (`git apply patch.diff`)", json_mode, status="CAPABILITY_DISABLED")


def cmd_publish(
    run_id: Annotated[str, typer.Argument(help="Run ID")],
    remote: Annotated[str, typer.Option("--remote", help="Remote name")],
    branch: Annotated[str, typer.Option("--branch", help="New branch name")],
    json_mode: JsonOpt = False,
) -> None:
    """P1 new-branch publication — disabled in this release (nothing is ever pushed)."""
    _typed_error("CAPABILITY_DISABLED", "Remote publication is a P1 effect that is disabled in this release; nothing is pushed",
                 json_mode, status="CAPABILITY_DISABLED")


@plugins_app.command("list")
def cmd_plugins_list(json_mode: JsonOpt = False) -> None:
    """List the pinned plugin set (slot, name, version, content hash)."""
    from .plugins.registry import PluginError, PluginRegistry

    try:
        resolved = PluginRegistry().resolve()
    except PluginError as exc:
        _typed_error(exc.code, str(exc), json_mode)
    payload = {"schema_version": "1.0", "plugin_set_id": resolved.lock.plugin_set_id, "lock_sha256": resolved.lock_sha256,
               "plugins": resolved.bindings()}
    _emit(payload, json_mode, lambda: [_console().print(f"  {b['slot']:<16} {b['name']:<36} {b['version']}  {b['content_sha256'][:12]}  "
                                                        f"self-check {b['self_check_status']}") for b in resolved.bindings()])
    raise typer.Exit(0)


@plugins_app.command("doctor")
def cmd_plugins_doctor(json_mode: JsonOpt = False) -> None:
    """Validate lock, hashes, interfaces, configuration, dependency DAG, capabilities, and self-checks."""
    from .plugins.registry import PluginError, PluginRegistry

    try:
        resolved = PluginRegistry().resolve()
    except PluginError as exc:
        _typed_error(exc.code, str(exc), json_mode, status="INVALID")
    _emit({"schema_version": "1.0", "status": "PASS", "plugins": len(resolved.plugins), "lock_sha256": resolved.lock_sha256},
          json_mode, lambda: _console().print(f"[green]Plugin set valid[/green]: {len(resolved.plugins)} reviewed plugins, lock {resolved.lock_sha256[:16]}"))
    raise typer.Exit(0)


@release_app.command("evidence")
def cmd_release_evidence(json_mode: JsonOpt = False) -> None:
    """Evaluate release gates from recorded evidence; an unexecuted gate is never PASS."""
    from .release.evidence import ReleaseEvidenceBuilder

    report = ReleaseEvidenceBuilder(get_config()).evaluate()

    def render() -> None:
        console = _console()
        marks = {"PASS": "[green]PASS[/green]", "FAIL": "[red]FAIL[/red]", "NOT_RUN": "[yellow]NOT_RUN[/yellow]",
                 "BLOCKED": "[red]BLOCKED[/red]", "NOT_APPLICABLE": "[dim]N/A[/dim]"}
        for gate in report["gates"]:
            console.print(f"  {marks[gate['status']]:<22} {gate['gate']:<24} {gate['evidence']}")
        console.print(f"\nRelease qualification: [bold]{report['status']}[/bold]")

    _emit(report, json_mode, render)
    raise typer.Exit(0 if report["status"] == "QUALIFIED" else 3)


def register(app: typer.Typer) -> None:
    app.command("export")(cmd_export)
    app.command("doctor")(cmd_doctor)
    app.command("replay")(cmd_replay)
    app.command("version")(cmd_version)
    app.command("apply")(cmd_apply)
    app.command("publish")(cmd_publish)
    app.add_typer(plugins_app, name="plugins")
    app.add_typer(release_app, name="release")
