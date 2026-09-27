from __future__ import annotations

import json

from typer.testing import CliRunner

import harness.cli as cli
from harness.contracts import OrchestrationPhaseResultV1


class StubController:
    def __init__(self, result: OrchestrationPhaseResultV1) -> None:
        self.result = result

    def continue_run(self, run_id: str, stop_at: str):
        assert run_id == "run_1"
        return self.result


def _result(status: str) -> OrchestrationPhaseResultV1:
    return OrchestrationPhaseResultV1(
        run_id="run_1",
        task_id="task_1",
        status=status,
        source_revision="B",
        usage={
            "model_calls_used": 2,
            "input_tokens_used": 100,
            "output_tokens_used": 50,
            "elapsed_seconds": 1,
            "verification_calls_reserved": 2,
        },
        remaining_repository_tasks=0,
        next_required_prd=3 if status == "ACTION_PROPOSED" else None,
        created_at="2026-09-27T00:00:00+00:00",
        questions=(
            [{"question": "Which API is authoritative?", "impact": "Changes the plan."}]
            if status == "NEEDS_INPUT"
            else []
        ),
    )


def test_continue_json_stdout_is_one_object(monkeypatch) -> None:
    result = _result("ACTION_PROPOSED")
    monkeypatch.setattr(
        cli,
        "_get_orchestration_controller",
        lambda: (StubController(result), object(), object()),
    )
    invocation = CliRunner().invoke(
        cli.app, ["continue", "run_1", "--until", "action-proposed", "--json"]
    )
    assert invocation.exit_code == 0
    parsed = json.loads(invocation.stdout)
    assert parsed["status"] == "ACTION_PROPOSED"
    assert invocation.stdout.count("{") >= 1
    assert invocation.stdout.strip().endswith("}")


def test_needs_input_is_structured_and_uses_exit_six(monkeypatch) -> None:
    result = _result("NEEDS_INPUT")
    monkeypatch.setattr(
        cli,
        "_get_orchestration_controller",
        lambda: (StubController(result), object(), object()),
    )
    invocation = CliRunner().invoke(cli.app, ["continue", "run_1", "--json"])
    assert invocation.exit_code == 6
    parsed = json.loads(invocation.stdout)
    assert parsed["status"] == "NEEDS_INPUT"
    assert parsed["questions"][0]["question"] == "Which API is authoritative?"
