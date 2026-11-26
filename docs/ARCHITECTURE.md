# Architecture

**Version:** 0.1.0 · **Implemented through:** Build 003 (Phase 3) · **Last updated:** 2026-11-25

Legend: ✅ implemented · 🟡 partially implemented · 🔜 planned (phase noted) · ⚠️ known gap

This document describes the system **as built**. Design rationale is in [DECISIONS.md](DECISIONS.md)
(ADR-nnn references below), and the chronological record with test results is in
[BUILD_LOG.md](BUILD_LOG.md).

---

## 1. The one-paragraph version

The **model** reasons and proposes actions. The **orchestrator** runs the loop and owns control,
never strategy. Before the first model call, the **context engine** collects a small,
evidence-driven briefing (project overview, parsed stack traces, failing code). Every tool call the
model proposes goes through the **ToolExecutor**: validated against a schema, authorized by the
**safety layer** (policy plus, when needed, the user), executed, size-bounded and **redacted**. Only
then is the actual result fed back to the model. The **task state** records every action through
explicit stages, tracks hypotheses, and refuses to call a task "fixed" without a verified
modification. Everything the orchestrator touches is an interface, so providers, tools, sinks and
storage can change without changing the loop.

---

## 2. Roadmap status

| Phase | Name | Status | Build | Commit |
|---|---|---|---|---|
| 0 | Architecture and project foundation | ✅ complete | 001 | `18c4240` |
| 1 | CLI and configuration | 🟡 partial: adapter, `config test` and CLI done; keyring, `trace init` and set-key deferred (ADR-014) | 001, 002 | `18c4240` |
| 2 | Agent brain and recursive loop | ✅ complete (read-only tools, ADR-013) | 002 | `18c4240` |
| 3 | Project understanding and investigation | ✅ complete | 003 | `af0f4aa` |
| 4 | Code editing and execution | 🔜 next | — | — |
| 5 | Benchmark infrastructure | 🔜 | — | — |
| 6 | Hardening and public release | 🔜 | — | — |

Builds 001 and 002 were committed together as the repository's initial commit. Remote:
https://github.com/samagragune/trace (branch `main`).

---

## 3. Build-by-build architecture

### Build 001: Phase 0, foundation (2026-11-24)

**Purpose:** an installable package with tested interfaces and fully configurable providers, but
no model connectivity yet.

| Module | Added |
|---|---|
| packaging | `pyproject.toml` (hatchling), distribution `trace-agent`, import package `trace_agent` (ADR-001), CLI entry point `trace` |
| `errors` | `TraceError` root: deliberate errors are distinguishable from bugs |
| `core.messages` | `Role`, `Message` (shape-validated), `ToolCall` (proposal, never authorization), `ToolDefinition` |
| `providers` | `ModelProvider` ABC (sync, ADR-004), normalized `ProviderError` hierarchy with a `retryable` flag, `ProviderRegistry` with 5 specs (ADR-003), `ProviderFactory` that raises an honest error when no adapter exists, `ScriptedProvider` test double (ADR-008) |
| `storage` | `TracePaths` (`TRACE_HOME` override), `TraceConfig`/`ProviderProfile` (secret-rejecting, ADR-019), atomic TOML `ConfigStore`, `CredentialStore` protocol + env-first resolution (ADR-020), `resolve_provider_settings` (the only place a profile meets a secret, ADR-021) |
| `safety` | `PermissionLevel` × `ExecutionMode` → `decide()` policy table, `resolve_in_project` (symlink/`..`/drive-safe), sensitive-file patterns |
| `tools` | `Tool` contract (declared permission + `ToolInput` schema + `ToolResult`), `ToolContext` with a cancel event, `ToolRegistry.parse_call` |
| `agent.state` | `AgentTask` lifecycle, `ActionRecord` stage machine (ADR-007), verified-fix invariant (ADR-006) |
| `observability` | `ExecutionEvent`, `EventSink` protocol, `InMemoryEventSink` |
| `cli` | `--version`, `status`, `config providers/show/set-profile/use/remove-profile/set-mode` |

