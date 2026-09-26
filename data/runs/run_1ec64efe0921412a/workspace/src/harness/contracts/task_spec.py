"""harness/contracts/task_spec.py
TaskSpecV1 – immutable task specification contract and normalization utilities.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TaskState(str, Enum):
    QUEUED = "QUEUED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


def normalize_content(text: str) -> str:
    """Normalize text without paraphrasing or summarizing:
    1. Unicode normalization (NFC)
    2. Normalize line endings to LF (\n)
    3. Strip trailing whitespace from each line
    4. Strip leading/trailing blank lines
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    result = "\n".join(lines).strip()
    return result


def compute_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class TaskSpecV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    task_id: str
    ordinal: int = Field(ge=0)
    source_type: str
    source_key: str
    source_snapshot_id: Optional[str] = None
    title: str
    body: str
    labels: List[str] = Field(default_factory=list)
    remote_state: str = "open"
    remote_created_at: Optional[str] = None
    remote_updated_at: Optional[str] = None
    raw_content_sha256: str
    normalized_content_sha256: str
    dependencies: List[str] = Field(default_factory=list)
    trust: str = "untrusted_input"

    @field_validator("raw_content_sha256", "normalized_content_sha256")
    @classmethod
    def validate_hash(cls, v: str) -> str:
        v = v.lower()
        if not re.match(r"^[0-9a-f]{64}$", v):
            raise ValueError("hash must be a 64-character lowercase hex string")
        return v
