"""
harness/config.py
─────────────────
Central configuration loaded from environment variables.
AI_API_KEY is never read; GITHUB_TOKEN is optional.
"""
from __future__ import annotations

import os
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class HarnessConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
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
    data_dir: Path = Path("data")

    @field_validator("data_dir", mode="before")
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
