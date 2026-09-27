"""The reviewed built-in plugin catalog: the only plugins a release profile can pin.

``scripts/generate_plugin_lock.py`` turns this catalog plus the exact module bytes
into manifests, configuration schemas, configurations, and the pinned lock under
``config/plugins``. Editing a plugin module without regenerating the lock makes
startup fail closed with a content-hash mismatch.
"""
from __future__ import annotations

from typing import Any, Dict, List

BUILTIN_SET_ID = "pset_builtin_release_v1"

_OBJECT = {"type": "object", "additionalProperties": False}

CATALOG: List[Dict[str, Any]] = [
    {
        "slot": "evaluator", "name": "builtin.native-json-evaluator", "version": "1.0.0",
        "module": "harness.evaluator.native_json", "entry_point": "NativeJsonEvaluatorAdapter",
        "interface": "EvaluatorAdapterPlugin", "capabilities": ["READ_RELEASE_HANDOFF", "WRITE_BOUNDED_RESULT"],
        "config_schema": {**_OBJECT, "properties": {"max_request_bytes": {"type": "integer", "minimum": 1024, "maximum": 4194304}},
                          "required": ["max_request_bytes"]},
        "config": {"max_request_bytes": 1048576}, "self_check": "native_json_contract_v1", "dependencies": [],
    },
    {
        "slot": "controller", "name": "builtin.safe-controller", "version": "1.0.0",
        "module": "harness.orchestration.controller", "entry_point": "OrchestrationController",
        "interface": "ControllerPlugin", "capabilities": ["READ_STATE_VIEW", "MODEL_CALL_SERVICE", "ADMITTED_ACTION_EXECUTION"],
        "config_schema": {**_OBJECT, "properties": {"max_coder_turns_per_task": {"type": "integer", "minimum": 1, "maximum": 200}},
                          "required": ["max_coder_turns_per_task"]},
        "config": {"max_coder_turns_per_task": 30}, "self_check": "controller_transition_table_v1",
        "dependencies": [{"name": "builtin.openai-compatible-model", "version_constraint": ">=1.0.0,<2.0.0"},
                         {"name": "builtin.docker-environment", "version_constraint": ">=1.0.0,<2.0.0"}],
    },
    {
        "slot": "model", "name": "builtin.openai-compatible-model", "version": "1.0.0",
        "module": "harness.model.adapter", "entry_point": "OpenAICompatibleModelAdapter",
        "interface": "ModelAdapterPlugin", "capabilities": ["MODEL_CALL_SERVICE"],
        "config_schema": {**_OBJECT, "properties": {"credential_env": {"type": "string", "enum": ["AI_API_KEY"]}},
                          "required": ["credential_env"]},
        "config": {"credential_env": "AI_API_KEY"}, "self_check": "one_model_one_key_v1", "dependencies": [],
    },
    {
        "slot": "context", "name": "builtin.context-builder", "version": "1.0.0",
        "module": "harness.context.builder", "entry_point": "ContextBuilder",
        "interface": "ContextBuilderPlugin", "capabilities": ["CONTEXT_PACKET", "EVIDENCE_QUERY"],
        "config_schema": {**_OBJECT, "properties": {}}, "config": {}, "self_check": "import_only_v1", "dependencies": [],
    },
    {
        "slot": "retriever", "name": "builtin.tree-sitter-retriever", "version": "1.0.0",
        "module": "harness.retrieval.retriever", "entry_point": "Retriever",
        "interface": "RetrieverPlugin", "capabilities": ["EVIDENCE_QUERY"],
        "config_schema": {**_OBJECT, "properties": {}}, "config": {}, "self_check": "import_only_v1", "dependencies": [],
    },
    {
        "slot": "repository", "name": "builtin.git-repository", "version": "1.0.0",
        "module": "harness.repository.repository_service", "entry_point": "RepositoryService",
        "interface": "RepositoryPlugin", "capabilities": ["REPOSITORY_SNAPSHOT"],
        "config_schema": {**_OBJECT, "properties": {}}, "config": {}, "self_check": "import_only_v1", "dependencies": [],
    },
    {
        "slot": "environment", "name": "builtin.docker-environment", "version": "1.0.0",
        "module": "harness.sandbox.docker_backend", "entry_point": "DockerBackend",
        "interface": "EnvironmentPlugin", "capabilities": ["ADMITTED_ACTION_EXECUTION"],
        "config_schema": {**_OBJECT, "properties": {"network_default": {"type": "string", "enum": ["none"]}},
                          "required": ["network_default"]},
        "config": {"network_default": "none"}, "self_check": "no_host_fallback_v1", "dependencies": [],
    },
    {
        "slot": "verifier", "name": "builtin.pytest-verifier", "version": "1.0.0",
        "module": "harness.verification.service", "entry_point": "VerificationService",
        "interface": "VerifierPlugin", "capabilities": ["VERIFICATION_OBSERVATION"],
        "config_schema": {**_OBJECT, "properties": {"validator_required": {"type": "boolean", "enum": [True]}},
                          "required": ["validator_required"]},
        "config": {"validator_required": True}, "self_check": "completion_gate_false_pass_v1",
        "dependencies": [{"name": "builtin.docker-environment", "version_constraint": ">=1.0.0,<2.0.0"}],
    },
    {
        "slot": "exporter", "name": "builtin.unified-git-patch-exporter", "version": "1.0.0",
        "module": "harness.export.patch_adapter", "entry_point": "PatchAdapter",
        "interface": "ExporterPlugin", "capabilities": ["EXPORT_BUNDLE_WRITE"],
        "config_schema": {**_OBJECT, "properties": {"format": {"type": "string", "enum": ["unified_git_patch_v1"]}},
                          "required": ["format"]},
        "config": {"format": "unified_git_patch_v1"}, "self_check": "patch_round_trip_v1", "dependencies": [],
    },
    {
        "slot": "report_renderer", "name": "builtin.markdown-report", "version": "1.0.0",
        "module": "harness.release.report_builder", "entry_point": "MarkdownReportRenderer",
        "interface": "ReportRendererPlugin", "capabilities": ["READ_STATE_VIEW"],
        "config_schema": {**_OBJECT, "properties": {}}, "config": {}, "self_check": "import_only_v1", "dependencies": [],
    },
]
