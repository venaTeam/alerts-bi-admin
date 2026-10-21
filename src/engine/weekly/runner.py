"""Running and publishing the weeks that are due (design section 7.11).

``alerts-bi weekly`` calls :func:`run_weekly`. It is safe to invoke as often as the
scheduler likes - the OpenShift CronJob runs it daily - because a week that is already
published is never due again. Running daily rather than weekly is what makes a missed
invocation self-healing.

For every team enrolled with ``weekly_review.enabled``, one at a time:

1. Plan the due weeks (:func:`alerts_bi_admin.engine.weekly.plan.plan_weeks`).
2. Run each due week oldest first, as an ordinary single-team run, and persist it. Every due
   week is run and stored even when it cannot be published yet, so no week is lost to
   Elasticsearch retention while an earlier one waits for a person.
3. Publish a week automatically only when it is healthy - the model assessed every alert it
   was asked about - and only while every earlier due week was published. An unhealthy week
   is **held** and retried on every invocation; healthy weeks after it are **stored** until it
   is resolved. A week still unhealthy :data:`HOLD_DAYS` days after it was first held is
   published anyway, with a reader note saying how many alerts were not assessed - the
   portal already shows those as "Not reviewed", so nothing is passed off as examined.

A failure for one team is recorded and never stops the others. Every outcome is appended to
``weekly_review_log``. Two schedulers can never overlap: the whole invocation holds a SQL
Server application lock.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from alerts_bi_operations.registry import TeamEntry, load_registry
from alerts_bi_operations.review.publication import SCHEDULED_PUBLISHER, publish_run
from alerts_bi_shared.db.connection import Database, connect
from alerts_bi_shared.logging_setup import bind_log_context, log, log_context, redact_error

from ..config import AppConfig
from ..db.repositories import persist_run
from ..es.client import EsClient
from ..llm.client import LlmClient
from ..run.orchestrator import compute_run_id, execute_run
from ..run.outputs import render_run_report
from .plan import WEEK, plan_weeks

__all__ = [
    "BACKFILL_WAIT",
    "BACKFILL_YIELD_RESOURCE",
    "HOLD_DAYS",
    "LOCK_RESOURCE",
    "PUBLISHER",
    "Outcome",
    "WeeklyBusy",
    "backfill_in_progress",
    "enrolled_teams",
    "run_health",
    "run_weekly",
    "schedule_lock",
    "weekly_lock",
]

#: Who a scheduled publication is recorded as.
PUBLISHER = SCHEDULED_PUBLISHER

#: How long an unhealthy week is held and retried before it is published anyway.
HOLD_DAYS = 3
LOCK_RESOURCE = "alerts-bi-weekly"

#: Held by a weekly invocation waiting for a running backfill week; while it is held the
#: manual-run worker claims no further backfill weeks (docs/backfill.md).
BACKFILL_YIELD_RESOURCE = "alerts-bi-backfill-yield"
#: How long a weekly invocation waits for the current backfill week before refusing.
BACKFILL_WAIT = timedelta(hours=3)
BACKFILL_POLL_SECONDS = 30.0

#: Outcomes that need a person: the command exits non-zero when any of these occurred.
NEEDS_ATTENTION = frozenset({"held", "failed", "blocked"})


class WeeklyBusy(RuntimeError):
    """Another run, report, weekly schedule or deletion holds the shared execution lock."""


@dataclass(frozen=True, slots=True)
class Outcome:
    team_id: str
    window_end: datetime | None
    outcome: str
    """``published`` | ``held`` | ``stored`` | ``failed`` | ``blocked`` | ``expired`` | ``due``"""
    run_id: str | None = None
    detail: str | None = None

    @property
    def needs_attention(self) -> bool:
        return self.outcome in NEEDS_ATTENTION


def enrolled_teams(registry_path: str | None, only: Sequence[str] = ()) -> list[TeamEntry]:
    """Teams enrolled for the weekly review, optionally narrowed to named ones."""
    loaded = load_registry(registry_path) if registry_path else load_registry()
    teams = [team for team in loaded.teams if team.weekly_review]
    if only:
        known = {team.team_id for team in teams}
        missing = sorted(set(only) - known)
        if missing:
            raise ValueError(f"not enrolled for the weekly review: {', '.join(missing)}")
        teams = [team for team in teams if team.team_id in set(only)]
    return teams


@contextmanager
def schedule_lock(
    config: AppConfig, database: str, *, allow_manual_job: bool = False
) -> Iterator[None]:
    """Hold the shared run/report/schedule/deletion SQL lock, or refuse immediately.

    Retain the historical resource name so existing schedulers still coordinate. This
    separate SQL session survives commits and rollbacks on the run's data connection.
    Manual callers hold it from before scanning through persistence and report writing;
    standalone reports hold it from run lookup through all file writes, and deletion
    holds it through SQL cleanup and file removal. Durable pending/running jobs
    keep their reservation after a worker exits. Job admission/reconciliation,
    or deletion with its own transactional active-job refusal, may bypass this
    check. Check under the lock to serialize it with admission.
    """
    with connect(config.sql, database, autocommit=True) as lock:
        row = lock.query_one(
            "SET NOCOUNT ON; DECLARE @result INT; "
            f"EXEC @result = sp_getapplock @Resource = N'{LOCK_RESOURCE}', "
            "@LockMode = 'Exclusive', @LockOwner = 'Session', @LockTimeout = 0; "
            "SELECT @result AS result"
        )
        if row is None or int(row["result"]) < 0:
            raise WeeklyBusy(
                "another run, report, weekly schedule or deletion is already in progress"
            )
        try:
            # Pre-job schemas still support historical CLI/weekly commands.
            if (
                not allow_manual_job
                and lock.query_one(
                    "SELECT 1 AS present WHERE OBJECT_ID('run_jobs', 'U') IS NOT NULL"
                )
                and lock.query_one("SELECT TOP 1 job_id FROM run_jobs WHERE active_slot=1")
            ):
                raise WeeklyBusy(
                    "a manual job is pending or running; let the worker complete or reconcile it first"
                )
            yield
        finally:
            lock.execute(
                f"EXEC sp_releaseapplock @Resource = N'{LOCK_RESOURCE}', @LockOwner = 'Session'"
            )


def backfill_in_progress(db: Database) -> bool:
    """Does any backfill week wait, hold the slot, or run? Pre-backfill schemas never do."""
    present = db.query_one("SELECT COL_LENGTH('run_jobs', 'backfill_id') AS present")
    if present is None or present["present"] is None:
        return False
    return (
        db.query_one(
            "SELECT TOP 1 job_id FROM run_jobs WHERE backfill_id IS NOT NULL "
            "AND status IN ('queued', 'pending', 'running')"
        )
        is not None
    )


@contextmanager
def weekly_lock(
    config: AppConfig,
    database: str,
    *,
    wait: timedelta = BACKFILL_WAIT,
    poll_seconds: float = BACKFILL_POLL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Iterator[None]:
    """The shared lock for a weekly invocation, letting it go ahead of queued backfill weeks.

    Without a backfill in progress this refuses at once, exactly like :func:`schedule_lock`.
    While one is in progress the invocation holds :data:`BACKFILL_YIELD_RESOURCE`, so the
    worker claims no further backfill weeks, and polls until the current week releases the
    shared lock or ``wait`` expires. Only one invocation can wait at a time.
    """
    with connect(config.sql, database, autocommit=True) as hold:
        holding = False
        try:
            deadline = clock() + wait.total_seconds()
            while True:
                stack = ExitStack()
                try:
                    stack.enter_context(schedule_lock(config, database))
                except WeeklyBusy:
                    if clock() >= deadline or not backfill_in_progress(hold):
                        raise
                    if not holding:
                        row = hold.query_one(
                            "SET NOCOUNT ON; DECLARE @result INT; "
                            "EXEC @result = sp_getapplock @Resource=:resource, "
                            "@LockMode='Exclusive', @LockOwner='Session', @LockTimeout=0; "
                            "SELECT @result AS result",
                            {"resource": BACKFILL_YIELD_RESOURCE},
                        )
                        if row is None or int(row["result"]) < 0:
                            raise
                        holding = True
                        log.info(
                            "weekly.waiting_for_backfill",
                            "A backfill week holds the lock; waiting up to {wait:dur} for it "
                            "to finish. The worker claims no further backfill weeks meanwhile",
                            wait=wait.total_seconds(),
                        )
                    sleep(poll_seconds)
                    continue
                with stack:
                    yield
                return
        finally:
            if holding:
                hold.execute(
                    "EXEC sp_releaseapplock @Resource=:resource, @LockOwner='Session'",
                    {"resource": BACKFILL_YIELD_RESOURCE},
                )


def run_health(db: Database, run_id: str) -> list[str]:
    """Why a stored run must not be published without a person, if anything.

    A run that raised never got here. What remains is the model: a week in which it did not
    assess every eligible alert - disabled, unreachable, or a batch that failed all three
    attempts - would publish ``unassessed`` alerts as if they had been examined.
    """
    run = db.query_one(
        "SELECT status, llm_assessed FROM runs WHERE run_id = :run_id", {"run_id": run_id}
    )
    if run is None:
        return ["the run is not in the store"]
    problems: list[str] = []
    if run["status"] != "completed":
        problems.append(f"the run is {run['status']}")
    if not run["llm_assessed"]:
        problems.append("the model did not assess this week")
    unassessed = db.query_one(
        "SELECT COUNT(*) AS n FROM alert_findings "
        "WHERE run_id = :run_id AND quality_state = 'unassessed'",
        {"run_id": run_id},
    )
    count = int(unassessed["n"]) if unassessed else 0
    if count:
        problems.append(f"{count} alert(s) were not assessed by the model")
    return problems


def _log(db: Database, invoked_at: datetime, outcome: Outcome) -> None:
    with db.transaction():
        db.execute(
            """
            INSERT INTO weekly_review_log
                (invoked_at, team_id, window_end, outcome, run_id, detail)
            VALUES (:invoked_at, :team_id, :window_end, :outcome, :run_id, :detail)
            """,
            {
                "invoked_at": invoked_at.astimezone(UTC).replace(tzinfo=None),
                "team_id": outcome.team_id,
                "window_end": outcome.window_end.astimezone(UTC).replace(tzinfo=None)
                if outcome.window_end
                else None,
                "outcome": outcome.outcome,
                "run_id": outcome.run_id,
                "detail": (outcome.detail or "")[:1000] or None,
            },
        )


def _first_held(db: Database, team_id: str, window_end: datetime) -> datetime | None:
    """When the schedule first held this team's week, if it ever did."""
    row = db.query_one(
        "SELECT MIN(invoked_at) AS first_held FROM weekly_review_log "
        "WHERE team_id = :team_id AND window_end = :window_end AND outcome = 'held'",
        {"team_id": team_id, "window_end": window_end.astimezone(UTC).replace(tzinfo=None)},
    )
    first = row["first_held"] if row else None
    return first.replace(tzinfo=UTC) if isinstance(first, datetime) else None


