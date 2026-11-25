"""Conversation messages and tool-call types in Trace's own, provider-neutral format.

Provider adapters translate between these types and each vendor's wire format.
The orchestrator only ever sees these types, which is what keeps it independent
of any specific SDK (spec: model_provider_layer.design_rule).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Tool names must be safe for every provider we target (OpenAI-style APIs cap at 64 chars).
TOOL_NAME_PATTERN = r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$"


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ToolCall(BaseModel):
    """A tool invocation *requested* by the model.

    A ToolCall is a proposal, never an authorization: it has not been validated,
    approved or executed. See safety.permissions for how it becomes an action.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1, description="Provider-assigned call id, echoed back in the tool result.")
    name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    arguments_error: str | None = Field(
        default=None,
        description=(
            "Set by an adapter when the model emitted arguments that are not valid JSON. "
            "The call is kept (not dropped) so the error can be reported back to the model."
        ),
    )
    provider_metadata: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Opaque provider data that must be echoed back unchanged on the next request "
            "(e.g. Gemini 3 thought signatures). Only the adapter interprets it."
        ),
    )


class Message(BaseModel):
    """One conversation turn. Shape rules are enforced so malformed histories fail early."""

    model_config = ConfigDict(frozen=True)

    role: Role
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None

    @model_validator(mode="after")
    def _check_shape(self) -> Message:
        if self.tool_calls and self.role is not Role.ASSISTANT:
            raise ValueError("only assistant messages may contain tool calls")
        if self.role is Role.TOOL:
            if not self.tool_call_id:
                raise ValueError("tool messages must set tool_call_id")
        elif self.tool_call_id is not None:
            raise ValueError("only tool messages may set tool_call_id")
        if self.role is Role.ASSISTANT:
            if self.content is None and not self.tool_calls:
                raise ValueError("assistant messages need content, tool calls, or both")
        elif self.content is None:
            raise ValueError(f"{self.role.value} messages require content")
        return self

    # Convenience constructors keep call sites short and intention-revealing.
    @classmethod
    def system(cls, content: str) -> Message:
        return cls(role=Role.SYSTEM, content=content)

    @classmethod
    def user(cls, content: str) -> Message:
        return cls(role=Role.USER, content=content)

    @classmethod
    def assistant(cls, content: str | None = None, tool_calls: tuple[ToolCall, ...] = ()) -> Message:
        return cls(role=Role.ASSISTANT, content=content, tool_calls=tool_calls)

    @classmethod
    def tool_result(cls, tool_call_id: str, content: str) -> Message:
        return cls(role=Role.TOOL, content=content, tool_call_id=tool_call_id)


class ToolDefinition(BaseModel):
    """What the model is told about a tool: its name, purpose and JSON-Schema parameters."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(pattern=TOOL_NAME_PATTERN)
    description: str = Field(min_length=1)
    parameters: dict[str, Any]

    @model_validator(mode="after")
    def _parameters_are_object_schema(self) -> ToolDefinition:
        if self.parameters.get("type") != "object":
            raise ValueError("tool parameters must be a JSON Schema with type 'object'")
        return self
