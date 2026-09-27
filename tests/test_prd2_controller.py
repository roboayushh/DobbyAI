from __future__ import annotations

import datetime
import hashlib
import json
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from harness.contracts import (
    LimitsV1,
    PreparedRunResultV1,
    QueueSummaryV1,
    RepositoryKind,
    RepositoryRefV1,
    RunRequestV1,
    TaskInputV1,
    TaskMode,
    TaskSpecV1,
    Role,
    OrchestrationState,
)
from harness.model import (
    FakeModelAdapter,
    ModelAdapterError,
    ModelAdapterResponse,
    ModelCallRequest,
    ModelProfileResolver,
)
from harness.orchestration import OrchestrationController, PreparedHandoffError
from harness.persistence import ArtifactStore, RunStore, canonical_json


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _profile_file(tmp_path: Path) -> Path:
    path = tmp_path / "profiles.toml"
    path.write_text(
        """
[profiles.designated]
protocol = "openai-compatible-chat"
adapter = "openai-compatible"
endpoint = "https://provider.example/v1"
model = "official-model-id"
context_window_tokens = 32000
max_output_tokens = 4000
safety_margin_tokens = 2000
tokenizer = "conservative-v1"
request_timeout_seconds = 120
temperature = 0.0
top_p = 1.0
supports_json_schema = true
allow_streaming = false
""".strip(),
        encoding="utf-8",
    )
    return path


def _make_readonly(root: Path) -> None:
    for current_root, dirs, files in os.walk(root):
        for name in files:
            path = Path(current_root) / name
            os.chmod(path, path.stat().st_mode & ~0o222)
        for name in dirs:
            path = Path(current_root) / name
            os.chmod(path, path.stat().st_mode & ~0o222)
    os.chmod(root, root.stat().st_mode & ~0o222)


