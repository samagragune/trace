"""LiteLLM adapter tests. No network: completion is injected, or LiteLLM's own mock_response is used."""

from types import SimpleNamespace

import httpx
import litellm
import pytest
from pydantic import SecretStr

from trace_agent.core.messages import Message, ToolCall, ToolDefinition
from trace_agent.providers.base import CompletionRequest, FinishReason, ProviderSettings
from trace_agent.providers.errors import (
    ContextWindowExceededError,
    ModelNotFoundError,
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderUnavailableError,
)
from trace_agent.providers.factory import DEFAULT_FACTORY
from trace_agent.providers.litellm_adapter import (
    LiteLLMProvider,
    parse_response,
    parse_tool_call,
    to_openai_messages,
)

KEY = "AIzaSyTESTKEY-0123456789abcdefghijklmnopq"


def settings(**kw):
    base = dict(profile_name="g", provider="gemini", model="gemini-2.5-flash", api_key=SecretStr(KEY), max_retries=2)
    return ProviderSettings(**{**base, **kw})


def fake_response(content=None, tool_calls=None, finish="stop"):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish)], usage=usage, model="m")


def fn_call(name, args, call_id="c1", psf=None):
    return SimpleNamespace(
        id=call_id, function=SimpleNamespace(name=name, arguments=args), provider_specific_fields=psf
    )


REQ = CompletionRequest(
    messages=(Message.user("hi"),),
    tools=(ToolDefinition(name="read_file", description="d", parameters={"type": "object"}),),
)


def http_response(status, headers=None):
    return httpx.Response(status, headers=headers or {}, request=httpx.Request("POST", "https://x"))


# ---- translation ------------------------------------------------------------------------


def test_messages_round_trip_to_openai_format():
    call = ToolCall(id="c1", name="read_file", arguments={"path": "a.py"}, provider_metadata={"thought_signature": "sig"})
    out = to_openai_messages(
        (Message.system("s"), Message.user("u"), Message.assistant(tool_calls=(call,)), Message.tool_result("c1", "r"))
    )
    assert out[0] == {"role": "system", "content": "s"}
    assert out[2]["tool_calls"][0] == {
        "id": "c1",
        "type": "function",
        "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
        "provider_specific_fields": {"thought_signature": "sig"},
    }
    assert out[3] == {"role": "tool", "tool_call_id": "c1", "content": "r"}


def test_tool_call_parsing_keeps_signature_id_and_reports_bad_json():
    ok = parse_tool_call(fn_call("read_file", '{"path": "x"}', "id__thought__SIG", {"thought_signature": "SIG"}), 0)
    assert ok.id == "id__thought__SIG" and ok.arguments == {"path": "x"}
    assert ok.provider_metadata == {"thought_signature": "SIG"}
    bad = parse_tool_call(fn_call("read_file", "{not json"), 0)
    assert bad.arguments == {} and bad.arguments_error
    not_obj = parse_tool_call(fn_call("read_file", "[1,2]"), 0)
    assert "JSON object" in not_obj.arguments_error


def test_response_parsing():
    r = parse_response(fake_response(tool_calls=[fn_call("read_file", "{}")], finish="stop"), "gemini", "m")
    assert r.finish_reason is FinishReason.TOOL_CALLS  # normalized
    assert r.usage.total_tokens == 15
    empty = parse_response(fake_response(content=None, finish="length"), "gemini", "m")
    assert empty.message.content == "" and empty.finish_reason is FinishReason.LENGTH
    with pytest.raises(ProviderResponseError):
        parse_response(SimpleNamespace(choices=[]), "gemini", "m")


def test_real_litellm_response_object_parses():
    """Uses LiteLLM's built-in mock (no network) so we parse a genuine ModelResponse."""
    raw = litellm.completion(model="gemini/gemini-2.5-flash", messages=[{"role": "user", "content": "hi"}], mock_response="pong")
    r = parse_response(raw, "gemini", "gemini-2.5-flash")
    assert r.message.content == "pong"


# ---- request building --------------------------------------------------------------------


def test_request_kwargs():
    captured = {}

    def completion(**kwargs):
        captured.update(kwargs)
        return fake_response(content="ok")

    p = LiteLLMProvider(settings(options={"temperature": 0.3}, base_url="https://proxy/v1"), completion_fn=completion)
    p.complete(REQ)
    assert captured["model"] == "gemini/gemini-2.5-flash"
    assert captured["api_key"] == KEY and captured["api_base"] == "https://proxy/v1"
    assert captured["temperature"] == 0.3 and captured["num_retries"] == 0
    assert captured["tools"][0]["function"]["name"] == "read_file"


