CREATE TABLE verification_receipts (
    id UUID PRIMARY KEY,
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE
        DEFERRABLE INITIALLY IMMEDIATE,
    session_id UUID NOT NULL,
    task_id UUID REFERENCES tasks(id) ON DELETE CASCADE
        DEFERRABLE INITIALLY IMMEDIATE,
    provider TEXT NOT NULL,
    execution_id TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    evidence_type TEXT NOT NULL DEFAULT 'validation_command',
    command TEXT,
    cwd TEXT,
    normalized_outcome TEXT NOT NULL CHECK (
        normalized_outcome IN ('provisional', 'success', 'failure', 'unknown', 'conflicting')
    ),
    outcome_provenance TEXT,
    exit_code INTEGER,
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    output_first_4k TEXT,
    output_last_4k TEXT,
    output_sha256 TEXT CHECK (output_sha256 ~ '^[0-9a-f]{64}$'),
    output_bytes BIGINT CHECK (output_bytes IS NULL OR output_bytes >= 0),
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    attribution_source TEXT NOT NULL CHECK (
        attribution_source IN ('active_task', 'sole_claim', 'explicit_task', 'manual_assignment', 'unassigned')
    ),
    attribution_actor TEXT,
    attributed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (project_id, session_id, provider, execution_id),
    CHECK (char_length(output_first_4k) <= 4096),
    CHECK (char_length(output_last_4k) <= 4096),
    CHECK (
        (task_id IS NULL AND attribution_source = 'unassigned')
        OR (task_id IS NOT NULL AND attribution_source <> 'unassigned')
    )
);

CREATE INDEX idx_verification_receipts_task
    ON verification_receipts(project_id, task_id, completed_at DESC, started_at DESC, id);

CREATE INDEX idx_verification_receipts_session
    ON verification_receipts(project_id, session_id, started_at DESC, id);

CREATE INDEX idx_verification_receipts_unassigned
    ON verification_receipts(project_id, session_id, started_at DESC, id)
    WHERE task_id IS NULL;

CREATE FUNCTION delete_unassigned_verification_receipts_for_session()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    DELETE FROM verification_receipts
    WHERE session_id = OLD.id AND task_id IS NULL;
    RETURN OLD;
END;
$$;

CREATE TRIGGER sessions_delete_unassigned_verification_receipts
BEFORE DELETE ON sessions
FOR EACH ROW
EXECUTE FUNCTION delete_unassigned_verification_receipts_for_session();
