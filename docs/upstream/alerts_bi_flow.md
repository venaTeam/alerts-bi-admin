# Alerts BI — Runtime Flow

Repository separation: use the [current source map](source-map.md) for package paths and application ownership. The behavior described below is unchanged.

This document describes one MVP run from start to finish. [`alerts_bi_design.md`](alerts_bi_design.md) remains the source of truth for rules, rationale, and scope.

## 1. Start a run for one team

An operator selects one team and starts a manual run. The run captures `run_at` once and covers the exact preceding 168 hours in UTC: `window_start` is inclusive and `window_end = run_at` is exclusive.

At startup, the tool:

1. Loads the selected team's registry entry: team name, v1 operators, v2 operator, and optional Grafana panel queries.
2. Freezes the registry, ruleset, prompt, and model versions for the run.
3. Creates a `run_id` and stores the run time and exact window boundaries.

A run never scans every team. Cross-team comparison is outside the MVP.

The registry is validated before any query. V1 operator lists may be empty after migration and the v2 operator may be absent before migration, but the selected entry must contain at least one source operator and no exact operator may belong to two teams. The run stores the registry version, complete-file hash, and an immutable snapshot of the selected entry.

## 2. Read the selected team's alerts

The tool queries Elasticsearch directly:

- `appchi-v1`: alerts in the run window whose `operator` is one of the team's configured v1 operators.
- `appchi-v2`: alerts in the run window whose `operator` equals the team's configured v2 operator.

The queries do not restrict `application`, apply Grafana panel filters, read the SQL hot table, or sweep alerts belonging to other teams.

The tool counts and evaluates every returned row incrementally, then releases its document. It retains exact daily and identity facts and the latest full representative, not the entire raw week (design section 7.15). It does not persist or depend on Elasticsearch row IDs because source documents expire.

## 3. Normalize, identify, and count

For both schemas, the alert identity is:

`application + key_field`

The tool then produces two views of the input:

- **Raw rows:** visited once for alert volume and row-level counts; compact facts survive processing.
- **Distinct alerts:** one record per identity, used for identity-level aggregation and LLM assessment. The most recent row in the run is the representative document.

Metrics are assigned to the UTC calendar date of `@timestamp` and rolled up into the seven-day team scorecard. A rolling 168-hour window normally touches eight UTC dates, so the first and last buckets may be partial; every row stores its exact boundaries and covered hours. The main volume metrics are total alerts, distinct alerts, and alerts per hour.

Within each bucket, `alerts` is the raw-row count, `distinct_alerts` is the distinct identity count, and `alerts_per_hour` divides rows by actual covered hours. On the scorecard, total alerts are summed, alerts per hour divide that sum by 168, and distinct alerts are published as `sum(daily distinct alerts) / 7`, labelled **distinct alerts per day**. V1 and v2 stay separate.

Two diagnostic ratios are also calculated:

- `node_name_ratio`: distinct `(application, component/object, node_name)` divided by distinct `(application, component/object)`. Only records with a nonempty `node_name` participate in either side.
- `key_inflation_ratio`: distinct `(application, key_field)` divided by distinct `(application, component/object)`.

These ratios provide context; they do not flag alerts.

Every daily row also stores both ratios' numerator and denominator. A zero denominator produces `null`. The scorecard rollup divides the sum of daily numerators by the sum of daily denominators rather than averaging already-rounded ratios.

## 4. Apply deterministic checks

Every raw alert row is evaluated by the deterministic rule engine. Results are then aggregated by `application + key_field`.

### Core findings

Core findings affect the quality score and prevent the alert from being sent to the LLM:

- **R1:** the complete normalized message matches the versioned generic-phrase catalogue. Message length alone does not trigger the rule.
- **R2:** the complete normalized message matches the versioned informational/heartbeat phrase catalogue.
- **R3:** a required identity/ownership field is empty, or an identity/ownership field contains an exact placeholder value. An absent optional `node_name` is valid.
- **R4:** missing alert-rule URL, but only when `provider = grafana`. API alerts never match R4.
- **R5:** alert suppressed by the approved panel-filter logic.
- **R6** (ruleset 1.1.0, design section 7.14): evaluated per identity over all of its rows in the window: stuck, spamming or flapping, judged by firing episodes (the Grafana repeat interval is disabled and Grafana writes a row per evaluation, so spamming applies to API alerts only). When it matches, every row of that identity carries the R6 finding.
- **R7:** invalid v1 `time_created`. It is valid only when it falls within the inclusive interval from 24 hours before `@timestamp` through `@timestamp`. Future values and older values are flagged.

An alert row may match several rules. For each rule, `count` is the number of matching rows and `distinct_count` is the number of identities with at least one matching row. `flagged_by_rule` counts the union of matching rows; `flagged_by_rule_distinct` counts the union of matching identities. Findings are not copied onto other non-matching rows under the same identity.

