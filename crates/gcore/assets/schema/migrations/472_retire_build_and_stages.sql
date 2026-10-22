-- Retire build dispatch after its code and storage consumers have been removed.
-- Keep task lifecycle history and the mutex used by task-bound agent spawns.
DROP TABLE task_stage_states;
DROP TABLE task_type_default_stages;
DROP TABLE task_stages_registry;
DROP TABLE build_history_events;
DROP TABLE build_runs;
DROP TABLE build_profiles;
DROP FUNCTION refresh_task_state_bucket_from_stage();
DROP INDEX idx_tasks_dispatch_scan;

ALTER TABLE tasks
    DROP COLUMN allow_automation,
    DROP COLUMN unattended,
    DROP COLUMN checkout_mode,
    DROP COLUMN dispatch_failure_count;

CREATE OR REPLACE FUNCTION compute_task_state_bucket(p_task_id uuid) RETURNS text
    LANGUAGE sql STABLE
    AS $$
    SELECT CASE
        WHEN t.closed_at IS NOT NULL THEN 'closed'
        WHEN t.escalated_at IS NOT NULL OR COALESCE(t.is_escalated, FALSE) IS TRUE THEN 'escalated'
        ELSE 'ready'
    END
    FROM tasks t
    WHERE t.id = p_task_id
$$;

ALTER TABLE tasks DROP CONSTRAINT tasks_state_bucket_check;
UPDATE tasks SET state_bucket = 'ready'
 WHERE state_bucket NOT IN ('ready', 'closed', 'escalated');
ALTER TABLE tasks ADD CONSTRAINT tasks_state_bucket_check
    CHECK (state_bucket = ANY (ARRAY['ready'::text, 'closed'::text, 'escalated'::text]));

-- DROP COLUMN also removes the old checks that referenced the run binding.
ALTER TABLE plan_review_evidence DROP COLUMN dispatch_run_id;
ALTER TABLE plan_review_evidence
    ADD CONSTRAINT plan_review_evidence_bound_lease_cleared
        CHECK (static_writer_session_id IS NULL OR lease_expires_at IS NULL),
    ADD CONSTRAINT plan_review_evidence_static_binding
        CHECK ((static_writer_session_id IS NULL AND static_coordinator_session_id IS NULL)
               OR (static_writer_session_id IS NOT NULL
                   AND static_coordinator_session_id IS NOT NULL
                   AND session_id IS NOT NULL));
