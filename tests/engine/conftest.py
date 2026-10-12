"""Shared pytest configuration.

The helpers package is imported by path so tests can say ``from tests.engine.helpers.rows import
v1_row`` without the test tree needing to be an installed distribution.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
