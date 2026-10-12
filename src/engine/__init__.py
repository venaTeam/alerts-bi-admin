"""Alerts BI — single-team weekly alert quality and migration scorecard.

Reports one selected team's alerting for one week: how much it fires, how much of that is
bad, how much of its own inventory it hides from its dashboards, and where it stands in the
migration to Appchi V2.

The tool reports numbers; people draw conclusions. Every figure is a statement about a
single week, with no comparison against a previous run.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
