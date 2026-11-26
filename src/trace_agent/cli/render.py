"""Terminal rendering of agent events and interactive approvals."""

from __future__ import annotations

import json
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Prompt

from trace_agent.agent.executor import ApprovalRequest
from trace_agent.agent.state import AgentTask, StopReason, TaskStatus
from trace_agent.observability.events import EventType, ExecutionEvent
from trace_agent.safety.permissions import PermissionLevel


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
            case EventType.FILE_MODIFIED:
                c.print(
                    f"    [magenta]{escape(p['change'])}[/magenta] {escape(p['path'])} "
                    f"[green]+{p.get('added', 0)}[/green]/[red]-{p.get('removed', 0)}[/red]",
                    soft_wrap=True,
                )
                if self.verbose and p.get("diff"):
                    render_diff(c, p["diff"], max_lines=40)
            case EventType.VERIFICATION:
                verdict = "[green]tests passed[/green]" if p["passed"] else "[red]tests failed[/red]"
                c.print(f"    {verdict}: {escape(p.get('summary') or '')}", soft_wrap=True)
            case EventType.AGENT_DECISION if p.get("decision") == "independent_verification":
                c.print(f"  [cyan]Trace is verifying independently:[/cyan] {escape(p['command'])}", soft_wrap=True)
            case EventType.AGENT_DECISION if p.get("decision") == "context_compacted":
                c.print(f"  [dim]context compacted: {p['elided_tool_outputs']} old tool outputs elided[/dim]")
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
    if "exit_code" in data:
        return f"exit {data['exit_code']} in {data.get('duration_s', 0)}s"
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
    if task.changed_files:
        lines += ["", "[bold]Files changed by Trace[/bold]"]
        lines += [escape(f"- {c['change']}: {c['path']}") for c in task.changed_files]
    if task.command_changed_files:
        lines += ["", "[bold]Files changed by commands[/bold] [dim](not covered by trace undo; use git)[/dim]"]
        lines += [escape(f"- {path}") for path in task.command_changed_files[:20]]
    if task.verifications:
        last = task.verifications[-1]
        verdict = "[green]passed[/green]" if last.passed else "[red]failed[/red]"
        lines += ["", f"[bold]Last verification:[/bold] {verdict} {escape(last.command)} | {escape(last.summary)}"]
    if task.changed_files and task.checkpoint_dir:
        lines += ["", f"[dim]Originals are backed up. Undo all changes: trace undo {task.id[:8]}[/dim]"]
    tokens = task.prompt_tokens + task.completion_tokens
    lines += [
        "",
        f"[dim]{task.iteration} steps | {task.model_calls} model calls | {len(task.actions)} tool calls | "
        f"{tokens:,} tokens | task {task.id[:8]}[/dim]",
    ]
    console.print(Panel("\n".join(lines), title="Trace result", border_style=style))


def render_diff(console: Console, diff: str, max_lines: int = 80) -> None:
    lines = diff.splitlines()
    for line in lines[:max_lines]:
        if line.startswith(("+++", "---")):
            style = "bold"
        elif line.startswith("+"):
            style = "green"
        elif line.startswith("-"):
            style = "red"
        elif line.startswith("@@"):
            style = "cyan"
        else:
            style = "dim"
        console.print(f"[{style}]{escape(line)}[/{style}]", soft_wrap=True)
    if len(lines) > max_lines:
        console.print(f"[dim]... {len(lines) - max_lines} more diff lines[/dim]")


class TerminalApprover:
    """Ask the user in the terminal before any action the policy marks ASK.

    Shows the reason and a preview (diff or command). Answers: y = once, n = deny,
    a = allow this tool for the rest of the task. "a" is never offered for destructive
    actions or for requests that carry a special reason (e.g. uncommitted user changes).
    """

    def __init__(self, console: Console) -> None:
        self.console = console
        self._always: set[str] = set()
        self._input_closed = False
        self.denial_reason: str | None = None

    def approve(self, request: ApprovalRequest) -> bool:
        if self._input_closed:
            return False
        can_remember = request.reason is None and request.permission is not PermissionLevel.DESTRUCTIVE
        if can_remember and request.tool_name in self._always:
            self.console.print(f"  [dim]auto-approved ({escape(request.tool_name)} allowed for this task)[/dim]")
            return True
        self.console.print(
            f"\n[bold yellow]Approval needed[/bold yellow] ({request.permission.value}): {escape(request.tool_name)}",
            soft_wrap=True,
        )
        if request.reason:
            self.console.print(f"[yellow]{escape(request.reason)}[/yellow]", soft_wrap=True)
        if request.preview:
            render_diff(self.console, request.preview)
        else:
            self.console.print(f"  {escape(_args(request.arguments, 400))}", soft_wrap=True)
        choices = ["y", "n", "a"] if can_remember else ["y", "n"]
        hint = "y = yes, n = no" + (", a = yes to all " + request.tool_name + " calls in this task" if can_remember else "")
        try:
            answer = Prompt.ask(f"Allow? ({hint})", choices=choices, default="n", console=self.console)
        except EOFError:
            # stdin looked like a terminal but is closed (IDE consoles, some CI runners): treat as
            # "nobody can approve" for the rest of the task instead of crashing.
            self._input_closed = True
            self.denial_reason = "approval was needed but no input could be read from the terminal, so it was denied"
            self.console.print("[yellow]No input available: denying (and any further approval requests).[/yellow]")
            return False
        if answer == "a":
            self._always.add(request.tool_name)
        return answer in ("y", "a")
