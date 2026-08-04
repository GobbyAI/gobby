-- gobby:destructive
-- Add fail-closed machine ownership to local-resource records.

ALTER TABLE worktrees
    ADD COLUMN IF NOT EXISTS machine_id UUID REFERENCES machines(id);
ALTER TABLE clones
    ADD COLUMN IF NOT EXISTS machine_id UUID REFERENCES machines(id);
ALTER TABLE agent_runs
    ADD COLUMN IF NOT EXISTS machine_id UUID REFERENCES machines(id);
ALTER TABLE cron_runs
    ADD COLUMN IF NOT EXISTS machine_id UUID REFERENCES machines(id);

UPDATE worktrees w
 SET machine_id = s.machine_id
  FROM sessions s
 WHERE s.id = w.agent_session_id
   AND w.machine_id IS NULL;

UPDATE clones c
 SET machine_id = s.machine_id
  FROM sessions s
 WHERE s.id = c.agent_session_id
   AND c.machine_id IS NULL;

UPDATE agent_runs ar
   SET machine_id = COALESCE(
       (
           SELECT child.machine_id
             FROM sessions child
            WHERE child.id = ar.child_session_id
       ),
       (
           SELECT parent.machine_id
             FROM sessions parent
            WHERE parent.id = ar.parent_session_id
       )
   )
 WHERE ar.machine_id IS NULL;

DO $$
DECLARE
    active_cron_runs JSONB;
BEGIN
    SELECT jsonb_agg(
        jsonb_build_object('id', id, 'status', status, 'scheduler_owner', scheduler_owner)
        ORDER BY id
    )
      INTO active_cron_runs
      FROM cron_runs
     WHERE status IN ('pending', 'running');

    IF active_cron_runs IS NOT NULL THEN
        RAISE EXCEPTION USING
            MESSAGE = 'machine scope migration blocked: cron state is not drained',
            DETAIL = active_cron_runs::TEXT,
            HINT = 'Stop every daemon and drain or explicitly resolve pending/running cron rows.';
    END IF;
END
$$;

UPDATE cron_runs cr
   SET machine_id = COALESCE(
       (
           SELECT ar.machine_id
             FROM agent_runs ar
            WHERE ar.id = cr.agent_run_id
       ),
       (
           SELECT s.machine_id
             FROM pipeline_executions pe
             JOIN sessions s ON s.id = pe.session_id
            WHERE pe.id = cr.pipeline_execution_id
       )
   )
 WHERE cr.machine_id IS NULL;

DO $$
DECLARE
    unresolved_worktrees JSONB;
    unresolved_clones JSONB;
    unresolved_agent_runs JSONB;
    unresolved_cron_runs JSONB;
    diagnostics JSONB;
BEGIN
    SELECT jsonb_agg(
        jsonb_build_object('id', id, 'agent_session_id', agent_session_id) ORDER BY id
    ) INTO unresolved_worktrees
      FROM worktrees
     WHERE machine_id IS NULL;

    SELECT jsonb_agg(
        jsonb_build_object('id', id, 'agent_session_id', agent_session_id) ORDER BY id
    ) INTO unresolved_clones
      FROM clones
     WHERE machine_id IS NULL;

    SELECT jsonb_agg(
        jsonb_build_object(
            'id', id,
            'child_session_id', child_session_id,
            'parent_session_id', parent_session_id
        ) ORDER BY id
    ) INTO unresolved_agent_runs
      FROM agent_runs
     WHERE machine_id IS NULL;

    SELECT jsonb_agg(
        jsonb_build_object(
            'id', id,
            'status', status,
            'agent_run_id', agent_run_id,
            'pipeline_execution_id', pipeline_execution_id
        ) ORDER BY id
    ) INTO unresolved_cron_runs
      FROM cron_runs
     WHERE machine_id IS NULL;

    diagnostics = jsonb_strip_nulls(
        jsonb_build_object(
            'unresolved_worktrees', unresolved_worktrees,
            'unresolved_clones', unresolved_clones,
            'unresolved_agent_runs', unresolved_agent_runs,
            'unresolved_cron_runs', unresolved_cron_runs
        )
    );

    IF diagnostics <> '{}'::JSONB THEN
        RAISE EXCEPTION USING
            MESSAGE = 'machine scope migration blocked: unresolved machine ownership',
            DETAIL = diagnostics::TEXT,
            HINT = 'DELETE confirmed stale rows or repair their authoritative session/run linkage, then rerun.';
    END IF;
END
$$;

ALTER TABLE worktrees
    ALTER COLUMN machine_id SET NOT NULL;
ALTER TABLE clones
    ALTER COLUMN machine_id SET NOT NULL;
ALTER TABLE agent_runs
    ALTER COLUMN machine_id SET NOT NULL;
ALTER TABLE cron_runs
    ALTER COLUMN machine_id SET NOT NULL;

DROP INDEX idx_worktrees_path;
CREATE UNIQUE INDEX idx_worktrees_path ON worktrees(machine_id, worktree_path);

DROP INDEX idx_worktrees_branch;
CREATE UNIQUE INDEX idx_worktrees_branch
    ON worktrees(project_id, branch_name, machine_id);

DROP INDEX idx_clones_path;
CREATE UNIQUE INDEX idx_clones_path ON clones(machine_id, clone_path);

CREATE INDEX IF NOT EXISTS idx_agent_runs_machine_status ON agent_runs(machine_id, status);
