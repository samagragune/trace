# Build log

## Build 001: Phase 0 foundation and configurable providers (2026-11-24)

**Objective.** Create an installable project with tested interfaces for providers, tools, safety,
task state and events, and make LLM provider choice fully configurable.

**Delivered.**
- Package `trace-agent` (import `trace_agent`, CLI `trace`), hatchling build, Python 3.11+.
- Interfaces: `ModelProvider`, `ProviderFactory`, `ProviderRegistry`, `CredentialStore`, `Tool`,
  `ToolRegistry`, `EventSink`.
- Working features: profile configuration (TOML, atomic save, validation that rejects secrets),
  credential resolution from environment variables, permission policy, project-boundary
  enforcement, task state machine with the verified-fix invariant, and the CLI configuration commands.
- Docs: ARCHITECTURE, AGENT_LOOP, TOOLS, PROVIDERS, SECURITY, BENCHMARKS, DECISIONS (ADR-001 to 008).

**Tests.** 122 passed, 1 skipped. The skipped test is the symlink-escape test, because Windows
without Developer Mode cannot create symlinks; it needs to run on Linux CI. Wheel builds
successfully. The installed `trace` command was smoke-tested end to end.

**Bugs found by running it for real.**
1. The providers table wrapped and truncated provider ids, env var names and model names at
   80 columns. Fixed by switching to a list layout.
2. CLI validation errors showed raw Pydantic output. Fixed with a shared formatter. Both fixes have
   regression tests.

**Not done (by design).** Network adapters, keyring, setup wizard, connection test, orchestrator,
concrete tools, SQLite, context engine, benchmarks.

**Next: Build 002 (Phase 1).** LiteLLM adapter behind `ModelProvider`, keyring `CredentialStore`,
`trace init` wizard, connection test (`trace config test`), and `trace config set-key` / `remove-key`.

---

## Build 002: Phase 2 agent loop, plus the Phase 1 parts it needs (2026-11-24)

**Objective.** An agent that receives a task, chooses tools, executes them under authorization,
observes real results and decides its next step, using a real model provider.

**Delivered.**
- LiteLLM adapter for all 5 provider types: normalized errors, readable vendor messages, retry
  with server delays (never waits more than 60s), key redaction, preserved Gemini 3 thought
  signatures. `trace config test`.
- Orchestrator loop, `ToolExecutor` (parse → policy → approval → run → truncate), observation
  rendering, repeated-call and idle detection, the `finish_task` tool with an unverified "fixed"
  claim rejected, and a pluggable system prompt (`[agent] system_prompt_file`).
- Read-only tools: `list_files`, `read_file`, `search_code`.
- Redaction applied to every event; SQLite history (`tasks`, `events`, retention pruning).
- CLI: interactive `trace` session, `trace run`, `trace history [show]`, terminal approvals,
  a live progress view and a final report panel.

**Tests.** 182 passed, 1 skipped (the symlink test, as before).
Phase 2 acceptance test: `test_next_action_depends_on_previous_tool_result`. The scripted model
computes its 2nd call's path from the 1st call's real search output, and its 3rd call's
evidence line from the 2nd call's real file content. ✅

**Live provider checks.**
- ✅ A request with an invalid key reached Google and was reported as
  `AUTHENTICATION FAILED: API key not valid. [INVALID_ARGUMENT, API_KEY_INVALID]` (exit 1).
