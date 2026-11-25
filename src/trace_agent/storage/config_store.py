"""Load and save :class:`TraceConfig` as TOML, safely.

* Reads with stdlib ``tomllib``; writes with ``tomli_w``.
* Saves atomically (write a temp file in the same directory, then ``os.replace``),
  so a crash mid-write never leaves a truncated config behind.
* All parse/validation failures surface as :class:`ConfigError` with a readable message.
"""

from __future__ import annotations

import os
import tempfile
import tomllib
from pathlib import Path

import tomli_w
from pydantic import ValidationError

from trace_agent.storage.config import ConfigError, ConfigNotFoundError, TraceConfig, format_validation_error


class ConfigStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> TraceConfig:
        if not self.exists():
            raise ConfigNotFoundError(f"no configuration found at {self.path}")
        try:
            with self.path.open("rb") as fh:
                raw = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{self.path} is not valid TOML: {exc}") from None
        except OSError as exc:
            raise ConfigError(f"cannot read {self.path}: {exc}") from None
        try:
            return TraceConfig.model_validate(raw)
        except ValidationError as exc:
            raise ConfigError(f"{self.path} is invalid:\n{format_validation_error(exc)}") from None

    def load_or_default(self) -> TraceConfig:
        return self.load() if self.exists() else TraceConfig()

    def save(self, config: TraceConfig) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = config.model_dump(mode="json", exclude_none=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".config-", suffix=".toml", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as fh:
                tomli_w.dump(data, fh)
            os.replace(tmp_name, self.path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
