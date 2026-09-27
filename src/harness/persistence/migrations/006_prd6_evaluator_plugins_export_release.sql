-- PRD 6: evaluator sessions, reviewed plugin registry, export bundles and round trips,
-- capability requests / approval grants / consumptions, guarded external effects,
-- retention and cleanup, reproducibility manifests, replays, doctor reports, and
-- release gates. Additive: PRD 1-5 records keep their historical meaning.
-- Taken from PRD 6 section 20.3; the migrator owns the transaction and version row.

CREATE TABLE h_release_assets (
    release_asset_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN (
        'RELEASE_PROFILE', 'PLUGIN_MANIFEST', 'PLUGIN_CONFIG_SCHEMA',
        'PLUGIN_CONFIGURATION', 'PLUGIN_PROVENANCE', 'PLUGIN_SET_LOCK',
        'RETENTION_POLICY', 'DOCTOR_REPORT', 'RELEASE_GATE_EVIDENCE',
        'SBOM', 'THIRD_PARTY_NOTICES'
    )),
    relative_path TEXT NOT NULL UNIQUE,
    media_type TEXT NOT NULL,
    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    created_at TEXT NOT NULL
);

CREATE TABLE h_release_profiles (
    release_profile_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    release_level TEXT NOT NULL CHECK (release_level IN ('P0', 'P1')),
    configuration_release_asset_id TEXT NOT NULL REFERENCES h_release_assets(release_asset_id),
    configuration_sha256 TEXT NOT NULL CHECK (length(configuration_sha256) = 64),
    evaluator_adapter_name TEXT NOT NULL,
    evaluator_adapter_version TEXT NOT NULL,
    plugin_set_lock_sha256 TEXT NOT NULL CHECK (length(plugin_set_lock_sha256) = 64),
    state TEXT NOT NULL CHECK (state IN ('ACTIVE', 'DEPRECATED', 'DISABLED')),
    created_at TEXT NOT NULL,
    UNIQUE (name, version)
);

CREATE TABLE h_evaluator_sessions (
    evaluator_session_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES h_runs(run_id) ON DELETE CASCADE,
    release_profile_id TEXT NOT NULL REFERENCES h_release_profiles(release_profile_id),
    external_request_id TEXT NOT NULL,
    adapter_name TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    request_schema_version TEXT NOT NULL,
    result_schema_version TEXT NOT NULL,
    request_artifact_id TEXT NOT NULL REFERENCES h_artifacts(artifact_id),
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    result_path_hash TEXT NOT NULL CHECK (length(result_path_hash) = 64),
    result_artifact_id TEXT REFERENCES h_artifacts(artifact_id),
    result_sha256 TEXT,
    state TEXT NOT NULL CHECK (state IN (
        'ACCEPTED', 'RUNNING', 'RESULT_BUILDING', 'RESULT_WRITTEN',
        'BLOCKED', 'FAILED', 'CANCELLED'
    )),
    created_at TEXT NOT NULL,
    settled_at TEXT,
    UNIQUE (adapter_name, adapter_version, external_request_id),
    CHECK (result_sha256 IS NULL OR length(result_sha256) = 64)
);

CREATE TABLE h_plugin_manifests (
    plugin_manifest_id TEXT PRIMARY KEY,
    plugin_name TEXT NOT NULL,
    plugin_version TEXT NOT NULL,
    distribution_name TEXT NOT NULL,
    module_name TEXT NOT NULL,
    entry_point TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    interface_json TEXT NOT NULL,
    configuration_schema_release_asset_id TEXT NOT NULL REFERENCES h_release_assets(release_asset_id),
    configuration_schema_sha256 TEXT NOT NULL CHECK (length(configuration_schema_sha256) = 64),
    capabilities_json TEXT NOT NULL,
    provenance_release_asset_id TEXT NOT NULL REFERENCES h_release_assets(release_asset_id),
    license_expression TEXT NOT NULL,
    review_status TEXT NOT NULL CHECK (review_status IN ('REVIEWED', 'REJECTED', 'EXPIRED')),
    in_process INTEGER NOT NULL CHECK (in_process IN (0, 1)),
    manifest_release_asset_id TEXT NOT NULL REFERENCES h_release_assets(release_asset_id),
    manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
    created_at TEXT NOT NULL,
    UNIQUE (plugin_name, plugin_version, content_sha256)
);

