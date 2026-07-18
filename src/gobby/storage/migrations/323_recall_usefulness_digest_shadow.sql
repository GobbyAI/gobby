-- Shadow query-relevance labels and fitted-constants provenance (#18422).
-- Existing digest labels are historical evidence: preserve their rows and
-- widen the source constraint for the new digest_shadow producer.

ALTER TABLE recall_usefulness
    DROP CONSTRAINT recall_usefulness_label_source_check;

ALTER TABLE recall_usefulness
    ADD CONSTRAINT recall_usefulness_label_source_check
    CHECK (
        label_source IN ('llm_judge', 'ablation', 'digest', 'digest_shadow', 'human')
    );

ALTER TABLE recall_signal_hits
    ADD COLUMN content_hash TEXT;

ALTER TABLE recall_signal_requests
    ADD COLUMN constants_provenance TEXT;

CREATE TABLE recall_shadow_judge_state (
    recall_request_id TEXT NOT NULL,
    label_source TEXT NOT NULL,
    judge_protocol_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('claimed', 'retryable', 'terminal', 'complete')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at TIMESTAMPTZ,
    lease_expires_at TIMESTAMPTZ,
    claim_token TEXT,
    last_error TEXT,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (recall_request_id, label_source, judge_protocol_version)
);

CREATE TABLE recall_shadow_prompt_snapshot (
    recall_request_id TEXT NOT NULL,
    label_source TEXT NOT NULL,
    judge_protocol_version TEXT NOT NULL,
    system_prompt TEXT NOT NULL,
    query_text TEXT NOT NULL,
    presented JSONB NOT NULL,
    prompt_hash TEXT NOT NULL,
    judge_model TEXT NOT NULL,
    judge_config_fingerprint TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (recall_request_id, label_source, judge_protocol_version)
);

CREATE TABLE recall_shadow_audit_verdicts (
    id BIGSERIAL PRIMARY KEY,
    cohort_digest TEXT NOT NULL,
    sample_digest TEXT NOT NULL,
    request_id TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    human_verdict BOOLEAN NOT NULL,
    reviewer TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (cohort_digest, request_id, memory_id)
);

CREATE TABLE recall_gate_runs (
    holdout_consumption_key TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('reserved', 'complete')),
    fit_settings_digest TEXT NOT NULL,
    claim_token TEXT NOT NULL,
    lease_expires_at TIMESTAMPTZ,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error TEXT,
    ship BOOLEAN,
    decision JSONB,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE recall_holdout_consumed (
    id BIGSERIAL PRIMARY KEY,
    request_id TEXT UNIQUE NOT NULL,
    holdout_consumption_key TEXT NOT NULL
        REFERENCES recall_gate_runs(holdout_consumption_key) ON DELETE RESTRICT,
    consumed_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_recall_usefulness_request_source_protocol
    ON recall_usefulness(recall_request_id, label_source, judge_protocol_version);
