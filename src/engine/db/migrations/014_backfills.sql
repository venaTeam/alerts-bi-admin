-- Operator multi-week backfill (design docs/backfill.md). One row per accepted request; each
-- week is an ordinary run_jobs row. Queued and cancelled jobs hold no active slot, so other
-- writers keep checking only the pending/running reservation. Revisions 001-013 are unchanged.
CREATE TABLE run_backfills (
    backfill_id NVARCHAR(32) NOT NULL CONSTRAINT pk_run_backfills PRIMARY KEY,
    team_id NVARCHAR(128) NOT NULL,
    requested_by NVARCHAR(256) NOT NULL,
    idempotency_key_hash BINARY(32) NOT NULL,
    request_hash NVARCHAR(64) NOT NULL,
    llm_mode NVARCHAR(8) NOT NULL,
    week_count INT NOT NULL,
    submitted_at DATETIME2(3) NOT NULL,
    cancelled_at DATETIME2(3) NULL,
    cancelled_by NVARCHAR(256) NULL,
    dismissed_at DATETIME2(3) NULL,
    CONSTRAINT uq_run_backfills_submission UNIQUE (requested_by, idempotency_key_hash),
    CONSTRAINT ck_run_backfills_mode CHECK (llm_mode IN ('live', 'fake', 'off')),
    CONSTRAINT ck_run_backfills_weeks CHECK (week_count BETWEEN 1 AND 12)
);
CREATE INDEX ix_run_backfills_team ON run_backfills(team_id, submitted_at DESC);
ALTER TABLE run_jobs ADD backfill_id NVARCHAR(32) NULL
    CONSTRAINT fk_run_jobs_backfill REFERENCES run_backfills(backfill_id);
GO
ALTER TABLE run_jobs DROP CONSTRAINT ck_run_jobs_state;
ALTER TABLE run_jobs ADD CONSTRAINT ck_run_jobs_state CHECK (
    (status IN ('pending', 'running') AND active_slot IS NOT NULL AND active_slot = 1)
    OR (status IN ('queued', 'completed', 'failed', 'cancelled') AND active_slot IS NULL)
);
CREATE INDEX ix_run_jobs_backfill ON run_jobs(backfill_id, run_at) WHERE backfill_id IS NOT NULL;
