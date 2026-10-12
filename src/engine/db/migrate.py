"""Migration runner, over Alembic.

Alembic owns ordering. Its revision graph is what makes two migrations added concurrently
collide as multiple heads rather than both applying in filename order and leaving the two
databases that ran them subtly different.

Alembic does not own content: ``alembic_version`` holds a single row naming the current
head, so an edit to an already-applied revision is invisible to it. The checksum ledger in
:mod:`alerts_bi_admin.engine.db.ledger` covers that, and is verified before every upgrade.

Tests do not substitute SQLite or another engine, because a constraint that only exists in
one dialect is a constraint that was never tested.
"""

from __future__ import annotations

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from alerts_bi_shared.db.connection import Database, connect, quote_identifier
from alerts_bi_shared.logging_setup import log

from ..config import SqlConfig
from .ledger import MIGRATIONS_DIR, Migration, load_migrations, read_ledger

__all__ = [
    "MIGRATIONS_DIR",
    "Migration",
    "alembic_config",
    "applied_migrations",
    "current_revision",
    "heads",
    "load_migrations",
    "migrate",
    "migrate_database",
    "reset_test_database",
    "stamp",
]


def alembic_config(sql: SqlConfig | None = None, database: str | None = None) -> Config:
    """Alembic configuration pointed at this package's migrations.

    Built in code rather than read from ``alembic.ini`` so the runner works from an
    installed wheel, where there is no repository to find an ini file in. The ini exists so
    the ``alembic`` CLI works too, for ``history`` and ``heads``.
    """
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    if sql is not None:
        config.attributes["sql_config"] = sql
    if database is not None:
        config.attributes["database"] = database
    return config


def migrate(sql: SqlConfig, database: str | None = None) -> tuple[list[str], list[str]]:
    """Apply every revision not yet applied.

    Returns ``(newly_applied, already_applied)`` in revision order, read from the checksum
    ledger either side of the upgrade, so a caller can report what it did without asking
    Alembic a second question.
    """
    target = database or sql.database

    with connect(sql, target) as db:
        before = set(read_ledger(db.connection))

    command.upgrade(alembic_config(sql, target), "head")

    with connect(sql, target) as db:
        after = set(read_ledger(db.connection))

    ordered = [migration.version for migration in load_migrations()]
    return (
        [version for version in ordered if version in after and version not in before],
        [version for version in ordered if version in before],
    )


def applied_migrations(db: Database) -> dict[str, str]:
    """``{version: checksum}`` for migrations already applied."""
    return read_ledger(db.connection)


def current_revision(db: Database) -> str | None:
    """The head Alembic believes this database is at, or ``None`` before any upgrade."""
    if db.query_one("SELECT 1 AS present WHERE OBJECT_ID('alembic_version', 'U') IS NOT NULL"):
        row = db.query_one("SELECT version_num FROM alembic_version")
        return None if row is None else str(row["version_num"])
    return None


def heads() -> list[str]:
    """Every head in the revision graph. More than one means two migrations collided."""
    return list(ScriptDirectory.from_config(alembic_config()).get_heads())


def migrate_database(config: SqlConfig, database: str) -> tuple[list[str], list[str]]:
    """Create the target database if it does not exist, then apply migrations to it."""
    quoted = quote_identifier(database)
    with connect(config, "master", autocommit=True) as master:
        exists = master.query_one(
            "SELECT 1 AS present FROM sys.databases WHERE name = :name", {"name": database}
        )
        if exists is None:
            master.execute(f"CREATE DATABASE {quoted}")
            log.info("db.database_created", database=database)

    return migrate(config, database)


def stamp(config: SqlConfig, database: str, revision: str = "head") -> None:
    """Record a revision as applied without running it.

    For a database migrated before Alembic was adopted: its schema and its checksum ledger
    are already correct, and only ``alembic_version`` is missing.
    """
    command.stamp(alembic_config(config, database), revision)
    log.info("db.stamped", database=database, revision=revision)


def reset_test_database(config: SqlConfig, database: str) -> None:
    """Drop and recreate ONLY the explicitly named disposable test database.

    Guarded on purpose: this is the one destructive operation in the codebase. It refuses
    any database whose name is not the configured test database, so a mistyped environment
    variable cannot take out ``alerts_bi_dev`` or a production store.
    """
    if database != config.test_database:
        raise ValueError(
            f"refusing to reset {database!r}: only the configured test database "
            f"({config.test_database!r}) may be recreated"
        )
    if "test" not in database.lower():
        raise ValueError(
            f'refusing to reset {database!r}: a disposable test database name must contain "test"'
        )

    quoted = quote_identifier(database)
    with connect(config, "master", autocommit=True) as master:
        # Single-user mode rolls back open sessions so the drop cannot hang behind a
        # connection left over from an interrupted test run.
        master.execute(
            f"""
            IF EXISTS (SELECT 1 FROM sys.databases WHERE name = N'{database}')
            BEGIN
              ALTER DATABASE {quoted} SET SINGLE_USER WITH ROLLBACK IMMEDIATE;
              DROP DATABASE {quoted};
            END
            """
        )
        master.execute(f"CREATE DATABASE {quoted}")
        log.info("db.test_database_recreated", database=database)

    migrate(config, database)
