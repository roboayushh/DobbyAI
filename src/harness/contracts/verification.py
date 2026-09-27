"""Versioned contracts for PRD 4: verification, feedback, and recovery."""
from __future__ import annotations

from enum import Enum
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

SHA256 = r"^[0-9a-f]{64}$"
ID = Field(min_length=1, max_length=128)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())


class CheckOrigin(str, Enum):
    USER_EXPLICIT = "USER_EXPLICIT"
    RUNTIME_ADAPTER = "RUNTIME_ADAPTER"
    REPOSITORY_DECLARED = "REPOSITORY_DECLARED"
    PLANNER_PROPOSED = "PLANNER_PROPOSED"
    VALIDATOR_PROPOSED = "VALIDATOR_PROPOSED"
    HARNESS_INVARIANT = "HARNESS_INVARIANT"


class CheckStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    TIMEOUT = "TIMEOUT"
    OOM = "OOM"
    OUTPUT_LIMIT = "OUTPUT_LIMIT"
    ZERO_TESTS = "ZERO_TESTS"
    ALL_SKIPPED = "ALL_SKIPPED"
    BLOCKED_ENVIRONMENT = "BLOCKED_ENVIRONMENT"
    UNPARSABLE = "UNPARSABLE"
    UNEXPECTED_MUTATION = "UNEXPECTED_MUTATION"
    CANCELLED = "CANCELLED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class TaskOutcome(str, Enum):
    PASS = "PASS"
    FAILED = "FAILED"
    UNVERIFIED = "UNVERIFIED"
    BLOCKED_ENVIRONMENT = "BLOCKED_ENVIRONMENT"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    NEEDS_INPUT = "NEEDS_INPUT"
    CANCELLED = "CANCELLED"


class ContractBaselineV1(_Strict):
    commit: str = Field(min_length=1, max_length=128)
    content_tree_sha256: str = Field(pattern=SHA256)


class ContractCriterionV1(_Strict):
    criterion_id: str = Field(min_length=1, max_length=128)
    statement: str = Field(min_length=1, max_length=2000)
    required: bool = True
    check_ids: List[str] = Field(max_length=32)


class ContractCheckV1(_Strict):
    check_id: str = Field(min_length=1, max_length=128)
    origin: CheckOrigin
    kind: Literal["test", "lint", "typecheck", "build", "smoke", "command", "invariant"]
    tier: Literal["focused", "relevant", "broad", "invariant", "validator"]
    required: bool
    baseline_policy: Literal["required", "optional", "not_applicable"]
    argv: List[str] = Field(min_length=1, max_length=256)
    cwd: str = "."
    timeout_seconds: int = Field(gt=0, le=1200)
    parser: str = Field(min_length=1, max_length=64)
    minimum_tests: int = Field(default=0, ge=0)
    allowed_workspace_outputs: List[str] = Field(default_factory=list, max_length=32)


class VerificationContractV1(_Strict):
    schema_version: Literal["1.0"] = "1.0"
    contract_id: str = ID
    run_id: str = ID
    task_id: str = ID
    task_revision: int = Field(ge=1)
    plan_id: str = ID
    plan_revision: int = Field(ge=1)
    baseline: ContractBaselineV1
    criteria: List[ContractCriterionV1] = Field(min_length=1, max_length=64)
    checks: List[ContractCheckV1] = Field(min_length=1, max_length=32)
    prohibited_shortcuts: List[str] = Field(max_length=32)
    validator_required: bool = True
    max_validator_overlay_files: int = Field(default=10, ge=0, le=50)
    max_validator_overlay_bytes: int = Field(default=200_000, ge=0, le=2_000_000)
    flake_retries: int = Field(default=1, ge=0, le=3)
    max_repair_attempts: int = Field(default=2, ge=0, le=10)
    runtime_profile_fingerprint: str = Field(pattern=SHA256)
    policy_sha256: str = Field(pattern=SHA256)
    contract_sha256: str = Field(pattern=SHA256)
    state: Literal["DRAFT", "FROZEN", "SUPERSEDED", "INVALIDATED"]


