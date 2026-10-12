-- Read-only review portal (design section 7.10).
--
-- Adds three things and changes nothing that 001 created:
--
--   * review_publications - which completed runs an operator has published as a team's
--     weekly review. Only published weeks ever reach a reader.
--   * finding_decisions   - the append-only human-review record: pending / confirmed /
--     dismissed per finding, never overwriting the pipeline's own verdict.
--   * the portal_* views and the alerts_bi_reader role - the only things the portal's own
--     SQL login may read. The views expose published weeks only and never the complete
--     source document, model request payloads or batch audit rows.

-- --------------------------------------------------------- review publications

CREATE TABLE review_publications (
    publication_id   INT IDENTITY(1, 1) NOT NULL,
    run_id           NVARCHAR(64)   NOT NULL,
    -- Copied from the run so the one-current-publication-per-week rule can be a
    -- constraint rather than a hope.
    team_id          NVARCHAR(128)  NOT NULL,
    window_start     DATETIME2(3)   NOT NULL,
    window_end       DATETIME2(3)   NOT NULL,
    published_at     DATETIME2(3)   NOT NULL,
    published_by     NVARCHAR(128)  NOT NULL,
    review_note      NVARCHAR(4000) NULL,
    -- Withdrawal keeps the row: a publication readers once saw is part of the audit trail.
    withdrawn_at     DATETIME2(3)   NULL,
    withdrawn_by     NVARCHAR(128)  NULL,
    withdrawn_reason NVARCHAR(1000) NULL,

    CONSTRAINT pk_review_publications PRIMARY KEY (publication_id),
    -- No cascade: a published run must not disappear underneath its readers.
    CONSTRAINT fk_review_publications_run FOREIGN KEY (run_id) REFERENCES runs (run_id),
    CONSTRAINT ck_review_publications_window CHECK (window_end > window_start),
    CONSTRAINT ck_review_publications_withdrawn CHECK (
        (withdrawn_at IS NULL AND withdrawn_by IS NULL AND withdrawn_reason IS NULL)
        OR (withdrawn_at IS NOT NULL AND withdrawn_by IS NOT NULL AND withdrawn_reason IS NOT NULL)
    )
);

-- At most one current publication per run, and per team and week.
CREATE UNIQUE INDEX ux_review_publications_current_run
    ON review_publications (run_id) WHERE withdrawn_at IS NULL;
CREATE UNIQUE INDEX ux_review_publications_current_week
    ON review_publications (team_id, window_start) WHERE withdrawn_at IS NULL;
CREATE INDEX ix_review_publications_team ON review_publications (team_id, window_end);

-- ------------------------------------------------------------ human decisions
-- Keyed on the exact identity and finding, so a decision never carries over to the new v2
-- key a team mints by enriching an alert (design section 3.7). run_id records which
-- published week the decision was made against; it is context, not a foreign key.

CREATE TABLE finding_decisions (
    decision_id  INT IDENTITY(1, 1) NOT NULL,
    team_id      NVARCHAR(128)  NOT NULL,
    run_id       NVARCHAR(64)   NOT NULL,
    alert_schema NVARCHAR(2)    NOT NULL,
    application  NVARCHAR(256)  NOT NULL,
    key_field    NVARCHAR(512)  NOT NULL,
    finding_id   NVARCHAR(8)    NOT NULL,
    state        NVARCHAR(16)   NOT NULL,
    note         NVARCHAR(2000) NOT NULL,
    decided_at   DATETIME2(3)   NOT NULL,
    decided_by   NVARCHAR(128)  NOT NULL,

    CONSTRAINT pk_finding_decisions PRIMARY KEY (decision_id),
    CONSTRAINT ck_finding_decisions_schema CHECK (alert_schema IN ('v1', 'v2')),
    CONSTRAINT ck_finding_decisions_state CHECK (state IN ('pending', 'confirmed', 'dismissed')),
    CONSTRAINT ck_finding_decisions_note CHECK (LEN(LTRIM(RTRIM(note))) > 0)
);

CREATE INDEX ix_finding_decisions_identity
    ON finding_decisions (alert_schema, application, key_field, finding_id, decided_at);
CREATE INDEX ix_finding_decisions_team ON finding_decisions (team_id, decided_at);
GO

-- A decision is history: a changed mind is a new row, never an edit.
CREATE TRIGGER tr_finding_decisions_append_only
ON finding_decisions
INSTEAD OF UPDATE, DELETE
AS
BEGIN
    THROW 51000, 'finding_decisions is append-only: record a new decision instead', 1;
END
GO

-- ---------------------------------------------------------------- portal views

-- Published weeks only. A withdrawn publication, or a run that is not complete, is not here.
CREATE VIEW portal_reviews AS
SELECT
    p.publication_id,
    p.team_id,
    r.team_display_name,
    p.run_id,
    p.window_start,
    p.window_end,
    p.published_at,
    p.review_note,
    r.phase_derived,
    r.phase2_readiness_pct
FROM review_publications AS p
JOIN runs AS r ON r.run_id = p.run_id
WHERE p.withdrawn_at IS NULL
  AND r.status = 'completed';
GO

