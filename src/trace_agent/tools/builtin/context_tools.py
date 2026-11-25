"""Read-only investigation tools built on the context engine:
get_project_info, parse_stack_trace, inspect_logs.
"""

from __future__ import annotations

import re
from collections import deque
from datetime import datetime
from pathlib import Path

from pydantic import Field

from trace_agent.context.project import detect_project, iter_project_files, render_project_info
from trace_agent.context.selection import read_window
from trace_agent.context.stacktrace import locate_frames, parse_stack_traces, render_trace
from trace_agent.safety.paths import UnsafePathError, is_sensitive_path, resolve_in_project
from trace_agent.safety.permissions import PermissionLevel
from trace_agent.tools.base import Tool, ToolContext, ToolInput, ToolResult, ToolStatus

LOG_SUFFIXES = (".log", ".out", ".err")
LOG_DIR_NAMES = {"log", "logs"}
MAX_LOG_FILES_LISTED = 30
MAX_LOG_BYTES_SCANNED = 5_000_000
DEFAULT_ERROR_PATTERN = r"error|exception|traceback|fatal|panic|critical|failed|warn"


# --------------------------------------------------------------------------- get_project_info


class ProjectInfoInput(ToolInput):
    directory: str = Field(default=".", description="Project directory relative to the root (for sub-projects).")


class GetProjectInfoTool(Tool):
    name = "get_project_info"
    description = (
        "Detect the project's languages, package managers, frameworks, test setup (including the likely "
        "test command) and git status. Use it for a sub-directory in a monorepo."
    )
    permission = PermissionLevel.READ
    input_model = ProjectInfoInput

    def run(self, args: ProjectInfoInput, context: ToolContext) -> ToolResult:
        try:
            target = resolve_in_project(context.project_root, args.directory)
        except UnsafePathError as exc:
            return ToolResult.failure(ToolStatus.DENIED, str(exc))
        if not target.is_dir():
            return ToolResult.failure(ToolStatus.ERROR, f"'{args.directory}' is not a directory")
        info = detect_project(target)
        return ToolResult.success(
            render_project_info(info),
            primary_language=info.primary_language,
            test_command=info.test_command,
        )


# --------------------------------------------------------------------------- parse_stack_trace


class ParseStackTraceInput(ToolInput):
    text: str = Field(min_length=1, max_length=200_000, description="Text containing one or more stack traces.")
    include_code: bool = Field(default=True, description="Include source around the innermost project frames.")


class ParseStackTraceTool(Tool):
    name = "parse_stack_trace"
    description = (
        "Parse stack traces (Python, JavaScript/Node, Java/Kotlin, Go, Rust, Ruby) from text such as test "
        "or log output. Maps every frame to project files, marks library frames, and shows code at the "
        "innermost project frames."
    )
    permission = PermissionLevel.READ
    input_model = ParseStackTraceInput

    def run(self, args: ParseStackTraceInput, context: ToolContext) -> ToolResult:
        root = context.project_root.resolve()
        traces = [locate_frames(t, root) for t in parse_stack_traces(args.text)]
        if not traces:
            return ToolResult.success("no stack traces recognized in the text", traces=0)
        blocks = [render_trace(t) for t in traces]
        if args.include_code:
            for trace in traces:
                frame = trace.innermost_project_frame
                if frame and frame.project_path and frame.line:
                    window = read_window(root, frame.project_path, frame.line, 5)
                    if window:
                        blocks.append(f"{frame.project_path} (around line {frame.line}):\n{window}")
        project_frames = sum(len(t.project_frames) for t in traces)
        return ToolResult.success("\n\n".join(blocks), traces=len(traces), project_frames=project_frames)


# --------------------------------------------------------------------------- inspect_logs


class InspectLogsInput(ToolInput):
    path: str | None = Field(default=None, description="Log file to inspect. Omit to discover log files.")
    pattern: str = Field(default=DEFAULT_ERROR_PATTERN, description="Case-insensitive regex for interesting lines.")
    context_lines: int = Field(default=2, ge=0, le=10, description="Lines of context around each match.")
    tail_lines: int = Field(default=40, ge=0, le=500, description="Also show the last N lines of the file.")
    max_matches: int = Field(default=40, ge=1, le=200)


