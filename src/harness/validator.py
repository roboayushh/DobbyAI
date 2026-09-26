"""
harness/validator.py
────────────────────
InputValidator – parses and normalises GitHub repository and issue URLs.
All validation happens before any network call (FR01, AC03).

Accepted inputs:
  owner/repo
  https://github.com/owner/repo
  https://github.com/owner/repo.git
  https://github.com/owner/repo/
  https://github.com/owner/repo/issues/123
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

# ── constants ────────────────────────────────────────────────────────────────

ALLOWED_HOST = "github.com"
_SLUG_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,100}$")
_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9\-]{0,37}[A-Za-z0-9])?$")


# ── result types ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RepoRef:
    owner: str
    name: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True)
class IssueRef:
    owner: str
    repo: str
    number: int


# ── errors ───────────────────────────────────────────────────────────────────

class InputError(ValueError):
    """Raised for invalid or unsupported input (exit code 2)."""


# ── validator ─────────────────────────────────────────────────────────────────

class InputValidator:
    """Stateless validator; all methods are class-level utilities."""

    @classmethod
    def parse_repo_ref(cls, raw: str) -> RepoRef:
        """
        Normalise raw user input to a RepoRef.
        Raises InputError with a human-readable reason on failure.
        """
        raw = raw.strip().rstrip("/")
        if not raw:
            raise InputError("Repository input is empty. Use owner/repo or a GitHub URL.")

        # Detect URL
        if raw.startswith("http://") or raw.startswith("https://"):
            cls._assert_scheme(raw)
            return cls._parse_repo_from_url(raw)

        # Bare owner/repo
        parts = raw.split("/")
        if len(parts) == 2:
            owner, name = parts
            name = name.removesuffix(".git")
            cls._validate_owner(owner)
            cls._validate_repo_name(name)
            return RepoRef(owner=owner, name=name)

        raise InputError(
            f"Cannot parse {raw!r}. "
            "Accepted: owner/repo  or  https://github.com/owner/repo"
        )

    @classmethod
    def parse_issue_ref(cls, raw: str) -> IssueRef | None:
        """
        If raw looks like a direct issue URL, return IssueRef; else None.
        Raises InputError for pull-request URLs or bad issue numbers.
        """
        raw = raw.strip().rstrip("/")
        if not raw.startswith("http://") and not raw.startswith("https://"):
            return None
        cls._assert_scheme(raw)

        parsed = urlparse(raw)
        cls._assert_host(parsed.netloc)

        parts = parsed.path.strip("/").split("/")
        # Expected: owner / repo / issues / number
        if len(parts) >= 4 and parts[2] == "issues":
            owner, repo = parts[0], parts[1]
            repo = repo.removesuffix(".git")
            cls._validate_owner(owner)
            cls._validate_repo_name(repo)
            number_str = parts[3]
            if not number_str.isdigit() or int(number_str) <= 0:
                raise InputError(
                    f"Invalid issue number {number_str!r}. Must be a positive integer."
                )
            return IssueRef(owner=owner, repo=repo, number=int(number_str))

        if len(parts) >= 4 and parts[2] == "pull":
            raise InputError(
                "Pull request URLs are not supported. "
                "Provide a repository URL or an issue URL (/issues/{number})."
            )

        return None

    # ── private helpers ───────────────────────────────────────────────────────

    @classmethod
    def _parse_repo_from_url(cls, raw: str) -> RepoRef:
        parsed = urlparse(raw)
        cls._assert_host(parsed.netloc)

        # Reject embedded credentials
        if parsed.username or parsed.password:
            raise InputError(
                "URLs with embedded credentials are not supported. "
                "Use the GITHUB_TOKEN environment variable."
            )

        path = parsed.path.strip("/").removesuffix(".git").rstrip("/")
        parts = path.split("/")

        if len(parts) < 2 or not parts[0] or not parts[1]:
            raise InputError(
                f"Cannot extract owner/repo from URL {raw!r}. "
                "Expected https://github.com/owner/repo"
            )

        owner, name = parts[0], parts[1]

        # If the URL is a sub-path that isn't /issues/…, still extract repo
        cls._validate_owner(owner)
        cls._validate_repo_name(name)
        return RepoRef(owner=owner, name=name)

    @classmethod
    def _assert_host(cls, netloc: str) -> None:
        host = netloc.lower().split(":")[0]  # strip port
        if host != ALLOWED_HOST:
            raise InputError(
                f"Unsupported host {netloc!r}. Only github.com is supported in Phase 1."
            )

    @classmethod
    def _assert_scheme(cls, raw: str) -> None:
        if raw.startswith("http://"):
            raise InputError(
                "HTTP URLs are not supported. Use HTTPS (https://github.com/…)."
            )

    @classmethod
    def _validate_owner(cls, owner: str) -> None:
        if not owner or not _OWNER_RE.match(owner):
            raise InputError(
                f"Invalid GitHub owner {owner!r}. "
                "Must be 1–39 alphanumeric characters or hyphens."
            )

    @classmethod
    def _validate_repo_name(cls, name: str) -> None:
        if not name or not _SLUG_RE.match(name):
            raise InputError(
                f"Invalid repository name {name!r}. "
                "Must be 1–100 alphanumeric, dot, hyphen, or underscore characters."
            )
