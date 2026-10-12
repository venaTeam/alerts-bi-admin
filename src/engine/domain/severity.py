"""Severity is carried as a number and read as a name.

Alerts store severity as an integer, not as the word. The scale is shared by both schemas
but the two schemas name its levels differently, so the number alone does not identify the
level - the schema does::

    code   v1 (Appchi)   v2 (Appchi V2)
    ----   -----------   --------------
    5      error         critical
    4      major         high
    3      warning       warning
    1      clear         clear

That is why the conversion takes the schema. Reading ``5`` without it would have to choose
between ``error`` and ``critical``, and those are not the same statement: R9 makes a missing
runbook block phase-2 completion on ``critical`` and merely visible on anything else. Since
R8-R10 are v2-only, a ``5`` reaching that rule is always ``critical``.

Codes 2 and anything above 5 are not defined by the standard. An unknown code is **kept, not
guessed at**: it is rendered as its own digits so the value a team actually sent survives
into the store and the work list, rather than being flattened to null or silently promoted
to a level nobody chose.
"""

from __future__ import annotations

from typing import Any, Final

__all__ = [
    "SEVERITY_CODES",
    "SEVERITY_NAMES",
    "code_for_name",
    "severity_label",
]

#: code -> the name each schema gives that level.
SEVERITY_NAMES: Final[dict[int, dict[str, str]]] = {
    5: {"v1": "error", "v2": "critical"},
    4: {"v1": "major", "v2": "high"},
    3: {"v1": "warning", "v2": "warning"},
    1: {"v1": "clear", "v2": "clear"},
}

#: Every accepted name -> its code. Both schemas' vocabularies, since a fixture definition
#: or a supplied panel query may use either.
SEVERITY_CODES: Final[dict[str, int]] = {
    name: code for code, names in SEVERITY_NAMES.items() for name in names.values()
}


def code_for_name(name: str) -> int:
    """The numeric code for a severity name.

    Raises rather than defaulting: a name outside the standard means the caller believes in
    a level that does not exist, and inventing a number for it would put a fabricated
    severity into the data.
    """
    try:
        return SEVERITY_CODES[name.strip().lower()]
    except (AttributeError, KeyError):
        known = ", ".join(sorted(SEVERITY_CODES))
        raise ValueError(f"unknown severity name {name!r}; known names are {known}") from None


def severity_label(schema: str, value: Any) -> str | None:
    """Read a stored severity as the name its schema gives it.

    Accepts the integer the source carries, and the digits of one as a string. A value that
    is neither - an empty field, or a name from a source that still sends text - is passed
    through unchanged rather than rejected, so this stays readable during a transition and
    never invents a level.
    """
    if value is None:
        return None
    if isinstance(value, bool):  # bool is an int subclass; a severity is never a boolean
        return str(value)

    code: int | None = None
    if isinstance(value, int):
        code = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.lstrip("-").isdigit():
            code = int(text)
        else:
            return text  # already a name, or something this module will not judge
    else:
        return str(value)

    names = SEVERITY_NAMES.get(code)
    if names is None:
        # Unknown code: keep what arrived. The digits are the honest rendering of a level
        # the standard does not define.
        return str(code)
    return names.get(schema, str(code))
