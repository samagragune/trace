"""Credential redaction for anything Trace persists or displays.

Two layers:
1. *Known* secrets (e.g. the resolved API key) are replaced verbatim. This is exact and catches any format.
2. *Patterns* for common key formats catch secrets Trace never saw (e.g. one printed by a test).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

REDACTED = "[REDACTED]"

DEFAULT_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),  # Google API keys
    re.compile(r"\bgsk_[0-9A-Za-z]{20,}"),  # Groq
    re.compile(r"\bsk-or-v1-[0-9a-f]{32,}"),  # OpenRouter
    re.compile(r"\bsk-(?:proj-|ant-)?[0-9A-Za-z_\-]{20,}"),  # OpenAI / Anthropic style
    re.compile(r"\bgh[pousr]_[0-9A-Za-z]{30,}"),  # GitHub tokens
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(bearer)\s+[0-9A-Za-z._\-]{20,}"),
)

_MIN_KNOWN_SECRET_LEN = 8  # never "redact" trivially short strings like "a"


class Redactor:
    def __init__(
        self,
        known_secrets: Iterable[str] = (),
        patterns: tuple[re.Pattern[str], ...] = DEFAULT_SECRET_PATTERNS,
    ) -> None:
        self._known = sorted({s for s in known_secrets if s and len(s) >= _MIN_KNOWN_SECRET_LEN}, key=len, reverse=True)
        self._patterns = patterns

    def add_secret(self, secret: str | None) -> None:
        if secret and len(secret) >= _MIN_KNOWN_SECRET_LEN and secret not in self._known:
            self._known.append(secret)
            self._known.sort(key=len, reverse=True)

    def text(self, value: str) -> str:
        for secret in self._known:
            value = value.replace(secret, REDACTED)
        for pattern in self._patterns:
            value = pattern.sub(REDACTED, value)
        return value

    def value(self, value: Any) -> Any:
        """Recursively redact strings inside dicts, lists and tuples."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {k: self.value(v) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return type(value)(self.value(v) for v in value)
        return value
