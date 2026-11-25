"""Project-boundary enforcement and sensitive-file detection.

Every tool that touches the filesystem must route model-supplied paths through
:func:`resolve_in_project`. Resolution follows symlinks and normalizes ``..``
*before* the boundary check, so ``../../etc/passwd``, absolute paths, symlinks
pointing outside the project and other-drive paths on Windows are all rejected.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

from trace_agent.errors import TraceError


class UnsafePathError(TraceError):
    """A path was rejected by the safety layer."""


class PathOutsideProjectError(UnsafePathError):
    pass


#: Filenames that commonly contain credentials. Matched case-insensitively against
#: the final path component. Deliberately conservative: false positives cost a
#: prompt, false negatives can leak a secret to a cloud model.
DEFAULT_SENSITIVE_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials.json",
    "secrets.*",
)


def resolve_in_project(project_root: str | Path, candidate: str | Path) -> Path:
    """Resolve ``candidate`` against ``project_root`` and guarantee it stays inside.

    Returns the absolute, resolved path. Raises :class:`PathOutsideProjectError`
    if it escapes the root, or :class:`UnsafePathError` if it is malformed.
    The target itself does not need to exist (tools may create files).
    """
    try:
        root = Path(project_root).resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise UnsafePathError(f"project root '{project_root}' does not exist") from exc
    if not root.is_dir():
        raise UnsafePathError(f"project root '{project_root}' is not a directory")

    candidate_str = str(candidate)
    if not candidate_str or "\x00" in candidate_str:
        raise UnsafePathError("path is empty or contains a null byte")

    raw = Path(candidate_str)
    try:
        target = (raw if raw.is_absolute() else root / raw).resolve(strict=False)
    except (OSError, ValueError) as exc:
        raise UnsafePathError(f"path '{candidate_str}' cannot be resolved") from exc

    if not target.is_relative_to(root):
        raise PathOutsideProjectError(f"path '{candidate_str}' is outside the project directory")
    return target


def is_sensitive_path(path: str | Path, patterns: tuple[str, ...] = DEFAULT_SENSITIVE_PATTERNS) -> bool:
    name = Path(path).name.lower()
    return any(fnmatch.fnmatchcase(name, pattern.lower()) for pattern in patterns)
