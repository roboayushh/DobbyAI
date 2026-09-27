"""Reproducibility manifest and replay (PRD 6 section 14).

The manifest records every identity needed to explain a terminal run (harness
build, schemas, adapter, plugin set, model profile, source/candidate, runtime
image and dependency locks, verification contracts, effective configuration)
with secrets excluded. Replays never overwrite the source run:

* ``audit``    rebuild the terminal projection and result from durable evidence
               (artifact hashes re-verified); no model, code, or external effect.
* ``reverify`` rerun the declared checks on the exact candidate in fresh sandboxes
               and record new evidence.
* ``live-model`` is a new linked run (see the CLI); ``recorded`` replay is covered
               by the deterministic scripted-response suite and is not offered as a
               CLI mode (reported as ``REPLAY_MODE_UNSUPPORTED``).
"""
from __future__ import annotations

import datetime
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from harness.config import HARNESS_ROOT
from harness.contracts.release import ReproducibilityManifestV1
from harness.persistence import canonical_json
from harness.persistence.events import append_event_sql
from harness.release.identity import commit_content_sha256, harness_build, sha256_json
from harness.release.run_facts import gather

ZERO = "0" * 64


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ZERO


def effective_configuration(config, *, model_profile_id: str, permission_profile: str, budgets: Dict[str, Any],
                            release_profile: str) -> Dict[str, Any]:
    """Canonical, redacted effective configuration (no secret values, no host paths)."""
    return {
        "model_profile": model_profile_id,
        "permission_profile": permission_profile,
        "release_profile": release_profile,
        "budgets": budgets,
        "dependency_setup": bool(getattr(config, "dependency_setup", True)),
        "auto_build_runtime": bool(getattr(config, "auto_build_runtime", True)),
        "model_profiles_sha256": _file_sha(Path(config.model_profiles_path)),
        "credential": "AI_API_KEY (value never recorded)",
    }


