"""Observation: turning actual tool outcomes into model input, and spotting lack of progress.

The model is told exactly what happened: status, error, whether output was truncated, and
the output itself. It never receives a paraphrase that could hide a failure.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field

from trace_agent.core.messages import ToolCall
from trace_agent.tools.base import ToolResult


def render_tool_result(result: ToolResult) -> str:
    lines = [f"status: {result.status.value}"]
    if result.error:
        lines.append(f"error: {result.error}")
    if result.truncated:
        lines.append("note: output was truncated by Trace (beginning and end kept)")
    if result.output:
        lines.append("output:")
        lines.append(result.output)
    return "\n".join(lines)


def call_signature(call: ToolCall) -> str:
    return f"{call.name}:{json.dumps(call.arguments, sort_keys=True, default=str)}"


@dataclass
class ProgressMonitor:
    """Detects repeated identical tool calls and runs of responses that do nothing.

    * ``warn_after``: identical calls before the model is warned in the tool result.
    * ``stop_after``: identical calls before the task is stopped with NO_PROGRESS.
    * ``max_idle``: consecutive responses without any tool call before stopping.
    """

    warn_after: int = 2
    stop_after: int = 4
    max_idle: int = 2
    _calls: Counter[str] = field(default_factory=Counter)
    idle_responses: int = 0

    def record_call(self, call: ToolCall) -> int:
        signature = call_signature(call)
        self._calls[signature] += 1
        return self._calls[signature]

    def should_warn(self, count: int) -> bool:
        return count > self.warn_after

    def should_stop(self, count: int) -> bool:
        return count >= self.stop_after

    def record_idle(self) -> bool:
        """Register a response with no tool calls. Returns True when the idle limit is exceeded."""
        self.idle_responses += 1
        return self.idle_responses > self.max_idle

    def record_activity(self) -> None:
        self.idle_responses = 0


def repeat_warning(count: int) -> str:
    return (
        f"\nwarning from Trace: this exact call has now been made {count} times with the same arguments. "
        "Repeating it will not produce new information; change approach or finish the task."
    )
