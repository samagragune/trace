# Architecture decision records

Format: context → decision → consequences. Status: Accepted / Proposed / Superseded.

---

## ADR-001: Import package is `trace_agent`, not `trace` (Accepted, Build 001)

**Context.** The spec places code in `src/trace/`. Python's standard library already has a module
named `trace`. The stdlib directory comes before site-packages on `sys.path`, so `import trace`
would load the stdlib module and our package would be unreachable once installed. In editable or
development setups, the shadowing could also go the other way and break tools that use the stdlib
`trace` (coverage tooling, for example).

**Decision.** Keep the product name, the `trace` CLI command and the `trace-agent` distribution
name. Rename only the import package, to `trace_agent`.

**Consequences.** `from trace_agent import ...` everywhere. Verified: after installation, both
`import trace` (stdlib) and `import trace_agent` resolve correctly.

## ADR-002: Repository root is the working directory (Accepted, Build 001)

The spec's `trace/` prefix describes the repository root. The project was started in an empty
`Tracer Source Code` directory, which serves as that root. No extra nesting.

## ADR-003: Profile-based provider configuration plus an `openai_compatible` provider (Accepted, Build 001)

**Context.** The request was for the LLM provider to be fully open to configuration. A single
`provider`/`model` pair cannot represent "Groq for speed, local Ollama when offline", and the four
named providers do not cover self-hosted or corporate endpoints.

**Decision.** `config.toml` holds named `[profiles.*]`, each naming a provider type from an
extensible registry, plus `active_profile`. Add a fifth provider type, `openai_compatible`, which
requires `base_url` and has an optional key. Profiles accept `base_url`, `api_key_env`, timeouts,
retries and free-form `options`.

**Alternatives.** One flat provider section (rejected: switching providers would mean re-entering
everything). Free-form LiteLLM model strings such as `groq/llama...` (rejected: ties the config file
format to one library).

**Consequences.** Credentials are keyed by profile name, which allows two keys for the same provider.

## ADR-004: Synchronous provider and tool interfaces (Accepted, Build 001)

**Context.** The loop is strictly sequential: model call, then tools, then the next model call.
Tools are mostly blocking subprocesses.

**Decision.** `ModelProvider.complete` and `Tool.run` are synchronous. Cancellation uses a
`threading.Event` in `ToolContext`, plus KeyboardInterrupt.

**Trade-off.** Streaming tokens to the terminal or running tools in parallel would need an
`async`/streaming method later. That can be *added* to the interface without breaking existing
implementations.

## ADR-005: Phase 0 dependencies are minimal; LiteLLM deferred (Accepted, Build 001)

Runtime: `pydantic` (validation at every boundary), `typer` and `rich` (spec-mandated CLI), and
`tomli-w` (writing TOML; stdlib `tomllib` can only read). Dev: `pytest`.

LiteLLM is large and changes frequently. It is deferred to Phase 1 and will be **confined to one
adapter module**. The registry stays library-agnostic: no LiteLLM routing prefixes in
`ProviderSpec`. Phase 1 should pin LiteLLM to an exact, reviewed version because of its size and
release frequency.

## ADR-006: Verified-fix invariant enforced in the task model (Accepted, Build 001)

The spec's rule that "a model statement claiming success must never mark a task complete" is
enforced by `AgentTask.finish`, not left to orchestrator discipline. A `VERIFIED_FIX` needs a
successful modification followed by a passing verification, and the most recent verification
after the last modification must be the one that passed.

## ADR-007: Explicit action stages (Accepted, Build 001)

`ActionRecord` moves REQUESTED → AUTHORIZED/DENIED → EXECUTED/FAILED. Illegal transitions raise an
error, and EXECUTED or FAILED require the actual `ToolResult`. This makes the spec's distinction
between requested, executed and verified actions structural rather than conventional.

## ADR-008: `ScriptedProvider` lives in the production package (Accepted, Build 001)

It is needed by unit tests, by Phase 2 loop tests, and by benchmark harness self-tests, which
cannot import from `tests/`. It is not in the provider registry, so users cannot select it.

## ADR-009: LiteLLM pinned exactly and confined to one module (Accepted, Build 002)

