"""Execution events: the raw material for traces, history and benchmarks.

The orchestrator emits an :class:`ExecutionEvent` for every meaningful step to
an :class:`EventSink`. Phase 0 ships an in-memory sink; Phase 2 adds a SQLite
sink (``history.db``) behind the same protocol. Payloads hold decision summaries
and observable evidence, never hidden chain-of-thought.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class EventType(StrEnum):
    TASK_STARTED = "task_started"
    CONTEXT_COLLECTED = "context_collected"
    MODEL_REQUEST = "model_request"
    MODEL_RESPONSE = "model_response"
    AGENT_DECISION = "agent_decision"
    TOOL_REQUESTED = "tool_requested"
    TOOL_AUTHORIZED = "tool_authorized"
    TOOL_DENIED = "tool_denied"
    TOOL_EXECUTED = "tool_executed"
    FILE_MODIFIED = "file_modified"
    VERIFICATION = "verification"
    ERROR = "error"
    TASK_FINISHED = "task_finished"


class ExecutionEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: uuid4().hex)
    task_id: str
    type: EventType
    iteration: int = Field(default=0, ge=0)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    payload: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class EventSink(Protocol):
    def emit(self, event: ExecutionEvent) -> None: ...


class InMemoryEventSink:
    def __init__(self) -> None:
        self.events: list[ExecutionEvent] = []

    def emit(self, event: ExecutionEvent) -> None:
        self.events.append(event)

    def of_type(self, event_type: EventType) -> list[ExecutionEvent]:
        return [e for e in self.events if e.type is event_type]
