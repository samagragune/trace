"""The ``trace`` command.

Commands: ``trace`` (interactive session), ``run``, ``status``, ``history``, ``config ...``.
Commands: ``undo`` restores files changed by a task (Build 004).
Not yet implemented, and therefore not registered: ``init`` wizard, keyring key storage
(``set-key``/``remove-key``) and ``reset``.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.markup import escape

from trace_agent import __version__
from trace_agent.agent.executor import DenyAllApprover
from trace_agent.agent.state import AgentTask, TaskStatus
from trace_agent.cli.session import RunOptions
from trace_agent.errors import TraceError
from trace_agent.providers.registry import DEFAULT_PROVIDERS
from trace_agent.safety.permissions import ExecutionMode
from trace_agent.storage.config import ProviderProfile, TraceConfig, format_validation_error
from trace_agent.storage.config_store import ConfigStore
from trace_agent.storage.credentials import CredentialSource, resolve_credential
from trace_agent.storage.paths import TracePaths

console = Console(highlight=False)
err_console = Console(stderr=True, highlight=False)

app = typer.Typer(
    name="trace",
    help="Describe what's broken. Trace investigates, fixes your code, and verifies the solution.",
    add_completion=False,
    rich_markup_mode=None,
)
config_app = typer.Typer(help="Inspect and change Trace configuration.", no_args_is_help=True)
app.add_typer(config_app, name="config")


# --------------------------------------------------------------------------- helpers


def _store() -> ConfigStore:
    return ConfigStore(TracePaths.from_env().config_file)


def _fail(message: str) -> typer.Exit:
    err_console.print(f"[red]Error:[/red] {escape(message)}", soft_wrap=True)
    return typer.Exit(code=1)


def _load(store: ConfigStore) -> TraceConfig:
    try:
        return store.load_or_default()
    except TraceError as exc:
        raise _fail(str(exc)) from None


def _save(store: ConfigStore, config: TraceConfig) -> None:
    TracePaths.from_env().ensure()
    store.save(config)


def _parse_option(raw: str) -> tuple[str, Any]:
    if "=" not in raw:
        raise _fail(f"--option must look like key=value, got '{raw}'")
    key, value = raw.split("=", 1)
    key = key.strip()
    if not key:
        raise _fail(f"--option has an empty key: '{raw}'")
    try:
        parsed: Any = json.loads(value)  # numbers, booleans, lists, objects
    except json.JSONDecodeError:
        parsed = value  # plain string
    return key, parsed


_CREDENTIAL_LABEL = {
    CredentialSource.ENVIRONMENT: "[green]found in environment[/green] ({env})",
    CredentialSource.STORE: "[green]found in credential store[/green]",
    CredentialSource.NOT_REQUIRED: "not required",
    CredentialSource.ABSENT_OPTIONAL: "none (optional for this provider)",
    CredentialSource.MISSING: "[red]missing[/red] (set {env})",
}


def _credential_text(name: str, profile: ProviderProfile) -> str:
    # Phase 0 has no persistent credential store yet; only the environment is consulted.
    resolved = resolve_credential(name, profile, store=None)
    return _CREDENTIAL_LABEL[resolved.source].format(env=resolved.env_var or "an API key")


# --------------------------------------------------------------------------- top level


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"trace {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def root(
    ctx: typer.Context,
    version: Annotated[
        bool, typer.Option("--version", callback=_version_callback, is_eager=True, help="Show version and exit.")
    ] = False,
) -> None:
    """Without a command: start an interactive debugging session in the current directory."""
    if ctx.invoked_subcommand is not None:
        return
    store = _store()
    config = _load(store)
    if config.active_profile is None:
        status()
        return
    console.print(f"Trace {__version__} | project: {escape(str(Path.cwd()))}", soft_wrap=True)
    console.print("[dim]Describe what's broken. Empty line or 'exit' to quit.[/dim]")
    while True:
        try:
            description = console.input("\n[bold]trace>[/bold] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return
        if description.lower() in ("", "exit", "quit"):
            return
        _execute(description, config, RunOptions(project_root=Path.cwd()))


def _execute(description: str, config: TraceConfig, options: RunOptions) -> AgentTask:
    from trace_agent.cli.render import TerminalApprover, render_final_report
    from trace_agent.cli.session import run_task

    approver = TerminalApprover(console) if sys.stdin.isatty() else DenyAllApprover()
    try:
        task = run_task(description, config, options, console=console, approver=approver)
    except TraceError as exc:
        raise _fail(str(exc)) from None
    console.print()
    render_final_report(console, task)
    return task


@app.command()
def run(
    description: Annotated[str, typer.Argument(help="What's broken, in plain language.")],
    project: Annotated[
        Path, typer.Option("--project", "-C", help="Project directory.", exists=True, file_okay=False)
    ] = Path("."),
    mode: Annotated[ExecutionMode | None, typer.Option(help="Override the execution mode.")] = None,
    profile: Annotated[str | None, typer.Option(help="Use this provider profile instead of the active one.")] = None,
    max_iterations: Annotated[int | None, typer.Option(min=1, max=200, help="Override the iteration limit.")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show model text, tool output and diffs.")] = False,
    test_command: Annotated[
        str | None, typer.Option("--test-command", help="Command used to verify fixes (default: detected).")
    ] = None,
) -> None:
    """Investigate a problem, fix it when the mode allows, verify, and print the result."""
    config = _load(_store())
    options = RunOptions(
        project_root=project.resolve(), mode=mode, profile=profile, max_iterations=max_iterations,
        verbose=verbose, test_command=test_command,
    )
    task = _execute(description, config, options)
    if task.status not in (TaskStatus.COMPLETED,):
        raise typer.Exit(code=2)


# --------------------------------------------------------------------------- undo


@app.command()
def undo(
    task_id: Annotated[str | None, typer.Argument(help="Task id or prefix (default: most recent task with changes).")] = None,
    force: Annotated[bool, typer.Option("--force", help="Also restore files you edited after Trace changed them.")] = False,
) -> None:
    """Restore every file a task changed to its state before the task."""
    from trace_agent.safety.checkpoints import CheckpointError, CheckpointStore

    root = TracePaths.from_env().checkpoint_dir
    candidates = CheckpointStore.list_all(root)
    if task_id:
        candidates = [d for d in candidates if d.name.startswith(task_id)]
        if len(candidates) > 1:
            raise _fail(f"'{task_id}' matches several tasks; use a longer prefix")
    else:
        candidates = [d for d in candidates if CheckpointStore.open(d).changed_files()][:1]
    if not candidates:
        raise _fail("no task with recorded changes found" + (f" for '{task_id}'" if task_id else ""))
    try:
        store = CheckpointStore.open(candidates[0])
    except CheckpointError as exc:
        raise _fail(str(exc)) from None
    changes = store.changed_files()
    if not changes:
        console.print(f"Task {store.task_id[:8]}: nothing to undo (files already match their originals).")
        return
    console.print(f"Undoing task {store.task_id[:8]} in {escape(str(store.project_root))}", soft_wrap=True)
    report = store.restore(force=force)
    for path in report.restored:
        console.print(f"  [green]restored[/green] {escape(path)}")
    for path in report.deleted:
        console.print(f"  [green]removed[/green]  {escape(path)} (created by Trace)")
    for path in report.skipped:
        console.print(f"  [yellow]skipped[/yellow]  {escape(path)}: changed after Trace edited it (use --force)")
    for error in report.errors:
        console.print(f"  [red]error[/red]    {escape(error)}")
    if report.skipped or report.errors:
        raise typer.Exit(code=1)


# --------------------------------------------------------------------------- history

history_app = typer.Typer(help="Inspect past debugging sessions.", invoke_without_command=True)
app.add_typer(history_app, name="history")


@history_app.callback()
def history_list(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option(min=1, max=500, help="How many tasks to show.")] = 20,
) -> None:
    """List recent tasks."""
    if ctx.invoked_subcommand is not None:
        return
    from trace_agent.storage.history import HistoryStore

    paths = TracePaths.from_env()
    if not paths.database.exists():
        console.print("No history yet.")
        return
    rows = HistoryStore(paths.database).list_tasks(limit)
    if not rows:
        console.print("No history yet.")
        return
    for row in rows:
        when = datetime.fromisoformat(row.created_at).astimezone().strftime("%Y-%m-%d %H:%M")  # local time
        console.print(
            f"[bold]{row.id[:8]}[/bold] {when} [{'green' if row.status == 'completed' else 'yellow'}]"
            f"{row.status}[/] ({row.stop_reason}) | {escape(row.model or '-')} | "
            f"{row.model_calls} calls | {escape(row.description[:70])}",
            soft_wrap=True,
        )


@history_app.command("show")
def history_show(
    task_id: Annotated[str, typer.Argument(help="Task id or unique prefix.")],
    full: Annotated[bool, typer.Option("--full", help="Print complete event payloads.")] = False,
) -> None:
    """Show the full execution trace of one task."""
    from trace_agent.storage.history import HistoryStore

    paths = TracePaths.from_env()
    if not paths.database.exists():
        raise _fail("no history yet")
    store = HistoryStore(paths.database)
    row = store.find_task(task_id)
    if row is None:
        raise _fail(f"no unique task matches '{task_id}'")
    console.print(f"[bold]Task {row.id}[/bold]  {row.status} ({row.stop_reason})", soft_wrap=True)
    console.print(f"Problem: {escape(row.description)}", soft_wrap=True)
    console.print(f"Model: {escape(str(row.provider))}/{escape(str(row.model))} | mode {row.mode}", soft_wrap=True)
    console.print(f"Summary: {escape(row.summary or '')}\n", soft_wrap=True)
    for event in store.events(row.id):
        payload = json.dumps(event.payload, default=str, ensure_ascii=False)
        if not full and len(payload) > 300:
            payload = payload[:300] + "..."
        console.print(f"[dim]{event.timestamp[11:19]}[/dim] #{event.iteration} [cyan]{event.type}[/cyan] {escape(payload)}", soft_wrap=True)


@app.command()
def status() -> None:
    """Show configuration location, active provider profile and credential status."""
    paths = TracePaths.from_env()
    store = ConfigStore(paths.config_file)
    console.print(f"Trace {__version__}")
    console.print(f"Config file: {escape(str(paths.config_file))}", soft_wrap=True)
    if not store.exists():
        console.print(
            "[yellow]Not configured.[/yellow] Create a provider profile, for example:\n"
            "  trace config set-profile groq --provider groq --model llama-3.3-70b-versatile",
            soft_wrap=True,
        )
        return
    config = _load(store)
    console.print(f"Execution mode: {config.agent.execution_mode.value}")
    if config.active_profile is None:
        console.print("[yellow]No active provider profile.[/yellow] Use `trace config use <name>`.")
        return
    name, profile = config.get_profile()
    spec = profile.spec()
    console.print(f"Active profile: [bold]{escape(name)}[/bold]")
    console.print(f"  Provider: {escape(spec.display_name)} ({spec.id})")
    console.print(f"  Model:    {escape(profile.model)}", soft_wrap=True)
    if profile.effective_base_url:
        console.print(f"  Endpoint: {escape(profile.effective_base_url)}", soft_wrap=True)
    console.print(f"  API key:  {_credential_text(name, profile)}", soft_wrap=True)


# --------------------------------------------------------------------------- config


@config_app.command("providers")
def config_providers() -> None:
    """List provider types that profiles can use."""
    # A list, not a table: ids, env vars and model names are meant to be copied,
    # and a table truncates them in narrow terminals.
    for spec in DEFAULT_PROVIDERS:
        console.print(f"[bold]{spec.id}[/bold]  {escape(spec.display_name)}", soft_wrap=True)
        key = spec.api_key.value + (f" (env: {spec.api_key_env})" if spec.api_key_env else "")
        console.print(f"    API key:  {key}", soft_wrap=True)
        if spec.default_base_url:
            console.print(f"    Endpoint: {spec.default_base_url}", soft_wrap=True)
        if spec.base_url_required:
            console.print("    Endpoint: required (--base-url)")
        if spec.suggested_models:
            console.print(f"    Models:   {escape(', '.join(spec.suggested_models))}", soft_wrap=True)
    console.print("\nModel names are suggestions; any model your provider serves is accepted.", soft_wrap=True)


@config_app.command("show")
def config_show() -> None:
    """Print the current configuration (contains no secrets)."""
    store = _store()
    if not store.exists():
        console.print(f"No configuration at {escape(str(store.path))}.", soft_wrap=True)
        return
    console.print(escape(store.path.read_text(encoding="utf-8")), soft_wrap=True)


@config_app.command("set-profile")
def config_set_profile(
    name: Annotated[str, typer.Argument(help="Profile name, e.g. 'groq' or 'local'.")],
    provider: Annotated[str, typer.Option("--provider", "-p", help="Provider id (see `trace config providers`).")],
    model: Annotated[str, typer.Option("--model", "-m", help="Model identifier as the provider names it.")],
    base_url: Annotated[str | None, typer.Option(help="Custom endpoint URL.")] = None,
    api_key_env: Annotated[
        str | None, typer.Option(help="Environment variable holding the API key (overrides provider default).")
    ] = None,
    timeout: Annotated[float, typer.Option(help="Request timeout in seconds.")] = 60.0,
    max_retries: Annotated[int, typer.Option(help="Retries for transient provider errors.")] = 3,
    option: Annotated[
        list[str] | None, typer.Option("--option", "-o", help="Provider option key=value (repeatable).")
    ] = None,
    activate: Annotated[bool, typer.Option(help="Make this the active profile.")] = True,
) -> None:
    """Create or replace a provider profile."""
    options = dict(_parse_option(raw) for raw in option or [])
    store = _store()
    config = _load(store)
    try:
        profile = ProviderProfile(
            provider=provider,
            model=model,
            base_url=base_url,
            api_key_env=api_key_env,
            timeout_seconds=timeout,
            max_retries=max_retries,
            options=options,
        )
        updated = config.with_profile(name, profile, activate=activate)
    except ValidationError as exc:
        raise _fail("invalid profile:\n" + format_validation_error(exc)) from None
    except TraceError as exc:
        raise _fail(str(exc)) from None
    _save(store, updated)
    verb = "Updated" if name in config.profiles else "Created"
    console.print(f"{verb} profile [bold]{escape(name)}[/bold] ({escape(provider)} / {escape(model)}).")
    if updated.active_profile == name:
        console.print(f"Active profile: {escape(name)}")
    console.print(f"API key: {_credential_text(name, profile)}", soft_wrap=True)


@config_app.command("use")
def config_use(name: Annotated[str, typer.Argument(help="Profile to activate.")]) -> None:
    """Switch the active provider profile."""
    store = _store()
    try:
        updated = _load(store).with_active(name)
    except TraceError as exc:
        raise _fail(str(exc)) from None
    _save(store, updated)
    console.print(f"Active profile: {escape(name)}")


@config_app.command("remove-profile")
def config_remove_profile(name: Annotated[str, typer.Argument(help="Profile to delete.")]) -> None:
    """Delete a provider profile."""
    store = _store()
    try:
        updated = _load(store).without_profile(name)
    except TraceError as exc:
        raise _fail(str(exc)) from None
    _save(store, updated)
    console.print(f"Removed profile {escape(name)}.")
    if updated.active_profile is None and updated.profiles:
        console.print("No profile is active now. Use `trace config use <name>`.")


@config_app.command("test")
def config_test(
    name: Annotated[str | None, typer.Argument(help="Profile to test (default: active profile).")] = None,
) -> None:
    """Send one tiny real request to check credentials, model and quota."""
    import time

    from trace_agent.cli.session import build_provider
    from trace_agent.providers.errors import (
        ModelNotFoundError,
        ProviderAuthError,
        ProviderError,
        ProviderRateLimitError,
    )

    config = _load(_store())
    try:
        provider = build_provider(config, profile=name)
    except TraceError as exc:
        raise _fail(str(exc)) from None
    console.print(
        f"Testing {escape(provider.settings.profile_name)}: {escape(provider.provider_id)} / "
        f"{escape(provider.model)} ...",
        soft_wrap=True,
    )
    provider.warm_up()  # exclude one-time local import cost from the reported latency
    started = time.perf_counter()
    try:
        with console.status("waiting for the model"):
            provider.check_connection()
    except ProviderRateLimitError as exc:
        wait = f" Retry after ~{exc.retry_after:.0f}s." if exc.retry_after else ""
        raise _fail(f"RATE LIMITED: credentials work but the quota is exhausted.{wait}\n{exc}") from None
    except ProviderAuthError as exc:
        raise _fail(f"AUTHENTICATION FAILED: {exc}") from None
    except ModelNotFoundError as exc:
        raise _fail(f"MODEL NOT FOUND: {exc}") from None
    except ProviderError as exc:
        raise _fail(f"FAILED ({type(exc).__name__}): {exc}") from None
    elapsed = time.perf_counter() - started
    caps = provider.capabilities()
    console.print(f"[green]OK[/green] in {elapsed:.1f}s. Tool calling: {'yes' if caps.tool_calling else 'NO'}")
    if not caps.tool_calling:
        console.print("[yellow]This model does not report tool-calling support; the agent needs it.[/yellow]")


@config_app.command("set-mode")
def config_set_mode(mode: Annotated[ExecutionMode, typer.Argument(help="plan, confirm or auto.")]) -> None:
    """Set the default execution mode."""
    store = _store()
    _save(store, _load(store).with_execution_mode(mode))
    console.print(f"Execution mode: {mode.value}")


def _harden_output_streams() -> None:
    """Never crash on characters the terminal can't encode (e.g. legacy cp1252 Windows consoles).

    Model output can contain any Unicode. Replacing unencodable characters with '?' is
    always better than aborting a debugging session in the middle.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except (ValueError, OSError):
                pass


def main() -> None:
    _harden_output_streams()
    app()