def _prepared_run(tmp_path: Path):
    data_root = tmp_path / "data"
    workspace = data_root / "runs/run_1/workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "tests").mkdir()
    (workspace / "src/example.py").write_text(
        "def normalize_value(value):\n    return value.strip()\n", encoding="utf-8"
    )
    (workspace / "tests/test_example.py").write_text(
        "from src.example import normalize_value\n\n"
        "def test_normalize_value():\n    assert normalize_value(' x ') == 'x'\n",
        encoding="utf-8",
    )

    manifest = {}
    for path in sorted(workspace.rglob("*")):
        if path.is_file():
            content = path.read_bytes()
            relative = path.relative_to(workspace).as_posix()
            manifest[relative] = {
                "path": relative,
                "mode": "100644",
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "mtime_ns": path.stat().st_mtime_ns,
            }
    lines = [
        f"{manifest[path]['mode']} {manifest[path]['sha256']} {path}"
        for path in sorted(manifest)
    ]
    content_tree = hashlib.sha256(("\n".join(lines) + "\n").encode()).hexdigest()

    task_body = "normalize_value must continue trimming surrounding whitespace"
    task_hash = hashlib.sha256(task_body.encode()).hexdigest()
    task = TaskSpecV1(
        task_id="task_1",
        ordinal=0,
        source_type="direct_text",
        source_key=f"direct:{task_hash}",
        title=task_body,
        body=task_body,
        raw_content_sha256=task_hash,
        normalized_content_sha256=task_hash,
    )
    request = RunRequestV1(
        idempotency_key="prd2-controller-key",
        task_mode=TaskMode.SINGLE_ISSUE,
        execution_mode="development",
        repository=RepositoryRefV1(
            kind=RepositoryKind.LOCAL_GIT, locator=str(tmp_path.resolve())
        ),
        task=TaskInputV1(text=task_body),
        limits=LimitsV1(max_tasks=1),
    )
    source_manifest = {
        "run_id": "run_1",
        "source_path": str(tmp_path),
        "upstream_commit": "U",
        "baseline_commit": "B",
        "baseline_tree": "tree",
        "content_tree_sha256": content_tree,
        "dirty_source_imported": False,
        "file_count": len(manifest),
        "total_bytes": sum(item["size"] for item in manifest.values()),
        "files": manifest,
        "created_at": _now(),
    }
    source_manifest_json = canonical_json(source_manifest)
    task_manifest = {
        "run_id": "run_1",
        "task_mode": "single_issue",
        "metadata": {"mode": "single_issue", "task_id": "task_1"},
        "summary": {
            "discovered": 1,
            "excluded": 0,
            "duplicates": 0,
            "selected": 1,
            "remaining": 0,
        },
        "tasks": [task.model_dump(mode="json")],
    }

    store = RunStore(str(data_root / "harness.db"))
    artifacts = ArtifactStore(str(data_root), store)
    now = _now()
    request_json = canonical_json(request.model_dump(mode="json"))
    with store.get_connection() as conn:
        with conn:
            conn.execute(
                """
                INSERT INTO h_runs(
                    run_id, schema_version, idempotency_key, task_mode, execution_mode,
                    state, request_json, request_sha256, runtime_profile,
                    next_event_seq, created_at, updated_at
                ) VALUES ('run_1', '1.0', ?, 'single_issue', 'development',
                          'PREPARED', ?, ?, 'local-default', 1, ?, ?)
                """,
                (
                    request.idempotency_key,
                    request_json,
                    hashlib.sha256(request_json.encode()).hexdigest(),
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO h_source_snapshots(
                    source_snapshot_id, run_id, source_kind, canonical_locator,
                    upstream_commit, baseline_commit, baseline_tree, content_tree_sha256,
                    import_manifest_sha256, dirty_source_imported, repo_bytes,
                    file_count, created_at
                ) VALUES ('src_1', 'run_1', 'local_git', ?, 'U', 'B', 'tree', ?, ?, 0, ?, ?, ?)
                """,
                (
                    str(tmp_path),
                    content_tree,
                    hashlib.sha256(source_manifest_json.encode()).hexdigest(),
                    source_manifest["total_bytes"],
                    len(manifest),
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO h_workspaces(
                    workspace_id, run_id, source_snapshot_id, root_relpath,
                    bare_repo_relpath, worktree_relpath, state, writable,
                    created_at, updated_at
                ) VALUES ('w_1', 'run_1', 'src_1', 'runs/run_1',
                          'runs/run_1/repo.git', 'runs/run_1/workspace', 'READY', 0, ?, ?)
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO h_tasks(
                    task_id, run_id, ordinal, source_type, source_key,
                    raw_content_sha256, normalized_content_sha256,
                    task_spec_json, state, created_at
                ) VALUES ('task_1', 'run_1', 0, 'direct_text', ?, ?, ?, ?, 'QUEUED', ?)
                """,
                (
                    task.source_key,
                    task.raw_content_sha256,
                    task.normalized_content_sha256,
                    canonical_json(task.model_dump(mode="json")),
                    now,
                ),
            )
    request_artifact = artifacts.write_json(
        "run_1", "request.json", request.model_dump(mode="json"), "request"
    )
    source_artifact = artifacts.write_json(
        "run_1", "source-manifest.json", source_manifest, "source_manifest"
    )
    task_artifact = artifacts.write_json(
        "run_1", "task-manifest.json", task_manifest, "task_manifest"
    )
    result = PreparedRunResultV1(
        run_id="run_1",
        task_mode="single_issue",
        execution_mode="development",
        source={
            "upstream_commit": "U",
            "baseline_commit": "B",
            "content_tree_sha256": content_tree,
        },
        workspace={
            "workspace_id": "w_1",
            "relative_root": "runs/run_1/workspace",
            "writable": False,
        },
        tasks=[{"task_id": "task_1", "ordinal": 0, "state": "QUEUED"}],
        queue_summary=QueueSummaryV1(
            discovered=1, excluded=0, duplicates=0, selected=1, remaining=0
        ),
        artifacts=[request_artifact, source_artifact, task_artifact],
        created_at=now,
    )
    artifacts.write_json("run_1", "result.json", result.model_dump(mode="json"), "result")
    _make_readonly(workspace)
    return data_root, workspace, store, artifacts


def _plan_response(revision: int = 1) -> str:
    return json.dumps(
        {
            "schema_version": "1.0",
            "decision": "PLAN_READY",
            "task_id": "task_1",
            "task_revision": 1,
            "plan_revision": revision,
            "objective": "Preserve normalize_value behavior.",
            "preserved_constraints": ["Do not change the public signature."],
            "acceptance_criteria": [
                {
                    "criterion_id": "ac1",
                    "statement": "Whitespace is trimmed.",
                    "evidence_needed": "Focused test",
                }
            ],
            "observed_evidence_ids": [],
            "hypotheses": [],
            "likely_edit_locations": [
                {"path": "src/example.py", "symbol": "normalize_value", "reason": "implementation"}
            ],
            "steps": [{"step_id": "s1", "purpose": "Inspect and patch", "depends_on": []}],
            "verification_strategy": ["Run focused test"],
            "unresolved_questions": [],
            "required_capabilities": ["read_file", "apply_patch"],
            "step_budget": 2,
        }
    )


def _code_response(plan_revision: int = 1) -> str:
    return json.dumps(
        {
            "schema_version": "1.0",
            "decision": "CODE",
            "task_id": "task_1",
            "task_revision": 1,
            "plan_revision": plan_revision,
            "workspace_version": "B",
            "purpose": "Propose the smallest patch.",
            "requested_capabilities": ["read_file", "apply_patch"],
            "declared_paths": ["src/example.py", "tests/test_example.py"],
            "python_action": "raise RuntimeError('this proposal must never execute in PRD 2')\n",
            "success_observations": ["Patch and regression test are prepared."],
            "max_action_seconds": 30,
        }
    )


class _EvidenceAwareReplanAdapter(FakeModelAdapter):
    def __init__(self, store: RunStore) -> None:
        super().__init__([])
        self.store = store

    def generate(self, request: ModelCallRequest) -> ModelAdapterResponse:
        self.calls.append(request)
        if len(self.calls) == 1:
            raw = _plan_response()
        elif len(self.calls) == 2:
            with self.store.get_connection() as conn:
                evidence_id = conn.execute(
                    """
                    SELECT evidence_id FROM h_evidence
                    WHERE task_id = 'task_1' AND valid = 1
                    ORDER BY created_at, evidence_id LIMIT 1
                    """
                ).fetchone()[0]
            raw = json.dumps(
                {
                    "schema_version": "1.0",
                    "decision": "REPLAN",
                    "task_id": "task_1",
                    "task_revision": 1,
                    "plan_revision": 1,
                    "contradicted_assumption": "The first plan omitted a required case.",
                    "evidence_ids": [evidence_id],
                    "requested_change": "Include the adjacent regression case.",
                }
            )
        else:
            raise AssertionError("Unexpected model call")
        return ModelAdapterResponse(
            raw_text=raw,
            input_tokens=100,
            output_tokens=50,
            usage_source="provider_reported",
            latency_ms=1,
        )


class _BlockingAdapter(FakeModelAdapter):
    def __init__(self) -> None:
        super().__init__([])
        self.started = threading.Event()
        self.released = threading.Event()

    def generate(self, request: ModelCallRequest) -> ModelAdapterResponse:
        self.calls.append(request)
        self.started.set()
        if not self.released.wait(timeout=5):
            raise AssertionError("Cancellation did not reach the active adapter")
        raise ModelAdapterError(
            "MODEL_CANCELLED",
            "Model call was cancelled",
            uncertain_usage=True,
        )

    def cancel(self, call_id: str) -> None:
        super().cancel(call_id)
        self.released.set()


class _InterruptingAdapter(FakeModelAdapter):
    def __init__(self) -> None:
        super().__init__([])

    def generate(self, request: ModelCallRequest) -> ModelAdapterResponse:
        self.calls.append(request)
        raise KeyboardInterrupt("simulated Ctrl-C")


def test_controller_reaches_unexecuted_action_proposal(tmp_path: Path) -> None:
    data_root, workspace, store, artifacts = _prepared_run(tmp_path)
    before = {
        path.relative_to(workspace).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in workspace.rglob("*")
        if path.is_file()
    }
    adapter = FakeModelAdapter([_plan_response(), _code_response()])
    controller = OrchestrationController(
        run_store=store,
        artifact_store=artifacts,
        data_root=data_root,
        profile_resolver=ModelProfileResolver(_profile_file(tmp_path)),
        adapter=adapter,
    )
    result = controller.continue_run("run_1")
    after = {
        path.relative_to(workspace).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in workspace.rglob("*")
        if path.is_file()
    }
    assert result.status.value == "ACTION_PROPOSED"
    assert result.proposal is not None
    assert before == after
    assert store.get_run("run_1")["state"] == "PREPARED"
    with store.get_connection() as conn:
        proposal = conn.execute("SELECT * FROM h_action_proposals").fetchone()
        fingerprints = {
            row[0] for row in conn.execute("SELECT profile_fingerprint FROM h_model_calls")
        }
        reserves = conn.execute(
            """
            SELECT reserved_future_calls, reserved_future_output_tokens,
                   reserved_future_wall_seconds
            FROM h_budget_ledgers WHERE run_id = 'run_1'
            """
        ).fetchone()
    assert proposal["state"] == "UNEXECUTED"
    assert len(fingerprints) == 1
    assert tuple(reserves) == (2, 8_000, 240)
    assert len(adapter.calls) == 2


def test_plan_stop_boundary_does_not_call_coder(tmp_path: Path) -> None:
    data_root, _, store, artifacts = _prepared_run(tmp_path)
    adapter = FakeModelAdapter([_plan_response()])
    controller = OrchestrationController(
        run_store=store,
        artifact_store=artifacts,
        data_root=data_root,
        profile_resolver=ModelProfileResolver(_profile_file(tmp_path)),
        adapter=adapter,
    )
    result = controller.continue_run("run_1", stop_at="plan")
    assert result.status.value == "PLAN_READY"
    assert result.proposal is None
    assert len(adapter.calls) == 1


def test_material_ambiguity_stops_before_coder(tmp_path: Path) -> None:
    data_root, _, store, artifacts = _prepared_run(tmp_path)
    needs_input = json.dumps(
        {
            "schema_version": "1.0",
            "decision": "NEEDS_INPUT",
            "task_id": "task_1",
            "questions": [
                {
                    "question": "Which normalization rule is authoritative?",
                    "impact": "It changes the acceptance contract.",
                }
            ],
        }
    )
    adapter = FakeModelAdapter([needs_input, _code_response()])
    controller = OrchestrationController(
        run_store=store,
        artifact_store=artifacts,
        data_root=data_root,
        profile_resolver=ModelProfileResolver(_profile_file(tmp_path)),
        adapter=adapter,
    )
    result = controller.continue_run("run_1")
    assert result.status == OrchestrationState.NEEDS_INPUT
    assert result.questions[0].question == "Which normalization rule is authoritative?"
    assert len(adapter.calls) == 1
    with store.get_connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM h_model_calls WHERE role = 'coder'"
        ).fetchone()[0] == 0


