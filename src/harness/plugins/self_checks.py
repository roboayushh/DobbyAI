"""Bounded deterministic self-checks run for every plugin before any run starts."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict

from harness.plugins.registry import PluginError


def _import_only(cls: Any, config: Dict[str, Any]) -> None:
    if not isinstance(cls, type):
        raise PluginError("PLUGIN_SELF_CHECK_FAILED", "entry point is not a class")


def _native_json(cls: Any, config: Dict[str, Any]) -> None:
    adapter = cls(max_request_bytes=config["max_request_bytes"])
    sample = {
        "schema_version": "1.0", "request_id": "ereq_selfcheck", "adapter": {"name": "native_json_v1", "version": "1.0.0"},
        "repository": {"kind": "local_git", "locator": "/tmp/selfcheck-repo"}, "task_mode": "single_issue",
        "execution_mode": "evaluation", "task": {"source_type": "direct_text", "text": "self check"},
        "idempotency_key": "selfcheck-0001",
    }
    request = adapter.parse(json.dumps(sample).encode())
    if request.request_id != "ereq_selfcheck":
        raise PluginError("PLUGIN_SELF_CHECK_FAILED", "native adapter did not round-trip the request")
    try:
        adapter.parse(json.dumps({**sample, "schema_version": "2.0"}).encode())
    except Exception:
        return
    raise PluginError("PLUGIN_SELF_CHECK_FAILED", "native adapter accepted an unsupported major schema version")


def _controller_table(cls: Any, config: Dict[str, Any]) -> None:
    from harness.contracts import OrchestrationState as S
    from harness.orchestration.lifecycle import ALLOWED_LIFECYCLE_TRANSITIONS

    # Completion authority: only verification may reach READY_FOR_REVIEW.
    for state, targets in ALLOWED_LIFECYCLE_TRANSITIONS.items():
        if S.READY_FOR_REVIEW in targets and state != S.VERIFYING:
            raise PluginError("PLUGIN_SELF_CHECK_FAILED", f"{state.value} can reach READY_FOR_REVIEW without verification")


def _one_model(cls: Any, config: Dict[str, Any]) -> None:
    from harness.model.credentials import CredentialProvider

    provider = CredentialProvider(environ={"AI_API_KEY": "sentinel", "OPENAI_API_KEY": "other"})
    if provider.get_ai_api_key() != "sentinel":
        raise PluginError("PLUGIN_SELF_CHECK_FAILED", "model adapter does not read exactly AI_API_KEY")


def _no_host_fallback(cls: Any, config: Dict[str, Any]) -> None:
    from harness.sandbox import ContainerLimits, ContainerSpec, SandboxSettingsError

    backend = cls(allowed_mount_roots=[])
    limits = ContainerLimits(cpus=1, memory_bytes=1 << 28, pids=16, wall_seconds=5, stdout_bytes=1024, stderr_bytes=1024, scratch_bytes=1 << 20)
    for spec in (
        ContainerSpec(name="selfcheck", image_id="sha256:0", command=("true",), user="0:0", env={}, mounts=(), limits=limits, labels={}),
        ContainerSpec(name="selfcheck", image_id="sha256:0", command=("true",), user="1000:1000", env={}, mounts=(), limits=limits,
                      labels={}, network="bridge"),
    ):
        try:
            backend.create_args(spec)
        except SandboxSettingsError:
            continue
        raise PluginError("PLUGIN_SELF_CHECK_FAILED", "environment accepted a root user or a network")


def _gate(cls: Any, config: Dict[str, Any]) -> None:
    from harness.contracts.verification import CheckOrigin, CheckStatus, ContractCheckV1, ContractCriterionV1, TaskOutcome
    from harness.verification.comparator import CheckEvidence, compare
    from harness.verification.completion_gate import GateInputs, evaluate

    check = ContractCheckV1(check_id="c", origin=CheckOrigin.RUNTIME_ADAPTER, kind="test", tier="focused", required=True,
                            baseline_policy="required", argv=["python"], timeout_seconds=10, parser="pytest-junit@1")
    comparison = compare([CheckEvidence(check, CheckStatus.PASS, cases={"t": "PASS"}, baseline_cases={"t": "FAIL"})])
    decision = evaluate(GateInputs(
        candidate_intact=True, contract_current=True, unsettled_work=False, comparison=comparison,
        criteria=[ContractCriterionV1(criterion_id="a", statement="s", check_ids=["c"])], baseline_required_missing=[],
        diff_review_status="CLEAN", validator_required=True, validator_ran=False, validator_decision=None,
        validator_blocking_findings=0,
    ))
    if decision.status == TaskOutcome.PASS:
        raise PluginError("PLUGIN_SELF_CHECK_FAILED", "verifier would PASS without the required validator")


def _patch(cls: Any, config: Dict[str, Any]) -> None:
    adapter = cls()
    bad = b"diff --git a/../evil b/../evil\n--- a/../evil\n+++ b/../evil\n@@ -1 +1 @@\n-a\n+b\n"
    try:
        adapter.inspect(bad, [("M", "../evil")])
    except ValueError:
        return
    raise PluginError("PLUGIN_SELF_CHECK_FAILED", "exporter accepted a traversal path")


SELF_CHECKS: Dict[str, Callable[[Any, Dict[str, Any]], None]] = {
    "import_only_v1": _import_only,
    "native_json_contract_v1": _native_json,
    "controller_transition_table_v1": _controller_table,
    "one_model_one_key_v1": _one_model,
    "no_host_fallback_v1": _no_host_fallback,
    "completion_gate_false_pass_v1": _gate,
    "patch_round_trip_v1": _patch,
}
