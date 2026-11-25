"""Bridge from persisted configuration to runtime provider settings.

This is the one place where a non-secret profile and a secret credential meet.
The result, :class:`ProviderSettings`, is what the provider factory consumes,
so ``providers`` never needs to import ``storage``.
"""

from __future__ import annotations

from collections.abc import Mapping

from trace_agent.providers.base import ProviderSettings
from trace_agent.providers.errors import MissingCredentialError
from trace_agent.storage.config import TraceConfig
from trace_agent.storage.credentials import CredentialSource, CredentialStore, resolve_credential


def resolve_provider_settings(
    config: TraceConfig,
    store: CredentialStore | None = None,
    *,
    profile_name: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> ProviderSettings:
    name, profile = config.get_profile(profile_name)
    credential = resolve_credential(name, profile, store, environ)
    if credential.source is CredentialSource.MISSING:
        hint = f"set the {credential.env_var} environment variable" if credential.env_var else "store a key"
        raise MissingCredentialError(
            f"profile '{name}' ({profile.spec().display_name}) needs an API key: {hint}.",
            provider=profile.provider,
        )
    return ProviderSettings(
        profile_name=name,
        provider=profile.provider,
        model=profile.model,
        base_url=profile.effective_base_url,
        api_key=credential.api_key,
        timeout_seconds=profile.timeout_seconds,
        max_retries=profile.max_retries,
        options=dict(profile.options),
    )
