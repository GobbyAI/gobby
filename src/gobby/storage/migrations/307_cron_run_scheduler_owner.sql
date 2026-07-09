ALTER TABLE cron_runs
ADD COLUMN IF NOT EXISTS scheduler_owner TEXT;

CREATE INDEX IF NOT EXISTS idx_cron_runs_scheduler_owner_active
ON cron_runs(scheduler_owner)
WHERE status IN ('pending', 'running');
