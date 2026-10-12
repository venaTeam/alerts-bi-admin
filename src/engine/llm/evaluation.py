"""Operator-only quality measurements. Never reads or writes production verdict caches.

Annotations are supplied independently of the classifier. Rates are descriptive: a draft
or synthetic corpus cannot establish production precision or authorize a release.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from random import Random
from statistics import mean
from typing import Any

from alerts_bi_shared.catalogs import CITABLE_IDS

from ..domain.normalize import AlertRecord
from .assess import AssessmentResult


@dataclass(frozen=True, slots=True)
class ReviewCase:
    case_id: str
    family: str
    split: str
    alert: AlertRecord
    principles: tuple[str, ...]
    confidences: tuple[str, ...]


def validate_corpus(
    manifest: dict[str, Any], documents: Sequence[dict[str, Any]]
) -> list[ReviewCase]:
    from ..domain.normalize import normalize_row

    if manifest.get("review_state") not in {"draft", "reviewed"}:
        raise ValueError("review_state must be draft or reviewed")
    if manifest["review_state"] == "reviewed" and not manifest.get("reviewers"):
        raise ValueError("reviewed labels require named reviewers")
    cases = []
    seen_ids: set[str] = set()
    seen_identity: set[str] = set()
    split_by_group: dict[str, str] = {}
    for row, document in zip(manifest["cases"], documents, strict=True):
        expected = row["expected"]
        if row["schema"] not in {"v1", "v2"} or row["split"] not in {"development", "holdout"}:
            raise ValueError("case schema or split is invalid")
        if not expected.get("rationale") or not expected.get("evidence_fields"):
            raise ValueError("each label needs evidence fields and an adjudication rationale")
        principles = tuple(expected["principles"])
        if not principles or not set(principles) <= {*CITABLE_IDS, "NONE", "OTHER"}:
            raise ValueError("expected principles must be catalogue IDs, NONE or OTHER")
        if "NONE" in principles and len(principles) != 1:
            raise ValueError("a case cannot simultaneously label good and violating")
        confidences = tuple(expected["confidences"])
        if not confidences or not set(confidences) <= {"high", "medium", "low"}:
            raise ValueError("expected confidences are invalid")
        alert = normalize_row(row["schema"], document)
        if row["case_id"] in seen_ids or alert.identity in seen_identity:
            raise ValueError("case IDs and alert identities must be unique")
        seen_ids.add(row["case_id"])
        seen_identity.add(alert.identity)
        group = (
            "url:" + str(alert.alert_rule_url)
            if alert.alert_rule_url
            else "app:" + alert.application
        )
        for key in ("family:" + row["family"], group):
            if key in split_by_group and split_by_group[key] != row["split"]:
                raise ValueError("rule/application groups and families must not cross splits")
            split_by_group[key] = row["split"]
        cases.append(
            ReviewCase(row["case_id"], row["family"], row["split"], alert, principles, confidences)
        )
    if not cases:
        raise ValueError("empty evaluation corpus")
    return cases


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _rates(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    high = [r for r in rows if r["high_catalogue"]]
    violations = [r for r in rows if r["expected_violation"]]
    return {
        "cases": len(rows),
        "high_catalogue_findings": len(high),
        "high_precision": _ratio(sum(r["citation_correct"] for r in high), len(high)),
        "known_violations": len(violations),
        "missed_as_good": sum(r["missed_as_good"] for r in violations),
        "missed_as_good_rate": _ratio(
            sum(r["missed_as_good"] for r in violations), len(violations)
        ),
        "unassessed": sum(r["state"] == "unassessed" for r in rows),
        "citation_accuracy": _ratio(sum(r["citation_correct"] for r in rows), len(rows)),
        "confidence_accuracy": _ratio(sum(r["confidence_correct"] for r in rows), len(rows)),
    }


def _precision_interval(groups: Sequence[list[dict[str, Any]]]) -> list[float] | None:
    """Descriptive cluster bootstrap: resample whole rule/application groups, not rows."""
    if len(groups) < 2 or not any(row["high_catalogue"] for group in groups for row in group):
        return None
    rng = Random(20260924)
    samples = []
    for _ in range(1000):
        flags = [row for _ in groups for row in rng.choice(groups) if row["high_catalogue"]]
        if flags:
            samples.append(sum(row["citation_correct"] for row in flags) / len(flags))
    samples.sort()
    return [samples[int((len(samples) - 1) * 0.025)], samples[int((len(samples) - 1) * 0.975)]]


def score_trial(cases: Sequence[ReviewCase], result: AssessmentResult) -> dict[str, Any]:
    """Keep missed violations and unassessed separate; no flags means undefined precision."""
    rows: list[dict[str, Any]] = []
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    slices: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        outcome = result.outcomes[case.alert.identity]
        row = {
            "case_id": case.case_id,
            "state": outcome.state,
            "principle_id": outcome.principle_id,
            "confidence": outcome.confidence,
            "expected_violation": case.principles != ("NONE",),
            "citation_correct": outcome.principle_id in case.principles,
            "confidence_correct": outcome.confidence in case.confidences,
            "high_catalogue": outcome.state == "llm_flagged",
            "missed_as_good": case.principles != ("NONE",) and outcome.state == "assessed_good",
        }
        rows.append(row)
        group = (
            "url:" + str(case.alert.alert_rule_url)
            if case.alert.alert_rule_url
            else "app:" + case.alert.application
        )
        groups[group].append(row)
        for key in (
            "schema:" + case.alert.schema,
            "provider:" + str(case.alert.provider),
            "severity:" + str(case.alert.severity),
            "principle:" + "|".join(case.principles),
        ):
            slices[key].append(row)
    group_rates = [_rates(members) for members in groups.values()]
    precisions = [r["high_precision"] for r in group_rates if r["high_precision"] is not None]
    attempts = result.batch_attempts
    usage: dict[str, int | None] = {}
    for key in ("input_tokens", "output_tokens", "cached_tokens"):
        values = [a["metadata"].get(key) for a in attempts]
        usage[key] = sum(values) if values and all(v is not None for v in values) else None
    durations = sorted(a["duration_ms"] for a in attempts)
    return {
        "metrics": _rates(rows),
        "group_count": len(groups),
        "high_precision_cluster_interval_95": _precision_interval(list(groups.values())),
        "uncertainty_note": "Descriptive group bootstrap; small/synthetic samples cannot establish deployment precision.",
        "group_weighted_precision": mean(precisions) if precisions else None,
        "groups_with_high_findings": len(precisions),
        "group_weighted_citation_accuracy": mean(r["citation_accuracy"] for r in group_rates),
        "slices": {key: _rates(value) for key, value in sorted(slices.items())},
        "cases": rows,
        "batches": result.requested_batches,
        "attempts": len(attempts),
        "failed_attempts": sum(a["status"] != "succeeded" for a in attempts),
        "max_actual_batch": max((a["alert_count"] for a in attempts), default=0),
        "request_bytes": sum(len(a["request_payload"].encode("utf-8")) for a in attempts),
        "latency_ms_p50": durations[(len(durations) - 1) // 2] if durations else None,
        "latency_ms_p95": durations[min(len(durations) - 1, int(len(durations) * 0.95))]
        if durations
        else None,
        "usage": usage,
        "explanation_quality": "requires blinded human review of SQL audit responses; not automatically scored",
    }


def compare_trials(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    before = {row["case_id"]: row for row in baseline["cases"]}
    after = {row["case_id"]: row for row in candidate["cases"]}
    if before.keys() != after.keys():
        raise ValueError("paired trials must assess exactly the same cases")
    return {
        "case_count": len(before),
        "changed_cases": [
            key
            for key in sorted(before)
            if any(
                before[key][field] != after[key][field]
                for field in ("state", "principle_id", "confidence")
            )
        ],
        "new_false_high": [
            key
            for key in sorted(before)
            if after[key]["high_catalogue"]
            and not after[key]["citation_correct"]
            and not (before[key]["high_catalogue"] and not before[key]["citation_correct"])
        ],
    }
