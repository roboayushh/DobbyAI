"""PRD 4 verification service: contract, baseline, candidate checks, validator, gate, repair."""
from __future__ import annotations

import datetime
import hashlib
import json
import math
import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from harness.contracts import ValidatorReviewV1, VerificationReferenceV1
from harness.contracts.verification import (
    BaselineResultV1,
    CheckFailureV1,
    CheckRunResultV1,
    CheckStatus,
    CompletionDecisionV1,
    ComparisonSummaryV1,
    ContractCheckV1,
    CriterionResultV1,
    DiffScopeReviewV1,
    HandoffCandidateRefV1,
    HandoffVerificationV1,
    OverlayCheckProposalV1,
    OverlayFileRefV1,
    ProcessSummaryV1,
    QueueBudgetRemainingV1,
    RegressionComparisonV1,
    RepairFeedbackV1,
    ReportArtifactsV1,
    ReportBaselineV1,
    ReportChecksV1,
    ReportDiffReviewV1,
    ReportRegressionsV1,
    ReportUsageV1,
    ReportValidatorV1,
    RequiredCheckCountsV1,
    ScopeFindingV1,
    TaskOutcome,
    TestChangeSummaryV1,
    TestCountsV1,
    ValidatorFindingRecordV1,
    ValidatorOverlayProposalV1,
    VerificationAttemptRequestV1,
    VerificationContractV1,
    VerificationReportV1,
    VerifiedTaskHandoffV1,
)
from harness.persistence import ArtifactStore, RunStore, canonical_json
from harness.persistence.events import append_event_sql
from harness.policy.limits import SandboxLimits
from harness.sandbox import DockerBackend, ResolvedRuntime, RuntimeProfileResolver, SandboxError
from harness.verification.comparator import CheckEvidence, ComparisonResult, compare
from harness.verification.completion_gate import GateInputs, evaluate
from harness.verification.contract_service import ContractBuilder, in_test_area, inventory, is_test_path, test_set_sha256
from harness.verification.diff_review import DiffScopeReviewer, Finding
from harness.verification.environment import CheckObservation, VerificationEnvironmentFactory
from harness.workspace.task_workspace import TaskWorkspaceService, task_ref

