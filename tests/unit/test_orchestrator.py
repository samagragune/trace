"""Loop tests with a scripted model. Includes the Phase 2 acceptance test."""

from pathlib import Path

import pytest

from trace_agent.agent.executor import AllowAllApprover, DenyAllApprover
from trace_agent.agent.orchestrator import FINISH_TOOL_NAME, Orchestrator, OrchestratorSettings
from trace_agent.agent.state import ActionStage, StopReason, TaskStatus
from trace_agent.core.messages import Role
from trace_agent.observability.events import EventType, InMemoryEventSink
from trace_agent.providers.errors import ProviderUnavailableError
from trace_agent.providers.scripted import ScriptedProvider, text_response, tool_call_response
from trace_agent.safety.permissions import ExecutionMode, PermissionLevel
from trace_agent.tools.base import Tool, ToolContext, ToolInput, ToolResult
from trace_agent.tools.builtin import default_registry
from trace_agent.tools.registry import ToolRegistry


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "app").mkdir(parents=True)
    (root / "app" / "forms.py").write_text("def register(data):\n    return data['email'].lower()\n")
    (root / "app" / "views.py").write_text("from app.forms import register\n")
    return root


def finish(outcome="diagnosed", **extra):
    return tool_call_response(FINISH_TOOL_NAME, {"outcome": outcome, "summary": "done", **extra})


def last_tool_output(request):
    return [m for m in request.messages if m.role is Role.TOOL][-1].content


def run(steps, repo, registry=None, **settings):
    provider = ScriptedProvider(steps)
    sink = InMemoryEventSink()
    orch = Orchestrator(
        provider, registry or default_registry(), settings=OrchestratorSettings(**settings), sink=sink
    )
    return orch.run("registration crashes", repo), provider, sink


# ---- Phase 2 acceptance test --------------------------------------------------------------


def test_next_action_depends_on_previous_tool_result(repo):
    """The model's 2nd and 3rd calls are computed from what tools actually returned.

    If the loop forwarded anything other than real observations, these steps could not
    produce a valid path, and the task would not finish with the right evidence.
    """

    def step2(request):
        found = last_tool_output(request)  # "status: success\noutput:\napp/forms.py:1: def register..."
        path = found.splitlines()[2].split(":")[0]  # first match line -> "app/forms.py"
        return tool_call_response("read_file", {"path": path})

    def step3(request):
        content = last_tool_output(request)  # output of read_file
        line = next(l for l in content.splitlines() if "data['email']" in l)
        return finish(root_cause="KeyError on missing email", evidence=[f"app/forms.py:{line.split('|')[0].strip()}"])

    task, provider, sink = run([tool_call_response("search_code", {"query": "def register"}), step2, step3], repo)

    assert task.status is TaskStatus.COMPLETED and task.stop_reason is StopReason.DIAGNOSED
    assert [a.call.name for a in task.actions] == ["search_code", "read_file"]
    assert task.actions[1].call.arguments == {"path": "app/forms.py"}
    assert task.report["evidence"] == ["app/forms.py:2"]
    assert task.model_calls == 3 and provider.remaining == 0
    assert [e.type for e in sink.events][0] is EventType.TASK_STARTED
    assert sink.events[-1].type is EventType.TASK_FINISHED


# ---- loop behavior -------------------------------------------------------------------------


def test_tool_errors_are_reported_back_not_raised(repo):
    def check(request):
        out = last_tool_output(request)
        assert "status: error" in out and "file not found" in out
        return finish()

    task, _, _ = run([tool_call_response("read_file", {"path": "nope.py"}), check], repo)
    assert task.status is TaskStatus.COMPLETED
    assert task.actions[0].stage is ActionStage.FAILED


def test_unknown_tool_and_bad_arguments_are_reported(repo):
    def check(request):
        assert "unknown tool" in last_tool_output(request)
        return tool_call_response("read_file", {"wrong": 1})

    def check2(request):
        assert "invalid arguments" in last_tool_output(request)
        return finish()

    task, _, _ = run([tool_call_response("delete_everything", {}), check, check2], repo)
    assert task.status is TaskStatus.COMPLETED
    assert all(a.stage is ActionStage.FAILED for a in task.actions)


