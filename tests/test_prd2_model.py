from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from harness.contracts import Role
from harness.model import (
    CredentialProvider,
    FakeModelAdapter,
    ModelAuthMissingError,
    ModelAdapterError,
    ModelCallRequest,
    ModelProfileError,
    ModelProfileResolver,
    OpenAICompatibleModelAdapter,
    TokenCounter,
)
from harness.roles import RoleSchemaError, RoleSchemaRegistry


def _write_profile(tmp_path: Path, *, endpoint: str = "https://models.example/v1", model: str = "m1") -> Path:
    path = tmp_path / "profiles.toml"
    path.write_text(
        f"""
[profiles.designated]
protocol = "openai-compatible-chat"
adapter = "openai-compatible"
endpoint = "{endpoint}"
model = "{model}"
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


def test_profile_fingerprint_is_deterministic_and_secret_free(tmp_path: Path) -> None:
    resolver = ModelProfileResolver(_write_profile(tmp_path))
    one = resolver.resolve("designated")
    two = resolver.resolve("designated")
    assert one.contract.profile_fingerprint == two.contract.profile_fingerprint
    assert one.contract.credential_env == "AI_API_KEY"
    assert "secret" not in one.contract.model_dump_json()
    resolver.validate_live(one)


def test_profile_rejects_placeholder_and_credential_url(tmp_path: Path) -> None:
    placeholder = ModelProfileResolver(
        _write_profile(tmp_path, endpoint="https://provider.example/v1", model="official-model-id")
    ).resolve("designated")
    with pytest.raises(ModelProfileError) as caught:
        ModelProfileResolver(_write_profile(tmp_path)).validate_live(placeholder)
    assert caught.value.code == "MODEL_PROFILE_INCOMPLETE"
    with pytest.raises(ModelProfileError):
        ModelProfileResolver(
            _write_profile(tmp_path, endpoint="https://user:password@models.example/v1")
        ).resolve("designated")


def test_missing_key_stops_before_http(tmp_path: Path) -> None:
    resolved = ModelProfileResolver(_write_profile(tmp_path)).resolve("designated")
    opened = False

    def factory() -> httpx.Client:
        nonlocal opened
        opened = True
        return httpx.Client()

    adapter = OpenAICompatibleModelAdapter(
        resolved,
        CredentialProvider({}),
        client_factory=factory,
    )
    with pytest.raises(ModelAuthMissingError):
        adapter.generate(
            ModelCallRequest(
                call_id="c1",
                role=Role.PLANNER,
                messages=[{"role": "user", "content": "task"}],
                response_schema={"type": "object"},
                max_output_tokens=100,
            )
        )
    assert opened is False


def test_live_adapter_parses_bounded_provider_response(tmp_path: Path) -> None:
    resolved = ModelProfileResolver(_write_profile(tmp_path)).resolve("designated")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-key"
        payload = json.loads(request.content)
        assert payload["model"] == "m1"
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"decision":"NEEDS_INPUT","task_id":"t","questions":[{"question":"q","impact":"i"}]}'}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 8},
            },
            headers={"x-request-id": "safe-id"},
        )

    adapter = OpenAICompatibleModelAdapter(
        resolved,
        CredentialProvider({"AI_API_KEY": "test-key"}),
        max_attempts=1,
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = adapter.generate(
        ModelCallRequest(
            call_id="c1",
            role=Role.PLANNER,
            messages=[{"role": "user", "content": "task"}],
            response_schema={"type": "object"},
            max_output_tokens=100,
        )
    )
    assert result.input_tokens == 12
    assert result.output_tokens == 8
    assert result.provider_request_id == "safe-id"


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [(302, "MODEL_REDIRECT_REJECTED"), (401, "MODEL_AUTH_INVALID")],
)
def test_live_adapter_fails_closed_without_fallback(
    tmp_path: Path, status: int, expected_code: str
) -> None:
    resolved = ModelProfileResolver(_write_profile(tmp_path)).resolve("designated")
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(status, headers={"location": "https://other.example"})

    adapter = OpenAICompatibleModelAdapter(
        resolved,
        CredentialProvider({"AI_API_KEY": "test-key"}),
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ModelAdapterError) as caught:
        adapter.generate(
            ModelCallRequest(
                call_id="closed-1",
                role=Role.PLANNER,
                messages=[],
                response_schema={"type": "object"},
                max_output_tokens=10,
            )
        )
    assert caught.value.code == expected_code
    assert requests == 1


def test_live_adapter_rejects_oversized_response(tmp_path: Path) -> None:
    resolved = ModelProfileResolver(_write_profile(tmp_path)).resolve("designated")
    adapter = OpenAICompatibleModelAdapter(
        resolved,
        CredentialProvider({"AI_API_KEY": "test-key"}),
        max_response_bytes=32,
        max_attempts=1,
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=b"x" * 33)
            )
        ),
    )
    with pytest.raises(ModelAdapterError) as caught:
        adapter.generate(
            ModelCallRequest(
                call_id="large-1",
                role=Role.PLANNER,
                messages=[],
                response_schema={"type": "object"},
                max_output_tokens=10,
            )
        )
    assert caught.value.code == "MODEL_RESPONSE_TOO_LARGE"


def test_live_adapter_redacts_echoed_credential_and_request_id(tmp_path: Path) -> None:
    resolved = ModelProfileResolver(_write_profile(tmp_path)).resolve("designated")
    secret = "opaque-test-credential"
    adapter = OpenAICompatibleModelAdapter(
        resolved,
        CredentialProvider({"AI_API_KEY": secret}),
        max_attempts=1,
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "choices": [{"message": {"content": f'{{"echo":"{secret}"}}'}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                    },
                    headers={"x-request-id": f"trace-{secret}"},
                )
            )
        ),
    )
    result = adapter.generate(
        ModelCallRequest(
            call_id="echo-1",
            role=Role.PLANNER,
            messages=[],
            response_schema={"type": "object"},
            max_output_tokens=10,
        )
    )
    assert secret not in result.raw_text
    assert result.provider_request_id is None


def test_fake_adapter_has_no_key_or_network_dependency() -> None:
    adapter = FakeModelAdapter(['{"decision":"x"}'])
    result = adapter.generate(
        ModelCallRequest(
            call_id="fake-1",
            role=Role.CODER,
            messages=[],
            response_schema={},
            max_output_tokens=10,
        )
    )
    assert result.raw_text == '{"decision":"x"}'
    assert len(adapter.calls) == 1


def test_role_parser_rejects_duplicate_keys_and_prose() -> None:
    registry = RoleSchemaRegistry()
    with pytest.raises(RoleSchemaError, match="Duplicate JSON key"):
        registry.validate(
            Role.PLANNER,
            '{"decision":"NEEDS_INPUT","decision":"PLAN_READY","task_id":"t"}',
        )
    with pytest.raises(RoleSchemaError, match="strict JSON"):
        registry.validate(Role.PLANNER, "Here is the result: {}")


def test_conservative_token_counter_is_deterministic() -> None:
    counter = TokenCounter()
    first = counter.count_messages([{"role": "user", "content": "hello"}])
    second = counter.count_messages([{"role": "user", "content": "hello"}])
    assert first == second
    assert first.mode == "conservative_estimate"
    assert first.tokens > 0


@pytest.mark.parametrize(("key", "profile", "hint"), [
    ("sk-or-v1-" + "a" * 40, "deepseek", "OpenRouter key"),
    ("sk-" + "b" * 32, "deepseek", "choose qwen"),
    ("sk-" + "c" * 32, "qwen", "choose deepseek"),
    ("local-dev-value", "deepseek", "does not start with sk-"),
    ('"sk-' + "d" * 32 + '"', "deepseek", "quotes or spaces"),
])
def test_rejected_key_is_diagnosed_without_revealing_it(key: str, profile: str, hint: str) -> None:
    from harness.model.adapter import _auth_rejected_message

    message = _auth_rejected_message(401, "https://api.deepseek.com", profile, key)
    assert hint in message and f"key length {len(key)}" in message
    assert key not in message and key[6:] not in message


def test_quota_refusal_is_its_own_non_retried_error(tmp_path: Path) -> None:
    resolved = ModelProfileResolver(_write_profile(tmp_path)).resolve("designated")
    calls = []
    adapter = OpenAICompatibleModelAdapter(
        resolved, CredentialProvider({"AI_API_KEY": "test-key"}),
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(402))),
    )
    with pytest.raises(ModelAdapterError) as caught:
        adapter.generate(ModelCallRequest(call_id="q-1", role=Role.PLANNER, messages=[], response_schema={"type": "object"},
                                          max_output_tokens=10))
    assert caught.value.code == "MODEL_QUOTA_EXHAUSTED" and "insufficient balance" in str(caught.value)
    assert len(calls) == 1 and "test-key" not in str(caught.value)
