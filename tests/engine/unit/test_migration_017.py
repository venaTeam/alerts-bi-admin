"""Revision 017 cannot be downgraded: case-only team variants written after it would collide."""

from __future__ import annotations

import importlib.util

import pytest
from src.engine.db.ledger import MIGRATIONS_DIR


def test_017_refuses_downgrade() -> None:
    path = MIGRATIONS_DIR / "versions" / "017_case_sensitive_teams.py"
    spec = importlib.util.spec_from_file_location("revision_017", path)
    assert spec is not None and spec.loader is not None
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)

    assert revision.down_revision == "016_case_sensitive_key_fields"
    with pytest.raises(NotImplementedError, match="collide"):
        revision.downgrade()
