"""The checksum ledger: what was applied, and whether it still says what it said.

Alembic orders migrations and refuses ambiguous history, which is what it is here for. It
does not verify content: ``alembic_version`` holds a single row naming the current head, so
editing an already-applied revision is invisible to it. Silently re-applying or ignoring
such an edit would leave the database in a state no version describes.

``schema_migrations`` is therefore kept alongside ``alembic_version`` and does the other
half of the job: one row per applied migration, carrying the SHA-256 of the SQL that ran.
Every upgrade verifies the ledger before touching anything and appends to it afterwards.

The DDL lives in ``.sql`` files rather than inside the revision modules, because the
checksum has to cover the text that actually ran. The revision is a few lines that name
their file; the schema is the file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from alerts_bi_shared.hashing import sha256_text
from alerts_bi_shared.logging_setup import log
from sqlalchemy import Connection, text

__all__ = [
    "MIGRATIONS_DIR",
    "Migration",
    "MigrationDrift",
    "apply_sql",
    "ensure_ledger",
    "load_migrations",
    "migration_named",
    "read_ledger",
    "verify_ledger",
]

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

#: ``GO`` is a batch separator understood by sqlcmd, not by the driver.
_GO = re.compile(r"^\s*GO\s*$", re.MULTILINE | re.IGNORECASE)


class MigrationDrift(RuntimeError):
    """An applied migration's file no longer matches what was applied."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: str
    filename: str
    sql_text: str
    checksum: str


def load_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Read the SQL migrations from disk in version order.

    Line endings are normalized before hashing, so a CRLF checkout does not read as a
    changed migration.
    """
    migrations: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        sql_text = path.read_bytes().decode("utf-8")
        migrations.append(
            Migration(
                version=path.stem,
                filename=path.name,
                sql_text=sql_text,
                checksum=sha256_text(sql_text.replace("\r\n", "\n")),
            )
        )
    return migrations


def migration_named(version: str, directory: Path = MIGRATIONS_DIR) -> Migration:
    for migration in load_migrations(directory):
        if migration.version == version:
            return migration
    raise FileNotFoundError(f"no SQL file for migration {version!r} in {directory}")


def ensure_ledger(connection: Connection) -> None:
    connection.exec_driver_sql(
        """
        IF OBJECT_ID('schema_migrations', 'U') IS NULL
        CREATE TABLE schema_migrations (
          version    NVARCHAR(128) NOT NULL CONSTRAINT pk_schema_migrations PRIMARY KEY,
          checksum   NVARCHAR(64)  NOT NULL,
          applied_at DATETIME2(3)  NOT NULL
        );
        """
    )


def read_ledger(connection: Connection) -> dict[str, str]:
    """``{version: checksum}`` for migrations already applied."""
    ensure_ledger(connection)
    rows = connection.execute(text("SELECT version, checksum FROM schema_migrations")).mappings()
    return {str(row["version"]): str(row["checksum"]) for row in rows}


def verify_ledger(connection: Connection, directory: Path = MIGRATIONS_DIR) -> None:
    """Refuse to proceed when an applied migration's file has changed since.

    A migration recorded in the ledger with no file on disk is left alone: that is what an
    older database looks like when a revision is removed from a newer checkout, and failing
    on it would make the tree unable to talk to its own history.
    """
    applied = read_ledger(connection)
    on_disk = {migration.version: migration for migration in load_migrations(directory)}

    drifted = [
        f"{version}: applied {applied[version][:12]}..., file is {on_disk[version].checksum[:12]}..."
        for version in sorted(applied)
        if version in on_disk and on_disk[version].checksum != applied[version]
    ]
    if drifted:
        raise MigrationDrift(
            "a migration was applied and its file has changed since; add a new migration "
            "instead of editing an applied one:\n  " + "\n  ".join(drifted)
        )


def apply_sql(version: str, connection: Connection | None = None) -> None:
    """Run one migration's SQL and record it in the ledger.

    Called from the revision module of the same name, so the revision graph and the
    checksum ledger cannot disagree about what ran.
    """
    if connection is None:
        from alembic import op

        connection = op.get_bind()

    migration = migration_named(version)
    for batch in _GO.split(migration.sql_text):
        if batch.strip():
            connection.exec_driver_sql(batch)

    ensure_ledger(connection)
    connection.execute(
        text(
            "INSERT INTO schema_migrations (version, checksum, applied_at) "
            "VALUES (:version, :checksum, :applied_at)"
        ),
        {
            "version": migration.version,
            "checksum": migration.checksum,
            "applied_at": datetime.now(UTC).replace(tzinfo=None),
        },
    )
    log.info("db.migration_applied", version=migration.version, checksum=migration.checksum)
