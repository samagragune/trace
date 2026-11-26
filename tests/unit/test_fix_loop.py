"""End-to-end fix loops through the orchestrator with a scripted model.

Real files, real pytest subprocesses, real checkpoints. Only the model is scripted.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from trace_agent.agent.executor import AllowAllApprover, DenyAllApprover
from trace_agent.agent.orchestrator import FINISH_TOOL_NAME, Orchestrator, OrchestratorSettings
from trace_agent.agent.state import ActionStage, StopReason, TaskStatus
from trace_agent.core.messages import Role
from trace_agent.observability.events import EventType, InMemoryEventSink
from trace_agent.providers.scripted import ScriptedProvider, tool_call_response
from trace_agent.safety.checkpoints import CheckpointStore
from trace_agent.safety.permissions import ExecutionMode
from trace_agent.tools.builtin import default_registry

TEST_CMD = f'"{sys.executable}" -m pytest -q -p no:cacheprovider'
BUGGY = "def add(a, b):\n    return a - b\n"


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "tests").mkdir(parents=True)
    (root / "calc.py").write_text(BUGGY)
    (root / "tests" / "test_calc.py").write_text(
        "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parents[1]))\n"
        "from calc import add\n\ndef test_add():\n    assert add(2, 2) == 4\n"
    )
    return root


def fix_edit(replacement="return a + b"):
    return tool_call_response("edit_file", {"path": "calc.py", "expected_original": "return a - b", "replacement": replacement})


def finish(outcome="fixed"):
    return tool_call_response(FINISH_TOOL_NAME, {"outcome": outcome, "summary": "add() subtracted; now adds."})


def last_tool(request):
    return [m for m in request.messages if m.role is Role.TOOL][-1].content


def run(proj, tmp_path, steps, *, mode=ExecutionMode.AUTO, approver=None, **settings):
    provider = ScriptedProvider(steps)
    sink = InMemoryEventSink()
    orch = Orchestrator(
        provider,
        default_registry(),
        settings=OrchestratorSettings(mode=mode, checkpoint_dir=tmp_path / "cp", test_command=TEST_CMD, **settings),
        sink=sink,
        approver=approver or DenyAllApprover(),
    )
    return orch.run("tests fail: add(2,2) returns 0", proj), provider, sink


# ---- the happy path ---------------------------------------------------------------------------------


def test_reproduce_edit_verify_and_independently_confirm(proj, tmp_path):
    steps = [tool_call_response("run_tests", {}), fix_edit(), tool_call_response("run_tests", {}), finish()]
    task, provider, sink = run(proj, tmp_path, steps)

    assert task.status is TaskStatus.COMPLETED and task.stop_reason is StopReason.VERIFIED_FIX
    assert (proj / "calc.py").read_text() == "def add(a, b):\n    return a + b\n"
    # reproduction failed, agent's check passed, then Trace's own independent run passed
    assert [v.passed for v in task.verifications] == [False, True, True]
    assert task.actions[-1].call.id.startswith("trace-verify-")
    assert task.changed_files == [{"path": "calc.py", "change": "modified"}]
    assert [e.payload["path"] for e in sink.of_type(EventType.FILE_MODIFIED)] == ["calc.py"]
    assert len(sink.of_type(EventType.VERIFICATION)) == 3
    # the reproduction's real failure output reached the model
    assert "tests did not pass" in last_tool(provider.requests[1]) and "1 failed" in last_tool(provider.requests[1])

    # and the whole thing can be undone
    CheckpointStore.open(Path(task.checkpoint_dir)).restore()
    assert (proj / "calc.py").read_text() == BUGGY


def test_fixed_claim_rejected_when_independent_tests_fail(proj, tmp_path):
    def check(request):
        out = last_tool(request)
        assert "rejected" in out and "Trace's own test run: failed" in out
        return finish("diagnosed")

    # wrong "fix", the agent skips testing and claims success
    task, _, sink = run(proj, tmp_path, [fix_edit("return a * b + 1"), finish(), check])
    assert task.stop_reason is StopReason.DIAGNOSED
    assert task.verifications[-1].passed is False
    assert sink.of_type(EventType.AGENT_DECISION)[-1].payload["decision"] == "finish"


def test_fixed_without_any_change_is_rejected(proj, tmp_path):
    def check(request):
        assert "requires a code change" in last_tool(request)
        return finish("diagnosed")

    task, _, _ = run(proj, tmp_path, [finish(), check])
    assert task.stop_reason is StopReason.DIAGNOSED and task.verifications == []


def test_revising_a_failed_fix(proj, tmp_path):
    steps = [
        fix_edit("return a * b - 1"),  # wrong first attempt: add(2, 2) == 3
        tool_call_response("run_tests", {}),
        tool_call_response("edit_file", {"path": "calc.py", "expected_original": "return a * b - 1", "replacement": "return a + b"}),
        tool_call_response("run_tests", {}),
        finish(),
    ]
    task, _, _ = run(proj, tmp_path, steps)
    assert task.stop_reason is StopReason.VERIFIED_FIX
    # failed check, passing check after the revision, then Trace's independent run
    assert [v.passed for v in task.verifications] == [False, True, True]


def test_fix_attempts_are_limited(proj, tmp_path):
    # A wrong fix, then repeated failing verifications (narrowed differently each time so the
    # repeated-call detector isn't what stops the task).
    narrowings = ["", " -k add", " -k test_add", " tests"]
    steps = [fix_edit("return a * 10")] + [tool_call_response("run_tests", {"test_command": TEST_CMD + n}) for n in narrowings]
    task, provider, _ = run(proj, tmp_path, steps, max_fix_attempts=2)
    assert task.stop_reason is StopReason.NO_PROGRESS and "fix attempts" in task.summary
    assert len(task.verifications) == 3  # stopped on the 3rd failure (> 2 allowed)
    assert any("One more failed attempt stops the task" in last_tool(r) for r in provider.requests[1:])


# ---- safety in the loop -------------------------------------------------------------------------------


def test_plan_mode_refuses_edits(proj, tmp_path):
    task, provider, _ = run(proj, tmp_path, [fix_edit(), finish("diagnosed")], mode=ExecutionMode.PLAN, approver=AllowAllApprover())
    assert task.actions[0].stage is ActionStage.DENIED and (proj / "calc.py").read_text() == BUGGY
    assert "not permitted in plan mode" in last_tool(provider.requests[1])


def test_confirm_mode_without_a_terminal_denies_edits(proj, tmp_path):
    task, _, _ = run(proj, tmp_path, [fix_edit(), finish("diagnosed")], mode=ExecutionMode.CONFIRM)
    assert task.actions[0].stage is ActionStage.DENIED and (proj / "calc.py").read_text() == BUGGY


def test_tests_run_without_approval_in_confirm_mode(proj, tmp_path):
    task, _, _ = run(proj, tmp_path, [tool_call_response("run_tests", {}), finish("diagnosed")], mode=ExecutionMode.CONFIRM)
    assert task.actions[0].stage is ActionStage.FAILED  # ran (and failed), was not denied
    assert task.verifications[0].passed is False


class RecordingApprover:
    def __init__(self, answer: bool):
        self.answer = answer
        self.requests = []

    def approve(self, request):
        self.requests.append(request)
        return self.answer


def test_uncommitted_user_changes_need_approval_even_in_auto(proj, tmp_path):
    try:
        for args in (["init", "-q"], ["add", "."], ["-c", "user.email=t@x", "-c", "user.name=T", "commit", "-q", "-m", "i"]):
            subprocess.run(["git", "-C", str(proj), *args], check=True, capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("git not available")
    (proj / "calc.py").write_text(BUGGY + "# my uncommitted note\n")  # user's work in progress

    approver = RecordingApprover(answer=False)
    task, _, _ = run(proj, tmp_path, [fix_edit(), finish("diagnosed")], approver=approver)
    assert len(approver.requests) == 1
    request = approver.requests[0]
    assert "uncommitted changes" in request.reason and "+    return a + b" in request.preview
    assert task.actions[0].stage is ActionStage.DENIED
    assert "my uncommitted note" in (proj / "calc.py").read_text()


def test_repeated_blocked_commands_stop_the_task(proj, tmp_path):
    steps = [tool_call_response("run_command", {"command": f"git push origin main{'' if i == 0 else ' -f' * i}"}) for i in range(3)]
    task, _, _ = run(proj, tmp_path, steps)
    assert task.stop_reason is StopReason.SAFETY_VIOLATION and task.status is TaskStatus.FAILED
    assert all(a.stage is ActionStage.DENIED for a in task.actions)


def test_destructive_command_needs_approval_even_in_auto(proj, tmp_path):
    approver = RecordingApprover(answer=False)
    task, _, _ = run(proj, tmp_path, [tool_call_response("run_command", {"command": "rm calc.py"}), finish("diagnosed")], approver=approver)
    assert approver.requests[0].permission.value == "destructive" and (proj / "calc.py").exists()


# ---- context budget -----------------------------------------------------------------------------------


def test_old_tool_outputs_are_elided_when_over_budget(proj, tmp_path):
    (proj / "big.txt").write_text("x" * 5000)
    reads = [tool_call_response("read_file", {"path": "big.txt", "start_line": 1, "end_line": i + 1}) for i in range(4)]
    task, provider, sink = run(proj, tmp_path, [*reads, finish("diagnosed")], max_context_chars=12_000, keep_recent_tool_outputs=1)
    final_request = provider.requests[-1]
    tool_messages = [m for m in final_request.messages if m.role is Role.TOOL]
    assert sum("elided by Trace" in m.content for m in tool_messages) >= 1
    assert "elided" not in tool_messages[-1].content  # the most recent output is always kept
    assert any(e.payload.get("decision") == "context_compacted" for e in sink.of_type(EventType.AGENT_DECISION))


# ---- robustness (regressions from the live Build 004 run) ------------------------------------------------


def test_approver_eof_denies_instead_of_crashing(proj, tmp_path, monkeypatch):
    """Live bug: stdin looked like a TTY but was closed; Prompt.ask raised EOFError and killed the run."""
    from rich.console import Console

    from trace_agent.cli.render import TerminalApprover

    def eof(*args, **kwargs):
        raise EOFError

    monkeypatch.setattr("trace_agent.cli.render.Prompt.ask", eof)
    approver = TerminalApprover(Console(file=open(tmp_path / "out.txt", "w")))
    task, provider, _ = run(
        proj, tmp_path, [fix_edit(), fix_edit("return a + b + 0"), finish("diagnosed")],
        mode=ExecutionMode.CONFIRM, approver=approver,
    )
    assert task.status is TaskStatus.COMPLETED and (proj / "calc.py").read_text() == BUGGY
    assert all(a.stage is ActionStage.DENIED for a in task.actions)
    assert "no input could be read" in last_tool(provider.requests[1])


def test_non_interactive_denial_explains_why(proj, tmp_path):
    _, provider, _ = run(proj, tmp_path, [fix_edit(), finish("diagnosed")], mode=ExecutionMode.CONFIRM)
    assert "without an interactive terminal" in last_tool(provider.requests[1])


def test_unexpected_exception_fails_task_safely(proj, tmp_path):
    class Exploding:
        def approve(self, request):
            raise RuntimeError("approver bug")

    task, _, sink = run(proj, tmp_path, [fix_edit(), finish("diagnosed")], mode=ExecutionMode.CONFIRM, approver=Exploding())
    assert task.status is TaskStatus.FAILED and task.stop_reason is StopReason.INTERNAL_ERROR
    assert "approver bug" in task.summary and (proj / "calc.py").read_text() == BUGGY
    [error] = [e for e in sink.of_type(EventType.ERROR) if e.payload["source"] == "orchestrator"]
    assert "RuntimeError" in error.payload["traceback"]
    assert sink.events[-1].type is EventType.TASK_FINISHED  # the trace is still complete


# ---- write guard: denials are binding for commands too (live Build 004 finding) ---------------------------

PY_Q = f'"{sys.executable}"'


def write_cmd(rel: str, text: str) -> str:
    return f"{PY_Q} -c \"import pathlib; pathlib.Path('{rel}').write_text('{text}')\""


def test_command_cannot_work_around_a_denied_edit(proj, tmp_path):
    """Reproduces the live run: edit denied, then the model writes the file from a command."""
    steps = [
        fix_edit(),  # denied (confirm mode, no terminal)
        tool_call_response("run_command", {"command": write_cmd("calc.py", "hacked")}),
        finish("diagnosed"),
    ]
    task, provider, _ = run(proj, tmp_path, steps, mode=ExecutionMode.CONFIRM, approver=AllowAllApproverForCommandsOnly())
    assert (proj / "calc.py").read_text() == BUGGY  # restored by the write guard
    out = last_tool(provider.requests[2])
    assert "VIOLATION" in out and "calc.py" in out and "Trace restored them" in out


def test_command_cannot_change_uncommitted_user_files(proj, tmp_path):
    try:
        for args in (["init", "-q"], ["add", "."], ["-c", "user.email=t@x", "-c", "user.name=T", "commit", "-q", "-m", "i"]):
            subprocess.run(["git", "-C", str(proj), *args], check=True, capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("git not available")
    (proj / "notes.md").write_text("my draft\n")  # untracked user work
    steps = [tool_call_response("run_command", {"command": write_cmd("notes.md", "gone")}), finish("diagnosed")]
    task, _, _ = run(proj, tmp_path, steps)
    assert (proj / "notes.md").read_text() == "my draft\n"


def test_repeated_guard_violations_stop_the_task(proj, tmp_path):
    steps = [fix_edit()] + [
        tool_call_response("run_command", {"command": write_cmd("calc.py", f"attempt {i}")}) for i in range(3)
    ]
    task, _, _ = run(proj, tmp_path, steps, mode=ExecutionMode.CONFIRM, approver=AllowAllApproverForCommandsOnly())
    assert task.stop_reason is StopReason.SAFETY_VIOLATION
    assert (proj / "calc.py").read_text() == BUGGY


def test_files_changed_by_commands_are_reported(proj, tmp_path):
    steps = [tool_call_response("run_command", {"command": write_cmd("generated.txt", "x")}), finish("diagnosed")]
    task, provider, sink = run(proj, tmp_path, steps)
    assert task.command_changed_files == ["generated.txt"]
    assert "not covered by `trace undo`" in last_tool(provider.requests[1])
    assert any(e.payload.get("change") == "changed by command" for e in sink.of_type(EventType.FILE_MODIFIED))


def test_test_runs_do_not_report_cache_files(proj, tmp_path):
    task, _, _ = run(proj, tmp_path, [tool_call_response("run_tests", {}), finish("diagnosed")])
    assert task.command_changed_files == []  # __pycache__ / .pytest_cache are ignored


class AllowAllApproverForCommandsOnly:
    """Approves commands, denies file edits: the scenario from the live run."""

    def approve(self, request):
        return request.tool_name == "run_command"
