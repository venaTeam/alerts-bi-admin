"""The weekly schedule, against the mock Elasticsearch and a disposable database (7.11)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alerts_bi_operations.registry import DEFAULT_REGISTRY_PATH
from alerts_bi_shared.db.connection import connect
from src.engine.config import load_config
from src.engine.db.migrate import reset_test_database
from src.engine.es.client import EsClient
from src.engine.es.reader import V1_INDEX
from src.engine.llm.fake import FakeLlmClient, ScriptedResult
from src.engine.weekly.runner import PUBLISHER, WeeklyBusy, run_weekly, schedule_lock

pytestmark = pytest.mark.integration

CONFIG = load_config()
DB = CONFIG.sql.test_database
TEAM = "notifications-svc"
MOCK_NOW = datetime(2026, 8, 25, 18, 0, tzinfo=UTC)
MONDAY = datetime(2026, 8, 24)


@pytest.fixture(scope="module")
def registry(tmp_path_factory: pytest.TempPathFactory) -> str:
    """The checked-in registry with exactly two teams enrolled."""
    document = json.loads(DEFAULT_REGISTRY_PATH.read_bytes().decode("utf-8"))
    for team in document["teams"]:
        team.pop("weekly_review", None)
        if team["team_id"] in (TEAM, "checkout-api"):
            team["weekly_review"] = {"enabled": True}
    path = Path(tmp_path_factory.mktemp("registry")) / "teams.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return str(path)


@pytest.fixture()
def fresh() -> Iterator[None]:
    try:
        if not EsClient(CONFIG.es).index_exists(V1_INDEX):
            pytest.skip("mock Elasticsearch has no appchi-v1 index")
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"mock Elasticsearch is not reachable: {exc}")
    try:
        reset_test_database(CONFIG.sql, DB)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"SQL Server is not reachable: {exc}")
    yield


def weekly(registry: str, now: datetime, tmp: Path, **kwargs: object) -> list:  # type: ignore[type-arg]
    options: dict[str, object] = {"llm_client": FakeLlmClient(), **kwargs}
    return run_weekly(
        CONFIG,
        now=now,
        database=DB,
        registry_path=registry,
        out_root=tmp,
        **options,  # type: ignore[arg-type]
    )


def published(team: str) -> list[datetime]:
    with connect(CONFIG.sql, DB) as db:
        rows = db.query(
            "SELECT window_end FROM review_publications WHERE team_id = :t "
            "AND withdrawn_at IS NULL ORDER BY window_end",
            {"t": team},
        )
    return [row["window_end"] for row in rows]


def test_only_enrolled_teams_are_run_and_new_teams_start_at_the_latest_week(
    fresh: None, registry: str, tmp_path: Path
) -> None:
    outcomes = weekly(registry, MOCK_NOW, tmp_path)
    assert {(o.team_id, o.outcome) for o in outcomes} == {
        (TEAM, "published"),
        ("checkout-api", "published"),
    }
    assert published(TEAM) == [MONDAY]
    with connect(CONFIG.sql, DB) as db:
        row = db.query_one(
            "SELECT r.run_at, r.window_start, r.window_end FROM runs r JOIN review_publications p "
            "ON p.run_id = r.run_id WHERE p.team_id = :t",
            {"t": TEAM},
        )
        by = db.query_one("SELECT DISTINCT published_by FROM review_publications")
    assert row == {
        "run_at": MONDAY,
        "window_start": MONDAY - timedelta(days=7),
        "window_end": MONDAY,
    }
    assert by == {"published_by": PUBLISHER}


def test_invoking_again_does_nothing_and_missed_weeks_are_caught_up(
    fresh: None, registry: str, tmp_path: Path
) -> None:
    weekly(registry, MOCK_NOW, tmp_path)
    assert weekly(registry, MOCK_NOW + timedelta(days=3), tmp_path) == []

    later = weekly(registry, MOCK_NOW + timedelta(days=14), tmp_path, teams=[TEAM])
    assert [(o.outcome, o.window_end) for o in later] == [
        ("published", MONDAY.replace(tzinfo=UTC) + timedelta(days=7)),
        ("published", MONDAY.replace(tzinfo=UTC) + timedelta(days=14)),
    ]
    assert published(TEAM) == [MONDAY + timedelta(days=d) for d in (0, 7, 14)]


def test_without_the_model_every_week_is_held_for_a_person(
    fresh: None, registry: str, tmp_path: Path
) -> None:
    weekly(registry, MOCK_NOW, tmp_path, teams=[TEAM])
    held = weekly(
        registry,
        MOCK_NOW + timedelta(days=14),
        tmp_path,
        teams=[TEAM],
        llm_client=None,
        llm_disabled_reason="LLM disabled",
    )
    assert [o.outcome for o in held] == ["held", "held"]
    assert all(o.needs_attention for o in held)
    assert "model did not assess" in (held[0].detail or "")
    assert published(TEAM) == [MONDAY]


def test_a_failed_model_week_is_held_and_the_healthy_week_after_it_waits(
    fresh: None, registry: str, tmp_path: Path
) -> None:
    # checkout-api still has a v2 alert in the week ending 31 Aug that no core rule (R6
    # included) withholds from the model, so the failing client has something to fail on.
    team = "checkout-api"
    weekly(registry, MOCK_NOW, tmp_path, teams=[team])
    assert published(team) == [MONDAY]

    # Every model call fails. The week ending 31 Aug holds the mock's last alerts and
    # exhausts its batches; the week ending 7 Sep has none, so it is healthy but must wait.
    failing = FakeLlmClient(fallback=ScriptedResult("transport_error"))
    now = datetime(2026, 9, 8, 1, 0, tzinfo=UTC)
    first = weekly(registry, now, tmp_path, teams=[team], llm_client=failing)
    assert [o.outcome for o in first] == ["held", "stored"]
    assert "not assessed" in (first[0].detail or "")
    assert "waiting for the week ending 2026-08-31" in (first[1].detail or "")
    assert published(team) == [MONDAY]
    with connect(CONFIG.sql, DB) as db:
        log = db.query(
            "SELECT outcome FROM weekly_review_log WHERE team_id = :t ORDER BY log_id", {"t": team}
        )
    assert [row["outcome"] for row in log] == ["published", "held", "stored"]

    resolved = weekly(registry, now, tmp_path, teams=[team])
    assert [o.outcome for o in resolved] == ["published", "published"]
    assert published(team) == [MONDAY + timedelta(days=d) for d in (0, 7, 14)]


def test_weeks_past_retention_are_skipped_and_the_gap_is_published_across(
    fresh: None, registry: str, tmp_path: Path
) -> None:
    weekly(registry, MOCK_NOW, tmp_path, teams=["checkout-api"])
    far = MOCK_NOW + timedelta(days=7 * 15)
    outcomes = weekly(registry, far, tmp_path, teams=["checkout-api"])
    expired = [o for o in outcomes if o.outcome == "expired"]
    done = [o for o in outcomes if o.outcome == "published"]
    assert expired and done and len(expired) + len(done) == 15
    assert all(o.window_end < d.window_end for o in expired for d in done)
    weeks = published("checkout-api")
    assert weeks[1] - weeks[0] > timedelta(days=7), "the chart shows a gap, not invented weeks"


def test_two_schedulers_never_overlap(fresh: None, registry: str, tmp_path: Path) -> None:
    with schedule_lock(CONFIG, DB), pytest.raises(WeeklyBusy):
        weekly(registry, MOCK_NOW, tmp_path)


def test_a_team_that_is_not_enrolled_cannot_be_named(
    fresh: None, registry: str, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="not enrolled"):
        weekly(registry, MOCK_NOW, tmp_path, teams=["fraud-detection"])


def test_history_published_by_hand_off_the_monday_boundary_blocks_the_team(
    fresh: None, registry: str, tmp_path: Path
) -> None:
    from alerts_bi_operations.review.publication import publish_run
    from src.engine.db.repositories import persist_run
    from src.engine.run.orchestrator import execute_run

    with connect(CONFIG.sql, DB) as db:
        payload, summary = execute_run(
            team_id=TEAM,
            run_at=MOCK_NOW,
            config=CONFIG,
            es_client=EsClient(CONFIG.es),
            llm_client=FakeLlmClient(),
            registry_path=registry,
            db=db,
        )
        persist_run(db, payload)
        publish_run(db, summary.run_id, published_by="operator")

    outcomes = weekly(registry, MOCK_NOW + timedelta(days=14), tmp_path, teams=[TEAM])
    assert [o.outcome for o in outcomes] == ["blocked"]
    assert "Monday" in (outcomes[0].detail or "")


def test_a_week_still_unhealthy_after_three_days_is_published_with_a_reader_note(
    fresh: None, registry: str, tmp_path: Path
) -> None:
    weekly(registry, MOCK_NOW, tmp_path, teams=[TEAM])
    no_model = {"llm_client": None, "llm_disabled_reason": "LLM disabled", "teams": [TEAM]}
    first_held = datetime(2026, 9, 1, 1, 0, tzinfo=UTC)

    assert [o.outcome for o in weekly(registry, first_held, tmp_path, **no_model)] == ["held"]
    two_days = weekly(registry, first_held + timedelta(days=2), tmp_path, **no_model)
    assert [o.outcome for o in two_days] == ["held"]
    assert published(TEAM) == [MONDAY]

    three_days = weekly(registry, first_held + timedelta(days=3), tmp_path, **no_model)
    assert [o.outcome for o in three_days] == ["published"]
    assert not three_days[0].needs_attention
    assert "after 3 days held" in (three_days[0].detail or "")
    assert published(TEAM) == [MONDAY, MONDAY + timedelta(days=7)]
    with connect(CONFIG.sql, DB) as db:
        note = db.query_one(
            "SELECT review_note FROM review_publications WHERE run_id = :r",
            {"r": three_days[0].run_id},
        )
    assert note is not None
    assert "Published automatically after 3 days" in str(note["review_note"])
    assert "Not reviewed" in str(note["review_note"])
