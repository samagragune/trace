# Architecture decision records

**Last updated:** 2026-11-25 (through Build 003)

Format per record: **Context → Decision → Alternatives → Consequences.** Statuses: *Accepted*,
*Superseded (by ADR-n)*, *Partially superseded*, *Proposed*. ADRs are never deleted; when a decision
changes, the old record is marked and a new one is added.

Related: [ARCHITECTURE.md](ARCHITECTURE.md) (the system as built) ·
[BUILD_LOG.md](BUILD_LOG.md) (chronological record with test results).

---

## Index

| ADR | Title | Phase | Build | Status |
|---|---|---|---|---|
| 001 | Import package is `trace_agent`, not `trace` | 0 | 001 | Accepted |
| 002 | Repository root is the working directory | 0 | 001 | Accepted |
| 003 | Profile-based provider configuration + `openai_compatible` | 0 | 001 | Accepted |
| 004 | Synchronous provider and tool interfaces | 0 | 001 | Accepted |
| 005 | Minimal Phase 0 dependencies; LiteLLM deferred | 0 | 001 | Partially superseded by 009 |
| 006 | Verified-fix invariant enforced in the task model | 0 | 001 | Accepted |
| 007 | Explicit action stages | 0 | 001 | Accepted |
| 008 | `ScriptedProvider` lives in the production package | 0 | 001 | Accepted |
| 019 | Config rejects secrets structurally | 0 | 001 | Accepted |
| 020 | Credential resolution: environment first, keyed by profile | 0 | 001 | Accepted |
| 021 | `storage.resolve` is the only place a profile meets a secret | 0 | 001 | Accepted |
| 009 | LiteLLM pinned exactly and confined to one module | 1/2 | 002 | Accepted |
| 010 | Tasks end through a `finish_task` tool | 2 | 002 | Accepted |
| 011 | History is one append-only event log | 2 | 002 | Accepted |
| 012 | Approvals block in the terminal | 2 | 002 | Accepted (resolves P-2) |
| 013 | Phase 2 ships read-only tools; outcome DIAGNOSED | 2 | 002 | Accepted |
| 014 | Phase 1 split around the user's skip to Phase 2 | 1/2 | 002 | Accepted |
| 022 | Opaque provider metadata on tool calls (Gemini 3 thought signatures) | 2 | 002 | Accepted |
| 023 | No-progress detection thresholds | 2 | 002 | Accepted |
| 024 | The brain is a replaceable prompt file | 2 | 002 | Accepted |
| 025 | `cli/session.py` is the single composition root | 2 | 002 | Accepted |
| 026 | Observers can never break the task they observe | 2 | 002 | Accepted |
| 027 | Model suggestions are hints, verified live | 1/2 | 002 | Accepted |
| 015 | Context collected up front, deterministically, within a budget | 3 | 003 | Accepted |
| 016 | Hypotheses are a built-in tool and part of task state | 3 | 003 | Accepted |
| 017 | Tool output is redacted before reaching the model | 3 | 003 | Accepted |
| 018 | `context/ignore.py` breaks the tools↔context cycle | 3 | 003 | Accepted |
| 028 | Token efficiency: compact schemas and batching guidance | 3 | 003 | Accepted |

ADR numbers reflect the order in which records were *written*. ADRs 019–028 document decisions
that were made during Builds 001–003 and written up retroactively on 2026-11-25. The index orders
them by the build that made them.

---

## Phase 0, Build 001: foundation

### ADR-001: Import package is `trace_agent`, not `trace`
*Accepted, Build 001*

**Context.** The spec places code in `src/trace/`. Python's standard library already has a module
named `trace`. The stdlib directory comes before site-packages on `sys.path`, so `import trace`
would load the stdlib module and our package would be unreachable once installed. In development
setups, the shadowing could also go the other way and break tools that use the stdlib `trace`
(coverage tooling, for example).

**Decision.** Keep the product name, the `trace` CLI command and the `trace-agent` distribution
name. Rename only the import package, to `trace_agent`.

