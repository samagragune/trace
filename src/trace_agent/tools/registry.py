"""Tool registry: lookup, model-facing definitions and argument validation.

The registry turns an untrusted :class:`ToolCall` from the model into a
:class:`ParsedToolCall` (known tool + validated arguments), or raises. It does
*not* authorize or execute. That is the orchestrator's job, with the safety layer.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from pydantic import ValidationError

from trace_agent.core.messages import TOOL_NAME_PATTERN, ToolCall, ToolDefinition
from trace_agent.errors import TraceError
from trace_agent.safety.permissions import PermissionLevel
from trace_agent.tools.base import Tool, ToolContext, ToolInput


class ToolError(TraceError):
    pass


class UnknownToolError(ToolError):
    pass


class ToolInputError(ToolError):
    def __init__(self, tool_name: str, message: str) -> None:
        super().__init__(f"invalid arguments for tool '{tool_name}': {message}")
        self.tool_name = tool_name


@dataclass(frozen=True)
class ParsedToolCall:
    call: ToolCall
    tool: Tool
    args: ToolInput


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        name = getattr(tool, "name", None)
        if not isinstance(name, str) or not re.fullmatch(TOOL_NAME_PATTERN, name):
            raise ToolError(f"tool {type(tool).__name__} has an invalid name: {name!r}")
        if not isinstance(getattr(tool, "permission", None), PermissionLevel):
            raise ToolError(f"tool '{name}' must declare a PermissionLevel")
        input_model = getattr(tool, "input_model", None)
        if not (isinstance(input_model, type) and issubclass(input_model, ToolInput)):
            raise ToolError(f"tool '{name}' must declare an input_model subclassing ToolInput")
        if name in self._tools:
            raise ToolError(f"tool '{name}' is already registered")
        self._tools[name] = tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError:
            raise UnknownToolError(f"unknown tool '{name}'. Available: {', '.join(self._tools) or 'none'}") from None

    def names(self) -> list[str]:
        return list(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def definitions(self, context: ToolContext | None = None) -> list[ToolDefinition]:
        """Definitions for the model, filtered to tools available in ``context``."""
        tools = self._tools.values()
        if context is not None:
            tools = [t for t in tools if t.is_available(context)]
        return [t.definition() for t in tools]

    def parse_call(self, call: ToolCall) -> ParsedToolCall:
        tool = self.get(call.name)
        if call.arguments_error:
            raise ToolInputError(call.name, f"arguments were not valid JSON ({call.arguments_error})")
        try:
            args = tool.input_model.model_validate(call.arguments)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}" for err in exc.errors()
            )
            raise ToolInputError(call.name, problems) from None
        return ParsedToolCall(call=call, tool=tool, args=args)
