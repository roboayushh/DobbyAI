"""Built-in model adapters for live, recorded, and deterministic fake calls."""
from __future__ import annotations

import json
import os
import random
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple

import httpx

from harness.contracts import ResolvedModelProfileV1, Role
from harness.model.credentials import CredentialProvider, ModelAuthMissingError
from harness.model.profile import ResolvedProfile
from harness.model.token_counter import TokenCounter


class ModelAdapterError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        uncertain_usage: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.uncertain_usage = uncertain_usage


@dataclass(frozen=True)
class ModelCallRequest:
    call_id: str
    role: Role
    messages: Sequence[Mapping[str, str]]
    response_schema: Mapping[str, Any]
    max_output_tokens: int


@dataclass(frozen=True)
class ModelAdapterResponse:
    raw_text: str
    input_tokens: int
    output_tokens: int
    usage_source: str
    provider_request_id: Optional[str] = None
    latency_ms: int = 0
    finish_reason: Optional[str] = None


class ModelAdapter(Protocol):
    name: str
    version: str

    def generate(self, request: ModelCallRequest) -> ModelAdapterResponse: ...

    def cancel(self, call_id: str) -> None: ...

    def healthcheck(self) -> Dict[str, Any]: ...


class OpenAICompatibleModelAdapter:
    name = "openai-compatible"
    version = "1.0"

    def __init__(
        self,
        resolved: ResolvedProfile,
        credential_provider: Optional[CredentialProvider] = None,
        *,
        max_response_bytes: int = 2 * 1024 * 1024,
        max_attempts: int = 4,
        client_factory: Optional[Callable[[], httpx.Client]] = None,
        max_retry_wait_seconds: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.resolved = resolved
        self.profile = resolved.contract
        self.credential_provider = credential_provider or CredentialProvider()
        self.max_response_bytes = max_response_bytes
        self.max_attempts = max(1, min(max_attempts, 5))
        self.max_retry_wait_seconds = max_retry_wait_seconds
        self._sleep = sleep
        self._client_factory = client_factory
        self._cancelled: set[str] = set()
        self._active_clients: Dict[str, httpx.Client] = {}
        self._lock = threading.Lock()

    def _new_client(self) -> httpx.Client:
        if self._client_factory is not None:
            return self._client_factory()
        timeout = httpx.Timeout(
            timeout=float(self.profile.request_timeout_seconds), connect=15.0
        )
        return httpx.Client(timeout=timeout, follow_redirects=False, verify=True)

    def generate(self, request: ModelCallRequest) -> ModelAdapterResponse:
        try:
            api_key = self.credential_provider.get_ai_api_key()
        except ModelAuthMissingError:
            raise

        endpoint = f"{self.resolved.endpoint_url}/chat/completions"
        payload: Dict[str, Any] = {
            "model": self.profile.model,
            "messages": [dict(message) for message in request.messages],
            "max_tokens": min(request.max_output_tokens, self.profile.max_output_tokens),
            "temperature": self.profile.sampling.temperature,
            "top_p": self.profile.sampling.top_p,
            "stream": False,
        }
        if self.profile.sampling.seed is not None:
            payload["seed"] = self.profile.sampling.seed
        response_format = getattr(self.resolved, "response_format", None) or (
            "json_schema" if self.profile.supports_json_schema else "none"
        )
        if response_format == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": f"{request.role.value}_response",
                    "strict": True,
                    "schema": dict(request.response_schema),
                },
            }
        elif response_format == "json_object":
            # DeepSeek / Qwen JSON mode: the schema itself travels in the prompt.
            payload["response_format"] = {"type": "json_object"}
        for key, value in dict(getattr(self.resolved, "extra_body", {}) or {}).items():
            payload.setdefault(key, value)

        last_error: Optional[ModelAdapterError] = None
        retry_after: Optional[float] = None
        origin = self.profile.endpoint_origin
        estimated_input = TokenCounter().count_messages(request.messages).tokens
        for attempt in range(1, max(self.max_attempts, RATE_LIMIT_ATTEMPTS) + 1):
            retry_after = None
            with self._lock:
                if request.call_id in self._cancelled:
                    raise ModelAdapterError("MODEL_CANCELLED", "Model call was cancelled")
            self._pace(origin, estimated_input, payload["max_tokens"])
            started = time.monotonic()
            try:
                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                }
                client = self._new_client()
                with self._lock:
                    self._active_clients[request.call_id] = client
                try:
                    with client.stream("POST", endpoint, headers=headers, json=payload) as response:
                        if 300 <= response.status_code < 400:
                            raise ModelAdapterError(
                                "MODEL_REDIRECT_REJECTED",
                                "Model endpoint redirect was rejected",
                            )
                        if response.status_code in (401, 403):
                            raise ModelAdapterError(
                                "MODEL_AUTH_INVALID",
                                _auth_rejected_message(response.status_code, self.profile.endpoint_origin,
                                                       self.profile.profile_id, api_key),
                            )
                        if response.status_code == 402:
                            raise ModelAdapterError(
                                "MODEL_QUOTA_EXHAUSTED",
                                f"{self.profile.endpoint_origin} accepted AI_API_KEY but refused the request (HTTP 402: "
                                "insufficient balance or quota). Top up the account or use a key with credit.",
                            )
                        if response.status_code == 404:
                            raise ModelAdapterError(
                                "MODEL_OR_ENDPOINT_UNKNOWN",
                                "Configured model or endpoint was not found",
                            )
                        if response.status_code in (413, 429):
                            tpm = _tpm_numbers(response.read()[:8192].decode("utf-8", "replace"))
                            if tpm is not None:
                                _learn_tpm(origin, tpm[0], tpm[1], estimated_input, payload["max_tokens"])
                            if tpm is not None and tpm[1] > tpm[0]:
                                # One request is larger than the whole per-minute cap: waiting cannot help,
                                # but a smaller packet can. The controller refits to the learned cap and retries.
                                error = ModelAdapterError(
                                    "MODEL_QUOTA_TPM_TOO_LOW",
                                    f"{origin} limits this account to {tpm[0]} tokens per minute, less than this "
                                    f"request ({tpm[1]} tokens); the harness shrinks its requests to fit.",
                                )
                                error.tpm_limit = tpm[0]
                                raise error
                        if response.status_code == 429 or response.status_code >= 500:
                            retry_after = _retry_after_seconds(response.headers.get("retry-after"))
                            raise ModelAdapterError(
                                "MODEL_RATE_LIMITED" if response.status_code == 429 else "MODEL_TRANSIENT_ERROR",
                                f"Transient model provider failure ({response.status_code})",
                                retryable=True,
                            )
                        if response.status_code >= 400:
                            error_body = response.read()
                            failed = _failed_generation(error_body) if len(error_body) <= self.max_response_bytes else None
                            if failed is not None:
                                # Groq JSON mode answers invalid JSON with HTTP 400 json_validate_failed and
                                # returns the text as failed_generation. Hand it to the normal schema
                                # validation so the role gets its bounded repair round with the exact error.
                                counter = TokenCounter()
                                return ModelAdapterResponse(
                                    raw_text=failed.replace(api_key, "[REDACTED_SECRET]"),
                                    input_tokens=counter.count_messages(request.messages).tokens,
                                    output_tokens=counter.count_text(failed).tokens,
                                    usage_source="estimated",
                                    provider_request_id=None,
                                    latency_ms=int((time.monotonic() - started) * 1000),
                                    finish_reason=None,
                                )
                            detail = error_body[:4096].decode("utf-8", "replace").lower()
                            if "response_format" in detail or "json_schema" in detail:
                                raise ModelAdapterError(
                                    "MODEL_RESPONSE_FORMAT_UNSUPPORTED",
                                    f"Model provider rejected the structured-output mode ({response.status_code}); "
                                    "set response_format = \"json_object\" (or \"none\") in the model profile",
                                )
                            if "context" in detail and ("length" in detail or "token" in detail):
                                raise ModelAdapterError(
                                    "MODEL_CONTEXT_OVERFLOW",
                                    f"Model provider rejected the request size ({response.status_code}); "
                                    "lower context_window_tokens in the model profile",
                                )
                            raise ModelAdapterError(
                                "MODEL_REQUEST_REJECTED",
                                f"Model provider rejected request ({response.status_code})",
                            )
                        content_length = response.headers.get("content-length")
                        if content_length and int(content_length) > self.max_response_bytes:
                            raise ModelAdapterError(
                                "MODEL_RESPONSE_TOO_LARGE", "Model response exceeded byte limit"
                            )
                        chunks: List[bytes] = []
                        size = 0
                        for chunk in response.iter_bytes():
                            size += len(chunk)
                            if size > self.max_response_bytes:
                                raise ModelAdapterError(
                                    "MODEL_RESPONSE_TOO_LARGE",
                                    "Model response exceeded byte limit",
                                )
                            chunks.append(chunk)
                        body = json.loads(b"".join(chunks))
                        choice = body["choices"][0]
                        raw_text = choice["message"].get("content")
                        # Reasoning models may return content=null (e.g. when the answer
                        # was truncated). Hidden reasoning fields are never stored.
                        if raw_text is None:
                            raw_text = ""
                        if not isinstance(raw_text, str):
                            raise (KeyError("content"))
                        finish_reason = choice.get("finish_reason")
                        if not isinstance(finish_reason, str) or len(finish_reason) > 32:
                            finish_reason = None
                        # A malicious or misconfigured provider must not be able
                        # to echo the sole credential into durable response data.
                        raw_text = raw_text.replace(api_key, "[REDACTED_SECRET]")
                        usage = body.get("usage") or {}
                        in_tokens = usage.get("prompt_tokens")
                        out_tokens = usage.get("completion_tokens")
                        usage_source = (
                            "provider_reported"
                            if isinstance(in_tokens, int) and isinstance(out_tokens, int)
                            else "unknown"
                        )
                        request_id = response.headers.get("x-request-id")
                        if request_id and (
                            api_key in request_id
                            or len(request_id) > 128
                            or any(ord(character) < 32 for character in request_id)
                        ):
                            request_id = None
                        return ModelAdapterResponse(
                            raw_text=raw_text,
                            input_tokens=in_tokens if isinstance(in_tokens, int) else 0,
                            output_tokens=out_tokens if isinstance(out_tokens, int) else 0,
                            usage_source=usage_source,
                            provider_request_id=request_id,
                            latency_ms=int((time.monotonic() - started) * 1000),
                            finish_reason=finish_reason,
                        )
                finally:
                    with self._lock:
                        self._active_clients.pop(request.call_id, None)
                    client.close()
            except ModelAdapterError as exc:
                last_error = exc
                rate_limited = exc.code == "MODEL_RATE_LIMITED"
                if not exc.retryable or attempt >= (RATE_LIMIT_ATTEMPTS if rate_limited else self.max_attempts):
                    raise
                if rate_limited:
                    # Wait for the provider's window to refill instead of failing the run.
                    wait = retry_after if retry_after is not None else min(2.0 ** attempt, RATE_LIMIT_MAX_WAIT_SECONDS)
                    _notify_wait(min(wait, RATE_LIMIT_MAX_WAIT_SECONDS), "Provider rate limit (HTTP 429)")
                    self._sleep(min(wait + random.random() * 0.25, RATE_LIMIT_MAX_WAIT_SECONDS))
                    continue
            except httpx.HTTPError as exc:
                with self._lock:
                    cancelled = request.call_id in self._cancelled
                last_error = ModelAdapterError(
                    "MODEL_CANCELLED" if cancelled else "MODEL_NETWORK_ERROR",
                    "Model call was cancelled"
                    if cancelled
                    else "Model request timed out or was interrupted",
                    retryable=not cancelled,
                    uncertain_usage=True,
                )
                if attempt >= self.max_attempts:
                    raise last_error from exc
                if cancelled:
                    raise last_error from exc
            except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError) as exc:
                raise ModelAdapterError(
                    "MODEL_PROTOCOL_ERROR", "Model provider returned an invalid response envelope"
                ) from exc

            # Bounded backoff honoring Retry-After; never include credentials or provider body in errors.
            base = retry_after if retry_after is not None else min(1.0 * (2 ** (attempt - 1)), 8.0)
            self._sleep(min(base + random.random() * 0.25, self.max_retry_wait_seconds))

        assert last_error is not None
        raise last_error

    def _pace(self, origin: str, estimated_input: int, max_tokens: int) -> None:
        """Hold a request until it fits the learned tokens-per-minute window (no-op when none is known)."""
        limits = provider_tpm_limit(origin)
        if not limits or "tpm" not in limits:
            return
        cap = int(limits["tpm"] * 0.95)
        needed = int(estimated_input * limits.get("ratio", 1.0)) + max_tokens
        for _ in range(8):  # bounded: each wait lets the oldest entry leave the 60 s window
            now = time.monotonic()
            with _LIMITS_LOCK:
                window = [entry for entry in _TPM_WINDOWS.get(origin, []) if now - entry[0] < 60.0]
                _TPM_WINDOWS[origin] = window
                used = sum(tokens for _, tokens in window)
                if not window or used + needed <= cap:
                    window.append((now, needed))
                    return
                wait = 60.0 - (now - window[0][0]) + 0.5
            _notify_wait(min(max(wait, 0.5), RATE_LIMIT_MAX_WAIT_SECONDS), f"Pacing to {int(limits['tpm'])} tokens/minute")
            self._sleep(min(max(wait, 0.5), RATE_LIMIT_MAX_WAIT_SECONDS))

    def cancel(self, call_id: str) -> None:
        with self._lock:
            self._cancelled.add(call_id)
            client = self._active_clients.get(call_id)
        if client is not None:
            client.close()

    def healthcheck(self) -> Dict[str, Any]:
        return {
            "adapter": self.name,
            "version": self.version,
            "profile_id": self.profile.profile_id,
            "profile_fingerprint": self.profile.profile_fingerprint,
            "endpoint_origin": self.profile.endpoint_origin,
            "model": self.profile.model,
        }


