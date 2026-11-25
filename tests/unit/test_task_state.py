import pytest

from trace_agent.agent.state import ActionStage, AgentTask, StopReason, TaskStateError, TaskStatus
from trace_agent.core.messages import ToolCall
from trace_agent.observability.events import EventSink, EventType, ExecutionEvent, InMemoryEventSink
from trace_agent.safety.permissions import Decision, PermissionLevel
from trace_agent.tools.base import ToolResult, ToolStatus


@pytest.fixture
def task(tmp_path):
    t = AgentTask(description="registration form crashes", project_root=tmp_path, max_iterations=3)
    t.start()
    return t


def _edit(task, ok=True):
    rec = task.record_action(ToolCall(id=f"c{len(task.actions)}", name="edit_file"), PermissionLevel.WRITE)
    task.advance_action(rec.seq, ActionStage.AUTHORIZED, decision=Decision.ALLOW)
    result = ToolResult.success("patched") if ok else ToolResult.failure(ToolStatus.ERROR, "mismatch")
    task.advance_action(rec.seq, ActionStage.EXECUTED, result=result)
    return rec


def test_lifecycle_requires_start(tmp_path):
    t = AgentTask(description="x", project_root=tmp_path)
    with pytest.raises(TaskStateError):
        t.begin_iteration()
    t.start()
    with pytest.raises(TaskStateError):
        t.start()


def test_iteration_limit_enforced(task):
    for _ in range(3):
        task.begin_iteration()
    with pytest.raises(TaskStateError, match="iteration limit"):
        task.begin_iteration()
    task.finish(StopReason.ITERATION_LIMIT, "ran out of iterations")
    assert task.status is TaskStatus.STOPPED and task.finished_at is not None


def test_action_stages_are_enforced(task):
    rec = task.record_action(ToolCall(id="1", name="read_file"), PermissionLevel.READ)
    assert rec.stage is ActionStage.REQUESTED
    with pytest.raises(TaskStateError):
        task.advance_action(rec.seq, ActionStage.EXECUTED, result=ToolResult.success())  # skipped authorization
    task.advance_action(rec.seq, ActionStage.AUTHORIZED, decision=Decision.ALLOW)
    with pytest.raises(TaskStateError, match="actual tool result"):
        task.advance_action(rec.seq, ActionStage.EXECUTED)
    task.advance_action(rec.seq, ActionStage.EXECUTED, result=ToolResult.success("contents"))
    with pytest.raises(TaskStateError):
        task.advance_action(rec.seq, ActionStage.FAILED, result=ToolResult.success())


def test_denied_action_is_terminal(task):
    rec = task.record_action(ToolCall(id="1", name="delete_file"), PermissionLevel.DESTRUCTIVE)
    task.advance_action(rec.seq, ActionStage.DENIED, decision=Decision.DENY)
    assert not rec.modified_project
    with pytest.raises(TaskStateError):
        task.advance_action(rec.seq, ActionStage.AUTHORIZED)


# ---- the verification invariant ----------------------------------------------------


def test_model_claim_alone_cannot_complete_task(task):
    with pytest.raises(TaskStateError, match="verified fix"):
        task.finish(StopReason.VERIFIED_FIX, "I fixed it!")


def test_passing_tests_without_a_modification_is_not_a_fix(task):
    task.add_verification("pytest", passed=True, exit_code=0)
    with pytest.raises(TaskStateError):
        task.finish(StopReason.VERIFIED_FIX, "fixed")


def test_verification_must_come_after_the_last_edit(task):
    _edit(task)
    task.add_verification("pytest", passed=True, exit_code=0)
    _edit(task)  # a later edit invalidates the earlier pass
    assert not task.is_fix_verified()
    task.add_verification("pytest", passed=True, exit_code=0)
    assert task.is_fix_verified()


def test_latest_post_edit_verification_must_pass(task):
    _edit(task)
    task.add_verification("pytest", passed=True, exit_code=0)
    task.add_verification("pytest -k regression", passed=False, exit_code=1)
    assert not task.is_fix_verified()


def test_failed_edit_does_not_count_as_modification(task):
    _edit(task, ok=False)
    task.add_verification("pytest", passed=True, exit_code=0)
    assert not task.is_fix_verified()


def test_verified_fix_completes(task):
    _edit(task)
    task.add_verification("pytest", passed=True, exit_code=0, summary="12 passed")
    task.finish(StopReason.VERIFIED_FIX, "null email crashed validator; added guard")
    assert task.status is TaskStatus.COMPLETED
    with pytest.raises(TaskStateError, match="already finished"):
        task.finish(StopReason.NO_FIX_NEEDED, "again")


def test_finish_requires_summary(task):
    with pytest.raises(TaskStateError, match="summary"):
        task.finish(StopReason.NO_FIX_NEEDED, "  ")


def test_approval_pending_can_resume(task):
    task.finish(StopReason.APPROVAL_PENDING, "waiting for approval to edit form.py")
    assert task.status is TaskStatus.AWAITING_APPROVAL and task.finished_at is None
    task.resume()
    assert task.status is TaskStatus.RUNNING


@pytest.mark.parametrize(
    ("reason", "status"),
    [
        (StopReason.USER_INTERRUPTED, TaskStatus.CANCELLED),
        (StopReason.PROVIDER_UNAVAILABLE, TaskStatus.FAILED),
        (StopReason.SAFETY_VIOLATION, TaskStatus.FAILED),
        (StopReason.NO_PROGRESS, TaskStatus.STOPPED),
        (StopReason.NO_FIX_NEEDED, TaskStatus.COMPLETED),
    ],
)
def test_stop_reason_status_mapping(task, reason, status):
    task.finish(reason, "explanation")
    assert task.status is status


# ---- events --------------------------------------------------------------------------


def test_in_memory_sink_records_events():
    sink = InMemoryEventSink()
    assert isinstance(sink, EventSink)
    sink.emit(ExecutionEvent(task_id="t", type=EventType.TASK_STARTED))
    sink.emit(ExecutionEvent(task_id="t", type=EventType.TOOL_REQUESTED, iteration=1, payload={"tool": "read_file"}))
    assert [e.type for e in sink.of_type(EventType.TOOL_REQUESTED)] == [EventType.TOOL_REQUESTED]
    assert len(sink.events) == 2
