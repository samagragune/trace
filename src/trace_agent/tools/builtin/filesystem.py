"""Read-only filesystem tools: list_files, read_file, search_code.

All three:
* route every model-supplied path through ``safety.resolve_in_project``,
* skip noise directories (VCS, virtualenvs, dependency and build caches),
* refuse to read sensitive files (``.env``, private keys...) so they are never sent to a model,
* bound their own output and report truncation honestly.
"""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Iterator
from pathlib import Path

from pydantic import Field

from trace_agent.safety.paths import UnsafePathError, is_sensitive_path, resolve_in_project
from trace_agent.safety.permissions import PermissionLevel
from trace_agent.tools.base import Tool, ToolContext, ToolInput, ToolResult, ToolStatus

#: Directories never worth showing a model. Users can't opt back in yet (Phase 3: .traceignore).
DEFAULT_EXCLUDED_DIRS = frozenset(
    {
        ".git", ".hg", ".svn", ".venv", "venv", "env", "__pycache__", "node_modules", ".mypy_cache",
        ".pytest_cache", ".ruff_cache", ".tox", ".nox", "dist", "build", ".next", ".idea", ".vscode",
        "target", ".gradle", ".eggs", "site-packages", ".cache", "coverage", "htmlcov",
    }
)

MAX_LIST_ENTRIES = 500
MAX_READ_BYTES = 1_000_000
MAX_SEARCH_MATCHES = 200
MAX_SEARCH_FILE_BYTES = 2_000_000


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix() or "."


def _is_excluded_dir(name: str, extra: list[str]) -> bool:
    return name in DEFAULT_EXCLUDED_DIRS or name.endswith(".egg-info") or any(fnmatch.fnmatch(name, p) for p in extra)


def _walk_files(root: Path, start: Path, exclude: list[str]) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(start):
        dirnames[:] = sorted(d for d in dirnames if not _is_excluded_dir(d, exclude))
        for name in sorted(filenames):
            if any(fnmatch.fnmatch(name, p) for p in exclude):
                continue
            yield Path(dirpath) / name


def _resolve(context: ToolContext, path: str) -> Path | ToolResult:
    try:
        return resolve_in_project(context.project_root, path)
    except UnsafePathError as exc:
        return ToolResult.failure(ToolStatus.DENIED, str(exc))


def _looks_binary(sample: bytes) -> bool:
    return b"\x00" in sample


# --------------------------------------------------------------------------- list_files


class ListFilesInput(ToolInput):
    directory: str = Field(default=".", description="Directory relative to the project root.")
    recursive: bool = Field(default=False, description="List the whole subtree instead of one level.")
    exclude_patterns: list[str] = Field(default_factory=list, description="Extra glob patterns to skip.")


class ListFilesTool(Tool):
    name = "list_files"
    description = (
        "List files and directories inside the project. Use recursive=true to see a subtree. "
        "Dependency, VCS and build directories are skipped automatically."
    )
    permission = PermissionLevel.READ
    input_model = ListFilesInput

    def run(self, args: ListFilesInput, context: ToolContext) -> ToolResult:
        target = _resolve(context, args.directory)
        if isinstance(target, ToolResult):
            return target
        if not target.is_dir():
            return ToolResult.failure(ToolStatus.ERROR, f"'{args.directory}' is not a directory")
        root = context.project_root.resolve()
        entries: list[str] = []
        truncated = False
        if args.recursive:
            for path in _walk_files(root, target, args.exclude_patterns):
                if len(entries) >= MAX_LIST_ENTRIES:
                    truncated = True
                    break
                entries.append(_relative(root, path))
        else:
            for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                if child.is_dir() and _is_excluded_dir(child.name, args.exclude_patterns):
                    continue
                if any(fnmatch.fnmatch(child.name, p) for p in args.exclude_patterns):
                    continue
                if len(entries) >= MAX_LIST_ENTRIES:
                    truncated = True
                    break
                entries.append(_relative(root, child) + ("/" if child.is_dir() else ""))
        output = "\n".join(entries) if entries else "(empty)"
        if truncated:
            output += f"\n... stopped after {MAX_LIST_ENTRIES} entries; narrow the directory."
        return ToolResult.success(output, count=len(entries), truncated_listing=truncated)


# --------------------------------------------------------------------------- read_file


class ReadFileInput(ToolInput):
    path: str = Field(description="File path relative to the project root.")
    start_line: int | None = Field(default=None, ge=1, description="First line to return (1-based).")
    end_line: int | None = Field(default=None, ge=1, description="Last line to return (inclusive).")


