"""Everything the surface does to the pipeline.

The routes are transport: they parse, negotiate and format. This module is where the
surface actually touches the pipeline, and it is deliberately thin - it calls the same
``execute_run`` and ``persist_run`` the command line calls, and renders through the same
``build_run_outputs``. There is no second code path that could drift from the CLI.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from alerts_bi_operations.registry import RegistryError, load_registry
from alerts_bi_operations.report.render import build_run_outputs, render_run_report
from alerts_bi_shared.db.connection import connect
from alerts_bi_shared.logging_setup import log, redact_error
from fastapi import HTTPException, status

from ..config import ApiSettings, AppConfig
from ..db.repositories import get_latest_run, persist_run
from ..es.client import EsClient
from ..llm.client import LlmClient
from ..llm.fake import FakeLlmClient
from ..run.orchestrator import RunSummary, execute_run, select_llm_client
from .schemas import CheckOut, LlmMode, RunRequest, TeamOut

__all__ = ["RunGate", "RunResult", "check_health", "list_teams", "resolve_outputs", "start_run"]


class RunGate:
    """Serializes runs across requests.

    Two concurrent requests for the same team and clock derive the same deterministic
    ``run_id`` and would race to replace each other's rows; two for different teams would
    only compete for the same Elasticsearch and SQL capacity for no gain, since a run reads
    one team at a time by design.

    Runtime state, not configuration, which is why it lives here rather than in
    :mod:`alerts_bi_admin.engine.config.api`.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def acquire(self) -> bool:
        return self._lock.acquire(blocking=False)

    def release(self) -> None:
        self._lock.release()


@dataclass(frozen=True, slots=True)
class RunResult:
    summary: RunSummary
    outputs: dict[str, str]
    out_dir: Path


def list_teams(settings: ApiSettings) -> list[TeamOut]:
    path = settings.registry_path
    try:
        loaded = load_registry(path) if path else load_registry()
    except RegistryError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from None
    return [
        TeamOut(
            team_id=team.team_id,
            display_name=team.display_name,
            v1_operators=list(team.v1_operators),
            v2_operator=team.v2_operator,
            panels=len(team.panels),
        )
        for team in loaded.teams
    ]


def check_health(settings: ApiSettings) -> dict[str, CheckOut]:
    """Report each dependency separately; a failure is data, not an exception."""
    checks: dict[str, CheckOut] = {}
    try:
        es = EsClient(settings.config.es)
        try:
            v1_exists = es.index_exists(settings.config.es.v1_index)
            v2_exists = es.index_exists(settings.config.es.v2_index)
            checks["elasticsearch"] = CheckOut(ok=v1_exists and v2_exists)
        finally:
            es.close()
    except Exception as exc:
        checks["elasticsearch"] = CheckOut(ok=False, error=redact_error(exc))
    try:
        with connect(settings.config.sql, settings.target_database) as db:
            db.query("SELECT 1 AS ok")
        checks["sql_server"] = CheckOut(ok=True, database=settings.target_database)
    except Exception as exc:
        checks["sql_server"] = CheckOut(ok=False, error=redact_error(exc))
    return checks


def select_client(config: AppConfig, mode: LlmMode) -> tuple[LlmClient | None, str | None]:
    # The deterministic fake stamps its own model_version onto the run record, so a run
    # started this way can never be mistaken for a live one after the fact.
    if mode == "fake":
        return FakeLlmClient(), None
    return select_llm_client(config, use_llm=mode == "live")


def start_run(settings: ApiSettings, gate: RunGate, body: RunRequest) -> RunResult:
    """Execute one run, persist it, write its files and render its outputs."""
    run_at = body.run_at or datetime.now(UTC)
    if run_at.tzinfo is None:
        run_at = run_at.replace(tzinfo=UTC)
    client, reason = select_client(settings.config, body.llm)

    if not gate.acquire():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "a run is already in progress; runs are serialized so two cannot race to write "
            "the same rows",
        )
    try:
        es_client = EsClient(settings.config.es)
        with connect(settings.config.sql, settings.target_database) as db:
            payload, summary = execute_run(
                team_id=body.team,
                run_at=run_at,
                config=settings.config,
                es_client=es_client,
                llm_client=client,
                llm_disabled_reason=reason,
                registry_path=settings.registry_path,
                db=db,
            )
            persist_run(db, payload)
            log.info("run.persisted", run_id=summary.run_id, team_id=summary.team_id)

            # The file contract is the same whether a run is started here or from the
            # command line: the four approved files, written once, under out/.
            out_dir = settings.out_root / summary.run_id[:16]
            render_run_report(db, summary.run_id, out_dir)
            return RunResult(summary, build_run_outputs(db, summary.run_id), out_dir)
    except RegistryError as exc:
        # An unknown team or a malformed entry is the caller's mistake, not a server fault,
        # and the registry is validated before any alert is queried.
        detail = "; ".join([str(exc), *exc.details]) if exc.details else str(exc)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail) from None
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
    finally:
        gate.release()


def resolve_outputs(
    settings: ApiSettings, run_id: str, team: str | None
) -> tuple[str, dict[str, str]]:
    """Render a stored run, resolving ``latest`` against a team when asked."""
    with connect(settings.config.sql, settings.target_database) as db:
        resolved = run_id
        if run_id == "latest":
            if not team:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "latest requires ?team=<team_id>")
            latest = get_latest_run(db, team)
            if latest is None:
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND, f"no completed run stored for team {team}"
                )
            resolved = str(latest["run_id"])
        try:
            return resolved, build_run_outputs(db, resolved)
        except ValueError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None
