-- Key alert identities by a fixed-width hash, so every identity the columns hold can be stored.
--
-- pk_alert_findings (run_id, alert_schema, application, key_field) and pk_llm_verdicts
-- (application, key_field, prompt_version, model_version) were clustered keys over the
-- NVARCHAR(256) application and NVARCHAR(512) key_field: up to 1,668 and 1,856 bytes, against
-- SQL Server's 900-byte limit for a clustered index entry. SQL Server created them with only a
-- warning, so the limit surfaced at insert time: beside a 64-character run id, any identity
-- whose application and key_field together exceed 384 characters failed with error 1946, and
-- persist_run rolled the whole run back.
--
-- identity_hash is SHA-256 over the identity as those keys compared it: binary collation
-- (migrations 015 and 016) with SQL Server's trailing-space padding, so each part is RTRIMmed.
-- The application's byte length prefixes the input, so no two pairs share one. The new keys
-- therefore accept and reject exactly the pairs the old keys did, at 164 and 352 bytes at most.
-- The engine's lookups compute the same expression over their parameters (repositories.py).
--
-- Existing rows already satisfy the old keys, so the rebuilt keys cannot conflict. No other
-- key, index, view or foreign key changes. Applied revisions are not edited. The Alembic
-- transaction rolls back all DDL and the ledger on failure.

ALTER TABLE alert_findings DROP CONSTRAINT pk_alert_findings;
ALTER TABLE llm_verdicts DROP CONSTRAINT pk_llm_verdicts;
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
GO
