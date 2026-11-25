"""The tool contract: what every local action Trace can take must look like.

A tool declares:

* ``name`` / ``description`` - shown to the model,
* ``input_model`` - a Pydantic model; its JSON Schema becomes the tool's parameters,
  and it validates whatever arguments the model sends,
* ``permission`` - fed into the safety policy before the tool may run,
* ``run()`` - does the work and returns a structured :class:`ToolResult`.

Tools never decide whether they are *allowed* to run; the orchestrator asks
``safety.decide`` first. Tools must still enforce project boundaries on paths.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from trace_agent.core.messages import ToolDefinition
from trace_agent.safety.permissions import PermissionLevel

TRUNCATION_MARKER = "\n... [{omitted} characters omitted by Trace] ...\n"


class ToolStatus(StrEnum):
    SUCCESS = "success"
    ERROR = "error"  # the tool ran and failed (e.g. file not found, non-zero exit)
    INVALID_INPUT = "invalid_input"  # arguments failed validation; nothing ran
    DENIED = "denied"  # the safety policy or the user refused; nothing ran
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


class ToolResult(BaseModel):
    """The *actual* outcome of a tool call, reported honestly back to the model."""

    model_config = ConfigDict(frozen=True)

    status: ToolStatus
    output: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    truncated: bool = False
    duration_ms: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _error_matches_status(self) -> ToolResult:
        if self.status is ToolStatus.SUCCESS and self.error is not None:
            raise ValueError("a successful result must not carry an error")
        if self.status is not ToolStatus.SUCCESS and not self.error:
            raise ValueError("a non-successful result must explain its error")
        return self

    @property
    def ok(self) -> bool:
        return self.status is ToolStatus.SUCCESS

    @classmethod
    def success(cls, output: str = "", **data: Any) -> ToolResult:
        return cls(status=ToolStatus.SUCCESS, output=output, data=data)

    @classmethod
    def failure(cls, status: ToolStatus, error: str, output: str = "", **data: Any) -> ToolResult:
        return cls(status=status, error=error, output=output, data=data)

    def truncated_to(self, max_chars: int) -> ToolResult:
        """Bound ``output`` size, keeping the head *and* tail.

        The tail matters: test runners and compilers print their summary last.
        """
        if max_chars <= 0:
            raise ValueError("max_chars must be positive")
        if len(self.output) <= max_chars:
            return self
        head = max_chars // 2
        tail = max_chars - head
        omitted = len(self.output) - max_chars
        text = self.output[:head] + TRUNCATION_MARKER.format(omitted=omitted) + self.output[-tail:]
        return self.model_copy(update={"output": text, "truncated": True})


@dataclass(frozen=True)
class ToolContext:
    """Per-execution environment handed to a tool by the orchestrator."""

    project_root: Path
    timeout_seconds: float = 60.0
    max_output_chars: int = 20_000
    cancel_event: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        if not self.project_root.is_absolute():
            raise ValueError("project_root must be an absolute path")
        if not self.project_root.is_dir():
            raise ValueError(f"project_root '{self.project_root}' is not a directory")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_output_chars <= 0:
            raise ValueError("max_output_chars must be positive")

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()


class ToolInput(BaseModel):
    """Base class for tool input models. Unknown arguments are rejected, not ignored."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Tool(ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    permission: ClassVar[PermissionLevel]
    input_model: ClassVar[type[ToolInput]]

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=self.input_model.model_json_schema(),
        )

    def is_available(self, context: ToolContext) -> bool:
        """Whether this tool works in the current environment (e.g. git installed).

        Spec: the model must only receive tools supported by the current environment.
        """
        return True

    @abstractmethod
    def run(self, args: Any, context: ToolContext) -> ToolResult:
        """Execute with already-validated ``args`` (an instance of ``input_model``)."""