# Public key-format prefixes of other providers (format markers, not secret material).
_FOREIGN_KEY_PREFIXES = (
    ("sk-or-", "an OpenRouter key: use the openrouter-deepseek profile (make run MODEL=openrouter-deepseek)"),
    ("sk-ant-", "an Anthropic key, which this OpenAI-compatible profile cannot use"),
    ("sk-proj-", "an OpenAI project key, not a DeepSeek or Qwen key"),
    ("gsk_", "a Groq key: use the groq-qwen profile (make run MODEL=groq-qwen)"),
    ("hf_", "a Hugging Face token, not a DeepSeek or Qwen key"),
    ("AIza", "a Google API key, not a DeepSeek or Qwen key"),
)


def _auth_rejected_message(status: int, origin: str, profile_id: str, api_key: str) -> str:
    """Explain a 401/403 without revealing any part of the key beyond a public format prefix."""
    hint = next((text for prefix, text in _FOREIGN_KEY_PREFIXES if api_key.startswith(prefix)), None)
    if hint is None and any(ch in api_key for ch in "\"' "):
        hint = "it contains quotes or spaces; export the bare key: export AI_API_KEY=sk-..."
    elif hint is None and not api_key.startswith("sk-"):
        hint = "it does not start with sk-, as DeepSeek and DashScope (Qwen) keys do"
    elif hint is None:
        other = "qwen (or qwen-cn for a mainland-China key)" if profile_id.startswith("deepseek") else "deepseek"
        hint = (f"DeepSeek and DashScope keys look alike: if this key is for the other provider, choose {other} "
                f"(make run MODEL=...); otherwise the key is wrong, revoked, or for another account")
    lead = f"{origin} rejected AI_API_KEY for profile {profile_id} (HTTP {status}; key length {len(api_key)})"
    if hint.startswith(("an ", "a ")):
        return f"{lead}. The key looks like {hint}."
    return f"{lead}: {hint}."


