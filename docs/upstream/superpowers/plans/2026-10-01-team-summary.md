# Team Summary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A team Summary page in the operator admin app and a Summary section in the reader
portal, plus R6 (stuck/spamming/flapping) as a core rule, the `unseen` visibility measure,
work-list filtering and a time-to-retire-v1 estimate.

**Architecture:** Pure summary logic lives in `src/insights` (no I/O). Each app turns its
own SQL rows into `SummaryInputs` and renders `TeamSummary` with server-side HTML and SVG.
Shared widgets live in `src/portal/summary_view.py`, which the admin app already may import.
R6 is evaluated per identity in `src/rules`. `unseen` is evaluated alongside suppression in
`src/suppression`.

**Tech Stack:** Python 3.12+, FastAPI, SQLAlchemy Core over `mssql+pymssql`, Alembic with
the `.sql` ledger, pytest, ruff, mypy (strict).

**Spec:** `docs/superpowers/specs/2026-10-01-team-summary-design.md`. Read it in full, after
`docs/alerts_bi_design.md` (mandatory, in full) and `AGENTS.md`.

**Already on the branch** (`feature/team-summary`):

| Commit | Contents |
|---|---|
| `24b0db0` | Migration 005, the repository columns, and the new fields on `EvaluatedIdentity`, `SuppressionResult` and `TeamEntry`, plus `src/config/planning.py` |
| `d000f6c` | `src/insights/model.py` data shapes and `src/domain/cadence.py` `REPEAT_INTERVAL` |
| `659fe7b` | The `src/insights` function stubs that Task D implements |

## Global Constraints

- **No JavaScript and no inline `style=` attributes**, in either app. Both CSPs are
  `style-src 'self'` with no `script-src`. Use CSS classes in `src/portal/assets.py` (portal),
  `ADMIN_CSS` in `src/admin/pages.py` (admin), and SVG geometry attributes (`x`, `width`,
  `height`, `points`) for bars and charts.
- **Portal copy and markup** never contain the substrings `per day`, `run_id`, `registry`,
  `ruleset`, `prompt` or `model version` (case-sensitive, as tests check). The portal never
  shows run ids, versions, run timing or hashes, and reads only `portal_*` views. It never
  imports `src.api`, `src.run`, `src.es`, `src.llm`, `src.review`, `src.report` or `src.cli`.
- **v1 and v2 are never added together**, anywhere.
- **The estimate** is computed at request time from **published weeks only**, never stored,
  never in the scorecard or the three CSVs.
- **R6 thresholds:**
  - flapping: `max_clear_cycles_24h >= 3`;
  - spamming: Grafana ratio `>= 2.0`, non-Grafana `events_per_24h >= 24` with an active span
    of at least 6 h;
  - stuck: Grafana, ratio `>= 0.9`, active span `>= 72 h`, `clear_count == 0`;
  - priority flapping → spamming → stuck;
  - active span = `last - first + REPEAT_INTERVAL[schema]`, where v1 = 5 min and v2 = 12 h.
- `RULESET_VERSION` becomes `1.1.0`. **`PROMPT_VERSION` stays `1.2.0`; do not edit
  `src/llm/prompt.py`.**
- **Effort default:** 0.5 working days per v1 rule (`src/config/planning.py`).
- **Every hashed or ordered output stays deterministic.** Sort explicitly.
- `test/fixtures/expected-results.json` is edited **by hand only**, never generated.
- **Before each commit:** `uv run ruff format`, `uv run ruff check src tests scripts` and
  `uv run mypy src` must be clean.
- **Commit messages** end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Lane rules (every implementer)

1. You work in your own git worktree. Run `cp /Users/yarin/GitProjects/alerts-bi/.env .` and
   then `uv sync` first.
2. Touch only the files your task owns. If you need a change elsewhere, stop and report it.
3. **Shared SQL Server and Elasticsearch:** run `tests/integration`, any `alerts-bi run`, and
   any generator run **only while holding the lock**:

   ```bash
   L=/private/tmp/claude-501/-Users-yarin-GitProjects-alerts-bi/6cd7fcb1-52db-4b43-b4b5-4aa34bf4f803/scratchpad/db.lock
   until mkdir "$L" 2>/dev/null; do sleep 10; done
   # ... integration commands ...
   rmdir "$L"
   ```

   Always release the lock, even after a failure. Never hold it for more than 15 minutes.
4. Report back with: assumptions, files changed, commands run with results, and commit hashes.

## Review Focus

1. **An identity with one row**, or rows that share one timestamp: the span is exactly one
   interval and must not divide by zero. Task A has a test.
2. **A team with no panels, or with a panel for only one schema:** `unseen` is `NULL` there,
   never 0, and every page shows "no dashboard supplied" rather than 0. Tasks B, C and E have
   tests.
3. **A week with no v1 alerts:** rules left is 0. The estimate says phase 1 has no v1 rules
   left, and effort is 0. Task D has a test.
4. **A publication gap or a basis change right before the selected week:** the lookback has 0
   earlier weeks, so "No estimate" says why. Task D has a test.
5. **Alert text containing HTML or a forbidden portal substring** (a message saying "per day"):
   it must be escaped, and the portal's forbidden-substring test must keep testing *our* copy,
   not alert data. Task E keeps the existing surface tests and checks copy constants.

