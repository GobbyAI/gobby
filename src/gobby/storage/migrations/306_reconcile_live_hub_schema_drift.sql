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

CREATE OR REPLACE FUNCTION pg_temp.gobby_is_uuid_castable(value TEXT)
RETURNS BOOLEAN
LANGUAGE plpgsql
AS $$
BEGIN
    IF value IS NULL THEN
        RETURN TRUE;
    END IF;

    IF value ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
        RETURN TRUE;
    END IF;

    BEGIN
        PERFORM value::UUID;
        RETURN TRUE;
    EXCEPTION WHEN invalid_text_representation THEN
        RETURN FALSE;
    END;
END;
$$;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM memory_dream_runs
         WHERE id IS NOT NULL
           AND NOT pg_temp.gobby_is_uuid_castable(id::TEXT)
    ) THEN
        RAISE EXCEPTION
            'memory_dream_runs.id UUID preflight failed: uncastable id values exist';
    END IF;

    IF EXISTS (
        SELECT 1
          FROM memory_dream_runs
         WHERE project_id IS NOT NULL
           AND NOT pg_temp.gobby_is_uuid_castable(project_id::TEXT)
    ) THEN
        RAISE EXCEPTION
            'memory_dream_runs.project_id UUID preflight failed: uncastable project_id values exist';
    END IF;

    IF EXISTS (
        SELECT 1
          FROM memory_dream_snapshots
         WHERE run_id IS NOT NULL
           AND NOT pg_temp.gobby_is_uuid_castable(run_id::TEXT)
    ) THEN
        RAISE EXCEPTION
            'memory_dream_snapshots.run_id UUID preflight failed: uncastable run_id values exist';
    END IF;

    IF EXISTS (
        SELECT 1
          FROM memory_dream_snapshots
         WHERE memory_id IS NOT NULL
           AND NOT pg_temp.gobby_is_uuid_castable(memory_id::TEXT)
    ) THEN
        RAISE EXCEPTION
            'memory_dream_snapshots.memory_id UUID preflight failed: uncastable memory_id values exist';
    END IF;
END $$;

ALTER TABLE memory_dream_snapshots
    DROP CONSTRAINT IF EXISTS memory_dream_snapshots_run_id_fkey;

ALTER TABLE memory_dream_runs
    DROP CONSTRAINT IF EXISTS memory_dream_runs_project_id_fkey;

ALTER TABLE memory_dream_runs
    ALTER COLUMN id TYPE UUID USING id::UUID,
    ALTER COLUMN project_id TYPE UUID USING project_id::UUID;

ALTER TABLE memory_dream_snapshots
    ALTER COLUMN run_id TYPE UUID USING run_id::UUID,
    ALTER COLUMN memory_id TYPE UUID USING memory_id::UUID;

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

    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'memory_dream_snapshots_run_id_fkey'
           AND conrelid = 'memory_dream_snapshots'::regclass
    ) THEN
        IF EXISTS (
            SELECT 1
              FROM memory_dream_snapshots snapshots
              LEFT JOIN memory_dream_runs runs
                ON runs.id = snapshots.run_id
             WHERE runs.id IS NULL
        ) THEN
            RAISE EXCEPTION
                'memory_dream_snapshots_run_id_fkey preflight failed: dangling run_id values exist';
        END IF;

        ALTER TABLE memory_dream_snapshots
            ADD CONSTRAINT memory_dream_snapshots_run_id_fkey
            FOREIGN KEY (run_id) REFERENCES memory_dream_runs(id)
            ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE;
    END IF;
END $$;
