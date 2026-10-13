"""Committed web reads remain available while a writer owns conflicting row locks."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from alerts_bi_shared.db.connection import WebDatabasePool, connect
from src.engine.config import load_config
from src.engine.db.migrate import reset_test_database
from src.engine.db.repositories import PersistencePayload, persist_run

from tests.engine.helpers.sql import sample_run

pytestmark = pytest.mark.integration


def test_versioned_pool_reads_committed_value_during_write_and_new_value_after_commit() -> None:
    config = load_config()
    database = config.sql.test_database
    assert config.sql.host in ("localhost", "127.0.0.1") and database == "alerts_bi_test"
    reset_test_database(config.sql, database)
    with connect(config.sql, "master", autocommit=True) as master:
        master.execute(
            "ALTER DATABASE [alerts_bi_test] SET READ_COMMITTED_SNAPSHOT ON WITH NO_WAIT"
        )
    pool = WebDatabasePool(config.sql, database, pool_size=1, max_overflow=0)

    def read() -> str:
        with pool.connect() as db:
            row = db.query_one("SELECT team_display_name FROM runs")
            assert row is not None
            return str(row["team_display_name"])

    try:
        with connect(config.sql, database) as writer:
            persist_run(writer, PersistencePayload(run=sample_run()))
            writer.execute("UPDATE runs SET team_display_name='committed later'")
            with ThreadPoolExecutor(max_workers=1) as threads:
                future = threads.submit(read)
                try:
                    assert future.result(timeout=5) == "Checkout API"
                finally:
                    # Release even on failure, so the check cannot leave a blocked reader.
                    writer.commit()
            assert read() == "committed later"
    finally:
        pool.dispose()
        with connect(config.sql, "master", autocommit=True) as master:
            master.execute(
                "ALTER DATABASE [alerts_bi_test] SET READ_COMMITTED_SNAPSHOT OFF WITH NO_WAIT"
            )