**Consequences.** `from trace_agent import ...` everywhere; `python -m trace_agent` also works.
Verified: after installation, both `import trace` (stdlib) and `import trace_agent` resolve correctly.

### ADR-002: Repository root is the working directory
*Accepted, Build 001*

The spec's `trace/` prefix describes the repository root. The project was started in an empty
`Tracer Source Code` directory, which serves as that root, with no extra nesting. Pushed to
https://github.com/samagragune/trace.

### ADR-003: Profile-based provider configuration plus an `openai_compatible` provider
*Accepted, Build 001. Origin: user request F-1.*

**Context.** The user asked that the LLM provider be fully open to configuration. A single
`provider`/`model` pair cannot represent "Groq for speed, local Ollama when offline", and the four
named providers don't cover self-hosted or corporate endpoints.

**Decision.** `config.toml` holds named `[profiles.*]`, each naming a provider type from an
extensible registry, plus `active_profile`. A fifth provider type, `openai_compatible`, requires
`base_url` and takes an optional key (LM Studio, vLLM, llama.cpp, gateways). Profiles accept
`base_url`, `api_key_env`, timeouts, retries and free-form `options`.

**Alternatives.** One flat provider section (rejected: switching providers would mean re-entering
everything). Free-form LiteLLM strings such as `groq/llama...` (rejected: ties the config format to
one library).

**Consequences.** Credentials are keyed by profile name (two keys for one provider are possible).
Adding a provider is a data change (`ProviderSpec`) and needs no new code paths.

### ADR-004: Synchronous provider and tool interfaces
*Accepted, Build 001*

**Context.** The loop is strictly sequential (model call → tools → model call); tools are mostly
blocking subprocesses.

**Decision.** `ModelProvider.complete` and `Tool.run` are synchronous. Cancellation uses a
`threading.Event` in `ToolContext`, plus KeyboardInterrupt.

**Trade-off.** Streaming tokens or running tools in parallel would need an async or streaming
method, which can be *added* to the interface later without breaking existing implementations.

### ADR-005: Minimal Phase 0 dependencies; LiteLLM deferred
*Partially superseded by ADR-009 (Build 002 added LiteLLM)*

Runtime: `pydantic` (validation at every boundary), `typer` and `rich` (spec-mandated CLI),
`tomli-w` (writing TOML; stdlib `tomllib` only reads). Dev: `pytest`. Build backend: `hatchling`
(the spec allowed hatchling or setuptools; hatchling needs no `setup.py` and supports PEP 639
license metadata). The deferral of LiteLLM ended in Build 002. The rule that it stays confined to
one adapter module and out of `ProviderSpec` still holds.

### ADR-006: Verified-fix invariant enforced in the task model
*Accepted, Build 001*

The spec's rule that "a model statement claiming success must never mark a task complete" is
enforced by `AgentTask.finish`, not left to orchestrator discipline. `VERIFIED_FIX` needs a
successful WRITE/DESTRUCTIVE action, and the most recent verification recorded after the last such
action must have passed. A passing test *before* the edit, or a later failing regression check,
both prevent completion. The orchestrator checks it as well (ADR-010), so there are two
independent checks.

### ADR-007: Explicit action stages
*Accepted, Build 001*

`ActionRecord` moves REQUESTED → AUTHORIZED/DENIED → EXECUTED/FAILED. Illegal transitions raise an
error, and EXECUTED or FAILED require the actual `ToolResult`. This makes the spec's distinction
between requested, executed and verified actions structural rather than a convention.

### ADR-008: `ScriptedProvider` lives in the production package
*Accepted, Build 001*

It is needed by unit tests, by loop tests (it carries the Phase 2 acceptance test), and later by
benchmark self-tests, which cannot import from `tests/`. It is not in the provider registry, so
users cannot select it.

### ADR-019: Configuration rejects secrets structurally
*Accepted, Build 001 (recorded retroactively)*

