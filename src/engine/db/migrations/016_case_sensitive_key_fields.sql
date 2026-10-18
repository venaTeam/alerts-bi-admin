-- Exact key_field identity in storage, independent of the database collation.
--
-- An alert's identity is the exact application + key_field pair (design section 3.7).
-- Migration 015 made every stored application column binary, but key_field kept the
-- database's case-insensitive default collation. Two keys differing only in case within
-- one exact application, such as node-1 and Node-1, were therefore separate in memory and
-- one value in SQL: alert_findings rejected the second as a duplicate key and the whole run
-- failed to persist, and llm_verdicts kept a verdict for only one of the spellings. Binary
-- collation makes SQL agree with the engine.
--
-- Existing rows cannot hold case-only duplicates, because the old keys forbade them, so
-- every key below rebuilds without conflict. Applied revisions are not edited. The keys
-- and indexes recreated here are the 015 definitions (from 001, 002 and 012) verbatim.
-- SQL Server equality still ignores trailing spaces under a binary collation.

-- Column statistics block a collation change. Auto-created names are not deterministic.
DECLARE @drop_stats NVARCHAR(MAX) = N'';
SELECT @drop_stats = @drop_stats + N'DROP STATISTICS '
    + QUOTENAME(OBJECT_NAME(s.object_id)) COLLATE DATABASE_DEFAULT + N'.'
    + QUOTENAME(s.name) COLLATE DATABASE_DEFAULT + N';'
FROM sys.stats AS s
JOIN sys.stats_columns AS sc ON sc.object_id = s.object_id AND sc.stats_id = s.stats_id
JOIN sys.columns AS c ON c.object_id = sc.object_id AND c.column_id = sc.column_id
WHERE c.name = N'key_field'
  AND OBJECT_NAME(s.object_id) IN (
      N'alert_findings', N'llm_verdicts', N'finding_decisions', N'run_alert_list')
  AND NOT EXISTS (
      SELECT 1 FROM sys.indexes AS i WHERE i.object_id = s.object_id AND i.index_id = s.stats_id);
EXEC sys.sp_executesql @drop_stats;
GO

ALTER TABLE alert_findings DROP CONSTRAINT pk_alert_findings;
ALTER TABLE llm_verdicts DROP CONSTRAINT pk_llm_verdicts;
DROP INDEX ix_finding_decisions_identity ON finding_decisions;
DROP INDEX ix_run_alert_list_order ON run_alert_list;
DROP INDEX ix_run_alert_list_events ON run_alert_list;
GO

ALTER TABLE alert_findings ALTER COLUMN key_field NVARCHAR(512) COLLATE Latin1_General_100_BIN2 NOT NULL;
ALTER TABLE llm_verdicts ALTER COLUMN key_field NVARCHAR(512) COLLATE Latin1_General_100_BIN2 NOT NULL;
ALTER TABLE finding_decisions ALTER COLUMN key_field NVARCHAR(512) COLLATE Latin1_General_100_BIN2 NOT NULL;
ALTER TABLE run_alert_list ALTER COLUMN key_field NVARCHAR(512) COLLATE Latin1_General_100_BIN2 NOT NULL;
GO

ALTER TABLE alert_findings ADD CONSTRAINT pk_alert_findings
    PRIMARY KEY (run_id, alert_schema, application, key_field);
ALTER TABLE llm_verdicts ADD CONSTRAINT pk_llm_verdicts
    PRIMARY KEY (application, key_field, prompt_version, model_version);
CREATE INDEX ix_finding_decisions_identity
    ON finding_decisions (alert_schema, application, key_field, finding_id, decided_at);
CREATE INDEX ix_run_alert_list_order ON run_alert_list(run_id, attention_rank, row_count DESC, alert_schema, application, key_field);
CREATE INDEX ix_run_alert_list_events ON run_alert_list(run_id, row_count DESC, application, key_field, alert_schema);
GO

-- Views record their columns' collation when created. Refresh the views that expose
-- key_field so readers see the binary collation too; sp_refreshview keeps the reader grants.
EXEC sys.sp_refreshview N'portal_alerts';
EXEC sys.sp_refreshview N'portal_decisions';
EXEC sys.sp_refreshview N'portal_alert_list';
GO
