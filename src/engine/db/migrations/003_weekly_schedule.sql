-- Automatic weekly reviews (design section 7.11).
--
-- One row per team and week each time `alerts-bi weekly` acts on it: what happened and why.
-- It is the operator's record of the schedule - published, held for a person, stored while an
-- earlier week is held, failed, blocked, or skipped because the week is older than
-- Elasticsearch retention. Append-only by use; nothing reads it but the operator CLI, and the
-- reader role has no grant on it.

CREATE TABLE weekly_review_log (
    log_id       INT IDENTITY(1, 1) NOT NULL,
    invoked_at   DATETIME2(3)   NOT NULL,
    team_id      NVARCHAR(128)  NOT NULL,
    window_end   DATETIME2(3)   NULL,
    outcome      NVARCHAR(16)   NOT NULL,
    run_id       NVARCHAR(64)   NULL,
    detail       NVARCHAR(1000) NULL,

    CONSTRAINT pk_weekly_review_log PRIMARY KEY (log_id),
    CONSTRAINT ck_weekly_review_log_outcome CHECK (
        outcome IN ('published', 'held', 'stored', 'failed', 'blocked', 'expired')
    )
);

CREATE INDEX ix_weekly_review_log_team ON weekly_review_log (team_id, invoked_at);
