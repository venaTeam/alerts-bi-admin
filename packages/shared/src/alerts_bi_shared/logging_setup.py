"""Structured logging for every Alerts BI process.

Deliberately narrow. Design section "Configuration and security" forbids alert documents,
credentials and complete LLM payloads in ordinary logs: log identifiers, hashes, counts,
timings and redacted errors only. Auditable payloads belong in SQL.

Each record has a stable dotted event name, a plain-English message and fields. Records go
to stderr, one per line, so stdout stays free for command output. ``LOG_FORMAT=console``
(the default) renders a line for people watching a terminal or ``oc logs``::

    2026-10-20 14:05:58.330Z INFO  worker es.read_schema     Read 48,213 v1 rows ...  [team=payments-core run=3f9a1c2b4d5e6f70 job=7c1e9a2f]

``LOG_FORMAT=json`` writes the same record as one JSON object with full identifiers.
Identifiers bound with :func:`log_context` (team, run, job, backfill, batch, request and
stage) are attached to every record written inside that scope.
"""

from __future__ import annotations

import json
import logging
import os
import re
import string
import sys
import sysconfig
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import PurePath
from types import TracebackType
from typing import Any, Final, TextIO

__all__ = [
    "CONTEXT_KEYS",
    "bind_log_context",
    "configure_logging",
    "format_duration",
    "log",
    "log_context",
    "mark_logged",
    "redact_error",
]

_LEVELS: Final = {"debug": 10, "info": 20, "warn": 30, "error": 40}
_LEVEL_ALIASES: Final = {"warning": "warn"}
_FORMATS: Final = ("console", "json")

#: Identifiers that describe *whose* work a record is about, in display order. A field
#: with one of these names is shown in the trailing ``[...]`` context, never in the body.
CONTEXT_KEYS: Final = (
    "team_id",
    "run_id",
    "job_id",
    "backfill_id",
    "batch_id",
    "request_id",
    "stage",
)
# Console label and the prefix length that identifies a value (the CLI and console show
# 16 characters of a run id). JSON always carries the full value.
_CONTEXT_LABELS: Final = {
    "team_id": ("team", None),
    "run_id": ("run", 16),
    "job_id": ("job", 8),
    "backfill_id": ("backfill", 8),
    "batch_id": ("batch", 12),
    "request_id": ("req", None),
    "stage": ("stage", None),
}
_RESERVED: Final = frozenset({"ts", "level", "proc", "event", "msg", "exception"})

# Secret-ish keys that must never reach the log.
_REDACT_KEYS: Final = {
    "password",
    "apikey",
    "api_key",
    "authorization",
    "token",
    "secret",
    "connectionstring",
    "connection_string",
}
_URL_CREDENTIALS: Final = re.compile(r"(\b[a-zA-Z][a-zA-Z0-9+.-]*://)[^\s/@:]+:[^\s/@]*@")
_MAX_MESSAGE: Final = 300
_MAX_FRAMES: Final = 12
_EVENT_WIDTH: Final = 18
_PROC_WIDTH: Final = 6

_CONTEXT: ContextVar[Mapping[str, Any] | None] = ContextVar("alerts_bi_log_context", default=None)
_WRITE_LOCK = threading.Lock()


@dataclass(slots=True)
class _Settings:
    proc: str
    threshold: int
    fmt: str
    color: bool | None
    """None decides per write from whether stderr is a terminal."""


_settings: _Settings | None = None
_settings_lock = threading.Lock()


def _env_settings(proc: str | None = None) -> tuple[_Settings, list[str]]:
    problems: list[str] = []
    raw_level = os.environ.get("LOG_LEVEL", "info").strip().lower() or "info"
    level = _LEVEL_ALIASES.get(raw_level, raw_level)
    if level not in _LEVELS:
        problems.append(f"LOG_LEVEL={raw_level!r} is not debug, info, warn or error; using info")
        level = "info"
    fmt = os.environ.get("LOG_FORMAT", "console").strip().lower() or "console"
    if fmt not in _FORMATS:
        problems.append(f"LOG_FORMAT={fmt!r} is not console or json; using console")
        fmt = "console"
    color: bool | None = None
    if os.environ.get("NO_COLOR"):
        color = False
    elif os.environ.get("FORCE_COLOR"):
        color = True
    return _Settings(proc or "app", _LEVELS[level], fmt, color), problems


