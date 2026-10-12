"""Choosing between HTML and JSON for one response.

The surface answers two callers from the same routes: a browser following the run form, and
a script. Keeping the rule in one place means the routes and the exception handlers cannot
disagree about which one they are talking to.
"""

from __future__ import annotations

from fastapi import Request

__all__ = ["wants_html"]


def wants_html(request: Request) -> bool:
    """A browser gets HTML; anything asking for JSON gets JSON.

    Explicit ``application/json`` wins, because a browser's ``Accept`` header lists it too,
    just after ``text/html``.
    """
    accept = request.headers.get("accept", "")
    if "application/json" in accept:
        return False
    return "text/html" in accept or "*/*" in accept or accept == ""
