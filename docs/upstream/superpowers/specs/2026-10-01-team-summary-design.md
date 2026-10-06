# Team summary — design

**Status:** implemented (2026-10-01). Amended the same day by the product owner: the presentation slides show day-by-day distinct and rule-flagged distinct alerts for the one selected published week, labelled "by UTC day", through the `portal_daily_metrics` view (migration `007_portal_daily`). This is a within-week view, never a comparison across weeks (design 7.10). Migration `008` later changed `portal_reviews.basis_changed` to compare the team's own registry entry.
**Date:** 2026-10-01
**Base:** `origin/main` at `a2fca5c` (review portal, weekly schedule, admin app, LLM audit)
**Visual reference:** the design canvas "Alerts BI Team Summary"
(<https://claude.ai/artifact/XwHRgdpHg12Gc2vsuRzUyD>). It was drawn before this rebase: its
layout, widgets and copy carry over; the per-surface differences are in section 4.

[`alerts_bi_design.md`](../../alerts_bi_design.md) remains the source of truth. This work
changes four approved decisions; section 11 lists every revision it needs, made in the same
session the spec is approved.

---

## 1. What this is

The standardization team, and the teams and managers they report to, need one page per team
that answers three questions: where does this team stand, which alerts are noisy and why, and
how much work is left to finish the move to v2.

**It is:**

- A **Summary** page in the **operator admin app** for any completed run, internals included.
- A **Summary** section on the **reader portal's** team week page for published weeks, under
  the portal's rules.

Both are built from shared, pure building blocks. Four additions to the pipeline and the pages
come with it:

1. **R6 becomes a core rule:** stuck, spamming and flapping alerts.
2. **`unseen`:** alerts a team owns that none of its dashboards shows.
3. **Work-list filtering** by state, schema and rule.
4. **An estimate of the time to retire v1:** a measured-pace date and a configured-effort
   figure, side by side.

**It is not:** a cross-team view, a leaderboard, company totals, a change to how runs start or
publish, or a second frontend. One team per page, as everywhere else.

## 2. Decisions taken (2026-10-01, product owner)

| Decision | Choice |
|---|---|
| Surfaces | Admin app (operators) **and** reader portal (published weeks). |
| Base | Build on `origin/main`. The superseded `integration/all-upgrades` frontend is dropped. |
| Rule display copy | Approved. Reuse `src/portal/explain.py` `RULE_TEXT` / `next_step` and add R6. |
| Stored firing facts | Approved: clear cycles per alert. |
| R6 depth | **Full core rule**, with the thresholds in section 5. |
| `unseen` | In scope. |
| Work-list filtering | In scope. |
| Time to v2 | In scope, as a scoped exception, with **both** a measured date and configured effort, so teams and management can plan engineering time. |

## 3. Constraints both apps already enforce

Every addition must hold these, as the existing tests require.

- **Rendering:** server-rendered HTML, **no JavaScript**, **no inline `style=`** (each CSP has
  `style-src 'self'` and no `script-src`). Bars and charts are SVG geometry and CSS classes,
  as `_quality` and `line_chart` already are. Filtering, sorting and paging are **GET links**,
  and paging stays in SQL.
- **Portal reads only `portal_*` views.** It never imports `api`, `run`, `es`, `llm`,
  `review`, `report` or `cli`, and is GET-only on the allowlist.
- **Admin is GET plus token-checked POST**, behind the proxy identity, and reads base tables.
- **Shared code** therefore lives where both may import it: a new pure package
  **`src/insights/`** (no I/O, no SQL) for the estimate, the key-findings sentences and the
  aggregations; and rendering helpers in **`src/portal/`** (the admin app already imports from
  there).

## 4. The Summary — widgets per surface

Each widget is computed from **one run's** rows, except the estimate (section 7), which reads
that team's published weeks. v1 and v2 are never summed.

| Widget | Admin (any completed run) | Portal (published week) |
|---|---|---|
| **Header** | team, phase, operators, panel count, run picker, publication state (published / withdrawn / never), last schedule outcome, run strip (window, model, versions, run id) | as today: team, week picker, week covered, published time, review note. **No** run id, version or timing. |
| **Volume tiles** per schema | distinct alerts per day, rows, rows an hour (design 3.3) | **distinct alerts this week**, **events this week** (design 7.10 totals). The words "per day" never appear. |
| **Rule-flagged** per schema | rows, share of that schema's rows within this run, distinct per day | rows of events, distinct alerts this week |
| **Examined by the model** | state counts per schema as an SVG stacked bar | the existing `_quality` bar |
| **Migration phase** | phase stepper, readiness, ready / all v2 alerts | the existing stepper and meter |
| **Why alerts were flagged** | bars per rule and schema in three groups: *Quality* (core incl. R6), *Phase-2 readiness*, *Model findings (advisory)* | same groups, counted as alerts this week |
| **Key findings** | up to five templated sentences from `src/insights` (largest core finding and its co-occurring rule, how few alerts make 80% of the events, rows hidden by the team's panels, `unseen` rows, readiness with critical alerts lacking a runbook, non-zero `unassessed`) | the same sentences. They never contain internals. |
| **Noisy alerts by application** | per application and schema: flagged alerts of all, flagged events of all events, rules seen | same |
| **How often alerts fire** | per alert: events, active span, fire rate as a multiple of the repeat interval, clear cycles, the R6 pattern | same |
| **Day by day** | complete UTC days of `daily_metrics`, one SVG per schema | shown in the slides for the one selected published week, labelled "by UTC day", from `portal_daily_metrics` (migration 007). A within-week view; the portal's headline figures stay weekly totals. |
| **Biggest single source** | alert with the most rows, with fields, findings and the fix | same |
| **Flagged by rule** | rule, title, schema, rows, distinct per day, top applications, what to change | same, counted per week |
| **Hidden by your own panels** | suppressed and unmeasured per schema; each panel's frozen SQL from `runs.registry_entry_snapshot` with suppression clauses highlighted | suppressed rows, the hidden alerts and their fix. **No** panel SQL. |
| **Not on any of your dashboards** | `unseen` rows and alerts, unmeasured clauses, panel identity narrowing | `unseen` events and alerts |
| **Migration progress** | phases with their *done when* criteria, what is left in phase 1 (v1 alerts, applications, v1 rules), readiness counts, the estimate | same, with the estimate |
| **Work list** | filters, sorting, paging, links to the existing findings and decision page | the existing list, with more filters |

**Filtering** is by quality state, schema, rule id (set by the "show these alerts" links) and
sort (events, application). It uses query parameters with bounded values, validated like the
existing `show` / `schema` / `page`.

**Surface-specific test changes, made deliberately:**

- The portal test asserting exactly four `<polyline>` elements on the team page is updated to
  the new count.
- No new copy may contain the portal's forbidden substrings (`per day`, `run_id`, `registry`,
  `ruleset`, `prompt`, `model version`).

> **Amended 2026-10-01:** the Grafana repeat interval is disabled in v1 and v2, so R6 is judged by firing episodes, not by a repeat interval. Design section 7.14 is authoritative; the cadence definitions below are superseded. **Clarified the same day:** Grafana writes an ES row on every evaluation, so a Grafana row count is evaluation cadence; spamming applies to API alerts only, and `max_episode_firing_rows` is a stored diagnostic. **Amended again after the final review (product owner):** stuck is a Grafana alert whose last row is firing and whose open episode's firing rows span at least 72 hours, from its first to its last firing row; it is never measured to the end of the week, so an alert that went silent is not stuck. Where this section and design 7.14 differ, 7.14 wins.

## 5. R6 — stuck, spamming and flapping (core rule)

R6 judges **one alert's firing pattern against its schema's repeat interval**. It never
judges a team's total volume, which stays displayed and unscored (design section 2, revised).

### 5.1 Facts stored per alert

Each identity's raw rows in the run window are evaluated in order of
`(@timestamp, document hash)`, an explicit tie-break, because the ES reader's shard order is
not stable.

| Fact | Definition |
|---|---|
| `clear_count` | rows that clear the alert: v1 severity `clear` (code 1), v2 `status = resolved` |
| `max_clear_cycles_24h` | the most fire → clear cycles inside any rolling 24 hours. A cycle is a clear row immediately preceded by a non-clear row of the same identity. |
| `fire_pattern` | `stuck`, `spamming`, `flapping` or `NULL` |

**Active span** = `last_seen − first_seen + repeat interval`, so a single row spans exactly one
interval. **Expected rows** = span ÷ interval. **Fire-rate ratio** = rows ÷ expected rows.
Repeat intervals: v1 **5 minutes**, v2 **12 hours**.

### 5.2 Patterns and thresholds

| Pattern | Matches when |
|---|---|
| **flapping** | `max_clear_cycles_24h ≥ 3`, on any provider |
| **spamming** | Grafana: ratio ≥ **2.0**. API: events per 24 h of active span ≥ **24**, with span ≥ 6 h. |
| **stuck** | Grafana, ratio ≥ **0.9**, span ≥ **72 h**, `clear_count = 0` |

Each alert gets one pattern, in the priority flapping → spamming → stuck. API alerts have no
repeat interval (design 7.3), so they are never stuck. Thresholds and intervals are catalogue
constants under `ruleset_version`, never environment settings.

### 5.3 R6 in the pipeline

- **Where:** in `src/rules/engine.py` `evaluate_rows`, after identities are grouped and before
  `core_rule_ids` is computed. `R6` joins `CORE_RULE_IDS`, without which
  `compute_daily_rule_counts` drops it.
- **Row allocation:** the pattern belongs to the identity's whole row set, so every row of a
  matching identity matches R6. The existing per-bucket allocation then applies unchanged.
- **The model:** R6 is a core finding, so the identity is withheld from the model and becomes
  `rule_flagged`. Accepted: a stuck or spamming alert already has a concrete fix.
- **Evidence:** a per-rule summary as today, with operands (events, span, ratio, cycles,
  pattern).
- **`ruleset_version` → 1.1.0.** This changes every new `run_id`, as any version bump does.
  Published runs are never re-persisted.
- **Prompt: superseded 2026-10-01 — ships as 1.3.0 (design 7.14), because the prompt embeds `ruleset_version`.** Original text: The prompt calls R6 a catalogue label that is
  "not evaluated deterministically in this version" and tells the model not to infer volume.
  Every R6 alert is withheld from the model, so the line cannot affect a verdict. Changing it
  forces `PROMPT_VERSION` 1.3.0, a new pinned prompt artifact and **re-classification of every
  alert** through the weekly schedule. The wording is corrected at the next planned prompt
  release instead, and design 5.1 records this. *Alternative: bump now and accept the
  re-classification load.*
- **Effect on realistic mock teams:** about 13 v1 and 2 v2 Grafana definitions fire at their
  interval for ≥ 72 h without clearing and will match stuck. That is intended. Their
  scorecards change; the hand-authored oracle covers acceptance teams only.

## 6. `unseen` — alerts a team owns that its dashboards never show

`unseen` is a **visibility measure, not a rule**. It never counts toward `flagged`, never
reaches the model and gets no blast-radius guard, because nothing is marked bad.

- **Identity leaves:** positive top-level `AND` leaves (`=`, `IN`, `LIKE`) on `operator`,
  `application`, `node_name` and `object` / `component`. `operator` is mapped onto the record
  for this purpose only. It stays a classification field for suppression.
- **Evaluation** (`src/suppression/evaluate.py`): a panel *hides* a row when one of its
  identity leaves positively mismatches the row's value. A `NULL` value or an "all"
  multi-value selection never hides. A row is **unseen** when every panel for its schema hides
  it, minus rows already suppressed, so the two counts are disjoint.
- **Unmeasured:** identity leaves nested in `OR`, or with an unresolved `query` variable, add to
  `unseen_unmeasured`, and that leaf never hides a row. Another top-level identity leaf of
  the same panel can still hide it. An unparseable panel hides nothing and adds 1 to
  `unseen_unmeasured`.
- **No panel for a schema:** `unseen` is `NULL`.
- **Allocation:** `unseen` per bucket by row date; `unseen_unmeasured` on the schema's first
  bucket, like `suppression_unmeasured` (design 7.6).

## 7. Time to retire v1 — a scoped exception

Design sections 2, 7.4 and 7.10 forbid conclusions drawn across weeks. This is a recorded
exception, bounded as follows.

- **Where:** the Migration progress widget on both surfaces. **Never** in the scorecard, the
  three CSVs or anything `outputs.md` describes. Computed at request time and **never stored**.
- **What:** when phase 1 ends, that is, when no v1 alert rule fires. It does not estimate
  phase 2 or "done".
- **Input: published weeks only**, on both surfaces, so a reader and an operator always see
  the same figure. The admin page shows it for a run only when that run is published, and says
  so otherwise.
- **Unit of work: a v1 alert rule** = distinct `alert_rule_url`, or `application` when there is
  none. This matches LLM batching and how teams fix things.

**Rules left** = v1 rules in the selected week. This is a fact.

### 7.1 Measured pace → a date

- **Lookback:** the selected published week and up to **3** consecutive published weeks before
  it. A publication gap ends the lookback. So does a change of `ruleset_version`, or of
  the team's own v1 operators, v2 operator or panels (migration 008; another team's
  enrolment no longer ends it), which the portal sees only as a `basis_changed` flag on
  `portal_reviews`, never as a version.