Daily deterministic counts stay on the dates of the rows that actually matched. LLM verdicts apply to the complete identity: an LLM-flagged identity contributes all of its rows to `flagged_by_llm`, and its distinct state is counted once on every UTC date where that identity appears. Review, good, and unassessed identities use the same once-per-present-date allocation. Scorecard distinct measures are daily rates; the actionable worklist remains one row per identity.

### Phase 2 readiness gaps

R8–R10 measure missing or inadequate v2 enrichment. R8 matches a missing, invalid-type, empty, or exact-placeholder `impact`; a present but poor impact belongs to R10 or the LLM. R9 matches a missing, invalid-type, placeholder, or non-absolute HTTP(S) `runbook_url` for every v2 severity. A critical R9 match prevents phase completion; a high or warning match remains visible without preventing completion. R10 matches only the complete normalized values in its narrow technical-cause catalogue; broader cause-versus-impact judgment belongs to LLM principle P9. These rules are stored in `phase2_gaps` and affect migration readiness, not alert quality. They do not prevent LLM assessment.

Phase-completion readiness is evaluated on each v2 identity's most recent representative. An identity is ready when it has no R8 or R10 gap and, if critical, no R9 gap. The percentage divides ready identities by all distinct v2 identities; it is `null` when no v2 identity exists. Non-critical R9 gaps remain visible without reducing this percentage.

## 5. Evaluate panel suppression

If the team registry contains panel queries, the tool parses them into an AST without using the LLM.

Suppression is measurable only when all applicable conditions are safe:

- The exclusion is a top-level `AND` leaf.
- The negated field is an approved instance field.
- Grafana custom, constant, and interval variables can be resolved from the frozen definitions supplied with the registry.
- Query variables do not need to be executed.
- When several panels apply, they unanimously suppress the alert.
- The resulting suppression set does not exceed the 50% blast-radius guard.

If these conditions are not met, the alert is recorded as `suppression_unmeasured`; it is not silently suppressed. A valid suppression becomes the core R5 finding.

The same parse yields `unseen` (design section 7.14): rows every panel for the schema leaves out through a positive identity predicate, minus suppressed rows. It is a visibility count, not a finding, and is `NULL` for a schema with no supplied panel.

The MVP does not call Grafana. Query variables and missing required definitions remain unresolved and make the affected suppression leaf unmeasured.

Panel parsing is cached by SQL-text hash so identical query text always receives the same interpretation.

## 6. Assess remaining alerts with the LLM

Only identities with no core finding on any row in the run window enter this stage. Alerts with v2 readiness gaps are still eligible. For each eligible identity, the most recent row is the representative document.

### 6.1 Reuse durable verdicts

Before sending anything, the tool looks up a verdict by:

`application + key_field + prompt_version + model_version`

If it exists, the tool reuses it. The verdict record contains the representative document, its hash, the verdict, and `classified_at`. It does not use an Elasticsearch row ID.

### 6.2 Build request groups

Alerts without a reusable verdict are grouped as follows:

1. By `alert_rule_url` when it exists.
2. By `application` when it does not.

Each request contains exactly one group. A request never mixes different rule URLs or fallback applications.

Each request may contain at most 200 alerts. If a group is larger, the tool sorts it deterministically by alert key and creates balanced partitions whose sizes differ by no more than one.

Only fields that are truly identical across every alert in the request may be moved to a shared header. All other fields remain on each alert.

The request carries the group identity, ruleset and prompt versions, shared fields, and an ordered list of alerts. Every alert retains its schema and all non-shared source fields, so the complete representative document can be reconstructed without loss. Alert transport IDs are deterministic hashes of schema, application, and key; batch IDs are deterministic hashes of the run, group, partition, ordered membership, prompt, and model versions. These transport IDs do not replace the approved alert identity or verdict-cache key.

### 6.3 Send and validate

Before calling, the shared assessment path holds a SQL application lock for its scope and
commits the exact prompt/schema provenance, immutable settings, serialized request and attempt
start to the independent LLM audit journal (design 7.13). Live assessment without this journal
is refused. Successful committed responses replay after interruption; a started attempt
without a committed result is recorded as interrupted and consumes its slot. A subsequent
explicit run after three failures opens a new numbered cycle, preserving earlier attempts.

The tool calls the on-prem OpenAI-compatible endpoint through the regular OpenAI Python SDK. It uses Chat Completions with strict JSON-schema output, temperature zero, a configurable timeout, and SDK automatic retries disabled. Each timeout, transport failure, invalid response, or other failed call consumes one of the pipeline's recorded attempts.

The cached prompt prefix contains the approved alerting guides, the R/P catalogue, and the fixed classification procedure. The model reconstructs and judges every alert independently, uses batch neighbours only as context, cites one most-actionable principle with a deterministic tie-break, uses `other` only for clear uncatalogued violations, and defaults to `no_violation` when evidence is ambiguous. The request body contains the losslessly factored representative documents and stable per-alert IDs.

The response is a closed object carrying the echoed batch ID and one verdict per alert ID. Each verdict contains an assessment, one primary principle, a confidence enum, and a required justification of at most 1,000 characters. The batch ID and alert-ID set must exactly match the request; duplicate IDs, invalid enum combinations, empty justifications, and additional fields reject the entire batch. Verdict order does not matter because IDs provide the binding.