def _current_settings() -> _Settings:
    global _settings
    if _settings is None:
        with _settings_lock:
            if _settings is None:
                _settings, _problems = _env_settings()
    return _settings


def configure_logging(
    proc: str,
    *,
    level: str | None = None,
    fmt: str | None = None,
    color: bool | None = None,
    capture_libraries: bool = True,
) -> None:
    """Name this process and read ``LOG_LEVEL``/``LOG_FORMAT``; call once at startup.

    ``proc`` is the short process name shown on every console line (``ui``, ``worker``,
    ``weekly``, ``migrate``, ``cli``, ``portal``). Arguments override the environment,
    which tests use. With ``capture_libraries`` the warnings and errors that libraries
    such as uvicorn and the Elasticsearch transport write through :mod:`logging` are
    rendered in the same format instead of their own.
    """
    global _settings
    settings, problems = _env_settings(proc)
    if level is not None:
        settings.threshold = _LEVELS[_LEVEL_ALIASES.get(level, level)]
    if fmt is not None:
        if fmt not in _FORMATS:
            raise ValueError(f"log format must be console or json, not {fmt!r}")
        settings.fmt = fmt
    if color is not None:
        settings.color = color
    with _settings_lock:
        _settings = settings
    for problem in problems:
        log.warn("log.settings_invalid", "{problem}", problem=problem)
    if capture_libraries:
        _install_library_bridge()


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Attach identifiers to every record written inside this block (``None`` is skipped)."""
    token = _CONTEXT.set(_merged(fields))
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def bind_log_context(**fields: Any) -> None:
    """Add identifiers to the current scope; they last until its :func:`log_context` ends.

    A run binds ``stage`` as it moves from step to step and clears it when it succeeds, so
    a failure recorded by the job, weekly or CLI caller still names the step that broke.
    """
    _CONTEXT.set(_merged(fields))


def _merged(fields: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(_CONTEXT.get() or {})
    for key, value in fields.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


def format_duration(seconds: float) -> str:
    """``41ms``, ``4.6s``, ``2m11s`` or ``1h02m``: short enough to scan in a log line."""
    if seconds < 0:
        seconds = 0
    if seconds < 1:
        return f"{round(seconds * 1000)}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    whole = round(seconds)
    if whole < 3600:
        return f"{whole // 60}m{whole % 60:02d}s"
    return f"{whole // 3600}h{whole % 3600 // 60:02d}m"


def _clean(text: str) -> str:
    """One line, no URL credentials, bounded length."""
    text = _URL_CREDENTIALS.sub(r"\1[redacted]@", text)
    text = " ".join(text.split())
    return text if len(text) <= _MAX_MESSAGE else text[: _MAX_MESSAGE - 1] + "…"


def redact_error(err: BaseException | None) -> str:
    """Reduce an exception to a short, non-leaking summary for logs and SQL columns."""
    if err is None:
        return "unknown error"
    message = str(err)[:_MAX_MESSAGE]
    message = _URL_CREDENTIALS.sub(r"\1[redacted]@", message)
    return f"{type(err).__name__}: {message}"


def mark_logged(err: BaseException) -> None:
    """Note that ``err`` was logged, so a library re-reporting it is not repeated."""
    with suppress(AttributeError, TypeError):
        err.__dict__["_alerts_bi_logged"] = True


def _is_logged(err: BaseException | None) -> bool:
    return bool(err is not None and getattr(err, "__dict__", {}).get("_alerts_bi_logged"))


class _MessageFormatter(string.Formatter):
    """``str.format`` plus ``{seconds:dur}``, ``{milliseconds:ms}`` and ``{instant:utc}``.

    ``{n} panel{n:plural}`` adds the plural ``s``; ``{n:,}`` already groups thousands.
    """

    def format_field(self, value: Any, format_spec: str) -> str:
        if format_spec == "dur":
            return format_duration(float(value))
        if format_spec == "ms":
            return format_duration(float(value) / 1000)
        if format_spec == "utc":
            return _instant(value, minutes=True, sep=" ").removesuffix("Z") + " UTC"
        if format_spec == "plural":
            return "" if value == 1 else "s"
        if isinstance(value, list | tuple | set | frozenset) and not format_spec:
            return ", ".join(str(item) for item in value)
        return str(super().format_field(value, format_spec))


_FORMATTER: Final = _MessageFormatter()


def _render_message(template: str | None, fields: Mapping[str, Any]) -> tuple[str, set[str]]:
    """Render ``template`` from ``fields``; return the text and the field names it used."""
    if not template:
        return "", set()
    try:
        used = {
            re.split(r"[.\[]", name, maxsplit=1)[0]
            for _, name, _, _ in _FORMATTER.parse(template)
            if name
        }
        return _clean_message(_FORMATTER.vformat(template, (), dict(fields))), used
    except Exception:
        # A logging mistake must never break the work being logged.
        return _clean_message(template), set()


def _clean_message(text: str) -> str:
    return _URL_CREDENTIALS.sub(r"\1[redacted]@", " ".join(text.split()))


def _redact(fields: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in fields.items():
        normalized = "".join(ch for ch in key.lower() if ch.isalpha() or ch == "_")
        out[key] = "[redacted]" if normalized in _REDACT_KEYS else value
    return out


def _instant(value: datetime | date, *, minutes: bool = False, sep: str = "T") -> str:
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=UTC)
        moment = moment.astimezone(UTC)
        if minutes:
            return moment.strftime(f"%Y-%m-%d{sep}%H:%MZ")
        return moment.isoformat(sep=sep, timespec="milliseconds").replace("+00:00", "Z")
    return value.isoformat()


def _stdlib_paths() -> tuple[str, ...]:
    paths = sysconfig.get_paths()
    return tuple(
        os.path.normcase(os.path.realpath(paths[key]))
        for key in ("stdlib", "platstdlib")
        if key in paths
    )


_STDLIB: Final = _stdlib_paths()


def _own_frame(filename: str) -> bool:
    # Compiled extensions (pymssql's Cython code) report a relative source name; Python
    # files of this application always have an absolute one.
    if not os.path.isabs(filename):
        return False
    normalized = os.path.normcase(os.path.realpath(filename))
    if "alerts_bi_" in normalized:
        return True
    if "site-packages" in normalized or "dist-packages" in normalized:
        return False
    return not normalized.startswith(_STDLIB) and not filename.startswith("<")


def _frame_path(filename: str) -> str:
    parts = PurePath(filename.replace("\\", "/")).parts
    for index in range(len(parts) - 1, -1, -1):
        if parts[index] == "src":
            return "/".join(parts[index + 1 :])
    for index, part in enumerate(parts):
        if part.startswith("alerts_bi_"):
            return "/".join(parts[index:])
    return parts[-1] if parts else filename


def _frames(traceback: TracebackType | None) -> tuple[list[str], int]:
    """This application's frames, innermost first, and how many library frames were left out."""
    entries: list[tuple[str, int, str]] = []
    while traceback is not None:
        code = traceback.tb_frame.f_code
        entries.append((code.co_filename, traceback.tb_lineno, code.co_name))
        traceback = traceback.tb_next
    frames: list[str] = []
    library = 0
    for filename, line, function in reversed(entries):
        if _own_frame(filename):
            if len(frames) < _MAX_FRAMES:
                frames.append(f"{_frame_path(filename)}:{line} in {function}")
        else:
            library += 1
    return frames, library


