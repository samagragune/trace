"""Stack-trace parsing and frame-to-project mapping.

Supported: Python tracebacks (incl. chained exceptions and pytest's short format),
JavaScript/Node, Java/Kotlin/Scala (incl. "Caused by"), Go panics, Rust panics, Ruby.

Frames are always returned outermost-first (call order). ``locate_frames`` maps each
file reference to a file inside the project when possible: absolute paths inside the
root, relative paths, and paths from *other machines* (CI runners, containers) by
matching progressively shorter path suffixes. Library frames (site-packages,
node_modules, JDK...) are marked as such. Path lookups go through the safety layer,
so a crafted trace cannot make Trace touch files outside the project.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath, PureWindowsPath

from pydantic import BaseModel, Field

from trace_agent.safety.paths import UnsafePathError, resolve_in_project

LIBRARY_MARKERS = ("site-packages", "dist-packages", "node_modules", "lib/python", "/usr/lib/", "<frozen", "<string>")


class Frame(BaseModel):
    file: str
    line: int | None = None
    column: int | None = None
    function: str | None = None
    project_path: str | None = Field(default=None, description="Path relative to the project root, if found there.")
    is_library: bool = False

    @property
    def in_project(self) -> bool:
        return self.project_path is not None

    def label(self) -> str:
        where = self.project_path or self.file
        loc = f"{where}:{self.line}" if self.line else where
        return f"{loc} in {self.function}" if self.function else loc


class StackTrace(BaseModel):
    language: str
    exception_type: str | None = None
    message: str | None = None
    frames: list[Frame] = Field(default_factory=list)

    @property
    def project_frames(self) -> list[Frame]:
        return [f for f in self.frames if f.in_project]

    @property
    def innermost_project_frame(self) -> Frame | None:
        frames = self.project_frames
        return frames[-1] if frames else None

    def headline(self) -> str:
        if self.exception_type and self.message:
            return f"{self.exception_type}: {self.message}"
        return self.exception_type or self.message or f"{self.language} stack trace"


# --------------------------------------------------------------------------- python

_PY_FRAME = re.compile(r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+)(?:, in (?P<func>.+))?\s*$')
_PY_EXC = re.compile(r"^(?P<type>[A-Za-z_][\w.]*(?:Error|Exception|Warning|Exit|Interrupt|Iteration|Failure)|[A-Z]\w*Error)(?::\s?(?P<msg>.*))?$")
_PYTEST_FRAME = re.compile(r"^(?P<file>[^\s:][^:]*\.py):(?P<line>\d+):\s+(?:in (?P<func>\w+)|(?P<type>[\w.]+))\s*$")
_PYTEST_E = re.compile(r"^E\s+(?P<type>[A-Za-z_][\w.]*(?:Error|Exception))(?::\s?(?P<msg>.*))?$")


def _parse_python(lines: list[str]) -> list[StackTrace]:
    traces: list[StackTrace] = []
    i = 0
    while i < len(lines):
        if lines[i].strip().startswith("Traceback (most recent call last)"):
            trace = StackTrace(language="python")
            i += 1
            while i < len(lines):
                m = _PY_FRAME.match(lines[i])
                if m:
                    trace.frames.append(Frame(file=m["file"], line=int(m["line"]), function=(m["func"] or "").strip() or None))
                    i += 1
                    continue
                stripped = lines[i].strip()
                if lines[i].startswith((" ", "\t")) or not stripped or stripped.startswith("^"):
                    i += 1  # source line / caret markers under a frame
                    continue
                exc = _PY_EXC.match(stripped)
                if exc:
                    trace.exception_type, trace.message = exc["type"], (exc["msg"] or None)
                break
            if trace.frames:
                traces.append(trace)
            continue
        i += 1

    if not traces:  # pytest --tb=short/long output: "path.py:12: in func" ... "path.py:7: ValueError"
        trace = StackTrace(language="python")
        for line in lines:
            m = _PYTEST_FRAME.match(line.strip())
            if m:
                trace.frames.append(Frame(file=m["file"], line=int(m["line"]), function=m["func"]))
                if m["type"] and not trace.exception_type:
                    trace.exception_type = m["type"]
                continue
            e = _PYTEST_E.match(line.strip())
            if e and not trace.message:
                trace.exception_type, trace.message = e["type"], e["msg"]
        if trace.frames:
            traces.append(trace)
    return traces


# --------------------------------------------------------------------------- javascript

_JS_FRAME = re.compile(r"^\s*at (?:(?P<func>.+?) \()?(?P<file>(?:[A-Za-z]:)?[^():]+):(?P<line>\d+):(?P<col>\d+)\)?\s*$")
_JS_EXC = re.compile(r"^(?:Uncaught\s+)?(?P<type>[A-Z]\w*(?:Error|Exception))(?::\s?(?P<msg>.*))?$")


def _parse_js(lines: list[str]) -> list[StackTrace]:
    trace = StackTrace(language="javascript")
    for idx, line in enumerate(lines):
        m = _JS_FRAME.match(line)
        if not m or line.strip().startswith("at ") and m["file"].endswith((".java", ".kt", ".scala")):
            continue
        if not trace.frames:
            for back in range(idx - 1, max(idx - 6, -1), -1):
                exc = _JS_EXC.match(lines[back].strip())
                if exc:
                    trace.exception_type, trace.message = exc["type"], exc["msg"]
                    break
        func = m["func"]
        trace.frames.append(Frame(file=m["file"], line=int(m["line"]), column=int(m["col"]), function=func))
    trace.frames.reverse()  # V8 prints innermost first
    return [trace] if trace.frames else []


# --------------------------------------------------------------------------- java / kotlin

_JAVA_FRAME = re.compile(r"^\s*at (?P<func>[\w$.<>]+)\((?P<file>[\w$.-]+\.(?:java|kt|scala|groovy)):(?P<line>\d+)\)")
_JAVA_EXC = re.compile(
    r'^(?:Exception in thread "[^"]*"\s+|Caused by:\s+)?(?P<type>[\w$.]+(?:Exception|Error|Throwable))(?::\s?(?P<msg>.*))?$'
)


def _parse_java(lines: list[str]) -> list[StackTrace]:
    traces: list[StackTrace] = []
    current: StackTrace | None = None
    for line in lines:
        stripped = line.strip()
        exc = _JAVA_EXC.match(stripped)
        if exc and not stripped.startswith("at "):
            current = StackTrace(language="java", exception_type=exc["type"], message=exc["msg"])
            traces.append(current)
            continue
        m = _JAVA_FRAME.match(line)
        if m:
            if current is None:
                current = StackTrace(language="java")
                traces.append(current)
            # Java prints the class-qualified method; keep the package path to help locate the file.
            func = m["func"]
            package = func.rsplit(".", 2)[0].replace(".", "/") if func.count(".") >= 2 else ""
            file = f"{package}/{m['file']}" if package else m["file"]
            current.frames.append(Frame(file=file, line=int(m["line"]), function=func))
    for t in traces:
        t.frames.reverse()  # JVM prints innermost first
    return [t for t in traces if t.frames]


# --------------------------------------------------------------------------- go / rust / ruby

_GO_PANIC = re.compile(r"^panic:\s*(?P<msg>.*)$")
_GO_FUNC = re.compile(r"^(?P<func>[\w./*()-]+)\(.*\)$")
_GO_LOC = re.compile(r"^\s+(?P<file>\S+\.go):(?P<line>\d+)(?:\s+\+0x[0-9a-f]+)?$")
_RUST_PANIC = re.compile(r"panicked at (?:'(?P<msg1>.*)', )?(?P<file>[^\s:]+\.rs):(?P<line>\d+):(?P<col>\d+)")
_RUBY_FRAME = re.compile(r"^\s*(?:from\s+)?(?P<file>[^\s:]+\.rb):(?P<line>\d+):in [`'](?P<func>[^']+)'")
_RUBY_EXC = re.compile(r"\((?P<type>[A-Z]\w*(?:Error|Exception))\)\s*$")


def _parse_go(lines: list[str]) -> list[StackTrace]:
    trace: StackTrace | None = None
    pending_func: str | None = None
    for line in lines:
        panic = _GO_PANIC.match(line.strip())
        if panic and trace is None:
            trace = StackTrace(language="go", exception_type="panic", message=panic["msg"])
            continue
        if trace is None:
            continue
        func = _GO_FUNC.match(line.strip())
        if func and not line.startswith((" ", "\t")):
            pending_func = func["func"]
            continue
        loc = _GO_LOC.match(line)
        if loc:
            trace.frames.append(Frame(file=loc["file"], line=int(loc["line"]), function=pending_func))
            pending_func = None
    if trace and trace.frames:
        trace.frames.reverse()
        return [trace]
    return []


def _parse_rust(lines: list[str]) -> list[StackTrace]:
    for idx, line in enumerate(lines):
        m = _RUST_PANIC.search(line)
        if m:
            message = m["msg1"]
            if message is None and idx + 1 < len(lines):
                message = lines[idx + 1].strip() or None
            frame = Frame(file=m["file"], line=int(m["line"]), column=int(m["col"]))
            return [StackTrace(language="rust", exception_type="panic", message=message, frames=[frame])]
    return []


def _parse_ruby(lines: list[str]) -> list[StackTrace]:
    trace = StackTrace(language="ruby")
    for line in lines:
        m = _RUBY_FRAME.match(line)
        if m:
            if not trace.frames:
                exc = _RUBY_EXC.search(line)
                if exc:
                    trace.exception_type = exc["type"]
                    msg = line[m.end():].strip(": ")
                    trace.message = _RUBY_EXC.sub("", msg).strip() or None
            trace.frames.append(Frame(file=m["file"], line=int(m["line"]), function=m["func"]))
    trace.frames.reverse()
    return [trace] if trace.frames else []


_PARSERS = (_parse_python, _parse_java, _parse_js, _parse_go, _parse_rust, _parse_ruby)


def parse_stack_traces(text: str) -> list[StackTrace]:
    """Extract every stack trace found in ``text`` (any supported language)."""
    lines = text.replace("\r\n", "\n").splitlines()
    traces: list[StackTrace] = []
    for parser in _PARSERS:
        traces.extend(parser(lines))
    return traces


# --------------------------------------------------------------------------- mapping to project


def _parts(file: str) -> tuple[str, ...]:
    pure = PureWindowsPath(file) if re.match(r"^[A-Za-z]:[\\/]", file) or "\\" in file else PurePosixPath(file)
    return tuple(p for p in pure.parts if p not in ("/", "\\") and not re.match(r"^[A-Za-z]:\\?$", p))


def locate_file(project_root: Path, file: str) -> str | None:
    """Return the project-relative path that ``file`` refers to, or None."""
    lowered = file.replace("\\", "/").lower()
    if any(marker in lowered for marker in LIBRARY_MARKERS):
        return None
    root = project_root.resolve()
    parts = _parts(file)
    for start in range(len(parts)):
        candidate = "/".join(parts[start:])
        if not candidate:
            continue
        try:
            resolved = resolve_in_project(root, candidate)
        except UnsafePathError:
            continue
        if resolved.is_file():
            return resolved.relative_to(root).as_posix()
    return None


def locate_frames(trace: StackTrace, project_root: Path) -> StackTrace:
    frames = []
    for frame in trace.frames:
        lowered = frame.file.replace("\\", "/").lower()
        is_library = any(marker in lowered for marker in LIBRARY_MARKERS)
        project_path = None if is_library else locate_file(project_root, frame.file)
        frames.append(frame.model_copy(update={"project_path": project_path, "is_library": is_library}))
    return trace.model_copy(update={"frames": frames})


def render_trace(trace: StackTrace, max_frames: int = 12) -> str:
    lines = [f"{trace.language} | {trace.headline()}"]
    frames = trace.frames[-max_frames:]
    if len(trace.frames) > max_frames:
        lines.append(f"  ... {len(trace.frames) - max_frames} outer frames omitted")
    innermost = trace.innermost_project_frame
    for frame in frames:
        tag = "PROJECT" if frame.in_project else ("library" if frame.is_library else "not found")
        marker = "  <-- innermost project frame" if frame is innermost else ""
        lines.append(f"  [{tag}] {frame.label()}{marker}")
    return "\n".join(lines)
