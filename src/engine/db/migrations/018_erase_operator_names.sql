-- Operator names are no longer recorded (design "Admin console without login", 2026-10-19).
-- Erase the names already stored. Columns, views and every other value are unchanged.
-- Names become N''; a canceller, which no constraint requires, becomes NULL. withdrawn_by stays
-- non-NULL on withdrawn rows because ck_review_publications_withdrawn requires it. The
-- scheduler's own marker 'weekly-schedule' names no person and is kept. The Alembic
-- transaction rolls back every statement, the trigger state and the ledger on failure.
UPDATE review_publications SET published_by = N''
WHERE published_by <> N'' AND published_by <> N'weekly-schedule';
UPDATE review_publications SET withdrawn_by = N''
WHERE withdrawn_by IS NOT NULL AND withdrawn_by <> N'';
GO

-- Decisions stay append-only: the trigger is disabled only around this one erasure.
DISABLE TRIGGER tr_finding_decisions_append_only ON finding_decisions;
UPDATE finding_decisions SET decided_by = N'' WHERE decided_by <> N'';
ENABLE TRIGGER tr_finding_decisions_append_only ON finding_decisions;
GO

-- Submission keys were unique per requester. Where two requesters used the same key, the
-- rows stay apart by their own id instead of a name.
UPDATE j SET requested_by = CASE WHEN k.uses > 1 THEN N'#' + j.job_id ELSE N'' END
FROM run_jobs AS j
JOIN (
    SELECT idempotency_key_hash, COUNT(*) AS uses FROM run_jobs GROUP BY idempotency_key_hash
) AS k ON k.idempotency_key_hash = j.idempotency_key_hash;
UPDATE b SET requested_by = CASE WHEN k.uses > 1 THEN N'#' + b.backfill_id ELSE N'' END
FROM run_backfills AS b
JOIN (
    SELECT idempotency_key_hash, COUNT(*) AS uses FROM run_backfills GROUP BY idempotency_key_hash
) AS k ON k.idempotency_key_hash = b.idempotency_key_hash;
UPDATE run_backfills SET cancelled_by = NULL WHERE cancelled_by IS NOT NULL;
-- Cancelled backfill weeks carried the canceller's name in their message.
UPDATE run_jobs SET error_summary = N'Cancelled before it started.'
WHERE status = N'cancelled' AND error_summary LIKE N'Cancelled by % before it started.';
