"""Credential storage interface and API-key resolution.

Keys are looked up per *profile name* (so two OpenRouter profiles can hold two
different keys). Resolution order:

1. the environment variable named by the profile (``api_key_env``) or, failing
   that, the provider's conventional variable (e.g. ``GROQ_API_KEY``);
2. the credential store (OS keyring in Phase 1).

Environment first is the common convention (CI, containers, one-off overrides).
Phase 0 ships the :class:`CredentialStore` protocol and an in-memory store; the
keyring-backed store is Phase 1. Keys are returned as ``SecretStr`` only.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import SecretStr

from trace_agent.providers.registry import ApiKeyRequirement
from trace_agent.storage.config import ProviderProfile


@runtime_checkable
class CredentialStore(Protocol):
    def get(self, profile_name: str) -> str | None: ...

    def set(self, profile_name: str, secret: str) -> None: ...

    def delete(self, profile_name: str) -> bool: ...


class InMemoryCredentialStore:
    """Non-persistent store, for tests and embedding."""

    def __init__(self, initial: Mapping[str, str] | None = None) -> None:
        self._secrets: dict[str, str] = dict(initial or {})

    def get(self, profile_name: str) -> str | None:
        return self._secrets.get(profile_name)

    def set(self, profile_name: str, secret: str) -> None:
        if not secret:
            raise ValueError("secret must not be empty")
        self._secrets[profile_name] = secret

    def delete(self, profile_name: str) -> bool:
        return self._secrets.pop(profile_name, None) is not None


class CredentialSource(StrEnum):
    ENVIRONMENT = "environment"
    STORE = "store"
    NOT_REQUIRED = "not_required"  # provider never uses a key
    ABSENT_OPTIONAL = "absent_optional"  # provider may use a key; none set
    MISSING = "missing"  # provider requires a key; none found


@dataclass(frozen=True)
class ResolvedCredential:
    source: CredentialSource
    api_key: SecretStr | None = None
    env_var: str | None = None  # which variable was consulted, for user-facing hints

    def __repr__(self) -> str:  # belt and braces: never print the key
        return f"ResolvedCredential(source={self.source.value!r}, env_var={self.env_var!r})"


def resolve_credential(
    profile_name: str,
    profile: ProviderProfile,
    store: CredentialStore | None = None,
    environ: Mapping[str, str] | None = None,
) -> ResolvedCredential:
    env = os.environ if environ is None else environ
    requirement = profile.spec().api_key
    env_var = profile.effective_api_key_env

    if requirement is ApiKeyRequirement.NONE and profile.api_key_env is None:
        return ResolvedCredential(CredentialSource.NOT_REQUIRED)

    if env_var:
        value = env.get(env_var, "").strip()
        if value:
            return ResolvedCredential(CredentialSource.ENVIRONMENT, SecretStr(value), env_var)

    if store is not None:
        value = (store.get(profile_name) or "").strip()
        if value:
            return ResolvedCredential(CredentialSource.STORE, SecretStr(value), env_var)

    if requirement is ApiKeyRequirement.REQUIRED:
        return ResolvedCredential(CredentialSource.MISSING, None, env_var)
    return ResolvedCredential(CredentialSource.ABSENT_OPTIONAL, None, env_var)
