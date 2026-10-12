"""Migrations, against a real disposable SQL Server database.

Alembic owns ordering; the checksum ledger owns content. Both halves are tested here,
because losing either is silent: a changed migration that re-applies leaves a database in a
state no version describes, and two migrations added concurrently would otherwise both
apply in filename order, leaving the databases that ran them subtly different.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alerts_bi_shared.db.connection import connect
from src.engine.config import load_config
from src.engine.db.ledger import (
    MIGRATIONS_DIR,
    MigrationDrift,
    load_migrations,
    read_ledger,
    verify_ledger,
)
from src.engine.db.migrate import (
    applied_migrations,
    current_revision,
    heads,
    migrate,
    reset_test_database,
)

pytestmark = pytest.mark.integration

CONFIG = load_config()


@pytest.fixture(scope="module")
def fresh_database() -> Iterator[str]:
    """A database built from nothing, entirely through Alembic."""
    try:
        reset_test_database(CONFIG.sql, CONFIG.sql.test_database)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"SQL Server is not reachable: {exc}")
    return_value = CONFIG.sql.test_database
    yield return_value


# --------------------------------------------------------------- the revision graph


def test_the_revision_graph_has_exactly_one_head() -> None:
    """Two heads mean two migrations were added without agreeing on an order."""
    assert len(heads()) == 1, f"multiple heads: {heads()}"


def test_every_sql_file_has_a_revision_module_of_the_same_name() -> None:
    """The pairing is what keeps the graph and the checksum ledger describing one thing."""
    revisions = {path.stem for path in (MIGRATIONS_DIR / "versions").glob("*.py")}
    for migration in load_migrations():
        assert migration.version in revisions, (
            f"{migration.filename} has no revision module; it would never run"
        )


def test_the_head_revision_is_the_last_sql_file_in_order() -> None:
    assert heads() == [load_migrations()[-1].version]


# ------------------------------------------------------------- applying them


def test_a_database_built_from_nothing_reaches_head(fresh_database: str) -> None:
    with connect(CONFIG.sql, fresh_database) as db:
        assert current_revision(db) == heads()[0]


def test_every_migration_is_recorded_with_the_checksum_of_the_sql_that_ran(
    fresh_database: str,
) -> None:
    expected = {m.version: m.checksum for m in load_migrations()}
    with connect(CONFIG.sql, fresh_database) as db:
        assert applied_migrations(db) == expected


def test_migrating_again_applies_nothing_and_reports_it(fresh_database: str) -> None:
    newly_applied, already_applied = migrate(CONFIG.sql, fresh_database)
    assert newly_applied == []
    assert already_applied == [m.version for m in load_migrations()]


def test_the_schema_the_pipeline_needs_is_actually_there(fresh_database: str) -> None:
    """A migration that ran but created nothing would satisfy every check above."""
    with connect(CONFIG.sql, fresh_database) as db:
        tables = {str(row["name"]) for row in db.query("SELECT name FROM sys.tables")}
    assert {
        "runs",
        "daily_metrics",
        "daily_rule_counts",
        "alert_findings",
        "llm_batch_attempts",
        "llm_verdicts",
        "panel_parses",
        "run_panels",
    } <= tables
    assert "alembic_version" in tables, "Alembic's own ordering table"
    assert "schema_migrations" in tables, "the checksum ledger"


# ------------------------------------------------------------- content drift


def test_an_applied_migration_whose_file_changed_is_refused(
    fresh_database: str, tmp_path: Path
) -> None:
    """Alembic cannot see this: alembic_version records a revision id, never its content."""
    original = load_migrations()[0]
    tampered = tmp_path / f"{original.version}.sql"
    tampered.write_text(original.sql_text + "\n-- edited after being applied\n", encoding="utf-8")

    with (
        connect(CONFIG.sql, fresh_database) as db,
        pytest.raises(MigrationDrift, match="has changed since"),
    ):
        verify_ledger(db.connection, tmp_path)


def test_an_unchanged_migration_verifies_cleanly(fresh_database: str) -> None:
    with connect(CONFIG.sql, fresh_database) as db:
        verify_ledger(db.connection)  # must not raise


def test_a_ledger_row_with_no_file_on_disk_is_tolerated(
    fresh_database: str, tmp_path: Path
) -> None:
    """An older database is not broken by a revision this checkout no longer carries."""
    with connect(CONFIG.sql, fresh_database) as db:
        assert read_ledger(db.connection), "the ledger is populated"
        verify_ledger(db.connection, tmp_path)  # empty directory: nothing to compare


# ------------------------------------------------------- the destructive guard


@pytest.mark.parametrize("target", ["alerts_bi_dev", "master", "production"])
def test_the_reset_refuses_any_database_that_is_not_the_configured_test_one(
    target: str,
) -> None:
    with pytest.raises(ValueError, match="refusing to reset"):
        reset_test_database(CONFIG.sql, target)
