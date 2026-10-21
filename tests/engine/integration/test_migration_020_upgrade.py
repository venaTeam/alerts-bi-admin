"""Migration 020 adds each published week's frozen operators to portal_reviews."""

from __future__ import annotations

import importlib.util
import json

import pytest
from alembic import command
from alerts_bi_operations.review.publication import publish_run
from alerts_bi_shared.db.connection import Database, connect
from src.engine.config import load_config
from src.engine.db import migrate as migrations
from src.engine.db.ledger import MIGRATIONS_DIR
from src.engine.db.repositories import PersistencePayload, persist_run
from src.readiness import check_schema

from tests.engine.helpers.sql import sample_finding, sample_run

pytestmark = pytest.mark.integration

CONFIG = load_config()
DB = CONFIG.sql.test_database
PREVIOUS_HEAD = "019_erase_operator_names"
#: (team, frozen registry entry): case variants and an empty v1 list must survive exactly.
TEAMS = (
    (
        "legacy",
        {"team_id": "legacy", "v1_operators": ["batch", "BATCH", "batch"], "v2_operator": None},
    ),
    ("payments", {"team_id": "payments", "v1_operators": [], "v2_operator": "Pay-Core"}),
    ("unknown", None),
)


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


def _reviews(db: Database) -> list[dict[str, object]]:
    return db.query("SELECT * FROM portal_reviews ORDER BY team_id")


def test_020_adds_operator_columns_and_keeps_every_review(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_to_previous_head(monkeypatch)
    with connect(CONFIG.sql, DB) as db:
        for number, (team, entry) in enumerate(TEAMS):
            run = sample_run(
                run_id=str(number) * 64,
                team_id=team,
                team_display_name=team.title(),
                registry_entry_snapshot="not json" if entry is None else json.dumps(entry),
            )
            persist_run(
                db, PersistencePayload(run=run, findings=[sample_finding(run_id=run["run_id"])])
            )
            publish_run(db, str(run["run_id"]), allow_gap=True)
        db.commit()
        before = _reviews(db)

    applied, _already = migrations.migrate(CONFIG.sql, DB)
    assert applied == ["020_portal_review_operators"]

    with connect(CONFIG.sql, DB) as db:
        assert migrations.current_revision(db) == "020_portal_review_operators"
        check_schema(db)
        after = _reviews(db)
        # Every earlier column keeps its value and position; the two new ones come last.
        assert [list(row)[: len(before[0])] for row in after] == [list(row) for row in before]
        assert [[v for k, v in row.items() if k not in before[0]] for row in after] == [
            [json.dumps(["batch", "BATCH", "batch"]), None],
            ["[]", "Pay-Core"],
            [None, None],
        ]
        assert list(after[0])[-2:] == ["v1_operators", "v2_operator"]


def test_020_refuses_downgrade() -> None:
    path = MIGRATIONS_DIR / "versions" / "020_portal_review_operators.py"
    spec = importlib.util.spec_from_file_location("revision_020", path)
    assert spec is not None and spec.loader is not None
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    assert revision.down_revision == PREVIOUS_HEAD
    with pytest.raises(NotImplementedError, match="new revision"):
        revision.downgrade()
