-- Parallel runs across teams (design "Parallel runs across teams", 2026-10-27). With
-- WORKER_CONCURRENCY above 1 the worker runs several jobs at once, so a pending or running job
-- holds a numbered slot from 1 instead of the one slot 1. ux_run_jobs_active still keeps each
-- slot number to one job, and ux_run_jobs_team_active lets a team hold at most one slot: two
-- weeks of one team never run at once. Queued, completed, failed and cancelled jobs still hold
-- none. Revisions 001-020 are unchanged.
--
-- No row changes: before this revision at most one job held a slot. An older image that uses
-- only slot 1 runs unchanged against this schema, so it can remain installed on rollback.
ALTER TABLE run_jobs DROP CONSTRAINT ck_run_jobs_state;
ALTER TABLE run_jobs ADD CONSTRAINT ck_run_jobs_state CHECK (
    (status IN ('pending', 'running') AND active_slot IS NOT NULL AND active_slot >= 1)
    OR (status IN ('queued', 'completed', 'failed', 'cancelled') AND active_slot IS NULL)
);
CREATE UNIQUE INDEX ux_run_jobs_team_active ON run_jobs(team_id) WHERE active_slot IS NOT NULL;
