"""Provider compatibility for the evaluators' models (DeepSeek, Qwen, local vLLM/Ollama).

Everything runs through the real ``OpenAICompatibleModelAdapter`` with an HTTP
mock that reproduces the providers' documented quirks.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from harness.contracts import Role
from harness.model import ModelProfileResolver
from harness.model.adapter import ModelAdapterError, ModelCallRequest, OpenAICompatibleModelAdapter
from harness.model.credentials import CredentialProvider
from harness.model.probe import probe_model
from harness.roles.schemas import RoleSchemaError, RoleSchemaRegistry, extract_json_text, repair_triple_quoted_strings

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ROOT / "config" / "model_profiles.toml"
KEY = "sk-test-provider-key-0000000000"


def adapter_for(profile: str, handler, **kwargs) -> OpenAICompatibleModelAdapter:
    resolved = ModelProfileResolver(PROFILES, environ={}).resolve(profile)
    return OpenAICompatibleModelAdapter(
        resolved, CredentialProvider(environ={"AI_API_KEY": KEY}),
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None, **kwargs,
    )


def request() -> ModelCallRequest:
    return ModelCallRequest(call_id="c1", role=Role.PLANNER,
                            messages=[{"role": "system", "content": "Return one JSON object."}, {"role": "user", "content": "go"}],
                            response_schema={"type": "object"}, max_output_tokens=500)


def reply(content, *, finish="stop", usage=True, status=200, headers=None):
    body = {"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": finish}]}
    if usage:
        body["usage"] = {"prompt_tokens": 11, "completion_tokens": 7}
    return httpx.Response(status, json=body, headers=headers or {})


@pytest.mark.parametrize("profile", ["deepseek", "qwen", "qwen-coder", "qwen-local", "openrouter-deepseek", "deepseek-reasoner", "groq-qwen"])
def test_bundled_profiles_send_json_object_mode_and_no_secret(profile: str) -> None:
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        seen["auth"] = req.headers["authorization"]
        return reply('{"ok": true}')

    adapter_for(profile, handler).generate(request())
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert seen["auth"] == f"Bearer {KEY}"
    assert KEY not in json.dumps(seen["body"])


def test_qwen_profile_disables_thinking_for_non_streaming_calls() -> None:
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(json.loads(req.content))
        return reply('{"ok": true}')

    adapter_for("qwen", handler).generate(request())
    assert seen["enable_thinking"] is False and seen["stream"] is False


def test_json_schema_rejection_is_diagnosed_not_retried() -> None:
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, json={"error": {"message": "This response_format type is unavailable now"}})

    resolver = ModelProfileResolver(PROFILES, environ={"HARNESS_MODEL_RESPONSE_FORMAT": "json_schema"})
    resolved = resolver.resolve("deepseek")
    adapter = OpenAICompatibleModelAdapter(resolved, CredentialProvider(environ={"AI_API_KEY": KEY}),
                                           client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)
    with pytest.raises(ModelAdapterError) as raised:
        adapter.generate(request())
    assert raised.value.code == "MODEL_RESPONSE_FORMAT_UNSUPPORTED" and len(calls) == 1


def test_rate_limit_honors_retry_after_then_succeeds() -> None:
    waits, calls = [], []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, json={"error": {"message": "slow down"}}, headers={"Retry-After": "2"})
        return reply('{"ok": true}')

    resolved = ModelProfileResolver(PROFILES, environ={}).resolve("deepseek")
    adapter = OpenAICompatibleModelAdapter(resolved, CredentialProvider(environ={"AI_API_KEY": KEY}),
                                           client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
                                           sleep=waits.append)
    assert adapter.generate(request()).raw_text == '{"ok": true}'
    assert len(calls) == 3 and all(2.0 <= w <= 2.3 for w in waits)


def test_reasoning_model_null_content_and_truncation_are_surfaced() -> None:
    response = adapter_for("deepseek-reasoner", lambda req: reply(None, finish="length")).generate(request())
    assert response.raw_text == "" and response.finish_reason == "length"


def test_missing_usage_is_labeled_unknown() -> None:
    response = adapter_for("qwen-local", lambda req: reply('{"ok": true}', usage=False)).generate(request())
    assert response.usage_source == "unknown"


def test_context_overflow_is_typed() -> None:
    handler = lambda req: httpx.Response(400, json={"error": {"message": "maximum context length is 65536 tokens"}})  # noqa: E731
    with pytest.raises(ModelAdapterError) as raised:
        adapter_for("deepseek", handler).generate(request())
    assert raised.value.code == "MODEL_CONTEXT_OVERFLOW"


# ------------------------------------------------------------ parsing quirks
def test_allowed_transport_wrappers_are_stripped() -> None:
    assert extract_json_text('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert extract_json_text('<think>long hidden {reasoning}</think>\n{"a": 2}') == '{"a": 2}'
    assert extract_json_text('```\n{"a": 3}\n```\n') == '{"a": 3}'


def test_prose_is_not_a_transport_wrapper() -> None:
    with pytest.raises(RoleSchemaError, match="strict JSON"):
        RoleSchemaRegistry().validate(Role.PLANNER, 'Sure! Here is the plan: {"decision": "NEEDS_INPUT"}')


def test_python_triple_quoted_action_is_repaired_to_json_string() -> None:
    raw = '{"python_action": """\nprint("hi")\n""", "x": 1}'
    assert json.loads(repair_triple_quoted_strings(raw)) == {"python_action": '\nprint("hi")\n', "x": 1}


# -------------------------------------------------------------- profiles
def test_loopback_http_is_allowed_but_remote_http_is_not(tmp_path: Path) -> None:
    config = tmp_path / "p.toml"
    base = PROFILES.read_text()
    config.write_text(base)
    resolver = ModelProfileResolver(config, environ={"HARNESS_MODEL_ENDPOINT": "http://127.0.0.1:9000/v1"})
    assert resolver.resolve("qwen-local").endpoint_url == "http://127.0.0.1:9000/v1"
    bad = ModelProfileResolver(config, environ={"HARNESS_MODEL_ENDPOINT": "http://evil.example/v1"})
    with pytest.raises(Exception):
        bad.resolve("qwen-local")


def test_environment_overrides_change_the_fingerprint() -> None:
    plain = ModelProfileResolver(PROFILES, environ={}).resolve("deepseek")
    renamed = ModelProfileResolver(PROFILES, environ={"HARNESS_MODEL_NAME": "deepseek-v3.2"}).resolve("deepseek")
    assert renamed.contract.model == "deepseek-v3.2"
    assert renamed.contract.profile_fingerprint != plain.contract.profile_fingerprint


def test_unknown_extra_body_keys_are_rejected(tmp_path: Path) -> None:
    config = tmp_path / "p.toml"
    config.write_text(PROFILES.read_text().replace("extra_body = { enable_thinking = false }", 'extra_body = { base_url = "http://x" }', 1))
    with pytest.raises(Exception, match="extra_body"):
        ModelProfileResolver(config, environ={}).resolve("qwen")


def test_live_probe_reports_wrapped_json() -> None:
    resolved = ModelProfileResolver(PROFILES, environ={}).resolve("deepseek")
    adapter = OpenAICompatibleModelAdapter(resolved, CredentialProvider(environ={"AI_API_KEY": KEY}),
                                           client_factory=lambda: httpx.Client(transport=httpx.MockTransport(
                                               lambda req: reply('```json\n{"ok": true, "sum": 42}\n```'))), sleep=lambda s: None)
    probe = probe_model(resolved, adapter)
    assert probe["status"] == "PASS" and probe["json_wrapped_in_prose_or_fence"] and probe["usage_reported"]


# ------------------------------------------------------------------ Groq (Qwen on GroqCloud)
def test_groq_profile_targets_groq_qwen_without_thinking() -> None:
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen.update(json.loads(req.content))
        return reply('{"ok": true}')

    adapter_for("groq-qwen", handler).generate(request())
    assert seen["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert seen["model"].startswith("qwen/") and seen["reasoning_effort"] == "none"
    assert seen["response_format"] == {"type": "json_object"} and "enable_thinking" not in seen


def test_groq_json_validate_failed_becomes_a_repairable_schema_failure() -> None:
    """Groq answers invalid JSON-mode output with HTTP 400 + failed_generation: that text goes to the schema
    validator (bounded repair round), it is not a fatal request rejection."""
    rejected = 'Here is the plan: {"decision": "PLAN_READY",'

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "Failed to generate JSON. Please adjust your prompt.",
                                                   "type": "invalid_request_error", "code": "json_validate_failed",
                                                   "failed_generation": rejected + " " + KEY}})

    result = adapter_for("groq-qwen", handler).generate(request())
    assert result.raw_text.startswith(rejected) and KEY not in result.raw_text
    assert result.usage_source == "estimated" and result.input_tokens > 0 and result.output_tokens > 0


def test_a_request_larger_than_the_tpm_cap_is_diagnosed_not_retried() -> None:
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(413, json={"error": {"type": "tokens", "code": "rate_limit_exceeded", "message": (
            "Request too large for model `qwen/qwen3.8-27b` in organization `org_x` service tier `on_demand` on tokens "
            "per minute (TPM): Limit 8000, Requested 21507, please reduce your message size and try again.")}})

    with pytest.raises(ModelAdapterError) as caught:
        adapter_for("groq-qwen", handler).generate(request())
    assert caught.value.code == "MODEL_QUOTA_TPM_TOO_LOW" and len(calls) == 1
    assert "8000 tokens per minute" in str(caught.value) and "21507" in str(caught.value)


def test_an_ordinary_tpm_rate_limit_is_still_retried() -> None:
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "1"}, json={"error": {"message": (
                "Rate limit reached for model `qwen/qwen3.8-27b` on tokens per minute (TPM): Limit 250000, "
                "Used 249000, Requested 2000. Please try again in 1s.")}})
        return reply('{"ok": true}')

    assert adapter_for("groq-qwen", handler).generate(request()).raw_text == '{"ok": true}' and len(calls) == 2


def test_learned_tpm_cap_shrinks_requests_to_fit_and_paces_calls() -> None:
    """Groq free tier: the 413 teaches the cap; the controller refits; later calls wait for the window."""
    from harness.model import adapter as adapter_module
    from harness.orchestration.controller import OrchestrationController

    def too_large(req: httpx.Request) -> httpx.Response:
        return httpx.Response(413, json={"error": {"message": (
            "Request too large for model `qwen/qwen3.8-27b` on tokens per minute (TPM): Limit 7000, Requested 21373, "
            "please reduce your message size and try again.")}})

    with pytest.raises(ModelAdapterError) as caught:
        adapter_for("groq-qwen", too_large).generate(request())
    assert caught.value.code == "MODEL_QUOTA_TPM_TOO_LOW" and caught.value.tpm_limit == 7000
    contract = ModelProfileResolver(PROFILES, environ={}).resolve("groq-qwen").contract
    fitted = OrchestrationController._fitted_contract(contract)
    assert fitted.max_output_tokens < contract.max_output_tokens
    assert fitted.context_window_tokens <= 7000 and fitted.profile_fingerprint == contract.profile_fingerprint
    # Pacing: two full-size calls inside one minute must wait for the window instead of drawing a 429.
    waits = []
    ok = adapter_for("groq-qwen", lambda req: reply('{"ok": true}'))
    ok._sleep = waits.append
    big = ModelCallRequest(call_id="c2", role=Role.PLANNER, messages=[{"role": "user", "content": "x" * 12000}],
                           response_schema={"type": "object"}, max_output_tokens=1400)
    ok.generate(big)
    ok.generate(big)
    assert waits and all(0 < w <= adapter_module.RATE_LIMIT_MAX_WAIT_SECONDS for w in waits)


def test_rate_limits_are_waited_out_beyond_the_normal_retry_count() -> None:
    calls, waits = [], []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 7:
            return httpx.Response(429, headers={"retry-after": "5"}, json={"error": {"message": "Rate limit reached"}})
        return reply('{"ok": true}')

    adapter = adapter_for("deepseek", handler)
    adapter._sleep = waits.append
    assert adapter.generate(request()).raw_text == '{"ok": true}' and len(calls) == 7 and len(waits) == 6


def test_prompt_schema_drops_generated_titles_but_keeps_real_fields() -> None:
    from harness.context.builder import _prompt_schema

    schema = {"title": "PlanV1", "type": "object", "properties": {"title": {"title": "Title", "type": "string"},
              "steps": {"title": "Steps", "type": "array", "items": {"$ref": "#/$defs/Step"}}},
              "$defs": {"Step": {"title": "Step", "type": "object", "properties": {"purpose": {"title": "Purpose"}}}}}
    compact = _prompt_schema(schema)
    assert "title" in compact["properties"] and compact["properties"]["title"] == {"type": "string"}
    assert "title" not in compact and "title" not in compact["$defs"]["Step"]
    assert json.dumps(compact).count('"title"') == 1


def test_raw_newlines_inside_json_strings_are_accepted_not_retried() -> None:
    """Live finding: models put real newlines inside python_action; that used to cost a full retry."""
    registry = RoleSchemaRegistry()
    raw = ('{"schema_version":"1.0","decision":"NEEDS_EVIDENCE","task_id":"t1","queries":[{"query_type":"PATH_GLOB",'
           '"query":"src/a.py"}],"reason":"line one\nline two"}')
    assert registry.validate(Role.PLANNER, raw).reason == "line one\nline two"
    with pytest.raises(RoleSchemaError):
        registry.validate(Role.PLANNER, '{"decision": "NEEDS_EVIDENCE", "reason": "unterminated')
