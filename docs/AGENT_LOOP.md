# Agent loop: Observe → Reason → Act → Verify

**Status: implemented in Build 002** (`agent/orchestrator.py`, `agent/executor.py`,
`agent/observation.py`). With only read-only tools available, the best possible outcome today is a
*diagnosis*; fixes come with the write tools in Phase 4.

## Observe first: the opening context (Build 003)

Before the first model call, `context.selection.build_task_context` adds to the task message:
- a project overview,
- stack traces parsed from the user's report, mapped to project files,
- code around the innermost project frames,
- the project locations of quoted error text.

It is bounded (ADR-015), recorded as a CONTEXT_COLLECTED event, and can be turned off with
`OrchestratorSettings(collect_context=False)`.

## Hypotheses (Build 003)

`record_hypothesis` creates hypothesis #N or updates it (`id`) to supported or rejected with
evidence. Each transition is stored (`proposed@1`, `proposed->rejected@4`) and shown in the terminal,
the history and the final report.

## Ending a task: the `finish_task` tool

The model ends a task only by calling the built-in `finish_task` tool, with `outcome`, `summary`,
`root_cause`, `evidence[]`, `recommended_fix` and `confidence`. The orchestrator checks the claim
before accepting it:

| outcome | accepted when | StopReason |
|---|---|---|
| `fixed` | `task.is_fix_verified()`; otherwise rejected and the model is told why | VERIFIED_FIX |
| `diagnosed` | always (reported as "diagnosis only") | DIAGNOSED |
| `no_fix_needed` | always | NO_FIX_NEEDED |
| `insufficient_information` | always | INSUFFICIENT_INFORMATION |

`finish_task` sent in the same response as other tool calls is rejected, because the model hasn't
seen those calls' results yet. Every call still receives a tool result, as providers require.

## No-progress detection (implemented)
- **The same tool call (same name and arguments) repeated:** on the 3rd call a warning is appended
  to the result; on the 4th the task stops with NO_PROGRESS.
- **A response with no tool call:** the model is nudged ("continue or call finish_task"). After
  2 nudges, the 3rd text-only reply stops the task with NO_PROGRESS, and that text becomes the summary.

## The brain is pluggable
The system prompt lives in `agent/prompts.py` (`DEFAULT_SYSTEM_PROMPT`). Set
`[agent] system_prompt_file = "path/to/brain.md"` to replace it. The loop's guarantees
(authorization, the verification rule, limits) are enforced in code and don't depend on the prompt.

## One iteration

```
            ┌─────────────────────────────────────────────────────────────┐
            ▼                                                             │
 task.begin_iteration()            (raises at max_iterations)             │
            │                                                             │
 provider.complete(messages + registry.definitions(ctx))                  │
            │                                                             │
   no tool calls? ──► model wants to finish ──► task.finish(reason, summary)
            │               (VERIFIED_FIX is refused unless verified)     │
            ▼                                                             │
 for each ToolCall:                                                       │
   rec = task.record_action(call, permission)        stage = REQUESTED    │
   parsed = registry.parse_call(call)   ── ToolInputError ─► report to model
   decision = safety.decide(mode, tool.permission)                        │
     DENY ─► stage = DENIED, report "denied" to model                     │
     ASK  ─► prompt user (or finish APPROVAL_PENDING)                     │
     ALLOW─► stage = AUTHORIZED                                           │
   result = tool.run(args, ctx).truncated_to(ctx.max_output_chars)        │
   stage = EXECUTED (result.ok) or FAILED                                 │
   append Message.tool_result(call.id, render(result))                    │
   if the tool was a test run: task.add_verification(...)                 │
            └─────────────────────────────────────────────────────────────┘
```

Every step emits an `ExecutionEvent` to the configured `EventSink`.

## Why it is agentic and not scripted

The orchestrator contains **no debugging sequence**. It only:
1. asks the model what to do,
2. checks and executes what the model asked for, and
3. reports what *actually* happened.

The order of investigation, editing and testing comes entirely from model decisions that react to
observed results. Phase 2's acceptance test uses `ScriptedProvider` with a callable step that reads
the previous tool result from the request. That proves the loop forwards real observations.

## Requested ≠ executed ≠ verified

| Concept | Where it is recorded |
|---|---|
| Model *requested* an action | `ActionRecord.stage = REQUESTED` |
| Safety *authorized* or *denied* it | `AUTHORIZED` / `DENIED`, plus `decision` |
| It *ran* (and how it went) | `EXECUTED` / `FAILED`, plus the actual `ToolResult` |
| The fix was *verified* | `VerificationResult` recorded after the last modification |

Illegal transitions (for example, executing without authorization) raise `TaskStateError`.

## Completion rule (implemented)

`AgentTask.finish(StopReason.VERIFIED_FIX, ...)` succeeds only if:
- at least one WRITE or DESTRUCTIVE action executed successfully, **and**
- the most recent verification recorded after the last such action passed.

So "I fixed it" from the model, a passing test *before* the edit, or a later failing regression
check cannot produce a "completed" task.

## Loop controls (configured in `[agent]`)

| Control | Default | Status |
|---|---|---|
| `max_iterations` | 30 | ✅ enforced by `AgentTask.begin_iteration` |
| provider retries | profile `max_retries` = 3 | ✅ in the adapter: backoff 1s, 2s, 4s..., or the server's retry delay; never waits more than 60s |
| `command_timeout_seconds` | 60 | passed to `ToolContext`; enforced by the subprocess tools in Phase 4 |
| repeated-call / no-progress detection | warn at 3, stop at 4 | ✅ |
| Ctrl-C | n/a | ✅ sets `cancel_event`, finishes USER_INTERRUPTED |

## Stop reasons → task status

| StopReason | TaskStatus |
|---|---|
| VERIFIED_FIX, NO_FIX_NEEDED, DIAGNOSED | COMPLETED |
| ITERATION_LIMIT, NO_PROGRESS, INSUFFICIENT_INFORMATION | STOPPED |
| SAFETY_VIOLATION, PROVIDER_UNAVAILABLE | FAILED |
| USER_INTERRUPTED | CANCELLED |
| APPROVAL_PENDING | AWAITING_APPROVAL (resumable) |