**Context.** Spec: "Never commit credentials to Git", "No API key storage" outside the keyring. A
plain-text config file invites users to paste keys into it.

**Decision.** Every config model uses `extra="forbid"`, so an `api_key = "..."` line makes the whole
file invalid with a pointed error. `options` rejects credential-like keys (`api_key`, `token`,
`secret`, `password`, ...); `max_tokens` is still allowed. Saves are atomic (temp file +
`os.replace`).

**Consequences.** Users get an error instead of a silently stored secret. A newer `schema_version`
is refused, rather than misread.

### ADR-020: Credential resolution is environment-first, keyed by profile name
*Accepted, Build 001 (recorded retroactively)*

**Decision.** Resolution order: the profile's `api_key_env`, then the provider's conventional
variable (`GEMINI_API_KEY`, ...), then `CredentialStore[profile_name]`. Blank values count as
absent. Outcomes: ENVIRONMENT, STORE, NOT_REQUIRED, ABSENT_OPTIONAL, MISSING. Keys exist only as
`SecretStr`.

**Why environment first:** that is the common convention, and it is what CI, containers and
one-off overrides need. **Consequence:** until the keyring ships (ADR-014), environment variables
are the only source.

### ADR-021: `storage.resolve` is the only place a profile meets a secret
*Accepted, Build 001 (recorded retroactively)*

`ProviderProfile` (persisted, secret-free) and `ProviderSettings` (runtime, holds the `SecretStr`)
are separate types, joined only in `resolve_provider_settings`. Result: `providers` never imports
`storage`, adapters are constructible in tests from plain settings, and there is exactly one code
path to audit for key handling.

---

## Phases 1–2, Build 002: model connectivity and the agent loop

### ADR-009: LiteLLM pinned exactly and confined to one module
*Accepted, Build 002*

**Decision.** `litellm==1.110.3`, imported only in `providers/litellm_adapter.py`, and only lazily
(the import takes several seconds; `warm_up()` lets the CLI exclude it from latency figures).

**Why an exact pin:** a large transitive dependency tree, very frequent releases, and a past
supply-chain compromise (1.82.7 and 1.82.8 were pulled from PyPI). Upgrades are deliberate and
reviewed.

**Behaviour Trace owns, not LiteLLM:**
- **Retries:** LiteLLM's retries are off. Trace retries only `retryable` errors, uses the server's
  delay (or 1/2/4s backoff, capped at 30s), and **never sleeps longer than 60s**. A daily-quota 429
  is reported at once instead of hanging (verified live: 15s against a requested wait of 3.4h).
- **Error text:** the vendor's JSON message is extracted, and the key is redacted from it.
- **Local-first:** `LITELLM_LOCAL_MODEL_COST_MAP=True` (no price-list download on import); telemetry off.

### ADR-010: Tasks end through a `finish_task` tool
*Accepted, Build 002*

**Alternatives.** Treat any text-only reply as "done" (rejected: a model musing "let me check X"
would end the task); parse a magic phrase (rejected: fragile).

