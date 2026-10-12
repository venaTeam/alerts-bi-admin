"""Plain-language explanations of stored findings (design section 7.10).

Everything here turns a stored finding into sentences a team member can act on without
looking anything up: a short reason for the work list, why the rule matched (from the stored
evidence), what to do next, and - for an uncertain model finding - the decision a person has
to make.

Plain text only. The page layer escapes it; nothing here produces markup, so an alert value
quoted in an explanation can never become HTML.

The wording of each principle is taken from the pinned catalogue in
:mod:`alerts_bi_shared.catalogs`, so a reader sees the principle exactly as the model was given it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from alerts_bi_shared.catalogs import PRINCIPLE_CATALOG

__all__ = [
    "EN_DASH",
    "QUALITY_STATE_LABELS",
    "Evidence",
    "decision_question",
    "dominant_r6_pattern",
    "format_date",
    "format_instant",
    "format_week",
    "list_reason",
    "principle_next_step",
    "principle_title",
    "r6_next_step",
    "rule_explanation",
]

Evidence = Mapping[str, Any]

EN_DASH: Final = "\u2013"

QUALITY_STATE_LABELS: Final[dict[str, str]] = {
    "rule_flagged": "Rule finding",
    "llm_flagged": "Automated finding (advisory)",
    "needs_review": "Needs a decision",
    "assessed_good": "No issue found",
    "unassessed": "Not reviewed",
}


def _text(value: Any) -> str:
    return "" if value is None else str(value)


# ------------------------------------------------------------------ deterministic rules


_R6_COPY: Final[dict[str, tuple[str, str, str]]] = {
    # pattern: (title, next step, what the pattern means)
    "stuck": (
        "Stuck: kept firing with no clear for days",
        "Fix the condition or threshold so the alert clears once the problem is handled; "
        "silence or delete an alert nobody acts on.",
        "It kept firing without a clear for at least 72 hours, from its first to its last firing "
        "row in the week.",
    ),
    "spamming": (
        "Spamming: sent again while already firing",
        "Send each alert once when it fires and once when it clears; remove duplicate senders.",
        "It was sent again and again while it was already firing.",
    ),
    "flapping": (
        "Flapping: fires and clears over and over",
        'Add hysteresis or a pending period (a "for" duration) so the alert does not toggle '
        "between firing and clear.",
        "It fired and cleared at least three times inside 24 hours.",
    ),
    "neutral": (
        "Firing pattern: stuck, spamming or flapping",
        "Fix the condition behind the firing pattern so the alert fires only while there is a "
        "problem and clears once it is handled; each alert names its own pattern.",
        "Its firing pattern was stuck, spamming or flapping.",
    ),
}


#: R6's own priority order (design 7.14): an alert carries the first pattern it matches.
_R6_PRIORITY: Final = ("flapping", "spamming", "stuck")


def _r6_pattern(evidence: Evidence) -> str:
    pattern = _text(evidence.get("pattern"))
    return pattern if pattern in _R6_COPY else "neutral"


def r6_next_step(pattern: str | None) -> str:
    """The R6 next step for one firing pattern; the neutral step for none or an unknown one.

    Only spamming (API alerts, which send their own rows) is told to send once on fire and
    once on clear: a Grafana alert writes a row on every evaluation (design 1.1), so that
    advice cannot be followed for stuck or flapping alerts.
    """
    return _R6_COPY[pattern if pattern in _R6_COPY else "neutral"][1]


def dominant_r6_pattern(alerts: Iterable[tuple[str | None, int]]) -> str | None:
    """The firing pattern carried by the most alerts, from ``(fire_pattern, events)`` pairs.

    Ties go to the pattern with more events, then to R6's own priority order (flapping,
    spamming, stuck). ``None`` when no alert carries a pattern.
    """
    alerts_by: dict[str, int] = {}
    events_by: dict[str, int] = {}
    for pattern, events in alerts:
        if pattern not in _R6_PRIORITY:
            continue
        alerts_by[pattern] = alerts_by.get(pattern, 0) + 1
        events_by[pattern] = events_by.get(pattern, 0) + events
    if not alerts_by:
        return None
    return min(alerts_by, key=lambda p: (-alerts_by[p], -events_by[p], _R6_PRIORITY.index(p)))


def _r6_rate(evidence: Evidence) -> str:
    """The API rate that justifies spamming, e.g. ``30 events per 24 h over 40 h``."""
    events = evidence.get("events_per_24h")
    if not isinstance(events, int | float) or evidence.get("span_hours") is None:
        return ""
    span = evidence.get("span_hours")
    shown = f"{span:g}" if isinstance(span, int | float) else _text(span)
    return f"{events:g} events per 24 h over {shown} h"


def _r6_why(evidence: Evidence) -> str:
    pattern = _r6_pattern(evidence)
    if evidence.get("rows") is None:
        return _R6_COPY[pattern][2]
    open_hours = evidence.get("open_hours")
    open_part = (
        ""
        if open_hours is None
        else (f" Its firing events since the last clear span {_text(open_hours)} hours.")
    )
    rate = _r6_rate(evidence) if pattern == "spamming" else ""
    open_part += f" {rate[0].upper()}{rate[1:]}." if rate else ""
    return (
        f"{_R6_COPY[pattern][2]} Events: {_text(evidence.get('rows'))}, "
        f"{_text(evidence.get('max_episode_firing_rows'))} in the longest firing episode, "
        f"{_text(evidence.get('clear_count'))} clears, "
        f"{_text(evidence.get('max_clear_cycles_24h'))} fire-and-clear cycles in the busiest "
        f"24 hours.{open_part}"
    )


def _r6_observed(evidence: Evidence) -> str:
    open_hours = evidence.get("open_hours")
    return (
        f"{_r6_pattern(evidence)} \u00b7 {_text(evidence.get('rows'))} events \u00b7 "
        f"{_text(evidence.get('max_episode_firing_rows'))} in one episode"
        + (f" \u00b7 open {_text(open_hours)} h" if open_hours is not None else "")
        + (
            f" \u00b7 {_r6_rate(evidence)}"
            if _r6_pattern(evidence) == "spamming" and _r6_rate(evidence)
            else ""
        )
    )


@dataclass(frozen=True, slots=True)
class RuleText:
    title: str
    reason: Callable[[Evidence], str]
    why: Callable[[Evidence], str]
    observed: Callable[[Evidence], str]
    next_step: str
    readiness: bool = False


def _r3_violations(evidence: Evidence) -> str:
    parts = []
    for violation in evidence.get("violations") or []:
        field = _text(violation.get("field"))
        if violation.get("reason") == "empty":
            parts.append(f"{field} is empty")
        else:
            parts.append(f'{field} is the placeholder "{_text(violation.get("normalized"))}"')
    return "; ".join(parts) or "an identity field is missing or a placeholder"


def _r7_why(evidence: Evidence) -> str:
    reason = evidence.get("reason")
    rule = (
        "time_created must fall between 24 hours before the alert was received and the moment "
        "it was received, both ends allowed."
    )
    if reason == "missing":
        return f"{rule} This firing had no time_created at all."
    if reason == "unparseable":
        return f"{rule} This firing's time_created is not a readable timestamp."
    if reason == "future":
        return f"{rule} This firing was stamped later than it was received."
    return f"{rule} This firing was stamped more than 24 hours before it was received."


def _r7_observed(evidence: Evidence) -> str:
    created = _text(evidence.get("time_created")) or "(none)"
    received = _text(evidence.get("timestamp"))
    return f"time_created {created}" + (f" · received {received}" if received else "")


def _r9_reason(evidence: Evidence) -> str:
    if evidence.get("blocks_completion"):
        return "No usable runbook on a critical alert: blocks phase 2"
    return "No usable runbook"


def _r9_why(evidence: Evidence) -> str:
    what = {
        "missing": "missing",
        "empty": "empty",
        "placeholder": f'the placeholder "{_text(evidence.get("normalized"))}"',
        "not_a_string": "not text",
        "not_absolute_http": "not an absolute http(s) link",
    }.get(_text(evidence.get("reason")), "not usable")
    severity = _text(evidence.get("severity")) or "unknown"
    effect = (
        "Because the alert is critical, this blocks phase-2 completion."
        if evidence.get("blocks_completion")
        else "At this severity it is shown but does not block phase-2 completion."
    )
    return f"runbook_url is {what}. Severity is {severity}. {effect}"


def _r8_why(evidence: Evidence) -> str:
    what = {
        "missing": "missing",
        "empty": "empty",
        "placeholder": f'the placeholder "{_text(evidence.get("normalized"))}"',
        "not_a_string": "not text",
    }.get(_text(evidence.get("reason")), "not usable")
    return f"impact is {what}. Checked on the latest firing."


RULE_TEXT: Final[dict[str, RuleText]] = {
    "R1": RuleText(
        title="Generic message",
        reason=lambda _: "Generic message: doesn't say what failed",
        why=lambda e: (
            "After trimming, lower-casing and collapsing spaces, the whole message is exactly "
            f'"{_text(e.get("normalized"))}", which is on the list of generic phrases. A '
            "message that merely contains the phrase never matches."
        ),
        observed=lambda e: _text(e.get("normalized")),
        next_step=(
            "Rewrite the message to say what failed, where, and by how much - for example "
            '"Dispatch queue depth above 10000 for 10m on notif-node-2".'
        ),
    ),
    "R2": RuleText(
        title="Heartbeat or status message",
        reason=lambda _: "Reports a status, not a problem",
        why=lambda e: (
            f'The whole normalized message is exactly "{_text(e.get("normalized"))}", which is '
            "on the list of heartbeat and status phrases. There is nothing for on-call to do "
            "when it fires."
        ),
        observed=lambda e: _text(e.get("normalized")),
        next_step=(
            'Delete this alert. "I am alive" and "completed" events belong in logs or a '
            "liveness probe, not in the alert pipeline."
        ),
    ),
    "R3": RuleText(
        title="Placeholder or missing identity field",
        reason=lambda _: "Placeholder or missing identity field",
        why=lambda e: (
            "Application, operator and component must name something real; node name is "
            f"optional but must not be a placeholder when supplied. Here: {_r3_violations(e)}."
        ),
        observed=_r3_violations,
        next_step="Replace the placeholder with the real application, component or operator name.",
    ),
    "R4": RuleText(
        title="Grafana alert without a rule link",
        reason=lambda _: "Grafana alert has no link to its rule",
        why=lambda _: (
            "The alert came from Grafana (provider = grafana) and has no alert_rule_url. "
            "Alerts sent through the API are never checked for this."
        ),
        observed=lambda e: (
            f"provider: {_text(e.get('provider'))} · alert_rule_url: "
            f"{_text(e.get('alert_rule_url')) or 'empty'}"
        ),
        next_step="Set alert_rule_url to the Grafana alert rule, so a responder can open the rule that fired.",
    ),
    "R5": RuleText(
        title="Hidden by your own dashboard",
        reason=lambda _: "Your own dashboard filters it out",
        why=lambda e: (
            "Every dashboard panel supplied for this schema ("
            + (", ".join(_text(p) for p in e.get("panels") or []) or "none named")
            + ") filters this alert out. On-call never sees it, but it still fires into the "
            "pipeline."
        ),
        observed=lambda e: "filtered out by "
        + (", ".join(_text(p) for p in e.get("panels") or [])),
        next_step=(
            "Fix or delete the alert at its source instead of hiding it, then remove the filter "
            "from the panel."
        ),
    ),
    "R6": RuleText(
        title=_R6_COPY["neutral"][0],
        reason=lambda e: _R6_COPY[_r6_pattern(e)][0],
        why=_r6_why,
        observed=_r6_observed,
        next_step=_R6_COPY["neutral"][1],
    ),
    "R7": RuleText(
        title="Creation time out of range",
        reason=lambda _: "Creation time outside the allowed 24 hours",
        why=_r7_why,
        observed=_r7_observed,
        next_step=(
            "Stamp time_created with the moment the condition was detected, in UTC. Appchi V2 "
            "stamps this field for you."
        ),
    ),
    "R8": RuleText(
        title="No impact",
        reason=lambda _: "No impact stated",
        why=_r8_why,
        observed=lambda e: f"impact: {_text(e.get('reason'))}",
        next_step=(
            "Add an impact that says what users or the business experience while this fires - "
            'for example "Users do not receive SMS login codes and cannot sign in".'
        ),
        readiness=True,
    ),
    "R9": RuleText(
        title="No usable runbook",
        reason=_r9_reason,
        why=_r9_why,
        observed=lambda e: f"runbook_url: {_text(e.get('reason'))} · severity {_text(e.get('severity'))}",
        next_step="Add runbook_url: an absolute https link to the runbook for this alert.",
        readiness=True,
    ),
    "R10": RuleText(
        title="Impact describes the cause, not the effect",
        reason=lambda _: "Impact describes the cause, not the effect",
        why=lambda e: (
            f'The whole normalized impact is exactly "{_text(e.get("normalized"))}": a technical '
            "cause rather than what users experience."
        ),
        observed=lambda e: _text(e.get("normalized")),
        next_step='Describe the effect instead - for example "checkout pages take over 5 s to load".',
        readiness=True,
    ),
}


@dataclass(frozen=True, slots=True)
class RuleExplanation:
    rule_id: str
    title: str
    reason: str
    why: str
    observed: str
    next_step: str
    readiness: bool


def rule_explanation(rule_id: str, evidence: Evidence | None) -> RuleExplanation:
    """Explain one deterministic finding from its stored sample evidence."""
    sample: Evidence = evidence or {}
    text = RULE_TEXT.get(rule_id)
    if text is None:
        return RuleExplanation(
            rule_id, rule_id, f"Rule {rule_id}", f"Rule {rule_id} matched.", "", "", False
        )
    title, next_step = text.title, text.next_step
    if rule_id == "R6":
        title, next_step = _R6_COPY[_r6_pattern(sample)][:2]
    return RuleExplanation(
        rule_id=rule_id,
        title=title,
        reason=text.reason(sample),
        why=text.why(sample),
        observed=text.observed(sample),
        next_step=next_step,
        readiness=text.readiness,
    )


def list_reason(rule_id: str, evidence: Evidence | None) -> str:
    return rule_explanation(rule_id, evidence).reason


# ------------------------------------------------------------------ model principles

_PRINCIPLES: Final = {principle.id: principle.text for principle in PRINCIPLE_CATALOG}

_PRINCIPLE_STEPS: Final[dict[str, str]] = {
    "P1": "Make the alert lead to an action, or delete it if nobody needs to act.",
    "P2": "If nothing needs doing when it fires, turn it into a log line or a dashboard metric.",
    "P3": "Say which operation failed and what users experience, not only that something failed.",
    "P4": "Use the real application and component names a responder can search for.",
    "P5": "Say where it fired: environment, site or cluster.",
    "P6": "Base the alert on a measurable signal: latency, traffic, errors or saturation.",
    "P7": "Lower the severity, or make sure it is urgent, damaging now, and has a runbook.",
    "P8": "Set the severity from the impact: the worse the effect on users, the higher the severity.",
    "P9": "Rewrite the impact as what users or the business experience.",
    "P10": "Automate the response instead of paging a person.",
    "P11": "Alert on the user-visible symptom, and keep the internal cause in the message.",
    "OTHER": "Read the reasoning below and fix what it describes.",
}

_DECISION_QUESTIONS: Final[dict[str, str]] = {
    "P1": (
        "Does anyone have to act when this fires? If not, confirm the finding and remove the "
        "alert. If there is a real action, dismiss it and note what the action is."
    ),
    "P2": (
        "Does this report a problem, or only that something happened? If it only reports an "
        "event, confirm: it belongs in a log. If on-call acts on it, dismiss and note why."
    ),
    "P3": (
        "Can a responder tell what failed from this message? If not, confirm. If the failure is "
        "clear, dismiss."
    ),
    "P4": (
        "Do the application and component names identify something a responder can find? "
        "Confirm if not; dismiss if they do."
    ),
    "P5": (
        "Can a responder tell where this fired - environment, site or cluster? Confirm if not; "
        "dismiss if the location is clear."
    ),
    "P6": (
        "Is this based on latency, traffic, errors or saturation? Confirm if it is not grounded "
        "in a measurable signal; dismiss if it is."
    ),
    "P7": (
        "Does this critical alert pass the Wake-Up Test - urgent, damaging now, with a runbook? "
        "Confirm if it should be a lower severity; dismiss if critical is right."
    ),
    "P8": (
        "Does the severity match the stated impact? Confirm if they disagree; dismiss if they "
        "are consistent."
    ),
    "P9": (
        "Does the impact say what users experience, or only the technical cause? Confirm if it "
        "is only the cause; dismiss if it states the effect."
    ),
    "P10": (
        "Is the response mechanical enough to automate? Confirm if a script could handle it; "
        "dismiss if it needs human judgment."
    ),
    "P11": (
        "Is there any user-visible symptom behind this internal cause? Confirm if none is "
        "stated; dismiss if the effect on users is clear."
    ),
    "OTHER": (
        "The automated review saw a problem that is not in the standard's catalogue. Read its "
        "reasoning and decide whether it is a real problem: confirm it, or dismiss it with a note."
    ),
}


def principle_title(principle_id: str) -> str:
    if principle_id in _PRINCIPLES:
        return _PRINCIPLES[principle_id]
    if principle_id in RULE_TEXT:
        return RULE_TEXT[principle_id].title
    if principle_id == "OTHER":
        return "Outside the catalogue"
    return principle_id


def principle_next_step(principle_id: str) -> str:
    if principle_id in _PRINCIPLE_STEPS:
        return _PRINCIPLE_STEPS[principle_id]
    if principle_id in RULE_TEXT:
        return RULE_TEXT[principle_id].next_step
    return _PRINCIPLE_STEPS["OTHER"]


def decision_question(principle_id: str) -> str:
    """The specific decision a person makes on a ``needs_review`` finding."""
    if principle_id in _DECISION_QUESTIONS:
        return _DECISION_QUESTIONS[principle_id]
    title = principle_title(principle_id)
    return (
        f'Decide whether this alert breaks the standard\'s rule "{title}": confirm the finding, '
        "or dismiss it with a note saying why it does not apply."
    )


# ------------------------------------------------------------------ dates


def format_instant(value: Any) -> str:
    """``30 Aug 2026, 16:44 UTC`` - the one way the portal shows a moment."""
    if not isinstance(value, datetime):
        return _text(value)
    moment = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return f"{moment.day} {moment:%b %Y, %H:%M} UTC"


def format_date(value: Any) -> str:
    """``24 Sep 2026`` - a UTC calendar date."""
    if not isinstance(value, datetime):
        return _text(value)
    moment = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return f"{moment.day} {moment:%b %Y}"


def format_week(start: datetime, end: datetime) -> str:
    """``23 Aug - 30 Aug 2026`` (with an en dash) for a published week."""
    return f"{start.day} {start:%b} {EN_DASH} {end.day} {end:%b %Y}"
