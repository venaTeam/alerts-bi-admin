-- Team summary (spec docs/superpowers/specs/2026-10-01-team-summary-design.md).
--
-- Adds the R6 firing facts and the `unseen` visibility measure to the store, and exposes
-- what the reader portal's Summary section needs through the portal_* views. No base table
-- is exposed and no version field reaches a view: `basis_changed` only says that the
-- measurement basis moved between two published weeks, never what it moved to.

-- -------------------------------------------------------------- daily metrics

ALTER TABLE daily_metrics ADD
    -- Rows hidden from every one of the team's panels by identity narrowing. NULL when the
    -- schema has no supplied panel: no dashboard is known, so nothing can be claimed.
    unseen            INT NULL,
    -- Identity leaves that could not be evaluated, allocated to the schema's first bucket
    -- like suppression_unmeasured (design section 7.6).
    unseen_unmeasured INT NULL;
GO

ALTER TABLE daily_metrics ADD
    CONSTRAINT ck_daily_metrics_unseen CHECK (unseen IS NULL OR unseen >= 0),
    -- Suppressed and unseen are disjoint by construction, so together they cannot exceed
    -- the rows in the bucket.
    CONSTRAINT ck_daily_metrics_visibility CHECK (suppressed + ISNULL(unseen, 0) <= alerts);
GO

-- ------------------------------------------------------------- alert findings

ALTER TABLE alert_findings ADD
    -- R6 facts: persisted as facts so history can be re-scored when thresholds change.
    clear_count          INT          NOT NULL CONSTRAINT df_alert_findings_clear_count DEFAULT 0,
    max_clear_cycles_24h INT          NOT NULL CONSTRAINT df_alert_findings_max_cycles DEFAULT 0,
    fire_pattern         NVARCHAR(16) NULL,
    -- The identity has at least one row no panel shows. NULL when no panel was supplied.
    unseen               BIT          NULL;
GO

ALTER TABLE alert_findings ADD
    CONSTRAINT ck_alert_findings_fire_pattern CHECK (
        fire_pattern IS NULL OR fire_pattern IN ('stuck', 'spamming', 'flapping')
    ),
    CONSTRAINT ck_alert_findings_cycles CHECK (
        clear_count >= 0 AND max_clear_cycles_24h >= 0 AND max_clear_cycles_24h <= clear_count
    );
GO

-- ---------------------------------------------------------------- portal views

-- basis_changed is 1 when this published week was measured under a different ruleset or
-- registry version than the team's previous published week, so the time-to-v2 estimate
-- restarts there. v1_rule_effort_days is the team's optional planning override, read from
-- the registry snapshot the run stored; the snapshot itself never reaches a view.
CREATE OR ALTER VIEW portal_reviews AS
SELECT
    p.publication_id,
    p.team_id,
    r.team_display_name,
    p.run_id,
    p.window_start,
    p.window_end,
    p.published_at,
    p.review_note,
    r.phase_derived,
    r.phase2_readiness_pct,
    CASE
        WHEN LAG(r.ruleset_version) OVER (PARTITION BY p.team_id ORDER BY p.window_end) IS NULL
            THEN CAST(0 AS BIT)
        WHEN LAG(r.ruleset_version) OVER (PARTITION BY p.team_id ORDER BY p.window_end)
                 <> r.ruleset_version
          OR LAG(r.registry_version) OVER (PARTITION BY p.team_id ORDER BY p.window_end)
                 <> r.registry_version
            THEN CAST(1 AS BIT)
        ELSE CAST(0 AS BIT)
    END AS basis_changed,
    CASE WHEN ISJSON(r.registry_entry_snapshot) = 1
         THEN TRY_CONVERT(DECIMAL(6, 2),
                          JSON_VALUE(r.registry_entry_snapshot, '$.planning.v1_rule_effort_days'))
    END AS v1_rule_effort_days
FROM review_publications AS p
JOIN runs AS r ON r.run_id = p.run_id
WHERE p.withdrawn_at IS NULL
  AND r.status = 'completed';
GO

CREATE OR ALTER VIEW portal_schema_totals AS
SELECT
    v.run_id,
    s.alert_schema,
    COALESCE(dm.events, 0)             AS events,
    COALESCE(dm.flagged_rows, 0)       AS flagged_rows,
    COALESCE(dm.suppressed, 0)         AS suppressed,
    dm.unseen                          AS unseen,
    COALESCE(af.distinct_alerts, 0)    AS distinct_alerts,
    COALESCE(af.rule_flagged, 0)       AS rule_flagged,
    COALESCE(af.llm_flagged, 0)        AS llm_flagged,
    COALESCE(af.needs_review, 0)       AS needs_review,
    COALESCE(af.assessed_good, 0)      AS assessed_good,
    COALESCE(af.unassessed, 0)         AS unassessed,
    COALESCE(af.readiness_gaps, 0)     AS readiness_gaps,
    COALESCE(af.needs_attention, 0)    AS needs_attention,
    af.unseen_alerts                   AS unseen_alerts,
    COALESCE(af.r6_alerts, 0)          AS r6_alerts
