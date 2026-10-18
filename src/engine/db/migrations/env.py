"""Alembic environment.

The database URL is never written here: it is built from the same configuration the
application uses, so there is one place that knows how to reach SQL Server. Which database
to migrate is passed in by the caller through ``config.attributes["database"]``.

Offline mode is deliberately unsupported. Every migration runs its DDL through the driver
and records a checksum in the ledger, and neither is expressible as emitted SQL text.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import context
from alerts_bi_shared.db.connection import encryption_connect_args, engine_url
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

# Alembic executes this resource outside the application package. Local type checks
# use the source package; installed runtime keeps the service namespace.
if TYPE_CHECKING:
    from src.engine.config import load_config
    from src.engine.db.ledger import verify_ledger
else:
    from alerts_bi_admin.engine.config import load_config
    from alerts_bi_admin.engine.db.ledger import verify_ledger

config = context.config


def run_migrations_online() -> None:
    sql = config.attributes.get("sql_config") or load_config().sql
    database = config.attributes.get("database") or sql.database

    engine = create_engine(
        engine_url(sql, database),
        poolclass=NullPool,
        connect_args=encryption_connect_args(sql),
    )
    with engine.connect() as connection:
        # Before anything is applied: an applied migration whose file has changed is a hard
        # error, which Alembic's own version table cannot detect.
        verify_ledger(connection)

        # Each migration gets its own transaction, so a failure leaves the revisions
        # before it applied and recorded, and a rerun resumes rather than starting over.
        context.configure(connection=connection, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()

        # A stamp writes alembic_version outside any migration, so it is not covered by the
        # per-migration transactions above and would otherwise roll back on close.
        connection.commit()
    engine.dispose()


if context.is_offline_mode():
    raise RuntimeError(
        "offline mode is not supported: migrations execute DDL through the driver and "
        "record a checksum, neither of which can be emitted as SQL text"
    )
run_migrations_online()
