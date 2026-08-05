-- gobby:destructive
-- Managed credential lifecycle metadata and daemon leases.

CREATE TABLE IF NOT EXISTS gobby_agent_auth.daemon_registry (
    machine_id UUID PRIMARY KEY REFERENCES public.machines(id) ON DELETE CASCADE,
    heartbeat_at TIMESTAMPTZ NOT NULL,
    lease_expires_at TIMESTAMPTZ NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (lease_expires_at > heartbeat_at)
);

CREATE TABLE IF NOT EXISTS gobby_agent_auth.orphan_revocation_retries (
    role_name NAME PRIMARY KEY,
    revocation_attempts INTEGER NOT NULL DEFAULT 0,
    next_retry_at TIMESTAMPTZ NOT NULL,
    last_failure TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE gobby_agent_auth.principal_bindings
    ADD COLUMN IF NOT EXISTS revocation_requested_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS revocation_attempts INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS next_revocation_retry_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_revocation_failure TEXT,
    ADD COLUMN IF NOT EXISTS predecessor_drain_deadline TIMESTAMPTZ;

ALTER TABLE gobby_agent_auth.principal_audit_events
    DROP CONSTRAINT IF EXISTS principal_audit_events_event_type_check;
ALTER TABLE gobby_agent_auth.principal_audit_events
    ADD CONSTRAINT principal_audit_events_event_type_check
    CHECK (event_type IN ('issue', 'rotate', 'revoke', 'reconcile', 'revoke_retry'));

REVOKE ALL ON gobby_agent_auth.daemon_registry FROM PUBLIC;
REVOKE ALL ON gobby_agent_auth.daemon_registry FROM gobby_daemon_runtime;
REVOKE ALL ON gobby_agent_auth.orphan_revocation_retries FROM PUBLIC;
REVOKE ALL ON gobby_agent_auth.orphan_revocation_retries FROM gobby_daemon_runtime;
ALTER TABLE gobby_agent_auth.daemon_registry OWNER TO gobby_agent_issuer;
ALTER TABLE gobby_agent_auth.orphan_revocation_retries OWNER TO gobby_agent_issuer;
GRANT SELECT (id, project_id, agent_run_id, status)
    ON public.sessions TO gobby_agent_issuer;
GRANT SELECT (id, status) ON public.agent_runs TO gobby_agent_issuer;
GRANT SELECT (id, name, repo_path) ON public.projects TO gobby_agent_issuer;

CREATE OR REPLACE FUNCTION gobby_agent_auth.enforce_principal_lifetime()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $function$
BEGIN
    IF NEW.expires_at <= NEW.issued_at THEN
        RAISE EXCEPTION 'managed principal expiry must follow issuance'
            USING ERRCODE = '22023';
    END IF;
    IF NEW.expires_at > NEW.issued_at + INTERVAL '1 hour' THEN
        RAISE EXCEPTION 'managed principal lifetime exceeds one hour'
            USING ERRCODE = '22023';
    END IF;
    RETURN NEW;
END
$function$;

DROP TRIGGER IF EXISTS principal_lifetime_guard
    ON gobby_agent_auth.principal_bindings;
CREATE TRIGGER principal_lifetime_guard
BEFORE INSERT OR UPDATE OF issued_at, expires_at
ON gobby_agent_auth.principal_bindings
FOR EACH ROW
EXECUTE FUNCTION gobby_agent_auth.enforce_principal_lifetime();

CREATE OR REPLACE FUNCTION gobby_agent_auth.heartbeat_daemon(
    p_machine_id UUID,
    p_lease_duration INTERVAL DEFAULT INTERVAL '2 minutes'
) RETURNS UUID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, gobby_agent_auth
AS $function$
DECLARE
    v_now TIMESTAMPTZ := clock_timestamp();
BEGIN
    IF p_lease_duration <= INTERVAL '0 seconds'
       OR p_lease_duration > INTERVAL '5 minutes' THEN
        RAISE EXCEPTION 'daemon lease duration must be between zero and five minutes'
            USING ERRCODE = '22023';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.machines WHERE id = p_machine_id) THEN
        RAISE EXCEPTION 'unknown issuing machine' USING ERRCODE = '23503';
    END IF;

    INSERT INTO gobby_agent_auth.daemon_registry (
        machine_id, heartbeat_at, lease_expires_at, started_at
    ) VALUES (
        p_machine_id, v_now, v_now + p_lease_duration, v_now
    )
    ON CONFLICT (machine_id) DO UPDATE
    SET heartbeat_at = EXCLUDED.heartbeat_at,
        lease_expires_at = EXCLUDED.lease_expires_at;
    RETURN p_machine_id;
