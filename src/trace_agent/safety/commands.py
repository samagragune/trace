"""Command parsing and risk classification for run_command / run_tests.

Commands run **without a shell**: no pipes, redirection, chaining or variable expansion.
That removes a whole class of injection, and makes classification reliable because what is
classified is exactly what runs.

Risk levels map onto the permission policy (``safety.permissions``):

=============  =================  ==========================================================
Risk           Permission         Examples
=============  =================  ==========================================================
SAFE           READ               test runners, linters/type-checkers, git read commands,
                                   version checks. Runs without asking in every mode.
NORMAL         EXECUTE            anything not recognized (scripts, builds, installs...).
                                   Asks in confirm/plan; allowed in auto.
DESTRUCTIVE    DESTRUCTIVE        deleting/moving files, discarding git work, killing procs.
                                   Always asks (denied in plan).
BLOCKED        (never runs)       privilege escalation, disk tools, publishing, git push.
=============  =================  ==========================================================

"SAFE" means "doesn't change the project or system state by design", not "harmless": a test
suite executes project code. Running tests is the core verification step, so this is an
accepted trade-off (ADR-031).
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePath

from trace_agent.errors import TraceError
from trace_agent.safety.permissions import PermissionLevel


class CommandRisk(StrEnum):
    SAFE = "safe"
    NORMAL = "normal"
    DESTRUCTIVE = "destructive"
    BLOCKED = "blocked"


RISK_PERMISSION = {
    CommandRisk.SAFE: PermissionLevel.READ,
    CommandRisk.NORMAL: PermissionLevel.EXECUTE,
    CommandRisk.DESTRUCTIVE: PermissionLevel.DESTRUCTIVE,
}

SHELL_OPERATORS = frozenset({"|", "||", "&", "&&", ";", ">", ">>", "<", "<<", "2>", "2>&1", "1>", "&>"})


#: A backslash that is not followed by a double quote or another backslash.
_WINDOWS_LONE_BACKSLASH = re.compile(r'\\(?![\\"])')


class CommandParseError(TraceError):
    pass


@dataclass(frozen=True)
class Classification:
    risk: CommandRisk
    reason: str

    @property
    def permission(self) -> PermissionLevel | None:
        return RISK_PERMISSION.get(self.risk)


def split_command(command: str) -> list[str]:
    """Split a command line into argv without invoking a shell."""
    text = command.strip()
    if not text:
        raise CommandParseError("empty command")
    if "\n" in text or "\r" in text:
        raise CommandParseError("multi-line commands are not supported; run one command at a time")
    try:
        if os.name == "nt":
            # POSIX rules handle quotes and backslash-quote escapes. First double every backslash that
            # does not escape a quote or another backslash, so Windows paths like C:\src\a.py survive.
            argv = shlex.split(_WINDOWS_LONE_BACKSLASH.sub(r"\\\\", text), posix=True)
        else:
            argv = shlex.split(text, posix=True)
    except ValueError as exc:
        raise CommandParseError(f"cannot parse command: {exc}") from None
    operators = [t for t in argv if t in SHELL_OPERATORS]
    if operators or any("`" in t or "$(" in t for t in argv):
        raise CommandParseError(
            "shell operators (pipes, redirects, &&, ;, `...`, $(...)) are not supported: commands run "
            "without a shell. Run one command at a time."
        )
    return argv


def _program(argv: list[str]) -> str:
    name = PurePath(argv[0]).name.lower()
    for suffix in (".exe", ".cmd", ".bat", ".ps1"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name


BLOCKED_PROGRAMS = {
    "sudo": "privilege escalation", "su": "privilege escalation", "doas": "privilege escalation",
    "runas": "privilege escalation", "shutdown": "system power control", "reboot": "system power control",
    "halt": "system power control", "poweroff": "system power control", "mkfs": "disk formatting",
    "format": "disk formatting", "diskpart": "disk partitioning", "fdisk": "disk partitioning",
    "dd": "raw disk writes", "reg": "Windows registry changes", "regedit": "Windows registry changes",
    "crontab": "system scheduling", "schtasks": "system scheduling",
}
DESTRUCTIVE_PROGRAMS = {
    "rm": "deletes files", "rmdir": "deletes directories", "del": "deletes files", "erase": "deletes files",
    "rd": "deletes directories", "remove-item": "deletes files", "mv": "moves/overwrites files",
    "move": "moves/overwrites files", "move-item": "moves/overwrites files", "chmod": "changes permissions",
    "chown": "changes ownership", "icacls": "changes permissions", "kill": "terminates processes",
    "killall": "terminates processes", "pkill": "terminates processes", "taskkill": "terminates processes",
    "truncate": "truncates files", "shred": "destroys files",
}
SAFE_PROGRAMS = {
    "pytest", "py.test", "tox", "nox", "mypy", "pyright", "flake8", "pylint", "jest", "vitest", "mocha",
    "tsc", "eslint", "go", "cargo", "mvn", "gradle", "gradlew", "rspec", "phpunit", "dotnet",
}
SAFE_VERSION_FLAGS = {"--version", "-v", "-V", "version"}
GIT_SAFE = {"status", "diff", "log", "show", "blame", "branch", "rev-parse", "ls-files", "grep"}
GIT_DESTRUCTIVE = {"reset", "clean", "checkout", "restore", "rebase", "stash", "rm", "mv", "commit", "merge", "cherry-pick", "revert", "switch", "tag", "am", "apply"}
GIT_BLOCKED = {"push": "pushing to remotes is never automatic", "remote": "remote configuration", "config": "git configuration", "filter-branch": "history rewriting", "gc": "garbage collection", "prune": "object pruning"}
PACKAGE_RUNNERS = {"npm", "pnpm", "yarn", "bun"}
PACKAGE_SAFE_SUBCOMMANDS = {"test", "t", "run-script test", "lint"}
PACKAGE_BLOCKED = {"publish": "publishing packages", "unpublish": "unpublishing packages", "login": "registry login", "adduser": "registry login"}
PACKAGE_DESTRUCTIVE = {"uninstall", "remove", "rm", "un", "prune"}


def classify(argv: list[str]) -> Classification:
    if not argv:
        return Classification(CommandRisk.BLOCKED, "empty command")
    program = _program(argv)
    args = argv[1:]
    lowered = [a.lower() for a in args]

    if program in BLOCKED_PROGRAMS or program.startswith("mkfs"):
        return Classification(CommandRisk.BLOCKED, BLOCKED_PROGRAMS.get(program, "disk formatting"))
    if program in ("bash", "sh", "zsh", "cmd", "powershell", "pwsh") and any(a in ("-c", "/c", "-command", "/k") for a in lowered):
        return Classification(CommandRisk.BLOCKED, "nested shells bypass command screening")

    if program == "git":
        sub = next((a for a in lowered if not a.startswith("-")), "")
        if sub in GIT_BLOCKED:
            return Classification(CommandRisk.BLOCKED, GIT_BLOCKED[sub])
        if sub in GIT_DESTRUCTIVE:
            return Classification(CommandRisk.DESTRUCTIVE, f"git {sub} can discard or rewrite work")
        if sub in GIT_SAFE and not (sub == "branch" and any(a in ("-d", "-D", "--delete", "-m", "-M") for a in args)):
            return Classification(CommandRisk.SAFE, f"read-only git {sub}")
        return Classification(CommandRisk.NORMAL, f"git {sub or '(no subcommand)'}")

    if program in DESTRUCTIVE_PROGRAMS:
        return Classification(CommandRisk.DESTRUCTIVE, DESTRUCTIVE_PROGRAMS[program])

    if program in PACKAGE_RUNNERS:
        sub = lowered[0] if lowered else ""
        if sub in PACKAGE_BLOCKED:
            return Classification(CommandRisk.BLOCKED, PACKAGE_BLOCKED[sub])
        if sub in PACKAGE_DESTRUCTIVE:
            return Classification(CommandRisk.DESTRUCTIVE, f"{program} {sub} removes packages")
        if sub in ("test", "t") or (sub == "run" and len(lowered) > 1 and lowered[1] in ("test", "lint", "typecheck")):
            return Classification(CommandRisk.SAFE, f"{program} test/lint script")
        return Classification(CommandRisk.NORMAL, f"{program} {sub}".strip())

    if program in ("python", "python3", "py", "pythonw"):
        if lowered[:2] in (["-m", "pytest"], ["-m", "unittest"], ["-m", "mypy"], ["-m", "pyflakes"], ["-m", "flake8"]):
            return Classification(CommandRisk.SAFE, f"python -m {lowered[1]}")
        if lowered[:1] and lowered[0] in SAFE_VERSION_FLAGS:
            return Classification(CommandRisk.SAFE, "version check")
        if lowered[:2] == ["-m", "pip"] and any(a in ("uninstall",) for a in lowered):
            return Classification(CommandRisk.DESTRUCTIVE, "pip uninstall removes packages")
        return Classification(CommandRisk.NORMAL, "runs Python code")

    if program == "ruff":
        return Classification(CommandRisk.SAFE if "--fix" not in lowered and "format" not in lowered else CommandRisk.NORMAL, "ruff")
    if program == "go" and lowered[:1] and lowered[0] in ("test", "vet", "build", "version"):
        return Classification(CommandRisk.SAFE, f"go {lowered[0]}")
    if program == "cargo" and lowered[:1] and lowered[0] in ("test", "check", "build", "clippy", "--version"):
        return Classification(CommandRisk.SAFE, f"cargo {lowered[0]}")
    if program in ("mvn", "gradle", "gradlew") and any(a in ("test", "verify", "check") for a in lowered):
        return Classification(CommandRisk.SAFE, f"{program} test")
    if program == "dotnet" and lowered[:1] == ["test"]:
        return Classification(CommandRisk.SAFE, "dotnet test")
    if program in SAFE_PROGRAMS - {"go", "cargo", "mvn", "gradle", "gradlew", "dotnet"}:
        if any(a in ("--fix", "--write", "-u", "--updatesnapshot", "--update-snapshots") for a in lowered):
            return Classification(CommandRisk.NORMAL, f"{program} with a file-modifying flag")
        return Classification(CommandRisk.SAFE, f"{program} (test/lint tool)")
    if args and args[0] in SAFE_VERSION_FLAGS and len(args) == 1:
        return Classification(CommandRisk.SAFE, "version check")
    return Classification(CommandRisk.NORMAL, f"runs {program}")
