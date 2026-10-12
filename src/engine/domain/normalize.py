"""Schema adapters mapping v1 (Appchi) and v2 (Appchi V2) documents onto one record.

Schema-specific fields are never erased, and the complete source document is always
retained: it is the representative payload sent to the model and the audit record stored
with a verdict. Elasticsearch row ids are deliberately not used as business identity,
because source documents expire after three months.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from alerts_bi_shared.hashing import sha256_of
from alerts_bi_shared.window import utc_date_key

from .severity import severity_label

__all__ = [
    "V1_FIELDS",
    "V2_FIELDS",
    "AlertRecord",
    "group_by_identity",
    "identity_of",
    "normalize_row",
    "select_representative",
    "split_identity",
]

#: Source fields carried by a v1 document, in a fixed order for stable serialization.
V1_FIELDS = (
    "application",
    "object",
    "message",
    "severity",
    "operator",
    "key_field",
    "time_created",
    "node_name",
    "network",
    "alert_rule_url",
    "provider",
    "@timestamp",
)

#: Source fields carried by a v2 document, in a fixed order for stable serialization.
V2_FIELDS = (
    "application",
    "component",
    "message",
    "severity",
    "status",
    "impact",
    "runbook_url",
    "environment",
    "site",
    "operator",
    "key_field",
    "time_created",
    "node_name",
    "network",
    "alert_rule_url",
    "provider",
    "@timestamp",
)


def identity_of(application: str, key_field: str) -> str:
    """Build the composite alert identity.

    ``application + key_field`` is the only alert identity in this design (sections 3.7,
    5.1). The parts are length-prefixed so no pair of values can produce a colliding key.

    This value is an in-process map key and is never persisted; findings store
    ``application`` and ``key_field`` as separate columns.
    """
    a = application or ""
    k = key_field or ""
    return f"{len(a)}:{a}|{len(k)}:{k}"


def split_identity(identity: str) -> tuple[str, str]:
    """Split an identity key back into its parts."""
    first_colon = identity.index(":")
    app_len = int(identity[:first_colon])
    application = identity[first_colon + 1 : first_colon + 1 + app_len]
    rest = identity[first_colon + 1 + app_len + 1 :]
    key_field = rest[rest.index(":") + 1 :]
    return application, key_field


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise TypeError(f"row has an unusable @timestamp: {value!r}")
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise TypeError(f"row has an unusable @timestamp: {value!r}") from exc
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class AlertRecord:
    schema: str
    application: str
    key_field: str
    identity: str
    component: str | None
    """``object`` in v1, ``component`` in v2."""
    message: str | None
    severity: str | None
    operator: str | None
    node_name: str | None
    network: str | None
    alert_rule_url: str | None
    provider: str | None
    status: str | None
    """v2 only."""
    impact: Any
    """v2 only; kept unnarrowed so R8 can observe a non-string."""
    runbook_url: Any
    """v2 only; kept unnarrowed for the same reason."""
    environment: str | None
    """v2 only."""
    site: str | None
    """v2 only."""
    time_created: str | None
    """Raw value as supplied."""
    timestamp: datetime
    """``@timestamp``, the receipt time."""
    snapshot_date: str
    """UTC calendar date of ``@timestamp``."""
    source: dict[str, Any] = field(repr=False)
    """Complete source document."""
    doc_hash: str
    """SHA-256 over the canonical encoding of ``source``."""


def normalize_row(
    schema: str, source: dict[str, Any], *, hash_document: bool = True
) -> AlertRecord:
    """Adapt one raw Elasticsearch ``_source`` document."""
    application = _str_or_none(source.get("application")) or ""
    key_field = _str_or_none(source.get("key_field")) or ""
    timestamp = _parse_timestamp(source.get("@timestamp"))
    is_v2 = schema == "v2"

    return AlertRecord(
        schema=schema,
        application=application,
        key_field=key_field,
        identity=identity_of(application, key_field),
        component=_str_or_none(source.get("component" if is_v2 else "object")),
        message=_str_or_none(source.get("message")),
        # Stored as a number; the schema decides which name that number carries.
        severity=severity_label(schema, source.get("severity")),
        operator=_str_or_none(source.get("operator")),
        node_name=_str_or_none(source.get("node_name")),
        network=_str_or_none(source.get("network")),
        alert_rule_url=_str_or_none(source.get("alert_rule_url")),
        provider=_str_or_none(source.get("provider")),
        status=_str_or_none(source.get("status")) if is_v2 else None,
        # impact and runbook_url stay unnarrowed: R8 and R9 must be able to observe a
        # non-string value, which stringifying would have hidden.
        impact=source.get("impact") if is_v2 else None,
        runbook_url=source.get("runbook_url") if is_v2 else None,
        environment=_str_or_none(source.get("environment")) if is_v2 else None,
        site=_str_or_none(source.get("site")) if is_v2 else None,
        time_created=_str_or_none(source.get("time_created")),
        timestamp=timestamp,
        snapshot_date=utc_date_key(timestamp),
        source=source,
        # The streaming path hashes only representative candidates tied at the latest
        # instant, and the final representative. Its transient rows never leave analysis.
        doc_hash=sha256_of(source) if hash_document else "",
    )


def select_representative(rows: Sequence[AlertRecord]) -> AlertRecord:
    """The most recent row for an identity within the run window.

    Design section 5.1 requires a *defined* representative, so two runs over the same data
    cannot pick different rows and reach different verdicts.

    Two rows can share the newest ``@timestamp`` - a v1 alert re-fired within the same
    second, or a re-index. The document hash breaks that tie, which keeps selection
    deterministic without depending on Elasticsearch row order or on ids that expire.
    """
    if not rows:
        raise ValueError("cannot select a representative from zero rows")
    best = rows[0]
    for row in rows[1:]:
        if row.timestamp > best.timestamp or (
            row.timestamp == best.timestamp and row.doc_hash < best.doc_hash
        ):
            best = row
    return best


def group_by_identity(rows: Iterable[AlertRecord]) -> dict[str, list[AlertRecord]]:
    """Group rows by identity, preserving input order within each group."""
    groups: dict[str, list[AlertRecord]] = {}
    for row in rows:
        groups.setdefault(row.identity, []).append(row)
    return groups