def _unassessed(db: Database, run_id: str) -> int:
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM alert_findings "
        "WHERE run_id = :run_id AND quality_state = 'unassessed'",
        {"run_id": run_id},
    )
    return int(row["n"]) if row else 0


def _forced_note(count: int) -> str:
    alerts = "1 alert" if count == 1 else f"{count} alerts"
    return (
        f"Published automatically after {HOLD_DAYS} days: the automated review could not "
        f"assess {alerts} this week. They are shown as Not reviewed; rule findings are complete."
    )


def _latest_published_end(db: Database, team_id: str) -> datetime | None:
    row = db.query_one(
        "SELECT MAX(window_end) AS latest FROM review_publications "
        "WHERE team_id = :team_id AND withdrawn_at IS NULL",
        {"team_id": team_id},
    )
    latest = row["latest"] if row else None
    return latest if isinstance(latest, datetime) else None


def _run_team(
    db: Database,
    team: TeamEntry,
    *,
    config: AppConfig,
    now: datetime,
    es_client: EsClient,
    llm_client: LlmClient | None,
    llm_disabled_reason: str | None,
    registry_path: str | None,
    out_root: Path | None,
    dry_run: bool,
) -> list[Outcome]:
    plan = plan_weeks(_latest_published_end(db, team.team_id), now)
    if plan.blocked:
        return [Outcome(team.team_id, None, "blocked", detail=plan.blocked)]

    outcomes = [
        Outcome(
            team.team_id,
            end,
            "expired",
            detail="older than Elasticsearch retention; not run, and the next week is "
            "published across the gap",
        )
        for end in plan.expired
    ]
    if dry_run:
        return outcomes + [Outcome(team.team_id, end, "due") for end in plan.due]

    allow_gap = bool(plan.expired)
    publishing = True
    held_week: datetime | None = None
    loaded = load_registry(registry_path) if registry_path else load_registry()
    for end in plan.due:
        # Each week starts clean: a previous week's failure leaves its run and stage bound.
        bind_log_context(run_id=None, stage=None)
        try:
            run_id = compute_run_id(
                team_id=team.team_id,
                run_at=end,
                window_start=end - WEEK,
                registry_sha256=loaded.file_sha256,
                model_version=llm_client.model_version if llm_client else None,
            )
            # Only an exact, complete, healthy result is reusable. Unhealthy weeks
            # still get their normal audited retry. Current publication is checked
            # below, under the unchanged full-invocation scheduler lock.
            if run_health(db, run_id):
                payload, summary = execute_run(
                    team_id=team.team_id,
                    run_at=end,
                    config=config,
                    es_client=es_client,
                    llm_client=llm_client,
                    llm_disabled_reason=llm_disabled_reason,
                    registry_path=registry_path,
                    db=db,
                    expected_run_id=run_id,
                    resume_analysis=True,
                )
                persist_run(db, payload)
                run_id = summary.run_id
            else:
                log.info(
                    "weekly.reused_run",
                    "Reusing the complete stored run for the week ending {week}",
                    week=f"{end:%Y-%m-%d}",
                    run_id=run_id,
                )
            # Files only on an explicit --out; downloads render from SQL on request.
            if out_root is not None:
                render_run_report(db, run_id, out_root / run_id[:16])
        except Exception as exc:
            log.error(
                "weekly.run_failed",
                "The run for the week ending {week} failed; later weeks of this team are "
                "stored but not published",
                week=f"{end:%Y-%m-%d}",
                exc=exc,
            )
            outcomes.append(Outcome(team.team_id, end, "failed", detail=redact_error(exc)))
            publishing = False
            continue

        problems = run_health(db, run_id)
        note: str | None = None
        if problems:
            first_held = _first_held(db, team.team_id, end)
            overdue = first_held is not None and now - first_held >= timedelta(days=HOLD_DAYS)
            if not (publishing and overdue):
                outcomes.append(Outcome(team.team_id, end, "held", run_id, "; ".join(problems)))
                publishing = False
                held_week = held_week or end
                continue
            # Held long enough: publish it as it is, and say so to readers.
            note = _forced_note(_unassessed(db, run_id))
        if not publishing:
            waiting = (
                f"waiting for the week ending {held_week:%Y-%m-%d} to be published"
                if held_week
                else "waiting for an earlier week that failed"
            )
            outcomes.append(Outcome(team.team_id, end, "stored", run_id, waiting))
            continue
        try:
            publish_run(db, run_id, published_by=PUBLISHER, allow_gap=allow_gap, note=note)
        except Exception as exc:
            outcomes.append(Outcome(team.team_id, end, "failed", run_id, redact_error(exc)))
            publishing = False
            continue
        allow_gap = False
        detail = f"published after {HOLD_DAYS} days held: " + "; ".join(problems) if note else None
        outcomes.append(Outcome(team.team_id, end, "published", run_id, detail))
    return outcomes