END
$function$;

REVOKE ALL ON FUNCTION gobby_agent_auth.enforce_principal_lifetime() FROM PUBLIC;
REVOKE ALL ON FUNCTION gobby_agent_auth.heartbeat_daemon(UUID, INTERVAL) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION gobby_agent_auth.heartbeat_daemon(UUID, INTERVAL)
    TO gobby_daemon_runtime;

CREATE OR REPLACE FUNCTION gobby_agent_auth.revoke_principal(
    requested_execution_id UUID,
    requested_generation INTEGER DEFAULT NULL
)
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = gobby_agent_auth, pg_temp
SET createrole_self_grant = ''
AS $function$
DECLARE
    binding principal_bindings%ROWTYPE;
    revoked_count INTEGER := 0;
    remaining_sessions INTEGER;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(requested_execution_id::TEXT, 0));
    FOR binding IN
        SELECT * FROM principal_bindings
        WHERE managed_execution_id = requested_execution_id
          AND revoked_at IS NULL
          AND (
              requested_generation IS NULL
              OR credential_generation = requested_generation
          )
        ORDER BY credential_generation
        FOR UPDATE
    LOOP
        UPDATE principal_bindings
        SET revocation_requested_at = COALESCE(revocation_requested_at, clock_timestamp()),
            revocation_attempts = revocation_attempts + 1,
            next_revocation_retry_at = NULL,
            last_revocation_failure = NULL
        WHERE id = binding.id;

        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = binding.role_name) THEN
            EXECUTE format('ALTER ROLE %I NOLOGIN', binding.role_name);
            EXECUTE format(
                'REVOKE %I FROM %I',
                'gobby_gcode_capability',
                binding.role_name
            );
            PERFORM pg_terminate_backend(pid, 5000)
            FROM pg_stat_activity
            WHERE usename = binding.role_name::TEXT
              AND pid <> pg_backend_pid();
            SELECT count(*) INTO remaining_sessions
            FROM pg_stat_activity
            WHERE usename = binding.role_name::TEXT;
            IF remaining_sessions <> 0 THEN
                UPDATE principal_bindings
                SET next_revocation_retry_at = clock_timestamp() + INTERVAL '15 seconds',
                    last_revocation_failure = 'active_sessions_remaining'
                WHERE id = binding.id;
                INSERT INTO principal_audit_events (
                    binding_id,
                    event_type,
                    managed_execution_id,
                    role_name,
                    credential_generation,
                    project_id,
                    details
                ) VALUES (
                    binding.id,
                    'revoke_retry',
                    binding.managed_execution_id,
                    binding.role_name,
                    binding.credential_generation,
                    binding.project_id,
                    jsonb_build_object('failure_code', 'active_sessions_remaining')
                );
                RETURN -1;
            END IF;
            EXECUTE format('DROP ROLE %I', binding.role_name);
        END IF;

        UPDATE principal_bindings
        SET revoked_at = clock_timestamp(),
            next_revocation_retry_at = NULL,
            last_revocation_failure = NULL
        WHERE id = binding.id;
        INSERT INTO principal_audit_events (
            binding_id,
            event_type,
            managed_execution_id,
            role_name,
            credential_generation,
            project_id
        ) VALUES (
            binding.id,
            'revoke',
            binding.managed_execution_id,
            binding.role_name,
            binding.credential_generation,
            binding.project_id
        );
        revoked_count := revoked_count + 1;
    END LOOP;
    RETURN revoked_count;
END
$function$;

REVOKE ALL ON FUNCTION gobby_agent_auth.revoke_principal(UUID, INTEGER) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION gobby_agent_auth.revoke_principal(UUID, INTEGER)
    TO gobby_daemon_runtime;

