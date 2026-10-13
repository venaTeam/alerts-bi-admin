-- Prevent a concurrent decision insert from outliving a deleted run. Historical
-- decisions are checked rather than silently removed if an orphan already exists.
ALTER TABLE finding_decisions WITH CHECK ADD CONSTRAINT fk_finding_decisions_run
    FOREIGN KEY (run_id) REFERENCES runs (run_id);
GO

-- Decisions remain append-only except within the owning connection's explicit
-- whole-run deletion scope. Updates are never permitted, even within that scope.
ALTER TRIGGER tr_finding_decisions_append_only
ON finding_decisions
INSTEAD OF UPDATE, DELETE
AS
BEGIN
    SET NOCOUNT ON;
    IF EXISTS (SELECT 1 FROM inserted)
       OR EXISTS (
           SELECT 1 FROM deleted
           WHERE run_id <> COALESCE(CONVERT(NVARCHAR(64), SESSION_CONTEXT(N'alerts_bi_delete_run')), N'')
       )
    BEGIN
        THROW 51000, 'finding_decisions is append-only outside whole-run deletion', 1;
    END;
    DELETE f FROM finding_decisions f JOIN deleted d ON d.decision_id = f.decision_id;
END
GO
