"""Analysis API adapters and a legacy wire-contract test factory.

Production HTTP requests enter the authenticated admin application.
"""

from .app import build_app
from .service import RunGate

__all__ = ["RunGate", "build_app"]
