"""Publishing a completed run as a team's weekly review (design section 7.10).

A run being complete and a review being published are different facts. Only an explicit
publication makes a week visible in the reader portal, and published weeks are back to back:
they never overlap, and a gap after the latest published week needs ``--allow-gap``.

The rules live in :func:`check_publication`, a pure function, so they are testable without a
database. :func:`publish_run` applies them inside a transaction that holds a lock on the
team's current publications, so two operators cannot publish overlapping weeks at once.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from alerts_bi_shared.db.connection import Database
from alerts_bi_shared.window import WINDOW_HOURS

__all__ = [
    "PublicationRefused",
    "PublishResult",
    "PublishedWeek",
    "check_publication",
    "is_published",
    "list_publications",
    "publish_run",
    "unpublish_run",
]

_WEEK = timedelta(hours=WINDOW_HOURS)
NOTE_LIMIT = 4000
REASON_LIMIT = 1000


class PublicationRefused(ValueError):
    """The operator asked for something the publication rules do not allow."""


@dataclass(frozen=True, slots=True)
class PublishedWeek:
    run_id: str
    window_start: datetime
    window_end: datetime
    publication_id: int | None = None


def _utc(value: datetime) -> datetime:
    """SQL Server hands back naive UTC; everything here compares in aware UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _naive(value: datetime) -> datetime:
    return _utc(value).replace(tzinfo=None)


def _label(week: PublishedWeek) -> str:
    return f"{_utc(week.window_start):%Y-%m-%d %H:%M} to {_utc(week.window_end):%Y-%m-%d %H:%M} UTC"


def _hours(delta: timedelta) -> str:
    hours = delta.total_seconds() / 3600
    return f"{hours:g} h"


def check_publication(
    candidate: PublishedWeek,
    current: Sequence[PublishedWeek],
    *,
    replace: bool,
    allow_gap: bool,
) -> PublishedWeek | None:
    """Decide whether ``candidate`` may be published next to the team's ``current`` weeks.

    Returns the current publication a ``replace`` withdraws, or ``None``. Raises
    :class:`PublicationRefused` with an actionable reason otherwise.
    """
    start, end = _utc(candidate.window_start), _utc(candidate.window_end)
    if end - start != _WEEK:
        raise PublicationRefused(
            f"a published review covers exactly {WINDOW_HOURS} hours; this run covers "
            f"{_hours(end - start)}"
        )

    for week in current:
        if week.run_id == candidate.run_id:
            raise PublicationRefused(f"run {candidate.run_id[:16]} is already published")

    same_week = [
        week
        for week in current
        if _utc(week.window_start) == start and _utc(week.window_end) == end
    ]
    replaced = same_week[0] if same_week else None
    if replaced is not None and not replace:
        raise PublicationRefused(
            f"the week {_label(replaced)} is already published by run {replaced.run_id[:16]}; "
            "pass --replace to publish this run in its place"
        )
    if replaced is None and replace:
        raise PublicationRefused(
            "--replace was given, but no published week has exactly this window"
        )

    others = [week for week in current if week is not replaced]
    for week in others:
        overlap = min(end, _utc(week.window_end)) - max(start, _utc(week.window_start))
        if overlap > timedelta(0):
            raise PublicationRefused(
                f"this run's week overlaps the published week {_label(week)} by "
                f"{_hours(overlap)}; published weeks never overlap"
            )

    # A replacement keeps the week where it already is, so a gap next to it was accepted when
    # that week was first published and is not asked about again.
    if not allow_gap and replaced is None:
        before = [week for week in others if _utc(week.window_end) <= start]
        after = [week for week in others if _utc(week.window_start) >= end]
        if before:
            previous = max(before, key=lambda week: _utc(week.window_end))
            gap = start - _utc(previous.window_end)
            if gap > timedelta(0):
                raise PublicationRefused(
                    f"this run's week starts {_hours(gap)} after the published week "
                    f"{_label(previous)} ends, leaving a gap; publish the missing week first "
                    "or pass --allow-gap"
                )
        if after:
            following = min(after, key=lambda week: _utc(week.window_start))
            gap = _utc(following.window_start) - end
            if gap > timedelta(0):
                raise PublicationRefused(
                    f"this run's week ends {_hours(gap)} before the published week "
                    f"{_label(following)} starts, leaving a gap; pass --allow-gap to publish it"
                )
    return replaced


def _clean(text: str | None, limit: int, name: str) -> str | None:
    if text is None:
        return None
    cleaned = text.strip()
    if not cleaned:
        return None
    if len(cleaned) > limit:
        raise PublicationRefused(f"the {name} is {len(cleaned)} characters; the limit is {limit}")
    return cleaned


def _current_weeks(db: Database, team_id: str, *, lock: bool) -> list[PublishedWeek]:
    hint = "WITH (UPDLOCK, HOLDLOCK)" if lock else ""
    rows = db.query(
        f"""
        SELECT publication_id, run_id, window_start, window_end
        FROM review_publications {hint}
        WHERE team_id = :team_id AND withdrawn_at IS NULL
        """,
        {"team_id": team_id},
    )
    return [
        PublishedWeek(
            run_id=str(row["run_id"]),
            window_start=row["window_start"],
            window_end=row["window_end"],
            publication_id=int(row["publication_id"]),
        )
        for row in rows
    ]


