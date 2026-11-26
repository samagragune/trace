"""Where Trace keeps its local files.

Default home is ``~/.trace/``. The ``TRACE_HOME`` environment variable overrides
it, which tests use to guarantee they never touch a real user's configuration.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

TRACE_HOME_ENV = "TRACE_HOME"


@dataclass(frozen=True)
class TracePaths:
    home: Path

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> TracePaths:
        env = os.environ if environ is None else environ
        override = env.get(TRACE_HOME_ENV)
        home = Path(override).expanduser() if override else Path.home() / ".trace"
        return cls(home=home.resolve())

    @property
    def config_file(self) -> Path:
        return self.home / "config.toml"

    @property
    def database(self) -> Path:
        return self.home / "history.db"

    @property
    def cache_dir(self) -> Path:
        return self.home / "cache"

    @property
    def log_dir(self) -> Path:
        return self.home / "logs"

    @property
    def checkpoint_dir(self) -> Path:
        """Per-task backups of files Trace changed (used by `trace undo`)."""
        return self.cache_dir / "checkpoints"

    def ensure(self) -> None:
        for directory in (self.home, self.cache_dir, self.log_dir):
            directory.mkdir(parents=True, exist_ok=True)
