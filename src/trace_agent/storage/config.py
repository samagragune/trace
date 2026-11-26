"""The persisted (non-secret) configuration model, stored as ``config.toml``.

Provider configuration is profile-based: a user can define any number of named
profiles (e.g. ``groq-fast``, ``local``, ``work-gateway``), each pointing at a
provider type from the registry with its own model, base URL, timeouts and
free-form provider options, and switch between them with ``active_profile``.

Secrets never live here. ``extra="forbid"`` rejects unknown keys such as an
``api_key`` typed into the TOML file, and ``options`` rejects credential-like keys.

Example::

    schema_version = 1
    active_profile = "groq"

    [profiles.groq]
    provider = "groq"
    model = "llama-3.3-70b-versatile"

    [profiles.lmstudio]
    provider = "openai_compatible"
    model = "qwen2.5-coder-14b"
    base_url = "http://localhost:1234/v1"
    options = { temperature = 0.2 }

    [agent]
    execution_mode = "confirm"
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from trace_agent.errors import TraceError
from trace_agent.providers.errors import UnknownProviderError
from trace_agent.providers.registry import DEFAULT_PROVIDERS, ProviderSpec
from trace_agent.safety.permissions import DEFAULT_EXECUTION_MODE, ExecutionMode

CONFIG_SCHEMA_VERSION = 1
PROFILE_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"
ENV_VAR_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"

#: Option keys that would mean a secret is being written to a plain-text file.
FORBIDDEN_OPTION_KEYS = frozenset(
    {"api_key", "apikey", "key", "secret", "password", "token", "access_token", "authorization", "auth"}
)


def format_validation_error(exc: ValidationError) -> str:
    """Render a pydantic error as readable bullet lines (no internal type codes or URLs)."""
    lines = []
    for err in exc.errors():
        location = ".".join(str(part) for part in err["loc"]) or "<root>"
        message = err["msg"].removeprefix("Value error, ")
        lines.append(f"  - {location}: {message}")
    return "\n".join(lines)


class ConfigError(TraceError):
    """The configuration is missing, unreadable or invalid."""


class ConfigNotFoundError(ConfigError):
    pass


class ProviderProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(description="Provider id from the registry, e.g. 'groq' or 'openai_compatible'.")
    model: str = Field(min_length=1)
    base_url: str | None = Field(default=None, description="Overrides the provider's default endpoint.")
    api_key_env: str | None = Field(
        default=None,
        pattern=ENV_VAR_PATTERN,
        description="Environment variable to read the key from, overriding the provider's convention.",
    )
    timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    max_retries: int = Field(default=3, ge=0, le=10)
    options: dict[str, Any] = Field(
        default_factory=dict, description="Provider-specific parameters passed through to the adapter."
    )

    @field_validator("model")
    @classmethod
    def _strip_model(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("model must not be blank")
        return value

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        return value

    @field_validator("options")
    @classmethod
    def _no_secrets_in_options(cls, value: dict[str, Any]) -> dict[str, Any]:
        bad = sorted(k for k in value if k.lower() in FORBIDDEN_OPTION_KEYS)
        if bad:
            raise ValueError(
                f"options must not contain credentials ({', '.join(bad)}); "
                "use an environment variable or the credential store instead"
            )
        return value

    @model_validator(mode="after")
    def _check_against_spec(self) -> ProviderProfile:
        spec = self.spec()
        if spec.base_url_required and not self.base_url:
            raise ValueError(f"provider '{spec.id}' requires base_url")
        return self

    def spec(self) -> ProviderSpec:
        try:
            return DEFAULT_PROVIDERS.get(self.provider)
        except UnknownProviderError as exc:
            raise ValueError(str(exc)) from None

    @property
    def effective_base_url(self) -> str | None:
        return self.base_url or self.spec().default_base_url

    @property
    def effective_api_key_env(self) -> str | None:
        return self.api_key_env or self.spec().api_key_env


class AgentSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    execution_mode: ExecutionMode = DEFAULT_EXECUTION_MODE
    max_iterations: int = Field(default=30, ge=1, le=200)
    max_retry_attempts: int = Field(
        default=3, ge=0, le=10, description="Failed post-edit verifications allowed before the task stops."
    )
    test_command: str | None = Field(
        default=None, description="Override the detected test command used for verification."
    )
    command_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    max_output_chars: int = Field(default=20_000, ge=1_000, le=200_000)
    system_prompt_file: str | None = Field(
        default=None,
        description="Path to a custom system prompt (the agent 'brain'). Replaces the built-in default.",
    )


class StorageSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    history_retention_days: int | None = Field(default=90, ge=1, description="None keeps history forever.")


class TraceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = CONFIG_SCHEMA_VERSION
    active_profile: str | None = None
    profiles: dict[str, ProviderProfile] = Field(default_factory=dict)
    agent: AgentSettings = AgentSettings()
    storage: StorageSettings = StorageSettings()

    @field_validator("schema_version")
    @classmethod
    def _supported_version(cls, value: int) -> int:
        if value > CONFIG_SCHEMA_VERSION:
            raise ValueError(
                f"config schema_version {value} is newer than this Trace supports ({CONFIG_SCHEMA_VERSION}); "
                "upgrade Trace"
            )
        if value < 1:
            raise ValueError("schema_version must be >= 1")
        return value

    @field_validator("profiles")
    @classmethod
    def _profile_names(cls, value: dict[str, ProviderProfile]) -> dict[str, ProviderProfile]:
        import re

        bad = [name for name in value if not re.fullmatch(PROFILE_NAME_PATTERN, name)]
        if bad:
            raise ValueError(f"invalid profile name(s): {', '.join(bad)}")
        return value

    @model_validator(mode="after")
    def _active_exists(self) -> TraceConfig:
        if self.active_profile is not None and self.active_profile not in self.profiles:
            raise ValueError(f"active_profile '{self.active_profile}' is not defined in [profiles]")
        return self

    # ---- immutable update helpers (return new configs) --------------------

    def with_profile(self, name: str, profile: ProviderProfile, *, activate: bool = False) -> TraceConfig:
        profiles = {**self.profiles, name: profile}
        active = name if (activate or self.active_profile is None) else self.active_profile
        return TraceConfig.model_validate(
            {**self.model_dump(), "profiles": profiles, "active_profile": active}
        )

    def without_profile(self, name: str) -> TraceConfig:
        if name not in self.profiles:
            raise ConfigError(f"no profile named '{name}'")
        profiles = {k: v for k, v in self.profiles.items() if k != name}
        active = None if self.active_profile == name else self.active_profile
        return TraceConfig.model_validate({**self.model_dump(), "profiles": profiles, "active_profile": active})

    def with_active(self, name: str) -> TraceConfig:
        if name not in self.profiles:
            raise ConfigError(f"no profile named '{name}'")
        return self.model_copy(update={"active_profile": name})

    def with_execution_mode(self, mode: ExecutionMode) -> TraceConfig:
        return self.model_copy(update={"agent": self.agent.model_copy(update={"execution_mode": mode})})

    def get_profile(self, name: str | None = None) -> tuple[str, ProviderProfile]:
        """Return (name, profile) for ``name`` or the active profile."""
        target = name or self.active_profile
        if target is None:
            raise ConfigError("no provider profile is active. Create one with `trace config set-profile`")
        try:
            return target, self.profiles[target]
        except KeyError:
            raise ConfigError(f"no profile named '{target}'") from None
