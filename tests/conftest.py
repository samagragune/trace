from __future__ import annotations

from pathlib import Path

import pytest

from trace_agent.providers.registry import BUILTIN_PROVIDERS


@pytest.fixture(autouse=True)
def isolated_trace_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Never let a test read or write the developer's real ~/.trace or real API keys."""
    home = tmp_path / "trace-home"
    monkeypatch.setenv("TRACE_HOME", str(home))
    for spec in BUILTIN_PROVIDERS:
        if spec.api_key_env:
            monkeypatch.delenv(spec.api_key_env, raising=False)
    return home


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    return root
