from pathlib import Path

import pytest

from trace_agent.tools.base import ToolContext, ToolStatus
from trace_agent.tools.builtin import ListFilesTool, ReadFileTool, SearchCodeTool, default_registry
from trace_agent.tools.builtin.filesystem import ListFilesInput, ReadFileInput, SearchCodeInput


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "app").mkdir(parents=True)
    (root / "app" / "forms.py").write_text("def register(data):\n    email = data['email']\n    return email.lower()\n")
    (root / "app" / "views.py").write_text("from app.forms import register\n\ndef submit(req):\n    return register(req)\n")
    (root / "node_modules" / "x").mkdir(parents=True)
    (root / "node_modules" / "x" / "index.js").write_text("register()")
    (root / ".env").write_text("SECRET=register-me")
    (root / "logo.png").write_bytes(b"\x89PNG\x00\x00binary")
    return root


@pytest.fixture
def ctx(repo):
    return ToolContext(project_root=repo)


def test_default_registry_is_read_only():
    reg = default_registry()
    assert reg.names() == [
        "get_project_info", "list_files", "read_file", "search_code", "parse_stack_trace", "inspect_logs"
    ]
    assert {reg.get(n).permission.value for n in reg.names()} == {"read"}


def test_list_files_top_level_skips_noise(ctx):
    r = ListFilesTool().run(ListFilesInput(), ctx)
    assert r.ok
    assert "app/" in r.output and "node_modules" not in r.output


def test_list_files_recursive(ctx):
    r = ListFilesTool().run(ListFilesInput(directory="app", recursive=True), ctx)
    assert r.output.splitlines() == ["app/forms.py", "app/views.py"]


def test_list_files_outside_project_denied(ctx):
    r = ListFilesTool().run(ListFilesInput(directory=".."), ctx)
    assert r.status is ToolStatus.DENIED


def test_read_file_with_line_numbers_and_range(ctx):
    r = ReadFileTool().run(ReadFileInput(path="app/forms.py", start_line=2, end_line=2), ctx)
    assert r.output == "2 |     email = data['email']"
    assert r.data["total_lines"] == 3


@pytest.mark.parametrize(
    ("path", "status"),
    [(".env", ToolStatus.DENIED), ("../x.py", ToolStatus.DENIED), ("missing.py", ToolStatus.ERROR), ("logo.png", ToolStatus.ERROR)],
)
def test_read_file_refusals(ctx, path, status):
    r = ReadFileTool().run(ReadFileInput(path=path), ctx)
    assert r.status is status and r.error


def test_read_file_bad_range(ctx):
    assert ReadFileTool().run(ReadFileInput(path="app/forms.py", start_line=50), ctx).status is ToolStatus.ERROR


def test_search_finds_matches_and_skips_secrets_and_deps(ctx):
    r = SearchCodeTool().run(SearchCodeInput(query="register"), ctx)
    assert "app/forms.py:1:" in r.output and "app/views.py:1:" in r.output
    assert ".env" not in r.output and "node_modules" not in r.output
    assert r.data["files"] == 2


def test_search_with_context_lines(ctx):
    r = SearchCodeTool().run(SearchCodeInput(query="email = ", context_lines=1), ctx)
    assert r.output.splitlines() == [
        "app/forms.py-1- def register(data):",
        "app/forms.py:2:     email = data['email']",
        "app/forms.py-3-     return email.lower()",
    ]


def test_search_regex_and_pattern(ctx):
    r = SearchCodeTool().run(SearchCodeInput(query=r"data\[.email.\]", regex=True, file_pattern="*.py"), ctx)
    assert r.data["match_count"] == 1
    assert SearchCodeTool().run(SearchCodeInput(query="(", regex=True), ctx).status is ToolStatus.INVALID_INPUT
    assert SearchCodeTool().run(SearchCodeInput(query="zzz_nothing"), ctx).output == "no matches"