RATE_LIMIT_ATTEMPTS = 8          # 429s are waited out, not fatal: the TPM window refills every minute
RATE_LIMIT_MAX_WAIT_SECONDS = 65.0

# What each provider origin has told us about its tokens-per-minute cap in this process:
# {"tpm": limit, "ratio": provider tokens per estimated token}. Used to size packets
# (controller) and to pace requests so they fit the one-minute window (adapter).
_PROVIDER_LIMITS: Dict[str, Dict[str, float]] = {}
_TPM_WINDOWS: Dict[str, List[Tuple[float, int]]] = {}
_LIMITS_LOCK = threading.Lock()
# Observers of rate-limit waits (the interactive progress feed): callable(seconds, reason).
WAIT_LISTENERS: List[Callable[[float, str], None]] = []


def _notify_wait(seconds: float, reason: str) -> None:
    for listener in list(WAIT_LISTENERS):
        try:
            listener(seconds, reason)
        except Exception:
            pass


def provider_tpm_limit(origin: str) -> Optional[Dict[str, float]]:
    """The learned (or HARNESS_MODEL_TPM_LIMIT-configured) tokens-per-minute cap for an origin."""
    configured = os.environ.get("HARNESS_MODEL_TPM_LIMIT", "").strip()
    with _LIMITS_LOCK:
        learned = dict(_PROVIDER_LIMITS.get(origin) or {})
    if configured.isdigit() and int(configured) > 0:
        learned["tpm"] = min(int(configured), int(learned.get("tpm", configured)))
    return learned or None