FROM portal_reviews AS v
CROSS JOIN (VALUES (N'v1'), (N'v2')) AS s (alert_schema)
LEFT JOIN (
    SELECT run_id, alert_schema,
           SUM(alerts) AS events,
           SUM(flagged_by_rule) AS flagged_rows,
           SUM(suppressed) AS suppressed,
           -- NULL stays NULL when no bucket measured it: no panel, nothing claimed.
           SUM(unseen) AS unseen
    FROM daily_metrics
    GROUP BY run_id, alert_schema
) AS dm ON dm.run_id = v.run_id AND dm.alert_schema = s.alert_schema
LEFT JOIN (
    SELECT run_id, alert_schema,
           COUNT(*) AS distinct_alerts,
           SUM(CASE WHEN quality_state = 'rule_flagged' THEN 1 ELSE 0 END) AS rule_flagged,
           SUM(CASE WHEN quality_state = 'llm_flagged' THEN 1 ELSE 0 END) AS llm_flagged,
           SUM(CASE WHEN quality_state = 'needs_review' THEN 1 ELSE 0 END) AS needs_review,
           SUM(CASE WHEN quality_state = 'assessed_good' THEN 1 ELSE 0 END) AS assessed_good,
           SUM(CASE WHEN quality_state = 'unassessed' THEN 1 ELSE 0 END) AS unassessed,
           SUM(CASE WHEN readiness_rule_ids <> '' THEN 1 ELSE 0 END) AS readiness_gaps,
           SUM(CASE WHEN quality_state IN ('rule_flagged', 'llm_flagged', 'needs_review')
                      OR readiness_rule_ids <> '' THEN 1 ELSE 0 END) AS needs_attention,
           SUM(CASE WHEN unseen = 1 THEN 1 WHEN unseen = 0 THEN 0 END) AS unseen_alerts,
           SUM(CASE WHEN fire_pattern IS NOT NULL THEN 1 ELSE 0 END) AS r6_alerts
    FROM alert_findings
    GROUP BY run_id, alert_schema
) AS af ON af.run_id = v.run_id AND af.alert_schema = s.alert_schema;
GO

CREATE OR ALTER VIEW portal_alerts AS
SELECT
    f.run_id,
    f.alert_schema,
    f.application,
    f.key_field,
    f.message,
    f.severity,
    f.component,
    f.node_name,
    f.environment,
    f.provider,
    f.alert_rule_url,
    f.row_count,
    f.first_seen,
    f.last_seen,
    f.representative_at,
    f.core_rule_ids,
    f.readiness_rule_ids,
    f.findings_evidence,
    f.quality_state,
    f.llm_principle_id,
    f.llm_confidence,
    f.llm_justification,
    CASE WHEN ISJSON(f.representative_doc) = 1
         THEN JSON_VALUE(f.representative_doc, '$.impact') END AS impact,
    CASE WHEN ISJSON(f.representative_doc) = 1
         THEN JSON_VALUE(f.representative_doc, '$.runbook_url') END AS runbook_url,
    CASE WHEN ISJSON(f.representative_doc) = 1
         THEN JSON_VALUE(f.representative_doc, '$.status') END AS alert_status,
    CASE WHEN ISJSON(f.representative_doc) = 1
         THEN JSON_VALUE(f.representative_doc, '$.time_created') END AS time_created,
    CASE
        WHEN f.quality_state = 'rule_flagged' THEN 0
        WHEN f.quality_state = 'llm_flagged' THEN 1
        WHEN f.quality_state = 'needs_review' THEN 2
        WHEN f.readiness_rule_ids <> '' THEN 3
        ELSE 4
    END AS attention_rank,
    f.clear_count,
    f.max_clear_cycles_24h,
    f.fire_pattern,
    f.unseen
FROM alert_findings AS f
JOIN portal_reviews AS v ON v.run_id = f.run_id;
GO

-- One row per published week, schema and rule: events (matching raw rows, summed over the
-- week's buckets) and alerts (distinct identities in the whole week carrying that rule).
-- Daily distinct counts are not summed, because an identity on two dates would count twice.
-- ruleset_version is deliberately absent.
CREATE OR ALTER VIEW portal_rule_totals AS
SELECT
    v.run_id,
    ev.alert_schema,
    ev.rule_id,
    ev.events,
    COALESCE(al.alerts, 0) AS alerts
FROM portal_reviews AS v
JOIN (
    SELECT run_id, alert_schema, rule_id, SUM(match_count) AS events
    FROM daily_rule_counts
    GROUP BY run_id, alert_schema, rule_id
) AS ev ON ev.run_id = v.run_id
LEFT JOIN (
    SELECT f.run_id, f.alert_schema, LTRIM(RTRIM(s.value)) AS rule_id, COUNT(*) AS alerts
    FROM alert_findings AS f
    CROSS APPLY STRING_SPLIT(
        f.core_rule_ids
        + CASE WHEN f.core_rule_ids <> '' AND f.readiness_rule_ids <> '' THEN ',' ELSE '' END
        + f.readiness_rule_ids, ',') AS s
    WHERE LTRIM(RTRIM(s.value)) <> ''
    GROUP BY f.run_id, f.alert_schema, LTRIM(RTRIM(s.value))
) AS al ON al.run_id = ev.run_id AND al.alert_schema = ev.alert_schema AND al.rule_id = ev.rule_id;
GO

IF DATABASE_PRINCIPAL_ID('alerts_bi_reader') IS NOT NULL
    GRANT SELECT ON portal_rule_totals TO alerts_bi_reader;
