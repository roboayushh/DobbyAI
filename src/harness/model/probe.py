"""One tiny structured call that proves a model profile works before a real run.

Used by ``harness model doctor --live`` and ``harness doctor --live``. It checks
endpoint reachability, authentication, the configured model name, the selected
structured-output mode, and whether the provider reports token usage. The key
is never printed; the probe response is discarded after parsing.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict

from harness.contracts import Role
from harness.model.adapter import ModelAdapterError, ModelCallRequest, OpenAICompatibleModelAdapter
from harness.model.profile import ResolvedProfile

PROBE_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}, "sum": {"type": "integer"}},
    "required": ["ok", "sum"],
    "additionalProperties": False,
}


def probe_model(resolved: ResolvedProfile, adapter: Any = None) -> Dict[str, Any]:
    from harness.roles.schemas import extract_json_text

    adapter = adapter or OpenAICompatibleModelAdapter(resolved, max_attempts=2)
    messages = [
        {"role": "system", "content": "Return exactly one JSON object and nothing else."},
        {"role": "user", "content": 'Return the JSON object {"ok": true, "sum": N} where N = 17 + 25. Schema: '
                                    + json.dumps(PROBE_SCHEMA)},
    ]
    started = time.monotonic()
    try:
        response = adapter.generate(ModelCallRequest(
            call_id=f"probe_{uuid.uuid4().hex[:12]}", role=Role.PLANNER, messages=messages,
            response_schema=PROBE_SCHEMA, max_output_tokens=min(256, resolved.contract.max_output_tokens),
        ))
    except ModelAdapterError as exc:
        return {"status": "FAIL", "error_code": exc.code, "message": str(exc)[:300],
                "latency_ms": int((time.monotonic() - started) * 1000)}
    parsed_ok = False
    wrapped = response.raw_text.strip()[:1] != "{"
    try:
        data = json.loads(extract_json_text(response.raw_text))
        parsed_ok = data.get("ok") is True and data.get("sum") == 42
    except (ValueError, AttributeError):
        data = None
    return {
        "status": "PASS" if parsed_ok else "FAIL",
        "error_code": None if parsed_ok else "MODEL_PROBE_UNEXPECTED_OUTPUT",
        "structured_output": resolved.response_format,
        "json_wrapped_in_prose_or_fence": wrapped,
        "usage_reported": response.usage_source == "provider_reported",
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        "finish_reason": response.finish_reason,
        "latency_ms": response.latency_ms or int((time.monotonic() - started) * 1000),
    }