def _learn_tpm(origin: str, limit: int, requested: int, estimated_input: int, max_tokens: int) -> None:
    with _LIMITS_LOCK:
        entry = _PROVIDER_LIMITS.setdefault(origin, {})
        entry["tpm"] = min(limit, int(entry.get("tpm", limit)))
        if requested > max_tokens and estimated_input > 0:
            entry["ratio"] = max(0.5, min(1.5, (requested - max_tokens) / estimated_input))


def _tpm_numbers(body: str) -> Optional[Tuple[int, int]]:
    """(limit, requested) from a provider's tokens-per-minute rate-limit message (Groq format)."""
    if "tokens per minute" not in body.lower():
        return None
    limit = re.search(r"Limit\s+(\d+)", body)
    requested = re.search(r"Requested\s+(\d+)", body)
    if not limit or not requested:
        return None
    return int(limit.group(1)), int(requested.group(1))


def _failed_generation(body: bytes) -> Optional[str]:
    """The model text a provider rejected in JSON mode (Groq ``json_validate_failed``), if any."""
    try:
        error = json.loads(body).get("error") or {}
    except (ValueError, AttributeError):
        return None
    text = error.get("failed_generation") if isinstance(error, dict) else None
    if error.get("code") != "json_validate_failed" or not isinstance(text, str):
        return None
    return text