class ProcessSummaryV1(_Strict):
    exit_code: Optional[int] = None
    signal: Optional[int] = None
    timed_out: bool = False
    oom_killed: bool = False
    output_limit_exceeded: bool = False
    elapsed_ms: int = Field(ge=0)


class TestCountsV1(_Strict):
    discovered: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    skipped: int = Field(ge=0)
    errors: int = Field(ge=0)


class BaselineResultV1(_Strict):
    schema_version: Literal["1.0"] = "1.0"
    baseline_result_id: str = ID
    contract_id: str = ID
    check_id: str = ID
    baseline_commit: str = Field(min_length=1, max_length=128)
    environment_sha256: str = Field(pattern=SHA256)
    status: Literal[
        "PASS", "EXPECTED_REPRODUCTION_FAILURE", "PRE_EXISTING_FAILURE", "BLOCKED_ENVIRONMENT",
        "TIMEOUT", "OOM", "OUTPUT_LIMIT", "ZERO_TESTS", "ALL_SKIPPED", "UNPARSABLE", "FLAKY",
        "UNEXPECTED_MUTATION", "CANCELLED", "INTERNAL_ERROR",
    ]
    process: ProcessSummaryV1
    tests: TestCountsV1
    failure_signatures: List[str] = Field(default_factory=list, max_length=10_000)
    stdout_artifact_id: Optional[str] = None
    stderr_artifact_id: Optional[str] = None
    report_artifact_id: Optional[str] = None
    captured_at: str


class VerificationAttemptRequestV1(_Strict):
    schema_version: Literal["1.0"] = "1.0"
    attempt_id: str = ID
    attempt_number: int = Field(ge=1)
    run_id: str = ID
    task_id: str = ID
    candidate_id: str = ID
    candidate_sha256: str = Field(pattern=SHA256)
    contract_id: str = ID
    contract_sha256: str = Field(pattern=SHA256)
    test_set_sha256: str = Field(pattern=SHA256)
    runtime_profile_fingerprint: str = Field(pattern=SHA256)
    dependency_environment_sha256: Optional[str] = Field(default=None, pattern=SHA256)
    network_mode: Literal["none"] = "none"
    required_check_ids: List[str] = Field(max_length=32)


class CheckRunResultV1(_Strict):
    schema_version: Literal["1.0"] = "1.0"
    check_run_id: str = ID
    attempt_id: str = ID
    check_id: str = ID
    candidate_sha256: str = Field(pattern=SHA256)
    overlay_sha256: Optional[str] = Field(default=None, pattern=SHA256)
    environment_sha256: str = Field(pattern=SHA256)
    command_sha256: str = Field(pattern=SHA256)
    status: CheckStatus
    process: ProcessSummaryV1
    tests: TestCountsV1
    unexpected_source_mutation: bool
    stdout_artifact_id: Optional[str] = None
    stderr_artifact_id: Optional[str] = None
    report_artifact_id: Optional[str] = None
    settled_at: str


class ComparisonSummaryV1(_Strict):
    resolved_targets: int = Field(ge=0)
    new_regressions: int = Field(ge=0)
    pre_existing_unchanged: int = Field(ge=0)
    changed_failures: int = Field(ge=0)
    coverage_lost: int = Field(ge=0)
    inconclusive: int = Field(ge=0)


class CriterionResultV1(_Strict):
    criterion_id: str = Field(min_length=1, max_length=128)
    status: Literal["SATISFIED", "NOT_SATISFIED", "INCONCLUSIVE", "NOT_RUN"]
    evidence_refs: List[str] = Field(default_factory=list, max_length=64)


class RegressionComparisonV1(_Strict):
    schema_version: Literal["1.0"] = "1.0"
    comparison_id: str = ID
    attempt_id: str = ID
    candidate_sha256: str = Field(pattern=SHA256)
    contract_sha256: str = Field(pattern=SHA256)
    summary: ComparisonSummaryV1
    criterion_results: List[CriterionResultV1] = Field(max_length=64)
    comparison_sha256: str = Field(pattern=SHA256)


