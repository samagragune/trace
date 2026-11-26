"""Code modification tools: edit_file, apply_patch, create_file, delete_file.

Shared guarantees (spec: code_modification_system):
* **Plan, then write.** The new content is fully computed and validated before any write. A
  failed match changes nothing, and the error says why (with the closest match when the text
  wasn't found) so the model can revise.
* **Backup first.** Every write goes through ``context.checkpoints`` (before/after hooks).
  With no checkpoint store, editing is blocked: Trace never writes what it can't undo.
* **Encoding and line endings preserved.** UTF-8 (with or without BOM) only; CRLF/LF style and
  the trailing-newline state are kept. The model's text is normalized to the file's style.
* **Verified.** After writing, the file is read back and compared with the intended bytes.
* **Boundaries.** Project-only paths; never ``.git/`` internals or sensitive files.
* **Uncommitted user work.** Files dirty in git at task start require explicit approval.
* **Readable result.** Every change returns a unified diff (shown to the user and the model).
"""

from __future__ import annotations

import codecs
import difflib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pydantic import Field

from trace_agent.safety.paths import UnsafePathError, is_sensitive_path, resolve_in_project
from trace_agent.safety.permissions import PermissionLevel
from trace_agent.tools.base import Assessment, Tool, ToolContext, ToolInput, ToolResult, ToolStatus

MAX_EDIT_FILE_BYTES = 2_000_000
PROTECTED_DIR_NAMES = {".git", ".hg", ".svn"}


class EditError(Exception):
    """A planned edit can't be applied. The message is written for the model to act on."""

    def __init__(self, message: str, status: ToolStatus = ToolStatus.ERROR) -> None:
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- text file I/O


@dataclass(frozen=True)
class TextFile:
    text: str  # normalized to "\n"
    newline: str  # "\r\n" or "\n"
    bom: bool

    @classmethod
    def read(cls, path: Path) -> TextFile:
        if path.stat().st_size > MAX_EDIT_FILE_BYTES:
            raise EditError(f"file is larger than {MAX_EDIT_FILE_BYTES:,} bytes; refusing to edit")
        raw = path.read_bytes()
        if b"\x00" in raw[:8192]:
            raise EditError("file looks binary; refusing to edit")
        bom = raw.startswith(codecs.BOM_UTF8)
        try:
            text = raw.decode("utf-8-sig" if bom else "utf-8")
        except UnicodeDecodeError:
            raise EditError("file is not valid UTF-8; refusing to edit to avoid corrupting its encoding") from None
        newline = "\r\n" if "\r\n" in text else "\n"
        return cls(text=text.replace("\r\n", "\n"), newline=newline, bom=bom)

    def encode(self, text: str) -> bytes:
        data = text.replace("\n", self.newline).encode("utf-8")
        return codecs.BOM_UTF8 + data if self.bom else data


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.trace-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        if path.exists():
            try:
                os.chmod(tmp, path.stat().st_mode)  # keep permissions (e.g. executable bit)
            except OSError:
                pass
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def unified_diff(rel: str, old: str, new: str, *, old_label: str | None = None, new_label: str | None = None) -> str:
    lines = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=old_label or f"a/{rel}",
        tofile=new_label or f"b/{rel}",
        n=3,
    )
    out = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines)
    return out.rstrip("\n")


def _diff_stats(diff: str) -> tuple[int, int]:
    added = sum(1 for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff.splitlines() if l.startswith("-") and not l.startswith("---"))
    return added, removed


# --------------------------------------------------------------------------- shared checks


def resolve_target(context: ToolContext, path: str) -> tuple[Path, str]:
    try:
        target = resolve_in_project(context.project_root, path)
    except UnsafePathError as exc:
        raise EditError(str(exc), ToolStatus.DENIED) from None
    root = context.project_root.resolve()
    rel = target.relative_to(root).as_posix()
    if any(part in PROTECTED_DIR_NAMES for part in Path(rel).parts):
        raise EditError(f"'{rel}' is inside version-control internals; Trace never edits them", ToolStatus.DENIED)
    if is_sensitive_path(target):
        raise EditError(f"'{rel}' may contain credentials; Trace does not edit it", ToolStatus.DENIED)
    if target.is_dir():
        raise EditError(f"'{rel}' is a directory")
    return target, rel


