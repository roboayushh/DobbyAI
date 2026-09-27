"""Hermetic test environment.

Evaluators run ``make test`` with their live configuration exported (for example
``AI_API_KEY`` and ``HARNESS_MODEL_PROFILE=deepseek``). None of that may leak into the
deterministic suite: tests that need a key or a profile set it explicitly.
"""
from __future__ import annotations

import os

import pytest

HOST_VARIABLES = (
    "AI_API_KEY", "GITHUB_TOKEN", "DATA_DIR", "MODEL_PROFILES_PATH",
    "HARNESS_MODEL_PROFILE", "HARNESS_PERMISSION_PROFILE", "HARNESS_MAX_MODEL_CALLS",
    "HARNESS_MAX_RUN_WALL_SECONDS", "HARNESS_AUTO_BUILD_RUNTIME", "HARNESS_DEPENDENCY_SETUP", "HARNESS_MODEL_TPM_LIMIT",
)


@pytest.fixture(autouse=True)
def _hermetic_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in HOST_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    for name in list(os.environ):
        if name.startswith("HARNESS_MODEL_"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _fresh_provider_limits():
    """Learned tokens-per-minute caps are process-wide; never let one test's provider leak into another."""
    from harness.model import adapter

    adapter._PROVIDER_LIMITS.clear()
    adapter._TPM_WINDOWS.clear()
    yield
    adapter._PROVIDER_LIMITS.clear()
    adapter._TPM_WINDOWS.clear()
