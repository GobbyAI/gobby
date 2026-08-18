-- Sweep leftover interactive and maintenance roles the same way agent
-- orphans are reaped. Hash-format gobby_ix_* leftovers 42710 the next
-- issue/rotate; slug-format and gobby_mnt_* leftovers are dead login
-- credentials. Drop the derived hash name at issue/rotate time when no
-- binding names it so the next issuance does not wait for reconcile.

CREATE OR REPLACE FUNCTION gobby_agent_auth.reconcile_daemon(
    p_machine_id UUID
)
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = gobby_agent_auth, pg_temp
SET createrole_self_grant = ''
AS $function$
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
              pb.expires_at <= clock_timestamp()
              OR pb.revocation_requested_at IS NOT NULL
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
$function$;

CREATE OR REPLACE FUNCTION gobby_agent_auth.drain_ephemeral_principals()
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = gobby_agent_auth, pg_temp
SET createrole_self_grant = ''
AS $function$
DECLARE
    binding RECORD;
    orphan_role RECORD;
    result_count INTEGER;
    remaining_sessions INTEGER;
    drained_count INTEGER := 0;
    retry_pending BOOLEAN := FALSE;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended('gobby-agent-auth-maintenance-drain', 0));

    FOR binding IN
        SELECT DISTINCT managed_execution_id, credential_generation
        FROM principal_bindings
        WHERE revoked_at IS NULL
        ORDER BY managed_execution_id, credential_generation
    LOOP
        result_count := revoke_principal(
            binding.managed_execution_id,
            binding.credential_generation
        );
        IF result_count > 0 THEN
            drained_count := drained_count + result_count;
        ELSIF result_count < 0 THEN
            retry_pending := TRUE;
        END IF;
    END LOOP;

    FOR orphan_role IN
        SELECT role.rolname
        FROM pg_roles AS role
        WHERE role.rolname ~ '^(gobby_agent_[0-9a-f]{32}|gobby_ix_([0-9a-f]{16}|[A-Za-z0-9]{1,8}_[0-9a-f]{8}_[0-9a-f]{8})|gobby_mnt_[0-9a-f]{32})_[1-9][0-9]*$'
        ORDER BY role.rolname
    LOOP
        EXECUTE format('ALTER ROLE %I NOLOGIN', orphan_role.rolname);
        EXECUTE format(
            'REVOKE %I FROM %I',
            'gobby_gcode_capability',
            orphan_role.rolname
        );
        PERFORM pg_terminate_backend(pid, 5000)
        FROM pg_stat_activity
        WHERE usename = orphan_role.rolname::TEXT
          AND pid <> pg_backend_pid();
        SELECT count(*) INTO remaining_sessions
        FROM pg_stat_activity
        WHERE usename = orphan_role.rolname::TEXT;
        IF remaining_sessions <> 0 THEN
            retry_pending := TRUE;
            CONTINUE;
        END IF;
        EXECUTE format('DROP ROLE %I', orphan_role.rolname);
        DELETE FROM orphan_revocation_retries
        WHERE role_name = orphan_role.rolname;
        drained_count := drained_count + 1;
    END LOOP;

    IF retry_pending THEN
        RETURN -1;
    END IF;
    RETURN drained_count;
END
$function$;

