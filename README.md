# Trace

> Describe what's broken. Trace investigates, fixes your code, and verifies the solution.

Trace is an open-source debugging agent that runs on your machine. It uses the model provider you
choose with your own key (BYOK). There is no Trace account and no Trace server.

**Status: v0.1.0, Phase 4 (code editing and execution).** Trace investigates a bug, edits your
code, runs your tests, and only reports "fixed" after **it has re-run your tests itself** and they
pass. Every file it changes is backed up and can be restored with `trace undo`. Details and test
results: [docs/BUILD_LOG.md](docs/BUILD_LOG.md).

## Quick start

```bash
pip install -e ".[dev]"            # from a clone; not yet on PyPI

export GEMINI_API_KEY=...          # or GROQ_API_KEY / OPENROUTER_API_KEY; Ollama needs none
trace config set-profile gem --provider gemini --model gemini-3.5-flash-lite
trace config test                  # one tiny real request: OK / RATE LIMITED / AUTH FAILED / MODEL NOT FOUND

cd my-project
trace run "Checkout fails for gold customers with 'ValueError: order total cannot be negative'"
trace                              # interactive session in the current directory
```

What a run looks like:

```
Context: Python | tests: python -m pytest | error text found at shop/checkout.py:7
Step 1  -> run_tests()                          tests failed: 1 failed in 0.12s
        -> read_file(shop/checkout.py) ...
Step 6  -> edit_file(shop/discounts.py)         modified shop/discounts.py +3/-3
Step 7  -> run_tests()                          tests passed: 1 passed
Step 8  Trace is verifying independently: python -m pytest      tests passed
Status: completed (verified_fix) ... Undo all changes: trace undo df16d42a
```

## Execution modes

| Mode | Reads, searches, tests | Edits files | Other commands | Destructive actions |
|---|---|---|---|---|
| `plan` | yes | **never** | asks | **never** |
| `confirm` (default) | yes | asks, showing the diff | asks | asks |
| `auto` | yes | yes | yes | asks |

Set the default with `trace config set-mode <mode>`, or per run with `trace run --mode auto ...`.
When nobody can answer an approval (CI, piped input), the request is denied.

## Safety

- **Backups and undo:** before Trace first changes a file, the original is saved.
  `trace undo [task]` restores everything. A file you edited *after* Trace changed it is skipped
  unless you pass `--force`.
- **Your uncommitted work is protected:** files with uncommitted git changes when a task starts
  always need your explicit approval, even in `auto` mode.
- **Denials are binding:** if you deny a change, no command may change that file either. Trace
  checks the project after every command, reverts any change to a protected file, and stops a task
  that keeps trying.
- **Commands run without a shell**, so pipes and `&&` don't work and can't be used for injection.
  Known test and lint commands run freely. `rm`, `git reset --hard` and similar always ask.
  `sudo`, `git push`, `npm publish` and similar are blocked.
- **"Fixed" means verified:** Trace accepts "fixed" only after a successful change followed by a
  passing test run, and it runs the project's test command itself before accepting.
- **Keys stay private:** keys are never written to config or history, are redacted from all
  output, and are removed from the environment of every command the agent runs. Secrets found in
  your project are redacted before anything is sent to the model.

## Commands

```bash
trace run "<problem>" [-C DIR] [--mode plan|confirm|auto] [--test-command CMD] [--profile P] [-v]
trace                               # interactive session
trace undo [TASK_ID] [--force]      # restore files changed by a task
trace history                       # past tasks; `trace history show <id>` for the full trace
trace status                        # active profile and whether its key is found
trace config providers | show | set-profile | use | remove-profile | set-mode | test
```

## Providers

| id | Key | Notes |
|---|---|---|
| `gemini` | required | Google AI Studio. Live-tested: `gemini-3.5-flash-lite`, `gemini-3-flash-preview` |
| `groq` | required | |
| `openrouter` | required | |
| `ollama` | none | local models, no cloud |
| `openai_compatible` | optional | any OpenAI-style endpoint (LM Studio, vLLM, llama.cpp, gateways); needs `--base-url` |

Keys are read from environment variables (`GEMINI_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`,
or any variable you name with `--api-key-env`); OS-keyring storage is planned. Configuration lives
in `~/.trace/config.toml` (override with `TRACE_HOME`). A custom agent prompt can be plugged in
with `[agent] system_prompt_file`.

## Development

```bash
python -m venv .venv
.venv/Scripts/activate      # Windows; use .venv/bin/activate elsewhere
pip install -e ".[dev]"
pytest                      # 317 tests; most use real files and real subprocesses (no network)
```

## Documentation

[Architecture](docs/ARCHITECTURE.md) · [Agent loop](docs/AGENT_LOOP.md) · [Tools](docs/TOOLS.md) ·
[Providers](docs/PROVIDERS.md) · [Security](docs/SECURITY.md) · [Benchmarks](docs/BENCHMARKS.md) ·
[Decisions](docs/DECISIONS.md) · [Build log](docs/BUILD_LOG.md)

## License

MIT
