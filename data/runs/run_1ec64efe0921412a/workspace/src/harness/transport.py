"""
harness/transport.py
────────────────────
GitHubTransport – HTTPS-only, auth-injecting, retry-capable HTTP client.
Maps HTTP status codes to structured exceptions (AC08, AC09, guardrails).

Security rules enforced here:
  • Only requests to api.github.com are permitted.
  • TLS verification is always enabled.
  • Authorization header is never logged or raised in exceptions.
  • Response size is capped at config.max_response_bytes (5 MiB default).
  • Retry on 5xx / network errors with jitter; never retry 4xx except 429.
"""
from __future__ import annotations

import logging
import random
import time
from enum import Enum

import httpx

from .config import HarnessConfig, get_config

logger = logging.getLogger(__name__)

ALLOWED_API_HOST = "api.github.com"


# ── error hierarchy ───────────────────────────────────────────────────────────

class HarnessError(Exception):
    """Base for all harness errors."""
    exit_code: int = 1


class InputError(HarnessError):
    """Bad user input (exit 2)."""
    exit_code = 2


class AuthError(HarnessError):
    """401 – invalid / expired credentials (exit 3)."""
    exit_code = 3


class AccessError(HarnessError):
    """403 / 404 – permission denied or not found (exit 3)."""
    exit_code = 3


class RateLimitError(HarnessError):
    """429 or X-RateLimit-Remaining == 0 (exit 4); includes retry_after."""
    exit_code = 4

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class NetworkError(HarnessError):
    """Temporary network or 5xx failure (exit 4)."""
    exit_code = 4


class OversizedResponseError(HarnessError):
    """Response exceeded configured size limit (exit 4)."""
    exit_code = 4


class CancelledError(HarnessError):
    """User cancelled (exit 130)."""
    exit_code = 130


# ── transport ────────────────────────────────────────────────────────────────

