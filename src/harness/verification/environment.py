"""Fresh, disposable verification environments (PRD 4 section 8).

Every check run starts from a new copy of an immutable commit (the baseline
``B`` or frozen candidate ``C``), never from the mutable coding workspace. The
copy is mounted into a fresh hardened container with no network. After the
check the host compares manifests; any source mutation outside declared
disposable outputs invalidates the result. The copy is always discarded.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from harness.contracts.verification import CheckStatus, ContractCheckV1
from harness.gitflow import make_writable_tree, secure_rmtree
from harness.persistence import ArtifactStore, RunStore, canonical_json
from harness.policy.limits import SandboxLimits
from harness.sandbox import (
    ContainerLimits,
    ContainerOutcome,
    ContainerSpec,
    DockerBackend,
    Mount,
    ResolvedRuntime,
)
from harness.verification.parsers import ParsedReport, parse_exit_code, parse_pytest_junit, parse_unittest
from harness.workspace.manifest import diff_manifests, is_disposable, scan_workspace
from harness.workspace.task_workspace import TaskWorkspaceService

VERIFY_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "HOME": "/tmp",
    "TMPDIR": "/tmp",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
    "PYTHONHASHSEED": "0",
    "PYTEST_ADDOPTS": "-p no:cacheprovider",
    "PIP_NO_INPUT": "1",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
}


def _sha(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass
class CheckObservation:
    status: CheckStatus
    parsed: ParsedReport
    outcome: ContainerOutcome
    environment_sha256: str
    command_sha256: str
    unexpected_mutation: bool
    mutation_paths: List[str]
    stdout_artifact_id: Optional[str]
    stderr_artifact_id: Optional[str]
    report_artifact_id: Optional[str]
    before_manifest_artifact_id: str
    after_manifest_artifact_id: Optional[str]
    stdout_excerpt: str = ""
    stderr_excerpt: str = ""
    overlay_sha256: Optional[str] = None
    container_name: str = ""


class VerificationEnvironmentFactory:
    def __init__(
        self,
        run_store: RunStore,
        artifact_store: ArtifactStore,
        backend: DockerBackend,
        workspaces: TaskWorkspaceService,
        limits: SandboxLimits,
    ) -> None:
        self.run_store = run_store
        self.artifact_store = artifact_store
        self.backend = backend
        self.workspaces = workspaces
        self.limits = limits

    def verification_root(self, run_id: str) -> Path:
        return self.workspaces.run_root(run_id) / "verification"

    def pristine(self, run_id: str, commit: str) -> Path:
        """Materialize ``commit`` once (read-only, never mounted); copies are mounted."""
        git = self.workspaces.git(run_id)
        tree = git.commit_tree_of(commit)
        target = self.verification_root(run_id) / "sources" / tree
        if target.exists():
            return target
        staging = target.with_name(target.name + f".tmp-{uuid.uuid4().hex[:8]}")
        git.materialize(tree, staging)
        os.replace(staging, target)
        return target

    def discard_pristine(self, run_id: str, commit: str) -> None:
        git = self.workspaces.git(run_id)
        target = self.verification_root(run_id) / "sources" / git.commit_tree_of(commit)
        if target.exists():
            secure_rmtree(target, self.verification_root(run_id))

    def environment_sha256(
        self,
        runtime: ResolvedRuntime,
        *,
        content_sha256: str,
        check: ContractCheckV1,
        overlay_sha256: Optional[str],
        contract_sha256: str,
        dependency_sha256: Optional[str] = None,
    ) -> str:
        return _sha({
            "content_sha256": content_sha256,
            "runtime_profile_fingerprint": runtime.fingerprint,
            "image_digest": runtime.contract.image_digest,
            "architecture": runtime.architecture,
            "worker_version": runtime.contract.worker_version,
            "tool_library_sha256": runtime.tool_library_sha256,
            "dependency_environment_sha256": dependency_sha256,
            "environment_allowlist_sha256": _sha(sorted(VERIFY_ENV.items())),
            "network": "none",
            "limits_sha256": runtime.limits.fingerprint(),
            "overlay_sha256": overlay_sha256,
            "contract_sha256": contract_sha256,
            "check_sha256": _sha(check.model_dump(mode="json")),
        })

    def run_check(
        self,
        run_id: str,
        task_id: str,
        *,
        commit: str,
        content_sha256: str,
        contract_sha256: str,
        check: ContractCheckV1,
        runtime: ResolvedRuntime,
        artifact_prefix: str,
        labels: Mapping[str, str],
        changed_paths: Sequence[str] = (),
        overlay_files: Optional[Mapping[str, str]] = None,
        local_modules: Sequence[str] = (),
        cancel_event: Optional[threading.Event] = None,
        dependency_site: Optional[Path] = None,
        dependency_sha256: Optional[str] = None,
    ) -> CheckObservation:
        source = self.pristine(run_id, commit)
        base = self.verification_root(run_id) / "work" / uuid.uuid4().hex[:16]
        workspace = base / "workspace"
        context = base / "context"
        output = base / "output"
        logs = base / "logs"
        try:
            shutil.copytree(source, workspace, symlinks=True)
            make_writable_tree(workspace)
            for directory in (context, output, logs):
                directory.mkdir(parents=True, exist_ok=True)
            overlay_sha = None
            if overlay_files:
                overlay_sha = _sha(sorted(overlay_files.items()))
                for relative, content in overlay_files.items():
                    target = workspace / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content, encoding="utf-8")
            (context / "changed_paths.json").write_text(json.dumps(sorted(changed_paths)), encoding="utf-8")
            os.chmod(context / "changed_paths.json", 0o444)
            os.chmod(context, 0o555)
            before = scan_workspace(workspace)
            before_path = f"{artifact_prefix}/before-manifest.json"
            self.artifact_store.write_json(run_id, before_path, before.to_json(), "verification_environment_manifest", task_id)
            before_artifact = self.artifact_store.get_artifact_by_path(run_id, before_path)

            env_sha = self.environment_sha256(
                runtime, content_sha256=content_sha256, check=check, overlay_sha256=overlay_sha,
                contract_sha256=contract_sha256, dependency_sha256=dependency_sha256,
            )
            command_sha = _sha({"argv": check.argv, "cwd": check.cwd, "parser": check.parser, "timeout": check.timeout_seconds})
            env = dict(VERIFY_ENV)
            mounts = [
                Mount(workspace.resolve(), "/workspace", False),
                Mount(context.resolve(), "/context", True),
                Mount(output.resolve(), "/output", False),
            ]
            from harness.execution.source_layout import source_roots

            python_path = [f"/workspace/{root}" for root in source_roots(workspace)]
            if dependency_site is not None:
                mounts.append(Mount(Path(dependency_site).resolve(), "/deps", True))
                python_path.append("/deps/site-packages")
            if python_path:
                env["PYTHONPATH"] = ":".join(python_path)
            workdir = "/workspace" if check.cwd in (".", "") else f"/workspace/{check.cwd.strip('/')}"
            name = f"dobby-chk-{uuid.uuid4().hex[:16]}"
            spec = ContainerSpec(
                name=name,
                image_id=runtime.image_id,
                command=tuple(check.argv),
                user=runtime.user,
                env=env,
                mounts=tuple(mounts),
                limits=ContainerLimits(
                    cpus=self.limits.cpus,
                    memory_bytes=self.limits.memory_bytes,
                    pids=self.limits.pids,
                    wall_seconds=min(check.timeout_seconds, self.limits.test_batch_seconds),
                    stdout_bytes=self.limits.stdout_bytes,
                    stderr_bytes=self.limits.stderr_bytes,
                    scratch_bytes=self.limits.scratch_bytes,
                    open_files=self.limits.open_files,
                    max_file_bytes=self.limits.max_file_bytes,
                    workspace_growth_bytes=self.limits.workspace_growth_bytes,
                    new_files=self.limits.new_files,
                ),
                labels={"org.dobby.harness": "1", "org.dobby.kind": "verification", **dict(labels)},
                workdir=workdir,
                watch_workspace=workspace,
                watch_output=output,
            )
            outcome = self.backend.run(
                spec,
                stdout_path=logs / "stdout.log",
                stderr_path=logs / "stderr.log",
                cancel_event=cancel_event,
            )
            stdout = _read(logs / "stdout.log", 256 * 1024)
            stderr = _read(logs / "stderr.log", 256 * 1024)
            after = scan_workspace(workspace)
            changes = diff_manifests(before, after)
            allowed = set(check.allowed_workspace_outputs)
            mutation_paths = sorted(
                path
                for change in changes.changes
                for path in [change.path] + ([change.old_path] if change.old_path else [])
                if not is_disposable(path) and not any(path == item or path.startswith(item.rstrip("/") + "/") for item in allowed)
            )
            mutation_paths += [path for path, _ in changes.special] + list(changes.reserved)
            after_path = f"{artifact_prefix}/after-manifest.json"
            self.artifact_store.write_json(run_id, after_path, after.to_json(), "verification_environment_manifest", task_id)
            after_artifact = self.artifact_store.get_artifact_by_path(run_id, after_path)
            report_bytes = None
            junit = output / "junit.xml"
            if junit.exists() and not junit.is_symlink() and junit.stat().st_size <= 16 * 1024 * 1024:
                report_bytes = junit.read_bytes()
            parsed = self._parse(check, report_bytes, outcome.exit_code, stdout, stderr, local_modules)
            status = parsed.status
            if outcome.timed_out:
                status = CheckStatus.TIMEOUT
            elif outcome.oom_killed:
                status = CheckStatus.OOM
            elif outcome.output_limit_exceeded or outcome.limit_breach in ("WORKSPACE_GROWTH", "NEW_FILES", "OUTPUT_DIR_LIMIT"):
                status = CheckStatus.OUTPUT_LIMIT
            elif outcome.cancelled:
                status = CheckStatus.CANCELLED
            elif mutation_paths:
                status = CheckStatus.UNEXPECTED_MUTATION
            if not outcome.removed:
                status = CheckStatus.INTERNAL_ERROR
            stdout_id = self._put(run_id, task_id, f"{artifact_prefix}/stdout.log", stdout.encode(), "text/plain", "check_stdout")
            stderr_id = self._put(run_id, task_id, f"{artifact_prefix}/stderr.log", stderr.encode(), "text/plain", "check_stderr")
            report_id = (
                self._put(run_id, task_id, f"{artifact_prefix}/junit.xml", report_bytes, "application/xml", "check_report")
                if report_bytes is not None
                else None
            )
            return CheckObservation(
                status=status,
                parsed=parsed,
                outcome=outcome,
                environment_sha256=env_sha,
                command_sha256=command_sha,
                unexpected_mutation=bool(mutation_paths),
                mutation_paths=mutation_paths[:100],
                stdout_artifact_id=stdout_id,
                stderr_artifact_id=stderr_id,
                report_artifact_id=report_id,
                before_manifest_artifact_id=before_artifact["artifact_id"],
                after_manifest_artifact_id=after_artifact["artifact_id"] if after_artifact else None,
                stdout_excerpt=_tail(stdout, 6000),
                stderr_excerpt=_tail(stderr, 3000),
                overlay_sha256=overlay_sha,
                container_name=name,
            )
        finally:
            if base.exists():
                secure_rmtree(base, self.verification_root(run_id))

    @staticmethod
    def _parse(
        check: ContractCheckV1,
        report: Optional[bytes],
        exit_code: Optional[int],
        stdout: str,
        stderr: str,
        local_modules: Sequence[str],
    ) -> ParsedReport:
        if check.parser == "pytest-junit@1":
            return parse_pytest_junit(report, exit_code, stdout=stdout, stderr=stderr, local_modules=local_modules, minimum_tests=check.minimum_tests)
        if check.parser == "unittest-verbose@1":
            return parse_unittest(stderr + "\n" + stdout, exit_code, minimum_tests=check.minimum_tests)
        return parse_exit_code(exit_code, stdout=stdout)

    def _put(self, run_id: str, task_id: str, path: str, data: bytes, media: str, kind: str) -> Optional[str]:
        self.artifact_store.write_bytes(run_id, path, data, media, kind, task_id)
        artifact = self.artifact_store.get_artifact_by_path(run_id, path)
        return artifact["artifact_id"] if artifact else None


def _read(path: Path, limit: int) -> str:
    try:
        with open(path, "rb") as handle:
            return handle.read(limit).decode("utf-8", "replace")
    except OSError:
        return ""


def _tail(text: str, limit: int) -> str:
    return text if len(text) <= limit else "…[truncated]…\n" + text[-limit:]
