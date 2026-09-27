"""Token counting with a deterministic conservative fallback."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence


@dataclass(frozen=True)
class TokenCount:
    tokens: int
    mode: str


class TokenCounter:
    """Count messages without making a provider call.

    A tokenizer can be injected when the designated model has a reviewed local
    implementation. Otherwise UTF-8 bytes are conservatively estimated at one
    token per three bytes, plus fixed message framing overhead.
    """

    def __init__(self, tokenizer: object | None = None) -> None:
        self._tokenizer = tokenizer

    def count_text(self, text: str) -> TokenCount:
        if self._tokenizer is not None and hasattr(self._tokenizer, "encode"):
            encoded = self._tokenizer.encode(text)  # type: ignore[attr-defined]
            return TokenCount(tokens=len(encoded), mode="tokenizer")
        byte_count = len(text.encode("utf-8"))
        return TokenCount(tokens=max(1, math.ceil(byte_count / 3)), mode="conservative_estimate")

    def count_messages(self, messages: Sequence[Mapping[str, str]]) -> TokenCount:
        total = 2
        mode = "tokenizer" if self._tokenizer is not None else "conservative_estimate"
        for message in messages:
            total += 8
            for key in ("role", "content", "name"):
                value = message.get(key)
                if value:
                    count = self.count_text(value)
                    total += count.tokens
                    if count.mode != "tokenizer":
                        mode = "conservative_estimate"
        return TokenCount(tokens=total, mode=mode)

