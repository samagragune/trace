"""The Observe-Reason-Act-Verify loop.

The orchestrator owns *control*, never *strategy*: it asks the model what to do, routes
each requested action through the ToolExecutor (validation + authorization + execution),
feeds back what actually happened, and enforces limits. The order of investigation comes
entirely from the model's decisions.

Observe (step 2 of the spec's loop) happens up front: ``context.selection`` builds a
bounded opening context (project overview, parsed stack traces, code at the failing
frames, locations of quoted error text) so the model starts oriented instead of
spending its first iterations on discovery.

Built-in tools act on task state rather than the machine (``agent.builtins``):
``record_hypothesis`` makes reasoning inspectable; ``finish_task`` ends the task, and
its claimed outcome is checked ("fixed" requires a verified modification).
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
from trace_agent.core.messages import Message, ToolCall, ToolDefinition
from trace_agent.observability.events import EventSink, EventType, ExecutionEvent, InMemoryEventSink
from trace_agent.providers.base import CompletionRequest, ModelProvider
from trace_agent.providers.errors import ProviderError
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
        context = ToolContext(
            project_root=root,
            timeout_seconds=self.settings.tool_timeout_seconds,
            max_output_chars=self.settings.max_output_chars,
        )
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
            },
        )
        task_message = build_task_message(description, root, self.settings.mode, tool_names)
        task_message += self._opening_context(description, root, emit)
        messages: list[Message] = [Message.system(self.system_prompt), Message.user(task_message)]

        try:
            self._loop(task, messages, tools, context, executor, monitor, emit)
        except KeyboardInterrupt:
            context.cancel_event.set()
            if not task.is_terminal:
                task.finish(StopReason.USER_INTERRUPTED, "Interrupted by the user.")

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
            },
        )
        return task

    # ------------------------------------------------------------------ observe (up front)

    def _opening_context(self, description: str, root: Path, emit: Any) -> str:
        if not self.settings.collect_context:
            return ""
        from trace_agent.context.selection import build_task_context  # lazy: filesystem scan

        try:
            selected = build_task_context(description, root)
        except Exception as exc:  # noqa: BLE001 - context is an optimization; never block the task on it
            emit(EventType.ERROR, {"source": "context", "type": type(exc).__name__, "message": str(exc)})
            return ""
        emit(EventType.CONTEXT_COLLECTED, selected.summary())
        rendered = self.redactor.text(selected.render())
        return f"\n\n{rendered}"

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

            # Evaluate -----------------------------------------------------------------
            for index, call in enumerate(finish_calls):
                if index == 0 and not other_calls and self._try_finish(task, call, messages, emit):
                    return
                if index > 0 or other_calls:
                    messages.append(
                        Message.tool_result(
                            call.id,
                            f"status: rejected\nerror: call {FINISH_TOOL_NAME} on its own, after reviewing the "
                            "results of your other tool calls.",
                        )
                    )

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

    def _try_finish(self, task: AgentTask, call: ToolCall, messages: list[Message], emit: Any) -> bool:
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

        if report.outcome is Outcome.FIXED and not task.is_fix_verified():
            return reject(
                "outcome 'fixed' requires a successful code modification followed by a passing verification, "
                "and Trace has not observed one. Use 'diagnosed' with a recommended_fix instead, or verify first."
            )

        emit(EventType.AGENT_DECISION, {"decision": "finish", **report.model_dump(mode="json")})
        task.report = report.model_dump(mode="json")
        task.finish(OUTCOME_REASON[report.outcome], report.summary)
        return True

    # ------------------------------------------------------------------ events

    def _emitter(self, task: AgentTask) -> Any:
        def emit(event_type: EventType, payload: dict[str, Any]) -> None:
            self.sink.emit(ExecutionEvent(task_id=task.id, type=event_type, iteration=task.iteration, payload=payload))

        return emit
