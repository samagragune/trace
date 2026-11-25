"""Normalized provider errors.

Adapters must translate every vendor/SDK exception into one of these classes so
the orchestrator can make decisions (retry? stop? ask the user?) without
knowing which provider is in use. ``retryable`` is the key signal.
"""

from __future__ import annotations

from trace_agent.errors import TraceError


class ProviderError(TraceError):
    """Base class for model-provider failures."""

    retryable: bool = False

    def __init__(self, message: str, *, provider: str | None = None) -> None:
        super().__init__(message)
        self.provider = provider


class ProviderAuthError(ProviderError):
    """Credentials missing, invalid, expired or lacking permission. Never retried."""


class MissingCredentialError(ProviderAuthError):
    """A provider requires an API key and none could be resolved."""


class ProviderRateLimitError(ProviderError):
    retryable = True

    def __init__(self, message: str, *, provider: str | None = None, retry_after: float | None = None) -> None:
        super().__init__(message, provider=provider)
        self.retry_after = retry_after


class ProviderTimeoutError(ProviderError):
    retryable = True


class ProviderUnavailableError(ProviderError):
    """Network failure or 5xx from the provider."""

    retryable = True


class ContextWindowExceededError(ProviderError):
    """The request exceeds the model's context length. Retrying unchanged cannot help."""


class ModelNotFoundError(ProviderError):
    """The configured model does not exist or is not available to this key."""


class ProviderResponseError(ProviderError):
    """The provider answered, but with something Trace cannot use (malformed, empty, unexpected)."""


class UnknownProviderError(ProviderError):
    """The provider id is not present in the provider registry."""


class ProviderNotSupportedError(ProviderError):
    """The provider is known, but no adapter capable of talking to it is installed."""
