"""Who is signed in, and proof that a form was submitted from this app.

Identity comes from the login proxy's header (``X-Forwarded-User`` from OpenShift's
oauth-proxy). The app binds to loopback, so only the proxy can set it.

Every write is a POST carrying a token: an HMAC of the signed-in user and the UTC date under
``ADMIN_SECRET``. A page elsewhere on the company network cannot produce one, so it cannot
make a signed-in operator's browser publish or withdraw something. Browsers that send
``Sec-Fetch-Site`` are additionally refused unless the request came from this site.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import date, timedelta

from starlette.requests import Request

from alerts_bi_admin.config import AdminSettings

__all__ = ["csrf_token", "identity", "same_site", "valid_csrf"]

_MAX_NAME = 128


def identity(request: Request, settings: AdminSettings) -> str | None:
    """The signed-in operator, or ``None`` when there is none."""
    value = request.headers.get(settings.user_header, "").strip()
    if value:
        return value[:_MAX_NAME]
    return settings.dev_user


def csrf_token(secret: str, user: str, day: date) -> str:
    message = f"{user}|{day.isoformat()}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def valid_csrf(secret: str, user: str, token: str, today: date) -> bool:
    """Accept today's token or yesterday's, so a form left open past midnight UTC still works."""
    return any(
        hmac.compare_digest(csrf_token(secret, user, day), token)
        for day in (today, today - timedelta(days=1))
    )


def same_site(request: Request) -> bool:
    """False when the browser says the request came from another site."""
    fetch_site = request.headers.get("sec-fetch-site")
    return fetch_site is None or fetch_site in ("same-origin", "none")
