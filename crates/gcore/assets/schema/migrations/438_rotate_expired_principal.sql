-- Handshake refresh after the 1-hour managed principal expires used to fail
-- with claims_mismatch: rotate_principal and rotate_principal_if_generation
-- required expires_at > now(), and issue_principal refuses any existing row
-- for the execution. An expired unrevoked predecessor is still rotatable.

CREATE OR REPLACE FUNCTION gobby_agent_auth.rotate_principal(
    requested_execution_id uuid,
    requested_expires_at timestamp with time zone,
    requested_password text
) RETURNS TABLE(role_name name, credential_generation integer)
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'gobby_agent_auth', 'pg_temp'
    SET createrole_self_grant TO ''
    AS $$
DECLARE
    source_binding principal_bindings%ROWTYPE;
    active_count INTEGER;
    next_generation INTEGER;
    derived_role_name NAME;
    binding_id UUID;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(requested_execution_id::TEXT, 0));
    IF requested_expires_at <= clock_timestamp() THEN
        RAISE EXCEPTION 'managed principal expiry must be in the future'
            USING ERRCODE = '22023';
    END IF;
    IF requested_password IS NULL OR requested_password = '' THEN
        RAISE EXCEPTION 'managed principal password must not be empty'
            USING ERRCODE = '22023';
    END IF;
    SELECT count(*) INTO active_count
    FROM principal_bindings
    WHERE managed_execution_id = requested_execution_id
      AND revoked_at IS NULL;
    IF active_count <> 1 THEN
        RAISE EXCEPTION 'rotation requires exactly one active principal binding'
            USING ERRCODE = '42501';
    END IF;
    SELECT * INTO STRICT source_binding
    FROM principal_bindings
    WHERE managed_execution_id = requested_execution_id
      AND revoked_at IS NULL;
    SELECT COALESCE(max(pb.credential_generation), 0) + 1
    INTO next_generation
    FROM principal_bindings pb
    WHERE pb.managed_execution_id = requested_execution_id;

    derived_role_name := (
        'gobby_agent_' || replace(requested_execution_id::TEXT, '-', '') || '_' || next_generation
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
        source_binding.owner_kind,
        requested_execution_id,
        source_binding.agent_run_id,
        source_binding.session_id,
        source_binding.project_id,
        source_binding.code_overlay_project_id,
        source_binding.issuing_machine_id,
        requested_expires_at,
        next_generation
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
        'rotate',
        requested_execution_id,
        derived_role_name,
        next_generation,
        source_binding.project_id
    );
    RETURN QUERY SELECT derived_role_name, next_generation;
END
$$;

ALTER FUNCTION gobby_agent_auth.rotate_principal(
    requested_execution_id uuid,
    requested_expires_at timestamp with time zone,
    requested_password text
) OWNER TO gobby_agent_issuer;

CREATE OR REPLACE FUNCTION gobby_agent_auth.rotate_principal_if_generation(
    p_execution_id uuid,
    p_expected_generation integer,
    p_expires_at timestamp with time zone,
    p_password text
) RETURNS TABLE(role_name name, credential_generation integer)
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'gobby_agent_auth', 'pg_temp'
    SET createrole_self_grant TO ''
    AS $$
DECLARE
    v_current_generation INTEGER;
    v_role_name NAME;
    v_generation INTEGER;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(p_execution_id::TEXT, 0));
    SELECT max(pb.credential_generation) INTO v_current_generation
    FROM principal_bindings AS pb
    WHERE pb.managed_execution_id = p_execution_id
      AND pb.revoked_at IS NULL;
    IF v_current_generation IS DISTINCT FROM p_expected_generation THEN
        RETURN;
    END IF;

    SELECT rotated.role_name, rotated.credential_generation
    INTO v_role_name, v_generation
    FROM rotate_principal(p_execution_id, p_expires_at, p_password) AS rotated;

    UPDATE principal_bindings AS predecessor
    SET revocation_requested_at = statement_timestamp(),
        predecessor_drain_deadline = statement_timestamp() + INTERVAL '5 minutes'
    WHERE predecessor.managed_execution_id = p_execution_id
      AND predecessor.credential_generation = p_expected_generation
      AND predecessor.revoked_at IS NULL;
    RETURN QUERY SELECT v_role_name, v_generation;
END
$$;

ALTER FUNCTION gobby_agent_auth.rotate_principal_if_generation(
    p_execution_id uuid,
    p_expected_generation integer,
    p_expires_at timestamp with time zone,
    p_password text
) OWNER TO gobby_agent_issuer;
