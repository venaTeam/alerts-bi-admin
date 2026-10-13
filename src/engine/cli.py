"""Alerts BI command line.

A run always names one team. There is deliberately no "all teams" mode: the MVP reports one
selected team per run, and a default that fanned out would be a scope change hiding in a
convenience.

Failure behaviour follows the blueprint: an invalid registry fails before any alert query,
a failed Elasticsearch query fails the run rather than publishing a partial scorecard as
complete, a failed persistence step fails the run rather than rendering from memory, and a
rendering failure after persistence leaves the analysis committed so rendering can be
retried.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from alerts_bi_operations.report.render import render_run_report
from alerts_bi_shared.config.env import read_str
from alerts_bi_shared.db.connection import connect
from alerts_bi_shared.logging_setup import log
from alerts_bi_shared.timefmt import iso_instant

from .config import AppConfig, load_config
from .db.migrate import (
    applied_migrations,
    current_revision,
    heads,
    load_migrations,
    migrate_database,
    reset_test_database,
)
from .db.repositories import get_latest_run, persist_run
from .es.client import EsClient
from .llm.client import LlmClient
from .llm.fake import FakeLlmClient
from .run.orchestrator import execute_run, select_llm_client
from .weekly.plan import is_week_boundary, latest_week_end
from .weekly.runner import schedule_lock

__all__ = ["ENGINE_COMMANDS", "add_commands", "dispatch"]


def add_commands(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    run = subparsers.add_parser("run", help="analyse one team, persist, and render")
    run.add_argument("--team", required=True, help="registry team_id; never defaults")
    run.add_argument("--run-at", help="Monday 00:00 UTC; defaults to the latest completed Monday")
    run.add_argument("--out", help="output directory; default out/<run id prefix>")
    run.add_argument("--registry", help="registry path; default config/teams.json")
    run.add_argument("--database", help="target database; default SQL_DATABASE")
    llm = run.add_mutually_exclusive_group()
    llm.add_argument(
        "--fake-llm",
        action="store_true",
        help="use the deterministic fake client instead of the on-prem model",
    )
    llm.add_argument(
        "--no-llm",
        action="store_true",
        help="skip assessment; eligible identities become unassessed with a reason",
    )

    report = subparsers.add_parser("report", help="re-render a stored run from SQL only")
    report.add_argument("--run-id", help="run to render")
    report.add_argument("--team", help="render that team's most recent completed run")
    report.add_argument("--out", help="output directory")
    report.add_argument("--database", help="target database; default SQL_DATABASE")

    database = subparsers.add_parser("db", help="database maintenance")
    db_subparsers = database.add_subparsers(dest="db_command", required=True)
    for name, help_text in (
        ("migrate", "create the database if absent and apply pending migrations"),
        ("status", "show which migrations are applied"),
    ):
        sub = db_subparsers.add_parser(name, help=help_text)
        sub.add_argument("--database", help="target database; default SQL_DATABASE")
    db_subparsers.add_parser(
        "reset-test", help="drop and recreate ONLY the configured disposable test database"
    )
    setup = db_subparsers.add_parser(
        "setup",
        help="apply pending migrations; idempotent, for an init container",
    )
    setup.add_argument("--database", help="target database; default SQL_DATABASE")
    grant = db_subparsers.add_parser(
        "grant-reader",
        help="optional legacy utility: create a restricted portal-view login",
    )
    grant.add_argument("--database", help="target database; default SQL_DATABASE")
    grant.add_argument("--login", help="login; default PORTAL_SQL_USER or alerts_bi_portal")

    weekly = subparsers.add_parser(
        "weekly",
        help="run and publish every due Monday-to-Monday UTC week of every enrolled team",
    )
    weekly.add_argument(
        "--as-of",
        help="treat this instant as now (ISO 8601 UTC); for the fixed-clock mock and backfilling tests",
    )
    weekly.add_argument(
        "--team", action="append", default=[], help="only this enrolled team; repeatable"
    )
    weekly.add_argument(
        "--dry-run", action="store_true", help="show the due weeks without running anything"
    )
    weekly.add_argument("--out", help="report directory root; default out/weekly")
    weekly.add_argument("--registry", help="registry path; default config/teams.json")
    weekly.add_argument("--database", help="target database; default SQL_DATABASE")
    weekly_llm = weekly.add_mutually_exclusive_group()
    weekly_llm.add_argument(
        "--fake-llm", action="store_true", help="use the deterministic fake client (mock only)"
    )
    weekly_llm.add_argument(
        "--no-llm",
        action="store_true",
        help="skip assessment; every week is then held, never auto-published",
    )

    weekly_status = subparsers.add_parser(
        "weekly-status", help="each enrolled team's latest published week and schedule outcome"
    )
    weekly_status.add_argument("--registry", help="registry path; default config/teams.json")
    weekly_status.add_argument("--database", help="target database; default SQL_DATABASE")

    verify = subparsers.add_parser(
        "verify-acceptance", help="compare persisted rows and CSVs against the manifest"
    )
    verify.add_argument("--manifest", help="path to expected-results.json")
    verify.add_argument("--out", help="output directory for the rendered reports")
    verify.add_argument("--database", help="target database; default SQL_DATABASE")


def _target_database(args: argparse.Namespace, config: AppConfig) -> str:
    return getattr(args, "database", None) or config.sql.database


def _command_run(args: argparse.Namespace, config: AppConfig) -> int:
    run_at = latest_week_end(datetime.now(UTC))
    if args.run_at:
        try:
            parsed = datetime.fromisoformat(str(args.run_at).replace("Z", "+00:00"))
        except ValueError:
            sys.stderr.write(f"--run-at {args.run_at!r} is not a valid ISO 8601 instant\n")
            return 2
        run_at = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        if not is_week_boundary(run_at):
            sys.stderr.write("--run-at must be a Monday at 00:00 UTC\n")
            return 2

    # The deterministic fake is opt-in and explicit, so a mock run can never be mistaken
    # for a live one: it stamps its own model_version onto the run record.
    client: LlmClient | None
    reason: str | None
    if args.fake_llm:
        client, reason = FakeLlmClient(), None
    else:
        client, reason = select_llm_client(config, use_llm=not args.no_llm)

    es_client = EsClient(config.es)
    with (
        schedule_lock(config, _target_database(args, config)),
        connect(config.sql, _target_database(args, config)) as db,
    ):
        payload, summary = execute_run(
            team_id=args.team,
            run_at=run_at,
            config=config,
            es_client=es_client,
            llm_client=client,
            llm_disabled_reason=reason,
            registry_path=args.registry or read_str("ADMIN_REGISTRY_PATH") or None,
            db=db,
        )

        persist_run(db, payload)
        log.info("run.persisted", run_id=summary.run_id, team_id=summary.team_id)

        out_dir = (
            Path(args.out)
            if args.out
            else Path(read_str("ADMIN_OUT_DIR", "out")) / summary.run_id[:16]
        )
        files = render_run_report(db, summary.run_id, out_dir)

    readiness = (
        "n/a (no v2 identities)" if summary.readiness is None else f"{summary.readiness:.1f}%"
    )
    sys.stdout.write(
        "\n".join(
            [
                f"run_id:        {summary.run_id}",
                f"team:          {summary.team_id}",
                f"phase:         {summary.phase}",
                f"readiness:     {readiness}",
                f"v1:            {summary.v1_rows} rows / {summary.v1_identities} distinct",
                f"v2:            {summary.v2_rows} rows / {summary.v2_identities} distinct",
                f"llm assessed:  {'yes' if summary.llm_assessed else 'no'} "
                f"({summary.llm_eligible} eligible identities)",
                "",
                *[f"wrote {path}" for path in files],
                "",
            ]
        )
    )
    return 0


def _command_report(args: argparse.Namespace, config: AppConfig) -> int:
    if not args.run_id and not args.team:
        sys.stderr.write("--run-id or --team is required\n")
        return 2

    with (
        schedule_lock(config, _target_database(args, config)),
        connect(config.sql, _target_database(args, config)) as db,
    ):
        run_id = args.run_id
        if not run_id:
            latest = get_latest_run(db, args.team)
            if latest is None:
                sys.stderr.write(f"no completed run stored for team {args.team}\n")
                return 1
            run_id = str(latest["run_id"])

        out_dir = (
            Path(args.out) if args.out else Path(read_str("ADMIN_OUT_DIR", "out")) / run_id[:16]
        )
        files = render_run_report(db, run_id, out_dir)

    sys.stdout.write("\n".join(f"wrote {path}" for path in files) + "\n")
    return 0


def _command_db(args: argparse.Namespace, config: AppConfig) -> int:
    if args.db_command == "migrate":
        database = _target_database(args, config)
        applied, already = migrate_database(config.sql, database)
        for version in applied:
            sys.stdout.write(f"applied {version}\n")
        if not applied:
            sys.stdout.write("database is up to date\n")
        log.info(
            "db.migrate_complete",
            database=database,
            applied=len(applied),
            already_applied=len(already),
        )
        return 0

    if args.db_command == "status":
        database = _target_database(args, config)
        with connect(config.sql, database) as db:
            applied_map = applied_migrations(db)
            revision = current_revision(db)
            sys.stdout.write(f"database: {database}\n")
            sys.stdout.write(f"revision: {revision or '(none - never migrated)'}\n")
            for migration in load_migrations():
                if migration.version not in applied_map:
                    state = "pending"
                elif applied_map[migration.version] == migration.checksum:
                    state = "applied"
                else:
                    state = "APPLIED BUT FILE CHANGED"
                sys.stdout.write(f"  {migration.version:<32} {state}\n")

        # More than one head means two migrations were added without agreeing on an order.
        graph_heads = heads()
        if len(graph_heads) > 1:
            sys.stderr.write(
                f"warning: {len(graph_heads)} revision heads ({', '.join(graph_heads)}); "
                "merge them before migrating\n"
            )
        return 0

    if args.db_command == "setup":
        database = _target_database(args, config)
        applied, _ = migrate_database(config.sql, database)
        for version in applied:
            sys.stdout.write(f"applied {version}\n")
        if not applied:
            sys.stdout.write("database is up to date\n")
        return 0

    if args.db_command == "grant-reader":
        from alerts_bi_shared.config.env import read_str

        from .db.reader import grant_reader

        database = _target_database(args, config)
        login = args.login or read_str("PORTAL_SQL_USER", "alerts_bi_portal")
        # The password comes from the environment only, never a flag, so it stays out of
        # shell history and process listings.
        grant_reader(config.sql, database, login, read_str("PORTAL_SQL_PASSWORD"))
        sys.stdout.write(f"{login} can now read the portal views of {database}, and nothing else\n")
        return 0

    # reset-test: only ever the configured disposable test database.
    reset_test_database(config.sql, config.sql.test_database)
    sys.stdout.write(f"recreated {config.sql.test_database}\n")
    return 0


def _instant(value: object) -> str:
    return iso_instant(value)[:16].replace("T", " ") + " UTC" if isinstance(value, datetime) else ""


def _parse_instant(text: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _command_weekly(args: argparse.Namespace, config: AppConfig) -> int:
    from .weekly.runner import WeeklyBusy, run_weekly

    now = datetime.now(UTC)
    if args.as_of:
        parsed = _parse_instant(args.as_of)
        if parsed is None:
            sys.stderr.write(f"--as-of {args.as_of!r} is not a valid ISO 8601 instant\n")
            return 2
        now = parsed

    client: LlmClient | None
    reason: str | None
    if args.fake_llm:
        client, reason = FakeLlmClient(), None
    else:
        client, reason = select_llm_client(config, use_llm=not args.no_llm)

    try:
        outcomes = run_weekly(
            config,
            now=now,
            database=_target_database(args, config),
            llm_client=client,
            llm_disabled_reason=reason,
            registry_path=args.registry or read_str("ADMIN_REGISTRY_PATH") or None,
            teams=args.team,
            out_root=Path(args.out)
            if args.out
            else Path(read_str("ADMIN_OUT_DIR", "out")) / "weekly",
            dry_run=args.dry_run,
        )
    except WeeklyBusy as exc:
        sys.stderr.write(f"{exc}\n")
        return 1

    if not outcomes:
        sys.stdout.write("nothing due\n")
    for outcome in outcomes:
        week = f"week ending {_instant(outcome.window_end)}" if outcome.window_end else "-"
        run = f" run {outcome.run_id[:16]}" if outcome.run_id else ""
        detail = f": {outcome.detail}" if outcome.detail else ""
        sys.stdout.write(f"{outcome.team_id:<24} {outcome.outcome:<9} {week}{run}{detail}\n")
    # Non-zero when a person is needed, so the CronJob shows it as failed.
    return 1 if any(outcome.needs_attention for outcome in outcomes) else 0


def _command_weekly_status(args: argparse.Namespace, config: AppConfig) -> int:
    from .weekly.runner import enrolled_teams, next_due

    teams = enrolled_teams(args.registry or read_str("ADMIN_REGISTRY_PATH") or None)
    if not teams:
        sys.stdout.write("no team is enrolled for the weekly review\n")
        return 0
    now = datetime.now(UTC)
    with connect(config.sql, _target_database(args, config)) as db:
        for team in teams:
            latest = db.query_one(
                "SELECT MAX(window_end) AS latest FROM review_publications "
                "WHERE team_id = :t AND withdrawn_at IS NULL",
                {"t": team.team_id},
            )
            latest_end = latest["latest"] if latest else None
            last = db.query_one(
                "SELECT TOP 1 outcome, window_end, detail, invoked_at FROM weekly_review_log "
                "WHERE team_id = :t ORDER BY log_id DESC",
                {"t": team.team_id},
            )
            published = _instant(latest_end) if latest_end else "nothing published"
            due = _instant(next_due(latest_end, now))
            outcome = (
                f"{last['outcome']} ({_instant(last['invoked_at'])})"
                + (f": {last['detail']}" if last["detail"] else "")
                if last
                else "never scheduled"
            )
            sys.stdout.write(
                f"{team.team_id:<24} latest published week ends {published}; next due {due}; "
                f"last outcome {outcome}\n"
            )
    return 0


def _command_verify(args: argparse.Namespace, config: AppConfig) -> int:
    from .run.verify import verify_acceptance

    result = verify_acceptance(
        config=config,
        manifest_path=args.manifest,
        out_dir=args.out,
        database=args.database,
    )
    if result.ok:
        sys.stdout.write(f"acceptance verification passed: {result.checks} checks\n")
        return 0

    sys.stderr.write(
        f"acceptance verification FAILED: {len(result.failures)} of {result.checks} checks\n\n"
    )
    for failure in result.failures:
        sys.stderr.write(
            f"  {failure.where}\n"
            f"    expected: {failure.expected!r}\n"
            f"    actual:   {failure.actual!r}\n"
        )
    return 1


ENGINE_COMMANDS = frozenset({"run", "report", "db", "weekly", "weekly-status", "verify-acceptance"})


def dispatch(args: argparse.Namespace) -> int:
    config = load_config()
    handlers = {
        "run": _command_run,
        "report": _command_report,
        "db": _command_db,
        "weekly": _command_weekly,
        "weekly-status": _command_weekly_status,
        "verify-acceptance": _command_verify,
    }
    return handlers[args.command](args, config)
