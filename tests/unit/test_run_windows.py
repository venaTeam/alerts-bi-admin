"""Manual entry points reject arbitrary times before touching source services."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from src.app import build_admin
from src.cli import build_parser
from src.config import AdminSettings, load_config
from src.engine.api.schemas import RunRequest
from src.engine.cli import _command_run
from src.engine.config import load_config as runtime_config
from src.engine.weekly.plan import latest_week_end
from src.runs import remove_run_files


@pytest.mark.parametrize(
    "value",
    ["2026-10-13", "2026-10-12T12:00Z", "2026-10-12T00:00:00.001Z", "2026-10-12T00:00+03:00"],
)
def test_non_monday_boundary_is_rejected(value: str) -> None:
    with pytest.raises(ValidationError, match="Monday at 00:00 UTC"):
        RunRequest.model_validate({"team": "checkout-api", "run_at": value})


@pytest.mark.parametrize("value", ["2026-10-12", "2026-10-12T00:00Z", "2026-10-12T03:00+03:00"])
def test_monday_dates_and_equivalent_instants_are_accepted(value: str) -> None:
    body = RunRequest.model_validate({"team": "checkout-api", "run_at": value})
    assert body.run_at is not None
    assert (
        body.run_at.replace(tzinfo=UTC) == datetime(2026, 10, 12, tzinfo=UTC)
        if body.run_at.tzinfo is None
        else body.run_at.astimezone(UTC) == datetime(2026, 10, 12, tzinfo=UTC)
    )


def test_form_and_json_reject_tampered_window_before_running() -> None:
    client = TestClient(
        build_admin(AdminSettings(load_config(), "unused", "s" * 40, dev_user="alice"))
    )
    token = client.get("/csrf").json()["token"]
    response = client.post(
        "/teams/checkout-api/trigger",
        data={"csrf": token, "run_at": "2026-10-13", "llm": "fake"},
        follow_redirects=False,
    )
    assert response.status_code == 303 and "Monday" in response.headers["location"]
    assert (
        client.post(
            "/runs",
            headers={"X-CSRF-Token": token},
            json={"team": "checkout-api", "run_at": "2026-10-12T12:00Z"},
        ).status_code
        == 422
    )


def test_cli_rejects_non_monday_before_connecting(capsys: pytest.CaptureFixture[str]) -> None:
    args = build_parser().parse_args(
        ["run", "--team", "checkout-api", "--run-at", "2026-10-13", "--no-llm"]
    )
    assert _command_run(args, runtime_config()) == 2
    assert "Monday at 00:00 UTC" in capsys.readouterr().err


def test_default_boundary_is_latest_completed_monday() -> None:
    assert latest_week_end(datetime(2026, 10, 13, 12, tzinfo=UTC)) == datetime(
        2026, 10, 12, tzinfo=UTC
    )


def test_report_cleanup_is_scoped_and_idempotent(tmp_path: Path) -> None:
    run_id = "a" * 64
    for parent in (tmp_path, tmp_path / "weekly"):
        directory = parent / run_id[:16]
        directory.mkdir(parents=True)
        (directory / "scorecard.html").write_text("run report")
    unrelated = tmp_path / ("b" * 16)
    unrelated.mkdir()
    (unrelated / "scorecard.html").write_text("another run")
    remove_run_files(tmp_path, run_id)
    remove_run_files(tmp_path, run_id)
    assert not (tmp_path / run_id[:16]).exists()
    assert not (tmp_path / "weekly" / run_id[:16]).exists()
    assert (unrelated / "scorecard.html").read_text() == "another run"
    with pytest.raises(ValueError, match="Invalid run id"):
        remove_run_files(tmp_path, "../outside")
