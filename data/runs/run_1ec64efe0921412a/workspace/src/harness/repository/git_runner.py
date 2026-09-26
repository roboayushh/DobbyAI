"""harness/repository/git_runner.py
Controlled Git subprocess execution with disabled hooks, empty config, and strict safety.
"""
from __future__ import annotations

import os
import re
import subprocess
from typing import Dict, List, Optional, Tuple


class GitCommandError(Exception):
    def __init__(self, message: str, returncode: int, stdout: str, stderr: str):
        super().__init__(f"{message} (exit {returncode}): {stderr.strip() or stdout.strip()}")
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def redact_secrets(text: str) -> str:
    """Redact tokens, credentials, and sensitive patterns from text."""
    if not text:
        return ""
    # Redact URL credentials: https://user:pass@host -> https://[REDACTED]@host
    text = re.sub(r"https?://([^@/\s]+)@", "https://[REDACTED]@", text)
    # Redact common token patterns
    text = re.sub(r"(ghp_[a-zA-Z0-9]{36}|github_pat_[a-zA-Z0-9_]{82})", "[REDACTED_TOKEN]", text)
    return text


class GitRunner:
    def __init__(self, timeout_seconds: int = 300, max_output_bytes: int = 10_000_000):
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes
        self._empty_config_file = "/dev/null"

    def _get_controlled_env(self) -> Dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin"),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": self._empty_config_file,
            "GIT_OPTIONAL_LOCKS": "0",
            "LC_ALL": "C",
        }
        return env

    def run(
        self,
        args: List[str],
        cwd: Optional[str] = None,
        check: bool = True,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> Tuple[str, str]:
        """Execute git command via argument array (shell=False)."""
        cmd = [
            "git",
            "-c", "core.hooksPath=/dev/null",
            "-c", "filter.lfs.smudge=",
            "-c", "filter.lfs.clean=",
            "-c", "filter.lfs.process=",
            "-c", "filter.lfs.required=false",
            "-c", "credential.helper=",
        ] + args

        env = self._get_controlled_env()
        if extra_env:
            env.update(extra_env)

        try:
            proc = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                env=env,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitCommandError(
                f"Git command timed out after {self.timeout_seconds}s",
                -1,
                redact_secrets(exc.stdout.decode("utf-8", errors="replace") if exc.stdout else ""),
                redact_secrets(exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""),
            )
        except Exception as exc:
            raise GitCommandError(f"Failed to execute git: {exc}", -1, "", str(exc))

        stdout = redact_secrets(
            proc.stdout[: self.max_output_bytes].decode("utf-8", errors="replace")
        )
        stderr = redact_secrets(
            proc.stderr[: self.max_output_bytes].decode("utf-8", errors="replace")
        )

        if check and proc.returncode != 0:
            raise GitCommandError(
                f"Git command failed: {' '.join(args[:3])}",
                proc.returncode,
                stdout,
                stderr,
            )

        return stdout, stderr
