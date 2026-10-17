"""SQL Server connection settings.

``test_database`` is not merely another target: it is the only database the guarded reset
in :mod:`alerts_bi_admin.engine.db.migrate` will destroy, so it is configuration in its own right rather
than a value a caller passes in.

``encrypt`` and ``trust_server_certificate`` are applied, or refused when the driver cannot
enforce them, by :func:`alerts_bi_shared.db.connection.encryption_connect_args`.
"""

from __future__ import annotations

from dataclasses import dataclass

from alerts_bi_shared.config.env import read_bool, read_int, read_str

__all__ = ["SqlConfig", "load_sql_config"]


@dataclass(frozen=True, slots=True)
class SqlConfig:
    host: str
    port: int
    user: str
    password: str
    database: str
    test_database: str
    encrypt: bool
    trust_server_certificate: bool
    request_timeout_ms: int


def load_sql_config() -> SqlConfig:
    return SqlConfig(
        host=read_str("SQL_HOST", "localhost"),
        port=read_int("SQL_PORT", 1433),
        user=read_str("SQL_USER", "sa"),
        password=read_str("SQL_PASSWORD"),
        database=read_str("SQL_DATABASE", "alerts_bi_dev"),
        test_database=read_str("SQL_TEST_DATABASE", "alerts_bi_test"),
        # Strict: a typo such as SQL_ENCRYPT=require must not silently mean plaintext.
        encrypt=read_bool("SQL_ENCRYPT", False, strict=True),
        trust_server_certificate=read_bool("SQL_TRUST_SERVER_CERTIFICATE", True),
        request_timeout_ms=read_int("SQL_REQUEST_TIMEOUT_MS", 60000),
    )
