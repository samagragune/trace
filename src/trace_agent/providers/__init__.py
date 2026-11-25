"""Model connectivity behind a provider-neutral interface.

Public surface:

* :class:`ModelProvider` - the abstract interface the orchestrator uses.
* :class:`ProviderSettings` - resolved runtime settings (incl. secret key).
* :data:`DEFAULT_PROVIDERS` / :class:`ProviderSpec` - catalog of configurable providers.
* :data:`DEFAULT_FACTORY` - builds a ModelProvider from settings.
"""

from trace_agent.providers.base import (
    CompletionRequest,
    CompletionResponse,
    FinishReason,
    ModelCapabilities,
    ModelProvider,
    ProviderSettings,
    TokenUsage,
)
from trace_agent.providers.factory import DEFAULT_FACTORY, ProviderFactory
from trace_agent.providers.registry import (
    BUILTIN_PROVIDERS,
    DEFAULT_PROVIDERS,
    ApiKeyRequirement,
    ProviderRegistry,
    ProviderSpec,
)

__all__ = [
    "BUILTIN_PROVIDERS",
    "DEFAULT_FACTORY",
    "DEFAULT_PROVIDERS",
    "ApiKeyRequirement",
    "CompletionRequest",
    "CompletionResponse",
    "FinishReason",
    "ModelCapabilities",
    "ModelProvider",
    "ProviderFactory",
    "ProviderRegistry",
    "ProviderSettings",
    "ProviderSpec",
    "TokenUsage",
]