def _exception(err: BaseException) -> dict[str, Any]:
    """Type, redacted message, cause chain and this application's frames, innermost first.

    Only file, line and function: no source text and no local values, so nothing from an
    alert document or model payload can reach the log through a traceback.
    """
    frames, library = _frames(err.__traceback__)
    causes: list[str] = []
    seen = {id(err)}
    current = err.__cause__ or (None if err.__suppress_context__ else err.__context__)
    while current is not None and id(current) not in seen and len(causes) < 3:
        seen.add(id(current))
        # Where a wrapped error started is often the most useful line of all.
        origin, _ = _frames(current.__traceback__)
        where = f" (at {origin[0]})" if origin else ""
        causes.append(f"{type(current).__name__}: {_clean(str(current))}{where}")
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    return {
        "type": type(err).__name__,
        "message": _clean(str(err)),
        "frames": frames,
        "library_frames": library,
        "caused_by": causes,
    }


@dataclass(slots=True)
class _Record:
    ts: datetime
    level: str
    proc: str
    event: str
    message: str
    context: dict[str, Any]
    fields: dict[str, Any]
    used: set[str]
    exception: dict[str, Any] | None


_ANSI: Final = {
    "time": "\x1b[90m",
    "debug": "\x1b[90m",
    "info": "\x1b[32m",
    "warn": "\x1b[33m",
    "error": "\x1b[1;31m",
    "proc": "\x1b[35m",
    "event": "\x1b[90m",
    "fields": "\x1b[90m",
    "context": "\x1b[36m",
    "trace": "\x1b[37m",
}
_RESET: Final = "\x1b[0m"


