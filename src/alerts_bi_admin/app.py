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
from urllib.parse import parse_qs, quote, urlencode

from alerts_bi_operations.registry import RegistryError, load_registry
from alerts_bi_operations.report.render import build_run_outputs
from alerts_bi_operations.review.decisions import DecisionRefused, record_decision
from alerts_bi_operations.review.publication import PublicationRefused, publish_run, unpublish_run
from alerts_bi_shared.db.connection import Database, connect
from alerts_bi_shared.logging_setup import log, redact_error
from alerts_bi_shared.versions import APP_VERSION
from fastapi import FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from alerts_bi_admin import pages, queries, summary
from alerts_bi_admin.auth import csrf_token, identity, same_site, valid_csrf
from alerts_bi_admin.config import AdminSettings
from alerts_bi_admin.readiness import check_schema

__all__ = ["SECURITY_HEADERS", "build_admin"]

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self'; img-src 'self' data:; form-action 'self'; "
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


def build_admin(settings: AdminSettings) -> FastAPI:
    app = FastAPI(
        title="Alerts BI admin",
        version=APP_VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

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
                "Sign in through the admin Route.", status_code=status.HTTP_401_UNAUTHORIZED
            )
        elif request.method not in ("GET", "HEAD", "POST"):
            response = PlainTextResponse(
                "Method not allowed.", status_code=status.HTTP_405_METHOD_NOT_ALLOWED
            )
        else:
            request.state.user = user
            response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException) -> Response:
        user = getattr(request.state, "user", "") or ""
        return HTMLResponse(
            pages.error_page(exc.status_code, str(exc.detail), user), exc.status_code
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
        return RedirectResponse(f"{path}?{urlencode({key: message})}", status.HTTP_303_SEE_OTHER)

    def run_row(db: Database, run_id: str) -> dict[str, Any]:
        row = db.query_one(
            "SELECT run_id, team_id, team_display_name, window_start, window_end, status "
            "FROM runs WHERE run_id = :run_id",
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

    @app.get("/healthz")
    def healthz() -> PlainTextResponse:
        try:
            with database() as db:
                check_schema(db)
        except Exception as exc:
            log.error("admin.database_unavailable", error=redact_error(exc))
            return PlainTextResponse("The operator data is unavailable right now.", status_code=503)
        return PlainTextResponse("ok")

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, done: Message = None, error: Message = None) -> HTMLResponse:
        teams = registry_teams()
        with database() as db:
            overview = queries.team_overview(db)
        return HTMLResponse(
            pages.dashboard_page(user_of(request), teams, overview, error or done, bool(error))
        )

    @app.get("/teams/{team_id}", response_class=HTMLResponse)
    def team(
        request: Request, team_id: str, done: Message = None, error: Message = None
    ) -> HTMLResponse:
        names = {team_id_: name for team_id_, name, _ in registry_teams()}
        with database() as db:
            runs = queries.team_runs(db, team_id)
            log_rows = queries.team_log(db, team_id)
        if team_id not in names and not runs:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such team.")
        name = names.get(team_id) or str(runs[0]["team_id"])
        return HTMLResponse(
            pages.team_page(
                user_of(request),
                team_id,
                name,
                runs,
                log_rows,
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
    ) -> HTMLResponse:
        filters = summary.WorklistFilter(
            state=state, schema=schema, rule=rule, sort=sort, page=page
        )
        with database() as db:
            try:
                loaded = summary.load_admin_summary(db, team_id, run_id, filters)
            except summary.SummaryNotFound as exc:
                raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None
        return HTMLResponse(
            summary.summary_page(user_of(request), loaded, shared=summary.shared_sections(loaded))
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
    ) -> HTMLResponse:
        with database() as db:
            run = run_row(db, run_id)
            rows, total = queries.alerts_with_findings(db, run_id, page)
            identities = [
                (str(r["alert_schema"]), str(r["application"]), str(r["key_field"])) for r in rows
            ]
            history = queries.decision_history(db, identities)
        return HTMLResponse(
            pages.findings_page(
                user_of(request),
                run,
                rows,
                total,
                page,
                queries.PAGE_SIZE,
                history,
                token_for(request),
                error or done,
                bool(error),
            )
        )

    def team_path(team_id: str) -> str:
        return "/teams/" + quote(team_id, safe="")

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
                    return back(team_path(str(run["team_id"])), f"Not published: {exc}", True)
            log.info("admin.published", run_id=run_id, operator=user)
            message = "Published."
            if result.replaced_run_id:
                message += (
                    f" The earlier publication of run {result.replaced_run_id[:16]} was withdrawn."
                )
            return back(team_path(str(run["team_id"])), message)

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
                    return back(team_path(str(run["team_id"])), f"Not withdrawn: {exc}", True)
            log.info("admin.withdrawn", run_id=run_id, operator=user)
            return back(
                team_path(str(run["team_id"])), "Withdrawn. Readers no longer see this week."
            )

        return await run_in_threadpool(act)

    @app.post("/runs/{run_id}/decide")
    async def decide(request: Request, run_id: str) -> Response:
        fields = await form(request)
        user = user_of(request)
        path = f"/runs/{quote(run_id, safe='')}/findings"

        def act() -> RedirectResponse:
            with database() as db:
                run_row(db, run_id)
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

    return app
