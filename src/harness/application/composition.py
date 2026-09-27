"""Composition root: builds the trusted service graph for PRD 2-5.

Every component is constructed here from explicit, host-owned configuration.
Nothing in a target repository can select, replace, or configure these
services.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from harness.contracts.execution import PermissionProfile
from harness.execution import ActionService, ExecutionBudgetLimits
from harness.model import ModelAdapter, ModelProfileResolver
from harness.orchestration import BudgetLimits, ExecutionServices, OrchestrationController
from harness.persistence import ArtifactStore, RunStore
from harness.policy import PolicyEngine, SandboxLimits
from harness.sandbox import DEFAULT_LOCK_PATH, DockerBackend, RuntimeProfileResolver
from harness.workspace.task_workspace import TaskWorkspaceService


@dataclass
class HarnessServices:
    run_store: RunStore
    artifact_store: ArtifactStore
    data_root: Path
    backend: DockerBackend
    runtime_resolver: RuntimeProfileResolver
    policy: PolicyEngine
    workspaces: TaskWorkspaceService
    actions: ActionService
    execution: ExecutionServices
    verifier: Any = None


def build_services(
    run_store: RunStore,
    artifact_store: ArtifactStore,
    data_root: str | Path,
    *,
    permission_profile: PermissionProfile = PermissionProfile.SANDBOX,
    sandbox_limits: SandboxLimits = SandboxLimits(),
    execution_limits: ExecutionBudgetLimits = ExecutionBudgetLimits(),
    lock_path: Path = DEFAULT_LOCK_PATH,
    backend: Optional[DockerBackend] = None,
    enable_verification: bool = True,
    verification_options: Optional[Mapping[str, Any]] = None,
    dependency_setup: bool = True,
) -> HarnessServices:
    root = Path(data_root).resolve()
    backend = backend or DockerBackend(allowed_mount_roots=[root / "runs", root / "deps"])
    resolver = RuntimeProfileResolver(backend, lock_path=lock_path, limits=sandbox_limits)
    policy = PolicyEngine(run_store, limits=sandbox_limits)
    workspaces = TaskWorkspaceService(run_store, artifact_store, root)
    actions = ActionService(
        run_store=run_store,
        artifact_store=artifact_store,
        data_root=root,
        backend=backend,
        runtime_resolver=resolver,
        policy_engine=policy,
        workspaces=workspaces,
        limits=sandbox_limits,
        permission_profile=permission_profile,
    )
    verifier = None
    if enable_verification:
        from harness.verification.service import VerificationService

        verifier = VerificationService(
            run_store=run_store,
            artifact_store=artifact_store,
            data_root=root,
            backend=backend,
            runtime_resolver=resolver,
            workspaces=workspaces,
            sandbox_limits=sandbox_limits,
            **dict(verification_options or {}),
        )
    from harness.execution.dependencies import DependencyEnvironmentService

    dependencies = DependencyEnvironmentService(
        run_store=run_store,
        artifact_store=artifact_store,
        data_root=root,
        backend=backend,
        runtime_resolver=resolver,
        workspaces=workspaces,
        enabled=dependency_setup,
        runtime_row=lambda run_id: actions.ensure_runtime_row(run_id, actions.runtime()),
    )
    if verifier is not None:
        verifier.dependencies = dependencies
    execution = ExecutionServices(
        workspaces=workspaces,
        actions=actions,
        policy=policy,
        budget_limits=execution_limits,
        verifier=verifier,
        permission_profile=permission_profile,
        dependencies=dependencies,
    )
    return HarnessServices(run_store, artifact_store, root, backend, resolver, policy, workspaces, actions, execution, verifier)


def build_controller(
    services: HarnessServices,
    profile_path: str | Path,
    *,
    adapter: Optional[ModelAdapter] = None,
    budget_limits: BudgetLimits = BudgetLimits(),
    profile_id: str = "designated",
) -> OrchestrationController:
    return OrchestrationController(
        run_store=services.run_store,
        artifact_store=services.artifact_store,
        data_root=services.data_root,
        profile_resolver=ModelProfileResolver(profile_path),
        adapter=adapter,
        profile_id=profile_id,
        budget_limits=budget_limits,
        execution=services.execution,
    )


def scaled_budget(task_count: int, *, max_calls: Optional[int] = None, max_wall_seconds: Optional[int] = None) -> BudgetLimits:
    """One global model budget for the run, sized for the number of selected tasks.

    A single task keeps enough headroom for planning, several coder turns,
    validation, and two repairs. Queues scale linearly with a fixed reserve so
    final aggregate verification and reporting never starve (PRD 5 INV5-15).
    """
    count = max(1, task_count)
    calls = max_calls or min(40 * count + 4, 400)
    wall = max_wall_seconds or min(1800 * count + 300, 6 * 3600)
    return BudgetLimits(
        max_calls=calls,
        max_input_tokens=calls * 48_000,
        max_output_tokens=calls * 6_000,
        max_wall_seconds=wall,
        reserved_future_calls=2,
        reserved_future_output_tokens=min(4_000, (calls * 6_000) // 2),
        reserved_future_wall_seconds=min(180, wall // 2),
    )


def ensure_runtime(services: HarnessServices, *, auto_build: bool = True, log: Optional[Any] = None) -> Optional[str]:
    """Resolve the pinned sandbox runtime, building it once when missing.

    Returns ``None`` when the runtime is usable, otherwise a reason code. The
    harness never falls back to host execution; callers surface the code as
    ``BLOCKED_ENVIRONMENT``.
    """
    from harness.sandbox import RuntimeImageInvalidError, SandboxUnavailableError

    try:
        services.runtime_resolver.resolve()
        return None
    except SandboxUnavailableError:
        return "SANDBOX_UNAVAILABLE"
    except RuntimeImageInvalidError as exc:
        if not auto_build:
            return "RUNTIME_IMAGE_INVALID"
        if log is not None:
            log(f"Sandbox runtime not ready ({exc}); building the pinned runtime image once...")
        try:
            services.runtime_resolver.build()
            services.runtime_resolver.resolve()
            return None
        except (RuntimeImageInvalidError, SandboxUnavailableError, OSError) as build_exc:
            if log is not None:
                log(f"Runtime build failed: {build_exc}")
            return getattr(build_exc, "code", "RUNTIME_IMAGE_INVALID")
        except Exception as build_exc:  # docker build errors surface as generic failures
            if log is not None:
                log(f"Runtime build failed: {str(build_exc)[:500]}")
            return "RUNTIME_IMAGE_INVALID"
