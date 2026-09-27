"""Live progress feed for interactive runs: which agent is working, what it decided, what ran.

It only reads the run's event log and the stored model responses (never the provider,
never the workspace), so it cannot change the run. Every read is best-effort: a feed
problem is swallowed and never interrupts the task.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

AGENTS = {
    "planner": ("🧭", "Planner", "magenta"),
    "coder": ("🛠 ", "Coder", "cyan"),
    "validator": ("🔍", "Validator", "yellow"),
    "summarizer": ("📝", "Summarizer", "blue"),
}


def _k(tokens: Any) -> str:
    try:
        value = int(tokens)
    except (TypeError, ValueError):
        return "?"
    return f"{value / 1000:.1f}k" if value >= 1000 else str(value)


def _clip(text: Any, limit: int = 110) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _escape(text: str) -> str:
    return text.replace("[", "\\[")


class ProgressFeed:
    """Render the run's events as they happen, with a spinner naming the current activity."""

    def __init__(self, console: Any, db_path: Path, run_id: str, artifacts_root: Path, poll_seconds: float = 0.4) -> None:
        self.console = console
        self.db_path = Path(db_path)
        self.run_id = run_id
        self.artifacts_root = Path(artifacts_root)
        self.poll_seconds = poll_seconds
        self._seq = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._status: Any = None
        self._roles: Dict[str, str] = {}
        self._calls = 0
        self._tokens = [0, 0]
        self._started = time.monotonic()

    # ------------------------------------------------------------------ lifecycle
    def __enter__(self) -> "ProgressFeed":
        from harness.model import adapter

        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM h_events WHERE run_id = ?", (self.run_id,)).fetchone()
        self._seq = int(row[0] or 0)
        self.console.rule("[bold cyan]DobbyAI[/bold cyan] [dim]· Planner → Coder → Validator · sandboxed · live[/dim]")
        self._status = self.console.status("[bold]Starting the agents…[/bold]", spinner="dots12")
        self._status.start()
        adapter.WAIT_LISTENERS.append(self._on_wait)
        self._thread = threading.Thread(target=self._loop, name="progress-feed", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        from harness.model import adapter

        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._drain()
        if self._on_wait in adapter.WAIT_LISTENERS:
            adapter.WAIT_LISTENERS.remove(self._on_wait)
        if self._status is not None:
            self._status.stop()
        elapsed = int(time.monotonic() - self._started)
        self.console.rule(f"[dim]{self._calls} model calls · {_k(self._tokens[0])} in / {_k(self._tokens[1])} out tokens · "
                          f"{elapsed // 60}m{elapsed % 60:02d}s[/dim]")

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            self._drain()

    def _drain(self) -> None:
        try:
            with sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=2) as conn:
                rows = conn.execute(
                    "SELECT seq, event_type, payload_json FROM h_events WHERE run_id = ? AND seq > ? ORDER BY seq",
                    (self.run_id, self._seq),
                ).fetchall()
        except Exception:
            return
        for seq, kind, payload in rows:
            self._seq = seq
            try:
                self._render(kind, json.loads(payload or "{}"))
            except Exception:
                continue

    # ------------------------------------------------------------------ output
    def _say(self, text: str) -> None:
        self.console.print(text, highlight=False)

    def _doing(self, text: str) -> None:
        if self._status is not None:
            self._status.update(text)

    def _on_wait(self, seconds: float, reason: str) -> None:
        if seconds >= 2:
            self._say(f"   [yellow]⏳ {reason}: waiting {seconds:.0f}s for the provider's next token window[/yellow]")
            self._doing(f"[yellow]⏳ Rate limit: resuming in {seconds:.0f}s…[/yellow]")

    def _agent(self, role: str) -> tuple:
        return AGENTS.get(role, ("🤖", role.title() or "Agent", "white"))

    def _render(self, kind: str, p: Dict[str, Any]) -> None:
        if kind == "SOURCE_ACQUIRED":
            self._say("📦 Private copy of the repository ready [dim](the original is never modified)[/dim]")
        elif kind == "INDEX_READY":
            self._say(f"🗂  Indexed [bold]{p.get('indexed_files', '?')}[/bold] files · {p.get('symbols', '?')} symbols")
        elif kind == "MODEL_CALL_REQUESTED":
            role = str(p.get("role", ""))
            self._roles[str(p.get("call_id"))] = role
            icon, name, colour = self._agent(role)
            self._doing(f"{icon} [bold {colour}]{name}[/bold {colour}] is thinking… "
                        f"[dim]({_k(p.get('reserved_input_tokens'))} tokens of context)[/dim]")
        elif kind == "MODEL_CALL_SETTLED":
            self._settled(p)
        elif kind == "PLAN_READY":
            self._say(f"📋 Plan accepted [dim](revision {p.get('plan_revision', 1)})[/dim]")
        elif kind == "REPLANNING_STARTED":
            self._say("🔄 [magenta]Replanning[/magenta] from new evidence")
        elif kind == "BASELINE_CAPTURE_STARTED":
            self._doing("🧪 Running the existing tests to record the baseline…")
        elif kind == "BASELINE_CHECK_SETTLED":
            self._say(f"🧪 Baseline [bold]{p.get('check_id')}[/bold]: {self._counts(p)}")
        elif kind == "SANDBOX_STARTED":
            self._say("🐳 Sandbox: executing the coder's action [dim](no network, non-root, resource-limited)[/dim]")
            self._doing("🐳 Action running in the sandbox…")
        elif kind == "SANDBOX_STOPPED" and p.get("limit_breach"):
            self._say(f"   [yellow]⚠ sandbox limit reached: {p.get('limit_breach')}[/yellow]")
        elif kind == "ACTION_SETTLED":
            if p.get("settlement") == "ACCEPTED":
                self._say("   [green]✓ action accepted — workspace updated[/green]")
            else:
                self._say(f"   [red]↩ action {str(p.get('settlement', 'rejected')).lower()} — changes rolled back[/red]")
        elif kind == "CANDIDATE_FROZEN":
            self._say(f"🧊 Candidate frozen: [bold]{_escape(', '.join(p.get('changed_paths') or []) or 'no changes')}[/bold]")
        elif kind == "VERIFICATION_ATTEMPT_STARTED":
            self._doing("🔬 Verifying the candidate (tests + diff review)…")
        elif kind == "CHECK_SETTLED":
            self._say(f"{'✅' if p.get('status') == 'PASS' else '❌'} Check [bold]{p.get('check_id')}[/bold]: {self._counts(p)}")
        elif kind == "VALIDATOR_REVIEW_SETTLED":
            findings = p.get("blocking_findings") or 0
            self._say(f"🔍 Validator review: [bold]{p.get('decision')}[/bold]"
                      + (f" [red]({findings} blocking)[/red]" if findings else ""))
        elif kind == "COMPLETION_GATE_EVALUATED":
            ok = p.get("status") == "PASS"
            reasons = ", ".join(str(r).lower().replace("_", " ") for r in (p.get("reason_codes") or [])[:3])
            self._say(f"{'🏁' if ok else '⛔'} Completion gate: [bold {'green' if ok else 'red'}]{p.get('status')}[/bold "
                      f"{'green' if ok else 'red'}] [dim]{reasons}[/dim]")
        elif kind == "INTEGRATION_REF_ADVANCED":
            self._say("🔀 Integrated into the private result branch")
        elif kind in ("BUDGET_EXHAUSTED", "TASK_BUDGET_EXHAUSTED"):
            self._say(f"[yellow]⌛ Budget exhausted {('(' + str(p.get('reason')) + ')') if p.get('reason') else ''}[/yellow]")
        elif kind == "ORCHESTRATION_FAILED":
            self._say("[red]✗ The task stopped; see the result below[/red]")

    def _counts(self, p: Dict[str, Any]) -> str:
        counts = p.get("counts") or {}
        status = p.get("status", "?")
        colour = "green" if status == "PASS" else ("yellow" if status in ("TIMEOUT", "FAILED", "FAIL") else "white")
        return (f"[{colour}]{status}[/{colour}] [dim]({counts.get('passed', 0)} passed, {counts.get('failed', 0)} failed"
                f"{', ' + str(counts.get('errors')) + ' errors' if counts.get('errors') else ''})[/dim]")

    def _settled(self, p: Dict[str, Any]) -> None:
        call_id = str(p.get("call_id"))
        icon, name, colour = self._agent(self._roles.pop(call_id, ""))
        if p.get("state") != "SUCCEEDED":
            self._say(f"   [dim]{icon} {name}: {self._failure(call_id)}[/dim]")
            return
        self._calls += 1
        self._tokens[0] += int(p.get("input_tokens") or 0)
        self._tokens[1] += int(p.get("output_tokens") or 0)
        lines = self._decision(call_id)
        usage = f"[dim]{_k(p.get('input_tokens'))}→{_k(p.get('output_tokens'))} tok[/dim]"
        head = lines[0] if lines else "replied"
        self._say(f"{icon} [bold {colour}]{name}[/bold {colour}] {_escape(head)}  {usage}")
        for extra in lines[1:]:
            self._say(f"   [dim]• {_escape(extra)}[/dim]")

    def _failure(self, call_id: str) -> str:
        try:
            with sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=2) as conn:
                row = conn.execute("SELECT error_code FROM h_model_calls WHERE call_id = ?", (call_id,)).fetchone()
            code = (row[0] if row else "") or ""
        except Exception:
            code = ""
        return {
            "MODEL_QUOTA_TPM_TOO_LOW": "request too big for the provider's tokens-per-minute cap → shrinking the context to fit",
            "ROLE_SCHEMA_INVALID": "reply did not match the required JSON → asking again with the exact error",
            "MODEL_RATE_LIMITED": "provider rate limit → waiting for the next window",
        }.get(code, f"call did not complete ({code.lower() or 'retrying'})")

    def _decision(self, call_id: str) -> List[str]:
        """A short, human summary of the agent's decision from its stored (filtered) response."""
        from harness.roles.schemas import extract_json_text

        path = self.artifacts_root / "prd2" / "model" / "responses" / f"{call_id}.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))["raw_text"]
            d = json.loads(extract_json_text(raw))
        except Exception:
            return []
        kind = d.get("decision")
        if kind == "PLAN_READY":
            steps = [_clip(step.get("purpose"), 90) for step in (d.get("steps") or [])[:4] if isinstance(step, dict)]
            return [f"planned: {_clip(d.get('objective'))}", *steps]
        if kind == "NEEDS_EVIDENCE":
            queries = [f"{q.get('query_type', '').lower()}: {_clip(q.get('query'), 60)}" for q in (d.get("queries") or [])[:4]]
            return [f"is reading the code ({len(d.get('queries') or [])} lookups)", *queries]
        if kind == "NEEDS_INPUT":
            return ["needs input from you", *[_clip(q.get("question"), 90) for q in (d.get("questions") or [])[:3]]]
        if kind == "CODE":
            paths = ", ".join((d.get("declared_paths") or [])[:4])
            return [f"writes code: {_clip(d.get('purpose'), 90)}", f"files: {paths}"] if paths else [f"writes code: {_clip(d.get('purpose'))}"]
        if kind == "COMPLETE":
            return ["says the fix is complete → independent verification"]
        if kind == "REPLAN":
            return [f"asks to replan: {_clip(d.get('contradicted_assumption') or d.get('requested_change'))}"]
        if kind == "NEED_CAPABILITY":
            return [f"needs a capability: {_clip(d.get('capability'))}"]
        verdict = d.get("verdict") or d.get("decision") or d.get("opinion")
        summary = d.get("summary") or d.get("rationale")
        return [f"opinion: {verdict}" + (f" — {_clip(summary, 80)}" if summary else "")] if verdict else []