def _console_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        text = f"{value:.3f}".rstrip("0").rstrip(".")
    elif isinstance(value, datetime | date):
        text = _instant(value)
    elif isinstance(value, list | tuple | set | frozenset):
        text = ",".join(str(item) for item in value)
    else:
        text = str(value)
    text = _clean_message(text)
    if text == "" or any(ch.isspace() or ch in '"=[]' for ch in text):
        return json.dumps(text, ensure_ascii=False)
    return text


def _short(key: str, value: Any) -> str:
    label, length = _CONTEXT_LABELS.get(key, (key, None))
    text = str(value)
    if length is not None and len(text) > length:
        text = text[:length]
    return f"{label}={_console_value(text)}"


def _console(record: _Record, color: bool) -> str:
    def paint(part: str, text: str) -> str:
        return f"{_ANSI[part]}{text}{_RESET}" if color and text else text

    head = " ".join(
        (
            paint("time", _instant(record.ts, sep=" ")),
            paint(record.level, f"{record.level.upper():<5}"),
            paint("proc", f"{record.proc:<{_PROC_WIDTH}}"),
            paint("event", f"{record.event:<{_EVENT_WIDTH}}"),
        )
    )
    tail_fields = [
        f"{key}={_console_value(value)}"
        for key, value in record.fields.items()
        if key not in record.used
        and value is not None
        and value != []
        and not (record.exception is not None and key == "error")
    ]
    parts = [head]
    if record.message:
        parts.append(" " + record.message)
    if tail_fields:
        parts.append("  " + paint("fields", " ".join(tail_fields)))
    # The stage matters when something goes wrong; on routine lines the event says it.
    quiet = record.level in ("debug", "info")
    context = [
        _short(key, record.context[key])
        for key in sorted(record.context, key=_context_order)
        if record.context[key] is not None and not (quiet and key == "stage")
    ]
    if context:
        parts.append("  " + paint("context", f"[{' '.join(context)}]"))
    line = "".join(parts)
    if record.exception is not None:
        exc = record.exception
        block = [f"    {exc['type']}: {exc['message']}"]
        block += [f"      at {frame}" for frame in exc["frames"]]
        if exc["library_frames"]:
            count = exc["library_frames"]
            block.append(f"      (+{count} library frame{'s' if count != 1 else ''})")
        block += [f"    caused by {cause}" for cause in exc["caused_by"]]
        line += "\n" + "\n".join(paint("trace", text) for text in block)
    return line


def _context_order(key: str) -> tuple[int, str]:
    return (CONTEXT_KEYS.index(key) if key in CONTEXT_KEYS else len(CONTEXT_KEYS), key)


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return _instant(value)
    if isinstance(value, set | frozenset):
        return sorted(value, key=str)
    return value


