"""The wire contract.

Declared as Pydantic models so validation happens before a handler runs and the OpenAPI
document is generated from these definitions rather than maintained beside them. The scope
rules of design section 7.8 are expressed here as types: ``team`` has no default, and only
the four approved output names exist.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

__all__ = [
    "CheckOut",
    "HealthOut",
    "LlmMode",
    "OutputFile",
    "RunOut",
    "RunRequest",
    "TeamOut",
    "TeamsOut",
    "VolumeOut",
]

#: How a run may treat the model. The names and their meanings are the CLI's: ``live``
#: matches the CLI's default (which still yields no model when LLM_ENABLED is false),
#: ``fake`` matches ``--fake-llm``, ``off`` matches ``--no-llm``.
LlmMode = Literal["live", "fake", "off"]

#: Only the four approved outputs are addressable. A type rather than a runtime check, so
#: an unknown name is refused by validation before any handler runs.
OutputFile = Literal["scorecard.html", "daily_metrics.csv", "rule_counts.csv", "alert_worklist.csv"]


class TeamOut(BaseModel):
    team_id: str
    display_name: str
    v1_operators: list[str]
    v2_operator: str | None
    panels: int


class TeamsOut(BaseModel):
    teams: list[TeamOut]


class CheckOut(BaseModel):
    ok: bool
    error: str | None = None
    database: str | None = None


class HealthOut(BaseModel):
    ok: bool
    version: str
    checks: dict[str, CheckOut]


class RunRequest(BaseModel):
    """The parameters of one run.

    ``team`` has no default on purpose: a run names one team, and a surface that filled it
    in would turn a scope rule into a convenience.
    """

    team: str = Field(min_length=1, description="registry team_id; a run never defaults")
    run_at: datetime | None = Field(
        default=None, description="freeze run_at (ISO 8601 UTC); defaults to now"
    )
    llm: LlmMode = Field(
        default="live", description="live: the on-prem model; fake: deterministic; off: skip"
    )


class VolumeOut(BaseModel):
    rows: int
    distinct: int


class RunOut(BaseModel):
    run_id: str
    team_id: str
    phase: str
    phase2_readiness_pct: float | None
    v1: VolumeOut
    v2: VolumeOut
    llm_assessed: bool
    llm_eligible: int
    out_dir: str
    scorecard: str
    files: dict[str, str]
