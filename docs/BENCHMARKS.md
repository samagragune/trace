# Benchmarks

**Status: design only (implementation is Phase 5).** Phase 0 prepared for it in two ways:
`ScriptedProvider` makes agent runs deterministic for harness self-tests, and `AgentTask` plus
`ExecutionEvent` already record the data the metrics need.

## Principles
1. **Independent verification.** Success is decided by hidden tests the harness runs after the
   agent stops, never by the model's claim or by the agent's own `VerificationResult`.
2. **Isolation.** Each task runs in a fresh copy of its fixture repository.
3. **Reproducibility.** Record the Trace version, provider, model, pinned dependencies, OS and
   Python version.
4. **Separation.** Harness code lives in `trace_agent.benchmark`, datasets in `benchmarks/`,
   results in `benchmarks/results/`. The production agent never imports the benchmark package.

## Planned task format (JSONL, one object per line)

```json
{"id": "py-keyerror-001", "fixture": "fixtures/registration_form", "difficulty": "easy",
 "description": "Submitting the registration form crashes",
 "reproduction": "pytest tests/test_register.py::test_submit",
 "expected_failing_test": "tests/test_register.py::test_submit",
 "hidden_tests": ["hidden/test_register_hidden.py"],
 "expected_files": ["app/forms.py"]}
```

## Metric sources

| Category | Metrics | Source |
|---|---|---|
| Localization | top-k file hit | files read and edited vs. `expected_files` |
| Patch | applies, regression pass rate | harness diff + hidden tests |
| Autonomous resolution | verified rate, tool calls, iterations | `AgentTask.actions`, `.iteration` |
| Efficiency | tokens, cost, time, model calls | `TokenUsage`, MODEL_* events |
| Safety | denied/out-of-project attempts, leaks | DENIED actions, `PathOutsideProjectError` events |