def _protection(context: ToolContext, rels: list[str]) -> str | None:
    denied = sorted(r for r in rels if r in context.denied_paths)
    if denied:
        return f"a change to {', '.join(denied)} was already denied in this task; approve only if you changed your mind."
    dirty = sorted(r for r in rels if r in context.protected_paths)
    if not dirty:
        return None
    return (
        f"{', '.join(dirty)} had uncommitted changes before this task started (your work in progress). "
        "Trace will back it up first; approve only if you want Trace to change it."
    )


def _blocked_without_checkpoints(context: ToolContext) -> str | None:
    return None if context.checkpoints is not None else "file changes are disabled: no checkpoint store is available, so Trace couldn't undo them"


@dataclass
class PlannedChange:
    path: Path
    rel: str
    action: str  # "modified" | "created" | "deleted"
    old_text: str  # normalized; "" for created
    new_text: str | None  # normalized; None for deleted
    file: TextFile | None  # format of the existing file (None for created)

    def diff(self) -> str:
        if self.action == "created":
            return unified_diff(self.rel, "", self.new_text or "", old_label="/dev/null")
        if self.action == "deleted":
            return unified_diff(self.rel, self.old_text, "", new_label="/dev/null")
        return unified_diff(self.rel, self.old_text, self.new_text or "")

    def encoded(self) -> bytes:
        fmt = self.file or TextFile(text="", newline="\n", bom=False)
        return fmt.encode(self.new_text or "")


def commit_changes(changes: list[PlannedChange], context: ToolContext) -> ToolResult:
    """Back up, write, verify. All-or-nothing: on failure, files already written are rolled back."""
    store = context.checkpoints
    if store is None:
        return ToolResult.failure(ToolStatus.DENIED, _blocked_without_checkpoints(context) or "no checkpoints")
    written: list[tuple[PlannedChange, bytes | None]] = []
    try:
        for change in changes:
            previous = change.path.read_bytes() if change.path.exists() else None
            store.before_change(change.rel)
            if change.action == "deleted":
                change.path.unlink()
            else:
                data = change.encoded()
                _atomic_write(change.path, data)
                if change.path.read_bytes() != data:
                    raise EditError(f"verification failed: {change.rel} does not contain the intended content after writing")
            written.append((change, previous))
            store.after_change(change.rel)
    except (OSError, EditError) as exc:
        for change, previous in reversed(written):
            try:
                if previous is None:
                    change.path.unlink(missing_ok=True)
                else:
                    _atomic_write(change.path, previous)
                store.after_change(change.rel)
            except OSError:
                pass
        message = str(exc)
        return ToolResult.failure(ToolStatus.ERROR, f"no changes were kept: {message}")

    diffs = [c.diff() for c in changes]
    file_changes = []
    for change, diff in zip(changes, diffs, strict=True):
        added, removed = _diff_stats(diff)
        file_changes.append({"path": change.rel, "change": change.action, "added": added, "removed": removed, "diff": diff})
    summary = ", ".join(f"{fc['change']} {fc['path']} (+{fc['added']}/-{fc['removed']})" for fc in file_changes)
    return ToolResult.success(f"{summary}\n\n" + "\n".join(diffs), file_changes=file_changes)


# --------------------------------------------------------------------------- edit_file


class EditFileInput(ToolInput):
    path: str = Field(description="File to edit, relative to the project root.")
    expected_original: str = Field(
        min_length=1,
        description="Exact existing text to replace, including indentation. Must match exactly once "
        "unless replace_all is true. Include enough surrounding lines to be unique.",
    )
    replacement: str = Field(description="Text that replaces expected_original (may be empty to delete it).")
    replace_all: bool = Field(default=False, description="Replace every occurrence instead of exactly one.")


def _closest_match_hint(text: str, expected: str) -> str:
    lines = text.split("\n")
    needle_lines = [l for l in expected.split("\n") if l.strip()]
    if not needle_lines:
        return ""
    first = needle_lines[0]
    stripped = [l.strip() for l in lines]
    if first.strip() in stripped:
        index = stripped.index(first.strip())
        return (
            f" A line matching '{first.strip()[:80]}' exists at line {index + 1} but with different "
            "indentation or surrounding lines; re-read the file and copy the text exactly."
        )
    close = difflib.get_close_matches(first, lines, n=1, cutoff=0.6)
    if close:
        index = lines.index(close[0])
        window = "\n".join(f"{n + 1:>5} | {lines[n]}" for n in range(max(0, index - 2), min(len(lines), index + 3)))
        return f" Closest match is near line {index + 1}:\n{window}"
    return " Re-read the file: the text may have changed."


