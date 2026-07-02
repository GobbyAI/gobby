-- Convert UUID-backed identity/reference columns from TEXT to PostgreSQL UUID.
-- Public JSON/MCP/HTTP payloads continue to use string IDs; the Postgres adapter
-- normalizes native UUID values back to strings when rows cross the DB boundary.

CREATE TEMP TABLE _gobby_uuid_fk_restore ON COMMIT DROP AS
SELECT conrelid, conname, pg_get_constraintdef(oid) AS constraint_def
  FROM pg_constraint
 WHERE contype = 'f'
   AND connamespace = current_schema()::regnamespace;

DO $$
DECLARE
    fk record;
BEGIN
    FOR fk IN SELECT * FROM _gobby_uuid_fk_restore LOOP
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', fk.conrelid::regclass, fk.conname);
    END LOOP;
END;
$$;

DROP INDEX IF EXISTS tasks_search_bm25;
DROP INDEX IF EXISTS memories_search_bm25;
DROP INDEX IF EXISTS code_symbols_search_bm25;
DROP INDEX IF EXISTS code_content_search_bm25;

DROP TRIGGER IF EXISTS tasks_state_bucket_ai ON tasks;
DROP TRIGGER IF EXISTS tasks_state_bucket_au ON tasks;
DROP TRIGGER IF EXISTS task_stage_states_state_bucket_ai ON task_stage_states;
DROP TRIGGER IF EXISTS task_stage_states_state_bucket_au ON task_stage_states;
DROP TRIGGER IF EXISTS task_stage_states_state_bucket_ad ON task_stage_states;
DROP FUNCTION IF EXISTS refresh_task_state_bucket_from_stage();
DROP FUNCTION IF EXISTS refresh_task_state_bucket_from_task();
DROP FUNCTION IF EXISTS refresh_task_state_bucket(UUID);
DROP FUNCTION IF EXISTS refresh_task_state_bucket(TEXT);
DROP FUNCTION IF EXISTS compute_task_state_bucket(UUID);
DROP FUNCTION IF EXISTS compute_task_state_bucket(TEXT);

DO $$
DECLARE
    constraint_name text;
BEGIN
    SELECT conname INTO constraint_name
      FROM pg_constraint
     WHERE conrelid = 'code_calls'::regclass
       AND contype = 'u'
       AND pg_get_constraintdef(oid) LIKE '%callee_symbol_id%'
     LIMIT 1;
    IF constraint_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE code_calls DROP CONSTRAINT %I', constraint_name);
    END IF;

    SELECT conname INTO constraint_name
      FROM pg_constraint
     WHERE conrelid = 'metrics_events_archive'::regclass
       AND contype = 'u'
       AND pg_get_constraintdef(oid) LIKE '%project_id%'
     LIMIT 1;
    IF constraint_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE metrics_events_archive DROP CONSTRAINT %I', constraint_name);
    END IF;
END;
$$;

ALTER TABLE code_calls
    ALTER COLUMN callee_symbol_id DROP DEFAULT,
    ALTER COLUMN callee_symbol_id DROP NOT NULL;

ALTER TABLE metrics_events_archive
    ALTER COLUMN project_id DROP DEFAULT,
    ALTER COLUMN project_id DROP NOT NULL;

ALTER TABLE agent_runs
    ALTER COLUMN child_session_id TYPE UUID USING NULLIF(child_session_id::TEXT, '')::UUID,
    ALTER COLUMN claimed_session_id TYPE UUID USING NULLIF(claimed_session_id::TEXT, '')::UUID,
    ALTER COLUMN parent_session_id TYPE UUID USING parent_session_id::UUID,
    ALTER COLUMN task_id TYPE UUID USING NULLIF(task_id::TEXT, '')::UUID;
ALTER TABLE build_history_events
    ALTER COLUMN project_id TYPE UUID USING project_id::UUID,
    ALTER COLUMN root_task_id TYPE UUID USING NULLIF(root_task_id::TEXT, '')::UUID,
    ALTER COLUMN task_id TYPE UUID USING NULLIF(task_id::TEXT, '')::UUID;
ALTER TABLE build_profiles
    ALTER COLUMN id TYPE UUID USING id::UUID,
    ALTER COLUMN project_id TYPE UUID USING NULLIF(project_id::TEXT, '')::UUID;
ALTER TABLE build_runs
    ALTER COLUMN project_id TYPE UUID USING project_id::UUID,
    ALTER COLUMN root_task_id TYPE UUID USING NULLIF(root_task_id::TEXT, '')::UUID;