**Verified:** 122 tests; wheel builds and installs into a clean virtualenv; stdlib `trace` and
`trace_agent` both import. **Found by running it:** the provider table truncated ids at 80 columns
(switched to a list), and raw Pydantic errors leaked to users (now formatted).

### Build 002: Phase 2 agent loop + the Phase 1 pieces it needs (2026-11-24/25)

**Purpose:** a real agent. It receives a task, chooses tools, executes them under authorization,
observes actual results and decides what to do next, on a real provider.

| Module | Added |
|---|---|
| `providers.litellm_adapter` | the only LiteLLM importer (lazy; pinned 1.110.3, ADR-009): OpenAI-format translation, routing prefixes, error normalization with vendor JSON message extraction, Trace-owned retry (server delay or 1/2/4s backoff, never waits more than 60s), key redaction, preserved Gemini 3 thought signatures (ADR-022), `warm_up()` |
| `agent.orchestrator` | the Observe-Reason-Act-Verify loop; `finish_task` with unverified-claim rejection (ADR-010); iteration limit; Ctrl-C handling |
| `agent.executor` | `ToolExecutor`: lookup → validate → `decide` → `Approver` → run → bound output; every failure becomes an honest `ToolResult` |
| `agent.observation` | result rendering (status/error/truncation/output), `ProgressMonitor` (repeat warning at 3 and stop at 4; idle nudge ×2, then stop; ADR-023) |
| `agent.prompts` | default system prompt, loadable replacement via `[agent] system_prompt_file` (ADR-024) |
| `agent.state` | `StopReason.DIAGNOSED` (ADR-013), usage counters, structured `report` |
| `tools.builtin` | `list_files`, `read_file`, `search_code` (read-only, boundary-checked, sensitive-file-aware) |
| `safety.redaction` | `Redactor`: known secrets + key-format patterns |
| `observability.sinks` | `CompositeSink` (failure-isolating, ADR-026), `RedactingSink` |
| `storage.history` | SQLite `tasks` + `events` log, retention pruning, short-id lookup (ADR-011) |
| `cli` | interactive `trace`, `run`, `history [show]`, `config test`, live progress view, result panel, `TerminalApprover` (ADR-012), `session.py` composition root (ADR-025), output-stream hardening |

**Verified:** 184 tests; Phase 2 acceptance test (the next action is computed from the previous
real tool output); live Gemini: OK, RATE LIMITED (`limit: 0`, reported in 15s instead of a
3.4-hour hang), AUTH FAILED, MODEL NOT FOUND; correct live multi-file diagnosis in 9 steps / 16.8k
tokens; key absent from `history.db` and `config.toml`. **Found live:** a cp1252 console
`UnicodeEncodeError` inside the terminal sink killed the whole run (fixed at 3 levels, ADR-026);
the built-in Gemini model suggestions were retired models (ADR-027).

### Build 003: Phase 3, project understanding and investigation (2026-11-25)

**Purpose:** start every task oriented, give the agent better investigation tools, and make its
reasoning inspectable.

| Module | Added |
|---|---|
| `context.project` | `detect_project`: languages, package managers, frameworks, test framework, likely test command, test dirs, git status. Data tables; 5,000-file bounded scan |
| `context.stacktrace` | parsers for Python (incl. chained + pytest short format), JS/Node, Java/Kotlin (+ "Caused by"), Go, Rust, Ruby; `locate_frames` maps local, CI/container (suffix match) and Windows paths through the safety layer; library frames are marked |
| `context.selection` | `build_task_context`: bounded opening briefing (ADR-015) |
| `context.ignore` | dependency-free ignore list (breaks a would-be import cycle, ADR-018) |
| `tools.builtin.context_tools` | `get_project_info`, `parse_stack_trace`, `inspect_logs`; `search_code` gains `context_lines` |
| `agent.builtins` | `record_hypothesis` (ADR-016) + `finish_task` moved here; `BUILTIN_TOOL_NAMES` reserved |
| `agent.state` | `Hypothesis` model with status-transition history |
| `agent.executor` | tool output redacted before it reaches the model (ADR-017) |
| `core.messages` | `compact_json_schema`: tool schemas sent without `title` noise (ADR-028) |
| `agent.prompts` | guidance to batch independent calls and avoid bookkeeping-only turns (ADR-028) |