CREATE TABLE h_plugin_dependencies (
    plugin_dependency_id TEXT PRIMARY KEY,
    plugin_manifest_id TEXT NOT NULL REFERENCES h_plugin_manifests(plugin_manifest_id) ON DELETE CASCADE,
    dependency_name TEXT NOT NULL,
    version_constraint TEXT NOT NULL,
    optional INTEGER NOT NULL CHECK (optional IN (0, 1)),
    resolved_plugin_manifest_id TEXT REFERENCES h_plugin_manifests(plugin_manifest_id),
    created_at TEXT NOT NULL,
    UNIQUE (plugin_manifest_id, dependency_name)
);

CREATE TABLE h_plugin_set_locks (
    plugin_set_lock_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    kernel_api_version TEXT NOT NULL,
    lock_release_asset_id TEXT NOT NULL REFERENCES h_release_assets(release_asset_id),
    lock_sha256 TEXT NOT NULL UNIQUE CHECK (length(lock_sha256) = 64),
    state TEXT NOT NULL CHECK (state IN ('ACTIVE', 'DEPRECATED', 'INVALID')),
    created_at TEXT NOT NULL,
    UNIQUE (name, version)
);

CREATE TABLE h_plugin_set_entries (
    plugin_set_entry_id TEXT PRIMARY KEY,
    plugin_set_lock_id TEXT NOT NULL REFERENCES h_plugin_set_locks(plugin_set_lock_id) ON DELETE CASCADE,
    slot TEXT NOT NULL,
    plugin_manifest_id TEXT NOT NULL REFERENCES h_plugin_manifests(plugin_manifest_id),
    configuration_release_asset_id TEXT NOT NULL REFERENCES h_release_assets(release_asset_id),
    configuration_sha256 TEXT NOT NULL CHECK (length(configuration_sha256) = 64),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    UNIQUE (plugin_set_lock_id, slot),
    UNIQUE (plugin_set_lock_id, ordinal)
);

CREATE TABLE h_run_plugin_bindings (
    run_plugin_binding_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES h_runs(run_id) ON DELETE CASCADE,
    plugin_set_entry_id TEXT NOT NULL REFERENCES h_plugin_set_entries(plugin_set_entry_id),
    slot TEXT NOT NULL,
    plugin_name TEXT NOT NULL,
    plugin_version TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    configuration_sha256 TEXT NOT NULL CHECK (length(configuration_sha256) = 64),
    self_check_status TEXT NOT NULL CHECK (self_check_status IN ('PASS', 'FAIL', 'NOT_RUN')),
    bound_at TEXT NOT NULL,
    UNIQUE (run_id, slot)
);

CREATE TABLE h_export_requests (
    export_request_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES h_runs(run_id) ON DELETE CASCADE,
    queue_result_id TEXT REFERENCES h_queue_results(queue_result_id),
    candidate_id TEXT REFERENCES h_candidate_snapshots(candidate_id),
    format TEXT NOT NULL,
    output_path_hash TEXT NOT NULL CHECK (length(output_path_hash) = 64),
    replace_existing INTEGER NOT NULL CHECK (replace_existing IN (0, 1)),
    max_bundle_bytes INTEGER NOT NULL CHECK (max_bundle_bytes > 0),
    request_artifact_id TEXT NOT NULL REFERENCES h_artifacts(artifact_id),
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    state TEXT NOT NULL CHECK (state IN (
        'REQUESTED', 'BUILDING', 'ROUND_TRIP', 'COMMITTING',
        'VALID', 'INVALID', 'FAILED', 'CANCELLED'
    )),
    created_at TEXT NOT NULL,
    settled_at TEXT
);

