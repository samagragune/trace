import codecs
from pathlib import Path

import pytest

from trace_agent.safety.checkpoints import CheckpointStore
from trace_agent.tools.base import ToolContext, ToolStatus
from trace_agent.tools.builtin.editing import (
    ApplyPatchInput,
    ApplyPatchTool,
    CreateFileInput,
    CreateFileTool,
    DeleteFileInput,
    DeleteFileTool,
    EditFileInput,
    EditFileTool,
)
from trace_agent.safety.permissions import PermissionLevel

APP = "def total(items):\n    return sum(i['price'] for i in items)\n\n\ndef tax(x):\n    return x * 0.2\n"


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "app").mkdir(parents=True)
    (root / "app" / "calc.py").write_text(APP, encoding="utf-8", newline="\n")
    return root


@pytest.fixture
def ctx(proj, tmp_path) -> ToolContext:
    return ToolContext(project_root=proj, checkpoints=CheckpointStore(tmp_path / "cp", "task1", proj))


def edit(ctx, **kw):
    return EditFileTool().run(EditFileInput(path="app/calc.py", **kw), ctx)


# ---- edit_file -------------------------------------------------------------------------------------


def test_edit_applies_backs_up_and_returns_diff(ctx, proj):
    r = edit(ctx, expected_original="x * 0.2", replacement="x * 0.25")
    assert r.ok and "-    return x * 0.2" in r.output and "+    return x * 0.25" in r.output
    assert "x * 0.25" in (proj / "app" / "calc.py").read_text()
    assert r.data["file_changes"][0] == {**r.data["file_changes"][0], "path": "app/calc.py", "change": "modified", "added": 1, "removed": 1}
    assert ctx.checkpoints.changed_files() == [{"path": "app/calc.py", "change": "modified"}]


def test_edit_not_found_suggests_closest_match(ctx):
    r = edit(ctx, expected_original="return x * 0.3", replacement="y")
    assert r.status is ToolStatus.ERROR and "not found" in r.error and "near line 6" in r.error


def test_edit_detects_indentation_mismatch(ctx):
    r = edit(ctx, expected_original="return x * 0.2", replacement="return x")  # found (substring) -> ok
    assert r.ok
    r2 = edit(ctx, expected_original="  return sum(i['price'] for i in items)\n  ", replacement="z")
    assert "different indentation" in r2.error


def test_edit_ambiguous_match_requires_more_context(ctx, proj):
    (proj / "app" / "calc.py").write_text("a = 1\nb = 1\n", encoding="utf-8")
    r = edit(ctx, expected_original="= 1", replacement="= 2")
    assert "matches 2 places" in r.error and "lines 1, 2" in r.error
    assert edit(ctx, expected_original="= 1", replacement="= 2", replace_all=True).ok
    assert (proj / "app" / "calc.py").read_text() == "a = 2\nb = 2\n"


def test_edit_preserves_crlf_and_bom(ctx, proj):
    path = proj / "app" / "calc.py"
    path.write_bytes(codecs.BOM_UTF8 + b"x = 1\r\ny = 2\r\n")
    assert edit(ctx, expected_original="y = 2\n", replacement="y = 3\nz = 4\n").ok  # model sends \n
    assert path.read_bytes() == codecs.BOM_UTF8 + b"x = 1\r\ny = 3\r\nz = 4\r\n"


@pytest.mark.parametrize(("rel", "status"), [(".env", ToolStatus.DENIED), (".git/config", ToolStatus.DENIED), ("../x.py", ToolStatus.DENIED), ("missing.py", ToolStatus.ERROR)])
def test_edit_refusals(ctx, proj, rel, status):
    (proj / ".env").write_text("K=1")
    (proj / ".git").mkdir()
    (proj / ".git" / "config").write_text("[core]")
    r = EditFileTool().run(EditFileInput(path=rel, expected_original="K", replacement="Q"), ctx)
    assert r.status is status


def test_edit_refuses_non_utf8(ctx, proj):
    (proj / "app" / "calc.py").write_bytes("caf\xe9 = 1\n".encode("latin-1"))
    assert "not valid UTF-8" in edit(ctx, expected_original="1", replacement="2").error


def test_assess_previews_diff_and_reports_certain_failures(ctx):
    tool = EditFileTool()
    ok = tool.assess(EditFileInput(path="app/calc.py", expected_original="0.2", replacement="0.25"), ctx)
    assert ok.error is None and "+    return x * 0.25" in ok.preview
    bad = tool.assess(EditFileInput(path="app/calc.py", expected_original="nope", replacement="x"), ctx)
    assert bad.error and "not found" in bad.error