ALTER TABLE chat_attachments
    ALTER COLUMN id TYPE UUID USING id::UUID,
    ALTER COLUMN project_id TYPE UUID USING project_id::UUID,
    ALTER COLUMN target_session_id TYPE UUID USING NULLIF(target_session_id::TEXT, '')::UUID;
ALTER TABLE chat_messages
    ALTER COLUMN id TYPE UUID USING id::UUID;
ALTER TABLE checkpoints
    ALTER COLUMN id TYPE UUID USING id::UUID,
    ALTER COLUMN session_id TYPE UUID USING NULLIF(session_id::TEXT, '')::UUID,
    ALTER COLUMN task_id TYPE UUID USING task_id::UUID;
ALTER TABLE clones
    ALTER COLUMN agent_session_id TYPE UUID USING NULLIF(agent_session_id::TEXT, '')::UUID,
    ALTER COLUMN project_id TYPE UUID USING project_id::UUID,
    ALTER COLUMN task_id TYPE UUID USING NULLIF(task_id::TEXT, '')::UUID;
ALTER TABLE code_calls
    ALTER COLUMN callee_symbol_id TYPE UUID USING NULLIF(callee_symbol_id::TEXT, '')::UUID,
    ALTER COLUMN caller_symbol_id TYPE UUID USING caller_symbol_id::UUID,
    ALTER COLUMN project_id TYPE UUID USING project_id::UUID;
ALTER TABLE code_content_chunks
    ALTER COLUMN id TYPE UUID USING id::UUID,
    ALTER COLUMN project_id TYPE UUID USING project_id::UUID;
ALTER TABLE code_imports
    ALTER COLUMN project_id TYPE UUID USING project_id::UUID;
ALTER TABLE code_index_projection_cleanup_pending
    ALTER COLUMN project_id TYPE UUID USING project_id::UUID;
ALTER TABLE code_index_prune_dirty_projects
    ALTER COLUMN project_id TYPE UUID USING project_id::UUID;
ALTER TABLE code_indexed_files
    ALTER COLUMN id TYPE UUID USING id::UUID,
    ALTER COLUMN project_id TYPE UUID USING project_id::UUID;
ALTER TABLE code_indexed_projects
    ALTER COLUMN id TYPE UUID USING id::UUID;
ALTER TABLE code_symbols
    ALTER COLUMN id TYPE UUID USING id::UUID,
    ALTER COLUMN parent_symbol_id TYPE UUID USING NULLIF(parent_symbol_id::TEXT, '')::UUID,
    ALTER COLUMN project_id TYPE UUID USING project_id::UUID;
ALTER TABLE comms_identities
    ALTER COLUMN project_id TYPE UUID USING NULLIF(project_id::TEXT, '')::UUID,
    ALTER COLUMN session_id TYPE UUID USING NULLIF(session_id::TEXT, '')::UUID;
ALTER TABLE comms_messages
    ALTER COLUMN session_id TYPE UUID USING NULLIF(session_id::TEXT, '')::UUID;
ALTER TABLE comms_routing_rules
    ALTER COLUMN project_id TYPE UUID USING NULLIF(project_id::TEXT, '')::UUID,
    ALTER COLUMN session_id TYPE UUID USING NULLIF(session_id::TEXT, '')::UUID;
ALTER TABLE completion_subscribers
    ALTER COLUMN session_id TYPE UUID USING session_id::UUID;
ALTER TABLE cron_jobs
    ALTER COLUMN project_id TYPE UUID USING project_id::UUID;
ALTER TABLE expansion_runs
    ALTER COLUMN parent_task_id TYPE UUID USING parent_task_id::UUID,
    ALTER COLUMN project_id TYPE UUID USING project_id::UUID,
    ALTER COLUMN triggering_session_id TYPE UUID USING NULLIF(triggering_session_id::TEXT, '')::UUID;
ALTER TABLE gh_issues_triaged
    ALTER COLUMN project_id TYPE UUID USING project_id::UUID,
    ALTER COLUMN task_id TYPE UUID USING NULLIF(task_id::TEXT, '')::UUID;
ALTER TABLE gh_triage_deliveries
    ALTER COLUMN project_id TYPE UUID USING project_id::UUID;
ALTER TABLE inter_session_messages
    ALTER COLUMN from_session TYPE UUID USING from_session::UUID,
    ALTER COLUMN id TYPE UUID USING id::UUID,
    ALTER COLUMN to_session TYPE UUID USING to_session::UUID;