- **Retired** = v1 rules present in an earlier lookback week and absent from the selected week.
- **Pace** = retired ÷ earlier weeks in the lookback, in rules a week.
- **Date** = selected week end + rules left ÷ pace, at week precision ("week of 12 Oct 2026").
- **No estimate** when the lookback has fewer than 2 earlier weeks or fewer than 2 retired
  rules. The widget says which.

### 7.2 Configured effort → engineering time

- **Effort** = rules left × effort per rule, in working days and weeks.
- **Default 0.5 working days per v1 rule.** It covers rebuilding the rule in v2 and
  re-deciding severity against the Wake-Up Test. Impact and runbook are phase 2 and are
  excluded.
- The default lives in `src.config`. A team may override it with optional
  `planning.v1_rule_effort_days` in its registry entry. The field is declared in
  `config/teams.schema.json` (`additionalProperties: false`) and parsed into `TeamEntry`, and
  using it needs a `registry_version` bump.
- The portal reads the override through a `portal_reviews` column extracted from the run's
  snapshot, not from the snapshot itself.
- The figure is labelled **configured, not measured**, with the per-rule value shown.

Both figures appear side by side with their inputs. Each is labelled a **projection**, with two
caveats: v1 falling may be cleanup rather than migration, and teams rebuild rather than port,
so v1 can fall while monitoring is lost (design 3.4).

