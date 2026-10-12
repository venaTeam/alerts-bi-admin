"""Optional restricted login utilities retained for existing installations.

Migration 002 created ``alerts_bi_reader`` for a separate portal login. The portal now uses
the application's SQL connection directly. The role and explicit grant command remain for
installations that already use them or need a restricted login for another view consumer.

Two operations live here:

* :func:`grant_reader` creates or updates that optional login and puts it in the role.
* :func:`read_only_problems` diagnoses whether such a login is restricted to the views.
"""

from __future__ import annotations

from typing import Final

from alerts_bi_shared.db.connection import Database, connect, quote_identifier
from alerts_bi_shared.logging_setup import log

from ..config import SqlConfig

__all__ = [
    "BASE_TABLES",
    "PORTAL_VIEWS",
    "READER_ROLE",
    "grant_reader",
    "read_only_problems",
]

READER_ROLE: Final = "alerts_bi_reader"

PORTAL_VIEWS: Final = (
    "portal_reviews",
    "portal_schema_totals",
    "portal_alerts",
    "portal_decisions",
    "portal_rule_totals",
    "portal_daily_metrics",
)

#: Every table the portal must be unable to touch, directly.
BASE_TABLES: Final = (
    "runs",
    "daily_metrics",
    "daily_rule_counts",
    "alert_findings",
    "llm_batch_attempts",
    "llm_verdicts",
    "panel_parses",
    "run_panels",
    "review_publications",
    "finding_decisions",
    "llm_prompt_artifacts",
    "llm_review_scopes",
    "llm_review_batches",
    "llm_review_attempts",
)


def grant_reader(admin: SqlConfig, database: str, login: str, password: str) -> None:
    """Create or update the reader login and make it a member of the reader role only.

    The password is passed as a bound parameter and quoted server-side with ``QUOTENAME``,
    so it never appears in the SQL text and cannot break out of the literal.
    """
    # A narrow identifier check: the login name is also interpolated into USER_ID() below.
    quoted_login = quote_identifier(login)
    quote_identifier(database)
    if not password:
        raise ValueError("PORTAL_SQL_PASSWORD is empty; set it before granting the reader login")
    if login == admin.user:
        raise ValueError("the reader login must not be the owning credential")

    with connect(admin, "master", autocommit=True) as master:
        master.execute(
            """
            DECLARE @statement NVARCHAR(MAX) =
                CASE WHEN SUSER_ID(:login) IS NULL THEN N'CREATE LOGIN ' ELSE N'ALTER LOGIN ' END
                + QUOTENAME(:login) + N' WITH PASSWORD = ' + QUOTENAME(:password, '''')
                + N', CHECK_POLICY = ON';
            EXEC (@statement);
            """,
            {"login": login, "password": password},
        )

    with connect(admin, database, autocommit=True) as db:
        db.execute(
            f"""
            IF USER_ID(N'{login}') IS NULL
                CREATE USER {quoted_login} FOR LOGIN {quoted_login};
            ALTER ROLE {READER_ROLE} ADD MEMBER {quoted_login};
            """
        )
    log.info("db.reader_granted", database=database, login=login, role=READER_ROLE)


def read_only_problems(db: Database) -> list[str]:
    """Report permissions beyond reading the portal views for an optional login."""
    problems: list[str] = []

    row = db.query_one(
        """
        SELECT IS_SRVROLEMEMBER('sysadmin') AS sysadmin,
               IS_MEMBER('db_owner') AS db_owner,
               IS_MEMBER('db_datawriter') AS db_datawriter,
               IS_MEMBER('db_ddladmin') AS db_ddladmin,
               IS_MEMBER('db_datareader') AS db_datareader,
               SUSER_SNAME() AS login_name
        """
    )
    assert row is not None
    for flag, meaning in (
        ("sysadmin", "is a server sysadmin"),
        ("db_owner", "owns the database"),
        ("db_datawriter", "can write every table (db_datawriter)"),
        ("db_ddladmin", "can change the schema (db_ddladmin)"),
        ("db_datareader", "can read every table (db_datareader)"),
    ):
        if row[flag] == 1:
            problems.append(f"login {row['login_name']} {meaning}")

    for table in BASE_TABLES:
        permissions = db.query_one(
            """
            SELECT HAS_PERMS_BY_NAME(:table, 'OBJECT', 'SELECT') AS can_select,
                   HAS_PERMS_BY_NAME(:table, 'OBJECT', 'INSERT') AS can_insert,
                   HAS_PERMS_BY_NAME(:table, 'OBJECT', 'UPDATE') AS can_update,
                   HAS_PERMS_BY_NAME(:table, 'OBJECT', 'DELETE') AS can_delete
            """,
            {"table": f"dbo.{table}"},
        )
        assert permissions is not None
        for action in ("select", "insert", "update", "delete"):
            if permissions[f"can_{action}"] == 1:
                problems.append(f"can {action.upper()} the base table {table}")

    for view in PORTAL_VIEWS:
        allowed = db.query_one(
            "SELECT HAS_PERMS_BY_NAME(:view, 'OBJECT', 'SELECT') AS can_select",
            {"view": f"dbo.{view}"},
        )
        if allowed is None or allowed["can_select"] != 1:
            problems.append(
                f"cannot read the view {view}; check that migrations and the optional "
                "reader grant are applied"
            )

    database_writes = db.query_one(
        """
        SELECT HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'CREATE TABLE') AS create_table,
               HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'ALTER') AS alter_database
        """
    )
    assert database_writes is not None
    if database_writes["create_table"] == 1:
        problems.append("can CREATE TABLE in the database")
    if database_writes["alter_database"] == 1:
        problems.append("can ALTER the database")
    return problems
