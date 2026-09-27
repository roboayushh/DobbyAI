"""Secret detection for untrusted structured inputs and outgoing evidence (defense in depth).

The primary control is never injecting secrets; this scanner rejects requests that
carry them inline (PRD 6 AT6-004) and checks export/release evidence for sentinels.
"""
from __future__ import annotations

import os
import re
from typing import Any, Iterable, List, Optional

SECRET_KEY_NAMES = re.compile(
    r"(?i)^(.*[_-])?(api[_-]?key|secret|client[_-]?secret|password|passwd|authorization|private[_-]?key|credentials?"
    r"|access[_-]?token|auth[_-]?token|refresh[_-]?token|bearer[_-]?token|token)$"
)
SECRET_VALUE_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{20,}"),
]


def known_secret_values(extra: Iterable[str] = ()) -> List[str]:
    values = [os.environ.get("AI_API_KEY", ""), os.environ.get("GITHUB_TOKEN", ""), *extra]
    return [value for value in values if value and len(value) >= 8]


def scan(value: Any, *, path: str = "$", known: Optional[List[str]] = None) -> List[str]:
    """Return JSON paths that look like they carry a credential (never the values themselves)."""
    known = known_secret_values() if known is None else known
    hits: List[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}"
            if SECRET_KEY_NAMES.search(str(key)) and item not in (None, "", [], {}):
                hits.append(child)
            hits.extend(scan(item, path=child, known=known))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            hits.extend(scan(item, path=f"{path}[{index}]", known=known))
    elif isinstance(value, str):
        if any(secret in value for secret in known) or any(p.search(value) for p in SECRET_VALUE_PATTERNS):
            hits.append(path)
    return hits


def scan_bytes(data: bytes, known: Optional[List[str]] = None) -> bool:
    text = data.decode("utf-8", "replace")
    known = known_secret_values() if known is None else known
    return any(secret in text for secret in known) or any(p.search(text) for p in SECRET_VALUE_PATTERNS)