## 8. Rule copy

`src/portal/explain.py` already gives every rule a title, reason and `next_step`, and every
principle a step. The summary reuses it. R6 gains a `RULE_TEXT` entry, one per pattern, and a
`SAMPLES["R6"]`, which `test_portal_explain.py` already requires for every rule.

## 9. Storage and outputs

**Migration `005_team_summary`** (`.sql` and `versions/005_team_summary.py`,
`down_revision = 004_llm_review_audit`):

| Object | Change |
|---|---|
| `daily_metrics` | `unseen INT NULL`, `unseen_unmeasured INT NULL`; check `suppressed + ISNULL(unseen, 0) <= alerts` |
| `alert_findings` | `clear_count INT NOT NULL DEFAULT 0`, `max_clear_cycles_24h INT NOT NULL DEFAULT 0`, `fire_pattern NVARCHAR(16) NULL` (checked), `unseen BIT NULL` |
| `portal_alerts` | `CREATE OR ALTER`: adds the four `alert_findings` columns |
| `portal_schema_totals` | `CREATE OR ALTER`: adds weekly `unseen`, `unseen_alerts`, `r6_alerts` |
| `portal_rule_totals` (new) | per published run, schema and rule: events and distinct alerts this week. No `ruleset_version`. Granted like the others, and added to `src/db/reader.py`. |
| `portal_reviews` | `CREATE OR ALTER`: adds `basis_changed` and `v1_rule_effort_days` |

