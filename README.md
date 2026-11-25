# Trace

> Describe what's broken. Trace investigates, fixes your code, and verifies the solution.

Trace is an open-source, locally executing debugging agent. It runs on your machine and uses the
model provider you choose with your own key (BYOK). There is no Trace account and no Trace server.

**Status: v0.1.0, Phase 2 (agent loop).** Trace can investigate a bug with read-only tools and
report a diagnosis (root cause, evidence, recommended fix). It cannot edit files yet (Phase 4).
See [docs/BUILD_LOG.md](docs/BUILD_LOG.md).

```bash
export GEMINI_API_KEY=...          # or GROQ_API_KEY, OPENROUTER_API_KEY; Ollama needs none
trace config set-profile gem --provider gemini --model gemini-3.5-flash-lite
trace config test                  # one tiny real request: OK / RATE LIMITED / AUTH FAILED ...
cd my-project
trace run "Submitting the registration form crashes with a KeyError"
trace                              # interactive session
trace history                      # past investigations; `trace history show <id>` for the trace
```

## What works today

```bash
pip install -e ".[dev]"            # from a clone; not yet on PyPI

trace --version
trace config providers             # list provider types
trace config set-profile groq --provider groq --model llama-3.3-70b-versatile
trace config set-profile local --provider ollama --model qwen2.5-coder:7b --no-activate
trace config set-profile lmstudio --provider openai_compatible \
      --model qwen2.5-coder-14b --base-url http://localhost:1234/v1 -o temperature=0.2
trace config use local             # switch active profile
trace config set-mode plan         # plan | confirm | auto
trace status                       # active profile + whether its API key is found
```

API keys are read from environment variables (`GEMINI_API_KEY`, `GROQ_API_KEY`,
`OPENROUTER_API_KEY`, or any variable you name with `--api-key-env`). OS-keyring storage and the
setup wizard arrive in Phase 1. Keys are never written to `config.toml`; the config loader rejects them.

## Supported providers

| id | Key | Notes |
|---|---|---|
| `gemini` | required | Google AI Studio |
| `groq` | required | |
| `openrouter` | required | |
| `ollama` | none | local models, no cloud |
| `openai_compatible` | optional | any OpenAI-style endpoint (LM Studio, vLLM, llama.cpp, gateways); needs `--base-url` |

Configuration is stored in `~/.trace/config.toml` (override with `TRACE_HOME`).

## Development

```bash
python -m venv .venv
.venv/Scripts/activate      # Windows; use .venv/bin/activate elsewhere
pip install -e ".[dev]"
pytest
```

## Documentation

[Architecture](docs/ARCHITECTURE.md) · [Agent loop](docs/AGENT_LOOP.md) · [Tools](docs/TOOLS.md) ·
[Providers](docs/PROVIDERS.md) · [Security](docs/SECURITY.md) · [Benchmarks](docs/BENCHMARKS.md) ·
[Decisions](docs/DECISIONS.md) · [Build log](docs/BUILD_LOG.md)

## License

MIT
