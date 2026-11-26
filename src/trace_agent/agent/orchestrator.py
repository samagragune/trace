"""The Observe-Reason-Act-Verify loop.

The orchestrator owns *control*, never *strategy*: it asks the model what to do, routes
each requested action through the ToolExecutor (validation + assessment + authorization +
execution), feeds back what actually happened, and enforces limits. The order of
investigation, editing and testing comes entirely from the model's decisions.

Per task it prepares a *workspace* (Build 004): a git snapshot of files with uncommitted
user changes (edits to them always need approval), a checkpoint store (every changed file is
backed up first and can be restored with ``trace undo``), and the project's test command.

Verification (ADR-006/029): when the model claims "fixed", the orchestrator runs the project's
test command **itself** and accepts the claim only if Trace observed a successful modification
followed by a passing verification.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from trace_agent.agent.builtins import (
    BUILTIN_TOOL_NAMES,
    BUILTIN_TOOLS,
    FINISH_TOOL,
    FINISH_TOOL_NAME,
    HYPOTHESIS_TOOL_NAME,
    OUTCOME_REASON,
    FinishInput,
    HypothesisInput,
    Outcome,
)
from trace_agent.agent.executor import Approver, DenyAllApprover, ToolExecutor
from trace_agent.agent.observation import ProgressMonitor, render_tool_result, repeat_warning
from trace_agent.agent.prompts import DEFAULT_SYSTEM_PROMPT, build_task_message
from trace_agent.agent.state import AgentTask, StopReason, TaskStateError
from trace_agent.core.messages import Message, Role, ToolCall, ToolDefinition
from trace_agent.observability.events import EventSink, EventType, ExecutionEvent, InMemoryEventSink
from trace_agent.providers.base import CompletionRequest, ModelProvider
from trace_agent.providers.errors import ProviderError
from trace_agent.providers.registry import DEFAULT_PROVIDERS
from trace_agent.safety.checkpoints import CheckpointStore
from trace_agent.safety.git_guard import dirty_files
from trace_agent.safety.permissions import DEFAULT_EXECUTION_MODE, ExecutionMode
from trace_agent.safety.redaction import Redactor
from trace_agent.tools.base import ToolContext
from trace_agent.tools.registry import ToolRegistry

# Re-exported for callers/tests that import them from here.
__all__ = ["FINISH_TOOL", "FINISH_TOOL_NAME", "HYPOTHESIS_TOOL_NAME", "Orchestrator", "OrchestratorSettings", "Outcome"]

IDLE_NUDGE = (
    "Trace: your last reply contained no tool call. Continue investigating with the tools, "
    f"or call {FINISH_TOOL_NAME} to end the task."
)
ELIDED_NOTE = "[earlier tool output elided by Trace to stay within the context budget; re-run the tool if you need it]"
VERIFY_CALL_PREFIX = "trace-verify-"


def _default_scrub_env() -> frozenset[str]:
    return frozenset(spec.api_key_env for spec in DEFAULT_PROVIDERS if spec.api_key_env)


@dataclass(frozen=True)
class OrchestratorSettings:
    mode: ExecutionMode = DEFAULT_EXECUTION_MODE
    max_iterations: int = 30
    max_output_chars: int = 20_000
    tool_timeout_seconds: float = 60.0
    temperature: float | None = None
    max_repeated_calls: int = 4
    max_idle_responses: int = 2
    collect_context: bool = True
    # ---- Build 004 ----------------------------------------------------------------------------
    checkpoint_dir: Path | None = None  # None disables file changes (nothing could be undone)
    test_command: str | None = None  # overrides the detected test command
    independent_verification: bool = True  # Trace re-runs the tests itself before accepting "fixed"
    max_fix_attempts: int = 3  # failed post-edit verifications allowed before stopping
    max_blocked_attempts: int = 3  # blocked-command attempts before SAFETY_VIOLATION
    max_context_chars: int = 160_000  # conversation budget before old tool outputs are elided
    keep_recent_tool_outputs: int = 6
    scrub_env: frozenset[str] | None = None  # env vars hidden from child processes (default: provider keys)


class Orchestrator:
    def __init__(
        self,
        provider: ModelProvider,
        registry: ToolRegistry,
        *,
        settings: OrchestratorSettings = OrchestratorSettings(),
        sink: EventSink | None = None,
        approver: Approver | None = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        redactor: Redactor | None = None,
    ) -> None:
        reserved = BUILTIN_TOOL_NAMES & set(registry.names())
        if reserved:
            raise ValueError(f"tool names reserved for the orchestrator: {', '.join(sorted(reserved))}")
        self.provider = provider
        self.registry = registry
        self.settings = settings
        self.sink = sink or InMemoryEventSink()
        self.approver = approver or DenyAllApprover()
        self.system_prompt = system_prompt
        self.redactor = redactor or Redactor()

    # ------------------------------------------------------------------ public API

    def run(self, description: str, project_root: Path) -> AgentTask:
        root = project_root.resolve()
        task = AgentTask(description=description, project_root=root, max_iterations=self.settings.max_iterations)
        task.start()
        emit = self._emitter(task)

        # Collected before TASK_STARTED (the workspace needs the test command), but its events are
        # emitted afterwards so every trace starts with TASK_STARTED.
        pending_events: list[tuple[EventType, dict[str, Any]]] = []
        opening, detected_test_command = self._opening_context(
            description, root, lambda t, p: pending_events.append((t, p))
        )
        checkpoints = CheckpointStore(self.settings.checkpoint_dir, task.id, root) if self.settings.checkpoint_dir else None
        protected = dirty_files(root) if checkpoints is not None else None
        context = ToolContext(
            project_root=root,
            timeout_seconds=self.settings.tool_timeout_seconds,
            max_output_chars=self.settings.max_output_chars,
            checkpoints=checkpoints,
            protected_paths=protected or frozenset(),
            test_command=self.settings.test_command or detected_test_command,
            scrub_env=self.settings.scrub_env if self.settings.scrub_env is not None else _default_scrub_env(),
        )
        task.checkpoint_dir = str(checkpoints.directory) if checkpoints else None
        executor = ToolExecutor(self.registry, self.settings.mode, self.approver, emit, redactor=self.redactor)
        monitor = ProgressMonitor(stop_after=self.settings.max_repeated_calls, max_idle=self.settings.max_idle_responses)
        tools = (*self.registry.definitions(context), *BUILTIN_TOOLS)
        tool_names = [t.name for t in tools]

        emit(
            EventType.TASK_STARTED,
            {
                "description": description,
                "project_root": str(root),
                "mode": self.settings.mode.value,
                "provider": self.provider.provider_id,
                "model": self.provider.model,
                "tools": tool_names,
                "test_command": context.test_command,
                "git_protected_files": sorted(context.protected_paths)[:50],
                "git_repository": protected is not None,
                "checkpoints": task.checkpoint_dir,
            },
        )
        for event_type, payload in pending_events:
            emit(event_type, payload)
        task_message = build_task_message(description, root, self.settings.mode, tool_names, context.test_command)
        messages: list[Message] = [Message.system(self.system_prompt), Message.user(task_message + opening)]

        try:
            self._loop(task, messages, tools, context, executor, monitor, emit)
        except KeyboardInterrupt:
            context.cancel_event.set()
            if not task.is_terminal:
                task.finish(StopReason.USER_INTERRUPTED, "Interrupted by the user.")
        except Exception as exc:  # noqa: BLE001 - never lose the task record because of a Trace bug
            context.cancel_event.set()
            import traceback

            emit(
                EventType.ERROR,
                {"source": "orchestrator", "type": type(exc).__name__, "message": str(exc),
                 "traceback": traceback.format_exc(limit=8)},
            )
            if not task.is_terminal:
                task.finish(
                    StopReason.INTERNAL_ERROR,
                    f"Trace hit an internal error and stopped safely ({type(exc).__name__}: {exc}). "
                    "No further changes were made; changed files (if any) can be restored with `trace undo`.",
                )

        task.changed_files = checkpoints.changed_files() if checkpoints else []
        emit(
            EventType.TASK_FINISHED,
            {
                "status": task.status.value,
                "stop_reason": task.stop_reason.value if task.stop_reason else None,
                "summary": task.summary,
                "iterations": task.iteration,
                "model_calls": task.model_calls,
                "prompt_tokens": task.prompt_tokens,
                "completion_tokens": task.completion_tokens,
                "hypotheses": [h.model_dump(mode="json") for h in task.hypotheses],
                "changed_files": task.changed_files,
                "verifications": [v.model_dump(mode="json") for v in task.verifications],
                "checkpoints": task.checkpoint_dir,
            },
        )
        return task

    # ------------------------------------------------------------------ observe (up front)

    def _opening_context(self, description: str, root: Path, emit: Any) -> tuple[str, str | None]:
        """Returns (rendered context to append to the task message, detected test command)."""
        from trace_agent.context.project import detect_project  # lazy: filesystem scan
        from trace_agent.context.selection import build_task_context

        try:
            if not self.settings.collect_context:
                return "", detect_project(root).test_command
            selected = build_task_context(description, root)
        except Exception as exc:  # noqa: BLE001 - context is an optimization; never block the task on it
            emit(EventType.ERROR, {"source": "context", "type": type(exc).__name__, "message": str(exc)})
            return "", None
        emit(EventType.CONTEXT_COLLECTED, selected.summary())
        return f"\n\n{self.redactor.text(selected.render())}", selected.project.test_command

    # ------------------------------------------------------------------ the loop

    def _loop(
        self,
        task: AgentTask,
        messages: list[Message],
        tools: tuple[ToolDefinition, ...],
        context: ToolContext,
        executor: ToolExecutor,
        monitor: ProgressMonitor,
        emit: Any,
    ) -> None:
        while True:
            if task.iterations_remaining <= 0:
                task.finish(
                    StopReason.ITERATION_LIMIT,
                    f"Stopped after {task.max_iterations} iterations without a conclusion.",
                )
                return
            iteration = task.begin_iteration()
            self._compact(messages, emit)

            # Reason ------------------------------------------------------------------
            request = CompletionRequest(messages=tuple(messages), tools=tools, temperature=self.settings.temperature)
            emit(EventType.MODEL_REQUEST, {"messages": len(messages), "iteration": iteration})
            try:
                response = self.provider.complete(request)
            except ProviderError as exc:
                emit(EventType.ERROR, {"source": "provider", "type": type(exc).__name__, "message": str(exc)})
                task.finish(StopReason.PROVIDER_UNAVAILABLE, f"The model provider failed: {exc}")
                return

            task.model_calls += 1
            task.prompt_tokens += response.usage.prompt_tokens or 0
            task.completion_tokens += response.usage.completion_tokens or 0
            emit(
                EventType.MODEL_RESPONSE,
                {
                    "finish_reason": response.finish_reason.value,
                    "content": response.message.content,
                    "tool_calls": [{"name": c.name, "arguments": c.arguments} for c in response.tool_calls],
                    "usage": response.usage.model_dump(exclude_none=True),
                },
            )
            messages.append(response.message)

            # Decide ----------------------------------------------------------------
            if not response.tool_calls:
                if monitor.record_idle():
                    task.finish(
                        StopReason.NO_PROGRESS,
                        response.message.content or "The model stopped calling tools without finishing.",
                    )
                    return
                messages.append(Message.user(IDLE_NUDGE))
                continue
            monitor.record_activity()

            finish_calls = [c for c in response.tool_calls if c.name == FINISH_TOOL_NAME]
            other_calls = [c for c in response.tool_calls if c.name != FINISH_TOOL_NAME]

            # Act + observe -------------------------------------------------------------
            for call in other_calls:
                count = monitor.record_call(call)
                if call.name == HYPOTHESIS_TOOL_NAME:
                    content = self._record_hypothesis(task, call, emit)
                else:
                    content = render_tool_result(executor.execute(task, call, context))
                    content += self._fix_attempt_note(task)
                if monitor.should_warn(count):
                    content += repeat_warning(count)
                messages.append(Message.tool_result(call.id, content))
                if monitor.should_stop(count):
                    emit(EventType.AGENT_DECISION, {"decision": "stop", "reason": "repeated identical tool calls"})
                    task.finish(
                        StopReason.NO_PROGRESS,
                        f"Stopped: the model repeated the same '{call.name}' call {count} times.",
                    )
                    return
                if executor.blocked_attempts >= self.settings.max_blocked_attempts:
                    emit(EventType.AGENT_DECISION, {"decision": "stop", "reason": "repeated blocked commands"})
                    task.finish(
                        StopReason.SAFETY_VIOLATION,
                        f"Stopped: the model attempted {executor.blocked_attempts} commands that Trace's safety "
                        "rules block.",
                    )
                    return
                if self._failed_fix_attempts(task) > self.settings.max_fix_attempts:
                    emit(EventType.AGENT_DECISION, {"decision": "stop", "reason": "fix attempts exhausted"})
                    task.finish(
                        StopReason.NO_PROGRESS,
                        f"Stopped: verification still failed after {self.settings.max_fix_attempts} fix attempts. "
                        "Changed files can be restored with `trace undo`.",
                    )
                    return

            # Evaluate -----------------------------------------------------------------
            for index, call in enumerate(finish_calls):
                if index == 0 and not other_calls and self._try_finish(task, call, messages, context, executor, emit):
                    return
                if index > 0 or other_calls:
                    messages.append(
                        Message.tool_result(
                            call.id,
                            f"status: rejected\nerror: call {FINISH_TOOL_NAME} on its own, after reviewing the "
                            "results of your other tool calls.",
                        )
                    )

    # ------------------------------------------------------------------ fix attempts

    @staticmethod
    def _failed_fix_attempts(task: AgentTask) -> int:
        """Failed verifications recorded after the first successful modification."""
        first_mod = next((a.seq for a in task.actions if a.modified_project), None)
        if first_mod is None:
            return 0
        return sum(1 for v in task.verifications if v.after_action_seq >= first_mod and not v.passed)

    def _fix_attempt_note(self, task: AgentTask) -> str:
        failed = self._failed_fix_attempts(task)
        if failed and failed == self.settings.max_fix_attempts:
            return (
                f"\nnote from Trace: verification has failed {failed} times after your changes. One more "
                "failed attempt stops the task; reconsider the root cause before editing again."
            )
        return ""

    # ------------------------------------------------------------------ context budget

    def _compact(self, messages: list[Message], emit: Any) -> None:
        """Elide the oldest tool outputs when the conversation exceeds the character budget."""
        def size() -> int:
            return sum(len(m.content or "") + sum(len(str(c.arguments)) for c in m.tool_calls) for m in messages)

        total = size()
        if total <= self.settings.max_context_chars:
            return
        tool_indexes = [i for i, m in enumerate(messages) if m.role is Role.TOOL]
        recent = set(tool_indexes[-self.settings.keep_recent_tool_outputs :])
        elided = 0
        for i in tool_indexes:
            if total <= self.settings.max_context_chars:
                break
            message = messages[i]
            if i in recent or (message.content or "").endswith(ELIDED_NOTE):
                continue
            first_line = (message.content or "").split("\n", 1)[0][:200]
            replacement = f"{first_line}\n{ELIDED_NOTE}"
            total -= len(message.content or "") - len(replacement)
            messages[i] = Message.tool_result(message.tool_call_id or "", replacement)
            elided += 1
        if elided:
            emit(EventType.AGENT_DECISION, {"decision": "context_compacted", "elided_tool_outputs": elided, "chars": total})

    # ------------------------------------------------------------------ built-ins

    def _record_hypothesis(self, task: AgentTask, call: ToolCall, emit: Any) -> str:
        if call.arguments_error:
            return f"status: invalid_input\nerror: arguments were not valid JSON ({call.arguments_error})"
        try:
            args = HypothesisInput.model_validate(call.arguments)
            hypothesis = task.record_hypothesis(args.statement, args.status, args.evidence, args.id)
        except ValidationError as exc:
            problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
            return f"status: invalid_input\nerror: {problems}"
        except TaskStateError as exc:
            return f"status: invalid_input\nerror: {exc}"
        emit(EventType.AGENT_DECISION, {"decision": "hypothesis", **hypothesis.model_dump(mode="json")})
        return (
            f"status: success\noutput: hypothesis #{hypothesis.id} is now {hypothesis.status.value} "
            f"({len(hypothesis.evidence)} evidence items)"
        )

    def _try_finish(
        self,
        task: AgentTask,
        call: ToolCall,
        messages: list[Message],
        context: ToolContext,
        executor: ToolExecutor,
        emit: Any,
    ) -> bool:
        def reject(reason: str) -> bool:
            emit(EventType.AGENT_DECISION, {"decision": "finish_rejected", "reason": reason})
            messages.append(Message.tool_result(call.id, f"status: rejected\nerror: {reason}"))
            return False

        if call.arguments_error:
            return reject(f"arguments were not valid JSON ({call.arguments_error})")
        try:
            report = FinishInput.model_validate(call.arguments)
        except ValidationError as exc:
            problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
            return reject(f"invalid finish_task arguments: {problems}")

        if report.outcome is Outcome.FIXED:
            if task.last_modification_seq is None:
                return reject(
                    "outcome 'fixed' requires a code change, and Trace has not observed a successful modification. "
                    "Use 'diagnosed' with a recommended_fix, or make the change first."
                )
            independent = self._independent_verification(task, context, executor, emit)
            if not task.is_fix_verified():
                detail = f" Trace's own test run: {independent}." if independent else ""
                return reject(
                    "outcome 'fixed' requires a passing verification after your last change, and Trace has not "
                    f"observed one.{detail} Run the tests, fix what fails, then finish again."
                )

        emit(EventType.AGENT_DECISION, {"decision": "finish", **report.model_dump(mode="json")})
        task.report = report.model_dump(mode="json")
        task.finish(OUTCOME_REASON[report.outcome], report.summary)
        return True

    def _independent_verification(self, task: AgentTask, context: ToolContext, executor: ToolExecutor, emit: Any) -> str | None:
        """Run the project's test command through the normal executor path; returns a summary."""
        if not (self.settings.independent_verification and context.test_command and "run_tests" in self.registry):
            return None
        call = ToolCall(id=f"{VERIFY_CALL_PREFIX}{len(task.verifications) + 1}", name="run_tests", arguments={})
        emit(EventType.AGENT_DECISION, {"decision": "independent_verification", "command": context.test_command})
        result = executor.execute(task, call, context)
        verification = result.data.get("verification") or {}
        return f"{'passed' if verification.get('passed') else 'failed'}: {verification.get('summary') or result.error}"

    # ------------------------------------------------------------------ events

    def _emitter(self, task: AgentTask) -> Any:
        def emit(event_type: EventType, payload: dict[str, Any]) -> None:
            self.sink.emit(ExecutionEvent(task_id=task.id, type=event_type, iteration=task.iteration, payload=payload))

        return emit
