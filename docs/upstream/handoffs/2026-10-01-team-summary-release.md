# Handoff: deploying the team-summary release

**Written:** 2026-10-01 · **For:** the agent or operator who deploys this release to the
airgapped OpenShift environment · **Release:** `main` at the merge `8d76043` and later
(`feature/team-summary`, 72 commits on top of `a2fca5c`).

Read [`AGENTS.md`](../../AGENTS.md) and [`alerts_bi_design.md`](../alerts_bi_design.md) in full
first, as the repository requires. This file tells you what changed for deployment and in
what order to do it; design section 7.14 is the authority on behaviour.

---

## 1. What this release is

| | Before (`a2fca5c`) | After (`main`) |
|---|---|---|
| `RULESET_VERSION` | 1.0.0 | **1.1.0** (R6 added) |
| `PROMPT_VERSION` | 1.2.0 | **1.3.0** (embeds the ruleset; R6 catalogue line) |
| `PARSER_VERSION` | 1.0.0 | **1.1.0** (identity leaves for `unseen`) |
| `APP_VERSION` | 0.1.0 | 0.1.0 |
| Schema head | `004_llm_review_audit` | **`008_measurement_basis`** |

New behaviour:

- **Team summary**: the admin page `GET /teams/{team_id}/summary` and a Summary section on the
  portal team page. Both include a bars/donut toggle, key findings, noisy applications, firing
  patterns, hidden and `unseen` alerts, the estimated time to retire v1, and two 16:9
  presentation slides with day-by-day charts.
- **R6, a core rule** judged by firing episodes (design 7.14):
  - **flapping:** 3 or more fire→clear cycles within 24 h, on any provider;
  - **spamming:** API alerts only, at least 24 events per 24 h over at least 6 h;
  - **stuck:** a Grafana alert whose last row is firing and whose open episode's firing
    rows span at least 72 h. It is measured to the last firing row, never to the end of the
    week.
- **`unseen`:** rows that no supplied dashboard panel shows. It is a visibility measure, never
  a rule, and `NULL` when not measured.

**Unchanged, so there is nothing to do for these:**
- no new Python dependencies (`pyproject.toml` and `uv.lock` are untouched);
- no new environment variables, Secret or ConfigMap keys;
- no Dockerfile change.

The registry schema gained one **optional** field, `planning.v1_rule_effort_days` (default
0.5 working days per rule). An existing registry stays valid as it is.

---

## 2. Ask before you touch anything

Alerts BI has already run live on OpenShift for a client: the user reported this on
2026-09-17. The repository holds no manifests and no record of that deployment. Before you
start, ask the user:

1. Which image is running, and how was it built? Rebuild from `main` the same way.
2. Which database the pods use (`SQL_DATABASE`), and who is allowed to run migrations on it.
   The design says the database-owning team applies migrations, or an init container does.
3. Whether the weekly CronJob (`alerts-bi weekly`) is running, and whether the portal and admin
   apps are deployed.
4. That a **database backup** exists, or ask for one. These migrations cannot be rolled back
   (section 6).

---

## 3. Pre-flight checks (read-only)

Run all four against the production database before migrating:

```sql
-- 1. 005 uses STRING_SPLIT: needs compatibility level >= 130. The user confirmed it is.
SELECT compatibility_level FROM sys.databases WHERE name = DB_NAME();

-- 2. 008 uses HASHBYTES over panel SQL that may exceed 8,000 bytes: needs SQL Server 2016+.
SELECT @@VERSION;

-- 3. 005 adds CHECK (suppressed + ISNULL(unseen, 0) <= alerts) WITH validation of existing
--    rows. unseen is NULL on every existing row, so this must return 0 or 005 will fail.
SELECT COUNT(*) FROM daily_metrics WHERE suppressed > alerts;
```

```bash
# 4. The ledger: 001-004 applied, every checksum clean, one head.
alerts-bi db status
```

If check 4 shows a checksum mismatch on 001–004, stop and ask. Never edit an applied migration.

---

## 4. Migrations to run, in order