def plan_edit(args: EditFileInput, context: ToolContext) -> PlannedChange:
    target, rel = resolve_target(context, args.path)
    if not target.is_file():
        raise EditError(f"file not found: {rel} (use create_file to create it)")
    fmt = TextFile.read(target)
    expected = _normalize(args.expected_original)
    replacement = _normalize(args.replacement)
    count = fmt.text.count(expected)
    if count == 0:
        raise EditError(f"expected_original was not found in {rel}.{_closest_match_hint(fmt.text, expected)}")
    if count > 1 and not args.replace_all:
        starts = [m.start() for m in re.finditer(re.escape(expected), fmt.text)]
        line_numbers = [fmt.text.count("\n", 0, s) + 1 for s in starts]
        raise EditError(
            f"expected_original matches {count} places in {rel} (lines {', '.join(map(str, line_numbers[:10]))}). "
            "Include more surrounding lines to make it unique, or set replace_all=true."
        )
    if expected == replacement:
        raise EditError("replacement is identical to expected_original; nothing would change", ToolStatus.INVALID_INPUT)
    new_text = fmt.text.replace(expected, replacement) if args.replace_all else fmt.text.replace(expected, replacement, 1)
    return PlannedChange(target, rel, "modified", fmt.text, new_text, fmt)


class EditFileTool(Tool):
    name = "edit_file"
    description = (
        "Change an existing file by replacing exact text. expected_original must be copied exactly from "
        "the current file (read it first) and be unique unless replace_all=true. Returns a diff. "
        "Files are backed up automatically and line endings/encoding are preserved."
    )
    permission = PermissionLevel.WRITE
    input_model = EditFileInput

    def is_available(self, context: ToolContext) -> bool:
        return context.checkpoints is not None  # never offer changes Trace couldn't undo

    def assess(self, args: EditFileInput, context: ToolContext) -> Assessment:
        blocked = _blocked_without_checkpoints(context)
        try:
            change = plan_edit(args, context)
        except EditError as exc:
            return Assessment(self.permission, blocked=blocked, error=str(exc), error_status=exc.status)  # run() reports the precise error
        return Assessment(self.permission, requires_approval=_protection(context, [change.rel]), blocked=blocked, preview=change.diff(), paths=(change.rel,))

    def run(self, args: EditFileInput, context: ToolContext) -> ToolResult:
        try:
            change = plan_edit(args, context)
        except EditError as exc:
            return ToolResult.failure(exc.status, str(exc))
        return commit_changes([change], context)


# --------------------------------------------------------------------------- create_file / delete_file


class CreateFileInput(ToolInput):
    path: str = Field(description="New file path, relative to the project root.")
    content: str = Field(description="Full content of the new file.")


def plan_create(args: CreateFileInput, context: ToolContext) -> PlannedChange:
    target, rel = resolve_target(context, args.path)
    if target.exists():
        raise EditError(f"{rel} already exists; use edit_file to change it")
    content = _normalize(args.content)
    if content and not content.endswith("\n"):
        content += "\n"
    return PlannedChange(target, rel, "created", "", content, None)


class CreateFileTool(Tool):
    name = "create_file"
    description = "Create a new file with the given content. Fails if the file already exists."
    permission = PermissionLevel.WRITE
    input_model = CreateFileInput

    def is_available(self, context: ToolContext) -> bool:
        return context.checkpoints is not None  # never offer changes Trace couldn't undo

    def assess(self, args: CreateFileInput, context: ToolContext) -> Assessment:
        blocked = _blocked_without_checkpoints(context)
        try:
            change = plan_create(args, context)
        except EditError as exc:
            return Assessment(self.permission, blocked=blocked, error=str(exc), error_status=exc.status)
        return Assessment(self.permission, blocked=blocked, preview=change.diff(), paths=(change.rel,))

    def run(self, args: CreateFileInput, context: ToolContext) -> ToolResult:
        try:
            change = plan_create(args, context)
        except EditError as exc:
            return ToolResult.failure(exc.status, str(exc))
        return commit_changes([change], context)


