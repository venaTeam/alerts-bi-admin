-- Alerts BI initial schema (design section 6, "Storage and output shape").
--
-- Guiding principle: persist facts, not judgments. Elasticsearch retains three months,
-- so every observation stored here outlives the source it came from and the deterministic
-- rules can be re-run over all of it when the ruleset changes. The LLM verdict is the one
-- deliberate exception - it cannot be recomputed for free, so it is stored as a pinned
-- result carrying the prompt and model version that produced it.
--
-- Naming note: the column is `alert_schema`, not `schema`, because SCHEMA is a reserved
-- word in T-SQL and quoting it at every use site invites a mistake.

-- ---------------------------------------------------------------- run record

CREATE TABLE runs (
    run_id                  NVARCHAR(64)   NOT NULL,
    run_at                  DATETIME2(3)   NOT NULL,
    team_id                 NVARCHAR(128)  NOT NULL,
    team_display_name       NVARCHAR(256)  NOT NULL,
    window_start            DATETIME2(3)   NOT NULL,
    window_end              DATETIME2(3)   NOT NULL,

    -- Versions exist so a movement in a team's numbers is always attributable:
    -- registry_version separates "their behaviour changed" from "we edited the mapping",
    -- and the ruleset/prompt/model triple does the same for "we changed what counts as bad".
    registry_version        NVARCHAR(64)   NOT NULL,
    registry_sha256         NVARCHAR(64)   NOT NULL,
    registry_entry_snapshot NVARCHAR(MAX)  NOT NULL,
    ruleset_version         NVARCHAR(32)   NOT NULL,
    prompt_version          NVARCHAR(32)   NOT NULL,
    model_version           NVARCHAR(128)  NULL,
    -- False on post-MVP backfilled history, so an empty flagged_by_llm there reads as
    -- absence of examination rather than absence of findings.
    llm_assessed            BIT            NOT NULL,

    phase_derived           NVARCHAR(16)   NOT NULL,
    -- NULL, never 0, when the team has no v2 identities at all.
    phase2_readiness_pct    DECIMAL(9, 6)  NULL,

    app_version             NVARCHAR(32)   NOT NULL,
    status                  NVARCHAR(16)   NOT NULL,
    started_at              DATETIME2(3)   NOT NULL,
    completed_at            DATETIME2(3)   NULL,
    error_summary           NVARCHAR(1000) NULL,

    CONSTRAINT pk_runs PRIMARY KEY (run_id),
    CONSTRAINT ck_runs_window CHECK (window_end > window_start),
    CONSTRAINT ck_runs_phase CHECK (
        phase_derived IN ('no_data', 'phase_0', 'phase_1', 'phase_2', 'done')
    ),
    CONSTRAINT ck_runs_status CHECK (status IN ('running', 'completed', 'failed')),
    CONSTRAINT ck_runs_readiness CHECK (
        phase2_readiness_pct IS NULL OR (phase2_readiness_pct >= 0 AND phase2_readiness_pct <= 100)
    )
);

CREATE INDEX ix_runs_team_run_at ON runs (team_id, run_at DESC);

-- ------------------------------------------------------------- daily metrics
-- One row per run x team x schema x UTC calendar date touched by the window. A rolling
-- 168-hour range normally touches eight dates, so the first and last rows are usually
-- partial and carry their real boundaries and covered_hours. Storing at a finer grain
-- than the report keeps any future window a presentation choice.

