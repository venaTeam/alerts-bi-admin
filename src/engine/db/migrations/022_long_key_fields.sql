-- Store key_field values up to 800 characters (design "Key fields up to 800 characters",
-- 2026-10-28).
--
-- Every stored key_field was NVARCHAR(512), so a run with a longer key failed to persist and
-- rolled back. Sending teams' keys reach 800 characters. alert_findings, llm_verdicts,
-- finding_decisions and run_alert_list now store NVARCHAR(800), still binary-collated
-- (migration 016) and NOT NULL. application stays NVARCHAR(256).
--
-- The computed identity_hash (migration 018) reads key_field, which blocks ALTER COLUMN, so
-- both hashed primary keys and the hash columns are dropped and recreated with the 018
-- definitions verbatim. The hash covers the value, not the declared width, so every row keeps
-- its hash and the rebuilt keys cannot conflict.
--
-- Three nonclustered indexes held key_field in their keys. At 800 characters beside a
-- 256-character application those keys reach 2,252 bytes, past SQL Server's 1,700-byte limit
-- for a nonclustered index entry, so an insert of a long identity would fail with error 1946.
-- They keep their other key columns in order and carry key_field as an included column, which
-- has no size limit. Work lists still order by key_field and return the same rows in the same
-- order; SQL Server sorts the ties among the remaining key columns instead of reading them in
-- index order.
--
-- The three views that expose key_field are refreshed so readers see the new width;
-- sp_refreshview keeps the reader grants. Applied revisions are not edited. The Alembic
-- transaction rolls back all DDL and the ledger on failure.

ALTER TABLE alert_findings DROP CONSTRAINT pk_alert_findings;
ALTER TABLE llm_verdicts DROP CONSTRAINT pk_llm_verdicts;
DROP INDEX ix_finding_decisions_identity ON finding_decisions;
DROP INDEX ix_run_alert_list_order ON run_alert_list;
DROP INDEX ix_run_alert_list_events ON run_alert_list;
GO

ALTER TABLE alert_findings DROP COLUMN identity_hash;
ALTER TABLE llm_verdicts DROP COLUMN identity_hash;
GO

ALTER TABLE alert_findings ALTER COLUMN key_field NVARCHAR(800) COLLATE Latin1_General_100_BIN2 NOT NULL;
ALTER TABLE llm_verdicts ALTER COLUMN key_field NVARCHAR(800) COLLATE Latin1_General_100_BIN2 NOT NULL;
ALTER TABLE finding_decisions ALTER COLUMN key_field NVARCHAR(800) COLLATE Latin1_General_100_BIN2 NOT NULL;
ALTER TABLE run_alert_list ALTER COLUMN key_field NVARCHAR(800) COLLATE Latin1_General_100_BIN2 NOT NULL;
GO

ALTER TABLE alert_findings ADD identity_hash AS CAST(HASHBYTES('SHA2_256',
    CAST(CAST(DATALENGTH(RTRIM(application)) AS INT) AS BINARY(4))
    + CAST(RTRIM(application) AS VARBINARY(MAX))
    + CAST(RTRIM(key_field) AS VARBINARY(MAX))) AS BINARY(32)) PERSISTED NOT NULL;
ALTER TABLE llm_verdicts ADD identity_hash AS CAST(HASHBYTES('SHA2_256',
    CAST(CAST(DATALENGTH(RTRIM(application)) AS INT) AS BINARY(4))
    + CAST(RTRIM(application) AS VARBINARY(MAX))
    + CAST(RTRIM(key_field) AS VARBINARY(MAX))) AS BINARY(32)) PERSISTED NOT NULL;
GO

ALTER TABLE alert_findings ADD CONSTRAINT pk_alert_findings
    PRIMARY KEY (run_id, alert_schema, identity_hash);
ALTER TABLE llm_verdicts ADD CONSTRAINT pk_llm_verdicts
    PRIMARY KEY (identity_hash, prompt_version, model_version);
CREATE INDEX ix_finding_decisions_identity
    ON finding_decisions (alert_schema, application, finding_id, decided_at) INCLUDE (key_field);
CREATE INDEX ix_run_alert_list_order ON run_alert_list(run_id, attention_rank, row_count DESC, alert_schema, application) INCLUDE (key_field);
CREATE INDEX ix_run_alert_list_events ON run_alert_list(run_id, row_count DESC, application, alert_schema) INCLUDE (key_field);
GO

EXEC sys.sp_refreshview N'portal_alerts';
EXEC sys.sp_refreshview N'portal_decisions';
EXEC sys.sp_refreshview N'portal_alert_list';
GO
