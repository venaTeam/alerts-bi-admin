"""Records forwarded to Fluent Bit (design "Log shipping to Elasticsearch")."""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from alerts_bi_shared import logging_setup
from alerts_bi_shared.log_forwarding import LogForwarder
from alerts_bi_shared.logging_setup import configure_logging, log, log_context

RUN_ID = "3f9a1c2b4d5e6f70" + "ab" * 24


class _Collector(ThreadingHTTPServer):
    """A stand-in for Fluent Bit's HTTP input: remembers each POST's path and records."""

    def __init__(self) -> None:
        self.received: list[tuple[str, list[dict[str, Any]]]] = []
        super().__init__(("127.0.0.1", 0), _Handler)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def records(self, stream: str) -> list[dict[str, Any]]:
        return [record for path, batch in self.received if path == stream for record in batch]


class _Handler(BaseHTTPRequestHandler):
    server: _Collector

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        assert self.headers["Content-Type"] == "application/json"
        self.server.received.append((self.path.lstrip("/"), json.loads(body)))
        self.send_response(201)
        self.end_headers()

    def log_message(self, *_args: Any) -> None:
        pass


@pytest.fixture
def collector() -> Iterator[_Collector]:
    server = _Collector()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in ("LOG_LEVEL", "LOG_FORMAT", "LOG_FORWARD_URL", "LOG_FORWARD_LEVEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(logging_setup, "_settings", None)
    monkeypatch.setattr(logging_setup, "_forwarder", None)
    yield
    if logging_setup._forwarder is not None:
        logging_setup._forwarder.close()


def _close() -> LogForwarder:
    forwarder = logging_setup._forwarder
    assert forwarder is not None
    forwarder.close()
    return forwarder


def _alerts() -> list[dict[str, Any]]:
    return [
        {"schema": "v2", "application": "checkout", "key_field": "disk-full", "state": "good"},
        {"schema": "v2", "application": "checkout", "key_field": "cpu-high", "state": "flagged"},
    ]


def test_records_reach_their_index_and_groups_carry_their_alerts(
    collector: _Collector, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LOG_FORWARD_URL", collector.url)
    configure_logging("worker", color=False, capture_libraries=False)
    with log_context(team_id="t1", run_id=RUN_ID):
        log.info("run.started", "Analysis started")
        log.debug("llm.batch_retry_wait", "below the forward level")
        log.info("llm.group", "Group 1 of 1: 2 alerts", console=False, alerts=_alerts())
    _close()

    runs = collector.records("runs")
    assert [record["event"] for record in runs] == ["run.started", "llm.group"]
    group = runs[1]
    assert group["run_id"] == RUN_ID and group["team_id"] == "t1"
    assert group["alerts"] == _alerts()
    assert "data" not in group
    assert group["line"].endswith(
        'Group 1 of 1: 2 alerts  alerts="2 items"  [team=t1 run=3f9a1c2b4d5e6f70]'
    )
    # The group stays off the console at the default level.
    console = capsys.readouterr().err
    assert "run.started" in console and "llm.group" not in console


def test_the_console_shows_groups_at_debug(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("worker", level="debug", color=False, capture_libraries=False)
    log.info("llm.group", "Group 1 of 1: 2 alerts", console=False, alerts=_alerts())
    assert "llm.group" in capsys.readouterr().err


def test_the_portal_and_admin_write_to_their_own_paths(
    collector: _Collector, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOG_FORWARD_URL", collector.url + "/")
    configure_logging("portal", capture_libraries=False)
    log.info("portal.listening", "ready")
    configure_logging("ui", capture_libraries=False)
    log.info("console.submitted", "a console action on a run", run_id=RUN_ID)
    _close()

    assert [record["event"] for record in collector.records("portal")] == ["portal.listening"]
    assert [record["event"] for record in collector.records("admin")] == ["console.submitted"]


def test_forward_level_is_separate_from_the_console_level(
    collector: _Collector, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOG_FORWARD_URL", collector.url)
    monkeypatch.setenv("LOG_FORWARD_LEVEL", "warn")
    configure_logging("worker", capture_libraries=False)
    log.info("run.started", "not forwarded")
    log.warn("es.search_retry", "forwarded")
    _close()
    assert [record["event"] for record in collector.records("runs")] == ["es.search_retry"]


def test_an_unreachable_collector_never_blocks_and_is_reported(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        port = unused.getsockname()[1]
    monkeypatch.setenv("LOG_FORWARD_URL", f"http://127.0.0.1:{port}")
    configure_logging("worker", color=False, capture_libraries=False)
    started = time.monotonic()
    for index in range(200):
        log.info("es.read_progress", "row {index}", index=index)
    assert time.monotonic() - started < 1.0
    forwarder = _close()

    assert forwarder.dropped == 200 and forwarder.sent == 0
    console = capsys.readouterr().err
    assert "log.forward_failed" in console
    assert "Could not send 200 log records" in console


@pytest.mark.parametrize("log_format", ["console", "json"])
def test_the_failure_warning_never_shows_url_credentials(
    log_format: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The warning names the collector URL; credentials in it are scrubbed like everywhere else.
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        port = unused.getsockname()[1]
    monkeypatch.setenv("LOG_FORWARD_URL", f"http://fluent:s3cret@127.0.0.1:{port}")
    monkeypatch.setenv("LOG_FORMAT", log_format)
    configure_logging("worker", color=False, capture_libraries=False)
    log.info("run.started", "Analysis started")
    _close()

    console = capsys.readouterr().err
    assert "log.forward_failed" in console
    assert "s3cret" not in console
    assert f"http://[redacted]@127.0.0.1:{port}" in console


def test_a_full_queue_drops_instead_of_growing() -> None:
    reports: list[tuple[str, str, str, dict[str, Any]]] = []
    forwarder = LogForwarder(
        "http://127.0.0.1:9",
        lambda *args: reports.append(args),
        max_queued_bytes=100,
        interval=60,
    )
    for _ in range(10):
        forwarder.submit("runs", b'{"message": "about forty bytes of record"}')
    assert forwarder.dropped >= 8
    forwarder.close(timeout=0.1)
    assert reports and reports[-1][1] == "log.forward_failed"


def test_an_invalid_forward_url_is_reported_and_ignored(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LOG_FORWARD_URL", "alerts-bi-logs:9880")
    configure_logging("worker", color=False, capture_libraries=False)
    assert logging_setup._forwarder is None
    assert "LOG_FORWARD_URL='alerts-bi-logs:9880' is not an http(s) URL" in capsys.readouterr().err


def test_a_send_that_raises_anything_is_counted_and_the_thread_keeps_going() -> None:
    reports: list[tuple[str, str, str, dict[str, Any]]] = []
    # A port that is not a number makes http.client raise InvalidURL, not URLError.
    forwarder = LogForwarder(
        "http://127.0.0.1:98x", lambda *args: reports.append(args), interval=0.05
    )
    forwarder.submit("runs", b'{"message": "one"}')
    time.sleep(0.3)
    forwarder.submit("runs", b'{"message": "two"}')
    forwarder.close()
    assert forwarder.dropped == 2 and forwarder.sent == 0
    assert sum(report[3]["count"] for report in reports) == 2


def test_a_forward_url_with_a_bad_port_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LOG_FORWARD_URL", "http://alerts-bi-logs:9880x")
    configure_logging("worker", color=False, capture_libraries=False)
    assert logging_setup._forwarder is None
    assert "is not an http(s) URL" in capsys.readouterr().err


def test_a_proxy_in_the_environment_is_not_used(
    collector: _Collector, monkeypatch: pytest.MonkeyPatch
) -> None:
    # HTTP_PROXY may be set for the model endpoint; the collector is reached directly.
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("LOG_FORWARD_URL", collector.url)
    configure_logging("worker", capture_libraries=False)
    log.info("run.started", "through no proxy")
    forwarder = _close()
    assert forwarder.sent == 1
    assert [record["event"] for record in collector.records("runs")] == ["run.started"]


def test_documents_carry_no_url_credentials_and_valid_json_numbers(
    collector: _Collector, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOG_FORWARD_URL", collector.url)
    configure_logging("worker", capture_libraries=False)
    log.info(
        "llm.group",
        "group",
        console=False,
        group_value="https://bot:s3cret@grafana.internal/d/abc",
        ratio=float("nan"),
    )
    _close()
    (record,) = collector.records("runs")
    fields = record["data"]["llm_group"]
    assert fields["group_value"] == "https://[redacted]@grafana.internal/d/abc"
    assert fields["ratio"] is None