Repositories extend `_DAILY_METRIC_COLUMNS` and `_FINDING_COLUMNS`.

**Outputs** (documented in `outputs.md`, enforced by `test_outputs_doc.py`):

- `daily_metrics.csv` gains `unseen`, `unseen_unmeasured`.
- `alert_worklist.csv` gains `clear_count`, `max_clear_cycles_24h`, `fire_pattern`, `unseen`.
- The scorecard's *Dashboard visibility* section gains `unseen`, and `rollup_schema` sums it.
  R6 appears in the rule breakdown, which is data-driven.
- The column counts and the prose saying R6 "never appears" are updated.
- Still exactly three CSVs and one scorecard. The estimate appears in none of them.

## 10. Testing

Written first, in the repository's existing style.

**Unit**
- R6 boundaries:
  - flapping at 2 and 3 cycles inside 24 h, and 3 cycles spread over 25 h;
  - spamming at 1.99× and 2.0×;
  - API at 23 and 24 events per 24 h, and under 6 h of span;
  - stuck at 71 h and 72 h, at 0.89× and 0.9×, and with one clear row;
  - pattern priority, the timestamp tie-break, and v1 `clear` versus v2 `resolved`.
- `test_rules_core` no longer asserts that R6 never fires.
- `unseen`:
  - disjoint from suppressed;
  - unanimity across panels;
  - `NULL` and "all" never hide;
  - `OR` and `query` variables counted as unmeasured;
  - `NULL` when there is no panel.
