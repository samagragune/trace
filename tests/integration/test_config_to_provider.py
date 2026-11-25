"""End-to-end path that Phase 1 will exercise for real:
config.toml -> profile -> credential -> ProviderSettings -> factory -> ModelProvider.
"""

import pytest

from trace_agent.core.messages import Message
from trace_agent.providers.base import CompletionRequest
from trace_agent.providers.factory import ProviderFactory
from trace_agent.providers.scripted import ScriptedProvider, text_response
from trace_agent.storage.config import ProviderProfile, TraceConfig
from trace_agent.storage.config_store import ConfigStore
from trace_agent.storage.credentials import InMemoryCredentialStore
from trace_agent.storage.resolve import resolve_provider_settings

pytestmark = pytest.mark.integration


def test_switching_profiles_switches_provider(tmp_path):
    store = ConfigStore(tmp_path / "config.toml")
    store.save(
        TraceConfig()
        .with_profile("cloud", ProviderProfile(provider="openrouter", model="qwen/qwen3-coder"))
        .with_profile("local", ProviderProfile(provider="ollama", model="llama3.1:8b"))
    )

    factory = ProviderFactory()
    for pid in ("openrouter", "ollama"):
        factory.register_adapter(pid, lambda s: ScriptedProvider([text_response(f"{s.provider}:{s.model}")], s))

    creds = InMemoryCredentialStore({"cloud": "sk-or-123"})
    config = store.load()
    request = CompletionRequest(messages=(Message.user("hello"),))

    cloud = factory.create(resolve_provider_settings(config, creds, environ={}))
    assert cloud.settings.base_url == "https://openrouter.ai/api/v1"
    assert cloud.settings.api_key.get_secret_value() == "sk-or-123"
    assert cloud.complete(request).message.content == "openrouter:qwen/qwen3-coder"

    store.save(config.with_active("local"))
    local = factory.create(resolve_provider_settings(store.load(), creds, environ={}))
    assert local.settings.api_key is None
    assert local.settings.base_url == "http://localhost:11434"
    assert local.complete(request).message.content == "ollama:llama3.1:8b"
