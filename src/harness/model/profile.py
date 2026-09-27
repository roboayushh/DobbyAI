"""Resolve and fingerprint trusted, non-secret model profiles."""
from __future__ import annotations

import hashlib
import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional
from urllib.parse import urlparse

from pydantic import ValidationError

from harness.contracts import ResolvedModelProfileV1, SamplingV1


class ModelProfileError(ValueError):
    code = "INVALID_MODEL_PROFILE"
    retryable = False


RESPONSE_FORMATS = ("json_schema", "json_object", "none")
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
# Provider knobs a profile may pass through verbatim (non-secret scalars only). Anything that
# could redirect traffic, add credentials, or change the model is deliberately absent.
EXTRA_BODY_KEYS = {
    "enable_thinking", "thinking_budget", "presence_penalty", "frequency_penalty", "repetition_penalty",
    "top_k", "min_p", "reasoning_effort", "parallel_tool_calls",
}
# User-owned environment overrides (never read from a target repository).
ENV_OVERRIDES = {
    "HARNESS_MODEL_ENDPOINT": ("endpoint", str),
    "HARNESS_MODEL_NAME": ("model", str),
    "HARNESS_MODEL_CONTEXT_WINDOW": ("context_window_tokens", int),
    "HARNESS_MODEL_MAX_OUTPUT_TOKENS": ("max_output_tokens", int),
    "HARNESS_MODEL_RESPONSE_FORMAT": ("response_format", str),
    "HARNESS_MODEL_TIMEOUT_SECONDS": ("request_timeout_seconds", int),
    "HARNESS_MODEL_TEMPERATURE": ("temperature", float),
}


@dataclass(frozen=True)
class ResolvedProfile:
    contract: ResolvedModelProfileV1
    endpoint_url: str
    adapter_name: str
    allow_streaming: bool
    response_format: str = "json_schema"
    extra_body: Mapping[str, Any] = field(default_factory=dict)
    overrides: Mapping[str, Any] = field(default_factory=dict)


