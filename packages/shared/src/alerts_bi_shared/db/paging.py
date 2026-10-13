"""Opaque, scope-bound seek cursors. All decoded values remain SQL parameters.

Cursors are navigation hints, never authorization. Callers always apply the original
run, publication and filter predicates. Old page-number links remain supported.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence
from hashlib import sha256
from typing import Any


def scope_key(values: object) -> str:
    return sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()[:24]


def cursor_for(
    scope: str,
    row: Mapping[str, Any],
    columns: Sequence[tuple[str, bool]],
    *,
    previous: bool = False,
) -> str:
    payload = [scope, previous, [row[name] for name, _ in columns]]
    return (
        base64.urlsafe_b64encode(json.dumps(payload, ensure_ascii=False).encode())
        .decode()
        .rstrip("=")
    )


def seek(
    token: str | None, scope: str, columns: Sequence[tuple[str, bool]]
) -> tuple[str, dict[str, Any], bool]:
    if not token or len(token) > 4096:
        return "", {}, False
    try:
        key, previous, values = json.loads(
            base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        )
        if (
            key != scope
            or type(previous) is not bool
            or not isinstance(values, list)
            or len(values) != len(columns)
        ):
            return "", {}, False
        if any(type(value) not in (int, str) for value in values):
            return "", {}, False
    except (ValueError, TypeError, UnicodeError):
        return "", {}, False
    terms = []
    for i, (name, descending) in enumerate(columns):
        equals = [f"{columns[j][0]}=:seek{j}" for j in range(i)]
        operator = "<" if descending != previous else ">"
        terms.append("(" + " AND ".join([*equals, f"{name}{operator}:seek{i}"]) + ")")
    return "(" + " OR ".join(terms) + ")", {f"seek{i}": v for i, v in enumerate(values)}, previous


def order_by(columns: Sequence[tuple[str, bool]], reverse: bool = False) -> str:
    return ", ".join(
        f"{name} {'DESC' if descending != reverse else 'ASC'}" for name, descending in columns
    )
