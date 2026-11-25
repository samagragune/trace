"""LiteLLM-backed ModelProvider: the only module in Trace that imports ``litellm``.

Responsibilities:
* translate Trace messages/tools <-> OpenAI-format dicts (LiteLLM's lingua franca),
* route to the right backend via a provider-id -> LiteLLM prefix table,
* convert *every* LiteLLM/vendor exception into a normalized ``ProviderError``,
* retry retryable errors with backoff, honoring server-provided retry delays,
* never let the API key leak through error messages.

LiteLLM is imported lazily (it is slow to import), and its import-time network fetch
of the model price list is disabled: Trace is local-first.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from typing import Any

from trace_agent.core.messages import Message, Role, ToolCall
from trace_agent.providers.base import (
    CompletionRequest,
    CompletionResponse,
    FinishReason,
    ModelCapabilities,
    ModelProvider,
    ProviderSettings,
    TokenUsage,
)
from trace_agent.providers.errors import (
    ContextWindowExceededError,
    ModelNotFoundError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from trace_agent.safety.redaction import Redactor

#: Trace provider id -> LiteLLM model prefix. Kept here, not in the registry (ADR-005).
LITELLM_PREFIXES: dict[str, str] = {
    "gemini": "gemini/",
    "groq": "groq/",
    "openrouter": "openrouter/",
    "ollama": "ollama_chat/",
    "openai_compatible": "openai/",
}

#: Never sleep longer than this between retries; longer waits surface to the user instead
#: (e.g. a daily free-tier quota: waiting would hang the CLI for hours).
MAX_RETRY_WAIT_SECONDS = 60.0

_FINISH_REASONS = {
    "stop": FinishReason.STOP,
    "tool_calls": FinishReason.TOOL_CALLS,
    "function_call": FinishReason.TOOL_CALLS,
    "length": FinishReason.LENGTH,
    "content_filter": FinishReason.CONTENT_FILTER,
}

CompletionFn = Callable[..., Any]


def _import_litellm() -> Any:
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    import litellm

    litellm.suppress_debug_info = True
    litellm.telemetry = False
    return litellm


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Read a field from either an object or a dict (LiteLLM returns both in places)."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


# --------------------------------------------------------------------------- translation


def to_openai_messages(messages: tuple[Message, ...]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for msg in messages:
        if msg.role is Role.ASSISTANT:
            item: dict[str, Any] = {"role": "assistant", "content": msg.content}
            if msg.tool_calls:
                item["tool_calls"] = [_tool_call_to_openai(c) for c in msg.tool_calls]
            out.append(item)
        elif msg.role is Role.TOOL:
            out.append({"role": "tool", "tool_call_id": msg.tool_call_id, "content": msg.content})
        else:
            out.append({"role": msg.role.value, "content": msg.content})
    return out


def _tool_call_to_openai(call: ToolCall) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": call.id,
        "type": "function",
        "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
    }
    if call.provider_metadata:
        item["provider_specific_fields"] = call.provider_metadata
    return item


def to_openai_tools(request: CompletionRequest) -> list[dict[str, Any]] | None:
    if not request.tools:
        return None
    return [
        {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
        for t in request.tools
    ]


def parse_tool_call(raw: Any, index: int) -> ToolCall:
    function = _get(raw, "function")
    name = _get(function, "name") or ""
    raw_args = _get(function, "arguments")
    arguments: dict[str, Any] = {}
    error: str | None = None
    if isinstance(raw_args, dict):
        arguments = raw_args
    elif raw_args in (None, ""):
        arguments = {}
    else:
        try:
            parsed = json.loads(raw_args)
            if isinstance(parsed, dict):
                arguments = parsed
            else:
                error = f"expected a JSON object, got {type(parsed).__name__}"
        except (json.JSONDecodeError, TypeError) as exc:
            error = str(exc)
    metadata = _get(raw, "provider_specific_fields") or None
    return ToolCall(
        id=_get(raw, "id") or f"call_{index}",
        name=name or "<missing>",
        arguments=arguments,
        arguments_error=error,
        provider_metadata=dict(metadata) if metadata else None,
    )


def parse_response(raw: Any, provider: str, model: str) -> CompletionResponse:
    choices = _get(raw, "choices") or []
    if not choices:
        raise ProviderResponseError("provider returned no choices", provider=provider)
    choice = choices[0]
    message = _get(choice, "message")
    content = _get(message, "content")
    tool_calls = tuple(parse_tool_call(tc, i) for i, tc in enumerate(_get(message, "tool_calls") or []))
    if content is not None and not isinstance(content, str):
        content = str(content)
    if content is None and not tool_calls:
        content = ""  # e.g. a reasoning model that spent its whole budget thinking
    finish = _FINISH_REASONS.get(str(_get(choice, "finish_reason") or "").lower(), FinishReason.OTHER)
    if tool_calls and finish is FinishReason.STOP:
        finish = FinishReason.TOOL_CALLS  # some providers report "stop" alongside tool calls
    usage_raw = _get(raw, "usage")
    usage = TokenUsage(
        prompt_tokens=_get(usage_raw, "prompt_tokens"),
        completion_tokens=_get(usage_raw, "completion_tokens"),
        total_tokens=_get(usage_raw, "total_tokens"),
    )
    return CompletionResponse(
        message=Message.assistant(content, tool_calls),
        finish_reason=finish,
        usage=usage,
        provider=provider,
        model=_get(raw, "model") or model,
    )


# --------------------------------------------------------------------------- error mapping

_RETRY_PATTERNS = (
    re.compile(r"retry in ([0-9.]+)\s*s", re.I),
    re.compile(r"retryDelay[\"']?\s*[:=]\s*[\"']?([0-9.]+)s", re.I),
    re.compile(r"try again in ([0-9.]+)\s*s", re.I),
)


def _retry_after(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is not None:
        try:
            value = headers.get("retry-after")
            if value is not None:
                return float(value)
        except (TypeError, ValueError):
            pass
    text = str(exc)
    for pattern in _RETRY_PATTERNS:
        match = pattern.search(text)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                continue
    return None


_JSON_MESSAGE = re.compile(r'"message"\s*:\s*"((?:[^"\\]|\\.)+)"')
_JSON_STATUS = re.compile(r'"(?:status|reason)"\s*:\s*"([A-Z_]{3,})"')
_LITELLM_PREFIX = re.compile(r"^litellm\.\w+:\s*(?:\w+Exception\s*-\s*)?")


def _short(text: str, limit: int = 600) -> str:
    """Human-readable error text: pull the vendor's own message out of JSON bodies when present."""
    text = " ".join(text.split())
    message = _JSON_MESSAGE.search(text)
    if message:
        codes = list(dict.fromkeys(_JSON_STATUS.findall(text)))  # e.g. RESOURCE_EXHAUSTED, API_KEY_INVALID
        body = message.group(1).replace('\\"', '"').replace("\\n", " ")  # JSON escapes inside the message
        text = " ".join(body.split()) + (f" [{', '.join(codes)}]" if codes else "")
    else:
        text = _LITELLM_PREFIX.sub("", text)
    return text if len(text) <= limit else text[:limit] + "..."