---

### Task A: R6 in the pipeline

**Files:**
- Create: `src/rules/firing.py`, `tests/unit/test_rules_firing.py`
- Modify:
  - `src/rules/catalogs.py`: add `"R6"` to `CORE_RULE_IDS`, plus the threshold constants;
  - `src/rules/engine.py`: `evaluate_rows` sets the facts and attaches `R6`;
  - check `attach_row_findings` keeps the new identity fields;
  - `src/versions.py`: `RULESET_VERSION = "1.1.0"` and its docstring;
  - `src/portal/explain.py`: `RULE_TEXT["R6"]`;
  - `tests/unit/test_portal_explain.py`: `SAMPLES["R6"]`;
  - `tests/unit/test_rules_core.py`, if it asserts R6 is absent from the engine.

**Interfaces:**
- Consumes: `REPEAT_INTERVAL` (`src/domain/cadence.py`); `EvaluatedIdentity.clear_count`,
  `.max_clear_cycles_24h` and `.fire_pattern` (they already exist with defaults);
  `AlertRecord.severity` (`"clear"` for v1 code 1), `.status` (v2), `.provider`, `.timestamp`
  and `.doc_hash`.
- Produces:
  - `firing.is_clear(row: AlertRecord) -> bool`;
  - `firing.FiringFacts` (a frozen dataclass): `clear_count: int`,
    `max_clear_cycles_24h: int`, `span: timedelta`, `ratio: float | None`,
    `events_per_24h: float`, `pattern: str | None`;
  - `firing.firing_facts(schema: str, rows: Sequence[AlertRecord]) -> FiringFacts`;
  - in the pipeline: identities carry the facts, and every row of a matching identity has a
    core `Finding("R6", "core", evidence)`.

- [ ] **Step 1: Write the failing tests** in `tests/unit/test_rules_firing.py`. Build
  `AlertRecord`s with the helper used in `tests/unit/test_rules_core.py` (reuse its factory).
  Cover each case; each is one `def test_...`:

  | Case | Expect |
  |---|---|
  | v1 Grafana, 1 row | span = 5 min, ratio 1.0, pattern None (no ZeroDivisionError) |
  | v1 Grafana, 3 rows with the same timestamp | span 5 min, ratio 3.0 → spamming |
  | v1 Grafana, one row every 5 min from t0 to t0+72h | span 72h05m, ratio 1.0 → stuck |
  | Same rows ending at t0+71h55m | span 72h → stuck (≥ 72 h is inclusive) |
  | Ending at t0+71h | span 71h05m → None |
  | v1 Grafana, stuck shape with one row severity `clear` | `clear_count` 1, `max_clear_cycles_24h` 1, not stuck |
  | Grafana ratio 1.99 (575 rows over span 24h05m = 289 intervals) | not spamming |
  | Grafana ratio 2.0 (578 rows over the same span) | spamming |
  | Grafana ratio 0.89 over 80h | not stuck |
  | Grafana ratio 0.9 over 80h | stuck |
  | v1 API, 24 rows evenly over 23h55m (span 24h) | spamming |
  | v1 API, 23 rows over the same span | None |
  | v1 API, 30 rows over 5h | None (span < 6 h) |
  | v1, 3 fire→clear cycles with all 3 clear rows inside 23h | flapping |
  | v1, 3 cycles with the clear rows at t, t+12h, t+25h | `max_clear_cycles_24h` 2 → not flapping |
  | v1, two consecutive clear rows | count 1 cycle |
  | v2, 3 cycles using `status="resolved"` | flapping |
  | Flapping and spamming at once | flapping wins (priority) |
  | Rows passed in reverse order, ties on timestamp | facts identical (sorted by `(timestamp, doc_hash)`) |

  The rolling 24 h window is half-open `[t, t + 24h)` over the clear rows' timestamps.

- [ ] **Step 2: Run** `uv run pytest tests/unit/test_rules_firing.py -q`. Expected: FAIL
  (module missing).

- [ ] **Step 3: Implement `src/rules/firing.py`:**

