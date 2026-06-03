ALTER TABLE tasks
ADD COLUMN IF NOT EXISTS merge_in_progress BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE tasks
ADD COLUMN IF NOT EXISTS blocked_by_merge BOOLEAN NOT NULL DEFAULT FALSE;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'tasks_merge_in_progress_bool_check'
    ) THEN
        ALTER TABLE tasks
        ADD CONSTRAINT tasks_merge_in_progress_bool_check
        CHECK (merge_in_progress IN (TRUE, FALSE));
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'tasks_blocked_by_merge_bool_check'
    ) THEN
        ALTER TABLE tasks
        ADD CONSTRAINT tasks_blocked_by_merge_bool_check
        CHECK (blocked_by_merge IN (TRUE, FALSE));
    END IF;
END $$;
