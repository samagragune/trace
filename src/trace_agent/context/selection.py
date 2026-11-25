"""Initial context selection: what the model sees before its first tool call.

Spec rule: "Do not load the entire repository into the model context indiscriminately."
So the opening context is small and evidence-driven, with hard size budgets:

1. a project overview (deterministic detection, a few hundred characters),
2. stack traces parsed from the user's report, mapped to project files,
3. source snippets around the innermost *project* frames (where bugs usually live),
4. project locations of error messages quoted in the report (grep for the exact text).

Everything else the agent retrieves incrementally with tools.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from trace_agent.context.project import ProjectInfo, iter_project_files, detect_project, render_project_info
from trace_agent.context.stacktrace import StackTrace, locate_frames, parse_stack_traces, render_trace
from trace_agent.safety.paths import UnsafePathError, is_sensitive_path, resolve_in_project

SNIPPET_RADIUS = 6
MAX_SNIPPETS = 3
MAX_SNIPPET_CHARS = 5_000
MAX_MESSAGE_HITS = 8
MAX_GREP_FILES = 3_000
MAX_GREP_FILE_BYTES = 1_000_000

# Quoted fragments in the user's report that are worth grepping for verbatim.
_QUOTED = re.compile(r"""["'`‘’“”]([^"'`‘’“”\n]{8,160})["'`‘’“”]""")
_EXC_PREFIX = re.compile(r"^[A-Za-z_][\w.]*(?:Error|Exception):\s*")


@dataclass
class MessageHit:
    query: str
    location: str  # "path:line"
    line_text: str


@dataclass
class TaskContext:
    project: ProjectInfo
    traces: list[StackTrace] = field(default_factory=list)
    snippets: list[str] = field(default_factory=list)
    message_hits: list[MessageHit] = field(default_factory=list)

    def render(self) -> str:
        sections = ["## Project overview (detected by Trace)", render_project_info(self.project)]
        if self.traces:
            sections.append("## Stack traces parsed from the report")
            sections.extend(render_trace(t) for t in self.traces)
        if self.snippets:
            sections.append("## Code at the innermost project frames")
            sections.extend(self.snippets)
        if self.message_hits:
            sections.append("## Where quoted error text appears in the project")
            sections.extend(f"{h.location}: {h.line_text}   (matched {h.query!r})" for h in self.message_hits)
        return "\n\n".join(sections)

    def summary(self) -> dict[str, object]:
        """Compact, loggable description of what was selected (for the CONTEXT_COLLECTED event)."""
        return {
            "languages": [s.name for s in self.project.languages[:5]],
            "frameworks": self.project.frameworks,
            "test_command": self.project.test_command,
            "git": self.project.git.model_dump() if self.project.git else None,
            "traces": [t.headline() for t in self.traces],
            "project_frames": [f.label() for t in self.traces for f in t.project_frames][-8:],
            "snippets": len(self.snippets),
            "message_hits": [h.location for h in self.message_hits],
        }


def build_task_context(description: str, project_root: Path, project: ProjectInfo | None = None) -> TaskContext:
    root = project_root.resolve()
    context = TaskContext(project=project or detect_project(root))
    context.traces = [locate_frames(t, root) for t in parse_stack_traces(description)]
    context.snippets = _snippets(root, context.traces)
    context.message_hits = _message_hits(root, description, context.traces)
    return context


def _snippets(root: Path, traces: list[StackTrace]) -> list[str]:
    seen: set[tuple[str, int]] = set()
    snippets: list[str] = []
    budget = MAX_SNIPPET_CHARS
    # Innermost project frames first: the code closest to the failure.
    candidates = [f for t in traces for f in reversed(t.project_frames)]
    for frame in candidates:
        if len(snippets) >= MAX_SNIPPETS or frame.line is None or frame.project_path is None:
            continue
        key = (frame.project_path, frame.line)
        if key in seen:
            continue
        seen.add(key)
        text = read_window(root, frame.project_path, frame.line, SNIPPET_RADIUS)
        if text is None:
            continue
        block = f"{frame.project_path} (around line {frame.line}):\n{text}"
        if len(block) > budget:
            break
        budget -= len(block)
        snippets.append(block)
    return snippets


def read_window(root: Path, rel_path: str, line: int, radius: int) -> str | None:
    """Line-numbered window around ``line``; the target line is marked with '>'."""
    try:
        path = resolve_in_project(root, rel_path)
    except UnsafePathError:
        return None
    if is_sensitive_path(path) or not path.is_file():
        return None
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    start, end = max(1, line - radius), min(len(lines), line + radius)
    width = len(str(end))
    return "\n".join(f"{'>' if n == line else ' '} {n:>{width}} | {lines[n - 1]}" for n in range(start, end + 1))


def _queries(description: str, traces: list[StackTrace]) -> list[str]:
    queries: list[str] = []
    for trace in traces:
        if trace.message and len(trace.message.strip()) >= 8:
            queries.append(trace.message.strip())
    for match in _QUOTED.finditer(description):
        text = _EXC_PREFIX.sub("", match.group(1).strip())  # "ValueError: x" -> "x"
        if len(text) >= 8:
            queries.append(text)
    unique: list[str] = []
    for q in queries:
        if q not in unique:
            unique.append(q)
    return unique[:4]


def _message_hits(root: Path, description: str, traces: list[StackTrace]) -> list[MessageHit]:
    queries = _queries(description, traces)
    if not queries:
        return []
    lowered = [(q, q.lower()) for q in queries]
    hits: list[MessageHit] = []
    for count, path in enumerate(iter_project_files(root)):
        if count >= MAX_GREP_FILES or len(hits) >= MAX_MESSAGE_HITS:
            break
        if is_sensitive_path(path):
            continue
        try:
            if path.stat().st_size > MAX_GREP_FILE_BYTES:
                continue
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw[:4096]:
            continue
        text = raw.decode("utf-8", errors="replace")
        lower_text = text.lower()
        if not any(q in lower_text for _, q in lowered):
            continue
        rel = path.relative_to(root).as_posix()
        for number, line in enumerate(text.splitlines(), start=1):
            line_lower = line.lower()
            for original, q in lowered:
                if q in line_lower:
                    hits.append(MessageHit(query=original, location=f"{rel}:{number}", line_text=line.strip()[:200]))
                    break
            if len(hits) >= MAX_MESSAGE_HITS:
                break
    return hits
