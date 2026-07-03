ALTER TABLE sessions
    DROP CONSTRAINT IF EXISTS sessions_context_usage_ratio_range;

DROP INDEX IF EXISTS idx_sessions_context_usage_ratio;

ALTER TABLE sessions
    ALTER COLUMN context_usage_ratio TYPE DOUBLE PRECISION
    USING context_usage_ratio::DOUBLE PRECISION;

ALTER TABLE sessions
    ADD CONSTRAINT sessions_context_usage_ratio_range
    CHECK (
        context_usage_ratio IS NULL
        OR (context_usage_ratio >= 0 AND context_usage_ratio <= 1)
    );

UPDATE tasks
   SET merge_in_progress = FALSE
 WHERE merge_in_progress IS NULL;

UPDATE tasks
   SET blocked_by_merge = FALSE
 WHERE blocked_by_merge IS NULL;

ALTER TABLE tasks
    ALTER COLUMN merge_in_progress SET DEFAULT FALSE,
    ALTER COLUMN merge_in_progress SET NOT NULL,
    ALTER COLUMN blocked_by_merge SET DEFAULT FALSE,
    ALTER COLUMN blocked_by_merge SET NOT NULL;

DROP INDEX IF EXISTS idx_memory_dream_snapshots_run;

CREATE INDEX idx_memory_dream_snapshots_run
ON memory_dream_snapshots(run_id);

CREATE INDEX idx_sessions_context_usage_ratio
ON sessions(context_usage_ratio DESC)
WHERE context_usage_ratio IS NOT NULL;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM memory_dream_runs runs
          LEFT JOIN projects projects
            ON projects.id = runs.project_id
         WHERE runs.project_id IS NOT NULL
           AND projects.id IS NULL
    ) THEN
        RAISE EXCEPTION
            'memory_dream_runs_project_id_fkey preflight failed: dangling project_id values exist';
    END IF;

    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'memory_dream_runs_project_id_fkey'
           AND conrelid = 'memory_dream_runs'::regclass
    ) THEN
        ALTER TABLE memory_dream_runs
            ADD CONSTRAINT memory_dream_runs_project_id_fkey
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE;
    END IF;
END $$;
