"""Versioned contracts for PRD 2 model, context, and orchestration boundaries."""
from __future__ import annotations

import re
from enum import Enum
from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SHA256_PATTERN = r"^[0-9a-f]{64}$"


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())


class Role(str, Enum):
    PLANNER = "planner"
    CODER = "coder"
    VALIDATOR = "validator"
    SUMMARIZER = "summarizer"


class TruthStatus(str, Enum):
    OBSERVED = "observed"
    REPORTED = "reported"
    HYPOTHESIS = "hypothesis"


class OrchestrationState(str, Enum):
    PREPARED = "PREPARED"
    INDEXING = "INDEXING"
    PLANNING = "PLANNING"
    PLAN_READY = "PLAN_READY"
    CODING = "CODING"
    REPLANNING = "REPLANNING"
    ACTION_PROPOSED = "ACTION_PROPOSED"
    VERIFICATION_REQUIRED = "VERIFICATION_REQUIRED"
    NEEDS_INPUT = "NEEDS_INPUT"
    NEEDS_CAPABILITY = "NEEDS_CAPABILITY"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    # PRD 3: sandbox execution
    ACTION_EXECUTING = "ACTION_EXECUTING"
    NEEDS_APPROVAL = "NEEDS_APPROVAL"
    BLOCKED_ENVIRONMENT = "BLOCKED_ENVIRONMENT"
    ACTION_UNKNOWN = "ACTION_UNKNOWN"
    # PRD 4: verification and repair
    VERIFYING = "VERIFYING"
    REPAIRING = "REPAIRING"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    UNVERIFIED = "UNVERIFIED"
    # PRD 5: queue
    QUEUE_SETTLED = "QUEUE_SETTLED"


class SamplingV1(StrictContract):
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    top_p: float = Field(default=1.0, gt=0.0, le=1.0)
    seed: Optional[int] = None


