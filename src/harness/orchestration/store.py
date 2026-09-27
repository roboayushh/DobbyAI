"""Durable plans and unexecuted coder proposals."""
from __future__ import annotations

import ast
import datetime
import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from harness.contracts import CoderDecisionV1, PlanV1
from harness.persistence import ArtifactStore, RunStore, canonical_json


class PlanRevisionError(RuntimeError):
    code = "PLAN_REVISION_INVALID"


class ActionProposalError(RuntimeError):
    code = "ACTION_PROPOSAL_INVALID"


from harness.policy.capabilities import DEFAULT_REGISTRY, LEGACY_ALIASES

KNOWN_PRD3_CAPABILITIES = frozenset(
    {
        "read_file",
        "list_files",
        "search_text",
        "apply_patch",
        "run",
        "write_file",
    }
    | set(LEGACY_ALIASES)
    | set(DEFAULT_REGISTRY.names())
)


@dataclass(frozen=True)
class PlanRecord:
    plan_id: str
    artifact_id: str
    sha256: str
    plan: PlanV1


@dataclass(frozen=True)
class ProposalRecord:
    action_proposal_id: str
    proposal_artifact_id: str
    code_artifact_id: str
    code_sha256: str


class OrchestrationRecordStore:
    def __init__(self, run_store: RunStore, artifact_store: ArtifactStore) -> None:
        self.run_store = run_store
        self.artifact_store = artifact_store

    def plan_identity(self, task_id: str) -> Dict[str, Any]:
        """The host-owned identity fields the next plan for ``task_id`` must carry."""
        with self.run_store.get_connection() as conn:
            lifecycle = conn.execute(
                "SELECT task_revision FROM h_task_lifecycle WHERE task_id = ?", (task_id,)
            ).fetchone()
            next_revision = conn.execute(
                "SELECT COALESCE(MAX(plan_revision), 0) + 1 FROM h_plans WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
        return {
            "task_id": task_id,
            "task_revision": lifecycle["task_revision"] if lifecycle else None,
            "plan_revision": next_revision,
        }

    def put_plan(
        self,
        *,
        run_id: str,
        task_id: str,
        source_revision: str,
        planner_call_id: str,
        plan: PlanV1,
    ) -> PlanRecord:
        if plan.task_id != task_id:
            raise PlanRevisionError("Planner returned a plan for a different task")
        with self.run_store.get_connection() as conn:
            task_lifecycle = conn.execute(
                "SELECT * FROM h_task_lifecycle WHERE task_id = ?", (task_id,)
            ).fetchone()
            next_revision = conn.execute(
                "SELECT COALESCE(MAX(plan_revision), 0) + 1 FROM h_plans WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
        if not task_lifecycle:
            raise PlanRevisionError("Task lifecycle is not initialized")
        if plan.task_revision != task_lifecycle["task_revision"]:
            raise PlanRevisionError("Plan task revision is stale")
        if plan.plan_revision != next_revision:
            raise PlanRevisionError(
                f"Expected plan revision {next_revision}, got {plan.plan_revision}"
            )
        self.validate_evidence_references(
            task_id=task_id,
            source_revision=source_revision,
            evidence_ids={
                evidence_id
                for hypothesis in plan.hypotheses
                for evidence_id in hypothesis.evidence_ids
            }
            | set(plan.observed_evidence_ids),
            observed_ids=set(plan.observed_evidence_ids),
        )
        plan_id = f"plan_{uuid.uuid4().hex[:16]}"
        plan_payload = plan.model_dump(mode="json")
        plan_json = canonical_json(plan_payload)
        plan_sha = hashlib.sha256(plan_json.encode("utf-8")).hexdigest()
        artifact_path = f"prd2/plans/{task_id}/{plan_id}.json"
        self.artifact_store.write_bytes(
            run_id,
            artifact_path,
            plan_json.encode("utf-8"),
            "application/json",
            "plan",
            task_id,
        )
        artifact = self.artifact_store.get_artifact_by_path(run_id, artifact_path)
        if not artifact:
            raise RuntimeError("Plan artifact metadata was not persisted")
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self.run_store.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            call = conn.execute(
                "SELECT state, task_id FROM h_model_calls WHERE call_id = ?", (planner_call_id,)
            ).fetchone()
            if not call or call["state"] != "SUCCEEDED" or call["task_id"] != task_id:
                conn.rollback()
                raise PlanRevisionError("Plan must reference a successful planner call")
            conn.execute(
                "UPDATE h_plans SET state = 'SUPERSEDED' WHERE task_id = ? AND state = 'ACTIVE'",
                (task_id,),
            )
            conn.execute(
                """
                INSERT INTO h_plans(
                    plan_id, run_id, task_id, task_revision, plan_revision,
                    source_revision, planner_call_id, artifact_id, plan_sha256,
                    state, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?)
                """,
                (
                    plan_id,
                    run_id,
                    task_id,
                    plan.task_revision,
                    plan.plan_revision,
                    source_revision,
                    planner_call_id,
                    artifact["artifact_id"],
                    plan_sha,
                    now,
                ),
            )
            conn.execute(
                "UPDATE h_task_lifecycle SET active_plan_revision = ? WHERE task_id = ?",
                (plan.plan_revision, task_id),
            )
            conn.commit()
        return PlanRecord(plan_id, artifact["artifact_id"], plan_sha, plan)

    def validate_evidence_references(
        self,
        *,
        task_id: str,
        source_revision: str,
        evidence_ids: set[str],
        observed_ids: Optional[set[str]] = None,
    ) -> None:
        observed_ids = observed_ids or set()
        if not evidence_ids:
            return
        placeholders = ",".join("?" for _ in evidence_ids)
        with self.run_store.get_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT evidence_id, task_id, source_revision, truth_status, valid
                FROM h_evidence
                WHERE evidence_id IN ({placeholders})
                """,
                tuple(sorted(evidence_ids)),
            ).fetchall()
        indexed = {row["evidence_id"]: row for row in rows}
        missing = evidence_ids - indexed.keys()
        if missing:
            raise PlanRevisionError(
                f"Plan references unknown evidence: {', '.join(sorted(missing))}"
            )
        invalid = [
            evidence_id
            for evidence_id, row in indexed.items()
            if row["task_id"] != task_id
            or row["source_revision"] != source_revision
            or not row["valid"]
        ]
        if invalid:
            raise PlanRevisionError(
                f"Plan references stale or cross-task evidence: {', '.join(sorted(invalid))}"
            )
        mislabeled = [
            evidence_id
            for evidence_id in observed_ids
            if indexed[evidence_id]["truth_status"] != "observed"
        ]
        if mislabeled:
            raise PlanRevisionError(
                "Plan cannot promote non-observed evidence to observed: "
                + ", ".join(sorted(mislabeled))
            )

    def get_active_plan(self, task_id: str) -> PlanRecord:
        with self.run_store.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM h_plans WHERE task_id = ? AND state = 'ACTIVE'",
                (task_id,),
            ).fetchone()
        if not row:
            raise KeyError(f"No active plan for task: {task_id}")
        artifact = self.artifact_store.get_artifact_by_id(row["artifact_id"])
        if not artifact:
            raise RuntimeError("Active plan artifact is missing")
        relative = artifact["relative_path"].split("/artifacts/", 1)[-1]
        raw = self.artifact_store.open_readonly(row["run_id"], relative)
        if hashlib.sha256(raw).hexdigest() != row["plan_sha256"]:
            raise RuntimeError("Active plan artifact hash mismatch")
        return PlanRecord(
            row["plan_id"],
            row["artifact_id"],
            row["plan_sha256"],
            PlanV1.model_validate_json(raw),
        )

    def put_action_proposal(
        self,
        *,
        run_id: str,
        task_id: str,
        lifecycle_version: int,
        plan_record: PlanRecord,
        coder_call_id: str,
        decision: CoderDecisionV1,
        authorization_policy_sha256: str,
    ) -> ProposalRecord:
        if decision.task_id != task_id:
            raise ActionProposalError("Coder proposal targets a different task")
        if decision.plan_revision != plan_record.plan.plan_revision:
            raise ActionProposalError("Coder proposal references a stale plan")
        unknown = set(decision.requested_capabilities) - KNOWN_PRD3_CAPABILITIES
        if unknown:
            raise ActionProposalError(
                f"Coder requested unknown capabilities: {', '.join(sorted(unknown))}"
            )
        if "\x00" in decision.python_action:
            raise ActionProposalError("Generated action contains a NUL byte")
        try:
            ast.parse(decision.python_action, mode="exec")
        except SyntaxError as exc:
            raise ActionProposalError(f"Generated action is not valid Python syntax: {exc.msg}") from exc

        proposal_id = f"ap_{uuid.uuid4().hex[:16]}"
        proposal_json = canonical_json(decision.model_dump(mode="json"))
        code_bytes = decision.python_action.encode("utf-8")
        code_sha = hashlib.sha256(code_bytes).hexdigest()
        proposal_path = f"prd2/proposals/{task_id}/{proposal_id}.json"
        code_path = f"prd2/proposals/{task_id}/{proposal_id}.py.txt"
        self.artifact_store.write_bytes(
            run_id,
            proposal_path,
            proposal_json.encode("utf-8"),
            "application/json",
            "action_proposal",
            task_id,
        )
        self.artifact_store.write_bytes(
            run_id,
            code_path,
            code_bytes,
            "text/plain; charset=utf-8",
            "generated_action",
            task_id,
        )
        proposal_artifact = self.artifact_store.get_artifact_by_path(run_id, proposal_path)
        code_artifact = self.artifact_store.get_artifact_by_path(run_id, code_path)
        if not proposal_artifact or not code_artifact:
            raise RuntimeError("Proposal artifacts were not persisted")
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self.run_store.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            call = conn.execute(
                "SELECT state, task_id FROM h_model_calls WHERE call_id = ?", (coder_call_id,)
            ).fetchone()
            if not call or call["state"] != "SUCCEEDED" or call["task_id"] != task_id:
                conn.rollback()
                raise ActionProposalError("Proposal must reference a successful coder call")
            conn.execute(
                """
                INSERT INTO h_action_proposals(
                    action_proposal_id, run_id, task_id, task_revision,
                    lifecycle_version, plan_id, plan_revision, workspace_version,
                    coder_call_id, proposal_artifact_id, code_artifact_id,
                    code_sha256, requested_capabilities_json, declared_paths_json,
                    requested_timeout_seconds, authorization_policy_sha256,
                    state, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'UNEXECUTED', ?)
                """,
                (
                    proposal_id,
                    run_id,
                    task_id,
                    decision.task_revision,
                    lifecycle_version,
                    plan_record.plan_id,
                    decision.plan_revision,
                    decision.workspace_version,
                    coder_call_id,
                    proposal_artifact["artifact_id"],
                    code_artifact["artifact_id"],
                    code_sha,
                    canonical_json(decision.requested_capabilities),
                    canonical_json(decision.declared_paths),
                    decision.max_action_seconds,
                    authorization_policy_sha256,
                    now,
                ),
            )
            conn.commit()
        return ProposalRecord(
            proposal_id,
            proposal_artifact["artifact_id"],
            code_artifact["artifact_id"],
            code_sha,
        )

    def get_proposal(self, run_id: str, task_id: str) -> Optional[Dict[str, Any]]:
        with self.run_store.get_connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM h_action_proposals
                WHERE run_id = ? AND task_id = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (run_id, task_id),
            ).fetchone()
        return dict(row) if row else None
