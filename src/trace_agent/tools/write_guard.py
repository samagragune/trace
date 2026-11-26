"""Write guard: makes "denied" and "protected" binding for commands, not just for editing tools.

Found live in Build 004: after an edit to a protected file was denied, the model tried
``run_command("python -c \\"Path('shop/discounts.py').write_text(...)\\"")``. Editing tools enforce
protection, but any command that runs code can write files. The guard closes that gap:

1. **Before** a command runs, back up the bytes of every guarded file (the user's uncommitted
   work + files whose edit was denied) and fingerprint the project's files (size + mtime).
2. **After** it runs, compare fingerprints to find every file the command created, changed or
   deleted.
3. Any guarded file that changed is **restored immediately** and reported as a violation. Other
   changes are reported, so the user and the model can see what the command did (they bypass
   checkpoints, so ``trace undo`` can't restore them; git can).

Dependency, cache and VCS directories are not fingerprinted (``context.ignore``), so build and
test artefacts like ``__pycache__`` don't show up as changes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

from trace_agent.context.project import iter_project_files

MAX_FINGERPRINT_FILES = 20_000
MAX_GUARD_BACKUP_BYTES = 20_000_000

T = TypeVar("T")

Fingerprint = dict[str, tuple[int, int]]  # rel -> (size, mtime_ns)


def fingerprint(root: Path) -> Fingerprint:
    prints: Fingerprint = {}
    for count, path in enumerate(iter_project_files(root)):
        if count >= MAX_FINGERPRINT_FILES:
            break
        try:
            stat = path.stat()
        except OSError:
            continue
        prints[path.relative_to(root).as_posix()] = (stat.st_size, stat.st_mtime_ns)
    return prints


@dataclass
class GuardReport:
    changed: list[str] = field(default_factory=list)  # all files the command created/changed/deleted
    restored: list[str] = field(default_factory=list)  # guarded files put back by Trace
    restore_errors: list[str] = field(default_factory=list)

    @property
    def violation(self) -> bool:
        return bool(self.restored or self.restore_errors)

    def describe(self) -> str:
        lines = []
        if self.restored:
            lines.append(
                "VIOLATION: this command modified files that are protected (the user's uncommitted work, or files "
                f"whose change was denied): {', '.join(self.restored)}. Trace restored them. Do not try to change "
                "these files by any means; choose another approach or finish with a recommendation."
            )
        if self.restore_errors:
            lines.append(f"Trace could not restore: {'; '.join(self.restore_errors)}")
        unguarded = [p for p in self.changed if p not in self.restored]
        if unguarded:
            shown = ", ".join(unguarded[:15]) + (f" (+{len(unguarded) - 15} more)" if len(unguarded) > 15 else "")
            lines.append(f"note: this command changed files outside Trace's editing tools (not covered by `trace undo`): {shown}")
        return "\n".join(lines)


def run_guarded(root: Path, guarded: frozenset[str], action: Callable[[], T]) -> tuple[T, GuardReport]:
    root = root.resolve()
    backups: dict[str, bytes | None] = {}
    budget = MAX_GUARD_BACKUP_BYTES
    for rel in sorted(guarded):
        path = root / rel
        if not path.is_file():
            backups[rel] = None
            continue
        size = path.stat().st_size
        if size <= budget:
            backups[rel] = path.read_bytes()
            budget -= size
    before = fingerprint(root)

    result = action()

    after = fingerprint(root)
    report = GuardReport()
    report.changed = sorted(
        {rel for rel in before.keys() ^ after.keys()} | {rel for rel in before.keys() & after.keys() if before[rel] != after[rel]}
    )
    for rel in guarded:
        if rel not in backups:
            continue
        original = backups[rel]
        path = root / rel
        current = path.read_bytes() if path.is_file() else None
        if current == original:
            continue
        try:
            if original is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(original)
            report.restored.append(rel)
        except OSError as exc:
            report.restore_errors.append(f"{rel}: {exc}")
    report.restored.sort()
    return result, report
