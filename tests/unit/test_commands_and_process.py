import os
import sys
import threading
import time

import pytest

from trace_agent.safety.commands import CommandParseError, CommandRisk, classify, split_command
from trace_agent.safety.permissions import PermissionLevel
from trace_agent.tools.process import build_env, run_process

PY = sys.executable


@pytest.mark.parametrize(
    ("command", "risk"),
    [
        ("python -m pytest -q", CommandRisk.SAFE),
        ("pytest tests/test_x.py", CommandRisk.SAFE),
        ("npm test", CommandRisk.SAFE),
        ("pnpm run test", CommandRisk.SAFE),
        ("go test ./...", CommandRisk.SAFE),
        ("cargo test", CommandRisk.SAFE),
        ("git status", CommandRisk.SAFE),
        ("git diff HEAD~1", CommandRisk.SAFE),
        ("node --version", CommandRisk.SAFE),
        ("ruff check .", CommandRisk.SAFE),
        ("eslint --fix src", CommandRisk.NORMAL),
        ("jest -u", CommandRisk.NORMAL),
        ("python app.py", CommandRisk.NORMAL),
        ("python -c print(1)", CommandRisk.NORMAL),
        ("npm install left-pad", CommandRisk.NORMAL),
        ("go run main.go", CommandRisk.NORMAL),
        ("make", CommandRisk.NORMAL),
        ("rm -rf build", CommandRisk.DESTRUCTIVE),
        ("del foo.txt", CommandRisk.DESTRUCTIVE),
        ("git reset --hard", CommandRisk.DESTRUCTIVE),
        ("git clean -fd", CommandRisk.DESTRUCTIVE),
        ("git checkout -- .", CommandRisk.DESTRUCTIVE),
        ("git branch -D feature", CommandRisk.NORMAL),
        ("npm uninstall react", CommandRisk.DESTRUCTIVE),
        ("python -m pip uninstall requests", CommandRisk.DESTRUCTIVE),
        ("git push origin main", CommandRisk.BLOCKED),
        ("sudo make install", CommandRisk.BLOCKED),
        ("npm publish", CommandRisk.BLOCKED),
        ("bash -c ls", CommandRisk.BLOCKED),
        ("powershell -Command Remove-Item x", CommandRisk.BLOCKED),
        ("mkfs.ext4 /dev/sda1", CommandRisk.BLOCKED),
    ],
)
def test_classification(command, risk):
    assert classify(split_command(command)).risk is risk


def test_risk_maps_to_permission():
    assert classify(["pytest"]).permission is PermissionLevel.READ
    assert classify(["python", "x.py"]).permission is PermissionLevel.EXECUTE
    assert classify(["rm", "x"]).permission is PermissionLevel.DESTRUCTIVE
    assert classify(["git", "push"]).permission is None


@pytest.mark.parametrize("command", ["pytest | tee out.txt", "make && make test", "ls ; rm x", "echo hi > f", "echo `id`", "a\nb", ""])
def test_shell_syntax_rejected(command):
    with pytest.raises(CommandParseError):
        split_command(command)


def test_quoted_arguments_are_kept_intact():
    assert split_command('pytest -k "a and b"') == ["pytest", "-k", "a and b"]
    assert split_command('python -c "print(\\"hi\\")"') == ["python", "-c", 'print("hi")']  # escaped quotes (live bug)
    if os.name == "nt":
        assert split_command(r'"C:\Program Files\x\tool.exe" C:\src\a.py') == [r"C:\Program Files\x\tool.exe", r"C:\src\a.py"]


# ---- process runner ----------------------------------------------------------------------------


def test_run_process_captures_output_and_exit_code(tmp_path):
    result = run_process([PY, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"], tmp_path, timeout=30)
    assert result.exit_code == 3 and "out" in result.stdout and "err" in result.stderr and not result.ok


def test_run_process_timeout_kills(tmp_path):
    started = time.perf_counter()
    result = run_process([PY, "-c", "import time; time.sleep(30)"], tmp_path, timeout=1.5)
    assert result.timed_out and result.exit_code is None
    assert time.perf_counter() - started < 20


def test_run_process_cancellation(tmp_path):
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    result = run_process([PY, "-c", "import time; time.sleep(30)"], tmp_path, timeout=60, cancel_event=cancel)
    assert result.cancelled


def test_run_process_missing_program(tmp_path):
    result = run_process(["definitely-not-a-real-program-xyz"], tmp_path, timeout=5)
    assert result.not_found and "command not found" in result.stderr


def test_provider_keys_are_scrubbed_from_child_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "secret-value")
    monkeypatch.setenv("UNRELATED_SETTING", "kept")
    code = "import os; print(os.environ.get('GEMINI_API_KEY', 'absent'), os.environ.get('UNRELATED_SETTING'), os.environ.get('CI'))"
    result = run_process([PY, "-c", code], tmp_path, timeout=30, scrub_env={"GEMINI_API_KEY"})
    assert result.stdout.split() == ["absent", "kept", "true"]
    assert "GEMINI_API_KEY" not in build_env({"gemini_api_key"}, {"GEMINI_API_KEY": "x"})  # case-insensitive
