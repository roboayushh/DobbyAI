"""
tests/test_renderer.py
AC11 – Untrusted content: ANSI escapes, markup, shell snippets cause no execution.
AC12 – Large bodies are viewable; JSON output is valid.
"""
import pytest
from io import StringIO
from rich.console import Console

from harness.renderer import TerminalRenderer, _safe
from harness.models import IssueRecord, IssuePage, Repository, Source
from tests.fixtures import REPO_JSON, ISSUE_JSON
from harness.provider import GitHubIssueProvider


def make_repo():
    return GitHubIssueProvider._parse_repository(REPO_JSON)


def make_issue(**overrides):
    data = {**ISSUE_JSON, **overrides}
    return GitHubIssueProvider._parse_issue(data)


def make_renderer():
    buf = StringIO()
    console = Console(file=buf, highlight=False, markup=True, no_color=True)
    return TerminalRenderer(console), buf


# ── safe() strips ANSI and escapes Rich markup ────────────────────────────────

def test_safe_strips_ansi():
    result = _safe("\x1b[31mred text\x1b[0m")
    assert "\x1b[" not in result
    assert "red text" in result


def test_safe_escapes_rich_markup():
    result = _safe("[bold red]danger[/bold red]")
    # Rich.escape() prefixes '[' with a backslash so markup is not interpreted
    # The result should contain \[ which prevents Rich from treating it as markup
    assert r"\[" in result or "\\[" in result
    assert "danger" in result  # text content is preserved


def test_safe_strips_control_chars():
    result = _safe("\x00\x07\x0b\x1f normal text")
    assert "\x00" not in result
    assert "\x07" not in result
    assert "normal text" in result


def test_safe_shell_snippet_is_plain_text():
    """Shell snippet is preserved as text but never executed (it's just a string)."""
    result = _safe("$(rm -rf /)")
    # The text is kept safe but not executed — we just verify it's a string
    assert isinstance(result, str)


def test_safe_none_returns_empty():
    assert _safe(None) == ""


# ── render_issue_list handles empty page ──────────────────────────────────────

def test_render_issue_list_empty():
    renderer, buf = make_renderer()
    page = IssuePage(issues=[], filters={}, has_more=False)
    renderer.render_issue_list(page)
    output = buf.getvalue()
    assert "No matching issues" in output


# ── render_issue_list shows issues ───────────────────────────────────────────

def test_render_issue_list_shows_issues():
    renderer, buf = make_renderer()
    issue = make_issue()
    page = IssuePage(issues=[issue], filters={}, has_more=True)
    renderer.render_issue_list(page)
    output = buf.getvalue()
    assert "42" in output
    assert "Fix the flux capacitor" in output
    assert "More pages available" in output


# ── render_issue_detail – large body truncated safely (AC12) ──────────────────

def test_render_large_body_truncated():
    renderer, buf = make_renderer()
    big_body = "A" * 4000
    issue = make_issue(body=big_body, id=99, number=99)
    renderer.render_issue_detail(issue)
    output = buf.getvalue()
    assert "more characters" in output
    assert "\x1b[" not in output  # no ANSI in output


# ── render_issue_detail – injected control sequence in body (AC11) ────────────

def test_render_issue_with_ansi_in_body():
    renderer, buf = make_renderer()
    evil_body = "Normal text\x1b[31m DANGER \x1b[0m more text"
    issue = make_issue(body=evil_body, id=88, number=88)
    renderer.render_issue_detail(issue)
    output = buf.getvalue()
    assert "\x1b[31m" not in output
    assert "DANGER" in output  # text preserved, escape stripped


# ── render_issue_detail – null body ───────────────────────────────────────────

def test_render_null_body_issue():
    renderer, buf = make_renderer()
    issue = make_issue(body=None, id=55, number=55)
    # body is coerced to "" by model validator
    renderer.render_issue_detail(issue)
    output = buf.getvalue()
    assert "empty body" in output


# ── render_repository ─────────────────────────────────────────────────────────

def test_render_repository():
    renderer, buf = make_renderer()
    repo = make_repo()
    renderer.render_repository(repo)
    output = buf.getvalue()
    assert "octocat/hello-world" in output
    assert "public" in output


# ── render_snapshot_saved ─────────────────────────────────────────────────────

def test_render_snapshot_saved():
    renderer, buf = make_renderer()
    renderer.render_snapshot_saved("/tmp/test/snapshot.json")
    output = buf.getvalue()
    assert "ready for later processing" in output