def test_unverified_fixed_claim_is_rejected(repo):
    def check(request):
        assert "rejected" in last_tool_output(request)
        return finish("diagnosed")

    task, _, sink = run([finish("fixed"), check], repo)
    assert task.stop_reason is StopReason.DIAGNOSED
    assert any(e.payload.get("decision") == "finish_rejected" for e in sink.of_type(EventType.AGENT_DECISION))


def test_invalid_finish_arguments_rejected(repo):
    task, _, _ = run([tool_call_response(FINISH_TOOL_NAME, {"outcome": "victory"}), finish()], repo)
    assert task.status is TaskStatus.COMPLETED and task.model_calls == 2


def test_finish_mixed_with_other_calls_is_deferred(repo):
    from trace_agent.core.messages import Message, ToolCall
    from trace_agent.providers.base import CompletionResponse, FinishReason

    both = CompletionResponse(
        message=Message.assistant(
            tool_calls=(
                ToolCall(id="a", name="list_files", arguments={}),
                ToolCall(id="b", name=FINISH_TOOL_NAME, arguments={"outcome": "diagnosed", "summary": "s"}),
            )
        ),
        finish_reason=FinishReason.TOOL_CALLS,
        provider="scripted",
        model="m",
    )
    task, provider, _ = run([both, finish()], repo)
    assert task.model_calls == 2
    tool_msgs = [m for m in provider.requests[1].messages if m.role is Role.TOOL]
    assert [m.tool_call_id for m in tool_msgs] == ["a", "b"]  # every call got a result


def test_iteration_limit(repo):
    steps = [tool_call_response("list_files", {"directory": d}) for d in (".", "app", ".")]
    task, _, _ = run(steps, repo, max_iterations=3)
    assert task.stop_reason is StopReason.ITERATION_LIMIT and task.status is TaskStatus.STOPPED


def test_repeated_identical_calls_warn_then_stop(repo):
    same = lambda: tool_call_response("list_files", {"directory": "app"})  # noqa: E731
    seen = []

    def watch(request):
        seen.append(last_tool_output(request))
        return same()

    task, _, _ = run([same(), watch, watch, watch, watch], repo, max_repeated_calls=4)
    assert task.stop_reason is StopReason.NO_PROGRESS
    # Calls 1-2 are normal, call 3 carries a warning, call 4 stops the task.
    assert "warning from Trace" not in seen[0] and "warning from Trace" not in seen[1]
    assert "made 3 times" in seen[2]
    assert task.model_calls == 4 and len(task.actions) == 4


def test_text_only_replies_are_nudged_then_stopped(repo):
    task, provider, _ = run([text_response("hmm"), text_response("still thinking"), text_response("I give up")], repo)
    assert task.stop_reason is StopReason.NO_PROGRESS and task.summary == "I give up"
    assert "no tool call" in provider.requests[1].messages[-1].content


def test_nudge_then_recovery(repo):
    task, _, _ = run([text_response("let me think"), finish()], repo)
    assert task.status is TaskStatus.COMPLETED


def test_provider_failure_ends_task_cleanly(repo):
    task, _, sink = run([ProviderUnavailableError("503")], repo)
    assert task.stop_reason is StopReason.PROVIDER_UNAVAILABLE and task.status is TaskStatus.FAILED
    assert sink.of_type(EventType.ERROR)


def test_keyboard_interrupt_cancels(repo):
    def interrupt(_):
        raise KeyboardInterrupt

    task, _, _ = run([interrupt], repo)
    assert task.status is TaskStatus.CANCELLED


def test_tools_and_finish_sent_to_model(repo):
    _, provider, _ = run([finish()], repo)
    names = [t.name for t in provider.requests[0].tools]
    # Only tools that work here are offered: no checkpoint store -> no editing tools; not a git repo -> no inspect_git.
    assert names == [
        "get_project_info", "list_files", "read_file", "search_code", "parse_stack_trace", "inspect_logs",
        "run_tests", "run_command", "record_hypothesis", FINISH_TOOL_NAME,
    ]
    first_user = provider.requests[0].messages[1].content
    assert "registration crashes" in first_user and "read-only" in first_user


def test_reserved_finish_name():
    class Bad(Tool):
        name = FINISH_TOOL_NAME
        description = "x"
        permission = PermissionLevel.READ
        input_model = ToolInput

        def run(self, args, context):
            return ToolResult.success()

    with pytest.raises(ValueError):
        Orchestrator(ScriptedProvider([]), ToolRegistry([Bad()]))