class DeleteFileInput(ToolInput):
    path: str = Field(description="File to delete, relative to the project root.")


def plan_delete(args: DeleteFileInput, context: ToolContext) -> PlannedChange:
    target, rel = resolve_target(context, args.path)
    if not target.is_file():
        raise EditError(f"file not found: {rel}")
    fmt = TextFile.read(target)
    return PlannedChange(target, rel, "deleted", fmt.text, None, fmt)


class DeleteFileTool(Tool):
    name = "delete_file"
    description = "Delete a project file. Destructive: always requires user approval (backed up first)."
    permission = PermissionLevel.DESTRUCTIVE
    input_model = DeleteFileInput

    def is_available(self, context: ToolContext) -> bool:
        return context.checkpoints is not None  # never offer changes Trace couldn't undo

    def assess(self, args: DeleteFileInput, context: ToolContext) -> Assessment:
        blocked = _blocked_without_checkpoints(context)
        try:
            change = plan_delete(args, context)
        except EditError as exc:
            return Assessment(self.permission, blocked=blocked, error=str(exc), error_status=exc.status)
        return Assessment(self.permission, requires_approval=_protection(context, [change.rel]), blocked=blocked, preview=change.diff(), paths=(change.rel,))

    def run(self, args: DeleteFileInput, context: ToolContext) -> ToolResult:
        try:
            change = plan_delete(args, context)
        except EditError as exc:
            return ToolResult.failure(exc.status, str(exc))
        return commit_changes([change], context)


# --------------------------------------------------------------------------- apply_patch


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class Hunk:
    old_start: int
    lines: list[tuple[str, str]]  # (tag, text) with tag in " ", "-", "+"
    header: str

    @property
    def old_block(self) -> list[str]:
        return [t for tag, t in self.lines if tag in " -"]

    @property
    def new_block(self) -> list[str]:
        return [t for tag, t in self.lines if tag in " +"]


@dataclass
class FilePatch:
    old_path: str | None  # None = /dev/null (creation)
    new_path: str | None  # None = /dev/null (deletion)
    hunks: list[Hunk]


def _strip_prefix(path: str) -> str | None:
    path = path.split("\t")[0].strip()
    if path == "/dev/null":
        return None
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def parse_unified_diff(patch: str) -> list[FilePatch]:
    lines = _normalize(patch).split("\n")
    files: list[FilePatch] = []
    current: FilePatch | None = None
    hunk: Hunk | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            current = FilePatch(_strip_prefix(line[4:]), _strip_prefix(lines[i + 1][4:]), [])
            files.append(current)
            hunk = None
            i += 2
            continue
        match = _HUNK.match(line)
        if match:
            if current is None:
                raise EditError("hunk found before any '--- a/file' / '+++ b/file' header", ToolStatus.INVALID_INPUT)
            hunk = Hunk(old_start=int(match.group(1)), lines=[], header=line)
            current.hunks.append(hunk)
        elif hunk is not None and line[:1] in (" ", "-", "+"):
            hunk.lines.append((line[0], line[1:]))
        elif hunk is not None and line == "" and i < len(lines) - 1:
            hunk.lines.append((" ", ""))  # some generators drop the space on blank context lines
        # "\ No newline at end of file", "diff --git", "index ..." and trailing blanks are ignored
        i += 1
    if not files:
        raise EditError("no file headers found; expected a unified diff with '--- a/path' and '+++ b/path'", ToolStatus.INVALID_INPUT)
    for f in files:
        if f.old_path is not None and f.new_path is not None and not f.hunks:
            raise EditError(f"no hunks for {f.new_path}", ToolStatus.INVALID_INPUT)
    return files


def _find_block(lines: list[str], block: list[str], hint: int) -> int | None:
    """Exact match nearest to ``hint``; falls back to ignoring trailing whitespace."""
    if not block:
        return min(max(hint, 0), len(lines))
    for compare in (lambda a, b: a == b, lambda a, b: a.rstrip() == b.rstrip()):
        positions = [
            i for i in range(len(lines) - len(block) + 1)
            if all(compare(lines[i + k], block[k]) for k in range(len(block)))
        ]
        if positions:
            return min(positions, key=lambda p: abs(p - hint))
    return None