Prompt 1.3.0 carries 1.2.0's evidence and applicability guidance plus the R6 catalogue line for ruleset 1.1.0, without changing the guides, catalogue
wording or verdict fields. Validation also rejects v2-only citations on v1, P7 outside v2
critical, R7 on v2 and R4 outside Grafana. Refusal, incomplete and empty SDK responses are
recorded failures; available model/token metadata is retained, with missing usage unknown.

The tool makes at most three attempts in total: the initial request plus two retries. Each retry sends the identical batch as a group. Alerts are never retried individually. If the third attempt fails, the whole batch becomes `unassessed`.

### 6.4 Record outcomes

Each successfully assessed alert receives one verdict:

- A named high-confidence catalogue violation becomes `flagged_by_llm`.
- A medium- or low-confidence catalogue violation enters the review worklist.
- An `other` result enters the review worklist.
- `no_violation` becomes `assessed_good`.
- Exhausted batches become `unassessed`.

LLM findings remain separate and advisory. A prompt/model version can be promoted only after it demonstrates at least 95% precision; a version change resets that evidence.

## 7. Derive the migration phase

The tool derives the selected team's phase from the alerts found in this run:

- No identities in either schema: `no_data`.
- V1 identities and no v2 identities: `phase_0`.
- Identities in both schemas: `phase_1`.
- No v1 identities, at least one v2 identity, and readiness below 100%: `phase_2`.
- No v1 identities, at least one v2 identity, and readiness at 100%: `done`.

The derived phase and v2 readiness percentage are saved with the run. They describe only alerts that fired inside this run's window; the tool cannot infer silent rule inventory.

## 8. Persist the result

This final transaction commits the complete report state atomically. The pre-call journal
already exists independently and survives a failed final transaction; it is not reader-visible.

The tool writes the run to SQL Server as:

- One run record with the team, window, versions, LLM status, phase, and readiness.
- Daily metrics per run, team, schema, and date.
- Daily per-rule counts.
- Alert findings and review-worklist records.
- LLM batch-attempt records.
- Durable LLM verdicts and representative-document hashes.
- Cached panel-query parses.

The stored categories remain explicit: `flagged_by_rule`, `flagged_by_llm`, `needs_review`, `assessed_good`, `unassessed`, and `phase2_gaps`. “Good” is never inferred by subtracting flagged alerts from total alerts.
The run-level identity states are mutually exclusive: `rule_flagged`, `llm_flagged`, `needs_review`, `assessed_good`, and `unassessed`. `needs_review`, `assessed_good`, and `unassessed` are identity counts; phase-2 gaps remain orthogonal.

## 9. Render the team scorecard

The HTML scorecard and CSV exports are generated from SQL Server, not recomputed from Elasticsearch. They show the selected team's:

- Seven-day volume and daily breakdown.
- Deterministic and LLM quality results.
- Assessment coverage and unassessed alerts.
- Rule breakdown and actionable alert worklist.
- Suppression visibility.
- Migration phase and v2 readiness.
- Node-name and key-inflation diagnostics.

The self-contained HTML also shows run/version metadata, supplied panel IDs, rule/principle breakdown, and the limitations of the single-week snapshot. It contains no cross-run comparison, leaderboard, or combined v1/v2 volume conclusion. The three exports are exactly `daily_metrics.csv`, `rule_counts.csv`, and `alert_worklist.csv`. Their ordering is deterministic; CSV retains full values and neutralizes spreadsheet-formula prefixes, while the HTML escapes all alert content. An interactive frontend is post-MVP.

## 10. Publish and read (design section 7.10)

A completed run is not yet visible to anyone outside the standardization team. An operator publishes it as the team's weekly review with `alerts-bi publish`, which refuses a week that overlaps a published one and, unless told otherwise, one that leaves a gap. The GET-only review portal then shows it to internal readers from the `portal_*` views using the same SQL database and login as the pipeline: weekly totals per schema, history across published weeks, the work list and each alert's stored evidence. Operators record human decisions on findings with `alerts-bi decide`; they sit beside the machine findings and never change them.

## 11. The weekly schedule (design section 7.11)

`alerts-bi weekly`, invoked daily, runs steps 1-9 for every team enrolled in the registry, once per completed Monday-to-Monday UTC week that has not been published yet, oldest first, with `run_at` set to the Monday boundary. It then performs step 10's publication itself when the run is healthy. An unhealthy week is held for an operator and the weeks after it are stored but not published; a newly enrolled team starts at its most recent completed week.

## After the MVP

The first next step, an interactive frontend over the persisted runs, is delivered as the read-only review portal (step 10). The second is deterministic historical backfill, processed oldest first and without LLM calls. Later work includes the company-wide unattributed-alert audit, and a cross-team leaderboard. R6 is already built as a core rule (ruleset 1.1.0, judged by firing episodes; design section 7.14).
