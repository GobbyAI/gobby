-- Enforce memory dream run status and snapshot action values on existing installs.

UPDATE memory_dream_snapshots
   SET action = CASE
       WHEN action = 'supersede_create' THEN 'supersede'
       ELSE 'review'
   END
 WHERE action NOT IN ('keep', 'delete', 'refresh', 'merge', 'supersede', 'review');

UPDATE memory_dream_runs
   SET status = 'failed',
       completed_at = COALESCE(completed_at, NOW()),
       updated_at = NOW()
 WHERE status NOT IN ('started', 'running', 'completed', 'failed', 'reverted', 'revert_failed');

ALTER TABLE memory_dream_runs
    ALTER COLUMN status SET DEFAULT 'started';

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'memory_dream_runs_status_check'
           AND conrelid = 'memory_dream_runs'::regclass
    ) THEN
        ALTER TABLE memory_dream_runs
            DROP CONSTRAINT memory_dream_runs_status_check;
    END IF;

    ALTER TABLE memory_dream_runs
        ADD CONSTRAINT memory_dream_runs_status_check
        CHECK (status IN ('started', 'running', 'completed', 'failed', 'reverted', 'revert_failed'));

    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'memory_dream_snapshots_action_check'
           AND conrelid = 'memory_dream_snapshots'::regclass
    ) THEN
        ALTER TABLE memory_dream_snapshots
            ADD CONSTRAINT memory_dream_snapshots_action_check
            CHECK (action IN ('keep', 'delete', 'refresh', 'merge', 'supersede', 'review'));
    END IF;
END $$;
