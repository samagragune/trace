import pytest
from pydantic import SecretStr, ValidationError

from trace_agent.core.messages import Message, ToolDefinition
from trace_agent.providers.base import CompletionRequest, CompletionResponse, FinishReason, ProviderSettings
from trace_agent.providers.errors import (
    ProviderNotSupportedError,
    ProviderRateLimitError,
    ProviderResponseError,
    UnknownProviderError,
)
from trace_agent.providers.factory import ProviderFactory
from trace_agent.providers.registry import (
    BUILTIN_PROVIDERS,
    DEFAULT_PROVIDERS,
    ApiKeyRequirement,
    ProviderRegistry,
    ProviderSpec,
)
from trace_agent.providers.scripted import ScriptedProvider, text_response, tool_call_response


def _request(text="hi"):
    return CompletionRequest(messages=(Message.user(text),))


# ---- registry ---------------------------------------------------------------


def test_spec_providers_are_builtin():
    assert {"gemini", "groq", "openrouter", "ollama", "openai_compatible"} <= set(DEFAULT_PROVIDERS.ids())


def test_ollama_needs_no_key_and_cloud_providers_do():
    assert DEFAULT_PROVIDERS.get("ollama").api_key is ApiKeyRequirement.NONE
    for pid in ("gemini", "groq", "openrouter"):
        assert DEFAULT_PROVIDERS.get(pid).api_key is ApiKeyRequirement.REQUIRED


def test_unknown_provider_lists_known_ones():
    with pytest.raises(UnknownProviderError, match="groq"):
        DEFAULT_PROVIDERS.get("nope")


def test_registry_accepts_new_providers_and_rejects_duplicates():
    registry = ProviderRegistry(BUILTIN_PROVIDERS)
    spec = ProviderSpec(id="mistral", display_name="Mistral", api_key=ApiKeyRequirement.REQUIRED)
    registry.register(spec)
    assert registry.get("mistral") is spec
    with pytest.raises(ValueError):
        registry.register(spec)
    registry.register(spec, replace=True)


# ---- settings / requests ---------------------------------------------------


def test_settings_never_reveal_api_key():
    settings = ProviderSettings(profile_name="p", provider="groq", model="m", api_key=SecretStr("sk-very-secret"))
    assert "sk-very-secret" not in repr(settings)
    assert "sk-very-secret" not in str(settings.model_dump())
    assert settings.api_key.get_secret_value() == "sk-very-secret"


def test_request_rejects_duplicate_tool_names():
    tool = ToolDefinition(name="t", description="d", parameters={"type": "object"})
    with pytest.raises(ValidationError):
        CompletionRequest(messages=(Message.user("x"),), tools=(tool, tool))


def test_response_must_be_from_assistant():
    with pytest.raises(ValidationError):
        CompletionResponse(message=Message.user("x"), finish_reason=FinishReason.STOP, provider="p", model="m")


def test_rate_limit_error_is_retryable():
    assert ProviderRateLimitError("slow down", retry_after=2).retryable
    assert not ProviderResponseError("bad").retryable


# ---- factory ----------------------------------------------------------------


def test_factory_reports_missing_adapter_honestly():
    factory = ProviderFactory()
    with pytest.raises(ProviderNotSupportedError, match="no adapter"):
        factory.create(ProviderSettings(profile_name="g", provider="groq", model="m"))


def test_factory_uses_registered_adapter():
    factory = ProviderFactory()
    factory.register_adapter("groq", lambda s: ScriptedProvider([], settings=s))
    provider = factory.create(ProviderSettings(profile_name="g", provider="groq", model="llama"))
    assert provider.provider_id == "groq" and provider.model == "llama"
    assert provider.capabilities().tool_calling


def test_factory_rejects_adapter_for_unknown_provider():
    with pytest.raises(UnknownProviderError):
        ProviderFactory().register_adapter("typo", lambda s: ScriptedProvider([]))


# ---- scripted provider -------------------------------------------------------


def test_scripted_provider_replays_and_records():
    provider = ScriptedProvider([tool_call_response("read_file", {"path": "a.py"}), text_response("done")])
    first = provider.complete(_request("one"))
    assert first.tool_calls[0].name == "read_file"
    assert provider.complete(_request("two")).message.content == "done"
    assert [r.messages[0].content for r in provider.requests] == ["one", "two"]


def test_scripted_provider_can_react_to_request():
    provider = ScriptedProvider([lambda req: text_response(f"echo:{req.messages[-1].content}")])
    assert provider.complete(_request("ping")).message.content == "echo:ping"


def test_scripted_provider_raises_scripted_errors_and_exhaustion():
    provider = ScriptedProvider([ProviderRateLimitError("429")])
    with pytest.raises(ProviderRateLimitError):
        provider.complete(_request())
    with pytest.raises(ProviderResponseError, match="no responses left"):
        provider.complete(_request())
