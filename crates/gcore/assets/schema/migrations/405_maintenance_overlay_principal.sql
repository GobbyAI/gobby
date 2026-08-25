-- Let maintenance principals carry a code-index overlay binding.
--
-- Daemon-side gcode launches (the incremental index trigger, maintenance)
-- that target a registered worktree or clone must write under that
-- workspace's derived code-index project id. The code_* RLS write policies
-- already admit current_code_overlay_project_id(); this migration lets the
-- maintenance issuer populate that binding column, gated by the same
-- registered-workspace proof the interactive issuer uses
-- (assert_interactive_overlay_registered, migration 403). Without it, every
-- daemon-triggered incremental index of a worktree fails RLS on
-- code_indexed_projects because the grant carries only the parent project.

DROP FUNCTION IF EXISTS gobby_agent_auth.issue_maintenance_principal(
    UUID, UUID, UUID, TIMESTAMPTZ, TEXT
);

CREATE OR REPLACE FUNCTION gobby_agent_auth.issue_maintenance_principal(
    p_execution_id UUID,
    p_project_id UUID,
    p_machine_id UUID,
    p_expires_at TIMESTAMPTZ,
    p_password TEXT,
    p_code_overlay_project_id UUID
)
RETURNS TABLE(role_name NAME, credential_generation INTEGER)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = gobby_agent_auth, pg_temp
SET createrole_self_grant = ''
AS $function$
DECLARE
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
    IF NOT EXISTS (SELECT 1 FROM public.machines WHERE id = p_machine_id) THEN
        RAISE EXCEPTION 'managed principal issuing machine does not exist'
            USING ERRCODE = '23503';
    END IF;
    PERFORM assert_interactive_overlay_registered(
        p_machine_id, p_project_id, p_code_overlay_project_id
    );

    v_role_name := (
        'gobby_mnt_' || replace(p_execution_id::TEXT, '-', '') || '_1'
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
        session_id, project_id, code_overlay_project_id, issuing_machine_id,
        expires_at, credential_generation
    ) VALUES (
        v_role_name, 'maintenance', p_execution_id, NULL,
        p_execution_id, p_project_id, p_code_overlay_project_id, p_machine_id,
        p_expires_at, 1
    ) RETURNING id INTO v_binding_id;
    INSERT INTO principal_audit_events (
        binding_id, event_type, managed_execution_id, role_name,
        credential_generation, project_id
    ) VALUES (
        v_binding_id, 'issue', p_execution_id, v_role_name, 1, p_project_id
    );
    RETURN QUERY SELECT v_role_name, 1;
END
$function$;

ALTER FUNCTION gobby_agent_auth.issue_maintenance_principal(
    UUID, UUID, UUID, TIMESTAMPTZ, TEXT, UUID
) OWNER TO gobby_agent_issuer;
REVOKE ALL ON FUNCTION gobby_agent_auth.issue_maintenance_principal(
    UUID, UUID, UUID, TIMESTAMPTZ, TEXT, UUID
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION gobby_agent_auth.issue_maintenance_principal(
    UUID, UUID, UUID, TIMESTAMPTZ, TEXT, UUID
) TO gobby_daemon_runtime;
