"""Migration 019 erases stored operator names and leaves every other value in place."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from alembic import command
from alerts_bi_operations.review.publication import publish_run
from alerts_bi_shared.db.connection import Database, connect
from alerts_bi_shared.hashing import sha256_text
from fastapi import HTTPException
from sqlalchemy.exc import DBAPIError
from src.engine.api import backfill as backfill_api
from src.engine.api import jobs as jobs_api
from src.engine.config import load_config
from src.engine.db import migrate as migrations
from src.engine.db.repositories import PersistencePayload, persist_run
from src.readiness import check_schema

from tests.engine.helpers.sql import sample_finding, sample_run

pytestmark = pytest.mark.integration

CONFIG = load_config()
DB = CONFIG.sql.test_database
PREVIOUS_HEAD = "018_alert_identity_hash"
HASH = "0" * 64
OTHER = "1" * 64
COUNTED = ("review_publications", "finding_decisions", "run_jobs", "run_backfills")


def _counts(db: Database) -> dict[str, int]:
    return {
        table: int(db.query_one(f"SELECT COUNT(*) AS n FROM {table}")["n"])  # type: ignore[index]
        for table in COUNTED
    }


def _reset_to_previous_head(monkeypatch: pytest.MonkeyPatch) -> None:
    def to_previous_head(sql: object, database: str) -> tuple[list[str], list[str]]:
        command.upgrade(migrations.alembic_config(CONFIG.sql, database), PREVIOUS_HEAD)
        return [], []

    with monkeypatch.context() as patch:
        patch.setattr(migrations, "migrate", to_previous_head)
        try:
            migrations.reset_test_database(CONFIG.sql, DB)
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"SQL Server is not reachable: {exc}")


def _digest(key: str) -> bytes:
    return bytes.fromhex(sha256_text(key))


def _job(db: Database, job_id: str, key: str, requester: str, **extra: object) -> None:
    db.execute(
        "INSERT INTO run_jobs (job_id,idempotency_key,idempotency_key_hash,requested_by,"
        "request_hash,team_id,run_at,llm_mode,registry_sha256,run_id,status,submitted_at,"
        "error_summary,backfill_id) VALUES (:id,:key,:digest,:requester,:request,'checkout-api',"
        "SYSUTCDATETIME(),'fake',:hash,:hash,:status,SYSUTCDATETIME(),:error,:backfill)",
        {
            "id": job_id,
            "key": key,
            "digest": _digest(key),
            "requester": requester,
            "request": extra.get("request", HASH),
            "hash": HASH,
            "status": extra.get("status", "failed"),
            "error": extra.get("error"),
            "backfill": extra.get("backfill"),
        },
    )


def _backfill(db: Database, backfill_id: str, key: str, requester: str, request: str) -> None:
    db.execute(
        "INSERT INTO run_backfills (backfill_id,team_id,requested_by,idempotency_key_hash,"
        "request_hash,llm_mode,week_count,submitted_at) VALUES "
        "(:id,'checkout-api',:requester,:digest,:request,'fake',1,SYSUTCDATETIME())",
        {"id": backfill_id, "requester": requester, "digest": _digest(key), "request": request},
    )


def _refusal(lookup: Callable[[], object]) -> str:
    with pytest.raises(HTTPException) as caught:
        lookup()
    assert caught.value.status_code == 409
    return str(caught.value.detail)


def test_019_erases_names_and_keeps_everything_else(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_to_previous_head(monkeypatch)
    run = sample_run()
    run_id = str(run["run_id"])
    with connect(CONFIG.sql, DB) as db:
        assert migrations.current_revision(db) == PREVIOUS_HEAD
        persist_run(db, PersistencePayload(run=run, findings=[sample_finding()]))
        # A person published and withdrew the week, and 018 stored both names (publish_run now
        # refuses a name); the scheduler then published it again.
        publish_run(db, run_id, allow_gap=True)
        db.execute(
            "UPDATE review_publications SET published_by=N'alice', withdrawn_at=SYSUTCDATETIME(), "
            "withdrawn_by=N'bob', withdrawn_reason=N'wrong week' WHERE run_id=:run",
            {"run": run_id},
        )
        publish_run(db, run_id, published_by="weekly-schedule", allow_gap=True)
        db.execute(
            "INSERT INTO finding_decisions (team_id, run_id, alert_schema, application, "
            "key_field, finding_id, state, note, decided_at, decided_by) VALUES "
            "(:team, :run, 'v1', 'checkout-api', 'checkout-api:cart:node-1', 'R1', "
            "'confirmed', 'kept', SYSUTCDATETIME(), 'carol')",
            {"team": run["team_id"], "run": run_id},
        )
        db.execute(
            "INSERT INTO run_backfills (backfill_id,team_id,requested_by,idempotency_key_hash,"
            "request_hash,llm_mode,week_count,submitted_at,cancelled_at,cancelled_by) VALUES "
            "(:id,'checkout-api','dave',:digest,:hash,'fake',1,SYSUTCDATETIME(),"
            "SYSUTCDATETIME(),'dave')",
            {"id": "f" * 32, "digest": _digest("backfill-form"), "hash": HASH},
        )
        # Two people once used the same backfill form key with different weeks.
        _backfill(db, "1" * 32, "shared-form", "erin", HASH)
        _backfill(db, "2" * 32, "shared-form", "frank", OTHER)
        # Two people once used one submission key with different options, two others one key
        # with the same options; a fifth key was used once.
        _job(db, "a" * 32, "shared-key", "erin")
        _job(db, "b" * 32, "shared-key", "frank", request=OTHER)
        _job(db, "c" * 32, "own-key", "grace")
        _job(db, "e" * 32, "same-options-key", "heidi")
        _job(db, "f" * 32, "same-options-key", "ivan")
        _job(
            db,
            "d" * 32,
            "backfill-week",
            "dave",
            status="cancelled",
            error="Cancelled by dave before it started.",
            backfill="f" * 32,
        )
        db.commit()
        before = _counts(db)
        with pytest.raises(ValueError, match="019_erase_operator_names"):
            check_schema(db)

    applied, _already = migrations.migrate(CONFIG.sql, DB)
    assert applied == [
        "019_erase_operator_names",
        "020_portal_review_operators",
        "021_parallel_run_slots",
        "022_long_key_fields",
    ]

    with connect(CONFIG.sql, DB) as db:
        assert migrations.current_revision(db) == "022_long_key_fields"
        assert _counts(db) == before
        check_schema(db)
        publications = db.query(
            "SELECT published_by, withdrawn_by, withdrawn_reason FROM review_publications "
            "ORDER BY publication_id"
        )
        assert publications == [
            # ck_review_publications_withdrawn keeps a withdrawn row's withdrawn_by non-NULL.
            {"published_by": "", "withdrawn_by": "", "withdrawn_reason": "wrong week"},
            {"published_by": "weekly-schedule", "withdrawn_by": None, "withdrawn_reason": None},
        ]
        assert db.query("SELECT decided_by, note FROM finding_decisions") == [
            {"decided_by": "", "note": "kept"}
        ]
        assert db.query("SELECT decided_by FROM portal_decisions") == [{"decided_by": ""}]
        jobs = db.query("SELECT job_id, requested_by, error_summary FROM run_jobs ORDER BY job_id")
        assert jobs == [
            {"job_id": "a" * 32, "requested_by": "#" + "a" * 32, "error_summary": None},
            {"job_id": "b" * 32, "requested_by": "#" + "b" * 32, "error_summary": None},
            {"job_id": "c" * 32, "requested_by": "", "error_summary": None},
            {
                "job_id": "d" * 32,
                "requested_by": "",
                "error_summary": "Cancelled before it started.",
            },
            {"job_id": "e" * 32, "requested_by": "#" + "e" * 32, "error_summary": None},
            {"job_id": "f" * 32, "requested_by": "#" + "f" * 32, "error_summary": None},
        ]
        backfills = db.query(
            "SELECT backfill_id, requested_by, cancelled_by FROM run_backfills ORDER BY backfill_id"
        )
        assert backfills == [
            {"backfill_id": "1" * 32, "requested_by": "#" + "1" * 32, "cancelled_by": None},
            {"backfill_id": "2" * 32, "requested_by": "#" + "2" * 32, "cancelled_by": None},
            {"backfill_id": "f" * 32, "requested_by": "", "cancelled_by": None},
        ]

        # A retry after the upgrade returns the one submission with its key and options, also
        # where two people had used its key, and refuses rather than guess or add a duplicate.
        def job(key: str, request: str) -> str:
            found = jobs_api._existing_submission(db, _digest(key), request)
            assert found is not None, key
            return str(found["job_id"])

        def form(key: str, request: str) -> str:
            found = backfill_api.existing_backfill(db, _digest(key), request)
            assert found is not None, key
            return str(found["backfill_id"])

        assert job("own-key", HASH) == "c" * 32
        assert job("shared-key", HASH) == "a" * 32
        assert job("shared-key", OTHER) == "b" * 32
        assert "different options" in _refusal(
            lambda: jobs_api._existing_submission(db, _digest("shared-key"), "2" * 64)
        )
        assert "Several earlier submissions" in _refusal(
            lambda: jobs_api._existing_submission(db, _digest("same-options-key"), HASH)
        )
        assert form("backfill-form", HASH) == "f" * 32
        assert form("shared-form", HASH) == "1" * 32
        assert form("shared-form", OTHER) == "2" * 32
        assert "different weeks" in _refusal(
            lambda: backfill_api.existing_backfill(db, _digest("shared-form"), "2" * 64)
        )
        assert _counts(db) == before
        # The append-only protection is back: a decision still cannot be changed.
        with pytest.raises(DBAPIError, match="append-only"):
            db.execute("UPDATE finding_decisions SET note = N'changed'")
        db.rollback()


def test_019_refuses_downgrade() -> None:
    import importlib.util

    from src.engine.db.ledger import MIGRATIONS_DIR

    path = MIGRATIONS_DIR / "versions" / "019_erase_operator_names.py"
    spec = importlib.util.spec_from_file_location("revision_019", path)
    assert spec is not None and spec.loader is not None
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    assert revision.down_revision == PREVIOUS_HEAD
    with pytest.raises(NotImplementedError, match="cannot be restored"):
        revision.downgrade()
