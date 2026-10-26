"""The operator admin app's factory (design section 7.12).

The console has no login (design "Admin console without login", 2026-10-19): it needs no
identity and records no operator names. Every write is a POST that must carry the anti-forgery
token and come from this site; it calls the same :mod:`alerts_bi_operations.review` functions
the command line calls, and then redirects back with a message.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import ExitStack, asynccontextmanager, contextmanager
from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import parse_qs, urlencode

from alerts_bi_operations.registry import LoadedRegistry, RegistryError, load_registry
from alerts_bi_operations.review.decisions import (
    DECISION_STATES,
    DecisionRefused,
    findings_on,
    record_decision,
)
from alerts_bi_operations.review.publication import PublicationRefused, publish_run, unpublish_run
from alerts_bi_shared.db.connection import Database, WebDatabasePool
from alerts_bi_shared.logging_setup import log
from alerts_bi_shared.request_logging import RequestLogMiddleware
from alerts_bi_shared.team_urls import decode_team_id
from alerts_bi_shared.timefmt import iso_instant
from alerts_bi_shared.versions import APP_VERSION
from fastapi import FastAPI, HTTPException, Query, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from . import pages, queries, summary
from .auth import csrf_token, same_site, valid_csrf
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
Tab = Annotated[str, Query(pattern="^(overview|findings|decisions|activity|publish)$")]
Show = Annotated[str, Query(pattern="^(all|attention)$")]
DecisionState = Annotated[str, Query(pattern="^(|" + "|".join(DECISION_STATES) + ")$")]
DecisionView = Annotated[str, Query(pattern="^(|needed|recorded)$")]
TeamFilter = Annotated[str, Query(max_length=128)]


def _team_id_from_path(value: str) -> str:
    try:
        return decode_team_id(value)
    except ValueError as exc:
        raise HTTPException(404, "Invalid team URL.") from exc


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

    def form_token() -> str:
        return csrf_token(settings.secret, datetime.now(UTC).date())

    @app.middleware("http")
    async def guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method not in ("GET", "HEAD", "POST"):
            response: Response = PlainTextResponse(
                "Method not allowed.", status_code=status.HTTP_405_METHOD_NOT_ALLOWED
            )
        else:
            # The compatibility JSON trigger uses the same CSRF token and RunGate.
            if (
                request.method == "POST"
                and request.url.path.rstrip("/") in ("/runs", "/run-jobs")
                and (
                    not same_site(request)
                    or not valid_csrf(
                        settings.secret,
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
        return HTMLResponse(pages.error_page(exc.status_code, str(exc.detail)), exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> Response:
        if not wants_html(request):
            return JSONResponse({"detail": jsonable_encoder(exc.errors())}, status_code=422)
        return HTMLResponse(
            pages.error_page(
                422,
                "Invalid selection or request. Return to the team and choose a valid value.",
            ),
            422,
        )

    async def form(request: Request) -> dict[str, str]:
        """Parse and check a POSTed form: same site, and a valid token."""
        return {key: values[-1] for key, values in (await form_values(request)).items()}

    async def form_values(request: Request) -> dict[str, list[str]]:
        """Every value of each checked form field, for repeated checkboxes."""
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
            (fields.get("csrf") or [""])[-1],
            datetime.now(UTC).date(),
        ):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "The form has expired or did not come from this app."
            )
        return fields

    def back(path: str, message: str, error: bool = False, anchor: str = "") -> RedirectResponse:
        if error:
            # Refusals return a redirect, so without this they would leave no trace.
            log.info("console.refused", "Refused: {message}", message=message, page=path)
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
            "(SELECT COUNT(*) FROM review_publications w WHERE w.run_id=r.run_id AND w.withdrawn_at IS NOT NULL) AS withdrawn, "
            "(SELECT COALESCE(CAST(JSON_VALUE(s.payload,'$.schemas.v1.states.unassessed') AS BIGINT),0)+"
            "COALESCE(CAST(JSON_VALUE(s.payload,'$.schemas.v2.states.unassessed') AS BIGINT),0) "
            "FROM run_summaries s WHERE s.run_id=r.run_id AND s.scope_kind='team') AS unassessed "
            "FROM runs r LEFT JOIN review_publications p ON p.run_id=r.run_id AND p.withdrawn_at IS NULL "
            "WHERE r.run_id = :run_id",
            {"run_id": run_id},
        )
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such run.")
        return row

    def current_registry() -> LoadedRegistry:
        try:
            return (
                load_registry(settings.registry_path) if settings.registry_path else load_registry()
            )
        except RegistryError as exc:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from None

    def registry_teams() -> list[tuple[str, str, bool]]:
        return [(t.team_id, t.display_name, t.weekly_review) for t in current_registry().teams]

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
        return {"token": form_token()}

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
            log.error(
                "admin.database_unavailable",
                "Health check: the operator schema cannot be read",
                exc=exc,
            )
            from .engine.api.schemas import CheckOut

            checks["operator_schema"] = CheckOut(
                ok=False, error="The operator schema is unavailable."
            )
        healthy = all(check.ok for check in checks.values())
        if not healthy:
            response.status_code = 503
        return HealthOut(ok=healthy, version=APP_VERSION, checks=checks)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(
        request: Request, done: Message = None, error: Message = None, show: Show = "all"
    ) -> HTMLResponse:
        teams = registry_teams()
        with database() as db:
            overview = queries.team_overview(db)
        return HTMLResponse(pages.dashboard_page(teams, overview, error or done, bool(error), show))

    @app.get("/schedule", response_class=HTMLResponse)
    def schedule(request: Request) -> HTMLResponse:
        with database() as db:
            overview = queries.team_overview(db)
        return HTMLResponse(pages.schedule_page(registry_teams(), overview, datetime.now(UTC)))

    @app.get("/decisions", response_class=HTMLResponse)
    def decisions(
        request: Request,
        page: Page = 1,
        team: TeamFilter = "",
        state: DecisionState = "",
        view: DecisionView = "",
    ) -> HTMLResponse:
        """Findings that need a decision by default; a state filter alone means recorded."""
        view = view or ("recorded" if state else "needed")
        with database() as db:
            if view == "needed":
                rows, total = queries.undecided_findings(db, page, team)
                counts = (total, queries.decision_count(db, team))
            else:
                rows, total = queries.all_decisions(db, page, team, state)
                counts = (queries.undecided_count(db, team), queries.decision_count(db, team))
        return HTMLResponse(
            pages.decisions_page(
                rows,
                total,
                page,
                registry_teams(),
                team=team,
                state=state,
                view=view,
                counts=counts,
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
        """One week of a team: a submission, a failed run, or a completed run's current step."""
        team_id = _team_id_from_path(team_id)
        registry = current_registry()
        registered = [(t.team_id, t.display_name, t.weekly_review) for t in registry.teams]
        names = {key: label for key, label, _ in registered}
        enrolled = {key: on for key, _, on in registered}.get(team_id, False)
        now = datetime.now(UTC)
        token = form_token()
        flash, failed = error or done, bool(error)
        with database() as db:
            jobs = team_jobs(db, team_id)
            backfill_row = latest_backfill(db, team_id)
            backfill_weeks = (
                week_rows(backfill_jobs(db, str(backfill_row["backfill_id"])))
                if backfill_row
                else []
            )
            calendar = queries.backfill_calendar(db, team_id, selectable_weeks(now)[0])
            selected_job: dict[str, Any] | None = None
            if job_id:
                submitted = get_job(db, job_id)
                if submitted["team_id"] != team_id:
                    raise HTTPException(404, "No such submission for this team.")
                if submitted["status"] == "completed" and not run_id:
                    run_id = str(submitted["run_id"])
                elif submitted["status"] not in ("completed", "cancelled") and not run_id:
                    selected_job = submitted
            count = db.query_one(
                "SELECT COUNT(*) AS n FROM runs WHERE team_id=:team_id", {"team_id": team_id}
            )
            total = int(count["n"]) if count else 0
            run_page = min(run_page, max(1, (total + 19) // 20))
            runs = queries.team_runs(db, team_id, page=run_page)
            if team_id not in names and not runs:
                raise HTTPException(404, "No such team.")
            if selected_job is None and not run_id:
                newest = max(
                    (job for job in jobs if job["status"] in pages.ACTIVE_JOB),
                    key=lambda job: job["run_at"],
                    default=None,
                )
                if newest is not None and (not runs or newest["run_at"] > runs[0]["run_at"]):
                    selected_job = newest
            selected = None
            if selected_job is None and (run_id or runs):
                selected = run_row(db, run_id or str(runs[0]["run_id"]))
                if selected["team_id"] != team_id:
                    raise HTTPException(404, "No such run for this team.")
            name = names.get(team_id) or str(runs[0]["team_display_name"])
            logs = queries.team_log(db, team_id)
            first_end = queries.first_run_end(db, team_id) if runs else None
            view, content, details, dialogs = "review", "", "", ""
            preselect: datetime | None = None
            runnable = set(pages.runnable_weeks(calendar, now))
            if selected_job is not None:
                view = "job"
                failed_job = selected_job["status"] == "failed"
                content = pages.job_panel(
                    selected_job,
                    can_run=team_id in names,
                    runnable=pages.as_utc(selected_job["run_at"]) in runnable,
                    deletable=failed_job,
                )
                if failed_job:
                    preselect = pages.as_utc(selected_job["run_at"])
                    # Deleting is how a week runs again under changed model settings. A rerun
                    # of a stored week deletes its stored results too, so it gets their dialog.
                    job_run_id = str(selected_job["run_id"])
                    stored = db.query_one(
                        "SELECT 1 AS present FROM runs WHERE run_id=:id", {"id": job_run_id}
                    )
                    dialogs += (
                        pages.delete_dialog(run_row(db, job_run_id), token)
                        if stored
                        else pages.delete_failed_dialog(
                            selected_job, names.get(team_id) or team_id, token
                        )
                    )
            elif selected is None:
                view = "empty"
                content = pages.empty_team_panel(
                    enrolled, bool(pages.runnable_weeks(calendar, now))
                ) + pages.log_section(logs)
            else:
                selected_id = str(selected["run_id"])
                snapshot = summary.read_snapshot(selected.get("registry_entry_snapshot"))
                sources = [
                    source
                    for source, enabled in (
                        ("v1", bool(snapshot.v1_operators)),
                        ("v2", bool(snapshot.v2_operator)),
                    )
                    if enabled
                ]
                selected["sources_label"] = (
                    " + ".join(sources) + " sources" if sources else "Team sources"
                )
                analysis, open_analysis = "", False
                if selected["status"] == "completed":
                    worklist_tab = tab == "overview"
                    filters = summary.WorklistFilter(
                        state=state,
                        schema=schema,
                        rule=rule,
                        sort=sort,
                        page=page if worklist_tab else 1,
                        cursor=request.query_params.get("cursor") if worklist_tab else None,
                    )
                    loaded = summary.load_admin_summary(db, team_id, selected_id, filters)
                    analysis = summary.detailed_content(loaded)
                    open_analysis = summary.is_filtered(loaded)
                    findings_page = page if tab == "findings" else 1
                    rows, finding_total = queries.alerts_with_findings(
                        db, selected_id, findings_page
                    )
                    history = queries.decision_history(
                        db, [pages.identity(row) for row in rows], team_id
                    )
                    if tab == "findings" and finding:
                        match = next(
                            (
                                (row, item)
                                for row in rows
                                for item in findings_on(dict(row))
                                if pages.finding_key(row, item) == finding
                            ),
                            None,
                        )
                        if match is not None:
                            row, item = match
                            return HTMLResponse(
                                pages.finding_page(
                                    name,
                                    selected,
                                    row,
                                    item,
                                    history.get(pages.identity(row), []),
                                    token,
                                    findings_page,
                                    flash,
                                    failed,
                                )
                            )
                    totals = summary.totals_html(loaded)
                    if selected.get("publication_id"):
                        view = "published"
                        listing = pages.findings_list(
                            selected, rows, finding_total, findings_page, history, decisions=True
                        )
                        content = pages.published_panel(selected, totals, listing)
                        dialogs += pages.withdraw_dialog(selected, token)
                    elif tab == "publish":
                        view = "publish"
                        alerts, unassessed = summary.week_counts(loaded)
                        content = pages.publish_panel(
                            selected,
                            token,
                            alerts=alerts,
                            flagged=finding_total,
                            unassessed=unassessed,
                            replace=queries.other_publication(
                                db, team_id, selected["window_end"], selected_id
                            ),
                        )
                    else:
                        withdrawal = (
                            {
                                "at": loaded.publication.at,
                                "note": loaded.publication.note,
                            }
                            if loaded.publication.state == "withdrawn"
                            else None
                        )
                        listing = pages.findings_list(
                            selected, rows, finding_total, findings_page, history, decisions=False
                        )
                        content = pages.review_panel(
                            selected, totals, listing, finding_total, withdrawal=withdrawal
                        )
                elif selected["status"] == "failed":
                    view = "failed"
                    content = pages.failed_panel(
                        selected,
                        can_run=team_id in names,
                        runnable=pages.as_utc(selected["window_end"]) in runnable,
                    )
                    preselect = pages.as_utc(selected["window_end"])
                else:
                    view, content = "other", pages.other_status_panel(selected)
                details = pages.details_section(
                    selected,
                    queries.run_publications(db, selected_id),
                    logs,
                    analysis,
                    open_analysis=open_analysis,
                )
                dialogs += pages.delete_dialog(selected, token)
        backfill_active = any(
            week["status"] in ("queued", "pending", "running") for week in backfill_weeks
        )
        registered_team = team_id in names
        return HTMLResponse(
            pages.team_page(
                team_id,
                name,
                enrolled,
                runs,
                total,
                run_page,
                selected,
                content,
                flash,
                failed,
                view=view,
                job=selected_job,
                jobs=pages.jobs_panel(
                    jobs, shown=str(selected_job["job_id"]) if selected_job else None
                ),
                backfill=pages.backfill_panel(team_id, backfill_row, backfill_weeks, token)
                if backfill_row
                else "",
                new_runs=pages.new_runs_dialog(
                    team_id,
                    token,
                    name,
                    calendar,
                    now,
                    preselect=preselect,
                    backfill_active=backfill_active,
                )
                if registered_team
                else "",
                note=pages.retention_note(calendar, now, first_end) if registered_team else "",
                operators=pages.operators_panel(
                    team_id,
                    registry.teams,
                    registry.registry_version,
                    registry.registry.get("effective_date"),
                    selected,
                    summary.snapshot_operators(selected.get("registry_entry_snapshot"))
                    if selected
                    else None,
                ),
                details=details,
                dialogs=dialogs,
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
        team_id = _team_id_from_path(team_id)
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

    def deletion_page(db: Database, run_id: str) -> str:
        """Where a refused deletion returns: the stored run, or a never-stored run's failure."""
        run = db.query_one("SELECT team_id FROM runs WHERE run_id=:id", {"id": run_id})
        if run is not None:
            return team_path(str(run["team_id"]), run_id)
        job = db.query_one(
            "SELECT TOP 1 job_id, team_id FROM run_jobs WHERE run_id=:id "
            "ORDER BY submitted_at DESC, job_id DESC",
            {"id": run_id},
        )
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such run.")
        return pages.team_url(str(job["team_id"]), job_id=str(job["job_id"]))

    @app.post("/teams/{team_id}/trigger")
    async def trigger(request: Request, team_id: str) -> Response:
        """The New runs dialog: one week is a manual run, two to twelve weeks a backfill."""
        team_id = _team_id_from_path(team_id)
        values = await form_values(request)
        fields = {key: items[-1] for key, items in values.items()}
        if team_id not in {t for t, _, _ in registry_teams()}:
            raise HTTPException(404, "No such registered team.")
        weeks = [week for week in values.get("week", []) if week]
        if not weeks and fields.get("weeks_form"):
            return back(pages.team_url(team_id), "Pick at least one week to run.", True)
        if len(weeks) > 1:
            return await start_backfill(request, team_id, values)
        try:
            # A date is midnight UTC; RunRequest also rejects forged non-Monday times.
            body = RunRequest.model_validate(
                {
                    "team": team_id,
                    "run_at": weeks[0] if weeks else fields.get("run_at"),
                    "llm": fields.get("llm"),
                }
            )
        except ValidationError:
            return back(
                pages.team_url(team_id),
                "Choose a Monday at 00:00 UTC and a valid model mode.",
                True,
            )
        try:
            job = await run_in_threadpool(
                submit_job, api_settings, body, fields.get("submission_key", "")
            )
        except HTTPException as exc:
            return back(pages.team_url(team_id), str(exc.detail), True)
        except Exception as exc:
            log.error(
                "console.trigger_failed",
                "The run submission could not be saved",
                team_id=team_id,
                exc=exc,
            )
            return back(
                pages.team_url(team_id),
                "The run could not finish. Check the service logs and team history before retrying.",
                True,
            )
        log.info(
            "console.submitted",
            "Run submitted from the console; job is {status}",
            status=job["status"],
            team_id=team_id,
            run_id=job.get("run_id"),
            job_id=job["job_id"],
        )
        return back(
            pages.team_url(team_id, job_id=job["job_id"]),
            "Run accepted. It starts before the next queued week. You can leave this page."
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
            403: {"description": "Same-site CSRF token required"},
            409: {"description": "Another operation is active or the key has different options"},
        },
        openapi_extra={
            "parameters": [
                {
                    "name": "Idempotency-Key",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string", "minLength": 1, "maxLength": 64},
                    "description": "Exact ASCII submission key",
                },
                {
                    "name": "X-CSRF-Token",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string"},
                    "description": "Token from GET /csrf",
                },
            ]
        },
    )
    def create_job(request: Request, body: RunRequest) -> JSONResponse:
        row = submit_job(api_settings, body, request.headers.get("Idempotency-Key", ""))
        return JSONResponse(
            jsonable_encoder(job_status(row)),
            status_code=202,
            headers={"Location": f"/run-jobs/{row['job_id']}"},
        )

    @app.get(
        "/run-jobs/{job_id}",
        response_model=RunJobOut,
        summary="Read durable job state",
        responses={
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

    async def start_backfill(
        request: Request, team_id: str, values: dict[str, list[str]]
    ) -> Response:
        """Queue one run per picked week, oldest first (docs/backfill.md)."""
        path = pages.team_url(team_id)
        llm = (values.get("llm") or ["live"])[-1]
        if llm not in ("live", "fake", "off"):
            return back(path, "Choose a valid model mode.", True)
        try:
            weeks = parse_weeks(values.get("week", []))
            row = await run_in_threadpool(
                submit_backfill,
                api_settings,
                team_id,
                weeks,
                llm,  # type: ignore[arg-type]
                (values.get("submission_key") or [""])[-1],
            )
        except HTTPException as exc:
            return back(path, str(exc.detail), True)
        except Exception as exc:
            log.error(
                "console.backfill_failed",
                "The backfill submission could not be saved",
                team_id=team_id,
                exc=exc,
            )
            return back(path, "The runs could not be saved. Check the service logs.", True)
        log.info(
            "console.backfill_submitted",
            "Backfill of {weeks} weeks submitted from the console",
            weeks=row["week_count"],
            team_id=team_id,
            backfill_id=row["backfill_id"],
        )
        count = int(row["week_count"])
        return back(
            path,
            f"{count} {'run' if count == 1 else 'runs'} accepted. They run one at a time, "
            "oldest first. You can leave this page and return later.",
        )

    @app.post("/teams/{team_id}/backfill")
    async def backfill(request: Request, team_id: str) -> Response:
        """The former Backfill weeks form, kept for compatibility (2026-10-19)."""
        team_id = _team_id_from_path(team_id)
        values = await form_values(request)
        if team_id not in {t for t, _, _ in registry_teams()}:
            raise HTTPException(404, "No such registered team.")
        return await start_backfill(request, team_id, values)

    @app.get(
        "/backfills/{backfill_id}",
        response_model=BackfillOut,
        summary="Read backfill progress",
        responses={
            404: {"description": "Unknown backfill"},
        },
    )
    def read_backfill(backfill_id: str) -> JSONResponse:
        with database() as db:
            row = get_backfill(db, backfill_id)
            return JSONResponse(jsonable_encoder(backfill_out(row, backfill_jobs(db, backfill_id))))

    async def backfill_action(
        request: Request, backfill_id: str, act: Callable[[], tuple[dict[str, Any], str, bool]]
    ) -> Response:
        await form(request)
        with database() as db:
            team_id = str(get_backfill(db, backfill_id)["team_id"])
        path = pages.team_url(team_id)
        try:
            _, message, failed = await run_in_threadpool(act)
        except HTTPException as exc:
            return back(path, str(exc.detail), True)
        return back(path, message, failed, "")

    @app.post("/backfills/{backfill_id}/cancel")
    async def cancel_backfill_route(request: Request, backfill_id: str) -> Response:
        def act() -> tuple[dict[str, Any], str, bool]:
            with database() as db:
                row, count = cancel_backfill(db, backfill_id)
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
        def act() -> tuple[dict[str, Any], str, bool]:
            row, count = retry_backfill(api_settings, backfill_id)
            noun = "week" if count == 1 else "weeks"
            return row, f"Retry queued for {count} failed {noun}.", False

        return await backfill_action(request, backfill_id, act)

    @app.post("/backfills/{backfill_id}/dismiss")
    async def dismiss_backfill_route(request: Request, backfill_id: str) -> Response:
        def act() -> tuple[dict[str, Any], str, bool]:
            with database() as db:
                return dismiss_backfill(db, backfill_id), "Dismissed.", False

        return await backfill_action(request, backfill_id, act)

    @app.post("/run-jobs/{job_id}/cancel")
    async def cancel_week(request: Request, job_id: str) -> Response:
        with database() as db:
            backfill_id = job_backfill(db, job_id)

        def act() -> tuple[dict[str, Any], str, bool]:
            with database() as db:
                row, count = cancel_backfill(db, backfill_id, job_id)
            if not count:
                return row, "Only queued weeks can be removed.", True
            return row, "Week removed.", False

        return await backfill_action(request, backfill_id, act)

    @app.post("/run-jobs/{job_id}/retry")
    async def retry_week(request: Request, job_id: str) -> Response:
        with database() as db:
            backfill_id = job_backfill(db, job_id)

        def act() -> tuple[dict[str, Any], str, bool]:
            row, _ = retry_backfill(api_settings, backfill_id, job_id)
            return row, "Retry queued for this week.", False

        return await backfill_action(request, backfill_id, act)

    @app.post("/runs/{run_id}/publish")
    async def publish(request: Request, run_id: str) -> Response:
        fields = await form(request)

        def act() -> RedirectResponse:
            with database() as db:
                run = run_row(db, run_id)
                try:
                    result = publish_run(
                        db,
                        run_id,
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
            log.info(
                "admin.published",
                "Published the run as the week ending {week}{replaced}",
                week=f"{result.window_end:%Y-%m-%d}",
                replaced=f"; withdrew run {result.replaced_run_id[:16]}"
                if result.replaced_run_id
                else "",
                team_id=result.team_id,
                run_id=run_id,
            )
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

        def act() -> RedirectResponse:
            with database() as db:
                run = run_row(db, run_id)
                try:
                    unpublish_run(db, run_id, reason=fields.get("reason", ""))
                except PublicationRefused as exc:
                    return back(
                        team_path(str(run["team_id"]), run_id), f"Not withdrawn: {exc}", True
                    )
            log.info(
                "admin.withdrawn",
                "Withdrew the published week; readers no longer see it",
                team_id=run["team_id"],
                run_id=run_id,
            )
            return back(
                team_path(str(run["team_id"]), run_id),
                "Withdrawn. Readers no longer see this week.",
            )

        return await run_in_threadpool(act)

    @app.post("/runs/{run_id}/delete")
    async def delete(request: Request, run_id: str) -> Response:
        fields = await form(request)
        if fields.get("confirm_delete") != "1":
            raise HTTPException(400, "Confirm permanent deletion of this run.")

        def act() -> RedirectResponse:
            if not gate.acquire():
                raise HTTPException(409, "A run is active. Try deleting after it finishes.")
            try:
                # delete_run checks the active slot inside its own transaction; retain its
                # browser refusal message. The worker holds the shared lock for a whole job,
                # so without it delete_run still deletes another run beside that job.
                with ExitStack() as stack:
                    try:
                        stack.enter_context(
                            schedule_lock(
                                api_settings.config, settings.database, allow_manual_job=True
                            )
                        )
                        shared_lock = True
                    except WeeklyBusy:
                        shared_lock = False
                    with database() as db:
                        page = deletion_page(db, run_id)
                        try:
                            team_id = delete_run(db, run_id, shared_lock=shared_lock)
                        except DeletionRefused as exc:
                            return back(page, str(exc), True)
                    log.info(
                        "admin.deleted",
                        "Permanently deleted the run and its stored results",
                        run_id=run_id,
                        team_id=team_id,
                    )
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
                    )
                except DecisionRefused as exc:
                    return back(path, f"Not recorded: {exc}", True)
                except Exception as exc:  # pragma: no cover - database failure
                    log.error(
                        "admin.decision_failed",
                        "The decision could not be recorded",
                        run_id=run_id,
                        exc=exc,
                    )
                    return back(path, "Not recorded: the database refused it.", True)
            log.info(
                "admin.decided",
                "Recorded decision {state} on finding {finding}",
                state=fields.get("state", ""),
                finding=fields.get("finding", ""),
                run_id=run_id,
            )
            return back(path, "Decision recorded.")

        return await run_in_threadpool(act)

    app.include_router(runs_router)
    # Added last, so it is outermost: it also sees the guard's own refusals.
    app.add_middleware(RequestLogMiddleware)
    return app
