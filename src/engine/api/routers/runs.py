"""Starting a run and serving what it produced.

These handlers are transport only: they negotiate the response type and format it. The work
is in :mod:`alerts_bi_admin.engine.api.service`, which calls the same pipeline the command line calls.
"""

from __future__ import annotations

from typing import Annotated

from alerts_bi_operations.report.render import OUTPUT_FILES
from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from ..dependencies import GateDep, SettingsDep
from ..negotiation import wants_html
from ..schemas import OutputFile, RunOut, RunRequest, VolumeOut
from ..service import resolve_outputs, start_run

__all__ = ["router"]

router = APIRouter(prefix="/runs", tags=["runs"])

CSV_MEDIA_TYPE = "text/csv; charset=utf-8"

_LatestTeam = Annotated[str | None, Query(description='required when run_id is "latest"')]


@router.post(
    "",
    summary="Run one team and return its scorecard",
    response_description="The scorecard HTML, or a JSON summary for an API caller.",
    responses={
        200: {"content": {"text/html": {}, "application/json": {}}},
        400: {"description": "Unknown team, or a registry the run refused to use"},
        409: {"description": "A run is already in progress"},
    },
)
# A plain def, not async: a run is minutes of blocking work, so FastAPI runs it in the
# thread pool instead of stalling the event loop for every other request.
def create_run(
    request: Request, settings: SettingsDep, gate: GateDep, body: RunRequest
) -> Response:
    result = start_run(settings, gate, body)
    summary = result.summary
    headers = {
        "X-Alerts-BI-Run-Id": summary.run_id,
        "X-Alerts-BI-Team": summary.team_id,
        "X-Alerts-BI-Out-Dir": str(result.out_dir),
    }
    if wants_html(request):
        return HTMLResponse(result.outputs["scorecard.html"], headers=headers)
    return JSONResponse(
        RunOut(
            run_id=summary.run_id,
            team_id=summary.team_id,
            phase=summary.phase,
            phase2_readiness_pct=summary.readiness,
            v1=VolumeOut(rows=summary.v1_rows, distinct=summary.v1_identities),
            v2=VolumeOut(rows=summary.v2_rows, distinct=summary.v2_identities),
            llm_assessed=summary.llm_assessed,
            llm_eligible=summary.llm_eligible,
            out_dir=str(result.out_dir),
            scorecard=f"/runs/{summary.run_id}",
            files={name: f"/runs/{summary.run_id}/{name}" for name in OUTPUT_FILES},
        ).model_dump(),
        headers=headers,
    )


@router.get(
    "/{run_id}",
    response_class=HTMLResponse,
    summary="Re-render a stored run's scorecard from SQL",
)
def get_scorecard(settings: SettingsDep, run_id: str, team: _LatestTeam = None) -> HTMLResponse:
    resolved, outputs = resolve_outputs(settings, run_id, team)
    return HTMLResponse(outputs["scorecard.html"], headers={"X-Alerts-BI-Run-Id": resolved})


@router.get(
    "/{run_id}/{name}",
    summary="One of the four approved outputs, rendered from SQL",
    responses={200: {"content": {"text/html": {}, "text/csv": {}}}},
)
def get_output(
    settings: SettingsDep, run_id: str, name: OutputFile, team: _LatestTeam = None
) -> Response:
    resolved, outputs = resolve_outputs(settings, run_id, team)
    headers = {"X-Alerts-BI-Run-Id": resolved}
    if name.endswith(".html"):
        return HTMLResponse(outputs[name], headers=headers)
    headers["Content-Disposition"] = f'attachment; filename="{name}"'
    return Response(outputs[name], media_type=CSV_MEDIA_TYPE, headers=headers)