**Verified:** 217 tests; 5 live Gemini runs, all correct diagnoses, including a real pasted pytest
traceback with Windows paths (the model never needed to open the crashing file). **Measured
honestly:** the first cut regressed to 30.9k tokens; after fixes, 12.1k–26.0k (high variance from
the model's batching behaviour). There is no reliable improvement on a 5-file fixture yet; the
Phase 5 benchmark is needed to settle it.

---

## 4. Package layout (current)

```
src/trace_agent/
  __init__.py, __main__.py, errors.py
  core/           messages.py          Message, ToolCall, ToolDefinition, compact_json_schema
  providers/      base.py              ModelProvider, CompletionRequest/Response, ProviderSettings
                  errors.py            normalized ProviderError hierarchy
                  registry.py          ProviderSpec, ProviderRegistry, DEFAULT_PROVIDERS (5 built-ins)
                  factory.py           ProviderFactory, DEFAULT_FACTORY (LiteLLM for all built-ins)
                  litellm_adapter.py   the only module importing litellm
                  scripted.py          ScriptedProvider (deterministic test double)
  storage/        paths.py             TracePaths (~/.trace or $TRACE_HOME)
                  config.py            TraceConfig, ProviderProfile, AgentSettings, StorageSettings
                  config_store.py      atomic TOML load/save
                  credentials.py       CredentialStore protocol, env-first resolution
                  resolve.py           profile + credential -> ProviderSettings
                  history.py           SQLite HistoryStore (EventSink + task records)
  safety/         permissions.py       PermissionLevel, ExecutionMode, Decision, decide()
                  paths.py             resolve_in_project, is_sensitive_path
                  redaction.py         Redactor
  tools/          base.py              Tool, ToolInput, ToolResult, ToolContext
                  registry.py          ToolRegistry, parse_call
                  builtin/filesystem.py      list_files, read_file, search_code
                  builtin/context_tools.py   get_project_info, parse_stack_trace, inspect_logs
  context/        ignore.py, project.py, stacktrace.py, selection.py
  agent/          state.py             AgentTask, ActionRecord, Hypothesis, VerificationResult
                  builtins.py          finish_task, record_hypothesis
                  orchestrator.py      the loop
                  executor.py          ToolExecutor, Approver protocol
                  observation.py       rendering, ProgressMonitor
                  prompts.py           default brain, task message
  observability/  events.py, sinks.py
  cli/            app.py (commands), session.py (composition root), render.py (terminal UI)
  benchmark/      🔜 Phase 5 (empty on purpose)
```

Size (lines of Python): agent 1,038 · providers 900 · context 860 · tools 727 · storage 704 ·
cli 671 · safety 234 · core 143 · observability 111.

---

## 5. Dependency direction

Derived from the actual `import` statements (Build 003). Arrows mean "imports". **The graph is
acyclic**, `core` and `errors` import nothing from Trace, and only `cli` knows about everything.

```
                     ┌──────────────────────────── cli ───────────────────────────┐
                     ▼                    ▼                ▼                     ▼
                  storage ───────────► agent ──────────► tools ──────────► context
                  │  │  │               │ │ │              │                  │
                  │  │  └─► observability◄┘ │ └─► providers│                  │
                  │  │          │           │       │      │                  │
                  │  └──────────┼───────────┼───────┼──────┼──────► safety ◄──┘
                  │             └───────────┼───────┼──────┘           ▲
                  └─► providers             │       └──────────────────┘
                                            └─► core ◄── providers, tools
```

| Package | Imports | Why |
|---|---|---|
| `core` | — | shared vocabulary only |
| `safety` | errors | policy, paths and redaction are leaf utilities |
| `providers` | core, safety, errors | `safety.redaction` keeps keys out of error messages |
| `context` | safety | every path lookup is boundary-checked |
| `tools` | core, context, safety, errors | context tools; the shared ignore list |
| `observability` | safety | `RedactingSink` |
| `agent` | core, context (lazy), observability, providers, safety, tools | the loop composes them |
| `storage` | agent, observability, providers, safety, errors | `history` persists `AgentTask`; `resolve` builds `ProviderSettings` |
| `cli` | everything | composition root (ADR-025) |

Rules this enforces:
- `providers` never imports `storage`; persisted config becomes `ProviderSettings` in `storage.resolve` (ADR-021).
- `agent` never imports `storage` or `cli`; the loop is testable with no files or terminal.
- `context` imports nothing from `tools`; the shared ignore list lives in `context.ignore` (ADR-018).

---

## 6. Runtime: what `trace run "<problem>"` does

```
cli/app.py run ─► cli/session.run_task()                        (composition root)
  │ load config.toml ─► resolve_provider_settings ─► DEFAULT_FACTORY ─► LiteLLMProvider
  │ Redactor(+ live API key)
  │ sink = RedactingSink( CompositeSink[ ConsoleEventPrinter, HistoryStore ] )
  ▼
Orchestrator(provider, default_registry(), sink, TerminalApprover | DenyAllApprover, prompt, redactor)
```

Mapped to the spec's 10-step loop:

| # | Spec step | Implementation |
|---|---|---|
| 1 | Receive | `AgentTask(description, project_root).start()`; TASK_STARTED event |
| 2 | Observe | `build_task_context`: overview + parsed traces + failing-frame snippets + error-text hits, redacted and appended to the task message; CONTEXT_COLLECTED event |
| 3 | Reason | `provider.complete(messages, tools = registry tools + record_hypothesis + finish_task)` |
| 4 | Decide | no tool call → nudge (max 2) then NO_PROGRESS; `record_hypothesis` → task state; `finish_task` → checked report; other calls → step 5 |
| 5 | Authorize | `ToolExecutor`: `parse_call` (schema) → `decide(mode, permission)` → `Approver` on ASK |
| 6 | Act | `tool.run(args, ToolContext)` (crashes become ERROR results) |
| 7 | Observe result | redact → truncate (head + tail) → `render_tool_result` |
| 8 | Update | action stage advanced; tool message appended; TOOL_* events |
| 9 | Evaluate | repeat detector (warn at 3, stop at 4); `finish_task` accepted only if valid, alone, and (for "fixed") verified |
| 10 | Repeat / finish | until a stop condition; TASK_FINISHED; `HistoryStore.save_task`; `render_final_report` |

Every stop condition in the spec maps to a `StopReason` (section 8).

---

## 7. Trust boundaries and secret handling

**Untrusted inputs:** model output (tool calls, arguments, claims), project files and logs (may
contain prompt injection or secrets), stack-trace paths, and `config.toml` contents.

| Boundary | Control | Since |
|---|---|---|
| Model → action | `ToolCall` is only a proposal; schema validation (`extra="forbid"`), then the `decide()` policy, then a user prompt on ASK | B001/B002 |
| Model → completion | `finish_task` "fixed" rejected without a verified modification; `AgentTask.finish` enforces it again | B001/B002 |
| Path → filesystem | `resolve_in_project` (resolves symlinks and `..` before checking), used by every tool and by trace mapping | B001/B003 |
| Project → model | sensitive files never read; tool output and the opening context pass through redaction | B002/B003 |
| Anything → disk / terminal | `RedactingSink` on every event (live key + patterns) | B002 |
| Provider errors → user | adapter redacts the key from vendor messages | B002 |
| `config.toml` | unknown fields and credential-like `options` keys rejected; no keys ever written | B001 |
| Observers → loop | `CompositeSink` isolates sink failures; the CLI replaces unencodable characters | B002 |

**Credential resolution order** (ADR-020): profile `api_key_env` → provider's conventional env var
→ `CredentialStore` (the keyring is 🔜). Keys exist in memory only as `SecretStr`.

---

## 8. State model

```
TaskStatus: PENDING ─start()─► RUNNING ─finish(reason)─► COMPLETED | STOPPED | FAILED | CANCELLED
                                   ▲                    └► AWAITING_APPROVAL ─resume()─┘

ActionRecord: REQUESTED ─► AUTHORIZED ─► EXECUTED | FAILED
                       └─► DENIED       └► FAILED (unknown tool / invalid input)

Hypothesis:   proposed ⇄ supported ⇄ rejected   (each transition stored, e.g. "proposed->rejected@4")
```

| StopReason | TaskStatus | Trigger |
|---|---|---|
| VERIFIED_FIX | COMPLETED | `finish_task(fixed)` **and** `is_fix_verified()` |
| DIAGNOSED | COMPLETED | `finish_task(diagnosed)`, labelled "Diagnosis only" |
| NO_FIX_NEEDED | COMPLETED | `finish_task(no_fix_needed)` |
| INSUFFICIENT_INFORMATION | STOPPED | `finish_task(insufficient_information)` |
| ITERATION_LIMIT | STOPPED | `max_iterations` reached |
| NO_PROGRESS | STOPPED | repeated identical call ×4, or 3 text-only replies |
| PROVIDER_UNAVAILABLE | FAILED | `ProviderError` after adapter retries |
| SAFETY_VIOLATION | FAILED | reserved (no producer yet) |
| USER_INTERRUPTED | CANCELLED | Ctrl-C |
| APPROVAL_PENDING | AWAITING_APPROVAL | reserved (approvals block in the terminal, ADR-012) |

`is_fix_verified()`: at least one successful WRITE/DESTRUCTIVE action, and the latest verification
recorded after the last such action passed. No producer of verifications exists until Phase 4.

---

## 9. Configuration and persistence

### `~/.trace/config.toml` (or `$TRACE_HOME/config.toml`)

```toml
schema_version = 1                 # newer than supported -> refused
active_profile = "gem"

[profiles.gem]                     # any number of named profiles (ADR-003)
provider = "gemini"                # gemini | groq | openrouter | ollama | openai_compatible
model = "gemini-3.5-flash-lite"
base_url = "..."                   # optional; required for openai_compatible
api_key_env = "MY_KEY_VAR"         # optional override of the provider's env var
timeout_seconds = 60.0             # (0, 600]
max_retries = 3                    # used by the adapter for retryable errors
options = { temperature = 0.2 }    # passed through; credential-like keys rejected

[agent]
execution_mode = "confirm"         # plan | confirm | auto
max_iterations = 30
command_timeout_seconds = 60.0     # passed to ToolContext; enforced by Phase 4 subprocess tools
max_output_chars = 20000           # tool-output truncation (head + tail)
system_prompt_file = "brain.md"    # optional custom brain (ADR-024)
max_retry_attempts = 3             # ⚠️ accepted but not used: provider retries come from the profile's max_retries

[storage]
history_retention_days = 90        # pruned at the start of each run; omit for "forever"
```

### Files

| Path | Content | Status |
|---|---|---|
| `config.toml` | non-secret configuration | ✅ |
| `history.db` | `tasks` (outcome, usage, report) + `events` (ordered, redacted trace) | ✅ |
| `cache/`, `logs/` | created by `TracePaths.ensure()` | ⚠️ unused |
| credentials | environment variables | ✅ · OS keyring 🔜 |

---

## 10. Key interfaces

| Interface | Module | Contract |
|---|---|---|
| `ModelProvider` | providers.base | `complete()`, `check_connection()`, `capabilities()`, `warm_up()`; raises only `ProviderError` subclasses |
| `ProviderSpec` / `ProviderRegistry` | providers.registry | provider-type metadata; library-agnostic |
| `ProviderFactory` | providers.factory | provider id → adapter; `ProviderNotSupportedError` if none |
| `TraceConfig` / `ProviderProfile` | storage.config | validated, secret-free persisted configuration |
| `CredentialStore` | storage.credentials | `get` / `set` / `delete`, keyed by profile name |
| `HistoryStore` | storage.history | `EventSink` + `save_task`, `list_tasks`, `find_task`, `events`, `prune` |
| `Tool` / `ToolInput` / `ToolResult` | tools.base | declared permission + strict schema + truthful structured result |
| `ToolRegistry.parse_call` | tools.registry | untrusted `ToolCall` → `ParsedToolCall` or `ToolInputError` |
| `ToolExecutor.execute` | agent.executor | the only path from a model's request to a real action |
| `Approver` | agent.executor | `approve(ApprovalRequest) -> bool` (terminal, deny-all, allow-all) |
| `Orchestrator.run` | agent.orchestrator | `(description, project_root) -> AgentTask` |
| `decide(mode, level)` | safety.permissions | ALLOW / ASK / DENY |
| `resolve_in_project` | safety.paths | path inside the project, or raises |
| `Redactor` | safety.redaction | `text()` / `value()` (recursive) |
| `detect_project` | context.project | `Path -> ProjectInfo` |
| `parse_stack_traces` / `locate_frames` | context.stacktrace | text → `StackTrace[]` → frames mapped to the project |
| `build_task_context` | context.selection | `(description, root) -> TaskContext` (bounded) |
| `EventSink` | observability.events | `emit(ExecutionEvent)` |

Extension points that need no orchestrator change: a new provider type (`ProviderSpec`), a new
connection library (adapter factory), a new secret backend (`CredentialStore`), a new tool
(`Tool` subclass in the registry), a new sink (`EventSink`), a new brain (`system_prompt_file`).

---

## 11. Testing architecture

217 tests (216 pass, 1 skipped on Windows without Developer Mode: the symlink-escape test).

| Area | Tests | Approach |
|---|---|---|
| safety | 26 | policy table exhaustiveness; traversal, absolute, drive and symlink escapes |
| orchestrator | 22 | `ScriptedProvider` with callable steps reacting to real tool output (Phase 2 acceptance test); authorization × mode matrix; hypotheses; redaction |
| litellm adapter | 21 | injected completion function + LiteLLM's own `mock_response`; error mapping and retry timing via an injected `sleep` (no network) |
| config, credentials, providers | 43 | validation, persistence, secret rejection, resolution order |
| context, stack traces | 26 | 6 languages, CI and Windows paths, bounded selection, sensitive files |
| tools, builtin tools | 26 | schema, truncation, boundaries, binary/sensitive refusals |
| task state, messages, CLI, history | 52 | state machine, verified-fix invariant, CLI via `CliRunner`, SQLite round trip |
| integration | 1 | config → profile → credential → factory → provider |

Isolation: an autouse fixture points `TRACE_HOME` at a temp dir and clears provider key variables,
so tests can't touch real configuration or keys. Live provider checks are run manually and
recorded in BUILD_LOG (not in CI: they need a key and cost quota).

---

## 12. Known gaps and architectural risks

| Item | Impact | Plan |
|---|---|---|
| No context-window management | long investigations will eventually fail with a context error | compaction / summarization of old tool output (before Phase 6) |
| No write or exec tools | Trace can diagnose but not fix or verify | Phase 4 |
| Verification producer missing | `VERIFIED_FIX` is unreachable in practice | Phase 4 `run_tests` → `add_verification` |
| In-process tools have no timeout | bounded only by size limits | Phase 4 subprocess tools enforce `command_timeout_seconds` |
| ⚠️ `[agent] max_retry_attempts` unused | a config field that does nothing | remove, or wire it to tool retries in Phase 4 |
| ⚠️ `cache/`, `logs/` unused | empty directories | use them or stop creating them |
| History row written only at task end | a hard crash leaves events but no task row | write the row at start, update at end |
| Message-level Gemini thought signatures not preserved | possible degradation on text-only Gemini 3 turns | carry `provider_specific_fields` on `Message` |
| Efficiency variance (4–8 steps for one task) | unpredictable cost | Phase 5 benchmark to measure; prompt/brain tuning |
| Keyring, `trace init`, set-key deferred | keys only via environment variables | finish Phase 1 (ADR-014) |
| Lexical (not semantic) context selection | may miss relevant code in large repos | revisit with benchmark evidence (ADR-015) |