def _retry_after_seconds(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    return seconds if 0 <= seconds <= 3600 else None


class FakeModelAdapter:
    name = "fake"
    version = "1.0"

    def __init__(
        self,
        responses: Sequence[ModelAdapterResponse | str | Exception],
        *,
        profile: Optional[ResolvedModelProfileV1] = None,
    ) -> None:
        self._responses = list(responses)
        self.profile = profile
        self.calls: List[ModelCallRequest] = []
        self.cancelled: set[str] = set()

    def generate(self, request: ModelCallRequest) -> ModelAdapterResponse:
        self.calls.append(request)
        if request.call_id in self.cancelled:
            raise ModelAdapterError("MODEL_CANCELLED", "Model call was cancelled")
        if not self._responses:
            raise ModelAdapterError("FAKE_RESPONSES_EXHAUSTED", "No fake response remains")
        value = self._responses.pop(0)
        if isinstance(value, Exception):
            raise value
        if isinstance(value, str):
            return ModelAdapterResponse(
                raw_text=value,
                input_tokens=100,
                output_tokens=50,
                usage_source="provider_reported",
                latency_ms=1,
            )
        return value

    def cancel(self, call_id: str) -> None:
        self.cancelled.add(call_id)

    def healthcheck(self) -> Dict[str, Any]:
        return {"adapter": self.name, "version": self.version, "ready": True}


class RecordedModelAdapter(FakeModelAdapter):
    name = "recorded"

    @classmethod
    def from_file(
        cls,
        fixture_path: str | Path,
        *,
        profile: Optional[ResolvedModelProfileV1] = None,
    ) -> "RecordedModelAdapter":
        parsed = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
        records = parsed.get("responses") if isinstance(parsed, dict) else parsed
        if not isinstance(records, list):
            raise ValueError("Recorded adapter fixture must contain a response list")
        responses: List[ModelAdapterResponse] = []
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("raw_text"), str):
                raise ValueError("Invalid recorded model response")
            responses.append(
                ModelAdapterResponse(
                    raw_text=record["raw_text"],
                    input_tokens=int(record.get("input_tokens", 0)),
                    output_tokens=int(record.get("output_tokens", 0)),
                    usage_source=str(record.get("usage_source", "estimated")),
                    provider_request_id=record.get("provider_request_id"),
                    latency_ms=int(record.get("latency_ms", 0)),
                )
            )
        return cls(responses, profile=profile)