**Decision.** A built-in tool with a schema: outcome, summary, root cause, evidence, fix,
confidence. The orchestrator rejects an invalid report, a `finish_task` mixed with other calls
(the model hasn't seen their results yet), and "fixed" without verification. Each rejection is
explained to the model and the loop continues.

### ADR-011: History is one append-only event log
*Accepted, Build 002*

The spec lists sessions, tasks, messages, tool_calls, tool_results, file_changes, verifications and
events. Trace stores a `tasks` table plus one typed `events` table that covers the rest: simpler to
write, and it reconstructs a run completely. Payloads are redacted before storage (ADR-026), and
retention pruning runs at the start of every task. Dedicated tables can be derived later if
queries need them.

### ADR-012: Approvals block in the terminal
*Accepted, Build 002. Resolves P-2.*

ASK prompts in the terminal, defaulting to **no**. Without a TTY (CI, pipes), every ASK becomes a
denial and the model is told. Suspend/resume (`AWAITING_APPROVAL` + `resume()`) exists in the task
model but isn't wired up, because nothing needs it yet.

### ADR-013: Phase 2 ships read-only tools; outcome DIAGNOSED added
*Accepted, Build 002*

Write and exec tools need checkpoints, git safety and command classification (Phase 4). Rather
than ship them without those, the agent only investigates. `StopReason.DIAGNOSED` (→ COMPLETED)
records an honest "root cause found; nothing changed or verified", and the final report always
labels it "Diagnosis only".

### ADR-014: Phase 1 split around the user's skip to Phase 2
*Accepted, Build 002. Origin: user request F-4.*

The user asked to go directly to Phase 2. Build 002 took only the parts of Phase 1 that Phase 2
depends on: the model adapter, `trace config test`, and the interactive session. **Deferred:** the
OS-keyring `CredentialStore`, the `trace init` wizard, and `config set-key`/`remove-key`. The spec's
`trace reset` is also not built yet.

### ADR-022: Opaque provider metadata on tool calls
*Accepted, Build 002 (recorded retroactively)*

**Context.** Gemini 3 attaches "thought signatures" to function calls. If they aren't sent back
unchanged on the next turn, multi-step tool use fails. LiteLLM 1.110.3 carries them in the tool-call
id (`<id>__thought__<sig>`) and in `provider_specific_fields` (verified in its source).

**Decision.** `ToolCall.provider_metadata` holds opaque provider data. The adapter round-trips both
the id and the fields unchanged; nothing else interprets them.

**Known gap.** Message-level signatures on text-only turns are not yet preserved.

### ADR-023: No-progress detection thresholds
*Accepted, Build 002 (recorded retroactively)*

- **Identical tool call** (same name and canonical-JSON arguments): a warning is appended to the
  result on the 3rd call; the task stops with NO_PROGRESS on the 4th.
- **Text-only replies:** nudged twice; the 3rd stops with NO_PROGRESS, and that text becomes the
  summary.
- **Iteration limit:** 30 by default.

Chosen to tolerate one legitimate re-check while bounding loops cheaply. Each is configurable
through `OrchestratorSettings`.

### ADR-024: The brain is a replaceable prompt file
*Accepted, Build 002. Origin: user request F-5.*

The user will supply their own agent brain later. The default prompt lives in `agent/prompts.py`;
`[agent] system_prompt_file` replaces it with no code change. Every safety guarantee
(authorization, verification, limits, redaction) is enforced in code, so a custom brain cannot
weaken them.

### ADR-025: `cli/session.py` is the single composition root
*Accepted, Build 002 (recorded retroactively)*

One module wires config → credentials → factory → provider → registry → sinks → approver →
orchestrator. Every other package depends only on interfaces, so the orchestrator is tested with
no files, network or terminal. Tests swap the provider through this one seam
(`session.build_provider`).

### ADR-026: Observers can never break the task they observe
*Accepted, Build 002 (live hotfix, 2026-11-25; recorded retroactively)*

**Context (found live).** On a cp1252 Windows console, Rich raised `UnicodeEncodeError` while
printing `→`. The exception propagated out of the terminal sink, through the orchestrator, and
**killed a live debugging run** at step 1. No unit test caught it, because tests don't use a
legacy console.

**Decision (three layers).**
1. `CompositeSink` isolates every sink: an exception is caught, reported once on stderr, and
   recorded in `failures`, and the other sinks still receive the event.
2. The CLI reconfigures stdout and stderr with `errors="replace"`: model text can contain any
   Unicode.
3. CLI glyphs are ASCII.

Regression tests cover both the isolation and "a crashing printer cannot fail the run".

### ADR-027: Model suggestions are hints, verified live
*Accepted, Build 002 (recorded retroactively)*

**Context.** A live probe on 2026-11-25 found `gemini-2.5-*` returning 404 "no longer available to
new users". The suggestions written in Build 001 were dead models.

**Decision.** `suggested_models` are hints only: any model string is accepted, and
`trace config test` is the source of truth. Gemini suggestions were replaced with live-verified
models (`gemini-3.5-flash-lite`, `gemini-3-flash-preview`, `gemini-3.8-flash`), with the
verification date in a code comment. Suggestions for other providers are still unverified.

---

## Phase 3, Build 003: project understanding

### ADR-015: Context is collected up front, deterministically and within a budget
*Accepted, Build 003*

The spec's loop has Observe before Reason. Trace runs it with **no model calls**: detection tables,
regex trace parsers and an exact-text grep, all fast, free and reproducible (which matters for
benchmarks). Hard budgets: a 5,000-file scan, at most 3 snippets within 5,000 chars, at most 8
error-text hits. This honours "do not load the entire repository".

**Rejected:** embeddings or a vector index (heavy, non-deterministic, unnecessary at this scale; the
spec's Phase 0 "do not build yet" list includes vector databases). Context failures never block a
task: they are logged as an ERROR event and the task proceeds without the extra context.

**Measured caveat (Build 003).** On a 5-file fixture this did not reliably reduce cost (see
ADR-028 and BUILD_LOG). Its value is expected in large repositories and traceback-heavy reports;
that needs proof from the Phase 5 benchmark.

### ADR-016: Hypotheses are a built-in tool and part of task state
*Accepted, Build 003*

The spec requires hypothesis formation and revision, and inspectable decisions without hidden
chain-of-thought. `record_hypothesis` stores statement, status, evidence and transition history
(`proposed->rejected@4`) for the terminal, history and benchmarks. Like `finish_task`, it acts only
on task state: it bypasses the executor and needs no authorization. It is optional for the model.

### ADR-017: Tool output is redacted before reaching the model
*Accepted, Build 003*

Project files and logs can contain credentials, and sending them to a cloud model leaks them.
Credential patterns (Google, Groq, OpenRouter, `sk-`, GitHub, AWS, PEM, bearer) are redacted from
every tool's output and from the opening context, and the model is told when this happens.
**Trade-off:** a bug *about* a hardcoded key is still diagnosable, because `[REDACTED]` appears at
the right location; a harmless test value that looks like a key gets hidden too.

### ADR-018: `context/ignore.py` breaks the tools↔context cycle
*Accepted, Build 003*

`context.project` needed the excluded-directory list from `tools.builtin.filesystem`, while the new
context tools import `context`, which would be a cycle whose outcome depends on import order. The
list moved to a dependency-free module that both import.

### ADR-028: Token efficiency: compact schemas and batching guidance
*Accepted, Build 003 (driven by live measurement)*

**Context (measured).** The first Build 003 cut used **30.9k tokens** against Build 002's 16.8k on
the same task. Per-call analysis from `history.db`: prompt tokens per call rose about 1,050 (from
1,371 to 2,424), and the whole conversation is re-sent on every call. Causes: 8 tool schemas (about
1,780 tokens, with 38 Pydantic `title` keys), the longer prompt and the opening context, plus 2
model calls spent purely on hypothesis bookkeeping.

**Decisions.**
1. `compact_json_schema` removes `title` annotations from every tool schema (a parameter named
   "title" survives): about 220 tokens saved per call.
2. The prompt asks for independent calls to be batched, for hypotheses to be recorded in the same
   turn as the call that tests them (or directly as "supported"), and for no bookkeeping-only turns.

**Result.** Over 5 live runs: 12.1k–26.0k tokens (best 28% below baseline). The bookkeeping turns
disappeared, but variance stays high, because the model batches inconsistently. **Not claimed:** a
reliable improvement. That needs the Phase 5 benchmark (many runs per configuration).

---

## Rejected alternatives (summary)

| Alternative | Rejected in | Reason |
|---|---|---|
| Import package named `trace` | ADR-001 | shadowed by the stdlib `trace` module |
| A single flat provider config | ADR-003 | switching providers would mean re-entering everything |
| LiteLLM model strings in config | ADR-003 | ties the config format to one library |
| async interfaces | ADR-004 | complexity without throughput in a sequential loop |
| LiteLLM's built-in retries | ADR-009 | can sleep through hour-long quota waits; hides retry policy |
| text-only reply = task done | ADR-010 | ends tasks on "let me check..." |
| one table per spec entity | ADR-011 | more schema for no reconstruction benefit yet |
| shipping write tools in Phase 2 | ADR-013 | no checkpoints, git safety or command screening yet |
| embeddings / vector index for context | ADR-015 | heavy, non-deterministic, unnecessary at this scale |
| auto-loading `.env` | P-3 | it would ingest the debugged project's secrets |

---

## User feedback record

Spec (human_feedback_protocol): *"Maintain a record of accepted and rejected feedback."*

| # | Date | Feedback / instruction | Response | Impact |
|---|---|---|---|---|
| F-1 | 2026-11-24 | "Make sure the LLM provider is open to configuration" | **Accepted** | ADR-003: profiles, a 5th `openai_compatible` provider, `options` passthrough |
| F-2 | 2026-11-24 | "Move onto build 2; test if the Gemini key works or is rate limited" | **Accepted** | key test blocked (the `.env` file was 0 bytes on disk until 2026-11-25); then verified live (ADR-027) |
| F-3 | 2026-11-24 | Key "configured in `.env`" | **Accepted, with a design caveat** | used `.env` only for the live tests; Trace does not auto-load `.env` (P-3) |
| F-4 | 2026-11-24 | "Let's configure the brain later, move on to phase 2" | **Accepted** | ADR-014 (Phase 1 split); ADR-024 (pluggable brain) |
| F-5 | 2026-11-24 | "I will provide a brain" | **Accepted, pending** | hook ready (`system_prompt_file`); brain not yet provided |
| F-6 | 2026-11-25 | "Push to github.com/samagragune/trace" | **Accepted** | initial commit `18c4240`; Phase 3 `af0f4aa` pushed on request |
| F-7 | 2026-11-25 | "Do not co-author it in commits" | **Accepted** | process rule: commits carry no co-author or attribution trailers |
| F-8 | 2026-11-25 | "Move onto next phase" (after Build 002) | **Accepted** | Build 003 = Phase 3, the roadmap order (Phase 4 was recommended; the user chose the next phase) |
| F-9 | 2026-11-25 | "Update architecture and decision md files with every phase and build detail" | **Accepted** | this revision; ADRs 019–028 written retroactively |

---

## Repository and process conventions

- **Commits:** authored by the user; no `Co-Authored-By` or other attribution trailers (F-7).
- **Line endings:** `.gitattributes` normalizes to LF in the repository on all platforms.
- **Secrets:** `.env`, `.venv/`, `history.db` and user config are excluded. Before every commit the
  staged content is scanned for the live API key.
- **Per build:** an architectural report, a BUILD_LOG entry, ADRs for new decisions, and docs that
  match the implementation.

---

## Open proposals (need a decision)

| ID | Proposal | Recommendation | Raised |
|---|---|---|---|
| P-1 | Rename for PyPI? `trace-agent` may be taken; a `trace` binary exists in some Linux distributions (bcc, `perf trace`) | check name availability before Phase 6 | Build 001 |
| P-2 | ~~Interactive approvals vs `AWAITING_APPROVAL`~~ | **Resolved** by ADR-012 | Build 001 |
| P-3 | Load a `.env` file? | no automatic loading; optionally add an explicit `--env-file PATH` | Build 002 |
| P-4 | `[agent] max_retry_attempts` is accepted but unused | remove it, or wire it to Phase 4 tool retries; don't leave a no-op setting | Build 003 review |
| P-5 | Context-window management priority | do it before or with Phase 4: editing adds diffs and test output, which will hit limits sooner | Build 003 review |
| P-6 | Next phase: 4 (edit and verify) or 5 (benchmark) | Phase 4: it is the step that turns a diagnosis into a verified fix | Build 003 |
