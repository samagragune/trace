from pathlib import Path

import pytest
from pydantic import ValidationError

from trace_agent.core.messages import ToolCall
from trace_agent.safety.permissions import PermissionLevel
from trace_agent.tools.base import Tool, ToolContext, ToolInput, ToolResult, ToolStatus
from trace_agent.tools.registry import ToolError, ToolInputError, ToolRegistry, UnknownToolError


class EchoInput(ToolInput):
    text: str
    times: int = 1


class EchoTool(Tool):
    name = "echo"
    description = "Repeat text."
    permission = PermissionLevel.READ
    input_model = EchoInput

    def run(self, args: EchoInput, context: ToolContext) -> ToolResult:
        return ToolResult.success(args.text * args.times, length=len(args.text) * args.times)


class UnavailableTool(EchoTool):
    name = "unavailable"

    def is_available(self, context: ToolContext) -> bool:
        return False


# ---- ToolResult -------------------------------------------------------------------


def test_result_status_and_error_must_agree():
    with pytest.raises(ValidationError):
        ToolResult(status=ToolStatus.SUCCESS, error="boom")
    with pytest.raises(ValidationError):
        ToolResult(status=ToolStatus.ERROR)
    assert not ToolResult.failure(ToolStatus.TIMEOUT, "took too long").ok


def test_truncation_keeps_head_and_tail():
    out = "HEAD" + "x" * 1000 + "TAIL"
    r = ToolResult.success(out).truncated_to(100)
    assert r.truncated and r.output.startswith("HEAD") and r.output.endswith("TAIL")
    assert "omitted" in r.output
    small = ToolResult.success("short")
    assert small.truncated_to(100) is small


# ---- ToolContext --------------------------------------------------------------------


def test_context_validates_root(tmp_path):
    ToolContext(project_root=tmp_path)
    with pytest.raises(ValueError):
        ToolContext(project_root=Path("relative"))
    with pytest.raises(ValueError):
        ToolContext(project_root=tmp_path / "missing")


def test_context_cancellation(tmp_path):
    ctx = ToolContext(project_root=tmp_path)
    assert not ctx.cancelled
    ctx.cancel_event.set()
    assert ctx.cancelled


# ---- registry ----------------------------------------------------------------------


def test_definition_exposes_json_schema():
    d = EchoTool().definition()
    assert d.name == "echo"
    assert d.parameters["type"] == "object"
    assert d.parameters["required"] == ["text"]


def test_parse_and_run(tmp_path):
    reg = ToolRegistry([EchoTool()])
    parsed = reg.parse_call(ToolCall(id="1", name="echo", arguments={"text": "ab", "times": 2}))
    result = parsed.tool.run(parsed.args, ToolContext(project_root=tmp_path))
    assert result.output == "abab" and result.data == {"length": 4}


@pytest.mark.parametrize(
    "arguments",
    [{}, {"text": "a", "times": "many"}, {"text": "a", "unexpected": True}],
)
def test_invalid_arguments_rejected(arguments):
    with pytest.raises(ToolInputError):
        ToolRegistry([EchoTool()]).parse_call(ToolCall(id="1", name="echo", arguments=arguments))


def test_malformed_json_from_model_is_reported():
    call = ToolCall(id="1", name="echo", arguments_error="Expecting value: line 1")
    with pytest.raises(ToolInputError, match="not valid JSON"):
        ToolRegistry([EchoTool()]).parse_call(call)


def test_unknown_tool_and_duplicates():
    reg = ToolRegistry([EchoTool()])
    with pytest.raises(UnknownToolError):
        reg.parse_call(ToolCall(id="1", name="rm_rf"))
    with pytest.raises(ToolError):
        reg.register(EchoTool())


def test_registry_rejects_incomplete_tools():
    class NoPermission(EchoTool):
        name = "noperm"
        permission = "read"  # a plain string, not a PermissionLevel

    with pytest.raises(ToolError):
        ToolRegistry([NoPermission()])


def test_unavailable_tools_hidden_from_model(tmp_path):
    reg = ToolRegistry([EchoTool(), UnavailableTool()])
    ctx = ToolContext(project_root=tmp_path)
    assert [d.name for d in reg.definitions(ctx)] == ["echo"]
    assert len(reg.definitions()) == 2