CREATE OR REPLACE FUNCTION gobby_agent_auth.issue_or_reuse_interactive_principal(
    requested_deployment_token TEXT,
    requested_machine_id UUID,
    requested_project_id UUID,
    requested_session_id UUID,
    requested_expires_at TIMESTAMPTZ,
    requested_password TEXT
)
RETURNS TABLE(
    role_name NAME,
    credential_generation INTEGER,
    reused BOOLEAN,
    managed_execution_id UUID
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = gobby_agent_auth, pg_temp
SET createrole_self_grant = ''
AS $function$
DECLARE
    existing_binding principal_bindings%ROWTYPE;
    derived_role_name NAME;
    next_generation INTEGER;
    binding_id UUID;
BEGIN
    IF requested_deployment_token IS NULL OR requested_deployment_token = '' THEN
        RAISE EXCEPTION 'interactive principal requires a deployment token'
            USING ERRCODE = '22023';
    END IF;
    IF requested_machine_id IS NULL THEN
        RAISE EXCEPTION 'interactive principal requires a machine id'
            USING ERRCODE = '22023';
    END IF;
    IF requested_project_id IS NULL THEN
        RAISE EXCEPTION 'interactive principal requires a project id'
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
    PERFORM pg_advisory_xact_lock(
        hashtextextended(
            requested_deployment_token || requested_machine_id::TEXT || requested_project_id::TEXT,
            0
        )
    );

    SELECT *
      INTO existing_binding
      FROM principal_bindings
     WHERE owner_kind = 'interactive'
       AND deployment_token = requested_deployment_token
       AND issuing_machine_id = requested_machine_id
       AND project_id = requested_project_id
       AND revoked_at IS NULL
       AND predecessor_drain_deadline IS NULL
     ORDER BY credential_generation DESC
     LIMIT 1;
    IF FOUND THEN
        UPDATE principal_bindings
           SET expires_at = requested_expires_at
         WHERE id = existing_binding.id;
        EXECUTE format(
            'ALTER ROLE %I VALID UNTIL %L',
            existing_binding.role_name,
            requested_expires_at
        );
        RETURN QUERY SELECT existing_binding.role_name,
            existing_binding.credential_generation, TRUE,
            existing_binding.managed_execution_id;
        RETURN;
    END IF;

    SELECT COALESCE(MAX(pb.credential_generation), 0) + 1
      INTO next_generation
      FROM principal_bindings AS pb
     WHERE pb.owner_kind = 'interactive'
       AND pb.deployment_token = requested_deployment_token
       AND pb.issuing_machine_id = requested_machine_id
       AND pb.project_id = requested_project_id;
    derived_role_name := gobby_agent_auth.interactive_role_name(
        requested_deployment_token,
        requested_machine_id,
        requested_project_id,
        next_generation
    );
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = derived_role_name)
       AND NOT EXISTS (
           SELECT 1 FROM principal_bindings AS binding
           WHERE binding.role_name = derived_role_name
       )
    THEN
        EXECUTE format('ALTER ROLE %I NOLOGIN', derived_role_name);
        EXECUTE format(
            'REVOKE %I FROM %I',
            'gobby_gcode_capability',
            derived_role_name
        );
        PERFORM pg_terminate_backend(pid, 5000)
        FROM pg_stat_activity
        WHERE usename = derived_role_name::TEXT
          AND pid <> pg_backend_pid();
        IF EXISTS (
            SELECT 1 FROM pg_stat_activity
            WHERE usename = derived_role_name::TEXT
        ) THEN
            RAISE EXCEPTION 'managed principal still has active database sessions'
                USING ERRCODE = '55006';
        END IF;
        EXECUTE format('DROP ROLE %I', derived_role_name);
    END IF;
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
        session_id,
        project_id,
        issuing_machine_id,
        deployment_token,
        expires_at,
        credential_generation
    ) VALUES (
        derived_role_name,
        'interactive',
        gen_random_uuid(),
        requested_session_id,
        requested_project_id,
        requested_machine_id,
        requested_deployment_token,
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
        'issue',
        (SELECT pb.managed_execution_id FROM principal_bindings AS pb WHERE pb.id = binding_id),
        derived_role_name,
        next_generation,
        requested_project_id
    );
    RETURN QUERY SELECT derived_role_name, next_generation, FALSE,
        (SELECT pb.managed_execution_id FROM principal_bindings AS pb WHERE pb.id = binding_id);
END
$function$;