All four are applied by the normal `alerts-bi db migrate` (or `alerts-bi db setup` in an init
container). They run in order, and each is recorded in `schema_migrations` with its checksum.

| Revision | What it does | Notes |
|---|---|---|
| `005_team_summary` | `daily_metrics` gains `unseen` and `unseen_unmeasured` (NULL). `alert_findings` gains `clear_count` and `max_clear_cycles_24h` (NOT NULL DEFAULT 0), plus `fire_pattern` and `unseen` (NULL). It adds CHECK constraints and recreates the views `portal_reviews`, `portal_schema_totals` and `portal_alerts`. It adds the view `portal_rule_totals`. | Uses `STRING_SPLIT` (compatibility level ≥ 130). The NOT NULL DEFAULT columns are metadata-only on Enterprise edition, but may rewrite `alert_findings` on Standard: run it in a quiet window. |
| `006_r6_episodes` | `alert_findings` gains `max_episode_firing_rows` (NOT NULL DEFAULT 0) and `open_since` (NULL), plus a CHECK. Recreates `portal_alerts`. | Same edition note. |
| `007_portal_daily` | Adds the view `portal_daily_metrics`: published weeks only, per schema and UTC day. It feeds the slides' charts. | View only. |
| `008_measurement_basis` | Recreates `portal_reviews`. `basis_changed` now compares the ruleset and the team's own operators and panels, not the whole-file registry version. | View only. Uses `HASHBYTES`. |

The views are granted to the `alerts_bi_reader` role when that role exists, using the same
pattern as 002 and 005. `CREATE OR ALTER VIEW` keeps grants that already exist.

```bash
alerts-bi db migrate          # or the migrate Job from docs/openshift-deployment.md §5
alerts-bi db status           # expect 001-008 applied, clean, single head
```

---

## 5. Rollout order

1. **Suspend the weekly CronJob** so nothing auto-publishes under the new rules before you have
   looked:
   `oc patch cronjob alerts-bi-weekly -p '{"spec":{"suspend":true}}'`
2. Take the backup (section 2) and run the pre-flight checks (section 3).
3. Build the image from `main` the same way the running one was built. Confirm the four runtime
   files are in it ([`openshift-deployment.md`](../openshift-deployment.md) §2). A missing
   guide only surfaces once the model is enabled.
4. Run the migrations (section 4), then `alerts-bi db status`.
5. Roll out the pipeline, portal and admin pods on the new image.
6. **Trial run on one team, not published.** Use a `--run-at` that is *not* a Monday 00:00 UTC,
   so it can never be the week the CronJob will publish:
   ```bash
   alerts-bi run --team <team_id> --run-at <now, ISO 8601 UTC> --no-llm
   alerts-bi run --team <team_id> --run-at <same instant>            # live model
   ```
   Open the admin Summary for that run (`/teams/<team_id>/summary`) and run the R6 sanity
   checks in section 7.
7. **Unsuspend the CronJob**. The next daily run publishes due weeks as usual:
   `oc patch cronjob alerts-bi-weekly -p '{"spec":{"suspend":false}}'`
8. Verify per [`openshift-deployment.md`](../openshift-deployment.md) §9.

---

## 6. Rollback

- **The migrations are forward-only.** Every `downgrade()` raises `NotImplementedError` on
  purpose; fixing forward means adding a new revision.
- **Rolling back the image is very likely safe, but untested.** 005–008 only *add* nullable or
  defaulted columns and views with *added* columns, so the previous code should still insert
  (the new columns take their defaults) and read the views (it selects named columns). Nobody
  has run the old image against the new schema.
- **To undo the schema itself, restore the backup.** The weeks published after the migration
  would be lost.

---

## 7. Warnings and what to expect

**The first run per team is heavier.**
- Saved model verdicts are keyed by `(application, key_field, prompt_version, model_version)`, so
  the move to prompt 1.3.0 means no verdict from 1.2.0 is reused. Every model-eligible alert is
  assessed again once.
- Watch the CronJob's `activeDeadlineSeconds` (43200 in the doc) and the model endpoint's
  capacity on the first night.