```python
"""R6: one alert's firing pattern against its schema's repeat interval (team summary spec 5)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta

from src.domain.cadence import REPEAT_INTERVAL
from src.domain.normalize import AlertRecord
from src.rules.catalogs import (
    R6_API_MIN_SPAN,
    R6_API_SPAM_PER_24H,
    R6_FLAP_CYCLES,
    R6_FLAP_WINDOW,
    R6_SPAM_RATIO,
    R6_STUCK_MIN_SPAN,
    R6_STUCK_RATIO,
)

_DAY = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class FiringFacts:
    clear_count: int
    max_clear_cycles_24h: int
    span: timedelta
    ratio: float | None
    events_per_24h: float
    pattern: str | None


def is_clear(row: AlertRecord) -> bool:
    """v1 clears with severity ``clear`` (code 1); v2 with ``status = resolved``."""
    if row.schema == "v1":
        return row.severity == "clear"
    return row.status == "resolved"


def firing_facts(schema: str, rows: Sequence[AlertRecord]) -> FiringFacts:
    ordered = sorted(rows, key=lambda r: (r.timestamp, r.doc_hash))
    clears = [is_clear(r) for r in ordered]
    cycle_times = [
        ordered[i].timestamp for i in range(1, len(ordered)) if clears[i] and not clears[i - 1]
    ]
    max_cycles = 0
    start = 0
    for end, t in enumerate(cycle_times):
        while t - cycle_times[start] >= R6_FLAP_WINDOW:
            start += 1
        max_cycles = max(max_cycles, end - start + 1)
    interval = REPEAT_INTERVAL[schema]
    span = ordered[-1].timestamp - ordered[0].timestamp + interval
    n = len(ordered)
    grafana = ordered[-1].provider == "grafana"
    ratio = n / (span / interval) if grafana else None
    per_24h = n / (span / _DAY)
    clear_count = sum(clears)
    pattern: str | None = None
    if max_cycles >= R6_FLAP_CYCLES:
        pattern = "flapping"
    elif (ratio is not None and ratio >= R6_SPAM_RATIO) or (
        not grafana and per_24h >= R6_API_SPAM_PER_24H and span >= R6_API_MIN_SPAN
    ):
        pattern = "spamming"
    elif (
        ratio is not None
        and ratio >= R6_STUCK_RATIO
        and span >= R6_STUCK_MIN_SPAN
        and clear_count == 0
    ):
        pattern = "stuck"
    return FiringFacts(clear_count, max_cycles, span, ratio, per_24h, pattern)
```

  In `src/rules/catalogs.py`:
  - Add the constants: `R6_FLAP_CYCLES = 3`, `R6_FLAP_WINDOW = timedelta(hours=24)`,
    `R6_SPAM_RATIO = 2.0`, `R6_API_SPAM_PER_24H = 24.0`, `R6_API_MIN_SPAN = timedelta(hours=6)`,
    `R6_STUCK_RATIO = 0.9`, `R6_STUCK_MIN_SPAN = timedelta(hours=72)`, each `Final` and
    commented with the spec section.
  - Change `CORE_RULE_IDS` to `("R1", "R2", "R3", "R4", "R5", "R6", "R7")`.
  - Update the comment about R6 being post-MVP.

  Adjust the floating-point boundary only if a test of 2.0 or 0.9 fails because of float
  error. Prefer comparing `n * interval >= RATIO * span` with integer-exact `timedelta`
  arithmetic.

- [ ] **Step 4: Wire it into `evaluate_rows`** (`src/rules/engine.py`). After `group` is
  known, and before `core_rule_ids` is computed:

```python
        facts = firing_facts(representative.schema, [item.row for item in group])
        if facts.pattern is not None:
            evidence = {
                "pattern": facts.pattern,
                "rows": len(group),
                "span_hours": round(facts.span.total_seconds() / 3600, 2),
                "ratio": None if facts.ratio is None else round(facts.ratio, 3),
                "events_per_24h": round(facts.events_per_24h, 2),
                "clear_count": facts.clear_count,
                "max_clear_cycles_24h": facts.max_clear_cycles_24h,
            }
            for item in group:
                item.core_findings.append(Finding(rule_id="R6", set="core", evidence=evidence))
```

  Then pass `clear_count=facts.clear_count`, `max_clear_cycles_24h=facts.max_clear_cycles_24h`
  and `fire_pattern=facts.pattern` to `EvaluatedIdentity(...)`. Use `Finding`'s real
  constructor from `src/rules/core.py`.

  Read `attach_row_findings`. If it rebuilds `EvaluatedIdentity`, carry the three fields
  across.

  Add `tests/unit/test_rules_firing.py::test_engine_attaches_r6_to_every_row_and_withholds_from_llm`:
  a stuck identity has `"R6"` in `core_rule_ids`, every row has an R6 finding,
  `llm_eligible is False`, and `compute_daily_rule_counts` emits R6 per bucket.

- [ ] **Step 5: Versions and copy.**
  - `src/versions.py`: `RULESET_VERSION = "1.1.0"`. Update the docstring to say R6 became a
    core rule in 1.1.0, and that the prompt's R6 line is deliberately unchanged (spec 5.3).
  - `src/portal/explain.py`: add `RULE_TEXT["R6"]` in the existing shape. Pick the copy from
    `sample_evidence["pattern"]`:
    - stuck: title "Stuck: re-fired for days without clearing"; next step "Resolve the cause
      or clear the alert when it recovers; a stuck alert hides new problems.";
    - spamming: title "Spamming: fires faster than its repeat interval"; next step "Send each
      alert from one place, once per repeat interval.";
    - flapping: title "Flapping: fires and clears over and over"; next step "Add hysteresis or
      a longer pending period so the alert settles before it fires."

    A single `RULE_TEXT["R6"]` entry whose functions branch on the evidence pattern is fine.
    The entry's copy must not contain the forbidden portal substrings.
  - `tests/unit/test_portal_explain.py`: add `SAMPLES["R6"]` using the evidence shape above.

- [ ] **Step 6: Run** `uv run pytest tests/unit -q`. Expected: all pass. Fix any test that pinned
  `RULESET_VERSION == "1.0.0"` or "R6 never produced" so it states the new truth: per-row
  `evaluate_core_rules` still never produces R6, and the engine does.

- [ ] **Step 7: Run integration under the lock:**
  `uv run pytest tests/integration -q -p no:cacheprovider`. Expected: all pass. If
  `verify-acceptance` or acceptance tests fail because existing acceptance teams now hit R6,
  report which teams and why, and do not edit the oracle (Task G owns it).

