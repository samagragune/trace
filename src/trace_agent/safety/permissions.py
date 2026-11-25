"""Permission levels, execution modes and the authorization policy.

Spec security rule: *a model tool call must never be treated as authorization
by itself.* Every tool declares a :class:`PermissionLevel`; the user picks an
:class:`ExecutionMode`; :func:`decide` combines the two into ALLOW / ASK / DENY.
The orchestrator (Phase 2) must consult this before executing anything.

The policy is a pure function over a fixed table so it is trivial to audit and test.
"""

from __future__ import annotations

from enum import StrEnum


class PermissionLevel(StrEnum):
    READ = "read"  # inspect files, search, git status
    WRITE = "write"  # create or modify project files
    EXECUTE = "execute"  # run commands or tests (side effects possible)
    DESTRUCTIVE = "destructive"  # delete files, irreversible operations


class ExecutionMode(StrEnum):
    PLAN = "plan"  # investigate and recommend, never modify
    CONFIRM = "confirm"  # ask before modifying or running commands (default)
    AUTO = "auto"  # run permitted operations; destructive ones still ask


class Decision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


DEFAULT_EXECUTION_MODE = ExecutionMode.CONFIRM

_POLICY: dict[ExecutionMode, dict[PermissionLevel, Decision]] = {
    ExecutionMode.PLAN: {
        PermissionLevel.READ: Decision.ALLOW,
        PermissionLevel.WRITE: Decision.DENY,
        # Reproducing a bug may need a command; plan mode never runs one unasked.
        PermissionLevel.EXECUTE: Decision.ASK,
        PermissionLevel.DESTRUCTIVE: Decision.DENY,
    },
    ExecutionMode.CONFIRM: {
        PermissionLevel.READ: Decision.ALLOW,
        PermissionLevel.WRITE: Decision.ASK,
        # Until Phase 4 adds command risk classification, every command is treated as risky.
        PermissionLevel.EXECUTE: Decision.ASK,
        PermissionLevel.DESTRUCTIVE: Decision.ASK,
    },
    ExecutionMode.AUTO: {
        PermissionLevel.READ: Decision.ALLOW,
        PermissionLevel.WRITE: Decision.ALLOW,
        PermissionLevel.EXECUTE: Decision.ALLOW,
        PermissionLevel.DESTRUCTIVE: Decision.ASK,
    },
}


def decide(mode: ExecutionMode, level: PermissionLevel) -> Decision:
    """Return the authorization decision for an action at ``level`` under ``mode``."""
    return _POLICY[mode][level]
