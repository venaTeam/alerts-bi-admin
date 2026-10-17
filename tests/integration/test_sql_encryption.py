"""SQL_ENCRYPT=true produces an encrypted session on a real SQL Server.

``sys.dm_exec_connections`` needs VIEW SERVER STATE, which the local ``sa`` login has.
"""

from __future__ import annotations

import dataclasses

import pytest
from alerts_bi_shared.db.connection import connect
from src.engine.config import load_config

pytestmark = pytest.mark.integration

CONFIG = load_config()
ENCRYPTION_QUERY = "SELECT encrypt_option FROM sys.dm_exec_connections WHERE session_id = @@SPID"


@pytest.mark.parametrize(("encrypt", "expected"), [(True, "TRUE"), (False, "FALSE")])
def test_session_encryption_follows_sql_encrypt(encrypt: bool, expected: str) -> None:
    config = dataclasses.replace(CONFIG.sql, encrypt=encrypt, trust_server_certificate=True)
    with connect(config, "master") as db:
        row = db.query_one(ENCRYPTION_QUERY)
    assert row is not None and row["encrypt_option"] == expected
