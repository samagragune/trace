"""Local tools the model can request, plus the registry that validates calls.

Phase 0 defines the contract only. Concrete tools (list_files, read_file,
search_code, edit_file, run_command, ...) arrive in Phases 2 to 4.
"""

from trace_agent.tools.base import Tool, ToolContext, ToolInput, ToolResult, ToolStatus
from trace_agent.tools.registry import (
    ParsedToolCall,
    ToolError,
    ToolInputError,
    ToolRegistry,
    UnknownToolError,
)

__all__ = [
    "ParsedToolCall",
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolInput",
    "ToolInputError",
    "ToolRegistry",
    "ToolResult",
    "ToolStatus",
    "UnknownToolError",
]