def normalize_error(exc: Exception, provider: str, model: str, redactor: Redactor) -> ProviderError:
    """Map a LiteLLM / vendor exception to Trace's normalized hierarchy."""
    if isinstance(exc, ProviderError):
        return exc
    litellm = _import_litellm()
    ex = litellm.exceptions
    message = redactor.text(_short(str(exc)))

    def is_(name: str) -> bool:
        cls = getattr(ex, name, None)
        return isinstance(cls, type) and isinstance(exc, cls)

    # Order matters: specific subclasses before their parents.
    if is_("ContextWindowExceededError"):
        return ContextWindowExceededError(f"request exceeds the model's context window: {message}", provider=provider)
    if is_("AuthenticationError") or is_("PermissionDeniedError"):
        return ProviderAuthError(f"authentication failed: {message}", provider=provider)
    if is_("RateLimitError"):
        return ProviderRateLimitError(f"rate limited: {message}", provider=provider, retry_after=_retry_after(exc))
    if is_("Timeout"):
        return ProviderTimeoutError(f"request timed out: {message}", provider=provider)
    if is_("NotFoundError"):
        return ModelNotFoundError(f"model '{model}' was not found: {message}", provider=provider)
    if any(is_(n) for n in ("APIConnectionError", "ServiceUnavailableError", "InternalServerError", "BadGatewayError")):
        return ProviderUnavailableError(f"provider unavailable: {message}", provider=provider)
    if is_("BadRequestError"):
        return ProviderResponseError(f"provider rejected the request: {message}", provider=provider)
    return ProviderResponseError(f"unexpected provider error ({type(exc).__name__}): {message}", provider=provider)


