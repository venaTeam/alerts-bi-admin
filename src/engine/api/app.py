"""The application factory.

Assembles the surface: state, exception handlers, routes. Nothing here knows how a run
works - that is :mod:`alerts_bi_admin.engine.api.service` - and nothing here formats a page, which is
:mod:`alerts_bi_admin.engine.api.ui`.
"""

from __future__ import annotations

from alerts_bi_shared.versions import APP_VERSION
from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse

from ..config import ApiSettings
from .negotiation import wants_html
from .routers import meta, runs
from .service import RunGate
from .ui import error_page

__all__ = ["build_app"]

DESCRIPTION = (
    "Starts one team's weekly alert-quality run and serves its scorecard. A wrapper over "
    "the same pipeline the command line drives; it performs no analysis of its own, and "
    "reports are rendered only from committed SQL rows."
)


def build_app(settings: ApiSettings) -> FastAPI:
    app = FastAPI(title="Alerts BI", version=APP_VERSION, description=DESCRIPTION)
    app.state.settings = settings
    app.state.run_gate = RunGate()

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError) -> Response:
        """Answer a browser in HTML and a script in JSON.

        The run form posts from the index page, so a validation failure has to be readable
        without a JSON viewer.
        """
        code = status.HTTP_422_UNPROCESSABLE_CONTENT
        if not wants_html(request):
            return JSONResponse({"detail": jsonable_encoder(exc.errors())}, status_code=code)
        detail = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'][1:])}: {error['msg']}"
            for error in exc.errors()
        )
        return HTMLResponse(error_page(code, detail), status_code=code)

    @app.exception_handler(HTTPException)
    async def _http_handler(request: Request, exc: HTTPException) -> Response:
        if wants_html(request):
            return HTMLResponse(
                error_page(exc.status_code, str(exc.detail)), status_code=exc.status_code
            )
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    app.include_router(meta.router)
    app.include_router(runs.router)
    return app
