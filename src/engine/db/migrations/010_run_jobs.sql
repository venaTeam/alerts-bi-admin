-- Durable manual submissions; report rows remain atomic and separate from job state.
CREATE TABLE run_jobs (
    job_id NVARCHAR(32) NOT NULL CONSTRAINT pk_run_jobs PRIMARY KEY,
    idempotency_key NVARCHAR(64) NOT NULL,
    requested_by NVARCHAR(256) NOT NULL,
    request_hash NVARCHAR(64) NOT NULL,
    team_id NVARCHAR(128) NOT NULL,
    run_at DATETIME2(7) NOT NULL,
    llm_mode NVARCHAR(8) NOT NULL,
    registry_sha256 NVARCHAR(64) NOT NULL,
    run_id NVARCHAR(64) NOT NULL,
    status NVARCHAR(16) NOT NULL,
    active_slot INT NULL,
    submitted_at DATETIME2(3) NOT NULL,
    started_at DATETIME2(3) NULL,
    completed_at DATETIME2(3) NULL,
    error_summary NVARCHAR(1000) NULL,
    CONSTRAINT uq_run_jobs_submission UNIQUE (requested_by, idempotency_key),
    CONSTRAINT ck_run_jobs_mode CHECK (llm_mode IN ('live', 'fake', 'off')),
    CONSTRAINT ck_run_jobs_state CHECK (
        (status IN ('pending', 'running') AND active_slot IS NOT NULL AND active_slot = 1)
        OR (status IN ('completed', 'failed') AND active_slot IS NULL)
    )
);
CREATE UNIQUE INDEX ux_run_jobs_active ON run_jobs(active_slot) WHERE active_slot IS NOT NULL;
CREATE INDEX ix_run_jobs_team ON run_jobs(team_id, submitted_at DESC);
