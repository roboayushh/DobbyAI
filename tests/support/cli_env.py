"""Point the CLI at an isolated data directory / model profile for tests."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from typer.testing import CliRunner

from tests.support.harness_fixtures import PROFILE_TOML


@dataclass
class CliEnv:
    root: Path
    config: Any

    def invoke(self, *args: str):
        from harness import cli

        return CliRunner().invoke(cli.app, list(args))

    def json(self, *args: str) -> Dict[str, Any]:
        result = self.invoke(*args)
        lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
        return {"exit_code": result.exit_code, "body": json.loads("\n".join(lines)) if lines else None, "stdout": result.stdout}


def configure(monkeypatch, root: Path, *, adapter_factory: Optional[Callable[[], Any]] = None, model_profile: str = "designated",
              profiles_text: Optional[str] = None) -> CliEnv:
    from harness import cli, cli_execution, cli_release, config as config_module
    from harness.config import HarnessConfig

    profiles = root / "profiles.toml"
    profiles.write_text(profiles_text or PROFILE_TOML)
    cfg = HarnessConfig(data_dir=root / "data", model_profiles_path=profiles, model_profile=model_profile)
    monkeypatch.setattr(config_module, "_config", cfg)
    for module in (cli, cli_execution, cli_release):
        monkeypatch.setattr(module, "get_config", lambda: cfg)
    monkeypatch.setattr(cli_execution, "ADAPTER_FACTORY", adapter_factory)
    return CliEnv(root, cfg)


def evaluator_request(repo: Path, out: Path, **overrides) -> Dict[str, Any]:
    request = {
        "schema_version": "1.0", "request_id": "ereq_test_0001", "adapter": {"name": "native_json_v1", "version": "1.0.0"},
        "repository": {"kind": "local_git", "locator": str(repo)}, "task_mode": "single_issue", "execution_mode": "evaluation",
        "task": {"source_type": "direct_text", "text": "add(2, 3) should return 5. See tests/test_calc.py::test_add"},
        "result_path": str(out / "result.json"), "export_path": str(out / "bundle"), "requested_effects": ["EXPORT"],
        "idempotency_key": "eval-test-0001",
    }
    for key, value in overrides.items():
        request[key] = value
    return request