def test_corrupt_handoff_stops_before_index_or_model(tmp_path: Path) -> None:
    data_root, _, store, artifacts = _prepared_run(tmp_path)
    request_path = data_root / "runs/run_1/artifacts/request.json"
    request_path.write_text('{"tampered":true}', encoding="utf-8")
    adapter = FakeModelAdapter([_plan_response()])
    controller = OrchestrationController(
        run_store=store,
        artifact_store=artifacts,
        data_root=data_root,
        profile_resolver=ModelProfileResolver(_profile_file(tmp_path)),
        adapter=adapter,
    )
    with pytest.raises(PreparedHandoffError):
        controller.continue_run("run_1")
    assert adapter.calls == []
    with store.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM h_file_index").fetchone()[0] == 0
        event = conn.execute(
            "SELECT event_type, payload_json FROM h_events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
    assert event["event_type"] == "ORCHESTRATION_HANDOFF_REJECTED"
    assert json.loads(event["payload_json"])["model_called"] is False


def test_coder_complete_is_only_verification_required(tmp_path: Path) -> None:
    data_root, _, store, artifacts = _prepared_run(tmp_path)
    complete = json.dumps(
        {
            "schema_version": "1.0",
            "decision": "COMPLETE",
            "task_id": "task_1",
            "task_revision": 1,
            "plan_revision": 1,
            "claimed_outcome": "No change appears necessary.",
            "evidence_ids": [],
        }
    )
    controller = OrchestrationController(
        run_store=store,
        artifact_store=artifacts,
        data_root=data_root,
        profile_resolver=ModelProfileResolver(_profile_file(tmp_path)),
        adapter=FakeModelAdapter([_plan_response(), complete]),
    )
    result = controller.continue_run("run_1")
    assert result.status.value == "VERIFICATION_REQUIRED"
    assert result.proposal is None


def test_plan_boundary_can_resume_to_action_proposal(tmp_path: Path) -> None:
    data_root, _, store, artifacts = _prepared_run(tmp_path)
    resolver = ModelProfileResolver(_profile_file(tmp_path))
    planner = OrchestrationController(
        run_store=store,
        artifact_store=artifacts,
        data_root=data_root,
        profile_resolver=resolver,
        adapter=FakeModelAdapter([_plan_response()]),
    )
    planned = planner.continue_run("run_1", stop_at="plan")
    assert planned.status == OrchestrationState.PLAN_READY

    coder_adapter = FakeModelAdapter([_code_response()])
    coder = OrchestrationController(
        run_store=store,
        artifact_store=artifacts,
        data_root=data_root,
        profile_resolver=resolver,
        adapter=coder_adapter,
    )
    proposed = coder.continue_run("run_1", stop_at="action-proposed")
    assert proposed.status == OrchestrationState.ACTION_PROPOSED
    assert len(coder_adapter.calls) == 1


def test_evidence_backed_replan_survives_controller_restart(tmp_path: Path) -> None:
    class SimulatedProcessLoss(BaseException):
        pass

    data_root, _, store, artifacts = _prepared_run(tmp_path)
    resolver = ModelProfileResolver(_profile_file(tmp_path))
    first_adapter = _EvidenceAwareReplanAdapter(store)
    first = OrchestrationController(
        run_store=store,
        artifact_store=artifacts,
        data_root=data_root,
        profile_resolver=resolver,
        adapter=first_adapter,
    )
    transition = first.lifecycle.transition

    def crash_after_replan_transition(*args, **kwargs):
        result = transition(*args, **kwargs)
        if kwargs.get("event_type") == "REPLANNING_STARTED":
            raise SimulatedProcessLoss("simulated process loss after durable replan event")
        return result

    first.lifecycle.transition = crash_after_replan_transition
    with pytest.raises(SimulatedProcessLoss, match="simulated process loss"):
        first.continue_run("run_1")
    assert first.lifecycle.get("run_1").state == OrchestrationState.PLANNING

    resumed_adapter = FakeModelAdapter([_plan_response(2), _code_response(2)])
    resumed = OrchestrationController(
        run_store=store,
        artifact_store=artifacts,
        data_root=data_root,
        profile_resolver=resolver,
        adapter=resumed_adapter,
    )
    result = resumed.continue_run("run_1")
    assert result.status == OrchestrationState.ACTION_PROPOSED
    with store.get_connection() as conn:
        revisions = [
            row[0]
            for row in conn.execute(
                "SELECT plan_revision FROM h_plans ORDER BY plan_revision"
            )
        ]
        replan_events = conn.execute(
            "SELECT COUNT(*) FROM h_events WHERE event_type = 'REPLAN_REQUESTED'"
        ).fetchone()[0]
    assert revisions == [1, 2]
    assert replan_events == 1
    assert len(resumed_adapter.calls) == 2


def test_cancellation_settles_active_call_and_releases_lease(tmp_path: Path) -> None:
    data_root, _, store, artifacts = _prepared_run(tmp_path)
    adapter = _BlockingAdapter()
    controller = OrchestrationController(
        run_store=store,
        artifact_store=artifacts,
        data_root=data_root,
        profile_resolver=ModelProfileResolver(_profile_file(tmp_path)),
        adapter=adapter,
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(controller.continue_run, "run_1")
        assert adapter.started.wait(timeout=5)
        cancelled = controller.cancel("run_1")
        assert cancelled.state == OrchestrationState.CANCELLED
        with pytest.raises(ModelAdapterError, match="cancelled"):
            future.result(timeout=5)

    with store.get_connection() as conn:
        call = conn.execute(
            "SELECT state, error_code FROM h_model_calls ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        active_leases = conn.execute(
            "SELECT COUNT(*) FROM h_orchestration_leases WHERE run_id = 'run_1'"
        ).fetchone()[0]
    assert dict(call) == {"state": "UNKNOWN", "error_code": "MODEL_CANCELLED"}
    assert active_leases == 0


def test_keyboard_interrupt_is_durably_cancelled(tmp_path: Path) -> None:
    data_root, _, store, artifacts = _prepared_run(tmp_path)
    adapter = _InterruptingAdapter()
    controller = OrchestrationController(
        run_store=store,
        artifact_store=artifacts,
        data_root=data_root,
        profile_resolver=ModelProfileResolver(_profile_file(tmp_path)),
        adapter=adapter,
    )
    with pytest.raises(KeyboardInterrupt, match="simulated Ctrl-C"):
        controller.continue_run("run_1")
    assert controller.lifecycle.get("run_1").state == OrchestrationState.CANCELLED
    with store.get_connection() as conn:
        call = conn.execute(
            "SELECT state, error_code FROM h_model_calls ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        active_leases = conn.execute(
            "SELECT COUNT(*) FROM h_orchestration_leases WHERE run_id = 'run_1'"
        ).fetchone()[0]
    assert dict(call) == {"state": "UNKNOWN", "error_code": "CANCELLED_BY_USER"}
    assert active_leases == 0


def test_format_errors_are_bounded_and_accounted(tmp_path: Path) -> None:
    data_root, _, store, artifacts = _prepared_run(tmp_path)
    adapter = FakeModelAdapter(["not json", '{"decision":"unknown"}', _plan_response(), _code_response()])
    controller = OrchestrationController(
        run_store=store,
        artifact_store=artifacts,
        data_root=data_root,
        profile_resolver=ModelProfileResolver(_profile_file(tmp_path)),
        adapter=adapter,
    )
    result = controller.continue_run("run_1")
    assert result.status == OrchestrationState.ACTION_PROPOSED
    assert len(adapter.calls) == 4
    with store.get_connection() as conn:
        states = [row[0] for row in conn.execute("SELECT state FROM h_model_calls ORDER BY created_at")]
        errors = conn.execute(
            "SELECT COUNT(*) FROM h_artifacts WHERE kind = 'role_schema_error'"
        ).fetchone()[0]
    assert states == ["FAILED", "FAILED", "SUCCEEDED", "SUCCEEDED"]
    assert errors == 2


def test_crash_after_response_artifact_replays_without_model_call(tmp_path: Path) -> None:
    data_root, _, store, artifacts = _prepared_run(tmp_path)
    resolver = ModelProfileResolver(_profile_file(tmp_path))
    first = OrchestrationController(
        run_store=store,
        artifact_store=artifacts,
        data_root=data_root,
        profile_resolver=resolver,
        adapter=FakeModelAdapter([_plan_response()]),
    )
    first.continue_run("run_1", stop_at="plan")
    snapshot = first.lifecycle.get("run_1")
    snapshot = first.lifecycle.transition(
        "run_1",
        snapshot.state,
        snapshot.version,
        OrchestrationState.CODING,
        event_type="CODING_STARTED",
    )
    profile = resolver.resolve("designated")
    plan = first.records.get_active_plan("task_1")
    built = first.context_builder.build(
        run_id="run_1",
        task_id="task_1",
        role=Role.CODER,
        purpose="coder-plan-1-format-0",
        profile=profile.contract,
        source_revision="B",
        plan=plan.plan,
        remaining_budget=first.budgets.get("run_1"),
    )
    call_id = "mcall_crash_fixture"
    first.budgets.reserve(
        run_id="run_1",
        task_id="task_1",
        packet_id=built.contract.packet_id,
        call_id=call_id,
        role=Role.CODER,
        attempt_no=1,
        profile_fingerprint=profile.contract.profile_fingerprint,
        request_sha256="a" * 64,
        input_tokens=built.contract.budget.estimated_input_tokens,
        output_tokens=profile.contract.max_output_tokens,
    )
    first.budgets.mark_in_flight(call_id)
    artifacts.write_json(
        "run_1",
        f"prd2/model/responses/{call_id}.json",
        {
            "schema_version": "1.0",
            "call_id": call_id,
            "role": "coder",
            "raw_text": _code_response(),
            "filtering": "secret_and_control_character_filter_v1",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "usage_source": "provider_reported",
            },
            "provider_request_id": "fixture",
            "latency_ms": 1,
        },
        "model_response",
        "task_1",
    )

    unused_adapter = FakeModelAdapter([])
    resumed = OrchestrationController(
        run_store=store,
        artifact_store=artifacts,
        data_root=data_root,
        profile_resolver=resolver,
        adapter=unused_adapter,
    )
    result = resumed.continue_run("run_1")
    assert result.status == OrchestrationState.ACTION_PROPOSED
    assert unused_adapter.calls == []
    with store.get_connection() as conn:
        assert conn.execute(
            "SELECT state FROM h_model_calls WHERE call_id = ?", (call_id,)
        ).fetchone()[0] == "SUCCEEDED"


def _evidence_request(path: str) -> str:
    return json.dumps({"schema_version": "1.0", "decision": "NEEDS_EVIDENCE", "task_id": "task_1",
                       "queries": [{"query_type": "PATH_GLOB", "query": path}], "reason": "Read the file."})


def _history_text(request: ModelCallRequest) -> str:
    return "\n".join(message["content"] for message in request.messages)


def test_exhausted_evidence_rounds_get_one_final_planning_call(tmp_path: Path) -> None:
    """PRD 2 caps initial evidence rounds at three: the planner is told, then must plan."""
    data_root, _, store, artifacts = _prepared_run(tmp_path)
    adapter = FakeModelAdapter([_evidence_request("src/example.py"), _evidence_request("tests/test_example.py"),
                                _evidence_request("src/*.py"), _plan_response()])
    controller = OrchestrationController(run_store=store, artifact_store=artifacts, data_root=data_root,
                                         profile_resolver=ModelProfileResolver(_profile_file(tmp_path)), adapter=adapter)
    result = controller.continue_run("run_1", stop_at="plan")
    assert result.status.value == "PLAN_READY" and len(adapter.calls) == 4
    assert "EVIDENCE_ROUNDS_EXHAUSTED" in _history_text(adapter.calls[3])
    assert "EVIDENCE_ROUNDS_EXHAUSTED" not in _history_text(adapter.calls[2])


def test_a_fourth_evidence_request_still_stops_at_the_prd2_boundary(tmp_path: Path) -> None:
    data_root, _, store, artifacts = _prepared_run(tmp_path)
    adapter = FakeModelAdapter([_evidence_request("src/example.py")] * 5)
    controller = OrchestrationController(run_store=store, artifact_store=artifacts, data_root=data_root,
                                         profile_resolver=ModelProfileResolver(_profile_file(tmp_path)), adapter=adapter)
    with pytest.raises(Exception, match="more than three initial evidence rounds"):
        controller.continue_run("run_1", stop_at="plan")
    assert len(adapter.calls) == 4  # three admitted rounds, then the refused fourth request


def test_wrong_plan_identity_gets_bounded_repair_feedback(tmp_path: Path) -> None:
    data_root, _, store, artifacts = _prepared_run(tmp_path)
    adapter = FakeModelAdapter([_plan_response(revision=7), _plan_response(revision=1)])
    controller = OrchestrationController(run_store=store, artifact_store=artifacts, data_root=data_root,
                                         profile_resolver=ModelProfileResolver(_profile_file(tmp_path)), adapter=adapter)
    result = controller.continue_run("run_1", stop_at="plan")
    assert result.status.value == "PLAN_READY" and len(adapter.calls) == 2
    assert "plan_revision=1" in _history_text(adapter.calls[1]) and "Expected plan revision 1, got 7" in _history_text(adapter.calls[1])
    with store.get_connection() as conn:
        assert [row[0] for row in conn.execute("SELECT plan_revision FROM h_plans")] == [1]


def test_repeated_wrong_plan_identity_keeps_the_typed_error(tmp_path: Path) -> None:
    data_root, _, store, artifacts = _prepared_run(tmp_path)
    adapter = FakeModelAdapter([_plan_response(revision=5)] * 4)
    controller = OrchestrationController(run_store=store, artifact_store=artifacts, data_root=data_root,
                                         profile_resolver=ModelProfileResolver(_profile_file(tmp_path)), adapter=adapter)
    with pytest.raises(Exception, match="Expected plan revision 1, got 5"):
        controller.continue_run("run_1", stop_at="plan")
    assert len(adapter.calls) == 3  # the original plus two repairs


def test_a_tiny_rate_cap_still_sends_the_pinned_minimum(tmp_path: Path, monkeypatch) -> None:
    """Groq run regression: a fitted budget below the pinned context must shrink to it, not stop the run."""
    monkeypatch.setenv("HARNESS_MODEL_TPM_LIMIT", "1000")  # fits only the 1024-token floor
    data_root, _, store, artifacts = _prepared_run(tmp_path)
    adapter = FakeModelAdapter([_plan_response()])
    controller = OrchestrationController(run_store=store, artifact_store=artifacts, data_root=data_root,
                                         profile_resolver=ModelProfileResolver(_profile_file(tmp_path)), adapter=adapter)
    result = controller.continue_run("run_1", stop_at="plan")
    assert result.status.value == "PLAN_READY" and len(adapter.calls) == 1
    assert adapter.calls[0].max_output_tokens <= 1024


def test_rounds_exhausted_instruction_survives_a_tiny_rate_cap(tmp_path: Path, monkeypatch) -> None:
    """todo-app regression: under a 7K tokens/minute cap, compaction dropped the host's 'plan now'
    feedback, so the planner kept asking and hit the round limit. Host feedback is pinned."""
    monkeypatch.setenv("HARNESS_MODEL_TPM_LIMIT", "1500")
    data_root, _, store, artifacts = _prepared_run(tmp_path)
    adapter = FakeModelAdapter([_evidence_request("src/example.py"), _evidence_request("tests/test_example.py"),
                                _evidence_request("src/*.py"), _plan_response()])
    controller = OrchestrationController(run_store=store, artifact_store=artifacts, data_root=data_root,
                                         profile_resolver=ModelProfileResolver(_profile_file(tmp_path)), adapter=adapter)
    result = controller.continue_run("run_1", stop_at="plan")
    assert result.status.value == "PLAN_READY" and len(adapter.calls) == 4
    assert "EVIDENCE_ROUNDS_EXHAUSTED" in _history_text(adapter.calls[3])
