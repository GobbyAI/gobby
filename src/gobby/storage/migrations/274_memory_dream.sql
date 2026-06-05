CREATE TABLE IF NOT EXISTS memory_dream_runs (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    status TEXT NOT NULL,
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
    action TEXT NOT NULL,
    before_data JSONB,
    after_data JSONB,
    applied BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_memory_dream_snapshots_run
ON memory_dream_snapshots(run_id, id);

DELETE FROM cron_jobs
WHERE name IN (
    'nightly-memory-cleanup',
    'gobby:nightly-memory-cleanup',
    'gobby:memory-cleanup'
)
OR (
    action_type = 'pipeline'
    AND action_config->>'pipeline_name' = 'nightly-memory-cleanup'
);
