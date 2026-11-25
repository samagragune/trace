"""A deterministic, offline provider that replays a script of responses.

This is real, working code, not a placeholder: it is the test double used by
unit tests, loop tests and (later) benchmark self-tests. A step can be

* a ``CompletionResponse`` returned as-is,
* a ``ProviderError`` instance, which is raised (to simulate failures), or
* a callable ``(CompletionRequest) -> CompletionResponse``, which lets a test
  react to what the agent actually sent. Phase 2's acceptance test relies on
  this to prove the next action depends on the previous tool result.

It is never registered as a user-selectable provider.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable
from itertools import count
from typing import Any

from trace_agent.core.messages import Message, ToolCall
from trace_agent.providers.base import (
    CompletionRequest,
    CompletionResponse,
    FinishReason,
    ModelCapabilities,
    ModelProvider,
    ProviderSettings,
)
from trace_agent.providers.errors import ProviderError, ProviderResponseError

ScriptStep = CompletionResponse | ProviderError | Callable[[CompletionRequest], CompletionResponse]

SCRIPTED_PROVIDER_ID = "scripted"

_call_ids = count(1)


def text_response(content: str, *, model: str = "scripted") -> CompletionResponse:
    return CompletionResponse(
        message=Message.assistant(content),
        finish_reason=FinishReason.STOP,
        provider=SCRIPTED_PROVIDER_ID,
        model=model,
    )


def tool_call_response(
    name: str, arguments: dict[str, Any] | None = None, *, call_id: str | None = None, model: str = "scripted"
) -> CompletionResponse:
    call = ToolCall(id=call_id or f"call_{next(_call_ids)}", name=name, arguments=arguments or {})
    return CompletionResponse(
        message=Message.assistant(tool_calls=(call,)),
        finish_reason=FinishReason.TOOL_CALLS,
        provider=SCRIPTED_PROVIDER_ID,
        model=model,
    )


class ScriptedProvider(ModelProvider):
    def __init__(self, steps: Iterable[ScriptStep], settings: ProviderSettings | None = None) -> None:
        super().__init__(
            settings
            or ProviderSettings(profile_name="scripted", provider=SCRIPTED_PROVIDER_ID, model="scripted")
        )
        self._steps: deque[ScriptStep] = deque(steps)
        self.requests: list[CompletionRequest] = []

    @property
    def remaining(self) -> int:
        return len(self._steps)

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.requests.append(request)
        if not self._steps:
            raise ProviderResponseError("scripted provider has no responses left", provider=self.provider_id)
        step = self._steps.popleft()
        if isinstance(step, ProviderError):
            raise step
        if isinstance(step, CompletionResponse):
            return step
        return step(request)

    def check_connection(self) -> None:
        return None

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(tool_calling=True)
