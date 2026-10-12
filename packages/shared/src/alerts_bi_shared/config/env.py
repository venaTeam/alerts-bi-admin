"""Reading configuration out of the environment.

Configuration never lives in source. A ``.env`` file is read when present, but the real
process environment always wins, so CI and production supply values without a file.

Only the mechanism lives here. What each service needs is declared next to that service's
configuration, so a new setting is added in one place rather than three.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["load_dotenv", "read_bool", "read_int", "read_str"]

_TRUTHY = {"1", "true", "yes", "on"}


def load_dotenv(path: str | Path = ".env") -> None:
    """Load ``KEY=value`` pairs from a dotenv file without overriding real environment."""
    file = Path(path)
    if not file.exists():
        return
    for raw_line in file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) > 1 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ[key] = value


def read_str(name: str, fallback: str = "") -> str:
    value = os.environ.get(name)
    return fallback if value is None or value == "" else value


def read_int(name: str, fallback: int) -> int:
    value = os.environ.get(name)
    if value is None or value == "":
        return fallback
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"env {name} must be an integer, got {value!r}") from exc


def read_bool(name: str, fallback: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return fallback
    return value.lower() in _TRUTHY
