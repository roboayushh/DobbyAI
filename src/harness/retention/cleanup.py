"""Registered-resource retention and cleanup (PRD 6 section 12).

Only exact resources registered to one run under ``<data>/runs/<run_id>`` are ever
eligible. The original source, export bundles, the shared dependency cache, and
other runs are always excluded. No glob, environment variable, home alias, broad
root, or model-supplied path is used. Deletion requires an exact approval grant
bound to the plan hash; each target is identity-checked immediately before
removal and settled individually, so a crash never deletes an unrelated path.
"""
from __future__ import annotations

import datetime
import hashlib
import os
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from harness.approvals.capabilities import CapabilityError, CapabilityService, KernelPrincipal
from harness.contracts.release import CleanupPlanV1
from harness.gitflow.private_git import make_writable_tree, secure_rmtree
from harness.persistence import canonical_json
from harness.persistence.events import append_event_sql
from harness.release.profiles import retention_policy


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _sha(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _size(path: Path) -> int:
    total = 0
    for current, dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += (Path(current) / name).lstat().st_size
            except OSError:
                pass
    return total


def _identity(path: Path) -> str:
    stat = path.lstat()
    return _sha({"path": str(path), "dev": stat.st_dev, "ino": stat.st_ino})


class CleanupService:
    def __init__(self, *, run_store, artifact_store, data_root: Path, backend=None, capabilities: Optional[CapabilityService] = None) -> None:
        self.run_store = run_store
        self.artifact_store = artifact_store
        self.data_root = Path(data_root).resolve()
        self.backend = backend
        self.capabilities = capabilities or CapabilityService(run_store, artifact_store)

    def run_root(self, run_id: str) -> Path:
        if not run_id.startswith("run_") or "/" in run_id or ".." in run_id:
            raise CapabilityError("CLEANUP_TARGET_REJECTED", "Invalid run identifier")
        return (self.data_root / "runs" / run_id).resolve()

    def _settled(self, run_id: str) -> bool:
        with self.run_store.get_connection() as conn:
            queue = conn.execute("SELECT state FROM h_task_queues WHERE run_id = ?", (run_id,)).fetchone()
            lifecycle = conn.execute("SELECT state FROM h_run_lifecycle WHERE run_id = ?", (run_id,)).fetchone()
            unsettled = conn.execute("SELECT COUNT(*) FROM h_actions WHERE run_id = ? AND state IN ('INTENT', 'RUNNING', 'SETTLING', 'UNKNOWN')",
                                     (run_id,)).fetchone()[0]
        if unsettled:
            return False
        if queue:
            return queue["state"] == "SETTLED"
        return lifecycle is None or lifecycle["state"] in ("READY_FOR_REVIEW", "VERIFICATION_FAILED", "UNVERIFIED", "FAILED",
                                                           "CANCELLED", "NEEDS_INPUT", "BUDGET_EXHAUSTED", "QUEUE_SETTLED", "PLAN_READY",
                                                           "ACTION_PROPOSED")

    def inventory(self, run_id: str) -> List[Dict[str, Any]]:
        root = self.run_root(run_id)
        policy = retention_policy(self.run_store)["classes"]
        settled = self._settled(run_id)
        resources: List[Dict[str, Any]] = []

        def add(resource_id: str, kind: str, path: Path) -> None:
            if not path.exists() and not path.is_symlink():
                return
            if path.is_symlink() or root not in path.resolve().parents:
                eligibility = "PROTECTED"
            else:
                eligibility = policy.get(kind, "RETAIN")
                if eligibility == "ELIGIBLE" and not settled and kind in ("TASK_WORKTREE", "VERIFICATION_WORKTREE", "CACHE"):
                    eligibility = "ACTIVE_REFERENCE"
            resources.append({"resource_id": resource_id, "kind": kind, "path": path, "eligibility": eligibility})

        add("verification_worktrees", "VERIFICATION_WORKTREE", root / "verification")
        add("export_work", "CACHE", root / "export-work")
        add("prd1_temp", "CACHE", root / "temp")
        add("prd1_workspace", "TASK_WORKTREE", root / "workspace")
        tasks_dir = root / "tasks"
        if tasks_dir.is_dir():
            for task_dir in sorted(p for p in tasks_dir.iterdir() if p.is_dir() and not p.is_symlink()):
                add(f"{task_dir.name}.workspace", "TASK_WORKTREE", task_dir / "workspace")
                add(f"{task_dir.name}.actions", "CACHE", task_dir / "actions")
        add("artifacts", "RUN_ARTIFACT", root / "artifacts")
        add("private_repository", "PRIVATE_REF", root / "repo.git")
        return resources

    def plan(self, run_id: str) -> CleanupPlanV1:
        root = self.run_root(run_id)
        if not root.is_dir():
            raise CapabilityError("CLEANUP_TARGET_REJECTED", f"Run {run_id} has no registered data root")
        policy = retention_policy(self.run_store)
        targets = []
        eligible_bytes = 0
        for resource in self.inventory(run_id):
            size = _size(resource["path"]) if not resource["path"].is_symlink() else 0
            if resource["eligibility"] == "ELIGIBLE":
                eligible_bytes += size
            targets.append({"resource_id": resource["resource_id"], "kind": resource["kind"],
                            "registered_path_hash": hashlib.sha256(str(resource["path"]).encode()).hexdigest(),
                            "estimated_bytes": size, "eligibility": resource["eligibility"]})
        source = self.run_store.get_source_snapshot(run_id) or {}
        excluded = [{"resource_id": "source_original", "reason": "PROTECTED_ORIGINAL"},
                    {"resource_id": "dependency_cache", "reason": "SHARED_CACHE"}]
        with self.run_store.get_connection() as conn:
            for row in conn.execute("SELECT export_id FROM h_exports WHERE run_id = ?", (run_id,)).fetchall():
                excluded.append({"resource_id": row["export_id"], "reason": "OUTSIDE_RUN_RETENTION"})
        core = {"schema_version": "1.0", "cleanup_plan_id": f"clnplan_{uuid.uuid4().hex[:16]}", "run_id": run_id,
                "retention_policy_id": policy["retention_policy_id"] or policy["name"], "targets": targets, "excluded": excluded,
                "estimated_reclaimed_bytes": eligible_bytes, "requires_approval": True}
        stable = {**core, "cleanup_plan_id": None, "targets": [{k: v for k, v in t.items() if k != "estimated_bytes"} for t in targets]}
        return CleanupPlanV1(**core, plan_sha256=_sha(stable))

    # -------------------------------------------------------------- request
    def request(self, run_id: str, plan: CleanupPlanV1, principal: KernelPrincipal) -> Dict[str, Any]:
        """Persist the plan and a CLEANUP_RUN capability request bound to the plan hash."""
        with self.run_store.get_connection() as conn:
            existing = conn.execute(
                """SELECT c.capability_request_id, c.state FROM h_cleanup_plans p JOIN h_capability_requests c
                   ON c.capability_request_id = p.capability_request_id
                   WHERE p.run_id = ? AND p.plan_sha256 = ? AND c.state IN ('PENDING', 'APPROVED') ORDER BY p.created_at DESC LIMIT 1""",
                (run_id, plan.plan_sha256)).fetchone()
        if existing:
            return {"capability_request_id": existing["capability_request_id"], "state": existing["state"]}
        request = self.capabilities.create(
            run_id, "CLEANUP_RUN", principal=principal, target={"run_id": run_id},
            artifact_sha256=plan.plan_sha256,
            summary=f"Remove {sum(1 for t in plan.targets if t.eligibility == 'ELIGIBLE')} registered transient resources "
                    f"({plan.estimated_reclaimed_bytes} bytes) of run {run_id}; evidence, refs, database, original source and exports are kept.",
        )
        path = f"prd6/cleanup/{plan.cleanup_plan_id}.json"
        self.artifact_store.write_json(run_id, path, plan.model_dump(mode="json"), "cleanup_plan")
        artifact = self.artifact_store.get_artifact_by_path(run_id, path)
        with self.run_store.get_connection() as conn:
            with conn:
                conn.execute("INSERT INTO h_cleanup_plans VALUES (?, ?, ?, ?, ?, 1, ?, ?, 'PLANNED', ?, NULL)",
                             (plan.cleanup_plan_id, run_id, self._policy_row(), request.capability_request_id, plan.estimated_reclaimed_bytes,
                              artifact["artifact_id"], plan.plan_sha256, _now()))
                for ordinal, target in enumerate(plan.targets):
                    conn.execute("INSERT INTO h_cleanup_targets VALUES (?, ?, ?, ?, ?, ?, ?, 'PLANNED', ?)",
                                 (f"clntgt_{uuid.uuid4().hex[:16]}", plan.cleanup_plan_id, target.resource_id, target.kind,
                                  target.registered_path_hash, target.estimated_bytes, target.eligibility, ordinal))
                append_event_sql(conn, run_id, "CLEANUP_PLAN_CREATED", {"cleanup_plan_id": plan.cleanup_plan_id,
                                                                        "capability_request_id": request.capability_request_id}, "PRD6", "PRD6", _now())
        return {"capability_request_id": request.capability_request_id, "state": "PENDING"}

    def _policy_row(self) -> str:
        policy = retention_policy(self.run_store)
        if not policy["retention_policy_id"]:
            raise CapabilityError("RETENTION_POLICY_MISSING", "Retention policy is not registered")
        return policy["retention_policy_id"]

    # -------------------------------------------------------------- execute
    def execute(self, run_id: str) -> Dict[str, Any]:
        """Execute the newest approved cleanup plan for ``run_id`` (exactly once)."""
        with self.run_store.get_connection() as conn:
            row = conn.execute(
                """SELECT p.*, c.request_sha256 FROM h_cleanup_plans p JOIN h_capability_requests c
                   ON c.capability_request_id = p.capability_request_id
                   WHERE p.run_id = ? AND c.state = 'APPROVED' AND p.state = 'PLANNED' ORDER BY p.created_at DESC LIMIT 1""",
                (run_id,)).fetchone()
        if row is None:
            raise CapabilityError("APPROVAL_MISSING", "No approved cleanup plan; run `harness clean RUN_ID` and approve the request")
        current = self.plan(run_id)
        if current.plan_sha256 != row["plan_sha256"]:
            raise CapabilityError("APPROVAL_BINDING_CHANGED", "Registered resources changed since approval; create a new cleanup plan")
        grant = self.capabilities.active_grant(row["capability_request_id"], row["request_sha256"])
        intent_id = f"eff_{uuid.uuid4().hex[:16]}"
        intent = {"schema_version": "1.0", "effect_intent_id": intent_id, "run_id": run_id, "operation": "CLEANUP_RUN",
                  "cleanup_plan_id": row["cleanup_plan_id"], "plan_sha256": row["plan_sha256"]}
        self.artifact_store.write_json(run_id, f"prd6/effects/{intent_id}/intent.json", intent, "external_effect_intent")
        intent_artifact = self.artifact_store.get_artifact_by_path(run_id, f"prd6/effects/{intent_id}/intent.json")
        with self.run_store.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                consumption = self.capabilities.consume_sql(conn, grant)
                conn.execute(
                    "INSERT INTO h_external_effect_intents VALUES (?, ?, ?, ?, 'CLEANUP_RUN', 'builtin.cleanup', '1.0.0', ?, ?, ?, ?, NULL, ?, ?, ?, 'PREPARED', ?, NULL, NULL)",
                    (intent_id, run_id, row["capability_request_id"], consumption, _sha({"run": run_id}), _sha({"root": str(self.run_root(run_id))}),
                     row["plan_sha256"], _sha({"removed": row["plan_sha256"]}), f"cleanup-{row['cleanup_plan_id']}",
                     intent_artifact["artifact_id"], _sha(intent), _now()),
                )
                conn.execute("UPDATE h_cleanup_plans SET state = 'EXECUTING' WHERE cleanup_plan_id = ?", (row["cleanup_plan_id"],))
                append_event_sql(conn, run_id, "APPROVAL_CONSUMED", {"capability_request_id": row["capability_request_id"],
                                                                      "consumption_id": consumption}, "PRD6", "PRD6", _now())
                append_event_sql(conn, run_id, "EXTERNAL_EFFECT_INTENT_RECORDED", {"effect_intent_id": intent_id, "operation": "CLEANUP_RUN"},
                                 "PRD6", "PRD6", _now())
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return self._run_targets(run_id, row["cleanup_plan_id"], intent_id, consumption)

    def _run_targets(self, run_id: str, plan_id: str, intent_id: str, consumption: str) -> Dict[str, Any]:
        root = self.run_root(run_id)
        resources = {r["resource_id"]: r for r in self.inventory(run_id)}
        with self.run_store.get_connection() as conn:
            targets = [dict(r) for r in conn.execute("SELECT * FROM h_cleanup_targets WHERE cleanup_plan_id = ? ORDER BY ordinal", (plan_id,)).fetchall()]
            conn.execute("UPDATE h_approval_consumptions SET state = 'DISPATCHED', dispatched_at = ? WHERE approval_consumption_id = ?", (_now(), consumption))
            conn.execute("UPDATE h_external_effect_intents SET state = 'DISPATCHED', dispatched_at = ? WHERE external_effect_intent_id = ?", (_now(), intent_id))
            conn.commit()
        removed, residual, uncertain = [], [], []
        for target in targets:
            if target["eligibility"] != "ELIGIBLE":
                state = "RETAINED"
            else:
                resource = resources.get(target["registered_resource_id"])
                if resource is None:
                    state = "MISSING"
                else:
                    path = resource["path"]
                    if hashlib.sha256(str(path).encode()).hexdigest() != target["registered_identity_sha256"] or path.is_symlink() \
                            or root not in path.resolve().parents:
                        state = "UNCERTAIN"
                        uncertain.append(target["registered_resource_id"])
                    else:
                        with self.run_store.get_connection() as conn:
                            with conn:
                                conn.execute("UPDATE h_cleanup_targets SET state = 'TOMBSTONED' WHERE cleanup_target_id = ?", (target["cleanup_target_id"],))
                        try:
                            make_writable_tree(path)
                            secure_rmtree(path, root)
                        except Exception:
                            pass
                        state = "REMOVED" if not path.exists() else "FAILED"
                        (removed if state == "REMOVED" else residual).append(target["registered_resource_id"])
            with self.run_store.get_connection() as conn:
                with conn:
                    conn.execute("UPDATE h_cleanup_targets SET state = ? WHERE cleanup_target_id = ?", (state, target["cleanup_target_id"]))
                    append_event_sql(conn, run_id, "CLEANUP_TARGET_SETTLED", {"resource_id": target["registered_resource_id"], "state": state},
                                     "PRD6", "PRD6", _now())
        status = "CLEANUP_UNCERTAIN" if uncertain else ("PARTIAL_CLEANUP" if residual else "CLEANED")
        plan_state = {"CLEANED": "SETTLED", "PARTIAL_CLEANUP": "PARTIAL", "CLEANUP_UNCERTAIN": "UNCERTAIN"}[status]
        report = {"schema_version": "1.0", "run_id": run_id, "cleanup_plan_id": plan_id, "status": status,
                  "removed": removed, "residual": residual, "uncertain": uncertain}
        self.artifact_store.write_json(run_id, f"prd6/effects/{intent_id}/receipt.json", report, "external_effect_receipt")
        receipt_artifact = self.artifact_store.get_artifact_by_path(run_id, f"prd6/effects/{intent_id}/receipt.json")
        with self.run_store.get_connection() as conn:
            with conn:
                conn.execute("INSERT INTO h_external_effect_observations VALUES (?, ?, 1, ?, ?, ?, ?)",
                             (f"effobs_{uuid.uuid4().hex[:16]}", intent_id, _sha(report), "DESIRED" if status == "CLEANED" else "PARTIAL",
                              receipt_artifact["artifact_id"], _now()))
                conn.execute("INSERT INTO h_external_effect_receipts VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?)",
                             (f"ercpt_{uuid.uuid4().hex[:16]}", intent_id, status, _sha(report), "OBSERVED_AFTER_REMOVAL",
                              receipt_artifact["artifact_id"], receipt_artifact["sha256"], _now()))
                conn.execute("UPDATE h_external_effect_intents SET state = ?, settled_at = ? WHERE external_effect_intent_id = ?",
                             ("SETTLED" if status == "CLEANED" else "UNCERTAIN" if uncertain else "SETTLED", _now(), intent_id))
                conn.execute("UPDATE h_approval_consumptions SET state = 'SETTLED', settled_at = ? WHERE approval_consumption_id = ?", (_now(), consumption))
                conn.execute("UPDATE h_cleanup_plans SET state = ?, settled_at = ? WHERE cleanup_plan_id = ?", (plan_state, _now(), plan_id))
                append_event_sql(conn, run_id, "EXTERNAL_EFFECT_SETTLED", {"effect_intent_id": intent_id, "status": status}, "PRD6", "PRD6", _now())
        return report
