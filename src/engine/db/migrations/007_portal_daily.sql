-- The slides' day-by-day charts (team summary, task S2): one row per published week, schema
-- and UTC day bucket. Published weeks only, through portal_reviews, like every portal view.
-- covered_hours marks the partial first and last day of a week that does not start at
-- midnight UTC; weekly reviews start on Monday 00:00 UTC and have seven full days.

CREATE OR ALTER VIEW portal_daily_metrics AS
SELECT
    v.run_id,
    d.alert_schema,
    d.snapshot_date,
    d.covered_hours,
    d.distinct_alerts,
    d.flagged_by_rule_distinct
FROM portal_reviews AS v
JOIN daily_metrics AS d ON d.run_id = v.run_id;
GO

IF DATABASE_PRINCIPAL_ID('alerts_bi_reader') IS NOT NULL
    GRANT SELECT ON portal_daily_metrics TO alerts_bi_reader;
GO