CREATE TABLE daily_metrics (
    run_id                    NVARCHAR(64)  NOT NULL,
    team_id                   NVARCHAR(128) NOT NULL,
    alert_schema              NVARCHAR(2)   NOT NULL,
    snapshot_date             DATE          NOT NULL,
    bucket_start              DATETIME2(3)  NOT NULL,
    bucket_end                DATETIME2(3)  NOT NULL,
    covered_hours             DECIMAL(6, 3) NOT NULL,

    -- volume
    alerts                    INT           NOT NULL,
    distinct_alerts           INT           NOT NULL,
    alerts_per_hour           DECIMAL(18, 6) NOT NULL,
    -- Both diagnostic operands are stored, not only the quotient, so the scorecard can
    -- roll up as sum(numerators)/sum(denominators) instead of averaging rounded ratios.
    node_name_numerator       INT           NOT NULL,
    node_name_denominator     INT           NOT NULL,
    node_name_ratio           DECIMAL(18, 6) NULL,
    key_inflation_numerator   INT           NOT NULL,
    key_inflation_denominator INT           NOT NULL,
    key_inflation_ratio       DECIMAL(18, 6) NULL,

    -- quality: the five identity states are mutually exclusive, and "good" is never
    -- inferred by subtracting flagged from total.
    flagged_by_rule           INT           NOT NULL,
    flagged_by_rule_distinct  INT           NOT NULL,
    flagged_by_llm            INT           NOT NULL,
    flagged_by_llm_distinct   INT           NOT NULL,
    needs_review              INT           NOT NULL,
    assessed_good             INT           NOT NULL,
    unassessed                INT           NOT NULL,
    phase2_gaps               INT           NOT NULL,

    -- visibility: suppressed is a SUBSET of flagged_by_rule (rule 5 promoted to a
    -- headline column), never an addition to it.
    suppressed                INT           NOT NULL,
    suppression_unmeasured    INT           NOT NULL,

    CONSTRAINT pk_daily_metrics PRIMARY KEY (run_id, alert_schema, snapshot_date),
    CONSTRAINT fk_daily_metrics_run FOREIGN KEY (run_id) REFERENCES runs (run_id) ON DELETE CASCADE,
    CONSTRAINT ck_daily_metrics_schema CHECK (alert_schema IN ('v1', 'v2')),
    CONSTRAINT ck_daily_metrics_bucket CHECK (bucket_end > bucket_start),
    CONSTRAINT ck_daily_metrics_covered CHECK (covered_hours > 0 AND covered_hours <= 24),
    CONSTRAINT ck_daily_metrics_distinct CHECK (distinct_alerts <= alerts),
    CONSTRAINT ck_daily_metrics_suppressed CHECK (suppressed <= flagged_by_rule)
);

-- --------------------------------------------------------- daily rule counts

CREATE TABLE daily_rule_counts (
    run_id          NVARCHAR(64)  NOT NULL,
    team_id         NVARCHAR(128) NOT NULL,
    alert_schema    NVARCHAR(2)   NOT NULL,
    snapshot_date   DATE          NOT NULL,
    rule_id         NVARCHAR(8)   NOT NULL,
    ruleset_version NVARCHAR(32)  NOT NULL,
    -- match_count is matching RAWS rows; distinct_count is identities with at least one
    -- matching row in this bucket.
    match_count     INT           NOT NULL,
    distinct_count  INT           NOT NULL,

    CONSTRAINT pk_daily_rule_counts PRIMARY KEY (run_id, alert_schema, snapshot_date, rule_id),
    CONSTRAINT fk_daily_rule_counts_run FOREIGN KEY (run_id) REFERENCES runs (run_id) ON DELETE CASCADE,
    CONSTRAINT ck_daily_rule_counts_schema CHECK (alert_schema IN ('v1', 'v2')),
    CONSTRAINT ck_daily_rule_counts_le CHECK (distinct_count <= match_count)
);

-- ------------------------------------------------------------ alert findings
-- One row per distinct identity per run: the actionable work list.

