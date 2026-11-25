import json
import subprocess
from pathlib import Path

import pytest

from trace_agent.context.project import detect_project, git_info, render_project_info
from trace_agent.context.selection import build_task_context
from trace_agent.tools.base import ToolContext, ToolStatus
from trace_agent.tools.builtin.context_tools import (
    GetProjectInfoTool,
    InspectLogsInput,
    InspectLogsTool,
    ParseStackTraceInput,
    ParseStackTraceTool,
    ProjectInfoInput,
)


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def py_project(tmp_path: Path) -> Path:
    root = tmp_path / "pyapp"
    write(root, "pyproject.toml", '[project]\nname="x"\ndependencies=["fastapi>=0.100","SQLAlchemy"]\n'
          '[project.optional-dependencies]\ndev=["pytest>=8"]\n')
    write(root, "uv.lock", "")
    write(root, "app/main.py", "def handler(data):\n    return data['email']\n")
    write(root, "tests/test_main.py", "from app.main import handler\n")
    write(root, "node_modules/junk/index.js", "x")
    return root


@pytest.fixture
def js_project(tmp_path: Path) -> Path:
    root = tmp_path / "jsapp"
    write(root, "package.json", json.dumps({
        "dependencies": {"react": "18", "next": "14"},
        "devDependencies": {"vitest": "1", "typescript": "5"},
        "scripts": {"test": "vitest run"},
    }))
    write(root, "pnpm-lock.yaml", "")
    write(root, "src/app.tsx", "export {}")
    write(root, "src/app.test.tsx", "test('x', () => {})")
    return root


# ---- project detection --------------------------------------------------------------------


def test_python_project_detection(py_project):
    info = detect_project(py_project)
    assert info.primary_language == "Python"
    assert info.package_managers == ["uv"]
    assert info.frameworks == ["FastAPI", "SQLAlchemy"]
    assert info.test_frameworks == ["pytest"]
    assert info.test_command == "python -m pytest"
    assert info.test_dirs == ["tests"] and info.test_file_count == 1
    assert "node_modules/" not in info.top_level  # excluded dirs never shown
    assert all(s.name != "JavaScript" for s in info.languages)  # node_modules not scanned


def test_js_project_detection(js_project):
    info = detect_project(js_project)
    assert info.primary_language == "TypeScript"
    assert info.package_managers == ["pnpm"]
    assert info.frameworks == ["React", "Next.js", "TypeScript"]
    assert info.test_frameworks == ["Vitest"]
    assert info.test_command == "pnpm test"


@pytest.mark.parametrize(
    ("files", "command"),
    [
        ({"go.mod": "module x", "main.go": "package main"}, "go test ./..."),
        ({"Cargo.toml": "[package]", "src/main.rs": "fn main(){}"}, "cargo test"),
        ({"package.json": json.dumps({"scripts": {"test": 'echo "Error: no test specified" && exit 1'}})}, None),
        ({"README.md": "hi"}, None),
    ],
)
def test_test_command_detection(tmp_path, files, command):
    for rel, text in files.items():
        write(tmp_path, rel, text)
    assert detect_project(tmp_path).test_command == command


