"""Catalog of model providers Trace knows how to configure.

A :class:`ProviderSpec` is *metadata*: what a provider needs (API key? base URL?),
where its key conventionally lives, and which models to suggest. It contains no
connection logic and no library-specific details (e.g. LiteLLM routing prefixes),
so the catalog stays valid even if the adapter library is replaced.

Adding a provider is a data change: build a ProviderSpec and register it.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from trace_agent.providers.errors import UnknownProviderError


class ApiKeyRequirement(StrEnum):
    REQUIRED = "required"  # cloud APIs: refuse to start without a key
    OPTIONAL = "optional"  # self-hosted gateways that may or may not enforce auth
    NONE = "none"  # e.g. a local Ollama daemon


class ProviderSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    display_name: str
    description: str = ""
    api_key: ApiKeyRequirement
    api_key_env: str | None = Field(
        default=None, description="Conventional environment variable holding the key."
    )
    default_base_url: str | None = None
    base_url_required: bool = False
    suggested_models: tuple[str, ...] = Field(
        default=(),
        description="Suggestions only. Any model string is accepted; validity is checked by a connection test.",
    )
    supports_tool_calling: bool = Field(
        default=True,
        description="Provider-level default. Individual models may not support tools; checked at runtime.",
    )
    docs_url: str | None = None


BUILTIN_PROVIDERS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        id="gemini",
        display_name="Google Gemini",
        description="Google AI Studio (Gemini API).",
        api_key=ApiKeyRequirement.REQUIRED,
        api_key_env="GEMINI_API_KEY",
        # Live-checked 2026-11-25: gemini-2.5-* now return 404 "no longer available to new users".
        suggested_models=("gemini-3.5-flash-lite", "gemini-3-flash-preview", "gemini-3.8-flash"),
        docs_url="https://ai.google.dev/gemini-api/docs/api-key",
    ),
    ProviderSpec(
        id="groq",
        display_name="Groq",
        description="Groq Cloud, fast hosted inference of open models.",
        api_key=ApiKeyRequirement.REQUIRED,
        api_key_env="GROQ_API_KEY",
        suggested_models=("llama-3.3-70b-versatile", "openai/gpt-oss-120b"),
        docs_url="https://console.groq.com/keys",
    ),
    ProviderSpec(
        id="openrouter",
        display_name="OpenRouter",
        description="One API key for many hosted models from different vendors.",
        api_key=ApiKeyRequirement.REQUIRED,
        api_key_env="OPENROUTER_API_KEY",
        default_base_url="https://openrouter.ai/api/v1",
        suggested_models=("qwen/qwen3-coder", "openai/gpt-oss-120b"),
        docs_url="https://openrouter.ai/keys",
    ),
    ProviderSpec(
        id="ollama",
        display_name="Ollama (local)",
        description="Models running locally through Ollama. No API key, no cloud.",
        api_key=ApiKeyRequirement.NONE,
        default_base_url="http://localhost:11434",
        suggested_models=("qwen2.5-coder:7b", "llama3.1:8b"),
        docs_url="https://ollama.com/library",
    ),
    ProviderSpec(
        id="openai_compatible",
        display_name="OpenAI-compatible endpoint",
        description=(
            "Any server exposing an OpenAI-style /chat/completions API "
            "(LM Studio, vLLM, llama.cpp server, LiteLLM proxy, corporate gateways...)."
        ),
        api_key=ApiKeyRequirement.OPTIONAL,
        base_url_required=True,
    ),
)


class ProviderRegistry:
    """A mutable, ordered catalog of provider specs.

    One process-wide instance (:data:`DEFAULT_PROVIDERS`) serves the CLI and
    config validation. Tests and embedders can build an isolated registry instead.
    """

    def __init__(self, specs: Iterable[ProviderSpec] = ()) -> None:
        self._specs: dict[str, ProviderSpec] = {}
        for spec in specs:
            self.register(spec)

    def register(self, spec: ProviderSpec, *, replace: bool = False) -> None:
        if spec.id in self._specs and not replace:
            raise ValueError(f"provider '{spec.id}' is already registered")
        self._specs[spec.id] = spec

    def get(self, provider_id: str) -> ProviderSpec:
        try:
            return self._specs[provider_id]
        except KeyError:
            known = ", ".join(self._specs) or "none"
            raise UnknownProviderError(
                f"Unknown provider '{provider_id}'. Known providers: {known}.", provider=provider_id
            ) from None

    def ids(self) -> list[str]:
        return list(self._specs)

    def __contains__(self, provider_id: object) -> bool:
        return provider_id in self._specs

    def __iter__(self) -> Iterator[ProviderSpec]:
        return iter(self._specs.values())

    def __len__(self) -> int:
        return len(self._specs)


#: Process-wide registry, pre-populated with the built-in providers.
DEFAULT_PROVIDERS = ProviderRegistry(BUILTIN_PROVIDERS)
