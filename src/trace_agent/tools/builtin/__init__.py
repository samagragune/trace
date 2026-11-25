"""Built-in tools shipped with Trace. All are read-only in this build."""

from trace_agent.tools.builtin.context_tools import GetProjectInfoTool, InspectLogsTool, ParseStackTraceTool
from trace_agent.tools.builtin.filesystem import ListFilesTool, ReadFileTool, SearchCodeTool
from trace_agent.tools.registry import ToolRegistry


def default_registry() -> ToolRegistry:
    """The tools available to the agent in this build (read-only investigation tools)."""
    return ToolRegistry(
        [
            GetProjectInfoTool(),
            ListFilesTool(),
            ReadFileTool(),
            SearchCodeTool(),
            ParseStackTraceTool(),
            InspectLogsTool(),
        ]
    )


__all__ = [
    "GetProjectInfoTool",
    "InspectLogsTool",
    "ListFilesTool",
    "ParseStackTraceTool",
    "ReadFileTool",
    "SearchCodeTool",
    "default_registry",
]
