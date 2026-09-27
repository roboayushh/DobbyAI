"""Trusted deployment resource defaults and hard caps (PRD 3 section 8.3)."""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, replace
from typing import Any, Dict, Mapping

from harness.persistence import canonical_json

MiB = 1024 * 1024
GiB = 1024 * MiB


@dataclass(frozen=True)
class SandboxLimits:
    cpus: float = 2.0
    memory_bytes: int = 4 * GiB
    pids: int = 256
    wall_seconds: int = 120
    max_action_seconds: int = 300
    test_batch_seconds: int = 600
    scratch_bytes: int = 1 * GiB
    workspace_growth_bytes: int = 512 * MiB
    new_files: int = 5_000
    stdout_bytes: int = 2 * MiB
    stderr_bytes: int = 2 * MiB
    frame_bytes: int = 1 * MiB
    tool_calls: int = 64
    run_calls: int = 8
    read_bytes: int = 4 * MiB
    max_file_bytes: int = 256 * MiB
    open_files: int = 1024

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def fingerprint(self) -> str:
        return hashlib.sha256(canonical_json(self.as_dict()).encode("utf-8")).hexdigest()


HARD_CAPS = SandboxLimits(
    cpus=4.0,
    memory_bytes=8 * GiB,
    pids=512,
    wall_seconds=600,
    max_action_seconds=600,
    test_batch_seconds=1200,
    scratch_bytes=4 * GiB,
    workspace_growth_bytes=2 * GiB,
    new_files=25_000,
    stdout_bytes=10 * MiB,
    stderr_bytes=10 * MiB,
    frame_bytes=4 * MiB,
    tool_calls=256,
    run_calls=32,
    read_bytes=32 * MiB,
    max_file_bytes=2 * GiB,
    open_files=4096,
)


class LimitConfigError(ValueError):
    code = "INVALID_LIMIT_CONFIGURATION"


def resolve_limits(overrides: Mapping[str, Any] | None = None) -> SandboxLimits:
    """Apply trusted overrides; a value above the hard cap is rejected, not clamped."""
    limits = SandboxLimits()
    if not overrides:
        return limits
    unknown = set(overrides) - set(limits.as_dict())
    if unknown:
        raise LimitConfigError(f"Unknown sandbox limit keys: {', '.join(sorted(unknown))}")
    updated = replace(limits, **dict(overrides))
    for key, value in updated.as_dict().items():
        cap = getattr(HARD_CAPS, key)
        if value <= 0 or value > cap:
            raise LimitConfigError(f"Sandbox limit {key}={value} is outside (0, {cap}]")
    return updated
