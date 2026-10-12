"""Request grouping and partitioning (design section 5.1; flow step 6.2).

Alerts are grouped by ``alert_rule_url`` where present and by ``application`` where it is
not. Each group is sent independently: groups are NEVER packed together to fill capacity,
even when they are small, so a request never mixes alert rules or applications.

The bet this rests on: showing a rule's instances together is context a reviewer would want
- the model can see whether a message is genuinely per-instance or one generic string
repeated across forty nodes. Design section 7.1 records that this is still unvalidated,
which is why grouping is isolated here and testable on its own.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Final

from alerts_bi_shared.hashing import sha256_of

from ..domain.normalize import AlertRecord

__all__ = [
    "MAX_BATCH_SIZE",
    "AlertGroup",
    "Batch",
    "alert_transport_id",
    "balanced_partition_sizes",
    "batch_id_of",
    "build_batches",
    "group_alerts",
]

#: Hard ceiling from design section 5.1. Configuration may lower it, never raise it.
MAX_BATCH_SIZE: Final = 200


@dataclass(frozen=True, slots=True)
class AlertGroup:
    type: str
    """``alert_rule_url`` or ``application``."""
    value: str
    alerts: tuple[AlertRecord, ...]


@dataclass(frozen=True, slots=True)
class Batch:
    batch_id: str
    group_type: str
    group_value: str
    partition_index: int
    partition_count: int
    alerts: tuple[AlertRecord, ...]
    alert_ids: tuple[str, ...]
    """Parallel to ``alerts``."""


def alert_transport_id(alert: AlertRecord) -> str:
    """Transport identifier for one alert.

    Explicitly NOT a second business identity: it is the SHA-256 of an unambiguous encoding
    of (schema, application, key_field). The verdict cache stays keyed on
    (application, key_field, prompt_version, model_version) as decided.
    """
    return sha256_of(
        {
            "schema": alert.schema,
            "application": alert.application,
            "key_field": alert.key_field,
        }
    )


def batch_id_of(
    run_id: str,
    group_type: str,
    group_value: str,
    partition_index: int,
    alert_ids: Sequence[str],
    prompt_version: str,
    model_version: str,
) -> str:
    """Deterministic batch identifier covering membership, ordering and versions."""
    return sha256_of(
        {
            "run_id": run_id,
            "group_type": group_type,
            "group_value": group_value,
            "partition_index": partition_index,
            "alert_ids": list(alert_ids),
            "prompt_version": prompt_version,
            "model_version": model_version,
        }
    )


def _is_usable(value: object) -> bool:
    return isinstance(value, str) and value.strip() != ""


def group_alerts(alerts: Sequence[AlertRecord]) -> list[AlertGroup]:
    """Group alerts by rule URL, falling back to application.

    API alerts do not carry an alert-rule URL at all, which is why the fallback exists and
    why R4 does not penalise them for the absence.

    Groups are ordered by (type, value) and alerts within a group by key_field, with
    application breaking the tie so the order is total even when two applications share a
    key_field value.
    """
    grouped: dict[tuple[str, str], list[AlertRecord]] = {}
    for alert in alerts:
        if _is_usable(alert.alert_rule_url):
            key = ("alert_rule_url", str(alert.alert_rule_url))
        else:
            key = ("application", alert.application)
        grouped.setdefault(key, []).append(alert)

    return [
        AlertGroup(
            type=group_type,
            value=group_value,
            alerts=tuple(sorted(members, key=lambda a: (a.key_field, a.application))),
        )
        for (group_type, group_value), members in sorted(grouped.items())
    ]


def balanced_partition_sizes(n: int, maximum: int) -> list[int]:
    """Split ``n`` into ``ceil(n / maximum)`` partitions differing by at most one.

    401 alerts become 134, 134 and 133 - never 200, 200 and 1. A tiny tail request would
    lose exactly the same-group context that batching exists to provide.
    """
    if n <= 0:
        return []
    count = -(-n // maximum)  # ceil division
    base, remainder = divmod(n, count)
    return [base + 1 if i < remainder else base for i in range(count)]


def build_batches(
    alerts: Sequence[AlertRecord],
    run_id: str,
    prompt_version: str,
    model_version: str,
    max_batch_size: int | None = None,
) -> list[Batch]:
    """Build the batches for one run."""
    cap = min(max_batch_size or MAX_BATCH_SIZE, MAX_BATCH_SIZE)
    batches: list[Batch] = []

    for group in group_alerts(alerts):
        sizes = balanced_partition_sizes(len(group.alerts), cap)
        offset = 0
        for partition_index, size in enumerate(sizes):
            members = group.alerts[offset : offset + size]
            offset += size
            alert_ids = tuple(alert_transport_id(alert) for alert in members)
            batches.append(
                Batch(
                    batch_id=batch_id_of(
                        run_id,
                        group.type,
                        group.value,
                        partition_index,
                        alert_ids,
                        prompt_version,
                        model_version,
                    ),
                    group_type=group.type,
                    group_value=group.value,
                    partition_index=partition_index,
                    partition_count=len(sizes),
                    alerts=members,
                    alert_ids=alert_ids,
                )
            )

    return batches


def reverse_for_evaluation(
    batch: Batch, run_id: str, prompt_version: str, model_version: str
) -> Batch:
    """Reverse a fixed partition only for a recorded evaluation trial."""
    ids = tuple(reversed(batch.alert_ids))
    return replace(
        batch,
        alerts=tuple(reversed(batch.alerts)),
        alert_ids=ids,
        batch_id=batch_id_of(
            run_id,
            batch.group_type,
            batch.group_value,
            batch.partition_index,
            ids,
            prompt_version,
            model_version,
        ),
    )
