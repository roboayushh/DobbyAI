"""
harness/renderer.py
───────────────────
Safe terminal rendering via Rich.
Strips ANSI escape sequences and disables untrusted Rich markup in
all user-supplied content (AC11 guardrail).

Never executes snippets, follows links, or displays remote images.
"""
from __future__ import annotations

import re
import textwrap
from datetime import datetime

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

from .models import IssuePage, IssueRecord, IssueSnapshot, Repository

# Strip ANSI/control sequences from untrusted text (AC11)
_ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _safe(text: str | None) -> str:
    """Strip terminal control sequences and escape Rich markup."""
    if text is None:
        return ""
    cleaned = _ANSI_RE.sub("", str(text))
    cleaned = _CTRL_RE.sub("", cleaned)
    return escape(cleaned)


class TerminalRenderer:
    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console(highlight=False, markup=False)

    # ── auth status banner ────────────────────────────────────────────────────

    def render_auth_status(self, has_token: bool) -> None:
        if has_token:
            self.console.print(
                "[bold green]●[/bold green] [green]GitHub authenticated (GITHUB_TOKEN)[/green]",
                markup=True,
            )
        else:
            self.console.print(
                "[yellow]○[/yellow] [yellow]GitHub anonymous mode "
                "(set GITHUB_TOKEN for higher rate limits)[/yellow]",
                markup=True,
            )

    # ── repository header ─────────────────────────────────────────────────────

    def render_repository(self, repo: Repository) -> None:
        self.console.print()
        table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
        table.add_column(style="bold cyan", no_wrap=True)
        table.add_column()
        table.add_row("Repository", _safe(repo.full_name))
        table.add_row("Visibility", _safe(repo.visibility))
        table.add_row("URL", _safe(repo.html_url))
        table.add_row("Default branch", _safe(repo.default_branch))
        table.add_row("Fetched at", _fmt_dt(repo.fetched_at))
        self.console.print(Panel(table, title="[bold]Repository Info[/bold]", expand=False), markup=True)

    # ── issue list ────────────────────────────────────────────────────────────

    def render_issue_list(self, page: IssuePage) -> None:
        self.console.print()
        if page.fetch_error:
            self.console.print(
                f"[bold yellow]⚠ Partial result:[/bold yellow] {_safe(page.fetch_error)}",
                markup=True,
            )

        if not page.issues:
            self.console.print("[dim]No matching issues on this page.[/dim]", markup=True)
        else:
            table = Table(
                box=box.ROUNDED,
                show_lines=False,
                padding=(0, 1),
                title="[bold]Issues[/bold]",
            )
            table.add_column("#", style="bold cyan", justify="right", no_wrap=True)
            table.add_column("State", no_wrap=True)
            table.add_column("Title")
            table.add_column("Labels", overflow="fold")
            table.add_column("Updated", no_wrap=True)

            for issue in page.issues:
                state_style = "green" if issue.state == "open" else "red"
                labels = ", ".join(_safe(l) for l in issue.labels) if issue.labels else "—"
                table.add_row(
                    str(issue.number),
                    f"[{state_style}]{_safe(issue.state)}[/{state_style}]",
                    _safe(issue.title),
                    labels,
                    _fmt_dt(issue.updated_at),
                )

            self.console.print(table, markup=True)

        # Pagination footer
        count = len(page.issues)
        status_parts = [f"Showing {count} issue(s)"]
        if page.has_more:
            status_parts.append("[bold]More pages available[/bold] (press n)")
        else:
            status_parts.append("[dim]No more pages[/dim]")
        if page.source.value == "cache":
            status_parts.append("[dim italic](cached)[/dim italic]")

        self.console.print("  " + "  ·  ".join(status_parts), markup=True)

    # ── issue detail ──────────────────────────────────────────────────────────

    def render_issue_detail(self, issue: IssueRecord, source: str = "live") -> None:
        self.console.print()
        meta_table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
        meta_table.add_column(style="bold cyan", no_wrap=True)
        meta_table.add_column()

        state_color = "green" if issue.state == "open" else "red"
        author = _safe(issue.author) if issue.author else "[dim]unknown[/dim]"
        labels = ", ".join(_safe(l) for l in issue.labels) if issue.labels else "—"
        assignees = ", ".join(_safe(a) for a in issue.assignees) if issue.assignees else "—"

        meta_table.add_row("Issue", f"[bold]#{issue.number}[/bold]  {_safe(issue.title)}")
        meta_table.add_row("State", f"[{state_color}]{_safe(issue.state)}[/{state_color}]")
        meta_table.add_row("Author", author)
        meta_table.add_row("Labels", labels)
        meta_table.add_row("Assignees", assignees)
        meta_table.add_row("Created", _fmt_dt(issue.created_at))
        meta_table.add_row("Updated", _fmt_dt(issue.updated_at))
        meta_table.add_row("Comments", str(issue.comments_count))
        meta_table.add_row("URL", _safe(issue.html_url))
        meta_table.add_row("Source", _safe(source))

        self.console.print(
            Panel(meta_table, title=f"[bold]Issue #{issue.number}[/bold]", expand=False),
            markup=True,
        )

        # Body – paginate long bodies (FR07)
        body = issue.body or "[dim](empty body)[/dim]"
        if len(body) > 3000:
            # Show first 3000 chars with continuation notice
            preview = _safe(body[:3000])
            remaining = len(body) - 3000
            self.console.print(
                Panel(
                    preview + f"\n\n[dim]… {remaining} more characters. "
                    "Full body saved in snapshot.[/dim]",
                    title="[bold]Body[/bold]",
                    expand=False,
                ),
                markup=True,
            )
        else:
            self.console.print(
                Panel(_safe(body), title="[bold]Body[/bold]", expand=False),
                markup=True,
            )

    # ── snapshot confirmation ─────────────────────────────────────────────────

    def render_snapshot_saved(self, path: str) -> None:
        self.console.print()
        self.console.print(
            f"[bold green]✓ Issue fetched and ready for later processing.[/bold green]\n"
            f"  Snapshot: [cyan]{_safe(path)}[/cyan]",
            markup=True,
        )

    # ── error messages ────────────────────────────────────────────────────────

    def render_error(self, message: str) -> None:
        self.console.print(f"[bold red]✗ Error:[/bold red] {_safe(message)}", markup=True)

    def render_warning(self, message: str) -> None:
        self.console.print(f"[yellow]⚠ {_safe(message)}[/yellow]", markup=True)

    def render_info(self, message: str) -> None:
        self.console.print(f"[dim]{_safe(message)}[/dim]", markup=True)


def _fmt_dt(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return dt.strftime("%Y-%m-%d %H:%M UTC")