- [ ] **Step 8: Commit** `feat: evaluate R6 stuck, spamming and flapping per alert`.

---

### Task B: `unseen` alongside suppression

**Files:**
- Modify:
  - `src/suppression/fields.py`: add the identity-field set; map `operator` to the record
    attribute (check `AlertRecord` for the operator attribute name);
  - `src/suppression/evaluate.py`: identity leaves, the hidden set per panel, and the
    `SuppressionResult.unseen_row_ids` / `.unseen_unmeasured` fields (they already exist with
    defaults);
  - `src/run/orchestrator.py`: fill `daily_metrics.unseen` / `unseen_unmeasured` and
    `alert_findings.unseen`.
- Test: `tests/unit/test_suppression.py` (or the existing suppression test file;
  add `test_unseen_*`), `tests/integration/test_persistence.py` (one round-trip assertion).

**Interfaces:**
- Consumes: the `SuppressionResult` fields above; the `unseen` keys already present as
  `None` in the orchestrator's daily-metric and finding dicts (replace those `None`s).
- Produces: `SuppressionResult.unseen_row_ids: set[int] | None` (row `id()`s, disjoint from
  `suppressed_row_ids`; `None` when the schema has no panel) and `.unseen_unmeasured: int`.

Semantics (spec 6):

| Topic | Rule |
|---|---|
| Identity leaf | top-level `AND` leaf, **not negated**, operator `=`, `IN` or `LIKE`, on `operator`, `application`, `node_name`, `object` / `component` |
| Hides | the row's value is non-null and does **not** match the leaf. Use the existing case-sensitive matching, including `like_to_regex`. |
| Never hides | a NULL value, or a multi-value variable with "all" selected |
| Panel hides a row | any of its identity leaves hides it |
| Unseen | every panel for the schema hides the row, minus `suppressed_row_ids` |
| Unmeasured | an identity leaf nested under `OR`/`NOT`, or one with an unresolved `query` variable, adds 1 to `unseen_unmeasured`, and that leaf never hides |
| Unparseable panel | hides nothing |
| Daily allocation | `unseen` per bucket = count of unseen rows on that `snapshot_date` (None when `unseen_row_ids is None`); `unseen_unmeasured` on the schema's first bucket only, else 0 (None when no panel) |
| Work list | `alert_findings.unseen`: None when no panel; True when any row of the identity is unseen; else False |

- [ ] **Step 1: Failing unit tests**, one each:
  - a row whose `application` differs from the panel's `application = 'a'` is unseen;
  - a row matching the narrowing is not unseen;
  - NULL `node_name` against `node_name LIKE 'n%'` is not unseen;
  - with two panels, one showing and one hiding, the row is not unseen (unanimity);
  - a row both suppressed and outside the narrowing counts only as suppressed;
  - `OR`-nested `application = 'a'` → `unseen_unmeasured == 1`, row not unseen;
  - unresolved `query` variable → unmeasured;
  - a schema with no panels → `unseen_row_ids is None`;
  - `operator IN ('x')` with a row operator `y` → unseen;
  - classification fields (`severity = 'critical'`) never hide.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** in `evaluate.py`: a new `LeafOutcome` kind `"identity"` decided
  before the classification-field branch, a per-panel `hidden` set built next to `excluded`,
  an intersection across panels, then subtract the suppressed set. Do not apply the
  blast-radius guard to `unseen`.
- [ ] **Step 4: Orchestrator wiring.** Replace the `None` placeholders with the values per the
  table above.
- [ ] **Step 5: Run** `uv run pytest tests/unit -q`, then integration under the lock. Add to
  `tests/integration/test_persistence.py` an assertion that `unseen` round-trips (NULL and an
  integer). Expected: all pass.
- [ ] **Step 6: Commit** `feat: count alerts a team owns that none of its panels shows`.

---

### Task C: Outputs (CSV, scorecard, outputs.md)

**Files:**
- Modify:
  - `src/report/csv_export.py`: `DAILY_METRIC_HEADERS` gets `unseen` and `unseen_unmeasured`
    after `suppression_unmeasured`; `WORKLIST_HEADERS` gets `clear_count`,
    `max_clear_cycles_24h`, `fire_pattern` and `unseen` at the end; row builders emit them,
    with NULL as an empty cell and booleans as `true`/`false` (match how the file renders
    other booleans);
  - `src/report/html.py`: `rollup_schema` sums `unseen` (None when every bucket is None) and
    `unseen_unmeasured`; `_render_visibility` shows "Unseen (rows no panel shows)" and
    "Unseen unmeasured (leaves)", rendering None as "— no panel supplied"; the limitations
    list gains one line, "R6 flags one alert's firing pattern against its repeat interval; it
    never scores a team's total volume.";
  - `docs/outputs.md`: document every new column in the existing style, update the "26
    columns" / "21 columns" counts to the new totals, and replace the prose saying R6 never
    appears;
  - `tests/unit/test_report.py`: key lists.
- Test: `tests/unit/test_outputs_doc.py` must pass unchanged.

**Interfaces:**
- Consumes: the `daily_metrics` keys `unseen` / `unseen_unmeasured` and the `alert_findings`
  keys `clear_count` / `max_clear_cycles_24h` / `fire_pattern` / `unseen` (all already
  persisted by contract).
