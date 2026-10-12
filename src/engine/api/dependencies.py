"""Request-scoped access to the objects the routes need.

Both are created once per application and held on ``app.state``; these are the typed
accessors, so no route reaches into application state directly.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from ..config import ApiSettings
from .service import RunGate

__all__ = ["GateDep", "SettingsDep", "get_run_gate", "get_settings"]


def get_settings(request: Request) -> ApiSettings:
    settings: ApiSettings = request.app.state.settings
    return settings


def get_run_gate(request: Request) -> RunGate:
    gate: RunGate = request.app.state.run_gate
    return gate


SettingsDep = Annotated[ApiSettings, Depends(get_settings)]
GateDep = Annotated[RunGate, Depends(get_run_gate)]
