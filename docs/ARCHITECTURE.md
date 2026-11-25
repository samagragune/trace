# Architecture

Status legend: ✅ implemented (Phase 0) · 🔜 planned (phase noted)

## The one-paragraph version

The **model** reasons and proposes actions. The **orchestrator** runs the loop. **Tools** touch
the machine, but only after the **safety layer** authorizes each call. The **observation**
step feeds *actual* results back to the model. The **task state** records every action through
explicit stages and refuses to call a task "fixed" without independent verification. Everything
the orchestrator touches is an interface, so providers, tools and storage can change without
touching the loop.

## Package layout

```
src/trace_agent/
  core/           ✅ provider-neutral types: Message, ToolCall, ToolDefinition
  providers/      ✅ ModelProvider interface, errors, registry, factory, ScriptedProvider
                  ✅ LiteLLM adapter (Build 002)
  storage/        ✅ TracePaths, TraceConfig/ProviderProfile, ConfigStore, credentials, resolve
                  ✅ SQLite history (Build 002) · 🔜 keyring store
  safety/         ✅ PermissionLevel, ExecutionMode, decide(), resolve_in_project, sensitive files
                  ✅ redaction (Build 002) · 🔜 command risk classifier, checkpoints (Phase 4)
  tools/          ✅ Tool contract, ToolResult, ToolContext, ToolRegistry
                  ✅ 6 read-only tools (Builds 002-003) · 🔜 write/exec tools (Phase 4)
  agent/          ✅ AgentTask, orchestrator loop, executor, observation, prompts (Build 002)
                  ✅ builtins: finish_task, record_hypothesis; hypotheses in task state (Build 003)
  observability/  ✅ ExecutionEvent, EventSink, InMemoryEventSink, CompositeSink, RedactingSink
  context/        ✅ project detection, stack-trace parsing, opening-context selection (Build 003)
  benchmark/      🔜 Phase 5 (empty on purpose)
  cli/            ✅ trace | run | status | history | config ... ; session.py = composition root
```

## Runtime wiring for `trace run` (Build 002)

```
cli/app.py ─► cli/session.run_task()            (composition root: the only module that knows everything)
                 │ build_provider(config) ─► resolve_provider_settings ─► DEFAULT_FACTORY ─► LiteLLMProvider
                 │ sink = RedactingSink(Composite[ConsoleEventPrinter, HistoryStore])
                 ▼
            Orchestrator(provider, default_registry(), sink, TerminalApprover | DenyAllApprover, prompt)
                 │ loop: provider.complete ─► ToolExecutor (parse ─► decide ─► approve ─► run ─► truncate)
                 ▼
            AgentTask ─► HistoryStore.save_task ─► render_final_report
```

The import package is `trace_agent`, not `trace`, because `trace` is a standard-library module
(ADR-001). The command is still `trace`.

## Dependency direction

Arrows mean "imports". There are no cycles, and `core` imports nothing from Trace.

```
cli ──► storage ──► providers ──► core
 │         │            ▲
 │         └──► safety  │
 ▼                ▲     │
agent ──► tools ──┘     │
  │                     │
  └──► providers ───────┘   (Phase 2)
observability ──► (nothing)
```

`providers` never imports `storage`. Persisted configuration is converted to runtime
`ProviderSettings` in `storage/resolve.py`, the single place where a non-secret profile and a
secret credential meet.

## Provider configuration flow ✅

```
config.toml ─► ConfigStore.load() ─► TraceConfig
                                       │ get_profile(active)
                                       ▼
                     ProviderProfile ─► resolve_credential() ◄─ env var / CredentialStore
                                       │
                                       ▼
                     resolve_provider_settings() ─► ProviderSettings (SecretStr key)
                                       │
                                       ▼
                     ProviderFactory.create() ─► ModelProvider   (adapter registered per provider id)
```

Each layer is replaceable on its own:
- **New provider type**: register a `ProviderSpec`. Config, CLI and status pick it up.
- **New connection library**: register a different adapter factory. Nothing else changes.
- **New secret backend**: implement the 3-method `CredentialStore` protocol.

## Key interfaces

| Interface | Module | Contract |
|---|---|---|
| `ModelProvider` | providers.base | `complete(CompletionRequest) -> CompletionResponse`, `check_connection()`, `capabilities()`; raises only `ProviderError` subclasses |
| `ProviderSpec` / `ProviderRegistry` | providers.registry | metadata about a provider type; library-agnostic |
| `ProviderFactory` | providers.factory | provider id → adapter; honest error if no adapter |
| `TraceConfig` / `ProviderProfile` | storage.config | validated, secret-free persisted config |
| `CredentialStore` | storage.credentials | `get/set/delete` keyed by profile name |
| `Tool` / `ToolInput` / `ToolResult` | tools.base | declared permission + validated input + structured, truthful result |
| `ToolRegistry.parse_call` | tools.registry | untrusted `ToolCall` → `ParsedToolCall` or `ToolInputError` |
| `decide(mode, level)` | safety.permissions | ALLOW / ASK / DENY |
| `resolve_in_project` | safety.paths | path guaranteed inside the project, or raises |
| `AgentTask` | agent.state | lifecycle + action stages + verified-fix invariant |
| `EventSink` | observability.events | `emit(ExecutionEvent)` |

## Runtime files

`~/.trace/` (or `$TRACE_HOME`): `config.toml` ✅, `history.db` ✅ (tables `tasks` and `events`,
pruned by `history_retention_days`), `cache/` and `logs/` (created, unused). Credentials:
environment ✅, OS keyring 🔜.

See [AGENT_LOOP.md](AGENT_LOOP.md) for how these pieces will run together.