def _split_lines(text: str) -> tuple[list[str], bool]:
    if text == "":
        return [], True
    trailing = text.endswith("\n")
    return (text[:-1] if trailing else text).split("\n"), trailing


def plan_patch(patch: str, context: ToolContext) -> list[PlannedChange]:
    changes: list[PlannedChange] = []
    for file_patch in parse_unified_diff(patch):
        if file_patch.old_path is None and file_patch.new_path is None:
            raise EditError("a patch section has /dev/null on both sides", ToolStatus.INVALID_INPUT)
        if file_patch.old_path is None:  # creation
            target, rel = resolve_target(context, file_patch.new_path or "")
            if target.exists():
                raise EditError(f"patch creates {rel}, but it already exists")
            body = [t for h in file_patch.hunks for tag, t in h.lines if tag == "+"]
            changes.append(PlannedChange(target, rel, "created", "", "\n".join(body) + "\n", None))
            continue
        target, rel = resolve_target(context, file_patch.old_path)
        if not target.is_file():
            raise EditError(f"patch targets {rel}, which does not exist")
        fmt = TextFile.read(target)
        if file_patch.new_path is None:  # deletion
            changes.append(PlannedChange(target, rel, "deleted", fmt.text, None, fmt))
            continue
        if file_patch.new_path != file_patch.old_path:
            raise EditError("renames are not supported in patches; create the new file and delete the old one")
        lines, trailing = _split_lines(fmt.text)
        offset = 0
        for hunk in file_patch.hunks:
            hint = hunk.old_start - 1 + offset
            if not hunk.old_block:
                hint = hunk.old_start + offset  # pure insertion goes after line old_start
            position = _find_block(lines, hunk.old_block, hint)
            if position is None:
                first = next((t for tag, t in hunk.lines if tag in " -"), "")
                raise EditError(
                    f"hunk '{hunk.header}' does not apply to {rel}: its context/removed lines were not found "
                    f"(first expected line: {first.strip()[:100]!r}). Re-read the file and regenerate the patch."
                )
            lines[position : position + len(hunk.old_block)] = hunk.new_block
            offset += len(hunk.new_block) - len(hunk.old_block)
        new_text = "\n".join(lines) + ("\n" if trailing and lines else "")
        if new_text == fmt.text:
            raise EditError(f"patch makes no change to {rel}", ToolStatus.INVALID_INPUT)
        changes.append(PlannedChange(target, rel, "modified", fmt.text, new_text, fmt))
    rels = [c.rel for c in changes]
    if len(rels) != len(set(rels)):
        raise EditError("the patch changes the same file in two sections; merge them into one", ToolStatus.INVALID_INPUT)
    return changes


class ApplyPatchInput(ToolInput):
    patch: str = Field(
        min_length=1,
        description="Unified diff ('--- a/path', '+++ b/path', '@@ ... @@' hunks with ' ', '-', '+' lines). "
        "May touch several files; applied all-or-nothing. Line numbers are hints; context must match exactly.",
    )


class ApplyPatchTool(Tool):
    name = "apply_patch"
    description = (
        "Apply a unified diff to one or more files, all-or-nothing. Best for multi-hunk or multi-file "
        "changes; for a single replacement prefer edit_file. Use /dev/null to create or delete files."
    )
    permission = PermissionLevel.WRITE
    input_model = ApplyPatchInput

    def is_available(self, context: ToolContext) -> bool:
        return context.checkpoints is not None  # never offer changes Trace couldn't undo

    def assess(self, args: ApplyPatchInput, context: ToolContext) -> Assessment:
        blocked = _blocked_without_checkpoints(context)
        try:
            changes = plan_patch(args.patch, context)
        except EditError as exc:
            return Assessment(self.permission, blocked=blocked, error=str(exc), error_status=exc.status)
        level = PermissionLevel.DESTRUCTIVE if any(c.action == "deleted" for c in changes) else self.permission
        return Assessment(
            level,
            requires_approval=_protection(context, [c.rel for c in changes]),
            blocked=blocked,
            preview="\n".join(c.diff() for c in changes),
            paths=tuple(c.rel for c in changes),
        )

    def run(self, args: ApplyPatchInput, context: ToolContext) -> ToolResult:
        try:
            changes = plan_patch(args.patch, context)
        except EditError as exc:
            return ToolResult.failure(exc.status, str(exc))
        return commit_changes(changes, context)
