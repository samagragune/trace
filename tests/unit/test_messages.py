import pytest
from pydantic import ValidationError

from trace_agent.core.messages import Message, Role, ToolCall, ToolDefinition


def test_constructors_produce_expected_roles():
    assert Message.system("s").role is Role.SYSTEM
    assert Message.user("u").role is Role.USER
    assert Message.tool_result("c1", "out").tool_call_id == "c1"


def test_assistant_may_have_only_tool_calls():
    msg = Message.assistant(tool_calls=(ToolCall(id="1", name="read_file"),))
    assert msg.content is None


def test_empty_assistant_message_rejected():
    with pytest.raises(ValidationError):
        Message.assistant()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"role": Role.USER, "content": "x", "tool_calls": (ToolCall(id="1", name="t"),)},
        {"role": Role.TOOL, "content": "x"},
        {"role": Role.USER, "content": "x", "tool_call_id": "1"},
        {"role": Role.USER},
    ],
)
def test_malformed_messages_rejected(kwargs):
    with pytest.raises(ValidationError):
        Message(**kwargs)


def test_compact_json_schema_drops_title_annotations_only():
    from trace_agent.core.messages import compact_json_schema

    schema = {
        "title": "Input",
        "type": "object",
        "properties": {
            "title": {"title": "Title", "type": "string"},  # a real parameter named "title" survives
            "tags": {"title": "Tags", "type": "array", "items": {"title": "Tag", "type": "string"}},
        },
        "required": ["title"],
    }
    assert compact_json_schema(schema) == {
        "type": "object",
        "properties": {"title": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}},
        "required": ["title"],
    }


def test_tool_definition_requires_object_schema():
    ToolDefinition(name="read_file", description="d", parameters={"type": "object"})
    with pytest.raises(ValidationError):
        ToolDefinition(name="read_file", description="d", parameters={"type": "string"})


@pytest.mark.parametrize("name", ["", "1abc", "has space", "x" * 65])
def test_tool_definition_rejects_bad_names(name):
    with pytest.raises(ValidationError):
        ToolDefinition(name=name, description="d", parameters={"type": "object"})