- ✅ A missing key is reported as `needs an API key: set the GEMINI_API_KEY environment variable`.
- ✅ **Real key, verified 2026-11-25** (the key file was saved that morning):

  | Model | `trace config test` |
  |---|---|
  | gemini-3.5-flash-lite | OK, ~1 to 3s, tool calling yes |
  | gemini-3-flash-preview | OK, ~8 to 12s, tool calling yes |
  | gemini-3.8-flash, gemini-flash-latest | HTTP 503 "high demand" (temporary, on Google's side) |
  | gemini-3.1-pro-preview | RATE LIMITED: free tier `limit: 0`, so no Pro quota on this plan; reported in 15s, not slept through |
  | gemini-2.5-flash / -lite / -pro | MODEL NOT FOUND: "no longer available to new users" |

- ✅ **Live agent run** (gemini-3.5-flash-lite) on a planted multi-file bug (percent stored as a
  whole number in `discounts.py`, crash raised in `checkout.py`): 9 steps, 8 read-only tool calls,
  16.8k tokens, 28s. Correct root cause, all 4 evidence `file:line` references correct, correct fix,
  honestly labelled "Diagnosis only". The key was confirmed absent from `history.db` and `config.toml`.

**Bugs found only by running live (all fixed, with regression tests):**
1. **A display crash killed the agent run.** The cp1252 Windows console couldn't encode `→`, and
   Rich raised from inside the event sink, which propagated into the loop. Fixed at three levels:
   `CompositeSink` now isolates failing sinks (warns once, other sinks continue); the CLI
   reconfigures stdout/stderr with `errors="replace"`; CLI glyphs are now ASCII.
2. The built-in Gemini suggestions (`gemini-2.5-*`) were dead models. Replaced with live-checked ones.
3. Vendor error text showed a literal `\n`; `config test` latency included the ~6s LiteLLM import
   (added `ModelProvider.warm_up()`); history showed UTC instead of local time.

**Bugs found during the build.** Two test bugs (an index into the observation format, and the
repeat-warning threshold), both fixed. Raw vendor JSON in error messages, fixed with message extraction.

**Deferred.** Keyring store, `trace init` wizard, set-key/remove-key (ADR-014); write and exec tools
(Phase 4); context engine (Phase 3).

**Known limitations (Build 002).** In-process tools have no timeout yet (they are bounded by size limits
instead). A task's row is written to history when it ends, so a hard crash leaves its events but
no task row. Message-level Gemini thought signatures on text-only turns are not preserved (only
tool-call ones). No token budget or context compaction yet: very long investigations will
eventually hit the context window and stop with an error.

---

## Build 003: Phase 3, project understanding and investigation (2026-11-25)

**Objective.** Start every task oriented (project overview, parsed stack traces, code at the
failing frames) and give the agent better investigation tools and explicit hypotheses.

**Delivered.**
- `context/project.py`: detects languages, package managers, frameworks, test framework, the likely
  test command, test dirs and git status. Data-driven tables; bounded scan (5,000 files).
- `context/stacktrace.py`: parses Python (including chained exceptions and pytest's short format),
  JS/Node, Java/Kotlin (with "Caused by"), Go, Rust and Ruby traces. Maps frames to project files,
  including CI and container paths (suffix matching) and Windows paths, marks library frames, and
  goes through the safety layer for every lookup.
- `context/selection.py`: a bounded opening context (overview + traces + up to 3 snippets of
  5,000 chars total + up to 8 locations of quoted error text). Emits CONTEXT_COLLECTED.
- Tools: `get_project_info`, `parse_stack_trace`, `inspect_logs` (discovers log files, then shows
  matching lines with context plus the tail; scans the newest 5 MB); `search_code` gains `context_lines`.
- The `record_hypothesis` built-in, and `AgentTask.hypotheses` with a status history (shown in the
  terminal, history and final report).
- Credential-pattern redaction of **tool output** before it reaches the model (secrets in the user's
  project are not sent to a cloud provider).
- Token efficiency: tool schemas compacted (`title` annotations removed), and the prompt asks for
  batched independent calls and no bookkeeping-only responses.

**Tests.** 216 passed, 1 skipped (the symlink test). New: 11 stack-trace tests (6 languages, CI
paths, boundary escapes), 13 context and tool tests, and 5 orchestrator tests (opening context,
hypothesis revision, project-secret redaction).

**Live verification (gemini-3.5-flash-lite, buggy-shop fixture). All 7 runs diagnosed correctly.**

| Run | Input | Steps | Tokens | Note |
|---|---|---|---|---|
| Build 002 baseline | quoted error | 9 | 16.8k | |
| 003, first cut | quoted error | 10 | 30.9k | +1,050 prompt tokens per call; 2 bookkeeping-only calls |
| 003, compact schemas + batching | quoted error | 8 / 6 | 23.6k / 18.0k | batching seen; propose-then-support ceremony remained |
| 003, final prompt | quoted error | 4 / 7 | 12.1k / 20.7k | ceremony gone; batching inconsistent |
| 003, final prompt | pasted pytest output | 8 | 26.0k | trace parsed live (Windows paths); never needed to read checkout.py |

**Honest efficiency verdict.** On this 5-file fixture, the opening context did *not* reliably cut
cost. Orientation was already cheap, and the variance is dominated by whether the model batches
calls (4 to 8 steps with an identical prompt and model). The token analysis found and fixed two real
regressions (schema overhead and bookkeeping calls). The context engine's expected value is in
large repositories and stack-trace-heavy reports, which this fixture can't show. With n ≤ 2 per
configuration these numbers are anecdotes; the Phase 5 benchmark harness is required to make
efficiency claims.

**Bug found live and fixed:** Rich swallowed `[supported]` in "Hypotheses considered" (it treated
it as a markup tag).

**Known limitations.** No context-window management yet (still the main scaling risk). Snippet and
message-hit selection is lexical, not semantic. Monorepo detection only covers the root (or a
directory passed to `get_project_info`). The model still sometimes re-reads files after it has
enough evidence.
