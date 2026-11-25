"""Built-in tools shipped with Trace."""

from trace_agent.tools.builtin.filesystem import ListFilesTool, ReadFileTool, SearchCodeTool
from trace_agent.tools.registry import ToolRegistry


def default_registry() -> ToolRegistry:
    """The tools available to the agent in this build (read-only investigation tools)."""
    return ToolRegistry([ListFilesTool(), ReadFileTool(), SearchCodeTool()])


__all__ = ["ListFilesTool", "ReadFileTool", "SearchCodeTool", "default_registry"]