@dataclass(frozen=True, slots=True)
class PublishResult:
    publication_id: int
    run_id: str
    team_id: str
    window_start: datetime
    window_end: datetime
    replaced_run_id: str | None


def publish_run(
    db: Database,
    run_id: str,
    *,
    published_by: str,
    note: str | None = None,
    replace: bool = False,
    allow_gap: bool = False,
    now: datetime | None = None,
) -> PublishResult:
    """Publish one completed run as its team's review of that week."""
    by = _clean(published_by, 128, "operator name")
    if by is None:
        raise PublicationRefused("an operator name is required")
    review_note = _clean(note, NOTE_LIMIT, "review note")
    at = _naive(now or datetime.now(UTC))

    with db.transaction():
        run = db.query_one(
            "SELECT run_id, team_id, window_start, window_end, status FROM runs "
            "WITH (UPDLOCK) WHERE run_id = :run_id",
            {"run_id": run_id},
        )
        if run is None:
            raise PublicationRefused(f"no stored run {run_id!r}")
        if run["status"] != "completed":
            raise PublicationRefused(
                f"run {run_id[:16]} is {run['status']}; only a completed run can be published"
            )

        team_id = str(run["team_id"])
        candidate = PublishedWeek(run_id, run["window_start"], run["window_end"])
        replaced = check_publication(
            candidate,
            _current_weeks(db, team_id, lock=True),
            replace=replace,
            allow_gap=allow_gap,
        )
        if replaced is not None:
            db.execute(
                """
                UPDATE review_publications
                SET withdrawn_at = :at, withdrawn_by = :by, withdrawn_reason = :reason
                WHERE publication_id = :publication_id AND withdrawn_at IS NULL
                """,
                {
                    "at": at,
                    "by": by,
                    "reason": f"replaced by run {run_id}",
                    "publication_id": replaced.publication_id,
                },
            )

        inserted = db.query_one(
            """
            INSERT INTO review_publications
                (run_id, team_id, window_start, window_end, published_at, published_by, review_note)
            OUTPUT INSERTED.publication_id
            VALUES (:run_id, :team_id, :window_start, :window_end, :at, :by, :note)
            """,
            {
                "run_id": run_id,
                "team_id": team_id,
                "window_start": run["window_start"],
                "window_end": run["window_end"],
                "at": at,
                "by": by,
                "note": review_note,
            },
        )
        assert inserted is not None
        return PublishResult(
            publication_id=int(inserted["publication_id"]),
            run_id=run_id,
            team_id=team_id,
            window_start=run["window_start"],
            window_end=run["window_end"],
            replaced_run_id=replaced.run_id if replaced else None,
        )


def unpublish_run(
    db: Database,
    run_id: str,
    *,
    withdrawn_by: str,
    reason: str,
    now: datetime | None = None,
) -> None:
    """Withdraw a run's current publication. The row is kept; readers stop seeing the week."""
    by = _clean(withdrawn_by, 128, "operator name")
    why = _clean(reason, REASON_LIMIT, "reason")
    if by is None:
        raise PublicationRefused("an operator name is required")
    if why is None:
        raise PublicationRefused("a reason is required to withdraw a published review")

    with db.transaction():
        current = db.query_one(
            "SELECT publication_id FROM review_publications WITH (UPDLOCK) "
            "WHERE run_id = :run_id AND withdrawn_at IS NULL",
            {"run_id": run_id},
        )
        if current is None:
            raise PublicationRefused(f"run {run_id[:16]} is not currently published")
        db.execute(
            """
            UPDATE review_publications
            SET withdrawn_at = :at, withdrawn_by = :by, withdrawn_reason = :reason
            WHERE publication_id = :publication_id
            """,
            {
                "at": _naive(now or datetime.now(UTC)),
                "by": by,
                "reason": why,
                "publication_id": current["publication_id"],
            },
        )


def is_published(db: Database, run_id: str) -> bool:
    return (
        db.query_one(
            "SELECT 1 AS published FROM review_publications "
            "WHERE run_id = :run_id AND withdrawn_at IS NULL",
            {"run_id": run_id},
        )
        is not None
    )


def list_publications(db: Database, team_id: str) -> list[dict[str, Any]]:
    """Every publication of a team, current and withdrawn, oldest week first."""
    return db.query(
        """
        SELECT p.publication_id, p.run_id, p.window_start, p.window_end, p.published_at,
               p.published_by, p.review_note, p.withdrawn_at, p.withdrawn_by,
               p.withdrawn_reason, r.status AS run_status
        FROM review_publications AS p
        JOIN runs AS r ON r.run_id = p.run_id
        WHERE p.team_id = :team_id
        ORDER BY p.window_start ASC, p.publication_id ASC
        """,
        {"team_id": team_id},
    )
