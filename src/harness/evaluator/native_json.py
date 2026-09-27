"""``native_json_v1``: the stable, frozen harness request/result contract (REL-001)."""
from __future__ import annotations

import json
import math
from typing import Any, Dict

from pydantic import ValidationError

from harness.contracts.release import EvaluatorRequestV1, EvaluatorResultV1
from harness.evaluator.interface import EvaluatorInputError
from harness.release import secrets


def _no_duplicates(pairs):
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EvaluatorInputError("INVALID_REQUEST", f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(name: str):
    raise EvaluatorInputError("INVALID_REQUEST", f"Non-finite number {name} is not allowed")


class NativeJsonEvaluatorAdapter:
    name = "native_json_v1"
    version = "1.0.0"

    def __init__(self, max_request_bytes: int = 1024 * 1024) -> None:
        self.max_request_bytes = max_request_bytes

    def parse(self, raw: bytes) -> EvaluatorRequestV1:
        if len(raw) > self.max_request_bytes:
            raise EvaluatorInputError("REQUEST_TOO_LARGE", f"Request exceeds {self.max_request_bytes} bytes")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EvaluatorInputError("INVALID_REQUEST", "Request is not UTF-8") from exc
        try:
            data = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_reject_constant)
        except json.JSONDecodeError as exc:
            raise EvaluatorInputError("INVALID_REQUEST", f"Request is not valid JSON: {exc.msg} at line {exc.lineno}") from exc
        if not isinstance(data, dict):
            raise EvaluatorInputError("INVALID_REQUEST", "Request must be one JSON object")
        hits = secrets.scan(data)
        if hits:
            # Never echo the value; secrets belong in the host environment (AI_API_KEY).
            raise EvaluatorInputError("SECRET_IN_REQUEST", "Request carries credential-like values at " + ", ".join(hits[:5])
                                      + "; supply secrets only through the host environment")
        try:
            request = EvaluatorRequestV1.model_validate(data)
        except ValidationError as exc:
            details = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:6])
            code = "UNSUPPORTED_SCHEMA_VERSION" if "UNSUPPORTED_SCHEMA_VERSION" in details else "INVALID_REQUEST"
            raise EvaluatorInputError(code, details[:1000]) from exc
        if request.adapter.name != self.name:
            raise EvaluatorInputError("ADAPTER_MISMATCH", f"Request targets adapter {request.adapter.name}, not {self.name}")
        if request.adapter.version.split(".")[0] != self.version.split(".")[0]:
            raise EvaluatorInputError("UNSUPPORTED_ADAPTER_VERSION", f"Adapter version {request.adapter.version} is not supported")
        return request

    def render(self, result: EvaluatorResultV1) -> Dict[str, Any]:
        return result.model_dump(mode="json")