ALTER TABLE loop_progress
    ALTER COLUMN session_id TYPE UUID USING session_id::UUID;
ALTER TABLE mcp_servers
    ALTER COLUMN id TYPE UUID USING id::UUID,
    ALTER COLUMN project_id TYPE UUID USING project_id::UUID;
ALTER TABLE memories
    ALTER COLUMN id TYPE UUID USING id::UUID,
    ALTER COLUMN project_id TYPE UUID USING NULLIF(project_id::TEXT, '')::UUID,
    ALTER COLUMN source_session_id TYPE UUID USING NULLIF(source_session_id::TEXT, '')::UUID;
ALTER TABLE memory_crossrefs
    ALTER COLUMN source_id TYPE UUID USING source_id::UUID,
    ALTER COLUMN target_id TYPE UUID USING target_id::UUID;
ALTER TABLE memory_dream_runs
    ALTER COLUMN id TYPE UUID USING id::UUID,
    ALTER COLUMN project_id TYPE UUID USING NULLIF(project_id::TEXT, '')::UUID;
ALTER TABLE memory_dream_snapshots
    ALTER COLUMN memory_id TYPE UUID USING memory_id::UUID,
    ALTER COLUMN run_id TYPE UUID USING run_id::UUID;
ALTER TABLE metrics_events
    ALTER COLUMN project_id TYPE UUID USING NULLIF(project_id::TEXT, '')::UUID,
    ALTER COLUMN session_id TYPE UUID USING NULLIF(session_id::TEXT, '')::UUID;
ALTER TABLE metrics_events_archive
    ALTER COLUMN project_id TYPE UUID USING NULLIF(project_id::TEXT, '')::UUID;
ALTER TABLE pending_interactions
    ALTER COLUMN id TYPE UUID USING id::UUID,
    ALTER COLUMN session_id TYPE UUID USING session_id::UUID;
ALTER TABLE pipeline_executions
    ALTER COLUMN project_id TYPE UUID USING project_id::UUID,
    ALTER COLUMN session_id TYPE UUID USING NULLIF(session_id::TEXT, '')::UUID;
ALTER TABLE plans
    ALTER COLUMN id TYPE UUID USING id::UUID,
    ALTER COLUMN project_id TYPE UUID USING project_id::UUID;
ALTER TABLE project_github_triage_configs
    ALTER COLUMN project_id TYPE UUID USING project_id::UUID;
ALTER TABLE project_lifecycle_events
    ALTER COLUMN project_id TYPE UUID USING project_id::UUID;
ALTER TABLE projects
    ALTER COLUMN id TYPE UUID USING id::UUID;
ALTER TABLE prompts
    ALTER COLUMN project_id TYPE UUID USING NULLIF(project_id::TEXT, '')::UUID;
ALTER TABLE secrets
    ALTER COLUMN id TYPE UUID USING id::UUID;
ALTER TABLE session_memories
    ALTER COLUMN memory_id TYPE UUID USING memory_id::UUID,
    ALTER COLUMN session_id TYPE UUID USING session_id::UUID;
ALTER TABLE session_skills
    ALTER COLUMN session_id TYPE UUID USING session_id::UUID;
ALTER TABLE session_stop_signals
    ALTER COLUMN session_id TYPE UUID USING session_id::UUID;
ALTER TABLE session_summary_revisions
    ALTER COLUMN id TYPE UUID USING id::UUID,
    ALTER COLUMN previous_revision_id TYPE UUID USING NULLIF(previous_revision_id::TEXT, '')::UUID,
    ALTER COLUMN session_id TYPE UUID USING session_id::UUID;
ALTER TABLE session_tasks
    ALTER COLUMN session_id TYPE UUID USING session_id::UUID,
    ALTER COLUMN task_id TYPE UUID USING task_id::UUID;
ALTER TABLE sessions
    ALTER COLUMN id TYPE UUID USING id::UUID,
    ALTER COLUMN parent_session_id TYPE UUID USING NULLIF(parent_session_id::TEXT, '')::UUID,
    ALTER COLUMN project_id TYPE UUID USING project_id::UUID,
    ALTER COLUMN summary_revision_id TYPE UUID USING NULLIF(summary_revision_id::TEXT, '')::UUID;
ALTER TABLE skills
    ALTER COLUMN project_id TYPE UUID USING NULLIF(project_id::TEXT, '')::UUID;
ALTER TABLE task_affected_files
    ALTER COLUMN task_id TYPE UUID USING task_id::UUID;
