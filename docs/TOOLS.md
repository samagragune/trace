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
| `search_code` | READ | `query, directory=".", file_pattern?, regex=false, case_sensitive=false` | `path:line: text`; skips sensitive and binary files; max 200 matches |

Plus the orchestrator's built-in `finish_task` (see AGENT_LOOP.md). Its name is reserved.

## Planned tools

| Tool | Permission | Phase | Notes |
|---|---|---|---|
| `get_project_info` | READ | 3 | |
| `inspect_git` | READ | 4 | hidden if git is unavailable |
| `create_file` | WRITE | 4 | |
| `edit_file` | WRITE | 4 | `expected_original` must match; checkpoint first |
| `run_tests` | EXECUTE | 4 | results feed `task.add_verification` |
| `run_command` | EXECUTE | 4 | risk classifier may escalate to DESTRUCTIVE |
| `delete_file` | DESTRUCTIVE | 4 | always asks, except in plan mode, where it is denied |
