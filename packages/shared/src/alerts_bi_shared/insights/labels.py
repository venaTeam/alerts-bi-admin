"""Plain-language names for findings, for pages that never show a rule id.

The reader portal names a problem, never its catalogue id (product owner, 2026-10-04): a
reader sees "No rule link", not "R4". Every label here is short on purpose; the longer
explanations stay in :mod:`alerts_bi_shared.ui.explain`.

Pure text, no markup: the page layer escapes it.
"""

from __future__ import annotations

from typing import Final

from alerts_bi_shared.catalogs import R6_API_SPAM_PER_24H, R6_FLAP_CYCLES, R6_STUCK_OPEN

__all__ = [
    "ACTIONS",
    "FIXES",
    "PROBLEMS",
    "STATE_LABELS",
    "WHY",
    "action",
    "fix",
    "problem",
    "why",
]

#: A chip on an alert: what is wrong with it, in two or three words.
PROBLEMS: Final[dict[str, str]] = {
    "R1": "Generic message",
    "R2": "Heartbeat",
    "R3": "Placeholder name",
    "R4": "No rule link",
    "R5": "Hidden in panel",
    "R6": "Firing pattern",
    "R7": "Bad creation time",
    "R8": "No impact",
    "R9": "No runbook",
    "R10": "Impact is a cause",
}

#: A row of the "what to change" table: the change that fixes every alert with the problem.
ACTIONS: Final[dict[str, str]] = {
    "R1": "Rewrite generic messages",
    "R2": "Delete heartbeat alerts",
    "R3": "Replace placeholder names",
    "R4": "Add the Grafana rule link",
    "R5": "Stop hiding alerts in panels",
    "R6": "Fix stuck, spamming or flapping alerts",
    "R7": "Stamp the creation time correctly",
    "R8": "Add an impact",
    "R9": "Add a runbook",
    "R10": "Describe impact as what users see",
}

#: One sentence: what to do.
FIXES: Final[dict[str, str]] = {
    "R1": "Say what failed, where and by how much.",
    "R2": "Delete it. A status belongs in logs, not in alerts.",
    "R3": "Use the real application, component or operator name.",
    "R4": "Set alert_rule_url to the Grafana alert rule.",
    "R5": "Fix or delete the alert at its source, then remove the panel filter.",
    "R6": "Fix the condition so the alert clears once the problem is handled.",
    "R7": "Stamp time_created when the problem is detected, in UTC.",
    "R8": "Say what users experience while it fires.",
    "R9": "Add an https link to the runbook.",
    "R10": "Describe what users see, not the cause.",
}

#: One sentence: why it was flagged.
WHY: Final[dict[str, str]] = {
    "R1": "The message doesn't say what failed.",
    "R2": "It reports a status, not a problem.",
    "R3": "A name is empty or a placeholder.",
    "R4": "Responders can't open the Grafana rule that fired.",
    "R5": "Every panel you own filters it out.",
    "R6": "It fires in a stuck, spamming or flapping pattern.",
    "R7": "time_created is outside the 24 hours before it arrived.",
    "R8": "No impact is stated.",
    "R9": "There is no usable runbook link.",
    "R10": "The impact names a cause, not what users see.",
}

#: R6 is one rule with three patterns; each gets its own words.
_R6: Final[dict[str, tuple[str, str, str]]] = {
    # pattern: (problem, fix, why)
    "stuck": (
        "Stuck",
        "Make it clear once the problem is handled, or delete it.",
        f"It kept firing for {int(R6_STUCK_OPEN.total_seconds() // 3600)} hours or more "
        "without clearing.",
    ),
    "spamming": (
        "Spamming",
        "Send it once when it fires and once when it clears.",
        f"It sent {R6_API_SPAM_PER_24H} or more events a day.",
    ),
    "flapping": (
        "Flapping",
        "Add a pending period so it stops toggling.",
        f"It fired and cleared {R6_FLAP_CYCLES} or more times in a day.",
    ),
}

#: The outcome of the review of one alert.
STATE_LABELS: Final[dict[str, str]] = {
    "rule_flagged": "Flagged",
    "llm_flagged": "Advisory",
    "needs_review": "Needs decision",
    "assessed_good": "No issue",
    "unassessed": "Not reviewed",
}


def problem(rule_id: str, pattern: str | None = None, *, critical: bool = False) -> str:
    """The chip for one finding. R6 names its pattern; a missing runbook on a critical alert
    says so, because that one blocks phase 2."""
    if rule_id == "R6" and pattern in _R6:
        return _R6[pattern][0]
    if rule_id == "R9" and critical:
        return "Critical, no runbook"
    return PROBLEMS.get(rule_id, "Other problem")


def action(rule_id: str) -> str:
    return ACTIONS.get(rule_id, "Fix the problem")


def fix(rule_id: str, pattern: str | None = None) -> str:
    if rule_id == "R6" and pattern in _R6:
        return _R6[pattern][1]
    return FIXES.get(rule_id, "")


def why(rule_id: str, pattern: str | None = None, *, critical: bool = False) -> str:
    if rule_id == "R6" and pattern in _R6:
        return _R6[pattern][2]
    if rule_id == "R9" and critical:
        return "A critical alert has no usable runbook link. This blocks phase 2."
    return WHY.get(rule_id, "")
