-- Current authority is one unrevoked binding without a revocation request.
-- At most one adjacent predecessor may drain, with matching owner identity.
CREATE FUNCTION gobby_agent_auth.managed_binding_states(p_machine_id uuid)
RETURNS TABLE (
    managed_execution_id uuid, role_name name, credential_generation integer,
    owner_kind text, agent_run_id uuid, session_id uuid, project_id uuid,
    issuing_machine_id uuid, expires_at timestamptz,
    revocation_requested_at timestamptz, predecessor_drain_deadline timestamptz,
    login_capable boolean, code_overlay_project_id uuid
)
LANGUAGE sql SECURITY DEFINER
SET search_path TO 'pg_catalog', 'gobby_agent_auth'
AS $$
    SELECT pb.managed_execution_id, pb.role_name, pb.credential_generation,
           pb.owner_kind, pb.agent_run_id, pb.session_id, pb.project_id,
           pb.issuing_machine_id, pb.expires_at, pb.revocation_requested_at,
           pb.predecessor_drain_deadline, COALESCE(roles.rolcanlogin, FALSE), pb.code_overlay_project_id
    FROM gobby_agent_auth.principal_bindings pb
    LEFT JOIN pg_catalog.pg_roles roles ON roles.rolname = pb.role_name
    WHERE pb.issuing_machine_id = p_machine_id AND pb.revoked_at IS NULL
    ORDER BY pb.managed_execution_id, pb.credential_generation
