"""Reproducible SQL/HTTP scale probe, restricted to local alerts_bi_test.

Requires the independently built portal installed in the benchmark environment.
--seed explicitly resets ONLY alerts_bi_test; never run alongside the test suite.
The default mix is one 20k-identity team, nine 2k teams and ninety 50-identity teams,
27 weekly runs each. --heavy-teams 100 --heavy-identities 50000 tests a much larger
upper envelope. Raw event counts are represented by exact persisted aggregates;
use benchmark_streaming.py separately for actual million-event CPU/memory work.
"""

from __future__ import annotations

import argparse
import json
import math
import platform
import sys
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from contextvars import ContextVar
from dataclasses import replace
from datetime import datetime, timedelta
from hashlib import sha256
from importlib import import_module
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alerts_bi_operations.registry import load_registry
from alerts_bi_shared.db.connection import Database, connect
from alerts_bi_shared.insights.reader import _decode
from fastapi.testclient import TestClient
from src.app import build_admin
from src.config import AdminConfig, AdminSettings
from src.engine.config import load_config
from src.engine.db.migrate import reset_test_database
from src.engine.db.repositories import PersistencePayload, persist_run
from tests.engine.helpers.sql import sample_daily, sample_finding, sample_run

from scripts.benchmark_streaming import peak_memory_mib

END = datetime(2026, 10, 12)
TABLES = (
    "runs",
    "daily_metrics",
    "daily_rule_counts",
    "alert_findings",
    "run_summaries",
    "run_alert_list",
    "run_v1_rules",
    "run_application_daily",
)
READS: ContextVar[list[int] | None] = ContextVar("benchmark_reads", default=None)


def run_id(team: int, week: int) -> str:
    return sha256(f"scale:{team}:{week}".encode()).hexdigest()


