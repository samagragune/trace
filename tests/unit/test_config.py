import pytest
from pydantic import ValidationError

from trace_agent.safety.permissions import ExecutionMode
from trace_agent.storage.config import ConfigError, ConfigNotFoundError, ProviderProfile, TraceConfig
from trace_agent.storage.config_store import ConfigStore
from trace_agent.storage.paths import TracePaths


def groq(**kw):
    return ProviderProfile(provider="groq", model="llama-3.3-70b-versatile", **kw)


# ---- profile validation -----------------------------------------------------


def test_profile_defaults_come_from_provider_spec():
    p = ProviderProfile(provider="openrouter", model="x/y")
    assert p.effective_base_url == "https://openrouter.ai/api/v1"
    assert p.effective_api_key_env == "OPENROUTER_API_KEY"


def test_profile_overrides_win():
    p = groq(base_url="https://gateway.example/v1/", api_key_env="MY_GROQ")
    assert p.effective_base_url == "https://gateway.example/v1"  # trailing slash normalized
    assert p.effective_api_key_env == "MY_GROQ"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"provider": "nope", "model": "m"},
        {"provider": "groq", "model": "   "},
        {"provider": "groq", "model": "m", "base_url": "ftp://x"},
        {"provider": "groq", "model": "m", "api_key_env": "BAD-NAME"},
        {"provider": "openai_compatible", "model": "m"},  # base_url required
        {"provider": "groq", "model": "m", "options": {"api_key": "sk-x"}},
        {"provider": "groq", "model": "m", "api_key": "sk-x"},  # unknown field: secrets can't sneak in
        {"provider": "groq", "model": "m", "timeout_seconds": 0},
    ],
)
def test_invalid_profiles_rejected(kwargs):
    with pytest.raises(ValidationError):
        ProviderProfile(**kwargs)


def test_openai_compatible_with_base_url_and_options():
    p = ProviderProfile(
        provider="openai_compatible",
        model="qwen2.5-coder",
        base_url="http://localhost:1234/v1",
        options={"temperature": 0.2, "max_tokens": 4096},
    )
    assert p.options["max_tokens"] == 4096  # "token" substring alone is not treated as a secret


# ---- config model -------------------------------------------------------------


def test_first_profile_becomes_active():
    cfg = TraceConfig().with_profile("g", groq())
    assert cfg.active_profile == "g"
    cfg2 = cfg.with_profile("local", ProviderProfile(provider="ollama", model="llama3.1:8b"))
    assert cfg2.active_profile == "g"
    assert cfg2.with_active("local").get_profile() == ("local", cfg2.profiles["local"])


def test_removing_active_profile_clears_it():
    cfg = TraceConfig().with_profile("g", groq()).without_profile("g")
    assert cfg.active_profile is None
    with pytest.raises(ConfigError, match="no provider profile is active"):
        cfg.get_profile()


def test_active_profile_must_exist():
    with pytest.raises(ValidationError):
        TraceConfig(active_profile="ghost")


def test_newer_schema_version_rejected():
    with pytest.raises(ValidationError, match="upgrade Trace"):
        TraceConfig(schema_version=99)


def test_default_execution_mode_is_confirm():
    assert TraceConfig().agent.execution_mode is ExecutionMode.CONFIRM


# ---- persistence ---------------------------------------------------------------


def test_round_trip(tmp_path):
    store = ConfigStore(tmp_path / "config.toml")
    cfg = (
        TraceConfig()
        .with_profile("g", groq(options={"temperature": 0.1}))
        .with_profile("lm", ProviderProfile(provider="openai_compatible", model="m", base_url="http://h:1/v1"))
        .with_execution_mode(ExecutionMode.PLAN)
    )
    store.save(cfg)
    assert store.load() == cfg
    assert not list(tmp_path.glob(".config-*"))  # no temp files left behind


def test_missing_and_corrupt_files(tmp_path):
    store = ConfigStore(tmp_path / "config.toml")
    with pytest.raises(ConfigNotFoundError):
        store.load()
    assert store.load_or_default() == TraceConfig()
    store.path.write_text("not = [valid", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid TOML"):
        store.load()


def test_invalid_content_gives_readable_error(tmp_path):
    store = ConfigStore(tmp_path / "config.toml")
    store.path.write_text('[profiles.x]\nprovider = "groq"\nmodel = "m"\napi_key = "sk-oops"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match=r"profiles\.x\.api_key"):
        store.load()


def test_trace_home_env_override(tmp_path):
    paths = TracePaths.from_env({"TRACE_HOME": str(tmp_path / "h")})
    assert paths.config_file == (tmp_path / "h" / "config.toml").resolve()
    paths.ensure()
    assert paths.cache_dir.is_dir() and paths.log_dir.is_dir()
