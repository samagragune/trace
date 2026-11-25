"""Task and execution state for one debugging task.

The key invariant from the spec is encoded here, not left to the orchestrator:

    "A successful tool call or a model statement claiming success must never
     independently mark a debugging task as complete."

:meth:`AgentTask.finish` refuses ``StopReason.VERIFIED_FIX`` unless the project
was actually modified *and* the most recent verification recorded after the last
modification passed.

Each action moves through explicit stages (requested, then authorized or denied,
then executed or failed), which keeps "requested", "executed" and "verified"
separate (spec: observation_engine.important).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from trace_agent.core.messages import ToolCall
from trace_agent.errors import TraceError
from trace_agent.safety.permissions import Decision, PermissionLevel
from trace_agent.tools.base import ToolResult


def _now() -> datetime:
    return datetime.now(UTC)


class TaskStateError(TraceError):
    """An illegal state transition was attempted. Always a programming error in the caller."""


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"  # verified fix, or a justified "no fix needed"
    STOPPED = "stopped"  # ended without resolution (limits, insufficient info, no progress)
    FAILED = "failed"  # ended by an error (provider down, safety violation)
    CANCELLED = "cancelled"  # the user interrupted


class StopReason(StrEnum):
    VERIFIED_FIX = "verified_fix"
    NO_FIX_NEEDED = "no_fix_needed"
    DIAGNOSED = "diagnosed"  # root cause identified and reported; no changes made or verified
    ITERATION_LIMIT = "iteration_limit"
    NO_PROGRESS = "no_progress"
    INSUFFICIENT_INFORMATION = "insufficient_information"
    APPROVAL_PENDING = "approval_pending"
    SAFETY_VIOLATION = "safety_violation"
    USER_INTERRUPTED = "user_interrupted"
    PROVIDER_UNAVAILABLE = "provider_unavailable"


_STATUS_FOR_REASON: dict[StopReason, TaskStatus] = {
    StopReason.VERIFIED_FIX: TaskStatus.COMPLETED,
    StopReason.NO_FIX_NEEDED: TaskStatus.COMPLETED,
    StopReason.DIAGNOSED: TaskStatus.COMPLETED,
    StopReason.ITERATION_LIMIT: TaskStatus.STOPPED,
    StopReason.NO_PROGRESS: TaskStatus.STOPPED,
    StopReason.INSUFFICIENT_INFORMATION: TaskStatus.STOPPED,
    StopReason.APPROVAL_PENDING: TaskStatus.AWAITING_APPROVAL,
    StopReason.SAFETY_VIOLATION: TaskStatus.FAILED,
    StopReason.PROVIDER_UNAVAILABLE: TaskStatus.FAILED,
    StopReason.USER_INTERRUPTED: TaskStatus.CANCELLED,
}

_TERMINAL = {TaskStatus.COMPLETED, TaskStatus.STOPPED, TaskStatus.FAILED, TaskStatus.CANCELLED}


class ActionStage(StrEnum):
    REQUESTED = "requested"
    AUTHORIZED = "authorized"
    DENIED = "denied"
    EXECUTED = "executed"
    FAILED = "failed"


_ALLOWED_TRANSITIONS: dict[ActionStage, set[ActionStage]] = {
    ActionStage.REQUESTED: {ActionStage.AUTHORIZED, ActionStage.DENIED, ActionStage.FAILED},
    ActionStage.AUTHORIZED: {ActionStage.EXECUTED, ActionStage.FAILED},
    ActionStage.DENIED: set(),
    ActionStage.EXECUTED: set(),
    ActionStage.FAILED: set(),
}


class ActionRecord(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    seq: int = Field(ge=1, description="Monotonic sequence number within the task.")
    iteration: int = Field(ge=0)
    call: ToolCall
    permission: PermissionLevel | None = Field(
        default=None, description="None when the tool is unknown or the call could not be parsed."
    )
    stage: ActionStage = ActionStage.REQUESTED
    decision: Decision | None = None
    result: ToolResult | None = None
    requested_at: datetime = Field(default_factory=_now)

    @property
    def modified_project(self) -> bool:
        return (
            self.stage is ActionStage.EXECUTED
            and self.permission in (PermissionLevel.WRITE, PermissionLevel.DESTRUCTIVE)
            and self.result is not None
            and self.result.ok
        )


class VerificationResult(BaseModel):
    """Outcome of an independent check (e.g. running the test suite), not a model claim."""

    model_config = ConfigDict(frozen=True)

    command: str = Field(min_length=1)
    passed: bool
    exit_code: int | None = None
    summary: str = ""
    after_action_seq: int = Field(ge=0, description="Number of actions recorded when this check ran.")
    recorded_at: datetime = Field(default_factory=_now)


class AgentTask(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    id: str = Field(default_factory=lambda: uuid4().hex)
    description: str = Field(min_length=1)
    project_root: Path
    status: TaskStatus = TaskStatus.PENDING
    max_iterations: int = Field(default=30, ge=1)
    iteration: int = Field(default=0, ge=0)
    actions: list[ActionRecord] = Field(default_factory=list)
    verifications: list[VerificationResult] = Field(default_factory=list)
    stop_reason: StopReason | None = None
    summary: str | None = None
    report: dict[str, Any] | None = Field(
        default=None, description="Structured final report from the agent (root cause, evidence, fix...)."
    )
    model_calls: int = Field(default=0, ge=0)
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=_now)
    finished_at: datetime | None = None

    # ---- lifecycle -------------------------------------------------------

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL

    def start(self) -> None:
        if self.status is not TaskStatus.PENDING:
            raise TaskStateError(f"cannot start a task in status '{self.status}'")
        self.status = TaskStatus.RUNNING

    def resume(self) -> None:
        """Continue after a pending approval has been resolved."""
        if self.status is not TaskStatus.AWAITING_APPROVAL:
            raise TaskStateError(f"cannot resume a task in status '{self.status}'")
        self.status = TaskStatus.RUNNING
        self.stop_reason = None

    @property
    def iterations_remaining(self) -> int:
        return self.max_iterations - self.iteration

    def begin_iteration(self) -> int:
        self._require_running()
        if self.iterations_remaining <= 0:
            raise TaskStateError("iteration limit reached; finish the task with ITERATION_LIMIT")
        self.iteration += 1
        return self.iteration

    # ---- actions ---------------------------------------------------------

    def record_action(self, call: ToolCall, permission: PermissionLevel | None) -> ActionRecord:
        self._require_running()
        record = ActionRecord(seq=len(self.actions) + 1, iteration=self.iteration, call=call, permission=permission)
        self.actions.append(record)
        return record

    def advance_action(
        self,
        seq: int,
        stage: ActionStage,
        *,
        decision: Decision | None = None,
        result: ToolResult | None = None,
    ) -> ActionRecord:
        record = self._action(seq)
        if stage not in _ALLOWED_TRANSITIONS[record.stage]:
            raise TaskStateError(f"action {seq}: cannot move from '{record.stage}' to '{stage}'")
        if stage in (ActionStage.EXECUTED, ActionStage.FAILED) and result is None:
            raise TaskStateError(f"action {seq}: '{stage}' requires the actual tool result")
        record.stage = stage
        if decision is not None:
            record.decision = decision
        if result is not None:
            record.result = result
        return record

    def _action(self, seq: int) -> ActionRecord:
        if not 1 <= seq <= len(self.actions):
            raise TaskStateError(f"no action with seq {seq}")
        return self.actions[seq - 1]

    # ---- verification ----------------------------------------------------

    def add_verification(
        self, command: str, passed: bool, *, exit_code: int | None = None, summary: str = ""
    ) -> VerificationResult:
        self._require_running()
        result = VerificationResult(
            command=command,
            passed=passed,
            exit_code=exit_code,
            summary=summary,
            after_action_seq=len(self.actions),
        )
        self.verifications.append(result)
        return result

    @property
    def last_modification_seq(self) -> int | None:
        for record in reversed(self.actions):
            if record.modified_project:
                return record.seq
        return None

    def is_fix_verified(self) -> bool:
        """True only if the project was modified and the latest post-modification check passed."""
        last_mod = self.last_modification_seq
        if last_mod is None:
            return False
        after = [v for v in self.verifications if v.after_action_seq >= last_mod]
        return bool(after) and after[-1].passed

    # ---- completion ------------------------------------------------------

    def finish(self, reason: StopReason, summary: str) -> None:
        if self.is_terminal:
            raise TaskStateError(f"task already finished with status '{self.status}'")
        if not summary.strip():
            raise TaskStateError("finishing a task requires a summary for the user")
        if reason is StopReason.VERIFIED_FIX and not self.is_fix_verified():
            raise TaskStateError(
                "cannot mark the task as a verified fix: no passing verification after the last modification"
            )
        self.stop_reason = reason
        self.summary = summary
        self.status = _STATUS_FOR_REASON[reason]
        if self.status is not TaskStatus.AWAITING_APPROVAL:
            self.finished_at = _now()

    def _require_running(self) -> None:
        if self.status is not TaskStatus.RUNNING:
            raise TaskStateError(f"task is '{self.status}', not running")
