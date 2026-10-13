-- Additive read models. Existing evidence and exports remain authoritative.
CREATE TABLE run_summaries (
    run_id NVARCHAR(64) NOT NULL,
    scope_kind NVARCHAR(16) NOT NULL,
    application NVARCHAR(256) NOT NULL,
    payload NVARCHAR(MAX) NOT NULL,
    CONSTRAINT pk_run_summaries PRIMARY KEY(run_id, scope_kind, application),
    CONSTRAINT fk_run_summaries_run FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE,
    CONSTRAINT ck_run_summaries_json CHECK(ISJSON(payload)=1)
);
GO
SELECT TOP 0 IDENTITY(BIGINT,1,1) AS finding_id,
    run_id, alert_schema, application, key_field,
    CAST(LEFT(message, 512) AS NVARCHAR(512)) AS message,
    severity, provider, alert_rule_url, component, node_name, row_count,
    first_seen, last_seen, quality_state, core_rule_ids, readiness_rule_ids,
    llm_principle_id, llm_confidence, clear_count, max_clear_cycles_24h,
    fire_pattern, unseen, max_episode_firing_rows, open_since,
    CAST(0 AS INT) AS attention_rank
INTO run_alert_list FROM alert_findings;
ALTER TABLE run_alert_list ADD CONSTRAINT pk_run_alert_list PRIMARY KEY(finding_id);
ALTER TABLE run_alert_list ADD CONSTRAINT fk_run_alert_list_run
    FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE;
CREATE INDEX ix_run_alert_list_order ON run_alert_list(run_id, attention_rank, row_count DESC, alert_schema, application, key_field);
CREATE INDEX ix_run_alert_list_application ON run_alert_list(run_id, application, alert_schema, row_count DESC, finding_id);
CREATE INDEX ix_run_alert_list_volume ON run_alert_list(run_id, alert_schema, row_count DESC, finding_id);
CREATE INDEX ix_run_alert_list_events ON run_alert_list(run_id, row_count DESC, application, key_field, alert_schema);
GO
CREATE TABLE run_rule_membership (
    finding_id BIGINT NOT NULL,
    run_id NVARCHAR(64) NOT NULL,
    rule_id NVARCHAR(8) NOT NULL,
    matched_rows BIGINT NOT NULL,
    CONSTRAINT pk_run_rule_membership PRIMARY KEY(finding_id, rule_id),
    CONSTRAINT fk_rule_membership_finding FOREIGN KEY(finding_id)
        REFERENCES run_alert_list(finding_id) ON DELETE CASCADE
);
CREATE INDEX ix_run_rule_membership_filter ON run_rule_membership(run_id, rule_id, finding_id);
GO
CREATE TABLE run_application_daily (
    run_id NVARCHAR(64) NOT NULL,
    alert_schema NVARCHAR(2) NOT NULL,
    application NVARCHAR(256) NOT NULL,
    snapshot_date DATE NOT NULL,
    covered_hours DECIMAL(9,4) NOT NULL,
    events BIGINT NOT NULL,
    distinct_alerts BIGINT NOT NULL,
    flagged_rows BIGINT NOT NULL,
    flagged_by_rule_distinct BIGINT NOT NULL,
    suppressed BIGINT NOT NULL,
    unseen BIGINT NULL,
    CONSTRAINT pk_run_application_daily PRIMARY KEY(run_id, alert_schema, application, snapshot_date),
    CONSTRAINT fk_application_daily_run FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);
GO
CREATE TABLE run_v1_rules (
    run_id NVARCHAR(64) NOT NULL,
    application NVARCHAR(256) NOT NULL,
    rule_hash BINARY(32) NOT NULL,
    CONSTRAINT pk_run_v1_rules PRIMARY KEY(run_id, application, rule_hash),
    CONSTRAINT fk_v1_rules_run FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);
