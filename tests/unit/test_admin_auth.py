"""The admin app's anti-forgery rules, without a database (design 7.12).

The console has no login (design "Admin console without login", 2026-10-19), so the token is
bound to the day and ``ADMIN_SECRET`` only, and the listener may bind any address.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from src.auth import csrf_token, valid_csrf
from src.config import AdminSettings, load_config

SECRET = "k" * 40
TODAY = date(2026, 9, 24)


def test_a_token_is_bound_to_the_secret_and_the_day() -> None:
    token = csrf_token(SECRET, TODAY)
    assert valid_csrf(SECRET, token, TODAY)
    assert valid_csrf(SECRET, token, TODAY + timedelta(days=1)), "past midnight UTC"
    assert not valid_csrf(SECRET, token, TODAY + timedelta(days=2))
    assert not valid_csrf("x" * 40, token, TODAY)
    assert not valid_csrf(SECRET, "", TODAY)


@pytest.mark.parametrize("host", ["127.0.0.1", "0.0.0.0", "10.0.0.5"])
def test_the_admin_app_may_bind_any_address(host: str) -> None:
    settings = AdminSettings(config=load_config(), database="db", secret=SECRET, host=host)
    assert settings.host == host


def test_a_short_secret_is_refused() -> None:
    with pytest.raises(ValueError, match="ADMIN_SECRET"):
        AdminSettings(config=load_config(), database="db", secret="short")
