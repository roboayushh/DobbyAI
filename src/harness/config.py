"""
harness/config.py
─────────────────
Central configuration loaded from environment variables.

Includes GitHub configuration and the trusted, non-secret model-profile path.
"""
from __future__ import annotations

import os
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator

# The harness installation root (…/src/harness/config.py -> repository root). Defaults are
# anchored here so `harness` behaves the same from any working directory.
HARNESS_ROOT = Path(__file__).resolve().parents[2]
from pydantic_settings import BaseSettings, SettingsConfigDict


class HarnessConfig(BaseSettings):
    model_config = SettingsConfigDict(
        # Only the harness installation's own .env is read — never a .env in the current
        # directory, which may belong to an untrusted target repository.
        env_file=str(HARNESS_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        protected_namespaces=("settings_",),
    )

    # ── GitHub credentials (optional) ──────────────────────
    github_token: str | None = None  # read from GITHUB_TOKEN

    # ── GitHub API ─────────────────────────────────────────
    github_api_base: str = "https://api.github.com"
    github_api_version: str = "2022-11-28"
    github_accept: str = "application/vnd.github+json"

    # ── Request limits ─────────────────────────────────────
    request_timeout_s: float = 15.0
    action_deadline_s: float = 45.0
    max_retries: int = 2
    max_response_bytes: int = 5 * 1024 * 1024  # 5 MiB

    # ── Pagination ─────────────────────────────────────────
    default_page_size: int = 30
    max_page_size: int = 100

    # ── Persistence ────────────────────────────────────────
    data_dir: Path = HARNESS_ROOT / "data"
    model_profiles_path: Path = HARNESS_ROOT / "config" / "model_profiles.toml"

    # Selected model profile id in model_profiles_path (HARNESS_MODEL_PROFILE); one profile for every role.
    # Alias order: an explicit constructor argument wins over the environment variable.
    model_profile: str = Field("designated", validation_alias=AliasChoices("model_profile", "HARNESS_MODEL_PROFILE"))

    # ── PRD 3-5 execution (non-secret, host-owned) ────────
    permission_profile: str = Field("sandbox", validation_alias=AliasChoices("permission_profile", "HARNESS_PERMISSION_PROFILE"))
    max_model_calls: int | None = Field(None, validation_alias=AliasChoices("max_model_calls", "HARNESS_MAX_MODEL_CALLS"))
    max_run_wall_seconds: int | None = Field(None, validation_alias=AliasChoices("max_run_wall_seconds", "HARNESS_MAX_RUN_WALL_SECONDS"))
    auto_build_runtime: bool = Field(True, validation_alias=AliasChoices("auto_build_runtime", "HARNESS_AUTO_BUILD_RUNTIME"))
    # Isolated dependency setup through the PyPI-only egress proxy (PRD 3 section 14); false = offline only.
    dependency_setup: bool = Field(True, validation_alias=AliasChoices("dependency_setup", "HARNESS_DEPENDENCY_SETUP"))

    @field_validator("data_dir", "model_profiles_path", mode="before")
    @classmethod
    def _expand_data_dir(cls, v: str | Path) -> Path:
        return Path(v).expanduser().resolve()

    @property
    def db_path(self) -> Path:
        return self.data_dir / "harness.db"

    @property
    def snapshots_dir(self) -> Path:
        return self.data_dir / "snapshots"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def quarantine_dir(self) -> Path:
        return self.data_dir / "quarantine"

    @property
    def has_github_token(self) -> bool:
        return bool(self.github_token)

    def auth_header(self) -> dict[str, str]:
        """Return Authorization header dict, or empty dict if no token."""
        if self.github_token:
            return {"Authorization": f"Bearer {self.github_token}"}
        return {}


# Module-level singleton
_config: HarnessConfig | None = None


def get_config() -> HarnessConfig:
    global _config
    if _config is None:
        _config = HarnessConfig()
        _config.data_dir.mkdir(parents=True, exist_ok=True)
        _config.snapshots_dir.mkdir(parents=True, exist_ok=True)
    return _config