class GitHubTransport:
    """
    Thin wrapper over httpx.Client that adds:
    - Auth headers (redacted from all logs)
    - Host allowlist enforcement
    - Response size cap
    - Retry with jitter for transient errors
    - Structured error classification
    """

    def __init__(self, config: HarnessConfig | None = None) -> None:
        self._cfg = config or get_config()
        self._client = httpx.Client(
            base_url=f"https://{ALLOWED_API_HOST}",
            headers={
                "Accept": self._cfg.github_accept,
                "X-GitHub-Api-Version": self._cfg.github_api_version,
                "User-Agent": "ai-coding-harness/0.1",
                **self._cfg.auth_header(),
            },
            timeout=self._cfg.request_timeout_s,
            verify=True,  # always verify TLS
        )

    # ── public API ────────────────────────────────────────────────────────────

    def get_json(self, path: str, params: dict | None = None) -> tuple[dict | list, httpx.Headers]:
        """
        GET path on api.github.com.
        Returns (parsed_json, response_headers).
        Never passes user-supplied URLs directly to the HTTP client.
        """
        self._assert_safe_path(path)
        attempt = 0
        last_exc: Exception | None = None

        while attempt <= self._cfg.max_retries:
            try:
                response = self._client.get(path, params=params)
                self._check_size(response)
                return self._parse_response(response)
            except (NetworkError, httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc
                attempt += 1
                if attempt <= self._cfg.max_retries:
                    sleep = self._jitter(attempt)
                    logger.debug("Retry %d/%d after %.1fs", attempt, self._cfg.max_retries, sleep)
                    time.sleep(sleep)
            except (AuthError, AccessError, RateLimitError, OversizedResponseError):
                raise  # do not retry these
            except KeyboardInterrupt:
                raise CancelledError("Operation cancelled by user.")

        raise NetworkError(f"Request failed after {self._cfg.max_retries} retries: {last_exc}")

    def get_page_from_url(self, url: str, params: dict | None = None) -> tuple[dict | list, httpx.Headers]:
        """
        Follow a validated pagination URL (must be on api.github.com).
        """
        self._assert_full_url_allowed(url)
        # Extract path + query and re-issue through controlled client
        from urllib.parse import urlparse, parse_qs, urlencode
        parsed = urlparse(url)
        path = parsed.path
        qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        if params:
            qs.update(params)
        return self.get_json(path, params=qs or None)

    def close(self) -> None:
        self._client.close()

    # ── helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _assert_safe_path(path: str) -> None:
        """Reject path traversal attempts."""
        if ".." in path or not path.startswith("/"):
            raise InputError(f"Unsafe API path rejected: {path!r}")

    @staticmethod
    def _assert_full_url_allowed(url: str) -> None:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        host = parsed.netloc.lower().split(":")[0]
        if host != ALLOWED_API_HOST:
            raise AccessError(
                f"Pagination redirect to {parsed.netloc!r} is not permitted. "
                f"Only {ALLOWED_API_HOST} is allowed."
            )

    def _check_size(self, response: httpx.Response) -> None:
        content_length = response.headers.get("content-length")
        if content_length and int(content_length) > self._cfg.max_response_bytes:
            raise OversizedResponseError(
                f"Response size {content_length} bytes exceeds "
                f"{self._cfg.max_response_bytes // (1024*1024)} MiB limit."
            )
        if len(response.content) > self._cfg.max_response_bytes:
            raise OversizedResponseError(
                f"Response body {len(response.content)} bytes exceeds "
                f"{self._cfg.max_response_bytes // (1024*1024)} MiB limit."
            )

    def _parse_response(self, response: httpx.Response) -> tuple[dict | list, httpx.Headers]:
        """Classify the response; never include auth values in error messages."""
        status = response.status_code

        if status == 200:
            try:
                return response.json(), response.headers
            except Exception as exc:
                raise NetworkError(f"JSON decode failed: {exc}") from exc

        if status == 401:
            raise AuthError(
                "GitHub returned 401 Unauthorized. "
                "Your GITHUB_TOKEN may be invalid or expired. "
                "Unset it to try anonymous access."
            )

        if status == 403:
            # Distinguish rate-limit-induced 403 from permission denial
            remaining = response.headers.get("x-ratelimit-remaining", "1")
            if remaining == "0":
                retry_after = self._parse_retry_after(response)
                raise RateLimitError(
                    "GitHub rate limit exhausted (403). "
                    f"Reset in approximately {retry_after:.0f}s." if retry_after else
                    "GitHub rate limit exhausted (403).",
                    retry_after=retry_after,
                )
            raise AccessError(
                "GitHub returned 403 Forbidden. "
                "You may not have permission to access this repository."
            )

        if status == 404:
            raise AccessError(
                "GitHub returned 404. "
                "The repository or issue is not found or is not accessible "
                "with your current credentials."
            )

        if status == 429:
            retry_after = self._parse_retry_after(response)
            raise RateLimitError(
                "GitHub rate limit exceeded (429)."
                + (f" Retry after {retry_after:.0f}s." if retry_after else ""),
                retry_after=retry_after,
            )

        if 500 <= status < 600:
            raise NetworkError(f"GitHub API returned server error {status}.")

        raise NetworkError(f"Unexpected HTTP {status} from GitHub API.")

    @staticmethod
    def _parse_retry_after(response: httpx.Response) -> float | None:
        """Parse Retry-After or X-RateLimit-Reset into seconds from now."""
        ra = response.headers.get("retry-after")
        if ra and ra.isdigit():
            return float(ra)
        reset = response.headers.get("x-ratelimit-reset")
        if reset and reset.isdigit():
            import time as _time
            return max(0.0, float(reset) - _time.time())
        return None

    @staticmethod
    def _jitter(attempt: int) -> float:
        base = 2 ** attempt  # 2s, 4s
        return base + random.uniform(0, 1)
