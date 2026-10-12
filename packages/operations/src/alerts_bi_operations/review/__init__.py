"""Operator actions on stored runs (design section 7.10).

Two writes, both made from the trusted command line with the owning credential and never
from the reader portal:

* :mod:`~alerts_bi_operations.review.publication` - publishing a completed run as a team's weekly review,
  replacing a published week, and withdrawing one.
* :mod:`~alerts_bi_operations.review.decisions` - the append-only human-review record on a finding.

Neither changes anything the pipeline stored: a publication points at a run, and a decision
sits beside a finding without overwriting its ``quality_state`` or model verdict.
"""
