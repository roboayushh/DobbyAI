"""Deterministic scripted model adapter for end-to-end tests.

Each script step is a callable receiving a :class:`PacketView` (the exact
filtered messages the harness sent) and returning a JSON-serializable decision.
This lets recorded coder responses bind to the real workspace revision the host
advertised, exactly as a live model must.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Union

from harness.model import FakeModelAdapter, ModelAdapterError, ModelAdapterResponse, ModelCallRequest

_REVISION = re.compile(r'"source_revision":"([0-9a-fA-F]{7,64}|[A-Za-z0-9_]+)"')
_TASK = re.compile(r'"task_id":"([A-Za-z0-9_]+)"')
_CANDIDATE = re.compile(r'"candidate_version":"([0-9a-f]{40,64})"')


@dataclass
class PacketView:
    request: ModelCallRequest

    @property
    def text(self) -> str:
        return "\n".join(message.get("content", "") for message in self.request.messages)

    @property
    def role(self) -> str:
        return self.request.role.value

    @property
    def source_revision(self) -> str:
        match = _REVISION.search(self.text)
        if not match:
            raise AssertionError("packet has no source_revision")
        return match.group(1)

    @property
    def task_id(self) -> str:
        match = _TASK.search(self.text)
        if not match:
            raise AssertionError("packet has no task_id")
        return match.group(1)

    @property
    def candidate_version(self) -> Optional[str]:
        match = _CANDIDATE.search(self.text)
        return match.group(1) if match else None

    def contains(self, needle: str) -> bool:
        return needle in self.text


Step = Union[Callable[[PacketView], Any], Mapping[str, Any], str, Exception]


class ScriptedAdapter(FakeModelAdapter):
    name = "scripted"

    def __init__(self, steps: Sequence[Step]) -> None:
        super().__init__([])
        self.steps: List[Step] = list(steps)
        self.views: List[PacketView] = []

    def generate(self, request: ModelCallRequest) -> ModelAdapterResponse:
        self.calls.append(request)
        view = PacketView(request)
        self.views.append(view)
        if not self.steps:
            raise ModelAdapterError("SCRIPT_EXHAUSTED", f"No scripted response for {request.role.value}")
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        value = step(view) if callable(step) else step
        raw = value if isinstance(value, str) else json.dumps(value)
        return ModelAdapterResponse(raw_text=raw, input_tokens=500, output_tokens=200, usage_source="provider_reported", latency_ms=1)


# ---------------------------------------------------------------- builders
def plan(task_id: str, *, revision: int = 1, edit_paths: Sequence[str] = ("src/calc.py",), tests: Sequence[str] = ()) -> Dict[str, Any]:
    return {
        "schema_version": "1.0",
        "decision": "PLAN_READY",
        "task_id": task_id,
        "task_revision": 1,
        "plan_revision": revision,
        "objective": "Fix the reported behavior with a minimal change.",
        "preserved_constraints": ["Keep the public API stable."],
        "acceptance_criteria": [
            {"criterion_id": "ac1", "statement": "The reported behavior is fixed.", "evidence_needed": "Relevant tests pass."}
        ],
        "observed_evidence_ids": [],
        "hypotheses": [],
        "likely_edit_locations": [{"path": path, "symbol": None, "reason": "implementation"} for path in (*edit_paths, *tests)],
        "steps": [{"step_id": "s1", "purpose": "Patch and run the focused tests", "depends_on": []}],
        "verification_strategy": ["Run the focused tests" + (": " + " ".join(tests) if tests else "")],
        "unresolved_questions": [],
        "required_capabilities": ["workspace.patch", "sandbox.command.argv"],
        "step_budget": 6,
    }


def plan_step(**kwargs: Any) -> Callable[[PacketView], Dict[str, Any]]:
    return lambda view: plan(view.task_id, **kwargs)


def code_step(
    action: str,
    *,
    declared: Sequence[str] = ("src/",),
    capabilities: Sequence[str] = ("workspace.patch", "sandbox.command.argv"),
    plan_revision: int = 1,
    seconds: int = 60,
    purpose: str = "Apply the fix and run the tests.",
    revision_override: Optional[str] = None,
) -> Callable[[PacketView], Dict[str, Any]]:
    def build(view: PacketView) -> Dict[str, Any]:
        return {
            "schema_version": "1.0",
            "decision": "CODE",
            "task_id": view.task_id,
            "task_revision": 1,
            "plan_revision": plan_revision,
            "workspace_version": revision_override or view.source_revision,
            "purpose": purpose,
            "requested_capabilities": list(capabilities),
            "declared_paths": list(declared),
            "python_action": action,
            "success_observations": ["The host-observed result shows the expected change."],
            "max_action_seconds": seconds,
        }

    return build


def complete_step(plan_revision: int = 1) -> Callable[[PacketView], Dict[str, Any]]:
    return lambda view: {
        "schema_version": "1.0",
        "decision": "COMPLETE",
        "task_id": view.task_id,
        "task_revision": 1,
        "plan_revision": plan_revision,
        "claimed_outcome": "The change is applied and the focused tests passed in the last action.",
        "evidence_ids": [],
    }


def validator_step(decision: str = "NO_OBJECTION", findings: Sequence[Mapping[str, Any]] = (), overlay: Sequence[Mapping[str, Any]] = ()) -> Callable[[PacketView], Dict[str, Any]]:
    def build(view: PacketView) -> Dict[str, Any]:
        text = view.text
        contract = re.search(r'"contract_sha256":"([0-9a-f]{64})"', text)
        candidate = re.search(r'"candidate_sha256":"([0-9a-f]{64})"', text)
        payload: Dict[str, Any] = {
            "schema_version": "1.0",
            "decision": decision,
            "task_id": view.task_id,
            "candidate_hash": candidate.group(1) if candidate else "0" * 64,
            "acceptance_contract_sha256": contract.group(1) if contract else "0" * 64,
            "findings": list(findings),
            "proposed_checks": [],
            "unresolved_risks": [],
        }
        if overlay:
            payload["overlay_tests"] = list(overlay)
        return payload

    return build


class RoutedAdapter(FakeModelAdapter):
    """Routes each request to a per-task script so queue order need not be hard-coded."""

    name = "scripted"

    def __init__(self, routes: Mapping[str, Sequence[Step]]) -> None:
        super().__init__([])
        self.routes: Dict[str, List[Step]] = {key: list(value) for key, value in routes.items()}
        self.views: List[PacketView] = []

    def generate(self, request: ModelCallRequest) -> ModelAdapterResponse:
        self.calls.append(request)
        view = PacketView(request)
        self.views.append(view)
        steps = self.routes.get(view.task_id)
        if not steps:
            raise ModelAdapterError("SCRIPT_EXHAUSTED", f"No scripted response for {view.task_id} ({request.role.value})")
        step = steps.pop(0)
        if isinstance(step, Exception):
            raise step
        value = step(view) if callable(step) else step
        raw = value if isinstance(value, str) else json.dumps(value)
        return ModelAdapterResponse(raw_text=raw, input_tokens=500, output_tokens=200, usage_source="provider_reported", latency_ms=1)


def needs_input_step(question: str = "Which behavior is expected?") -> Callable[[PacketView], Dict[str, Any]]:
    return lambda view: {
        "schema_version": "1.0",
        "decision": "NEEDS_INPUT",
        "task_id": view.task_id,
        "questions": [{"question": question, "impact": "The expected behavior cannot be inferred from the repository."}],
    }


def patch_action(path: str, old: str, new: str, test: str, note: str = "patched") -> str:
    return (
        f"r = read_file({path!r})\n"
        f"apply_patch([{{'path': {path!r}, 'old': {old!r}, 'new': {new!r}}}], {{{path!r}: r['sha256']}})\n"
        f"res = run(['python', '-m', 'pytest', '-q', {test!r}])\n"
        "print(res['stdout'][-400:])\n"
        f"emit_result('ACTION_COMPLETED', {note!r}, [f\"exit={{res['exit_code']}}\"])\n"
    )
