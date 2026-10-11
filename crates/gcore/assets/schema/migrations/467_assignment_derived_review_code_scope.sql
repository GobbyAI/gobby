-- Review assignments are live read authority; frozen principal bindings remain
-- the sole write authority. Existing reviewer logins gain/lose reads immediately.
GRANT SELECT (session_id, task_id, action) ON public.session_tasks TO gobby_agent_issuer;
GRANT SELECT (id, project_id, claimed_by_session_id, closed_at) ON public.tasks TO gobby_agent_issuer;
GRANT SELECT (workspace_path, agent_run_id) ON public.sessions TO gobby_agent_issuer;
GRANT SELECT (agent_name, child_session_id) ON public.agent_runs TO gobby_agent_issuer;
GRANT SELECT (worktree_path, status) ON public.worktrees TO gobby_agent_issuer;

CREATE FUNCTION gobby_agent_auth.current_review_code_project_id() RETURNS uuid
    LANGUAGE plpgsql STABLE SECURITY DEFINER PARALLEL SAFE
    SET search_path TO 'pg_catalog', 'gobby_agent_auth'
    AS $$
DECLARE
    binding gobby_agent_auth.principal_bindings%ROWTYPE;
    assignment_count integer;
    review_project_id uuid;
BEGIN
    SELECT * INTO STRICT binding FROM gobby_agent_auth.principal_bindings
    WHERE role_name = session_user::name AND revoked_at IS NULL
      AND expires_at > clock_timestamp();

    SELECT count(*), min(gobby_agent_auth.code_index_project_id(owner.workspace_path)::text)::uuid
    INTO assignment_count, review_project_id
    FROM public.session_tasks link
    JOIN public.sessions reviewer ON reviewer.id = link.session_id
    JOIN public.agent_runs run ON run.id = reviewer.agent_run_id
    JOIN public.tasks task ON task.id = link.task_id
    JOIN public.sessions owner ON owner.id = task.claimed_by_session_id
    WHERE link.session_id = binding.session_id AND link.action = 'reviewing'
      AND run.agent_name = 'code-reviewer' AND run.status = 'running'
      AND run.child_session_id = reviewer.id
      AND task.closed_at IS NULL AND task.project_id = binding.project_id
      AND reviewer.project_id = binding.project_id AND owner.project_id = binding.project_id
      AND reviewer.machine_id = binding.issuing_machine_id
      AND owner.machine_id = binding.issuing_machine_id
      AND EXISTS (
          SELECT 1 FROM public.worktrees worktree
          WHERE worktree.project_id = binding.project_id
            AND worktree.machine_id = binding.issuing_machine_id
            AND worktree.worktree_path = owner.workspace_path AND worktree.status = 'active'
      );
    IF assignment_count = 1 THEN
        RETURN review_project_id;
    END IF;
    RETURN NULL;
END
$$;
ALTER FUNCTION gobby_agent_auth.current_review_code_project_id() OWNER TO gobby_agent_issuer;
REVOKE ALL ON FUNCTION gobby_agent_auth.current_review_code_project_id() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION gobby_agent_auth.current_review_code_project_id() TO gobby_gcode_capability;

-- Keep the single ANY qual so pg_search can push the scope into BM25 scans.
ALTER POLICY gobby_gcode_project_read ON public.code_calls
    USING (project_id = ANY (ARRAY[gobby_agent_auth.current_project_id(), gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_review_code_project_id()]));
ALTER POLICY gobby_gcode_project_read ON public.code_content_chunks
    USING (project_id = ANY (ARRAY[gobby_agent_auth.current_project_id(), gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_review_code_project_id()]));
ALTER POLICY gobby_gcode_project_read ON public.code_imports
    USING (project_id = ANY (ARRAY[gobby_agent_auth.current_project_id(), gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_review_code_project_id()]));
ALTER POLICY gobby_gcode_project_read ON public.code_index_projection_cleanup_pending
    USING (project_id = ANY (ARRAY[gobby_agent_auth.current_project_id(), gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_review_code_project_id()]));
ALTER POLICY gobby_gcode_project_read ON public.code_index_prune_dirty_projects
    USING (project_id = ANY (ARRAY[gobby_agent_auth.current_project_id(), gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_review_code_project_id()]) AND machine_id = gobby_agent_auth.current_machine_id());
ALTER POLICY gobby_gcode_project_read ON public.code_indexed_file_states
    USING (project_id = ANY (ARRAY[gobby_agent_auth.current_project_id(), gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_review_code_project_id()]) AND machine_id = gobby_agent_auth.current_machine_id());
ALTER POLICY gobby_gcode_project_read ON public.code_indexed_files
    USING (project_id = ANY (ARRAY[gobby_agent_auth.current_project_id(), gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_review_code_project_id()]));
ALTER POLICY gobby_gcode_project_read ON public.code_indexed_project_states
    USING (project_id = ANY (ARRAY[gobby_agent_auth.current_project_id(), gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_review_code_project_id()]) AND machine_id = gobby_agent_auth.current_machine_id());
ALTER POLICY gobby_gcode_project_read ON public.code_indexed_projects
    USING (id = ANY (ARRAY[gobby_agent_auth.current_project_id(), gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_review_code_project_id()]));
ALTER POLICY gobby_gcode_project_read ON public.code_inheritance
    USING (project_id = ANY (ARRAY[gobby_agent_auth.current_project_id(), gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_review_code_project_id()]));
ALTER POLICY gobby_gcode_project_read ON public.code_symbols
    USING (project_id = ANY (ARRAY[gobby_agent_auth.current_project_id(), gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_review_code_project_id()]));
ALTER POLICY gobby_gcode_project_read ON public.code_communities
    USING (project_id = ANY (ARRAY[gobby_agent_auth.current_project_id(), gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_review_code_project_id()]) AND machine_id = gobby_agent_auth.current_machine_id());