def run_weekly(
    config: AppConfig,
    *,
    now: datetime,
    database: str,
    llm_client: LlmClient | None,
    llm_disabled_reason: str | None = None,
    registry_path: str | None = None,
    teams: Sequence[str] = (),
    out_root: Path | None = None,
    dry_run: bool = False,
    es_client: EsClient | None = None,
) -> list[Outcome]:
    """Run and publish every due week of every enrolled team."""
    enrolled = enrolled_teams(registry_path, teams)
    client = es_client or EsClient(config.es)
    # The invocation's clock is the scheduler's notion of now, so a held week's age is
    # measured on the same clock the weeks are planned on (and --as-of stays consistent).
    invoked_at = now if now.tzinfo else now.replace(tzinfo=UTC)
    outcomes: list[Outcome] = []
    clock = time.monotonic()
    log.info(
        "weekly.started",
        "{mode} as of {now:utc} for {teams} enrolled team{teams:plural}",
        mode="Dry run of the weekly schedule" if dry_run else "Weekly schedule",
        now=invoked_at,
        teams=len(enrolled),
        database=database,
        model=llm_client.model_version if llm_client else f"off ({llm_disabled_reason})",
    )

    with weekly_lock(config, database):
        for team in enrolled:
            with connect(config.sql, database) as db, log_context(team_id=team.team_id):
                try:
                    team_outcomes = _run_team(
                        db,
                        team,
                        config=config,
                        now=now,
                        es_client=client,
                        llm_client=llm_client,
                        llm_disabled_reason=llm_disabled_reason,
                        registry_path=registry_path,
                        out_root=out_root,
                        dry_run=dry_run,
                    )
                except Exception as exc:
                    log.error(
                        "weekly.team_failed",
                        "The weekly schedule could not process this team",
                        exc=exc,
                    )
                    team_outcomes = [
                        Outcome(team.team_id, None, "failed", detail=redact_error(exc))
                    ]
                bind_log_context(run_id=None, stage=None)
                if not dry_run:
                    for outcome in team_outcomes:
                        _log(db, invoked_at, outcome)
                outcomes.extend(team_outcomes)
                for outcome in team_outcomes:
                    _log_outcome(outcome)
    counts: dict[str, Any] = {}
    for outcome in outcomes:
        counts[outcome.outcome] = counts.get(outcome.outcome, 0) + 1
    attention = sum(outcome.needs_attention for outcome in outcomes)
    (log.warn if attention else log.info)(
        "weekly.finished",
        "Finished in {elapsed_seconds:dur}: {summary}. {attention_text}",
        elapsed_seconds=round(time.monotonic() - clock, 3),
        summary=", ".join(f"{n} {name}" for name, n in sorted(counts.items())) or "nothing due",
        attention_text=f"{attention} {'week needs' if attention == 1 else 'weeks need'} a "
        "person (see weekly-status); exiting 1"
        if attention
        else "Nothing needs a person",
        **counts,
    )
    return outcomes