# ---- Phase 3: context, hypotheses, redaction -------------------------------------------------


def test_opening_context_is_sent_and_recorded(repo):
    _, provider, sink = run([finish()], repo)
    first_user = provider.requests[0].messages[1].content
    assert "## Project overview (detected by Trace)" in first_user and "Python" in first_user
    [collected] = sink.of_type(EventType.CONTEXT_COLLECTED)
    assert collected.payload["languages"] == ["Python"]


def test_context_collection_can_be_disabled(repo):
    _, provider, sink = run([finish()], repo, collect_context=False)
    assert "Project overview" not in provider.requests[0].messages[1].content
    assert not sink.of_type(EventType.CONTEXT_COLLECTED)


def test_hypotheses_are_recorded_revised_and_reported(repo):
    from trace_agent.agent.orchestrator import HYPOTHESIS_TOOL_NAME
    from trace_agent.agent.state import HypothesisStatus

    steps = [
        tool_call_response(HYPOTHESIS_TOOL_NAME, {"statement": "email missing from form data"}),
        tool_call_response(HYPOTHESIS_TOOL_NAME, {"statement": "register() lowercases None"}),
        tool_call_response(HYPOTHESIS_TOOL_NAME, {"id": 2, "status": "rejected", "evidence": ["forms.py:2 indexes first"]}),
        tool_call_response(HYPOTHESIS_TOOL_NAME, {"id": 1, "status": "supported", "evidence": ["app/forms.py:2"]}),
        tool_call_response(HYPOTHESIS_TOOL_NAME, {"id": 9, "status": "supported"}),  # bad id -> reported, not raised
        finish(),
    ]
    task, provider, sink = run(steps, repo)
    assert task.status is TaskStatus.COMPLETED
    h1, h2 = task.hypotheses
    assert h1.status is HypothesisStatus.SUPPORTED and h1.evidence == ["app/forms.py:2"]
    assert h1.history == ["proposed@1", "proposed->supported@4"]
    assert h2.status is HypothesisStatus.REJECTED
    assert "no hypothesis #9" in last_tool_output(provider.requests[5])
    assert len([e for e in sink.of_type(EventType.AGENT_DECISION) if e.payload["decision"] == "hypothesis"]) == 4
    assert task.actions == []  # built-ins act on task state, not the machine


def test_project_secrets_are_redacted_before_reaching_the_model(repo):
    key = "AIza" + "Q" * 35
    (repo / "app" / "settings.py").write_text(f"GOOGLE_KEY = '{key}'\n")

    def check(request):
        out = last_tool_output(request)
        assert key not in out and "[REDACTED]" in out and "redacted credential-like values" in out
        return finish()

    task, _, _ = run([tool_call_response("read_file", {"path": "app/settings.py"}), check], repo)
    assert task.status is TaskStatus.COMPLETED


# ---- authorization through the loop -----------------------------------------------------------


class WriteInput(ToolInput):
    text: str


class FakeWrite(Tool):
    name = "fake_write"
    description = "pretend to write"
    permission = PermissionLevel.WRITE
    input_model = WriteInput

    def __init__(self):
        self.ran = 0

    def run(self, args, context: ToolContext) -> ToolResult:
        self.ran += 1
        return ToolResult.success("written")


@pytest.mark.parametrize(
    ("mode", "approver", "expected_runs", "stage"),
    [
        (ExecutionMode.PLAN, AllowAllApprover(), 0, ActionStage.DENIED),  # policy DENY: approver never asked
        (ExecutionMode.CONFIRM, DenyAllApprover(), 0, ActionStage.DENIED),
        (ExecutionMode.CONFIRM, AllowAllApprover(), 1, ActionStage.EXECUTED),
        (ExecutionMode.AUTO, DenyAllApprover(), 1, ActionStage.EXECUTED),  # AUTO allows WRITE without asking
    ],
)
def test_write_authorization(repo, mode, approver, expected_runs, stage):
    tool = FakeWrite()
    provider = ScriptedProvider([tool_call_response("fake_write", {"text": "x"}), finish()])
    orch = Orchestrator(provider, ToolRegistry([tool]), settings=OrchestratorSettings(mode=mode), approver=approver)
    task = orch.run("x", repo)
    assert tool.ran == expected_runs
    assert task.actions[0].stage is stage
