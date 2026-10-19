"""The console has no login; anti-forgery covers the UI and compatibility trigger API alike."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from src.app import build_admin
from src.auth import csrf_token
from src.config import AdminSettings, load_config

SECRET = "s" * 40


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
        "/run-jobs/id",
    ],
)
def test_no_read_asks_for_a_login(path: str) -> None:
    # The "unused" database is unreachable, so pages that read SQL fail; none refuses a caller.
    client = TestClient(
        build_admin(AdminSettings(load_config(), "unused", SECRET)), raise_server_exceptions=False
    )
    response = client.get(path)
    assert response.status_code not in (401, 403)
    if response.status_code < 500:
        assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "path",
    [
        "/runs",
        "/run-jobs",
        "/teams/checkout-api/trigger",
        "/runs/id/publish",
        "/runs/id/withdraw",
        "/runs/id/delete",
        "/runs/id/decide",
    ],
)
def test_all_write_paths_refuse_missing_or_cross_site_tokens(client: TestClient, path: str) -> None:
    assert client.post(path).status_code == 403
    token = csrf_token(SECRET, datetime.now(UTC).date())
    assert (
        client.post(
            path,
            headers={"Sec-Fetch-Site": "cross-site", "X-CSRF-Token": token},
            data={"csrf": token},
        ).status_code
        == 403
    )
    forged = csrf_token("f" * 40, datetime.now(UTC).date())
    assert (
        client.post(path, headers={"X-CSRF-Token": forged}, data={"csrf": forged}).status_code
        == 403
    )


def test_token_endpoint_and_validation(client: TestClient) -> None:
    token = client.get("/csrf").json()["token"]
    response = client.post(
        "/teams/checkout-api/trigger",
        data={"csrf": token, "llm": "invalid", "run_at": "invalid"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert (
        client.post(
            "/runs",
            headers={"X-CSRF-Token": token},
            json={"team": "checkout-api", "llm": "invalid"},
        ).status_code
        == 422
    )


def test_an_identity_header_is_ignored(client: TestClient) -> None:
    plain = client.get("/csrf").json()["token"]
    assert client.get("/csrf", headers={"X-Forwarded-User": "alice"}).json()["token"] == plain


@pytest.mark.parametrize("host", ["0.0.0.0", "10.1.2.3", "console.internal"])
def test_console_may_bind_any_address(host: str) -> None:
    assert AdminSettings(load_config(), "unused", SECRET, host=host).host == host


def test_console_requires_secret() -> None:
    with pytest.raises(ValueError, match="ADMIN_SECRET"):
        AdminSettings(load_config(), "unused", "")


def test_non_ascii_csrf_token_is_refused_without_crashing(client: TestClient) -> None:
    assert client.post("/teams/checkout-api/trigger", data={"csrf": "é" * 64}).status_code == 403


def test_console_openapi_describes_async_submission_and_status(client: TestClient) -> None:
    schema = client.app.openapi()  # type: ignore[attr-defined]
    post = schema["paths"]["/run-jobs"]["post"]
    parameters = {p["name"]: p for p in post["parameters"]}
    assert parameters["Idempotency-Key"]["required"] is True
    assert parameters["X-CSRF-Token"]["required"] is True
    assert "401" not in post["responses"]
    ref = {"$ref": "#/components/schemas/RunJobOut"}
    assert post["responses"]["202"]["content"]["application/json"]["schema"] == ref
    assert (
        schema["paths"]["/run-jobs/{job_id}"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]
        == ref
    )
    assert schema["components"]["schemas"]["RunJobOut"]["properties"]["status"]["enum"] == [
        "pending",
        "queued",
        "running",
        "completed",
        "failed",
        "cancelled",
    ]
    backfill = schema["paths"]["/backfills/{backfill_id}"]["get"]["responses"]["200"]
    assert backfill["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/BackfillOut"
    }
    for path in (
        "/teams/{team_id}/backfill",
        "/backfills/{backfill_id}/cancel",
        "/backfills/{backfill_id}/retry",
        "/backfills/{backfill_id}/dismiss",
        "/run-jobs/{job_id}/cancel",
        "/run-jobs/{job_id}/retry",
    ):
        assert "post" in schema["paths"][path], path
    assert client.get("/openapi.json").status_code == 404
