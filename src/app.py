"""The operator admin app's factory (design section 7.12).

Every request needs a signed-in operator, taken from the login proxy's identity header. Every
write is a POST that must carry the anti-forgery token and come from this site; it calls the
same :mod:`alerts_bi_operations.review` functions the command line calls, under the operator's identity, and
then redirects back with a message.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import parse_qs, urlencode

from alerts_bi_operations.registry import RegistryError, load_registry
from alerts_bi_operations.report.render import build_run_outputs
from alerts_bi_operations.review.decisions import DecisionRefused, record_decision
from alerts_bi_operations.review.publication import PublicationRefused, publish_run, unpublish_run
from alerts_bi_runs.api.negotiation import wants_html
from alerts_bi_runs.api.routers.runs import router as runs_router
from alerts_bi_runs.api.schemas import HealthOut, RunRequest, TeamsOut
from alerts_bi_runs.api.service import RunGate, check_health, list_teams, start_run
from alerts_bi_shared.db.connection import Database, connect
from alerts_bi_shared.logging_setup import log, redact_error
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
from .readiness import check_schema

__all__ = ["SECURITY_HEADERS", "build_admin"]

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self' data:; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

#: The scorecard is self-contained with its own inline stylesheet and no script.
SCORECARD_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src data:; frame-ancestors 'none'"
)

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
    app = FastAPI(
        title="Alerts BI operator console",
        version=APP_VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    api_settings = settings.api_settings()
    gate = RunGate()
    app.state.settings = api_settings
    app.state.run_gate = gate

    @contextmanager
    def database() -> Iterator[Database]:
        with connect(settings.config.sql, settings.database) as db:
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
                and request.url.path.rstrip("/") == "/runs"
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
        if (
            request.url.path.startswith("/runs/")
            and response.headers.get("content-type", "").startswith("text/html")
            and (
                request.url.path.endswith(("/scorecard", "/scorecard.html"))
                or request.url.path.count("/") == 2
            )
        ):
            response.headers.setdefault("Content-Security-Policy", SCORECARD_CSP)
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
        if not same_site(request):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "Cross-site form submissions are refused."
            )
        body = await request.body()
        if len(body) > _MAX_FORM:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "The form is too large.")
        fields = {
            key: values[-1]
            for key, values in parse_qs(
                body.decode("utf-8", "replace"), keep_blank_values=True
            ).items()
        }
        if not valid_csrf(
            settings.secret, user_of(request), fields.get("csrf", ""), datetime.now(UTC).date()
        ):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "The form has expired or did not come from this app."
            )
        return fields

    def back(path: str, message: str, error: bool = False) -> RedirectResponse:
        key = "error" if error else "done"
        separator = "&" if "?" in path else "?"
        return RedirectResponse(
            f"{path}{separator}{urlencode({key: message})}#selected-run", status.HTTP_303_SEE_OTHER
        )

    def run_row(db: Database, run_id: str) -> dict[str, Any]:
        row = db.query_one(
            "SELECT r.*, p.publication_id, p.published_by, p.published_at, p.review_note, "
            "(SELECT COUNT(*) FROM weekly_review_log l WHERE l.run_id=r.run_id) AS scheduled, "
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
            from alerts_bi_runs.api.schemas import CheckOut

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
        with database() as db:
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
                        state=state, schema=schema, rule=rule, sort=sort, page=page
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
                team_id, run_id, state=state, schema=schema, rule=rule, sort=sort, page=page
            ),
            303,
        )

    @app.get("/runs/{run_id}/scorecard", response_class=HTMLResponse)
    def scorecard(run_id: str) -> HTMLResponse:
        with database() as db:
            run_row(db, run_id)
            html = build_run_outputs(db, run_id)["scorecard.html"]
        return HTMLResponse(html, headers={"Content-Security-Policy": SCORECARD_CSP})

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
            # datetime-local explicitly means UTC; the engine freezes the preceding 168 hours.
            body = RunRequest.model_validate(
                {"team": team_id, "run_at": fields.get("run_at"), "llm": fields.get("llm")}
            )
        except ValidationError:
            return back(
                pages.team_url(team_id), "Enter a valid UTC window end and model mode.", True
            )
        try:
            result = await run_in_threadpool(start_run, api_settings, gate, body)
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
        log.info("console.triggered", run_id=result.summary.run_id, operator=user_of(request))
        return back(
            pages.team_url(team_id, result.summary.run_id),
            "Run completed. Review it before publishing.",
        )

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
                        allow_gap=fields.get("allow_gap") == "1",
                    )
                except PublicationRefused as exc:
                    return back(
                        team_path(str(run["team_id"]), run_id), f"Not published: {exc}", True
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
