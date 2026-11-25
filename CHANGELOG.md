# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow SemVer.

## [Unreleased]

### Added (Phase 3, Build 003)
- Project detection (languages, package managers, frameworks, test command, git status).
- Stack-trace parsing for Python, JS/Node, Java/Kotlin, Go, Rust and Ruby, with frame-to-project
  mapping (including CI paths).
- Bounded opening context: project overview, parsed traces, failing-frame snippets, error-text locations.
- Tools: `get_project_info`, `parse_stack_trace`, `inspect_logs`; `search_code --context_lines`.
- `record_hypothesis` built-in; hypotheses in task state, terminal output, history and the report.
- Redaction of credentials in tool output before it is sent to the model.

### Changed
- Tool schemas are sent without `title` annotations (fewer tokens per call).
- Gemini model suggestions updated (the `gemini-2.5-*` models are retired for new users).

### Fixed
- A terminal encoding error (cp1252) could abort an agent run; observer failures are now isolated.
- Hypothesis statuses were hidden by Rich markup in the final report.

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
