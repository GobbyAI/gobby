-- Singleton admission and durable checkpoints for memory dream runs.

-- Recovery ahead of index reconciliation: migrations run during startup
-- before the daemon serves requests, so any non-terminal row is an orphan of
-- a pre-admission daemon. Sweep them so the single-running index can build.
UPDATE memory_dream_runs
   SET status = 'interrupted',
       error = 'Interrupted: daemon restarted while the dream run was in progress',
       completed_at = COALESCE(completed_at, NOW()),
       updated_at = NOW()
 WHERE status IN ('started', 'running');

-- 'partial' joins the terminal status vocabulary for runs stopped mid-backlog.
ALTER TABLE memory_dream_runs
    DROP CONSTRAINT IF EXISTS memory_dream_runs_status_check;
ALTER TABLE memory_dream_runs
    ADD CONSTRAINT memory_dream_runs_status_check
    CHECK (status IN ('started', 'running', 'completed', 'failed', 'reverted', 'revert_failed', 'interrupted', 'partial'));

-- Durable work-unit progress.
ALTER TABLE memory_dream_runs ADD COLUMN IF NOT EXISTS checkpoint JSONB;

-- Single-flight admission: at most one 'running' dream row at a time.
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_dream_runs_single_running
    ON memory_dream_runs (status)
    WHERE status = 'running';