def _json(record: _Record) -> str:
    line: dict[str, Any] = {
        "ts": _instant(record.ts),
        "level": record.level,
        "proc": record.proc,
        "event": record.event,
        "msg": record.message,
    }
    for key in sorted(record.context, key=_context_order):
        line[key] = _json_value(record.context[key])
    for key, value in record.fields.items():
        line[f"field_{key}" if key in _RESERVED else key] = _json_value(value)
    if record.exception is not None:
        line["exception"] = record.exception
    return json.dumps(line, default=str, ensure_ascii=False)


def _stream() -> TextIO:
    return sys.stderr


def _emit(
    level: str,
    event: str,
    message: str | None,
    fields: dict[str, Any],
    exc: BaseException | None = None,
) -> None:
    settings = _current_settings()
    if _LEVELS[level] < settings.threshold:
        return
    try:
        context = dict(_CONTEXT.get() or {})
        body: dict[str, Any] = {}
        for key, value in _redact(fields).items():
            if key in CONTEXT_KEYS:
                if value is not None:
                    context[key] = value
            else:
                body[key] = value
        if exc is not None and "error" not in body:
            body["error"] = redact_error(exc)
        text, used = _render_message(message, {**context, **body})
        record = _Record(
            ts=datetime.now(UTC),
            level=level,
            proc=settings.proc,
            event=event,
            message=text,
            context=_redact(context),
            fields=body,
            used=used,
            exception=_exception(exc) if exc is not None else None,
        )
        stream = _stream()
        if settings.fmt == "json":
            line = _json(record)
        else:
            color = settings.color
            if color is None:
                color = bool(getattr(stream, "isatty", lambda: False)())
            line = _console(record, color)
        with _WRITE_LOCK:
            stream.write(line + "\n")
            stream.flush()
    except Exception:  # pragma: no cover - last resort; logging never breaks the caller
        with suppress(Exception):
            sys.stderr.write(f"logging failed for event {event}\n")
    if exc is not None:
        mark_logged(exc)


class _Logger:
    """Call sites read as ``log.info("es.read_schema", "Read {rows:,} rows", rows=n)``.

    The message is a ``str.format`` template over the fields and the current context; the
    console shows fields it does not mention after the message. ``exc`` attaches an
    exception's type, redacted message, cause chain and this application's frames.
    """

    @staticmethod
    def debug(
        event: str,
        message: str | None = None,
        /,
        *,
        exc: BaseException | None = None,
        **fields: Any,
    ) -> None:
        _emit("debug", event, message, fields, exc)

    @staticmethod
    def info(
        event: str,
        message: str | None = None,
        /,
        *,
        exc: BaseException | None = None,
        **fields: Any,
    ) -> None:
        _emit("info", event, message, fields, exc)

    @staticmethod
    def warn(
        event: str,
        message: str | None = None,
        /,
        *,
        exc: BaseException | None = None,
        **fields: Any,
    ) -> None:
        _emit("warn", event, message, fields, exc)

    @staticmethod
    def error(
        event: str,
        message: str | None = None,
        /,
        *,
        exc: BaseException | None = None,
        **fields: Any,
    ) -> None:
        _emit("error", event, message, fields, exc)

    @staticmethod
    def enabled(level: str) -> bool:
        """True when a record at ``level`` would be written; skips costly field building."""
        return _LEVELS[level] >= _current_settings().threshold


log = _Logger()


class _LibraryBridge(logging.Handler):
    """Render library :mod:`logging` records (uvicorn, Elasticsearch transport) as ours."""

    def emit(self, record: logging.LogRecord) -> None:
        err = record.exc_info[1] if record.exc_info else None
        if _is_logged(err):
            return  # already logged with its request or job context
        if record.levelno >= logging.ERROR:
            level = "error"
        elif record.levelno >= logging.WARNING:
            level = "warn"
        elif record.levelno >= logging.INFO:
            level = "info"
        else:
            level = "debug"
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        _emit(
            level,
            f"lib.{record.name}",
            _clean(message).replace("{", "{{").replace("}", "}}"),
            {},
            err,
        )


def _install_library_bridge() -> None:
    root = logging.getLogger()
    if not any(isinstance(handler, _LibraryBridge) for handler in root.handlers):
        root.addHandler(_LibraryBridge(level=logging.WARNING))
