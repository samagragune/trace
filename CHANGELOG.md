# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow SemVer.

## [Unreleased]

### Added (Phase 2, Build 002)
- LiteLLM adapter (`litellm==1.110.3`) for gemini, groq, openrouter, ollama and openai_compatible,
  with normalized errors, retry/backoff and key redaction. `trace config test`.
- Agent loop: orchestrator, tool executor with authorization and terminal approvals, observation
  handling, no-progress detection, and the `finish_task` tool with verification enforcement.
- Read-only tools: `list_files`, `read_file`, `search_code`.
- SQLite history (`~/.trace/history.db`), `trace history`, `trace history show`.
- `trace run`, the interactive `trace` session, and `[agent] system_prompt_file` for a custom brain.
- Credential redaction for all recorded and displayed events.

### Added (Phase 0, Build 001)
- Installable `trace-agent` package (import name `trace_agent`) with the `trace` CLI entry point.
- Provider-neutral message, tool-call and completion models.
- `ModelProvider` interface, normalized provider errors, provider registry (Gemini, Groq,
  OpenRouter, Ollama, OpenAI-compatible) and adapter factory.
- `ScriptedProvider`, a deterministic offline provider for tests.
- Profile-based provider configuration in `~/.trace/config.toml`, with atomic saves and validation
  that rejects secrets.
- Credential resolution (environment variables, then a pluggable credential store).
- Tool contract (`Tool`, `ToolInput`, `ToolResult`, `ToolContext`) and `ToolRegistry`.
- Safety: permission levels, execution modes, authorization policy, project-boundary
  path resolution, sensitive-file detection.
- Task state with enforced action stages and a verified-fix completion invariant.
- Execution events with an in-memory sink.
- CLI: `--version`, `status`, `config providers|show|set-profile|use|remove-profile|set-mode`.
- Architecture documentation and ADRs 001 to 008.