class ModelProfileResolver:
    _ALLOWED_KEYS = {
        "protocol",
        "adapter",
        "endpoint",
        "model",
        "context_window_tokens",
        "max_output_tokens",
        "safety_margin_tokens",
        "tokenizer",
        "request_timeout_seconds",
        "temperature",
        "top_p",
        "seed",
        "supports_json_schema",
        "allow_streaming",
        "response_format",
        "extra_body",
        "description",
    }

    def __init__(self, config_path: str | Path, environ: Optional[Mapping[str, str]] = None) -> None:
        self.config_path = Path(config_path).resolve()
        self.environ = os.environ if environ is None else environ
        self._endpoint_by_fingerprint: Dict[str, str] = {}

    def profile_ids(self) -> list:
        if not self.config_path.is_file():
            return []
        with self.config_path.open("rb") as handle:
            raw = tomllib.load(handle)
        return sorted((raw.get("profiles") or {}).keys())

    def _env_overrides(self) -> Dict[str, Any]:
        overrides: Dict[str, Any] = {}
        for variable, (key, kind) in ENV_OVERRIDES.items():
            value = self.environ.get(variable)
            if value is None or value == "":
                continue
            try:
                overrides[key] = kind(value)
            except ValueError as exc:
                raise ModelProfileError(f"{variable} must be a valid {kind.__name__}") from exc
        return overrides

    def resolve(self, profile_id: str) -> ResolvedProfile:
        if not self.config_path.is_file():
            raise ModelProfileError(f"Model profile file not found: {self.config_path}")
        with self.config_path.open("rb") as handle:
            raw = tomllib.load(handle)
        profiles = raw.get("profiles")
        if not isinstance(profiles, dict) or profile_id not in profiles:
            raise ModelProfileError(f"Unknown model profile: {profile_id}")
        values = profiles[profile_id]
        if not isinstance(values, dict):
            raise ModelProfileError(f"Invalid model profile object: {profile_id}")
        unknown = set(values) - self._ALLOWED_KEYS
        if unknown:
            raise ModelProfileError(
                f"Unknown keys in model profile {profile_id}: {', '.join(sorted(unknown))}"
            )
        overrides = self._env_overrides()
        values = {**values, **overrides}

        endpoint_url = str(values.get("endpoint", "")).rstrip("/")
        parsed = urlparse(endpoint_url)
        scheme = parsed.scheme.lower()
        loopback_http = scheme == "http" and (parsed.hostname or "").lower() in LOOPBACK_HOSTS
        if (
            (scheme != "https" and not loopback_http)
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ModelProfileError(
                "Model endpoint must be a credential-free HTTPS URL (plain HTTP is allowed only for loopback hosts)"
            )
        supports_schema = values.get("supports_json_schema", True)
        response_format = str(values.get("response_format") or ("json_schema" if supports_schema else "none"))
        if response_format not in RESPONSE_FORMATS:
            raise ModelProfileError(f"response_format must be one of {', '.join(RESPONSE_FORMATS)}")
        supports_schema = response_format == "json_schema"
        extra_body = values.get("extra_body") or {}
        if not isinstance(extra_body, dict):
            raise ModelProfileError("extra_body must be a table of scalar provider parameters")
        for key, value in extra_body.items():
            if key not in EXTRA_BODY_KEYS:
                raise ModelProfileError(f"extra_body key {key!r} is not an allowed provider parameter")
            if not isinstance(value, (bool, int, float, str)):
                raise ModelProfileError(f"extra_body value for {key!r} must be a scalar")
        endpoint_origin = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
        fingerprint_payload: Dict[str, Any] = {
            "profile_id": profile_id,
            "protocol": values.get("protocol"),
            "adapter": values.get("adapter", "openai-compatible"),
            "endpoint": endpoint_url,
            "model": values.get("model"),
            "context_window_tokens": values.get("context_window_tokens"),
            "max_output_tokens": values.get("max_output_tokens"),
            "safety_margin_tokens": values.get("safety_margin_tokens", 2000),
            "tokenizer": values.get("tokenizer"),
            "request_timeout_seconds": values.get("request_timeout_seconds"),
            "temperature": values.get("temperature", 0.0),
            "top_p": values.get("top_p", 1.0),
            "seed": values.get("seed"),
            "supports_json_schema": supports_schema,
            "allow_streaming": values.get("allow_streaming", False),
            "credential_env": "AI_API_KEY",
        }
        # New keys join the fingerprint only when non-default so existing frozen
        # profiles keep their historical fingerprints.
        if response_format not in ("json_schema",) and not (response_format == "none" and not values.get("response_format")):
            fingerprint_payload["response_format"] = response_format
        if extra_body:
            fingerprint_payload["extra_body"] = dict(sorted(extra_body.items()))
        canonical = json.dumps(
            fingerprint_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        try:
            contract = ResolvedModelProfileV1(
                profile_id=profile_id,
                protocol=fingerprint_payload["protocol"],
                endpoint_origin=endpoint_origin,
                model=fingerprint_payload["model"],
                context_window_tokens=fingerprint_payload["context_window_tokens"],
                max_output_tokens=fingerprint_payload["max_output_tokens"],
                safety_margin_tokens=fingerprint_payload["safety_margin_tokens"],
                tokenizer=fingerprint_payload["tokenizer"],
                request_timeout_seconds=fingerprint_payload["request_timeout_seconds"],
                sampling=SamplingV1(
                    temperature=fingerprint_payload["temperature"],
                    top_p=fingerprint_payload["top_p"],
                    seed=fingerprint_payload["seed"],
                ),
                supports_json_schema=fingerprint_payload["supports_json_schema"],
                profile_fingerprint=fingerprint,
            )
        except ValidationError as exc:
            raise ModelProfileError(f"Invalid model profile {profile_id}: {exc}") from exc
        resolved = ResolvedProfile(
            contract=contract,
            endpoint_url=endpoint_url,
            adapter_name=str(fingerprint_payload["adapter"]),
            allow_streaming=bool(fingerprint_payload["allow_streaming"]),
            response_format=response_format,
            extra_body=dict(extra_body),
            overrides={k: v for k, v in overrides.items()},
        )
        self._endpoint_by_fingerprint[fingerprint] = endpoint_url
        return resolved

    def validate_live(self, resolved: ResolvedProfile) -> None:
        profile = resolved.contract
        placeholders = (
            profile.endpoint_origin.endswith("provider.example")
            or profile.model == "official-model-id"
            or "placeholder" in profile.model.lower()
        )
        if placeholders:
            error = ModelProfileError(
                f"Model profile {profile.profile_id} still contains placeholder endpoint/model values"
            )
            error.code = "MODEL_PROFILE_INCOMPLETE"
            raise error
        if resolved.adapter_name != "openai-compatible":
            raise ModelProfileError(
                f"Live profile requires the built-in openai-compatible adapter, got {resolved.adapter_name}"
            )
        if resolved.allow_streaming:
            raise ModelProfileError("Streaming is disabled for auditable PRD 2 model calls")

