"""Send log records to the in-namespace Fluent Bit (design "Log shipping to Elasticsearch").

Records are POSTed as a JSON array to ``<LOG_FORWARD_URL>/<stream>`` (``runs``, ``admin`` or
``portal``); Fluent Bit tags them by that path and writes each stream to its own index.

Logging must never slow or break the work it describes, so the caller only appends an
encoded record to a bounded in-memory queue. A background thread sends batches at most every
second. When Fluent Bit is unreachable or the queue is full, records are dropped and counted,
and the console says so at most once a minute. At exit the thread gets a few seconds to send
what is left, so a CronJob's last records are not lost.
"""

from __future__ import annotations

import atexit
import threading
import time
import urllib.request
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from typing import Any, Final

__all__ = ["LogForwarder", "Report"]

#: ``report(level, event, message, fields)`` writes a record to the console only.
Report = Callable[[str, str, str, dict[str, Any]], None]

_MAX_QUEUED_BYTES: Final = 16 * 1024 * 1024
_BATCH_BYTES: Final = 1024 * 1024
_INTERVAL_SECONDS: Final = 1.0
_TIMEOUT_SECONDS: Final = 3.0
_CLOSE_SECONDS: Final = 5.0
_REPORT_EVERY_SECONDS: Final = 60.0


class LogForwarder:
    def __init__(
        self,
        url: str,
        report: Report,
        *,
        max_queued_bytes: int = _MAX_QUEUED_BYTES,
        batch_bytes: int = _BATCH_BYTES,
        interval: float = _INTERVAL_SECONDS,
        timeout: float = _TIMEOUT_SECONDS,
    ) -> None:
        self.url = url.rstrip("/")
        # The collector is in the namespace: never route it through HTTP_PROXY, which a
        # deployment may set for the model endpoint.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._report = report
        self._max_queued_bytes = max_queued_bytes
        self._batch_bytes = batch_bytes
        self._interval = interval
        self._timeout = timeout
        self._ready = threading.Condition()
        self._queue: deque[tuple[str, bytes]] = deque()
        self._queued_bytes = 0
        self._closed = False
        self.sent = 0
        self.dropped = 0
        self._unreported = 0
        self._last_problem = ""
        self._last_report = 0.0
        self._thread = threading.Thread(
            target=self._run, name="alerts-bi-log-forwarder", daemon=True
        )
        self._thread.start()
        atexit.register(self.close)

    def submit(self, stream: str, record: bytes) -> None:
        """Queue one encoded record; never blocks and never raises."""
        with self._ready:
            if self._closed or self._queued_bytes + len(record) > self._max_queued_bytes:
                self._lost(1, "the send queue is full" if not self._closed else "logging closed")
                return
            self._queue.append((stream, record))
            self._queued_bytes += len(record)
            if self._queued_bytes >= self._batch_bytes:
                self._ready.notify()

    def close(self, timeout: float = _CLOSE_SECONDS) -> None:
        """Send what is queued, waiting at most ``timeout`` seconds, then stop."""
        with self._ready:
            if self._closed:
                return
            self._closed = True
            self._ready.notify()
        self._thread.join(timeout)
        with self._ready:
            if self._queue:
                self._lost(len(self._queue), "the process ended before they were sent")
                self._queue.clear()
                self._queued_bytes = 0
        self._flush_report(force=True)

    def _run(self) -> None:
        while True:
            with self._ready:
                # Wait for a full batch or the interval, whichever comes first.
                if not self._closed and self._queued_bytes < self._batch_bytes:
                    self._ready.wait(self._interval)
                batch = self._take() if self._queue else []
                finished = self._closed and not self._queue
            if batch:
                try:
                    self._send(batch)
                except Exception as exc:  # the thread must outlive any surprise
                    with self._ready:
                        self._lost(len(batch), f"{type(exc).__name__}: {exc}"[:200])
            # Also on idle ticks, so a quiet process still reports drops within a minute.
            with suppress(Exception):
                self._flush_report()
            if finished:
                return

    def _take(self) -> list[tuple[str, bytes]]:
        batch: list[tuple[str, bytes]] = []
        size = 0
        while self._queue and size < self._batch_bytes:
            stream, record = self._queue.popleft()
            self._queued_bytes -= len(record)
            size += len(record)
            batch.append((stream, record))
        return batch

    def _send(self, batch: list[tuple[str, bytes]]) -> None:
        streams: dict[str, list[bytes]] = {}
        for stream, record in batch:
            streams.setdefault(stream, []).append(record)
        for stream, records in streams.items():
            body = b"[" + b",".join(records) + b"]"
            request = urllib.request.Request(
                f"{self.url}/{stream}",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with self._opener.open(request, timeout=self._timeout) as response:
                    response.read()
                self.sent += len(records)
            except Exception as exc:  # URLError, timeouts, http.client errors, bad URLs
                with self._ready:
                    self._lost(len(records), f"{type(exc).__name__}: {exc}"[:200])

    def _lost(self, count: int, problem: str) -> None:
        # Called with self._ready held.
        self.dropped += count
        self._unreported += count
        self._last_problem = problem

    def _flush_report(self, *, force: bool = False) -> None:
        with self._ready:
            if not self._unreported:
                return
            now = time.monotonic()
            if not force and now - self._last_report < _REPORT_EVERY_SECONDS:
                return
            count, problem = self._unreported, self._last_problem
            self._unreported = 0
            self._last_report = now
        self._report(
            "warn",
            "log.forward_failed",
            "Could not send {count} log records to {url}: {problem}. They were dropped from "
            "the Elasticsearch log; this console output is unaffected",
            {"count": count, "url": self.url, "problem": problem, "dropped_total": self.dropped},
        )