CREATE OR REPLACE FUNCTION gobby_agent_auth.principals_due_for_rotation(
    p_machine_id UUID
)
RETURNS TABLE(
    managed_execution_id UUID,
    role_name NAME,
    credential_generation INTEGER
)
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, gobby_agent_auth
AS $function$
    WITH single_active AS (
        SELECT pb.managed_execution_id
        FROM gobby_agent_auth.principal_bindings AS pb
        WHERE pb.issuing_machine_id = p_machine_id
          AND pb.revoked_at IS NULL
          AND pb.expires_at > clock_timestamp()
        GROUP BY pb.managed_execution_id
        HAVING count(*) = 1
    )
    SELECT pb.managed_execution_id, pb.role_name, pb.credential_generation
    FROM gobby_agent_auth.principal_bindings AS pb
    JOIN single_active AS active USING (managed_execution_id)
    WHERE pb.revoked_at IS NULL
      AND pb.revocation_requested_at IS NULL
      AND pb.issued_at <= clock_timestamp() - INTERVAL '45 minutes'
      AND pb.expires_at > clock_timestamp()
    ORDER BY pb.managed_execution_id
$function$;

CREATE OR REPLACE FUNCTION gobby_agent_auth.rotate_principal_if_generation(
    p_execution_id UUID,
    p_expected_generation INTEGER,
    p_expires_at TIMESTAMPTZ,
    p_password TEXT
)
RETURNS TABLE(role_name NAME, credential_generation INTEGER)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = gobby_agent_auth, pg_temp
SET createrole_self_grant = ''
AS $function$
DECLARE
    v_current_generation INTEGER;
    v_role_name NAME;
    v_generation INTEGER;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(p_execution_id::TEXT, 0));
    SELECT max(pb.credential_generation) INTO v_current_generation
    FROM principal_bindings AS pb
    WHERE pb.managed_execution_id = p_execution_id
      AND pb.revoked_at IS NULL
      AND pb.expires_at > clock_timestamp();
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
$function$;

CREATE OR REPLACE FUNCTION gobby_agent_auth.cancel_principal_rotation(
    p_execution_id UUID,
    p_predecessor_generation INTEGER,
    p_successor_generation INTEGER
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = gobby_agent_auth, pg_temp
AS $function$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(p_execution_id::TEXT, 0));
    IF EXISTS (
        SELECT 1 FROM principal_bindings
        WHERE managed_execution_id = p_execution_id
          AND credential_generation = p_successor_generation
          AND revoked_at IS NOT NULL
    ) THEN
        UPDATE principal_bindings
        SET revocation_requested_at = NULL,
            predecessor_drain_deadline = NULL,
            next_revocation_retry_at = NULL,
            last_revocation_failure = NULL
        WHERE managed_execution_id = p_execution_id
          AND credential_generation = p_predecessor_generation
          AND revoked_at IS NULL;
    END IF;
END
$function$;