# --------------------------------------------------------------------------- provider


class LiteLLMProvider(ModelProvider):
    def __init__(
        self,
        settings: ProviderSettings,
        *,
        completion_fn: CompletionFn | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(settings)
        if settings.provider not in LITELLM_PREFIXES:
            raise ProviderResponseError(f"LiteLLM adapter has no route for provider '{settings.provider}'")
        self._completion_fn = completion_fn
        self._sleep = sleep
        self._redactor = Redactor([settings.api_key.get_secret_value()] if settings.api_key else [])

    @property
    def litellm_model(self) -> str:
        return LITELLM_PREFIXES[self.provider_id] + self.model

    def _completion(self) -> CompletionFn:
        if self._completion_fn is None:
            self._completion_fn = _import_litellm().completion
        return self._completion_fn

    def _kwargs(self, request: CompletionRequest) -> dict[str, Any]:
        kwargs: dict[str, Any] = dict(self.settings.options)
        kwargs.update(
            model=self.litellm_model,
            messages=to_openai_messages(request.messages),
            timeout=self.settings.timeout_seconds,
            num_retries=0,  # Trace owns retry policy
            drop_params=True,  # silently drop params a backend doesn't support
        )
        tools = to_openai_tools(request)
        if tools:
            kwargs["tools"] = tools
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if request.max_output_tokens is not None:
            kwargs["max_tokens"] = request.max_output_tokens
        if self.settings.api_key is not None:
            kwargs["api_key"] = self.settings.api_key.get_secret_value()
        if self.settings.base_url:
            kwargs["api_base"] = self.settings.base_url
        return kwargs

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        kwargs = self._kwargs(request)
        attempt = 0
        while True:
            try:
                raw = self._completion()(**kwargs)
                return parse_response(raw, self.provider_id, self.model)
            except Exception as exc:  # noqa: BLE001 - every failure must be normalized
                error = normalize_error(exc, self.provider_id, self.model, self._redactor)
                if not error.retryable or attempt >= self.settings.max_retries:
                    raise error from None
                wait = self._backoff(attempt, error)
                if wait is None:
                    raise error from None
                attempt += 1
                self._sleep(wait)

    @staticmethod
    def _backoff(attempt: int, error: ProviderError) -> float | None:
        server_wait = getattr(error, "retry_after", None)
        wait = server_wait if server_wait is not None else min(2.0**attempt, 30.0)
        return None if wait > MAX_RETRY_WAIT_SECONDS else wait

    def warm_up(self) -> None:
        self._completion()  # pays LiteLLM's multi-second import cost up front

    def check_connection(self) -> None:
        self.complete(
            CompletionRequest(messages=(Message.user("Reply with the single word OK."),), max_output_tokens=256)
        )

    def capabilities(self) -> ModelCapabilities:
        tool_calling = True
        max_context: int | None = None
        try:
            litellm = _import_litellm()
            tool_calling = bool(litellm.supports_function_calling(model=self.litellm_model))
            info = litellm.get_model_info(model=self.litellm_model)
            max_context = info.get("max_input_tokens")
        except Exception:  # noqa: BLE001 - unknown models (e.g. local) are allowed; assume tools work
            pass
        return ModelCapabilities(tool_calling=tool_calling, max_context_tokens=max_context)
