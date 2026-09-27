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
    "HARNESS_MAX_RUN_WALL_SECONDS", "HARNESS_AUTO_BUILD_RUNTIME", "HARNESS_DEPENDENCY_SETUP",
)


@pytest.fixture(autouse=True)
def _hermetic_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in HOST_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    for name in list(os.environ):
        if name.startswith("HARNESS_MODEL_"):
            monkeypatch.delenv(name, raising=False)
