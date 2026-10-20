"""The console and JSON log formats (design "Console log format", 2026-10-20)."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from typing import Any

import pytest
from alerts_bi_shared import logging_setup
from alerts_bi_shared.logging_setup import (
    bind_log_context,
    configure_logging,
    format_duration,
    log,
    log_context,
    redact_error,
)
from alerts_bi_shared.request_logging import RequestLogMiddleware
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

RUN_ID = "3f9a1c2b4d5e6f70" + "ab" * 24
JOB_ID = "7c1e9a2f" + "cd" * 12
HEAD = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}Z ")


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(logging_setup, "_settings", None)
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.delenv("LOG_FORMAT", raising=False)
    configure_logging("worker", color=False, capture_libraries=False)
    yield


def _lines(capsys: pytest.CaptureFixture[str]) -> list[str]:
    captured = capsys.readouterr()
    assert captured.out == "", "log records go to stderr; stdout is for command output"
    return captured.err.splitlines()


def _json_lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    return [json.loads(line) for line in _lines(capsys)]


def test_console_line_puts_the_message_before_the_context(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with log_context(team_id="payments-core", run_id=RUN_ID, job_id=JOB_ID):
        log.info(
            "es.read_schema",
            "Read {rows:,} {schema} rows in {pages} pages, {elapsed:dur}",
            rows=48213,
            schema="v1",
            pages=49,
            elapsed=47.3,
            index="appchi-v1",
            skipped=None,
        )

    (line,) = _lines(capsys)
    assert HEAD.match(line)
    assert line[25:].startswith("INFO  worker es.read_schema     Read 48,213 v1 rows in 49 pages")
    # Fields the message does not use follow it; None is left out; the context comes last.
    assert line.endswith(
        "47.3s  index=appchi-v1  [team=payments-core run=3f9a1c2b4d5e6f70 job=7c1e9a2f]"
    )


def test_json_keeps_full_identifiers_and_every_field(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("weekly", fmt="json", capture_libraries=False)
    with log_context(team_id="t1", run_id=RUN_ID):
        log.warn("weekly.outcome", "Held the week ending {week}", week="2026-10-19", msg="x")

    (record,) = _json_lines(capsys)
    assert record["level"] == "warn"
    assert record["proc"] == "weekly"
    assert record["event"] == "weekly.outcome"
    assert record["msg"] == "Held the week ending 2026-10-19"
    assert record["run_id"] == RUN_ID
    assert record["team_id"] == "t1"
    assert record["week"] == "2026-10-19"
    assert record["field_msg"] == "x"
    assert record["ts"].endswith("Z") and "T" in record["ts"]


def test_identifier_fields_join_the_context(capsys: pytest.CaptureFixture[str]) -> None:
    log.info("run.job_submitted", "Accepted", status="queued", job_id=JOB_ID, team_id="t")

    (line,) = _lines(capsys)
    assert line.endswith("Accepted  status=queued  [team=t job=7c1e9a2f]")


def test_context_is_scoped_and_bound_values_end_with_the_scope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with log_context(team_id="t"):
        bind_log_context(stage="es:v1")
        log.warn("a.one", "inside")
        log.info("a.routine", "stage is shown only when something goes wrong")
        bind_log_context(stage=None)
        log.warn("a.two", "cleared")
    log.info("a.three", "outside")

    one, routine, two, three = _lines(capsys)
    assert one.endswith("[team=t stage=es:v1]")
    assert routine.endswith("goes wrong  [team=t]")
    assert two.endswith("[team=t]")
    assert three.endswith("outside")


def test_secrets_are_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("cli", fmt="json", capture_libraries=False)
    log.info("x.y", "connect", password="hunter2", api_key="k", note="https://u:p@es:9200/i")
    (record,) = _json_lines(capsys)
    assert record["password"] == "[redacted]"
    assert record["api_key"] == "[redacted]"
    assert "hunter2" not in json.dumps(record)

    error = ValueError("cannot reach https://elastic:s3cret@es.internal:9200/appchi-v1")
    assert "s3cret" not in redact_error(error)
    assert "https://[redacted]@es.internal:9200" in redact_error(error)


def _inner(document: dict[str, str]) -> None:
    raise KeyError("missing field")


def _outer() -> None:
    try:
        _inner({"summary": "SECRET ALERT TEXT"})
    except KeyError as exc:
        raise RuntimeError("analysis failed") from exc


def test_errors_show_the_chain_and_own_frames_without_values(
    capsys: pytest.CaptureFixture[str],
) -> None:
    try:
        _outer()
    except RuntimeError as exc:
        with log_context(run_id=RUN_ID, stage="analysis:v1"):
            log.error(
                "run.job_failed", "The manual run failed after {elapsed:dur}", elapsed=64.2, exc=exc
            )

    lines = _lines(capsys)
    assert lines[0].endswith(
        "The manual run failed after 1m04s  [run=3f9a1c2b4d5e6f70 stage=analysis:v1]"
    )
    assert lines[1] == "    RuntimeError: analysis failed"
    assert lines[2].startswith("      at test_logging_setup.py:") and lines[2].endswith(
        " in _outer"
    )
    assert re.fullmatch(
        r"    caused by KeyError: 'missing field' \(at test_logging_setup\.py:\d+ in _inner\)",
        lines[-1],
    )
    assert "SECRET ALERT TEXT" not in "\n".join(lines)


def test_json_error_carries_a_structured_exception(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("worker", fmt="json", capture_libraries=False)
    try:
        _outer()
    except RuntimeError as exc:
        log.error("run.job_failed", "failed", exc=exc)

    (record,) = _json_lines(capsys)
    assert record["error"] == "RuntimeError: analysis failed"
    exception = record["exception"]
    assert exception["type"] == "RuntimeError"
    assert exception["frames"][0].endswith(" in _outer")
    assert exception["caused_by"][0].startswith("KeyError: 'missing field' (at ")


def test_compiled_and_installed_library_frames_are_not_ours() -> None:
    # pymssql's Cython frames name a relative source file.
    assert not logging_setup._own_frame("src/pymssql/_pymssql.pyx")
    assert not logging_setup._own_frame("<frozen runpy>")
    assert logging_setup._own_frame(__file__)


def test_a_broken_template_still_logs(capsys: pytest.CaptureFixture[str]) -> None:
    log.info("x.y", "Missing {nope} and {rows:,}", rows="many")
    (line,) = _lines(capsys)
    assert "Missing {nope} and {rows:,}" in line
    assert "rows=many" in line


def test_level_threshold_and_invalid_settings(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "warning")
    monkeypatch.setenv("LOG_FORMAT", "yaml")
    configure_logging("ui", color=False, capture_libraries=False)
    log.info("x.quiet", "not shown")
    log.warn("x.loud", "shown")

    problem, shown = _lines(capsys)
    assert "log.settings_invalid" in problem
    assert "LOG_FORMAT='yaml' is not console or json; using console" in problem
    assert " WARN  ui     x.loud" in shown
    assert log.enabled("warn") and not log.enabled("info")


def test_library_warnings_use_the_same_format(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("ui", color=False, capture_libraries=True)
    logging.getLogger("uvicorn.error").warning("Invalid HTTP request received.")
    logging.getLogger("uvicorn.error").info("Started server process")

    (line,) = _lines(capsys)
    assert " WARN  ui     lib.uvicorn.error  Invalid HTTP request received." in line


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(0.041, "41ms"), (4.62, "4.6s"), (59.94, "59.9s"), (131, "2m11s"), (3720, "1h02m")],
)
def test_durations_are_short(seconds: float, text: str) -> None:
    assert format_duration(seconds) == text


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/teams")
    def teams() -> dict[str, str]:
        # A sync endpoint runs in the thread pool: the request id must follow it there.
        log.info("x.inside", "inside the handler")
        return {"ok": "yes"}

    @app.post("/teams/{team}/trigger")
    def trigger(team: str) -> dict[str, str]:
        return {"team": team}

    @app.get("/busy")
    def busy() -> None:
        raise HTTPException(409, "A run is already in progress.")

    @app.get("/broken")
    def broken() -> None:
        raise RuntimeError("boom")

    app.add_middleware(RequestLogMiddleware)
    return app


def test_requests_get_an_id_and_one_record_each(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("ui", color=False, capture_libraries=True)
    client = TestClient(_app(), raise_server_exceptions=False)

    response = client.get("/teams?page=2")
    request_id = response.headers["x-request-id"]
    client.post("/teams/t1/trigger")
    client.get("/favicon.ico")
    client.get("/busy")
    client.get("/broken")

    lines = _lines(capsys)
    inside, posted, busy, broken, *trace = lines
    # Successful reads and missing pages are debug: GET /teams shows only the handler's
    # own line, and the favicon's 404 shows nothing.
    assert inside.endswith(f"inside the handler  [req={request_id}]")
    assert "http.request       POST /teams/t1/trigger → 200 in " in posted
    assert " INFO  ui     http.request       GET /busy → 409 in " in busy
    assert " ERROR ui     http.request       GET /broken → 500 in " in broken
    assert trace[0] == "    RuntimeError: boom"
    # Uvicorn or Starlette re-reporting the same exception is not written twice.
    assert not any("lib." in line for line in lines)


def test_debug_shows_successful_reads(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("portal", level="debug", color=False, capture_libraries=False)
    TestClient(_app()).get("/teams?page=2")

    lines = _lines(capsys)
    assert any(
        " DEBUG portal http.request       GET /teams?page=2 → 200 in " in line for line in lines
    )
