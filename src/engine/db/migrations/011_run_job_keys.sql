-- Exact ASCII submission identity, independent of collation and string padding.
-- Preserve existing jobs and the original key for audit; do not edit revision 010.
ALTER TABLE run_jobs ADD idempotency_key_hash BINARY(32) NULL;
GO
UPDATE run_jobs
SET idempotency_key_hash = HASHBYTES('SHA2_256', CONVERT(VARCHAR(64), idempotency_key));
ALTER TABLE run_jobs ALTER COLUMN idempotency_key_hash BINARY(32) NOT NULL;
ALTER TABLE run_jobs DROP CONSTRAINT uq_run_jobs_submission;
ALTER TABLE run_jobs ADD CONSTRAINT uq_run_jobs_submission_hash
    UNIQUE (requested_by, idempotency_key_hash);