**The estimate starts empty.**
- The ruleset changes from 1.0.0 to 1.1.0, so `basis_changed` is true on the first new week, and
  the v1 retirement estimate shows "No estimate" with the reason.
- It needs 2–3 weeks published under 1.1.0 before it projects a date. This is expected, not a
  bug.

**Old weeks keep their old meaning.**
- Weeks published before this release show "Not measured this week" for dashboard visibility
  (`unseen` did not exist) and have no R6 findings.
- A published run can never be re-persisted (design 7.10), so do not try to re-run them under
  the new rules.

**R6 changes counts.** Alerts that R6 flags become rule-flagged and are withheld from the model,
so rule-flagged counts rise and model-assessed counts fall. Firing patterns are a core rule from
1.1.0 on, so this is intended.

**R6 depends on production's clear rows.** This has been validated **only on mock data.**
- **What counts as a clear row:** `is_clear` treats a v1 row as a clear when its normalised
  `severity` is `clear` (code 1), and a v2 row when its `status` is `resolved`
  (`src/rules/firing.py`). Rows are per evaluation; the user confirmed this.
- **After the trial run,** check the stored facts:
  ```sql
  SELECT alert_schema, fire_pattern, COUNT(*) AS alerts, SUM(clear_count) AS clears
  FROM alert_findings WHERE run_id = '<trial run_id>'
  GROUP BY alert_schema, fire_pattern;
  ```
  - If `clears` is 0 for a schema whose alerts you know resolve, production marks clears
    differently. In that case flapping can never fire, and every long-firing Grafana alert reads
    as stuck. Stop and raise it before unsuspending the CronJob.
  - A large stuck count with sensible clears is plausible: it means alerts that fired for 72 h
    or more without clearing.

**The new prompt has never run on the on-prem model.** Prompt 1.3.0 only adds the R6 catalogue
line to 1.2.0, but it has not run against the real model. In the trial run with the live model,
confirm `llm_assessed: true`, then read a few verdicts.

**Day-by-day charts on the portal.** The slides show the distinct and rule-flagged distinct
alerts of each UTC day *inside one published week*, labelled "by UTC day". This is a product
decision recorded in design 7.10, not a cross-week comparison.

---

## 8. Where things are

| Need | Place |
|---|---|
| Behaviour and every decision | `docs/alerts_bi_design.md` §7.10, §7.14 (the R6 definitions and storage for 005–008) |
| What each number, view and column means | `docs/outputs.md` (§10 lists all six `portal_*` views and their columns) |
| Cluster objects, the migrate Job, the CronJob and verification | `docs/openshift-deployment.md` §5, §8, §9 |
| Upgrade notes and the compatibility-level check | `README.md` (review portal and LLM sections) |
| Fixture expectations | `docs/team_alert_status.md`; the hand-authored oracle `test/fixtures/expected-results.json` |

Tests at `main` (2026-10-01), on the local mock with a clean `RESET=1` reload:
- ruff and mypy clean;
- 982 unit tests;
- 167 integration tests;
- 4 acceptance tests;
- `verify-acceptance` passed 843 checks.

---

## 9. Follow-ups, not blocking deployment

**From the browser test, not applied:**
- fold the slides into a collapsed section so they don't push the work list down;
- cache the fingerprinted stylesheet for a year (it is served `no-store` today);
- add a favicon (a 404 on every page);
- a print stylesheet, one slide per landscape page;
- gzip;
- hide the long rules-table column on phones;
- a "ranked by rule-flagged alerts" note on noisiest applications.

**From the reviews, minor:**
- the biggest source and the application sort compare v1 and v2 event counts;
- model answers that cite R5 or R6 are not rejected;
- `r6_alerts` is unused;
- each portal page loads every alert of the week;
- `unseen` matching is case-sensitive;
- an integration case for 008's fallback hash;
- the unused `window_end` parameters in `evaluate_rows` and `fire_rows`;
- stuck copy built from the constant.

**Still open from before:** historical backfill (the next approved post-MVP step), and
reconciling `openshift-deployment.md`'s "proposed, not proven" header with the live client run.
