"""Deterministic context compaction with pinned and pair-retention invariants."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple


class ContextLimitError(RuntimeError):
    code = "CONTEXT_LIMIT"
    retryable = False


@dataclass(frozen=True)
class CompactItem:
    item_id: str
    section: str
    content: str
    estimated_tokens: int
    pinned: bool
    rank: int
    content_sha256: str
    valid: bool = True
    source_revision: Optional[str] = None
    pair_id: Optional[str] = None
    content_kind: str = "text"
    evidence_id: Optional[str] = None
    artifact_id: Optional[str] = None


class ContextCompactor:
    def compact(
        self,
        items: Sequence[CompactItem],
        max_tokens: int,
        *,
        source_revision: str,
    ) -> Tuple[List[CompactItem], List[Dict[str, str]]]:
        decisions: List[Dict[str, str]] = []
        valid: List[CompactItem] = []
        seen: set[str] = set()
        for item in items:
            if not item.valid or (
                item.source_revision is not None and item.source_revision != source_revision
            ):
                decisions.append({"item_id": item.item_id, "action": "remove_stale"})
                continue
            identity = f"{item.content_kind}:{item.content_sha256}"
            if identity in seen and not item.pinned:
                decisions.append({"item_id": item.item_id, "action": "remove_duplicate"})
                continue
            seen.add(identity)
            valid.append(item)

        pinned_tokens = sum(item.estimated_tokens for item in valid if item.pinned)
        if pinned_tokens > max_tokens:
            raise ContextLimitError(
                f"Pinned context requires {pinned_tokens} tokens but maximum input is {max_tokens}"
            )
        total = sum(item.estimated_tokens for item in valid)
        if total <= max_tokens:
            return valid, decisions

        pair_members: Dict[str, List[CompactItem]] = {}
        for item in valid:
            if item.pair_id:
                pair_members.setdefault(item.pair_id, []).append(item)
        removable = sorted(
            (item for item in valid if not item.pinned),
            key=lambda item: (item.rank, item.section, item.item_id),
        )
        removed: set[str] = set()
        for item in removable:
            if total <= max_tokens:
                break
            group = pair_members.get(item.pair_id, [item]) if item.pair_id else [item]
            if any(member.pinned for member in group):
                continue
            for member in group:
                if member.item_id not in removed:
                    removed.add(member.item_id)
                    total -= member.estimated_tokens
                    decisions.append(
                        {"item_id": member.item_id, "action": "remove_low_rank_optional"}
                    )
        if total > max_tokens:
            raise ContextLimitError("Context cannot fit without dropping pinned content")
        return [item for item in valid if item.item_id not in removed], decisions