class ReproducibilityService:
    def __init__(self, *, run_store, artifact_store, services, coordinator, plugin_lock_sha256: str = ZERO) -> None:
        self.run_store = run_store
        self.artifact_store = artifact_store
        self.services = services
        self.coordinator = coordinator
        self.plugin_lock_sha256 = plugin_lock_sha256

    def manifest(self, run_id: str, *, effective_config: Dict[str, Any], adapter: Dict[str, str]) -> ReproducibilityManifestV1:
        facts = gather(run_id, run_store=self.run_store, artifact_store=self.artifact_store,
                       services=self.services, coordinator=self.coordinator)
        with self.run_store.get_connection() as conn:
            model = conn.execute("SELECT * FROM h_run_model_config WHERE run_id = ?", (run_id,)).fetchone()
            version = conn.execute("SELECT MAX(version) FROM h_schema_migrations").fetchone()[0]
            contracts = [dict(r) for r in conn.execute(
                "SELECT contract_sha256, test_set_sha256 FROM h_verification_contracts WHERE run_id = ? AND state = 'FROZEN' ORDER BY task_id",
                (run_id,)).fetchall()]
            envs = [r[0] for r in conn.execute(
                """SELECT d.environment_set_sha256 FROM h_completion_decisions d JOIN h_verification_attempts a
                   ON a.attempt_id = d.attempt_id WHERE a.run_id = ? ORDER BY d.decided_at""", (run_id,)).fetchall()]
        runtime_lock = {}
        try:
            runtime_lock = self.services.runtime_resolver.load_lock()
        except Exception:
            runtime_lock = {}
        git = self.services.workspaces.git(run_id)
        head = facts.candidate.head or None
        build = harness_build()
        core = {
            "schema_version": "1.0",
            "run_id": run_id,
            "harness": {"version": build["version"], "source_commit": build["source_commit"], "build_id": build["build_id"]},
            "schemas": {"request": "1.0", "event": "1.0", "result": "1.0", "database_migration": int(version or 1)},
            "evaluator_adapter": adapter,
            "plugin_set_lock_sha256": self.plugin_lock_sha256,
            "model": {
                "provider": model["endpoint_origin"] if model else "unconfigured",
                "model": model["model_id"] if model else "unconfigured",
                "adapter_version": model["adapter_version"] if model else "unknown",
                "config_sha256": model["profile_fingerprint"] if model else ZERO,
                "sampling": json.loads(model["sampling_json"]) if model else {},
            },
            "source": {
                "baseline_commit": facts.source.get("baseline_commit", ""),
                "baseline_tree": facts.source.get("baseline_tree", ""),
                "candidate_commit": head,
                "candidate_tree": git.commit_tree_of(head) if head else None,
            },
            "runtime": {
                "image_digest": runtime_lock.get("image_id", "unavailable"),
                "architecture": runtime_lock.get("architecture", "unknown"),
                "dependency_lock_sha256": _file_sha(HARNESS_ROOT / "runtime" / "python" / "requirements.lock"),
            },
            "verification": {
                "contract_set_sha256": sha256_json([c["contract_sha256"] for c in contracts]) if contracts else None,
                "test_command_set_sha256": sha256_json([c["test_set_sha256"] for c in contracts]) if contracts else None,
                "environment_set_sha256": sha256_json(envs) if envs else None,
            },
            "effective_configuration_sha256": sha256_json(effective_config),
            "created_at": _now(),
        }
        manifest = ReproducibilityManifestV1(**core, manifest_sha256=sha256_json({k: v for k, v in core.items() if k != "created_at"}))
        return manifest

    def record(self, run_id: str, manifest: ReproducibilityManifestV1, effective_config: Dict[str, Any]) -> Dict[str, str]:
        with self.run_store.get_connection() as conn:
            version = conn.execute("SELECT COALESCE(MAX(manifest_version), 0) + 1 FROM h_reproducibility_manifests WHERE run_id = ?",
                                   (run_id,)).fetchone()[0]
        path = f"prd6/reproducibility/manifest-v{version}.json"
        payload = {**manifest.model_dump(mode="json"), "effective_configuration": effective_config}
        self.artifact_store.write_json(run_id, path, payload, "reproducibility_manifest")
        artifact = self.artifact_store.get_artifact_by_path(run_id, path)
        manifest_id = f"repro_{uuid.uuid4().hex[:16]}"
        candidate_identity = sha256_json(manifest.source.model_dump()) if manifest.source.candidate_commit else None
        with self.run_store.get_connection() as conn:
            with conn:
                conn.execute(
                    "INSERT INTO h_reproducibility_manifests VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (manifest_id, run_id, version, manifest.harness.source_commit, manifest.harness.build_id,
                     manifest.evaluator_adapter.name, manifest.evaluator_adapter.version, manifest.plugin_set_lock_sha256,
                     manifest.effective_configuration_sha256, sha256_json({"b": manifest.source.baseline_commit, "t": manifest.source.baseline_tree}),
                     candidate_identity, sha256_json(manifest.runtime.model_dump()),
                     sha256_json(manifest.verification.model_dump()) if manifest.verification.contract_set_sha256 else None,
                     artifact["artifact_id"], manifest.manifest_sha256, _now()),
                )
                append_event_sql(conn, run_id, "REPRODUCIBILITY_MANIFEST_CREATED", {"manifest_id": manifest_id,
                                                                                     "manifest_sha256": manifest.manifest_sha256}, "PRD6", "PRD6", _now())
        return {"manifest_id": manifest_id, "artifact_id": artifact["artifact_id"]}

    def latest_manifest_id(self, run_id: str) -> Optional[str]:
        with self.run_store.get_connection() as conn:
            row = conn.execute("SELECT reproducibility_manifest_id FROM h_reproducibility_manifests WHERE run_id = ? ORDER BY manifest_version DESC LIMIT 1",
                               (run_id,)).fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------- replays
    def replay(self, run_id: str, mode: str, *, effective_config: Dict[str, Any], adapter: Dict[str, str]) -> Dict[str, Any]:
        mode = mode.lower()
        if mode not in ("audit", "reverify"):
            return {"schema_version": "1.0", "run_id": run_id, "mode": mode, "state": "BLOCKED",
                    "reason": "REPLAY_MODE_UNSUPPORTED" if mode == "recorded" else "USE_A_NEW_RUN_FOR_LIVE_MODEL"}
        manifest_id = self.latest_manifest_id(run_id)
        if manifest_id is None:
            manifest_id = self.record(run_id, self.manifest(run_id, effective_config=effective_config, adapter=adapter), effective_config)["manifest_id"]
        replay_id = f"replay_{uuid.uuid4().hex[:16]}"
        started = _now()
        report = self._audit(run_id) if mode == "audit" else self._reverify(run_id)
        path = f"prd6/replays/{replay_id}.json"
        self.artifact_store.write_json(run_id, path, report, "replay_report")
        artifact = self.artifact_store.get_artifact_by_path(run_id, path)
        with self.run_store.get_connection() as conn:
            with conn:
                conn.execute("INSERT INTO h_replays VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?)",
                             (replay_id, run_id, mode.upper(), manifest_id, report["state"], artifact["artifact_id"], started, _now()))
                append_event_sql(conn, run_id, "REPLAY_SETTLED", {"replay_id": replay_id, "mode": mode, "state": report["state"]},
                                 "PRD6", "PRD6", _now())
        return {**report, "replay_id": replay_id}

    def _audit(self, run_id: str) -> Dict[str, Any]:
        """Re-verify every artifact hash and rebuild the terminal projection from durable records."""
        problems: List[str] = []
        artifacts = self.artifact_store.get_artifacts(run_id)
        run_root = self.artifact_store.data_root / "runs" / run_id / "artifacts"
        for artifact in artifacts:
            relative = str(Path(artifact["relative_path"]).relative_to(Path("runs") / run_id / "artifacts"))
            if not self.artifact_store.verify(run_id, relative):
                problems.append(f"ARTIFACT_HASH_MISMATCH:{relative}")
        facts = gather(run_id, run_store=self.run_store, artifact_store=self.artifact_store,
                       services=self.services, coordinator=self.coordinator)
        recorded = None
        final = facts.final
        if final is not None:
            recomputed = self.coordinator._derive_status(facts.queue, self.coordinator.items(facts.queue), final.aggregate_verification.status)
            if recomputed != final.status:
                problems.append(f"QUEUE_STATUS_DIFFERS:{final.status}->{recomputed}")
            recorded = final.status
        with self.run_store.get_connection() as conn:
            result_row = conn.execute("SELECT result_artifact_id FROM h_evaluator_sessions WHERE run_id = ? AND result_artifact_id IS NOT NULL",
                                      (run_id,)).fetchone()
        if result_row:
            artifact = self.artifact_store.get_artifact_by_id(result_row[0])
            recorded_result = json.loads((self.artifact_store.data_root / artifact["relative_path"]).read_text())
            if recorded_result.get("status") not in (facts.status, "PENDING_APPROVAL", "INVALID", "INTERNAL_ERROR", "BLOCKED_ENVIRONMENT", "CANCELLED"):
                problems.append(f"RESULT_STATUS_DIFFERS:{recorded_result.get('status')}->{facts.status}")
        if facts.candidate.head:
            git = self.services.workspaces.git(run_id)
            if git.read_ref(f"refs/harness/runs/{run_id}/integration") not in (None, facts.candidate.head) and facts.candidate.kind == "INTEGRATED":
                problems.append("INTEGRATION_REF_DIFFERS")
        return {
            "schema_version": "1.0", "run_id": run_id, "mode": "audit",
            "state": "PASS" if not problems else "DIFFERENT",
            "artifacts_verified": len(artifacts), "reconstructed_status": facts.status, "recorded_status": recorded,
            "candidate": facts.candidate.head, "problems": problems[:100],
            "note": "Audit replay reads durable evidence only; no model call, code execution, or external effect occurred.",
        }

    def _reverify(self, run_id: str) -> Dict[str, Any]:
        from harness.verification.service import load_contract_by_id, verify_commit

        facts = gather(run_id, run_store=self.run_store, artifact_store=self.artifact_store,
                       services=self.services, coordinator=self.coordinator)
        verifier = self.services.verifier
        if not facts.candidate.head or facts.candidate.head == facts.candidate.base:
            return {"schema_version": "1.0", "run_id": run_id, "mode": "reverify", "state": "BLOCKED", "reason": "NO_CANDIDATE"}
        contracts = []
        for task in facts.tasks:
            row = verifier.contract_row(run_id, task.task_id)
            if row:
                contracts.append(load_contract_by_id(verifier, row["contract_id"]))
        git = self.services.workspaces.git(run_id)
        prefix = f"prd6/replays/reverify-{uuid.uuid4().hex[:8]}"
        result = verify_commit(verifier, run_id, commit=facts.candidate.head, contracts=contracts, artifact_prefix=prefix,
                               changed_paths=[p for _, p in git.diff_paths(facts.candidate.base, facts.candidate.head)], reuse=False)
        original = facts.final.aggregate_verification.status if facts.final is not None else None
        if original in (None, "NOT_RUN"):  # evaluation cases are never integrated: compare the task decisions
            statuses = {(task.decision or {}).get("status") for task in facts.tasks if task.decision}
            original = "PASS" if statuses == {"PASS"} else ("FAILED" if "FAILED" in statuses else "UNVERIFIED")
        return {
            "schema_version": "1.0", "run_id": run_id, "mode": "reverify",
            "state": "PASS" if result.status == original else "DIFFERENT",
            "original_status": original, "reverified_status": result.status,
            "executed_checks": result.executed_checks, "reused_checks": result.reused_checks,
            "report_artifact_id": result.report_artifact_id,
            "note": "Reverify creates new evidence on the exact candidate; the original run is unchanged.",
        }