CREATE TABLE h_exports (
    export_id TEXT PRIMARY KEY,
    export_request_id TEXT NOT NULL UNIQUE REFERENCES h_export_requests(export_request_id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES h_runs(run_id) ON DELETE CASCADE,
    base_commit_oid TEXT NOT NULL,
    base_tree_oid TEXT NOT NULL,
    candidate_commit_oid TEXT NOT NULL,
    candidate_tree_oid TEXT NOT NULL,
    candidate_content_sha256 TEXT NOT NULL CHECK (length(candidate_content_sha256) = 64),
    patch_artifact_id TEXT NOT NULL REFERENCES h_artifacts(artifact_id),
    patch_sha256 TEXT NOT NULL CHECK (length(patch_sha256) = 64),
    result_artifact_id TEXT NOT NULL REFERENCES h_artifacts(artifact_id),
    report_artifact_id TEXT NOT NULL REFERENCES h_artifacts(artifact_id),
    manifest_artifact_id TEXT NOT NULL REFERENCES h_artifacts(artifact_id),
    manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
    bundle_bytes INTEGER NOT NULL CHECK (bundle_bytes >= 0),
    status TEXT NOT NULL CHECK (status IN ('VALID', 'INVALID', 'FAILED')),
    created_at TEXT NOT NULL
);

CREATE TABLE h_export_files (
    export_file_id TEXT PRIMARY KEY,
    export_id TEXT NOT NULL REFERENCES h_exports(export_id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL,
    media_type TEXT NOT NULL,
    artifact_id TEXT NOT NULL REFERENCES h_artifacts(artifact_id),
    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    UNIQUE (export_id, relative_path),
    UNIQUE (export_id, ordinal)
);

CREATE TABLE h_export_round_trips (
    export_round_trip_id TEXT PRIMARY KEY,
    export_id TEXT NOT NULL UNIQUE REFERENCES h_exports(export_id) ON DELETE CASCADE,
    environment_sha256 TEXT NOT NULL CHECK (length(environment_sha256) = 64),
    expected_tree_oid TEXT NOT NULL,
    observed_tree_oid TEXT,
    expected_content_sha256 TEXT NOT NULL CHECK (length(expected_content_sha256) = 64),
    observed_content_sha256 TEXT,
    changed_path_set_sha256 TEXT NOT NULL CHECK (length(changed_path_set_sha256) = 64),
    status TEXT NOT NULL CHECK (status IN ('PASS', 'FAIL', 'BLOCKED_ENVIRONMENT', 'CANCELLED')),
    report_artifact_id TEXT NOT NULL REFERENCES h_artifacts(artifact_id),
    started_at TEXT NOT NULL,
    settled_at TEXT NOT NULL,
    CHECK (observed_content_sha256 IS NULL OR length(observed_content_sha256) = 64)
);

CREATE TABLE h_capability_requests (
    capability_request_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES h_runs(run_id) ON DELETE CASCADE,
    operation TEXT NOT NULL CHECK (operation IN (
        'APPLY_LOCAL', 'PUSH_NEW_BRANCH', 'CREATE_PULL_REQUEST',
        'MERGE_TARGET', 'CLEANUP_RUN', 'DELETE_REMOTE_BRANCH'
    )),
    candidate_id TEXT REFERENCES h_candidate_snapshots(candidate_id),
    export_id TEXT REFERENCES h_exports(export_id),
    created_by TEXT NOT NULL CHECK (created_by IN (
        'USER_INTERACTIVE', 'USER_HEADLESS_REQUEST', 'ADMIN_POLICY'
    )),
    target_artifact_id TEXT NOT NULL REFERENCES h_artifacts(artifact_id),
    target_sha256 TEXT NOT NULL CHECK (length(target_sha256) = 64),
    policy_id TEXT NOT NULL,
    policy_sha256 TEXT NOT NULL CHECK (length(policy_sha256) = 64),
    permission_profile TEXT NOT NULL,
    requested_uses INTEGER NOT NULL CHECK (requested_uses >= 1),
    expires_at TEXT NOT NULL,
    summary_artifact_id TEXT NOT NULL REFERENCES h_artifacts(artifact_id),
    request_artifact_id TEXT NOT NULL REFERENCES h_artifacts(artifact_id),
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    state TEXT NOT NULL CHECK (state IN (
        'PENDING', 'APPROVED', 'DENIED', 'CANCELLED',
        'EXPIRED', 'CONSUMED', 'REVOKED'
    )),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE h_approval_grants (
    approval_grant_id TEXT PRIMARY KEY,
    capability_request_id TEXT NOT NULL REFERENCES h_capability_requests(capability_request_id) ON DELETE CASCADE,
    principal_id TEXT NOT NULL,
    approval_channel TEXT NOT NULL CHECK (approval_channel IN (
        'INTERACTIVE_TERMINAL', 'HEADLESS_PREGRANT', 'ADMIN_POLICY'
    )),
    bound_request_sha256 TEXT NOT NULL CHECK (length(bound_request_sha256) = 64),
    credential_scope_id TEXT,
    max_uses INTEGER NOT NULL CHECK (max_uses >= 1),
    remaining_uses INTEGER NOT NULL CHECK (remaining_uses >= 0),
    state TEXT NOT NULL CHECK (state IN ('ACTIVE', 'CONSUMED', 'REVOKED', 'EXPIRED', 'INVALIDATED')),
    grant_artifact_id TEXT NOT NULL REFERENCES h_artifacts(artifact_id),
    grant_sha256 TEXT NOT NULL CHECK (length(grant_sha256) = 64),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    CHECK (remaining_uses <= max_uses)
);

CREATE UNIQUE INDEX h_approval_grants_one_active_idx
    ON h_approval_grants(capability_request_id)
    WHERE state = 'ACTIVE';

CREATE TABLE h_approval_consumptions (
    approval_consumption_id TEXT PRIMARY KEY,
    approval_grant_id TEXT NOT NULL REFERENCES h_approval_grants(approval_grant_id),
    capability_request_id TEXT NOT NULL REFERENCES h_capability_requests(capability_request_id),
    use_number INTEGER NOT NULL CHECK (use_number >= 1),
    bound_request_sha256 TEXT NOT NULL CHECK (length(bound_request_sha256) = 64),
    state TEXT NOT NULL CHECK (state IN ('RESERVED', 'DISPATCHED', 'SETTLED', 'RELEASED', 'UNCERTAIN')),
    reserved_at TEXT NOT NULL,
    dispatched_at TEXT,
    settled_at TEXT,
    UNIQUE (approval_grant_id, use_number)
);

CREATE TABLE h_publication_candidates (
    publication_candidate_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES h_runs(run_id) ON DELETE CASCADE,
    source_candidate_id TEXT NOT NULL REFERENCES h_candidate_snapshots(candidate_id),
    candidate_snapshot_id TEXT NOT NULL REFERENCES h_candidate_snapshots(candidate_id),
    completion_decision_id TEXT NOT NULL REFERENCES h_completion_decisions(completion_decision_id),
    target_repository_identity TEXT NOT NULL,
    target_base_oid TEXT NOT NULL,
    candidate_commit_oid TEXT NOT NULL,
    candidate_tree_oid TEXT NOT NULL,
    candidate_content_sha256 TEXT NOT NULL CHECK (length(candidate_content_sha256) = 64),
    source_kind TEXT NOT NULL CHECK (source_kind IN ('CLEAN_SHARED_ANCESTRY', 'PATCH_ON_CLEAN_TARGET')),
    status TEXT NOT NULL CHECK (status IN ('PASS', 'INVALIDATED')),
    created_at TEXT NOT NULL,
    UNIQUE (run_id, target_repository_identity, target_base_oid, candidate_commit_oid)
);

CREATE TABLE h_application_plans (
    application_plan_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES h_runs(run_id) ON DELETE CASCADE,
    capability_request_id TEXT NOT NULL UNIQUE REFERENCES h_capability_requests(capability_request_id),
    export_id TEXT NOT NULL REFERENCES h_exports(export_id),
    target_root_identity TEXT NOT NULL,
    target_root_path_hash TEXT NOT NULL CHECK (length(target_root_path_hash) = 64),
    precondition_set_sha256 TEXT NOT NULL CHECK (length(precondition_set_sha256) = 64),
    postcondition_set_sha256 TEXT NOT NULL CHECK (length(postcondition_set_sha256) = 64),
    backup_bytes INTEGER NOT NULL CHECK (backup_bytes >= 0),
    concurrent_change_policy TEXT NOT NULL CHECK (concurrent_change_policy IN (
        'STOP', 'STOP_AND_REVERIFY'
    )),
    plan_artifact_id TEXT NOT NULL REFERENCES h_artifacts(artifact_id),
    plan_sha256 TEXT NOT NULL CHECK (length(plan_sha256) = 64),
    preflight_status TEXT NOT NULL CHECK (preflight_status IN ('PASS', 'FAIL', 'NEEDS_REVERIFY')),
    created_at TEXT NOT NULL
);

CREATE TABLE h_application_paths (
    application_path_id TEXT PRIMARY KEY,
    application_plan_id TEXT NOT NULL REFERENCES h_application_plans(application_plan_id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('CREATE', 'MODIFY', 'DELETE', 'MODE_CHANGE', 'RENAME')),
    expected_before_sha256 TEXT,
    desired_after_sha256 TEXT,
    expected_before_mode TEXT,
    desired_after_mode TEXT,
    backup_artifact_id TEXT REFERENCES h_artifacts(artifact_id),
    state TEXT NOT NULL CHECK (state IN (
        'PLANNED', 'BACKED_UP', 'WRITTEN', 'VERIFIED',
        'COMPENSATED', 'FAILED', 'UNCERTAIN'
    )),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    UNIQUE (application_plan_id, relative_path),
    UNIQUE (application_plan_id, ordinal),
    CHECK (expected_before_sha256 IS NULL OR length(expected_before_sha256) = 64),
    CHECK (desired_after_sha256 IS NULL OR length(desired_after_sha256) = 64)
);

CREATE TABLE h_external_effect_intents (
    external_effect_intent_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES h_runs(run_id) ON DELETE CASCADE,
    capability_request_id TEXT NOT NULL REFERENCES h_capability_requests(capability_request_id),
    approval_consumption_id TEXT NOT NULL UNIQUE REFERENCES h_approval_consumptions(approval_consumption_id),
    operation TEXT NOT NULL CHECK (operation IN ('APPLY_LOCAL', 'PUSH_NEW_BRANCH', 'CLEANUP_RUN')),
    adapter_name TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    source_identity_sha256 TEXT NOT NULL CHECK (length(source_identity_sha256) = 64),
    target_identity_sha256 TEXT NOT NULL CHECK (length(target_identity_sha256) = 64),
    expected_state_sha256 TEXT NOT NULL CHECK (length(expected_state_sha256) = 64),
    desired_state_sha256 TEXT NOT NULL CHECK (length(desired_state_sha256) = 64),
    credential_scope_id TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    intent_artifact_id TEXT NOT NULL REFERENCES h_artifacts(artifact_id),
    intent_sha256 TEXT NOT NULL CHECK (length(intent_sha256) = 64),
    state TEXT NOT NULL CHECK (state IN (
        'PREPARED', 'DISPATCHED', 'OBSERVING', 'SETTLED',
        'NOT_APPLIED', 'FAILED', 'UNCERTAIN'
    )),
    created_at TEXT NOT NULL,
    dispatched_at TEXT,
    settled_at TEXT
);

CREATE TABLE h_external_effect_observations (
    external_effect_observation_id TEXT PRIMARY KEY,
    external_effect_intent_id TEXT NOT NULL REFERENCES h_external_effect_intents(external_effect_intent_id) ON DELETE CASCADE,
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    observed_state_sha256 TEXT,
    observation_status TEXT NOT NULL CHECK (observation_status IN (
        'DESIRED', 'EXPECTED_OLD', 'CONFLICT', 'UNAVAILABLE', 'PARTIAL'
    )),
    observation_artifact_id TEXT NOT NULL REFERENCES h_artifacts(artifact_id),
    observed_at TEXT NOT NULL,
    UNIQUE (external_effect_intent_id, attempt_number),
    CHECK (observed_state_sha256 IS NULL OR length(observed_state_sha256) = 64)
);

CREATE TABLE h_external_effect_receipts (
    external_effect_receipt_id TEXT PRIMARY KEY,
    external_effect_intent_id TEXT NOT NULL UNIQUE REFERENCES h_external_effect_intents(external_effect_intent_id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN (
        'APPLIED', 'PUBLISHED', 'CLEANED', 'NOT_APPLIED',
        'PARTIAL_CLEANUP', 'FAILED', 'APPLICATION_UNCERTAIN',
        'PUBLICATION_UNCERTAIN', 'CLEANUP_UNCERTAIN', 'REMOTE_CONFLICT'
    )),
    response_received INTEGER NOT NULL CHECK (response_received IN (0, 1)),
    final_observed_state_sha256 TEXT,
    reconciliation TEXT NOT NULL,
    receipt_artifact_id TEXT NOT NULL REFERENCES h_artifacts(artifact_id),
    receipt_sha256 TEXT NOT NULL CHECK (length(receipt_sha256) = 64),
    settled_at TEXT NOT NULL,
    CHECK (final_observed_state_sha256 IS NULL OR length(final_observed_state_sha256) = 64)
);

CREATE TABLE h_retention_policies (
    retention_policy_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    policy_release_asset_id TEXT NOT NULL REFERENCES h_release_assets(release_asset_id),
    policy_sha256 TEXT NOT NULL CHECK (length(policy_sha256) = 64),
    state TEXT NOT NULL CHECK (state IN ('ACTIVE', 'DEPRECATED', 'DISABLED')),
    created_at TEXT NOT NULL,
    UNIQUE (name, version)
);

CREATE TABLE h_cleanup_plans (
    cleanup_plan_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES h_runs(run_id) ON DELETE CASCADE,
    retention_policy_id TEXT NOT NULL REFERENCES h_retention_policies(retention_policy_id),
    capability_request_id TEXT REFERENCES h_capability_requests(capability_request_id),
    estimated_reclaimed_bytes INTEGER NOT NULL CHECK (estimated_reclaimed_bytes >= 0),
    requires_approval INTEGER NOT NULL CHECK (requires_approval IN (0, 1)),
    plan_artifact_id TEXT NOT NULL REFERENCES h_artifacts(artifact_id),
    plan_sha256 TEXT NOT NULL CHECK (length(plan_sha256) = 64),
    state TEXT NOT NULL CHECK (state IN (
        'PLANNED', 'APPROVED', 'TOMBSTONED', 'EXECUTING',
        'SETTLED', 'PARTIAL', 'FAILED', 'UNCERTAIN'
    )),
    created_at TEXT NOT NULL,
    settled_at TEXT
);

CREATE TABLE h_cleanup_targets (
    cleanup_target_id TEXT PRIMARY KEY,
    cleanup_plan_id TEXT NOT NULL REFERENCES h_cleanup_plans(cleanup_plan_id) ON DELETE CASCADE,
    registered_resource_id TEXT NOT NULL,
    resource_kind TEXT NOT NULL CHECK (resource_kind IN (
        'TEMP_CONTAINER', 'VERIFICATION_WORKTREE', 'TASK_WORKTREE',
        'CACHE', 'PRIVATE_REF', 'RAW_LOG', 'RUN_ARTIFACT', 'RUN_DATABASE', 'RUN_ROOT'
    )),
    registered_identity_sha256 TEXT NOT NULL CHECK (length(registered_identity_sha256) = 64),
    estimated_bytes INTEGER NOT NULL CHECK (estimated_bytes >= 0),
    eligibility TEXT NOT NULL CHECK (eligibility IN ('ELIGIBLE', 'RETAIN', 'PROTECTED', 'ACTIVE_REFERENCE')),
    state TEXT NOT NULL CHECK (state IN (
        'PLANNED', 'TOMBSTONED', 'REMOVED', 'RETAINED',
        'MISSING', 'FAILED', 'UNCERTAIN'
    )),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    UNIQUE (cleanup_plan_id, registered_resource_id),
    UNIQUE (cleanup_plan_id, ordinal)
);

CREATE TABLE h_reproducibility_manifests (
    reproducibility_manifest_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES h_runs(run_id) ON DELETE CASCADE,
    manifest_version INTEGER NOT NULL CHECK (manifest_version >= 1),
    harness_source_commit TEXT NOT NULL,
    build_id TEXT NOT NULL,
    evaluator_adapter_name TEXT NOT NULL,
    evaluator_adapter_version TEXT NOT NULL,
    plugin_set_lock_sha256 TEXT NOT NULL CHECK (length(plugin_set_lock_sha256) = 64),
    effective_configuration_sha256 TEXT NOT NULL CHECK (length(effective_configuration_sha256) = 64),
    source_identity_sha256 TEXT NOT NULL CHECK (length(source_identity_sha256) = 64),
    candidate_identity_sha256 TEXT,
    runtime_identity_sha256 TEXT NOT NULL CHECK (length(runtime_identity_sha256) = 64),
    verification_identity_sha256 TEXT,
    manifest_artifact_id TEXT NOT NULL REFERENCES h_artifacts(artifact_id),
    manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
    created_at TEXT NOT NULL,
    UNIQUE (run_id, manifest_version),
    CHECK (candidate_identity_sha256 IS NULL OR length(candidate_identity_sha256) = 64),
    CHECK (verification_identity_sha256 IS NULL OR length(verification_identity_sha256) = 64)
);

CREATE TABLE h_replays (
    replay_id TEXT PRIMARY KEY,
    source_run_id TEXT NOT NULL REFERENCES h_runs(run_id),
    replay_run_id TEXT REFERENCES h_runs(run_id),
    mode TEXT NOT NULL CHECK (mode IN ('AUDIT', 'RECORDED', 'REVERIFY', 'LIVE_MODEL')),
    source_manifest_id TEXT NOT NULL REFERENCES h_reproducibility_manifests(reproducibility_manifest_id),
    state TEXT NOT NULL CHECK (state IN ('REQUESTED', 'RUNNING', 'PASS', 'DIFFERENT', 'BLOCKED', 'FAILED', 'CANCELLED')),
    result_artifact_id TEXT REFERENCES h_artifacts(artifact_id),
    started_at TEXT NOT NULL,
    settled_at TEXT
);

CREATE TABLE h_doctor_reports (
    doctor_report_id TEXT PRIMARY KEY,
    release_profile_id TEXT NOT NULL REFERENCES h_release_profiles(release_profile_id),
    status TEXT NOT NULL CHECK (status IN ('READY', 'BLOCKED', 'WARNING')),
    report_release_asset_id TEXT NOT NULL REFERENCES h_release_assets(release_asset_id),
    report_sha256 TEXT NOT NULL CHECK (length(report_sha256) = 64),
    blocking_check_count INTEGER NOT NULL CHECK (blocking_check_count >= 0),
    warning_count INTEGER NOT NULL CHECK (warning_count >= 0),
    environment_fingerprint_sha256 TEXT NOT NULL CHECK (length(environment_fingerprint_sha256) = 64),
    created_at TEXT NOT NULL
);

CREATE TABLE h_release_gates (
    release_gate_id TEXT PRIMARY KEY,
    release_profile_id TEXT NOT NULL REFERENCES h_release_profiles(release_profile_id),
    gate_code TEXT NOT NULL,
    required INTEGER NOT NULL CHECK (required IN (0, 1)),
    status TEXT NOT NULL CHECK (status IN ('PASS', 'FAIL', 'BLOCKED', 'NOT_RUN', 'NOT_APPLICABLE')),
    evidence_release_asset_id TEXT REFERENCES h_release_assets(release_asset_id),
    evidence_sha256 TEXT,
    limitation_code TEXT,
    evaluated_at TEXT NOT NULL,
    UNIQUE (release_profile_id, gate_code),
    CHECK (evidence_sha256 IS NULL OR length(evidence_sha256) = 64),
    CHECK (status <> 'PASS' OR evidence_release_asset_id IS NOT NULL)
);

CREATE INDEX h_evaluator_sessions_state_idx
    ON h_evaluator_sessions(state, created_at);
CREATE INDEX h_plugin_manifests_name_idx
    ON h_plugin_manifests(plugin_name, plugin_version, review_status);
CREATE INDEX h_run_plugin_bindings_run_idx
    ON h_run_plugin_bindings(run_id, slot);
CREATE INDEX h_export_requests_run_idx
    ON h_export_requests(run_id, state, created_at);
CREATE INDEX h_capability_requests_run_idx
    ON h_capability_requests(run_id, state, operation, created_at);
CREATE INDEX h_approval_grants_state_idx
    ON h_approval_grants(state, expires_at);
CREATE INDEX h_external_effect_intents_state_idx
    ON h_external_effect_intents(run_id, state, created_at);
CREATE INDEX h_external_effect_observations_intent_idx
    ON h_external_effect_observations(external_effect_intent_id, attempt_number);
CREATE INDEX h_cleanup_plans_run_idx
    ON h_cleanup_plans(run_id, state, created_at);
CREATE INDEX h_cleanup_targets_state_idx
    ON h_cleanup_targets(cleanup_plan_id, state, ordinal);
CREATE INDEX h_replays_source_idx
    ON h_replays(source_run_id, mode, state);
CREATE INDEX h_release_gates_profile_idx
    ON h_release_gates(release_profile_id, required, status);
