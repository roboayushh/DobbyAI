"""Trusted built-in capability registry.

Capabilities are registered by application code only. Repository content and
model output can *request* a capability by name; they can never register,
widen, or redefine one.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Iterable, List, Tuple

from harness.persistence import canonical_json

ALL_PROFILES: FrozenSet[str] = frozenset({"guided", "sandbox", "delegated"})


@dataclass(frozen=True)
class Capability:
    name: str
    version: str
    mutates_workspace: bool
    allowed_profiles: FrozenSet[str]
    grantable_to_actions: bool
    description: str
    implicit: bool = False  # automatically included (read-only, automatic in all profiles)

    def as_dict(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "version": self.version,
            "mutates_workspace": self.mutates_workspace,
            "allowed_profiles": sorted(self.allowed_profiles),
            "grantable_to_actions": self.grantable_to_actions,
            "implicit": self.implicit,
        }


_BUILTINS: Tuple[Capability, ...] = (
    Capability("source.search", "1.0", False, ALL_PROFILES, True, "Bounded text search under /workspace", True),
    Capability("source.read", "1.0", False, ALL_PROFILES, True, "Bounded reads of regular text files", True),
    Capability("source.symbols", "1.0", False, ALL_PROFILES, True, "Staged symbol index snapshot", True),
    Capability("context.artifact.read", "1.0", False, ALL_PROFILES, True, "Read scoped artifacts by opaque ID", True),
    Capability("action.result.emit", "1.0", False, ALL_PROFILES, True, "Emit one advisory action result", True),
    Capability("workspace.patch", "1.0", True, ALL_PROFILES, True, "Atomic hash-checked patches"),
    Capability("sandbox.command.argv", "1.0", True, ALL_PROFILES, True, "Run argv commands inside the sandbox"),
    Capability("sandbox.command.shell", "1.0", True, frozenset({"sandbox", "delegated"}), True, "Run the profile's fixed shell inside the sandbox"),
    Capability("dependency.setup", "1.0", True, ALL_PROFILES, False, "Host-initiated isolated dependency setup"),
    Capability("network.egress.scoped", "1.0", False, frozenset({"delegated"}), False, "Exceptional scoped egress (not P0)"),
)

# PRD 2 proposal vocabulary and tool names map onto registry names.
LEGACY_ALIASES: Dict[str, str] = {
    "read_file": "source.read",
    "list_files": "source.search",
    "search_text": "source.search",
    "search": "source.search",
    "symbols": "source.symbols",
    "read_artifact": "context.artifact.read",
    "emit_result": "action.result.emit",
    "apply_patch": "workspace.patch",
    "write_file": "workspace.patch",
    "run": "sandbox.command.argv",
    "shell": "sandbox.command.shell",
}


class UnknownCapabilityError(ValueError):
    code = "UNKNOWN_CAPABILITY"


@dataclass
class CapabilityRegistry:
    capabilities: Dict[str, Capability] = field(
        default_factory=lambda: {capability.name: capability for capability in _BUILTINS}
    )

    def get(self, name: str) -> Capability:
        canonical = LEGACY_ALIASES.get(name, name)
        if canonical not in self.capabilities:
            raise UnknownCapabilityError(f"Capability is not registered: {name}")
        return self.capabilities[canonical]

    def names(self) -> List[str]:
        return sorted(self.capabilities)

    def implicit(self) -> List[str]:
        return sorted(name for name, capability in self.capabilities.items() if capability.implicit)

    def normalize(self, requested: Iterable[str]) -> Tuple[List[str], List[str]]:
        """Return (canonical capability names including implicit ones, unknown names)."""
        known: set[str] = set(self.implicit())
        unknown: List[str] = []
        for name in requested:
            canonical = LEGACY_ALIASES.get(name, name)
            if canonical in self.capabilities:
                known.add(canonical)
            else:
                unknown.append(name)
        return sorted(known), sorted(set(unknown))

    def validate_request(self, requested: Iterable[str], profile: str) -> List[str]:
        """Return reason codes that make ``requested`` inadmissible for ``profile``."""
        reasons: List[str] = []
        normalized, unknown = self.normalize(requested)
        if unknown:
            reasons.append("UNKNOWN_CAPABILITY")
        for name in normalized:
            capability = self.capabilities[name]
            if not capability.grantable_to_actions:
                reasons.append(f"CAPABILITY_NOT_GRANTABLE:{name}")
            if profile not in capability.allowed_profiles:
                reasons.append(f"CAPABILITY_NOT_ALLOWED_FOR_PROFILE:{name}")
        return reasons

    def mutating(self, names: Iterable[str]) -> List[str]:
        return sorted(name for name in names if self.get(name).mutates_workspace)

    def fingerprint(self) -> str:
        payload = [self.capabilities[name].as_dict() for name in sorted(self.capabilities)]
        return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


DEFAULT_REGISTRY = CapabilityRegistry()