class ReadFileTool(Tool):
    name = "read_file"
    description = (
        "Read a text file from the project. Output lines are prefixed with their line numbers. "
        "Use start_line/end_line to read part of a large file."
    )
    permission = PermissionLevel.READ
    input_model = ReadFileInput

    def run(self, args: ReadFileInput, context: ToolContext) -> ToolResult:
        target = _resolve(context, args.path)
        if isinstance(target, ToolResult):
            return target
        if is_sensitive_path(target):
            return ToolResult.failure(
                ToolStatus.DENIED, f"'{args.path}' may contain credentials; Trace does not send it to the model"
            )
        if not target.is_file():
            return ToolResult.failure(ToolStatus.ERROR, f"file not found: {args.path}")
        size = target.stat().st_size
        if size > MAX_READ_BYTES:
            return ToolResult.failure(
                ToolStatus.ERROR, f"file is {size} bytes; larger than the {MAX_READ_BYTES}-byte read limit"
            )
        raw = target.read_bytes()
        if _looks_binary(raw[:8192]):
            return ToolResult.failure(ToolStatus.ERROR, f"'{args.path}' looks like a binary file")
        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines()
        total = len(lines)
        start = args.start_line or 1
        end = min(args.end_line or total, total)
        if total and start > total:
            return ToolResult.failure(ToolStatus.ERROR, f"start_line {start} is past the end of the file ({total} lines)")
        if end < start and total:
            return ToolResult.failure(ToolStatus.ERROR, "end_line must be >= start_line")
        width = len(str(end)) if total else 1
        body = "\n".join(f"{n:>{width}} | {lines[n - 1]}" for n in range(start, end + 1)) if total else "(empty file)"
        rel = _relative(context.project_root.resolve(), target)
        return ToolResult.success(body, path=rel, total_lines=total, start_line=start, end_line=end)


# --------------------------------------------------------------------------- search_code


class SearchCodeInput(ToolInput):
    query: str = Field(min_length=1, description="Text or regular expression to search for.")
    directory: str = Field(default=".", description="Directory to search, relative to the project root.")
    file_pattern: str | None = Field(default=None, description="Glob for file names, e.g. '*.py'.")
    regex: bool = Field(default=False, description="Treat query as a regular expression.")
    case_sensitive: bool = Field(default=False)


class SearchCodeTool(Tool):
    name = "search_code"
    description = (
        "Search project files for text (or a regex when regex=true). Returns path:line: content for "
        "each match. Use file_pattern like '*.py' to narrow the search."
    )
    permission = PermissionLevel.READ
    input_model = SearchCodeInput

    def run(self, args: SearchCodeInput, context: ToolContext) -> ToolResult:
        target = _resolve(context, args.directory)
        if isinstance(target, ToolResult):
            return target
        if not target.is_dir():
            return ToolResult.failure(ToolStatus.ERROR, f"'{args.directory}' is not a directory")
        flags = 0 if args.case_sensitive else re.IGNORECASE
        try:
            pattern = re.compile(args.query if args.regex else re.escape(args.query), flags)
        except re.error as exc:
            return ToolResult.failure(ToolStatus.INVALID_INPUT, f"invalid regular expression: {exc}")
        root = context.project_root.resolve()
        matches: list[str] = []
        files_with_matches = 0
        truncated = False
        for path in _walk_files(root, target, []):
            if context.cancelled:
                return ToolResult.failure(ToolStatus.CANCELLED, "search cancelled")
            if args.file_pattern and not fnmatch.fnmatch(path.name, args.file_pattern):
                continue
            if is_sensitive_path(path):
                continue
            try:
                if path.stat().st_size > MAX_SEARCH_FILE_BYTES:
                    continue
                raw = path.read_bytes()
            except OSError:
                continue
            if _looks_binary(raw[:8192]):
                continue
            found = False
            for number, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), start=1):
                if pattern.search(line):
                    found = True
                    matches.append(f"{_relative(root, path)}:{number}: {line.strip()[:300]}")
                    if len(matches) >= MAX_SEARCH_MATCHES:
                        truncated = True
                        break
            files_with_matches += found
            if truncated:
                break
        if not matches:
            return ToolResult.success("no matches", match_count=0, files=0)
        output = "\n".join(matches)
        if truncated:
            output += f"\n... stopped after {MAX_SEARCH_MATCHES} matches; refine the query."
        return ToolResult.success(output, match_count=len(matches), files=files_with_matches)
