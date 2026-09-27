"""Fail-closed parsing for untrusted structured role responses."""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, Tuple

from pydantic import TypeAdapter, ValidationError

from harness.contracts import (
    CoderRoleDecisionV1,
    PlannerDecisionV1,
    Role,
    ValidatorReviewV1,
)


class DuplicateJSONKeyError(ValueError):
    pass


class RoleSchemaError(ValueError):
    code = "ROLE_SCHEMA_INVALID"
    retryable = True


def _reject_duplicate_keys(pairs: Iterable[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJSONKeyError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
_FENCE = re.compile(r"^```[a-zA-Z0-9_-]*[ \t]*\n(?P<body>.*?)\n?```\s*$", re.DOTALL)


def extract_json_text(raw: str) -> str:
    """Strip only the explicitly allowed transport wrappers (PRD 2 section 13.3).

    Allowed, tested normalizers: ``<think>...</think>`` reasoning blocks emitted by
    reasoning models (discarded, never stored as evidence) and ONE surrounding
    Markdown code fence. Prose around the object is not a wrapper and is not
    stripped, so ``"Here is the result: {...}"`` still fails as non-strict JSON.
    """
    text = raw.strip().lstrip("\ufeff")
    text = _THINK_BLOCK.sub("", text).strip()
    if text.lower().startswith("</think>"):
        text = text[len("</think>"):].strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group("body").strip()
    return text


_TRIPLE_QUOTED = re.compile(r'"""(.*?)"""', re.DOTALL)


def repair_triple_quoted_strings(text: str) -> str:
    """Convert Python-style triple-quoted values (a frequent model mistake when
    embedding code in JSON) into properly escaped JSON strings. Format repair only:
    the result must still be strict JSON and pass the role schema."""
    return _TRIPLE_QUOTED.sub(lambda match: json.dumps(match.group(1)), text)


class RoleSchemaRegistry:
    def __init__(self) -> None:
        self._adapters = {
            Role.PLANNER: TypeAdapter(PlannerDecisionV1),
            Role.CODER: TypeAdapter(CoderRoleDecisionV1),
            Role.VALIDATOR: TypeAdapter(ValidatorReviewV1),
        }

    def schema(self, role: Role | str) -> Dict[str, Any]:
        normalized = Role(role)
        if normalized == Role.SUMMARIZER:
            raise RoleSchemaError("Summarizer is disabled until a versioned schema is registered")
        return self._adapters[normalized].json_schema()

    def validate(self, role: Role | str, raw: str) -> Any:
        normalized = Role(role)
        if normalized == Role.SUMMARIZER:
            raise RoleSchemaError("Summarizer is disabled until a versioned schema is registered")
        if not isinstance(raw, str) or not raw.strip():
            raise RoleSchemaError("Role response is empty")
        candidate = extract_json_text(raw)
        if not candidate:
            raise RoleSchemaError("Role response is empty")
        try:
            parsed = json.loads(candidate, object_pairs_hook=_reject_duplicate_keys)
        except DuplicateJSONKeyError as exc:
            raise RoleSchemaError(f"Role response is not strict JSON: {exc}") from exc
        except json.JSONDecodeError as exc:
            repaired = repair_triple_quoted_strings(candidate) if '"""' in candidate else None
            try:
                if repaired is None and exc.msg.startswith("Invalid control character"):
                    # Models often put real newlines inside the python_action string. The object is
                    # still one complete JSON value; only in-string escaping is lax, so accept it
                    # (strict=False) instead of paying for a full retry of the call.
                    parsed = json.loads(candidate, object_pairs_hook=_reject_duplicate_keys, strict=False)
                elif repaired is None:
                    raise exc
                else:
                    parsed = json.loads(repaired, object_pairs_hook=_reject_duplicate_keys, strict=False)
            except (json.JSONDecodeError, DuplicateJSONKeyError):
                raise RoleSchemaError(
                    f"Role response is not strict JSON: {exc}. Encode python_action as ONE JSON string "
                    "(escape newlines as \\n and double quotes as \\\"); never use Python triple quotes inside JSON."
                ) from exc
        if not isinstance(parsed, dict):
            raise RoleSchemaError("Role response must be one JSON object")
        try:
            return self._adapters[normalized].validate_python(parsed)
        except ValidationError as exc:
            # Pydantic errors contain field paths and rules, not the original
            # untrusted response text.
            raise RoleSchemaError(f"Role response failed schema validation: {exc}") from exc

