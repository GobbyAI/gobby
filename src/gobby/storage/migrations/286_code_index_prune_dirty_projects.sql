CREATE TABLE IF NOT EXISTS code_index_prune_dirty_projects (
    project_id TEXT PRIMARY KEY,
    root_path TEXT NOT NULL,
    reason TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cipdp_updated
    ON code_index_prune_dirty_projects(updated_at, created_at);