CREATE OR REPLACE FUNCTION gobby_agent_auth.rotate_interactive_principal(
    requested_deployment_token TEXT,
    requested_machine_id UUID,
    requested_project_id UUID,
    requested_session_id UUID,
    requested_expires_at TIMESTAMPTZ,
    requested_password TEXT,
    requested_drain_until TIMESTAMPTZ
)
RETURNS TABLE(
    role_name NAME,
    credential_generation INTEGER,
    managed_execution_id UUID
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = gobby_agent_auth, pg_temp
SET createrole_self_grant = ''
AS $function$
DECLARE
    existing_binding principal_bindings%ROWTYPE;
    derived_role_name NAME;
    next_generation INTEGER;
    binding_id UUID;
    drain_until TIMESTAMPTZ;
BEGIN
    IF requested_deployment_token IS NULL OR requested_deployment_token = '' THEN
        RAISE EXCEPTION 'interactive principal requires a deployment token'
            USING ERRCODE = '22023';
    END IF;
    IF requested_machine_id IS NULL THEN
        RAISE EXCEPTION 'interactive principal requires a machine id'
            USING ERRCODE = '22023';
    END IF;
    IF requested_project_id IS NULL THEN
        RAISE EXCEPTION 'interactive principal requires a project id'
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
    PERFORM pg_advisory_xact_lock(
        hashtextextended(
            requested_deployment_token || requested_machine_id::TEXT || requested_project_id::TEXT,
            0
        )
    );
    SELECT *
      INTO existing_binding
      FROM principal_bindings
     WHERE owner_kind = 'interactive'
       AND deployment_token = requested_deployment_token
       AND issuing_machine_id = requested_machine_id
       AND project_id = requested_project_id
       AND revoked_at IS NULL
       AND predecessor_drain_deadline IS NULL
     ORDER BY credential_generation DESC
     LIMIT 1;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'interactive rotation requires an active principal'
            USING ERRCODE = '42501';
    END IF;
    drain_until := GREATEST(requested_drain_until, clock_timestamp() + INTERVAL '1 second');
    UPDATE principal_bindings
       SET predecessor_drain_deadline = drain_until,
           revocation_requested_at = COALESCE(revocation_requested_at, clock_timestamp())
     WHERE id = existing_binding.id;
    SELECT COALESCE(MAX(pb.credential_generation), 0) + 1
      INTO next_generation
      FROM principal_bindings AS pb
     WHERE pb.owner_kind = 'interactive'
       AND pb.deployment_token = requested_deployment_token
       AND pb.issuing_machine_id = requested_machine_id
       AND pb.project_id = requested_project_id;
    derived_role_name := gobby_agent_auth.interactive_role_name(
        requested_deployment_token,
        requested_machine_id,
        requested_project_id,
        next_generation
    );
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = derived_role_name)
       AND NOT EXISTS (
           SELECT 1 FROM principal_bindings AS binding
           WHERE binding.role_name = derived_role_name
       )
    THEN
        EXECUTE format('ALTER ROLE %I NOLOGIN', derived_role_name);
        EXECUTE format(
            'REVOKE %I FROM %I',
            'gobby_gcode_capability',
            derived_role_name
        );
        PERFORM pg_terminate_backend(pid, 5000)
        FROM pg_stat_activity
        WHERE usename = derived_role_name::TEXT
          AND pid <> pg_backend_pid();
        IF EXISTS (
            SELECT 1 FROM pg_stat_activity
            WHERE usename = derived_role_name::TEXT
        ) THEN
            RAISE EXCEPTION 'managed principal still has active database sessions'
                USING ERRCODE = '55006';
        END IF;
        EXECUTE format('DROP ROLE %I', derived_role_name);
    END IF;
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
        session_id,
        project_id,
        issuing_machine_id,
        deployment_token,
        expires_at,
        credential_generation
    ) VALUES (
        derived_role_name,
        'interactive',
        gen_random_uuid(),
        requested_session_id,
        requested_project_id,
        requested_machine_id,
        requested_deployment_token,
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
        (SELECT pb.managed_execution_id FROM principal_bindings AS pb WHERE pb.id = binding_id),
        derived_role_name,
        next_generation,
        requested_project_id
    );
    RETURN QUERY SELECT derived_role_name, next_generation,
        (SELECT pb.managed_execution_id FROM principal_bindings AS pb WHERE pb.id = binding_id);
END
$function$;
