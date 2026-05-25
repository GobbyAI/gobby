CREATE INDEX IF NOT EXISTS idx_build_runs_project_root_started
    ON build_runs(project_id, root_task_id, started_at DESC);
