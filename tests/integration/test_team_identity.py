"""Free-form IDs remain distinct through real SQL and every team HTTP action."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alerts_bi_operations.review.publication import publish_run
from alerts_bi_shared.db.connection import connect
from alerts_bi_shared.team_urls import encode_team_id
from fastapi.testclient import TestClient
from src.app import build_admin
from src.config import AdminConfig, AdminSettings
from src.engine.config import load_config
from src.engine.db import migrate as migrations
from src.engine.db.repositories import PersistencePayload, persist_run
from src.queries import team_runs
from src.readiness import check_schema

from tests.helpers.sql import sample_run

pytestmark = pytest.mark.integration
CONFIG = load_config()
DB = CONFIG.sql.test_database
TEAM_REVISION = "017_case_sensitive_teams"
TABLES = {
    "runs",
    "daily_metrics",
    "daily_rule_counts",
    "review_publications",
    "finding_decisions",
    "weekly_review_log",
    "run_jobs",
    "run_backfills",
}


def test_upgrade_preserves_rows_and_case_distinct_publications(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def previous(sql: object, database: str) -> tuple[list[str], list[str]]:
        command.upgrade(
            migrations.alembic_config(CONFIG.sql, database), "016_case_sensitive_key_fields"
        )
        return [], []

    with monkeypatch.context() as patch:
        patch.setattr(migrations, "migrate", previous)
        migrations.reset_test_database(CONFIG.sql, DB)
    with connect(CONFIG.sql, DB) as db:
        persist_run(db, PersistencePayload(run=sample_run(run_id="1" * 64, team_id="team-a")))
        publish_run(db, "1" * 64, allow_gap=True)
        with pytest.raises(ValueError, match=TEAM_REVISION):
            check_schema(db)
        # Exercise migration rollback after indexes are dropped.
        db.execute("CREATE VIEW dbo.blocks_017 WITH SCHEMABINDING AS SELECT team_id FROM dbo.runs")
        db.commit()
    try:
        with pytest.raises(Exception, match="blocks_017"):
            migrations.migrate(CONFIG.sql, DB)
        with connect(CONFIG.sql, DB) as db:
            assert migrations.current_revision(db) == "016_case_sensitive_key_fields"
            assert db.query_one("SELECT COUNT(*) AS n FROM review_publications") == {"n": 1}
            assert db.query_one("SELECT name FROM sys.indexes WHERE name='ix_runs_team_run_at'")
    finally:
        with connect(CONFIG.sql, DB) as db:
            db.execute("DROP VIEW IF EXISTS dbo.blocks_017")
            db.commit()
    assert migrations.migrate(CONFIG.sql, DB)[0] == [
        TEAM_REVISION,
        "018_alert_identity_hash",
        "019_erase_operator_names",
        "020_portal_review_operators",
        "021_parallel_run_slots",
        "022_long_key_fields",
    ]
    with connect(CONFIG.sql, DB) as db:
        check_schema(db)
        columns = db.query(
            "SELECT OBJECT_NAME(object_id) AS table_name, collation_name FROM sys.columns WHERE name='team_id' AND OBJECTPROPERTY(object_id, 'IsUserTable')=1"
        )
        assert {row["table_name"] for row in columns} == TABLES
        assert {row["collation_name"] for row in columns} == {"Latin1_General_100_BIN2"}
        for view in ("portal_reviews", "portal_decisions"):
            assert db.query_one(
                "SELECT collation_name FROM sys.columns WHERE object_id=OBJECT_ID(:view) AND name='team_id'",
                {"view": view},
            ) == {"collation_name": "Latin1_General_100_BIN2"}
        persist_run(db, PersistencePayload(run=sample_run(run_id="2" * 64, team_id="Team-A")))
        second = publish_run(db, "2" * 64, allow_gap=True)
        assert second.replaced_run_id is None
        persist_run(db, PersistencePayload(run=sample_run(run_id="3" * 64, team_id="Team-A")))
        replacement = publish_run(db, "3" * 64, replace=True, allow_gap=True)
        assert replacement.replaced_run_id == "2" * 64
        assert [r["run_id"] for r in team_runs(db, "team-a")] == ["1" * 64]
        assert {r["run_id"] for r in team_runs(db, "Team-A")} == {"2" * 64, "3" * 64}
        assert db.query(
            "SELECT run_id FROM portal_reviews WHERE team_id=:team", {"team": "team-a"}
        ) == [{"run_id": "1" * 64}]
        assert db.query(
            "SELECT run_id FROM portal_reviews WHERE team_id=:team", {"team": "Team-A"}
        ) == [{"run_id": "3" * 64}]
    assert migrations.migrate(CONFIG.sql, DB)[0] == []


def test_slash_ids_reach_workspace_summary_trigger_and_backfill(tmp_path: Path) -> None:
    migrations.reset_test_database(CONFIG.sql, DB)
    teams = ["team/a", "team/a/summary", "a%2Fb", "~61", ".."]
    registry = tmp_path / "teams.json"
    registry.write_text(
        json.dumps(
            {
                "registry_version": "routes",
                "teams": [
                    {
                        "team_id": team,
                        "display_name": team,
                        "v1_operators": ["shared", "shared"],
                        "v2_operator": "shared",
                    }
                    for team in teams
                ],
            }
        ),
        encoding="utf-8",
    )
    settings = AdminSettings(AdminConfig(CONFIG.sql), DB, "s" * 40, registry_path=str(registry))
    with TestClient(build_admin(settings), follow_redirects=False) as client:
        assert client.get("/").status_code == 200
        for team in teams:
            path = "/teams/" + encode_team_id(team)
            workspace = client.get(path)
            assert workspace.status_code == 200, workspace.text
            assert f'action="{path}/trigger"' in workspace.text
            response = client.get(path + "/summary", follow_redirects=True)
            assert response.status_code == 200 and team in response.text
        csrf = client.get("/csrf").json()["token"]
        # Weeks end on Monday 00:00 UTC; local time can already be Monday before then.
        now = datetime.now(UTC)
        monday = (now - timedelta(days=now.weekday())).date()
        path = "/teams/" + encode_team_id(teams[0])
        trigger = client.post(
            path + "/trigger",
            data={
                "csrf": csrf,
                "run_at": monday.isoformat(),
                "llm": "fake",
                "submission_key": "route-trigger",
            },
        )
        assert trigger.status_code == 303 and "job_id=" in trigger.headers["location"], trigger.text
        path = "/teams/" + encode_team_id(teams[1])
        queued = client.post(
            path + "/backfill",
            data={
                "csrf": csrf,
                "week": monday.isoformat(),
                "llm": "fake",
                "submission_key": "route-backfill",
            },
        )
        assert queued.status_code == 303 and "error=" not in queued.headers["location"], (
            queued.headers
        )
        assert client.get("/teams/~ff").status_code == 404
    with connect(CONFIG.sql, DB) as db:
        jobs = db.query("SELECT team_id FROM run_jobs ORDER BY team_id")
        assert [r["team_id"] for r in jobs] == teams[:2]
        assert db.query("SELECT team_id FROM run_backfills") == [{"team_id": teams[1]}]
