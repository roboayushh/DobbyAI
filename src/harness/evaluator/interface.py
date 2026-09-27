"""Evaluator adapter boundary (PRD 6 section 6.1)."""
from __future__ import annotations


class EvaluatorInputError(ValueError):
    """Invalid evaluator input: always fails before repository mutation or model calls."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
