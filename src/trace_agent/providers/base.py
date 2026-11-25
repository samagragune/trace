"""The provider interface the orchestrator depends on.

The orchestrator talks to *one* abstract type, :class:`ModelProvider`, using
Trace's own request/response models. Concrete adapters (LiteLLM in Phase 1,
anything else later) live behind it and are chosen by the factory.

Design note: the interface is synchronous. The agent loop is strictly
sequential (one model call, then tools, then the next call), and tools run
subprocesses that are blocking anyway, so async would add complexity without
adding throughput. See docs/DECISIONS.md, ADR-004.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from trace_agent.core.messages import Message, Role, ToolCall, ToolDefinition


class FinishReason(StrEnum):
    STOP = "stop"
    TOOL_CALLS = "tool_calls"
    LENGTH = "length"
    CONTENT_FILTER = "content_filter"
    OTHER = "other"


class TokenUsage(BaseModel):
    """Token accounting. Every field is optional because not every provider reports it."""

    model_config = ConfigDict(frozen=True)

    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)


class CompletionRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    messages: tuple[Message, ...] = Field(min_length=1)
    tools: tuple[ToolDefinition, ...] = ()
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_output_tokens: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _unique_tool_names(self) -> CompletionRequest:
        names = [tool.name for tool in self.tools]
        if len(names) != len(set(names)):
            raise ValueError("tool names in a request must be unique")
        return self


class CompletionResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    message: Message
    finish_reason: FinishReason
    usage: TokenUsage = TokenUsage()
    provider: str
    model: str

    @model_validator(mode="after")
    def _assistant_only(self) -> CompletionResponse:
        if self.message.role is not Role.ASSISTANT:
            raise ValueError("a completion response must contain an assistant message")
        return self

    @property
    def tool_calls(self) -> tuple[ToolCall, ...]:
        return self.message.tool_calls


class ModelCapabilities(BaseModel):
    model_config = ConfigDict(frozen=True)

    tool_calling: bool
    max_context_tokens: int | None = None


class ProviderSettings(BaseModel):
    """Fully resolved runtime settings for one provider connection.

    Built from a persisted profile plus a resolved credential (see
    ``storage.resolve``). The API key is a ``SecretStr`` so it never appears in
    ``repr()``, logs or tracebacks by accident.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_name: str
    provider: str
    model: str = Field(min_length=1)
    base_url: str | None = None
    api_key: SecretStr | None = None
    timeout_seconds: float = Field(default=60.0, gt=0)
    max_retries: int = Field(default=3, ge=0)
    options: dict[str, Any] = Field(default_factory=dict)


class ModelProvider(ABC):
    """Abstract model connection. One instance is bound to one provider + model."""

    def __init__(self, settings: ProviderSettings) -> None:
        self.settings = settings

    @property
    def provider_id(self) -> str:
        return self.settings.provider

    @property
    def model(self) -> str:
        return self.settings.model

    @abstractmethod
    def complete(self, request: CompletionRequest) -> CompletionResponse:
        """Send one request and return the model's reply.

        Implementations must raise a ``ProviderError`` subclass on failure and
        never leak vendor-specific exceptions.
        """

    def warm_up(self) -> None:
        """Do any slow one-time local setup (e.g. heavy imports) so latency measurements are honest."""
        return None

    @abstractmethod
    def check_connection(self) -> None:
        """Make a minimal real request to confirm credentials and model work.

        Raises a ``ProviderError`` subclass on failure; returns ``None`` on success.
        """

    def capabilities(self) -> ModelCapabilities:
        """Default: trust the provider-level spec. Adapters should refine per model."""
        from trace_agent.providers.registry import DEFAULT_PROVIDERS

        spec = DEFAULT_PROVIDERS.get(self.provider_id)
        return ModelCapabilities(tool_calling=spec.supports_tool_calling)