_OUTCOME_TEXT = {
    "published": "Published the week ending {week}",
    "held": "Held the week ending {week}",
    "stored": "Stored the week ending {week} without publishing it",
    "failed": "The week ending {week} failed",
    "blocked": "Blocked",
    "expired": "Skipped the week ending {week}",
    "due": "Due: the week ending {week}",
}


def _log_outcome(outcome: Outcome) -> None:
    if outcome.window_end is None:
        # A blocked team, or one that failed before any week: no week to name, and no
        # placeholder, so the week field holds a date in every record.
        template = {
            "blocked": "Blocked",
            "failed": "The weekly schedule could not process this team",
        }.get(outcome.outcome, "{outcome}")
    else:
        template = _OUTCOME_TEXT.get(outcome.outcome, "{outcome} the week ending {week}")
    if outcome.detail:
        template += ": {detail}"
    emit = (
        log.error
        if outcome.outcome == "failed"
        else log.warn
        if outcome.outcome in ("held", "blocked", "expired")
        else log.info
    )
    emit(
        "weekly.outcome",
        template,
        outcome=outcome.outcome,
        week=f"{outcome.window_end:%Y-%m-%d}" if outcome.window_end else None,
        detail=outcome.detail,
        run_id=outcome.run_id,
    )


def next_due(latest_published_end: datetime | None, now: datetime) -> datetime:
    """The end of the next week the schedule will publish for a team."""
    plan = plan_weeks(latest_published_end, now)
    if plan.due:
        return plan.due[0]
    base = latest_published_end or now
    return (base.replace(tzinfo=UTC) if base.tzinfo is None else base) + WEEK
