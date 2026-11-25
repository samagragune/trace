import os

import pytest

from trace_agent.safety.paths import PathOutsideProjectError, UnsafePathError, is_sensitive_path, resolve_in_project
from trace_agent.safety.permissions import Decision, ExecutionMode, PermissionLevel, decide


# ---- permission policy ---------------------------------------------------------


def test_reads_always_allowed():
    for mode in ExecutionMode:
        assert decide(mode, PermissionLevel.READ) is Decision.ALLOW


def test_plan_mode_never_modifies():
    assert decide(ExecutionMode.PLAN, PermissionLevel.WRITE) is Decision.DENY
    assert decide(ExecutionMode.PLAN, PermissionLevel.DESTRUCTIVE) is Decision.DENY


def test_confirm_mode_asks_for_everything_but_reads():
    for level in (PermissionLevel.WRITE, PermissionLevel.EXECUTE, PermissionLevel.DESTRUCTIVE):
        assert decide(ExecutionMode.CONFIRM, level) is Decision.ASK


def test_auto_mode_still_asks_for_destructive():
    assert decide(ExecutionMode.AUTO, PermissionLevel.WRITE) is Decision.ALLOW
    assert decide(ExecutionMode.AUTO, PermissionLevel.DESTRUCTIVE) is Decision.ASK


def test_policy_is_total():
    for mode in ExecutionMode:
        for level in PermissionLevel:
            assert isinstance(decide(mode, level), Decision)


# ---- path boundaries -------------------------------------------------------------


def test_relative_paths_resolve_inside(project):
    assert resolve_in_project(project, "src/app.py") == (project / "src" / "app.py").resolve()
    assert resolve_in_project(project, "src/new_file.py").name == "new_file.py"  # need not exist


@pytest.mark.parametrize("bad", ["../outside.txt", "src/../../outside.txt", "..", "../project-evil/x"])
def test_traversal_rejected(project, bad):
    with pytest.raises(PathOutsideProjectError):
        resolve_in_project(project, bad)


def test_absolute_path_outside_rejected(project, tmp_path):
    with pytest.raises(PathOutsideProjectError):
        resolve_in_project(project, tmp_path / "elsewhere.txt")


def test_absolute_path_inside_accepted(project):
    assert resolve_in_project(project, project / "src" / "app.py").is_file()


@pytest.mark.parametrize("bad", ["", "a\x00b"])
def test_malformed_paths_rejected(project, bad):
    with pytest.raises(UnsafePathError):
        resolve_in_project(project, bad)


def test_missing_root_rejected(tmp_path):
    with pytest.raises(UnsafePathError):
        resolve_in_project(tmp_path / "missing", "x")


def test_symlink_escape_rejected(project, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("s", encoding="utf-8")
    link = project / "link.txt"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this system")
    with pytest.raises(PathOutsideProjectError):
        resolve_in_project(project, "link.txt")


# ---- sensitive files ---------------------------------------------------------------


@pytest.mark.parametrize("name", [".env", ".env.local", "server.PEM", "id_rsa", "id_ed25519.pub", "secrets.yaml"])
def test_sensitive_files_detected(name):
    assert is_sensitive_path(f"some/dir/{name}")


@pytest.mark.parametrize("name", ["app.py", "environment.py", "keyboard.js", "README.md"])
def test_normal_files_not_sensitive(name):
    assert not is_sensitive_path(name)
