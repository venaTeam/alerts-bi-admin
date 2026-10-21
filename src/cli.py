"""Unified operator, analysis, migration and weekly-job commands."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime

from alerts_bi_shared.config.env import load_dotenv, read_str
from alerts_bi_shared.db.connection import connect
from alerts_bi_shared.logging_setup import configure_logging, log
from alerts_bi_shared.timefmt import iso_instant
from alerts_bi_shared.versions import APP_VERSION

from .config import AdminConfig, load_config
from .engine.cli import ENGINE_COMMANDS, add_commands, dispatch


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alerts-bi-admin", description="Alerts BI operator and analysis application."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    parser.add_argument("--version", action="version", version=f"alerts-bi-admin {APP_VERSION}")
    serve = subparsers.add_parser(
        "serve",
        help="serve the admin console; it has no login, so anyone who can reach it may use it",
    )
    serve.add_argument("--port", type=int, help="ADMIN_PORT, else 8200")
    serve.add_argument("--database", help="ADMIN_DATABASE, else SQL_DATABASE")
    serve.add_argument("--registry", help="team registry path; default config/teams.json")
    worker = subparsers.add_parser("run-worker", help="execute durable manual run submissions")
    worker.add_argument("--database", help="ADMIN_DATABASE, else SQL_DATABASE")
    worker.add_argument("--registry", help="ADMIN_REGISTRY_PATH, else the packaged registry")
    worker.add_argument("--once", action="store_true", help="process at most one job and exit")
    publish = subparsers.add_parser(
        "publish", help="publish a completed run as its team's weekly review"
    )
    publish.add_argument("--run-id", required=True, help="the completed run to publish")
    publish.add_argument("--note", help="review note shown to readers with this week")
    publish.add_argument(
        "--replace",
        action="store_true",
        help="publish in place of the run already published for exactly this week",
    )
    publish.add_argument(
        "--allow-gap",
        action="store_true",
        help="publish even though the week is not adjacent to the team's published weeks",
    )
    publish.add_argument("--database", help="target database; default SQL_DATABASE")

    unpublish = subparsers.add_parser("unpublish", help="withdraw a published weekly review")
    unpublish.add_argument("--run-id", required=True)
    unpublish.add_argument("--reason", required=True, help="why it is withdrawn; kept for audit")
    unpublish.add_argument("--database", help="target database; default SQL_DATABASE")

    publications = subparsers.add_parser(
        "publications", help="list a team's publications, current and withdrawn"
    )
    publications.add_argument("--team", required=True)
    publications.add_argument("--database", help="target database; default SQL_DATABASE")

    decide = subparsers.add_parser(
        "decide", help="record a human decision on one finding of a published week"
    )
    decide.add_argument("--run-id", help="the published week, by run id")
    decide.add_argument("--team", help="the published week, by team ...")
    decide.add_argument("--week", help="... and the UTC date the week ends, YYYY-MM-DD")
    decide.add_argument("--schema", required=True, choices=("v1", "v2"))
    decide.add_argument("--application", required=True)
    decide.add_argument("--key-field", required=True)
    decide.add_argument("--finding", required=True, help="finding id, e.g. R1, R9, P2, OTHER")
    decide.add_argument("--state", required=True, choices=("pending", "confirmed", "dismissed"))
    decide.add_argument("--note", required=True, help="why; readers see it")
    decide.add_argument("--database", help="target database; default SQL_DATABASE")

    decisions = subparsers.add_parser("decisions", help="list a team's decision history")
    decisions.add_argument("--team", required=True)
    decisions.add_argument("--database", help="target database; default SQL_DATABASE")

    registry = subparsers.add_parser("registry", help="team registry tools")
    registry_subparsers = registry.add_subparsers(dest="registry_command", required=True)
    check = registry_subparsers.add_parser(
        "check", help="validate the registry before deploying an edit"
    )
    check.add_argument("--registry", help="registry path; default config/teams.json")

    add_commands(subparsers)
    return parser


def _target_database(args: argparse.Namespace, config: AdminConfig) -> str:
    return str(args.database or config.sql.database)


def _instant(value: object) -> str:
    return iso_instant(value)[:16].replace("T", " ") + " UTC" if isinstance(value, datetime) else ""


def _command_publish(args: argparse.Namespace, config: AdminConfig) -> int:
    from alerts_bi_operations.review.publication import PublicationRefused, publish_run

    with connect(config.sql, _target_database(args, config)) as db:
        try:
            result = publish_run(
                db,
                args.run_id,
                note=args.note,
                replace=args.replace,
                allow_gap=args.allow_gap,
            )
        except PublicationRefused as exc:
            sys.stderr.write(f"not published: {exc}\n")
            return 1
    sys.stdout.write(
        f"published {result.team_id}: week {_instant(result.window_start)} to "
        f"{_instant(result.window_end)} (run {result.run_id[:16]})\n"
    )
    if result.replaced_run_id:
        sys.stdout.write(f"withdrew the earlier publication of run {result.replaced_run_id[:16]}\n")
    return 0


def _command_unpublish(args: argparse.Namespace, config: AdminConfig) -> int:
    from alerts_bi_operations.review.publication import PublicationRefused, unpublish_run

    with connect(config.sql, _target_database(args, config)) as db:
        try:
            unpublish_run(db, args.run_id, reason=args.reason)
        except PublicationRefused as exc:
            sys.stderr.write(f"not withdrawn: {exc}\n")
            return 1
    sys.stdout.write(f"withdrew run {args.run_id[:16]}; readers no longer see that week\n")
    return 0


def _command_publications(args: argparse.Namespace, config: AdminConfig) -> int:
    from alerts_bi_operations.review.publication import list_publications

    with connect(config.sql, _target_database(args, config)) as db:
        rows = list_publications(db, args.team)
    if not rows:
        sys.stdout.write(f"{args.team} has no publications\n")
        return 0
    for row in rows:
        state = (
            "published"
            if row["withdrawn_at"] is None
            else f"withdrawn {_instant(row['withdrawn_at'])}: {row['withdrawn_reason']}"
        )
        scheduled = " by the weekly schedule" if row["published_by"] == "weekly-schedule" else ""
        sys.stdout.write(
            f"{_instant(row['window_start'])} to {_instant(row['window_end'])}  "
            f"run {str(row['run_id'])[:16]}  published {_instant(row['published_at'])}"
            f"{scheduled}  [{state}]\n"
        )
    return 0


def _command_decide(args: argparse.Namespace, config: AdminConfig) -> int:
    from alerts_bi_operations.review.decisions import (
        DecisionRefused,
        record_decision,
        resolve_published_run,
    )

    with connect(config.sql, _target_database(args, config)) as db:
        try:
            run_id = resolve_published_run(
                db, run_id=args.run_id, team_id=args.team, week=args.week
            )
            decision_id = record_decision(
                db,
                run_id=run_id,
                alert_schema=args.schema,
                application=args.application,
                key_field=args.key_field,
                finding_id=args.finding,
                state=args.state,
                note=args.note,
            )
        except DecisionRefused as exc:
            sys.stderr.write(f"not recorded: {exc}\n")
            return 1
    sys.stdout.write(f"recorded decision {decision_id}: {args.finding} {args.state}\n")
    return 0


def _command_decisions(args: argparse.Namespace, config: AdminConfig) -> int:
    from alerts_bi_operations.review.decisions import list_decisions

    with connect(config.sql, _target_database(args, config)) as db:
        rows = list_decisions(db, args.team)
    if not rows:
        sys.stdout.write(f"{args.team} has no recorded decisions\n")
        return 0
    for row in rows:
        sys.stdout.write(
            f"{_instant(row['decided_at'])}  {row['state']:<9} {row['finding_id']:<5} "
            f"{row['alert_schema']} {row['application']} / {row['key_field']}  "
            f"{row['note']}\n"
        )
    return 0


def _command_registry(args: argparse.Namespace, config: AdminConfig) -> int:
    from alerts_bi_operations.registry import RegistryError, load_registry

    try:
        registry_path = args.registry or read_str("ADMIN_REGISTRY_PATH") or None
        loaded = load_registry(registry_path) if registry_path else load_registry()
    except RegistryError as exc:
        sys.stderr.write(f"registry is invalid: {exc}\n")
        for detail in exc.details:
            sys.stderr.write(f"  {detail}\n")
        return 1
    sys.stdout.write(
        f"registry {loaded.registry_version} is valid: {len(loaded.teams)} teams, "
        f"sha256 {loaded.file_sha256[:16]}\n"
    )
    for team in loaded.teams:
        enrolled = "weekly" if team.weekly_review else "manual"
        sys.stdout.write(f"  {team.team_id:<28} {enrolled}\n")
    return 0


def _command_admin(args: argparse.Namespace, config: AdminConfig) -> int:
    import uvicorn

    from .app import build_admin
    from .config import load_admin_settings

    settings = load_admin_settings(
        config,
        port=args.port,
        database=args.database,
        registry_path=args.registry,
    )
    log.info(
        "admin.listening",
        "Admin console {version} on http://{host}:{port}; it has no login, so anyone who can "
        "reach this address may use it. Writing {database}",
        version=APP_VERSION,
        host=settings.host,
        port=settings.port,
        database=settings.database,
        sql=f"{config.sql.host}:{config.sql.port}",
        registry=settings.registry_path or "packaged",
    )
    uvicorn.run(
        build_admin(settings),
        host=settings.host,
        port=settings.port,
        log_level="warning",
        # Uvicorn's own warnings and errors go through the shared log format.
        log_config=None,
        access_log=False,
        server_header=False,
    )
    return 0


def _process_name(args: argparse.Namespace) -> str:
    """The short name each console line carries: which container or job wrote it."""
    if args.command == "serve":
        return "ui"
    if args.command == "run-worker":
        return "worker"
    if args.command == "weekly":
        return "weekly"
    if args.command == "db" and getattr(args, "db_command", None) in ("setup", "migrate"):
        return "migrate"
    return "cli"


def main(argv: list[str] | None = None) -> int:
    # A bare invocation starts the app; subcommands expose the existing operator tools.
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or (
        arguments[0].startswith("--") and arguments[0] not in ("--help", "--version")
    ):
        arguments.insert(0, "serve")
    args = build_parser().parse_args(arguments)
    # LOG_LEVEL and LOG_FORMAT may come from .env, so read it before naming the process.
    load_dotenv()
    configure_logging(_process_name(args))
    if args.command in ENGINE_COMMANDS:
        return dispatch(args)
    config = load_config()
    if args.command == "run-worker":
        from dataclasses import replace

        from alerts_bi_shared.config.env import read_str

        from .engine.api.jobs import run_worker
        from .engine.config import ApiSettings
        from .engine.config import load_config as load_engine_config

        run_worker(
            ApiSettings(
                replace(load_engine_config(), sql=config.sql),
                database=args.database or read_str("ADMIN_DATABASE") or config.sql.database,
                registry_path=args.registry or read_str("ADMIN_REGISTRY_PATH") or None,
            ),
            once=args.once,
        )
        return 0
    handlers = {
        "serve": _command_admin,
        "publish": _command_publish,
        "unpublish": _command_unpublish,
        "publications": _command_publications,
        "decide": _command_decide,
        "decisions": _command_decisions,
        "registry": _command_registry,
    }
    return handlers[args.command](args, config)


def run_cli() -> None:
    try:
        sys.exit(main())
    except Exception as exc:
        log.error("admin.cli_failed", "The command stopped with an error", exc=exc)
        sys.exit(1)
