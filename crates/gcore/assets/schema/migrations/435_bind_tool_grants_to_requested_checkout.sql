-- Bind caller-root tool grants to their registered overlay, independent of
-- the issuing session's own workspace. Keep session-derived issuance for
-- callers that do not supply an operation root.
GRANT SELECT(machine_id) ON public.sessions TO gobby_agent_issuer;
GRANT SELECT(machine_id, project_id, root_path) ON public.project_checkouts TO gobby_agent_issuer;
CREATE POLICY gobby_agent_issuer_read ON public.project_checkouts
    FOR SELECT TO gobby_agent_issuer USING (true);
DROP FUNCTION gobby_agent_auth.issue_tool_principal(uuid, uuid, uuid, timestamp with time zone, text);

CREATE FUNCTION gobby_agent_auth.issue_tool_principal(p_execution_id uuid, p_session_id uuid, p_machine_id uuid, p_expires_at timestamp with time zone, p_password text, p_requested_project_path text DEFAULT NULL) RETURNS TABLE(role_name name, credential_generation integer)
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'gobby_agent_auth', 'pg_temp'
    SET createrole_self_grant TO ''
    AS $$
DECLARE
    v_project_id UUID;
    v_workspace_count INTEGER;
    v_workspace_path TEXT;
    v_overlay_project_id UUID;
    v_role_name NAME;
    v_binding_id UUID;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(p_execution_id::TEXT, 0));
    IF p_expires_at <= clock_timestamp() THEN
        RAISE EXCEPTION 'managed principal expiry must be in the future'
            USING ERRCODE = '22023';
    END IF;
    IF p_password IS NULL OR p_password = '' THEN
        RAISE EXCEPTION 'managed principal password must not be empty'
            USING ERRCODE = '22023';
    END IF;
    IF EXISTS (
        SELECT 1 FROM principal_bindings
        WHERE managed_execution_id = p_execution_id
    ) THEN
        RAISE EXCEPTION 'managed execution already has a principal binding'
            USING ERRCODE = '23505';
    END IF;
    SELECT session.project_id INTO v_project_id
    FROM public.sessions AS session
    WHERE session.id = p_session_id
      AND session.machine_id = p_machine_id
      AND COALESCE(session.status, 'active') NOT IN ('expired', 'deleted');
    IF NOT FOUND THEN
        RAISE EXCEPTION 'managed principal session does not exist'
            USING ERRCODE = '23503';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.machines WHERE id = p_machine_id) THEN
        RAISE EXCEPTION 'managed principal issuing machine does not exist'
            USING ERRCODE = '23503';
    END IF;

    IF p_requested_project_path IS NOT NULL THEN
        IF NOT EXISTS (
            SELECT 1 FROM public.project_checkouts
            WHERE machine_id = p_machine_id AND project_id = v_project_id
              AND root_path = p_requested_project_path
        ) THEN
            v_overlay_project_id := code_index_project_id(p_requested_project_path);
            PERFORM assert_interactive_overlay_registered(p_machine_id, v_project_id, v_overlay_project_id);
        END IF;
    ELSE
        SELECT count(*), min(workspace.workspace_path)
        INTO v_workspace_count, v_workspace_path
        FROM (
            SELECT worktree.worktree_path AS workspace_path
            FROM public.worktrees AS worktree
            WHERE worktree.agent_session_id = p_session_id
              AND worktree.project_id = v_project_id
              AND worktree.machine_id = p_machine_id
            UNION ALL
            SELECT clone.clone_path AS workspace_path
            FROM public.clones AS clone
            WHERE clone.agent_session_id = p_session_id
              AND clone.project_id = v_project_id
              AND clone.machine_id = p_machine_id
        ) AS workspace;
        IF v_workspace_count > 1 THEN
            RAISE EXCEPTION 'tool-chat principal session has multiple isolation workspaces'
                USING ERRCODE = '23514';
        END IF;
        IF v_workspace_count = 1 THEN
            v_overlay_project_id := code_index_project_id(v_workspace_path);
        END IF;
    END IF;

    v_role_name := (
        'gobby_agent_' || replace(p_execution_id::TEXT, '-', '') || '_1'
    )::NAME;
    EXECUTE format(
        'CREATE ROLE %I LOGIN PASSWORD %L VALID UNTIL %L INHERIT '
        'NOSUPERUSER NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS',
        v_role_name,
        p_password,
        p_expires_at
    );
    EXECUTE format(
        'GRANT %I TO %I WITH ADMIN FALSE, INHERIT TRUE, SET FALSE',
        'gobby_gcode_capability',
        v_role_name
    );
    INSERT INTO principal_bindings (
        role_name, owner_kind, managed_execution_id, agent_run_id,
        session_id, project_id, code_overlay_project_id, issuing_machine_id, expires_at,
        credential_generation
    ) VALUES (
        v_role_name, 'tool_chat', p_execution_id, NULL,
        p_session_id, v_project_id, v_overlay_project_id, p_machine_id, p_expires_at, 1
    ) RETURNING id INTO v_binding_id;
    INSERT INTO principal_audit_events (
        binding_id, event_type, managed_execution_id, role_name,
        credential_generation, project_id
    ) VALUES (
        v_binding_id, 'issue', p_execution_id, v_role_name, 1, v_project_id
    );
    RETURN QUERY SELECT v_role_name, 1;
END
$$;

ALTER FUNCTION gobby_agent_auth.issue_tool_principal(uuid, uuid, uuid, timestamp with time zone, text, text) OWNER TO gobby_agent_issuer;
REVOKE ALL ON FUNCTION gobby_agent_auth.issue_tool_principal(uuid, uuid, uuid, timestamp with time zone, text, text) FROM PUBLIC;
GRANT ALL ON FUNCTION gobby_agent_auth.issue_tool_principal(uuid, uuid, uuid, timestamp with time zone, text, text) TO gobby_daemon_runtime;
