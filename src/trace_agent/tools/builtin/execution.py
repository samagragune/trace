"""Execution tools: run_command, run_tests, inspect_git.

Commands never go through a shell (``safety.commands``). Each call is classified *before*
authorization, so the executor sees the effective risk of these exact arguments: ``pytest`` is
READ, ``python script.py`` is EXECUTE, ``rm`` is DESTRUCTIVE, ``git push`` is blocked.

``run_tests`` reports a structured verification (``data["verification"]``) that the executor
records on the task. That is how "the tests passed after the edit" becomes evidence the
verified-fix invariant can check (ADR-006).
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import Field

from trace_agent.safety.commands import Classification, CommandParseError, CommandRisk, classify, split_command
from trace_agent.safety.git_guard import run_git
from trace_agent.safety.paths import UnsafePathError, resolve_in_project
from trace_agent.safety.permissions import PermissionLevel
from trace_agent.tools.base import Assessment, Tool, ToolContext, ToolInput, ToolResult, ToolStatus
from trace_agent.tools.process import ProcessResult, run_process
from trace_agent.tools.write_guard import GuardReport, run_guarded

MAX_TIMEOUT_SECONDS = 600.0


def _workdir(context: ToolContext, working_directory: str) -> Path:
    target = resolve_in_project(context.project_root, working_directory)
    if not target.is_dir():
        raise UnsafePathError(f"working directory '{working_directory}' does not exist")
    return target


def _assess_command(command: str, context: ToolContext, working_directory: str) -> tuple[Assessment, list[str] | None]:
    try:
        _workdir(context, working_directory)
    except UnsafePathError as exc:
        return Assessment(PermissionLevel.EXECUTE, error=str(exc), error_status=ToolStatus.DENIED), None
    try:
        argv = split_command(command)
    except CommandParseError as exc:
        return Assessment(PermissionLevel.EXECUTE, error=str(exc), error_status=ToolStatus.INVALID_INPUT), None
    verdict: Classification = classify(argv)
    if verdict.risk is CommandRisk.BLOCKED:
        return Assessment(PermissionLevel.DESTRUCTIVE, blocked=f"'{argv[0]}' is blocked: {verdict.reason}"), argv
    preview = f"$ {command}   (in {working_directory}; risk: {verdict.risk.value}, {verdict.reason})"
    return Assessment(verdict.permission or PermissionLevel.EXECUTE, preview=preview), argv


def _timeout(requested: float | None, context: ToolContext) -> float:
    limit = min(context.timeout_seconds, MAX_TIMEOUT_SECONDS)
    return limit if requested is None else max(1.0, min(requested, limit))


def _process_failure(proc: ProcessResult, timeout: float) -> str | None:
    if proc.not_found:
        return proc.stderr
    if proc.timed_out:
        return f"timed out after {timeout:.0f}s and was killed (with its child processes)"
    if proc.cancelled:
        return "cancelled by the user"
    return None


def _run(argv: list[str], working_directory: str, context: ToolContext, timeout: float) -> tuple[ProcessResult, GuardReport]:
    """Run a command inside the write guard (protected/denied files can't be changed by it)."""
    return run_guarded(
        context.project_root,
        context.guarded_paths,
        lambda: run_process(argv, _workdir(context, working_directory), timeout=timeout,
                            cancel_event=context.cancel_event, scrub_env=context.scrub_env),
    )


def _guard_data(report: GuardReport) -> dict[str, object]:
    changed = [p for p in report.changed if p not in report.restored]
    return {"changed_by_command": changed, "guard_violation": report.violation}


# --------------------------------------------------------------------------- run_command


class RunCommandInput(ToolInput):
    command: str = Field(min_length=1, description="One command, no shell operators (no |, &&, >, ;).")
    working_directory: str = Field(default=".", description="Directory to run in, relative to the project root.")
    timeout_seconds: float | None = Field(default=None, gt=0, description="Optional lower timeout.")


class RunCommandTool(Tool):
    name = "run_command"
    description = (
        "Run one command in the project (no shell: pipes, redirects and && are not supported). Use it to "
        "reproduce bugs, run scripts or build. Returns exit code and output. Risky commands need user "
        "approval; dangerous ones are blocked."
    )
    permission = PermissionLevel.EXECUTE
    input_model = RunCommandInput

    def assess(self, args: RunCommandInput, context: ToolContext) -> Assessment:
        return _assess_command(args.command, context, args.working_directory)[0]

    def run(self, args: RunCommandInput, context: ToolContext) -> ToolResult:
        assessment, argv = _assess_command(args.command, context, args.working_directory)
        if assessment.error or argv is None:
            return ToolResult.failure(assessment.error_status, assessment.error or "invalid command")
        if assessment.blocked:
            return ToolResult.failure(ToolStatus.DENIED, assessment.blocked)
        timeout = _timeout(args.timeout_seconds, context)
        proc, guard = _run(argv, args.working_directory, context, timeout)
        failure = _process_failure(proc, timeout)
        output = proc.combined_output() or "(no output)"
        if guard.describe():
            output += "\n\n" + guard.describe()
        data = {"exit_code": proc.exit_code, "duration_s": round(proc.duration_s, 2), **_guard_data(guard)}
        if guard.violation:
            return ToolResult.failure(ToolStatus.ERROR, "the command modified protected files; Trace restored them", output=output, **data)
        if failure:
            status = ToolStatus.CANCELLED if proc.cancelled else (ToolStatus.TIMEOUT if proc.timed_out else ToolStatus.ERROR)
            return ToolResult.failure(status, failure, output=output, **data)
        if proc.exit_code != 0:
            return ToolResult.failure(ToolStatus.ERROR, f"command exited with code {proc.exit_code}", output=output, **data)
        return ToolResult.success(f"exit code 0\n{output}", **data)


# --------------------------------------------------------------------------- run_tests

_SUMMARY_PATTERNS = (
    re.compile(r"^=+ (.*(?:passed|failed|error|no tests ran).*?) =+$", re.M),  # pytest
    re.compile(r"^(Tests?:\s+.*total.*)$", re.M),  # jest
    re.compile(r"^\s*(Tests\s+\d+.*\(\d+\))\s*$", re.M),  # vitest
    re.compile(r"^\s*(\d+ passing.*)$", re.M),  # mocha
    re.compile(r"^(Ran \d+ tests? in [\d.]+s)\s*\n+\s*(OK.*|FAILED.*)$", re.M),  # unittest
    re.compile(r"^(test result: .*)$", re.M),  # cargo
    re.compile(r"^((?:ok|FAIL|---\s*FAIL)\s+\S+.*)$", re.M),  # go test
    re.compile(r"^(Tests run: \d+, Failures: \d+.*)$", re.M),  # maven/surefire
)


def summarize_test_output(output: str) -> str:
    for pattern in _SUMMARY_PATTERNS:
        matches = pattern.findall(output)
        if matches:
            last = matches[-1]
            return " ".join(last) if isinstance(last, tuple) else last.strip()
    lines = [l.strip() for l in output.strip().splitlines() if l.strip()]
    return lines[-1][:200] if lines else "(no output)"


class RunTestsInput(ToolInput):
    test_command: str | None = Field(
        default=None,
        description="Test command to run. Omit to use the project's detected test command. You may "
        "narrow it (e.g. 'python -m pytest tests/test_x.py -q') while iterating.",
    )
    working_directory: str = Field(default=".", description="Directory to run in, relative to the project root.")
    timeout_seconds: float | None = Field(default=None, gt=0)


class RunTestsTool(Tool):
    name = "run_tests"
    description = (
        "Run the project's tests (or a narrower test command) and report pass/fail with a summary. "
        "Use it to reproduce a failing test and to verify a fix after editing. The result is recorded "
        "as verification evidence."
    )
    permission = PermissionLevel.READ
    input_model = RunTestsInput

    def _command(self, args: RunTestsInput, context: ToolContext) -> str | None:
        return args.test_command or context.test_command

    def assess(self, args: RunTestsInput, context: ToolContext) -> Assessment:
        command = self._command(args, context)
        if not command:
            return Assessment(self.permission, error="no test command was given and none was detected for this project; pass test_command")
        return _assess_command(command, context, args.working_directory)[0]

    def run(self, args: RunTestsInput, context: ToolContext) -> ToolResult:
        command = self._command(args, context)
        if not command:
            return ToolResult.failure(ToolStatus.INVALID_INPUT, "no test command available; pass test_command")
        assessment, argv = _assess_command(command, context, args.working_directory)
        if assessment.error or argv is None:
            return ToolResult.failure(assessment.error_status, assessment.error or "invalid command")
        if assessment.blocked:
            return ToolResult.failure(ToolStatus.DENIED, assessment.blocked)
        timeout = _timeout(args.timeout_seconds, context)
        proc, guard = _run(argv, args.working_directory, context, timeout)
        output = proc.combined_output() or "(no output)"
        failure = _process_failure(proc, timeout)
        summary = failure or summarize_test_output(output)
        if guard.violation:
            summary = "invalid: the test run modified protected files (Trace restored them)"
        if guard.describe():
            output += "\n\n" + guard.describe()
        passed = proc.ok and not guard.violation
        verification = {"command": command, "passed": passed, "exit_code": proc.exit_code, "summary": summary}
        data = {"verification": verification, "exit_code": proc.exit_code, "duration_s": round(proc.duration_s, 2), **_guard_data(guard)}
        body = f"{'PASSED' if passed else 'FAILED'}: {summary}\ncommand: {command} (exit code {proc.exit_code})\n\n{output}"
        if passed:
            return ToolResult.success(body, **data)
        status = ToolStatus.TIMEOUT if proc.timed_out else (ToolStatus.CANCELLED if proc.cancelled else ToolStatus.ERROR)
        return ToolResult.failure(status, f"tests did not pass: {summary}", output=body, **data)


# --------------------------------------------------------------------------- inspect_git


class InspectGitInput(ToolInput):
    operation: str = Field(
        description="One of: status, diff, diff_staged, log, show, blame.",
        pattern="^(status|diff|diff_staged|log|show|blame)$",
    )
    path: str | None = Field(default=None, description="Limit to this file (required for blame).")
    revision: str | None = Field(default=None, description="Commit for 'show' (default HEAD).", pattern=r"^[\w./~^-]{1,80}$")
    max_count: int = Field(default=10, ge=1, le=50, description="Number of commits for 'log'.")
    start_line: int | None = Field(default=None, ge=1, description="blame: first line.")
    end_line: int | None = Field(default=None, ge=1, description="blame: last line.")


class InspectGitTool(Tool):
    name = "inspect_git"
    description = (
        "Read-only git inspection: status, diff (unstaged), diff_staged, log, show <revision>, and blame "
        "<path> (optionally a line range). Useful to see recent changes that may have introduced a bug."
    )
    permission = PermissionLevel.READ
    input_model = InspectGitInput

    def is_available(self, context: ToolContext) -> bool:
        proc = run_git(context.project_root, ["rev-parse", "--is-inside-work-tree"])
        return proc is not None and proc.returncode == 0

    def run(self, args: InspectGitInput, context: ToolContext) -> ToolResult:
        root = context.project_root.resolve()
        rel: str | None = None
        if args.path:
            try:
                rel = resolve_in_project(root, args.path).relative_to(root).as_posix()
            except UnsafePathError as exc:
                return ToolResult.failure(ToolStatus.DENIED, str(exc))
        pathspec = ["--", rel] if rel else []
        op = args.operation
        if op == "status":
            cmd = ["status", "--short", "--branch", *pathspec]
        elif op == "diff":
            cmd = ["diff", "--no-color", *pathspec]
        elif op == "diff_staged":
            cmd = ["diff", "--cached", "--no-color", *pathspec]
        elif op == "log":
            cmd = ["log", f"-n{args.max_count}", "--date=short", "--format=%h %ad %an: %s", *pathspec]
        elif op == "show":
            cmd = ["show", "--no-color", "--stat", "--patch", args.revision or "HEAD", *pathspec]
        else:  # blame
            if not rel:
                return ToolResult.failure(ToolStatus.INVALID_INPUT, "blame requires path")
            line_range = []
            if args.start_line:
                line_range = ["-L", f"{args.start_line},{args.end_line or args.start_line + 30}"]
            cmd = ["blame", "--date=short", *line_range, "--", rel]
        proc = run_git(root, cmd, timeout=context.timeout_seconds)
        if proc is None:
            return ToolResult.failure(ToolStatus.ERROR, "git is not available or timed out")
        if proc.returncode != 0:
            return ToolResult.failure(ToolStatus.ERROR, proc.stderr.strip() or f"git {op} failed")
        return ToolResult.success(proc.stdout.rstrip() or "(no output)", operation=op)
