-- Exact application identity in storage, independent of the database collation.
--
-- The engine keys an alert on the exact application + key_field strings (design section
-- 3.7), but every stored application column inherited the database's case-insensitive
-- default collation. Two applications differing only in case, such as Moby and moby, in
-- one run were therefore separate in memory and one value in SQL: run_summaries,
-- run_application_daily and alert_findings rejected the second as a duplicate key and the
-- whole run failed to persist. Binary collation makes SQL agree with the engine.
--
-- Existing rows cannot hold case-only duplicates, because the old keys forbade them, so
-- every key below rebuilds without conflict. Applied revisions are not edited. The
-- definitions recreated here are the 001, 002 and 012 definitions verbatim.
-- llm_batch_attempts.group_value is audit text, never a key or join, and is unchanged.

-- Column statistics block a collation change. Auto-created names are not deterministic.
DECLARE @drop_stats NVARCHAR(MAX) = N'';
SELECT @drop_stats = @drop_stats + N'DROP STATISTICS '
    + QUOTENAME(OBJECT_NAME(s.object_id)) COLLATE DATABASE_DEFAULT + N'.'
    + QUOTENAME(s.name) COLLATE DATABASE_DEFAULT + N';'
FROM sys.stats AS s
JOIN sys.stats_columns AS sc ON sc.object_id = s.object_id AND sc.stats_id = s.stats_id
JOIN sys.columns AS c ON c.object_id = sc.object_id AND c.column_id = sc.column_id
WHERE c.name = N'application'
  AND OBJECT_NAME(s.object_id) IN (
      N'alert_findings', N'llm_verdicts', N'finding_decisions', N'run_alert_list',
      N'run_summaries', N'run_application_daily', N'run_v1_rules')
  AND NOT EXISTS (
      SELECT 1 FROM sys.indexes AS i WHERE i.object_id = s.object_id AND i.index_id = s.stats_id);
EXEC sys.sp_executesql @drop_stats;
GO

ALTER TABLE alert_findings DROP CONSTRAINT pk_alert_findings;
ALTER TABLE llm_verdicts DROP CONSTRAINT pk_llm_verdicts;
ALTER TABLE run_summaries DROP CONSTRAINT pk_run_summaries;
ALTER TABLE run_application_daily DROP CONSTRAINT pk_run_application_daily;
ALTER TABLE run_v1_rules DROP CONSTRAINT pk_run_v1_rules;
DROP INDEX ix_finding_decisions_identity ON finding_decisions;
DROP INDEX ix_run_alert_list_order ON run_alert_list;
DROP INDEX ix_run_alert_list_application ON run_alert_list;
DROP INDEX ix_run_alert_list_events ON run_alert_list;
GO

ALTER TABLE alert_findings ALTER COLUMN application NVARCHAR(256) COLLATE Latin1_General_100_BIN2 NOT NULL;
ALTER TABLE llm_verdicts ALTER COLUMN application NVARCHAR(256) COLLATE Latin1_General_100_BIN2 NOT NULL;
ALTER TABLE finding_decisions ALTER COLUMN application NVARCHAR(256) COLLATE Latin1_General_100_BIN2 NOT NULL;
ALTER TABLE run_alert_list ALTER COLUMN application NVARCHAR(256) COLLATE Latin1_General_100_BIN2 NOT NULL;
ALTER TABLE run_summaries ALTER COLUMN application NVARCHAR(256) COLLATE Latin1_General_100_BIN2 NOT NULL;
ALTER TABLE run_application_daily ALTER COLUMN application NVARCHAR(256) COLLATE Latin1_General_100_BIN2 NOT NULL;
ALTER TABLE run_v1_rules ALTER COLUMN application NVARCHAR(256) COLLATE Latin1_General_100_BIN2 NOT NULL;
GO

ALTER TABLE alert_findings ADD CONSTRAINT pk_alert_findings
    PRIMARY KEY (run_id, alert_schema, application, key_field);
ALTER TABLE llm_verdicts ADD CONSTRAINT pk_llm_verdicts
    PRIMARY KEY (application, key_field, prompt_version, model_version);
ALTER TABLE run_summaries ADD CONSTRAINT pk_run_summaries
    PRIMARY KEY (run_id, scope_kind, application);
ALTER TABLE run_application_daily ADD CONSTRAINT pk_run_application_daily
    PRIMARY KEY (run_id, alert_schema, application, snapshot_date);
ALTER TABLE run_v1_rules ADD CONSTRAINT pk_run_v1_rules
    PRIMARY KEY (run_id, application, rule_hash);
CREATE INDEX ix_finding_decisions_identity
    ON finding_decisions (alert_schema, application, key_field, finding_id, decided_at);
CREATE INDEX ix_run_alert_list_order ON run_alert_list(run_id, attention_rank, row_count DESC, alert_schema, application, key_field);
CREATE INDEX ix_run_alert_list_application ON run_alert_list(run_id, application, alert_schema, row_count DESC, finding_id);
CREATE INDEX ix_run_alert_list_events ON run_alert_list(run_id, row_count DESC, application, key_field, alert_schema);
GO

-- Views record their columns' collation when created. Refresh them so readers see the
-- binary collation too; sp_refreshview keeps the reader grants.
EXEC sys.sp_refreshview N'portal_alerts';
EXEC sys.sp_refreshview N'portal_decisions';
EXEC sys.sp_refreshview N'portal_alert_list';
EXEC sys.sp_refreshview N'portal_application_daily';
EXEC sys.sp_refreshview N'portal_run_summaries';
EXEC sys.sp_refreshview N'portal_v1_rules';
EXEC sys.sp_refreshview N'portal_rule_totals';
EXEC sys.sp_refreshview N'portal_schema_totals';
GO
