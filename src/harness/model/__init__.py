"""Trusted model configuration and built-in adapters."""

from .adapter import (
    FakeModelAdapter,
    ModelAdapter,
    ModelAdapterError,
    ModelAdapterResponse,
    ModelCallRequest,
    OpenAICompatibleModelAdapter,
    RecordedModelAdapter,
)
from .credentials import CredentialProvider, ModelAuthMissingError
from .config_store import ModelConfigConflictError, ModelConfigStore
from .profile import ModelProfileError, ModelProfileResolver, ResolvedProfile
from .token_counter import TokenCount, TokenCounter

__all__ = [
    "CredentialProvider",
    "FakeModelAdapter",
    "ModelAdapter",
    "ModelAdapterError",
    "ModelAdapterResponse",
    "ModelAuthMissingError",
    "ModelConfigConflictError",
    "ModelConfigStore",
    "ModelCallRequest",
    "ModelProfileError",
    "ModelProfileResolver",
    "OpenAICompatibleModelAdapter",
    "RecordedModelAdapter",
    "ResolvedProfile",
    "TokenCount",
    "TokenCounter",
]
