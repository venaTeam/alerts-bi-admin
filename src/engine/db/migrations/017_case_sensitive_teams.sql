-- Match exact registry team IDs independently of the database collation.
-- No row or applied migration is rewritten. SQL still ignores trailing spaces;
-- the registry rejects surrounding whitespace before querying or persisting.
-- Index definitions below are unchanged from revisions 001/002/003/010/014.
-- The Alembic transaction rolls back all DDL and the ledger on failure.
DECLARE @drop_stats NVARCHAR(MAX) = N'';
SELECT @drop_stats = @drop_stats + N'DROP STATISTICS '
    + QUOTENAME(OBJECT_NAME(s.object_id)) COLLATE DATABASE_DEFAULT + N'.'
    + QUOTENAME(s.name) COLLATE DATABASE_DEFAULT + N';'
FROM sys.stats AS s
JOIN sys.stats_columns AS sc ON sc.object_id = s.object_id AND sc.stats_id = s.stats_id
JOIN sys.columns AS c ON c.object_id = sc.object_id AND c.column_id = sc.column_id
WHERE c.name = N'team_id'
  AND OBJECT_NAME(s.object_id) IN (
      N'runs', N'daily_metrics', N'daily_rule_counts', N'review_publications',
      N'finding_decisions', N'weekly_review_log', N'run_jobs', N'run_backfills')
  AND NOT EXISTS (
      SELECT 1 FROM sys.indexes AS i WHERE i.object_id = s.object_id AND i.index_id = s.stats_id);
EXEC sys.sp_executesql @drop_stats;
GO

DROP INDEX ix_runs_team_run_at ON runs;
DROP INDEX ux_review_publications_current_week ON review_publications;
DROP INDEX ix_review_publications_team ON review_publications;
DROP INDEX ix_finding_decisions_team ON finding_decisions;
DROP INDEX ix_weekly_review_log_team ON weekly_review_log;
DROP INDEX ix_run_jobs_team ON run_jobs;
DROP INDEX ix_run_backfills_team ON run_backfills;
GO

ALTER TABLE runs ALTER COLUMN team_id NVARCHAR(128) COLLATE Latin1_General_100_BIN2 NOT NULL;
ALTER TABLE daily_metrics ALTER COLUMN team_id NVARCHAR(128) COLLATE Latin1_General_100_BIN2 NOT NULL;
ALTER TABLE daily_rule_counts ALTER COLUMN team_id NVARCHAR(128) COLLATE Latin1_General_100_BIN2 NOT NULL;
ALTER TABLE review_publications ALTER COLUMN team_id NVARCHAR(128) COLLATE Latin1_General_100_BIN2 NOT NULL;
ALTER TABLE finding_decisions ALTER COLUMN team_id NVARCHAR(128) COLLATE Latin1_General_100_BIN2 NOT NULL;
ALTER TABLE weekly_review_log ALTER COLUMN team_id NVARCHAR(128) COLLATE Latin1_General_100_BIN2 NOT NULL;
ALTER TABLE run_jobs ALTER COLUMN team_id NVARCHAR(128) COLLATE Latin1_General_100_BIN2 NOT NULL;
ALTER TABLE run_backfills ALTER COLUMN team_id NVARCHAR(128) COLLATE Latin1_General_100_BIN2 NOT NULL;
GO

CREATE INDEX ix_runs_team_run_at ON runs (team_id, run_at DESC);
CREATE UNIQUE INDEX ux_review_publications_current_week ON review_publications (team_id, window_start) WHERE withdrawn_at IS NULL;
CREATE INDEX ix_review_publications_team ON review_publications (team_id, window_end);
CREATE INDEX ix_finding_decisions_team ON finding_decisions (team_id, decided_at);
CREATE INDEX ix_weekly_review_log_team ON weekly_review_log (team_id, invoked_at);
CREATE INDEX ix_run_jobs_team ON run_jobs(team_id, submitted_at DESC);
CREATE INDEX ix_run_backfills_team ON run_backfills(team_id, submitted_at DESC);
GO

-- Refresh exposed team identity and dependent view metadata; reader grants survive.
EXEC sys.sp_refreshview N'portal_reviews';
EXEC sys.sp_refreshview N'portal_decisions';
EXEC sys.sp_refreshview N'portal_schema_totals';
EXEC sys.sp_refreshview N'portal_alerts';
EXEC sys.sp_refreshview N'portal_daily_metrics';
EXEC sys.sp_refreshview N'portal_run_summaries';
EXEC sys.sp_refreshview N'portal_alert_list';
EXEC sys.sp_refreshview N'portal_rule_membership';
EXEC sys.sp_refreshview N'portal_application_daily';
EXEC sys.sp_refreshview N'portal_v1_rules';
GO