- Produces: CSV columns in exactly the positions above.

- [ ] **Step 1:** Add failing assertions to `tests/unit/test_report.py`:
  - the header tuples end as specified;
  - a daily row with `unseen=None` renders an empty cell;
  - the scorecard HTML for a run with `unseen=3` shows "Unseen";
  - with `unseen=None` it shows "no panel supplied".
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement**, including the formula-injection neutralization the CSV writer
  already applies (reuse it, change nothing).
- [ ] **Step 4:** Update `docs/outputs.md`. Run
  `uv run pytest tests/unit/test_outputs_doc.py tests/unit/test_report.py -q`.
  Expected: PASS.
- [ ] **Step 5:** Run integration under the lock. Expected: PASS. If an acceptance CSV
  comparison fails only because of the new columns, extend the comparison in `verify.py` to
  the new columns, or report why not.
- [ ] **Step 6: Commit** `feat: export unseen and the R6 facts`.

---

### Task D: `src/insights` (pure)

**Files:**
- Modify (replace the stubs): `src/insights/estimate.py`, `aggregate.py`, `findings.py`,
  `summary.py`
- Test: `tests/unit/test_insights.py`

**Interfaces:**
- Consumes: `src/insights/model.py` (do not change field names; adding optional fields at the
  end is allowed), `src/config/planning.py`, `src/domain/cadence.py`.
- Produces:
  - `v1_rule_key(alert: AlertRow) -> str`;
  - `estimate(inputs: SummaryInputs) -> Estimate`;
  - `by_application(alerts) -> tuple[AppRow, ...]`;
  - `fire_rows(alerts) -> tuple[FireRow, ...]`;
  - `biggest(alerts) -> AlertRow | None`;
  - `key_findings(inputs) -> tuple[KeyFinding, ...]`;
  - `summarize(inputs) -> TeamSummary`.

**Algorithms** (exactly):

- **`v1_rule_key`:** `"url:" + url.strip()` when `alert_rule_url` is non-empty after strip,
  else `"app:" + application`.
- **`estimate`:**
  - `rules_left` = distinct `v1_rule_key` over the v1 alerts.
  - `per_rule` = `inputs.v1_rule_effort_days` if not None, else
    `DEFAULT_V1_RULE_EFFORT_DAYS`; `effort_is_override` = override present.
  - `effort_days` = `rules_left * per_rule`; `effort_weeks` =
    `effort_days / WORKING_DAYS_PER_WEEK`.
  - Not published, or `history` empty → `no_estimate_reason` = "This week is not published,
    so there is no published history to measure a pace from."
  - Otherwise `selected = history[-1]`. Walk back from `history[-2]`, collecting up to 3
    earlier weeks. Stop before week `h[i]` if `h[i+1].basis_changed`, or if
    `h[i+1].week_end - h[i].week_end != 7 days`.
  - Fewer than 2 earlier weeks → "Needs at least 2 earlier published weeks back to back;
    found {n}."
  - `retired` = `|union(earlier.v1_rules) - selected.v1_rules|`. If `retired < 2` →
    "Fewer than 2 v1 alert rules stopped firing over the last {n} weeks, too few to
    measure a pace."
  - Else `pace = retired / len(earlier)`. If `rules_left == 0`, `projected_week_end =
    selected.week_end.date()`; else `projected_week_end = (selected.week_end + 7 days *
    ceil(rules_left / pace)).date()`.
  - `lookback_weeks` = number of earlier weeks used. `retired` and `pace_per_week` are set
    whenever they were computed.
- **`by_application`:**
  - Group by `(application, schema)`.
  - Counts per group: `alerts` is all of them; `rule_flagged_*` are those with `quality_state
    == "rule_flagged"`; `llm_flagged_*` are those with `== "llm_flagged"`. Events are
    `row_count` sums.
  - `rules` is the union of `core_rule_ids` sorted by rule number.
  - Order by `(-(rule_flagged_events + llm_flagged_events), -events, application, schema)`.
- **`fire_rows`:**
  - `span = last_seen - first_seen + REPEAT_INTERVAL[schema]`.
  - `ratio = row_count / (span / interval)` if provider is `"grafana"`, else None.
  - `events_per_24h = row_count / (span / 24h)`; `pattern = alert.fire_pattern`.
  - Order by `(-row_count, schema, application, key_field)`.
- **`biggest`:** max `row_count`; ties broken by `(schema, key_field)` ascending; None when
  empty.
