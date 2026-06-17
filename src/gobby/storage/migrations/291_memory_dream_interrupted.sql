-- Add the 'interrupted' memory dream run status and normalize orphaned runs.
--
-- A dream run executes as an in-process asyncio background task with no external
-- liveness handle. A daemon restart cancels that task without persisting a
-- terminal status, leaving the row stuck at 'running'/'started'. The new
-- 'interrupted' status records this distinctly from genuine 'failed' runs, and
-- the one-time normalization below clears any orphan left by a prior process.
-- The drop must precede the normalization because 'interrupted' is not allowed
-- by the pre-existing constraint.

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
END $$;

UPDATE memory_dream_runs
   SET status = 'interrupted',
       completed_at = COALESCE(completed_at, NOW()),
       error = COALESCE(
           error,
           'Interrupted: daemon restarted while the dream run was in progress'
       ),
       updated_at = NOW()
 WHERE status IN ('started', 'running')
   AND created_at < NOW() - INTERVAL '1 minute';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'memory_dream_runs_status_check'
           AND conrelid = 'memory_dream_runs'::regclass
    ) THEN
        ALTER TABLE memory_dream_runs
            ADD CONSTRAINT memory_dream_runs_status_check
            CHECK (
                status IN (
                    'started', 'running', 'completed', 'failed', 'reverted',
                    'revert_failed', 'interrupted'
                )
            );
    END IF;
END $$;