def test_protected_file_requires_approval(proj, tmp_path):
    ctx = ToolContext(project_root=proj, checkpoints=CheckpointStore(tmp_path / "cp", "t", proj), protected_paths=frozenset({"app/calc.py"}))
    a = EditFileTool().assess(EditFileInput(path="app/calc.py", expected_original="0.2", replacement="0.3"), ctx)
    assert a.requires_approval and "uncommitted changes" in a.requires_approval


def test_editing_tools_unavailable_without_checkpoints(proj):
    bare = ToolContext(project_root=proj)
    for tool in (EditFileTool(), ApplyPatchTool(), CreateFileTool(), DeleteFileTool()):
        assert not tool.is_available(bare)
    assert EditFileTool().assess(EditFileInput(path="app/calc.py", expected_original="0.2", replacement="0.3"), bare).blocked


# ---- create / delete ------------------------------------------------------------------------------


def test_create_and_delete(ctx, proj):
    r = CreateFileTool().run(CreateFileInput(path="app/new.py", content="X = 1"), ctx)
    assert r.ok and (proj / "app" / "new.py").read_text() == "X = 1\n"
    assert CreateFileTool().run(CreateFileInput(path="app/new.py", content="again"), ctx).error.endswith("use edit_file to change it")
    assert DeleteFileTool().permission is PermissionLevel.DESTRUCTIVE
    assert DeleteFileTool().run(DeleteFileInput(path="app/calc.py"), ctx).ok
    assert not (proj / "app" / "calc.py").exists()
    changes = {c["path"]: c["change"] for c in ctx.checkpoints.changed_files()}
    assert changes == {"app/new.py": "created", "app/calc.py": "deleted"}


# ---- apply_patch ------------------------------------------------------------------------------------


PATCH = """\
--- a/app/calc.py
+++ b/app/calc.py
@@ -1,2 +1,2 @@
 def total(items):
-    return sum(i['price'] for i in items)
+    return sum(i['price'] * i['qty'] for i in items)
@@ -40,2 +40,2 @@
 def tax(x):
-    return x * 0.2
+    return round(x * 0.2, 2)
--- /dev/null
+++ b/app/util.py
@@ -0,0 +1,2 @@
+def double(x):
+    return 2 * x
"""


def test_patch_multi_hunk_multi_file_with_wrong_line_numbers(ctx, proj):
    r = ApplyPatchTool().run(ApplyPatchInput(patch=PATCH), ctx)
    assert r.ok, r.error
    text = (proj / "app" / "calc.py").read_text()
    assert "i['price'] * i['qty']" in text and "round(x * 0.2, 2)" in text
    assert (proj / "app" / "util.py").read_text() == "def double(x):\n    return 2 * x\n"


def test_patch_is_all_or_nothing(ctx, proj):
    bad = PATCH.replace(" def tax(x):", " def tax(amount):")  # second hunk's context won't match
    r = ApplyPatchTool().run(ApplyPatchInput(patch=bad), ctx)
    assert r.status is ToolStatus.ERROR and "does not apply" in r.error
    assert (proj / "app" / "calc.py").read_text() == APP and not (proj / "app" / "util.py").exists()


def test_patch_deleting_a_file_is_destructive(ctx):
    patch = "--- a/app/calc.py\n+++ /dev/null\n@@ -1,6 +0,0 @@\n-x\n"
    assert ApplyPatchTool().assess(ApplyPatchInput(patch=patch), ctx).permission is PermissionLevel.DESTRUCTIVE


def test_patch_garbage_rejected(ctx):
    r = ApplyPatchTool().run(ApplyPatchInput(patch="just some text"), ctx)
    assert r.status is ToolStatus.INVALID_INPUT


# ---- checkpoints / undo ----------------------------------------------------------------------------


def test_restore_returns_original_state(ctx, proj, tmp_path):
    edit(ctx, expected_original="0.2", replacement="0.5")
    edit(ctx, expected_original="0.5", replacement="0.9")  # second edit: backup is still the original
    CreateFileTool().run(CreateFileInput(path="app/extra.py", content="E = 1"), ctx)
    store = CheckpointStore.open(tmp_path / "cp" / "task1")  # re-open from disk, as `trace undo` does
    report = store.restore()
    assert report.restored == ["app/calc.py"] and report.deleted == ["app/extra.py"]
    assert (proj / "app" / "calc.py").read_text() == APP and not (proj / "app" / "extra.py").exists()
    assert store.changed_files() == []


def test_restore_never_clobbers_later_user_edits(ctx, proj, tmp_path):
    edit(ctx, expected_original="0.2", replacement="0.5")
    (proj / "app" / "calc.py").write_text("user rewrote this\n")
    store = CheckpointStore.open(tmp_path / "cp" / "task1")
    report = store.restore()
    assert report.skipped == ["app/calc.py"] and (proj / "app" / "calc.py").read_text() == "user rewrote this\n"
    assert store.restore(force=True).restored == ["app/calc.py"]
    assert (proj / "app" / "calc.py").read_text() == APP
