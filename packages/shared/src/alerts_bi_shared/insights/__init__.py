"""Pure building blocks for the team summary (spec docs/superpowers/specs/2026-10-01-team-summary-design.md).

No I/O and no SQL: the reader portal and the admin app each turn their own rows into a
:class:`SummaryInputs`, and everything here turns that into what both pages show. The
portal may import this package; it imports nothing the portal is forbidden to reach.
"""

from alerts_bi_shared.insights.model import (
    AlertRow,
    AppRow,
    DailyPoint,
    Estimate,
    FireRow,
    KeyFinding,
    RuleTotal,
    SchemaTotals,
    SummaryInputs,
    TeamSummary,
    WeekRules,
)

__all__ = [
    "AlertRow",
    "AppRow",
    "DailyPoint",
    "Estimate",
    "FireRow",
    "KeyFinding",
    "RuleTotal",
    "SchemaTotals",
    "SummaryInputs",
    "TeamSummary",
    "WeekRules",
]
