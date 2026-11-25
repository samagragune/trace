# Model providers

## Three separate concepts

| Concept | What it is | Where |
|---|---|---|
| **Provider spec** | metadata about a provider *type*: key requirement, default endpoint, key env var, suggested models | `providers/registry.py` |
| **Profile** | a user's named, persisted choice: provider type + model + overrides | `[profiles.<name>]` in `config.toml` |
| **Adapter** | code that actually talks to the API, implementing `ModelProvider` | registered in `ProviderFactory` (Phase 1) |

They are kept apart so you can have several profiles per provider (for example, two OpenRouter
models), switch models without code changes, and swap the HTTP library without touching
configuration.

## Built-in provider types ✅

| id | API key | Key env var | Default endpoint |
|---|---|---|---|
| `gemini` | required | `GEMINI_API_KEY` | (library default) |
| `groq` | required | `GROQ_API_KEY` | (library default) |
| `openrouter` | required | `OPENROUTER_API_KEY` | `https://openrouter.ai/api/v1` |
| `ollama` | none | n/a | `http://localhost:11434` |
| `openai_compatible` | optional | set per profile with `api_key_env` | **required** (`base_url`) |

`openai_compatible` is an addition to the original spec (ADR-003). It lets Trace use LM Studio,
vLLM, llama.cpp server, LiteLLM proxy or a corporate gateway without code changes.

Suggested model names are hints only. Any model string is accepted; whether it exists and supports
tool calling is checked by the Phase 1 connection test.

## Profile fields

```toml
[profiles.work]
provider = "openai_compatible"     # registry id (validated)
model = "qwen2.5-coder-32b"        # required, free-form
base_url = "https://llm.corp/v1"   # optional override; required for openai_compatible
api_key_env = "CORP_LLM_KEY"       # optional: read the key from this env var
timeout_seconds = 60               # 0 < t <= 600
max_retries = 3                    # 0..10, for retryable errors only
options = { temperature = 0.1 }    # passed through to the adapter
```

Rejected at load time: unknown fields (so `api_key = "..."` cannot be stored), credential-like keys
in `options` (`api_key`, `token`, `secret`, `password`, ...), non-http(s) URLs, blank models,
unknown providers, and a `schema_version` newer than this Trace supports.

## Credential resolution ✅

For a profile named `P`:
1. the environment variable `api_key_env` if set, otherwise the provider's convention;
2. the `CredentialStore` entry for `P` (OS keyring in Phase 1).

Possible results: `ENVIRONMENT`, `STORE`, `NOT_REQUIRED`, `ABSENT_OPTIONAL`, `MISSING`.
`resolve_provider_settings` raises `MissingCredentialError` (naming the variable to set) for `MISSING`.
Keys only exist as `pydantic.SecretStr` and are masked in `repr`, `str` and `model_dump`.

## Error normalization ✅

Adapters must translate every vendor exception into:

| Error | retryable | Typical cause |
|---|---|---|
| `ProviderAuthError` / `MissingCredentialError` | no | bad or absent key |
| `ProviderRateLimitError` (`retry_after`) | yes | HTTP 429 |
| `ProviderTimeoutError` | yes | |
| `ProviderUnavailableError` | yes | network failure, 5xx |
| `ContextWindowExceededError` | no | prompt too large |
| `ProviderResponseError` | no | malformed or empty response |
| `UnknownProviderError` / `ProviderNotSupportedError` | no | config typo, or no adapter installed |

Spec rule: Trace never silently switches providers on failure.

## Adding a provider

```python
from trace_agent.providers import DEFAULT_PROVIDERS, DEFAULT_FACTORY, ProviderSpec, ApiKeyRequirement

DEFAULT_PROVIDERS.register(ProviderSpec(id="mistral", display_name="Mistral",
                                        api_key=ApiKeyRequirement.REQUIRED, api_key_env="MISTRAL_API_KEY"))
DEFAULT_FACTORY.register_adapter("mistral", MyMistralAdapter)   # any ModelProvider subclass
```

## LiteLLM adapter ✅ (Build 002)

`providers/litellm_adapter.py` is the only module that imports `litellm` (pinned `==1.110.3`).
`DEFAULT_FACTORY` registers it for all five built-in providers with these routing prefixes:
`gemini/`, `groq/`, `openrouter/`, `ollama_chat/` and `openai/` (the last for `openai_compatible`).

- **Retries:** LiteLLM's own retries are disabled (`num_retries=0`). Trace retries retryable errors
  up to the profile's `max_retries`, using the server's retry delay when one is given (from the
  `Retry-After` header or a Gemini "retry in Ns" message), otherwise 1s, 2s, 4s... capped at 30s.
  A wait longer than 60s (for example, an exhausted daily free-tier quota) is reported immediately
  rather than slept through.
- **Errors:** every LiteLLM or vendor exception becomes a Trace `ProviderError`. The vendor's JSON
  message is extracted (for example, `You exceeded your current quota... [RESOURCE_EXHAUSTED]`)
  and the API key is redacted from it.
- **Gemini 3 thought signatures:** opaque `provider_specific_fields` and the signature-carrying
  tool-call ids are kept on `ToolCall.provider_metadata` and sent back unchanged.
- **Local-first:** `LITELLM_LOCAL_MODEL_COST_MAP=True` stops LiteLLM from downloading its price
  list at import time, and LiteLLM telemetry is disabled.

Check a profile with `trace config test [profile]`. It makes one tiny real request and reports one
of: OK (and whether the model supports tool calling), RATE LIMITED (with the retry delay),
AUTHENTICATION FAILED, MODEL NOT FOUND, or another failure.

Live-verified: a request with an invalid key reached Google and came back as
`AUTHENTICATION FAILED: API key not valid. [INVALID_ARGUMENT, API_KEY_INVALID]`.
**Not yet verified with a valid key** (see BUILD_LOG).