CREATE TABLE alert_findings (
    run_id              NVARCHAR(64)   NOT NULL,
    alert_schema        NVARCHAR(2)    NOT NULL,
    application         NVARCHAR(256)  NOT NULL,
    key_field           NVARCHAR(512)  NOT NULL,

    -- representative document: the identity's most recent row in the window
    representative_at   DATETIME2(3)   NOT NULL,
    representative_hash NVARCHAR(64)   NOT NULL,
    representative_doc  NVARCHAR(MAX)  NOT NULL,
    message             NVARCHAR(MAX)  NULL,
    severity            NVARCHAR(32)   NULL,
    component           NVARCHAR(256)  NULL,
    node_name           NVARCHAR(256)  NULL,
    environment         NVARCHAR(64)   NULL,
    provider            NVARCHAR(32)   NULL,
    alert_rule_url      NVARCHAR(1024) NULL,

    row_count           INT            NOT NULL,
    first_seen          DATETIME2(3)   NOT NULL,
    last_seen           DATETIME2(3)   NOT NULL,

    core_rule_ids       NVARCHAR(128)  NOT NULL,
    readiness_rule_ids  NVARCHAR(128)  NOT NULL,
    findings_evidence   NVARCHAR(MAX)  NOT NULL,

    -- Exactly one of the five mutually exclusive run-level identity states.
    quality_state       NVARCHAR(16)   NOT NULL,
    llm_principle_id    NVARCHAR(8)    NULL,
    llm_confidence      NVARCHAR(8)    NULL,
    llm_justification   NVARCHAR(1000) NULL,
    unassessed_reason   NVARCHAR(500)  NULL,

    CONSTRAINT pk_alert_findings PRIMARY KEY (run_id, alert_schema, application, key_field),
    CONSTRAINT fk_alert_findings_run FOREIGN KEY (run_id) REFERENCES runs (run_id) ON DELETE CASCADE,
    CONSTRAINT ck_alert_findings_schema CHECK (alert_schema IN ('v1', 'v2')),
    CONSTRAINT ck_alert_findings_state CHECK (
        quality_state IN ('rule_flagged', 'llm_flagged', 'needs_review', 'assessed_good', 'unassessed')
    ),
    CONSTRAINT ck_alert_findings_confidence CHECK (
        llm_confidence IS NULL OR llm_confidence IN ('high', 'medium', 'low')
    ),
    -- An unassessed identity must always say why: under exhaustive coverage a non-zero
    -- unassessed count is a classifier failure, and it has to read as one.
    CONSTRAINT ck_alert_findings_unassessed CHECK (
        quality_state <> 'unassessed' OR unassessed_reason IS NOT NULL
    )
);

CREATE INDEX ix_alert_findings_state ON alert_findings (run_id, quality_state);

-- -------------------------------------------------------- LLM batch attempts
-- Every attempt is recorded, so a batch that exhausted its three attempts can be audited
-- rather than inferred.

CREATE TABLE llm_batch_attempts (
    run_id           NVARCHAR(64)   NOT NULL,
    batch_id         NVARCHAR(64)   NOT NULL,
    attempt_number   INT            NOT NULL,
    group_type       NVARCHAR(16)   NOT NULL,
    group_value      NVARCHAR(1024) NOT NULL,
    partition_index  INT            NOT NULL,
    partition_count  INT            NOT NULL,
    alert_count      INT            NOT NULL,
    alert_ids        NVARCHAR(MAX)  NOT NULL,
    request_hash     NVARCHAR(64)   NOT NULL,
    request_payload  NVARCHAR(MAX)  NULL,
    status           NVARCHAR(24)   NOT NULL,
    failure_reason   NVARCHAR(1000) NULL,
    duration_ms      INT            NULL,
    created_at       DATETIME2(3)   NOT NULL,

    CONSTRAINT pk_llm_batch_attempts PRIMARY KEY (run_id, batch_id, attempt_number),
    CONSTRAINT fk_llm_batch_attempts_run FOREIGN KEY (run_id) REFERENCES runs (run_id) ON DELETE CASCADE,
    CONSTRAINT ck_llm_batch_attempts_group CHECK (group_type IN ('alert_rule_url', 'application')),
    CONSTRAINT ck_llm_batch_attempts_number CHECK (attempt_number BETWEEN 1 AND 3),
    CONSTRAINT ck_llm_batch_attempts_size CHECK (alert_count BETWEEN 1 AND 200)
);

-- ------------------------------------------------------------- LLM verdicts
-- Durable and deliberately NOT scoped to a run: the verdict is a property of the alert,
-- keyed by (application, key_field, prompt_version, model_version). A stored verdict is
-- never recomputed except on a version bump.