MiB = 1024 * 1024
TIER_ORDER = {"focused": 0, "relevant": 1, "broad": 2, "invariant": 3, "validator": 4}


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _sha(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class VerificationError(RuntimeError):
    code = "VERIFICATION_ERROR"


class VerificationBudgetExhaustedError(VerificationError):
    code = "VERIFICATION_BUDGET_EXHAUSTED"


@dataclass(frozen=True)
class VerificationBudgetDefaults:
    check_runs_per_task: int = 40
    wall_seconds_per_task: int = 2400
    output_bytes_per_task: int = 200 * MiB
    repair_attempts_per_task: int = 2


@dataclass
class VerificationOutcome:
    status: str
    decision_id: Optional[str]
    report_artifact_id: Optional[str]
    repair_allowed: bool = False
    repair_number: int = 0
    replan: bool = False
    reasons: List[str] = field(default_factory=list)


class VerificationService:
    def __init__(
        self,
        *,
        run_store: RunStore,
        artifact_store: ArtifactStore,
        data_root: Path,
        backend: DockerBackend,
        runtime_resolver: RuntimeProfileResolver,
        workspaces: TaskWorkspaceService,
        sandbox_limits: SandboxLimits = SandboxLimits(),
        budget_defaults: VerificationBudgetDefaults = VerificationBudgetDefaults(),
        validator_required: bool = True,
        skip_validator_on_failure: bool = True,
    ) -> None:
        self.run_store = run_store
        self.artifact_store = artifact_store
        self.data_root = Path(data_root)
        self.backend = backend
        self.runtime_resolver = runtime_resolver
        self.workspaces = workspaces
        self.limits = sandbox_limits
        self.budget_defaults = budget_defaults
        self.validator_required = validator_required
        self.skip_validator_on_failure = skip_validator_on_failure
        self.environments = VerificationEnvironmentFactory(run_store, artifact_store, backend, workspaces, sandbox_limits)
        self.builder = ContractBuilder(test_batch_seconds=sandbox_limits.test_batch_seconds)
        self.diff_reviewer = DiffScopeReviewer()
        self._runtime: Optional[ResolvedRuntime] = None
        self.dependencies: Any = None  # DependencyEnvironmentService, wired by the composition root

    def _deps(self, run_id: str, task_id: str, commit: str) -> Dict[str, Any]:
        """Read-only dependency mount for checks on ``commit`` (empty when none is READY)."""
        if self.dependencies is None:
            return {}
        try:
            env = self.dependencies.ensure(run_id, task_id, commit)
        except Exception:
            return {}
        if env.state == "READY" and env.site_root is not None:
            return {"dependency_site": env.site_root, "dependency_sha256": env.environment_sha256}
        return {}

    # ------------------------------------------------------------ runtime
    def runtime(self) -> ResolvedRuntime:
        if self._runtime is None:
            self._runtime = self.runtime_resolver.resolve()
        return self._runtime

    def _runtime_row(self, run_id: str) -> str:
        runtime = self.runtime()
        with self.run_store.get_connection() as conn:
            row = conn.execute(
                "SELECT runtime_profile_row_id FROM h_runtime_profiles WHERE run_id = ? AND profile_fingerprint = ?",
                (run_id, runtime.fingerprint),
            ).fetchone()
        if row:
            return row["runtime_profile_row_id"]
        row_id = f"rtp_{uuid.uuid4().hex[:16]}"
        with self.run_store.get_connection() as conn:
            with conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO h_runtime_profiles(
                        runtime_profile_row_id, run_id, runtime_profile_id, runtime_name, runtime_version,
                        image_reference, image_digest, worker_version, tool_library_version, limits_json,
                        profile_fingerprint, created_at
                    ) VALUES (?, ?, ?, 'python', ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row_id, run_id, runtime.contract.runtime_profile_id, runtime.contract.runtime_version,
                        runtime.contract.image_reference, runtime.contract.image_digest,
                        runtime.contract.worker_version, runtime.contract.tool_library_version,
                        canonical_json(runtime.limits.as_dict()), runtime.fingerprint, _now(),
                    ),
                )
        return self._runtime_row(run_id)

    # ------------------------------------------------------------ budgets
    def _ensure_budget(self, run_id: str) -> None:
        with self.run_store.get_connection() as conn:
            tasks = conn.execute("SELECT COUNT(*) FROM h_tasks WHERE run_id = ?", (run_id,)).fetchone()[0]
            count = max(1, tasks)
            with conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO h_verification_budgets(
                        run_id, max_check_runs, max_wall_seconds, max_output_bytes, max_repair_attempts, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        self.budget_defaults.check_runs_per_task * count,
                        self.budget_defaults.wall_seconds_per_task * count,
                        self.budget_defaults.output_bytes_per_task * count,
                        self.budget_defaults.repair_attempts_per_task * count,
                        _now(),
                    ),
                )

    def budget(self, run_id: str) -> Dict[str, Any]:
        self._ensure_budget(run_id)
        with self.run_store.get_connection() as conn:
            row = dict(conn.execute("SELECT * FROM h_verification_budgets WHERE run_id = ?", (run_id,)).fetchone())
        row["remaining_check_runs"] = row["max_check_runs"] - row["used_check_runs"] - row["reserved_check_runs"]
        row["remaining_wall_seconds"] = row["max_wall_seconds"] - row["used_wall_seconds"] - row["reserved_wall_seconds"]
        return row

    def _reserve(self, run_id: str, wall: int, output: int) -> None:
        self._ensure_budget(run_id)
        with self.run_store.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM h_verification_budgets WHERE run_id = ?", (run_id,)).fetchone()
            if (
                row["used_check_runs"] + row["reserved_check_runs"] + 1 > row["max_check_runs"]
                or row["used_wall_seconds"] + row["reserved_wall_seconds"] + wall > row["max_wall_seconds"]
                or row["used_output_bytes"] + row["reserved_output_bytes"] + output > row["max_output_bytes"]
            ):
                conn.rollback()
                raise VerificationBudgetExhaustedError("Verification budget exhausted")
            conn.execute(
                """
                UPDATE h_verification_budgets
                SET reserved_check_runs = reserved_check_runs + 1, reserved_wall_seconds = reserved_wall_seconds + ?,
                    reserved_output_bytes = reserved_output_bytes + ?, updated_at = ?
                WHERE run_id = ?
                """,
                (wall, output, _now(), run_id),
            )
            conn.commit()

    @staticmethod
    def _settle_budget_sql(conn: sqlite3.Connection, run_id: str, wall: int, output: int, used_wall: int, used_output: int) -> None:
        conn.execute(
            """
            UPDATE h_verification_budgets
            SET reserved_check_runs = reserved_check_runs - 1, reserved_wall_seconds = reserved_wall_seconds - ?,
                reserved_output_bytes = reserved_output_bytes - ?, used_check_runs = used_check_runs + 1,
                used_wall_seconds = used_wall_seconds + ?, used_output_bytes = used_output_bytes + ?, updated_at = ?
            WHERE run_id = ?
            """,
            (wall, output, min(wall, used_wall), min(output, used_output), _now(), run_id),
        )

    # ----------------------------------------------------------- contract
    def contract_row(self, run_id: str, task_id: str) -> Optional[Dict[str, Any]]:
        with self.run_store.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM h_verification_contracts WHERE run_id = ? AND task_id = ? AND state = 'FROZEN'",
                (run_id, task_id),
            ).fetchone()
        return dict(row) if row else None

    def load_contract(self, row: Mapping[str, Any]) -> VerificationContractV1:
        contract = VerificationContractV1.model_validate_json(row["contract_json"])
        core = contract.model_dump(mode="json")
        core.pop("contract_sha256")
        if _sha(core) != contract.contract_sha256 or contract.contract_sha256 != row["contract_sha256"]:
            raise VerificationError("Verification contract hash mismatch")
        return contract

    def contract_sha(self, run_id: str, task_id: str) -> Optional[str]:
        row = self.contract_row(run_id, task_id)
        return row["contract_sha256"] if row else None

    def ensure_contract(self, run_id: str, task_id: str, *, plan_record: Any, version: Any) -> VerificationContractV1:
        existing = self.contract_row(run_id, task_id)
        if existing and existing["plan_revision"] == plan_record.plan.plan_revision:
            contract = self.load_contract(existing)
            self._ensure_baseline(run_id, task_id, contract)
            return contract
        workspace = self.workspaces.get(run_id, task_id)
        start = workspace["task_start_commit"]
        root = self.environments.pristine(run_id, start)
        with self.run_store.get_connection() as conn:
            task = conn.execute("SELECT task_spec_json FROM h_tasks WHERE task_id = ?", (task_id,)).fetchone()
            lifecycle = conn.execute("SELECT task_revision FROM h_task_lifecycle WHERE task_id = ?", (task_id,)).fetchone()
            policy = conn.execute(
                "SELECT policy_sha256 FROM h_policy_snapshots WHERE run_id = ? AND revoked = 0 ORDER BY version DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        spec = json.loads(task["task_spec_json"])
        start_manifest = self.workspaces.load_manifest(self.workspaces.versions(run_id, task_id)[0])
        contract = self.builder.build(
            contract_id=f"vcon_{uuid.uuid4().hex[:16]}",
            run_id=run_id,
            task_id=task_id,
            task_revision=lifecycle["task_revision"] if lifecycle else 1,
            plan=plan_record.plan,
            plan_id=plan_record.plan_id,
            task_text=f"{spec.get('title', '')}\n{spec.get('body', '')}",
            root=root,
            baseline_commit=start,
            baseline_content_sha256=start_manifest.content_tree_sha256(),
            runtime_profile_fingerprint=self.runtime().fingerprint,
            policy_sha256=policy["policy_sha256"] if policy else "0" * 64,
        )
        path = f"prd4/contracts/{task_id}/{contract.contract_id}.json"
        self.artifact_store.write_json(run_id, path, contract.model_dump(mode="json"), "verification_contract", task_id)
        artifact = self.artifact_store.get_artifact_by_path(run_id, path)
        now = _now()
        with self.run_store.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if existing:
                    conn.execute("UPDATE h_verification_contracts SET state = 'SUPERSEDED' WHERE contract_id = ?", (existing["contract_id"],))
                    append_event_sql(conn, run_id, "VERIFICATION_CONTRACT_INVALIDATED", {
                        "task_id": task_id, "contract_id": existing["contract_id"], "reason": "PLAN_REVISED",
                    }, "PRD4", "PRD4", now)
                conn.execute(
                    """
                    INSERT INTO h_verification_contracts(
                        contract_id, run_id, task_id, task_revision, plan_id, plan_revision, baseline_commit,
                        baseline_content_sha256, runtime_profile_fingerprint, policy_sha256, contract_json,
                        contract_sha256, test_set_sha256, artifact_id, state, created_at, frozen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'FROZEN', ?, ?)
                    """,
                    (
                        contract.contract_id, run_id, task_id, contract.task_revision, contract.plan_id,
                        contract.plan_revision, contract.baseline.commit, contract.baseline.content_tree_sha256,
                        contract.runtime_profile_fingerprint, contract.policy_sha256,
                        canonical_json(contract.model_dump(mode="json")), contract.contract_sha256,
                        test_set_sha256(contract), artifact["artifact_id"], now, now,
                    ),
                )
                for ordinal, check in enumerate(contract.checks):
                    conn.execute(
                        """
                        INSERT INTO h_verification_checks(
                            verification_check_id, contract_id, external_check_id, ordinal, origin, kind, tier,
                            required, baseline_policy, argv_json, working_directory, timeout_seconds, parser_id,
                            minimum_tests, allowed_outputs_json, check_sha256, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            f"vchk_{uuid.uuid4().hex[:16]}", contract.contract_id, check.check_id, ordinal,
                            check.origin.value, check.kind, check.tier, int(check.required), check.baseline_policy,
                            canonical_json(check.argv), check.cwd, check.timeout_seconds, check.parser,
                            check.minimum_tests, canonical_json(check.allowed_workspace_outputs),
                            _sha(check.model_dump(mode="json")), now,
                        ),
                    )
                append_event_sql(conn, run_id, "VERIFICATION_CONTRACT_FROZEN", {
                    "task_id": task_id,
                    "contract_id": contract.contract_id,
                    "contract_sha256": contract.contract_sha256,
                    "checks": [check.check_id for check in contract.checks],
                    "criteria": [criterion.criterion_id for criterion in contract.criteria],
                }, "PRD4", "PRD4", now)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        self._ensure_baseline(run_id, task_id, contract)
        return contract

    def _check_row(self, contract_id: str, check_id: str) -> Dict[str, Any]:
        with self.run_store.get_connection() as conn:
            return dict(conn.execute(
                "SELECT * FROM h_verification_checks WHERE contract_id = ? AND external_check_id = ?",
                (contract_id, check_id),
            ).fetchone())

    # ----------------------------------------------------------- baseline
    def _ensure_baseline(self, run_id: str, task_id: str, contract: VerificationContractV1) -> None:
        with self.run_store.get_connection() as conn:
            capture = conn.execute("SELECT * FROM h_baseline_captures WHERE contract_id = ?", (contract.contract_id,)).fetchone()
        if capture and capture["state"] in ("CAPTURED", "PARTIAL", "BLOCKED", "NOT_APPLICABLE", "NOT_CAPTURED"):
            return
        capture_id = capture["baseline_capture_id"] if capture else f"bcap_{uuid.uuid4().hex[:16]}"
        if not capture:
            with self.run_store.get_connection() as conn:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO h_baseline_captures(baseline_capture_id, run_id, task_id, contract_id, state,
                            limitation_json, started_at, created_at)
                        VALUES (?, ?, ?, ?, 'RUNNING', '[]', ?, ?)
                        """,
                        (capture_id, run_id, task_id, contract.contract_id, _now(), _now()),
                    )
                    append_event_sql(conn, run_id, "BASELINE_CAPTURE_STARTED", {"task_id": task_id, "contract_id": contract.contract_id}, "PRD4", "PRD4", _now())
        runtime = self.runtime()
        runtime_row = self._runtime_row(run_id)
        limitations: List[str] = []
        states: List[str] = []
        _, local_modules = inventory(self.environments.pristine(run_id, contract.baseline.commit))
        targeted_ids: set = set()
        for check in sorted(contract.checks, key=lambda item: TIER_ORDER[item.tier]):
            if check.baseline_policy == "not_applicable":
                continue
            check_row = self._check_row(contract.contract_id, check.check_id)
            with self.run_store.get_connection() as conn:
                done = conn.execute(
                    "SELECT status FROM h_baseline_runs WHERE contract_id = ? AND verification_check_id = ?",
                    (contract.contract_id, check_row["verification_check_id"]),
                ).fetchone()
            if done:
                states.append(done["status"])
                continue
            try:
                self._reserve(run_id, check.timeout_seconds + 10, self.limits.stdout_bytes + self.limits.stderr_bytes)
            except VerificationBudgetExhaustedError:
                limitations.append(f"{check.check_id}:BASELINE_BUDGET_EXHAUSTED")
                continue
            prefix = f"prd4/baseline/{task_id}/{contract.contract_id}/{check.check_id}"
            observation = self.environments.run_check(
                run_id, task_id,
                commit=contract.baseline.commit,
                content_sha256=contract.baseline.content_tree_sha256,
                contract_sha256=contract.contract_sha256,
                check=check,
                runtime=runtime,
                artifact_prefix=prefix,
                labels={"org.dobby.run_id": run_id, "org.dobby.task_id": task_id, "org.dobby.baseline": contract.contract_id},
                local_modules=sorted(local_modules),
                **self._deps(run_id, task_id, contract.baseline.commit),
            )
            failing = set(observation.parsed.failing_ids())
            if check.tier in ("focused", "relevant"):
                targeted_ids |= failing
            status = self._baseline_status(check, observation, failing, targeted_ids)
            states.append(status)
            cases = {
                case.test_id: {"status": case.status, "signature": case.failure_signature}
                for case in observation.parsed.cases
            }
            result = BaselineResultV1(
                baseline_result_id=f"base_{uuid.uuid4().hex[:16]}",
                contract_id=contract.contract_id,
                check_id=check.check_id,
                baseline_commit=contract.baseline.commit,
                environment_sha256=observation.environment_sha256,
                status=status,
                process=self._process(observation),
                tests=TestCountsV1(**observation.parsed.counts()),
                failure_signatures=sorted({case.failure_signature for case in observation.parsed.cases if case.failure_signature})[:1000],
                stdout_artifact_id=observation.stdout_artifact_id,
                stderr_artifact_id=observation.stderr_artifact_id,
                report_artifact_id=observation.report_artifact_id,
                captured_at=_now(),
            )
            with self.run_store.get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute(
                        """
                        INSERT INTO h_baseline_runs(
                            baseline_run_id, run_id, task_id, contract_id, baseline_capture_id, verification_check_id,
                            attempt_no, baseline_commit, environment_sha256, command_sha256, status, exit_code,
                            elapsed_ms, discovered_count, passed_count, failed_count, skipped_count, error_count,
                            stdout_artifact_id, stderr_artifact_id, report_artifact_id, cases_json, result_json, settled_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            result.baseline_result_id, run_id, task_id, contract.contract_id, capture_id,
                            check_row["verification_check_id"], contract.baseline.commit, observation.environment_sha256,
                            observation.command_sha256, status, observation.outcome.exit_code, observation.outcome.elapsed_ms,
                            observation.parsed.discovered, observation.parsed.passed, observation.parsed.failed,
                            observation.parsed.skipped, observation.parsed.errors, observation.stdout_artifact_id,
                            observation.stderr_artifact_id, observation.report_artifact_id, canonical_json(cases),
                            canonical_json(result.model_dump(mode="json")), _now(),
                        ),
                    )
                    self._settle_budget_sql(
                        conn, run_id, check.timeout_seconds + 10, self.limits.stdout_bytes + self.limits.stderr_bytes,
                        math.ceil(observation.outcome.elapsed_ms / 1000),
                        observation.outcome.stdout_bytes + observation.outcome.stderr_bytes,
                    )
                    append_event_sql(conn, run_id, "BASELINE_CHECK_SETTLED", {
                        "task_id": task_id, "check_id": check.check_id, "status": status,
                        "counts": observation.parsed.counts(),
                    }, "PRD4", "PRD4", _now())
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
        if not states:
            final = "NOT_APPLICABLE"
        elif all(state in ("PASS", "EXPECTED_REPRODUCTION_FAILURE", "PRE_EXISTING_FAILURE", "ZERO_TESTS") for state in states) and not limitations:
            final = "CAPTURED"
        elif all(state == "BLOCKED_ENVIRONMENT" for state in states):
            final = "BLOCKED"
        else:
            final = "PARTIAL"
        with self.run_store.get_connection() as conn:
            with conn:
                conn.execute(
                    "UPDATE h_baseline_captures SET state = ?, limitation_json = ?, settled_at = ? WHERE baseline_capture_id = ?",
                    (final, canonical_json(limitations), _now(), capture_id),
                )
                append_event_sql(conn, run_id, "BASELINE_CAPTURE_COMPLETED", {"task_id": task_id, "state": final, "limitations": limitations}, "PRD4", "PRD4", _now())
        self.environments.discard_pristine(run_id, contract.baseline.commit)

    @staticmethod
    def _baseline_status(check: ContractCheckV1, observation: CheckObservation, failing: set, targeted: set) -> str:
        status = observation.status
        if status == CheckStatus.PASS:
            return "PASS"
        if status == CheckStatus.FAIL:
            if check.tier in ("focused", "relevant") or failing & targeted:
                return "EXPECTED_REPRODUCTION_FAILURE"
            return "PRE_EXISTING_FAILURE"
        return status.value

    # -------------------------------------------------------- inspection
    def has_decision(self, candidate_id: str) -> bool:
        with self.run_store.get_connection() as conn:
            return bool(conn.execute(
                "SELECT 1 FROM h_completion_decisions WHERE candidate_id = ?", (candidate_id,)
            ).fetchone())

    def reference(self, run_id: str, task_id: str) -> Optional[VerificationReferenceV1]:
        with self.run_store.get_connection() as conn:
            row = conn.execute(
                """
                SELECT d.* FROM h_completion_decisions d JOIN h_candidate_snapshots c ON c.candidate_id = d.candidate_id
                WHERE c.run_id = ? AND c.task_id = ? ORDER BY d.decided_at DESC LIMIT 1
                """,
                (run_id, task_id),
            ).fetchone()
            repairs = conn.execute("SELECT COUNT(*) FROM h_repair_attempts WHERE task_id = ?", (task_id,)).fetchone()[0]
        if not row:
            return None
        return VerificationReferenceV1(
            status=row["status"],
            completion_decision_id=row["completion_decision_id"],
            report_artifact_id=row["report_artifact_id"],
            repair_attempts=repairs,
        )

    def latest_repair_feedback(self, run_id: str, task_id: str) -> Optional[Dict[str, Any]]:
        with self.run_store.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM h_repair_attempts WHERE run_id = ? AND task_id = ? ORDER BY repair_number DESC LIMIT 1",
                (run_id, task_id),
            ).fetchone()
        if not row:
            return None
        artifact = self.artifact_store.get_artifact_by_id(row["feedback_artifact_id"])
        relative = artifact["relative_path"].split("/artifacts/", 1)[-1]
        if not self.artifact_store.verify(run_id, relative):
            return None
        feedback = json.loads(self.artifact_store.open_readonly(run_id, relative))
        feedback["instruction"] = (
            "Host verification of your last candidate failed. Fix the failures below, keep existing tests intact, "
            "then request COMPLETE again only after an action shows the checks passing."
        )
        return feedback

    def task_outcome(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self.run_store.get_connection() as conn:
            row = conn.execute("SELECT * FROM h_task_outcomes WHERE task_id = ?", (task_id,)).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------ verify
    def verify(
        self,
        run_id: str,
        task_id: str,
        candidate_id: str,
        *,
        plan_record: Any,
        validator: Callable[[Sequence[Mapping[str, Any]], str], ValidatorReviewV1],
    ) -> VerificationOutcome:
        candidate = self.workspaces.get_candidate(candidate_id)
        if candidate is None:
            raise VerificationError(f"Unknown candidate {candidate_id}")
        with self.run_store.get_connection() as conn:
            decided = conn.execute("SELECT * FROM h_completion_decisions WHERE candidate_id = ?", (candidate_id,)).fetchone()
        if decided:
            return self._outcome_from_decision(run_id, task_id, dict(decided))
        contract_row = self.contract_row(run_id, task_id)
        if contract_row is None:
            raise VerificationError("No frozen verification contract exists for this task")
        contract = self.load_contract(contract_row)
        try:
            return self._verify(run_id, task_id, candidate, contract, contract_row, plan_record=plan_record, validator=validator)
        finally:
            for commit in {candidate["candidate_commit"], candidate["task_start_commit"]}:
                try:
                    self.environments.discard_pristine(run_id, commit)
                except Exception:
                    pass

    def _verify(
        self,
        run_id: str,
        task_id: str,
        candidate: Dict[str, Any],
        contract: VerificationContractV1,
        contract_row: Dict[str, Any],
        *,
        plan_record: Any,
        validator: Callable[[Sequence[Mapping[str, Any]], str], ValidatorReviewV1],
    ) -> VerificationOutcome:
        candidate_id = candidate["candidate_id"]
        runtime = self.runtime()
        intact = True
        try:
            self.workspaces.verify_candidate(candidate)
        except Exception:
            intact = False
        attempt_id, attempt_number = self._start_attempt(run_id, task_id, candidate, contract, contract_row, runtime)
        start_commit = candidate["task_start_commit"]
        git = self.workspaces.git(run_id)
        diff_text = git.diff(start_commit, candidate["candidate_commit"]).decode("utf-8", "replace")
        changed_paths = [path for _, path in git.diff_paths(start_commit, candidate["candidate_commit"])]
        _, local_modules = inventory(self.environments.pristine(run_id, candidate["candidate_commit"]))
        baseline = self._baseline_cases(contract)
        protected = self._protected_oracle_overlay(run_id, contract, candidate, baseline)
        evidence: List[CheckEvidence] = []
        observations: Dict[str, Tuple[str, CheckObservation]] = {}
        budget_exhausted = False
        stop_after_focused = False
        for check in sorted(contract.checks, key=lambda item: (TIER_ORDER[item.tier], item.check_id)):
            if stop_after_focused and check.tier in ("relevant", "broad"):
                evidence.append(CheckEvidence(check, None, not_run_reason="SKIPPED_AFTER_FOCUSED_FAILURE"))
                continue
            if budget_exhausted:
                evidence.append(CheckEvidence(check, None, not_run_reason="VERIFICATION_BUDGET_EXHAUSTED"))
                continue
            # Oracle tests the task named (or that reproduced the failure) run in their
            # original form: a modified test can never be the sole proof of success.
            check_overlay = protected if (protected and check.tier in ("focused", "relevant") and check.kind == "test") else None
            try:
                run_id_check, observation = self._run_candidate_check(
                    run_id, task_id, attempt_id, candidate, contract, check, runtime, changed_paths, sorted(local_modules), 1,
                    overlay=check_overlay,
                )
            except VerificationBudgetExhaustedError:
                budget_exhausted = True
                evidence.append(CheckEvidence(check, None, not_run_reason="VERIFICATION_BUDGET_EXHAUSTED"))
                continue
            item = self._evidence(check, observation, run_id_check, baseline)
            # Flake policy: one fresh rerun for a new-regression outcome.
            if contract.flake_retries and item.status == CheckStatus.FAIL and self._has_new_regression(item):
                try:
                    rerun_id, rerun = self._run_candidate_check(
                        run_id, task_id, attempt_id, candidate, contract, check, runtime, changed_paths, sorted(local_modules), 2,
                        overlay=check_overlay,
                    )
                    if rerun.parsed.case_map() != observation.parsed.case_map() or rerun.status != observation.status:
                        item.flaky = True
                except VerificationBudgetExhaustedError:
                    budget_exhausted = True
            evidence.append(item)
            observations[check.check_id] = (run_id_check, observation)
            if check.tier == "focused" and item.status != CheckStatus.PASS:
                stop_after_focused = True
        comparison = compare(evidence)
        diff_review, diff_review_id = self._diff_review(run_id, task_id, attempt_id, candidate, contract, diff_text, plan_record,
                                                        protected_paths=sorted(protected or {}))
        checks_failed = any(v.required and v.verdict == "NOT_SATISFIED" for v in comparison.verdicts.values())
        environment_blocked = any(v.required and v.verdict == "BLOCKED_ENVIRONMENT" for v in comparison.verdicts.values())

        validator_ran = False
        validator_decision: Optional[str] = None
        blocking_findings = 0
        overlay_verdicts: List[str] = []
        validator_findings: List[ValidatorFindingRecordV1] = []
        review_id: Optional[str] = None
        skip_reason: Optional[str] = None
        if self.validator_required and self.skip_validator_on_failure and (checks_failed or environment_blocked or not intact):
            skip_reason = "HOST_CHECKS_ALREADY_CONCLUSIVE"
        elif self.validator_required and not budget_exhausted:
            self._attempt_state(attempt_id, "RUNNING_VALIDATOR")
            sections = self._validator_sections(contract, candidate, diff_text, comparison, evidence, diff_review)
            try:
                review = validator(sections, f"validator-{candidate_id}")
                validator_ran = True
                validator_decision = review.decision
                validator_findings = [
                    ValidatorFindingRecordV1(
                        severity=finding.severity, category=finding.category[:128],
                        statement=finding.statement[:4000], evidence_refs=finding.evidence_ids[:32],
                    )
                    for finding in review.findings
                ]
                blocking_findings = sum(
                    1 for finding in review.findings
                    if finding.severity in ("high", "critical") and review.decision == "CHANGES_NEEDED"
                )
                review_id = self._persist_review(run_id, task_id, attempt_id, candidate, review, blocking_findings)
                if review.overlay_tests:
                    overlay_verdicts = self._run_overlay(
                        run_id, task_id, attempt_id, candidate, contract, runtime, review, review_id, changed_paths, sorted(local_modules)
                    )
            except VerificationBudgetExhaustedError:
                budget_exhausted = True
            except Exception as exc:  # budget, schema, adapter failures of the model call
                code = getattr(exc, "code", "VALIDATOR_FAILED")
                if "BUDGET" in code:
                    budget_exhausted = True
                skip_reason = f"VALIDATOR_UNAVAILABLE:{code}"
        if review_id is None:
            review_id = self._persist_review(run_id, task_id, attempt_id, candidate, None, 0, skip_reason=skip_reason or "NOT_REQUIRED")

        self._attempt_state(attempt_id, "COMPLETION_GATE")
        baseline_missing = [
            check.check_id for check in contract.checks
            if check.baseline_policy == "required" and check.check_id not in baseline
        ]
        gate = evaluate(GateInputs(
            candidate_intact=intact,
            contract_current=contract_row["contract_sha256"] == contract.contract_sha256,
            unsettled_work=False,
            comparison=comparison,
            criteria=contract.criteria,
            baseline_required_missing=baseline_missing,
            diff_review_status=diff_review.status,
            validator_required=self.validator_required and skip_reason != "HOST_CHECKS_ALREADY_CONCLUSIVE",
            validator_ran=validator_ran,
            validator_decision=validator_decision,
            validator_blocking_findings=blocking_findings,
            overlay_verdicts=overlay_verdicts,
            budget_exhausted=budget_exhausted,
        ))
        comparison_id = self._persist_comparison(run_id, task_id, attempt_id, candidate, contract, comparison, gate)
        decision, report_id = self._persist_decision(
            run_id, task_id, attempt_id, attempt_number, candidate, contract, runtime, gate, evidence, comparison,
            comparison_id, diff_review, diff_review_id, review_id, validator_decision, overlay_verdicts, observations,
        )
        outcome = VerificationOutcome(
            status=decision.status.value,
            decision_id=decision.decision_id,
            report_artifact_id=report_id,
            reasons=list(decision.reason_codes),
        )
        if decision.status == TaskOutcome.PASS:
            self._on_pass(run_id, task_id, candidate, decision, contract, report_id, comparison)
            return outcome
        repairs_used = self._repair_count(task_id)
        budget = self.budget(run_id)
        if gate.repairable and repairs_used < contract.max_repair_attempts and budget["remaining_check_runs"] > 0:
            feedback = self._repair_feedback(
                run_id, task_id, candidate, contract, gate, comparison, evidence, observations, diff_review, validator_findings, repairs_used + 1
            )
            outcome.repair_allowed = True
            outcome.repair_number = repairs_used + 1
            outcome.replan = self._same_failure_as_previous(task_id, candidate, feedback)
            return outcome
        self._settle_outcome(run_id, task_id, decision, report_id)
        return outcome

    # ------------------------------------------------------- check runs
    def _start_attempt(self, run_id: str, task_id: str, candidate: Dict[str, Any], contract: VerificationContractV1,
                       contract_row: Dict[str, Any], runtime: ResolvedRuntime) -> Tuple[str, int]:
        with self.run_store.get_connection() as conn:
            existing = conn.execute(
                "SELECT * FROM h_verification_attempts WHERE candidate_id = ? AND state NOT IN ('SETTLED', 'CANCELLED', 'FAILED_INTERNAL') ORDER BY attempt_number DESC LIMIT 1",
                (candidate["candidate_id"],),
            ).fetchone()
            number = conn.execute(
                "SELECT COALESCE(MAX(attempt_number), 0) + 1 FROM h_verification_attempts WHERE candidate_id = ?",
                (candidate["candidate_id"],),
            ).fetchone()[0]
        if existing:
            # An interrupted attempt is abandoned; a fresh one re-runs every check from a new copy.
            with self.run_store.get_connection() as conn:
                with conn:
                    conn.execute("UPDATE h_verification_attempts SET state = 'FAILED_INTERNAL', settled_at = ? WHERE attempt_id = ?", (_now(), existing["attempt_id"]))
                    append_event_sql(conn, run_id, "VERIFICATION_RECONCILED", {"attempt_id": existing["attempt_id"], "outcome": "ABANDONED_INTERRUPTED"}, "PRD4", "PRD4", _now())
        attempt_id = f"vatt_{uuid.uuid4().hex[:16]}"
        request = VerificationAttemptRequestV1(
            attempt_id=attempt_id,
            attempt_number=int(number),
            run_id=run_id,
            task_id=task_id,
            candidate_id=candidate["candidate_id"],
            candidate_sha256=candidate["candidate_sha256"],
            contract_id=contract.contract_id,
            contract_sha256=contract.contract_sha256,
            test_set_sha256=contract_row["test_set_sha256"],
            runtime_profile_fingerprint=runtime.fingerprint,
            required_check_ids=[check.check_id for check in contract.checks if check.required],
        )
        with self.run_store.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    INSERT INTO h_verification_attempts(
                        attempt_id, run_id, task_id, candidate_id, contract_id, attempt_number, candidate_sha256,
                        contract_sha256, test_set_sha256, request_json, state, started_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'RUNNING_CHECKS', ?)
                    """,
                    (
                        attempt_id, run_id, task_id, candidate["candidate_id"], contract.contract_id, int(number),
                        candidate["candidate_sha256"], contract.contract_sha256, contract_row["test_set_sha256"],
                        canonical_json(request.model_dump(mode="json")), _now(),
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO h_candidate_verification_state(candidate_id, contract_id, state, active_attempt_id, updated_at)
                    VALUES (?, ?, 'VERIFYING', ?, ?)
                    ON CONFLICT(candidate_id) DO UPDATE SET state = 'VERIFYING', active_attempt_id = excluded.active_attempt_id,
                        state_version = h_candidate_verification_state.state_version + 1, updated_at = excluded.updated_at
                    """,
                    (candidate["candidate_id"], contract.contract_id, attempt_id, _now()),
                )
                append_event_sql(conn, run_id, "VERIFICATION_ATTEMPT_STARTED", {
                    "task_id": task_id, "attempt_id": attempt_id, "candidate_id": candidate["candidate_id"],
                    "candidate_sha256": candidate["candidate_sha256"],
                }, "PRD4", "PRD4", _now())
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return attempt_id, int(number)

    def _attempt_state(self, attempt_id: str, state: str) -> None:
        with self.run_store.get_connection() as conn:
            with conn:
                conn.execute("UPDATE h_verification_attempts SET state = ? WHERE attempt_id = ?", (state, attempt_id))

    def _run_candidate_check(
        self,
        run_id: str,
        task_id: str,
        attempt_id: str,
        candidate: Dict[str, Any],
        contract: VerificationContractV1,
        check: ContractCheckV1,
        runtime: ResolvedRuntime,
        changed_paths: Sequence[str],
        local_modules: Sequence[str],
        run_number: int,
        *,
        overlay: Optional[Mapping[str, str]] = None,
        proposal_id: Optional[str] = None,
    ) -> Tuple[str, CheckObservation]:
        reserve_wall = check.timeout_seconds + 10
        reserve_output = self.limits.stdout_bytes + self.limits.stderr_bytes
        self._reserve(run_id, reserve_wall, reserve_output)
        check_run_id = f"crun_{uuid.uuid4().hex[:16]}"
        with self.run_store.get_connection() as conn:
            with conn:
                append_event_sql(conn, run_id, "CHECK_INTENT_PERSISTED", {
                    "attempt_id": attempt_id, "check_id": check.check_id, "check_run_id": check_run_id, "run_number": run_number,
                }, "PRD4", "PRD4", _now())
        prefix = f"prd4/attempts/{task_id}/{attempt_id}/{check.check_id}-{run_number}"
        try:
            observation = self.environments.run_check(
                run_id, task_id,
                commit=candidate["candidate_commit"],
                content_sha256=candidate["candidate_sha256"],
                contract_sha256=contract.contract_sha256,
                check=check,
                runtime=runtime,
                artifact_prefix=prefix,
                labels={"org.dobby.run_id": run_id, "org.dobby.task_id": task_id, "org.dobby.attempt_id": attempt_id, "org.dobby.check_run_id": check_run_id},
                changed_paths=changed_paths,
                overlay_files=overlay,
                local_modules=local_modules,
                **self._deps(run_id, task_id, candidate["candidate_commit"]),
            )
        except SandboxError:
            with self.run_store.get_connection() as conn:
                with conn:
                    self._settle_budget_sql(conn, run_id, reserve_wall, reserve_output, 0, 0)
            raise
        runtime_row = self._runtime_row(run_id)
        env_id = f"venv_{uuid.uuid4().hex[:16]}"
        check_row = None if proposal_id else self._check_row(contract.contract_id, check.check_id)
        result = CheckRunResultV1(
            check_run_id=check_run_id,
            attempt_id=attempt_id,
            check_id=check.check_id,
            candidate_sha256=candidate["candidate_sha256"],
            overlay_sha256=observation.overlay_sha256,
            environment_sha256=observation.environment_sha256,
            command_sha256=observation.command_sha256,
            status=observation.status,
            process=self._process(observation),
            tests=TestCountsV1(**observation.parsed.counts()),
            unexpected_source_mutation=observation.unexpected_mutation,
            stdout_artifact_id=observation.stdout_artifact_id,
            stderr_artifact_id=observation.stderr_artifact_id,
            report_artifact_id=observation.report_artifact_id,
            settled_at=_now(),
        )
        with self.run_store.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    INSERT INTO h_verification_environments(
                        verification_environment_id, attempt_id, verification_check_id, runtime_profile_row_id,
                        source_commit, candidate_sha256, overlay_sha256, environment_sha256, container_name,
                        container_id_hash, before_manifest_artifact_id, after_manifest_artifact_id, state, created_at, stopped_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'DISCARDED', ?, ?)
                    """,
                    (
                        env_id, attempt_id, check_row["verification_check_id"] if check_row else None, runtime_row,
                        candidate["candidate_commit"], candidate["candidate_sha256"], observation.overlay_sha256,
                        observation.environment_sha256, observation.container_name,
                        hashlib.sha256((observation.outcome.container_id or "").encode()).hexdigest(),
                        observation.before_manifest_artifact_id, observation.after_manifest_artifact_id, _now(), _now(),
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO h_check_runs(
                        check_run_id, attempt_id, verification_check_id, validator_check_proposal_id,
                        verification_environment_id, run_number, candidate_sha256, overlay_sha256, environment_sha256,
                        command_sha256, status, exit_code, signal, elapsed_ms, discovered_count, passed_count,
                        failed_count, skipped_count, error_count, unexpected_source_mutation, stdout_artifact_id,
                        stderr_artifact_id, report_artifact_id, result_json, settled_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        check_run_id, attempt_id, check_row["verification_check_id"] if check_row else None, proposal_id,
                        env_id, run_number, candidate["candidate_sha256"], observation.overlay_sha256,
                        observation.environment_sha256, observation.command_sha256, observation.status.value,
                        observation.outcome.exit_code, observation.outcome.signal, observation.outcome.elapsed_ms,
                        observation.parsed.discovered, observation.parsed.passed, observation.parsed.failed,
                        observation.parsed.skipped, observation.parsed.errors, int(observation.unexpected_mutation),
                        observation.stdout_artifact_id, observation.stderr_artifact_id, observation.report_artifact_id,
                        canonical_json(result.model_dump(mode="json")), _now(),
                    ),
                )
                for case in observation.parsed.cases[:20_000]:
                    conn.execute(
                        "INSERT OR IGNORE INTO h_test_case_results VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (f"tcr_{uuid.uuid4().hex[:16]}", check_run_id, case.test_id, case.status, case.duration_ms, case.failure_signature, case.raw_hash),
                    )
                self._settle_budget_sql(
                    conn, run_id, reserve_wall, reserve_output,
                    math.ceil(observation.outcome.elapsed_ms / 1000),
                    observation.outcome.stdout_bytes + observation.outcome.stderr_bytes,
                )
                append_event_sql(conn, run_id, "CHECK_SETTLED", {
                    "attempt_id": attempt_id, "check_id": check.check_id, "check_run_id": check_run_id,
                    "status": observation.status.value, "counts": observation.parsed.counts(),
                }, "PRD4", "PRD4", _now())
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return check_run_id, observation

    @staticmethod
    def _process(observation: CheckObservation) -> ProcessSummaryV1:
        outcome = observation.outcome
        return ProcessSummaryV1(
            exit_code=outcome.exit_code,
            signal=outcome.signal,
            timed_out=outcome.timed_out,
            oom_killed=outcome.oom_killed,
            output_limit_exceeded=outcome.output_limit_exceeded,
            elapsed_ms=outcome.elapsed_ms,
        )

    def _baseline_cases(self, contract: VerificationContractV1) -> Dict[str, Dict[str, Any]]:
        with self.run_store.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT c.external_check_id, b.status, b.cases_json, b.baseline_run_id
                FROM h_baseline_runs b JOIN h_verification_checks c ON c.verification_check_id = b.verification_check_id
                WHERE b.contract_id = ?
                """,
                (contract.contract_id,),
            ).fetchall()
        return {
            row["external_check_id"]: {
                "status": row["status"],
                "cases": json.loads(row["cases_json"]),
                "baseline_run_id": row["baseline_run_id"],
            }
            for row in rows
        }

    def _evidence(self, check: ContractCheckV1, observation: CheckObservation, check_run_id: str, baseline: Dict[str, Any]) -> CheckEvidence:
        base = baseline.get(check.check_id)
        base_cases = None
        base_signatures: Dict[str, Optional[str]] = {}
        if base and base["status"] not in ("BLOCKED_ENVIRONMENT", "UNPARSABLE", "TIMEOUT", "OOM", "OUTPUT_LIMIT", "UNEXPECTED_MUTATION", "INTERNAL_ERROR", "CANCELLED"):
            base_cases = {test: value["status"] for test, value in base["cases"].items()}
            base_signatures = {test: value.get("signature") for test, value in base["cases"].items()}
        return CheckEvidence(
            check=check,
            status=observation.status,
            cases=observation.parsed.case_map(),
            signatures={case.test_id: case.failure_signature for case in observation.parsed.cases},
            baseline_status=base["status"] if base else None,
            baseline_cases=base_cases,
            baseline_signatures=base_signatures,
            check_run_id=check_run_id,
            baseline_run_id=base["baseline_run_id"] if base else None,
        )

    @staticmethod
    def _has_new_regression(item: CheckEvidence) -> bool:
        if item.baseline_cases is None:
            return False
        return any(
            item.baseline_cases.get(test) == "PASS" and status in ("FAIL", "ERROR")
            for test, status in item.cases.items()
        )

    # ------------------------------------------------------- diff review
    def _protected_oracle_overlay(self, run_id: str, contract: VerificationContractV1, candidate: Dict[str, Any],
                                  baseline: Mapping[str, Dict[str, Any]]) -> Dict[str, str]:
        """Original task-start text of oracle test files the candidate modified or deleted.

        Oracle files are the test files a focused check names plus files holding
        tests that failed at baseline (the reproduction). Their original form is
        overlaid for focused/relevant checks so weakening them cannot produce PASS.
        """
        git = self.workspaces.git(run_id)
        start, commit = candidate["task_start_commit"], candidate["candidate_commit"]
        touched = {path for status, path in git.diff_paths(start, commit) if status in ("M", "D", "T")}
        touched = {path for path in touched if is_test_path(path) or in_test_area(path)}
        if not touched:
            return {}
        oracle: set = set()
        for check in contract.checks:
            if check.tier != "focused":
                continue
            for token in check.argv:
                path = token.split("::", 1)[0]
                if path.endswith(".py"):
                    oracle.add(path.removeprefix("./"))
                elif path and not path.startswith("-") and "/" in path:
                    oracle |= {item for item in touched if item.startswith(path.rstrip("/") + "/")}
        for check_id, record in baseline.items():
            for test_id, value in (record.get("cases") or {}).items():
                if value.get("status") in ("FAIL", "ERROR"):
                    module = test_id.split("::", 1)[0]
                    candidates = {module.replace(".", "/") + ".py", module}
                    oracle |= {item for item in touched if item in candidates}
        protected: Dict[str, str] = {}
        paths = sorted(oracle & touched)
        if not paths:
            return {}
        entries = {entry.path: entry for entry in git.ls_tree(git.commit_tree_of(start)) if entry.object_type == "blob"}
        blobs = git.cat_blobs(entries[path].oid for path in paths if path in entries)
        for path in paths:
            entry = entries.get(path)
            if entry is None:
                continue
            try:
                protected[path] = blobs[entry.oid].decode("utf-8")
            except UnicodeDecodeError:
                continue
        return protected

    def _diff_review(self, run_id: str, task_id: str, attempt_id: str, candidate: Dict[str, Any],
                     contract: VerificationContractV1, diff_text: str, plan_record: Any,
                     protected_paths: Sequence[str] = ()) -> Tuple[Any, str]:
        baseline_tests, _ = inventory(self.environments.pristine(run_id, candidate["task_start_commit"]))
        plan_paths = [location.path for location in getattr(plan_record.plan, "likely_edit_locations", [])]
        review = self.diff_reviewer.review(diff_text, baseline_tests=baseline_tests, plan_paths=plan_paths)
        for path in protected_paths:
            review.findings.append(Finding(
                "WARN", "oracle_test_modified", path,
                "This pre-existing test is part of the acceptance oracle; it was verified in its ORIGINAL form. "
                "Fix the implementation instead of changing the test.",
            ))
        if protected_paths and review.status == "CLEAN":
            review.status = "WARN"
        review_id = f"drev_{uuid.uuid4().hex[:16]}"
        path = f"prd4/attempts/{task_id}/{attempt_id}/diff-scope-review.json"
        payload = {
            "diff_review_id": review_id,
            "status": review.status,
            "findings": [finding.as_dict() for finding in review.findings],
            "changed_paths": review.changed_paths,
        }
        self.artifact_store.write_json(run_id, path, payload, "diff_scope_review", task_id)
        artifact = self.artifact_store.get_artifact_by_path(run_id, path)
        contract_view = DiffScopeReviewV1(
            diff_review_id=review_id,
            candidate_id=candidate["candidate_id"],
            candidate_sha256=candidate["candidate_sha256"],
            status=review.status,
            changed_paths=review.changed_paths,
            test_changes=TestChangeSummaryV1(
                existing_tests_deleted=review.existing_tests_deleted,
                skip_markers_added=review.skip_markers_added,
                assertions_weakened_suspected=review.assertions_weakened_suspected,
                discovery_config_changed=review.discovery_config_changed,
            ),
            scope_findings=[ScopeFindingV1(**finding.as_dict()) for finding in review.findings[:500]],
            review_artifact_id=artifact["artifact_id"],
            review_sha256=artifact["sha256"],
        )
        with self.run_store.get_connection() as conn:
            with conn:
                conn.execute(
                    "INSERT INTO h_diff_scope_reviews VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (review_id, attempt_id, candidate["candidate_id"], review.status, artifact["artifact_id"], artifact["sha256"], len(review.findings), _now()),
                )
                append_event_sql(conn, run_id, "DIFF_SCOPE_REVIEW_CREATED", {"attempt_id": attempt_id, "status": review.status, "findings": len(review.findings)}, "PRD4", "PRD4", _now())
        review.contract = contract_view  # type: ignore[attr-defined]
        return review, review_id

    # ---------------------------------------------------------- validator
    def _validator_sections(self, contract: VerificationContractV1, candidate: Dict[str, Any], diff_text: str,
                            comparison: ComparisonResult, evidence: Sequence[CheckEvidence], diff_review: Any) -> List[Dict[str, Any]]:
        checks = []
        for item in evidence:
            verdict = comparison.verdicts.get(item.check.check_id)
            checks.append({
                "check_id": item.check.check_id,
                "tier": item.check.tier,
                "command": item.check.argv,
                "status": item.status.value if item.status else "NOT_RUN",
                "verdict": verdict.verdict if verdict else None,
                "reasons": verdict.reasons if verdict else [],
                "resolved_targets": (verdict.resolved_targets if verdict else [])[:30],
                "new_regressions": (verdict.new_regressions if verdict else [])[:30],
                "baseline_status": item.baseline_status,
                "tests": {"total": len(item.cases), "failing": sorted(t for t, s in item.cases.items() if s in ("FAIL", "ERROR"))[:30]},
            })
        return [
            {
                "section": "contract",
                "label": "acceptance_contract",
                "content": {
                    "contract_sha256": contract.contract_sha256,
                    "criteria": [criterion.model_dump() for criterion in contract.criteria],
                    "prohibited_shortcuts": contract.prohibited_shortcuts,
                },
                "host_observed": True,
                "pinned": True,
                "rank": 9_300,
            },
            {
                "section": "candidate",
                "label": "candidate_identity",
                "content": {
                    "candidate_sha256": candidate["candidate_sha256"],
                    "candidate_commit": candidate["candidate_commit"],
                    "frozen": True,
                },
                "host_observed": True,
                "pinned": True,
                "rank": 9_200,
            },
            {
                "section": "candidate",
                "label": "candidate_diff",
                "content": {"diff": diff_text[:40_000], "truncated": len(diff_text) > 40_000},
                "host_observed": False,
                "rank": 8_500,
            },
            {
                "section": "results",
                "label": "host_check_results",
                "content": {"checks": checks, "targeted_baseline_failures": comparison.targeted_baseline_failures[:50]},
                "host_observed": True,
                "rank": 8_800,
            },
            {
                "section": "results",
                "label": "diff_scope_review",
                "content": {"status": diff_review.status, "findings": [f.as_dict() for f in diff_review.findings][:50]},
                "host_observed": True,
                "rank": 8_700,
            },
            {
                "section": "instructions",
                "label": "validator_overlay_instructions",
                "content": {
                    "instruction": (
                        "Review whether the frozen candidate satisfies every acceptance criterion using the host-observed "
                        "results. Echo candidate_hash = candidate_sha256 and acceptance_contract_sha256 = contract_sha256. "
                        "If a criterion is not exercised by any check, you may add up to "
                        f"{contract.max_validator_overlay_files} small pytest files in overlay_tests (paths under tests_overlay/). "
                        "They run in a separate overlay and never modify the candidate. Test only behavior the task states "
                        "explicitly. Use severity high/critical with CHANGES_NEEDED only for evidence-backed defects."
                    )
                },
                "host_observed": True,
                "pinned": True,
                "rank": 9_100,
            },
        ]

    def _persist_review(self, run_id: str, task_id: str, attempt_id: str, candidate: Dict[str, Any],
                        review: Optional[ValidatorReviewV1], blocking: int, *, skip_reason: Optional[str] = None) -> str:
        review_id = f"vrev_{uuid.uuid4().hex[:16]}"
        payload = review.model_dump(mode="json") if review else {"decision": "NOT_RUN", "skip_reason": skip_reason}
        path = f"prd4/attempts/{task_id}/{attempt_id}/validator-review.json"
        self.artifact_store.write_json(run_id, path, payload, "validator_review", task_id)
        artifact = self.artifact_store.get_artifact_by_path(run_id, path)
        with self.run_store.get_connection() as conn:
            call = conn.execute(
                """
                SELECT call_id, packet_id FROM h_model_calls
                WHERE run_id = ? AND task_id = ? AND role = 'validator' AND state = 'SUCCEEDED'
                ORDER BY created_at DESC LIMIT 1
                """,
                (run_id, task_id),
            ).fetchone() if review else None
            with conn:
                conn.execute(
                    """
                    INSERT INTO h_validator_reviews(
                        validator_review_id, attempt_id, candidate_id, model_call_id, packet_id, decision,
                        review_artifact_id, review_sha256, blocking_finding_count, skip_reason, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        review_id, attempt_id, candidate["candidate_id"], call["call_id"] if call else None,
                        call["packet_id"] if call else None, review.decision if review else "NOT_RUN",
                        artifact["artifact_id"], artifact["sha256"], blocking, skip_reason, _now(),
                    ),
                )
                append_event_sql(conn, run_id, "VALIDATOR_REVIEW_SETTLED", {
                    "attempt_id": attempt_id, "decision": review.decision if review else "NOT_RUN",
                    "blocking_findings": blocking, "skip_reason": skip_reason,
                }, "PRD4", "PRD4", _now())
        return review_id

    def _run_overlay(self, run_id: str, task_id: str, attempt_id: str, candidate: Dict[str, Any], contract: VerificationContractV1,
                     runtime: ResolvedRuntime, review: ValidatorReviewV1, review_id: str, changed_paths: Sequence[str],
                     local_modules: Sequence[str]) -> List[str]:
        files = {item.path: item.content for item in review.overlay_tests}
        total = sum(len(content.encode("utf-8")) for content in files.values())
        existing = {entry.path for entry in self.workspaces.git(run_id).ls_tree(candidate["candidate_tree"])}
        reason = None
        if len(files) > contract.max_validator_overlay_files:
            reason = "OVERLAY_TOO_MANY_FILES"
        elif total > contract.max_validator_overlay_bytes:
            reason = "OVERLAY_TOO_LARGE"
        elif any(path in existing for path in files):
            reason = "OVERLAY_COLLIDES_WITH_CANDIDATE"
        overlay_sha = _sha(sorted(files.items()))
        refs: List[OverlayFileRefV1] = []
        for path, content in sorted(files.items()):
            artifact_path = f"prd4/attempts/{task_id}/{attempt_id}/overlay/{hashlib.sha256(path.encode()).hexdigest()[:16]}.py.txt"
            self.artifact_store.write_bytes(run_id, artifact_path, content.encode("utf-8"), "text/plain", "test_overlay", task_id)
            artifact = self.artifact_store.get_artifact_by_path(run_id, artifact_path)
            refs.append(OverlayFileRefV1(path=path, content_sha256=hashlib.sha256(content.encode()).hexdigest(), content_artifact_id=artifact["artifact_id"]))
        overlay_id = f"ovl_{uuid.uuid4().hex[:16]}"
        proposal_id = f"vcp_{uuid.uuid4().hex[:16]}"
        argv = ["python", "-m", "pytest", "-q", "-rfE", "-o", "junit_family=xunit2", *sorted(files), "--junitxml=/output/junit.xml"]
        proposal = OverlayCheckProposalV1(
            proposal_id=proposal_id,
            purpose="; ".join(item.purpose for item in review.overlay_tests)[:1000] or "validator overlay",
            files=refs,
            argv=argv,
            timeout_seconds=180,
            parser="pytest-junit@1",
        )
        persisted = ValidatorOverlayProposalV1(
            review_id=review_id,
            candidate_sha256=candidate["candidate_sha256"],
            decision=review.decision,
            findings=[],
            proposed_checks=[proposal],
            review_sha256=_sha(review.model_dump(mode="json")),
        )
        overlay_artifact_path = f"prd4/attempts/{task_id}/{attempt_id}/overlay-proposal.json"
        self.artifact_store.write_json(run_id, overlay_artifact_path, persisted.model_dump(mode="json"), "test_overlay", task_id)
        overlay_artifact = self.artifact_store.get_artifact_by_path(run_id, overlay_artifact_path)
        with self.run_store.get_connection() as conn:
            with conn:
                conn.execute(
                    "INSERT INTO h_test_overlays VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (overlay_id, attempt_id, review_id, candidate["candidate_id"], overlay_artifact["artifact_id"], overlay_sha,
                     len(files), total, "REJECTED" if reason else "ADMITTED", reason, _now()),
                )
                conn.execute(
                    "INSERT INTO h_validator_check_proposals VALUES (?, ?, ?, 0, ?, ?, ?, ?)",
                    (proposal_id, review_id, overlay_id, canonical_json(proposal.model_dump(mode="json")),
                     _sha(proposal.model_dump(mode="json")), "REJECTED" if reason else "ADMITTED", reason),
                )
                append_event_sql(conn, run_id, "VALIDATOR_CHECK_REJECTED" if reason else "VALIDATOR_CHECK_ADMITTED", {
                    "attempt_id": attempt_id, "overlay_id": overlay_id, "reason": reason, "files": sorted(files),
                }, "PRD4", "PRD4", _now())
                if not reason:
                    append_event_sql(conn, run_id, "TEST_OVERLAY_CREATED", {"overlay_id": overlay_id, "overlay_sha256": overlay_sha}, "PRD4", "PRD4", _now())
        if reason:
            return []
        check = ContractCheckV1(
            check_id=f"overlay_{overlay_id}",
            origin="VALIDATOR_PROPOSED",
            kind="test",
            tier="validator",
            required=True,
            baseline_policy="not_applicable",
            argv=argv,
            timeout_seconds=180,
            parser="pytest-junit@1",
            minimum_tests=1,
        )
        _, observation = self._run_candidate_check(
            run_id, task_id, attempt_id, candidate, contract, check, runtime, changed_paths, local_modules, 1,
            overlay=files, proposal_id=proposal_id,
        )
        with self.run_store.get_connection() as conn:
            with conn:
                conn.execute("UPDATE h_test_overlays SET state = 'EXECUTED' WHERE overlay_id = ?", (overlay_id,))
                conn.execute("UPDATE h_validator_check_proposals SET admission_state = 'EXECUTED' WHERE validator_check_proposal_id = ?", (proposal_id,))
        verdict = compare([CheckEvidence(check, observation.status, observation.parsed.case_map())]).verdicts[check.check_id]
        return [verdict.verdict]

    # ------------------------------------------------------------ persist
    def _persist_comparison(self, run_id: str, task_id: str, attempt_id: str, candidate: Dict[str, Any],
                            contract: VerificationContractV1, comparison: ComparisonResult, gate: Any) -> str:
        totals = comparison.totals()
        comparison_id = f"cmp_{uuid.uuid4().hex[:16]}"
        core = {
            "schema_version": "1.0",
            "comparison_id": comparison_id,
            "attempt_id": attempt_id,
            "candidate_sha256": candidate["candidate_sha256"],
            "contract_sha256": contract.contract_sha256,
            "summary": ComparisonSummaryV1(
                resolved_targets=totals["resolved_targets"],
                new_regressions=totals["new_regressions"],
                pre_existing_unchanged=totals["pre_existing_unchanged"],
                changed_failures=totals["changed_failures"],
                coverage_lost=totals["coverage_lost"],
                inconclusive=totals["inconclusive"],
            ).model_dump(),
            "criterion_results": [CriterionResultV1(**result).model_dump() for result in gate.criteria],
        }
        model = RegressionComparisonV1(**core, comparison_sha256=_sha(core))
        detail = {**model.model_dump(mode="json"), "verdicts": {key: value.__dict__ for key, value in comparison.verdicts.items()}}
        path = f"prd4/attempts/{task_id}/{attempt_id}/regression-comparison.json"
        self.artifact_store.write_json(run_id, path, detail, "regression_comparison", task_id)
        artifact = self.artifact_store.get_artifact_by_path(run_id, path)
        with self.run_store.get_connection() as conn:
            with conn:
                conn.execute(
                    "INSERT INTO h_regression_comparisons VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (comparison_id, attempt_id, candidate["candidate_id"], artifact["artifact_id"], model.comparison_sha256,
                     totals["resolved_targets"], totals["new_regressions"], totals["coverage_lost"], totals["inconclusive"], _now()),
                )
                append_event_sql(conn, run_id, "REGRESSION_COMPARISON_CREATED", {"attempt_id": attempt_id, **totals}, "PRD4", "PRD4", _now())
        return comparison_id

    def _persist_decision(self, run_id, task_id, attempt_id, attempt_number, candidate, contract, runtime, gate, evidence,
                          comparison, comparison_id, diff_review, diff_review_id, review_id, validator_decision,
                          overlay_verdicts, observations) -> Tuple[CompletionDecisionV1, str]:
        env_set = _sha(sorted({obs.environment_sha256 for _, obs in observations.values()}))
        with self.run_store.get_connection() as conn:
            test_set = conn.execute("SELECT test_set_sha256 FROM h_verification_contracts WHERE contract_id = ?", (contract.contract_id,)).fetchone()[0]
            capture = conn.execute("SELECT * FROM h_baseline_captures WHERE contract_id = ?", (contract.contract_id,)).fetchone()
            usage = conn.execute(
                "SELECT COUNT(*) AS runs, COALESCE(SUM(elapsed_ms), 0) AS ms FROM h_check_runs WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            validator_calls = conn.execute(
                "SELECT COUNT(*) FROM h_model_calls WHERE run_id = ? AND task_id = ? AND role = 'validator'", (run_id, task_id)
            ).fetchone()[0]
        totals = comparison.totals()
        required_checks = [c for c in contract.checks if c.required]
        optional_checks = [c for c in contract.checks if not c.required]
        not_run = [item.check.check_id + ":" + (item.not_run_reason or "NOT_RUN") for item in evidence if item.status is None]
        warnings = [f"{f.category}:{f.path or '-'}" for f in diff_review.findings if f.severity == "WARN"]
        limitations = list(gate.limitations) + [
            "PASS means the declared checks passed on this exact candidate; hidden evaluator tests were not available.",
        ]
        if capture is None or capture["state"] != "CAPTURED":
            limitations.append(f"BASELINE_{capture['state'] if capture else 'NOT_CAPTURED'}")
        report = VerificationReportV1(
            run_id=run_id,
            task_id=task_id,
            candidate_id=candidate["candidate_id"],
            status=gate.status,
            baseline=ReportBaselineV1(
                state=(capture["state"] if capture and capture["state"] in ("CAPTURED", "PARTIAL", "BLOCKED", "NOT_APPLICABLE", "NOT_CAPTURED") else "NOT_CAPTURED"),
                commit=contract.baseline.commit,
                limitations=json.loads(capture["limitation_json"]) if capture else [],
            ),
            candidate_sha256=candidate["candidate_sha256"],
            contract_sha256=contract.contract_sha256,
            checks=ReportChecksV1(
                required_total=len(required_checks),
                required_passed=gate.required_passed,
                optional_total=len(optional_checks),
                optional_passed=sum(1 for c in optional_checks if comparison.verdicts.get(c.check_id) and comparison.verdicts[c.check_id].verdict == "SATISFIED"),
                not_run=not_run[:64],
            ),
            regressions=ReportRegressionsV1(new=totals["new_regressions"], pre_existing=totals["pre_existing_unchanged"], resolved_targets=totals["resolved_targets"]),
            validator=ReportValidatorV1(decision=validator_decision or "NOT_RUN", overlay_checks_run=len(overlay_verdicts)),
            diff_review=ReportDiffReviewV1(status=diff_review.status, warnings=warnings[:200]),
            usage=ReportUsageV1(
                verification_attempts=attempt_number,
                check_runs=usage["runs"],
                validator_calls=validator_calls,
                repair_attempts=self._repair_count(task_id),
                wall_seconds=int(usage["ms"] / 1000),
            ),
            artifacts=ReportArtifactsV1(report="pending", candidate_diff=candidate["diff_artifact_id"], comparison=comparison_id),
            limitations=limitations[:64],
        )
        detail = {
            "report": report.model_dump(mode="json"),
            "reason_codes": gate.reasons,
            "criteria": gate.criteria,
            "checks": [
                {
                    "check_id": item.check.check_id,
                    "tier": item.check.tier,
                    "required": item.check.required,
                    "status": item.status.value if item.status else None,
                    "baseline_status": item.baseline_status,
                    "verdict": comparison.verdicts[item.check.check_id].__dict__,
                    "check_run_id": item.check_run_id,
                    "baseline_run_id": item.baseline_run_id,
                }
                for item in evidence
            ],
        }
        report_path = f"prd4/attempts/{task_id}/{attempt_id}/verification-report.json"
        self.artifact_store.write_json(run_id, report_path, detail, "verification_report", task_id)
        report_artifact = self.artifact_store.get_artifact_by_path(run_id, report_path)
        decision = CompletionDecisionV1(
            decision_id=f"vdec_{uuid.uuid4().hex[:16]}",
            attempt_id=attempt_id,
            candidate_id=candidate["candidate_id"],
            status=gate.status,
            reason_codes=gate.reasons[:64],
            criteria=[CriterionResultV1(**result) for result in gate.criteria],
            required_checks=RequiredCheckCountsV1(
                total=gate.required_total, passed=gate.required_passed,
                failed=gate.required_failed, inconclusive=gate.required_inconclusive,
            ),
            validator_decision=validator_decision or "NOT_RUN",
            diff_review_status=diff_review.status,
            candidate_sha256=candidate["candidate_sha256"],
            test_set_sha256=test_set,
            environment_set_sha256=env_set,
            report_artifact_id=report_artifact["artifact_id"],
            decided_at=_now(),
        )
        decision_path = f"prd4/attempts/{task_id}/{attempt_id}/completion-decision.json"
        self.artifact_store.write_json(run_id, decision_path, decision.model_dump(mode="json"), "completion_decision", task_id)
        state = gate.status.value
        with self.run_store.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    INSERT INTO h_completion_decisions(
                        completion_decision_id, attempt_id, candidate_id, status, reason_codes_json, candidate_sha256,
                        contract_sha256, test_set_sha256, environment_set_sha256, comparison_id, validator_review_id,
                        diff_review_id, report_artifact_id, report_sha256, decision_json, decided_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        decision.decision_id, attempt_id, candidate["candidate_id"], state, canonical_json(gate.reasons),
                        candidate["candidate_sha256"], contract.contract_sha256, test_set, env_set, comparison_id,
                        review_id, diff_review_id, report_artifact["artifact_id"], report_artifact["sha256"],
                        canonical_json(decision.model_dump(mode="json")), decision.decided_at,
                    ),
                )
                conn.execute(
                    "UPDATE h_candidate_verification_state SET state = ?, active_attempt_id = NULL, state_version = state_version + 1, updated_at = ? WHERE candidate_id = ?",
                    (state, _now(), candidate["candidate_id"]),
                )
                conn.execute("UPDATE h_verification_attempts SET state = 'SETTLED', settled_at = ? WHERE attempt_id = ?", (_now(), attempt_id))
                append_event_sql(conn, run_id, "COMPLETION_GATE_EVALUATED", {
                    "attempt_id": attempt_id, "candidate_id": candidate["candidate_id"], "status": state,
                    "reason_codes": gate.reasons[:20],
                }, "PRD4", "PRD4", _now())
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return decision, report_artifact["artifact_id"]

    def _repair_count(self, task_id: str) -> int:
        with self.run_store.get_connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM h_repair_attempts WHERE task_id = ?", (task_id,)).fetchone()[0]

    def _repair_feedback(self, run_id, task_id, candidate, contract, gate, comparison, evidence, observations,
                         diff_review, validator_findings, number) -> RepairFeedbackV1:
        failures: List[CheckFailureV1] = []
        new_regressions: List[str] = []
        for item in evidence:
            verdict = comparison.verdicts[item.check.check_id]
            new_regressions.extend(verdict.new_regressions)
            if verdict.verdict in ("NOT_SATISFIED", "INCONCLUSIVE") and item.check_run_id:
                observation = observations.get(item.check.check_id, (None, None))[1]
                failing = sorted(t for t, s in item.cases.items() if s in ("FAIL", "ERROR"))
                excerpt = ""
                if observation is not None:
                    excerpt = (observation.stdout_excerpt[-5000:] + "\n" + observation.stderr_excerpt[-2000:]).strip()
                failures.append(CheckFailureV1(
                    check_run_id=item.check_run_id,
                    check_id=item.check.check_id,
                    status=item.status or CheckStatus.INTERNAL_ERROR,
                    failure_signature=hashlib.sha256("|".join(verdict.reasons + failing).encode()).hexdigest(),
                    log_artifact_ids=[a for a in (observation.stdout_artifact_id, observation.stderr_artifact_id, observation.report_artifact_id) if a] if observation else [],
                    failing_tests=failing[:50],
                    output_excerpt=excerpt[:8000],
                ))
        failed_criteria = [result["criterion_id"] for result in gate.criteria if result["status"] != "SATISFIED"]
        repair_id = f"rpa_{uuid.uuid4().hex[:16]}"
        core = {
            "schema_version": "1.0",
            "repair_attempt_id": repair_id,
            "task_id": task_id,
            "from_candidate_id": candidate["candidate_id"],
            "repair_number": number,
            "trigger": (gate.reasons[0] if gate.reasons else "FAILED_REQUIRED_CHECK")[:64],
            "failed_criteria": failed_criteria,
            "new_regressions": sorted(set(new_regressions))[:200],
            "check_failures": [failure.model_dump(mode="json") for failure in failures[:32]],
            "diff_findings": [ScopeFindingV1(**f.as_dict()).model_dump() for f in diff_review.findings][:100],
            "validator_findings": [f.model_dump() for f in validator_findings][:50],
            "workspace_start_candidate_sha256": candidate["candidate_sha256"],
            "remaining_repairs": max(0, contract.max_repair_attempts - number),
        }
        feedback = RepairFeedbackV1(**core, feedback_sha256=_sha(core))
        path = f"prd4/repairs/{task_id}/{repair_id}.json"
        self.artifact_store.write_json(run_id, path, {**feedback.model_dump(mode="json"), "reason_codes": gate.reasons}, "repair_feedback", task_id)
        artifact = self.artifact_store.get_artifact_by_path(run_id, path)
        version = self.workspaces.current_version(run_id, task_id)
        with self.run_store.get_connection() as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO h_repair_attempts(
                        repair_attempt_id, run_id, task_id, from_candidate_id, repair_number, trigger_code,
                        feedback_artifact_id, feedback_sha256, starting_workspace_version_id, state, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'REQUESTED', ?, ?)
                    """,
                    (repair_id, run_id, task_id, candidate["candidate_id"], number, feedback.trigger, artifact["artifact_id"],
                     feedback.feedback_sha256, version.version_id, _now(), _now()),
                )
                conn.execute("UPDATE h_verification_budgets SET used_repair_attempts = MIN(max_repair_attempts, used_repair_attempts + 1) WHERE run_id = ?", (run_id,))
                append_event_sql(conn, run_id, "REPAIR_FEEDBACK_CREATED", {
                    "task_id": task_id, "repair_attempt_id": repair_id, "repair_number": number, "trigger": feedback.trigger,
                }, "PRD4", "PRD4", _now())
        return feedback

    def _same_failure_as_previous(self, task_id: str, candidate: Dict[str, Any], feedback: RepairFeedbackV1) -> bool:
        with self.run_store.get_connection() as conn:
            rows = conn.execute(
                "SELECT feedback_artifact_id FROM h_repair_attempts WHERE task_id = ? AND repair_attempt_id <> ? ORDER BY repair_number DESC LIMIT 1",
                (task_id, feedback.repair_attempt_id),
            ).fetchall()
        if not rows:
            return False
        artifact = self.artifact_store.get_artifact_by_id(rows[0]["feedback_artifact_id"])
        relative = artifact["relative_path"].split("/artifacts/", 1)[-1]
        previous = json.loads(self.artifact_store.open_readonly(artifact["run_id"], relative))
        old = sorted(f.get("failure_signature") for f in previous.get("check_failures", []))
        new = sorted(f.failure_signature for f in feedback.check_failures)
        return bool(old) and old == new

    def _outcome_from_decision(self, run_id: str, task_id: str, decision: Dict[str, Any]) -> VerificationOutcome:
        outcome = VerificationOutcome(decision["status"], decision["completion_decision_id"], decision["report_artifact_id"],
                                      reasons=json.loads(decision["reason_codes_json"]))
        if decision["status"] == "PASS":
            return outcome
        with self.run_store.get_connection() as conn:
            repair = conn.execute(
                "SELECT * FROM h_repair_attempts WHERE from_candidate_id = ?", (decision["candidate_id"],)
            ).fetchone()
        if repair:
            outcome.repair_allowed = True
            outcome.repair_number = repair["repair_number"]
        return outcome

    # ------------------------------------------------------------ outcome
    def _on_pass(self, run_id: str, task_id: str, candidate: Dict[str, Any], decision: CompletionDecisionV1,
                 contract: VerificationContractV1, report_id: str, comparison: ComparisonResult) -> None:
        git = self.workspaces.git(run_id)
        ref = task_ref(run_id, task_id, "verified")
        current = git.read_ref(ref)
        if current != candidate["candidate_commit"]:
            git.update_ref_cas(ref, candidate["candidate_commit"], current)
        with self.run_store.get_connection() as conn:
            task = conn.execute("SELECT ordinal FROM h_tasks WHERE task_id = ?", (task_id,)).fetchone()
            ledger = conn.execute("SELECT * FROM h_budget_ledgers WHERE run_id = ?", (run_id,)).fetchone()
        changed = [path for _, path in git.diff_paths(candidate["task_start_commit"], candidate["candidate_commit"])]
        handoff = VerifiedTaskHandoffV1(
            run_id=run_id,
            task_id=task_id,
            task_ordinal=task["ordinal"],
            task_start_commit=candidate["task_start_commit"],
            outcome=TaskOutcome.PASS,
            candidate=HandoffCandidateRefV1(
                candidate_id=candidate["candidate_id"], commit=candidate["candidate_commit"],
                tree=candidate["candidate_tree"], sha256=candidate["candidate_sha256"],
            ),
            verification=HandoffVerificationV1(
                completion_decision_id=decision.decision_id,
                contract_sha256=contract.contract_sha256,
                test_set_sha256=decision.test_set_sha256,
                environment_set_sha256=decision.environment_set_sha256,
                new_regressions=comparison.totals()["new_regressions"],
                report_artifact_id=report_id,
            ),
            changed_paths=changed,
            queue_budget_remaining=QueueBudgetRemainingV1(
                model_calls=max(0, ledger["max_calls"] - ledger["used_calls"] - ledger["reserved_calls"]) if ledger else 0,
                input_tokens=max(0, ledger["max_input_tokens"] - ledger["used_input_tokens"]) if ledger else 0,
                output_tokens=max(0, ledger["max_output_tokens"] - ledger["used_output_tokens"]) if ledger else 0,
                wall_seconds=0,
            ),
        )
        path = f"prd4/handoffs/{task_id}/{candidate['candidate_id']}-verified.json"
        self.artifact_store.write_json(run_id, path, handoff.model_dump(mode="json"), "verified_task_handoff", task_id)
        self._write_outcome(run_id, task_id, "PASS", candidate["candidate_id"], None, decision.decision_id, report_id, event="TASK_READY_FOR_REVIEW")

    def _settle_outcome(self, run_id: str, task_id: str, decision: CompletionDecisionV1, report_id: str) -> None:
        best = self.best_partial(run_id, task_id)
        event = {
            "FAILED": "TASK_FAILED",
            "UNVERIFIED": "TASK_UNVERIFIED",
            "BLOCKED_ENVIRONMENT": "TASK_BLOCKED_ENVIRONMENT",
            "BUDGET_EXHAUSTED": "TASK_BUDGET_EXHAUSTED",
        }.get(decision.status.value, "TASK_UNVERIFIED")
        self._write_outcome(run_id, task_id, decision.status.value, None, best, decision.decision_id, report_id, event=event)

    def _write_outcome(self, run_id: str, task_id: str, status: str, accepted: Optional[str], best: Optional[str],
                       decision_id: Optional[str], report_id: str, *, event: str) -> None:
        core = {"task_id": task_id, "status": status, "accepted": accepted, "best_partial": best, "decision": decision_id}
        with self.run_store.get_connection() as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO h_task_outcomes(task_id, run_id, status, accepted_candidate_id, best_partial_candidate_id,
                        completion_decision_id, report_artifact_id, outcome_sha256, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(task_id) DO UPDATE SET status = excluded.status,
                        accepted_candidate_id = excluded.accepted_candidate_id,
                        best_partial_candidate_id = excluded.best_partial_candidate_id,
                        completion_decision_id = excluded.completion_decision_id,
                        report_artifact_id = excluded.report_artifact_id,
                        outcome_sha256 = excluded.outcome_sha256, updated_at = excluded.updated_at
                    """,
                    (task_id, run_id, status, accepted, best, decision_id, report_id, _sha(core), _now()),
                )
                append_event_sql(conn, run_id, event, core, "PRD4", "PRD4", _now())

    def record_terminal_outcome(self, run_id: str, task_id: str, status: str, reason: str) -> None:
        """Project a non-verification terminal state (budget, needs input, cancel) as a task outcome."""
        if self.task_outcome(task_id):
            return
        path = f"prd4/outcomes/{task_id}/{status.lower()}-{uuid.uuid4().hex[:8]}.json"
        self.artifact_store.write_json(run_id, path, {"task_id": task_id, "status": status, "reason": reason}, "verification_report", task_id)
        artifact = self.artifact_store.get_artifact_by_path(run_id, path)
        best = self.best_partial(run_id, task_id)
        self._write_outcome(run_id, task_id, status, None, best, None, artifact["artifact_id"], event=f"TASK_{status}")

    def best_partial(self, run_id: str, task_id: str) -> Optional[str]:
        """Deterministic best non-PASS candidate (PRD 4 section 15.5)."""
        with self.run_store.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT c.candidate_id, c.candidate_ordinal, d.decision_json, r.new_regression_count, r.inconclusive_count
                FROM h_candidate_snapshots c
                LEFT JOIN h_completion_decisions d ON d.candidate_id = c.candidate_id
                LEFT JOIN h_regression_comparisons r ON r.attempt_id = d.attempt_id
                WHERE c.run_id = ? AND c.task_id = ?
                """,
                (run_id, task_id),
            ).fetchall()
        best_key = None
        best_id = None
        git = self.workspaces.git(run_id)
        for row in rows:
            decision = json.loads(row["decision_json"]) if row["decision_json"] else {}
            reasons = decision.get("reason_codes", [])
            integrity = any("INTEGRITY" in reason or "DIFF_REVIEW_BLOCKING" in reason for reason in reasons)
            satisfied = sum(1 for c in decision.get("criteria", []) if c.get("status") == "SATISFIED")
            candidate = self.workspaces.get_candidate(row["candidate_id"])
            size = len(git.diff_paths(candidate["task_start_commit"], candidate["candidate_commit"])) if candidate else 0
            key = (
                0 if integrity else 1,
                satisfied,
                -(row["new_regression_count"] or 0),
                -(row["inconclusive_count"] or 0),
                -size,
                row["candidate_ordinal"],
            )
            if best_key is None or key > best_key:
                best_key, best_id = key, row["candidate_id"]
        return best_id


@dataclass
class CommitVerification:
    status: str
    report_artifact_id: str
    contract_set_artifact_id: str
    contract_set_sha256: str
    per_contract: Dict[str, str]
    reused_checks: int
    executed_checks: int


def _commit_status(verdicts: Sequence[str]) -> str:
    if any(v == "NOT_SATISFIED" for v in verdicts):
        return "FAILED"
    if any(v == "BLOCKED_ENVIRONMENT" for v in verdicts):
        return "BLOCKED_ENVIRONMENT"
    if any(v in ("INCONCLUSIVE", "NOT_RUN") for v in verdicts):
        return "UNVERIFIED"
    return "PASS"


def verify_commit(
    service: VerificationService,
    run_id: str,
    *,
    commit: str,
    contracts: Sequence[VerificationContractV1],
    artifact_prefix: str,
    changed_paths: Sequence[str],
    reuse: bool = True,
) -> CommitVerification:
    """Run the union of ``contracts`` on an integration commit (PRD 5 section 16).

    A check is reused only when a settled run exists with an identical
    environment hash (same content tree, check, runtime, contract, overlay);
    otherwise it re-runs in a fresh environment. Each contract is then judged
    against its own baseline.
    """
    git = service.workspaces.git(run_id)
    tree = git.commit_tree_of(commit)
    entries = [entry for entry in git.ls_tree(tree) if entry.object_type == "blob"]
    blobs = git.cat_blobs(entry.oid for entry in entries)
    lines = [f"{e.mode} {hashlib.sha256(blobs[e.oid]).hexdigest()} {e.path}" for e in sorted(entries, key=lambda x: x.path)]
    content_sha = hashlib.sha256(("\n".join(lines) + ("\n" if lines else "")).encode()).hexdigest()
    runtime = service.runtime()
    _, local_modules = inventory(service.environments.pristine(run_id, commit))
    contract_set = [{"contract_id": c.contract_id, "task_id": c.task_id, "contract_sha256": c.contract_sha256} for c in contracts]
    set_path = f"{artifact_prefix}/contract-set.json"
    service.artifact_store.write_json(run_id, set_path, contract_set, "aggregate_contract_set")
    set_artifact = service.artifact_store.get_artifact_by_path(run_id, set_path)
    per_contract: Dict[str, str] = {}
    details: List[Dict[str, Any]] = []
    by_command: Dict[str, CheckObservation] = {}
    reused = executed = 0
    all_verdicts: List[str] = []
    try:
        for contract in contracts:
            baseline = service._baseline_cases(contract)
            evidence: List[CheckEvidence] = []
            for check in contract.checks:
                if not check.required:
                    continue
                deps = service._deps(run_id, contract.task_id, commit)
                env_sha = service.environments.environment_sha256(
                    runtime, content_sha256=content_sha, check=check, overlay_sha256=None, contract_sha256=contract.contract_sha256,
                    dependency_sha256=deps.get("dependency_sha256"),
                )
                with service.run_store.get_connection() as conn:
                    prior = conn.execute(
                        "SELECT check_run_id, status FROM h_check_runs WHERE environment_sha256 = ? ORDER BY settled_at DESC LIMIT 1",
                        (env_sha,),
                    ).fetchone()
                    cases = {
                        row["normalized_test_id"]: row for row in conn.execute(
                            "SELECT * FROM h_test_case_results WHERE check_run_id = ?", (prior["check_run_id"],)
                        ).fetchall()
                    } if prior else {}
                if prior is not None and not reuse:
                    prior = None
                if prior is not None:
                    reused += 1
                    item = CheckEvidence(
                        check,
                        CheckStatus(prior["status"]),
                        cases={key: row["status"] for key, row in cases.items()},
                        signatures={key: row["failure_signature_sha256"] for key, row in cases.items()},
                        check_run_id=prior["check_run_id"],
                    )
                else:
                    command_key = _sha({"argv": check.argv, "cwd": check.cwd, "parser": check.parser})
                    observation = by_command.get(command_key)
                    if observation is None:
                        try:
                            service._reserve(run_id, check.timeout_seconds + 10, service.limits.stdout_bytes + service.limits.stderr_bytes)
                        except VerificationBudgetExhaustedError:
                            evidence.append(CheckEvidence(check, None, not_run_reason="VERIFICATION_BUDGET_EXHAUSTED"))
                            continue
                        observation = service.environments.run_check(
                            run_id, contract.task_id,
                            commit=commit,
                            content_sha256=content_sha,
                            contract_sha256=contract.contract_sha256,
                            check=check,
                            runtime=runtime,
                            artifact_prefix=f"{artifact_prefix}/{contract.task_id}-{check.check_id}",
                            labels={"org.dobby.run_id": run_id, "org.dobby.scope": "integration"},
                            changed_paths=changed_paths,
                            local_modules=sorted(local_modules),
                            **deps,
                        )
                        with service.run_store.get_connection() as conn:
                            with conn:
                                service._settle_budget_sql(
                                    conn, run_id, check.timeout_seconds + 10, service.limits.stdout_bytes + service.limits.stderr_bytes,
                                    math.ceil(observation.outcome.elapsed_ms / 1000),
                                    observation.outcome.stdout_bytes + observation.outcome.stderr_bytes,
                                )
                        by_command[command_key] = observation
                        executed += 1
                    item = CheckEvidence(
                        check,
                        observation.status,
                        cases=observation.parsed.case_map(),
                        signatures={case.test_id: case.failure_signature for case in observation.parsed.cases},
                    )
                base = baseline.get(check.check_id)
                if base and base["status"] not in ("BLOCKED_ENVIRONMENT", "UNPARSABLE", "TIMEOUT", "OOM", "OUTPUT_LIMIT", "UNEXPECTED_MUTATION", "INTERNAL_ERROR", "CANCELLED"):
                    item.baseline_cases = {test: value["status"] for test, value in base["cases"].items()}
                    item.baseline_signatures = {test: value.get("signature") for test, value in base["cases"].items()}
                    item.baseline_status = base["status"]
                evidence.append(item)
            comparison = compare(evidence)
            verdicts = [v.verdict for v in comparison.verdicts.values() if v.required]
            all_verdicts.extend(verdicts)
            per_contract[contract.contract_id] = _commit_status(verdicts)
            details.append({
                "contract_id": contract.contract_id,
                "task_id": contract.task_id,
                "status": per_contract[contract.contract_id],
                "verdicts": {key: value.__dict__ for key, value in comparison.verdicts.items()},
            })
    finally:
        try:
            service.environments.discard_pristine(run_id, commit)
        except Exception:
            pass
    status = _commit_status(all_verdicts) if contracts else "UNVERIFIED"
    report_path = f"{artifact_prefix}/report.json"
    service.artifact_store.write_json(run_id, report_path, {
        "commit": commit,
        "content_sha256": content_sha,
        "status": status,
        "contracts": details,
        "reused_checks": reused,
        "executed_checks": executed,
    }, "aggregate_verification_report")
    report = service.artifact_store.get_artifact_by_path(run_id, report_path)
    return CommitVerification(
        status=status,
        report_artifact_id=report["artifact_id"],
        contract_set_artifact_id=set_artifact["artifact_id"],
        contract_set_sha256=set_artifact["sha256"],
        per_contract=per_contract,
        reused_checks=reused,
        executed_checks=executed,
    )


def load_contract_by_id(service: VerificationService, contract_id: str) -> VerificationContractV1:
    with service.run_store.get_connection() as conn:
        row = conn.execute("SELECT * FROM h_verification_contracts WHERE contract_id = ?", (contract_id,)).fetchone()
    if not row:
        raise VerificationError(f"Unknown contract {contract_id}")
    return service.load_contract(dict(row))
