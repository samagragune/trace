"""Built-in tools shipped with Trace.

Read-only investigation tools (Builds 002-003) plus, from Build 004, tools that change the
project or run commands. Every one of them is authorized per call by the executor.
"""

from trace_agent.tools.builtin.context_tools import GetProjectInfoTool, InspectLogsTool, ParseStackTraceTool
from trace_agent.tools.builtin.editing import ApplyPatchTool, CreateFileTool, DeleteFileTool, EditFileTool
from trace_agent.tools.builtin.execution import InspectGitTool, RunCommandTool, RunTestsTool
from trace_agent.tools.builtin.filesystem import ListFilesTool, ReadFileTool, SearchCodeTool
from trace_agent.tools.registry import ToolRegistry

READ_ONLY_TOOLS = (GetProjectInfoTool, ListFilesTool, ReadFileTool, SearchCodeTool, ParseStackTraceTool, InspectLogsTool, InspectGitTool)
CHANGE_TOOLS = (EditFileTool, ApplyPatchTool, CreateFileTool, DeleteFileTool, RunTestsTool, RunCommandTool)


def default_registry(*, read_only: bool = False) -> ToolRegistry:
    """All built-in tools; ``read_only=True`` gives the investigation-only set."""
    tools = READ_ONLY_TOOLS if read_only else READ_ONLY_TOOLS + CHANGE_TOOLS
    return ToolRegistry(tool() for tool in tools)


__all__ = [
    "CHANGE_TOOLS",
    "READ_ONLY_TOOLS",
    "ApplyPatchTool",
    "CreateFileTool",
    "DeleteFileTool",
    "EditFileTool",
    "GetProjectInfoTool",
    "InspectGitTool",
    "InspectLogsTool",
    "ListFilesTool",
    "ParseStackTraceTool",
    "ReadFileTool",
    "RunCommandTool",
    "RunTestsTool",
    "SearchCodeTool",
    "default_registry",
]
