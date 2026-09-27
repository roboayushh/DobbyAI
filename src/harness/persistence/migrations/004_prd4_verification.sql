-- PRD 4: verification contracts, baselines, candidate-bound checks, validator
-- review, completion decisions, repair, and task outcomes. Additive only:
-- PRD 3 candidate snapshots stay immutable; verification state is projected
-- into a separate table.

CREATE TABLE h_verification_contracts (
    contract_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES h_runs(run_id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES h_tasks(task_id) ON DELETE CASCADE,
    task_revision INTEGER NOT NULL CHECK (task_revision >= 1),
    plan_id TEXT NOT NULL REFERENCES h_plans(plan_id),
    plan_revision INTEGER NOT NULL CHECK (plan_revision >= 1),
    baseline_commit TEXT NOT NULL,
    baseline_content_sha256 TEXT NOT NULL CHECK (length(baseline_content_sha256) = 64),
    runtime_profile_fingerprint TEXT NOT NULL CHECK (length(runtime_profile_fingerprint) = 64),
    policy_sha256 TEXT NOT NULL CHECK (length(policy_sha256) = 64),
    contract_json TEXT NOT NULL,
    contract_sha256 TEXT NOT NULL CHECK (length(contract_sha256) = 64),
    test_set_sha256 TEXT NOT NULL CHECK (length(test_set_sha256) = 64),
    artifact_id TEXT NOT NULL REFERENCES h_artifacts(artifact_id),
    state TEXT NOT NULL CHECK (state IN ('DRAFT', 'FROZEN', 'SUPERSEDED', 'INVALIDATED')),
    created_at TEXT NOT NULL,
    frozen_at TEXT,
    UNIQUE (task_id, task_revision, plan_revision, contract_sha256)
);

CREATE UNIQUE INDEX h_verification_contracts_one_frozen_idx
    ON h_verification_contracts(task_id)
    WHERE state = 'FROZEN';

CREATE TABLE h_verification_checks (
    verification_check_id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL REFERENCES h_verification_contracts(contract_id) ON DELETE CASCADE,
    external_check_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    origin TEXT NOT NULL CHECK (origin IN (
        'USER_EXPLICIT', 'RUNTIME_ADAPTER', 'REPOSITORY_DECLARED',
        'PLANNER_PROPOSED', 'VALIDATOR_PROPOSED', 'HARNESS_INVARIANT'
    )),
    kind TEXT NOT NULL CHECK (kind IN ('test', 'lint', 'typecheck', 'build', 'smoke', 'command', 'invariant')),
    tier TEXT NOT NULL CHECK (tier IN ('focused', 'relevant', 'broad', 'invariant', 'validator')),
    required INTEGER NOT NULL CHECK (required IN (0, 1)),
    baseline_policy TEXT NOT NULL CHECK (baseline_policy IN ('required', 'optional', 'not_applicable')),
    argv_json TEXT NOT NULL,
    working_directory TEXT NOT NULL,
    timeout_seconds INTEGER NOT NULL CHECK (timeout_seconds > 0),
    parser_id TEXT NOT NULL,
    minimum_tests INTEGER NOT NULL DEFAULT 0 CHECK (minimum_tests >= 0),
    allowed_outputs_json TEXT NOT NULL,
    check_sha256 TEXT NOT NULL CHECK (length(check_sha256) = 64),
    created_at TEXT NOT NULL,
    UNIQUE (contract_id, external_check_id),
    UNIQUE (contract_id, ordinal)
);

CREATE TABLE h_verification_budgets (
    run_id TEXT PRIMARY KEY REFERENCES h_runs(run_id) ON DELETE CASCADE,
    max_check_runs INTEGER NOT NULL CHECK (max_check_runs >= 0),
    max_wall_seconds INTEGER NOT NULL CHECK (max_wall_seconds >= 0),
    max_output_bytes INTEGER NOT NULL CHECK (max_output_bytes >= 0),
    max_repair_attempts INTEGER NOT NULL CHECK (max_repair_attempts >= 0),
    used_check_runs INTEGER NOT NULL DEFAULT 0 CHECK (used_check_runs >= 0),
    reserved_check_runs INTEGER NOT NULL DEFAULT 0 CHECK (reserved_check_runs >= 0),
    used_wall_seconds INTEGER NOT NULL DEFAULT 0 CHECK (used_wall_seconds >= 0),
    reserved_wall_seconds INTEGER NOT NULL DEFAULT 0 CHECK (reserved_wall_seconds >= 0),
    used_output_bytes INTEGER NOT NULL DEFAULT 0 CHECK (used_output_bytes >= 0),
    reserved_output_bytes INTEGER NOT NULL DEFAULT 0 CHECK (reserved_output_bytes >= 0),
    used_repair_attempts INTEGER NOT NULL DEFAULT 0 CHECK (used_repair_attempts >= 0),
    updated_at TEXT NOT NULL,
    CHECK (used_check_runs + reserved_check_runs <= max_check_runs),
    CHECK (used_wall_seconds + reserved_wall_seconds <= max_wall_seconds),
    CHECK (used_output_bytes + reserved_output_bytes <= max_output_bytes),
    CHECK (used_repair_attempts <= max_repair_attempts)
);

CREATE TABLE h_baseline_captures (
    baseline_capture_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES h_runs(run_id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES h_tasks(task_id) ON DELETE CASCADE,
    contract_id TEXT NOT NULL UNIQUE REFERENCES h_verification_contracts(contract_id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK (state IN (
        'REQUESTED', 'RUNNING', 'CAPTURED', 'PARTIAL',
        'BLOCKED', 'NOT_APPLICABLE', 'NOT_CAPTURED'
    )),
    limitation_json TEXT NOT NULL,
    started_at TEXT,
    settled_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE h_baseline_runs (
    baseline_run_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES h_runs(run_id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES h_tasks(task_id) ON DELETE CASCADE,
    contract_id TEXT NOT NULL REFERENCES h_verification_contracts(contract_id) ON DELETE CASCADE,
    baseline_capture_id TEXT NOT NULL REFERENCES h_baseline_captures(baseline_capture_id) ON DELETE CASCADE,
    verification_check_id TEXT NOT NULL REFERENCES h_verification_checks(verification_check_id),
    attempt_no INTEGER NOT NULL CHECK (attempt_no >= 1),
    baseline_commit TEXT NOT NULL,
    environment_sha256 TEXT NOT NULL CHECK (length(environment_sha256) = 64),
    command_sha256 TEXT NOT NULL CHECK (length(command_sha256) = 64),
    status TEXT NOT NULL CHECK (status IN (
        'PASS', 'EXPECTED_REPRODUCTION_FAILURE', 'PRE_EXISTING_FAILURE',
        'BLOCKED_ENVIRONMENT', 'TIMEOUT', 'OOM', 'OUTPUT_LIMIT',
        'ZERO_TESTS', 'ALL_SKIPPED', 'UNPARSABLE', 'FLAKY',
        'UNEXPECTED_MUTATION', 'CANCELLED', 'INTERNAL_ERROR'
    )),
    exit_code INTEGER,
    elapsed_ms INTEGER NOT NULL CHECK (elapsed_ms >= 0),
    discovered_count INTEGER,
    passed_count INTEGER,
    failed_count INTEGER,
    skipped_count INTEGER,
    error_count INTEGER,
    stdout_artifact_id TEXT REFERENCES h_artifacts(artifact_id),
    stderr_artifact_id TEXT REFERENCES h_artifacts(artifact_id),
    report_artifact_id TEXT REFERENCES h_artifacts(artifact_id),
    cases_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL,
    settled_at TEXT NOT NULL,
    UNIQUE (contract_id, verification_check_id, attempt_no)
);

CREATE TABLE h_action_verification_bindings (
    action_id TEXT PRIMARY KEY REFERENCES h_actions(action_id) ON DELETE CASCADE,
    contract_id TEXT NOT NULL REFERENCES h_verification_contracts(contract_id),
    baseline_capture_id TEXT NOT NULL REFERENCES h_baseline_captures(baseline_capture_id),
    contract_sha256 TEXT NOT NULL CHECK (length(contract_sha256) = 64),
    binding_sha256 TEXT NOT NULL CHECK (length(binding_sha256) = 64),
    created_at TEXT NOT NULL
);

CREATE TABLE h_candidate_verification_state (
    candidate_id TEXT PRIMARY KEY REFERENCES h_candidate_snapshots(candidate_id) ON DELETE CASCADE,
    contract_id TEXT NOT NULL REFERENCES h_verification_contracts(contract_id),
    state TEXT NOT NULL CHECK (state IN (
        'NOT_RUN', 'PREFLIGHT', 'VERIFYING', 'VALIDATING',
        'PASS', 'FAILED', 'UNVERIFIED', 'BLOCKED_ENVIRONMENT',
        'BUDGET_EXHAUSTED', 'NEEDS_INPUT', 'CANCELLED', 'INVALIDATED'
    )),
    active_attempt_id TEXT,
    state_version INTEGER NOT NULL DEFAULT 1 CHECK (state_version >= 1),
    invalidation_reason TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE h_verification_attempts (
    attempt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES h_runs(run_id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES h_tasks(task_id) ON DELETE CASCADE,
    candidate_id TEXT NOT NULL REFERENCES h_candidate_snapshots(candidate_id) ON DELETE CASCADE,
    contract_id TEXT NOT NULL REFERENCES h_verification_contracts(contract_id),
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    candidate_sha256 TEXT NOT NULL CHECK (length(candidate_sha256) = 64),
    contract_sha256 TEXT NOT NULL CHECK (length(contract_sha256) = 64),
    test_set_sha256 TEXT NOT NULL CHECK (length(test_set_sha256) = 64),
    request_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'REQUESTED', 'RUNNING_CHECKS', 'RUNNING_VALIDATOR',
        'COMPLETION_GATE', 'SETTLED', 'CANCELLED', 'FAILED_INTERNAL'
    )),
    started_at TEXT NOT NULL,
    settled_at TEXT,
    UNIQUE (candidate_id, attempt_number)
);

CREATE TABLE h_verification_environments (
    verification_environment_id TEXT PRIMARY KEY,
    attempt_id TEXT REFERENCES h_verification_attempts(attempt_id) ON DELETE CASCADE,
    baseline_capture_id TEXT REFERENCES h_baseline_captures(baseline_capture_id) ON DELETE CASCADE,
    verification_check_id TEXT REFERENCES h_verification_checks(verification_check_id),
    runtime_profile_row_id TEXT NOT NULL REFERENCES h_runtime_profiles(runtime_profile_row_id),
    dependency_environment_id TEXT REFERENCES h_dependency_environments(dependency_environment_id),
    source_commit TEXT NOT NULL,
    candidate_sha256 TEXT NOT NULL CHECK (length(candidate_sha256) = 64),
    overlay_sha256 TEXT,
    environment_sha256 TEXT NOT NULL CHECK (length(environment_sha256) = 64),
    container_name TEXT NOT NULL,
    container_id_hash TEXT,
    before_manifest_artifact_id TEXT NOT NULL REFERENCES h_artifacts(artifact_id),
    after_manifest_artifact_id TEXT REFERENCES h_artifacts(artifact_id),
    state TEXT NOT NULL CHECK (state IN ('CREATED', 'RUNNING', 'STOPPED', 'DISCARDED', 'UNKNOWN')),
    created_at TEXT NOT NULL,
    stopped_at TEXT,
    CHECK (attempt_id IS NOT NULL OR baseline_capture_id IS NOT NULL)
);

CREATE TABLE h_validator_reviews (
    validator_review_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL UNIQUE REFERENCES h_verification_attempts(attempt_id) ON DELETE CASCADE,
    candidate_id TEXT NOT NULL REFERENCES h_candidate_snapshots(candidate_id),
    model_call_id TEXT REFERENCES h_model_calls(call_id),
    packet_id TEXT REFERENCES h_context_packets(packet_id),
    decision TEXT NOT NULL CHECK (decision IN ('NO_OBJECTION', 'CHANGES_NEEDED', 'INSUFFICIENT_EVIDENCE', 'NOT_RUN')),
    review_artifact_id TEXT NOT NULL REFERENCES h_artifacts(artifact_id),
    review_sha256 TEXT NOT NULL CHECK (length(review_sha256) = 64),
    blocking_finding_count INTEGER NOT NULL DEFAULT 0 CHECK (blocking_finding_count >= 0),
    skip_reason TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE h_test_overlays (
    overlay_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL REFERENCES h_verification_attempts(attempt_id) ON DELETE CASCADE,
    validator_review_id TEXT NOT NULL REFERENCES h_validator_reviews(validator_review_id) ON DELETE CASCADE,
    candidate_id TEXT NOT NULL REFERENCES h_candidate_snapshots(candidate_id),
    overlay_artifact_id TEXT NOT NULL REFERENCES h_artifacts(artifact_id),
    overlay_sha256 TEXT NOT NULL CHECK (length(overlay_sha256) = 64),
    file_count INTEGER NOT NULL CHECK (file_count >= 0),
    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
    state TEXT NOT NULL CHECK (state IN ('PROPOSED', 'ADMITTED', 'EXECUTED', 'REJECTED', 'INVALIDATED')),
    rejection_reason TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (attempt_id, overlay_sha256)
);

CREATE TABLE h_validator_check_proposals (
    validator_check_proposal_id TEXT PRIMARY KEY,
    validator_review_id TEXT NOT NULL REFERENCES h_validator_reviews(validator_review_id) ON DELETE CASCADE,
    overlay_id TEXT REFERENCES h_test_overlays(overlay_id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    proposal_json TEXT NOT NULL,
    proposal_sha256 TEXT NOT NULL CHECK (length(proposal_sha256) = 64),
    admission_state TEXT NOT NULL CHECK (admission_state IN ('PROPOSED', 'ADMITTED', 'REJECTED', 'EXECUTED')),
    reason_code TEXT,
    UNIQUE (validator_review_id, ordinal)
);

CREATE TABLE h_check_runs (
    check_run_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL REFERENCES h_verification_attempts(attempt_id) ON DELETE CASCADE,
    verification_check_id TEXT REFERENCES h_verification_checks(verification_check_id),
    validator_check_proposal_id TEXT REFERENCES h_validator_check_proposals(validator_check_proposal_id),
    verification_environment_id TEXT NOT NULL REFERENCES h_verification_environments(verification_environment_id),
    run_number INTEGER NOT NULL CHECK (run_number >= 1),
    candidate_sha256 TEXT NOT NULL CHECK (length(candidate_sha256) = 64),
    overlay_sha256 TEXT,
    environment_sha256 TEXT NOT NULL CHECK (length(environment_sha256) = 64),
    command_sha256 TEXT NOT NULL CHECK (length(command_sha256) = 64),
    status TEXT NOT NULL CHECK (status IN (
        'PASS', 'FAIL', 'TIMEOUT', 'OOM', 'OUTPUT_LIMIT', 'ZERO_TESTS',
        'ALL_SKIPPED', 'BLOCKED_ENVIRONMENT', 'UNPARSABLE',
        'UNEXPECTED_MUTATION', 'CANCELLED', 'INTERNAL_ERROR'
    )),
    exit_code INTEGER,
    signal INTEGER,
    elapsed_ms INTEGER NOT NULL CHECK (elapsed_ms >= 0),
    discovered_count INTEGER,
    passed_count INTEGER,
    failed_count INTEGER,
    skipped_count INTEGER,
    error_count INTEGER,
    unexpected_source_mutation INTEGER NOT NULL CHECK (unexpected_source_mutation IN (0, 1)),
    stdout_artifact_id TEXT REFERENCES h_artifacts(artifact_id),
    stderr_artifact_id TEXT REFERENCES h_artifacts(artifact_id),
    report_artifact_id TEXT REFERENCES h_artifacts(artifact_id),
    result_json TEXT NOT NULL,
    settled_at TEXT NOT NULL,
    CHECK (
        (verification_check_id IS NOT NULL AND validator_check_proposal_id IS NULL) OR
        (verification_check_id IS NULL AND validator_check_proposal_id IS NOT NULL)
    )
);

CREATE UNIQUE INDEX h_check_runs_contract_unique_idx
    ON h_check_runs(attempt_id, verification_check_id, run_number)
    WHERE verification_check_id IS NOT NULL;
CREATE UNIQUE INDEX h_check_runs_validator_unique_idx
    ON h_check_runs(attempt_id, validator_check_proposal_id, run_number)
    WHERE validator_check_proposal_id IS NOT NULL;

CREATE TABLE h_test_case_results (
    test_case_result_id TEXT PRIMARY KEY,
    check_run_id TEXT NOT NULL REFERENCES h_check_runs(check_run_id) ON DELETE CASCADE,
    normalized_test_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PASS', 'FAIL', 'SKIP', 'ERROR')),
    duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
    failure_signature_sha256 TEXT,
    raw_name_sha256 TEXT NOT NULL CHECK (length(raw_name_sha256) = 64),
    UNIQUE (check_run_id, normalized_test_id)
);

CREATE TABLE h_diff_scope_reviews (
    diff_review_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL UNIQUE REFERENCES h_verification_attempts(attempt_id) ON DELETE CASCADE,
    candidate_id TEXT NOT NULL REFERENCES h_candidate_snapshots(candidate_id),
    status TEXT NOT NULL CHECK (status IN ('CLEAN', 'WARN', 'BLOCKING')),
    review_artifact_id TEXT NOT NULL REFERENCES h_artifacts(artifact_id),
    review_sha256 TEXT NOT NULL CHECK (length(review_sha256) = 64),
    finding_count INTEGER NOT NULL CHECK (finding_count >= 0),
    created_at TEXT NOT NULL
);

CREATE TABLE h_regression_comparisons (
    comparison_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL UNIQUE REFERENCES h_verification_attempts(attempt_id) ON DELETE CASCADE,
    candidate_id TEXT NOT NULL REFERENCES h_candidate_snapshots(candidate_id),
    comparison_artifact_id TEXT NOT NULL REFERENCES h_artifacts(artifact_id),
    comparison_sha256 TEXT NOT NULL CHECK (length(comparison_sha256) = 64),
    resolved_target_count INTEGER NOT NULL CHECK (resolved_target_count >= 0),
    new_regression_count INTEGER NOT NULL CHECK (new_regression_count >= 0),
    coverage_lost_count INTEGER NOT NULL CHECK (coverage_lost_count >= 0),
    inconclusive_count INTEGER NOT NULL CHECK (inconclusive_count >= 0),
    created_at TEXT NOT NULL
);

CREATE TABLE h_completion_decisions (
    completion_decision_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL UNIQUE REFERENCES h_verification_attempts(attempt_id) ON DELETE CASCADE,
    candidate_id TEXT NOT NULL REFERENCES h_candidate_snapshots(candidate_id),
    status TEXT NOT NULL CHECK (status IN (
        'PASS', 'FAILED', 'UNVERIFIED', 'BLOCKED_ENVIRONMENT',
        'BUDGET_EXHAUSTED', 'NEEDS_INPUT', 'CANCELLED'
    )),
    reason_codes_json TEXT NOT NULL,
    candidate_sha256 TEXT NOT NULL CHECK (length(candidate_sha256) = 64),
    contract_sha256 TEXT NOT NULL CHECK (length(contract_sha256) = 64),
    test_set_sha256 TEXT NOT NULL CHECK (length(test_set_sha256) = 64),
    environment_set_sha256 TEXT NOT NULL CHECK (length(environment_set_sha256) = 64),
    comparison_id TEXT REFERENCES h_regression_comparisons(comparison_id),
    validator_review_id TEXT REFERENCES h_validator_reviews(validator_review_id),
    diff_review_id TEXT REFERENCES h_diff_scope_reviews(diff_review_id),
    report_artifact_id TEXT NOT NULL REFERENCES h_artifacts(artifact_id),
    report_sha256 TEXT NOT NULL CHECK (length(report_sha256) = 64),
    decision_json TEXT NOT NULL,
    decided_at TEXT NOT NULL
);

CREATE TABLE h_repair_attempts (
    repair_attempt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES h_runs(run_id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES h_tasks(task_id) ON DELETE CASCADE,
    from_candidate_id TEXT NOT NULL REFERENCES h_candidate_snapshots(candidate_id),
    repair_number INTEGER NOT NULL CHECK (repair_number >= 1),
    trigger_code TEXT NOT NULL,
    feedback_artifact_id TEXT NOT NULL REFERENCES h_artifacts(artifact_id),
    feedback_sha256 TEXT NOT NULL CHECK (length(feedback_sha256) = 64),
    starting_workspace_version_id TEXT NOT NULL REFERENCES h_workspace_versions(workspace_version_id),
    resulting_candidate_id TEXT REFERENCES h_candidate_snapshots(candidate_id),
    state TEXT NOT NULL CHECK (state IN ('REQUESTED', 'CODING', 'CANDIDATE_CREATED', 'FAILED', 'CANCELLED')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (task_id, repair_number)
);

CREATE TABLE h_task_outcomes (
    task_id TEXT PRIMARY KEY REFERENCES h_tasks(task_id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES h_runs(run_id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN (
        'PASS', 'FAILED', 'UNVERIFIED', 'BLOCKED_ENVIRONMENT',
        'BUDGET_EXHAUSTED', 'NEEDS_INPUT', 'CANCELLED'
    )),
    accepted_candidate_id TEXT REFERENCES h_candidate_snapshots(candidate_id),
    best_partial_candidate_id TEXT REFERENCES h_candidate_snapshots(candidate_id),
    completion_decision_id TEXT REFERENCES h_completion_decisions(completion_decision_id),
    report_artifact_id TEXT NOT NULL REFERENCES h_artifacts(artifact_id),
    outcome_sha256 TEXT NOT NULL CHECK (length(outcome_sha256) = 64),
    updated_at TEXT NOT NULL,
    CHECK (
        (status = 'PASS' AND accepted_candidate_id IS NOT NULL) OR
        (status <> 'PASS' AND accepted_candidate_id IS NULL)
    )
);

CREATE INDEX h_verification_contracts_task_idx ON h_verification_contracts(task_id, state, created_at);
CREATE INDEX h_verification_checks_contract_idx ON h_verification_checks(contract_id, tier, required, ordinal);
CREATE INDEX h_baseline_captures_state_idx ON h_baseline_captures(run_id, task_id, state);
CREATE INDEX h_baseline_runs_check_idx ON h_baseline_runs(verification_check_id, status, attempt_no);
CREATE INDEX h_verification_attempts_candidate_idx ON h_verification_attempts(candidate_id, state, attempt_number);
CREATE INDEX h_verification_environments_state_idx ON h_verification_environments(attempt_id, state);
CREATE INDEX h_check_runs_attempt_idx ON h_check_runs(attempt_id, status, settled_at);
CREATE INDEX h_test_case_results_status_idx ON h_test_case_results(check_run_id, status);
CREATE INDEX h_repair_attempts_task_idx ON h_repair_attempts(task_id, state, repair_number);
CREATE INDEX h_task_outcomes_run_idx ON h_task_outcomes(run_id, status);
