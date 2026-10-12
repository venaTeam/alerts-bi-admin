"""Structured logging.

Deliberately narrow. Design section "Configuration and security" forbids alert documents,
credentials and complete LLM payloads in ordinary logs: log identifiers, hashes, counts,
timings and redacted errors only. Auditable payloads belong in SQL.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from typing import Any

__all__ = ["log", "redact_error"]

_LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}
_THRESHOLD = _LEVELS.get(os.environ.get("LOG_LEVEL", "info").lower(), 20)

# Secret-ish keys that must never reach stdout.
_REDACT_KEYS = {
    "password",
    "apikey",
    "api_key",
    "authorization",
    "token",
    "secret",
    "connectionstring",
    "connection_string",
}


def _redact(fields: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in fields.items():
        normalized = "".join(ch for ch in key.lower() if ch.isalpha() or ch == "_")
        out[key] = "[redacted]" if normalized in _REDACT_KEYS else value
    return out


def _emit(level: str, event: str, fields: dict[str, Any]) -> None:
    if _LEVELS[level] < _THRESHOLD:
        return
    line = {
        "ts": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "level": level,
        "event": event,
        **_redact(fields),
    }
    stream = sys.stderr if level in ("warn", "error") else sys.stdout
    stream.write(json.dumps(line, default=str) + "\n")


class _Logger:
    """Tiny façade so call sites read as ``log.info("event", key=value)``."""

    @staticmethod
    def debug(event: str, **fields: Any) -> None:
        _emit("debug", event, fields)

    @staticmethod
    def info(event: str, **fields: Any) -> None:
        _emit("info", event, fields)

    @staticmethod
    def warn(event: str, **fields: Any) -> None:
        _emit("warn", event, fields)

    @staticmethod
    def error(event: str, **fields: Any) -> None:
        _emit("error", event, fields)


log = _Logger()


def redact_error(err: BaseException | None) -> str:
    """Reduce an exception to a short, non-leaking summary for logs and SQL columns."""
    if err is None:
        return "unknown error"
    message = str(err)[:300]
    return f"{type(err).__name__}: {message}"
