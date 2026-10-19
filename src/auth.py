"""Proof that a form was submitted from this app.

The console has no login (design "Admin console without login", 2026-10-19): anyone who can
reach it may use it, and no request carries or records an operator identity.

Every write is a POST carrying a token: an HMAC of the UTC date under ``ADMIN_SECRET``. A page
on another site cannot read one, so it cannot make a visitor's browser publish, withdraw or
delete something through the console. Browsers that send ``Sec-Fetch-Site`` are additionally
refused unless the request came from this site.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import date, timedelta

from starlette.requests import Request

__all__ = ["csrf_token", "same_site", "valid_csrf"]


def csrf_token(secret: str, day: date) -> str:
    message = f"console|{day.isoformat()}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def valid_csrf(secret: str, token: str, today: date) -> bool:
    """Accept today's token or yesterday's, so a form left open past midnight UTC still works."""
    if len(token) != 64 or any(char not in "0123456789abcdef" for char in token):
        return False
    return any(
        hmac.compare_digest(csrf_token(secret, day), token)
        for day in (today, today - timedelta(days=1))
    )


def same_site(request: Request) -> bool:
    """False when the browser says the request came from another site."""
    fetch_site = request.headers.get("sec-fetch-site")
    return fetch_site is None or fetch_site in ("same-origin", "none")