$$;
ALTER FUNCTION gobby_agent_auth.managed_binding_states(uuid) OWNER TO gobby_agent_issuer;
REVOKE ALL ON FUNCTION gobby_agent_auth.managed_binding_states(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION gobby_agent_auth.managed_binding_states(uuid) TO gobby_daemon_runtime;

-- Private predicate shared by selection, cleanup and rotation. It exposes no
-- binding row and is not executable by runtime or seat roles directly.
CREATE FUNCTION gobby_agent_auth.is_current_rotation_binding(p_execution_id uuid, p_generation integer)
RETURNS boolean LANGUAGE sql SECURITY DEFINER
SET search_path TO 'pg_catalog', 'gobby_agent_auth'
AS $$
    SELECT EXISTS (
        SELECT 1 FROM gobby_agent_auth.principal_bindings current
        WHERE current.managed_execution_id = p_execution_id
          AND current.credential_generation = p_generation
          AND current.revoked_at IS NULL
          AND current.revocation_requested_at IS NULL
          AND current.predecessor_drain_deadline IS NULL
          AND (SELECT count(*) FROM gobby_agent_auth.principal_bindings pb
               WHERE pb.managed_execution_id = p_execution_id AND pb.revoked_at IS NULL) <= 2
          AND NOT EXISTS (
              SELECT 1 FROM gobby_agent_auth.principal_bindings other
              WHERE other.managed_execution_id = p_execution_id AND other.revoked_at IS NULL
                AND other.id <> current.id
                AND NOT (
                    other.credential_generation = current.credential_generation - 1
                    AND other.revocation_requested_at IS NOT NULL
                    AND other.predecessor_drain_deadline IS NOT NULL
                    AND other.predecessor_drain_deadline > other.revocation_requested_at
                    AND other.predecessor_drain_deadline <= other.revocation_requested_at + INTERVAL '5 minutes'
                    AND ROW(other.owner_kind, other.agent_run_id, other.session_id,
                            other.project_id, other.issuing_machine_id, other.code_overlay_project_id)
                        IS NOT DISTINCT FROM
                        ROW(current.owner_kind, current.agent_run_id, current.session_id,
                            current.project_id, current.issuing_machine_id, current.code_overlay_project_id)
                )
          )
    )
$$;
ALTER FUNCTION gobby_agent_auth.is_current_rotation_binding(uuid, integer) OWNER TO gobby_agent_issuer;
REVOKE ALL ON FUNCTION gobby_agent_auth.is_current_rotation_binding(uuid, integer) FROM PUBLIC;

CREATE FUNCTION gobby_agent_auth.drain_rotation_predecessors(
    p_machine_id uuid, p_execution_id uuid DEFAULT NULL
) RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
SET search_path TO 'gobby_agent_auth', 'pg_temp'
SET createrole_self_grant TO ''
AS $$
DECLARE
    candidate RECORD;
    predecessor RECORD;
    result_count integer;
    drained_count integer := 0;
BEGIN
    FOR candidate IN
        SELECT current.managed_execution_id, current.credential_generation
        FROM principal_bindings current
        WHERE current.issuing_machine_id = p_machine_id
          AND (p_execution_id IS NULL OR current.managed_execution_id = p_execution_id)
          AND current.revoked_at IS NULL AND current.revocation_requested_at IS NULL
          AND is_current_rotation_binding(current.managed_execution_id, current.credential_generation)
          AND EXISTS (
              SELECT 1 FROM principal_bindings old
              WHERE old.managed_execution_id = current.managed_execution_id
                AND old.revoked_at IS NULL AND old.predecessor_drain_deadline IS NOT NULL
                AND (old.predecessor_drain_deadline <= clock_timestamp()
                     OR current.expires_at <= clock_timestamp())
          )
        ORDER BY current.managed_execution_id LIMIT 32
    LOOP
        PERFORM pg_advisory_xact_lock(hashtextextended(candidate.managed_execution_id::text, 0));
        IF NOT is_current_rotation_binding(candidate.managed_execution_id, candidate.credential_generation) THEN
            CONTINUE;
        END IF;
        -- Residual pre-migration state: never starve an expired current binding
        -- or create a second drainer. Normal rotations cap the old deadline at
        -- successor expiry, so this only shortens an exceptional grace window.
        UPDATE principal_bindings old
        SET predecessor_drain_deadline = LEAST(old.predecessor_drain_deadline, clock_timestamp())
        FROM principal_bindings current
        WHERE current.managed_execution_id = candidate.managed_execution_id
          AND current.credential_generation = candidate.credential_generation
          AND current.expires_at <= clock_timestamp()
          AND old.managed_execution_id = current.managed_execution_id
          AND old.revoked_at IS NULL AND old.predecessor_drain_deadline IS NOT NULL;
        FOR predecessor IN
            SELECT pb.credential_generation FROM principal_bindings pb
            WHERE pb.managed_execution_id = candidate.managed_execution_id
              AND pb.revoked_at IS NULL AND pb.predecessor_drain_deadline <= clock_timestamp()
        LOOP
            result_count := revoke_principal(candidate.managed_execution_id, predecessor.credential_generation);
            IF result_count > 0 THEN
                drained_count := drained_count + result_count;
            END IF;
        END LOOP;
    END LOOP;
    RETURN drained_count;
END
$$;
ALTER FUNCTION gobby_agent_auth.drain_rotation_predecessors(uuid, uuid) OWNER TO gobby_agent_issuer;
REVOKE ALL ON FUNCTION gobby_agent_auth.drain_rotation_predecessors(uuid, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION gobby_agent_auth.drain_rotation_predecessors(uuid, uuid) TO gobby_daemon_runtime;

CREATE OR REPLACE FUNCTION gobby_agent_auth.principals_due_for_rotation(p_machine_id uuid)
RETURNS TABLE(managed_execution_id uuid, role_name name, credential_generation integer)
LANGUAGE sql SECURITY DEFINER
SET search_path TO 'pg_catalog', 'gobby_agent_auth'
AS $$
    SELECT current.managed_execution_id, current.role_name, current.credential_generation
    FROM gobby_agent_auth.principal_bindings current
    WHERE current.issuing_machine_id = p_machine_id
      AND current.revoked_at IS NULL AND current.revocation_requested_at IS NULL
      AND current.owner_kind <> 'interactive'
      AND (current.issued_at <= clock_timestamp() - INTERVAL '45 minutes'
           OR current.expires_at <= clock_timestamp())
      AND gobby_agent_auth.is_current_rotation_binding(current.managed_execution_id, current.credential_generation)
      AND NOT EXISTS (
          SELECT 1 FROM gobby_agent_auth.principal_bindings old
          WHERE old.managed_execution_id = current.managed_execution_id
            AND old.revoked_at IS NULL AND old.predecessor_drain_deadline IS NOT NULL
      )
    ORDER BY current.managed_execution_id
$$;

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
      AND revoked_at IS NULL
      AND revocation_requested_at IS NULL;
    IF active_count <> 1 THEN
        RAISE EXCEPTION 'rotation requires exactly one active principal binding'
            USING ERRCODE = '42501';
    END IF;
    SELECT * INTO STRICT source_binding
    FROM principal_bindings
    WHERE managed_execution_id = requested_execution_id
      AND revoked_at IS NULL
      AND revocation_requested_at IS NULL;
    IF NOT is_current_rotation_binding(requested_execution_id, source_binding.credential_generation)
       OR EXISTS (SELECT 1 FROM principal_bindings pb
                  WHERE pb.managed_execution_id = requested_execution_id AND pb.revoked_at IS NULL
                    AND pb.predecessor_drain_deadline IS NOT NULL) THEN
        RAISE EXCEPTION 'rotation requires one current binding without a draining predecessor'
            USING ERRCODE = '42501';
    END IF;
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
    v_machine_id UUID;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(p_execution_id::TEXT, 0));
    SELECT pb.issuing_machine_id INTO v_machine_id
    FROM principal_bindings pb
    WHERE pb.managed_execution_id = p_execution_id AND pb.revoked_at IS NULL
      AND pb.revocation_requested_at IS NULL
      AND is_current_rotation_binding(p_execution_id, pb.credential_generation);
    IF v_machine_id IS NULL THEN
        RETURN;
    END IF;
    PERFORM drain_rotation_predecessors(v_machine_id, p_execution_id);
    IF EXISTS (SELECT 1 FROM principal_bindings pb
               WHERE pb.managed_execution_id = p_execution_id AND pb.revoked_at IS NULL
                 AND pb.predecessor_drain_deadline IS NOT NULL) THEN
        RETURN;
    END IF;
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
        predecessor_drain_deadline = LEAST(statement_timestamp() + INTERVAL '5 minutes', p_expires_at)
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

-- Rotation requests revocation while advertising a predecessor drain deadline.
-- Reconciliation waits for that deadline. Explicit revoke attempts remain immediate.

CREATE OR REPLACE FUNCTION gobby_agent_auth.reconcile_daemon(p_machine_id uuid) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'gobby_agent_auth', 'pg_temp'
    SET createrole_self_grant TO ''
    AS $_$
DECLARE
    candidate RECORD;
    orphan_role RECORD;
    result_count INTEGER;
    remaining_sessions INTEGER;
    reconciled_count INTEGER := 0;
    retry_pending BOOLEAN := FALSE;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended('gobby-agent-auth-reconcile', 0));
    IF NOT EXISTS (SELECT 1 FROM public.machines WHERE id = p_machine_id) THEN
        RAISE EXCEPTION 'unknown reconciling machine' USING ERRCODE = '23503';
    END IF;

    FOR candidate IN
        SELECT pb.managed_execution_id, pb.credential_generation
        FROM principal_bindings AS pb
        LEFT JOIN daemon_registry AS daemon
          ON daemon.machine_id = pb.issuing_machine_id
        LEFT JOIN public.agent_runs AS run
          ON run.id = pb.agent_run_id
        WHERE pb.revoked_at IS NULL
          AND (
              pb.issuing_machine_id = p_machine_id
              OR daemon.lease_expires_at IS NULL
              OR daemon.lease_expires_at <= clock_timestamp()
          )
          AND (
              (pb.predecessor_drain_deadline IS NULL AND pb.expires_at <= clock_timestamp())
              OR pb.predecessor_drain_deadline <= clock_timestamp()
              OR (
                  pb.revocation_requested_at IS NOT NULL
                  AND (
                      pb.predecessor_drain_deadline IS NULL
                      OR pb.revocation_attempts > 0
                  )
              )
              OR (
                  pb.owner_kind = 'agent_run'
                  AND run.status IN ('success', 'error', 'timeout', 'cancelled')
              )
          )
        ORDER BY pb.managed_execution_id, pb.credential_generation
    LOOP
        result_count := revoke_principal(
            candidate.managed_execution_id,
            candidate.credential_generation
        );
        IF result_count > 0 THEN
            reconciled_count := reconciled_count + result_count;
        ELSIF result_count < 0 THEN
            retry_pending := TRUE;
        END IF;
    END LOOP;

    FOR orphan_role IN
        SELECT roles.rolname
        FROM pg_roles AS roles
        WHERE roles.rolname ~ '^(gobby_agent_[0-9a-f]{32}|gobby_ix_([0-9a-f]{16}|[A-Za-z0-9]{1,8}_[0-9a-f]{8}_[0-9a-f]{8})|gobby_mnt_[0-9a-f]{32})_[1-9][0-9]*$'
          AND NOT EXISTS (
              SELECT 1 FROM principal_bindings AS binding
              WHERE binding.role_name = roles.rolname
          )
        ORDER BY roles.rolname
    LOOP
        EXECUTE format('ALTER ROLE %I NOLOGIN', orphan_role.rolname);
        PERFORM pg_terminate_backend(pid, 5000)
        FROM pg_stat_activity
        WHERE usename = orphan_role.rolname::TEXT
          AND pid <> pg_backend_pid();
        SELECT count(*) INTO remaining_sessions
        FROM pg_stat_activity
        WHERE usename = orphan_role.rolname::TEXT;
        IF remaining_sessions <> 0 THEN
            INSERT INTO orphan_revocation_retries (
                role_name, revocation_attempts, next_retry_at, last_failure, updated_at
            ) VALUES (
                orphan_role.rolname, 1, clock_timestamp() + INTERVAL '15 seconds',
                'active_sessions_remaining', clock_timestamp()
            )
            ON CONFLICT (role_name) DO UPDATE
            SET revocation_attempts = orphan_revocation_retries.revocation_attempts + 1,
                next_retry_at = EXCLUDED.next_retry_at,
                last_failure = EXCLUDED.last_failure,
                updated_at = EXCLUDED.updated_at;
            retry_pending := TRUE;
            CONTINUE;
        END IF;
        EXECUTE format('DROP ROLE %I', orphan_role.rolname);
        DELETE FROM orphan_revocation_retries
        WHERE role_name = orphan_role.rolname;
        reconciled_count := reconciled_count + 1;
    END LOOP;
    IF retry_pending THEN
        RETURN -1;
    END IF;
    RETURN reconciled_count;
END
$_$;
