# Tools

## Contract ✅

```python
class ReadFileInput(ToolInput):          # extra="forbid": unknown arguments are rejected
    path: str
    start_line: int | None = None

class ReadFile(Tool):
    name = "read_file"                    # ^[a-zA-Z][a-zA-Z0-9_-]{0,63}$
    description = "Read a source file."
    permission = PermissionLevel.READ     # required; drives the safety policy
    input_model = ReadFileInput           # JSON Schema sent to the model comes from this

    def is_available(self, ctx) -> bool:  # optional; hide tools the environment can't run
        return True

    def run(self, args: ReadFileInput, ctx: ToolContext) -> ToolResult:
        path = resolve_in_project(ctx.project_root, args.path)   # mandatory for paths
        ...
        return ToolResult.success(text, lines=n)
```

Rules enforced in code:
- `ToolRegistry.register` rejects tools with an invalid name, no `PermissionLevel`, or an input
  model that doesn't subclass `ToolInput`, as well as duplicate names.
- `ToolRegistry.parse_call` validates model arguments. Malformed JSON, missing fields, wrong types
  and unknown fields all raise `ToolInputError`, with a message meant to be sent back to the model.
- `ToolResult`: `SUCCESS` must not carry an `error`, and every other status must explain one.
- `ToolResult.truncated_to(n)` keeps the head *and* tail of the output, because test runners
  print their summary last.

Tools do **not** decide whether they may run. The orchestrator calls `safety.decide` first.

## Implemented tools (Build 002): `tools/builtin/filesystem.py`

| Tool | Permission | Inputs | Behavior |
|---|---|---|---|
| `list_files` | READ | `directory=".", recursive=false, exclude_patterns=[]` | skips `.git`, venvs, `node_modules`, caches and build dirs; max 500 entries |
| `read_file` | READ | `path, start_line?, end_line?` | line-numbered output; refuses sensitive files, binaries and files over 1 MB |
| `search_code` | READ | `query, directory=".", file_pattern?, regex=false, case_sensitive=false, context_lines=0` | grep-style `path:N:` (match) / `path-N-` (context); skips sensitive and binary files; max 200 lines |
| `get_project_info` | READ | `directory="."` | languages, package managers, frameworks, test framework and command, git status (Build 003) |
| `parse_stack_trace` | READ | `text, include_code=true` | Python, JS, Java/Kotlin, Go, Rust, Ruby traces → frames mapped to project files + code at the innermost project frame (Build 003) |
| `inspect_logs` | READ | `path?, pattern, context_lines=2, tail_lines=40, max_matches=40` | no path: list log files, newest first; with a path: matching lines with context + the tail; scans the newest 5 MB (Build 003) |

All tool output is redacted for credential patterns before it reaches the model (ADR-017).

Orchestrator built-ins (names reserved; they act on task state, not on the machine):
`record_hypothesis` (`statement?, status, evidence[], id?`) and `finish_task` (see AGENT_LOOP.md).

## Tools that change things (Build 004)

| Tool | Permission (effective, from `assess`) | Inputs | Behavior |
|---|---|---|---|
| `edit_file` | WRITE | `path, expected_original, replacement, replace_all=false` | exact replace; must match once (or `replace_all`); closest-match hint on a miss; CRLF/BOM/trailing newline preserved; returns a diff |
| `apply_patch` | WRITE (DESTRUCTIVE if it deletes a file) | `patch` (unified diff) | multi-file, all-or-nothing; line numbers are hints, context must match (exactly, then ignoring trailing whitespace); `/dev/null` creates/deletes |
| `create_file` | WRITE | `path, content` | fails if the file exists |
| `delete_file` | DESTRUCTIVE | `path` | always asks; backed up first |
| `run_command` | from `classify()`: READ / EXECUTE / DESTRUCTIVE / blocked | `command, working_directory=".", timeout_seconds?` | no shell; timeout + process-tree kill; runs inside the write guard; reports files it changed |
| `run_tests` | same as `run_command` (the detected test command is READ) | `test_command?, working_directory, timeout_seconds?` | default = detected/configured test command; summary for pytest, jest, vitest, mocha, unittest, cargo, go, maven; produces **verification evidence** |
| `inspect_git` | READ | `operation (status, diff, diff_staged, log, show, blame), path?, revision?, max_count, start_line?, end_line?` | read-only git; offered only inside a repository |

