"""Terminal rendering of agent events and interactive approvals."""

from __future__ import annotations

import json
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Confirm

from trace_agent.agent.executor import ApprovalRequest
from trace_agent.agent.state import AgentTask, StopReason, TaskStatus
from trace_agent.observability.events import EventType, ExecutionEvent


def _args(arguments: dict[str, Any], limit: int = 120) -> str:
    text = ", ".join(f"{k}={json.dumps(v, default=str)}" for k, v in arguments.items())
    return text if len(text) <= limit else text[:limit] + "..."


def _first_line(text: str | None, limit: int = 160) -> str:
    if not text:
        return ""
    line = text.strip().splitlines()[0] if text.strip() else ""
    return line if len(line) <= limit else line[:limit] + "..."


class ConsoleEventPrinter:
    """EventSink that shows progress: current step, tool being run, its outcome."""

    def __init__(self, console: Console, *, verbose: bool = False) -> None:
        self.console = console
        self.verbose = verbose

    def emit(self, event: ExecutionEvent) -> None:
        p = event.payload
        c = self.console
        match event.type:
            case EventType.TASK_STARTED:
                c.print(
                    f"[dim]Task {event.task_id[:8]} | {escape(p['provider'])}/{escape(p['model'])} | "
                    f"mode {p['mode']} | tools: {', '.join(p['tools'])}[/dim]",
                    soft_wrap=True,
                )
            case EventType.MODEL_REQUEST:
                c.print(f"\n[bold cyan]Step {event.iteration}[/bold cyan] [dim]thinking...[/dim]")
            case EventType.MODEL_RESPONSE:
                if p.get("content") and (self.verbose or not p.get("tool_calls")):
                    c.print(f"  [italic]{escape(_first_line(p['content'], 400))}[/italic]", soft_wrap=True)
            case EventType.TOOL_REQUESTED:
                c.print(f"  [yellow]-> {escape(p['tool'])}[/yellow]({escape(_args(p['arguments']))})", soft_wrap=True)
            case EventType.TOOL_DENIED:
                c.print(f"    [red]denied:[/red] {escape(p['reason'] or '')}", soft_wrap=True)
            case EventType.TOOL_EXECUTED:
                if p["status"] == "success":
                    detail = _summarize_data(p.get("data") or {})
                    c.print(f"    [green]ok[/green] [dim]{escape(detail)} {p['duration_ms']:.0f}ms[/dim]", soft_wrap=True)
                else:
                    c.print(f"    [red]{p['status']}:[/red] {escape(p.get('error') or '')}", soft_wrap=True)
                if self.verbose and p.get("output_preview"):
                    c.print(f"[dim]{escape(p['output_preview'])}[/dim]", soft_wrap=True)
            case EventType.CONTEXT_COLLECTED:
                bits = [", ".join(p.get("languages") or []) or "unknown language"]
                if p.get("test_command"):
                    bits.append(f"tests: {p['test_command']}")
                if p.get("traces"):
                    bits.append(f"{len(p['traces'])} stack trace(s), {len(p.get('project_frames') or [])} project frames")
                if p.get("message_hits"):
                    bits.append(f"error text found at {', '.join(p['message_hits'][:3])}")
                c.print(f"[dim]Context: {escape(' | '.join(bits))}[/dim]", soft_wrap=True)
            case EventType.AGENT_DECISION if p.get("decision") == "hypothesis":
                style = {"supported": "green", "rejected": "red"}.get(p["status"], "blue")
                c.print(
                    f"  [{style}]hypothesis #{p['id']} {p['status']}:[/{style}] {escape(_first_line(p['statement'], 200))}",
                    soft_wrap=True,
                )
            case EventType.AGENT_DECISION if p.get("decision") == "finish_rejected":
                c.print(f"  [magenta]finish rejected:[/magenta] {escape(p['reason'])}", soft_wrap=True)
            case EventType.ERROR:
                c.print(f"  [red]error ({escape(p.get('type', ''))}):[/red] {escape(p.get('message', ''))}", soft_wrap=True)
            case _:
                pass


def _summarize_data(data: dict[str, Any]) -> str:
    if "total_lines" in data:
        return f"{data.get('path')} lines {data.get('start_line')}-{data.get('end_line')} of {data['total_lines']}"
    if "match_count" in data:
        return f"{data['match_count']} matches in {data.get('files', 0)} files"
    if "count" in data:
        return f"{data['count']} entries"
    return ""


_STATUS_STYLE = {
    TaskStatus.COMPLETED: "green",
    TaskStatus.STOPPED: "yellow",
    TaskStatus.FAILED: "red",
    TaskStatus.CANCELLED: "yellow",
    TaskStatus.AWAITING_APPROVAL: "yellow",
}


def render_final_report(console: Console, task: AgentTask) -> None:
    style = _STATUS_STYLE.get(task.status, "white")
    reason = task.stop_reason.value if task.stop_reason else "-"
    lines = [f"[bold]Status:[/bold] [{style}]{task.status.value}[/{style}] ({reason})"]
    if task.stop_reason is StopReason.DIAGNOSED:
        lines.append("[dim]Diagnosis only: no files were changed and no fix was verified.[/dim]")
    lines.append("")
    lines.append(escape(task.summary or ""))
    report = task.report or {}
    if report.get("root_cause"):
        lines += ["", "[bold]Root cause[/bold]", escape(report["root_cause"])]
    if report.get("evidence"):
        lines += ["", "[bold]Evidence[/bold]", *[f"- {escape(e)}" for e in report["evidence"]]]
    if report.get("recommended_fix"):
        lines += ["", "[bold]Recommended fix[/bold]", escape(report["recommended_fix"])]
    if report.get("confidence"):
        lines += ["", f"[bold]Confidence:[/bold] {report['confidence']}"]
    if task.hypotheses:
        lines += ["", "[bold]Hypotheses considered[/bold]"]
        for h in task.hypotheses:
            lines.append(escape(f"#{h.id} [{h.status.value}] {_first_line(h.statement, 200)}"))
    tokens = task.prompt_tokens + task.completion_tokens
    lines += [
        "",
        f"[dim]{task.iteration} steps | {task.model_calls} model calls | {len(task.actions)} tool calls | "
        f"{tokens:,} tokens | task {task.id[:8]}[/dim]",
    ]
    console.print(Panel("\n".join(lines), title="Trace result", border_style=style))


class TerminalApprover:
    """Ask the user in the terminal before any action the policy marks ASK."""

    def __init__(self, console: Console) -> None:
        self.console = console

    def approve(self, request: ApprovalRequest) -> bool:
        self.console.print(
            f"\n[bold yellow]Approval needed[/bold yellow] ({request.permission.value}): "
            f"{escape(request.tool_name)}({escape(_args(request.arguments, 400))})",
            soft_wrap=True,
        )
        return Confirm.ask("Allow this action?", console=self.console, default=False)