REVOKE ALL ON FUNCTION gobby_agent_auth.principals_due_for_rotation(UUID) FROM PUBLIC;
REVOKE ALL ON FUNCTION gobby_agent_auth.rotate_principal_if_generation(
    UUID, INTEGER, TIMESTAMPTZ, TEXT
) FROM PUBLIC;
REVOKE ALL ON FUNCTION gobby_agent_auth.cancel_principal_rotation(
    UUID, INTEGER, INTEGER
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION gobby_agent_auth.principals_due_for_rotation(UUID)
    TO gobby_daemon_runtime;
GRANT EXECUTE ON FUNCTION gobby_agent_auth.rotate_principal_if_generation(
    UUID, INTEGER, TIMESTAMPTZ, TEXT
) TO gobby_daemon_runtime;
GRANT EXECUTE ON FUNCTION gobby_agent_auth.cancel_principal_rotation(
    UUID, INTEGER, INTEGER
) TO gobby_daemon_runtime;

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
        WHERE roles.rolname ~ '^gobby_agent_[0-9a-f]{32}_[1-9][0-9]*$'
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

CREATE OR REPLACE FUNCTION gobby_agent_auth.managed_execution_is_login_capable(
    p_execution_id UUID
)
RETURNS BOOLEAN
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, gobby_agent_auth
AS $function$
    SELECT EXISTS (
        SELECT 1
        FROM gobby_agent_auth.principal_bindings AS binding
        JOIN pg_catalog.pg_roles AS role ON role.rolname = binding.role_name
        WHERE binding.managed_execution_id = p_execution_id
          AND binding.revoked_at IS NULL
          AND binding.expires_at > clock_timestamp()
          AND role.rolcanlogin
    )
$function$;

REVOKE ALL ON FUNCTION gobby_agent_auth.reconcile_daemon(UUID) FROM PUBLIC;
REVOKE ALL ON FUNCTION gobby_agent_auth.managed_execution_is_login_capable(UUID)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION gobby_agent_auth.reconcile_daemon(UUID)
    TO gobby_daemon_runtime;
GRANT EXECUTE ON FUNCTION gobby_agent_auth.managed_execution_is_login_capable(UUID)
    TO gobby_daemon_runtime;

CREATE OR REPLACE FUNCTION gobby_agent_auth.resolve_tool_session(
    p_session_id UUID
)
RETURNS TABLE(session_id UUID, project_id UUID, repo_path TEXT)
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
    SELECT session.id, project.id, project.repo_path
    FROM public.sessions AS session
    JOIN public.projects AS project ON project.id = session.project_id
    WHERE session.id = p_session_id
      AND COALESCE(session.status, 'active') NOT IN ('expired', 'deleted')
$function$;

REVOKE ALL ON FUNCTION gobby_agent_auth.resolve_tool_session(UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION gobby_agent_auth.resolve_tool_session(UUID)
    TO gobby_daemon_runtime;

CREATE OR REPLACE FUNCTION gobby_agent_auth.issue_tool_principal(
    p_execution_id UUID,
    p_session_id UUID,
    p_machine_id UUID,
    p_expires_at TIMESTAMPTZ,
    p_password TEXT
)
RETURNS TABLE(role_name NAME, credential_generation INTEGER)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = gobby_agent_auth, pg_temp
SET createrole_self_grant = ''
AS $function$
DECLARE
    v_project_id UUID;
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
      AND COALESCE(session.status, 'active') NOT IN ('expired', 'deleted');
    IF NOT FOUND THEN
        RAISE EXCEPTION 'managed principal session does not exist'
            USING ERRCODE = '23503';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.machines WHERE id = p_machine_id) THEN
        RAISE EXCEPTION 'managed principal issuing machine does not exist'
            USING ERRCODE = '23503';
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
        session_id, project_id, issuing_machine_id, expires_at,
        credential_generation
    ) VALUES (
        v_role_name, 'tool_chat', p_execution_id, NULL,
        p_session_id, v_project_id, p_machine_id, p_expires_at, 1
    ) RETURNING id INTO v_binding_id;
    INSERT INTO principal_audit_events (
        binding_id, event_type, managed_execution_id, role_name,
        credential_generation, project_id
    ) VALUES (
        v_binding_id, 'issue', p_execution_id, v_role_name, 1, v_project_id
    );
    RETURN QUERY SELECT v_role_name, 1;
END
$function$;

REVOKE ALL ON FUNCTION gobby_agent_auth.issue_tool_principal(
    UUID, UUID, UUID, TIMESTAMPTZ, TEXT
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION gobby_agent_auth.issue_tool_principal(
    UUID, UUID, UUID, TIMESTAMPTZ, TEXT
) TO gobby_daemon_runtime;

ALTER FUNCTION gobby_agent_auth.heartbeat_daemon(UUID, INTERVAL)
    OWNER TO gobby_agent_issuer;
ALTER FUNCTION gobby_agent_auth.principals_due_for_rotation(UUID)
    OWNER TO gobby_agent_issuer;
ALTER FUNCTION gobby_agent_auth.rotate_principal_if_generation(
    UUID, INTEGER, TIMESTAMPTZ, TEXT
) OWNER TO gobby_agent_issuer;
ALTER FUNCTION gobby_agent_auth.cancel_principal_rotation(UUID, INTEGER, INTEGER)
    OWNER TO gobby_agent_issuer;
ALTER FUNCTION gobby_agent_auth.reconcile_daemon(UUID)
    OWNER TO CURRENT_USER;
ALTER FUNCTION gobby_agent_auth.managed_execution_is_login_capable(UUID)
    OWNER TO gobby_agent_issuer;
ALTER FUNCTION gobby_agent_auth.resolve_tool_session(UUID)
    OWNER TO CURRENT_USER;
ALTER FUNCTION gobby_agent_auth.issue_tool_principal(
    UUID, UUID, UUID, TIMESTAMPTZ, TEXT
) OWNER TO gobby_agent_issuer;