Shared rules for the editing tools:
- **Only offered when a checkpoint store exists**: no change Trace couldn't undo.
- Never `.git/` internals or sensitive files; project boundary enforced.
- Files with uncommitted user changes, or whose change was already denied, force an approval prompt
  (even in auto mode).
- Writes are planned first, backed up, written atomically and read back to verify.
- A call that is certain to fail (text not found, ambiguous match) is reported to the model without
  asking the user to approve it.

## Tools that change things (Build 004)

| Tool | Permission (effective, from `assess`) | Inputs | Behavior |
|---|---|---|---|
| `edit_file` | WRITE | `path, expected_original, replacement, replace_all=false` | exact replace; must match once (or `replace_all`); closest-match hint on a miss; CRLF/BOM/trailing newline preserved; returns a diff |
| `apply_patch` | WRITE (DESTRUCTIVE if it deletes a file) | `patch` (unified diff) | multi-file, all-or-nothing; line numbers are hints, context must match (exactly, then ignoring trailing whitespace); `/dev/null` creates/deletes |
| `create_file` | WRITE | `path, content` | fails if the file exists |
| `delete_file` | DESTRUCTIVE | `path` | always asks; backed up first |
| `run_command` | from `classify()`: READ / EXECUTE / DESTRUCTIVE / blocked | `command, working_directory=".", timeout_seconds?` | no shell; timeout + process-tree kill; runs inside the write guard; reports files it changed |
| `run_tests` | same as `run_command` (the detected test command is READ) | `test_command?, working_directory, timeout_seconds?` | default = detected/configured test command; summary for pytest, jest, vitest, mocha, unittest, cargo, go, maven; produces **verification evidence** |
| `inspect_git` | READ | `operation (status, diff, diff_staged, log, show, blame), path?, revision?, max_count, start_line?, end_line?` | read-only git; offered only inside a repository |

Shared rules for the editing tools:
- **Only offered when a checkpoint store exists**: no change Trace couldn't undo.
- Never `.git/` internals or sensitive files; project boundary enforced.
- Files with uncommitted user changes, or whose change was already denied, force an approval prompt
  (even in auto mode).
- Writes are planned first, backed up, written atomically and read back to verify.
- A call that is certain to fail (text not found, ambiguous match) is reported to the model without
  asking the user to approve it.

## Tools that change things (Build 004)

| Tool | Permission (effective, from `assess`) | Inputs | Behavior |
|---|---|---|---|
| `edit_file` | WRITE | `path, expected_original, replacement, replace_all=false` | exact replace; must match once (or `replace_all`); closest-match hint on a miss; CRLF/BOM/trailing newline preserved; returns a diff |
| `apply_patch` | WRITE (DESTRUCTIVE if it deletes a file) | `patch` (unified diff) | multi-file, all-or-nothing; line numbers are hints, context must match (exactly, then ignoring trailing whitespace); `/dev/null` creates/deletes |
| `create_file` | WRITE | `path, content` | fails if the file exists |
| `delete_file` | DESTRUCTIVE | `path` | always asks; backed up first |
| `run_command` | from `classify()`: READ / EXECUTE / DESTRUCTIVE / blocked | `command, working_directory=".", timeout_seconds?` | no shell; timeout + process-tree kill; runs inside the write guard; reports files it changed |
| `run_tests` | same as `run_command` (the detected test command is READ) | `test_command?, working_directory, timeout_seconds?` | default = detected/configured test command; summary for pytest, jest, vitest, mocha, unittest, cargo, go, maven; produces **verification evidence** |
| `inspect_git` | READ | `operation (status, diff, diff_staged, log, show, blame), path?, revision?, max_count, start_line?, end_line?` | read-only git; offered only inside a repository |

Shared rules for the editing tools:
- **Only offered when a checkpoint store exists**: no change Trace couldn't undo.
- Never `.git/` internals or sensitive files; project boundary enforced.
- Files with uncommitted user changes, or whose change was already denied, force an approval prompt
  (even in auto mode).
- Writes are planned first, backed up, written atomically and read back to verify.
- A call that is certain to fail (text not found, ambiguous match) is reported to the model without
  asking the user to approve it.

## Planned tools

All ten tools from the original specification are implemented. Candidates for later phases:
a tool to create a debugging branch, and opt-in commits after approval (spec: git `future_features`).