class InspectLogsTool(Tool):
    name = "inspect_logs"
    description = (
        "Investigate log files. Without a path: list log files in the project (newest first). With a path: "
        "show lines matching an error pattern (with context) plus the tail of the file."
    )
    permission = PermissionLevel.READ
    input_model = InspectLogsInput

    def run(self, args: InspectLogsInput, context: ToolContext) -> ToolResult:
        root = context.project_root.resolve()
        if args.path is None:
            return self._discover(root)
        try:
            target = resolve_in_project(root, args.path)
        except UnsafePathError as exc:
            return ToolResult.failure(ToolStatus.DENIED, str(exc))
        if is_sensitive_path(target):
            return ToolResult.failure(ToolStatus.DENIED, f"'{args.path}' may contain credentials")
        if not target.is_file():
            return ToolResult.failure(ToolStatus.ERROR, f"file not found: {args.path}")
        try:
            pattern = re.compile(args.pattern, re.IGNORECASE)
        except re.error as exc:
            return ToolResult.failure(ToolStatus.INVALID_INPUT, f"invalid pattern: {exc}")
        return self._inspect(root, target, pattern, args)

    def _discover(self, root: Path) -> ToolResult:
        logs: list[tuple[float, int, str]] = []
        for path in iter_project_files(root):
            in_log_dir = any(p.name.lower() in LOG_DIR_NAMES for p in path.relative_to(root).parents)
            if path.suffix.lower() in LOG_SUFFIXES or (in_log_dir and path.suffix.lower() in (".txt", ".json", "")):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                logs.append((stat.st_mtime, stat.st_size, path.relative_to(root).as_posix()))
        if not logs:
            return ToolResult.success("no log files found (looked for *.log, *.out, *.err and logs/ directories)", count=0)
        logs.sort(reverse=True)
        lines = [
            f"{rel}  ({size:,} bytes, modified {datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M')})"
            for mtime, size, rel in logs[:MAX_LOG_FILES_LISTED]
        ]
        if len(logs) > MAX_LOG_FILES_LISTED:
            lines.append(f"... {len(logs) - MAX_LOG_FILES_LISTED} more")
        return ToolResult.success("\n".join(lines), count=len(logs))

    def _inspect(self, root: Path, target: Path, pattern: re.Pattern[str], args: InspectLogsInput) -> ToolResult:
        size = target.stat().st_size
        skipped = max(0, size - MAX_LOG_BYTES_SCANNED)
        with target.open("rb") as fh:
            if skipped:
                fh.seek(skipped)  # logs grow at the end: scan the most recent part
            raw = fh.read()
        if b"\x00" in raw[:4096]:
            return ToolResult.failure(ToolStatus.ERROR, "file looks binary")
        lines = raw.decode("utf-8", errors="replace").splitlines()
        first_line_no = 1 if not skipped else None

        def label(i: int) -> str:
            return str(i + 1) if first_line_no else f"~{i + 1}"

        out: list[str] = []
        matches = 0
        last_printed = -1
        for i, line in enumerate(lines):
            if not pattern.search(line):
                continue
            matches += 1
            if matches > args.max_matches:
                continue
            start, end = max(0, i - args.context_lines), min(len(lines) - 1, i + args.context_lines)
            if start > last_printed + 1 and out:
                out.append("  ...")
            for j in range(max(start, last_printed + 1), end + 1):
                mark = ">" if j == i or pattern.search(lines[j]) else " "
                out.append(f"{mark} {label(j)}: {lines[j][:400]}")
            last_printed = end
        rel = target.relative_to(root).as_posix()
        header = f"{rel}: {len(lines):,} lines scanned, {matches} matching /{pattern.pattern}/"
        if skipped:
            header += f" (first {skipped:,} bytes skipped; only the most recent {MAX_LOG_BYTES_SCANNED:,} scanned)"
        if matches > args.max_matches:
            header += f"; showing first {args.max_matches}"
        sections = [header, *(out or ["(no matching lines)"])]
        if args.tail_lines:
            tail = deque(enumerate(lines), maxlen=args.tail_lines)
            sections.append(f"--- last {len(tail)} lines ---")
            sections.extend(f"  {label(i)}: {text[:400]}" for i, text in tail)
        return ToolResult.success("\n".join(sections), matches=matches, lines=len(lines))
