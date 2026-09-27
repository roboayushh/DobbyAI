"""Trusted host policy: capabilities, admission, approvals, and limits."""

from .approvals import ApprovalError, ApprovalService
from .capabilities import DEFAULT_REGISTRY, LEGACY_ALIASES, Capability, CapabilityRegistry
from .engine import (
    AdmissionFacts,
    PolicyEngine,
    PolicyError,
    PolicySnapshot,
    normalize_declared_path,
    path_within,
)
from .limits import HARD_CAPS, LimitConfigError, SandboxLimits, resolve_limits

__all__ = [
    "AdmissionFacts",
    "ApprovalError",
    "ApprovalService",
    "Capability",
    "CapabilityRegistry",
    "DEFAULT_REGISTRY",
    "HARD_CAPS",
    "LEGACY_ALIASES",
    "LimitConfigError",
    "PolicyEngine",
    "PolicyError",
    "PolicySnapshot",
    "SandboxLimits",
    "normalize_declared_path",
    "path_within",
    "resolve_limits",
]
