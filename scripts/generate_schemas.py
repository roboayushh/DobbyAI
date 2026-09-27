"""Generate checked-in JSON Schemas from the authoritative Pydantic contracts."""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import TypeAdapter

from harness.contracts import (
    CoderRoleDecisionV1,
    ContextPacketV1,
    EvidenceRefV1,
    ModelCallResultV1,
    OrchestrationPhaseResultV1,
    PlanV1,
    PlannerDecisionV1,
    PRD3ActionHandoffV1,
    ResolvedModelProfileV1,
    ValidatorReviewV1,
)
from harness.contracts.execution import (
    ActionAdmissionRequestV1,
    ApprovalRequestV1,
    CandidateSnapshotV1,
    ExecutionResultV1,
    PolicyDecisionV1,
    PolicySnapshotV1,
    RuntimeProfileV1,
    SandboxExecutionRequestV1,
    ToolCallEventV1,
    VerificationCandidateHandoffV1,
    WorkspaceVersionV1,
)
from harness.contracts.queue import (
    EvaluationCaseResultV1,
    IntegrationIntentV1,
    IntegrationResultV1,
    QueueFinalResultV1,
    QueuePlanV1,
    QueuePolicyV1,
    QueueProgressResultV1,
    ReleaseCandidateHandoffV1,
    TaskExecutionStartV1,
    VerifiedTaskCommitV1,
)
from harness.contracts.verification import (
    BaselineResultV1,
    CheckRunResultV1,
    CompletionDecisionV1,
    DiffScopeReviewV1,
    RegressionComparisonV1,
    RepairFeedbackV1,
    ValidatorOverlayProposalV1,
    VerificationAttemptRequestV1,
    VerificationContractV1,
    VerificationReportV1,
    VerifiedTaskHandoffV1,
)
from harness.contracts.release import (
    ApprovalGrantV1,
    CapabilityRequestV1,
    CleanupPlanV1,
    DoctorReportV1,
    EvaluatorRequestV1,
    EvaluatorResultV1,
    ExportManifestV1,
    ExportRequestV1,
    ExternalEffectIntentV1,
    ExternalEffectReceiptV1,
    LocalApplyPlanV1,
    PluginManifestV1,
    PluginSetLockV1,
    ReproducibilityManifestV1,
)


ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = {
    "orchestration/resolved_model_profile.json": ResolvedModelProfileV1,
    "orchestration/evidence_ref.json": EvidenceRefV1,
    "orchestration/context_packet.json": ContextPacketV1,
    "roles/plan.json": PlanV1,
    "roles/planner_decision.json": PlannerDecisionV1,
    "roles/coder_decision.json": CoderRoleDecisionV1,
    "roles/validator_review.json": ValidatorReviewV1,
    "orchestration/model_call_result.json": ModelCallResultV1,
    "orchestration/orchestration_phase_result.json": OrchestrationPhaseResultV1,
    "orchestration/prd3_action_handoff.json": PRD3ActionHandoffV1,
    # PRD 3: policy, sandbox execution, tools, workspace versions, candidates
    "execution/policy_snapshot.json": PolicySnapshotV1,
    "execution/action_admission_request.json": ActionAdmissionRequestV1,
    "execution/policy_decision.json": PolicyDecisionV1,
    "execution/approval_request.json": ApprovalRequestV1,
    "execution/runtime_profile.json": RuntimeProfileV1,
    "execution/sandbox_execution_request.json": SandboxExecutionRequestV1,
    "execution/execution_result.json": ExecutionResultV1,
    "execution/workspace_version.json": WorkspaceVersionV1,
    "execution/candidate_snapshot.json": CandidateSnapshotV1,
    "execution/verification_candidate_handoff.json": VerificationCandidateHandoffV1,
    "tools/tool_call_event.json": ToolCallEventV1,
    # PRD 4: verification, validation, completion, repair
    "verification/verification_contract.json": VerificationContractV1,
    "verification/baseline_result.json": BaselineResultV1,
    "verification/verification_attempt_request.json": VerificationAttemptRequestV1,
    "verification/check_run_result.json": CheckRunResultV1,
    "verification/regression_comparison.json": RegressionComparisonV1,
    "verification/validator_overlay_proposal.json": ValidatorOverlayProposalV1,
    "verification/diff_scope_review.json": DiffScopeReviewV1,
    "verification/completion_decision.json": CompletionDecisionV1,
    "verification/repair_feedback.json": RepairFeedbackV1,
    "verification/verification_report.json": VerificationReportV1,
    "verification/verified_task_handoff.json": VerifiedTaskHandoffV1,
    # PRD 5: queue, private Git integration, results
    "queue/queue_policy.json": QueuePolicyV1,
    "queue/queue_plan.json": QueuePlanV1,
    "queue/queue_progress_result.json": QueueProgressResultV1,
    "queue/queue_final_result.json": QueueFinalResultV1,
    "queue/task_execution_start.json": TaskExecutionStartV1,
    "integration/verified_task_commit.json": VerifiedTaskCommitV1,
    "integration/integration_intent.json": IntegrationIntentV1,
    "integration/integration_result.json": IntegrationResultV1,
    "integration/release_candidate_handoff.json": ReleaseCandidateHandoffV1,
    "evaluation/evaluation_case_result.json": EvaluationCaseResultV1,
    # PRD 6: evaluator I/O, plugins, export, approvals, effects, cleanup, reproducibility, doctor
    "evaluator/evaluator_request.json": EvaluatorRequestV1,
    "evaluator/evaluator_result.json": EvaluatorResultV1,
    "plugins/plugin_manifest.json": PluginManifestV1,
    "plugins/plugin_set_lock.json": PluginSetLockV1,
    "export/export_request.json": ExportRequestV1,
    "export/export_manifest.json": ExportManifestV1,
    "approvals/capability_request.json": CapabilityRequestV1,
    "approvals/approval_grant.json": ApprovalGrantV1,
    "effects/local_apply_plan.json": LocalApplyPlanV1,
    "effects/external_effect_intent.json": ExternalEffectIntentV1,
    "effects/external_effect_receipt.json": ExternalEffectReceiptV1,
    "effects/cleanup_plan.json": CleanupPlanV1,
    "reproducibility/reproducibility_manifest.json": ReproducibilityManifestV1,
    "reproducibility/doctor_report.json": DoctorReportV1,
}


def main() -> None:
    base = ROOT / "schemas" / "v1"
    for relative_path, model in SCHEMAS.items():
        destination = base / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        schema = TypeAdapter(model).json_schema()
        destination.write_text(
            json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
