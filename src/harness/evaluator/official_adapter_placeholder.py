"""Slot for the organizer's official evaluator protocol (PRD 6 section 6.5).

The official request/result format is external input that has not been supplied.
This placeholder never guesses field names, hidden-test hooks, scoring weights, or
output locations: it refuses, and ``harness doctor --profile submission`` reports
``OFFICIAL_ADAPTER_NOT_CONFIGURED`` so nothing is labeled submission-ready.
"""
from __future__ import annotations

from harness.evaluator.interface import EvaluatorInputError


class OfficialAdapterPlaceholder:
    name = "official_unconfigured"
    version = "0.0.0"
    configured = False

    def parse(self, raw: bytes):
        raise EvaluatorInputError(
            "OFFICIAL_ADAPTER_NOT_CONFIGURED",
            "The official evaluator protocol has not been supplied; use adapter native_json_v1 or add the pinned official adapter",
        )

    def render(self, result):
        raise EvaluatorInputError("OFFICIAL_ADAPTER_NOT_CONFIGURED", "The official evaluator protocol has not been supplied")
