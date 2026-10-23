-- Live registration widens reads only. Principal bindings and every write policy
-- retain their issuance-time authority. No review assignment is needed to read.
GRANT SELECT (status) ON public.clones TO gobby_agent_issuer;

CREATE FUNCTION gobby_agent_auth.current_read_code_project_ids() RETURNS uuid[]
    LANGUAGE plpgsql STABLE SECURITY DEFINER PARALLEL SAFE
    SET search_path TO 'pg_catalog', 'gobby_agent_auth'
    AS $$
DECLARE
    binding gobby_agent_auth.principal_bindings%ROWTYPE;
BEGIN
    SELECT * INTO STRICT binding FROM gobby_agent_auth.principal_bindings
    WHERE role_name = session_user::name AND revoked_at IS NULL
      AND expires_at > clock_timestamp();

    RETURN ARRAY(
        SELECT candidate_id FROM (
            SELECT binding.project_id AS candidate_id
            UNION
            SELECT binding.code_overlay_project_id
            UNION
            SELECT gobby_agent_auth.code_index_project_id(worktree.worktree_path)
            FROM public.worktrees worktree
            WHERE worktree.project_id = binding.project_id
              AND worktree.machine_id = binding.issuing_machine_id
              AND worktree.status = 'active'
            UNION
            SELECT gobby_agent_auth.code_index_project_id(clone.clone_path)
            FROM public.clones clone
            WHERE clone.project_id = binding.project_id
              AND clone.machine_id = binding.issuing_machine_id
              AND clone.status = 'active'
        ) scopes WHERE candidate_id IS NOT NULL ORDER BY candidate_id
    );
END
$$;
ALTER FUNCTION gobby_agent_auth.current_read_code_project_ids() OWNER TO gobby_agent_issuer;
REVOKE ALL ON FUNCTION gobby_agent_auth.current_read_code_project_ids() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION gobby_agent_auth.current_read_code_project_ids() TO gobby_gcode_capability;

-- Keep a single ANY qual so pg_search can push the scope into BM25 scans.
-- Machine-keyed state retains its separate issuing-machine restriction.
ALTER POLICY gobby_gcode_project_read ON public.code_calls
    USING (project_id = ANY (gobby_agent_auth.current_read_code_project_ids()));
ALTER POLICY gobby_gcode_project_read ON public.code_content_chunks
    USING (project_id = ANY (gobby_agent_auth.current_read_code_project_ids()));
ALTER POLICY gobby_gcode_project_read ON public.code_imports
    USING (project_id = ANY (gobby_agent_auth.current_read_code_project_ids()));
ALTER POLICY gobby_gcode_project_read ON public.code_index_projection_cleanup_pending
    USING (project_id = ANY (gobby_agent_auth.current_read_code_project_ids()));
ALTER POLICY gobby_gcode_project_read ON public.code_index_prune_dirty_projects
    USING (project_id = ANY (gobby_agent_auth.current_read_code_project_ids()) AND machine_id = gobby_agent_auth.current_machine_id());
ALTER POLICY gobby_gcode_project_read ON public.code_indexed_file_states
    USING (project_id = ANY (gobby_agent_auth.current_read_code_project_ids()) AND machine_id = gobby_agent_auth.current_machine_id());
ALTER POLICY gobby_gcode_project_read ON public.code_indexed_files
    USING (project_id = ANY (gobby_agent_auth.current_read_code_project_ids()));
ALTER POLICY gobby_gcode_project_read ON public.code_indexed_project_states
    USING (project_id = ANY (gobby_agent_auth.current_read_code_project_ids()) AND machine_id = gobby_agent_auth.current_machine_id());
ALTER POLICY gobby_gcode_project_read ON public.code_indexed_projects
    USING (id = ANY (gobby_agent_auth.current_read_code_project_ids()));
ALTER POLICY gobby_gcode_project_read ON public.code_inheritance
    USING (project_id = ANY (gobby_agent_auth.current_read_code_project_ids()));
ALTER POLICY gobby_gcode_project_read ON public.code_symbols
    USING (project_id = ANY (gobby_agent_auth.current_read_code_project_ids()));
ALTER POLICY gobby_gcode_project_read ON public.code_communities
    USING (project_id = ANY (gobby_agent_auth.current_read_code_project_ids()) AND machine_id = gobby_agent_auth.current_machine_id());

DROP FUNCTION gobby_agent_auth.current_review_code_project_id();
