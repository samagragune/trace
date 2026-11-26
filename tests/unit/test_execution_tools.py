import subprocess
import sys
from pathlib import Path

import pytest

from trace_agent.safety.git_guard import dirty_files
from trace_agent.safety.permissions import PermissionLevel
from trace_agent.tools.base import ToolContext, ToolStatus
from trace_agent.tools.builtin.execution import (
    InspectGitInput,
    InspectGitTool,
    RunCommandInput,
    RunCommandTool,
    RunTestsInput,
    RunTestsTool,
    summarize_test_output,
)

PY = f'"{sys.executable}"'


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "tests").mkdir(parents=True)
    (root / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (root / "tests" / "test_calc.py").write_text(
        "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parents[1]))\n"
        "from calc import add\n\ndef test_add():\n    assert add(2, 2) == 4\n"
    )
    return root


def ctx(root: Path, **kw) -> ToolContext:
    return ToolContext(project_root=root, timeout_seconds=60, **kw)


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, timeout=30)


# ---- run_command -------------------------------------------------------------------------------------


def test_run_command_success_and_failure(proj):
    ok = RunCommandTool().run(RunCommandInput(command=f"{PY} -c \"print('hello')\""), ctx(proj))
    assert ok.ok and "hello" in ok.output and ok.data["exit_code"] == 0
    bad = RunCommandTool().run(RunCommandInput(command=f"{PY} -c \"raise SystemExit(4)\""), ctx(proj))
    assert bad.status is ToolStatus.ERROR and "exited with code 4" in bad.error


def test_run_command_assessment(proj):
    tool = RunCommandTool()
    assert tool.assess(RunCommandInput(command="git status"), ctx(proj)).permission is PermissionLevel.READ
    assert tool.assess(RunCommandInput(command="python app.py"), ctx(proj)).permission is PermissionLevel.EXECUTE
    assert tool.assess(RunCommandInput(command="rm -rf src"), ctx(proj)).permission is PermissionLevel.DESTRUCTIVE
    assert "blocked" in tool.assess(RunCommandInput(command="git push"), ctx(proj)).blocked
    piped = tool.assess(RunCommandInput(command="pytest | head"), ctx(proj))
    assert piped.error and piped.error_status is ToolStatus.INVALID_INPUT
    outside = tool.assess(RunCommandInput(command="pytest", working_directory=".."), ctx(proj))
    assert outside.error_status is ToolStatus.DENIED


def test_run_command_timeout(proj):
    r = RunCommandTool().run(RunCommandInput(command=f"{PY} -c \"import time; time.sleep(20)\"", timeout_seconds=1), ctx(proj))
    assert r.status is ToolStatus.TIMEOUT and "killed" in r.error


# ---- run_tests ---------------------------------------------------------------------------------------


def test_run_tests_reports_verification(proj):
    command = f"{PY} -m pytest -q -p no:cacheprovider"
    failing = RunTestsTool().run(RunTestsInput(), ctx(proj, test_command=command))
    assert failing.status is ToolStatus.ERROR
    v = failing.data["verification"]
    assert v["passed"] is False and v["exit_code"] == 1 and "1 failed" in v["summary"]
    (proj / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    passing = RunTestsTool().run(RunTestsInput(), ctx(proj, test_command=command))
    assert passing.ok and passing.data["verification"]["passed"] and "1 passed" in passing.output


def test_run_tests_without_command(proj):
    assert RunTestsTool().assess(RunTestsInput(), ctx(proj)).error.startswith("no test command")


@pytest.mark.parametrize(
    ("output", "summary"),
    [
        ("....\n========== 2 failed, 5 passed in 0.31s ==========\n", "2 failed, 5 passed in 0.31s"),
        ("Tests:       1 failed, 3 passed, 4 total\nTime: 1s", "Tests:       1 failed, 3 passed, 4 total"),
        ("Ran 3 tests in 0.002s\n\nFAILED (failures=1)\n", "Ran 3 tests in 0.002s FAILED (failures=1)"),
        ("test result: ok. 4 passed; 0 failed\n", "test result: ok. 4 passed; 0 failed"),
        ("--- FAIL: TestAdd (0.00s)\nFAIL\tgithub.com/x/calc\t0.01s\n", "FAIL\tgithub.com/x/calc\t0.01s"),
        ("something unusual\nlast line\n", "last line"),
    ],
)
def test_summaries(output, summary):
    assert summarize_test_output(output) == summary


# ---- git ---------------------------------------------------------------------------------------------


@pytest.fixture
def repo(proj):
    try:
        git(proj, "init", "-q", "-b", "main")
        git(proj, "-c", "user.email=t@example.com", "-c", "user.name=T", "add", ".")
        git(proj, "-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-q", "-m", "init")
    except (OSError, subprocess.SubprocessError):
        pytest.skip("git not available")
    return proj


def test_dirty_files_detects_user_work(repo, tmp_path):
    assert dirty_files(repo) == frozenset()
    (repo / "calc.py").write_text("changed\n")
    (repo / "notes.txt").write_text("new\n")
    assert dirty_files(repo) == {"calc.py", "notes.txt"}
    assert dirty_files(tmp_path / "nowhere") is None


def test_inspect_git(repo):
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    tool = InspectGitTool()
    assert tool.is_available(ctx(repo))
    assert "+    return a + b" in tool.run(InspectGitInput(operation="diff"), ctx(repo)).output
    assert "init" in tool.run(InspectGitInput(operation="log"), ctx(repo)).output
    blame = tool.run(InspectGitInput(operation="blame", path="calc.py", start_line=1, end_line=1), ctx(repo))
    assert blame.ok and "def add(a, b):" in blame.output
    assert tool.run(InspectGitInput(operation="blame"), ctx(repo)).status is ToolStatus.INVALID_INPUT


def test_inspect_git_unavailable_outside_repo(tmp_path):
    assert not InspectGitTool().is_available(ctx(tmp_path))
