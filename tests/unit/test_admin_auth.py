"""The admin app's identity and anti-forgery rules, without a database (design 7.12)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from src.auth import csrf_token, valid_csrf
from src.config import AdminSettings, load_config

SECRET = "k" * 40
TODAY = date(2026, 9, 24)


def test_a_token_is_bound_to_the_user_and_the_day() -> None:
    token = csrf_token(SECRET, "alice", TODAY)
    assert valid_csrf(SECRET, "alice", token, TODAY)
    assert valid_csrf(SECRET, "alice", token, TODAY + timedelta(days=1)), "past midnight UTC"
    assert not valid_csrf(SECRET, "alice", token, TODAY + timedelta(days=2))
    assert not valid_csrf(SECRET, "bob", token, TODAY)
    assert not valid_csrf("x" * 40, "alice", token, TODAY)
    assert not valid_csrf(SECRET, "alice", "", TODAY)


@pytest.mark.parametrize("host", ["0.0.0.0", "10.0.0.5", "admin.internal"])
def test_the_admin_app_refuses_to_bind_anywhere_but_loopback(host: str) -> None:
    with pytest.raises(ValueError, match="loopback"):
        AdminSettings(config=load_config(), database="db", secret=SECRET, host=host)


def test_a_short_secret_is_refused() -> None:
    with pytest.raises(ValueError, match="ADMIN_SECRET"):
        AdminSettings(config=load_config(), database="db", secret="short")