ALTER TABLE task_artifacts
    ALTER COLUMN task_id TYPE UUID USING task_id::UUID;
ALTER TABLE task_comments
    ALTER COLUMN id TYPE UUID USING id::UUID,
    ALTER COLUMN parent_comment_id TYPE UUID USING NULLIF(parent_comment_id::TEXT, '')::UUID,
    ALTER COLUMN task_id TYPE UUID USING task_id::UUID;
ALTER TABLE task_delivery_campaigns
    ALTER COLUMN task_id TYPE UUID USING task_id::UUID;
ALTER TABLE task_delivery_units
    ALTER COLUMN id TYPE UUID USING id::UUID,
    ALTER COLUMN task_id TYPE UUID USING task_id::UUID;
ALTER TABLE task_dependencies
    ALTER COLUMN depends_on TYPE UUID USING depends_on::UUID,
    ALTER COLUMN task_id TYPE UUID USING task_id::UUID;
ALTER TABLE task_dispatch_mutex
    ALTER COLUMN task_id TYPE UUID USING task_id::UUID;
ALTER TABLE task_lifecycle_events
    ALTER COLUMN task_id TYPE UUID USING task_id::UUID;
ALTER TABLE task_selection_history
    ALTER COLUMN session_id TYPE UUID USING session_id::UUID,
    ALTER COLUMN task_id TYPE UUID USING task_id::UUID;
ALTER TABLE task_stage_states
    ALTER COLUMN completed_by_session_id TYPE UUID USING NULLIF(completed_by_session_id::TEXT, '')::UUID,
    ALTER COLUMN entered_by_session_id TYPE UUID USING NULLIF(entered_by_session_id::TEXT, '')::UUID,
    ALTER COLUMN task_id TYPE UUID USING task_id::UUID;
ALTER TABLE task_validation_backoff
    ALTER COLUMN task_id TYPE UUID USING task_id::UUID;
ALTER TABLE task_validation_history
    ALTER COLUMN task_id TYPE UUID USING task_id::UUID;
ALTER TABLE tasks
    ALTER COLUMN claimed_by_session_id TYPE UUID USING NULLIF(claimed_by_session_id::TEXT, '')::UUID,
    ALTER COLUMN closed_in_session_id TYPE UUID USING NULLIF(closed_in_session_id::TEXT, '')::UUID,
    ALTER COLUMN created_in_session_id TYPE UUID USING NULLIF(created_in_session_id::TEXT, '')::UUID,
    ALTER COLUMN id TYPE UUID USING id::UUID,
    ALTER COLUMN parent_task_id TYPE UUID USING NULLIF(parent_task_id::TEXT, '')::UUID,
    ALTER COLUMN project_id TYPE UUID USING project_id::UUID;
ALTER TABLE token_events
    ALTER COLUMN project_id TYPE UUID USING NULLIF(project_id::TEXT, '')::UUID,
    ALTER COLUMN session_id TYPE UUID USING session_id::UUID;
ALTER TABLE tool_embeddings
    ALTER COLUMN project_id TYPE UUID USING project_id::UUID,
    ALTER COLUMN tool_id TYPE UUID USING tool_id::UUID;
ALTER TABLE tool_metrics
    ALTER COLUMN project_id TYPE UUID USING project_id::UUID;
ALTER TABLE tool_metrics_daily
    ALTER COLUMN project_id TYPE UUID USING project_id::UUID;
ALTER TABLE tool_schema_hashes
    ALTER COLUMN project_id TYPE UUID USING project_id::UUID;
ALTER TABLE tools
    ALTER COLUMN id TYPE UUID USING id::UUID,
    ALTER COLUMN mcp_server_id TYPE UUID USING mcp_server_id::UUID;
ALTER TABLE workflow_audit_log
    ALTER COLUMN session_id TYPE UUID USING session_id::UUID;
ALTER TABLE workflow_definitions
    ALTER COLUMN id TYPE UUID USING id::UUID,
    ALTER COLUMN project_id TYPE UUID USING NULLIF(project_id::TEXT, '')::UUID;
ALTER TABLE workflow_instances
    ALTER COLUMN id TYPE UUID USING id::UUID,
    ALTER COLUMN session_id TYPE UUID USING session_id::UUID;
ALTER TABLE workflow_states
    ALTER COLUMN session_id TYPE UUID USING session_id::UUID;