- **`key_findings`:** in this order, keeping at most 5:
  1. **largest:** the core rule (R1–R7, from `inputs.rules`) with the most events across
     schemas.
     - Title "{rid} is your largest finding".
     - Body "{events:,} {schema} events from {n} alert(s)."
     - Append " The same alerts also match {other}." when every alert carrying rid also
       carries one other core rule.
     - `rule_filter=rid`, `fix=None`; the renderer adds `explain` copy.
  2. **unassessed:** if the total unassessed is > 0.
     - Title "{u} alerts could not be classified".
     - Body "Unassessed should be zero. A non-zero count is a classifier failure, not a
       finding."
  3. **concentration:** for the schema with the most events (v1 on a tie), alerts sorted by
     `row_count` descending; the smallest k with cumulative events ≥ 80%. Only when there
     are more than 2 alerts and k < n.
     - Title "{k} of {n} {schema} alerts make {pct}% of the events".
     - Body "They produced {acc:,} of {tot:,} {schema} events this week."
  4. **hidden:** if suppressed > 0 in either schema.
     - Title "Your own panels hide alerts you still send".
     - Body lists "{x} v1 events" / "{y} v2 events" joined by " and ", then " match a filter
       in your dashboard (R5)."
     - `rule_filter="R5"`.
  5. **unseen:** if any schema has `unseen` > 0.
     - Title "Some alerts reach none of your dashboards".
     - Body "{a} alerts ({e} events) are outside every panel's narrowing."
     - Fix "Widen a panel to include them, or confirm they are meant to stay out of view."
  6. **readiness:** if there are v2 alerts. An alert is ready when it has no R8, no R10, and
     not (`severity == "critical"` with R9).
     - Title "{ready} of {n} v2 alerts are phase-2 ready".
     - Body "{c} critical alert(s) have no runbook." or "Every critical alert has a
       runbook."
     - Fix "Add impact and an https:// runbook, starting with critical alerts."

  No string in these templates contains `per day`, `run_id`, `registry`, `ruleset`, `prompt`
  or `model version`.
- **`summarize`:** builds `TeamSummary(inputs, key_findings(inputs), by_application(...),
  fire_rows(...), biggest(...), estimate(inputs))`.

- [ ] **Step 1: Failing tests** in `tests/unit/test_insights.py`. Use a small builder
  `def alert(**kw) -> AlertRow` with defaults. One test per bullet:
  - **`v1_rule_key`:** URL vs application fallback, and a whitespace-only URL.
  - **Estimate: 0 v1 alerts:** `rules_left == 0`, `effort_days == 0`.
  - **Estimate: not published:** gives the not-published reason, and effort is still computed.
  - **Estimate: one week of history:** gives the "found 0" reason.
  - **Estimate: gap:** weeks 14 days apart stop the lookback.
  - **Estimate: basis change:** `basis_changed` on the selected week means 0 earlier weeks.
  - **Estimate: retired == 1:** gives the "too few" reason.
  - **Estimate: happy path:** 3 earlier weeks with rules `{a,b,c,d}`, `{a,b,c}`, `{a,b}`, the
    selected week `{a}`, so retired = 3 (b,c,d) and pace 1.0. With rules left 1, the
    projection is `selected.week_end + 7 days`.
  - **Estimate: override:** override 2.0 with 4 rules gives `effort_days == 8.0`,
    `effort_weeks == 1.6` and `effort_is_override`.
  - **`by_application`:** ordering and counts.
  - **`fire_rows`:** a single row (ratio 1.0, span = interval); an API alert has ratio None.
  - **`biggest`:** the tie-break.
  - **Key findings:**
    - the order;
    - the cap at 5;
    - the co-occurrence sentence;
    - the concentration threshold;
    - hidden omitted when suppressed = 0;
    - unseen omitted when None;
    - the readiness critical count;
    - every generated string free of the forbidden substrings.
- [ ] **Step 2: Run.** Expected: FAIL (NotImplementedError).
- [ ] **Step 3: Implement** to the algorithms above.
- [ ] **Step 4: Run** `uv run pytest tests/unit/test_insights.py -q` and `uv run mypy src`.
  Expected: PASS.
- [ ] **Step 5: Commit** `feat: compute the team summary and the time-to-v2 estimate`.

---

### Task E: Reader portal Summary section and filters

**Files:**
- Create: `src/portal/summary_view.py`, `src/portal/summary_queries.py`
- Modify:
  - `src/portal/pages.py`: the team page inserts the Summary section between "This week" and
    "Over time", and the work-list filters gain `state` and `rule`;
  - `src/portal/app.py`: new query params validated like `show` / `schema`;
  - `src/portal/queries.py`: the work-list SQL filters by state and rule;
  - `src/portal/assets.py`: CSS classes for the new widgets;
  - `src/portal/charts.py`: SVG bar helpers, if needed.
- Test: `tests/unit/test_portal_surface.py`, `tests/integration/test_portal.py`, and new
  `tests/unit/test_portal_summary_view.py`.

**Interfaces:**
- Consumes: `src.insights` (`summarize`, the model), and the portal views `portal_reviews`
  (`basis_changed`, `v1_rule_effort_days`), `portal_schema_totals` (`unseen`,
  `unseen_alerts`, `r6_alerts`), `portal_alerts` (`clear_count`, `max_clear_cycles_24h`,
  `fire_pattern`, `unseen`) and `portal_rule_totals` (`run_id`, `alert_schema`, `rule_id`,
  `events`, `alerts`).
- Produces (Task F imports these, so keep the signatures exact):

```python
def render_summary_sections(
    summary: TeamSummary,
    *,
    rule_link: Callable[[str | None], str],
) -> str:
    """Every shared Summary widget as HTML, surface-aware via summary.inputs.surface.

    rule_link(rule_id) returns the escaped href of the work list filtered by that rule
    (None = unfiltered). No <script>, no style= attributes. The portal surface never
    prints per-day rates or internals; the admin surface prints distinct_per_day when set.
    """
```

  and

