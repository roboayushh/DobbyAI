"""`make run` model selection at launch: asked once only when no real profile is configured."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from harness import cli_release
from harness.config import HARNESS_ROOT, HarnessConfig

BUNDLED = HARNESS_ROOT / "config" / "model_profiles.toml"


def config(tmp_path: Path, profile: str = "designated") -> HarnessConfig:
    return HarnessConfig(data_dir=tmp_path / "data", model_profiles_path=BUNDLED, model_profile=profile)


@pytest.fixture
def answers(monkeypatch):
    replies: list = []
    asked: list = []

    def ask(prompt, **kwargs):
        asked.append(prompt)
        return replies.pop(0)

    monkeypatch.setattr("rich.prompt.Prompt.ask", ask)
    return replies, asked


def test_placeholder_default_asks_once_and_pins_the_choice(tmp_path, monkeypatch, answers) -> None:
    replies, asked = answers
    monkeypatch.setenv("AI_API_KEY", "test-key")
    replies.extend(["9", "2"])  # an out-of-range answer is re-asked, never guessed
    cfg = config(tmp_path)
    cli_release.ensure_model_profile(cfg, non_interactive=False)
    assert cfg.model_profile == cli_release.LAUNCH_PROFILES[1] == "qwen"
    assert os.environ["HARNESS_MODEL_PROFILE"] == "qwen" and len(asked) == 2
    monkeypatch.delenv("HARNESS_MODEL_PROFILE")


def test_profile_names_are_accepted_as_answers(tmp_path, monkeypatch, answers) -> None:
    replies, _ = answers
    monkeypatch.setenv("AI_API_KEY", "test-key")
    replies.append("deepseek")
    cfg = config(tmp_path)
    cli_release.ensure_model_profile(cfg, non_interactive=False)
    assert cfg.model_profile == "deepseek"
    monkeypatch.delenv("HARNESS_MODEL_PROFILE")


def test_configured_profile_is_never_second_guessed(tmp_path, monkeypatch, answers) -> None:
    _, asked = answers
    monkeypatch.setenv("AI_API_KEY", "test-key")
    cfg = config(tmp_path, "qwen")
    cli_release.ensure_model_profile(cfg, non_interactive=False)
    assert cfg.model_profile == "qwen" and asked == []


def test_non_interactive_and_missing_key_fail_with_instructions(tmp_path, monkeypatch, answers) -> None:
    _, asked = answers
    monkeypatch.setenv("AI_API_KEY", "test-key")
    with pytest.raises(ValueError, match="HARNESS_MODEL_PROFILE=deepseek or qwen"):
        cli_release.ensure_model_profile(config(tmp_path), non_interactive=True)
    monkeypatch.delenv("AI_API_KEY")
    with pytest.raises(ValueError, match="export AI_API_KEY"):
        cli_release.ensure_model_profile(config(tmp_path, "deepseek"), non_interactive=False)
    assert asked == []


def test_launch_menu_offers_only_bundled_prescribed_profiles() -> None:
    import tomllib

    bundled = tomllib.loads(BUNDLED.read_text())["profiles"]
    assert cli_release.LAUNCH_PROFILES[:2] == ("deepseek", "qwen")
    assert set(cli_release.LAUNCH_PROFILES) <= set(bundled)
    assert not {"designated", "claude-bridge", "qwen-local"} & set(cli_release.LAUNCH_PROFILES)


def test_a_groq_key_makes_groq_the_default_answer(tmp_path, monkeypatch) -> None:
    captured = {}

    def ask(prompt, **kwargs):
        captured["default"] = kwargs.get("default")
        return kwargs["default"]  # the evaluator just presses Enter

    monkeypatch.setattr("rich.prompt.Prompt.ask", ask)
    monkeypatch.setenv("AI_API_KEY", "gsk_" + "x" * 52)
    cfg = config(tmp_path)
    cli_release.ensure_model_profile(cfg, non_interactive=False)
    assert cfg.model_profile == "groq-qwen" and captured["default"] == str(cli_release.LAUNCH_PROFILES.index("groq-qwen") + 1)
    monkeypatch.delenv("HARNESS_MODEL_PROFILE")
