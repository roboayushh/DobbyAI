"""Typed plugin ports (PRD 6 section 13.2) and the kernel-facing proposal types.

Plugins are reviewed, pinned, in-process Python code: trusted release code, NOT a
sandbox. They receive capability-limited ports (``KernelPorts``) and never the
raw SQLite connection, Docker socket, filesystem root, API key, or approval store.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Protocol, runtime_checkable

KERNEL_API_VERSION = "1.0"

INTERFACES = (
    "ControllerPlugin", "ContextBuilderPlugin", "ModelAdapterPlugin", "RepositoryPlugin", "RetrieverPlugin",
    "EnvironmentPlugin", "VerifierPlugin", "EvaluatorAdapterPlugin", "ExporterPlugin", "PublisherPlugin",
    "ReportRendererPlugin",
)

# Capabilities a plugin may declare. The release profile grants a subset.
PLUGIN_CAPABILITIES = (
    "READ_STATE_VIEW", "READ_RELEASE_HANDOFF", "WRITE_BOUNDED_RESULT", "WRITE_BOUNDED_ARTIFACT",
    "CONTEXT_PACKET", "EVIDENCE_QUERY", "MODEL_CALL_SERVICE", "REPOSITORY_SNAPSHOT",
    "ADMITTED_ACTION_EXECUTION", "VERIFICATION_OBSERVATION", "EXPORT_BUNDLE_WRITE", "EFFECT_DISPATCH",
)


@dataclass(frozen=True)
class TransitionProposal:
    """What a controller plugin may ask for. The kernel decides; the proposal has no authority."""

    kind: str  # LIFECYCLE_TRANSITION | ACTION | MODEL_CALL | EFFECT | APPROVAL | RAW
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class KernelDecision:
    allowed: bool
    code: str
    detail: str = ""
    capability_request_id: Optional[str] = None


@runtime_checkable
class ControllerPlugin(Protocol):
    def next(self, state_view: Mapping[str, Any]) -> TransitionProposal: ...


@runtime_checkable
class EvaluatorAdapterPlugin(Protocol):
    name: str
    version: str

    def parse(self, raw: bytes) -> Any: ...

    def render(self, result: Any) -> Dict[str, Any]: ...


@runtime_checkable
class ExporterPlugin(Protocol):
    format: str


@runtime_checkable
class ReportRendererPlugin(Protocol):
    def render(self, view: Any) -> str: ...
