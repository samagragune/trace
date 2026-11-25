"""Turns resolved :class:`ProviderSettings` into a live :class:`ModelProvider`.

The factory maps provider ids to *adapter factories*. Phase 0 ships no network
adapter on purpose: asking for one raises :class:`ProviderNotSupportedError`
with a clear message rather than pretending to work. Phase 1 registers a
LiteLLM-backed adapter for every built-in provider.
"""

from __future__ import annotations

from collections.abc import Callable

from trace_agent.providers.base import ModelProvider, ProviderSettings
from trace_agent.providers.errors import ProviderNotSupportedError
from trace_agent.providers.registry import DEFAULT_PROVIDERS, ProviderRegistry

AdapterFactory = Callable[[ProviderSettings], ModelProvider]


class ProviderFactory:
    def __init__(self, registry: ProviderRegistry = DEFAULT_PROVIDERS) -> None:
        self._registry = registry
        self._adapters: dict[str, AdapterFactory] = {}

    def register_adapter(self, provider_id: str, factory: AdapterFactory, *, replace: bool = False) -> None:
        self._registry.get(provider_id)  # raises UnknownProviderError for typos
        if provider_id in self._adapters and not replace:
            raise ValueError(f"an adapter for provider '{provider_id}' is already registered")
        self._adapters[provider_id] = factory

    def has_adapter(self, provider_id: str) -> bool:
        return provider_id in self._adapters

    def create(self, settings: ProviderSettings) -> ModelProvider:
        spec = self._registry.get(settings.provider)
        adapter = self._adapters.get(spec.id)
        if adapter is None:
            raise ProviderNotSupportedError(
                f"Provider '{spec.display_name}' is configured, but no adapter for it is installed "
                "in this version of Trace (model connectivity arrives in Phase 1).",
                provider=spec.id,
            )
        return adapter(settings)


def _litellm_adapter(settings: ProviderSettings) -> ModelProvider:
    from trace_agent.providers.litellm_adapter import LiteLLMProvider  # lazy: litellm is slow to import

    return LiteLLMProvider(settings)


def build_default_factory(registry: ProviderRegistry = DEFAULT_PROVIDERS) -> ProviderFactory:
    from trace_agent.providers.litellm_adapter import LITELLM_PREFIXES

    factory = ProviderFactory(registry)
    for provider_id in LITELLM_PREFIXES:
        if provider_id in registry:
            factory.register_adapter(provider_id, _litellm_adapter)
    return factory


#: Process-wide factory used by the CLI, with the LiteLLM adapter for every built-in provider.
DEFAULT_FACTORY = build_default_factory()
