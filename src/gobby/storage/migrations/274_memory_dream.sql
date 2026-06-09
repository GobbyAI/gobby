CREATE TABLE IF NOT EXISTS memory_dream_runs (
    id TEXT PRIMARY KEY,
    -- Nullable for global/system dream runs; cron rows are anchored to PERSONAL_PROJECT_ID.
    project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'started'
        CONSTRAINT memory_dream_runs_status_check
        CHECK (status IN ('started', 'running', 'completed', 'failed', 'reverted')),
    dry_run BOOLEAN NOT NULL DEFAULT FALSE,
    options JSONB NOT NULL DEFAULT '{}'::jsonb,
    plan JSONB,
    summary JSONB,
    error TEXT,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    reverted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS memory_dream_snapshots (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES memory_dream_runs(id)
        ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    memory_id TEXT NOT NULL,
    action TEXT NOT NULL
        CONSTRAINT memory_dream_snapshots_action_check
        CHECK (action IN ('keep', 'delete', 'refresh', 'merge', 'supersede', 'review')),
    before_data JSONB,
    after_data JSONB,
    applied BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_memory_dream_snapshots_run
ON memory_dream_snapshots(run_id);

DELETE FROM cron_jobs
WHERE is_system = true
AND (
    name IN (
        'nightly-memory-cleanup',
        'gobby:nightly-memory-cleanup',
        'gobby:memory-cleanup'
    )
    OR (
        action_type = 'pipeline'
        AND action_config->>'pipeline_name' = 'nightly-memory-cleanup'
    )
);