`litellm==1.110.3`, imported only in `providers/litellm_adapter.py` and only lazily (importing it
takes several seconds). Reasons for the exact pin: a large transitive tree, very frequent releases,
and a past supply-chain compromise (1.82.7 and 1.82.8 were pulled from PyPI). Upgrades are
deliberate and reviewed. LiteLLM's own retries are disabled so that retry policy (and its
60-second cap) stays under Trace's control. The import-time price-list download is disabled
(local-first).

## ADR-010: The agent ends tasks through a `finish_task` tool (Accepted, Build 002)

**Alternatives:** treat any text-only reply as "done" (rejected: a model musing "let me check X"
would end the task), or parse a magic phrase (rejected: fragile). A tool call gives a structured,
schema-validated report (outcome, root cause, evidence, fix, confidence), and lets the
orchestrator *reject* an unverified "fixed" claim and keep the loop going.

## ADR-011: History is one append-only event log (Accepted, Build 002)

The spec lists sessions, tasks, messages, tool_calls, tool_results, file_changes, verifications and
events. Build 002 stores a `tasks` table plus a typed `events` table that covers all the rest.
This is simpler to write, and it reconstructs a run completely. Dedicated tables can be derived later
if queries need them. Payloads are redacted before they are stored.

## ADR-012: Approvals block in the terminal (Accepted, Build 002; resolves P-2)

ASK prompts the user in the terminal and waits. With no terminal (CI or piped input), every ASK
becomes a denial and the model is told. Suspending and resuming tasks (`AWAITING_APPROVAL`) remains
possible in the model but isn't wired up, because nothing needs it yet.

## ADR-013: Phase 2 ships read-only tools; outcome DIAGNOSED added (Accepted, Build 002)

Write and exec tools need checkpoints, git safety and command classification (Phase 4). Rather than
ship them without those protections, Build 002's agent investigates only. The new
`StopReason.DIAGNOSED` (→ COMPLETED) records an honest "root cause found, nothing changed or
verified". The final report always labels it "Diagnosis only".

## ADR-014: Phase 1 split (Accepted, Build 002)

The user asked to go to Phase 2 directly. Build 002 includes the parts of Phase 1 that Phase 2
depends on (the model adapter and `trace config test`). The keyring store, the `trace init` wizard
and `set-key`/`remove-key` are deferred; keys come from environment variables until then.

## ADR-015: Context is collected up front, deterministically and within a budget (Accepted, Build 003)

The spec's loop has an Observe step before Reason. Trace runs it without a model: detection tables,
regex trace parsers and an exact-text grep. These are fast, free and reproducible, which matters for
benchmarks. Hard budgets (3 snippets, 5,000 chars, 8 hits, a 5,000-file scan) honour "do not load
the entire repository". **Rejected:** embeddings or a vector index (heavy, non-deterministic, not
needed at this scale; the spec's Phase 0 "do not build yet" list includes vector databases).
Context failures never block a task: they are logged as an ERROR event and the task proceeds
without the extra context.

## ADR-016: Hypotheses are a built-in tool and part of task state (Accepted, Build 003)

The spec requires hypothesis formation and revision, and inspectable decisions without hidden
chain-of-thought. `record_hypothesis` captures them as structured data (statement, status,
evidence, transition history) that the terminal, history and benchmarks can use. Like
`finish_task`, it acts only on task state, so it bypasses the executor and needs no authorization.
It is optional for the model. The cost risk (bookkeeping-only round trips) was measured live and
addressed in the prompt.

## ADR-017: Tool output is redacted before it is sent to the model (Accepted, Build 003)

Project files and logs can contain credentials. Pattern-based redaction (Google, Groq, OpenRouter,
`sk-`, GitHub, AWS, PEM, bearer tokens) is applied to every tool's output, and the model is told
when it happens. Trade-off: a bug *about* a hardcoded key is still diagnosable (the model sees
`[REDACTED]` at the right location), and a deliberate test value that looks like a key gets hidden.

## ADR-018: The circular import is broken with `context/ignore.py` (Accepted, Build 003)

`tools` needs the ignore list and `context` is used by tools. The shared list now lives in a
dependency-free module, so neither package imports the other's internals.

---

## Open proposals (need your decision)

- **P-1: Rename for PyPI?** `trace-agent` may already be taken on PyPI, and on Linux a `trace`
  binary exists in some distributions (bcc tools, `perf trace`). Check name availability before
  Phase 6.
- **P-3: Load a `.env` file?** Recommendation: no automatic loading. Inside a user's project,
  `.env` holds *that project's* secrets. If wanted, add an explicit `--env-file PATH` option.
