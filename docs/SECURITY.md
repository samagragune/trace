# Security

Core rule: **a model tool call is never authorization by itself.**

## Implemented ✅

### Authorization policy (`safety/permissions.py`)

| | READ | WRITE | EXECUTE | DESTRUCTIVE |
|---|---|---|---|---|
| **plan** | allow | deny | ask | deny |
| **confirm** (default) | allow | ask | ask | ask |
| **auto** | allow | allow | allow | ask |

The policy is a pure, fully tested table. In `confirm` mode every command currently asks, because
command risk classification does not exist yet (Phase 4).

### Project boundary (`safety/paths.py`)
`resolve_in_project(root, path)` resolves symlinks and `..` **before** checking containment, so the
following are all rejected:
- relative traversal (`../x`)
- absolute paths outside the root
- symlinks that point outside the root
- paths on another Windows drive
- empty paths and paths containing null bytes

Every filesystem tool must use it.

### Sensitive files
`is_sensitive_path` flags `.env*`, private keys (`*.pem`, `*.key`, `id_rsa*`, ...), `.netrc`,
`.npmrc`, `.pypirc`, `credentials.json` and `secrets.*`. Phase 2 tools will require explicit
approval before sending these to a model.

### Credentials
- Never stored in `config.toml`. Unknown fields and credential-like `options` keys are rejected
  when config is loaded and when it is saved.
- Held only as `SecretStr` (masked in `repr`, `str` and `model_dump`). `ResolvedCredential.__repr__`
  omits the key.
- `trace status` reports *where* a key was found, never its value (tested).
- No key is stored in SQLite (no SQLite yet; this rule applies when it arrives).

### Test isolation
The test suite redirects `TRACE_HOME` and clears provider key variables for every test, so tests
cannot read or modify a developer's real configuration or keys.

### Redaction ✅ (Build 002)
`safety/redaction.py` replaces the active API key verbatim, plus common key formats (Google `AIza…`,
Groq `gsk_…`, OpenRouter, `sk-…`, GitHub, AWS, PEM private keys, bearer tokens), with `[REDACTED]`.
`RedactingSink` applies it to **every** event before it reaches the terminal or `history.db`.
Provider error messages are redacted by the adapter as well.

### Approvals ✅ (Build 002)
Actions the policy marks ASK prompt in the terminal (default: **no**). When stdin isn't a
terminal (CI, pipes), every ASK becomes a denial (`DenyAllApprover`). Denials are reported to the
model as `status: denied` so it can change approach.

### Project secrets are not sent to the model (Build 003)
Tool output is redacted for credential patterns before it reaches the model, which is told when that happens (ADR-017). The opening context is redacted too, and never reads sensitive files.

### Read-only agent
Build 002's tools are read-only. `read_file` refuses sensitive files, `search_code` skips them, and
both enforce the project boundary.

### Code changes and commands ✅ (Build 004)
- **Backups and undo (ADR-030).** The first change to any file is preceded by a backup;
  `trace undo` restores everything, and never overwrites a file the user edited afterwards unless
  `--force`. Without a checkpoint store, editing tools aren't offered.
- **Uncommitted user work (ADR-032).** Files with uncommitted git changes at task start need
  explicit approval to change, even in auto mode; without a terminal they are denied.
- **Commands (ADR-031).** No shell. Classified before authorization: SAFE (tests, linters, read-only
  git) runs; NORMAL asks in confirm and plan; DESTRUCTIVE always asks; BLOCKED never runs (`sudo`,
  disk tools, `git push`, `npm publish`, nested shells).
- **Binding denials: the write guard (ADR-033).** Every command runs with guarded files (uncommitted
  work + denied files) backed up and the project fingerprinted; guarded files changed by a command
  are restored and the attempt counts as a violation (3 → `SAFETY_VIOLATION`). Found and fixed live:
  the model tried to bypass a denied edit with `python -c`.
- **Process hygiene.** Timeouts kill the whole process tree; Ctrl-C cancels running commands;
  stdin is closed; Trace's provider key variables are removed from every child environment.
- **Verification can't be faked (ADR-029).** "Fixed" requires Trace's own passing test run after the
  last change.

## Planned 🔜

| Control | Phase |
|---|---|
| OS keyring credential store | 1 (deferred) |
| Sandboxing commands (outside-project files, network) | 6 (P-9) |
| Undo for files changed by commands | later |

## Known risk
Prompt injection: repository content read by Trace is sent to the model and could contain
instructions. The defense is structural. The model can only *propose* actions, and the policy above
decides what runs. Phase 2 should also mark tool output clearly as untrusted data in prompts.