ALTER TABLE worktrees
    ALTER COLUMN agent_session_id TYPE UUID USING NULLIF(agent_session_id::TEXT, '')::UUID,
    ALTER COLUMN project_id TYPE UUID USING project_id::UUID,
    ALTER COLUMN task_id TYPE UUID USING NULLIF(task_id::TEXT, '')::UUID;
ALTER TABLE code_calls
    ADD CONSTRAINT code_calls_unique_call_target
    UNIQUE NULLS NOT DISTINCT (
        project_id,
        caller_symbol_id,
        callee_symbol_id,
        callee_name,
        callee_target_kind,
        callee_external_module,
        file_path,
        line
    );

ALTER TABLE metrics_events_archive
    ADD CONSTRAINT metrics_events_archive_unique_rollup
    UNIQUE NULLS NOT DISTINCT(event_type, project_id, server_name, name);

DO $$
DECLARE
    fk record;
BEGIN
    FOR fk IN SELECT * FROM _gobby_uuid_fk_restore LOOP
        EXECUTE format(
            'ALTER TABLE %s ADD CONSTRAINT %I %s',
            fk.conrelid::regclass,
            fk.conname,
            fk.constraint_def
        );
    END LOOP;
END;
$$;

CREATE FUNCTION compute_task_state_bucket(p_task_id UUID)
RETURNS TEXT
LANGUAGE sql
STABLE
AS $$
    SELECT CASE
        WHEN t.closed_at IS NOT NULL THEN 'closed'
        WHEN t.escalated_at IS NOT NULL OR COALESCE(t.is_escalated, FALSE) IS TRUE THEN 'escalated'
        ELSE COALESCE(
            (
                SELECT CASE
                    WHEN stage_scan.state IN (
                        'ready', 'in_progress', 'needs_review', 'review_approved'
                    )
                    THEN stage_scan.state
                    ELSE 'ready'
                END
                  FROM task_stage_states stage_scan
                 WHERE stage_scan.task_id = p_task_id
                   AND stage_scan.state <> 'done'
                 ORDER BY stage_scan.position
                 LIMIT 1
            ),
            'ready'
        )
    END
    FROM tasks t
    WHERE t.id = p_task_id
$$;

CREATE FUNCTION refresh_task_state_bucket(p_task_id UUID)
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    UPDATE tasks
       SET state_bucket = compute_task_state_bucket(p_task_id)
     WHERE id = p_task_id;
END;
$$;

CREATE FUNCTION refresh_task_state_bucket_from_task()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM refresh_task_state_bucket(NEW.id);
    RETURN NEW;
END;
$$;

CREATE FUNCTION refresh_task_state_bucket_from_stage()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        PERFORM refresh_task_state_bucket(OLD.task_id);
        RETURN OLD;
    END IF;

    PERFORM refresh_task_state_bucket(NEW.task_id);
    IF TG_OP = 'UPDATE' AND OLD.task_id IS DISTINCT FROM NEW.task_id THEN
        PERFORM refresh_task_state_bucket(OLD.task_id);
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER tasks_state_bucket_ai
AFTER INSERT ON tasks
FOR EACH ROW
EXECUTE FUNCTION refresh_task_state_bucket_from_task();

CREATE TRIGGER tasks_state_bucket_au
AFTER UPDATE OF closed_at, escalated_at, is_escalated ON tasks
FOR EACH ROW
EXECUTE FUNCTION refresh_task_state_bucket_from_task();

CREATE TRIGGER task_stage_states_state_bucket_ai
AFTER INSERT ON task_stage_states
FOR EACH ROW
EXECUTE FUNCTION refresh_task_state_bucket_from_stage();

CREATE TRIGGER task_stage_states_state_bucket_au
AFTER UPDATE OF state, position ON task_stage_states
FOR EACH ROW
EXECUTE FUNCTION refresh_task_state_bucket_from_stage();

CREATE TRIGGER task_stage_states_state_bucket_ad
AFTER DELETE ON task_stage_states
FOR EACH ROW
EXECUTE FUNCTION refresh_task_state_bucket_from_stage();

CREATE INDEX tasks_search_bm25 ON tasks
USING bm25 (id, title, description)
WITH (key_field='id');

CREATE INDEX memories_search_bm25 ON memories
USING bm25 (id, content, tags_text)
WITH (key_field='id');

CREATE INDEX code_symbols_search_bm25 ON code_symbols
USING bm25 (id, name, qualified_name, signature, docstring, summary)
WITH (key_field='id');

CREATE INDEX code_content_search_bm25 ON code_content_chunks
USING bm25 (id, content)
WITH (key_field='id');
