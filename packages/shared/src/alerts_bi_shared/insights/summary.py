"""Assemble a :class:`TeamSummary` from :class:`SummaryInputs`."""

from __future__ import annotations

from alerts_bi_shared.insights.aggregate import biggest, by_application, fire_rows
from alerts_bi_shared.insights.estimate import estimate
from alerts_bi_shared.insights.findings import key_findings
from alerts_bi_shared.insights.model import SummaryInputs, TeamSummary


def summarize(inputs: SummaryInputs) -> TeamSummary:
    return TeamSummary(
        inputs=inputs,
        key_findings=key_findings(inputs),
        by_application=by_application(inputs.alerts),
        fire=fire_rows(inputs.alerts, inputs.window_end),
        biggest=biggest(inputs.alerts),
        estimate=estimate(inputs),
    )
