"""Directories that are never worth scanning or showing a model.

Kept dependency-free so both ``tools`` and ``context`` can import it without cycles.
"""

from __future__ import annotations

import fnmatch

DEFAULT_EXCLUDED_DIRS = frozenset(
    {
        ".git", ".hg", ".svn", ".venv", "venv", "env", "__pycache__", "node_modules", ".mypy_cache",
        ".pytest_cache", ".ruff_cache", ".tox", ".nox", "dist", "build", ".next", ".idea", ".vscode",
        "target", ".gradle", ".eggs", "site-packages", ".cache", "coverage", "htmlcov",
    }
)


def is_excluded_dir(name: str, extra_patterns: list[str] | tuple[str, ...] = ()) -> bool:
    return (
        name in DEFAULT_EXCLUDED_DIRS
        or name.endswith(".egg-info")
        or any(fnmatch.fnmatch(name, p) for p in extra_patterns)
    )
