"""Authentication must cover the unified UI and compatibility trigger API alike."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from src.app import build_admin
from src.auth import csrf_token
from src.config import AdminSettings, load_config

SECRET = "s" * 40
USER = {"X-Forwarded-User": "alice"}


@pytest.fixture
def client() -> TestClient:
    return TestClient(build_admin(AdminSettings(load_config(), "unused", SECRET)))


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/schedule",
        "/decisions",
        "/teams",
        "/csrf",
        "/healthz",
        "/teams/checkout-api",
        "/runs/id",
        "/runs/id/findings",
    ],
)
def test_every_read_requires_identity(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "path",
    [
        "/runs",
        "/teams/checkout-api/trigger",
        "/runs/id/publish",
        "/runs/id/withdraw",
        "/runs/id/decide",
    ],
)
def test_all_write_paths_refuse_missing_or_cross_site_tokens(client: TestClient, path: str) -> None:
    assert client.post(path).status_code == 401
    assert client.post(path, headers=USER).status_code == 403
    token = csrf_token(SECRET, "alice", datetime.now(UTC).date())
    assert (
        client.post(
            path,
            headers={**USER, "Sec-Fetch-Site": "cross-site", "X-CSRF-Token": token},
            data={"csrf": token},
        ).status_code
        == 403
    )
    bob = csrf_token(SECRET, "bob", datetime.now(UTC).date())
    assert (
        client.post(path, headers={**USER, "X-CSRF-Token": bob}, data={"csrf": bob}).status_code
        == 403
    )


def test_authenticated_token_endpoint_and_validation(client: TestClient) -> None:
    token = client.get("/csrf", headers=USER).json()["token"]
    response = client.post(
        "/teams/checkout-api/trigger",
        headers=USER,
        data={"csrf": token, "llm": "invalid", "run_at": "invalid"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert (
        client.post(
            "/runs",
            headers={**USER, "X-CSRF-Token": token},
            json={"team": "checkout-api", "llm": "invalid"},
        ).status_code
        == 422
    )


@pytest.mark.parametrize("host", ["0.0.0.0", "10.1.2.3", "console.internal"])
def test_console_refuses_non_loopback(host: str) -> None:
    with pytest.raises(ValueError, match="loopback"):
        AdminSettings(load_config(), "unused", SECRET, host=host)


def test_console_requires_secret() -> None:
    with pytest.raises(ValueError, match="ADMIN_SECRET"):
        AdminSettings(load_config(), "unused", "")


def test_non_ascii_csrf_token_is_refused_without_crashing(client: TestClient) -> None:
    assert (
        client.post(
            "/teams/checkout-api/trigger", headers=USER, data={"csrf": "é" * 64}
        ).status_code
        == 403
    )