class ResolvedModelProfileV1(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    profile_id: str = Field(min_length=1, max_length=64)
    protocol: Literal["openai-compatible-chat"] = "openai-compatible-chat"
    endpoint_origin: str
    model: str = Field(min_length=1, max_length=256)
    context_window_tokens: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    safety_margin_tokens: int = Field(ge=0)
    tokenizer: str = Field(min_length=1, max_length=128)
    request_timeout_seconds: int = Field(gt=0, le=600)
    sampling: SamplingV1 = Field(default_factory=SamplingV1)
    supports_json_schema: bool = True
    credential_env: Literal["AI_API_KEY"] = "AI_API_KEY"
    profile_fingerprint: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_capacity(self) -> "ResolvedModelProfileV1":
        if self.max_output_tokens + self.safety_margin_tokens >= self.context_window_tokens:
            raise ValueError("model profile leaves no input-token capacity")
        return self


class EvidenceRefV1(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    evidence_id: str
    run_id: str
    task_id: str
    source_revision: str
    path: Optional[str] = None
    start_line: Optional[int] = Field(default=None, ge=1)
    end_line: Optional[int] = Field(default=None, ge=1)
    symbol: Optional[str] = None
    evidence_type: str
    retrieval_reason: str
    truth_status: TruthStatus
    content_sha256: str = Field(pattern=SHA256_PATTERN)
    parser_version: Optional[str] = None
    valid: bool = True

    @model_validator(mode="after")
    def validate_span(self) -> "EvidenceRefV1":
        if (self.start_line is None) != (self.end_line is None):
            raise ValueError("start_line and end_line must be provided together")
        if self.start_line is not None and self.end_line is not None and self.end_line < self.start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        return self


class ContextPinnedV1(StrictContract):
    task_spec_sha256: str = Field(pattern=SHA256_PATTERN)
    authorization_policy_sha256: str = Field(pattern=SHA256_PATTERN)
    role_schema_version: str
    candidate_version: Optional[str] = None


class ContextSectionV1(StrictContract):
    name: str
    item_ids: List[str]
    pinned: bool
    estimated_tokens: int = Field(ge=0)


class ContextBudgetV1(StrictContract):
    context_window_tokens: int = Field(gt=0)
    reserved_output_tokens: int = Field(gt=0)
    safety_margin_tokens: int = Field(ge=0)
    max_input_tokens: int = Field(ge=0)
    estimated_input_tokens: int = Field(ge=0)
    counter_mode: Literal["tokenizer", "conservative_estimate"]

    @model_validator(mode="after")
    def validate_equation(self) -> "ContextBudgetV1":
        expected = self.context_window_tokens - self.reserved_output_tokens - self.safety_margin_tokens
        if self.max_input_tokens != expected:
            raise ValueError("max_input_tokens must equal context_window_tokens - reserved_output_tokens - safety_margin_tokens")
        if self.estimated_input_tokens > self.max_input_tokens:
            raise ValueError("context packet exceeds maximum input capacity")
        return self


class ContextPacketV1(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    packet_id: str
    run_id: str
    task_id: str
    role: Role
    purpose: str
    packet_revision: int = Field(ge=1)
    source_revision: str
    pinned: ContextPinnedV1
    sections: List[ContextSectionV1]
    budget: ContextBudgetV1
    packet_sha256: str = Field(pattern=SHA256_PATTERN)


class AcceptanceCriterionV1(StrictContract):
    criterion_id: str
    statement: str
    evidence_needed: str


class HypothesisV1(StrictContract):
    hypothesis_id: str
    statement: str
    evidence_ids: List[str] = Field(default_factory=list)
    confidence: Literal["low", "medium", "high"]


class EditLocationV1(StrictContract):
    path: str
    symbol: Optional[str] = None
    reason: str

    @field_validator("path")
    @classmethod
    def validate_path(cls, path: str) -> str:
        if not path or path.startswith(("/", "\\")) or ".." in path.replace("\\", "/").split("/"):
            raise ValueError(f"unsafe edit location: {path}")
        return path


class PlanStepV1(StrictContract):
    step_id: str
    purpose: str
    depends_on: List[str] = Field(default_factory=list)


class PlanV1(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    decision: Literal["PLAN_READY"]
    task_id: str
    task_revision: int = Field(ge=1)
    plan_revision: int = Field(ge=1)
    objective: str
    preserved_constraints: List[str] = Field(default_factory=list)
    acceptance_criteria: List[AcceptanceCriterionV1] = Field(min_length=1)
    observed_evidence_ids: List[str] = Field(default_factory=list)
    hypotheses: List[HypothesisV1] = Field(default_factory=list)
    likely_edit_locations: List[EditLocationV1] = Field(default_factory=list)
    steps: List[PlanStepV1] = Field(min_length=1)
    verification_strategy: List[str] = Field(min_length=1)
    unresolved_questions: List[str] = Field(default_factory=list)
    required_capabilities: List[str] = Field(default_factory=list)
    step_budget: int = Field(ge=1, le=100)

    @model_validator(mode="after")
    def validate_identifiers_and_dependencies(self) -> "PlanV1":
        criterion_ids = [item.criterion_id for item in self.acceptance_criteria]
        if len(criterion_ids) != len(set(criterion_ids)):
            raise ValueError("acceptance criterion IDs must be unique")

        hypothesis_ids = [item.hypothesis_id for item in self.hypotheses]
        if len(hypothesis_ids) != len(set(hypothesis_ids)):
            raise ValueError("hypothesis IDs must be unique")

        step_ids = [item.step_id for item in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("plan step IDs must be unique")
        known_steps = set(step_ids)
        dependencies = {step.step_id: set(step.depends_on) for step in self.steps}
        for step_id, required in dependencies.items():
            if step_id in required:
                raise ValueError(f"plan step {step_id} cannot depend on itself")
            unknown = required - known_steps
            if unknown:
                raise ValueError(
                    f"plan step {step_id} has unknown dependencies: {', '.join(sorted(unknown))}"
                )

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(step_id: str) -> None:
            if step_id in visiting:
                raise ValueError("plan step dependencies contain a cycle")
            if step_id in visited:
                return
            visiting.add(step_id)
            for dependency in dependencies[step_id]:
                visit(dependency)
            visiting.remove(step_id)
            visited.add(step_id)

        for step_id in step_ids:
            visit(step_id)
        return self


class EvidenceQueryV1(StrictContract):
    query_type: Literal[
        "PATH_GLOB",
        "EXACT_TEXT",
        "IDENTIFIER",
        "SYMBOL_DEFINITION",
        "SYMBOL_REFERENCES",
        "STACK_TRACE_LOCATION",
        "ADJACENT_TESTS",
        "IMPORT_NEIGHBORS",
        "MANIFEST_OR_CONFIG",
        "INSTRUCTION_FILE",
    ]
    query: str = Field(min_length=1, max_length=512)
    max_results: int = Field(default=10, ge=1, le=50)
    max_bytes: int = Field(default=32768, ge=256, le=262144)


class PlannerNeedsEvidenceV1(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    decision: Literal["NEEDS_EVIDENCE"]
    task_id: str
    queries: List[EvidenceQueryV1] = Field(min_length=1, max_length=10)
    reason: str


class MaterialQuestionV1(StrictContract):
    question: str
    impact: str


class PlannerNeedsInputV1(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    decision: Literal["NEEDS_INPUT"]
    task_id: str
    questions: List[MaterialQuestionV1] = Field(min_length=1, max_length=3)


class NeedCapabilityV1(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    decision: Literal["NEED_CAPABILITY"]
    task_id: str
    capability: str
    reason: str
    affected_acceptance_criteria: List[str] = Field(default_factory=list)


PlannerDecisionV1 = Annotated[
    Union[PlanV1, PlannerNeedsEvidenceV1, PlannerNeedsInputV1, NeedCapabilityV1],
    Field(discriminator="decision"),
]


class CoderDecisionV1(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    decision: Literal["CODE"]
    task_id: str
    task_revision: int = Field(ge=1)
    plan_revision: int = Field(ge=1)
    workspace_version: str
    purpose: str
    requested_capabilities: List[str]
    declared_paths: List[str]
    python_action: str = Field(min_length=1, max_length=65536)
    success_observations: List[str] = Field(min_length=1)
    max_action_seconds: int = Field(gt=0, le=300)

    @field_validator("declared_paths")
    @classmethod
    def validate_declared_paths(cls, paths: List[str]) -> List[str]:
        for path in paths:
            if not path or path.startswith(("/", "\\")) or ".." in path.split("/"):
                raise ValueError(f"unsafe declared path: {path}")
        return paths


class CoderCompleteV1(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    decision: Literal["COMPLETE"]
    task_id: str
    task_revision: int = Field(ge=1)
    plan_revision: int = Field(ge=1)
    claimed_outcome: str
    evidence_ids: List[str] = Field(default_factory=list)


class CoderReplanV1(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    decision: Literal["REPLAN"]
    task_id: str
    task_revision: int = Field(ge=1)
    plan_revision: int = Field(ge=1)
    contradicted_assumption: str
    evidence_ids: List[str] = Field(min_length=1)
    requested_change: str


CoderRoleDecisionV1 = Annotated[
    Union[CoderDecisionV1, CoderCompleteV1, CoderReplanV1, NeedCapabilityV1],
    Field(discriminator="decision"),
]


class ValidatorFindingV1(StrictContract):
    severity: Literal["low", "medium", "high", "critical"]
    category: str
    statement: str
    evidence_ids: List[str] = Field(default_factory=list)


class ProposedCheckV1(StrictContract):
    check_id: str
    purpose: str
    kind: Literal["test_proposal", "lint_proposal", "inspection_proposal"]
    requested_scope: List[str] = Field(default_factory=list)


class OverlayTestFileV1(StrictContract):
    """A validator-proposed test that runs only in a separate overlay (PRD 4)."""

    path: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=100_000)
    purpose: str = Field(min_length=1, max_length=500)

    @field_validator("path")
    @classmethod
    def validate_overlay_path(cls, path: str) -> str:
        parts = path.split("/")
        if (
            not path.startswith("tests_overlay/")
            or not path.endswith(".py")
            or any(part in ("", ".", "..") for part in parts)
            or "\\" in path
            or len(parts) > 4
        ):
            raise ValueError("overlay tests must be .py files under tests_overlay/")
        return path


class ValidatorReviewV1(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    decision: Literal["NO_OBJECTION", "CHANGES_NEEDED", "INSUFFICIENT_EVIDENCE"]
    task_id: str
    candidate_hash: str = Field(pattern=SHA256_PATTERN)
    acceptance_contract_sha256: str = Field(pattern=SHA256_PATTERN)
    findings: List[ValidatorFindingV1] = Field(default_factory=list)
    proposed_checks: List[ProposedCheckV1] = Field(default_factory=list)
    unresolved_risks: List[str] = Field(default_factory=list)
    overlay_tests: List[OverlayTestFileV1] = Field(default_factory=list, max_length=10)
    narrative_trust: Literal["model_opinion_not_host_verification"] = "model_opinion_not_host_verification"


class ModelUsageV1(StrictContract):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    usage_source: Literal["provider_reported", "estimated", "unknown"]


class ModelCallResultV1(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    call_id: str
    role: Role
    state: Literal["SUCCEEDED", "FAILED", "CANCELLED", "UNKNOWN"]
    profile_fingerprint: str = Field(pattern=SHA256_PATTERN)
    packet_id: str
    response_artifact_id: Optional[str] = None
    parsed_output_sha256: Optional[str] = Field(default=None, pattern=SHA256_PATTERN)
    usage: ModelUsageV1
    latency_ms: int = Field(ge=0)
    format_retry_count: int = Field(default=0, ge=0, le=2)
    provider_request_id: Optional[str] = None


class PlanReferenceV1(StrictContract):
    plan_id: str
    revision: int = Field(ge=1)


class ProposalReferenceV1(StrictContract):
    action_proposal_id: str
    proposal_artifact_id: str
    decision: Literal["CODE"]


class PhaseUsageV1(StrictContract):
    model_calls_used: int = Field(ge=0)
    input_tokens_used: int = Field(ge=0)
    output_tokens_used: int = Field(ge=0)
    elapsed_seconds: int = Field(ge=0)
    verification_calls_reserved: int = Field(ge=2)


class CandidateReferenceV1(StrictContract):
    candidate_id: str
    commit: str
    candidate_sha256: str = Field(pattern=SHA256_PATTERN)
    changed_paths: List[str] = Field(default_factory=list)


class ActionReferenceV1(StrictContract):
    action_id: str
    settlement: str
    reason_codes: List[str] = Field(default_factory=list)


class VerificationReferenceV1(StrictContract):
    status: str
    completion_decision_id: Optional[str] = None
    report_artifact_id: Optional[str] = None
    repair_attempts: int = Field(default=0, ge=0)


class OrchestrationPhaseResultV1(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    run_id: str
    task_id: str
    status: OrchestrationState
    source_revision: str
    plan: Optional[PlanReferenceV1] = None
    proposal: Optional[ProposalReferenceV1] = None
    usage: PhaseUsageV1
    remaining_repository_tasks: int = Field(ge=0)
    questions: List[MaterialQuestionV1] = Field(default_factory=list)
    required_capability: Optional[str] = None
    next_required_prd: Optional[Literal[3, 4, 5]] = None
    workspace_version: Optional[str] = None
    actions_executed: int = Field(default=0, ge=0)
    last_action: Optional[ActionReferenceV1] = None
    candidate: Optional[CandidateReferenceV1] = None
    verification: Optional[VerificationReferenceV1] = None
    approval_request_id: Optional[str] = None
    stop_reason_code: Optional[str] = None
    created_at: str


class PRD3ActionHandoffV1(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    run_id: str
    task_id: str
    task_revision: int = Field(ge=1)
    lifecycle_version: int = Field(ge=1)
    workspace_version: str
    baseline_commit: str
    plan_id: str
    plan_revision: int = Field(ge=1)
    plan_artifact_id: str
    plan_sha256: str = Field(pattern=SHA256_PATTERN)
    action_proposal_id: str
    proposal_artifact_id: str
    code_artifact_id: str
    code_sha256: str = Field(pattern=SHA256_PATTERN)
    requested_capabilities: List[str]
    declared_paths: List[str]
    requested_timeout_seconds: int = Field(gt=0)
    authorization_policy_sha256: str = Field(pattern=SHA256_PATTERN)
    remaining_budget: Dict[str, int]


def is_sha256(value: str) -> bool:
    return bool(re.fullmatch(SHA256_PATTERN, value))
