"""Wiring: build a ready-to-run orchestrator from configuration.

This is the composition root. It is the only place that knows about every module
(config, credentials, provider factory, tools, history, terminal). Each module stays
independent; this file connects them.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

from trace_agent.agent.executor import Approver
from trace_agent.agent.orchestrator import Orchestrator, OrchestratorSettings
from trace_agent.agent.prompts import load_system_prompt
from trace_agent.agent.state import AgentTask
from trace_agent.cli.render import ConsoleEventPrinter
from trace_agent.observability.sinks import CompositeSink, RedactingSink
from trace_agent.providers.base import ModelProvider
from trace_agent.providers.factory import DEFAULT_FACTORY, ProviderFactory
from trace_agent.safety.permissions import ExecutionMode
from trace_agent.safety.redaction import Redactor
from trace_agent.storage.config import TraceConfig
from trace_agent.storage.credentials import CredentialStore
from trace_agent.storage.history import HistoryStore
from trace_agent.storage.paths import TracePaths
from trace_agent.storage.resolve import resolve_provider_settings
from trace_agent.tools.builtin import default_registry


@dataclass
class RunOptions:
    project_root: Path
    mode: ExecutionMode | None = None
    profile: str | None = None
    max_iterations: int | None = None
    verbose: bool = False


def build_provider(
    config: TraceConfig,
    *,
    profile: str | None = None,
    store: CredentialStore | None = None,
    factory: ProviderFactory = DEFAULT_FACTORY,
) -> ModelProvider:
    return factory.create(resolve_provider_settings(config, store, profile_name=profile))


def run_task(
    description: str,
    config: TraceConfig,
    options: RunOptions,
    *,
    console: Console,
    approver: Approver,
    provider: ModelProvider | None = None,
    paths: TracePaths | None = None,
) -> AgentTask:
    paths = paths or TracePaths.from_env()
    provider = provider or build_provider(config, profile=options.profile)
    agent_cfg = config.agent
    mode = options.mode or agent_cfg.execution_mode

    redactor = Redactor()
    if provider.settings.api_key is not None:
        redactor.add_secret(provider.settings.api_key.get_secret_value())

    history = HistoryStore(paths.database)
    history.prune(config.storage.history_retention_days)
    sink = RedactingSink(CompositeSink([ConsoleEventPrinter(console, verbose=options.verbose), history]), redactor)

    orchestrator = Orchestrator(
        provider,
        default_registry(),
        settings=OrchestratorSettings(
            mode=mode,
            max_iterations=options.max_iterations or agent_cfg.max_iterations,
            max_output_chars=agent_cfg.max_output_chars,
            tool_timeout_seconds=agent_cfg.command_timeout_seconds,
        ),
        sink=sink,
        approver=approver,
        system_prompt=load_system_prompt(agent_cfg.system_prompt_file),
        redactor=redactor,
    )
    task = orchestrator.run(description, options.project_root)
    history.save_task(task, provider=provider.provider_id, model=provider.model, mode=mode.value)
    return task
