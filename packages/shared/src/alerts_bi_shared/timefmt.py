"""One canonical instant format for the whole pipeline.

Every instant that leaves the process - a hashed run or batch identifier, an Elasticsearch
range bound, a CSV cell, a scorecard field, a stored evidence timestamp - is written in this
one shape: UTC, always three fractional digits, always a trailing ``Z``::

    2026-08-25T18:00:00.000Z

The fixed three digits matter beyond tidiness. ``run_id`` and ``batch_id`` are SHA-256
digests over documents that contain these strings, so a formatter that dropped ``.000`` for a
whole second and kept it otherwise would give the same run two different identities depending
on where its clock happened to land.
"""

from __future__ import annotations

from datetime import UTC, datetime

__all__ = ["iso_date", "iso_instant"]


def iso_instant(value: datetime) -> str:
    """Format an instant as UTC with milliseconds.

    Naive values are read as UTC: every datetime inside the pipeline is UTC by construction,
    and the SQL Server driver hands back naive values for DATETIME2 columns.
    """
    moment = value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def iso_date(value: datetime) -> str:
    """The UTC calendar date of an instant, ``YYYY-MM-DD``."""
    return iso_instant(value)[:10]
