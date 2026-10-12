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

Connections are not pooled. Each :func:`connect` builds an engine with ``NullPool``, opens
one connection and disposes the engine on exit, which is exactly the lifetime the pipeline
had before. A pool would hold connections open across the guarded test-database reset, which
drops the database out from under them.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from sqlalchemy import URL, create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.pool import NullPool

from alerts_bi_shared.config.sql import SqlConfig
from alerts_bi_shared.logging_setup import log, redact_error

__all__ = ["Database", "connect", "engine_url", "quote_identifier"]


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
        connect_args={"timeout": timeout_seconds, "login_timeout": timeout_seconds},
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