@pytest.mark.parametrize(
    ("provider", "prefix"),
    [("groq", "groq/"), ("openrouter", "openrouter/"), ("ollama", "ollama_chat/"), ("openai_compatible", "openai/")],
)
def test_routing_prefixes(provider, prefix):
    assert LiteLLMProvider(settings(provider=provider, model="m")).litellm_model == prefix + "m"


def test_default_factory_has_adapters_for_all_builtins():
    for pid in ("gemini", "groq", "openrouter", "ollama", "openai_compatible"):
        assert DEFAULT_FACTORY.has_adapter(pid)
    assert isinstance(DEFAULT_FACTORY.create(settings()), LiteLLMProvider)


# ---- errors and retries ----------------------------------------------------------------------


def raising(*excs):
    calls = iter(excs)

    def completion(**_):
        exc = next(calls)
        if isinstance(exc, Exception):
            raise exc
        return exc

    return completion


def ex_auth():
    return litellm.exceptions.AuthenticationError(f"API key {KEY} not valid", llm_provider="gemini", model="m")


def ex_rate(msg="Quota exceeded. Please retry in 7.5s."):
    return litellm.exceptions.RateLimitError(msg, llm_provider="gemini", model="m", response=http_response(429))


def test_vendor_json_errors_become_readable():
    body = (
        'litellm.RateLimitError: GeminiException - { "error": { "code": 429, "message": "You exceeded your current '
        'quota. Please retry in 41.2s.", "status": "RESOURCE_EXHAUSTED", "details": [ { "retryDelay": "41s" } ] } }'
    )
    p = LiteLLMProvider(settings(max_retries=0), completion_fn=raising(ex_rate(body)))
    with pytest.raises(ProviderRateLimitError) as info:
        p.complete(REQ)
    assert str(info.value) == (
        "rate limited: You exceeded your current quota. Please retry in 41.2s. [RESOURCE_EXHAUSTED]"
    )
    assert info.value.retry_after == 41.2


def test_auth_error_is_normalized_and_key_redacted():
    p = LiteLLMProvider(settings(), completion_fn=raising(ex_auth()))
    with pytest.raises(ProviderAuthError) as info:
        p.complete(REQ)
    assert KEY not in str(info.value)


def test_rate_limit_retries_with_server_delay_then_succeeds():
    sleeps = []
    p = LiteLLMProvider(settings(), completion_fn=raising(ex_rate(), fake_response(content="ok")), sleep=sleeps.append)
    assert p.complete(REQ).message.content == "ok"
    assert sleeps == [7.5]


def test_rate_limit_gives_up_after_max_retries():
    sleeps = []
    p = LiteLLMProvider(settings(max_retries=2), completion_fn=raising(ex_rate(), ex_rate(), ex_rate()), sleep=sleeps.append)
    with pytest.raises(ProviderRateLimitError) as info:
        p.complete(REQ)
    assert info.value.retry_after == 7.5 and len(sleeps) == 2


def test_long_server_delay_is_not_waited_out():
    sleeps = []
    p = LiteLLMProvider(settings(), completion_fn=raising(ex_rate("retry in 3600s")), sleep=sleeps.append)
    with pytest.raises(ProviderRateLimitError):
        p.complete(REQ)
    assert sleeps == []


def test_transient_errors_back_off_exponentially():
    sleeps = []
    err = lambda: litellm.exceptions.APIConnectionError("conn reset", llm_provider="gemini", model="m")  # noqa: E731
    p = LiteLLMProvider(settings(max_retries=3), completion_fn=raising(err(), err(), fake_response(content="ok")), sleep=sleeps.append)
    assert p.complete(REQ).message.content == "ok"
    assert sleeps == [1.0, 2.0]


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (lambda: litellm.exceptions.NotFoundError("no model", llm_provider="gemini", model="m"), ModelNotFoundError),
        (lambda: litellm.exceptions.ContextWindowExceededError("too long", llm_provider="gemini", model="m"), ContextWindowExceededError),
        (lambda: litellm.exceptions.ServiceUnavailableError("503", llm_provider="gemini", model="m"), ProviderUnavailableError),
        (lambda: litellm.exceptions.BadRequestError("bad", llm_provider="gemini", model="m"), ProviderResponseError),
        (lambda: RuntimeError("weird"), ProviderResponseError),
    ],
)
def test_error_mapping(exc, expected):
    p = LiteLLMProvider(settings(max_retries=0), completion_fn=raising(exc()))
    with pytest.raises(expected):
        p.complete(REQ)