class OverlayFileRefV1(_Strict):
    path: str = Field(min_length=1, max_length=256)
    content_sha256: str = Field(pattern=SHA256)
    content_artifact_id: str = ID


class ValidatorFindingRecordV1(_Strict):
    severity: Literal["low", "medium", "high", "critical"]
    category: str = Field(min_length=1, max_length=128)
    statement: str = Field(min_length=1, max_length=4000)
    evidence_refs: List[str] = Field(default_factory=list, max_length=32)


class OverlayCheckProposalV1(_Strict):
    proposal_id: str = ID
    purpose: str = Field(min_length=1, max_length=1000)
    files: List[OverlayFileRefV1] = Field(max_length=50)
    argv: List[str] = Field(min_length=1, max_length=64)
    timeout_seconds: int = Field(gt=0, le=600)
    parser: str = Field(min_length=1, max_length=64)


class ValidatorOverlayProposalV1(_Strict):
    schema_version: Literal["1.0"] = "1.0"
    review_id: str = ID
    candidate_sha256: str = Field(pattern=SHA256)
    decision: Literal["NO_OBJECTION", "CHANGES_NEEDED", "INSUFFICIENT_EVIDENCE"]
    findings: List[ValidatorFindingRecordV1] = Field(default_factory=list, max_length=50)
    proposed_checks: List[OverlayCheckProposalV1] = Field(default_factory=list, max_length=10)
    review_sha256: str = Field(pattern=SHA256)


class TestChangeSummaryV1(_Strict):
    existing_tests_deleted: int = Field(ge=0)
    skip_markers_added: int = Field(ge=0)
    assertions_weakened_suspected: int = Field(ge=0)
    discovery_config_changed: bool


class ScopeFindingV1(_Strict):
    severity: Literal["WARN", "BLOCKING"]
    category: str = Field(min_length=1, max_length=64)
    path: Optional[str] = None
    detail: str = Field(min_length=1, max_length=1000)


class DiffScopeReviewV1(_Strict):
    schema_version: Literal["1.0"] = "1.0"
    diff_review_id: str = ID
    candidate_id: str = ID
    candidate_sha256: str = Field(pattern=SHA256)
    status: Literal["CLEAN", "WARN", "BLOCKING"]
    changed_paths: List[str] = Field(max_length=25_000)
    test_changes: TestChangeSummaryV1
    scope_findings: List[ScopeFindingV1] = Field(default_factory=list, max_length=500)
    review_artifact_id: str = ID
    review_sha256: str = Field(pattern=SHA256)


class RequiredCheckCountsV1(_Strict):
    total: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    inconclusive: int = Field(ge=0)


class CompletionDecisionV1(_Strict):
    schema_version: Literal["1.0"] = "1.0"
    decision_id: str = ID
    attempt_id: str = ID
    candidate_id: str = ID
    status: TaskOutcome
    reason_codes: List[str] = Field(max_length=64)
    criteria: List[CriterionResultV1] = Field(max_length=64)
    required_checks: RequiredCheckCountsV1
    validator_decision: Optional[Literal["NO_OBJECTION", "CHANGES_NEEDED", "INSUFFICIENT_EVIDENCE", "NOT_RUN"]] = None
    diff_review_status: Literal["CLEAN", "WARN", "BLOCKING"]
    candidate_sha256: str = Field(pattern=SHA256)
    test_set_sha256: str = Field(pattern=SHA256)
    environment_set_sha256: str = Field(pattern=SHA256)
    report_artifact_id: str = ID
    decided_at: str


class CheckFailureV1(_Strict):
    check_run_id: str = ID
    check_id: Optional[str] = None
    status: CheckStatus
    failure_signature: Optional[str] = Field(default=None, pattern=SHA256)
    log_artifact_ids: List[str] = Field(default_factory=list, max_length=8)
    failing_tests: List[str] = Field(default_factory=list, max_length=50)
    output_excerpt: str = Field(default="", max_length=8000)


