"""Composable event sinks: fan-out and redaction."""

from __future__ import annotations

import sys
from collections.abc import Iterable

from trace_agent.observability.events import EventSink, ExecutionEvent
from trace_agent.safety.redaction import Redactor


class CompositeSink:
    """Send each event to several sinks (e.g. the terminal and the history database).

    Observability must never break the task it observes: an exception from one sink is
    caught, reported once on stderr, and the remaining sinks still receive the event.
    Failures are kept in ``failures`` so callers can surface them (e.g. "history incomplete").
    """

    def __init__(self, sinks: Iterable[EventSink]) -> None:
        self.sinks = list(sinks)
        self.failures: dict[str, str] = {}

    def emit(self, event: ExecutionEvent) -> None:
        for sink in self.sinks:
            try:
                sink.emit(event)
            except Exception as exc:  # noqa: BLE001 - isolate observer failures from the agent
                name = type(sink).__name__
                if name not in self.failures:
                    self.failures[name] = f"{type(exc).__name__}: {exc}"
                    try:
                        sys.stderr.write(f"\n[trace] warning: {name} failed and some events were not recorded: {exc}\n")
                    except Exception:  # noqa: BLE001 - stderr itself may be broken
                        pass


class RedactingSink:
    """Redact credentials from every event payload before it reaches ``inner``."""

    def __init__(self, inner: EventSink, redactor: Redactor) -> None:
        self.inner = inner
        self.redactor = redactor

    def emit(self, event: ExecutionEvent) -> None:
        self.inner.emit(event.model_copy(update={"payload": self.redactor.value(event.payload)}))
