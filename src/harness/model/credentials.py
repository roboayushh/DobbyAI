"""Late-bound access to the only model credential accepted by the harness."""
from __future__ import annotations

import os
from typing import Mapping, Optional


class ModelAuthMissingError(RuntimeError):
    code = "MODEL_AUTH_MISSING"
    retryable = False


class CredentialProvider:
    """Read AI_API_KEY only at the live request boundary.

    The value is intentionally never retained on this object.
    """

    ENV_NAME = "AI_API_KEY"

    def __init__(self, environ: Optional[Mapping[str, str]] = None) -> None:
        self._environ = environ

    def get_ai_api_key(self) -> str:
        source = self._environ if self._environ is not None else os.environ
        value = source.get(self.ENV_NAME, "").strip()
        if not value:
            raise ModelAuthMissingError("AI_API_KEY is required for a live model request")
        return value

