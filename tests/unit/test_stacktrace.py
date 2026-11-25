from pathlib import Path

import pytest

from trace_agent.context.stacktrace import locate_file, locate_frames, parse_stack_traces, render_trace

PYTHON_TB = """\
Traceback (most recent call last):
  File "/home/runner/work/shop/shop/app/main.py", line 10, in <module>
    run()
  File "/home/runner/work/shop/shop/app/checkout.py", line 7, in checkout
    raise ValueError("order total cannot be negative")
  File "/usr/lib/python3.12/site-packages/requests/api.py", line 59, in request
    return session.request(method=method, url=url, **kwargs)
ValueError: order total cannot be negative
"""

CHAINED = """\
Traceback (most recent call last):
  File "app/db.py", line 3, in load
    return rows[0]
IndexError: list index out of range

During handling of the above exception, another exception occurred:

Traceback (most recent call last):
  File "app/main.py", line 9, in main
    load()
KeyError: 'user'
"""

PYTEST_SHORT = """\
tests/test_checkout.py:6: in test_gold
    assert checkout(cart)["total"] == 80.0
app/checkout.py:7: in checkout
    raise ValueError("order total cannot be negative")
E   ValueError: order total cannot be negative
"""

NODE = """\
TypeError: Cannot read properties of undefined (reading 'email')
    at validate (/srv/app/src/forms.js:14:22)
    at handler (/srv/app/src/routes.js:30:5)
    at Layer.handle [as handle_request] (/srv/app/node_modules/express/lib/router/layer.js:95:5)
"""

JAVA = """\
Exception in thread "main" java.lang.IllegalStateException: cart is empty
\tat com.shop.Checkout.total(Checkout.java:42)
\tat com.shop.Main.main(Main.java:10)
Caused by: java.lang.NullPointerException: items
\tat com.shop.Cart.items(Cart.java:7)
"""

GO = """\
panic: runtime error: index out of range [3] with length 3

goroutine 1 [running]:
main.lookup(...)
\t/build/app/lookup.go:12 +0x1d
main.main()
\t/build/app/main.go:5 +0x25
exit status 2
"""

RUST = "thread 'main' panicked at src/parser.rs:88:14:\ncalled `Option::unwrap()` on a `None` value\n"

RUBY = """\
app/models/user.rb:12:in `full_name': undefined method `upcase' for nil:NilClass (NoMethodError)
\tfrom app/controllers/users_controller.rb:5:in `show'
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "shop"
    for rel in ("app/main.py", "app/checkout.py", "app/db.py", "tests/test_checkout.py", "src/forms.js",
                "src/routes.js", "com/shop/Checkout.java", "com/shop/Main.java", "lookup.go", "main.go",
                "src/parser.rs", "app/models/user.rb", "app/controllers/users_controller.rb"):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(f"line {i}" for i in range(1, 60)))
    return root


def test_python_traceback_with_ci_paths_and_library_frames(project):
    [trace] = parse_stack_traces(PYTHON_TB)
    assert (trace.language, trace.exception_type, trace.message) == ("python", "ValueError", "order total cannot be negative")
    located = locate_frames(trace, project)
    assert [f.project_path for f in located.frames] == ["app/main.py", "app/checkout.py", None]
    assert located.frames[2].is_library
    assert located.innermost_project_frame.label() == "app/checkout.py:7 in checkout"


def test_chained_python_exceptions():
    traces = parse_stack_traces(CHAINED)
    assert [(t.exception_type, t.message) for t in traces] == [
        ("IndexError", "list index out of range"),
        ("KeyError", "'user'"),
    ]


def test_pytest_short_format(project):
    [trace] = parse_stack_traces(PYTEST_SHORT)
    assert trace.exception_type == "ValueError" and trace.message == "order total cannot be negative"
    located = locate_frames(trace, project)
    assert [f.project_path for f in located.frames] == ["tests/test_checkout.py", "app/checkout.py"]


def test_node_trace_is_reordered_outermost_first(project):
    [trace] = parse_stack_traces(NODE)
    assert trace.exception_type == "TypeError"
    located = locate_frames(trace, project)
    assert [f.project_path for f in located.frames] == [None, "src/routes.js", "src/forms.js"]
    assert located.frames[0].is_library
    assert located.innermost_project_frame.function == "validate"


def test_java_with_caused_by(project):
    traces = parse_stack_traces(JAVA)
    assert [t.exception_type for t in traces] == ["java.lang.IllegalStateException", "java.lang.NullPointerException"]
    located = locate_frames(traces[0], project)
    assert located.innermost_project_frame.project_path == "com/shop/Checkout.java"
    assert located.innermost_project_frame.line == 42


def test_go_panic(project):
    [trace] = parse_stack_traces(GO)
    assert trace.message.startswith("runtime error: index out of range")
    located = locate_frames(trace, project)
    assert [f.project_path for f in located.frames] == ["main.go", "lookup.go"]
    assert located.frames[-1].function == "main.lookup"


def test_rust_panic(project):
    [trace] = parse_stack_traces(RUST)
    assert trace.message == "called `Option::unwrap()` on a `None` value"
    assert locate_frames(trace, project).frames[0].project_path == "src/parser.rs"


def test_ruby(project):
    [trace] = parse_stack_traces(RUBY)
    assert trace.exception_type == "NoMethodError"
    located = locate_frames(trace, project)
    assert located.innermost_project_frame.project_path == "app/models/user.rb"


def test_no_trace_in_plain_text():
    assert parse_stack_traces("the button does nothing when I click it") == []


def test_locate_never_escapes_project(project, tmp_path):
    (tmp_path / "secret.py").write_text("x")
    assert locate_file(project, str(tmp_path / "secret.py")) is None
    assert locate_file(project, "../secret.py") is None
    assert locate_file(project, "C:\\agent\\_work\\1\\s\\app\\db.py") == "app/db.py"


def test_render_marks_innermost(project):
    text = render_trace(locate_frames(parse_stack_traces(PYTHON_TB)[0], project))
    assert "app/checkout.py:7 in checkout  <-- innermost project frame" in text
    assert "[library]" in text
