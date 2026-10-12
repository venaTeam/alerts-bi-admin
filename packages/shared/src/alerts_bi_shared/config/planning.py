"""Planning defaults for the time-to-v2 estimate (team summary spec section 7.2).

A setting, not a measurement: the effort figure it produces is labelled "configured, not
measured" wherever it is shown. A team overrides it with ``planning.v1_rule_effort_days`` in
its registry entry, which is versioned with the registry.
"""

from __future__ import annotations

from typing import Final

#: Working days to rebuild one v1 alert rule in the v2 schema and re-decide its severity
#: against the Wake-Up Test (phase 1). Impact and runbook are phase 2 and not included.
DEFAULT_V1_RULE_EFFORT_DAYS: Final = 0.5

#: Working days in a working week, for showing effort in weeks.
WORKING_DAYS_PER_WEEK: Final = 5
