"""Text normalization shared by the deterministic rules (design section 4).

Two distinct normalizations exist and they are not interchangeable:

- :func:`normalize_message` (R1, R2, R10) trims, lowercases, collapses repeated whitespace
  and removes surrounding punctuation, then matches the COMPLETE value against a
  catalogue.
- :func:`normalize_field_value` (R3, R8, R9 placeholders) trims, lowercases and collapses
  whitespace but does NOT strip punctuation, because ``n/a`` is itself a catalogue value.

Matching is always whole-value equality. Substring matching is explicitly rejected:
``backup completed with 10 failures`` is not a heartbeat, and ``test-payments-service`` is
not a placeholder. Message length alone never flags an alert.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

__all__ = ["is_blank", "normalize_field_value", "normalize_message"]

_WHITESPACE = re.compile(r"\s+")


def _is_punctuation(char: str) -> bool:
    r"""Unicode General_Category Punctuation, matching the JavaScript ``\p{P}`` class."""
    return unicodedata.category(char).startswith("P")


def normalize_field_value(value: Any) -> str | None:
    """Trim, lowercase and collapse repeated whitespace. ``None`` for a non-string."""
    if not isinstance(value, str):
        return None
    return _WHITESPACE.sub(" ", value.strip().lower())


def normalize_message(value: Any) -> str | None:
    """Field normalization plus removal of surrounding punctuation.

    Stripping punctuation can expose whitespace that was sitting inside it
    (``" error occurred ."``), so the value is trimmed once more afterwards.
    """
    normalized = normalize_field_value(value)
    if normalized is None:
        return None
    start = 0
    end = len(normalized)
    while start < end and _is_punctuation(normalized[start]):
        start += 1
    while end > start and _is_punctuation(normalized[end - 1]):
        end -= 1
    return normalized[start:end].strip()


def is_blank(value: Any) -> bool:
    """Is a value absent for rule purposes: not a string, or empty/whitespace-only?"""
    return not isinstance(value, str) or value.strip() == ""
