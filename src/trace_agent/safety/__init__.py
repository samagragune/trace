"""Authorization, project boundaries and (later) recovery.

Implemented in Phase 0: permission policy, path boundary checks, sensitive-file
detection. Planned: command risk classification, approval prompts, credential
redaction, checkpoints/backups (Phases 2 and 4).
"""

from trace_agent.safety.paths import (
    DEFAULT_SENSITIVE_PATTERNS,
    PathOutsideProjectError,
    UnsafePathError,
    is_sensitive_path,
    resolve_in_project,
)
from trace_agent.safety.permissions import (
    DEFAULT_EXECUTION_MODE,
    Decision,
    ExecutionMode,
    PermissionLevel,
    decide,
)

__all__ = [
    "DEFAULT_EXECUTION_MODE",
    "DEFAULT_SENSITIVE_PATTERNS",
    "Decision",
    "ExecutionMode",
    "PathOutsideProjectError",
    "PermissionLevel",
    "UnsafePathError",
    "decide",
    "is_sensitive_path",
    "resolve_in_project",
]
