"""Truthful human (Markdown) and machine release reports (PRD 6 section 8.5)."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List

from harness.release.run_facts import RunFacts
from harness.release.result_builder import usage, verification_summary

MAX_REPORT_CHARS = 200_000


def _md(text: Any, limit: int = 300) -> str:
    value = str(text if text is not None else "")
    value = "".join(ch for ch in value if ch == "\n" or ch >= " ").replace("|", "\\|")
    return value if len(value) <= limit else value[: limit - 1] + "…"


def machine_report(facts: RunFacts, *, manifest: Dict[str, Any] = None, round_trip: Dict[str, Any] = None,
                   provenance: Dict[str, Any] = None) -> Dict[str, Any]:
    locator = facts.source.get("canonical_locator") or ""
    checks: List[Dict[str, Any]] = []
    for task in facts.tasks:
        for criterion in (task.decision or {}).get("criteria", []):
            checks.append({"task_id": task.task_id, "criterion_id": criterion.get("criterion_id"), "status": criterion.get("status")})
    return {
        "schema_version": "1.0",
        "run_id": facts.run_id,
        "status": facts.status,
        "request": {
            "task_mode": facts.run.get("task_mode"),
            "execution_mode": facts.run.get("execution_mode"),
            "source_locator_sha256": hashlib.sha256(locator.encode()).hexdigest() if locator else None,
        },
        "source": {
            "upstream_commit": facts.source.get("upstream_commit"),
            "baseline_commit": facts.source.get("baseline_commit"),
            "baseline_tree": facts.source.get("baseline_tree"),
            "dirty_or_synthetic_baseline": bool(facts.source.get("dirty_source_imported")),
        },
        "candidate": {"commit": facts.candidate.head, "kind": facts.candidate.kind, "verified": facts.candidate.verified},
        "tasks": [
            {
                "task_id": task.task_id,
                "ordinal": task.ordinal,
                "title": task.title[:200],
                "queue_state": task.item_state,
                "status": task.external_status,
                "reasons": task.reasons[:20],
                "changed_paths": task.changed_paths[:500],
                "commit": task.commit,
                "decision": {
                    key: (task.decision or {}).get(key)
                    for key in ("status", "reason_codes", "required_checks", "validator_decision", "diff_review_status")
                } if task.decision else None,
            }
            for task in facts.tasks
        ],
        "criteria": checks,
        "verification": verification_summary(facts).model_dump(mode="json"),
        "usage": usage(facts).model_dump(mode="json"),
        "best_partial_candidates": facts.report.get("best_partial_candidates", []),
        "integration_heads": facts.report.get("integration_heads", []),
        "pending_approvals": facts.pending_approvals,
        "export": manifest,
        "round_trip": round_trip,
        "provenance": provenance,
        "limitations": facts.limitations,
        "publication_authorized": False,
    }


def markdown_report(facts: RunFacts, *, manifest: Dict[str, Any] = None, round_trip: Dict[str, Any] = None,
                    provenance: Dict[str, Any] = None) -> str:
    verification = verification_summary(facts)
    spend = usage(facts)
    lines = [
        f"# Harness run report — {facts.run_id}",
        "",
        f"**Result:** `{facts.status}`  ",
        f"**Mode:** {facts.run.get('task_mode')} / {facts.run.get('execution_mode')}  ",
        f"**Baseline:** `{facts.source.get('baseline_commit')}`"
        + (" (synthetic: includes pre-existing uncommitted changes)" if facts.source.get("dirty_source_imported") else ""),
        f"**Candidate:** `{facts.candidate.head}` ({facts.candidate.kind}{', verified' if facts.candidate.verified else ', NOT verified'})",
        "",
        "## Tasks",
        "",
        "| # | Task | Status | Changed paths | Notes |",
        "|---|------|--------|---------------|-------|",
    ]
    for task in facts.tasks:
        notes = ", ".join(task.reasons[:4])
        lines.append(f"| {task.ordinal + 1} | {_md(task.title, 80)} | `{task.external_status}` | "
                     f"{_md(', '.join(task.changed_paths[:8]), 200) or '—'} | {_md(notes, 160) or '—'} |")
    lines += [
        "",
        "## Verification (host-observed)",
        "",
        f"- Status: `{verification.status}`",
        f"- Required checks: {verification.required_checks} (passed {verification.passed_checks}, failed "
        f"{verification.failed_checks}, skipped {verification.skipped_checks}, inconclusive/unavailable {verification.unavailable_checks})",
    ]
    for task in facts.tasks:
        decision = task.decision or {}
        if decision:
            lines.append(f"- {task.task_id}: `{decision.get('status')}` — {_md(', '.join(decision.get('reason_codes', [])[:8]), 400)}; "
                         f"validator {decision.get('validator_decision') or 'n/a'}; diff review {decision.get('diff_review_status')}")
    partials = facts.report.get("best_partial_candidates", [])
    if partials:
        lines += ["", "## Unintegrated attempts (not verified, never integrated)", ""]
        for partial in partials:
            lines.append(f"- {partial['task_id']}: `{partial['status']}` candidate `{partial['candidate_commit'][:12]}`")
    lines += [
        "",
        "## Usage",
        "",
        f"- Model calls: {spend.model_calls}; input tokens: {spend.input_tokens}; output tokens: {spend.output_tokens}"
        + (f" (estimated: {', '.join(spend.estimated_fields)})" if spend.estimated_fields else ""),
        f"- Wall time: {spend.wall_seconds} s",
    ]
    if round_trip:
        lines += ["", "## Export", "", f"- Patch round-trip from exact baseline: `{round_trip.get('status')}`"
                  f" (tree `{round_trip.get('observed_tree')}`)"]
    if provenance:
        model = provenance.get("model", {})
        lines += ["", "## Provenance", "", f"- Model: `{model.get('model')}` via `{model.get('provider')}` "
                  f"(profile fingerprint `{str(model.get('config_sha256'))[:16]}`)",
                  f"- Runtime image: `{provenance.get('runtime', {}).get('image_digest')}`",
                  f"- Harness build: `{provenance.get('harness', {}).get('build_id')}`"]
    if facts.pending_approvals:
        lines += ["", "## Pending approvals", ""] + [f"- `{p['capability_request_id']}` {p['operation']}: {_md(p['summary'])}" for p in facts.pending_approvals]
    lines += ["", "## Limitations", ""] + [f"- {_md(item, 500)}" for item in facts.limitations]
    lines += ["", "_Nothing was pushed, merged, or applied to the original repository._", ""]
    text = "\n".join(lines)
    return text if len(text) <= MAX_REPORT_CHARS else text[:MAX_REPORT_CHARS] + "\n\n…[report truncated]\n"


class MarkdownReportRenderer:
    """Built-in ReportRendererPlugin: renders a RunFacts view as bounded Markdown."""

    def render(self, view: RunFacts, **extras: Any) -> str:
        return markdown_report(view, **extras)