- `src/insights`:
  - rule grouping with the `application` fallback;
  - retired rules;
  - the lookback ending at a gap and at a basis change;
  - every "No estimate" branch;
  - effort with and without an override;
  - key-findings sentences free of internals and of the portal's forbidden substrings.
- `explain.py` R6 text.

**Integration**
- Migration 005, with a single Alembic head and the ledger verified.
- Round-trip of the new columns and views.
- Portal: the Summary section under every existing invariant; the filters; the updated
  polyline count; totals matching stored metrics; the estimate drawn from published weeks only.
- Admin: the Summary returns 401 with no proxy user, shows "Signed in as", 404 for an unknown
  run, GET only.
- The estimate never appears in the four output files.

**Acceptance**
- Append `acceptance-fire-patterns` and `acceptance-unseen` teams last to the generator, so
  existing data stays byte-stable. Give v1 interleaved `clear` rows and v2 per-row
  `resolved` rows.
- Add their registry entries with a version bump.
- Extend `test/fixtures/expected-results.json` **by hand**. Confirm that existing acceptance
  teams stay R6-free.

## 11. Required design revisions (same session as approval)

- **`alerts_bi_design.md`**
  - **§2 and §7.10:** volume stays unscored at team level, and R6 is scoped per alert; the
    time-to-v1-retirement exception and its bounds.
  - **§3.2:** restore `unseen`.
  - **§4:** R6 becomes core.
  - **§5.1:** R6 withholds alerts from the model; the prompt line is deferred to the next
    prompt release.
  - **§6:** storage.
  - **§7.3:** resolved.
  - **§7.4:** `unseen`, R6 and filtering are built.
  - **§7.12:** the admin Summary page.
- **`AGENTS.md`:** the R6 boundary bullet, the portal bullet (Summary section and estimate),
  and the post-MVP order.
- **`alerts_bi_flow.md`:** rule evaluation now includes R6 and `unseen`.
- **`outputs.md`:** section 9.

## 12. Parallel implementation lanes

Shared contracts first, then disjoint lanes, one integrator.

| Lane | Owns | Depends on |
|---|---|---|
| **0. Contracts** | migration 005, `_DAILY_METRIC_COLUMNS` / `_FINDING_COLUMNS`, `src/insights` dataclasses and signatures, `TeamEntry.planning` and schema, version constants | — |
| **A. R6** | `src/rules`, its tests, `explain.py` R6 entry | 0 |
| **B. `unseen`** | `src/suppression`, the `orchestrator.py` hookup, its tests | 0 |
| **C. Outputs** | `src/report` (CSV and scorecard), `outputs.md`, output tests | 0 |
| **D. Insights** | `src/insights`: estimate, key findings, aggregations, unit tests | 0 |
| **E. Portal** | portal views wiring, `src/portal` queries, pages, charts, CSS, tests | 0, D |
| **F. Admin** | `src/admin` route, queries, pages, tests | 0, D, E's shared render helpers |
| **G. Fixtures** | generator acceptance teams, registry entries, oracle (by hand) | A, B |
| **H. Docs** | design revisions of section 11 | approval |

## 13. Still out

- Cross-team views, leaderboards, company totals, deltas, two-run diffs, and an estimate for
  phase 2 or "done".
- A day-by-day chart across weeks in the portal. (The within-week, by-UTC-day chart on the summary slides is in scope; see section 4.)
- Model classification of backfilled history.
- Porting the historical backfill and the Claude client from `integration/all-upgrades`.
  Recommended as a separate PR; not part of this spec.