def payload(team: int, identities: int, events: int) -> PersistencePayload:
    name = f"bench-{team:03d}"
    rid = run_id(team, 0)
    run = sample_run(
        run_id=rid,
        team_id=name,
        team_display_name=f"Benchmark {team:03d}",
        run_at=END,
        window_start=END - timedelta(days=7),
        window_end=END,
        registry_entry_snapshot=json.dumps(
            {"team_id": name, "v1_operators": [name], "v2_operator": name}
        ),
        started_at=END,
        completed_at=END,
    )
    rows = []
    hot = max(1, identities // 50)
    extra = max(0, events - identities)
    for i in range(identities):
        schema = "v1" if i % 2 else "v2"
        count = 1 + (extra // hot + (i < extra % hot) if i < hot else 0)
        core = "R1,R6" if i % 3 == 0 else "R5" if i % 7 == 0 else ""
        ready = "R8,R9" if schema == "v2" and i % 5 == 0 else ""
        state = "rule_flagged" if core else "llm_flagged" if i % 11 == 0 else "assessed_good"
        app = f"{name}-app-{i % 10}"
        evidence = json.dumps(
            [
                {
                    "rule_id": r,
                    "matched_rows": count,
                    "sample_evidence": {"message": "synthetic benchmark alert"},
                }
                for r in filter(None, (core + "," + ready).split(","))
            ]
        )
        rows.append(
            sample_finding(
                run_id=rid,
                alert_schema=schema,
                application=app,
                key_field=f"key-{i:06d}",
                row_count=count,
                message="Synthetic capacity fixture: request latency exceeds the configured threshold",
                severity="critical" if ready else "warning",
                core_rule_ids=core,
                readiness_rule_ids=ready,
                findings_evidence=evidence,
                quality_state=state,
                llm_principle_id="P1" if state == "llm_flagged" else "NONE",
                clear_count=0,
                max_clear_cycles_24h=0,
                max_episode_firing_rows=count,
                fire_pattern="stuck" if "R6" in core else None,
                unseen=bool(i % 17 == 0),
                alert_rule_url=f"https://benchmark.invalid/rules/{i // 4}",
                representative_at=END - timedelta(hours=1),
                first_seen=END - timedelta(days=6),
                last_seen=END - timedelta(hours=1),
            )
        )
    daily = []
    rules = []
    app_daily = []
    for schema in ("v1", "v2"):
        members = [r for r in rows if r["alert_schema"] == schema]
        total = sum(r["row_count"] for r in members)
        flagged = sum(r["row_count"] for r in members if r["core_rule_ids"])
        suppressed = sum(r["row_count"] for r in members if "R5" in r["core_rule_ids"])
        unseen = sum(r["row_count"] for r in members if r["unseen"])
        daily.append(
            sample_daily(
                run_id=rid,
                team_id=name,
                alert_schema=schema,
                snapshot_date=(END - timedelta(days=1)).date(),
                bucket_start=END - timedelta(days=1),
                bucket_end=END,
                alerts=total,
                distinct_alerts=len(members),
                alerts_per_hour=total / 24,
                flagged_by_rule=flagged,
                flagged_by_rule_distinct=sum(bool(r["core_rule_ids"]) for r in members),
                suppressed=suppressed,
                unseen=unseen,
                unseen_unmeasured=0,
            )
        )
        for rule in ("R1", "R5", "R6", "R8", "R9"):
            carriers = [
                r
                for r in members
                if rule in (r["core_rule_ids"] + "," + r["readiness_rule_ids"]).split(",")
            ]
            if carriers:
                rules.append(
                    {
                        "run_id": rid,
                        "team_id": name,
                        "alert_schema": schema,
                        "snapshot_date": daily[-1]["snapshot_date"],
                        "rule_id": rule,
                        "ruleset_version": "1.0.0",
                        "match_count": sum(r["row_count"] for r in carriers),
                        "distinct_count": len(carriers),
                    }
                )
        for app in sorted({r["application"] for r in members}):
            subset = [r for r in members if r["application"] == app]
            app_daily.append(
                {
                    "run_id": rid,
                    "alert_schema": schema,
                    "application": app,
                    "snapshot_date": daily[-1]["snapshot_date"],
                    "covered_hours": 24,
                    "events": sum(r["row_count"] for r in subset),
                    "distinct_alerts": len(subset),
                    "flagged_rows": sum(r["row_count"] for r in subset if r["core_rule_ids"]),
                    "flagged_by_rule_distinct": sum(bool(r["core_rule_ids"]) for r in subset),
                    "suppressed": sum(r["row_count"] for r in subset if "R5" in r["core_rule_ids"]),
                    "unseen": sum(r["row_count"] for r in subset if r["unseen"]),
                }
            )
    return PersistencePayload(
        run=run, findings=rows, daily_metrics=daily, rule_counts=rules, application_daily=app_daily
    )


def clone(
    db: Database, source: str, target: str, days: int, columns: dict[str, list[dict[str, Any]]]
) -> None:
    with db.transaction():
        for table in TABLES:
            fields = columns[table]
            names = ",".join(f"[{r['name']}]" for r in fields)
            expressions = ",".join(
                ":target"
                if r["name"] == "run_id"
                else f"DATEADD(day,:days,[{r['name']}])"
                if r["type"] in ("date", "datetime2")
                else f"[{r['name']}]"
                for r in fields
            )
            db.execute(
                f"INSERT INTO {table}({names}) SELECT {expressions} FROM {table} WHERE run_id=:source",
                {"source": source, "target": target, "days": days},
            )
        db.execute(
            "INSERT INTO run_rule_membership(finding_id,run_id,rule_id,matched_rows) SELECT n.finding_id,:target,m.rule_id,m.matched_rows FROM run_rule_membership m JOIN run_alert_list o ON o.finding_id=m.finding_id JOIN run_alert_list n ON n.run_id=:target AND n.alert_schema=o.alert_schema AND n.application=o.application AND n.key_field=o.key_field WHERE m.run_id=:source",
            {"target": target, "source": source},
        )


def publish(db: Database, rid: str) -> None:
    db.execute(
        "INSERT INTO review_publications(run_id,team_id,window_start,window_end,published_at,published_by) SELECT run_id,team_id,window_start,window_end,SYSUTCDATETIME(),'capacity-fixture' FROM runs WHERE run_id=:run AND NOT EXISTS (SELECT 1 FROM review_publications WHERE run_id=:run)",
        {"run": rid},
    )
    db.commit()
    db.execute(
        "INSERT INTO finding_decisions(team_id,run_id,alert_schema,application,key_field,finding_id,state,note,decided_at,decided_by) "
        "SELECT r.team_id,f.run_id,f.alert_schema,f.application,f.key_field,'R1','confirmed','Synthetic capacity decision',r.window_end,'capacity-fixture' "
        "FROM run_alert_list f JOIN runs r ON r.run_id=f.run_id WHERE f.run_id=:run AND f.core_rule_ids LIKE 'R1%' AND NOT EXISTS (SELECT 1 FROM finding_decisions WHERE run_id=:run)",
        {"run": rid},
    )
    db.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", action="store_true")
    parser.add_argument(
        "--resume-seed",
        action="store_true",
        help="Resume an interrupted fixture load with exactly the same workload sizes",
    )
    parser.add_argument("--teams", type=int, default=100)
    parser.add_argument("--weeks", type=int, default=27)
    parser.add_argument("--heavy-teams", type=int, default=1)
    parser.add_argument("--heavy-identities", type=int, default=20000)
    parser.add_argument("--heavy-events", type=int, default=1200000)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--requests", type=int, default=80)
    parser.add_argument(
        "--read-committed-snapshot",
        action="store_true",
        help="Enable versioned reads only in local alerts_bi_test before measurement",
    )
    parser.add_argument("--output", type=Path, default=Path("out/scale-reads.json"))
    args = parser.parse_args()
    if (
        min(
            args.teams,
            args.weeks,
            args.heavy_identities,
            args.heavy_events,
            args.concurrency,
            args.requests,
        )
        < 1
        or not 0 <= args.heavy_teams <= args.teams
    ):
        parser.error("Positive bounded workload sizes are required")
    config = load_config()
    if (
        config.sql.host not in ("localhost", "127.0.0.1")
        or config.sql.test_database != "alerts_bi_test"
    ):
        raise SystemExit("Only local alerts_bi_test is allowed")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    registry = args.output.with_suffix(".teams.json")
    registry.write_text(
        json.dumps(
            {
                "registry_version": "2026-10-13.1",
                "effective_date": "2026-10-13",
                "teams": [
                    {
                        "team_id": f"bench-{i:03d}",
                        "display_name": f"Benchmark {i:03d}",
                        "v1_operators": [f"bench-{i:03d}"],
                        "v2_operator": f"bench-{i:03d}",
                        "panels": [],
                    }
                    for i in range(args.teams)
                ],
            }
        ),
        encoding="utf-8",
    )
    load_registry(str(registry))
    seed_started = perf_counter()
    if args.seed and args.resume_seed:
        parser.error("Choose --seed or --resume-seed")
    if args.seed or args.resume_seed:
        if args.seed:
            reset_test_database(config.sql, config.sql.test_database)
        # Offline fixture cloning is separate from measured application timeouts.
        with connect(
            replace(config.sql, request_timeout_ms=600000), config.sql.test_database
        ) as db:
            existing = {str(r["run_id"]) for r in db.query("SELECT run_id FROM runs")}
            columns = {
                table: db.query(
                    "SELECT c.name,t.name AS type FROM sys.columns c JOIN sys.types t ON t.user_type_id=c.user_type_id WHERE c.object_id=OBJECT_ID(:table) AND c.is_identity=0 ORDER BY c.column_id",
                    {"table": table},
                )
                for table in TABLES
            }
            for team in range(args.teams):
                identities = (
                    args.heavy_identities if team < args.heavy_teams else 2000 if team < 10 else 50
                )
                events = args.heavy_events if team < args.heavy_teams else identities * 20
                if run_id(team, 0) not in existing:
                    persist_run(db, payload(team, identities, events))
                publish(db, run_id(team, 0))
                for week in range(1, args.weeks):
                    if run_id(team, week) not in existing:
                        clone(db, run_id(team, 0), run_id(team, week), -7 * week, columns)
                    publish(db, run_id(team, week))
                    if team < args.heavy_teams and week % 5 == 0:
                        print(json.dumps({"seeding_team": team, "week": week}), flush=True)
                print(
                    json.dumps(
                        {
                            "seeded_teams": team + 1,
                            "weeks": args.weeks,
                            "identities_per_run": identities,
                        }
                    ),
                    flush=True,
                )
    seed_seconds = perf_counter() - seed_started
    if args.read_committed_snapshot:
        with connect(config.sql, "master", autocommit=True) as master:
            master.execute(
                "IF (SELECT is_read_committed_snapshot_on FROM sys.databases WHERE name='alerts_bi_test')=0 "
                "ALTER DATABASE [alerts_bi_test] SET READ_COMMITTED_SNAPSHOT ON WITH NO_WAIT"
            )
    portal_app = import_module("alerts_bi_portal.app")
    portal_config = import_module("alerts_bi_portal.config")
    original = Database.query

    def measured_query(
        self: Database, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        result = original(self, sql, params)
        counts = READS.get()
        if counts is not None:
            counts.append(len(result))
        return result

    Database.query = measured_query  # type: ignore[method-assign]
    results: dict[str, Any] = {
        "environment": {
            "platform": platform.platform(),
            "python": sys.version,
            "teams": args.teams,
            "weeks": args.weeks,
            "heavy_teams": args.heavy_teams,
            "heavy_identities": args.heavy_identities,
            "heavy_events": args.heavy_events,
            "concurrency": args.concurrency,
            "seed_seconds": round(seed_seconds, 3),
            "cold_definition": "empty decoded-summary cache; SQL buffer cache is not flushed",
            "timing": "real ASGI entry points through TestClient, including render and local adapter overhead",
        }
    }
    with ExitStack() as stack:
        admin = stack.enter_context(
            TestClient(
                build_admin(
                    AdminSettings(
                        AdminConfig(config.sql),
                        config.sql.test_database,
                        "capacity-test-secret-" * 3,
                        registry_path=str(registry),
                    )
                ),
                client=("127.0.0.1", 50000),
            )
        )
        portal = stack.enter_context(
            TestClient(
                portal_app.build_portal(
                    portal_config.PortalSettings(config.sql, config.sql.test_database)
                ),
                client=("127.0.0.1", 50001),
            )
        )
        routes = [
            ("admin_directory", admin, "/"),
            ("admin_overview", admin, "/teams/bench-000"),
            ("admin_findings", admin, "/teams/bench-000?tab=findings"),
            ("portal_directory", portal, "/"),
            ("portal_overview", portal, "/teams/bench-000"),
            (
                "portal_filter",
                portal,
                "/teams/bench-000/weeks/2026-10-12/fix?rule=R1&scope=selected&apps=bench-000-app-0",
            ),
            ("portal_deep_page", portal, "/teams/bench-000/weeks/2026-10-12/fix?show=all&page=100"),
        ]

        ingesting = threading.Event()

        def request(route: tuple[str, TestClient, str]) -> dict[str, Any]:
            name, client, path = route
            counts: list[int] = []
            token = READS.set(counts)
            start = perf_counter()
            overlapped_write = ingesting.is_set()
            try:
                response = client.get(path)
                elapsed = (perf_counter() - start) * 1000
                return {
                    "route": name,
                    "ms": elapsed,
                    "bytes": len(response.content),
                    "sql_rows": sum(counts),
                    "sql_queries": len(counts),
                    "status": response.status_code,
                    "overlapped_write": overlapped_write,
                }
            except Exception as exc:
                return {
                    "route": name,
                    "ms": (perf_counter() - start) * 1000,
                    "bytes": 0,
                    "sql_rows": sum(counts),
                    "sql_queries": len(counts),
                    "status": 0,
                    "error": type(exc).__name__,
                    "overlapped_write": overlapped_write,
                }
            finally:
                READS.reset(token)

        _decode.cache_clear()
        results["cold"] = [request(route) for route in routes]
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            warm = list(pool.map(request, [routes[i % len(routes)] for i in range(args.requests)]))
        started = threading.Event()

        def ingest() -> float:
            incoming = payload(args.teams, args.heavy_identities, args.heavy_events)
            with connect(config.sql, config.sql.test_database) as db:
                ingesting.set()
                started.set()
                start = perf_counter()
                try:
                    persist_run(db, incoming)
                    return perf_counter() - start
                finally:
                    ingesting.clear()

        with ThreadPoolExecutor(max_workers=args.concurrency + 1) as pool:
            writer = pool.submit(ingest)
            started.wait(30)
            during = list(
                pool.map(request, [routes[i % len(routes)] for i in range(args.requests)])
            )
            results["ingestion_seconds"] = writer.result()
        results["during_ingestion_definition"] = (
            "Only requests begun while the writer transaction was active; blocked requests may finish after commit"
        )
        for label, measurements in (
            ("warm", warm),
            ("during_ingestion", [r for r in during if r["overlapped_write"]]),
        ):
            grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for item in measurements:
                grouped[item["route"]].append(item)
            results[label] = {
                name: {
                    "requests": len(items),
                    "p50_ms": round(sorted(i["ms"] for i in items)[len(items) // 2], 2),
                    "p95_ms": round(
                        sorted(i["ms"] for i in items)[math.ceil(len(items) * 0.95) - 1], 2
                    ),
                    "max_sql_rows": max(i["sql_rows"] for i in items),
                    "max_ms": round(max(i["ms"] for i in items), 2),
                    "failures": sum(i["status"] != 200 for i in items),
                    "max_sql_queries": max(i["sql_queries"] for i in items),
                    "max_response_bytes": max(i["bytes"] for i in items),
                }
                for name, items in grouped.items()
            }
        # Real durable submission endpoint. No worker executes these fixture jobs;
        # each newly created job is closed as a test failure before the next sample.
        token = admin.get("/csrf").json()["token"]
        acknowledgements = []
        for _ in range(20):
            start = perf_counter()
            response = admin.post(
                "/run-jobs",
                json={"team": "bench-000", "run_at": "2026-10-12T00:00:00Z", "llm": "fake"},
                headers={"X-CSRF-Token": token, "Idempotency-Key": "capacity-" + uuid4().hex},
            )
            acknowledgements.append((perf_counter() - start) * 1000)
            response.raise_for_status()
            with connect(config.sql, config.sql.test_database) as db:
                db.execute(
                    "UPDATE run_jobs SET status='failed',active_slot=NULL,completed_at=SYSUTCDATETIME(),error_summary='Capacity fixture: intentionally not executed' WHERE job_id=:id AND status='pending'",
                    {"id": response.json()["job_id"]},
                )
                db.commit()
        results["acknowledgement"] = {
            "new_submissions": len(acknowledgements),
            "p95_ms": round(sorted(acknowledgements)[18], 2),
        }
    Database.query = original  # type: ignore[method-assign]
    with connect(config.sql, config.sql.test_database) as db:
        results["sql_version"] = db.query_one(
            "SELECT CAST(SERVERPROPERTY('ProductVersion') AS NVARCHAR(128)) AS version"
        )
        results["read_committed_snapshot"] = db.query_one(
            "SELECT is_read_committed_snapshot_on AS enabled FROM sys.databases WHERE name=DB_NAME()"
        )
        results["storage"] = db.query(
            "SELECT OBJECT_NAME(p.object_id) AS table_name,SUM(CASE WHEN p.index_id IN (0,1) THEN p.row_count ELSE 0 END) AS rows,SUM(p.reserved_page_count)*8 AS reserved_kib FROM sys.dm_db_partition_stats p GROUP BY p.object_id ORDER BY table_name"
        )
        results["database_files"] = db.query(
            "SELECT type_desc,size*8 AS kib FROM sys.database_files"
        )
    results["peak_process_mib"] = round(peak_memory_mib(), 1)
    args.output.write_text(json.dumps(results, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"Evidence: {args.output}")
    if any(r["status"] != 200 for r in [*results["cold"], *warm, *during]):
        raise SystemExit("Benchmark recorded request failures; inspect the evidence")


if __name__ == "__main__":
    main()