GO
CREATE INDEX ix_weekly_review_log_run ON weekly_review_log(run_id, log_id DESC);
CREATE INDEX ix_finding_decisions_run ON finding_decisions(run_id, decided_at DESC, decision_id DESC);
GO
CREATE VIEW portal_run_summaries AS
SELECT s.run_id, s.scope_kind, s.application, s.payload FROM run_summaries s
JOIN portal_reviews r ON r.run_id=s.run_id;
GO
CREATE VIEW portal_alert_list AS
SELECT s.* FROM run_alert_list s JOIN portal_reviews r ON r.run_id=s.run_id;
GO
CREATE VIEW portal_rule_membership AS
SELECT s.finding_id,s.run_id,s.rule_id,s.matched_rows FROM run_rule_membership s
JOIN portal_reviews r ON r.run_id=s.run_id;
GO
CREATE VIEW portal_application_daily AS
SELECT s.* FROM run_application_daily s JOIN portal_reviews r ON r.run_id=s.run_id;
GO
CREATE VIEW portal_v1_rules AS
SELECT s.* FROM run_v1_rules s JOIN portal_reviews r ON r.run_id=s.run_id;
GO
IF DATABASE_PRINCIPAL_ID('alerts_bi_reader') IS NOT NULL BEGIN
    GRANT SELECT ON portal_run_summaries TO alerts_bi_reader;
    GRANT SELECT ON portal_alert_list TO alerts_bi_reader;
    GRANT SELECT ON portal_rule_membership TO alerts_bi_reader;
    GRANT SELECT ON portal_application_daily TO alerts_bi_reader;
    GRANT SELECT ON portal_v1_rules TO alerts_bi_reader;
END;
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
    SELECT s.run_id,CAST(j.[key] AS NVARCHAR(2)) AS alert_schema,
           TRY_CONVERT(BIGINT,JSON_VALUE(j.value,'$.distinct_alerts')) AS distinct_alerts,
           TRY_CONVERT(BIGINT,JSON_VALUE(j.value,'$.readiness_gaps')) AS readiness_gaps,
           TRY_CONVERT(BIGINT,JSON_VALUE(j.value,'$.unseen_alerts')) AS unseen_alerts,
           COALESCE(TRY_CONVERT(BIGINT,JSON_VALUE(j.value,'$.states.rule_flagged')),0) AS rule_flagged,
           COALESCE(TRY_CONVERT(BIGINT,JSON_VALUE(j.value,'$.states.llm_flagged')),0) AS llm_flagged,
           COALESCE(TRY_CONVERT(BIGINT,JSON_VALUE(j.value,'$.states.needs_review')),0) AS needs_review,
           COALESCE(TRY_CONVERT(BIGINT,JSON_VALUE(j.value,'$.states.assessed_good')),0) AS assessed_good,
           COALESCE(TRY_CONVERT(BIGINT,JSON_VALUE(j.value,'$.states.unassessed')),0) AS unassessed,
           COALESCE(TRY_CONVERT(BIGINT,JSON_VALUE(j.value,'$.needs_attention')),0) AS needs_attention,
           COALESCE(TRY_CONVERT(BIGINT,JSON_VALUE(j.value,'$.patterns.stuck[0]')),0)+COALESCE(TRY_CONVERT(BIGINT,JSON_VALUE(j.value,'$.patterns.spamming[0]')),0)+COALESCE(TRY_CONVERT(BIGINT,JSON_VALUE(j.value,'$.patterns.flapping[0]')),0) AS r6_alerts
    FROM run_summaries s CROSS APPLY OPENJSON(s.payload,'$.schemas') j
    WHERE s.scope_kind='team'
) AS af ON af.run_id = v.run_id AND af.alert_schema = s.alert_schema;
GO

CREATE OR ALTER VIEW portal_rule_totals AS
SELECT s.run_id, CAST(j.[key] AS NVARCHAR(2)) AS alert_schema,CAST(r.[key] AS NVARCHAR(8)) AS rule_id,
       TRY_CONVERT(BIGINT,JSON_VALUE(r.value,'$[0]')) AS events,
       TRY_CONVERT(BIGINT,JSON_VALUE(r.value,'$[1]')) AS alerts
FROM portal_run_summaries s
CROSS APPLY OPENJSON(s.payload,'$.schemas') j
CROSS APPLY OPENJSON(j.value,'$.rules') r
WHERE s.scope_kind='team';
GO
