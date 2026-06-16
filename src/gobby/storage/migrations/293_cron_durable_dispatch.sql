-- Normalize active cron runs for durable pipeline/agent dispatch and add indexes.
--
-- Existing pending/running rows from an older daemon process are not owned by
-- this process. Pipeline and agent runs with a linked active child are terminal
-- from cron's perspective (`dispatched`); everything else is a stale active row.

UPDATE cron_runs cr
   SET status = 'dispatched',
       completed_at = COALESCE(cr.completed_at, NOW()),
       error = NULL
  FROM cron_jobs cj, pipeline_executions pe
 WHERE cr.cron_job_id = cj.id
   AND cj.action_type = 'pipeline'
   AND pe.id = cr.pipeline_execution_id
   AND pe.status IN ('pending', 'running', 'waiting_approval', 'interrupted')
   AND cr.status IN ('pending', 'running');

UPDATE cron_runs cr
   SET status = 'dispatched',
       completed_at = COALESCE(cr.completed_at, NOW()),
       error = NULL
  FROM cron_jobs cj, agent_runs ar
 WHERE cr.cron_job_id = cj.id
   AND cj.action_type = 'agent_spawn'
   AND ar.id = cr.agent_run_id
   AND ar.status IN ('pending', 'running')
   AND cr.status IN ('pending', 'running');

UPDATE cron_runs
   SET status = 'failed',
       completed_at = COALESCE(completed_at, NOW()),
       error = COALESCE(
           error,
           'Cron run was still active when the durable dispatch migration ran'
       )
 WHERE status IN ('pending', 'running');

CREATE INDEX IF NOT EXISTS idx_cron_runs_agent_run
    ON cron_runs(agent_run_id)
    WHERE agent_run_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_cron_runs_pipeline_execution
    ON cron_runs(pipeline_execution_id)
    WHERE pipeline_execution_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_cron_runs_one_active_per_job
    ON cron_runs(cron_job_id)
    WHERE status IN ('pending', 'running');
