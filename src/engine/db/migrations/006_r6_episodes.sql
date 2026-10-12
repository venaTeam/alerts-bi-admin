-- R6 judges firing episodes, not a repeat interval (design 7.14): Grafana's repeat interval
-- is disabled, so a firing alert sends once when it fires and once when it clears.
--
-- Stores the two new per-alert facts and exposes them through portal_alerts. The view is
-- the 005 definition with the two columns appended.

ALTER TABLE alert_findings ADD
    -- The most firing rows in any one episode (a clear row closes an episode).
    max_episode_firing_rows INT NOT NULL
        CONSTRAINT df_alert_findings_max_episode DEFAULT 0,
    -- First firing row of the open episode when the identity's last row is firing, else NULL.
    open_since              DATETIME2(3) NULL;
GO

ALTER TABLE alert_findings ADD
    CONSTRAINT ck_alert_findings_max_episode CHECK (max_episode_firing_rows >= 0);
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
    f.unseen,
    f.max_episode_firing_rows,
    f.open_since
FROM alert_findings AS f
JOIN portal_reviews AS v ON v.run_id = f.run_id;
GO
