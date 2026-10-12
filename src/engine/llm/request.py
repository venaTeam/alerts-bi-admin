"""Request payload construction (design section 5.1; flow step 6.2).

The payload factors out only what the batch demonstrably shares. Grouping by
``alert_rule_url`` or ``application`` does NOT prove that ``component``, ``severity``,
``provider``, ``impact`` or ``runbook_url`` are identical, so a field moves into the group
header only when its value is equal across every alert in that batch.

This is lossless: every full document is reconstructable from header plus row. It is the
same content sent once instead of repeatedly, not an extraction that withholds context
from the model.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from alerts_bi_shared.hashing import canonical_encode, sha256_text

from .grouping import Batch

__all__ = [
    "assert_lossless",
    "build_request",
    "reconstruct_document",
    "serialize_request",
    "shared_field_names",
]


def shared_field_names(documents: Sequence[Mapping[str, Any]]) -> set[str]:
    """Compute the fields identical across every document in the batch.

    A key present in some documents and absent in others can never be shared:
    reconstructing would then invent the key on the documents that lacked it.
    """
    if not documents:
        return set()
    first, *rest = documents
    shared: set[str] = set()
    for key in first:
        encoded = canonical_encode(first[key])
        if all(key in doc and canonical_encode(doc[key]) == encoded for doc in rest):
            shared.add(key)
    return shared


def build_request(batch: Batch, ruleset_version: str, prompt_version: str) -> dict[str, Any]:
    """Build the logical request object for one batch.

    ``schema`` always stays on the alert envelope, never in ``shared_fields``, so a reader
    never has to consult the header to know which schema an alert belongs to.
    """
    documents = [alert.source for alert in batch.alerts]
    shared = shared_field_names(documents)

    shared_fields = {key: documents[0][key] for key in sorted(shared)}

    alerts = [
        {
            "alert_id": batch.alert_ids[index],
            "schema": alert.schema,
            "fields": {key: alert.source[key] for key in sorted(alert.source) if key not in shared},
        }
        for index, alert in enumerate(batch.alerts)
    ]

    return {
        "batch_id": batch.batch_id,
        "group": {"type": batch.group_type, "value": batch.group_value},
        "ruleset_version": ruleset_version,
        "prompt_version": prompt_version,
        "shared_fields": shared_fields,
        "alerts": alerts,
    }


def reconstruct_document(request: Mapping[str, Any], alert: Mapping[str, Any]) -> dict[str, Any]:
    """Reconstruct one alert's complete source document from header plus row."""
    return {**request["shared_fields"], **alert["fields"]}


def assert_lossless(request: Mapping[str, Any], batch: Batch) -> None:
    """Verify that every alert in the request reconstructs to its original document.

    Losslessness is a property that must be asserted, not assumed: the pipeline verifies it
    before the first attempt, so a factoring bug fails the batch rather than quietly
    sending the model a document with fields missing.
    """
    for index, alert in enumerate(request["alerts"]):
        rebuilt = reconstruct_document(request, alert)
        original = batch.alerts[index].source
        if canonical_encode(rebuilt) != canonical_encode(original):
            raise ValueError(
                f"factored payload is not lossless for alert {alert['alert_id']} "
                f"in batch {request['batch_id']}"
            )


def serialize_request(request: Mapping[str, Any]) -> tuple[str, str]:
    """Serialize the request exactly once, returning ``(text, sha256)``.

    The serialized text is persisted before the first attempt and reused BYTE-FOR-BYTE on
    retries: "retry the identical batch as a unit" is only meaningful if the bytes are the
    same, and re-serializing per attempt would leave that guarantee resting on key order.
    """
    text = json.dumps(request, separators=(",", ":"), ensure_ascii=False)
    return text, sha256_text(text)
