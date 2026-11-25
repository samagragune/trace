"""Provider-neutral data types shared by several modules.

``core`` has no dependencies on any other Trace package. It exists so that
``agent``, ``providers`` and ``tools`` can share a vocabulary (messages, tool
calls, tool definitions) without importing each other.
"""

from trace_agent.core.messages import Message, Role, ToolCall, ToolDefinition

__all__ = ["Message", "Role", "ToolCall", "ToolDefinition"]
