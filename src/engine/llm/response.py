"""Response validation (design section 5.1; flow step 6.3).

The contract is exact and CLOSED, and a failing response is never partially accepted. A
verdict attached to the wrong alert is the worst output this system can produce: it is a
false positive that also destroys the audit trail that would have caught it. So every check
below rejects the whole batch rather than dropping one verdict.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from alerts_bi_shared.catalogs import CITABLE_IDS

from ..domain.normalize import AlertRecord

__all__ = [
    "ASSESSMENTS",
    "CONFIDENCES",
    "MAX_JUSTIFICATION_LENGTH",
    "LlmResponseError",
    "Verdict",
    "response_json_schema",
    "state_for_verdict",
    "validate_response",
]

ASSESSMENTS: Final = ("no_violation", "catalog_violation", "other")
CONFIDENCES: Final = ("high", "medium", "low")
MAX_JUSTIFICATION_LENGTH: Final = 1000

_TOP_LEVEL_KEYS: Final = frozenset({"batch_id", "verdicts"})
_VERDICT_KEYS: Final = ("alert_id", "assessment", "principle_id", "confidence", "justification")


class LlmResponseError(Exception):
    """Any violation of the closed response contract. Always rejects the whole batch."""


@dataclass(frozen=True, slots=True)
class Verdict:
    alert_id: str
    assessment: str
    principle_id: str
    confidence: str
    justification: str


def _validate_verdict(raw: Any) -> Verdict:
    if not isinstance(raw, dict):
        raise LlmResponseError("a verdict is not an object")

    for key in raw:
        if key not in _VERDICT_KEYS:
            raise LlmResponseError(f'verdict carries unexpected field "{key}"')
    for key in _VERDICT_KEYS:
        if key not in raw:
            raise LlmResponseError(f'verdict is missing "{key}"')

    alert_id = raw["alert_id"]
    assessment = raw["assessment"]
    principle_id = raw["principle_id"]
    confidence = raw["confidence"]
    justification = raw["justification"]

    if not isinstance(alert_id, str) or alert_id == "":
        raise LlmResponseError("verdict alert_id is not a non-empty string")
    if not isinstance(assessment, str) or assessment not in ASSESSMENTS:
        raise LlmResponseError(f"verdict assessment {assessment!r} is not valid")
    if not isinstance(confidence, str) or confidence not in CONFIDENCES:
        raise LlmResponseError(f"verdict confidence {confidence!r} is not valid")
    if not isinstance(principle_id, str):
        raise LlmResponseError("verdict principle_id is not a string")
    if not isinstance(justification, str) or justification.strip() == "":
        raise LlmResponseError("verdict justification is empty")
    if len(justification) > MAX_JUSTIFICATION_LENGTH:
        raise LlmResponseError(
            f"verdict justification exceeds {MAX_JUSTIFICATION_LENGTH} characters"
        )

    # The assessment/principle pairing is closed. `other` is the escape hatch for a clear
    # violation absent from the catalogue - never for uncertainty - and it never counts
    # toward flagged.
    if assessment == "no_violation" and principle_id != "NONE":
        raise LlmResponseError("no_violation must carry principle_id NONE")
    if assessment == "other" and principle_id != "OTHER":
        raise LlmResponseError("other must carry principle_id OTHER")
    if assessment == "catalog_violation" and principle_id not in CITABLE_IDS:
        raise LlmResponseError(
            f"catalog_violation cites {principle_id!r}, which is not in the catalogue"
        )

    return Verdict(alert_id, assessment, principle_id, confidence, justification)


def validate_response(
    parsed: Any,
    batch_id: str,
    alert_ids: Sequence[str],
    *,
    alerts: Sequence[AlertRecord] | None = None,
) -> list[Verdict]:
    """Validate a parsed response against the request it answers.

    Returns verdicts in REQUEST order, so callers never depend on response ordering:
    verdict order is immaterial because ids provide the binding.
    """
    if not isinstance(parsed, dict):
        raise LlmResponseError("response is not a JSON object")

    for key in parsed:
        if key not in _TOP_LEVEL_KEYS:
            raise LlmResponseError(f'response carries unexpected top-level field "{key}"')
    if "batch_id" not in parsed:
        raise LlmResponseError("response is missing batch_id")
    if "verdicts" not in parsed:
        raise LlmResponseError("response is missing verdicts")

    if parsed["batch_id"] != batch_id:
        # Never trust a response that answers a different request.
        raise LlmResponseError("response batch_id does not match the request")
    if not isinstance(parsed["verdicts"], list):
        raise LlmResponseError("verdicts is not an array")

    by_alert_id: dict[str, Verdict] = {}
    for raw in parsed["verdicts"]:
        verdict = _validate_verdict(raw)
        if verdict.alert_id in by_alert_id:
            raise LlmResponseError(f"response contains a duplicate verdict for {verdict.alert_id}")
        by_alert_id[verdict.alert_id] = verdict

    expected = set(alert_ids)
    for alert_id in by_alert_id:
        if alert_id not in expected:
            raise LlmResponseError(f"response contains a verdict for unknown alert {alert_id}")
    for alert_id in expected:
        if alert_id not in by_alert_id:
            raise LlmResponseError(f"response is missing a verdict for alert {alert_id}")

    ordered = [by_alert_id[alert_id] for alert_id in alert_ids]
    if alerts is not None:
        if len(alerts) != len(alert_ids):
            raise ValueError("applicability context must match request membership")
        for verdict, alert in zip(ordered, alerts, strict=True):
            validate_applicability(verdict, alert)
    return ordered


def validate_applicability(verdict: Verdict, alert: AlertRecord) -> None:
    """Reject objective scope errors, without attempting to grade semantic evidence."""
    principle = verdict.principle_id
    invalid = (
        (principle in {"P7", "P8", "P9", "R8", "R9", "R10"} and alert.schema != "v2")
        or (principle == "P7" and alert.severity != "critical")
        or (principle == "R7" and alert.schema != "v1")
        or (principle == "R4" and (alert.provider or "").strip().lower() != "grafana")
    )
    if invalid:
        raise LlmResponseError(
            f"principle {principle} is not applicable to alert {verdict.alert_id}"
        )


def state_for_verdict(verdict: Verdict | Mapping[str, Any]) -> str:
    """Map a validated verdict onto the run-level identity state.

    A HIGH-confidence catalogue violation becomes ``llm_flagged``. Medium and low
    confidence, and every ``other``, go to review. ``no_violation`` becomes
    ``assessed_good``.

    Confidence is a three-value enum rather than a number on purpose: models are not
    calibrated well enough on a 0-1 scale to justify a numeric cutoff, and a float invites
    a threshold carrying more precision than the judgment underneath it has.
    """
    if isinstance(verdict, Verdict):
        assessment, confidence = verdict.assessment, verdict.confidence
    else:
        assessment, confidence = verdict["assessment"], verdict["confidence"]

    if assessment == "no_violation":
        return "assessed_good"
    if assessment == "other":
        return "needs_review"
    return "llm_flagged" if confidence == "high" else "needs_review"


def response_json_schema() -> dict[str, Any]:
    """The JSON schema sent to the model for strict structured output."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["batch_id", "verdicts"],
        "properties": {
            "batch_id": {"type": "string"},
            "verdicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": list(_VERDICT_KEYS),
                    "properties": {
                        "alert_id": {"type": "string"},
                        "assessment": {"type": "string", "enum": list(ASSESSMENTS)},
                        "principle_id": {
                            "type": "string",
                            "enum": ["NONE", "OTHER", *CITABLE_IDS],
                        },
                        "confidence": {"type": "string", "enum": list(CONFIDENCES)},
                        "justification": {
                            "type": "string",
                            "maxLength": MAX_JUSTIFICATION_LENGTH,
                        },
                    },
                },
            },
        },
    }
