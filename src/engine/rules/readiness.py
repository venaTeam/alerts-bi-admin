"""V2 phase-2 readiness gaps R8-R10 (design section 4; flow step 4).

These are a readiness number, not a quality number. They stay out of ``flagged``, and they
never withhold an identity from the LLM: a missing ``impact`` is a phase-2 gap, but the
alert must still be assessed for core problems such as being informational.

Folding them into ``flagged`` before ``impact`` and ``runbook_url`` are mandatory would
make a team that migrated correctly appear to regress (design section 3.6).
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from alerts_bi_shared.catalogs import PLACEHOLDER_VALUES, R10_TECHNICAL_CAUSE_IMPACTS

from ..domain.normalize import AlertRecord
from .core import Finding
from .text import normalize_field_value, normalize_message

__all__ = [
    "evaluate_r8",
    "evaluate_r9",
    "evaluate_r10",
    "evaluate_readiness_rules",
    "is_absolute_http_url",
    "is_completion_ready",
    "phase2_readiness_pct",
]

_SCHEME = re.compile(r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*):(?P<rest>.*)$", re.DOTALL)


def is_absolute_http_url(value: str) -> bool:
    """Is a value a valid absolute HTTP(S) URL naming a host?

    Deliberately implements the WHATWG URL parser's tolerance for special schemes, where
    any number of slashes after ``http:`` is accepted, so ``http:runbook`` and
    ``https:/single-slash`` resolve to a host. Python's ``urlsplit`` rejects both.

    Matching the WHATWG behaviour is intentional: the superseded JavaScript implementation
    used ``new URL()``, and diverging here would silently change which alerts carry an R9
    gap - and R9 on a ``critical`` alert blocks phase-2 completion.

    The protocol is checked explicitly because a ``mailto:`` or ``file:`` runbook is not
    something an on-call engineer can open from the alert.
    """
    match = _SCHEME.match(value)
    if match is None:
        return False
    if match.group("scheme").lower() not in ("http", "https"):
        return False

    rest = match.group("rest").lstrip("/")
    # The authority ends at the first path, query or fragment delimiter.
    authority = re.split(r"[/?#]", rest, maxsplit=1)[0]
    # Strip any userinfo, then any port; what remains must be a non-empty host.
    host = authority.rpartition("@")[2]
    host = host.rpartition(":")[0] if ":" in host else host
    return host != ""


def evaluate_r8(row: AlertRecord) -> Finding | None:
    """R8 - missing or unusable ``impact``.

    A present but poor impact such as ``high cpu`` is NOT R8; that is R10's job, or the
    LLM's under principle P9.
    """
    if row.schema != "v2":
        return None
    impact = row.impact

    if impact is None:
        return Finding("R8", "v2_readiness", {"reason": "missing"})
    if not isinstance(impact, str):
        return Finding(
            "R8", "v2_readiness", {"reason": "not_a_string", "type": type(impact).__name__}
        )
    if impact.strip() == "":
        return Finding("R8", "v2_readiness", {"reason": "empty"})
    normalized = normalize_field_value(impact)
    if normalized is not None and normalized in PLACEHOLDER_VALUES:
        return Finding("R8", "v2_readiness", {"reason": "placeholder", "normalized": normalized})
    return None


def evaluate_r9(row: AlertRecord) -> Finding | None:
    """R9 - missing or invalid absolute HTTP(S) ``runbook_url``.

    Reported as a readiness gap for every severity. Severity only changes what it means
    for phase completion: a ``critical`` match is a mandatory completion failure, while
    ``high`` and ``warning`` stay visible without blocking completion under the current
    100%-on-critical criterion.
    """
    if row.schema != "v2":
        return None
    url = row.runbook_url
    severity = normalize_field_value(row.severity)
    base = {"severity": row.severity, "blocks_completion": severity == "critical"}

    if url is None:
        return Finding("R9", "v2_readiness", {**base, "reason": "missing"})
    if not isinstance(url, str):
        return Finding(
            "R9", "v2_readiness", {**base, "reason": "not_a_string", "type": type(url).__name__}
        )
    if url.strip() == "":
        return Finding("R9", "v2_readiness", {**base, "reason": "empty"})
    normalized = normalize_field_value(url)
    if normalized is not None and normalized in PLACEHOLDER_VALUES:
        return Finding(
            "R9", "v2_readiness", {**base, "reason": "placeholder", "normalized": normalized}
        )
    if not is_absolute_http_url(url.strip()):
        return Finding("R9", "v2_readiness", {**base, "reason": "not_absolute_http"})
    return None


def evaluate_r10(row: AlertRecord) -> Finding | None:
    """R10 - ``impact`` exactly matches the technical-cause catalogue.

    Deliberately narrow, and deliberately duplicated by LLM principle P9: R10 is the regex
    proxy, P9 is the judgment. ``high cpu causes checkout latency`` does not match and
    continues to the model.
    """
    if row.schema != "v2" or not isinstance(row.impact, str):
        return None
    normalized = normalize_message(row.impact)
    if normalized is None or normalized not in R10_TECHNICAL_CAUSE_IMPACTS:
        return None
    return Finding("R10", "v2_readiness", {"field": "impact", "normalized": normalized})


def evaluate_readiness_rules(row: AlertRecord) -> list[Finding]:
    """Evaluate every v2 readiness rule for one row."""
    findings: list[Finding] = []
    for rule in (evaluate_r8, evaluate_r9, evaluate_r10):
        finding = rule(row)
        if finding is not None:
            findings.append(finding)
    return findings


def is_completion_ready(representative: AlertRecord) -> bool:
    """Is a v2 identity ready for phase-2 completion?

    Evaluated on the identity's most recent representative row. Ready means no R8 gap, no
    R10 match, and - only when ``severity = critical`` - no R9 gap. A missing runbook on
    ``high`` or ``warning`` stays visible in ``phase2_gaps`` and on the work list but does
    not reduce the completion percentage.
    """
    if representative.schema != "v2":
        raise TypeError("phase-2 readiness is only defined for v2 identities")
    if evaluate_r8(representative) is not None:
        return False
    if evaluate_r10(representative) is not None:
        return False
    severity = normalize_field_value(representative.severity)
    return not (severity == "critical" and evaluate_r9(representative) is not None)


def phase2_readiness_pct(v2_representatives: Sequence[AlertRecord]) -> float | None:
    """``phase2_readiness_pct`` - completion-ready v2 identities over all v2 identities.

    ``None`` when there are no v2 identities: zero percent would assert that a team failed
    a measurement that was never taken.
    """
    if not v2_representatives:
        return None
    ready = sum(1 for row in v2_representatives if is_completion_ready(row))
    return (ready / len(v2_representatives)) * 100
