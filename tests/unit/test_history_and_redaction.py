from datetime import UTC, datetime, timedelta

from trace_agent.agent.orchestrator import FINISH_TOOL_NAME, Orchestrator
from trace_agent.observability.events import EventType, ExecutionEvent, InMemoryEventSink
from trace_agent.observability.sinks import CompositeSink, RedactingSink
from trace_agent.providers.scripted import ScriptedProvider, tool_call_response
from trace_agent.safety.redaction import REDACTED, Redactor
from trace_agent.storage.history import HistoryStore
from trace_agent.tools.builtin import default_registry

GOOGLE_KEY = "AIza" + "B" * 35


def test_redactor_known_secrets_and_patterns():
    r = Redactor(["my-custom-secret-value"])
    text = f"key={GOOGLE_KEY} other=my-custom-secret-value gsk_{'a' * 30} fine=hello"
    out = r.text(text)
    assert GOOGLE_KEY not in out and "my-custom-secret-value" not in out and "gsk_" not in out
    assert "fine=hello" in out and out.count(REDACTED) == 3


def test_redactor_ignores_short_known_values_and_recurses():
    r = Redactor(["abc"])
    assert r.text("abc") == "abc"
    assert r.value({"a": [GOOGLE_KEY, 1], "b": (GOOGLE_KEY,)}) == {"a": [REDACTED, 1], "b": (REDACTED,)}


def test_redacting_and_composite_sinks():
    a, b = InMemoryEventSink(), InMemoryEventSink()
    sink = RedactingSink(CompositeSink([a, b]), Redactor())
    sink.emit(ExecutionEvent(task_id="t", type=EventType.ERROR, payload={"message": f"bad key {GOOGLE_KEY}"}))
    assert a.events[0].payload["message"] == f"bad key {REDACTED}" == b.events[0].payload["message"]


def test_failing_sink_does_not_break_others_or_the_caller(capsys):
    class Broken:
        def emit(self, event):
            raise UnicodeEncodeError("charmap", "→", 0, 1, "character maps to <undefined>")

    good = InMemoryEventSink()
    sink = CompositeSink([Broken(), good])
    for _ in range(3):
        sink.emit(ExecutionEvent(task_id="t", type=EventType.TASK_STARTED))
    assert len(good.events) == 3  # the healthy sink still got everything
    assert "Broken" in sink.failures
    assert capsys.readouterr().err.count("warning: Broken failed") == 1  # reported once, not per event


def test_display_crash_cannot_kill_agent_run(tmp_path):
    """Regression: a UnicodeEncodeError in the terminal printer aborted a live Gemini run."""

    class CrashingPrinter:
        def emit(self, event):
            raise UnicodeEncodeError("charmap", "→", 0, 1, "character maps to <undefined>")

    (tmp_path / "repo").mkdir()
    provider = ScriptedProvider(
        [
            tool_call_response("list_files", {}),
            tool_call_response(FINISH_TOOL_NAME, {"outcome": "diagnosed", "summary": "ok"}),
        ]
    )
    task = Orchestrator(provider, default_registry(), sink=CompositeSink([CrashingPrinter()])).run("x", tmp_path / "repo")
    assert task.status.value == "completed"


def test_history_records_full_run_and_prunes(tmp_path):
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / "a.py").write_text(f"KEY = '{GOOGLE_KEY}'\n")
    store = HistoryStore(tmp_path / "history.db")
    provider = ScriptedProvider(
        [
            tool_call_response("read_file", {"path": "a.py"}),
            tool_call_response(FINISH_TOOL_NAME, {"outcome": "diagnosed", "summary": "hardcoded key"}),
        ]
    )
    task = Orchestrator(provider, default_registry(), sink=RedactingSink(store, Redactor())).run("x", tmp_path / "repo")
    store.save_task(task, provider="scripted", model="m", mode="confirm")

    row = store.find_task(task.id[:6])
    assert row.status == "completed" and row.stop_reason == "diagnosed" and row.actions == 1
    events = store.events(task.id)
    assert events[0].type == "task_started" and events[-1].type == "task_finished"
    executed = [e for e in events if e.type == "tool_executed"][0]
    assert GOOGLE_KEY not in str(executed.payload) and REDACTED in executed.payload["output_preview"]
    assert store.list_tasks()[0].id == task.id

    assert store.prune(30, now=datetime.now(UTC) + timedelta(days=31)) == 1
    assert store.list_tasks() == [] and store.events(task.id) == []
    assert store.prune(None) == 0
