import tomllib

from typer.testing import CliRunner

from trace_agent import __version__
from trace_agent.cli.app import app
from trace_agent.storage.paths import TracePaths

runner = CliRunner()


def _config():
    with TracePaths.from_env().config_file.open("rb") as fh:
        return tomllib.load(fh)


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0 and __version__ in result.output


def test_status_when_unconfigured():
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "Not configured" in result.output


def test_bare_trace_without_config_shows_setup_help():
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert "Not configured" in result.output


def test_bare_trace_with_config_opens_session_and_exits_on_empty_input():
    runner.invoke(app, ["config", "set-profile", "g", "-p", "gemini", "-m", "gemini-2.5-flash"])
    result = runner.invoke(app, [], input="\n")
    assert result.exit_code == 0
    assert "Describe what's broken" in result.output


def _scripted(monkeypatch, steps):
    from trace_agent.cli import session
    from trace_agent.providers.scripted import ScriptedProvider

    provider = ScriptedProvider(steps)
    monkeypatch.setattr(session, "build_provider", lambda *a, **k: provider)
    return provider


def test_run_executes_agent_and_records_history(monkeypatch, tmp_path):
    from trace_agent.agent.orchestrator import FINISH_TOOL_NAME
    from trace_agent.providers.scripted import tool_call_response

    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / "main.py").write_text("print(1/0)\n")
    runner.invoke(app, ["config", "set-profile", "g", "-p", "gemini", "-m", "gemini-2.5-flash"])
    _scripted(
        monkeypatch,
        [
            tool_call_response("read_file", {"path": "main.py"}),
            tool_call_response(
                FINISH_TOOL_NAME,
                {"outcome": "diagnosed", "summary": "Division by zero.", "root_cause": "1/0 on line 1",
                 "recommended_fix": "Guard the divisor."},
            ),
        ],
    )
    result = runner.invoke(app, ["run", "it crashes", "-C", str(tmp_path / "proj")])
    assert result.exit_code == 0, result.output
    assert "read_file" in result.output and "Division by zero." in result.output
    assert "Diagnosis only" in result.output

    history = runner.invoke(app, ["history"])
    assert "it crashes" in history.output and "diagnosed" in history.output
    task_id = history.output.split()[0]
    shown = runner.invoke(app, ["history", "show", task_id])
    assert shown.exit_code == 0 and "tool_executed" in shown.output


def test_run_exit_code_when_not_resolved(monkeypatch, tmp_path):
    from trace_agent.providers.errors import ProviderRateLimitError

    runner.invoke(app, ["config", "set-profile", "g", "-p", "gemini", "-m", "gemini-2.5-flash"])
    _scripted(monkeypatch, [ProviderRateLimitError("quota")])
    result = runner.invoke(app, ["run", "x", "-C", str(tmp_path)])
    assert result.exit_code == 2
    assert "provider_unavailable" in result.output


def test_config_test_reports_rate_limit(monkeypatch):
    from trace_agent.providers.errors import ProviderRateLimitError

    runner.invoke(app, ["config", "set-profile", "g", "-p", "gemini", "-m", "gemini-2.5-flash"])
    provider = _scripted(monkeypatch, [])
    monkeypatch.setattr(
        provider, "check_connection", lambda: (_ for _ in ()).throw(ProviderRateLimitError("429", retry_after=30))
    )
    result = runner.invoke(app, ["config", "test"])
    assert result.exit_code == 1 and "RATE LIMITED" in result.output and "30s" in result.output


def test_config_test_success(monkeypatch):
    runner.invoke(app, ["config", "set-profile", "g", "-p", "gemini", "-m", "gemini-2.5-flash"])
    _scripted(monkeypatch, [])
    result = runner.invoke(app, ["config", "test"])
    assert result.exit_code == 0 and "OK" in result.output


def test_providers_lists_all_builtins():
    result = runner.invoke(app, ["config", "providers"])
    assert result.exit_code == 0
    for pid in ("gemini", "groq", "openrouter", "ollama", "openai_compatible"):
        assert pid in result.output


def test_set_profile_creates_and_activates():
    result = runner.invoke(app, ["config", "set-profile", "fast", "-p", "groq", "-m", "llama-3.3-70b-versatile"])
    assert result.exit_code == 0, result.output
    cfg = _config()
    assert cfg["active_profile"] == "fast"
    assert cfg["profiles"]["fast"] == {
        "provider": "groq",
        "model": "llama-3.3-70b-versatile",
        "timeout_seconds": 60.0,
        "max_retries": 3,
        "options": {},
    }
    assert "missing" in result.output  # no GROQ_API_KEY in the test environment


def test_status_reports_env_key_without_printing_it():
    runner.invoke(app, ["config", "set-profile", "fast", "-p", "groq", "-m", "llama"])
    result = runner.invoke(app, ["status"], env={"GROQ_API_KEY": "sk-super-secret-value"})
    assert result.exit_code == 0
    assert "GROQ_API_KEY" in result.output
    assert "sk-super-secret-value" not in result.output


def test_custom_endpoint_with_options():
    result = runner.invoke(
        app,
        [
            "config", "set-profile", "lm", "-p", "openai_compatible", "-m", "qwen2.5-coder",
            "--base-url", "http://localhost:1234/v1", "-o", "temperature=0.2", "-o", "stop=[\"END\"]",
            "-o", "label=local box",
        ],
    )
    assert result.exit_code == 0, result.output
    profile = _config()["profiles"]["lm"]
    assert profile["base_url"] == "http://localhost:1234/v1"
    assert profile["options"] == {"temperature": 0.2, "stop": ["END"], "label": "local box"}


def test_validation_errors_exit_nonzero_and_write_nothing():
    result = runner.invoke(app, ["config", "set-profile", "x", "-p", "openai_compatible", "-m", "m"])
    assert result.exit_code == 1
    assert "base_url" in result.output
    assert not TracePaths.from_env().config_file.exists()
    result = runner.invoke(app, ["config", "set-profile", "x", "-p", "groq", "-m", "m", "-o", "api_key=sk"])
    assert result.exit_code == 1
    assert "must not contain credentials" in result.output
    assert "errors.pydantic.dev" not in result.output  # readable, not raw pydantic output


def test_use_remove_and_mode():
    runner.invoke(app, ["config", "set-profile", "a", "-p", "groq", "-m", "m"])
    runner.invoke(app, ["config", "set-profile", "b", "-p", "ollama", "-m", "llama3.1:8b", "--no-activate"])
    assert _config()["active_profile"] == "a"
    assert runner.invoke(app, ["config", "use", "b"]).exit_code == 0
    assert _config()["active_profile"] == "b"
    assert runner.invoke(app, ["config", "use", "ghost"]).exit_code == 1
    assert runner.invoke(app, ["config", "remove-profile", "b"]).exit_code == 0
    assert "active_profile" not in _config()
    assert runner.invoke(app, ["config", "set-mode", "auto"]).exit_code == 0
    assert _config()["agent"]["execution_mode"] == "auto"
    assert runner.invoke(app, ["config", "set-mode", "yolo"]).exit_code != 0


def test_corrupt_config_reported_cleanly():
    paths = TracePaths.from_env()
    paths.ensure()
    paths.config_file.write_text("broken = [", encoding="utf-8")
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 1
    assert "not valid TOML" in result.output
