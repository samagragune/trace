"""Tool executor: the only path from a model's tool call to a real action.

For each call it performs, in order, and records every stage on the task:

    lookup -> validate arguments -> authorize (policy + user) -> run -> bound output

Nothing in here trusts the model: unknown tools, bad arguments, denials and tool
crashes all become honest ``ToolResult`` failures that are reported back to the model.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from trace_agent.agent.state import ActionStage, AgentTask
from trace_agent.core.messages import ToolCall
from trace_agent.observability.events import EventType
from trace_agent.safety.permissions import Decision, ExecutionMode, PermissionLevel, decide
from trace_agent.tools.base import ToolContext, ToolResult, ToolStatus
from trace_agent.tools.registry import ToolError, ToolRegistry

EmitFn = Callable[[EventType, dict[str, Any]], None]


@dataclass(frozen=True)
class ApprovalRequest:
    tool_name: str
    permission: PermissionLevel
    arguments: dict[str, Any]
    mode: ExecutionMode


class Approver(Protocol):
    def approve(self, request: ApprovalRequest) -> bool: ...


class DenyAllApprover:
    """Used when nobody can be asked (non-interactive runs): every ASK becomes a denial."""

    def approve(self, request: ApprovalRequest) -> bool:
        return False


class AllowAllApprover:
    """For tests and explicitly trusted automation only."""

    def approve(self, request: ApprovalRequest) -> bool:
        return True


def _preview(text: str, limit: int = 500) -> str:
    return text if len(text) <= limit else text[:limit] + f"... [{len(text) - limit} more chars]"


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, mode: ExecutionMode, approver: Approver, emit: EmitFn) -> None:
        self._registry = registry
        self._mode = mode
        self._approver = approver
        self._emit = emit

    def execute(self, task: AgentTask, call: ToolCall, context: ToolContext) -> ToolResult:
        self._emit(EventType.TOOL_REQUESTED, {"call_id": call.id, "tool": call.name, "arguments": call.arguments})

        # 1. lookup + validation ------------------------------------------------------
        try:
            parsed = self._registry.parse_call(call)
        except ToolError as exc:
            record = task.record_action(call, None)
            result = ToolResult.failure(ToolStatus.INVALID_INPUT, str(exc))
            task.advance_action(record.seq, ActionStage.FAILED, result=result)
            self._emit_result(call, result, 0.0)
            return result

        tool = parsed.tool
        record = task.record_action(call, tool.permission)

        # 2. authorization ---------------------------------------------------------------
        decision = decide(self._mode, tool.permission)
        if decision is Decision.ASK:
            approved = self._approver.approve(
                ApprovalRequest(tool_name=tool.name, permission=tool.permission, arguments=call.arguments, mode=self._mode)
            )
            decision = Decision.ALLOW if approved else Decision.DENY
            reason = "the user declined this action" if not approved else None
        else:
            reason = f"'{tool.permission.value}' actions are not permitted in {self._mode.value} mode"

        if decision is Decision.DENY:
            result = ToolResult.failure(ToolStatus.DENIED, reason or "denied by policy")
            task.advance_action(record.seq, ActionStage.DENIED, decision=Decision.DENY, result=result)
            self._emit(EventType.TOOL_DENIED, {"call_id": call.id, "tool": tool.name, "reason": result.error})
            return result

        task.advance_action(record.seq, ActionStage.AUTHORIZED, decision=Decision.ALLOW)
        self._emit(EventType.TOOL_AUTHORIZED, {"call_id": call.id, "tool": tool.name})

        # 3. execution ---------------------------------------------------------------------
        started = time.perf_counter()
        try:
            result = tool.run(parsed.args, context)
        except Exception as exc:  # noqa: BLE001 - a crashing tool must not crash the agent
            result = ToolResult.failure(ToolStatus.ERROR, f"tool crashed: {type(exc).__name__}: {exc}")
        duration_ms = (time.perf_counter() - started) * 1000
        result = result.truncated_to(context.max_output_chars).model_copy(update={"duration_ms": duration_ms})

        stage = ActionStage.EXECUTED if result.ok else ActionStage.FAILED
        task.advance_action(record.seq, stage, result=result)
        self._emit_result(call, result, duration_ms)
        return result

    def _emit_result(self, call: ToolCall, result: ToolResult, duration_ms: float) -> None:
        self._emit(
            EventType.TOOL_EXECUTED,
            {
                "call_id": call.id,
                "tool": call.name,
                "status": result.status.value,
                "error": result.error,
                "truncated": result.truncated,
                "duration_ms": round(duration_ms, 1),
                "output_preview": _preview(result.output),
                "data": result.data,
            },
        )
