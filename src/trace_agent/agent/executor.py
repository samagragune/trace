"""Tool executor: the only path from a model's tool call to a real action.

For each call, in order, with every stage recorded on the task:

    lookup + validate  ->  assess (effective permission, preview, certain failure, blocked)
      ->  policy decide(mode, effective permission)  ->  escalate if the tool demands approval
      ->  user approval (on ASK)  ->  run  ->  redact + truncate  ->  record evidence

Nothing here trusts the model: unknown tools, bad arguments, blocked commands, denials and
tool crashes all become honest ``ToolResult`` failures that are reported back to the model.

Evidence recorded from results:
* ``data["verification"]`` (from run_tests) -> ``AgentTask.add_verification`` (ADR-006)
* ``data["file_changes"]`` (from editing tools) -> FILE_MODIFIED events with the diff
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
from trace_agent.safety.redaction import Redactor
from trace_agent.tools.base import Assessment, ToolContext, ToolResult, ToolStatus
from trace_agent.tools.registry import ToolError, ToolRegistry

EmitFn = Callable[[EventType, dict[str, Any]], None]


@dataclass(frozen=True)
class ApprovalRequest:
    tool_name: str
    permission: PermissionLevel
    arguments: dict[str, Any]
    mode: ExecutionMode
    preview: str | None = None  # e.g. the diff that will be applied
    reason: str | None = None  # why approval is required beyond the normal policy


class Approver(Protocol):
    def approve(self, request: ApprovalRequest) -> bool: ...


class DenyAllApprover:
    """Used when nobody can be asked (non-interactive runs): every ASK becomes a denial."""

    denial_reason = (
        "this action needs the user's approval, but Trace is running without an interactive terminal, "
        "so it was denied"
    )

    def approve(self, request: ApprovalRequest) -> bool:
        return False


class AllowAllApprover:
    """For tests and explicitly trusted automation only."""

    def approve(self, request: ApprovalRequest) -> bool:
        return True


def _preview(text: str, limit: int = 500) -> str:
    return text if len(text) <= limit else text[:limit] + f"... [{len(text) - limit} more chars]"


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        mode: ExecutionMode,
        approver: Approver,
        emit: EmitFn,
        *,
        redactor: Redactor | None = None,
    ) -> None:
        self._registry = registry
        self._mode = mode
        self._approver = approver
        self._emit = emit
        # Redacts credentials found in the *project* (keys in code, logs...) before tool
        # output is sent to a cloud model. Patterns only, plus any known secrets.
        self._redactor = redactor or Redactor()
        self.blocked_attempts = 0

    def execute(self, task: AgentTask, call: ToolCall, context: ToolContext) -> ToolResult:
        self._emit(EventType.TOOL_REQUESTED, {"call_id": call.id, "tool": call.name, "arguments": call.arguments})

        # 1. lookup + validation ------------------------------------------------------------
        try:
            parsed = self._registry.parse_call(call)
        except ToolError as exc:
            return self._fail_early(task, call, None, ToolResult.failure(ToolStatus.INVALID_INPUT, str(exc)))
        tool = parsed.tool

        # 2. assessment of this specific call --------------------------------------------------
        try:
            assessment = tool.assess(parsed.args, context)
        except Exception as exc:  # noqa: BLE001 - a crashing assess must not crash the agent
            assessment = Assessment(tool.permission, error=f"tool could not assess the call: {type(exc).__name__}: {exc}")
        if assessment.error:
            return self._fail_early(task, call, assessment.permission, ToolResult.failure(assessment.error_status, assessment.error))

        record = task.record_action(call, assessment.permission)
        if assessment.blocked:
            self.blocked_attempts += 1
            return self._deny(task, record.seq, call, tool.name, f"blocked by Trace's safety rules: {assessment.blocked}")

        # 3. authorization ---------------------------------------------------------------------
        decision = decide(self._mode, assessment.permission)
        if decision is Decision.DENY:
            context.denied_paths.update(assessment.paths)
            return self._deny(
                task, record.seq, call, tool.name,
                f"'{assessment.permission.value}' actions are not permitted in {self._mode.value} mode",
            )
        if decision is Decision.ALLOW and assessment.requires_approval:
            decision = Decision.ASK  # e.g. editing a file with uncommitted user changes
        if decision is Decision.ASK:
            self._emit(EventType.AGENT_DECISION, {"decision": "approval_requested", "tool": tool.name, "reason": assessment.requires_approval})
            approved = self._approver.approve(
                ApprovalRequest(
                    tool_name=tool.name,
                    permission=assessment.permission,
                    arguments=call.arguments,
                    mode=self._mode,
                    preview=assessment.preview,
                    reason=assessment.requires_approval,
                )
            )
            if not approved:
                reason = getattr(self._approver, "denial_reason", None) or "the user declined this action"
                context.denied_paths.update(assessment.paths)  # a denial covers the file, not just this call
                return self._deny(task, record.seq, call, tool.name, reason)

        task.advance_action(record.seq, ActionStage.AUTHORIZED, decision=Decision.ALLOW)
        self._emit(EventType.TOOL_AUTHORIZED, {"call_id": call.id, "tool": tool.name, "permission": assessment.permission.value})

        # 4. execution -------------------------------------------------------------------------
        started = time.perf_counter()
        try:
            result = tool.run(parsed.args, context)
        except Exception as exc:  # noqa: BLE001 - a crashing tool must not crash the agent
            result = ToolResult.failure(ToolStatus.ERROR, f"tool crashed: {type(exc).__name__}: {exc}")
        duration_ms = (time.perf_counter() - started) * 1000
        redacted_output = self._redactor.text(result.output)
        update: dict[str, Any] = {"duration_ms": duration_ms}
        if redacted_output != result.output:
            update["output"] = redacted_output + "\n(note: Trace redacted credential-like values from this output)"
        result = result.model_copy(update=update).truncated_to(context.max_output_chars)

        stage = ActionStage.EXECUTED if result.ok else ActionStage.FAILED
        task.advance_action(record.seq, stage, result=result)
        self._record_evidence(task, call, result)
        self._emit_result(call, result, duration_ms)
        return result

    # ------------------------------------------------------------------ helpers

    def _fail_early(self, task: AgentTask, call: ToolCall, permission: PermissionLevel | None, result: ToolResult) -> ToolResult:
        record = task.record_action(call, permission)
        task.advance_action(record.seq, ActionStage.FAILED, result=result)
        self._emit_result(call, result, 0.0)
        return result

    def _deny(self, task: AgentTask, seq: int, call: ToolCall, tool_name: str, reason: str) -> ToolResult:
        result = ToolResult.failure(ToolStatus.DENIED, reason)
        task.advance_action(seq, ActionStage.DENIED, decision=Decision.DENY, result=result)
        self._emit(EventType.TOOL_DENIED, {"call_id": call.id, "tool": tool_name, "reason": reason})
        return result

    def _record_evidence(self, task: AgentTask, call: ToolCall, result: ToolResult) -> None:
        verification = result.data.get("verification")
        if isinstance(verification, dict) and verification.get("command"):
            recorded = task.add_verification(
                str(verification["command"]),
                bool(verification.get("passed")),
                exit_code=verification.get("exit_code"),
                summary=str(verification.get("summary") or ""),
            )
            self._emit(EventType.VERIFICATION, {"call_id": call.id, **recorded.model_dump(mode="json")})
        for change in result.data.get("file_changes") or []:
            self._emit(EventType.FILE_MODIFIED, {"call_id": call.id, **change})
        for path in result.data.get("changed_by_command") or []:
            if path not in task.command_changed_files:
                task.command_changed_files.append(path)
            self._emit(EventType.FILE_MODIFIED, {"call_id": call.id, "path": path, "change": "changed by command"})
        if result.data.get("guard_violation"):
            self.blocked_attempts += 1  # a command tried to change guarded files: counts toward SAFETY_VIOLATION

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
                "data": {k: v for k, v in result.data.items() if k not in ("file_changes", "verification", "changed_by_command")},
            },
        )