CREATE TABLE llm_verdicts (
    application        NVARCHAR(256)  NOT NULL,
    key_field          NVARCHAR(512)  NOT NULL,
    prompt_version     NVARCHAR(32)   NOT NULL,
    model_version      NVARCHAR(128)  NOT NULL,

    alert_schema       NVARCHAR(2)    NOT NULL,
    assessment         NVARCHAR(24)   NOT NULL,
    principle_id       NVARCHAR(8)    NOT NULL,
    confidence         NVARCHAR(8)    NOT NULL,
    justification      NVARCHAR(1000) NOT NULL,
    -- The complete document the model judged, plus its hash: the Elasticsearch row it
    -- came from expires after three months, so the input has to be kept here or the
    -- verdict becomes unauditable.
    representative_doc NVARCHAR(MAX)  NOT NULL,
    doc_hash           NVARCHAR(64)   NOT NULL,
    classified_at      DATETIME2(3)   NOT NULL,
    ruleset_version    NVARCHAR(32)   NOT NULL,
    first_run_id       NVARCHAR(64)   NOT NULL,

    CONSTRAINT pk_llm_verdicts PRIMARY KEY (application, key_field, prompt_version, model_version),
    CONSTRAINT ck_llm_verdicts_schema CHECK (alert_schema IN ('v1', 'v2')),
    CONSTRAINT ck_llm_verdicts_assessment CHECK (
        assessment IN ('no_violation', 'catalog_violation', 'other')
    ),
    CONSTRAINT ck_llm_verdicts_confidence CHECK (confidence IN ('high', 'medium', 'low')),
    -- The assessment/principle pairing is closed: NONE for no_violation, OTHER for other,
    -- and a catalogue id otherwise.
    CONSTRAINT ck_llm_verdicts_pairing CHECK (
        (assessment = 'no_violation' AND principle_id = 'NONE')
        OR (assessment = 'other' AND principle_id = 'OTHER')
        OR (assessment = 'catalog_violation' AND principle_id NOT IN ('NONE', 'OTHER'))
    ),
    CONSTRAINT ck_llm_verdicts_justification CHECK (LEN(LTRIM(RTRIM(justification))) > 0)
);

-- -------------------------------------------------------------- panel parses
-- Frozen by SQL-text hash and parser version, so identical query text always receives the
-- same interpretation and rule 5 stays stable between runs on identical input.

CREATE TABLE panel_parses (
    sql_text_hash     NVARCHAR(64)   NOT NULL,
    parser_version    NVARCHAR(32)   NOT NULL,
    parsed_result     NVARCHAR(MAX)  NOT NULL,
    safety_state      NVARCHAR(24)   NOT NULL,
    unmeasured_reason NVARCHAR(1000) NULL,
    created_at        DATETIME2(3)   NOT NULL,

    CONSTRAINT pk_panel_parses PRIMARY KEY (sql_text_hash, parser_version),
    CONSTRAINT ck_panel_parses_state CHECK (safety_state IN ('parsed', 'unparseable'))
);

-- --------------------------------------------------- run panel participation
-- Which supplied panels were used for a run, published with its numbers so the
-- suppression result can be disputed.

CREATE TABLE run_panels (
    run_id            NVARCHAR(64)   NOT NULL,
    panel_id          NVARCHAR(128)  NOT NULL,
    alert_schema      NVARCHAR(2)    NOT NULL,
    sql_text_hash     NVARCHAR(64)   NOT NULL,
    parser_version    NVARCHAR(32)   NOT NULL,
    suppression_leaves INT           NOT NULL,
    unmeasured_leaves  INT           NOT NULL,
    notes             NVARCHAR(MAX)  NULL,

    CONSTRAINT pk_run_panels PRIMARY KEY (run_id, panel_id),
    CONSTRAINT fk_run_panels_run FOREIGN KEY (run_id) REFERENCES runs (run_id) ON DELETE CASCADE,
    CONSTRAINT ck_run_panels_schema CHECK (alert_schema IN ('v1', 'v2'))
);
