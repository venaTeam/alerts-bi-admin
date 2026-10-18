"""The operator admin app's factory (design section 7.12).

Every request needs a signed-in operator, taken from the login proxy's identity header. Every
write is a POST that must carry the anti-forgery token and come from this site; it calls the
same :mod:`alerts_bi_operations.review` functions the command line calls, under the operator's identity, and
then redirects back with a message.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import parse_qs, urlencode

from alerts_bi_operations.registry import RegistryError, load_registry
from alerts_bi_operations.review.decisions import DecisionRefused, record_decision
from alerts_bi_operations.review.publication import PublicationRefused, publish_run, unpublish_run
from alerts_bi_shared.db.connection import Database, WebDatabasePool
from alerts_bi_shared.logging_setup import log, redact_error
from alerts_bi_shared.timefmt import iso_instant
from alerts_bi_shared.versions import APP_VERSION
from fastapi import FastAPI, HTTPException, Query, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from . import pages, queries, summary
from .auth import csrf_token, identity, same_site, valid_csrf
from .config import AdminSettings
from .engine.api.backfill import (
    backfill_jobs,
    cancel_backfill,
    dismiss_backfill,
    get_backfill,
    job_backfill,
    latest_backfill,
    parse_weeks,
    retry_backfill,
    selectable_weeks,
    submit_backfill,
    week_rows,
)
from .engine.api.jobs import get_job, submit_job, team_jobs
from .engine.api.negotiation import wants_html
from .engine.api.routers.runs import router as runs_router
from .engine.api.schemas import BackfillOut, HealthOut, RunJobOut, RunRequest, TeamsOut
from .engine.api.service import (
    RunGate,
    WeeklyBusy,
    check_health,
    list_teams,
    schedule_lock,
)
from .readiness import check_schema
from .runs import DeletionRefused, delete_run

__all__ = ["SECURITY_HEADERS", "build_admin"]

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self' data:; form-action 'self'; "
        "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

_MAX_FORM = 64 * 1024

Page = Annotated[int, Query(ge=1, le=100_000)]
Message = Annotated[str | None, Query(max_length=500)]
RunId = Annotated[str | None, Query(max_length=128)]
StateFilter = Annotated[str, Query(pattern="^(" + "|".join(summary.STATES) + ")$")]
SchemaFilter = Annotated[str, Query(pattern="^(all|v1|v2)$")]
RuleFilter = Annotated[str, Query(pattern=summary.RULE_PATTERN)]
SortOrder = Annotated[str, Query(pattern="^(" + "|".join(summary.SORTS) + ")$")]
Tab = Annotated[str, Query(pattern="^(overview|findings|decisions|activity)$")]


def build_admin(settings: AdminSettings) -> FastAPI:
    pool = WebDatabasePool(settings.config.sql, settings.database)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            pool.dispose()

    app = FastAPI(
        lifespan=lifespan,
        title="Alerts BI operator console",
        version=APP_VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    api_settings = settings.api_settings()
    gate = RunGate(durable=True)
    app.state.settings = api_settings
    app.state.run_gate = gate

    @contextmanager
    def database() -> Iterator[Database]:
        with pool.connect() as db:
            yield db

    def user_of(request: Request) -> str:
        user: str = request.state.user
        return user

    def token_for(request: Request) -> str:
        return csrf_token(settings.secret, user_of(request), datetime.now(UTC).date())

    @app.middleware("http")
    async def guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        user = identity(request, settings)
        if user is None:
            response: Response = PlainTextResponse(
                "Sign in through the operator Route.", status_code=status.HTTP_401_UNAUTHORIZED
            )
        elif request.method not in ("GET", "HEAD", "POST"):
            response = PlainTextResponse(
                "Method not allowed.", status_code=status.HTTP_405_METHOD_NOT_ALLOWED
            )
        else:
            request.state.user = user
            # The compatibility JSON trigger uses the same identity, CSRF and RunGate.
            if (
                request.method == "POST"
                and request.url.path.rstrip("/") in ("/runs", "/run-jobs")
                and (
                    not same_site(request)
                    or not valid_csrf(
                        settings.secret,
                        user,
                        request.headers.get("X-CSRF-Token", ""),
                        datetime.now(UTC).date(),
                    )
                )
            ):
                response = PlainTextResponse("A same-site CSRF token is required.", status_code=403)
            else:
                response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException) -> Response:
        if not wants_html(request):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        user = getattr(request.state, "user", "") or ""
        return HTMLResponse(
            pages.error_page(exc.status_code, str(exc.detail), user), exc.status_code
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> Response:
        if not wants_html(request):
            return JSONResponse({"detail": jsonable_encoder(exc.errors())}, status_code=422)
        return HTMLResponse(
            pages.error_page(
                422,
                "Invalid selection or request. Return to the team and choose a valid value.",
                user_of(request),
            ),
            422,
        )

    async def form(request: Request) -> dict[str, str]:
        """Parse and authenticate a POSTed form: same site, and a valid token for this user."""
        return {key: values[-1] for key, values in (await form_values(request)).items()}

    async def form_values(request: Request) -> dict[str, list[str]]:
        """Every value of each authenticated form field, for repeated checkboxes."""
        if not same_site(request):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "Cross-site form submissions are refused."
            )
        body = await request.body()
        if len(body) > _MAX_FORM:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "The form is too large.")
        fields = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
        if not valid_csrf(
            settings.secret,
            user_of(request),
            (fields.get("csrf") or [""])[-1],
            datetime.now(UTC).date(),
        ):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "The form has expired or did not come from this app."
            )
        return fields

    def back(
        path: str, message: str, error: bool = False, anchor: str = "selected-run"
    ) -> RedirectResponse:
        key = "error" if error else "done"
        separator = "&" if "?" in path else "?"
        fragment = f"#{anchor}" if anchor else ""
        return RedirectResponse(
            f"{path}{separator}{urlencode({key: message})}{fragment}", status.HTTP_303_SEE_OTHER
        )

    def run_row(db: Database, run_id: str) -> dict[str, Any]:
        row = db.query_one(
            "SELECT r.*, p.publication_id, p.published_by, p.published_at, p.review_note, "
            "(SELECT COUNT(*) FROM weekly_review_log l WHERE l.run_id=r.run_id) AS scheduled, "
            "(SELECT COUNT(*) FROM run_jobs j WHERE j.run_id=r.run_id AND j.backfill_id IS NOT NULL "
            "AND j.status='completed') AS backfilled, "
            "(SELECT TOP 1 l.outcome FROM weekly_review_log l WHERE l.run_id=r.run_id ORDER BY l.log_id DESC) AS schedule_outcome, "
            "(SELECT TOP 1 l.detail FROM weekly_review_log l WHERE l.run_id=r.run_id ORDER BY l.log_id DESC) AS schedule_detail, "
            "(SELECT COALESCE(SUM(CASE WHEN COALESCE(f.core_rule_ids,'')='' THEN 0 ELSE LEN(f.core_rule_ids)-LEN(REPLACE(f.core_rule_ids,',',''))+1 END + "
            "CASE WHEN COALESCE(f.readiness_rule_ids,'')='' THEN 0 ELSE LEN(f.readiness_rule_ids)-LEN(REPLACE(f.readiness_rule_ids,',',''))+1 END + "
            "CASE WHEN f.quality_state IN ('llm_flagged','needs_review') AND COALESCE(f.llm_principle_id,'')<>'' THEN 1 ELSE 0 END),0) FROM alert_findings f WHERE f.run_id=r.run_id) AS findings_count, "
            "(SELECT COUNT(*) FROM finding_decisions d WHERE d.run_id=r.run_id) AS decisions_count, "
            "(SELECT COUNT(*) FROM review_publications w WHERE w.run_id=r.run_id AND w.withdrawn_at IS NOT NULL) AS withdrawn "
            "FROM runs r LEFT JOIN review_publications p ON p.run_id=r.run_id AND p.withdrawn_at IS NULL "
            "WHERE r.run_id = :run_id",
            {"run_id": run_id},
        )
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such run.")
        return row

    def registry_teams() -> list[tuple[str, str, bool]]:
        try:
            loaded = (
                load_registry(settings.registry_path) if settings.registry_path else load_registry()
            )
        except RegistryError as exc:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from None
        return [(t.team_id, t.display_name, t.weekly_review) for t in loaded.teams]

    @app.get(pages.STYLESHEET_PATH, include_in_schema=False)
    def stylesheet() -> Response:
        return Response(
            pages.STYLESHEET,
            media_type="text/css; charset=utf-8",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @app.get(pages.SCRIPT_PATH, include_in_schema=False)
    def script() -> Response:
        return Response(
            pages.SCRIPT,
            media_type="text/javascript",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @app.get("/csrf")
    def csrf(request: Request) -> dict[str, str]:
        return {"token": token_for(request)}

    @app.get("/teams", response_model=TeamsOut)
    def teams_json() -> TeamsOut:
        return TeamsOut(teams=list_teams(api_settings))

    @app.get("/healthz")
    def healthz(response: Response) -> HealthOut:
        checks = check_health(api_settings)
        try:
            with database() as db:
                check_schema(db)
        except Exception as exc:
            log.error("admin.database_unavailable", error=redact_error(exc))
            from .engine.api.schemas import CheckOut

            checks["operator_schema"] = CheckOut(
                ok=False, error="The operator schema is unavailable."
            )
        healthy = all(check.ok for check in checks.values())
        if not healthy:
            response.status_code = 503
        return HealthOut(ok=healthy, version=APP_VERSION, checks=checks)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, done: Message = None, error: Message = None) -> HTMLResponse:
        teams = registry_teams()
        with database() as db:
            overview = queries.team_overview(db)
        return HTMLResponse(
            pages.dashboard_page(user_of(request), teams, overview, error or done, bool(error))
        )

    @app.get("/schedule", response_class=HTMLResponse)
    def schedule(request: Request) -> HTMLResponse:
        with database() as db:
            overview = queries.team_overview(db)
        return HTMLResponse(
            pages.schedule_page(user_of(request), registry_teams(), overview, token_for(request))
        )

    @app.get("/decisions", response_class=HTMLResponse)
    def decisions(request: Request, page: Page = 1) -> HTMLResponse:
        with database() as db:
            rows, total = queries.all_decisions(db, page)
        return HTMLResponse(
            pages.decisions_page(
                user_of(request), rows, total, page, registry_teams(), token_for(request)
            )
        )

    @app.get("/teams/{team_id}", response_class=HTMLResponse)
    def team(
        request: Request,
        team_id: str,
        done: Message = None,
        error: Message = None,
        run_id: RunId = None,
        job_id: Annotated[str | None, Query(max_length=32, pattern=r"^[a-f0-9]{32}$")] = None,
        tab: Tab = "overview",
        run_page: Page = 1,
        state: StateFilter = "all",
        schema: SchemaFilter = "all",
        rule: RuleFilter = "",
        sort: SortOrder = "events",
        page: Page = 1,
        finding: Annotated[str, Query(max_length=24, pattern=r"^[a-f0-9]*$")] = "",
    ) -> HTMLResponse:
        names = {team_id_: name for team_id_, name, _ in registry_teams()}
        enrollment = {t: e for t, _, e in registry_teams()}
        now = datetime.now(UTC)
        with database() as db:
            jobs = team_jobs(db, team_id)
            backfill_row = latest_backfill(db, team_id)
            backfill_weeks = (
                week_rows(backfill_jobs(db, str(backfill_row["backfill_id"])))
                if backfill_row
                else []
            )
            calendar = queries.backfill_calendar(db, team_id, selectable_weeks(now)[0])
            if job_id:
                submitted = get_job(db, job_id)
                if submitted["team_id"] != team_id:
                    raise HTTPException(404, "No such submission for this team.")
                if submitted["status"] == "completed" and not run_id:
                    run_id = str(submitted["run_id"])
            count = db.query_one(
                "SELECT COUNT(*) AS n FROM runs WHERE team_id=:team_id", {"team_id": team_id}
            )
            total = int(count["n"]) if count else 0
            run_page = min(run_page, max(1, (total + 19) // 20))
            runs = queries.team_runs(db, team_id, page=run_page)
            if team_id not in names and not runs:
                raise HTTPException(404, "No such team.")
            selected = (
                run_row(db, run_id)
                if run_id
                else run_row(db, str(runs[0]["run_id"]))
                if runs
                else None
            )
            if selected and selected["team_id"] != team_id:
                raise HTTPException(404, "No such run for this team.")
            content = (
                pages.activity_content(queries.team_log(db, team_id), []) if not selected else ""
            )
            if selected:
                selected_id = str(selected["run_id"])
                snapshot = summary.read_snapshot(selected.get("registry_entry_snapshot"))
                sources = [
                    name
                    for name, enabled in (
                        ("v1", bool(snapshot.v1_operators)),
                        ("v2", bool(snapshot.v2_operator)),
                    )
                    if enabled
                ]
                selected["sources_label"] = (
                    " + ".join(sources) + " sources" if sources else "Team sources"
                )
                if tab == "overview" and selected["status"] == "completed":
                    filters = summary.WorklistFilter(
                        state=state,
                        schema=schema,
                        rule=rule,
                        sort=sort,
                        page=page,
                        cursor=request.query_params.get("cursor"),
                    )
                    loaded = summary.load_admin_summary(db, team_id, selected_id, filters)
                    content = summary.overview_content(loaded)
                elif tab == "overview":
                    from alerts_bi_shared.ui.html import h

                    content = f'<section class="abi-panel abi-panel-pad"><h3>Analysis {h(selected["status"])}</h3><p class="abi-small abi-mt">{h(selected.get("error_summary") or "No completed analysis is available.")}</p><p class="abi-small abi-muted abi-mt">Only completed runs can be published.</p></section>'
                elif tab == "findings":
                    rows, finding_total = queries.alerts_with_findings(db, selected_id, page)
                    history = queries.decision_history(
                        db,
                        [
                            (
                                str(row["alert_schema"]),
                                str(row["application"]),
                                str(row["key_field"]),
                            )
                            for row in rows
                        ],
                        team_id,
                    )
                    content = pages.findings_content(
                        selected, rows, finding_total, page, token_for(request), history, finding
                    )
                elif tab == "decisions":
                    decisions, decision_total = queries.run_decisions(db, selected_id, page)
                    content = pages.decisions_content(selected, decisions, decision_total, page)
                else:
                    pubs = db.query(
                        "SELECT * FROM review_publications WHERE run_id=:run_id ORDER BY publication_id DESC",
                        {"run_id": selected_id},
                    )
                    content = pages.activity_content(queries.team_log(db, team_id), pubs, selected)
        name = names.get(team_id) or str(runs[0]["team_display_name"])
        backfill_active = any(
            week["status"] in ("queued", "pending", "running") for week in backfill_weeks
        )
        backfill_html = (
            pages.backfill_panel(team_id, backfill_row, backfill_weeks, token_for(request))
            if backfill_row
            else ""
        )
        if not backfill_active and team_id in names:
            backfill_html = pages.gap_banner(calendar, now) + backfill_html
        return HTMLResponse(
            pages.team_page(
                user_of(request),
                team_id,
                name,
                enrollment.get(team_id, False),
                runs,
                total,
                run_page,
                selected,
                tab,
                content,
                token_for(request),
                error or done,
                bool(error),
                jobs=jobs,
                backfill=backfill_html,
                backfill_dialog=pages.backfill_form(
                    team_id, token_for(request), name, calendar, now
                )
                if team_id in names
                else "",
                backfill_active=backfill_active,
            )
        )

    @app.get("/teams/{team_id}/summary", response_class=HTMLResponse)
    def team_summary(
        request: Request,
        team_id: str,
        run_id: RunId = None,
        state: StateFilter = "all",
        schema: SchemaFilter = "all",
        rule: RuleFilter = "",
        sort: SortOrder = "events",
        page: Page = 1,
    ) -> RedirectResponse:
        return RedirectResponse(
            pages.team_url(
                team_id,
                run_id,
                state=state,
                schema=schema,
                rule=rule,
                sort=sort,
                page=page,
                cursor=request.query_params.get("cursor"),
            ),
            303,
        )

    @app.get("/runs/{run_id}/findings", response_class=HTMLResponse)
    def findings(
        request: Request,
        run_id: str,
        page: Page = 1,
        done: Message = None,
        error: Message = None,
        finding: Annotated[str, Query(max_length=24, pattern=r"^[a-f0-9]*$")] = "",
    ) -> RedirectResponse:
        with database() as db:
            run = run_row(db, run_id)
            if finding:
                page = queries.finding_page(db, run_id, finding)
        messages = {k: v for k, v in {"done": done, "error": error}.items() if v}
        return RedirectResponse(
            pages.team_url(
                str(run["team_id"]), run_id, "findings", page=page, finding=finding, **messages
            ),
            303,
        )

    def team_path(team_id: str, run_id: str) -> str:
        return pages.team_url(team_id, run_id)

    @app.post("/teams/{team_id}/trigger")
    async def trigger(request: Request, team_id: str) -> Response:
        fields = await form(request)
        if team_id not in {t for t, _, _ in registry_teams()}:
            raise HTTPException(404, "No such registered team.")
        try:
            # A date is midnight UTC; RunRequest also rejects forged non-Monday times.
            body = RunRequest.model_validate(
                {"team": team_id, "run_at": fields.get("run_at"), "llm": fields.get("llm")}
            )
        except ValidationError:
            return back(
                pages.team_url(team_id),
                "Choose a Monday at 00:00 UTC and a valid model mode.",
                True,
            )
        try:
            job = await run_in_threadpool(
                submit_job, api_settings, body, user_of(request), fields.get("submission_key", "")
            )
        except HTTPException as exc:
            return back(pages.team_url(team_id), str(exc.detail), True)
        except Exception as exc:
            log.error(
                "console.trigger_failed",
                team_id=team_id,
                operator=user_of(request),
                error=redact_error(exc),
            )
            return back(
                pages.team_url(team_id),
                "The run could not finish. Check the service logs and team history before retrying.",
                True,
            )
        log.info("console.submitted", job_id=job["job_id"], operator=user_of(request))
        return back(
            pages.team_url(team_id, job_id=job["job_id"]),
            "Run accepted. It starts before the next backfill week. You can leave this page."
            if job["status"] == "queued"
            else "Run accepted. You can leave this page and return later.",
        )

    def job_status(row: dict[str, Any]) -> dict[str, Any]:
        result = {
            key: row[key]
            for key in (
                "job_id",
                "team_id",
                "run_id",
                "status",
                "submitted_at",
                "started_at",
                "completed_at",
                "error_summary",
            )
        }
        for key in ("submitted_at", "started_at", "completed_at"):
            result[key] = iso_instant(row[key]) if row[key] is not None else None
        return result | {
            "status_url": f"/run-jobs/{row['job_id']}",
            "result_url": pages.team_url(str(row["team_id"]), str(row["run_id"])) + "#selected-run"
            if row["status"] == "completed"
            else None,
        }

    @app.post(
        "/run-jobs",
        status_code=202,
        response_model=RunJobOut,
        summary="Accept a durable manual run submission",
        responses={
            400: {"description": "Invalid ASCII submission key or unknown team"},
            401: {"description": "Operator identity required"},
            403: {"description": "Same-site actor-bound CSRF required"},
            409: {"description": "Another operation is active or the key has different options"},
        },
        openapi_extra={
            "parameters": [
                {
                    "name": "Idempotency-Key",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string", "minLength": 1, "maxLength": 64},
                    "description": "Exact ASCII key scoped to the authenticated operator",
                },
                {
                    "name": "X-CSRF-Token",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string"},
                    "description": "Actor-bound token from authenticated GET /csrf",
                },
            ]
        },
    )
    def create_job(request: Request, body: RunRequest) -> JSONResponse:
        row = submit_job(
            api_settings, body, user_of(request), request.headers.get("Idempotency-Key", "")
        )
        return JSONResponse(
            jsonable_encoder(job_status(row)),
            status_code=202,
            headers={"Location": f"/run-jobs/{row['job_id']}"},
        )

    @app.get(
        "/run-jobs/{job_id}",
        response_model=RunJobOut,
        summary="Read durable job state for an authenticated operator",
        responses={
            401: {"description": "Operator identity required"},
            404: {"description": "Unknown submission"},
        },
    )
    def read_job(job_id: str) -> JSONResponse:
        with database() as db:
            return JSONResponse(jsonable_encoder(job_status(get_job(db, job_id))))

    def backfill_out(backfill: dict[str, Any], jobs: list[dict[str, Any]]) -> dict[str, Any]:
        weeks = week_rows(jobs)
        return {
            "backfill_id": backfill["backfill_id"],
            "team_id": backfill["team_id"],
            "llm_mode": backfill["llm_mode"],
            "submitted_at": iso_instant(backfill["submitted_at"]),
            "cancelled_at": iso_instant(backfill["cancelled_at"])
            if backfill["cancelled_at"] is not None
            else None,
            "active": any(week["status"] in ("queued", "pending", "running") for week in weeks),
            "weeks": [
                {
                    "week_end": iso_instant(week["run_at"]),
                    "job_id": week["job_id"],
                    "run_id": week["run_id"],
                    "status": week["status"],
                    "attempts": week["attempts"],
                    "started_at": iso_instant(week["started_at"])
                    if week["started_at"] is not None
                    else None,
                    "completed_at": iso_instant(week["completed_at"])
                    if week["completed_at"] is not None
                    else None,
                    "error_summary": week["error_summary"],
                    "result_url": pages.team_url(str(week["team_id"]), str(week["run_id"]))
                    + "#selected-run"
                    if week["status"] == "completed"
                    else None,
                }
                for week in weeks
            ],
        }

    @app.post("/teams/{team_id}/backfill")
    async def backfill(request: Request, team_id: str) -> Response:
        values = await form_values(request)
        if team_id not in {t for t, _, _ in registry_teams()}:
            raise HTTPException(404, "No such registered team.")
        path = pages.team_url(team_id)
        llm = (values.get("llm") or ["live"])[-1]
        if llm not in ("live", "fake", "off"):
            return back(path, "Choose a valid model mode.", True, "")
        try:
            weeks = parse_weeks(values.get("week", []))
            row = await run_in_threadpool(
                submit_backfill,
                api_settings,
                team_id,
                weeks,
                llm,  # type: ignore[arg-type]
                user_of(request),
                (values.get("submission_key") or [""])[-1],
            )
        except HTTPException as exc:
            return back(path, str(exc.detail), True, "")
        except Exception as exc:
            log.error(
                "console.backfill_failed",
                team_id=team_id,
                operator=user_of(request),
                error=redact_error(exc),
            )
            return back(path, "The backfill could not be saved. Check the service logs.", True, "")
        log.info(
            "console.backfill_submitted", backfill_id=row["backfill_id"], operator=user_of(request)
        )
        count = int(row["week_count"])
        return back(
            path,
            f"Backfill accepted. {count} {'run' if count == 1 else 'runs'} queued, oldest first. "
            "You can leave this page and return later.",
            anchor="",
        )

    @app.get(
        "/backfills/{backfill_id}",
        response_model=BackfillOut,
        summary="Read backfill progress for an authenticated operator",
        responses={
            401: {"description": "Operator identity required"},
            404: {"description": "Unknown backfill"},
        },
    )
    def read_backfill(backfill_id: str) -> JSONResponse:
        with database() as db:
            row = get_backfill(db, backfill_id)
            return JSONResponse(jsonable_encoder(backfill_out(row, backfill_jobs(db, backfill_id))))

    async def backfill_action(
        request: Request, backfill_id: str, act: Callable[[str], tuple[dict[str, Any], str, bool]]
    ) -> Response:
        await form(request)
        user = user_of(request)
        with database() as db:
            team_id = str(get_backfill(db, backfill_id)["team_id"])
        path = pages.team_url(team_id)
        try:
            _, message, failed = await run_in_threadpool(act, user)
        except HTTPException as exc:
            return back(path, str(exc.detail), True, "")
        return back(path, message, failed, "")

    @app.post("/backfills/{backfill_id}/cancel")
    async def cancel_backfill_route(request: Request, backfill_id: str) -> Response:
        def act(user: str) -> tuple[dict[str, Any], str, bool]:
            with database() as db:
                row, count = cancel_backfill(db, backfill_id, user)
                running = any(
                    job["status"] in ("pending", "running")
                    for job in backfill_jobs(db, backfill_id)
                )
            if not count:
                return row, "No queued weeks were left to cancel.", True
            noun = "week" if count == 1 else "weeks"
            message = f"{count} queued {noun} cancelled."
            return row, message + (" The running week will finish." if running else ""), False

        return await backfill_action(request, backfill_id, act)

    @app.post("/backfills/{backfill_id}/retry")
    async def retry_backfill_route(request: Request, backfill_id: str) -> Response:
        def act(user: str) -> tuple[dict[str, Any], str, bool]:
            row, count = retry_backfill(api_settings, backfill_id, user)
            noun = "week" if count == 1 else "weeks"
            return row, f"Retry queued for {count} failed {noun}.", False

        return await backfill_action(request, backfill_id, act)

    @app.post("/backfills/{backfill_id}/dismiss")
    async def dismiss_backfill_route(request: Request, backfill_id: str) -> Response:
        def act(user: str) -> tuple[dict[str, Any], str, bool]:
            with database() as db:
                return dismiss_backfill(db, backfill_id), "Backfill dismissed.", False

        return await backfill_action(request, backfill_id, act)

    @app.post("/run-jobs/{job_id}/cancel")
    async def cancel_week(request: Request, job_id: str) -> Response:
        with database() as db:
            backfill_id = job_backfill(db, job_id)

        def act(user: str) -> tuple[dict[str, Any], str, bool]:
            with database() as db:
                row, count = cancel_backfill(db, backfill_id, user, job_id)
            if not count:
                return row, "Only queued weeks can be removed.", True
            return row, "Week removed from the backfill.", False

        return await backfill_action(request, backfill_id, act)

    @app.post("/run-jobs/{job_id}/retry")
    async def retry_week(request: Request, job_id: str) -> Response:
        with database() as db:
            backfill_id = job_backfill(db, job_id)

        def act(user: str) -> tuple[dict[str, Any], str, bool]:
            row, _ = retry_backfill(api_settings, backfill_id, user, job_id)
            return row, "Retry queued for this week.", False

        return await backfill_action(request, backfill_id, act)

    @app.post("/runs/{run_id}/publish")
    async def publish(request: Request, run_id: str) -> Response:
        fields = await form(request)
        user = user_of(request)

        def act() -> RedirectResponse:
            with database() as db:
                run = run_row(db, run_id)
                try:
                    result = publish_run(
                        db,
                        run_id,
                        published_by=user,
                        note=fields.get("note") or None,
                        replace=fields.get("replace") == "1",
                        allow_gap=False,
                    )
                except PublicationRefused as exc:
                    detail = str(exc)
                    if "--allow-gap" in detail:
                        detail = "Published weeks must be back to back. Publish the missing Monday week first."
                    return back(
                        team_path(str(run["team_id"]), run_id), f"Not published: {detail}", True
                    )
            log.info("admin.published", run_id=run_id, operator=user)
            message = "Published."
            if result.replaced_run_id:
                message += (
                    f" The earlier publication of run {result.replaced_run_id[:16]} was withdrawn."
                )
            return back(team_path(str(run["team_id"]), run_id), message)

        return await run_in_threadpool(act)

    @app.post("/runs/{run_id}/withdraw")
    async def withdraw(request: Request, run_id: str) -> Response:
        fields = await form(request)
        user = user_of(request)

        def act() -> RedirectResponse:
            with database() as db:
                run = run_row(db, run_id)
                try:
                    unpublish_run(db, run_id, withdrawn_by=user, reason=fields.get("reason", ""))
                except PublicationRefused as exc:
                    return back(
                        team_path(str(run["team_id"]), run_id), f"Not withdrawn: {exc}", True
                    )
            log.info("admin.withdrawn", run_id=run_id, operator=user)
            return back(
                team_path(str(run["team_id"]), run_id),
                "Withdrawn. Readers no longer see this week.",
            )

        return await run_in_threadpool(act)

    @app.post("/runs/{run_id}/delete")
    async def delete(request: Request, run_id: str) -> Response:
        fields = await form(request)
        user = user_of(request)
        if fields.get("confirm_delete") != "1":
            raise HTTPException(400, "Confirm permanent deletion of this run.")

        def act() -> RedirectResponse:
            if not gate.acquire():
                raise HTTPException(409, "A run is active. Try deleting after it finishes.")
            try:
                # delete_run checks the active slot inside its own transaction;
                # retain its browser refusal message while holding the shared lock.
                with schedule_lock(api_settings.config, settings.database, allow_manual_job=True):
                    with database() as db:
                        run = run_row(db, run_id)
                        try:
                            team_id = delete_run(db, run_id)
                        except DeletionRefused as exc:
                            return back(team_path(str(run["team_id"]), run_id), str(exc), True)
                    log.info("admin.deleted", run_id=run_id, team_id=team_id, operator=user)
                    return back(pages.team_url(team_id), "Run permanently deleted.")
            except WeeklyBusy:
                raise HTTPException(
                    409,
                    "A run, report, weekly schedule or deletion is active. Try deleting after it finishes.",
                ) from None
            finally:
                gate.release()

        return await run_in_threadpool(act)

    @app.post("/runs/{run_id}/decide")
    async def decide(request: Request, run_id: str) -> Response:
        fields = await form(request)
        user = user_of(request)

        def act() -> RedirectResponse:
            with database() as db:
                run = run_row(db, run_id)
                identity = {
                    "alert_schema": fields.get("schema", ""),
                    "application": fields.get("application", ""),
                    "key_field": fields.get("key_field", ""),
                }
                selected_finding = pages.finding_key(identity, fields.get("finding", ""))
                selected_page = queries.finding_page(db, run_id, selected_finding)
                path = pages.team_url(
                    str(run["team_id"]),
                    run_id,
                    "findings",
                    page=selected_page,
                    finding=selected_finding,
                )
                try:
                    record_decision(
                        db,
                        run_id=run_id,
                        alert_schema=fields.get("schema", ""),
                        application=fields.get("application", ""),
                        key_field=fields.get("key_field", ""),
                        finding_id=fields.get("finding", ""),
                        state=fields.get("state", ""),
                        note=fields.get("note", ""),
                        decided_by=user,
                    )
                except DecisionRefused as exc:
                    return back(path, f"Not recorded: {exc}", True)
                except Exception as exc:  # pragma: no cover - database failure
                    log.error("admin.decision_failed", error=redact_error(exc))
                    return back(path, "Not recorded: the database refused it.", True)
            log.info("admin.decided", run_id=run_id, operator=user)
            return back(path, "Decision recorded.")

        return await run_in_threadpool(act)

    app.include_router(runs_router)
    return app