-- One row per published week and schema. events is every firing in the window; distinct
-- is the identity count for the whole window (one work-list row each), not the scorecard's
-- per-day rate.
CREATE VIEW portal_schema_totals AS
SELECT
    v.run_id,
    s.alert_schema,
    COALESCE(dm.events, 0)             AS events,
    COALESCE(dm.flagged_rows, 0)       AS flagged_rows,
    COALESCE(dm.suppressed, 0)         AS suppressed,
    COALESCE(af.distinct_alerts, 0)    AS distinct_alerts,
    COALESCE(af.rule_flagged, 0)       AS rule_flagged,
    COALESCE(af.llm_flagged, 0)        AS llm_flagged,
    COALESCE(af.needs_review, 0)       AS needs_review,
    COALESCE(af.assessed_good, 0)      AS assessed_good,
    COALESCE(af.unassessed, 0)         AS unassessed,
    COALESCE(af.readiness_gaps, 0)     AS readiness_gaps,
    COALESCE(af.needs_attention, 0)    AS needs_attention
FROM portal_reviews AS v
CROSS JOIN (VALUES (N'v1'), (N'v2')) AS s (alert_schema)
LEFT JOIN (
    SELECT run_id, alert_schema,
           SUM(alerts) AS events,
           SUM(flagged_by_rule) AS flagged_rows,
           SUM(suppressed) AS suppressed
    FROM daily_metrics
    GROUP BY run_id, alert_schema
) AS dm ON dm.run_id = v.run_id AND dm.alert_schema = s.alert_schema
LEFT JOIN (
    SELECT run_id, alert_schema,
           COUNT(*) AS distinct_alerts,
           SUM(CASE WHEN quality_state = 'rule_flagged' THEN 1 ELSE 0 END) AS rule_flagged,
           SUM(CASE WHEN quality_state = 'llm_flagged' THEN 1 ELSE 0 END) AS llm_flagged,
           SUM(CASE WHEN quality_state = 'needs_review' THEN 1 ELSE 0 END) AS needs_review,
           SUM(CASE WHEN quality_state = 'assessed_good' THEN 1 ELSE 0 END) AS assessed_good,
           SUM(CASE WHEN quality_state = 'unassessed' THEN 1 ELSE 0 END) AS unassessed,
           SUM(CASE WHEN readiness_rule_ids <> '' THEN 1 ELSE 0 END) AS readiness_gaps,
           SUM(CASE WHEN quality_state IN ('rule_flagged', 'llm_flagged', 'needs_review')
                      OR readiness_rule_ids <> '' THEN 1 ELSE 0 END) AS needs_attention
    FROM alert_findings
    GROUP BY run_id, alert_schema
) AS af ON af.run_id = v.run_id AND af.alert_schema = s.alert_schema;
GO

-- The work list and alert detail. Only the source fields a reviewer needs are extracted from
-- the stored representative document; the document itself is not exposed.
CREATE VIEW portal_alerts AS
SELECT
    f.run_id,
    f.alert_schema,
    f.application,
    f.key_field,
    f.message,
    f.severity,
    f.component,
    f.node_name,
    f.environment,
    f.provider,
    f.alert_rule_url,
    f.row_count,
    f.first_seen,
    f.last_seen,
    f.representative_at,
    f.core_rule_ids,
    f.readiness_rule_ids,
    f.findings_evidence,
    f.quality_state,
    f.llm_principle_id,
    f.llm_confidence,
    f.llm_justification,
    CASE WHEN ISJSON(f.representative_doc) = 1
         THEN JSON_VALUE(f.representative_doc, '$.impact') END AS impact,
    CASE WHEN ISJSON(f.representative_doc) = 1
         THEN JSON_VALUE(f.representative_doc, '$.runbook_url') END AS runbook_url,
    CASE WHEN ISJSON(f.representative_doc) = 1
         THEN JSON_VALUE(f.representative_doc, '$.status') END AS alert_status,
    CASE WHEN ISJSON(f.representative_doc) = 1
         THEN JSON_VALUE(f.representative_doc, '$.time_created') END AS time_created,
    -- The work-list order: rule findings, advisory model findings, decisions needed, then
    -- readiness-only gaps. 4 means nothing needs attention.
    CASE
        WHEN f.quality_state = 'rule_flagged' THEN 0
        WHEN f.quality_state = 'llm_flagged' THEN 1
        WHEN f.quality_state = 'needs_review' THEN 2
        WHEN f.readiness_rule_ids <> '' THEN 3
        ELSE 4
    END AS attention_rank
FROM alert_findings AS f
JOIN portal_reviews AS v ON v.run_id = f.run_id;
GO

-- Decisions made against a week that is still published.
CREATE VIEW portal_decisions AS
SELECT
    d.decision_id,
    d.team_id,
    d.alert_schema,
    d.application,
    d.key_field,
    d.finding_id,
    d.state,
    d.note,
    d.decided_at,
    d.decided_by
FROM finding_decisions AS d
WHERE EXISTS (SELECT 1 FROM portal_reviews AS v WHERE v.run_id = d.run_id);
GO

-- ------------------------------------------------------------- the reader role
-- The portal's login is a member of this role and nothing else. It can read the four views
-- and no base table; ownership chaining lets the views read the tables on its behalf.

CREATE ROLE alerts_bi_reader;
GRANT SELECT ON portal_reviews TO alerts_bi_reader;
GRANT SELECT ON portal_schema_totals TO alerts_bi_reader;
GRANT SELECT ON portal_alerts TO alerts_bi_reader;
GRANT SELECT ON portal_decisions TO alerts_bi_reader;
