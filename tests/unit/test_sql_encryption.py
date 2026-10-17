"""SQL_ENCRYPT is applied by the driver, or refused when the driver cannot enforce it."""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest
import sqlalchemy
from alembic import command
from alerts_bi_shared.config.sql import SqlConfig
from alerts_bi_shared.db import connection
from alerts_bi_shared.db.connection import WebDatabasePool, connect, encryption_connect_args
from src.engine.db.migrate import alembic_config

CONFIG = SqlConfig(
    host="sql.internal",
    port=1433,
    user="app",
    password="test-placeholder",
    database="alerts_bi_dev",
    test_database="alerts_bi_test",
    encrypt=False,
    trust_server_certificate=True,
    request_timeout_ms=60000,
)
ENCRYPTED = dataclasses.replace(CONFIG, encrypt=True)


@pytest.fixture(autouse=True)
def fresh_warning_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(connection, "_unencrypted_warned", set())


def _unencrypted_warnings(stderr: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stderr.splitlines() if '"sql.unencrypted"' in line]


class _Engine:
    def connect(self) -> _Connection:
        return _Connection()

    def dispose(self) -> None:
        pass


class _Connection:
    def close(self) -> None:
        pass


def test_default_connections_are_unchanged_and_logged_once_per_server(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert encryption_connect_args(CONFIG) == {}
    WebDatabasePool(CONFIG, CONFIG.database).dispose()
    WebDatabasePool(CONFIG, "other_database").dispose()
    warnings = _unencrypted_warnings(capsys.readouterr().err)
    assert len(warnings) == 1
    assert warnings[0]["level"] == "warn"
    assert (warnings[0]["host"], warnings[0]["port"]) == ("sql.internal", 1433)
    assert "test-placeholder" not in json.dumps(warnings[0])

    WebDatabasePool(dataclasses.replace(CONFIG, host="other-sql"), CONFIG.database).dispose()
    assert len(_unencrypted_warnings(capsys.readouterr().err)) == 1


def test_sql_encrypt_requires_tls_without_an_unencrypted_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert encryption_connect_args(ENCRYPTED) == {"encryption": "require"}
    assert _unencrypted_warnings(capsys.readouterr().err) == []


@pytest.mark.parametrize("encrypt", [False, True])
def test_pools_and_connections_pass_the_encryption_to_the_driver(
    monkeypatch: pytest.MonkeyPatch, encrypt: bool
) -> None:
    calls: list[dict[str, Any]] = []

    def create_engine(url: object, **kwargs: Any) -> _Engine:
        calls.append(kwargs["connect_args"])
        return _Engine()

    monkeypatch.setattr(connection, "create_engine", create_engine)
    config = dataclasses.replace(CONFIG, encrypt=encrypt)
    pool = WebDatabasePool(config, config.database)
    pool._get_engine()
    with connect(config):
        pass
    expected = {"encryption": "require"} if encrypt else {}
    assert [{k: v for k, v in args.items() if k == "encryption"} for args in calls] == [
        expected,
        expected,
    ]
    assert all(args["login_timeout"] == 60 for args in calls)


def test_certificate_validation_cannot_be_promised_and_stops_startup() -> None:
    strict = dataclasses.replace(ENCRYPTED, trust_server_certificate=False)
    with pytest.raises(ValueError, match="cannot validate the SQL Server certificate"):
        encryption_connect_args(strict)
    with pytest.raises(ValueError, match="SQL_TRUST_SERVER_CERTIFICATE=false"):
        WebDatabasePool(strict, strict.database)


@pytest.mark.parametrize("driver", ["2.3.13", "2.4.2", "unknown"])
def test_a_driver_that_would_connect_unencrypted_is_refused(
    monkeypatch: pytest.MonkeyPatch, driver: str
) -> None:
    monkeypatch.setattr(connection, "installed_version", lambda name: driver)
    with pytest.raises(RuntimeError, match=f"pymssql {driver} would connect unencrypted"):
        WebDatabasePool(ENCRYPTED, ENCRYPTED.database)
    monkeypatch.setattr(connection, "installed_version", lambda name: "2.4.3")
    assert encryption_connect_args(ENCRYPTED) == {"encryption": "require"}


class _EngineCreated(Exception):
    pass


@pytest.mark.parametrize("encrypt", [False, True])
def test_migrations_connect_with_the_same_encryption(
    monkeypatch: pytest.MonkeyPatch, encrypt: bool
) -> None:
    """The Alembic environment builds its own engine; it must not bypass SQL_ENCRYPT."""
    seen: list[dict[str, Any]] = []

    def create_engine(url: object, **kwargs: Any) -> None:
        seen.append(kwargs)
        raise _EngineCreated

    # migrations/env.py imports create_engine from sqlalchemy each time Alembic runs it.
    monkeypatch.setattr(sqlalchemy, "create_engine", create_engine)
    config = dataclasses.replace(CONFIG, encrypt=encrypt)
    with pytest.raises(_EngineCreated):
        command.upgrade(alembic_config(config, config.database), "head")
    assert seen == [
        {
            "poolclass": seen[0]["poolclass"],
            "connect_args": {"encryption": "require"} if encrypt else {},
        }
    ]
