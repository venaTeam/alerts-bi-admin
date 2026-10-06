"""Unit tests of readiness's adapter boundary; real SQL checks live in design integration."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import cast
from unittest.mock import Mock

import pytest
from alerts_bi_shared.db.connection import Database
from fastapi.testclient import TestClient
from src import app
from src.config import AdminSettings, load_config
from src.readiness import REQUIRED_COLUMNS


def client_for(monkeypatch: pytest.MonkeyPatch, db: Mock) -> TestClient:
    @contextmanager
    def connect(*args: object, **kwargs: object) -> Iterator[Database]:
        yield cast(Database, db)

    monkeypatch.setattr(app, "connect", connect)
    settings = AdminSettings(config=load_config(), database="alerts_bi_test", secret="x" * 40)
    return TestClient(app.build_admin(settings), headers={"X-Forwarded-User": "operator"})


def test_healthy_schema_checks_all_operator_tables_without_fetching_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = Mock(spec=Database)
    db.query.return_value = []
    with client_for(monkeypatch, db) as client:
        response = client.get("/healthz")
    assert response.status_code == 200 and response.text == "ok"
    statements = [call.args[0] for call in db.query.call_args_list]
    assert len(statements) == 9
    assert all(statement.startswith("SELECT TOP 0 ") for statement in statements)
    assert {statement.rsplit(" FROM ", 1)[1] for statement in statements} == {
        f"[{table}]" for table in REQUIRED_COLUMNS
    }
    assert "[open_since]" in " ".join(statements)
    assert "[unseen_unmeasured]" in " ".join(statements)
    assert "[withdrawn_reason]" in " ".join(statements)


@pytest.mark.parametrize("missing", [*sorted(REQUIRED_COLUMNS), "open_since"])
def test_missing_table_or_column_returns_generic_503(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    db = Mock(spec=Database)

    def query(statement: str) -> list[dict[str, object]]:
        if f"[{missing}]" in statement:
            raise RuntimeError(f"SQL driver: missing {missing}; private connection details")
        return []

    db.query.side_effect = query
    with client_for(monkeypatch, db) as client:
        response = client.get("/healthz")
    assert response.status_code == 503
    assert "private connection" not in response.text
    assert missing not in response.text
    assert db.query.call_count <= len(REQUIRED_COLUMNS)
