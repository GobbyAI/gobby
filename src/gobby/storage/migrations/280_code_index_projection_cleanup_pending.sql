CREATE TABLE IF NOT EXISTS code_index_projection_cleanup_pending (
    project_id TEXT NOT NULL,
    store TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(project_id, store),
    CONSTRAINT code_index_projection_cleanup_store
        CHECK (store IN ('graph', 'vector'))
);

CREATE INDEX IF NOT EXISTS idx_cipcp_updated
    ON code_index_projection_cleanup_pending(updated_at, created_at);