```python
def load_portal_summary(db: Database, team_id: str, run_id: str) -> SummaryInputs:
    """Build portal SummaryInputs for one published week from portal_* views only.
    history = the team's published weeks up to and including this one, oldest first,
    v1_rules = {v1_rule_key(...)} per week from portal_alerts where alert_schema = 'v1'."""
```

Widgets rendered by `render_summary_sections`, in order (spec section 4, portal column; the
canvas artboard "Summary · Data Pipeline / ETL" is the visual reference):

1. Volume tiles per schema.
2. Rule-flagged.
3. Examined-by-model bar.
4. Why alerts were flagged: SVG bars, three groups.
5. Key findings, with `explain` next-step copy for `rule_filter`.
6. Noisy alerts by application.
7. How often alerts fire: top 8 `FireRow`s, an SVG ratio bar with 1× and 2× ticks, the
   pattern pill, and the "Proposed defaults" box replaced by a plain legend of the three
   R6 thresholds.
8. Biggest single source.
9. Flagged by rule, with `explain` titles and next steps.
10. Hidden by your own panels (counts and hidden alerts only).
11. Not on any of your dashboards ("No dashboard supplied" when None).
12. Migration progress, with the estimate card: the date or "No estimate: {reason}", the
    inputs, and the effort "configured, not measured", each labelled a projection with the
    two caveats.

Admin-only content (per-day rates, unmeasured counts) renders only when
`surface == "admin"`.

- [ ] **Step 1: Failing unit tests** (`test_portal_summary_view.py`), using a hand-built
  `TeamSummary`:
  - no `<script`, no ` style=`, and no forbidden substrings on the portal surface (scan the
    rendered HTML after removing escaped alert data; build the summary with clean alert
    text);
  - "No dashboard supplied" when `unseen` is None;
  - "No estimate" and its reason when the estimate has none;
  - the projected week formatted "week of 12 Oct 2026";
  - "configured, not measured";
  - alert text with `<b>` escaped;
  - `rule_link` used for each rule link;
  - the admin surface shows "per day" and the portal surface does not.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement** `summary_view.py`, the CSS classes, and `summary_queries.py`
  (SQL against views only; parameterized; deterministic ORDER BY).
- [ ] **Step 4: Wire into the portal team page and the filters.**
  - New params: `state` ∈ {`all`, `rule_flagged`, `llm_flagged`, `needs_review`,
    `assessed_good`, `unassessed`} (default `all`); `rule` matching `^R(10|[1-9])$` or empty.
  - The rule filter in SQL: `(',' + core_rule_ids + ',' + readiness_rule_ids + ',') LIKE
    '%,' + :rule + ',%'`.
  - Pagination and counts respect the filters.
  - Filter links preserve the other params.
- [ ] **Step 5: Update integration tests** (`tests/integration/test_portal.py`):
  - update the polyline count assertion deliberately, to the new exact number;
  - add a Summary-present test: tile totals equal `portal_schema_totals`, and rule totals
    equal `portal_rule_totals`;
  - add a filters test: `?state=rule_flagged&rule=R1` returns only those rows, with a correct
    "Showing" text;
  - add an estimate-from-published-weeks-only test: publish 4 back-to-back weeks for one
    team, then assert the estimate card text; also that an unpublished run never contributes.

  Keep every existing invariant test passing.
- [ ] **Step 6: Run** unit tests, then integration under the lock. Expected: PASS. Until
  Task D merges, tests calling `summarize` will raise `NotImplementedError`. Merge the Task D
  branch into yours when the integrator says it is ready, or construct `TeamSummary` directly
  in unit tests.
- [ ] **Step 7: Commit** `feat: add the Summary section to the reader portal`.

---

### Task F: Admin Summary page

**Files:**
- Create: `src/admin/summary.py` (route helpers, queries to `SummaryInputs`, admin-only
  widgets)
- Modify:
  - `src/admin/app.py`: `GET /teams/{team_id}/summary`;
  - `src/admin/pages.py`: a "Summary" link per run in the team page's runs table, a link from
    the dashboard, and `ADMIN_CSS` classes;
  - `src/admin/queries.py`, if shared helpers belong there.
- Test: `tests/integration/test_admin.py`, `tests/unit/test_admin_summary.py`.

**Interfaces:**
- Consumes:
  - `render_summary_sections` from Task E (`src/portal/summary_view.py`, signature in Task E);
  - `src.insights`;
  - base tables `runs`, `daily_metrics`, `daily_rule_counts`, `alert_findings`,
    `review_publications`, `weekly_review_log`;
  - `runs.registry_entry_snapshot` for the panel SQL;
  - `src/db/repositories.py` getters.
- Produces: `GET /teams/{team_id}/summary?run_id=&state=&schema=&rule=&page=`.

Behaviour (spec section 4, admin column):
- **Default run:** the team's latest completed run (design 7.6 ordering). An unknown team or
  run gives 404, and a run of another team gives 404.
- **Header:**
  - team, phase, operators, panel count;
  - a run picker as a GET form with a `<select>` and a submit button (no script);
  - publication state: published (by, when, note), withdrawn, or never;
  - the last schedule outcome from `weekly_review_log`;
  - a run strip with the window, model, ruleset, prompt, registry version and run id.
