"""SQL Server access through SQLAlchemy Core.

The store is the non-negotiable part of this design: Elasticsearch retains three months
and the pre-project period is expiring at a rate of one day per day, so a run that cannot
persist has not done its job. Reports are therefore rendered only from committed rows.

SQLAlchemy is a toolkit over a driver, not a driver: the DBAPI underneath is still
``pymssql``, reached through the ``mssql+pymssql`` dialect. Nothing about the schema, the
constraints or the transaction boundaries changes with it; SQL is still written by hand and
executed as text. The ORM is deliberately unused - the pipeline persists rows it has already
computed and reads them back for rendering, so an identity map and lazy loading would add
machinery with nothing to do.

Two execution paths, matching what each statement actually is:

* **Parameterized statements** go through ``text()`` with ``:name`` binds, which the dialect
  renders into the driver's ``pyformat`` placeholders and escapes literal ``%`` for.
* **Parameterless statements** - the DDL, the guarded database reset - go through
  ``exec_driver_sql``, which hands the text to the driver untouched. ``text()`` reads
  ``:word`` as a bind parameter, and the migration DDL carries colons in its comments;
  parsing those would be a change in meaning for no benefit.

Dedicated execution connections are not pooled. Each :func:`connect` builds an engine with ``NullPool``, opens
one connection and disposes the engine on exit, which is exactly the lifetime the pipeline
had before. A pool would hold connections open across the guarded test-database reset, which
drops the database out from under them. WebDatabasePool provides separate bounded request
connections; close application lifespans before resetting a disposable database.

``SQL_ENCRYPT=true`` makes every connection require TLS; see :func:`encryption_connect_args`.
The default leaves connections unencrypted, and each process logs that once when its first
pool or connection is set up, so the condition shows in every service's startup output.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from importlib.metadata import version as installed_version
from threading import Lock
from typing import Any

from sqlalchemy import URL, create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.pool import NullPool

from alerts_bi_shared.config.sql import SqlConfig
from alerts_bi_shared.logging_setup import log, redact_error

__all__ = [
    "Database",
    "WebDatabasePool",
    "connect",
    "encryption_connect_args",
    "engine_url",
    "quote_identifier",
]

# pymssql applies its ``encryption`` argument per connection only from 2.4.3. Earlier wheels
# accept it and connect in plaintext (checked against SQL Server 2022 on 2026-10-17).
_MIN_ENCRYPTING_PYMSSQL = (2, 4, 3)

_unencrypted_warned: set[tuple[str, int]] = set()
_unencrypted_lock = Lock()


def encryption_connect_args(config: SqlConfig) -> dict[str, str]:
    """Driver arguments that apply SQL_ENCRYPT, refusing settings the driver cannot enforce.

    With ``encrypt`` the connection requires TLS: FreeTDS refuses a server that will not
    encrypt instead of falling back to plaintext. FreeTDS validates the server certificate
    only through its own configuration file, which this application does not manage, so the
    certificate is trusted, and a request to validate it is refused rather than skipped.
    Without ``encrypt`` nothing changes, and the process logs that once per server.
    """
    if not config.encrypt:
        _warn_unencrypted(config)
        return {}
    if not config.trust_server_certificate:
        raise ValueError(
            "SQL_TRUST_SERVER_CERTIFICATE=false is not supported: the pymssql driver cannot "
            "validate the SQL Server certificate. Set SQL_TRUST_SERVER_CERTIFICATE=true to "
            "encrypt without certificate validation."
        )
    driver = installed_version("pymssql")
    release = re.match(r"(\d+)\.(\d+)\.(\d+)", driver)
    if release is None or tuple(map(int, release.groups())) < _MIN_ENCRYPTING_PYMSSQL:
        raise RuntimeError(
            f"SQL_ENCRYPT=true needs pymssql 2.4.3 or later; pymssql {driver} would connect "
            "unencrypted."
        )
    return {"encryption": "require"}


def _warn_unencrypted(config: SqlConfig) -> None:
    with _unencrypted_lock:
        server = (config.host, config.port)
        if server in _unencrypted_warned:
            return
        _unencrypted_warned.add(server)
    log.warn(
        "sql.unencrypted",
        host=config.host,
        port=config.port,
        detail="SQL_ENCRYPT=false: SQL Server traffic is not encrypted",
    )


class WebDatabasePool:
    """One bounded pool per web app; never used for execution locks or DB resets.

    Construction is lazy so creating an application does not contact SQL. A connection
    belongs to exactly one request and is rolled back on return. Shutdown disposes all
    idle connections. Worker/migration callers continue to use unpooled ``connect``.
    """

    def __init__(
        self, config: SqlConfig, database: str, *, pool_size: int = 5, max_overflow: int = 5
    ) -> None:
        if pool_size < 1 or max_overflow < 0:
            raise ValueError("Web pool limits must be bounded and positive")
        # Resolved now so an unenforceable SQL_ENCRYPT setting stops the app at startup.
        self._encryption = encryption_connect_args(config)
        self.config = config
        self.database = database
        self.pool_size = pool_size
        self.max_overflow = max_overflow
        self._engine: Engine | None = None
        self._lock = Lock()

    def _get_engine(self) -> Engine:
        with self._lock:
            if self._engine is None:
                timeout = max(1, self.config.request_timeout_ms // 1000)
                self._engine = create_engine(
                    engine_url(self.config, self.database),
                    pool_size=self.pool_size,
                    max_overflow=self.max_overflow,
                    pool_timeout=5,
                    pool_pre_ping=True,
                    pool_recycle=1800,
                    hide_parameters=True,
                    connect_args={
                        "timeout": timeout,
                        "login_timeout": timeout,
                        **self._encryption,
                    },
                )
            return self._engine

    @contextmanager
    def connect(self) -> Iterator[Database]:
        with self._get_engine().connect() as connection:
            yield Database(connection)

    def dispose(self) -> None:
        with self._lock:
            if self._engine is not None:
                self._engine.dispose()
                self._engine = None


def quote_identifier(name: str) -> str:
    """Quote a SQL Server identifier, rejecting anything that is not a plain name.

    Database names reach this from configuration and the reset path is destructive, so the
    safe set is deliberately narrow.
    """
    if not name or len(name) > 128:
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    if not (name[0].isalpha() or name[0] == "_"):
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    if not all(ch.isalnum() or ch == "_" for ch in name):
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    return f"[{name}]"


def engine_url(config: SqlConfig, database: str | None = None) -> URL:
    """Build the connection URL.

    ``URL.create`` is used rather than a formatted string so a password containing ``@``,
    ``/`` or ``:`` is escaped by SQLAlchemy instead of silently truncating the URL.
    """
    return URL.create(
        "mssql+pymssql",
        username=config.user,
        password=config.password,
        host=config.host,
        port=config.port,
        database=database if database is not None else config.database,
    )


class Database:
    """A connection plus the few helpers the pipeline needs."""

    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        if params is None:
            self.connection.exec_driver_sql(sql)
            return
        self.connection.execute(text(sql), params)

    def execute_many(self, sql: str, rows: Sequence[dict[str, Any]]) -> None:
        if not rows:
            return
        self.connection.execute(text(sql), list(rows))

    def query(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        result = (
            self.connection.exec_driver_sql(sql)
            if params is None
            else self.connection.execute(text(sql), params)
        )
        return [dict(row) for row in result.mappings()]

    def query_one(self, sql: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def commit(self) -> None:
        self.connection.commit()

    def rollback(self) -> None:
        self.connection.rollback()

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def transaction(self) -> Iterator[Database]:
        """Run a block inside a transaction, rolling back on any error.

        Persistence is all-or-nothing per run: a half-written run would be
        indistinguishable from a complete one when the report is rendered from SQL.
        """
        try:
            yield self
            self.commit()
        except BaseException:
            try:
                self.rollback()
            except Exception as rollback_error:
                log.error("sql.rollback_failed", error=redact_error(rollback_error))
            raise


@contextmanager
def connect(
    config: SqlConfig, database: str | None = None, autocommit: bool = False
) -> Iterator[Database]:
    """Open a connection to one database, closing it on exit."""
    timeout_seconds = int(config.request_timeout_ms / 1000)
    engine = create_engine(
        engine_url(config, database),
        poolclass=NullPool,
        # Audit parameters contain full alerts and model responses; never print them
        # through a SQLAlchemy exception when a database write fails.
        hide_parameters=True,
        connect_args={
            "timeout": timeout_seconds,
            "login_timeout": timeout_seconds,
            **encryption_connect_args(config),
        },
    )
    connection = engine.connect()
    if autocommit:
        # CREATE DATABASE and DROP DATABASE cannot run inside a transaction.
        connection = connection.execution_options(isolation_level="AUTOCOMMIT")
    db = Database(connection)
    try:
        yield db
    finally:
        db.close()
        engine.dispose()
