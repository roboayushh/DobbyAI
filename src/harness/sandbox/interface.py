"""Backend-neutral sandbox types.

The Docker backend implements :class:`SandboxBackend`. A future microVM backend
can implement the same interface without changing policy, settlement, or
verification code (PRD 3 section 8.8).
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Protocol, Sequence, Tuple


class SandboxError(RuntimeError):
    code = "SANDBOX_ERROR"
    retryable = False


class SandboxUnavailableError(SandboxError):
    code = "SANDBOX_UNAVAILABLE"


class RuntimeImageInvalidError(SandboxError):
    code = "RUNTIME_IMAGE_INVALID"


class SandboxSettingsError(SandboxError):
    code = "SANDBOX_SETTINGS_INVALID"


class SandboxCommunicationError(SandboxError):
    """The engine stopped answering; the container state cannot be proven."""

    code = "SANDBOX_COMMUNICATION_LOST"


@dataclass(frozen=True)
class Mount:
    source: Path
    target: str
    read_only: bool


@dataclass(frozen=True)
class ContainerLimits:
    cpus: float
    memory_bytes: int
    pids: int
    wall_seconds: int
    stdout_bytes: int
    stderr_bytes: int
    scratch_bytes: int
    open_files: int = 1024
    max_file_bytes: int = 256 * 1024 * 1024
    workspace_growth_bytes: Optional[int] = None
    new_files: Optional[int] = None
    output_dir_bytes: int = 64 * 1024 * 1024
    stop_grace_seconds: int = 2


@dataclass(frozen=True)
class ContainerSpec:
    name: str
    image_id: str
    command: Tuple[str, ...]
    user: str
    env: Dict[str, str]
    mounts: Tuple[Mount, ...]
    limits: ContainerLimits
    labels: Dict[str, str]
    workdir: str = "/workspace"
    network: str = "none"
    tmpfs: Dict[str, str] = field(default_factory=dict)
    # Host directories watched for growth while the container runs.
    watch_workspace: Optional[Path] = None
    watch_output: Optional[Path] = None


@dataclass
class ContainerOutcome:
    container_name: str
    container_id: Optional[str]
    exit_code: Optional[int]
    signal: Optional[int]
    oom_killed: bool
    timed_out: bool
    cancelled: bool
    output_limit_exceeded: bool
    limit_breach: Optional[str]
    elapsed_ms: int
    stdout_path: Path
    stderr_path: Path
    stdout_bytes: int
    stderr_bytes: int
    settings_sha256: str
    engine_version: str
    removed: bool
    started: bool

    @property
    def stopped_cleanly(self) -> bool:
        return self.removed and self.started


class SandboxBackend(Protocol):
    name: str

    def availability(self) -> Tuple[bool, str, Optional[str]]: ...

    def run(
        self,
        spec: ContainerSpec,
        *,
        stdout_path: Path,
        stderr_path: Path,
        cancel_event: Optional[threading.Event] = None,
        on_created: Optional[Callable[[str, str], None]] = None,
        on_started: Optional[Callable[[], None]] = None,
    ) -> ContainerOutcome: ...

    def find_by_labels(self, labels: Dict[str, str]) -> List[str]: ...

    def force_remove(self, container: str) -> bool: ...
