-- Execution audit is independent of replaceable, completed scorecard rows.
-- Interrupted work must be visible to operators without appearing in the portal.
CREATE TABLE llm_prompt_artifacts (
    prompt_version NVARCHAR(32) NOT NULL PRIMARY KEY,
    system_prompt_hash NVARCHAR(64) NOT NULL,
    system_prompt NVARCHAR(MAX) NOT NULL,
    response_schema_hash NVARCHAR(64) NOT NULL,
    response_schema NVARCHAR(MAX) NOT NULL
);

CREATE TABLE llm_review_scopes (
    scope_id NVARCHAR(64) NOT NULL PRIMARY KEY,
    scope_kind NVARCHAR(16) NOT NULL,
    prompt_version NVARCHAR(32) NOT NULL,
    model_version NVARCHAR(128) NOT NULL,
    settings_hash NVARCHAR(64) NOT NULL,
    settings_json NVARCHAR(MAX) NOT NULL,
    created_at DATETIME2(3) NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT ck_llm_scope_kind CHECK (scope_kind IN ('run', 'evaluation')),
    CONSTRAINT fk_llm_scope_prompt FOREIGN KEY (prompt_version) REFERENCES llm_prompt_artifacts(prompt_version)
);

CREATE TABLE llm_review_batches (
    cycle_id NVARCHAR(64) NOT NULL PRIMARY KEY,
    scope_id NVARCHAR(64) NOT NULL,
    batch_id NVARCHAR(64) NOT NULL,
    cycle_number INT NOT NULL,
    request_hash NVARCHAR(64) NOT NULL,
    request_payload NVARCHAR(MAX) NOT NULL,
    status NVARCHAR(16) NOT NULL,
    created_at DATETIME2(3) NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT uq_llm_batch_cycle UNIQUE (scope_id, batch_id, cycle_number),
    CONSTRAINT ck_llm_batch_cycle CHECK (cycle_number > 0),
    CONSTRAINT ck_llm_batch_state CHECK (status IN ('running', 'succeeded', 'exhausted')),
    CONSTRAINT fk_llm_batch_scope FOREIGN KEY (scope_id) REFERENCES llm_review_scopes(scope_id)
);

CREATE TABLE llm_review_attempts (
    cycle_id NVARCHAR(64) NOT NULL,
    attempt_number INT NOT NULL,
    status NVARCHAR(24) NOT NULL,
    response_text NVARCHAR(MAX) NULL,
    failure_reason NVARCHAR(1000) NULL,
    metadata_json NVARCHAR(MAX) NOT NULL DEFAULT N'{}',
    duration_ms INT NULL,
    created_at DATETIME2(3) NOT NULL,
    completed_at DATETIME2(3) NULL,
    CONSTRAINT pk_llm_review_attempts PRIMARY KEY (cycle_id, attempt_number),
    CONSTRAINT ck_llm_review_attempt_number CHECK (attempt_number BETWEEN 1 AND 3),
    CONSTRAINT ck_llm_review_attempt_status CHECK (status IN
        ('started', 'succeeded', 'invalid_response', 'timeout', 'transport_error', 'refusal', 'incomplete', 'empty', 'interrupted')),
    CONSTRAINT fk_llm_review_attempt_batch FOREIGN KEY (cycle_id) REFERENCES llm_review_batches(cycle_id)
);

DENY SELECT, INSERT, UPDATE, DELETE ON llm_prompt_artifacts TO alerts_bi_reader;
DENY SELECT, INSERT, UPDATE, DELETE ON llm_review_scopes TO alerts_bi_reader;
DENY SELECT, INSERT, UPDATE, DELETE ON llm_review_batches TO alerts_bi_reader;
DENY SELECT, INSERT, UPDATE, DELETE ON llm_review_attempts TO alerts_bi_reader;
