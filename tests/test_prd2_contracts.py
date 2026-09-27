from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from harness.contracts import (
    CoderDecisionV1,
    CoderRoleDecisionV1,
    ContextBudgetV1,
    EvidenceRefV1,
    PlanV1,
    PlannerDecisionV1,
    ResolvedModelProfileV1,
    TruthStatus,
    ValidatorReviewV1,
)


H = "a" * 64


def profile_data() -> dict:
    return {
        "profile_id": "designated",
        "endpoint_origin": "https://model.example",
        "model": "model-1",
        "context_window_tokens": 32_000,
        "max_output_tokens": 4_000,
        "safety_margin_tokens": 2_000,
        "tokenizer": "conservative-v1",
        "request_timeout_seconds": 120,
        "profile_fingerprint": H,
    }


def plan_data() -> dict:
    return {
        "decision": "PLAN_READY",
        "task_id": "tsk_1",
        "task_revision": 1,
        "plan_revision": 1,
        "objective": "Repair the boundary behavior.",
        "preserved_constraints": ["Keep the public API."],
        "acceptance_criteria": [
            {"criterion_id": "ac_1", "statement": "Boundary works", "evidence_needed": "test"}
        ],
        "observed_evidence_ids": ["evd_1"],
        "hypotheses": [],
        "likely_edit_locations": [],
        "steps": [{"step_id": "s1", "purpose": "Add test", "depends_on": []}],
        "verification_strategy": ["Run focused tests"],
        "unresolved_questions": [],
        "required_capabilities": ["read_file", "apply_patch"],
        "step_budget": 3,
    }


def test_profile_is_strict_and_has_capacity() -> None:
    profile = ResolvedModelProfileV1.model_validate(profile_data())
    assert profile.credential_env == "AI_API_KEY"
    with pytest.raises(ValidationError):
        ResolvedModelProfileV1.model_validate({**profile_data(), "unknown": True})
    with pytest.raises(ValidationError):
        ResolvedModelProfileV1.model_validate(
            {**profile_data(), "max_output_tokens": 31_000, "safety_margin_tokens": 2_000}
        )


def test_context_budget_enforces_w_minus_o_minus_s() -> None:
    valid = ContextBudgetV1(
        context_window_tokens=32_000,
        reserved_output_tokens=4_000,
        safety_margin_tokens=2_000,
        max_input_tokens=26_000,
        estimated_input_tokens=12_000,
        counter_mode="conservative_estimate",
    )
    assert valid.max_input_tokens == 26_000
    with pytest.raises(ValidationError):
        ContextBudgetV1(
            context_window_tokens=32_000,
            reserved_output_tokens=4_000,
            safety_margin_tokens=2_000,
            max_input_tokens=27_000,
            estimated_input_tokens=12_000,
            counter_mode="conservative_estimate",
        )


def test_evidence_span_and_truth_status_are_strict() -> None:
    evidence = EvidenceRefV1(
        evidence_id="evd_1",
        run_id="run_1",
        task_id="tsk_1",
        source_revision="B",
        path="src/a.py",
        start_line=2,
        end_line=4,
        evidence_type="definition",
        retrieval_reason="identifier match",
        truth_status=TruthStatus.OBSERVED,
        content_sha256=H,
    )
    assert evidence.truth_status == TruthStatus.OBSERVED
    with pytest.raises(ValidationError):
        EvidenceRefV1.model_validate({**evidence.model_dump(), "end_line": None})


def test_planner_union_rejects_unknown_decision_and_fields() -> None:
    parsed = TypeAdapter(PlannerDecisionV1).validate_python(plan_data())
    assert isinstance(parsed, PlanV1)
    with pytest.raises(ValidationError):
        TypeAdapter(PlannerDecisionV1).validate_python({**plan_data(), "decision": "SOLVED"})
    with pytest.raises(ValidationError):
        TypeAdapter(PlannerDecisionV1).validate_python({**plan_data(), "reasoning": "hidden"})


def test_plan_rejects_unsafe_locations_and_invalid_step_graph() -> None:
    with pytest.raises(ValidationError, match="unsafe edit location"):
        PlanV1.model_validate(
            {**plan_data(), "likely_edit_locations": [{"path": "../escape", "reason": "x"}]}
        )
    cyclic = {
        **plan_data(),
        "steps": [
            {"step_id": "s1", "purpose": "one", "depends_on": ["s2"]},
            {"step_id": "s2", "purpose": "two", "depends_on": ["s1"]},
        ],
    }
    with pytest.raises(ValidationError, match="cycle"):
        PlanV1.model_validate(cyclic)


def test_coder_action_rejects_unsafe_paths() -> None:
    data = {
        "decision": "CODE",
        "task_id": "tsk_1",
        "task_revision": 1,
        "plan_revision": 1,
        "workspace_version": "B",
        "purpose": "Read and patch",
        "requested_capabilities": ["read_file", "apply_patch"],
        "declared_paths": ["src/a.py"],
        "python_action": "print('proposal only')",
        "success_observations": ["Patch prepared"],
        "max_action_seconds": 30,
    }
    decision = TypeAdapter(CoderRoleDecisionV1).validate_python(data)
    assert isinstance(decision, CoderDecisionV1)
    with pytest.raises(ValidationError):
        TypeAdapter(CoderRoleDecisionV1).validate_python(
            {**data, "declared_paths": ["../outside"]}
        )


def test_validator_opinion_is_never_host_verification() -> None:
    review = ValidatorReviewV1(
        decision="NO_OBJECTION",
        task_id="tsk_1",
        candidate_hash=H,
        acceptance_contract_sha256=H,
    )
    assert review.narrative_trust == "model_opinion_not_host_verification"


def test_checked_in_schemas_are_strict() -> None:
    schema_root = Path(__file__).parents[1] / "schemas" / "v1"
    expected = [
        "orchestration/resolved_model_profile.json",
        "orchestration/evidence_ref.json",
        "orchestration/context_packet.json",
        "roles/plan.json",
        "roles/planner_decision.json",
        "roles/coder_decision.json",
        "roles/validator_review.json",
        "orchestration/model_call_result.json",
        "orchestration/orchestration_phase_result.json",
        "orchestration/prd3_action_handoff.json",
    ]
    for relative in expected:
        parsed = json.loads((schema_root / relative).read_text(encoding="utf-8"))
        assert isinstance(parsed, dict)
        assert "$defs" in parsed or parsed.get("additionalProperties") is False
