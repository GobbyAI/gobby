-- Bind an agent run without its own isolation workspace (a task-close reviewer)
-- to the code-index overlay of the registered worktree it was launched in, so
-- gcode can write that overlay. Any other requested path binds no overlay.
DROP FUNCTION gobby_agent_auth.issue_principal(uuid, text, uuid, uuid, uuid, timestamp with time zone, text);

CREATE FUNCTION gobby_agent_auth.issue_principal(requested_execution_id uuid, requested_owner_kind text, requested_session_id uuid, requested_agent_run_id uuid, requested_machine_id uuid, requested_expires_at timestamp with time zone, requested_password text, requested_project_path text DEFAULT NULL) RETURNS TABLE(role_name name, credential_generation integer)
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'gobby_agent_auth', 'pg_temp'
    SET createrole_self_grant TO ''
    AS $$
DECLARE
    resolved_project_id UUID;
    resolved_agent_run_id UUID;
    resolved_run_machine_id UUID;
    resolved_worktree_id UUID;
    resolved_clone_id UUID;
    resolved_workspace_path TEXT;
    resolved_overlay_project_id UUID;
    derived_role_name NAME;
    binding_id UUID;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(requested_execution_id::TEXT, 0));
    IF requested_owner_kind NOT IN ('agent_run', 'tool_chat') THEN
        RAISE EXCEPTION 'unsupported managed principal owner kind'
            USING ERRCODE = '22023';
    END IF;
    IF requested_expires_at <= clock_timestamp() THEN
        RAISE EXCEPTION 'managed principal expiry must be in the future'
            USING ERRCODE = '22023';
    END IF;
    IF requested_password IS NULL OR requested_password = '' THEN
        RAISE EXCEPTION 'managed principal password must not be empty'
            USING ERRCODE = '22023';
    END IF;
    IF EXISTS (
        SELECT 1 FROM principal_bindings
        WHERE managed_execution_id = requested_execution_id
    ) THEN
        RAISE EXCEPTION 'managed execution already has a principal binding'
            USING ERRCODE = '23505';
    END IF;

    SELECT project_id, agent_run_id
    INTO resolved_project_id, resolved_agent_run_id
    FROM public.sessions
    WHERE id = requested_session_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'managed principal session does not exist'
            USING ERRCODE = '23503';
    END IF;
    IF resolved_agent_run_id IS DISTINCT FROM requested_agent_run_id THEN
        RAISE EXCEPTION 'managed principal agent run does not match its session'
            USING ERRCODE = '23503';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.machines WHERE id = requested_machine_id) THEN
        RAISE EXCEPTION 'managed principal issuing machine does not exist'
            USING ERRCODE = '23503';
    END IF;

    IF requested_owner_kind = 'agent_run' THEN
        IF requested_agent_run_id IS NULL THEN
            RAISE EXCEPTION 'agent-run principal requires an agent run'
                USING ERRCODE = '23503';
        END IF;
        SELECT machine_id, worktree_id, clone_id
        INTO resolved_run_machine_id, resolved_worktree_id, resolved_clone_id
        FROM public.agent_runs
        WHERE id = requested_agent_run_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'managed principal agent run does not exist'
                USING ERRCODE = '23503';
        END IF;
        IF resolved_run_machine_id IS DISTINCT FROM requested_machine_id THEN
            RAISE EXCEPTION 'managed principal agent run belongs to another machine'
                USING ERRCODE = '23503';
        END IF;
        IF resolved_worktree_id IS NOT NULL AND resolved_clone_id IS NOT NULL THEN
            RAISE EXCEPTION 'managed principal agent run has multiple isolation workspaces'
                USING ERRCODE = '23514';
        END IF;
        IF resolved_worktree_id IS NOT NULL THEN
            SELECT worktree_path INTO resolved_workspace_path
            FROM public.worktrees
            WHERE id = resolved_worktree_id
              AND project_id = resolved_project_id
              AND machine_id = requested_machine_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'managed principal worktree does not match project and machine'
                    USING ERRCODE = '23503';
            END IF;
        ELSIF resolved_clone_id IS NOT NULL THEN
            SELECT clone_path INTO resolved_workspace_path
            FROM public.clones
            WHERE id = resolved_clone_id
              AND project_id = resolved_project_id
              AND machine_id = requested_machine_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'managed principal clone does not match project and machine'
                    USING ERRCODE = '23503';
            END IF;
        ELSIF requested_project_path IS NOT NULL THEN
            SELECT worktree_path INTO resolved_workspace_path
            FROM public.worktrees
            WHERE worktree_path = requested_project_path
              AND project_id = resolved_project_id
              AND machine_id = requested_machine_id;
        END IF;
        IF resolved_workspace_path IS NOT NULL THEN
            resolved_overlay_project_id := code_index_project_id(resolved_workspace_path);
        END IF;
    END IF;

    derived_role_name := (
        'gobby_agent_' || replace(requested_execution_id::TEXT, '-', '') || '_1'
    )::NAME;
    EXECUTE format(
        'CREATE ROLE %I LOGIN PASSWORD %L VALID UNTIL %L INHERIT '
        'NOSUPERUSER NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS',
        derived_role_name,
        requested_password,
        requested_expires_at
    );
    EXECUTE format(
        'GRANT %I TO %I WITH ADMIN FALSE, INHERIT TRUE, SET FALSE',
        'gobby_gcode_capability',
        derived_role_name
    );

    INSERT INTO principal_bindings (
        role_name,
        owner_kind,
        managed_execution_id,
        agent_run_id,
        session_id,
        project_id,
        code_overlay_project_id,
        issuing_machine_id,
        expires_at,
        credential_generation
    ) VALUES (
        derived_role_name,
        requested_owner_kind,
        requested_execution_id,
        requested_agent_run_id,
        requested_session_id,
        resolved_project_id,
        resolved_overlay_project_id,
        requested_machine_id,
        requested_expires_at,
        1
    ) RETURNING id INTO binding_id;

    INSERT INTO principal_audit_events (
        binding_id,
        event_type,
        managed_execution_id,
        role_name,
        credential_generation,
        project_id
    ) VALUES (
        binding_id,
        'issue',
        requested_execution_id,
        derived_role_name,
        1,
        resolved_project_id
    );
    RETURN QUERY SELECT derived_role_name, 1;
END
$$;

ALTER FUNCTION gobby_agent_auth.issue_principal(uuid, text, uuid, uuid, uuid, timestamp with time zone, text, text) OWNER TO gobby_agent_issuer;
REVOKE ALL ON FUNCTION gobby_agent_auth.issue_principal(uuid, text, uuid, uuid, uuid, timestamp with time zone, text, text) FROM PUBLIC;
GRANT ALL ON FUNCTION gobby_agent_auth.issue_principal(uuid, text, uuid, uuid, uuid, timestamp with time zone, text, text) TO gobby_daemon_runtime;
