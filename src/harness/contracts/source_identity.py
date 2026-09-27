"""harness/contracts/source_identity.py
SourceIdentityV1 – exact repository provenance contract.
"""
from __future__ import annotations

import re
from typing import Optional

from pydantic import BaseModel, ConfigDict, field_validator


class SourceIdentityV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    source_kind: str
    canonical_locator: str
    upstream_commit: Optional[str] = None
    baseline_commit: str
    baseline_tree: str
    content_tree_sha256: str
    import_manifest_sha256: str
    dirty_source_imported: bool
    created_at: str

    @field_validator("content_tree_sha256", "import_manifest_sha256")
    @classmethod
    def validate_hash(cls, v: str) -> str:
        v = v.lower()
        if not re.match(r"^[0-9a-f]{64}$", v):
            raise ValueError("hash must be a 64-character lowercase hex string")
        return v
