"""Pure HTML escaping and link validation shared by the reader and operator UI."""

from __future__ import annotations

from html import escape
from typing import Any
from urllib.parse import urlsplit

SCHEMA_NAMES = {"v1": "Appchi", "v2": "Appchi V2"}
PHASE_STEPS = (
    ("phase_0", "Phase 0 · Clean up"),
    ("phase_1", "Phase 1 · New rules"),
    ("phase_2", "Phase 2 · Enrich"),
    ("done", "Done"),
)


def h(value: Any) -> str:
    """Escape any value for HTML text and attribute contexts."""
    return "" if value is None else escape(str(value), quote=True)


def safe_link(url: Any) -> str | None:
    """Return ``url`` when it is an absolute http(s) URL with a host, else ``None``."""
    if not isinstance(url, str) or not url.strip():
        return None
    candidate = url.strip()
    try:
        parts = urlsplit(candidate)
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not host:
        return None
    if any(ord(ch) < 32 or ch.isspace() for ch in candidate):
        return None
    return candidate
