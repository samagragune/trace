"""Git safety: find files with pre-existing uncommitted user changes, and run read-only git commands.

Spec: "Inspect Git status before modifying files. Never overwrite pre-existing uncommitted
changes." At task start Trace snapshots which files are already modified or untracked.
Edits to those files are always escalated to explicit user approval (even in auto mode)
and are always backed up, so the user's in-progress work can't be clobbered silently.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

GIT_TIMEOUT_SECONDS = 10.0


def run_git(cwd: Path, args: list[str], timeout: float = GIT_TIMEOUT_SECONDS) -> subprocess.CompletedProcess[str] | None:
    """Run ``git <args>`` with no shell. Returns None when git is missing or times out."""
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def repo_root(project_root: Path) -> Path | None:
    proc = run_git(project_root, ["rev-parse", "--show-toplevel"])
    if proc is None or proc.returncode != 0 or not proc.stdout.strip():
        return None
    return Path(proc.stdout.strip()).resolve()


def dirty_files(project_root: Path) -> frozenset[str] | None:
    """Project-relative posix paths that have uncommitted changes (modified, staged or untracked).

    Returns None when the project isn't inside a git repository (nothing to protect via git;
    checkpoints still apply).
    """
    root = project_root.resolve()
    top = repo_root(root)
    if top is None:
        return None
    proc = run_git(root, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
    if proc is None or proc.returncode != 0:
        return None
    paths: set[str] = set()
    entries = proc.stdout.split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        if status[0] in "RC":  # renames/copies are followed by the original path
            i += 1
        absolute = (top / path).resolve()
        if absolute.is_relative_to(root):
            paths.add(absolute.relative_to(root).as_posix())
    return frozenset(paths)
