"""One ``http.request`` log record per HTTP request, for the admin console and the portal.

Plain ASGI, so the shared library needs no web framework. Each request gets a short id,
returned in ``X-Request-ID`` and bound as ``request_id`` to every record written while it
is handled. Successful reads, and reads of a missing page or method, log at debug (the
console polls job status, readers page through weeks, browsers ask for a favicon); writes
and other refusals log at info, and server errors at error with the exception, which is
then not reported a second time by uvicorn.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping
from time import perf_counter
from typing import Any, Final
from uuid import uuid4

from .logging_setup import log, log_context, mark_logged

__all__ = ["RequestLogMiddleware"]

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
App = Callable[[Scope, Receive, Send], Awaitable[None]]

_MAX_QUERY: Final = 200
_READS: Final = frozenset({"GET", "HEAD"})
#: A browser's favicon request and a probe's HEAD would otherwise log on every page view.
_QUIET_READ_STATUSES: Final = frozenset({404, 405})


class RequestLogMiddleware:
    def __init__(self, app: App) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid4().hex[:8]
        started = perf_counter()
        status = 0

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        with log_context(request_id=request_id):
            try:
                await self.app(scope, receive, send_with_id)
            except Exception as exc:
                _record(scope, 500, perf_counter() - started, exc)
                mark_logged(exc)
                raise
            _record(scope, status, perf_counter() - started, None)


def _record(scope: Scope, status: int, elapsed: float, exc: BaseException | None) -> None:
    method = str(scope.get("method", ""))
    path = str(scope.get("path", ""))
    query = bytes(scope.get("query_string", b"")).decode("latin-1")
    if query:
        path += "?" + (query if len(query) <= _MAX_QUERY else query[:_MAX_QUERY] + "…")
    fields: dict[str, Any] = {
        "method": method,
        "path": path,
        "status": status,
        "elapsed_seconds": round(elapsed, 3),
    }
    message = "{method} {path} → {status} in {elapsed_seconds:dur}"
    if exc is not None or status >= 500:
        log.error("http.request", message, exc=exc, **fields)
    elif method not in _READS or (status >= 400 and status not in _QUIET_READ_STATUSES):
        log.info("http.request", message, **fields)
    else:
        log.debug("http.request", message, **fields)