class RepairFeedbackV1(_Strict):
    schema_version: Literal["1.0"] = "1.0"
    repair_attempt_id: str = ID
    task_id: str = ID
    from_candidate_id: str = ID
    repair_number: int = Field(ge=1)
    trigger: str = Field(min_length=1, max_length=64)
    failed_criteria: List[str] = Field(default_factory=list, max_length=64)
    new_regressions: List[str] = Field(default_factory=list, max_length=200)
    check_failures: List[CheckFailureV1] = Field(default_factory=list, max_length=32)
    diff_findings: List[ScopeFindingV1] = Field(default_factory=list, max_length=100)
    validator_findings: List[ValidatorFindingRecordV1] = Field(default_factory=list, max_length=50)
    workspace_start_candidate_sha256: str = Field(pattern=SHA256)
    remaining_repairs: int = Field(ge=0)
    feedback_sha256: str = Field(pattern=SHA256)


class ReportBaselineV1(_Strict):
    state: Literal["CAPTURED", "PARTIAL", "BLOCKED", "NOT_APPLICABLE", "NOT_CAPTURED"]
    commit: str = Field(min_length=1, max_length=128)
    limitations: List[str] = Field(default_factory=list, max_length=64)


class ReportChecksV1(_Strict):
    required_total: int = Field(ge=0)
    required_passed: int = Field(ge=0)
    optional_total: int = Field(ge=0)
    optional_passed: int = Field(ge=0)
    not_run: List[str] = Field(default_factory=list, max_length=64)


class ReportRegressionsV1(_Strict):
    new: int = Field(ge=0)
    pre_existing: int = Field(ge=0)
    resolved_targets: int = Field(ge=0)


class ReportValidatorV1(_Strict):
    decision: str = Field(min_length=1, max_length=64)
    overlay_checks_run: int = Field(ge=0)


class ReportDiffReviewV1(_Strict):
    status: Literal["CLEAN", "WARN", "BLOCKING"]
    warnings: List[str] = Field(default_factory=list, max_length=200)


class ReportUsageV1(_Strict):
    verification_attempts: int = Field(ge=0)
    check_runs: int = Field(ge=0)
    validator_calls: int = Field(ge=0)
    repair_attempts: int = Field(ge=0)
    wall_seconds: int = Field(ge=0)


class ReportArtifactsV1(_Strict):
    report: str = ID
    candidate_diff: str = ID
    comparison: Optional[str] = None


class VerificationReportV1(_Strict):
    schema_version: Literal["1.0"] = "1.0"
    run_id: str = ID
    task_id: str = ID
    candidate_id: str = ID
    status: TaskOutcome
    baseline: ReportBaselineV1
    candidate_sha256: str = Field(pattern=SHA256)
    contract_sha256: str = Field(pattern=SHA256)
    checks: ReportChecksV1
    regressions: ReportRegressionsV1
    validator: ReportValidatorV1
    diff_review: ReportDiffReviewV1
    usage: ReportUsageV1
    artifacts: ReportArtifactsV1
    limitations: List[str] = Field(default_factory=list, max_length=64)


class HandoffVerificationV1(_Strict):
    completion_decision_id: str = ID
    contract_sha256: str = Field(pattern=SHA256)
    test_set_sha256: str = Field(pattern=SHA256)
    environment_set_sha256: str = Field(pattern=SHA256)
    new_regressions: int = Field(ge=0)
    report_artifact_id: str = ID


class HandoffCandidateRefV1(_Strict):
    candidate_id: str = ID
    commit: str = Field(min_length=1, max_length=128)
    tree: str = Field(min_length=1, max_length=128)
    sha256: str = Field(pattern=SHA256)


class QueueBudgetRemainingV1(_Strict):
    model_calls: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    wall_seconds: int = Field(ge=0)


class VerifiedTaskHandoffV1(_Strict):
    schema_version: Literal["1.0"] = "1.0"
    run_id: str = ID
    task_id: str = ID
    task_ordinal: int = Field(ge=0)
    task_start_commit: str = Field(min_length=1, max_length=128)
    outcome: TaskOutcome
    candidate: HandoffCandidateRefV1
    verification: HandoffVerificationV1
    changed_paths: List[str] = Field(max_length=25_000)
    queue_budget_remaining: QueueBudgetRemainingV1
    publication_authorized: Literal[False] = False
