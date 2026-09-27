"""Sandbox backends, runtime profiles, and container supervision (PRD 3)."""

from .docker_backend import DockerBackend, host_architecture
from .interface import (
    ContainerLimits,
    ContainerOutcome,
    ContainerSpec,
    Mount,
    RuntimeImageInvalidError,
    SandboxBackend,
    SandboxCommunicationError,
    SandboxError,
    SandboxSettingsError,
    SandboxUnavailableError,
)
from .runtime_profile import (
    DEFAULT_LOCK_PATH,
    IMAGE_TAG,
    RUNTIME_PROFILE_ID,
    ResolvedRuntime,
    RuntimeProfileResolver,
    container_user,
    tool_library_sha256,
)

__all__ = [
    "ContainerLimits",
    "ContainerOutcome",
    "ContainerSpec",
    "DEFAULT_LOCK_PATH",
    "DockerBackend",
    "IMAGE_TAG",
    "Mount",
    "RUNTIME_PROFILE_ID",
    "ResolvedRuntime",
    "RuntimeImageInvalidError",
    "RuntimeProfileResolver",
    "SandboxBackend",
    "SandboxCommunicationError",
    "SandboxError",
    "SandboxSettingsError",
    "SandboxUnavailableError",
    "container_user",
    "host_architecture",
    "tool_library_sha256",
]
