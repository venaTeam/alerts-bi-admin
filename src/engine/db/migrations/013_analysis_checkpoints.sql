-- Private completed deterministic analysis, before model assessment. No portal grants.
-- A row exists only after both schemas finish; partial Elasticsearch scans are never saved.
CREATE TABLE run_analysis_checkpoints (
    run_id NVARCHAR(64) NOT NULL PRIMARY KEY,
    source_hash NVARCHAR(64) NOT NULL,
    format_version INT NOT NULL,
    payload VARBINARY(MAX) NOT NULL,
    created_at DATETIME2(3) NOT NULL DEFAULT SYSUTCDATETIME()
);
GO
