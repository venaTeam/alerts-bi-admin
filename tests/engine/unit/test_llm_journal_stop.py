"""The audit journal stops writing once a write fails or its review ends.

After a lost connection SQLAlchemy would run the next statement on a fresh session that
holds none of the run's locks, so the run's other request threads must not write there.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from alerts_bi_shared.db.connection import Database
from src.engine.db.llm_audit import JournalStopped, SqlLlmJournal

NOW = datetime(2026, 10, 27, tzinfo=UTC)


class FakeDatabase:
    """Records statements; fails the statement whose SQL contains ``fail_on``."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.fail_on = fail_on
        self.statements: list[str] = []

    def _run(self, sql: str) -> None:
        self.statements.append(sql)
        if self.fail_on is not None and self.fail_on in sql:
            raise ConnectionError("connection lost")

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        self._run(sql)

    def query_one(self, sql: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        self._run(sql)
        return {"result": 0} if "sp_getapplock" in sql else None

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    @contextmanager
    def transaction(self) -> Iterator[FakeDatabase]:
        yield self


def journal(db: FakeDatabase) -> SqlLlmJournal:
    return SqlLlmJournal(cast(Database, db), "a" * 64)


def test_a_failed_batch_write_stops_every_later_one_without_touching_sql() -> None:
    db = FakeDatabase(fail_on="INSERT INTO llm_review_attempts")
    audit = journal(db)
    with pytest.raises(ConnectionError):
        audit.start("cycle", 1, NOW)
    written = len(db.statements)

    db.fail_on = None
    with pytest.raises(JournalStopped):
        audit.attempt("cycle", 2)
    with pytest.raises(JournalStopped):
        audit.finish(
            "cycle",
            1,
            status="succeeded",
            response="{}",
            reason=None,
            metadata={},
            duration_ms=1,
            completed_at=NOW,
        )
    with pytest.raises(JournalStopped):
        audit.complete_batch("cycle", succeeded=False)
    assert len(db.statements) == written


def test_batch_writes_stop_once_the_review_lock_is_released() -> None:
    db = FakeDatabase()
    audit = journal(db)
    with audit.locked():
        audit.start("cycle", 1, NOW)
    written = len(db.statements)

    with pytest.raises(JournalStopped):
        audit.start("cycle", 2, NOW)
    assert len(db.statements) == written
