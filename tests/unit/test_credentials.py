import pytest

from trace_agent.providers.errors import MissingCredentialError
from trace_agent.storage.config import ProviderProfile, TraceConfig
from trace_agent.storage.credentials import CredentialSource, InMemoryCredentialStore, resolve_credential
from trace_agent.storage.resolve import resolve_provider_settings

GROQ = ProviderProfile(provider="groq", model="llama")


def test_environment_variable_is_used():
    r = resolve_credential("g", GROQ, environ={"GROQ_API_KEY": "sk-env"})
    assert r.source is CredentialSource.ENVIRONMENT
    assert r.api_key.get_secret_value() == "sk-env"
    assert "sk-env" not in repr(r)


def test_environment_beats_store():
    store = InMemoryCredentialStore({"g": "sk-store"})
    r = resolve_credential("g", GROQ, store, environ={"GROQ_API_KEY": "sk-env"})
    assert r.api_key.get_secret_value() == "sk-env"


def test_store_is_keyed_by_profile_name():
    store = InMemoryCredentialStore({"g": "sk-store"})
    assert resolve_credential("g", GROQ, store, environ={}).source is CredentialSource.STORE
    assert resolve_credential("other", GROQ, store, environ={}).source is CredentialSource.MISSING


def test_custom_env_var_override():
    p = ProviderProfile(provider="groq", model="m", api_key_env="WORK_GROQ")
    assert resolve_credential("g", p, environ={"GROQ_API_KEY": "x"}).source is CredentialSource.MISSING
    assert resolve_credential("g", p, environ={"WORK_GROQ": "y"}).source is CredentialSource.ENVIRONMENT


def test_blank_values_are_ignored():
    assert resolve_credential("g", GROQ, environ={"GROQ_API_KEY": "  "}).source is CredentialSource.MISSING


def test_local_and_optional_providers():
    ollama = ProviderProfile(provider="ollama", model="llama3.1:8b")
    assert resolve_credential("o", ollama, environ={}).source is CredentialSource.NOT_REQUIRED
    compat = ProviderProfile(provider="openai_compatible", model="m", base_url="http://x/v1")
    assert resolve_credential("c", compat, environ={}).source is CredentialSource.ABSENT_OPTIONAL


def test_in_memory_store_operations():
    store = InMemoryCredentialStore()
    store.set("p", "s")
    assert store.get("p") == "s"
    assert store.delete("p") and not store.delete("p")
    with pytest.raises(ValueError):
        store.set("p", "")


def test_resolve_provider_settings_merges_profile_and_secret():
    cfg = TraceConfig().with_profile("g", ProviderProfile(provider="groq", model="llama", options={"temperature": 0}))
    s = resolve_provider_settings(cfg, environ={"GROQ_API_KEY": "sk-1"})
    assert (s.profile_name, s.provider, s.model) == ("g", "groq", "llama")
    assert s.api_key.get_secret_value() == "sk-1"
    assert s.options == {"temperature": 0}


def test_resolve_provider_settings_missing_key_explains_fix():
    cfg = TraceConfig().with_profile("g", GROQ)
    with pytest.raises(MissingCredentialError, match="GROQ_API_KEY"):
        resolve_provider_settings(cfg, environ={})