- **Body:**
  - `render_summary_sections(summary, rule_link=...)`;
  - admin-only blocks: day by day (complete UTC days of `daily_metrics`, one SVG per schema,
    using `src/portal/charts.py`) and each panel's SQL from the snapshot, with suppression
    clauses highlighted using `<mark>` elements;
  - a work list with GET filters over `alert_findings`, paginated in SQL (50 per page),
    linking each row to the existing `/runs/{run_id}/findings` page.
- **Estimate:** published weeks only. History comes from `review_publications` (not
  withdrawn) joined to `runs` and `alert_findings`. `basis_changed` is computed in Python from
  consecutive runs' `ruleset_version` / `registry_version`. When the selected run is
  unpublished, `SummaryInputs.published=False`.
- **Page invariants:** every page passes through the guard middleware: 401 without the proxy
  user, "Signed in as", security headers, GET only, no script, no inline style.

- [ ] **Step 1: Failing tests:**
  - `tests/unit/test_admin_summary.py`: the queries-to-inputs mapper on fixture rows,
    `distinct_per_day = sum(daily distinct)/7`, the `basis_changed` computation, and the
    panel highlighting escaping SQL text;
  - `tests/integration/test_admin.py`:
    - 401 without the header;
    - 200 with the header for a persisted run;
    - "Signed in as";
    - 404 for an unknown run and for another team's run;
    - the run picker lists the team's runs;
    - the filters work;
    - no `<script`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.** Until Task E merges, render only the admin-only blocks behind a
  temporary local call. When the integrator merges Task E, switch to
  `render_summary_sections` and remove the temporary code. Do not leave dead code.
- [ ] **Step 4: Run** unit tests, then integration under the lock. Expected: PASS.
- [ ] **Step 5: Commit** `feat: add the operator Summary page to the admin app`.

---

### Task G: Acceptance fixtures and oracle

**Files:**
- Modify:
  - `scripts/acceptance_teams.py`: append `acceptance-fire-patterns` and
    `acceptance-unseen` last;
  - `scripts/generate_mock_alerts.py`: only if per-row status or severity needs a small
    extension, and keep existing output byte-stable;
  - `config/teams.json`: two entries and a `registry_version` bump; add a `planning`
    override to one of them to exercise it;
  - `test/fixtures/expected-results.json`: by hand;
  - `scripts/mock-data-stats.json`: regenerate by running the generator, never by hand.

**Interfaces:** consumes the R6 semantics (Task A table) and the `unseen` semantics (Task B
table) exactly as written in this plan.

Fixture cases (the clock is the acceptance clock already used by the generator):
- **`acceptance-fire-patterns`** (v1 operator `acc-fire`, v2 operator `acc-fire-v2`):

  | Case | Shape | Expect |
  |---|---|---|
  | (a) | v1 Grafana, every 5 min for exactly 72h05m of span | stuck |
  | (b) | v1 Grafana, every 5 min for 71h05m of span | none |
  | (c) | v1 Grafana, two definitions with the same key interleaved at 2.5-minute offsets for 24h | spamming |
  | (d) | v1 Grafana with interleaved `clear` rows making 3 cycles inside 20h | flapping |
  | (e) | 3 cycles spread over 26h | none |
  | (f) | v1 API, 24 rows over 24h | spamming |
  | (g) | v1 API, 23 rows | none |
  | (h) | v2 Grafana with per-row `resolved` making 3 cycles inside 24h | flapping |

- **`acceptance-unseen`** (v1 operator `acc-unseen`), with one panel whose SQL is
  `WHERE operator = 'acc-unseen' AND application = 'shown-app' AND (node_name = 'n1' OR
  severity = 'critical') AND node_name != 'junk'`:

  | Rows | Expect |
  |---|---|
  | `application = 'other-app'` | unseen |
  | `application = 'shown-app'` | seen |
  | `node_name = 'junk'` | suppressed, not unseen |
  | the `OR` leaf | `unseen_unmeasured = 1` |

  No v2 panel, so v2 `unseen` is NULL. Give it a few v2 rows.

- [ ] **Step 1:** Write the definitions. Run `STATS_ONLY=1 uv run python
  scripts/generate_mock_alerts.py` and check the counts.
- [ ] **Step 2:** Compute every expected figure **by hand** from the definitions: daily
  rows, distinct, rule counts per date (R6 on every row of a matching identity), identity
  states, `unseen` per date and `unseen_unmeasured`. Add them to `expected-results.json` in
  the existing structure, with a `_why` per team.
- [ ] **Step 3: Under the lock, after Tasks A and B are merged** (the integrator will tell
  you):
  - `RESET=1` regenerate against the local mock only;
  - run each acceptance team at the acceptance clock with `--fake-llm` into `alerts_bi_test`;
  - run `uv run alerts-bi verify-acceptance` (see `--help` for flags).

  Expected: PASS. Existing acceptance teams must stay R6-free. If one does not, report it.
- [ ] **Step 4: Commit** `test: add fire-pattern and unseen acceptance teams`.

---

### Task H: Design revisions (integrator)

The integrator does this in the main worktree after the lanes merge: spec section 11 in
`docs/alerts_bi_design.md` (with `Last updated`), `AGENTS.md`, `docs/alerts_bi_flow.md`, and
the implementation plan doc if affected. Then the integrator runs everything: format, lint,
mypy, all unit and integration tests, a `RESET=1` reload, acceptance runs and
`verify-acceptance`.