def test_git_info(tmp_path):
    assert git_info(tmp_path) is None
    try:
        subprocess.run(["git", "init", "-q", "-b", "work", str(tmp_path)], check=True, capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("git not available")
    write(tmp_path, "new.py", "x")
    info = git_info(tmp_path)
    assert info.branch == "work" and info.untracked == 1 and info.dirty
    assert "Git: branch work" in render_project_info(detect_project(tmp_path))


# ---- context selection ------------------------------------------------------------------------


def test_context_from_traceback_includes_snippet_and_message_hits(py_project):
    description = (
        "Crash on submit:\nTraceback (most recent call last):\n"
        '  File "/ci/work/pyapp/app/main.py", line 2, in handler\n'
        "    return data['email']\nKeyError: 'email'\n"
    )
    ctx = build_task_context(description, py_project)
    text = ctx.render()
    assert "## Project overview" in text and "FastAPI" in text
    assert "app/main.py:2 in handler  <-- innermost project frame" in text
    assert "> 2 |     return data['email']" in text  # snippet marks the failing line
    assert ctx.summary()["project_frames"] == ["app/main.py:2 in handler"]


def test_context_greps_quoted_error_text(py_project):
    write(py_project, "app/errors.py", 'raise ValueError("order total cannot be negative")\n')
    ctx = build_task_context("Checkout fails with 'ValueError: order total cannot be negative'", py_project)
    assert [h.location for h in ctx.message_hits] == ["app/errors.py:1"]
    assert "Where quoted error text appears" in ctx.render()


def test_context_without_evidence_is_just_the_overview(py_project):
    ctx = build_task_context("the page is slow", py_project)
    assert ctx.traces == [] and ctx.snippets == [] and ctx.message_hits == []
    assert len(ctx.render()) < 1500  # bounded: no repository dump


def test_context_never_reads_sensitive_files(py_project):
    write(py_project, ".env", "API_KEY=order total cannot be negative")
    ctx = build_task_context("error 'order total cannot be negative'", py_project)
    assert all(".env" not in h.location for h in ctx.message_hits)


# ---- tools ----------------------------------------------------------------------------------------


def test_get_project_info_tool(py_project):
    r = GetProjectInfoTool().run(ProjectInfoInput(), ToolContext(project_root=py_project))
    assert r.ok and "FastAPI" in r.output and r.data["test_command"] == "python -m pytest"
    denied = GetProjectInfoTool().run(ProjectInfoInput(directory=".."), ToolContext(project_root=py_project))
    assert denied.status is ToolStatus.DENIED


def test_parse_stack_trace_tool(py_project):
    text = 'Traceback (most recent call last):\n  File "app/main.py", line 2, in handler\nKeyError: \'email\'\n'
    r = ParseStackTraceTool().run(ParseStackTraceInput(text=text), ToolContext(project_root=py_project))
    assert r.data == {"traces": 1, "project_frames": 1}
    assert "KeyError: 'email'" in r.output and "> 2 |     return data['email']" in r.output
    none = ParseStackTraceTool().run(ParseStackTraceInput(text="all good"), ToolContext(project_root=py_project))
    assert none.output.startswith("no stack traces")


def test_inspect_logs_discovery_and_reading(tmp_path):
    lines = [f"INFO request {i} ok" for i in range(100)]
    lines[50] = "ERROR payment failed: card declined"
    lines[51] = "  at charge (billing.js:9:3)"
    write(tmp_path, "logs/app.log", "\n".join(lines))
    write(tmp_path, "server.out", "started\n")
    ctx = ToolContext(project_root=tmp_path)

    found = InspectLogsTool().run(InspectLogsInput(), ctx)
    assert found.data["count"] == 2 and "logs/app.log" in found.output

    r = InspectLogsTool().run(InspectLogsInput(path="logs/app.log", context_lines=1, tail_lines=2), ctx)
    assert r.data["matches"] == 1
    assert "> 51: ERROR payment failed: card declined" in r.output
    assert "  50: INFO request 49 ok" in r.output  # context line
    assert "--- last 2 lines ---" in r.output and "100: INFO request 99 ok" in r.output


def test_inspect_logs_refusals(tmp_path):
    write(tmp_path, ".env", "SECRET=1")
    ctx = ToolContext(project_root=tmp_path)
    assert InspectLogsTool().run(InspectLogsInput(path=".env"), ctx).status is ToolStatus.DENIED
    assert InspectLogsTool().run(InspectLogsInput(path="../x.log"), ctx).status is ToolStatus.DENIED
    write(tmp_path, "a.log", "x")
    assert InspectLogsTool().run(InspectLogsInput(path="a.log", pattern="("), ctx).status is ToolStatus.INVALID_INPUT
