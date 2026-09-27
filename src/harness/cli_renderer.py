"""harness/cli_renderer.py
ResultRenderer – provides pure JSON formatting for machine output and safe, Rich-formatted
terminal displays for human inspection.
"""
from __future__ import annotations

import json
import re
import sys
from typing import Any, Dict, List, Optional

from rich import box
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from harness.contracts import (
    ErrorResultV1,
    PreparedRunResultV1,
)

# Strips ANSI escape, OSC, C0/C1 controls, and bidi override characters (FND-017, AT-013)
_ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(\x07|\x1b\\)")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x80-\x9f]")
_BIDI_RE = re.compile(r"[\u202a-\u202e\u2066-\u2069]")


def sanitize_terminal_text(text: Optional[str]) -> str:
    """Sanitize untrusted text for terminal display."""
    if text is None:
        return ""
    text = str(text)
    text = _OSC_RE.sub("", text)
    text = _ANSI_RE.sub("", text)
    text = _CTRL_RE.sub("", text)
    text = _BIDI_RE.sub("", text)
    return escape(text)


class ResultRenderer:
    def __init__(self, console: Optional[Console] = None):
        self.console = console or Console(file=sys.stderr, highlight=False, markup=True)
        self.stdout_console = Console(file=sys.stdout, highlight=False, markup=False)

    def render_json(self, result: Any) -> None:
        """Output strictly valid JSON to stdout with no extra text or ANSI codes."""
        if hasattr(result, "model_dump_json"):
            json_str = result.model_dump_json(indent=2)
        elif isinstance(result, (dict, list)):
            json_str = json.dumps(result, indent=2, sort_keys=True)
        else:
            json_str = json.dumps(result, indent=2)
        print(json_str, file=sys.stdout)

    def render_terminal(self, result: PreparedRunResultV1) -> None:
        """Render PreparedRunResultV1 for terminal display."""
        table = Table(box=box.ROUNDED, show_header=False, padding=(0, 1))
        table.add_column(style="bold cyan", no_wrap=True)
        table.add_column()

        table.add_row("Run ID", result.run_id)
        table.add_row("Status", f"[bold green]{result.status}[/bold green]")
        table.add_row("Task Mode", result.task_mode.value)
        table.add_row("Execution Mode", result.execution_mode.value)
        table.add_row("Upstream Commit", result.source.upstream_commit or "(none)")
        table.add_row("Baseline Commit", result.source.baseline_commit)
        table.add_row("Content Tree SHA", result.source.content_tree_sha256[:16] + "...")
        table.add_row("Workspace Root", result.workspace.relative_root)
        table.add_row("Prepared Tasks", str(len(result.tasks)))
        table.add_row(
            "Queue Summary",
            f"selected={result.queue_summary.selected}, discovered={result.queue_summary.discovered}, remaining={result.queue_summary.remaining}",
        )

        panel = Panel(
            table,
            title="[bold blue]Harness Run Prepared[/bold blue]",
            subtitle="[yellow]Preparation is complete. Agent orchestration begins in PRD 2.[/yellow]",
            expand=False,
        )
        self.console.print(panel)

    def render_inspect(
        self,
        run: Dict[str, Any],
        source: Optional[Dict[str, Any]],
        workspace: Optional[Dict[str, Any]],
        tasks: List[Dict[str, Any]],
        artifacts: List[Dict[str, Any]],
        events: List[Dict[str, Any]],
    ) -> None:
        """Render full run details for inspect command."""
        self.console.print(f"\n[bold green]=== Run Inspect: {run['run_id']} ===[/bold green]")
        self.console.print(f"State: [bold]{run['state']}[/bold]")
        self.console.print(f"Task Mode: {run['task_mode']} | Execution Mode: {run['execution_mode']}")
        self.console.print(f"Idempotency Key: {run['idempotency_key']}")
        self.console.print(f"Created: {run['created_at']}")

        if source:
            self.console.print("\n[bold cyan]Source Identity:[/bold cyan]")
            self.console.print(f"  Kind: {source['source_kind']}")
            self.console.print(f"  Locator: {sanitize_terminal_text(source['canonical_locator'])}")
            self.console.print(f"  Upstream Commit: {source['upstream_commit']}")
            self.console.print(f"  Baseline Commit: {source['baseline_commit']}")
            self.console.print(f"  Content Tree SHA256: {source['content_tree_sha256']}")
            self.console.print(f"  Dirty Imported: {bool(source['dirty_source_imported'])}")

        if workspace:
            self.console.print("\n[bold cyan]Workspace:[/bold cyan]")
            self.console.print(f"  Workspace ID: {workspace['workspace_id']}")
            self.console.print(f"  Worktree Relpath: {workspace['worktree_relpath']}")
            self.console.print(f"  State: {workspace['state']}")
            self.console.print(f"  Writable: {bool(workspace['writable'])}")

        if tasks:
            self.console.print(f"\n[bold cyan]Tasks ({len(tasks)}):[/bold cyan]")
            for t in tasks:
                self.console.print(
                    f"  [{t['ordinal']}] {t['task_id']}: {sanitize_terminal_text(t['source_key'])} (state: {t['state']})"
                )

        if artifacts:
            self.console.print(f"\n[bold cyan]Artifacts ({len(artifacts)}):[/bold cyan]")
            for a in artifacts:
                self.console.print(f"  {a['kind']}: {a['relative_path']} (SHA: {a['sha256'][:12]}...)")

        if events:
            self.console.print(f"\n[bold cyan]Events ({len(events)}):[/bold cyan]")
            for e in events:
                self.console.print(
                    f"  seq={e['seq']} {e['event_type']} ({e['from_state']} -> {e['to_state']})"
                )
