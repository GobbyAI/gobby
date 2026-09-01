SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;
CREATE SCHEMA gobby_agent_auth;
ALTER SCHEMA gobby_agent_auth OWNER TO gobby_agent_issuer;
CREATE SCHEMA public;
ALTER SCHEMA public OWNER TO pg_database_owner;
COMMENT ON SCHEMA public IS 'standard public schema';
CREATE FUNCTION gobby_agent_auth.assert_interactive_overlay_registered(requested_machine_id uuid, requested_project_id uuid, requested_overlay_project_id uuid) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'gobby_agent_auth', 'pg_temp'
    AS $$
BEGIN
    IF requested_overlay_project_id IS NULL THEN
        RETURN;
    END IF;
    IF requested_overlay_project_id = requested_project_id THEN
        RAISE EXCEPTION 'interactive overlay must differ from the session project'
            USING ERRCODE = '22023';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM (
            SELECT worktree.worktree_path AS workspace_path
            FROM public.worktrees AS worktree
            WHERE worktree.project_id = requested_project_id
              AND worktree.machine_id = requested_machine_id
            UNION ALL
            SELECT clone.clone_path AS workspace_path
            FROM public.clones AS clone
            WHERE clone.project_id = requested_project_id
              AND clone.machine_id = requested_machine_id
        ) AS workspace
        WHERE code_index_project_id(workspace.workspace_path) = requested_overlay_project_id
    ) THEN
        RAISE EXCEPTION
            'interactive overlay is not a registered isolation workspace of the project'
            USING ERRCODE = '23503';
    END IF;
END
$$;
ALTER FUNCTION gobby_agent_auth.assert_interactive_overlay_registered(requested_machine_id uuid, requested_project_id uuid, requested_overlay_project_id uuid) OWNER TO gobby_agent_issuer;
CREATE FUNCTION gobby_agent_auth.cancel_principal_rotation(p_execution_id uuid, p_predecessor_generation integer, p_successor_generation integer) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'gobby_agent_auth', 'pg_temp'
    AS $$
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
$$;
ALTER FUNCTION gobby_agent_auth.cancel_principal_rotation(p_execution_id uuid, p_predecessor_generation integer, p_successor_generation integer) OWNER TO gobby_agent_issuer;
CREATE FUNCTION gobby_agent_auth.code_index_project_id(root_path text) RETURNS uuid
    LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE
    SET search_path TO 'pg_catalog', 'public'
    AS $$
DECLARE
    uuid_bytes BYTEA;
BEGIN
    uuid_bytes := substring(
        "public".digest(
            pg_catalog.uuid_send('c0de1de0-0000-4000-8000-000000000000'::UUID)
                || pg_catalog.convert_to(root_path, 'UTF8'),
            'sha1'
        )
        FROM 1 FOR 16
    );
    uuid_bytes := pg_catalog.set_byte(
        uuid_bytes,
        6,
        (pg_catalog.get_byte(uuid_bytes, 6) & 15) | 80
    );
    uuid_bytes := pg_catalog.set_byte(
        uuid_bytes,
        8,
        (pg_catalog.get_byte(uuid_bytes, 8) & 63) | 128
    );
    RETURN pg_catalog.encode(uuid_bytes, 'hex')::UUID;
END
$$;
ALTER FUNCTION gobby_agent_auth.code_index_project_id(root_path text) OWNER TO gobby_agent_issuer;
CREATE FUNCTION gobby_agent_auth.current_code_overlay_project_id() RETURNS uuid
    LANGUAGE plpgsql STABLE SECURITY DEFINER PARALLEL SAFE
    SET search_path TO 'gobby_agent_auth', 'pg_temp'
    AS $$
DECLARE
    binding_count INTEGER;
    bound_overlay_project_id UUID;
BEGIN
    SELECT count(*), min(code_overlay_project_id::TEXT)::UUID
    INTO binding_count, bound_overlay_project_id
    FROM principal_bindings
    WHERE role_name = session_user::NAME
      AND revoked_at IS NULL
      AND expires_at > clock_timestamp();
    IF binding_count <> 1 THEN
        RAISE EXCEPTION 'managed principal binding is missing, expired, revoked, or duplicated'
            USING ERRCODE = '42501';
    END IF;
    RETURN bound_overlay_project_id;
END
$$;
ALTER FUNCTION gobby_agent_auth.current_code_overlay_project_id() OWNER TO gobby_agent_issuer;
CREATE FUNCTION gobby_agent_auth.current_machine_id() RETURNS uuid
    LANGUAGE plpgsql STABLE SECURITY DEFINER PARALLEL SAFE
    SET search_path TO 'gobby_agent_auth', 'pg_temp'
    AS $$
DECLARE
    binding_count INTEGER;
    bound_machine_id UUID;
BEGIN
    SELECT count(*), min(issuing_machine_id::TEXT)::UUID
    INTO binding_count, bound_machine_id
    FROM principal_bindings
    WHERE role_name = session_user::NAME
      AND revoked_at IS NULL
      AND expires_at > clock_timestamp();
    IF binding_count <> 1 THEN
        RAISE EXCEPTION 'managed principal binding is missing, expired, revoked, or duplicated'
            USING ERRCODE = '42501';
    END IF;
    RETURN bound_machine_id;
END
$$;
ALTER FUNCTION gobby_agent_auth.current_machine_id() OWNER TO gobby_agent_issuer;
CREATE FUNCTION gobby_agent_auth.current_project_id() RETURNS uuid
    LANGUAGE plpgsql STABLE SECURITY DEFINER PARALLEL SAFE
    SET search_path TO 'gobby_agent_auth', 'pg_temp'
    AS $$
DECLARE
    binding_count INTEGER;
    bound_project_id UUID;
BEGIN
    SELECT count(*), min(project_id::TEXT)::UUID
    INTO binding_count, bound_project_id
    FROM principal_bindings
    WHERE role_name = session_user::NAME
      AND revoked_at IS NULL
      AND expires_at > clock_timestamp();
    IF binding_count <> 1 THEN
        RAISE EXCEPTION 'managed principal binding is missing, expired, revoked, or duplicated'
            USING ERRCODE = '42501';
    END IF;
    RETURN bound_project_id;
END
$$;
ALTER FUNCTION gobby_agent_auth.current_project_id() OWNER TO gobby_agent_issuer;
CREATE FUNCTION gobby_agent_auth.drain_ephemeral_principals() RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'gobby_agent_auth', 'pg_temp'
    SET createrole_self_grant TO ''
    AS $_$
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
          AND NOT EXISTS (
              SELECT 1 FROM principal_bindings AS bound
              WHERE bound.role_name = role.rolname
          )
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
$_$;
ALTER FUNCTION gobby_agent_auth.drain_ephemeral_principals() OWNER TO gobby_agent_issuer;
CREATE FUNCTION gobby_agent_auth.enforce_principal_lifetime() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog'
    AS $$
BEGIN
    IF NEW.expires_at <= NEW.issued_at THEN
        RAISE EXCEPTION 'managed principal expiry must follow issuance'
            USING ERRCODE = '22023';
    END IF;
    IF NEW.expires_at > NEW.issued_at + INTERVAL '24 hours' THEN
        RAISE EXCEPTION 'managed principal lifetime exceeds 24 hours'
            USING ERRCODE = '22023';
    END IF;
    RETURN NEW;
END
$$;
ALTER FUNCTION gobby_agent_auth.enforce_principal_lifetime() OWNER TO gobby_test;
CREATE FUNCTION gobby_agent_auth.heartbeat_daemon(p_machine_id uuid, p_lease_duration interval DEFAULT '00:02:00'::interval) RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'gobby_agent_auth'
    AS $$
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
$$;
ALTER FUNCTION gobby_agent_auth.heartbeat_daemon(p_machine_id uuid, p_lease_duration interval) OWNER TO gobby_agent_issuer;
CREATE FUNCTION gobby_agent_auth.interactive_role_name(requested_deployment_token text, requested_machine_id uuid, requested_project_id uuid, generation integer) RETURNS name
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    SET search_path TO 'gobby_agent_auth', 'pg_temp'
    AS $$
    SELECT (
        'gobby_ix_'
        || substr(
            encode(
                "public".digest(
                    convert_to(
                        requested_deployment_token
                        || requested_machine_id::TEXT
                        || requested_project_id::TEXT,
                        'UTF8'
                    ),
                    'sha256'
                ),
                'hex'
            ),
            1,
            16
        )
        || '_'
        || generation::TEXT
    )::NAME
$$;
ALTER FUNCTION gobby_agent_auth.interactive_role_name(requested_deployment_token text, requested_machine_id uuid, requested_project_id uuid, generation integer) OWNER TO gobby_test;
CREATE FUNCTION gobby_agent_auth.issue_maintenance_principal(p_execution_id uuid, p_project_id uuid, p_machine_id uuid, p_expires_at timestamp with time zone, p_password text, p_code_overlay_project_id uuid) RETURNS TABLE(role_name name, credential_generation integer)
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'gobby_agent_auth', 'pg_temp'
    SET createrole_self_grant TO ''
    AS $$
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
$$;
ALTER FUNCTION gobby_agent_auth.issue_maintenance_principal(p_execution_id uuid, p_project_id uuid, p_machine_id uuid, p_expires_at timestamp with time zone, p_password text, p_code_overlay_project_id uuid) OWNER TO gobby_agent_issuer;
CREATE FUNCTION gobby_agent_auth.issue_or_reuse_interactive_principal(requested_deployment_token text, requested_machine_id uuid, requested_project_id uuid, requested_session_id uuid, requested_expires_at timestamp with time zone, requested_password text, requested_overlay_project_id uuid) RETURNS TABLE(role_name name, credential_generation integer, reused boolean, managed_execution_id uuid)
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'gobby_agent_auth', 'pg_temp'
    SET createrole_self_grant TO ''
    AS $$
DECLARE
    existing_binding principal_bindings%ROWTYPE;
    derived_role_name NAME;
    next_generation INTEGER;
    binding_id UUID;
    audit_event_type TEXT := 'issue';
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
    IF requested_expires_at IS NULL THEN
        RAISE EXCEPTION 'managed principal expiry must be in the future'
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
    PERFORM gobby_agent_auth.assert_interactive_overlay_registered(
        requested_machine_id,
        requested_project_id,
        requested_overlay_project_id
    );
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
       AND code_overlay_project_id IS NOT DISTINCT FROM requested_overlay_project_id
       AND revoked_at IS NULL
       AND predecessor_drain_deadline IS NULL
     ORDER BY credential_generation DESC
     LIMIT 1;
    IF FOUND THEN
        IF requested_expires_at <= existing_binding.issued_at + INTERVAL '24 hours' THEN
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
        -- The binding's credential age would exceed 24 hours: roll to the next
        -- generation instead of extending. The predecessor keeps its own
        -- VALID UNTIL and drains until its remaining validity runs out, so
        -- outstanding grants (capped at the role's validity) finish cleanly.
        UPDATE principal_bindings
           SET predecessor_drain_deadline = GREATEST(
                   existing_binding.expires_at,
                   clock_timestamp() + INTERVAL '1 second'
               ),
               revocation_requested_at = COALESCE(revocation_requested_at, clock_timestamp())
         WHERE id = existing_binding.id;
        audit_event_type := 'rotate';
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
        code_overlay_project_id,
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
        requested_overlay_project_id,
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
        audit_event_type,
        (SELECT pb.managed_execution_id FROM principal_bindings AS pb WHERE pb.id = binding_id),
        derived_role_name,
        next_generation,
        requested_project_id
    );
    RETURN QUERY SELECT derived_role_name, next_generation, FALSE,
        (SELECT pb.managed_execution_id FROM principal_bindings AS pb WHERE pb.id = binding_id);
END
$$;
ALTER FUNCTION gobby_agent_auth.issue_or_reuse_interactive_principal(requested_deployment_token text, requested_machine_id uuid, requested_project_id uuid, requested_session_id uuid, requested_expires_at timestamp with time zone, requested_password text, requested_overlay_project_id uuid) OWNER TO gobby_agent_issuer;
CREATE FUNCTION gobby_agent_auth.issue_principal(requested_execution_id uuid, requested_owner_kind text, requested_session_id uuid, requested_agent_run_id uuid, requested_machine_id uuid, requested_expires_at timestamp with time zone, requested_password text) RETURNS TABLE(role_name name, credential_generation integer)
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
ALTER FUNCTION gobby_agent_auth.issue_principal(requested_execution_id uuid, requested_owner_kind text, requested_session_id uuid, requested_agent_run_id uuid, requested_machine_id uuid, requested_expires_at timestamp with time zone, requested_password text) OWNER TO gobby_agent_issuer;
CREATE FUNCTION gobby_agent_auth.issue_tool_principal(p_execution_id uuid, p_session_id uuid, p_machine_id uuid, p_expires_at timestamp with time zone, p_password text) RETURNS TABLE(role_name name, credential_generation integer)
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
      AND COALESCE(session.status, 'active') NOT IN ('expired', 'deleted');
    IF NOT FOUND THEN
        RAISE EXCEPTION 'managed principal session does not exist'
            USING ERRCODE = '23503';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.machines WHERE id = p_machine_id) THEN
        RAISE EXCEPTION 'managed principal issuing machine does not exist'
            USING ERRCODE = '23503';
    END IF;
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
ALTER FUNCTION gobby_agent_auth.issue_tool_principal(p_execution_id uuid, p_session_id uuid, p_machine_id uuid, p_expires_at timestamp with time zone, p_password text) OWNER TO gobby_agent_issuer;
CREATE FUNCTION gobby_agent_auth.list_active_principals() RETURNS TABLE(role_name name, managed_execution_id uuid, owner_kind text, agent_run_id uuid, session_id uuid, project_id uuid, expires_at timestamp with time zone, login_capable boolean, active_sessions bigint)
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'gobby_agent_auth'
    AS $$
SELECT
    binding.role_name,
    binding.managed_execution_id,
    binding.owner_kind,
    binding.agent_run_id,
    binding.session_id,
    binding.project_id,
    binding.expires_at,
    role.rolcanlogin,
    count(activity.pid) FILTER (WHERE activity.pid IS NOT NULL)
FROM gobby_agent_auth.principal_bindings AS binding
JOIN pg_catalog.pg_roles AS role
    ON role.rolname = binding.role_name
LEFT JOIN pg_catalog.pg_stat_activity AS activity
    ON activity.usename = binding.role_name::TEXT
WHERE binding.revoked_at IS NULL
GROUP BY
    binding.role_name,
    binding.managed_execution_id,
    binding.owner_kind,
    binding.agent_run_id,
    binding.session_id,
    binding.project_id,
    binding.expires_at,
    role.rolcanlogin
ORDER BY binding.expires_at, binding.managed_execution_id
$$;
ALTER FUNCTION gobby_agent_auth.list_active_principals() OWNER TO gobby_agent_issuer;
CREATE FUNCTION gobby_agent_auth.load_interactive_credential_material(requested_deployment_token text, requested_machine_id uuid, requested_project_id uuid, requested_generation integer) RETURNS TABLE(ciphertext text, aad_identity text)
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'gobby_agent_auth', 'pg_temp'
    AS $$
    SELECT ciphertext, aad_identity
      FROM interactive_credential_material
     WHERE deployment_token = requested_deployment_token
       AND machine_id = requested_machine_id
       AND project_id = requested_project_id
       AND credential_generation = requested_generation
$$;
ALTER FUNCTION gobby_agent_auth.load_interactive_credential_material(requested_deployment_token text, requested_machine_id uuid, requested_project_id uuid, requested_generation integer) OWNER TO gobby_agent_issuer;
CREATE FUNCTION gobby_agent_auth.lookup_interactive_principal(requested_deployment_token text, requested_machine_id uuid, requested_project_id uuid, requested_generation integer) RETURNS TABLE(managed_execution_id uuid, role_name name, credential_generation integer, revoked_at timestamp with time zone)
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'gobby_agent_auth', 'pg_temp'
    AS $$
    SELECT managed_execution_id, role_name, credential_generation, revoked_at
      FROM principal_bindings
     WHERE owner_kind = 'interactive'
       AND deployment_token = requested_deployment_token
       AND issuing_machine_id = requested_machine_id
       AND project_id = requested_project_id
       AND (
           requested_generation IS NULL
           OR credential_generation = requested_generation
       )
     ORDER BY credential_generation DESC
     LIMIT 1
$$;
ALTER FUNCTION gobby_agent_auth.lookup_interactive_principal(requested_deployment_token text, requested_machine_id uuid, requested_project_id uuid, requested_generation integer) OWNER TO gobby_agent_issuer;
CREATE FUNCTION gobby_agent_auth.managed_execution_is_login_capable(p_execution_id uuid) RETURNS boolean
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'gobby_agent_auth'
    AS $$
    SELECT EXISTS (
        SELECT 1
        FROM gobby_agent_auth.principal_bindings AS binding
        JOIN pg_catalog.pg_roles AS role ON role.rolname = binding.role_name
        WHERE binding.managed_execution_id = p_execution_id
          AND binding.revoked_at IS NULL
          AND binding.expires_at > clock_timestamp()
          AND role.rolcanlogin
    )
$$;
ALTER FUNCTION gobby_agent_auth.managed_execution_is_login_capable(p_execution_id uuid) OWNER TO gobby_agent_issuer;
CREATE FUNCTION gobby_agent_auth.principals_due_for_rotation(p_machine_id uuid) RETURNS TABLE(managed_execution_id uuid, role_name name, credential_generation integer)
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'gobby_agent_auth'
    AS $$
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
      AND pb.owner_kind <> 'interactive'
      AND pb.issued_at <= clock_timestamp() - INTERVAL '45 minutes'
      AND pb.expires_at > clock_timestamp()
    ORDER BY pb.managed_execution_id
$$;
ALTER FUNCTION gobby_agent_auth.principals_due_for_rotation(p_machine_id uuid) OWNER TO gobby_agent_issuer;
CREATE FUNCTION gobby_agent_auth.reconcile_daemon(p_machine_id uuid) RETURNS integer
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
$_$;
ALTER FUNCTION gobby_agent_auth.reconcile_daemon(p_machine_id uuid) OWNER TO gobby_test;
CREATE FUNCTION gobby_agent_auth.reconcile_principal(requested_execution_id uuid) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'gobby_agent_auth', 'pg_temp'
    SET createrole_self_grant TO ''
    AS $$
DECLARE
    binding principal_bindings%ROWTYPE;
    reconciled_count INTEGER := 0;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(requested_execution_id::TEXT, 0));
    FOR binding IN
        SELECT * FROM principal_bindings
        WHERE managed_execution_id = requested_execution_id
          AND revoked_at IS NULL
          AND (
              expires_at <= clock_timestamp()
              OR NOT EXISTS (
                  SELECT 1 FROM pg_roles WHERE rolname = principal_bindings.role_name
              )
          )
        ORDER BY credential_generation
    LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = binding.role_name) THEN
            reconciled_count := reconciled_count + revoke_principal(
                requested_execution_id,
                binding.credential_generation
            );
        ELSE
            UPDATE principal_bindings
            SET revoked_at = clock_timestamp()
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
                'reconcile',
                binding.managed_execution_id,
                binding.role_name,
                binding.credential_generation,
                binding.project_id,
                '{"reason":"missing_role"}'::JSONB
            );
            reconciled_count := reconciled_count + 1;
        END IF;
    END LOOP;
    RETURN reconciled_count;
END
$$;
ALTER FUNCTION gobby_agent_auth.reconcile_principal(requested_execution_id uuid) OWNER TO gobby_agent_issuer;
CREATE FUNCTION gobby_agent_auth.replace_interactive_credential_material(requested_deployment_token text, requested_machine_id uuid, requested_project_id uuid, requested_generation integer, requested_ciphertext text, requested_aad_identity text) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'gobby_agent_auth', 'pg_temp'
    AS $$
BEGIN
    IF requested_ciphertext IS NULL OR requested_ciphertext = '' THEN
        RAISE EXCEPTION 'interactive credential material must be ciphertext'
            USING ERRCODE = '22023';
    END IF;
    IF requested_aad_identity IS NULL OR requested_aad_identity = '' THEN
        RAISE EXCEPTION 'interactive credential material requires AAD identity'
            USING ERRCODE = '22023';
    END IF;
    PERFORM 1
       FROM principal_bindings AS pb
      WHERE pb.owner_kind = 'interactive'
        AND pb.deployment_token = requested_deployment_token
        AND pb.issuing_machine_id = requested_machine_id
        AND pb.project_id = requested_project_id
        AND pb.credential_generation = requested_generation
        AND pb.revoked_at IS NULL
        FOR UPDATE;
    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;
    DELETE FROM interactive_credential_material AS icm
     WHERE icm.deployment_token = requested_deployment_token
       AND icm.machine_id = requested_machine_id
       AND icm.project_id = requested_project_id
       AND icm.credential_generation <> requested_generation
       AND NOT EXISTS (
           SELECT 1
             FROM principal_bindings AS pb
            WHERE pb.owner_kind = 'interactive'
              AND pb.deployment_token = icm.deployment_token
              AND pb.issuing_machine_id = icm.machine_id
              AND pb.project_id = icm.project_id
              AND pb.credential_generation = icm.credential_generation
              AND pb.revoked_at IS NULL
       );
    INSERT INTO interactive_credential_material (
        deployment_token,
        machine_id,
        project_id,
        credential_generation,
        ciphertext,
        aad_identity
    ) VALUES (
        requested_deployment_token,
        requested_machine_id,
        requested_project_id,
        requested_generation,
        requested_ciphertext,
        requested_aad_identity
    )
    ON CONFLICT (deployment_token, machine_id, project_id, credential_generation)
    DO UPDATE SET
        ciphertext = EXCLUDED.ciphertext,
        aad_identity = EXCLUDED.aad_identity,
        created_at = clock_timestamp();
    RETURN TRUE;
END
$$;
ALTER FUNCTION gobby_agent_auth.replace_interactive_credential_material(requested_deployment_token text, requested_machine_id uuid, requested_project_id uuid, requested_generation integer, requested_ciphertext text, requested_aad_identity text) OWNER TO gobby_agent_issuer;
CREATE FUNCTION gobby_agent_auth.resolve_tool_session(p_session_id uuid) RETURNS TABLE(session_id uuid, project_id uuid, machine_id uuid, root_path text)
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
    SELECT session.id, session.project_id, session.machine_id, checkout.root_path
    FROM public.sessions AS session
    LEFT JOIN public.project_checkouts AS checkout
      ON checkout.machine_id = session.machine_id
     AND checkout.project_id = session.project_id
    WHERE session.id = p_session_id
      AND COALESCE(session.status, 'active') NOT IN ('expired', 'deleted')
$$;
ALTER FUNCTION gobby_agent_auth.resolve_tool_session(p_session_id uuid) OWNER TO gobby_test;
CREATE FUNCTION gobby_agent_auth.revoke_principal(requested_execution_id uuid, requested_generation integer DEFAULT NULL::integer) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'gobby_agent_auth', 'pg_temp'
    SET createrole_self_grant TO ''
    AS $$
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
        IF binding.owner_kind = 'interactive' AND binding.deployment_token IS NOT NULL THEN
            DELETE FROM interactive_credential_material
             WHERE deployment_token = binding.deployment_token
               AND machine_id = binding.issuing_machine_id
               AND project_id = binding.project_id
               AND credential_generation = binding.credential_generation;
        END IF;
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
$$;
ALTER FUNCTION gobby_agent_auth.revoke_principal(requested_execution_id uuid, requested_generation integer) OWNER TO gobby_agent_issuer;
CREATE FUNCTION gobby_agent_auth.rotate_interactive_principal(requested_deployment_token text, requested_machine_id uuid, requested_project_id uuid, requested_session_id uuid, requested_expires_at timestamp with time zone, requested_password text, requested_drain_until timestamp with time zone, requested_overlay_project_id uuid) RETURNS TABLE(role_name name, credential_generation integer, managed_execution_id uuid)
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'gobby_agent_auth', 'pg_temp'
    SET createrole_self_grant TO ''
    AS $$
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
    IF requested_expires_at IS NULL THEN
        RAISE EXCEPTION 'managed principal expiry must be in the future'
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
    PERFORM gobby_agent_auth.assert_interactive_overlay_registered(
        requested_machine_id,
        requested_project_id,
        requested_overlay_project_id
    );
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
       AND code_overlay_project_id IS NOT DISTINCT FROM requested_overlay_project_id
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
        code_overlay_project_id,
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
        requested_overlay_project_id,
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
$$;
ALTER FUNCTION gobby_agent_auth.rotate_interactive_principal(requested_deployment_token text, requested_machine_id uuid, requested_project_id uuid, requested_session_id uuid, requested_expires_at timestamp with time zone, requested_password text, requested_drain_until timestamp with time zone, requested_overlay_project_id uuid) OWNER TO gobby_agent_issuer;
CREATE FUNCTION gobby_agent_auth.rotate_principal(requested_execution_id uuid, requested_expires_at timestamp with time zone, requested_password text) RETURNS TABLE(role_name name, credential_generation integer)
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
      AND expires_at > clock_timestamp();
    IF active_count <> 1 THEN
        RAISE EXCEPTION 'rotation requires exactly one active principal binding'
            USING ERRCODE = '42501';
    END IF;
    SELECT * INTO STRICT source_binding
    FROM principal_bindings
    WHERE managed_execution_id = requested_execution_id
      AND revoked_at IS NULL
      AND expires_at > clock_timestamp();
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
ALTER FUNCTION gobby_agent_auth.rotate_principal(requested_execution_id uuid, requested_expires_at timestamp with time zone, requested_password text) OWNER TO gobby_agent_issuer;
CREATE FUNCTION gobby_agent_auth.rotate_principal_if_generation(p_execution_id uuid, p_expected_generation integer, p_expires_at timestamp with time zone, p_password text) RETURNS TABLE(role_name name, credential_generation integer)
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
$$;
ALTER FUNCTION gobby_agent_auth.rotate_principal_if_generation(p_execution_id uuid, p_expected_generation integer, p_expires_at timestamp with time zone, p_password text) OWNER TO gobby_agent_issuer;
CREATE FUNCTION public.compute_task_state_bucket(p_task_id uuid) RETURNS text
    LANGUAGE sql STABLE
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
ALTER FUNCTION public.compute_task_state_bucket(p_task_id uuid) OWNER TO gobby_test;
CREATE FUNCTION public.enforce_chat_attachments_bound_at_write_once() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF OLD.bound_at IS NOT NULL AND NEW.bound_at IS DISTINCT FROM OLD.bound_at THEN
        RAISE EXCEPTION 'chat_attachments.bound_at is write-once';
    END IF;
    RETURN NEW;
END;
$$;
ALTER FUNCTION public.enforce_chat_attachments_bound_at_write_once() OWNER TO gobby_test;
CREATE FUNCTION public.gobby_maintenance_epoch_login_guard() RETURNS event_trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
        DECLARE
            active_epoch UUID;
            supplied_epoch TEXT;
        BEGIN
            IF pg_catalog.pg_is_in_recovery() THEN
                RETURN;
            END IF;
            BEGIN
                SELECT id
                INTO active_epoch
                FROM public.maintenance_epochs
                WHERE released_at IS NULL;
            EXCEPTION
                WHEN undefined_table OR invalid_schema_name THEN
                    RETURN;
            END;
            IF active_epoch IS NULL THEN
                RETURN;
            END IF;
            supplied_epoch :=
                pg_catalog.current_setting('gobby.maintenance_epoch', TRUE);
            IF supplied_epoch IS DISTINCT FROM active_epoch::TEXT THEN
                RAISE EXCEPTION USING
                    ERRCODE = '55000',
                    MESSAGE =
                        'Gobby hub maintenance is active; use '
                        '`gobby hub-maintenance status` to inspect it or '
                        '`gobby hub-maintenance resume` from the operator shell.';
            END IF;
        END;
        $$;
ALTER FUNCTION public.gobby_maintenance_epoch_login_guard() OWNER TO gobby_test;
CREATE FUNCTION public.memories_tags_to_text(tags jsonb) RETURNS text
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $$
    SELECT COALESCE(string_agg(value, ' ' ORDER BY ord), '')
    FROM jsonb_array_elements_text(tags) WITH ORDINALITY AS t(value, ord);
$$;
ALTER FUNCTION public.memories_tags_to_text(tags jsonb) OWNER TO gobby_test;
CREATE FUNCTION public.refresh_task_state_bucket(p_task_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
BEGIN
    UPDATE tasks
       SET state_bucket = compute_task_state_bucket(p_task_id)
     WHERE id = p_task_id;
END;
$$;
ALTER FUNCTION public.refresh_task_state_bucket(p_task_id uuid) OWNER TO gobby_test;
CREATE FUNCTION public.refresh_task_state_bucket_from_stage() RETURNS trigger
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
ALTER FUNCTION public.refresh_task_state_bucket_from_stage() OWNER TO gobby_test;
CREATE FUNCTION public.refresh_task_state_bucket_from_task() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    PERFORM refresh_task_state_bucket(NEW.id);
    RETURN NEW;
END;
$$;
ALTER FUNCTION public.refresh_task_state_bucket_from_task() OWNER TO gobby_test;
CREATE FUNCTION public.touch_chat_attachments_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF NEW.updated_at IS NOT DISTINCT FROM OLD.updated_at THEN
        NEW.updated_at := NOW();
    END IF;
    RETURN NEW;
END;
$$;
ALTER FUNCTION public.touch_chat_attachments_updated_at() OWNER TO gobby_test;
SET default_tablespace = '';
SET default_table_access_method = heap;
CREATE TABLE gobby_agent_auth.daemon_registry (
    machine_id uuid NOT NULL,
    heartbeat_at timestamp with time zone NOT NULL,
    lease_expires_at timestamp with time zone NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT daemon_registry_check CHECK ((lease_expires_at > heartbeat_at))
);
ALTER TABLE gobby_agent_auth.daemon_registry OWNER TO gobby_agent_issuer;
CREATE TABLE gobby_agent_auth.interactive_credential_material (
    deployment_token text NOT NULL,
    machine_id uuid NOT NULL,
    project_id uuid NOT NULL,
    credential_generation integer NOT NULL,
    ciphertext text NOT NULL,
    aad_identity text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT interactive_credential_material_credential_generation_check CHECK ((credential_generation > 0))
);
ALTER TABLE gobby_agent_auth.interactive_credential_material OWNER TO gobby_agent_issuer;
CREATE TABLE gobby_agent_auth.orphan_revocation_retries (
    role_name name NOT NULL,
    revocation_attempts integer DEFAULT 0 NOT NULL,
    next_retry_at timestamp with time zone NOT NULL,
    last_failure text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE gobby_agent_auth.orphan_revocation_retries OWNER TO gobby_agent_issuer;
CREATE TABLE gobby_agent_auth.principal_audit_events (
    id bigint NOT NULL,
    binding_id uuid,
    event_type text NOT NULL,
    managed_execution_id uuid NOT NULL,
    role_name name NOT NULL,
    credential_generation integer NOT NULL,
    project_id uuid NOT NULL,
    occurred_at timestamp with time zone DEFAULT now() NOT NULL,
    details jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT principal_audit_events_event_type_check CHECK ((event_type = ANY (ARRAY['issue'::text, 'rotate'::text, 'revoke'::text, 'reconcile'::text, 'revoke_retry'::text])))
);
ALTER TABLE gobby_agent_auth.principal_audit_events OWNER TO gobby_agent_issuer;
ALTER TABLE gobby_agent_auth.principal_audit_events ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME gobby_agent_auth.principal_audit_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);
CREATE TABLE gobby_agent_auth.principal_bindings (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    role_name name NOT NULL,
    owner_kind text NOT NULL,
    managed_execution_id uuid NOT NULL,
    agent_run_id uuid,
    session_id uuid,
    project_id uuid NOT NULL,
    issuing_machine_id uuid NOT NULL,
    issued_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    revoked_at timestamp with time zone,
    credential_generation integer NOT NULL,
    revocation_requested_at timestamp with time zone,
    revocation_attempts integer DEFAULT 0 NOT NULL,
    next_revocation_retry_at timestamp with time zone,
    last_revocation_failure text,
    predecessor_drain_deadline timestamp with time zone,
    code_overlay_project_id uuid,
    deployment_token text,
    CONSTRAINT principal_bindings_credential_generation_check CHECK ((credential_generation > 0)),
    CONSTRAINT principal_bindings_expiry_after_issue CHECK ((expires_at > issued_at)),
    CONSTRAINT principal_bindings_owner_kind_check CHECK ((owner_kind = ANY (ARRAY['agent_run'::text, 'tool_chat'::text, 'interactive'::text, 'maintenance'::text]))),
    CONSTRAINT principal_bindings_revoke_after_issue CHECK (((revoked_at IS NULL) OR (revoked_at >= issued_at)))
);
ALTER TABLE gobby_agent_auth.principal_bindings OWNER TO gobby_agent_issuer;
CREATE TABLE public.agent_definitions (
    id uuid NOT NULL,
    project_id uuid,
    name text NOT NULL,
    description text,
    enabled boolean DEFAULT true NOT NULL,
    enabled_pinned boolean DEFAULT false NOT NULL,
    definition_json jsonb NOT NULL,
    source text DEFAULT 'installed'::text NOT NULL,
    tags jsonb,
    deleted_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT agent_definitions_source_check CHECK ((source = ANY (ARRAY['installed'::text, 'custom'::text, 'project'::text])))
);
ALTER TABLE public.agent_definitions OWNER TO gobby_test;
CREATE TABLE public.agent_runs (
    id uuid NOT NULL,
    machine_id uuid NOT NULL,
    parent_session_id uuid NOT NULL,
    child_session_id uuid,
    claimed_session_id uuid,
    workflow_name text,
    agent_name text,
    provider text NOT NULL,
    model text,
    is_local boolean DEFAULT false NOT NULL,
    requested_reasoning_effort text,
    effective_reasoning_effort text,
    reasoning_required boolean DEFAULT false NOT NULL,
    reasoning_status text DEFAULT 'not_requested'::text NOT NULL,
    reasoning_message text,
    status text DEFAULT 'pending'::text NOT NULL,
    prompt text NOT NULL,
    result text,
    error text,
    tool_calls_count integer DEFAULT 0,
    turns_used integer DEFAULT 0,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    sdk_session_id text,
    continuation_prompt text,
    task_id uuid,
    pid integer,
    worktree_id uuid,
    clone_id uuid,
    timeout_seconds real,
    terminal_reason text,
    resume_metadata_json jsonb,
    capture_id text,
    capture_revision bigint DEFAULT 0 NOT NULL,
    pending_terminal_action text,
    pending_terminal_reason text,
    termination_requested_at timestamp with time zone,
    terminal_id uuid,
    CONSTRAINT agent_runs_pending_terminal_action_valid CHECK (((pending_terminal_action IS NULL) OR (pending_terminal_action = ANY (ARRAY['complete'::text, 'fail'::text, 'timeout'::text, 'cancel'::text]))))
);
ALTER TABLE public.agent_runs OWNER TO gobby_test;
CREATE TABLE public.agent_step_instances (
    id uuid NOT NULL,
    session_id uuid NOT NULL,
    agent_step_workflow_id uuid,
    agent_name text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    current_step text,
    step_entered_at timestamp with time zone,
    step_action_count integer DEFAULT 0 NOT NULL,
    total_action_count integer DEFAULT 0 NOT NULL,
    variables jsonb DEFAULT '{}'::jsonb NOT NULL,
    context_injected boolean DEFAULT false NOT NULL,
    snapshot_json jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE public.agent_step_instances OWNER TO gobby_test;
CREATE TABLE public.agent_step_workflows (
    id uuid NOT NULL,
    agent_definition_id uuid NOT NULL,
    steps_json jsonb NOT NULL,
    variables_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    exit_condition text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE public.agent_step_workflows OWNER TO gobby_test;
CREATE TABLE public.attention_states (
    entry_id text NOT NULL,
    run_id text,
    session_id text,
    attention_id text NOT NULL,
    state text,
    reason text,
    kind text,
    fingerprint text,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    since timestamp with time zone,
    seen_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT attention_states_check CHECK (((state IS NULL) OR ((attention_id IS NOT NULL) AND (reason IS NOT NULL) AND (kind IS NOT NULL) AND (fingerprint IS NOT NULL) AND (since IS NOT NULL)))),
    CONSTRAINT attention_states_kind_check CHECK (((kind IS NULL) OR (kind = ANY (ARRAY['actionable'::text, 'non_actionable'::text])))),
    CONSTRAINT attention_states_state_check CHECK (((state IS NULL) OR (state = 'blocked'::text)))
);
ALTER TABLE public.attention_states OWNER TO gobby_test;
CREATE TABLE public.auth_sessions (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    token_hash text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    remember_me boolean DEFAULT false NOT NULL
);
ALTER TABLE public.auth_sessions OWNER TO gobby_test;
CREATE TABLE public.bin_update_state (
    machine_id uuid NOT NULL,
    tool_name text NOT NULL,
    installed_version text,
    floor_version text NOT NULL,
    latest_version text,
    binary_path text,
    target text,
    last_status text NOT NULL,
    last_error text,
    checked_at timestamp with time zone DEFAULT now() NOT NULL,
    installed_at timestamp with time zone,
    source_url text,
    is_dev boolean DEFAULT false NOT NULL,
    floor_drift boolean DEFAULT false NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT bin_update_state_floor_drift_check CHECK ((floor_drift = ANY (ARRAY[false, true]))),
    CONSTRAINT bin_update_state_is_dev_check CHECK ((is_dev = ANY (ARRAY[false, true]))),
    CONSTRAINT bin_update_state_last_status_check CHECK ((last_status = ANY (ARRAY['updated'::text, 'up_to_date'::text, 'failed'::text, 'floor_violated'::text, 'dev'::text, 'source_unavailable'::text])))
);
ALTER TABLE public.bin_update_state OWNER TO gobby_test;
CREATE TABLE public.build_history_events (
    id integer NOT NULL,
    run_id uuid,
    project_id uuid NOT NULL,
    root_task_id uuid,
    task_id uuid,
    event_type text NOT NULL,
    action text,
    message text,
    payload_json jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE public.build_history_events OWNER TO gobby_test;
ALTER TABLE public.build_history_events ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.build_history_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);
CREATE TABLE public.build_profiles (
    id uuid NOT NULL,
    name text NOT NULL,
    display_label text NOT NULL,
    description text NOT NULL,
    skip_stages_json jsonb DEFAULT '[]'::jsonb NOT NULL,
    isolation text DEFAULT 'worktree'::text NOT NULL,
    unattended boolean DEFAULT false NOT NULL,
    plan_enhancement_rounds integer DEFAULT 0 NOT NULL,
    delivery_mode text DEFAULT 'auto'::text NOT NULL,
    delivery_target_repo text,
    enabled boolean DEFAULT true NOT NULL,
    source text NOT NULL,
    project_id uuid,
    tags_json jsonb DEFAULT '[]'::jsonb NOT NULL,
    bundled_hash text,
    deleted_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT build_profiles_check CHECK (((source <> 'installed'::text) OR (project_id IS NULL))),
    CONSTRAINT build_profiles_delivery_mode_check CHECK ((delivery_mode = ANY (ARRAY['auto'::text, 'pull_request'::text]))),
    CONSTRAINT build_profiles_enabled_check CHECK ((enabled = ANY (ARRAY[false, true]))),
    CONSTRAINT build_profiles_isolation_check CHECK ((isolation = ANY (ARRAY['none'::text, 'worktree'::text, 'clone'::text]))),
    CONSTRAINT build_profiles_plan_enhancement_rounds_check CHECK ((plan_enhancement_rounds >= 0)),
    CONSTRAINT build_profiles_source_check CHECK ((source = ANY (ARRAY['installed'::text, 'project'::text]))),
    CONSTRAINT build_profiles_unattended_check CHECK ((unattended = ANY (ARRAY[false, true])))
);
ALTER TABLE public.build_profiles OWNER TO gobby_test;
CREATE TABLE public.build_runs (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    root_task_id uuid,
    input_ref text,
    action text NOT NULL,
    status text DEFAULT 'started'::text NOT NULL,
    actor text DEFAULT 'build'::text NOT NULL,
    summary_json jsonb,
    error text,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    CONSTRAINT build_runs_status_check CHECK ((status = ANY (ARRAY['started'::text, 'completed'::text, 'failed'::text, 'skipped'::text])))
);
ALTER TABLE public.build_runs OWNER TO gobby_test;
CREATE TABLE public.chat_attachment_cleanup_fences (
    scope_kind text NOT NULL,
    scope_id text NOT NULL,
    token uuid,
    owner text,
    claimed_at timestamp with time zone,
    state text NOT NULL,
    CONSTRAINT chat_attachment_cleanup_fences_scope_kind_check CHECK ((scope_kind = ANY (ARRAY['conversation'::text, 'session'::text]))),
    CONSTRAINT chat_attachment_cleanup_fences_state_check CHECK ((state = ANY (ARRAY['idle'::text, 'active'::text, 'terminal'::text])))
);
ALTER TABLE public.chat_attachment_cleanup_fences OWNER TO gobby_test;
CREATE TABLE public.chat_attachments (
    id uuid NOT NULL,
    machine_id uuid NOT NULL,
    project_id uuid NOT NULL,
    draft_id text,
    conversation_id text,
    message_id text,
    target_session_id uuid,
    filename text NOT NULL,
    mime_type text NOT NULL,
    size_bytes integer NOT NULL,
    local_path text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    bound_at timestamp with time zone,
    claim_token uuid,
    claimed_at timestamp with time zone,
    published boolean DEFAULT false NOT NULL,
    CONSTRAINT chat_attachments_size_bytes_check CHECK ((size_bytes >= 0))
);
ALTER TABLE public.chat_attachments OWNER TO gobby_test;
CREATE TABLE public.chat_messages (
    id uuid NOT NULL,
    conversation_id text NOT NULL,
    role text NOT NULL,
    content text DEFAULT ''::text NOT NULL,
    tool_calls_json jsonb,
    content_blocks_json jsonb,
    metadata_json jsonb,
    seq integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE public.chat_messages OWNER TO gobby_test;
CREATE TABLE public.checkpoints (
    id uuid NOT NULL,
    task_id uuid NOT NULL,
    session_id uuid,
    run_id uuid NOT NULL,
    ref_name text NOT NULL,
    commit_sha text NOT NULL,
    parent_sha text NOT NULL,
    files_changed integer DEFAULT 0 NOT NULL,
    message text DEFAULT 'auto-checkpoint'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE public.checkpoints OWNER TO gobby_test;
CREATE TABLE public.clones (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    machine_id uuid NOT NULL,
    branch_name text,
    clone_path text NOT NULL,
    base_branch text DEFAULT 'main'::text,
    task_id uuid,
    agent_session_id uuid,
    status text DEFAULT 'active'::text,
    remote_url text,
    last_sync_at timestamp with time zone,
    cleanup_after timestamp with time zone,
    workspace_role text DEFAULT 'task'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE public.clones OWNER TO gobby_test;
CREATE TABLE public.code_calls (
    id integer NOT NULL,
    project_id uuid NOT NULL,
    caller_symbol_id uuid,
    callee_symbol_id uuid,
    callee_name text NOT NULL,
    callee_target_kind text DEFAULT 'unresolved'::text NOT NULL,
    callee_external_module text DEFAULT ''::text NOT NULL,
    file_path text NOT NULL,
    content_hash text NOT NULL,
    line integer DEFAULT 0 NOT NULL
);
ALTER TABLE ONLY public.code_calls FORCE ROW LEVEL SECURITY;
ALTER TABLE public.code_calls OWNER TO gobby_test;
ALTER TABLE public.code_calls ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.code_calls_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);
CREATE TABLE public.code_content_chunks (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    file_path text NOT NULL,
    content_hash text NOT NULL,
    chunk_index integer NOT NULL,
    line_start integer NOT NULL,
    line_end integer NOT NULL,
    content text NOT NULL,
    language text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE ONLY public.code_content_chunks FORCE ROW LEVEL SECURITY;
ALTER TABLE public.code_content_chunks OWNER TO gobby_test;
CREATE TABLE public.code_imports (
    id integer NOT NULL,
    project_id uuid NOT NULL,
    source_file text NOT NULL,
    content_hash text NOT NULL,
    target_module text NOT NULL
);
ALTER TABLE ONLY public.code_imports FORCE ROW LEVEL SECURITY;
ALTER TABLE public.code_imports OWNER TO gobby_test;
ALTER TABLE public.code_imports ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.code_imports_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);
CREATE TABLE public.code_index_projection_cleanup_pending (
    project_id uuid NOT NULL,
    store text NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    last_error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT code_index_projection_cleanup_store CHECK ((store = ANY (ARRAY['graph'::text, 'vector'::text])))
);
ALTER TABLE ONLY public.code_index_projection_cleanup_pending FORCE ROW LEVEL SECURITY;
ALTER TABLE public.code_index_projection_cleanup_pending OWNER TO gobby_test;
CREATE TABLE public.code_index_prune_dirty_projects (
    machine_id uuid NOT NULL,
    project_id uuid NOT NULL,
    root_path text NOT NULL,
    reason text NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    last_error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE ONLY public.code_index_prune_dirty_projects FORCE ROW LEVEL SECURITY;
ALTER TABLE public.code_index_prune_dirty_projects OWNER TO gobby_test;
CREATE TABLE public.code_indexed_file_states (
    machine_id uuid NOT NULL,
    project_id uuid NOT NULL,
    file_path text NOT NULL,
    content_hash text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE ONLY public.code_indexed_file_states FORCE ROW LEVEL SECURITY;
ALTER TABLE public.code_indexed_file_states OWNER TO gobby_test;
CREATE TABLE public.code_indexed_files (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    file_path text NOT NULL,
    language text NOT NULL,
    content_hash text NOT NULL,
    symbol_count integer DEFAULT 0 NOT NULL,
    byte_size integer DEFAULT 0 NOT NULL,
    graph_synced boolean DEFAULT false NOT NULL,
    vectors_synced boolean DEFAULT false NOT NULL,
    graph_sync_attempted_at timestamp with time zone,
    vector_sync_attempted_at timestamp with time zone,
    indexed_at timestamp with time zone DEFAULT now() NOT NULL,
    last_referenced_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE ONLY public.code_indexed_files FORCE ROW LEVEL SECURITY;
ALTER TABLE public.code_indexed_files OWNER TO gobby_test;
CREATE TABLE public.code_indexed_project_states (
    machine_id uuid NOT NULL,
    project_id uuid NOT NULL,
    root_path text NOT NULL,
    total_files integer DEFAULT 0 NOT NULL,
    total_symbols integer DEFAULT 0 NOT NULL,
    last_indexed_at timestamp with time zone,
    index_duration_ms integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    indexer_version text
);
ALTER TABLE ONLY public.code_indexed_project_states FORCE ROW LEVEL SECURITY;
ALTER TABLE public.code_indexed_project_states OWNER TO gobby_test;
CREATE TABLE public.code_indexed_projects (
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE ONLY public.code_indexed_projects FORCE ROW LEVEL SECURITY;
ALTER TABLE public.code_indexed_projects OWNER TO gobby_test;
CREATE TABLE public.code_inheritance (
    id integer NOT NULL,
    project_id uuid NOT NULL,
    source_symbol_id uuid,
    source_name text NOT NULL,
    source_kind text DEFAULT 'symbol'::text NOT NULL,
    source_external_module text DEFAULT ''::text NOT NULL,
    target_symbol_id uuid,
    target_name text NOT NULL,
    target_kind text DEFAULT 'unresolved'::text NOT NULL,
    target_external_module text DEFAULT ''::text NOT NULL,
    heritage_kind text NOT NULL,
    file_path text NOT NULL,
    content_hash text NOT NULL,
    line integer DEFAULT 0 NOT NULL,
    CONSTRAINT code_inheritance_heritage_kind_check CHECK ((heritage_kind = ANY (ARRAY['INHERITS'::text, 'EXTENDS'::text, 'IMPLEMENTS'::text])))
);
ALTER TABLE ONLY public.code_inheritance FORCE ROW LEVEL SECURITY;
ALTER TABLE public.code_inheritance OWNER TO gobby_test;
ALTER TABLE public.code_inheritance ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.code_inheritance_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);
CREATE TABLE public.code_symbols (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    file_path text NOT NULL,
    name text NOT NULL,
    qualified_name text NOT NULL,
    kind text NOT NULL,
    language text NOT NULL,
    byte_start integer NOT NULL,
    byte_end integer NOT NULL,
    line_start integer NOT NULL,
    line_end integer NOT NULL,
    signature text,
    docstring text,
    parent_symbol_id uuid,
    file_content_hash text NOT NULL,
    content_hash text NOT NULL,
    summary text,
    summary_attempted_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE ONLY public.code_symbols FORCE ROW LEVEL SECURITY;
ALTER TABLE public.code_symbols OWNER TO gobby_test;
CREATE TABLE public.comms_attachments (
    id uuid NOT NULL,
    machine_id uuid NOT NULL,
    message_id uuid NOT NULL,
    filename text NOT NULL,
    content_type text DEFAULT 'application/octet-stream'::text NOT NULL,
    size_bytes integer DEFAULT 0 NOT NULL,
    local_path text,
    platform_url text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE public.comms_attachments OWNER TO gobby_test;
CREATE TABLE public.comms_channels (
    id uuid NOT NULL,
    channel_type text NOT NULL,
    name text NOT NULL,
    enabled boolean DEFAULT true,
    config_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    webhook_secret text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE public.comms_channels OWNER TO gobby_test;
CREATE TABLE public.comms_identities (
    id uuid NOT NULL,
    channel_id uuid NOT NULL,
    external_user_id text NOT NULL,
    external_username text,
    session_id uuid,
    project_id uuid,
    metadata_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE public.comms_identities OWNER TO gobby_test;
CREATE TABLE public.comms_messages (
    id uuid NOT NULL,
    channel_id uuid NOT NULL,
    identity_id uuid,
    direction text NOT NULL,
    content text NOT NULL,
    content_type text DEFAULT 'text'::text NOT NULL,
    platform_message_id text,
    platform_thread_id text,
    session_id uuid,
    status text DEFAULT 'sent'::text NOT NULL,
    error text,
    metadata_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT comms_messages_direction_check CHECK ((direction = ANY (ARRAY['inbound'::text, 'outbound'::text])))
);
ALTER TABLE public.comms_messages OWNER TO gobby_test;
CREATE TABLE public.comms_routing_rules (
    id uuid NOT NULL,
    name text NOT NULL,
    channel_id uuid,
    event_pattern text DEFAULT '*'::text NOT NULL,
    project_id uuid,
    session_id uuid,
    priority integer DEFAULT 0,
    enabled boolean DEFAULT true,
    config_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE public.comms_routing_rules OWNER TO gobby_test;
CREATE TABLE public.completion_subscribers (
    completion_id uuid NOT NULL,
    session_id uuid NOT NULL
);
ALTER TABLE public.completion_subscribers OWNER TO gobby_test;
CREATE TABLE public.config_state (
    id boolean NOT NULL,
    revision bigint NOT NULL,
    CONSTRAINT config_state_id_check CHECK (id)
);
ALTER TABLE public.config_state OWNER TO gobby_test;
CREATE TABLE public.config_store (
    key text NOT NULL,
    value text NOT NULL,
    source text DEFAULT 'user'::text NOT NULL,
    is_secret boolean DEFAULT false NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    revision bigint DEFAULT 0 NOT NULL
);
ALTER TABLE public.config_store OWNER TO gobby_test;
CREATE TABLE public.cron_jobs (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    name text NOT NULL,
    display_name text,
    description text,
    schedule_type text NOT NULL,
    cron_expr text,
    interval_seconds integer,
    run_at timestamp with time zone,
    timezone text DEFAULT 'UTC'::text,
    action_type text NOT NULL,
    action_config jsonb NOT NULL,
    enabled boolean DEFAULT true,
    is_system boolean DEFAULT false NOT NULL,
    next_run_at timestamp with time zone,
    last_run_at timestamp with time zone,
    last_status text,
    consecutive_failures integer DEFAULT 0,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT cron_jobs_is_system_check CHECK ((is_system = ANY (ARRAY[false, true])))
);
ALTER TABLE public.cron_jobs OWNER TO gobby_test;
CREATE TABLE public.cron_runs (
    id uuid NOT NULL,
    cron_job_id uuid NOT NULL,
    machine_id uuid NOT NULL,
    triggered_at timestamp with time zone NOT NULL,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    status text DEFAULT 'pending'::text,
    output text,
    error text,
    agent_run_id uuid,
    pipeline_execution_id uuid,
    scheduler_owner text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE public.cron_runs OWNER TO gobby_test;
CREATE TABLE public.definition_revisions (
    domain text NOT NULL,
    revision bigint DEFAULT 0 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE public.definition_revisions OWNER TO gobby_test;
CREATE TABLE public.deployment_runtime (
    deployment_token text NOT NULL,
    fencing_epoch bigint DEFAULT 0 NOT NULL,
    grant_signing_secret text NOT NULL,
    epoch_updated_at timestamp with time zone
);
ALTER TABLE public.deployment_runtime OWNER TO gobby_test;
CREATE TABLE public.destructive_batches (
    id uuid NOT NULL,
    maintenance_epoch_id uuid NOT NULL,
    campaign text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    backup_manifest_path text,
    backup_manifest_sha256 text,
    intent jsonb DEFAULT '{}'::jsonb NOT NULL,
    migration_plan jsonb DEFAULT '[]'::jsonb NOT NULL,
    target_receipts jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    verified_at timestamp with time zone,
    aborted_at timestamp with time zone,
    abort_disposition text,
    CONSTRAINT destructive_batches_backup_manifest_sha256_check CHECK (((backup_manifest_sha256 IS NULL) OR (backup_manifest_sha256 ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT destructive_batches_campaign_check CHECK ((campaign = ANY (ARRAY['account-identity-cutover'::text, 'project-checkout-cutover'::text, 'schema-apply'::text, 'purge'::text, 'reconcile'::text, 'flatten'::text]))),
    CONSTRAINT destructive_batches_check CHECK ((((status = 'verified'::text) AND (verified_at IS NOT NULL)) OR ((status <> 'verified'::text) AND (verified_at IS NULL)))),
    CONSTRAINT destructive_batches_check1 CHECK ((((status = 'aborted'::text) AND (aborted_at IS NOT NULL) AND (abort_disposition IS NOT NULL)) OR ((status <> 'aborted'::text) AND (aborted_at IS NULL) AND (abort_disposition IS NULL)))),
    CONSTRAINT destructive_batches_intent_check CHECK ((jsonb_typeof(intent) = 'object'::text)),
    CONSTRAINT destructive_batches_migration_plan_check CHECK ((jsonb_typeof(migration_plan) = 'array'::text)),
    CONSTRAINT destructive_batches_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'applied'::text, 'verified'::text, 'aborted'::text]))),
    CONSTRAINT destructive_batches_target_receipts_check CHECK ((jsonb_typeof(target_receipts) = 'object'::text))
);
ALTER TABLE public.destructive_batches OWNER TO gobby_test;
CREATE TABLE public.detection_manifests (
    provider_id text NOT NULL,
    version text NOT NULL,
    engine integer NOT NULL,
    content text NOT NULL,
    source text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT detection_manifests_engine_check CHECK ((engine > 0)),
    CONSTRAINT detection_manifests_provider_id_check CHECK ((provider_id ~ '^[a-z][a-z0-9_-]*$'::text)),
    CONSTRAINT detection_manifests_source_check CHECK ((source = ANY (ARRAY['bundled'::text, 'user'::text]))),
    CONSTRAINT detection_manifests_version_check CHECK ((version ~ '^[0-9]+(\.[0-9]+)*$'::text))
);
ALTER TABLE public.detection_manifests OWNER TO gobby_test;
CREATE TABLE public.embedding_generation_acks (
    daemon_instance_id uuid NOT NULL,
    generation text NOT NULL,
    committed_revision bigint NOT NULL,
    acknowledged boolean DEFAULT false NOT NULL,
    lease_expires_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE public.embedding_generation_acks OWNER TO gobby_test;
CREATE TABLE public.embedding_projection_changes (
    sequence bigint NOT NULL,
    source_kind text NOT NULL,
    source_id text NOT NULL,
    is_tombstone boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT embedding_projection_changes_source_kind_check CHECK ((source_kind = ANY (ARRAY['memory'::text, 'tool'::text, 'github_issue'::text])))
);
ALTER TABLE public.embedding_projection_changes OWNER TO gobby_test;
ALTER TABLE public.embedding_projection_changes ALTER COLUMN sequence ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.embedding_projection_changes_sequence_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);
CREATE TABLE public.expansion_runs (
    id uuid NOT NULL,
    parent_task_id uuid NOT NULL,
    project_id uuid NOT NULL,
    triggering_session_id uuid,
    status text DEFAULT 'pending'::text NOT NULL,
    input_source text NOT NULL,
    plan_file text,
    provider text,
    model text,
    options_json jsonb,
    compiled_spec_json jsonb,
    qa_result_json jsonb,
    task_id_map_json jsonb,
    created_task_ids_json jsonb,
    error text,
    logs_json jsonb,
    checkpoints_json jsonb,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT expansion_runs_input_source_check CHECK ((input_source = ANY (ARRAY['task'::text, 'plan'::text]))),
    CONSTRAINT expansion_runs_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'running'::text, 'compiled'::text, 'applying'::text, 'completed'::text, 'failed'::text, 'cancelled'::text])))
);
ALTER TABLE public.expansion_runs OWNER TO gobby_test;
CREATE TABLE public.external_issue_sync_status (
    project_id uuid NOT NULL,
    provider text NOT NULL,
    state text DEFAULT 'disabled'::text NOT NULL,
    last_attempt_at timestamp with time zone,
    last_success_at timestamp with time zone,
    last_outbound_success_at timestamp with time zone,
    linked_count integer DEFAULT 0 NOT NULL,
    pending_count integer DEFAULT 0 NOT NULL,
    consecutive_failures integer DEFAULT 0 NOT NULL,
    retry_at timestamp with time zone,
    last_statistics jsonb DEFAULT '{}'::jsonb NOT NULL,
    last_error text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT external_issue_sync_status_consecutive_failures_check CHECK ((consecutive_failures >= 0)),
    CONSTRAINT external_issue_sync_status_linked_count_check CHECK ((linked_count >= 0)),
    CONSTRAINT external_issue_sync_status_pending_count_check CHECK ((pending_count >= 0)),
    CONSTRAINT external_issue_sync_status_provider_check CHECK ((provider = ANY (ARRAY['linear'::text, 'github'::text]))),
    CONSTRAINT external_issue_sync_status_state_check CHECK ((state = ANY (ARRAY['disabled'::text, 'pending'::text, 'running'::text, 'healthy'::text, 'degraded'::text, 'rate_limited'::text, 'unready'::text])))
);
ALTER TABLE public.external_issue_sync_status OWNER TO gobby_test;
CREATE TABLE public.feedback_review_runs (
    id uuid NOT NULL,
    status text NOT NULL,
    dry_run boolean DEFAULT false NOT NULL,
    window_start timestamp with time zone,
    window_end timestamp with time zone,
    rows_considered integer DEFAULT 0 NOT NULL,
    findings jsonb,
    actions jsonb,
    digest_md text,
    error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    CONSTRAINT feedback_review_runs_status_vocab CHECK ((status = ANY (ARRAY['running'::text, 'completed'::text, 'failed'::text, 'interrupted'::text])))
);
ALTER TABLE public.feedback_review_runs OWNER TO gobby_test;
CREATE TABLE public.gh_issues_triaged (
    id text NOT NULL,
    project_id uuid NOT NULL,
    repo text NOT NULL,
    issue_number integer NOT NULL,
    issue_url text,
    issue_state text,
    labels_json jsonb DEFAULT '[]'::jsonb NOT NULL,
    issue_updated_at timestamp with time zone,
    content_hash text NOT NULL,
    verdict text NOT NULL,
    decision_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    task_id uuid,
    vector_point_id text,
    dedup_issue_key text,
    source text NOT NULL,
    source_text text,
    last_triaged_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT gh_issues_triaged_verdict_check CHECK ((verdict = ANY (ARRAY['implement'::text, 'skip'::text, 'escalate'::text, 'dedup'::text])))
);
ALTER TABLE public.gh_issues_triaged OWNER TO gobby_test;
CREATE TABLE public.gh_triage_build_dispatches (
    project_id uuid NOT NULL,
    repo text NOT NULL,
    issue_number integer NOT NULL,
    task_id uuid NOT NULL,
    dispatched_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE public.gh_triage_build_dispatches OWNER TO gobby_test;
CREATE TABLE public.gh_triage_deliveries (
    id text NOT NULL,
    project_id uuid NOT NULL,
    delivery_id text NOT NULL,
    event text NOT NULL,
    action text,
    repository text,
    issue_number integer,
    status text DEFAULT 'pending'::text NOT NULL,
    payload_hash text NOT NULL,
    headers_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    raw_body text DEFAULT ''::text NOT NULL,
    error text,
    attempt_count integer DEFAULT 0 NOT NULL,
    next_attempt_at timestamp with time zone,
    received_at timestamp with time zone DEFAULT now() NOT NULL,
    processed_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT gh_triage_deliveries_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'processing'::text, 'processed'::text, 'ignored'::text, 'duplicate'::text, 'error'::text])))
);
ALTER TABLE public.gh_triage_deliveries OWNER TO gobby_test;
CREATE TABLE public.hook_force_continue_budgets (
    session_id uuid NOT NULL,
    execution_num integer NOT NULL,
    count integer DEFAULT 0 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT hook_force_continue_budgets_count_nonnegative CHECK ((count >= 0))
);
ALTER TABLE public.hook_force_continue_budgets OWNER TO gobby_test;
CREATE TABLE public.hook_receipt_effects (
    receipt_id uuid NOT NULL,
    original_envelope_id text NOT NULL,
    current_envelope_id text NOT NULL,
    session_id uuid NOT NULL,
    delivery_generation integer DEFAULT 1 NOT NULL,
    state text NOT NULL,
    staged_payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    transition_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT hook_receipt_effects_delivery_generation_positive CHECK ((delivery_generation >= 1)),
    CONSTRAINT hook_receipt_effects_state_valid CHECK ((state = ANY (ARRAY['prepared'::text, 'acknowledged'::text, 'released'::text, 'terminal-undelivered'::text])))
);
ALTER TABLE public.hook_receipt_effects OWNER TO gobby_test;
CREATE TABLE public.integration_workspace_mutex (
    integration_key text NOT NULL,
    lease_until timestamp with time zone,
    lease_holder text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE public.integration_workspace_mutex OWNER TO gobby_test;
CREATE TABLE public.inter_session_messages (
    id uuid NOT NULL,
    from_session uuid NOT NULL,
    to_session uuid NOT NULL,
    content text NOT NULL,
    priority text DEFAULT 'normal'::text NOT NULL,
    sent_at timestamp with time zone NOT NULL,
    message_type text DEFAULT 'message'::text NOT NULL,
    metadata_json jsonb,
    delivered_at timestamp with time zone
);
ALTER TABLE public.inter_session_messages OWNER TO gobby_test;
CREATE TABLE public.loop_progress (
    id integer NOT NULL,
    session_id uuid NOT NULL,
    progress_type text NOT NULL,
    tool_name text,
    details text,
    recorded_at timestamp with time zone NOT NULL,
    is_high_value boolean DEFAULT false NOT NULL
);
ALTER TABLE public.loop_progress OWNER TO gobby_test;
ALTER TABLE public.loop_progress ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.loop_progress_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);
CREATE TABLE public.machines (
    id uuid NOT NULL,
    hostname text,
    os text,
    label text,
    tailscale_name text,
    owner_user_id uuid NOT NULL,
    first_seen timestamp with time zone DEFAULT now() NOT NULL,
    last_seen timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE public.machines OWNER TO gobby_test;
CREATE TABLE public.maintenance_epochs (
    id uuid NOT NULL,
    campaign text NOT NULL,
    opened_at timestamp with time zone DEFAULT now() NOT NULL,
    opened_by text NOT NULL,
    scope_note text NOT NULL,
    released_at timestamp with time zone,
    released_by_command text,
    CONSTRAINT maintenance_epochs_campaign_check CHECK ((campaign = ANY (ARRAY['account-identity-cutover'::text, 'project-checkout-cutover'::text, 'schema-apply'::text, 'purge'::text, 'reconcile'::text, 'flatten'::text]))),
    CONSTRAINT maintenance_epochs_check CHECK ((((released_at IS NULL) AND (released_by_command IS NULL)) OR ((released_at IS NOT NULL) AND (released_by_command IS NOT NULL))))
);
ALTER TABLE public.maintenance_epochs OWNER TO gobby_test;
CREATE TABLE public.mcp_server_templates (
    id uuid NOT NULL,
    name text NOT NULL,
    project_id uuid NOT NULL,
    owner text DEFAULT 'user'::text NOT NULL,
    source_path text,
    definition jsonb NOT NULL,
    definition_hash text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE public.mcp_server_templates OWNER TO gobby_test;
CREATE TABLE public.mcp_servers (
    id uuid NOT NULL,
    name text NOT NULL,
    project_id uuid NOT NULL,
    transport text NOT NULL,
    url text,
    command text,
    args jsonb,
    env jsonb,
    headers jsonb,
    enabled boolean DEFAULT true,
    description text,
    requires_oauth boolean DEFAULT false,
    oauth_provider text,
    connect_timeout double precision DEFAULT 30.0,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    template_id uuid,
    template_values jsonb,
    runtime_hook text
);
ALTER TABLE public.mcp_servers OWNER TO gobby_test;
CREATE TABLE public.memories (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    is_global boolean DEFAULT false NOT NULL,
    memory_type text DEFAULT 'fact'::text NOT NULL,
    content text NOT NULL,
    source_type text,
    source_session_id uuid,
    access_count integer DEFAULT 0,
    last_accessed_at timestamp with time zone,
    tags jsonb,
    graph_processed boolean DEFAULT true,
    graph_attempts integer DEFAULT 0 NOT NULL,
    graph_status text DEFAULT 'completed'::text NOT NULL,
    vector_needs_reindex boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone,
    dream_action text,
    last_dreamed_at timestamp with time zone,
    dream_due_version bigint DEFAULT 0 NOT NULL,
    tags_text text GENERATED ALWAYS AS (public.memories_tags_to_text(tags)) STORED,
    rationale text,
    source_task_id uuid,
    created_by_agent text,
    CONSTRAINT memories_dream_action_check CHECK (((dream_action IS NULL) OR (dream_action = ANY (ARRAY['review'::text, 'delete'::text])))),
    CONSTRAINT memories_dream_action_requires_deleted CHECK (((dream_action IS NULL) OR (deleted_at IS NOT NULL))),
    CONSTRAINT memories_graph_status_check CHECK ((graph_status = ANY (ARRAY['pending'::text, 'completed'::text, 'failed'::text]))),
    CONSTRAINT memories_memory_type_check CHECK ((memory_type = ANY (ARRAY['fact'::text, 'preference'::text, 'pattern'::text, 'context'::text]))),
    CONSTRAINT tags_is_array CHECK (((tags IS NULL) OR (jsonb_typeof(tags) = 'array'::text)))
);
ALTER TABLE public.memories OWNER TO gobby_test;
CREATE TABLE public.memory_crossrefs (
    source_id uuid NOT NULL,
    target_id uuid NOT NULL,
    similarity real NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE public.memory_crossrefs OWNER TO gobby_test;
CREATE TABLE public.memory_dream_runs (
    id uuid NOT NULL,
    project_id uuid,
    status text DEFAULT 'started'::text NOT NULL,
    dry_run boolean DEFAULT false NOT NULL,
    options jsonb DEFAULT '{}'::jsonb NOT NULL,
    plan jsonb,
    summary jsonb,
    checkpoint jsonb,
    error text,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    reverted_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT memory_dream_runs_status_check CHECK ((status = ANY (ARRAY['started'::text, 'running'::text, 'completed'::text, 'failed'::text, 'reverted'::text, 'revert_failed'::text, 'revert_forfeited'::text, 'interrupted'::text, 'partial'::text])))
);
ALTER TABLE public.memory_dream_runs OWNER TO gobby_test;
CREATE TABLE public.memory_dream_snapshots (
    id integer NOT NULL,
    run_id uuid NOT NULL,
    memory_id uuid NOT NULL,
    action text NOT NULL,
    before_data jsonb,
    after_data jsonb,
    applied boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT memory_dream_snapshots_action_check CHECK ((action = ANY (ARRAY['keep'::text, 'delete'::text, 'refresh'::text, 'review'::text, 'promote'::text])))
);
ALTER TABLE public.memory_dream_snapshots OWNER TO gobby_test;
ALTER TABLE public.memory_dream_snapshots ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.memory_dream_snapshots_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);
CREATE TABLE public.memory_dream_truth_state (
    project_id text NOT NULL,
    digest_hash text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE public.memory_dream_truth_state OWNER TO gobby_test;
CREATE TABLE public.merge_conflicts (
    id uuid NOT NULL,
    resolution_id uuid NOT NULL,
    file_path text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    ours_content text,
    theirs_content text,
    resolved_content text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE public.merge_conflicts OWNER TO gobby_test;
CREATE TABLE public.merge_resolutions (
    id uuid NOT NULL,
    worktree_id uuid NOT NULL,
    source_branch text NOT NULL,
    target_branch text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    tier_used text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE public.merge_resolutions OWNER TO gobby_test;
CREATE TABLE public.metric_snapshots (
    id integer NOT NULL,
    "timestamp" timestamp with time zone DEFAULT now() NOT NULL,
    metrics_json jsonb NOT NULL
);
ALTER TABLE public.metric_snapshots OWNER TO gobby_test;
ALTER TABLE public.metric_snapshots ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.metric_snapshots_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);
CREATE TABLE public.metrics_events (
    id integer NOT NULL,
    event_type text NOT NULL,
    project_id uuid,
    session_id uuid,
    server_name text,
    name text NOT NULL,
    success boolean DEFAULT true NOT NULL,
    latency_ms real,
    result text,
    metadata_json jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE public.metrics_events OWNER TO gobby_test;
CREATE TABLE public.metrics_events_archive (
    id integer NOT NULL,
    event_type text NOT NULL,
    project_id uuid,
    server_name text DEFAULT ''::text NOT NULL,
    name text NOT NULL,
    call_count integer DEFAULT 0 NOT NULL,
    success_count integer DEFAULT 0 NOT NULL,
    failure_count integer DEFAULT 0 NOT NULL,
    total_latency_ms real DEFAULT 0 NOT NULL,
    block_count integer DEFAULT 0 NOT NULL,
    allow_count integer DEFAULT 0 NOT NULL
);
ALTER TABLE public.metrics_events_archive OWNER TO gobby_test;
ALTER TABLE public.metrics_events_archive ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.metrics_events_archive_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);
ALTER TABLE public.metrics_events ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.metrics_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);
CREATE TABLE public.model_metadata (
    model text NOT NULL,
    context_length integer,
    max_completion_tokens integer,
    source text DEFAULT 'registry'::text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    reasoning_present boolean,
    reasoning_supported_efforts jsonb,
    reasoning_default_effort text,
    reasoning_default_enabled boolean,
    reasoning_mandatory boolean
);
ALTER TABLE public.model_metadata OWNER TO gobby_test;
CREATE TABLE public.pending_interactions (
    id uuid NOT NULL,
    session_id uuid NOT NULL,
    kind text NOT NULL,
    provider text NOT NULL,
    tool_name text,
    payload_json jsonb NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    decision text,
    response_json jsonb,
    timeout_seconds integer DEFAULT 300 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    resolved_at timestamp with time zone
);
ALTER TABLE public.pending_interactions OWNER TO gobby_test;
CREATE TABLE public.pipeline_definitions (
    id uuid NOT NULL,
    project_id uuid,
    name text NOT NULL,
    description text,
    enabled boolean DEFAULT true NOT NULL,
    enabled_pinned boolean DEFAULT false NOT NULL,
    version text DEFAULT '1.0'::text NOT NULL,
    definition_json jsonb NOT NULL,
    canvas_json jsonb,
    source text DEFAULT 'installed'::text NOT NULL,
    tags jsonb,
    deleted_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT pipeline_definitions_source_check CHECK ((source = ANY (ARRAY['installed'::text, 'custom'::text, 'project'::text])))
);
ALTER TABLE public.pipeline_definitions OWNER TO gobby_test;
CREATE TABLE public.pipeline_executions (
    id uuid NOT NULL,
    pipeline_name text NOT NULL,
    project_id uuid NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    inputs_json jsonb,
    outputs_json jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    resume_token text,
    session_id uuid,
    parent_execution_id uuid,
    continuation_prompt text,
    definition_json jsonb,
    review_json jsonb
);
ALTER TABLE public.pipeline_executions OWNER TO gobby_test;
CREATE TABLE public.plan_review_evidence (
    evidence_id uuid NOT NULL,
    project_id uuid NOT NULL,
    plan_path text NOT NULL,
    plan_hash text NOT NULL,
    section_manifest jsonb NOT NULL,
    snapshot bytea NOT NULL,
    round_number integer NOT NULL,
    session_id uuid,
    task_id uuid,
    stage text,
    dispatch_run_id uuid,
    lease_expires_at timestamp with time zone,
    finalized_at timestamp with time zone,
    expired_at timestamp with time zone,
    round_result jsonb,
    approval_result jsonb,
    approved_at timestamp with time zone,
    lesson_mint_status text,
    lesson_mint_detail jsonb,
    manifest_digest text,
    manifest_payload jsonb,
    manifest_state text,
    manifest_result jsonb,
    manifest_applied_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT plan_review_evidence_attempt_binding CHECK ((((session_id IS NOT NULL) AND (task_id IS NULL) AND (stage IS NULL)) OR ((session_id IS NULL) AND (task_id IS NOT NULL) AND (stage IS NOT NULL)))),
    CONSTRAINT plan_review_evidence_bound_lease_cleared CHECK (((dispatch_run_id IS NULL) OR (lease_expires_at IS NULL))),
    CONSTRAINT plan_review_evidence_lifecycle_exclusive CHECK ((NOT ((finalized_at IS NOT NULL) AND (expired_at IS NOT NULL)))),
    CONSTRAINT plan_review_evidence_manifest_array CHECK ((jsonb_typeof(section_manifest) = 'array'::text)),
    CONSTRAINT plan_review_evidence_manifest_state CHECK (((manifest_state IS NULL) OR (manifest_state = ANY (ARRAY['pending'::text, 'applied'::text, 'revoked'::text])))),
    CONSTRAINT plan_review_evidence_mint_status CHECK (((lesson_mint_status IS NULL) OR (lesson_mint_status = ANY (ARRAY['pending'::text, 'minted'::text, 'failed'::text, 'none'::text])))),
    CONSTRAINT plan_review_evidence_round_positive CHECK ((round_number > 0))
);
ALTER TABLE public.plan_review_evidence OWNER TO gobby_test;
CREATE TABLE public.plans (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    plan_id text NOT NULL,
    plan_path text NOT NULL,
    plan_hash text,
    plan_kind text NOT NULL,
    state text NOT NULL,
    root_task_ref text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    archived_at timestamp with time zone,
    CONSTRAINT plans_plan_kind_check CHECK ((plan_kind = ANY (ARRAY['implementation'::text, 'strategy'::text]))),
    CONSTRAINT plans_state_check CHECK ((state = ANY (ARRAY['active'::text, 'archived'::text])))
);
ALTER TABLE public.plans OWNER TO gobby_test;
CREATE TABLE public.project_checkouts (
    machine_id uuid NOT NULL,
    project_id uuid NOT NULL,
    root_path text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE ONLY public.project_checkouts FORCE ROW LEVEL SECURITY;
ALTER TABLE public.project_checkouts OWNER TO gobby_test;
CREATE TABLE public.project_github_triage_configs (
    project_id uuid NOT NULL,
    sync_enabled boolean DEFAULT false NOT NULL,
    triage_enabled boolean DEFAULT false NOT NULL,
    webhook_enabled boolean DEFAULT false NOT NULL,
    repositories_json jsonb DEFAULT '[]'::jsonb NOT NULL,
    reconcile_interval_seconds integer DEFAULT 3600 CONSTRAINT project_github_triage_confi_reconcile_interval_seconds_not_null NOT NULL,
    webhook_secret_ref text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT project_github_triage_configs_reconcile_interval_seconds_check CHECK ((reconcile_interval_seconds > 0)),
    CONSTRAINT project_github_triage_configs_sync_enabled_check CHECK ((sync_enabled = ANY (ARRAY[false, true]))),
    CONSTRAINT project_github_triage_configs_triage_enabled_check CHECK ((triage_enabled = ANY (ARRAY[false, true]))),
    CONSTRAINT project_github_triage_configs_webhook_enabled_check CHECK ((webhook_enabled = ANY (ARRAY[false, true])))
);
ALTER TABLE public.project_github_triage_configs OWNER TO gobby_test;
CREATE TABLE public.project_lifecycle_events (
    id integer NOT NULL,
    project_id uuid NOT NULL,
    event text NOT NULL,
    reason text NOT NULL,
    by_actor text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE public.project_lifecycle_events OWNER TO gobby_test;
ALTER TABLE public.project_lifecycle_events ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.project_lifecycle_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);
CREATE TABLE public.projects (
    id uuid NOT NULL,
    name text NOT NULL,
    github_url text,
    github_repo text,
    linear_team_id text,
    linear_project_id text,
    linear_synced_at timestamp with time zone,
    linear_sync_enabled boolean DEFAULT false NOT NULL,
    deleted_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT projects_linear_sync_enabled_check CHECK ((linear_sync_enabled = ANY (ARRAY[false, true])))
);
ALTER TABLE ONLY public.projects FORCE ROW LEVEL SECURITY;
ALTER TABLE public.projects OWNER TO gobby_test;
CREATE TABLE public.prompts (
    id uuid NOT NULL,
    name text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    content text NOT NULL,
    version text DEFAULT '1.0'::text,
    variables jsonb,
    scope text DEFAULT 'bundled'::text NOT NULL,
    source_path text,
    project_id uuid,
    enabled boolean DEFAULT true,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT prompts_scope_check CHECK ((scope = ANY (ARRAY['bundled'::text, 'global'::text, 'project'::text])))
);
ALTER TABLE public.prompts OWNER TO gobby_test;
CREATE TABLE public.provider_capability_refresh_state (
    provider text NOT NULL,
    source_key text NOT NULL,
    source_url text,
    required boolean DEFAULT true NOT NULL,
    generation bigint DEFAULT 0 NOT NULL,
    state text DEFAULT 'pending'::text NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    last_attempt_at timestamp with time zone,
    last_success_at timestamp with time zone,
    last_error text
);
ALTER TABLE public.provider_capability_refresh_state OWNER TO gobby_test;
CREATE TABLE public.provider_capacity_snapshots (
    machine_id uuid NOT NULL,
    provider text NOT NULL,
    state text NOT NULL,
    observed_at timestamp with time zone NOT NULL,
    windows jsonb NOT NULL,
    reason text,
    source_version text NOT NULL,
    CONSTRAINT provider_capacity_snapshots_provider_nonempty CHECK ((btrim(provider) <> ''::text)),
    CONSTRAINT provider_capacity_snapshots_state_valid CHECK ((state = ANY (ARRAY['available'::text, 'exhausted'::text]))),
    CONSTRAINT provider_capacity_snapshots_windows_array CHECK ((jsonb_typeof(windows) = 'array'::text))
);
ALTER TABLE public.provider_capacity_snapshots OWNER TO gobby_test;
CREATE TABLE public.provider_model_capabilities (
    provider text NOT NULL,
    canonical_model text NOT NULL,
    display_name text NOT NULL,
    aliases jsonb DEFAULT '[]'::jsonb NOT NULL,
    available boolean DEFAULT true NOT NULL,
    hidden boolean DEFAULT false NOT NULL,
    is_default boolean DEFAULT false NOT NULL,
    context_length integer,
    max_output_tokens integer,
    reasoning text DEFAULT 'unknown'::text NOT NULL,
    supported_efforts jsonb,
    default_effort text,
    latency_class text,
    input_modalities jsonb,
    supports_tools boolean,
    generation bigint NOT NULL,
    provenance jsonb NOT NULL
);
ALTER TABLE public.provider_model_capabilities OWNER TO gobby_test;
CREATE TABLE public.provider_model_routes (
    provider text NOT NULL,
    canonical_model text NOT NULL,
    speed_mode text NOT NULL,
    selector text NOT NULL,
    available boolean DEFAULT true NOT NULL,
    usage_multiplier numeric,
    throughput_multiplier numeric,
    latency_class text,
    activations jsonb DEFAULT '[]'::jsonb NOT NULL,
    generation bigint NOT NULL,
    provenance jsonb NOT NULL
);
ALTER TABLE public.provider_model_routes OWNER TO gobby_test;
CREATE TABLE public.recall_gate_runs (
    holdout_consumption_key text NOT NULL,
    status text NOT NULL,
    fit_settings_digest text NOT NULL,
    claim_token text NOT NULL,
    lease_expires_at timestamp with time zone,
    attempts integer DEFAULT 0 NOT NULL,
    last_error text,
    ship boolean,
    decision jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT recall_gate_runs_attempts_check CHECK ((attempts >= 0)),
    CONSTRAINT recall_gate_runs_status_check CHECK ((status = ANY (ARRAY['reserved'::text, 'complete'::text])))
);
ALTER TABLE public.recall_gate_runs OWNER TO gobby_test;
CREATE TABLE public.recall_holdout_consumed (
    id bigint NOT NULL,
    request_id text NOT NULL,
    holdout_consumption_key text NOT NULL,
    consumed_at timestamp with time zone NOT NULL
);
ALTER TABLE public.recall_holdout_consumed OWNER TO gobby_test;
CREATE SEQUENCE public.recall_holdout_consumed_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.recall_holdout_consumed_id_seq OWNER TO gobby_test;
ALTER SEQUENCE public.recall_holdout_consumed_id_seq OWNED BY public.recall_holdout_consumed.id;
CREATE TABLE public.recall_injection_outcomes (
    session_id text NOT NULL,
    recall_request_id text NOT NULL,
    memory_id text NOT NULL,
    project_id text,
    outcome text NOT NULL,
    drop_reason text,
    drop_detail text,
    injection_position integer,
    injection_group text,
    turn_seq integer,
    caller text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT recall_injection_outcomes_check CHECK (((outcome = 'injected'::text) = (drop_reason IS NULL))),
    CONSTRAINT recall_injection_outcomes_check1 CHECK (((outcome = 'filtered'::text) = (injection_position IS NULL))),
    CONSTRAINT recall_injection_outcomes_drop_reason_check CHECK (((drop_reason IS NULL) OR (drop_reason = ANY (ARRAY['already_injected'::text, 'review_lesson'::text, 'empty_content'::text, 'payload_empty'::text, 'budget'::text, 'other'::text])))),
    CONSTRAINT recall_injection_outcomes_outcome_check CHECK ((outcome = ANY (ARRAY['injected'::text, 'filtered'::text])))
);
ALTER TABLE public.recall_injection_outcomes OWNER TO gobby_test;
CREATE TABLE public.recall_shadow_audit_verdicts (
    id bigint NOT NULL,
    cohort_digest text NOT NULL,
    sample_digest text NOT NULL,
    request_id text NOT NULL,
    memory_id text NOT NULL,
    prompt_hash text NOT NULL,
    human_verdict boolean NOT NULL,
    reviewer text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE public.recall_shadow_audit_verdicts OWNER TO gobby_test;
CREATE SEQUENCE public.recall_shadow_audit_verdicts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.recall_shadow_audit_verdicts_id_seq OWNER TO gobby_test;
ALTER SEQUENCE public.recall_shadow_audit_verdicts_id_seq OWNED BY public.recall_shadow_audit_verdicts.id;
CREATE TABLE public.recall_shadow_judge_state (
    recall_request_id text NOT NULL,
    label_source text NOT NULL,
    judge_protocol_version text NOT NULL,
    status text NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    next_attempt_at timestamp with time zone,
    lease_expires_at timestamp with time zone,
    claim_token text,
    last_error text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT recall_shadow_judge_state_attempts_check CHECK ((attempts >= 0)),
    CONSTRAINT recall_shadow_judge_state_status_check CHECK ((status = ANY (ARRAY['claimed'::text, 'retryable'::text, 'terminal'::text, 'complete'::text])))
);
ALTER TABLE public.recall_shadow_judge_state OWNER TO gobby_test;
CREATE TABLE public.recall_shadow_prompt_snapshot (
    recall_request_id text NOT NULL,
    label_source text NOT NULL,
    judge_protocol_version text NOT NULL,
    system_prompt text NOT NULL,
    query_text text NOT NULL,
    presented jsonb NOT NULL,
    prompt_hash text NOT NULL,
    judge_model text NOT NULL,
    judge_config_fingerprint text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE public.recall_shadow_prompt_snapshot OWNER TO gobby_test;
CREATE TABLE public.recall_signal_hits (
    session_id text NOT NULL,
    recall_request_id text NOT NULL,
    memory_id text NOT NULL,
    project_id text,
    rank integer NOT NULL,
    search_via text,
    similarity double precision,
    raw_semantic_score double precision,
    temporal_decay_factor double precision,
    ranking_score double precision,
    ranking_mode text,
    graph_score double precision,
    edge_cosine double precision,
    edge_support_norm double precision,
    edge_weight_blend double precision,
    edge_decay_factor double precision,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    content_hash text
);
ALTER TABLE public.recall_signal_hits OWNER TO gobby_test;
CREATE TABLE public.recall_signal_requests (
    session_id text NOT NULL,
    recall_request_id text NOT NULL,
    project_id text,
    caller text NOT NULL,
    query text,
    merged_ids jsonb DEFAULT '[]'::jsonb NOT NULL,
    returned_ids jsonb DEFAULT '[]'::jsonb NOT NULL,
    rrf_applied boolean DEFAULT false NOT NULL,
    graph_synthetic_similarity_discount double precision,
    ranking_score_map jsonb DEFAULT '{}'::jsonb NOT NULL,
    graph_score_map jsonb DEFAULT '{}'::jsonb NOT NULL,
    weighting jsonb DEFAULT '{}'::jsonb NOT NULL,
    schema_version integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    constants_provenance text
);
ALTER TABLE public.recall_signal_requests OWNER TO gobby_test;
CREATE TABLE public.recall_usefulness (
    id bigint NOT NULL,
    project_id text,
    session_id text NOT NULL,
    recall_request_id text NOT NULL,
    memory_id text NOT NULL,
    label_source text NOT NULL,
    judge_useful boolean NOT NULL,
    judge_confidence real,
    judge_model text,
    judge_protocol_version text NOT NULL,
    position_randomized boolean NOT NULL,
    length_controlled boolean NOT NULL,
    ablation_delta real,
    ablation_method text,
    rationale text,
    feature_extractor_version text,
    labeled_at timestamp with time zone NOT NULL,
    CONSTRAINT recall_usefulness_label_source_check CHECK ((label_source = ANY (ARRAY['llm_judge'::text, 'ablation'::text, 'digest'::text, 'digest_shadow'::text, 'human'::text])))
);
ALTER TABLE public.recall_usefulness OWNER TO gobby_test;
CREATE SEQUENCE public.recall_usefulness_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.recall_usefulness_id_seq OWNER TO gobby_test;
ALTER SEQUENCE public.recall_usefulness_id_seq OWNED BY public.recall_usefulness.id;
CREATE TABLE public.rule_definitions (
    id uuid NOT NULL,
    project_id uuid,
    name text NOT NULL,
    description text,
    enabled boolean DEFAULT true NOT NULL,
    enabled_pinned boolean DEFAULT false NOT NULL,
    priority integer DEFAULT 100 NOT NULL,
    sources jsonb,
    definition_json jsonb NOT NULL,
    source text DEFAULT 'installed'::text NOT NULL,
    tags jsonb,
    deleted_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT rule_definitions_source_check CHECK ((source = ANY (ARRAY['installed'::text, 'custom'::text, 'project'::text])))
);
ALTER TABLE public.rule_definitions OWNER TO gobby_test;
CREATE TABLE public.schema_migrations (
    version integer NOT NULL,
    applied_at timestamp with time zone DEFAULT now() NOT NULL,
    filename text,
    checksum text
);
ALTER TABLE public.schema_migrations OWNER TO gobby_test;
CREATE TABLE public.secret_key_material (
    id text NOT NULL,
    wrapped_dek text NOT NULL,
    kek_posture text NOT NULL,
    kek_salt text,
    kek_kdf_n integer,
    kek_kdf_r integer,
    kek_kdf_p integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE public.secret_key_material OWNER TO gobby_test;
CREATE TABLE public.secrets (
    id uuid NOT NULL,
    name text NOT NULL,
    encrypted_value text NOT NULL,
    category text DEFAULT 'general'::text,
    description text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    project_id uuid DEFAULT '00000000-0000-0000-0000-000000000002'::uuid NOT NULL
);
ALTER TABLE public.secrets OWNER TO gobby_test;
CREATE TABLE public.session_feedback (
    id uuid NOT NULL,
    session_id uuid NOT NULL,
    source text NOT NULL,
    kind text NOT NULL,
    evidence text NOT NULL,
    impact text NOT NULL,
    frequency text NOT NULL,
    suggestion text,
    disposition text,
    reviewed boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    kind_other_label text,
    review_run_id uuid,
    CONSTRAINT session_feedback_disposition_nonblank CHECK (((disposition IS NULL) OR (btrim(disposition) <> ''::text))),
    CONSTRAINT session_feedback_disposition_vocab CHECK (((disposition IS NULL) OR (disposition = ANY (ARRAY['worked-around'::text, 'filed-task'::text, 'fixed'::text, 'escalated'::text, 'noted'::text])))),
    CONSTRAINT session_feedback_evidence_nonblank CHECK ((btrim(evidence) <> ''::text)),
    CONSTRAINT session_feedback_frequency_nonblank CHECK ((btrim(frequency) <> ''::text)),
    CONSTRAINT session_feedback_frequency_vocab CHECK ((frequency = ANY (ARRAY['once'::text, 'repeated'::text, 'always'::text]))),
    CONSTRAINT session_feedback_impact_nonblank CHECK ((btrim(impact) <> ''::text)),
    CONSTRAINT session_feedback_kind_nonblank CHECK ((btrim(kind) <> ''::text)),
    CONSTRAINT session_feedback_kind_other_label_nonblank CHECK (((kind_other_label IS NULL) OR (btrim(kind_other_label) <> ''::text))),
    CONSTRAINT session_feedback_kind_other_label_pairing CHECK (((kind = 'other'::text) = (kind_other_label IS NOT NULL))),
    CONSTRAINT session_feedback_kind_vocab CHECK ((kind = ANY (ARRAY['friction'::text, 'bug'::text, 'noise'::text, 'surprise'::text, 'missing-affordance'::text, 'useful'::text, 'other'::text]))),
    CONSTRAINT session_feedback_source_nonblank CHECK ((btrim(source) <> ''::text)),
    CONSTRAINT session_feedback_suggestion_nonblank CHECK (((suggestion IS NULL) OR (btrim(suggestion) <> ''::text)))
);
ALTER TABLE public.session_feedback OWNER TO gobby_test;
CREATE TABLE public.session_skills (
    id integer NOT NULL,
    session_id uuid NOT NULL,
    skill_name text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE public.session_skills OWNER TO gobby_test;
ALTER TABLE public.session_skills ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.session_skills_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);
CREATE TABLE public.session_stop_signals (
    session_id uuid NOT NULL,
    source text NOT NULL,
    reason text,
    requested_at timestamp with time zone NOT NULL,
    acknowledged_at timestamp with time zone
);
ALTER TABLE public.session_stop_signals OWNER TO gobby_test;
CREATE TABLE public.session_summary_revisions (
    id uuid NOT NULL,
    session_id uuid NOT NULL,
    summary_markdown text NOT NULL,
    generation_mode text NOT NULL,
    source_context_hash text,
    previous_revision_id uuid,
    metadata_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT session_summary_revisions_generation_mode_valid CHECK ((generation_mode = ANY (ARRAY['agent_authored'::text, 'full'::text, 'noop'::text])))
);
ALTER TABLE public.session_summary_revisions OWNER TO gobby_test;
CREATE TABLE public.session_tasks (
    id integer NOT NULL,
    session_id uuid NOT NULL,
    task_id uuid NOT NULL,
    action text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE public.session_tasks OWNER TO gobby_test;
ALTER TABLE public.session_tasks ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.session_tasks_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);
CREATE TABLE public.session_variable_defaults (
    id uuid NOT NULL,
    project_id uuid,
    name text NOT NULL,
    description text,
    enabled boolean DEFAULT true NOT NULL,
    enabled_pinned boolean DEFAULT false NOT NULL,
    default_value jsonb,
    source text DEFAULT 'installed'::text NOT NULL,
    tags jsonb,
    deleted_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT session_variable_defaults_source_check CHECK ((source = ANY (ARRAY['installed'::text, 'custom'::text, 'project'::text])))
);
ALTER TABLE public.session_variable_defaults OWNER TO gobby_test;
CREATE TABLE public.session_variables (
    session_id uuid NOT NULL,
    variables jsonb DEFAULT '{}'::jsonb,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE public.session_variables OWNER TO gobby_test;
CREATE TABLE public.sessions (
    id uuid NOT NULL,
    external_id text NOT NULL,
    machine_id uuid NOT NULL,
    source text NOT NULL,
    project_id uuid NOT NULL,
    title text,
    title_source text,
    status text DEFAULT 'active'::text,
    transcript_path text,
    summary_path text,
    summary_markdown text,
    handoff_markdown text,
    summary_revision_id uuid,
    summary_source_context_hash text,
    summary_generation_mode text,
    summary_generated_at timestamp with time zone,
    git_branch text,
    parent_session_id uuid,
    transcript_processed boolean DEFAULT false,
    agent_depth integer DEFAULT 0,
    spawned_by_agent_id text,
    workflow_name text,
    agent_run_id uuid,
    context_injected boolean DEFAULT false,
    original_prompt text,
    usage_input_tokens integer DEFAULT 0,
    usage_output_tokens integer DEFAULT 0,
    usage_cache_creation_tokens integer DEFAULT 0,
    usage_cache_read_tokens integer DEFAULT 0,
    context_window integer,
    context_used_tokens integer,
    context_usage_ratio double precision,
    context_usage_source text,
    context_usage_confidence text,
    context_usage_updated_at timestamp with time zone,
    last_prompt_input_tokens integer,
    last_prompt_uncached_input_tokens integer,
    last_prompt_cache_read_tokens integer,
    last_prompt_cache_creation_tokens integer,
    last_completion_output_tokens integer,
    terminal_context jsonb,
    seq_num integer,
    model text,
    is_local boolean DEFAULT false NOT NULL,
    had_edits boolean DEFAULT false,
    chat_mode text DEFAULT 'plan'::text,
    message_count integer DEFAULT 0,
    turn_count integer DEFAULT 0,
    tool_call_count integer DEFAULT 0,
    last_assistant_content text,
    approved_tools_json jsonb,
    session_type text DEFAULT 'terminal'::text NOT NULL,
    sandbox_enabled boolean DEFAULT false,
    sandbox_policy_hash text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    last_activity timestamp with time zone DEFAULT now() NOT NULL,
    workspace_path text,
    workspace_generation integer DEFAULT 0 NOT NULL,
    startup_claim_generation integer DEFAULT 0 NOT NULL,
    startup_claim_owner text,
    startup_claim_state text DEFAULT 'idle'::text NOT NULL,
    CONSTRAINT sessions_context_usage_confidence_valid CHECK (((context_usage_confidence IS NULL) OR (context_usage_confidence = ANY (ARRAY['reported'::text, 'estimated'::text, 'unknown'::text])))),
    CONSTRAINT sessions_context_usage_ratio_range CHECK (((context_usage_ratio IS NULL) OR ((context_usage_ratio >= (0)::double precision) AND (context_usage_ratio <= (1)::double precision)))),
    CONSTRAINT sessions_context_usage_tokens_nonnegative CHECK ((((usage_input_tokens IS NULL) OR (usage_input_tokens >= 0)) AND ((usage_output_tokens IS NULL) OR (usage_output_tokens >= 0)) AND ((usage_cache_creation_tokens IS NULL) OR (usage_cache_creation_tokens >= 0)) AND ((usage_cache_read_tokens IS NULL) OR (usage_cache_read_tokens >= 0)) AND ((context_window IS NULL) OR (context_window >= 0)) AND ((context_used_tokens IS NULL) OR (context_used_tokens >= 0)) AND ((last_prompt_input_tokens IS NULL) OR (last_prompt_input_tokens >= 0)) AND ((last_prompt_uncached_input_tokens IS NULL) OR (last_prompt_uncached_input_tokens >= 0)) AND ((last_prompt_cache_read_tokens IS NULL) OR (last_prompt_cache_read_tokens >= 0)) AND ((last_prompt_cache_creation_tokens IS NULL) OR (last_prompt_cache_creation_tokens >= 0)) AND ((last_completion_output_tokens IS NULL) OR (last_completion_output_tokens >= 0)))),
    CONSTRAINT sessions_parent_session_not_self CHECK (((parent_session_id IS NULL) OR (parent_session_id <> id))),
    CONSTRAINT sessions_startup_claim_generation_nonnegative CHECK ((startup_claim_generation >= 0)),
    CONSTRAINT sessions_startup_claim_state_valid CHECK ((startup_claim_state = ANY (ARRAY['idle'::text, 'claimed'::text, 'committed'::text, 'invalidated'::text]))),
    CONSTRAINT sessions_workspace_generation_nonnegative CHECK ((workspace_generation >= 0))
);
ALTER TABLE public.sessions OWNER TO gobby_test;
CREATE TABLE public.skill_files (
    id uuid NOT NULL,
    skill_id uuid NOT NULL,
    path text NOT NULL,
    file_type text NOT NULL,
    content text NOT NULL,
    content_hash text NOT NULL,
    size_bytes integer DEFAULT 0 NOT NULL,
    deleted_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE public.skill_files OWNER TO gobby_test;
CREATE TABLE public.skills (
    id uuid NOT NULL,
    name text NOT NULL,
    description text NOT NULL,
    content text NOT NULL,
    version text,
    license text,
    compatibility text,
    allowed_tools jsonb,
    metadata jsonb,
    source_path text,
    source_type text,
    source_ref text,
    hub_name text,
    hub_slug text,
    hub_version text,
    enabled boolean DEFAULT true,
    always_apply boolean DEFAULT false,
    injection_format text DEFAULT 'summary'::text,
    project_id uuid,
    source text DEFAULT 'installed'::text,
    deleted_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE public.skills OWNER TO gobby_test;
CREATE TABLE public.spans (
    span_id text NOT NULL,
    trace_id text NOT NULL,
    parent_span_id text,
    name text NOT NULL,
    kind text,
    start_time_ns bigint NOT NULL,
    end_time_ns bigint,
    status text,
    status_message text,
    attributes_json jsonb,
    events_json jsonb,
    created_at timestamp with time zone DEFAULT now()
);
ALTER TABLE public.spans OWNER TO gobby_test;
CREATE TABLE public.step_executions (
    id integer NOT NULL,
    execution_id uuid NOT NULL,
    step_id text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    input_json jsonb,
    output_json jsonb,
    error text,
    approval_token text,
    approved_by text,
    approved_at timestamp with time zone,
    approval_timeout_seconds integer
);
ALTER TABLE public.step_executions OWNER TO gobby_test;
ALTER TABLE public.step_executions ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.step_executions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);
CREATE TABLE public.task_affected_files (
    id integer NOT NULL,
    task_id uuid NOT NULL,
    file_path text NOT NULL,
    annotation_source text DEFAULT 'expansion'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE public.task_affected_files OWNER TO gobby_test;
ALTER TABLE public.task_affected_files ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.task_affected_files_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);
CREATE TABLE public.task_artifacts (
    task_id uuid NOT NULL,
    plan_file_path text,
    plan_file_hash text,
    worktree_path text,
    worktree_id uuid,
    clone_path text,
    clone_id uuid,
    base_commit_sha text,
    target_branch text,
    integration_branch text,
    integration_workspace_id uuid,
    integration_clone_id uuid,
    expansion_run_id uuid,
    expansion_attempts integer DEFAULT 0 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    plan_enhancement_rounds integer DEFAULT 0 NOT NULL,
    plan_enhancement_rounds_completed integer DEFAULT 0 NOT NULL,
    plan_enhancement_converged boolean DEFAULT false NOT NULL,
    CONSTRAINT task_artifacts_check CHECK ((((worktree_path IS NULL) = (worktree_id IS NULL)) AND ((clone_path IS NULL) = (clone_id IS NULL)) AND ((worktree_path IS NULL) OR (clone_path IS NULL)) AND ((integration_workspace_id IS NULL) OR (integration_clone_id IS NULL)) AND ((integration_workspace_id IS NULL) OR (integration_branch IS NOT NULL)) AND ((integration_clone_id IS NULL) OR (integration_branch IS NOT NULL)) AND ((base_commit_sha IS NULL) OR (worktree_path IS NOT NULL) OR (clone_path IS NOT NULL)))),
    CONSTRAINT task_artifacts_plan_enhancement_rounds_completed_nonnegative CHECK ((plan_enhancement_rounds_completed >= 0)),
    CONSTRAINT task_artifacts_plan_enhancement_rounds_nonnegative CHECK ((plan_enhancement_rounds >= 0))
);
ALTER TABLE public.task_artifacts OWNER TO gobby_test;
CREATE TABLE public.task_close_reviews (
    id uuid NOT NULL,
    task_id uuid NOT NULL,
    task_ref text NOT NULL,
    caller_session_id uuid NOT NULL,
    agent_run_id uuid,
    close_arguments jsonb NOT NULL,
    review_fingerprint text NOT NULL,
    evidence_fingerprint text NOT NULL,
    status text DEFAULT 'launching'::text NOT NULL,
    result_payload jsonb,
    error text,
    launched_at timestamp with time zone,
    completed_at timestamp with time zone,
    delivered_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT task_close_reviews_close_arguments_check CHECK ((jsonb_typeof(close_arguments) = 'object'::text)),
    CONSTRAINT task_close_reviews_result_payload_check CHECK (((result_payload IS NULL) OR (jsonb_typeof(result_payload) = 'object'::text))),
    CONSTRAINT task_close_reviews_status_check CHECK ((status = ANY (ARRAY['launching'::text, 'running'::text, 'finalizing'::text, 'closed'::text, 'invalid'::text, 'stale'::text, 'error'::text])))
);
ALTER TABLE public.task_close_reviews OWNER TO gobby_test;
CREATE TABLE public.task_comments (
    id uuid NOT NULL,
    task_id uuid NOT NULL,
    parent_comment_id uuid,
    author text NOT NULL,
    author_type text DEFAULT 'session'::text NOT NULL,
    body text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE public.task_comments OWNER TO gobby_test;
CREATE TABLE public.task_delivery_campaigns (
    task_id uuid NOT NULL,
    state text DEFAULT 'pending'::text NOT NULL,
    delivery_mode text DEFAULT 'auto'::text NOT NULL,
    source_repo text,
    target_repo text,
    merge_strategy text DEFAULT 'squash'::text NOT NULL,
    structured_pr_verdict jsonb,
    pr_report_ref text,
    merge_sha text,
    merge_report_ref text,
    last_error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT task_delivery_campaigns_delivery_mode_check CHECK ((delivery_mode = ANY (ARRAY['auto'::text, 'pull_request'::text]))),
    CONSTRAINT task_delivery_campaigns_merge_strategy_check CHECK ((merge_strategy = ANY (ARRAY['merge'::text, 'squash'::text, 'rebase'::text])))
);
ALTER TABLE public.task_delivery_campaigns OWNER TO gobby_test;
CREATE TABLE public.task_delivery_units (
    id uuid NOT NULL,
    task_id uuid NOT NULL,
    unit_key text NOT NULL,
    worktree_id uuid,
    repo text,
    source_branch text,
    target_branch text DEFAULT 'main'::text NOT NULL,
    pr_required boolean,
    protection_json jsonb,
    pr_url text,
    github_pr_number integer,
    gate_snapshot_json jsonb,
    pr_state text,
    local_update_attempts integer DEFAULT 0 NOT NULL,
    last_error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT task_delivery_units_pr_required_check CHECK ((pr_required = ANY (ARRAY[false, true])))
);
ALTER TABLE public.task_delivery_units OWNER TO gobby_test;
CREATE TABLE public.task_dependencies (
    id integer NOT NULL,
    task_id uuid NOT NULL,
    depends_on uuid NOT NULL,
    dep_type text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE public.task_dependencies OWNER TO gobby_test;
ALTER TABLE public.task_dependencies ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.task_dependencies_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);
CREATE TABLE public.task_dispatch_mutex (
    task_id uuid NOT NULL,
    lease_until timestamp with time zone,
    lease_holder text,
    run_id uuid,
    action_kind text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE public.task_dispatch_mutex OWNER TO gobby_test;
CREATE TABLE public.task_lifecycle_events (
    id integer NOT NULL,
    task_id uuid NOT NULL,
    from_state text,
    to_state text NOT NULL,
    reason text NOT NULL,
    failure_category text,
    by_actor text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT task_lifecycle_events_failure_category_check CHECK ((failure_category = ANY (ARRAY['environment'::text, 'dependency'::text, 'code'::text, 'test'::text, 'provider'::text, 'timeout'::text])))
);
ALTER TABLE public.task_lifecycle_events OWNER TO gobby_test;
ALTER TABLE public.task_lifecycle_events ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.task_lifecycle_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);
CREATE TABLE public.task_selection_history (
    id integer NOT NULL,
    session_id uuid NOT NULL,
    task_id uuid NOT NULL,
    selected_at timestamp with time zone NOT NULL,
    context jsonb
);
ALTER TABLE public.task_selection_history OWNER TO gobby_test;
ALTER TABLE public.task_selection_history ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.task_selection_history_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);
CREATE TABLE public.task_stage_states (
    task_id uuid NOT NULL,
    stage_name text NOT NULL,
    "position" integer NOT NULL,
    state text DEFAULT 'ready'::text NOT NULL,
    review_policy text DEFAULT 'none'::text NOT NULL,
    reviewer_agent text,
    entered_at timestamp with time zone,
    entered_by_session_id uuid,
    entered_by_actor text,
    completed_at timestamp with time zone,
    completed_by_session_id uuid,
    completed_by_actor text,
    completed_commit_sha text,
    work_attempt_count integer DEFAULT 0 NOT NULL,
    review_round_count integer DEFAULT 0 NOT NULL,
    retry_neutral_failure_count integer DEFAULT 0 NOT NULL,
    max_work_attempts integer,
    max_review_rounds integer,
    artifact_refs jsonb,
    notes text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT task_stage_states_review_policy_check CHECK ((review_policy = ANY (ARRAY['none'::text, 'required'::text, 'optional'::text]))),
    CONSTRAINT task_stage_states_state_check CHECK (((state = ANY (ARRAY['ready'::text, 'in_progress'::text, 'done'::text])) OR (state = ANY (ARRAY['needs_review'::text, 'review_approved'::text]))))
);
ALTER TABLE public.task_stage_states OWNER TO gobby_test;
CREATE TABLE public.task_stages_registry (
    name text NOT NULL,
    display_label text NOT NULL,
    description text NOT NULL,
    category text NOT NULL,
    default_agent text,
    reviewer_agent text,
    reviewer_agent_selector_json jsonb,
    review_policy text DEFAULT 'none'::text NOT NULL,
    dispatch_type text,
    dispatch_target text,
    dispatch_inputs_json jsonb,
    position_hint integer NOT NULL,
    requires_human boolean DEFAULT false NOT NULL,
    is_terminal boolean DEFAULT false NOT NULL,
    default_max_work_attempts integer DEFAULT 3 NOT NULL,
    default_max_review_rounds integer DEFAULT 5 NOT NULL,
    bundled_hash text,
    deleted_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT task_stages_registry_category_check CHECK ((category = ANY (ARRAY['discovery'::text, 'design'::text, 'verification'::text, 'implementation'::text, 'delivery'::text]))),
    CONSTRAINT task_stages_registry_dispatch_type_check CHECK (((dispatch_type IS NULL) OR (dispatch_type = ANY (ARRAY['agent'::text, 'pipeline'::text])))),
    CONSTRAINT task_stages_registry_review_policy_check CHECK ((review_policy = ANY (ARRAY['none'::text, 'required'::text, 'optional'::text])))
);
ALTER TABLE public.task_stages_registry OWNER TO gobby_test;
CREATE TABLE public.task_type_default_stages (
    task_type text NOT NULL,
    stage_name text NOT NULL,
    "position" integer NOT NULL
);
ALTER TABLE public.task_type_default_stages OWNER TO gobby_test;
CREATE TABLE public.task_validation_backoff (
    task_id uuid NOT NULL,
    consecutive_failures integer DEFAULT 0 NOT NULL,
    next_retry_at timestamp with time zone,
    last_error text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE public.task_validation_backoff OWNER TO gobby_test;
CREATE TABLE public.task_validation_history (
    id integer NOT NULL,
    task_id uuid NOT NULL,
    iteration integer NOT NULL,
    status text NOT NULL,
    feedback text,
    issues text,
    context_type text,
    context_summary text,
    validator_type text,
    failure_category text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT task_validation_history_failure_category_check CHECK ((failure_category = ANY (ARRAY['environment'::text, 'dependency'::text, 'code'::text, 'test'::text, 'provider'::text, 'timeout'::text])))
);
ALTER TABLE public.task_validation_history OWNER TO gobby_test;
ALTER TABLE public.task_validation_history ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.task_validation_history_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);
CREATE TABLE public.tasks (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    parent_task_id uuid,
    created_in_session_id uuid,
    claimed_by_session_id uuid,
    closed_in_session_id uuid,
    closed_commit_sha text,
    closed_at timestamp with time zone,
    title text NOT NULL,
    description text,
    priority integer DEFAULT 2,
    task_type text DEFAULT 'task'::text,
    labels jsonb,
    closed_reason text,
    compacted_at timestamp with time zone,
    validation_status text,
    validation_feedback text,
    validation_override_reason text,
    category text,
    validation_criteria text,
    validation_fail_count integer DEFAULT 0,
    dispatch_failure_count integer DEFAULT 0,
    merge_in_progress boolean DEFAULT false NOT NULL,
    blocked_by_merge boolean DEFAULT false NOT NULL,
    allow_automation boolean DEFAULT false NOT NULL,
    unattended boolean DEFAULT false NOT NULL,
    isolation text DEFAULT 'worktree'::text NOT NULL,
    assigned_agent text,
    implementation_domain text,
    additional_skills jsonb,
    commits jsonb,
    escalated_at timestamp with time zone,
    escalation_reason text,
    github_issue_number integer,
    github_pr_number integer,
    github_repo text,
    linear_issue_id text,
    linear_team_id text,
    seq_num integer,
    path_cache text,
    start_date date,
    due_date date,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    is_escalated boolean DEFAULT false NOT NULL,
    state_bucket text DEFAULT 'ready'::text NOT NULL,
    CONSTRAINT tasks_allow_automation_check CHECK ((allow_automation = ANY (ARRAY[false, true]))),
    CONSTRAINT tasks_blocked_by_merge_check CHECK ((blocked_by_merge = ANY (ARRAY[false, true]))),
    CONSTRAINT tasks_implementation_domain_check CHECK (((implementation_domain IS NULL) OR (implementation_domain = ANY (ARRAY['backend'::text, 'frontend'::text, 'fullstack'::text])))),
    CONSTRAINT tasks_is_escalated_check CHECK ((is_escalated = ANY (ARRAY[false, true]))),
    CONSTRAINT tasks_isolation_check CHECK ((isolation = ANY (ARRAY['none'::text, 'worktree'::text, 'clone'::text]))),
    CONSTRAINT tasks_merge_in_progress_check CHECK ((merge_in_progress = ANY (ARRAY[false, true]))),
    CONSTRAINT tasks_state_bucket_check CHECK ((state_bucket = ANY (ARRAY['ready'::text, 'in_progress'::text, 'needs_review'::text, 'review_approved'::text, 'closed'::text, 'escalated'::text]))),
    CONSTRAINT tasks_unattended_check CHECK ((unattended = ANY (ARRAY[false, true]))),
    CONSTRAINT tasks_validation_status_check CHECK ((validation_status = ANY (ARRAY['pending'::text, 'valid'::text, 'invalid'::text, 'error'::text])))
);
ALTER TABLE public.tasks OWNER TO gobby_test;
CREATE TABLE public.terminals (
    id uuid NOT NULL,
    backend text NOT NULL,
    ownership text NOT NULL,
    state text NOT NULL,
    spawn_key text,
    machine_id uuid NOT NULL,
    locator jsonb,
    locator_key text,
    session_name text,
    window_id text,
    title text,
    host_epoch text,
    unresolved_writes jsonb DEFAULT '{}'::jsonb NOT NULL,
    automatic_write_quarantined_at timestamp with time zone,
    automatic_write_quarantine_action_key text,
    attempt_generation integer DEFAULT 1 NOT NULL,
    attempt_started_at timestamp with time zone DEFAULT now() NOT NULL,
    process jsonb,
    rows integer,
    cols integer,
    project_id uuid NOT NULL,
    session_id uuid,
    agent_run_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    liveness_at timestamp with time zone,
    CONSTRAINT terminals_backend_check CHECK ((backend = ANY (ARRAY['tmux'::text, 'native'::text]))),
    CONSTRAINT terminals_external_always_has_locator CHECK (((ownership = 'gobby'::text) OR ((locator IS NOT NULL) AND (locator_key IS NOT NULL)))),
    CONSTRAINT terminals_external_is_never_pending CHECK (((ownership = 'gobby'::text) OR (state <> 'pending'::text))),
    CONSTRAINT terminals_host_epoch_is_native_only CHECK (((host_epoch IS NULL) OR (backend = 'native'::text))),
    CONSTRAINT terminals_locator_pair_consistent CHECK (((locator IS NULL) = (locator_key IS NULL))),
    CONSTRAINT terminals_locator_present_when_attachable CHECK (((state = ANY (ARRAY['pending'::text, 'exited'::text])) OR ((locator IS NOT NULL) AND (locator_key IS NOT NULL)))),
    CONSTRAINT terminals_native_attachable_has_epoch CHECK (((backend <> 'native'::text) OR (state <> ALL (ARRAY['live'::text, 'orphaned'::text])) OR (host_epoch IS NOT NULL))),
    CONSTRAINT terminals_ownership_check CHECK ((ownership = ANY (ARRAY['gobby'::text, 'external'::text]))),
    CONSTRAINT terminals_pending_has_no_identity CHECK (((state <> 'pending'::text) OR ((locator IS NULL) AND (locator_key IS NULL) AND (host_epoch IS NULL)))),
    CONSTRAINT terminals_process_is_native_only CHECK (((process IS NULL) OR (backend = 'native'::text))),
    CONSTRAINT terminals_quarantine_pair_consistent CHECK (((automatic_write_quarantined_at IS NULL) = (automatic_write_quarantine_action_key IS NULL))),
    CONSTRAINT terminals_spawn_key_matches_ownership CHECK ((((ownership = 'gobby'::text) AND (spawn_key IS NOT NULL)) OR ((ownership = 'external'::text) AND (spawn_key IS NULL)))),
    CONSTRAINT terminals_state_check CHECK ((state = ANY (ARRAY['pending'::text, 'live'::text, 'exited'::text, 'orphaned'::text]))),
    CONSTRAINT terminals_title_byte_limit CHECK (((title IS NULL) OR (octet_length(title) <= 1024)))
);
ALTER TABLE public.terminals OWNER TO gobby_test;
CREATE TABLE public.token_events (
    id integer NOT NULL,
    session_id uuid NOT NULL,
    project_id uuid,
    message_id text,
    source text NOT NULL,
    origin text NOT NULL,
    model text,
    model_family text,
    input_tokens integer DEFAULT 0 NOT NULL,
    output_tokens integer DEFAULT 0 NOT NULL,
    cache_creation_tokens integer DEFAULT 0 NOT NULL,
    cache_read_tokens integer DEFAULT 0 NOT NULL,
    context_window integer,
    event_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    metadata jsonb
);
ALTER TABLE public.token_events OWNER TO gobby_test;
ALTER TABLE public.token_events ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.token_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);
CREATE TABLE public.tool_metrics (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    server_name text NOT NULL,
    tool_name text NOT NULL,
    call_count integer DEFAULT 0 NOT NULL,
    success_count integer DEFAULT 0 NOT NULL,
    failure_count integer DEFAULT 0 NOT NULL,
    total_latency_ms real DEFAULT 0 NOT NULL,
    avg_latency_ms real,
    last_called_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE public.tool_metrics OWNER TO gobby_test;
CREATE TABLE public.tool_metrics_daily (
    id integer NOT NULL,
    project_id uuid NOT NULL,
    server_name text NOT NULL,
    tool_name text NOT NULL,
    date date NOT NULL,
    call_count integer DEFAULT 0 NOT NULL,
    success_count integer DEFAULT 0 NOT NULL,
    failure_count integer DEFAULT 0 NOT NULL,
    total_latency_ms real DEFAULT 0 NOT NULL,
    avg_latency_ms real,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE public.tool_metrics_daily OWNER TO gobby_test;
ALTER TABLE public.tool_metrics_daily ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.tool_metrics_daily_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);
CREATE TABLE public.tool_result_chunks (
    id uuid NOT NULL,
    result_id uuid NOT NULL,
    ordinal integer NOT NULL,
    start_offset integer NOT NULL,
    end_offset integer NOT NULL,
    content text NOT NULL
);
ALTER TABLE public.tool_result_chunks OWNER TO gobby_test;
CREATE TABLE public.tool_results (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    session_id uuid,
    server_name text NOT NULL,
    tool_name text NOT NULL,
    content text NOT NULL,
    content_kind text NOT NULL,
    total_chars bigint NOT NULL,
    stored_chars integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT tool_results_content_kind_check CHECK ((content_kind = ANY (ARRAY['json'::text, 'text'::text])))
);
ALTER TABLE public.tool_results OWNER TO gobby_test;
CREATE TABLE public.tool_schema_hashes (
    id integer NOT NULL,
    server_name text NOT NULL,
    tool_name text NOT NULL,
    project_id uuid NOT NULL,
    schema_hash text NOT NULL,
    last_verified_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE public.tool_schema_hashes OWNER TO gobby_test;
ALTER TABLE public.tool_schema_hashes ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.tool_schema_hashes_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);
CREATE TABLE public.tools (
    id uuid NOT NULL,
    mcp_server_id uuid NOT NULL,
    name text NOT NULL,
    description text,
    input_schema jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE public.tools OWNER TO gobby_test;
CREATE TABLE public.unmodeled_observation_events (
    id uuid NOT NULL,
    session_id uuid,
    source text NOT NULL,
    kind text NOT NULL,
    name text NOT NULL,
    server_name text DEFAULT ''::text NOT NULL,
    tool_type text DEFAULT ''::text NOT NULL,
    source_ref text DEFAULT ''::text NOT NULL,
    source_line integer,
    sample_keys jsonb DEFAULT '[]'::jsonb NOT NULL,
    sample_hash text NOT NULL,
    first_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    last_seen_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE public.unmodeled_observation_events OWNER TO gobby_test;
CREATE TABLE public.unmodeled_observations (
    source text NOT NULL,
    kind text NOT NULL,
    name text NOT NULL,
    server_name text DEFAULT ''::text NOT NULL,
    tool_type text DEFAULT ''::text NOT NULL,
    count bigint DEFAULT 0 NOT NULL,
    first_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    last_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    example_session_id uuid,
    sample_keys jsonb DEFAULT '[]'::jsonb NOT NULL,
    sample_hash text NOT NULL,
    CONSTRAINT unmodeled_observations_count_check CHECK ((count >= 0))
);
ALTER TABLE public.unmodeled_observations OWNER TO gobby_test;
CREATE TABLE public.users (
    id uuid NOT NULL,
    email text NOT NULL,
    name text NOT NULL,
    password_hash text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT users_email_check CHECK ((btrim(email) <> ''::text)),
    CONSTRAINT users_name_check CHECK ((btrim(name) <> ''::text))
);
ALTER TABLE public.users OWNER TO gobby_test;
CREATE TABLE public.workflow_audit_log (
    id integer NOT NULL,
    session_id uuid NOT NULL,
    "timestamp" timestamp with time zone DEFAULT now() NOT NULL,
    step text NOT NULL,
    event_type text NOT NULL,
    tool_name text,
    rule_id text,
    condition text,
    result text NOT NULL,
    reason text,
    context jsonb
);
ALTER TABLE public.workflow_audit_log OWNER TO gobby_test;
ALTER TABLE public.workflow_audit_log ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.workflow_audit_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);
CREATE TABLE public.worktrees (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    machine_id uuid NOT NULL,
    task_id uuid,
    branch_name text,
    worktree_path text NOT NULL,
    base_branch text DEFAULT 'main'::text,
    agent_session_id uuid,
    status text DEFAULT 'active'::text,
    merge_state text,
    merged_at timestamp with time zone,
    cleanup_after timestamp with time zone,
    workspace_role text DEFAULT 'task'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    last_activity_at timestamp with time zone
);
ALTER TABLE public.worktrees OWNER TO gobby_test;
ALTER TABLE ONLY public.recall_holdout_consumed ALTER COLUMN id SET DEFAULT nextval('public.recall_holdout_consumed_id_seq'::regclass);
ALTER TABLE ONLY public.recall_shadow_audit_verdicts ALTER COLUMN id SET DEFAULT nextval('public.recall_shadow_audit_verdicts_id_seq'::regclass);
ALTER TABLE ONLY public.recall_usefulness ALTER COLUMN id SET DEFAULT nextval('public.recall_usefulness_id_seq'::regclass);
ALTER TABLE ONLY gobby_agent_auth.daemon_registry
    ADD CONSTRAINT daemon_registry_pkey PRIMARY KEY (machine_id);
ALTER TABLE ONLY gobby_agent_auth.interactive_credential_material
    ADD CONSTRAINT interactive_credential_material_pkey PRIMARY KEY (deployment_token, machine_id, project_id, credential_generation);
ALTER TABLE ONLY gobby_agent_auth.orphan_revocation_retries
    ADD CONSTRAINT orphan_revocation_retries_pkey PRIMARY KEY (role_name);
ALTER TABLE ONLY gobby_agent_auth.principal_audit_events
    ADD CONSTRAINT principal_audit_events_pkey PRIMARY KEY (id);
ALTER TABLE ONLY gobby_agent_auth.principal_bindings
    ADD CONSTRAINT principal_bindings_execution_generation UNIQUE (managed_execution_id, credential_generation);
ALTER TABLE ONLY gobby_agent_auth.principal_bindings
    ADD CONSTRAINT principal_bindings_pkey PRIMARY KEY (id);
ALTER TABLE ONLY gobby_agent_auth.principal_bindings
    ADD CONSTRAINT principal_bindings_role_name_key UNIQUE (role_name);
ALTER TABLE ONLY public.agent_definitions
    ADD CONSTRAINT agent_definitions_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.agent_runs
    ADD CONSTRAINT agent_runs_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.agent_step_instances
    ADD CONSTRAINT agent_step_instances_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.agent_step_instances
    ADD CONSTRAINT agent_step_instances_session_id_key UNIQUE (session_id);
ALTER TABLE ONLY public.agent_step_workflows
    ADD CONSTRAINT agent_step_workflows_agent_definition_id_key UNIQUE (agent_definition_id);
ALTER TABLE ONLY public.agent_step_workflows
    ADD CONSTRAINT agent_step_workflows_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.attention_states
    ADD CONSTRAINT attention_states_pkey PRIMARY KEY (entry_id);
ALTER TABLE ONLY public.auth_sessions
    ADD CONSTRAINT auth_sessions_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.auth_sessions
    ADD CONSTRAINT auth_sessions_token_hash_key UNIQUE (token_hash);
ALTER TABLE ONLY public.bin_update_state
    ADD CONSTRAINT bin_update_state_pkey PRIMARY KEY (machine_id, tool_name);
ALTER TABLE ONLY public.build_history_events
    ADD CONSTRAINT build_history_events_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.build_profiles
    ADD CONSTRAINT build_profiles_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.build_runs
    ADD CONSTRAINT build_runs_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.chat_attachment_cleanup_fences
    ADD CONSTRAINT chat_attachment_cleanup_fences_pkey PRIMARY KEY (scope_kind, scope_id);
ALTER TABLE ONLY public.chat_attachments
    ADD CONSTRAINT chat_attachments_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.chat_messages
    ADD CONSTRAINT chat_messages_conversation_seq_unique UNIQUE (conversation_id, seq);
ALTER TABLE ONLY public.chat_messages
    ADD CONSTRAINT chat_messages_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.checkpoints
    ADD CONSTRAINT checkpoints_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.clones
    ADD CONSTRAINT clones_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.code_calls
    ADD CONSTRAINT code_calls_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.code_calls
    ADD CONSTRAINT code_calls_unique_call_target UNIQUE NULLS NOT DISTINCT (project_id, file_path, content_hash, caller_symbol_id, callee_symbol_id, callee_name, callee_target_kind, callee_external_module, line);
ALTER TABLE ONLY public.code_content_chunks
    ADD CONSTRAINT code_content_chunks_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.code_content_chunks
    ADD CONSTRAINT code_content_chunks_project_file_hash_chunk_index_key UNIQUE (project_id, file_path, content_hash, chunk_index);
ALTER TABLE ONLY public.code_imports
    ADD CONSTRAINT code_imports_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.code_imports
    ADD CONSTRAINT code_imports_project_source_file_hash_target_module_key UNIQUE (project_id, source_file, content_hash, target_module);
ALTER TABLE ONLY public.code_index_projection_cleanup_pending
    ADD CONSTRAINT code_index_projection_cleanup_pending_pkey PRIMARY KEY (project_id, store);
ALTER TABLE ONLY public.code_index_prune_dirty_projects
    ADD CONSTRAINT code_index_prune_dirty_projects_pkey PRIMARY KEY (machine_id, project_id);
ALTER TABLE ONLY public.code_indexed_file_states
    ADD CONSTRAINT code_indexed_file_states_pkey PRIMARY KEY (machine_id, project_id, file_path);
ALTER TABLE ONLY public.code_indexed_files
    ADD CONSTRAINT code_indexed_files_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.code_indexed_files
    ADD CONSTRAINT code_indexed_files_project_id_file_path_content_hash_key UNIQUE (project_id, file_path, content_hash);
ALTER TABLE ONLY public.code_indexed_project_states
    ADD CONSTRAINT code_indexed_project_states_pkey PRIMARY KEY (machine_id, project_id);
ALTER TABLE ONLY public.code_indexed_projects
    ADD CONSTRAINT code_indexed_projects_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.code_inheritance
    ADD CONSTRAINT code_inheritance_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.code_inheritance
    ADD CONSTRAINT code_inheritance_unique_target UNIQUE NULLS NOT DISTINCT (project_id, file_path, content_hash, source_symbol_id, source_name, source_kind, source_external_module, target_symbol_id, target_name, target_kind, target_external_module, heritage_kind, line);
ALTER TABLE ONLY public.code_symbols
    ADD CONSTRAINT code_symbols_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.comms_attachments
    ADD CONSTRAINT comms_attachments_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.comms_channels
    ADD CONSTRAINT comms_channels_name_key UNIQUE (name);
ALTER TABLE ONLY public.comms_channels
    ADD CONSTRAINT comms_channels_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.comms_identities
    ADD CONSTRAINT comms_identities_channel_id_external_user_id_key UNIQUE (channel_id, external_user_id);
ALTER TABLE ONLY public.comms_identities
    ADD CONSTRAINT comms_identities_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.comms_messages
    ADD CONSTRAINT comms_messages_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.comms_routing_rules
    ADD CONSTRAINT comms_routing_rules_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.completion_subscribers
    ADD CONSTRAINT completion_subscribers_pkey PRIMARY KEY (completion_id, session_id);
ALTER TABLE ONLY public.config_state
    ADD CONSTRAINT config_state_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.config_store
    ADD CONSTRAINT config_store_pkey PRIMARY KEY (key);
ALTER TABLE ONLY public.cron_jobs
    ADD CONSTRAINT cron_jobs_name_key UNIQUE (name);
ALTER TABLE ONLY public.cron_jobs
    ADD CONSTRAINT cron_jobs_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.cron_runs
    ADD CONSTRAINT cron_runs_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.definition_revisions
    ADD CONSTRAINT definition_revisions_pkey PRIMARY KEY (domain);
ALTER TABLE ONLY public.deployment_runtime
    ADD CONSTRAINT deployment_runtime_pkey PRIMARY KEY (deployment_token);
ALTER TABLE ONLY public.destructive_batches
    ADD CONSTRAINT destructive_batches_maintenance_epoch_id_key UNIQUE (maintenance_epoch_id);
ALTER TABLE ONLY public.destructive_batches
    ADD CONSTRAINT destructive_batches_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.detection_manifests
    ADD CONSTRAINT detection_manifests_pkey PRIMARY KEY (provider_id);
ALTER TABLE ONLY public.embedding_generation_acks
    ADD CONSTRAINT embedding_generation_acks_pkey PRIMARY KEY (daemon_instance_id);
ALTER TABLE ONLY public.embedding_projection_changes
    ADD CONSTRAINT embedding_projection_changes_pkey PRIMARY KEY (sequence);
ALTER TABLE ONLY public.expansion_runs
    ADD CONSTRAINT expansion_runs_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.external_issue_sync_status
    ADD CONSTRAINT external_issue_sync_status_pkey PRIMARY KEY (project_id, provider);
ALTER TABLE ONLY public.feedback_review_runs
    ADD CONSTRAINT feedback_review_runs_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.gh_issues_triaged
    ADD CONSTRAINT gh_issues_triaged_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.gh_issues_triaged
    ADD CONSTRAINT gh_issues_triaged_project_id_repo_issue_number_key UNIQUE (project_id, repo, issue_number);
ALTER TABLE ONLY public.gh_triage_build_dispatches
    ADD CONSTRAINT gh_triage_build_dispatches_pkey PRIMARY KEY (project_id, repo, issue_number);
ALTER TABLE ONLY public.gh_triage_deliveries
    ADD CONSTRAINT gh_triage_deliveries_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.gh_triage_deliveries
    ADD CONSTRAINT gh_triage_deliveries_project_id_delivery_id_key UNIQUE (project_id, delivery_id);
ALTER TABLE ONLY public.hook_force_continue_budgets
    ADD CONSTRAINT hook_force_continue_budgets_pkey PRIMARY KEY (session_id, execution_num);
ALTER TABLE ONLY public.hook_receipt_effects
    ADD CONSTRAINT hook_receipt_effects_pkey PRIMARY KEY (receipt_id);
ALTER TABLE ONLY public.prompts
    ADD CONSTRAINT idx_prompts_name_scope_project UNIQUE NULLS NOT DISTINCT (name, scope, project_id);
ALTER TABLE ONLY public.skills
    ADD CONSTRAINT idx_skills_name_project_source UNIQUE NULLS NOT DISTINCT (name, project_id, source);
ALTER TABLE ONLY public.integration_workspace_mutex
    ADD CONSTRAINT integration_workspace_mutex_pkey PRIMARY KEY (integration_key);
ALTER TABLE ONLY public.inter_session_messages
    ADD CONSTRAINT inter_session_messages_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.loop_progress
    ADD CONSTRAINT loop_progress_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.machines
    ADD CONSTRAINT machines_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.maintenance_epochs
    ADD CONSTRAINT maintenance_epochs_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.mcp_server_templates
    ADD CONSTRAINT mcp_server_templates_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.mcp_servers
    ADD CONSTRAINT mcp_servers_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.memories
    ADD CONSTRAINT memories_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.memory_crossrefs
    ADD CONSTRAINT memory_crossrefs_pkey PRIMARY KEY (source_id, target_id);
ALTER TABLE ONLY public.memory_dream_runs
    ADD CONSTRAINT memory_dream_runs_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.memory_dream_snapshots
    ADD CONSTRAINT memory_dream_snapshots_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.memory_dream_truth_state
    ADD CONSTRAINT memory_dream_truth_state_pkey PRIMARY KEY (project_id);
ALTER TABLE ONLY public.merge_conflicts
    ADD CONSTRAINT merge_conflicts_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.merge_resolutions
    ADD CONSTRAINT merge_resolutions_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.metric_snapshots
    ADD CONSTRAINT metric_snapshots_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.metrics_events_archive
    ADD CONSTRAINT metrics_events_archive_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.metrics_events_archive
    ADD CONSTRAINT metrics_events_archive_unique_rollup UNIQUE NULLS NOT DISTINCT (event_type, project_id, server_name, name);
ALTER TABLE ONLY public.metrics_events
    ADD CONSTRAINT metrics_events_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.model_metadata
    ADD CONSTRAINT model_metadata_pkey PRIMARY KEY (model);
ALTER TABLE ONLY public.pending_interactions
    ADD CONSTRAINT pending_interactions_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.pipeline_definitions
    ADD CONSTRAINT pipeline_definitions_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.pipeline_executions
    ADD CONSTRAINT pipeline_executions_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.pipeline_executions
    ADD CONSTRAINT pipeline_executions_resume_token_key UNIQUE (resume_token);
ALTER TABLE ONLY public.plan_review_evidence
    ADD CONSTRAINT plan_review_evidence_pkey PRIMARY KEY (evidence_id);
ALTER TABLE ONLY public.plans
    ADD CONSTRAINT plans_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.plans
    ADD CONSTRAINT plans_project_id_plan_id_key UNIQUE (project_id, plan_id);
ALTER TABLE ONLY public.project_checkouts
    ADD CONSTRAINT project_checkouts_machine_id_root_path_key UNIQUE (machine_id, root_path);
ALTER TABLE ONLY public.project_checkouts
    ADD CONSTRAINT project_checkouts_pkey PRIMARY KEY (machine_id, project_id);
ALTER TABLE ONLY public.project_github_triage_configs
    ADD CONSTRAINT project_github_triage_configs_pkey PRIMARY KEY (project_id);
ALTER TABLE ONLY public.project_lifecycle_events
    ADD CONSTRAINT project_lifecycle_events_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.prompts
    ADD CONSTRAINT prompts_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.provider_capability_refresh_state
    ADD CONSTRAINT provider_capability_refresh_state_pkey PRIMARY KEY (provider, source_key);
ALTER TABLE ONLY public.provider_capacity_snapshots
    ADD CONSTRAINT provider_capacity_snapshots_pkey PRIMARY KEY (machine_id, provider);
ALTER TABLE ONLY public.provider_model_capabilities
    ADD CONSTRAINT provider_model_capabilities_pkey PRIMARY KEY (provider, canonical_model);
ALTER TABLE ONLY public.provider_model_routes
    ADD CONSTRAINT provider_model_routes_pkey PRIMARY KEY (provider, canonical_model, speed_mode);
ALTER TABLE ONLY public.recall_gate_runs
    ADD CONSTRAINT recall_gate_runs_pkey PRIMARY KEY (holdout_consumption_key);
ALTER TABLE ONLY public.recall_holdout_consumed
    ADD CONSTRAINT recall_holdout_consumed_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.recall_holdout_consumed
    ADD CONSTRAINT recall_holdout_consumed_request_id_key UNIQUE (request_id);
ALTER TABLE ONLY public.recall_injection_outcomes
    ADD CONSTRAINT recall_injection_outcomes_pkey PRIMARY KEY (recall_request_id, memory_id);
ALTER TABLE ONLY public.recall_shadow_audit_verdicts
    ADD CONSTRAINT recall_shadow_audit_verdicts_cohort_digest_request_id_memor_key UNIQUE (cohort_digest, request_id, memory_id);
ALTER TABLE ONLY public.recall_shadow_audit_verdicts
    ADD CONSTRAINT recall_shadow_audit_verdicts_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.recall_shadow_judge_state
    ADD CONSTRAINT recall_shadow_judge_state_pkey PRIMARY KEY (recall_request_id, label_source, judge_protocol_version);
ALTER TABLE ONLY public.recall_shadow_prompt_snapshot
    ADD CONSTRAINT recall_shadow_prompt_snapshot_pkey PRIMARY KEY (recall_request_id, label_source, judge_protocol_version);
ALTER TABLE ONLY public.recall_signal_hits
    ADD CONSTRAINT recall_signal_hits_pkey PRIMARY KEY (recall_request_id, memory_id);
ALTER TABLE ONLY public.recall_signal_requests
    ADD CONSTRAINT recall_signal_requests_pkey PRIMARY KEY (session_id, recall_request_id);
ALTER TABLE ONLY public.recall_usefulness
    ADD CONSTRAINT recall_usefulness_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.recall_usefulness
    ADD CONSTRAINT recall_usefulness_recall_request_id_memory_id_label_source__key UNIQUE (recall_request_id, memory_id, label_source, judge_protocol_version);
ALTER TABLE ONLY public.rule_definitions
    ADD CONSTRAINT rule_definitions_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.schema_migrations
    ADD CONSTRAINT schema_migrations_pkey PRIMARY KEY (version);
ALTER TABLE ONLY public.secret_key_material
    ADD CONSTRAINT secret_key_material_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.secrets
    ADD CONSTRAINT secrets_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.session_feedback
    ADD CONSTRAINT session_feedback_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.session_skills
    ADD CONSTRAINT session_skills_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.session_stop_signals
    ADD CONSTRAINT session_stop_signals_pkey PRIMARY KEY (session_id);
ALTER TABLE ONLY public.session_summary_revisions
    ADD CONSTRAINT session_summary_revisions_id_session_id_unique UNIQUE (id, session_id);
ALTER TABLE ONLY public.session_summary_revisions
    ADD CONSTRAINT session_summary_revisions_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.session_tasks
    ADD CONSTRAINT session_tasks_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.session_tasks
    ADD CONSTRAINT session_tasks_session_id_task_id_action_key UNIQUE (session_id, task_id, action);
ALTER TABLE ONLY public.session_variable_defaults
    ADD CONSTRAINT session_variable_defaults_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.session_variables
    ADD CONSTRAINT session_variables_pkey PRIMARY KEY (session_id);
ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_id_machine_id_key UNIQUE (id, machine_id);
ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.skill_files
    ADD CONSTRAINT skill_files_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.skill_files
    ADD CONSTRAINT skill_files_skill_id_path_key UNIQUE (skill_id, path);
ALTER TABLE ONLY public.skills
    ADD CONSTRAINT skills_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.spans
    ADD CONSTRAINT spans_pkey PRIMARY KEY (span_id);
ALTER TABLE ONLY public.step_executions
    ADD CONSTRAINT step_executions_approval_token_key UNIQUE (approval_token);
ALTER TABLE ONLY public.step_executions
    ADD CONSTRAINT step_executions_execution_id_step_id_key UNIQUE (execution_id, step_id);
ALTER TABLE ONLY public.step_executions
    ADD CONSTRAINT step_executions_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.task_affected_files
    ADD CONSTRAINT task_affected_files_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.task_affected_files
    ADD CONSTRAINT task_affected_files_task_id_file_path_key UNIQUE (task_id, file_path);
ALTER TABLE ONLY public.task_artifacts
    ADD CONSTRAINT task_artifacts_pkey PRIMARY KEY (task_id);
ALTER TABLE ONLY public.task_close_reviews
    ADD CONSTRAINT task_close_reviews_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.task_comments
    ADD CONSTRAINT task_comments_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.task_delivery_campaigns
    ADD CONSTRAINT task_delivery_campaigns_pkey PRIMARY KEY (task_id);
ALTER TABLE ONLY public.task_delivery_units
    ADD CONSTRAINT task_delivery_units_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.task_delivery_units
    ADD CONSTRAINT task_delivery_units_task_id_unit_key_key UNIQUE (task_id, unit_key);
ALTER TABLE ONLY public.task_dependencies
    ADD CONSTRAINT task_dependencies_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.task_dependencies
    ADD CONSTRAINT task_dependencies_task_id_depends_on_dep_type_key UNIQUE (task_id, depends_on, dep_type);
ALTER TABLE ONLY public.task_dispatch_mutex
    ADD CONSTRAINT task_dispatch_mutex_pkey PRIMARY KEY (task_id);
ALTER TABLE ONLY public.task_lifecycle_events
    ADD CONSTRAINT task_lifecycle_events_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.task_selection_history
    ADD CONSTRAINT task_selection_history_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.task_stage_states
    ADD CONSTRAINT task_stage_states_pkey PRIMARY KEY (task_id, stage_name);
ALTER TABLE ONLY public.task_stages_registry
    ADD CONSTRAINT task_stages_registry_pkey PRIMARY KEY (name);
ALTER TABLE ONLY public.task_type_default_stages
    ADD CONSTRAINT task_type_default_stages_pkey PRIMARY KEY (task_type, stage_name);
ALTER TABLE ONLY public.task_validation_backoff
    ADD CONSTRAINT task_validation_backoff_pkey PRIMARY KEY (task_id);
ALTER TABLE ONLY public.task_validation_history
    ADD CONSTRAINT task_validation_history_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_pkey PRIMARY KEY (id);
ALTER TABLE public.tasks
    ADD CONSTRAINT tasks_require_validation_criteria CHECK (((task_type = 'epic'::text) OR (NULLIF(btrim(validation_criteria), ''::text) IS NOT NULL))) NOT VALID;
ALTER TABLE ONLY public.terminals
    ADD CONSTRAINT terminals_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.token_events
    ADD CONSTRAINT token_events_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.tool_metrics_daily
    ADD CONSTRAINT tool_metrics_daily_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.tool_metrics_daily
    ADD CONSTRAINT tool_metrics_daily_project_id_server_name_tool_name_date_key UNIQUE (project_id, server_name, tool_name, date);
ALTER TABLE ONLY public.tool_metrics
    ADD CONSTRAINT tool_metrics_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.tool_metrics
    ADD CONSTRAINT tool_metrics_project_id_server_name_tool_name_key UNIQUE (project_id, server_name, tool_name);
ALTER TABLE ONLY public.tool_result_chunks
    ADD CONSTRAINT tool_result_chunks_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.tool_result_chunks
    ADD CONSTRAINT tool_result_chunks_result_id_ordinal_key UNIQUE (result_id, ordinal);
ALTER TABLE ONLY public.tool_results
    ADD CONSTRAINT tool_results_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.tool_schema_hashes
    ADD CONSTRAINT tool_schema_hashes_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.tool_schema_hashes
    ADD CONSTRAINT tool_schema_hashes_project_id_server_name_tool_name_key UNIQUE (project_id, server_name, tool_name);
ALTER TABLE ONLY public.tools
    ADD CONSTRAINT tools_mcp_server_id_name_key UNIQUE (mcp_server_id, name);
ALTER TABLE ONLY public.tools
    ADD CONSTRAINT tools_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.unmodeled_observation_events
    ADD CONSTRAINT unmodeled_observation_events_dedup_key UNIQUE NULLS NOT DISTINCT (session_id, source, kind, name, server_name, tool_type, source_ref, sample_hash);
ALTER TABLE ONLY public.unmodeled_observation_events
    ADD CONSTRAINT unmodeled_observation_events_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.unmodeled_observations
    ADD CONSTRAINT unmodeled_observations_pkey PRIMARY KEY (source, kind, name, server_name, tool_type);
ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.workflow_audit_log
    ADD CONSTRAINT workflow_audit_log_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.worktrees
    ADD CONSTRAINT worktrees_pkey PRIMARY KEY (id);
CREATE INDEX idx_principal_bindings_active_role ON gobby_agent_auth.principal_bindings USING btree (role_name) WHERE (revoked_at IS NULL);
CREATE INDEX idx_principal_bindings_execution ON gobby_agent_auth.principal_bindings USING btree (managed_execution_id, credential_generation DESC);
CREATE INDEX idx_principal_bindings_expiry ON gobby_agent_auth.principal_bindings USING btree (expires_at) WHERE (revoked_at IS NULL);
CREATE UNIQUE INDEX uq_interactive_principal_active ON gobby_agent_auth.principal_bindings USING btree (deployment_token, issuing_machine_id, project_id, code_overlay_project_id) NULLS NOT DISTINCT WHERE ((owner_kind = 'interactive'::text) AND (revoked_at IS NULL) AND (predecessor_drain_deadline IS NULL));
CREATE INDEX code_content_search_bm25 ON public.code_content_chunks USING bm25 (id, content) WITH (key_field=id);
CREATE INDEX code_symbols_search_bm25 ON public.code_symbols USING bm25 (id, name, qualified_name, signature, docstring, summary) WITH (key_field=id);
CREATE INDEX destructive_batches_epoch_lookup ON public.destructive_batches USING btree (maintenance_epoch_id, created_at);
CREATE INDEX hook_force_continue_budgets_updated_at_idx ON public.hook_force_continue_budgets USING btree (updated_at);
CREATE INDEX hook_receipt_effects_current_envelope_idx ON public.hook_receipt_effects USING btree (current_envelope_id);
CREATE INDEX hook_receipt_effects_session_id_idx ON public.hook_receipt_effects USING btree (session_id);
CREATE INDEX hook_receipt_effects_state_transition_idx ON public.hook_receipt_effects USING btree (state, transition_at);
CREATE INDEX idx_agent_defs_project ON public.agent_definitions USING btree (project_id);
CREATE INDEX idx_agent_runs_child_session ON public.agent_runs USING btree (child_session_id);
CREATE INDEX idx_agent_runs_machine_status ON public.agent_runs USING btree (machine_id, status);
CREATE INDEX idx_agent_runs_parent_session ON public.agent_runs USING btree (parent_session_id);
CREATE INDEX idx_agent_runs_pending_termination ON public.agent_runs USING btree (termination_requested_at) WHERE ((status = ANY (ARRAY['pending'::text, 'running'::text])) AND (pending_terminal_action IS NOT NULL));
CREATE INDEX idx_agent_runs_provider ON public.agent_runs USING btree (provider);
CREATE INDEX idx_agent_runs_status ON public.agent_runs USING btree (status);
CREATE INDEX idx_agent_runs_task_id ON public.agent_runs USING btree (task_id);
CREATE INDEX idx_asi_step_workflow ON public.agent_step_instances USING btree (agent_step_workflow_id);
CREATE INDEX idx_attention_states_blocked ON public.attention_states USING btree (updated_at DESC) WHERE (state = 'blocked'::text);
CREATE INDEX idx_audit_event_type ON public.workflow_audit_log USING btree (event_type);
CREATE INDEX idx_audit_result ON public.workflow_audit_log USING btree (result);
CREATE INDEX idx_audit_session ON public.workflow_audit_log USING btree (session_id);
CREATE INDEX idx_audit_timestamp ON public.workflow_audit_log USING btree ("timestamp");
CREATE INDEX idx_auth_sessions_expires ON public.auth_sessions USING btree (expires_at);
CREATE INDEX idx_auth_sessions_user_id ON public.auth_sessions USING btree (user_id);
CREATE INDEX idx_build_history_events_project ON public.build_history_events USING btree (project_id, created_at DESC);
CREATE INDEX idx_build_history_events_root ON public.build_history_events USING btree (root_task_id, created_at DESC);
CREATE INDEX idx_build_history_events_run ON public.build_history_events USING btree (run_id, created_at DESC);
CREATE UNIQUE INDEX idx_build_profiles_active_unique ON public.build_profiles USING btree (name, project_id, source) NULLS NOT DISTINCT WHERE (deleted_at IS NULL);
CREATE INDEX idx_build_profiles_project_source ON public.build_profiles USING btree (project_id, source, name);
CREATE INDEX idx_build_runs_input_started ON public.build_runs USING btree (project_id, input_ref, started_at DESC);
CREATE INDEX idx_build_runs_project_root_started ON public.build_runs USING btree (project_id, root_task_id, started_at DESC);
CREATE INDEX idx_build_runs_project_started ON public.build_runs USING btree (project_id, started_at DESC);
CREATE INDEX idx_build_runs_root_started ON public.build_runs USING btree (root_task_id, started_at DESC);
CREATE INDEX idx_cc_caller ON public.code_calls USING btree (project_id, caller_symbol_id);
CREATE INDEX idx_cc_file ON public.code_calls USING btree (project_id, file_path);
CREATE INDEX idx_cc_target ON public.code_calls USING btree (project_id, callee_target_kind, callee_symbol_id, callee_name);
CREATE INDEX idx_ccc_file ON public.code_content_chunks USING btree (project_id, file_path);
CREATE INDEX idx_ccc_project ON public.code_content_chunks USING btree (project_id);
CREATE INDEX idx_chat_attachments_claim ON public.chat_attachments USING btree (claim_token, claimed_at) WHERE (claim_token IS NOT NULL);
CREATE INDEX idx_chat_attachments_conversation ON public.chat_attachments USING btree (conversation_id);
CREATE INDEX idx_chat_attachments_draft ON public.chat_attachments USING btree (draft_id);
CREATE INDEX idx_chat_attachments_local_path ON public.chat_attachments USING btree (local_path);
CREATE INDEX idx_chat_attachments_machine_unbound ON public.chat_attachments USING btree (machine_id, created_at) WHERE ((conversation_id IS NULL) AND (message_id IS NULL) AND (target_session_id IS NULL) AND (bound_at IS NULL));
CREATE INDEX idx_chat_attachments_message ON public.chat_attachments USING btree (message_id);
CREATE INDEX idx_chat_attachments_project ON public.chat_attachments USING btree (project_id);
CREATE INDEX idx_chat_attachments_target_session ON public.chat_attachments USING btree (target_session_id);
CREATE INDEX idx_chat_attachments_unpublished ON public.chat_attachments USING btree (created_at) WHERE (published IS FALSE);
CREATE INDEX idx_checkpoints_run ON public.checkpoints USING btree (run_id);
CREATE INDEX idx_checkpoints_session ON public.checkpoints USING btree (session_id);
CREATE INDEX idx_checkpoints_task ON public.checkpoints USING btree (task_id, created_at DESC);
CREATE INDEX idx_ci_file ON public.code_imports USING btree (project_id, source_file);
CREATE INDEX idx_cif_graph_synced ON public.code_indexed_files USING btree (project_id, graph_synced);
CREATE INDEX idx_cif_project ON public.code_indexed_files USING btree (project_id);
CREATE INDEX idx_cif_vectors_synced ON public.code_indexed_files USING btree (project_id, vectors_synced);
CREATE INDEX idx_cifs_content ON public.code_indexed_file_states USING btree (project_id, file_path, content_hash);
CREATE INDEX idx_cifs_machine_project ON public.code_indexed_file_states USING btree (machine_id, project_id);
CREATE INDEX idx_cinherit_file ON public.code_inheritance USING btree (project_id, file_path);
CREATE INDEX idx_cinherit_source ON public.code_inheritance USING btree (project_id, source_symbol_id);
CREATE INDEX idx_cinherit_target ON public.code_inheritance USING btree (project_id, target_kind, target_symbol_id, target_name);
CREATE INDEX idx_cipcp_updated ON public.code_index_projection_cleanup_pending USING btree (updated_at, created_at);
CREATE INDEX idx_cipdp_updated ON public.code_index_prune_dirty_projects USING btree (updated_at, created_at);
CREATE INDEX idx_cips_project ON public.code_indexed_project_states USING btree (project_id);
CREATE UNIQUE INDEX idx_clones_path ON public.clones USING btree (machine_id, clone_path);
CREATE INDEX idx_clones_project ON public.clones USING btree (project_id);
CREATE INDEX idx_clones_session ON public.clones USING btree (agent_session_id);
CREATE INDEX idx_clones_status ON public.clones USING btree (status);
CREATE INDEX idx_clones_task ON public.clones USING btree (task_id);
CREATE INDEX idx_comms_attachments_message ON public.comms_attachments USING btree (message_id);
CREATE INDEX idx_comms_attachments_message_machine ON public.comms_attachments USING btree (message_id, machine_id);
CREATE INDEX idx_comms_identities_channel ON public.comms_identities USING btree (channel_id);
CREATE INDEX idx_comms_identities_external_user ON public.comms_identities USING btree (external_user_id);
CREATE INDEX idx_comms_identities_session ON public.comms_identities USING btree (session_id);
CREATE INDEX idx_comms_messages_channel_created ON public.comms_messages USING btree (channel_id, created_at);
CREATE UNIQUE INDEX idx_comms_messages_channel_platform_message ON public.comms_messages USING btree (channel_id, platform_message_id) WHERE (platform_message_id IS NOT NULL);
CREATE INDEX idx_comms_messages_direction ON public.comms_messages USING btree (direction);
CREATE INDEX idx_comms_messages_session ON public.comms_messages USING btree (session_id);
CREATE INDEX idx_comms_routing_rules_channel ON public.comms_routing_rules USING btree (channel_id);
CREATE INDEX idx_comms_routing_rules_enabled ON public.comms_routing_rules USING btree (enabled);
CREATE INDEX idx_completion_subscribers_completion ON public.completion_subscribers USING btree (completion_id);
CREATE INDEX idx_config_store_source ON public.config_store USING btree (source);
CREATE INDEX idx_cron_jobs_due ON public.cron_jobs USING btree (project_id, enabled, next_run_at);
CREATE INDEX idx_cron_jobs_enabled ON public.cron_jobs USING btree (enabled);
CREATE INDEX idx_cron_jobs_next_run ON public.cron_jobs USING btree (next_run_at);
CREATE INDEX idx_cron_jobs_project ON public.cron_jobs USING btree (project_id);
CREATE INDEX idx_cron_runs_agent_run ON public.cron_runs USING btree (agent_run_id) WHERE (agent_run_id IS NOT NULL);
CREATE INDEX idx_cron_runs_job ON public.cron_runs USING btree (cron_job_id);
CREATE UNIQUE INDEX idx_cron_runs_one_active_per_job ON public.cron_runs USING btree (cron_job_id) WHERE (status = ANY (ARRAY['pending'::text, 'running'::text]));
CREATE INDEX idx_cron_runs_pipeline_execution ON public.cron_runs USING btree (pipeline_execution_id) WHERE (pipeline_execution_id IS NOT NULL);
CREATE INDEX idx_cron_runs_scheduler_owner_active ON public.cron_runs USING btree (scheduler_owner) WHERE (status = ANY (ARRAY['pending'::text, 'running'::text]));
CREATE INDEX idx_cron_runs_status ON public.cron_runs USING btree (status);
CREATE INDEX idx_cron_runs_triggered ON public.cron_runs USING btree (triggered_at);
CREATE INDEX idx_crossrefs_similarity ON public.memory_crossrefs USING btree (similarity DESC);
CREATE INDEX idx_crossrefs_source ON public.memory_crossrefs USING btree (source_id);
CREATE INDEX idx_crossrefs_target ON public.memory_crossrefs USING btree (target_id);
CREATE INDEX idx_cs_file ON public.code_symbols USING btree (project_id, file_path);
CREATE INDEX idx_cs_kind ON public.code_symbols USING btree (kind);
CREATE INDEX idx_cs_name ON public.code_symbols USING btree (name);
CREATE INDEX idx_cs_parent ON public.code_symbols USING btree (parent_symbol_id);
CREATE INDEX idx_cs_project ON public.code_symbols USING btree (project_id);
CREATE INDEX idx_cs_qualified ON public.code_symbols USING btree (qualified_name);
CREATE INDEX idx_deps_depends_on ON public.task_dependencies USING btree (depends_on);
CREATE INDEX idx_deps_task ON public.task_dependencies USING btree (task_id);
CREATE INDEX idx_dispatch_mutex_run_id ON public.task_dispatch_mutex USING btree (run_id);
CREATE INDEX idx_dispatch_mutex_scan ON public.task_dispatch_mutex USING btree (lease_until, run_id);
CREATE INDEX idx_expansion_runs_parent_task ON public.expansion_runs USING btree (parent_task_id, created_at DESC);
CREATE INDEX idx_expansion_runs_status ON public.expansion_runs USING btree (status, created_at DESC);
CREATE INDEX idx_external_issue_sync_status_state ON public.external_issue_sync_status USING btree (provider, state, updated_at);
CREATE INDEX idx_feedback_review_runs_created ON public.feedback_review_runs USING btree (created_at DESC);
CREATE INDEX idx_gh_issues_triaged_project_hash ON public.gh_issues_triaged USING btree (project_id, content_hash);
CREATE INDEX idx_gh_issues_triaged_task ON public.gh_issues_triaged USING btree (task_id);
CREATE INDEX idx_gh_triage_build_dispatches_task_id ON public.gh_triage_build_dispatches USING btree (task_id);
CREATE INDEX idx_gh_triage_deliveries_issue ON public.gh_triage_deliveries USING btree (project_id, repository, issue_number);
CREATE INDEX idx_gh_triage_deliveries_project_status ON public.gh_triage_deliveries USING btree (project_id, status);
CREATE INDEX idx_gh_triage_deliveries_retry ON public.gh_triage_deliveries USING btree (project_id, status, next_attempt_at, updated_at);
CREATE INDEX idx_inter_session_messages_from_session ON public.inter_session_messages USING btree (from_session);
CREATE INDEX idx_inter_session_messages_to_session ON public.inter_session_messages USING btree (to_session);
CREATE INDEX idx_ism_completion_lookup ON public.inter_session_messages USING btree (to_session, message_type) WHERE (metadata_json IS NOT NULL);
CREATE INDEX idx_ism_undelivered ON public.inter_session_messages USING btree (to_session, delivered_at) WHERE (delivered_at IS NULL);
CREATE INDEX idx_lifecycle_events_task ON public.task_lifecycle_events USING btree (task_id, created_at);
CREATE INDEX idx_loop_progress_high_value ON public.loop_progress USING btree (session_id, is_high_value, recorded_at DESC) WHERE (is_high_value IS TRUE);
CREATE INDEX idx_loop_progress_session ON public.loop_progress USING btree (session_id, recorded_at DESC);
CREATE INDEX idx_machines_last_seen ON public.machines USING btree (last_seen);
CREATE INDEX idx_machines_owner_user_id ON public.machines USING btree (owner_user_id);
CREATE UNIQUE INDEX idx_mcp_server_templates_name_project ON public.mcp_server_templates USING btree (name, project_id);
CREATE INDEX idx_mcp_servers_enabled ON public.mcp_servers USING btree (enabled);
CREATE INDEX idx_mcp_servers_name ON public.mcp_servers USING btree (name);
CREATE UNIQUE INDEX idx_mcp_servers_name_project ON public.mcp_servers USING btree (name, project_id);
CREATE INDEX idx_mcp_servers_project_id ON public.mcp_servers USING btree (project_id);
CREATE INDEX idx_mcp_servers_template_id ON public.mcp_servers USING btree (template_id);
CREATE INDEX idx_me_created ON public.metrics_events USING btree (created_at);
CREATE INDEX idx_me_name ON public.metrics_events USING btree (name, event_type);
CREATE INDEX idx_me_session ON public.metrics_events USING btree (session_id, created_at);
CREATE INDEX idx_me_type_created ON public.metrics_events USING btree (event_type, created_at);
CREATE INDEX idx_memories_dream_candidates ON public.memories USING btree (last_dreamed_at, updated_at) WHERE (deleted_at IS NULL);
CREATE INDEX idx_memories_dream_purge ON public.memories USING btree (dream_action, deleted_at) WHERE (deleted_at IS NOT NULL);
CREATE INDEX idx_memories_global_live ON public.memories USING btree (updated_at DESC) WHERE ((is_global IS TRUE) AND (deleted_at IS NULL));
CREATE INDEX idx_memories_graph_pending ON public.memories USING btree (graph_processed) WHERE (graph_processed IS FALSE);
CREATE INDEX idx_memories_graph_status_pending ON public.memories USING btree (created_at) WHERE (graph_status = 'pending'::text);
CREATE INDEX idx_memories_project_live ON public.memories USING btree (project_id, updated_at DESC) WHERE (deleted_at IS NULL);
CREATE INDEX idx_memories_source_session ON public.memories USING btree (source_session_id);
CREATE INDEX idx_memories_source_task ON public.memories USING btree (source_task_id);
CREATE INDEX idx_memories_type ON public.memories USING btree (memory_type);
CREATE INDEX idx_memories_vector_needs_reindex ON public.memories USING btree (id) WHERE (vector_needs_reindex IS TRUE);
CREATE UNIQUE INDEX idx_memory_dream_runs_single_running ON public.memory_dream_runs USING btree (status) WHERE (status = 'running'::text);
CREATE INDEX idx_memory_dream_snapshots_run ON public.memory_dream_snapshots USING btree (run_id);
CREATE INDEX idx_merge_conflicts_file_path ON public.merge_conflicts USING btree (file_path);
CREATE INDEX idx_merge_conflicts_resolution ON public.merge_conflicts USING btree (resolution_id);
CREATE INDEX idx_merge_conflicts_status ON public.merge_conflicts USING btree (status);
CREATE INDEX idx_merge_resolutions_source_branch ON public.merge_resolutions USING btree (source_branch);
CREATE INDEX idx_merge_resolutions_status ON public.merge_resolutions USING btree (status);
CREATE INDEX idx_merge_resolutions_target_branch ON public.merge_resolutions USING btree (target_branch);
CREATE INDEX idx_merge_resolutions_worktree ON public.merge_resolutions USING btree (worktree_id);
CREATE INDEX idx_metric_snapshots_ts ON public.metric_snapshots USING btree ("timestamp");
CREATE INDEX idx_pe_status_project_updated ON public.pipeline_executions USING btree (status, project_id, updated_at);
CREATE INDEX idx_pe_status_updated ON public.pipeline_executions USING btree (status, updated_at);
CREATE UNIQUE INDEX idx_pending_interactions_active ON public.pending_interactions USING btree (session_id, kind) WHERE (status = 'pending'::text);
CREATE INDEX idx_pending_interactions_session ON public.pending_interactions USING btree (session_id, status);
CREATE INDEX idx_pipeline_defs_project ON public.pipeline_definitions USING btree (project_id);
CREATE INDEX idx_pipeline_executions_created_at ON public.pipeline_executions USING btree (created_at DESC);
CREATE INDEX idx_pipeline_executions_project ON public.pipeline_executions USING btree (project_id);
CREATE INDEX idx_pipeline_executions_resume_token ON public.pipeline_executions USING btree (resume_token);
CREATE INDEX idx_pipeline_executions_status ON public.pipeline_executions USING btree (status);
CREATE UNIQUE INDEX idx_plan_review_evidence_active_path ON public.plan_review_evidence USING btree (project_id, plan_path) WHERE ((finalized_at IS NULL) AND (expired_at IS NULL));
CREATE UNIQUE INDEX idx_plan_review_evidence_dispatch_run ON public.plan_review_evidence USING btree (dispatch_run_id) WHERE (dispatch_run_id IS NOT NULL);
CREATE UNIQUE INDEX idx_plan_review_evidence_interactive_round ON public.plan_review_evidence USING btree (session_id, plan_path, round_number) WHERE ((session_id IS NOT NULL) AND (expired_at IS NULL));
CREATE INDEX idx_plan_review_evidence_project_path ON public.plan_review_evidence USING btree (project_id, plan_path, created_at);
CREATE UNIQUE INDEX idx_plan_review_evidence_stage_round ON public.plan_review_evidence USING btree (task_id, stage, round_number) WHERE ((task_id IS NOT NULL) AND (expired_at IS NULL));
CREATE INDEX idx_plans_project_state ON public.plans USING btree (project_id, state);
CREATE INDEX idx_plans_root_task ON public.plans USING btree (root_task_ref);
CREATE INDEX idx_plans_state ON public.plans USING btree (state);
CREATE INDEX idx_project_lifecycle_events_project ON public.project_lifecycle_events USING btree (project_id, created_at);
CREATE UNIQUE INDEX idx_projects_active_name ON public.projects USING btree (name) WHERE (deleted_at IS NULL);
CREATE INDEX idx_projects_name ON public.projects USING btree (name);
CREATE INDEX idx_prompts_name ON public.prompts USING btree (name);
CREATE INDEX idx_prompts_project ON public.prompts USING btree (project_id);
CREATE INDEX idx_prompts_scope ON public.prompts USING btree (scope);
CREATE INDEX idx_recall_injection_outcomes_memory ON public.recall_injection_outcomes USING btree (memory_id);
CREATE INDEX idx_recall_injection_outcomes_session ON public.recall_injection_outcomes USING btree (session_id);
CREATE INDEX idx_recall_signal_hits_memory ON public.recall_signal_hits USING btree (memory_id);
CREATE INDEX idx_recall_signal_hits_session ON public.recall_signal_hits USING btree (session_id);
CREATE INDEX idx_recall_signal_requests_project_created ON public.recall_signal_requests USING btree (project_id, created_at);
CREATE UNIQUE INDEX idx_recall_signal_requests_request_id ON public.recall_signal_requests USING btree (recall_request_id);
CREATE INDEX idx_recall_usefulness_memory ON public.recall_usefulness USING btree (memory_id);
CREATE INDEX idx_recall_usefulness_request_source_protocol ON public.recall_usefulness USING btree (recall_request_id, label_source, judge_protocol_version);
CREATE INDEX idx_recall_usefulness_session ON public.recall_usefulness USING btree (session_id);
CREATE INDEX idx_rule_defs_event ON public.rule_definitions USING btree (((definition_json ->> 'event'::text))) WHERE (deleted_at IS NULL);
CREATE INDEX idx_rule_defs_project ON public.rule_definitions USING btree (project_id);
CREATE INDEX idx_schema_hashes_project ON public.tool_schema_hashes USING btree (project_id);
CREATE INDEX idx_schema_hashes_server ON public.tool_schema_hashes USING btree (server_name);
CREATE INDEX idx_schema_hashes_verified ON public.tool_schema_hashes USING btree (last_verified_at);
CREATE INDEX idx_secrets_category ON public.secrets USING btree (category);
CREATE UNIQUE INDEX idx_secrets_name_project ON public.secrets USING btree (name, project_id);
CREATE INDEX idx_session_feedback_review_run ON public.session_feedback USING btree (review_run_id) WHERE (review_run_id IS NOT NULL);
CREATE INDEX idx_session_feedback_session_created ON public.session_feedback USING btree (session_id, created_at DESC);
CREATE INDEX idx_session_feedback_unreviewed ON public.session_feedback USING btree (created_at) WHERE (reviewed = false);
CREATE INDEX idx_session_skills_session ON public.session_skills USING btree (session_id);
CREATE UNIQUE INDEX idx_session_skills_unique ON public.session_skills USING btree (session_id, skill_name);
CREATE INDEX idx_session_summary_revisions_previous ON public.session_summary_revisions USING btree (previous_revision_id);
CREATE INDEX idx_session_summary_revisions_session_created ON public.session_summary_revisions USING btree (session_id, created_at DESC);
CREATE INDEX idx_session_tasks_session ON public.session_tasks USING btree (session_id);
CREATE INDEX idx_session_tasks_task ON public.session_tasks USING btree (task_id);
CREATE INDEX idx_session_var_defs_project ON public.session_variable_defaults USING btree (project_id);
CREATE INDEX idx_sessions_agent_depth ON public.sessions USING btree (agent_depth);
CREATE INDEX idx_sessions_agent_run ON public.sessions USING btree (agent_run_id);
CREATE INDEX idx_sessions_context_usage_ratio ON public.sessions USING btree (context_usage_ratio DESC) WHERE (context_usage_ratio IS NOT NULL);
CREATE INDEX idx_sessions_external_id ON public.sessions USING btree (external_id);
CREATE INDEX idx_sessions_machine_id ON public.sessions USING btree (machine_id);
CREATE INDEX idx_sessions_parent_session ON public.sessions USING btree (parent_session_id);
CREATE INDEX idx_sessions_pending_transcript ON public.sessions USING btree (status, transcript_processed) WHERE ((status = 'expired'::text) AND (transcript_processed = false));
CREATE INDEX idx_sessions_project_id ON public.sessions USING btree (project_id);
CREATE INDEX idx_sessions_prune_status_updated_at ON public.sessions USING btree (status, updated_at);
CREATE UNIQUE INDEX idx_sessions_seq_num ON public.sessions USING btree (project_id, seq_num);
CREATE INDEX idx_sessions_source ON public.sessions USING btree (source);
CREATE INDEX idx_sessions_spawned_by ON public.sessions USING btree (spawned_by_agent_id);
CREATE INDEX idx_sessions_status ON public.sessions USING btree (status);
CREATE INDEX idx_sessions_status_last_activity ON public.sessions USING btree (status, last_activity);
CREATE INDEX idx_sessions_summary_revision ON public.sessions USING btree (summary_revision_id);
CREATE UNIQUE INDEX idx_sessions_unique ON public.sessions USING btree (external_id, source, project_id, session_type) NULLS NOT DISTINCT;
CREATE INDEX idx_sessions_workflow ON public.sessions USING btree (workflow_name);
CREATE INDEX idx_skill_files_skill_id ON public.skill_files USING btree (skill_id);
CREATE INDEX idx_skill_files_type ON public.skill_files USING btree (file_type);
CREATE INDEX idx_skills_always_apply ON public.skills USING btree (always_apply);
CREATE INDEX idx_skills_deleted_at ON public.skills USING btree (deleted_at);
CREATE INDEX idx_skills_enabled ON public.skills USING btree (enabled);
CREATE INDEX idx_skills_name ON public.skills USING btree (name);
CREATE INDEX idx_skills_project_id ON public.skills USING btree (project_id);
CREATE INDEX idx_spans_start_time ON public.spans USING btree (start_time_ns);
CREATE INDEX idx_spans_trace_id ON public.spans USING btree (trace_id);
CREATE INDEX idx_step_executions_approval_token ON public.step_executions USING btree (approval_token);
CREATE INDEX idx_step_executions_execution ON public.step_executions USING btree (execution_id);
CREATE INDEX idx_stop_signals_pending ON public.session_stop_signals USING btree (acknowledged_at) WHERE (acknowledged_at IS NULL);
CREATE INDEX idx_taf_file_path ON public.task_affected_files USING btree (file_path);
CREATE INDEX idx_taf_task_id ON public.task_affected_files USING btree (task_id);
CREATE INDEX idx_task_close_reviews_recovery ON public.task_close_reviews USING btree (status, delivered_at, updated_at);
CREATE INDEX idx_task_comments_created ON public.task_comments USING btree (task_id, created_at);
CREATE INDEX idx_task_comments_parent ON public.task_comments USING btree (parent_comment_id);
CREATE INDEX idx_task_comments_task ON public.task_comments USING btree (task_id);
CREATE INDEX idx_task_delivery_units_pr_url ON public.task_delivery_units USING btree (pr_url);
CREATE INDEX idx_task_delivery_units_task_id ON public.task_delivery_units USING btree (task_id);
CREATE INDEX idx_task_selection_session ON public.task_selection_history USING btree (session_id, selected_at DESC);
CREATE INDEX idx_task_selection_task ON public.task_selection_history USING btree (session_id, task_id, selected_at DESC);
CREATE INDEX idx_task_stage_states_open ON public.task_stage_states USING btree (task_id, "position") WHERE (state <> 'done'::text);
CREATE UNIQUE INDEX idx_task_stage_states_position ON public.task_stage_states USING btree (task_id, "position");
CREATE INDEX idx_task_stage_states_state ON public.task_stage_states USING btree (stage_name, state);
CREATE INDEX idx_task_stages_registry_deleted ON public.task_stages_registry USING btree (deleted_at);
CREATE INDEX idx_task_type_default_stages_position ON public.task_type_default_stages USING btree (task_type, "position");
CREATE INDEX idx_tasks_claimed_session ON public.tasks USING btree (claimed_by_session_id);
CREATE INDEX idx_tasks_closed_session ON public.tasks USING btree (closed_in_session_id);
CREATE INDEX idx_tasks_created_session ON public.tasks USING btree (created_in_session_id);
CREATE INDEX idx_tasks_dispatch_scan ON public.tasks USING btree (allow_automation, closed_at, is_escalated);
CREATE UNIQUE INDEX idx_tasks_github_issue_link ON public.tasks USING btree (project_id, github_repo, github_issue_number) WHERE ((github_repo IS NOT NULL) AND (github_issue_number IS NOT NULL));
CREATE INDEX idx_tasks_parent ON public.tasks USING btree (parent_task_id);
CREATE INDEX idx_tasks_path_cache ON public.tasks USING btree (path_cache);
CREATE INDEX idx_tasks_project ON public.tasks USING btree (project_id);
CREATE UNIQUE INDEX idx_tasks_seq_num ON public.tasks USING btree (project_id, seq_num);
CREATE INDEX idx_tasks_state_bucket ON public.tasks USING btree (state_bucket);
CREATE INDEX idx_terminals_live ON public.terminals USING btree (state) WHERE (state = ANY (ARRAY['pending'::text, 'live'::text]));
CREATE UNIQUE INDEX idx_terminals_locator_key_active ON public.terminals USING btree (locator_key) WHERE ((locator_key IS NOT NULL) AND (state = ANY (ARRAY['pending'::text, 'live'::text])));
CREATE INDEX idx_terminals_machine ON public.terminals USING btree (machine_id);
CREATE INDEX idx_terminals_project ON public.terminals USING btree (project_id);
CREATE INDEX idx_terminals_run ON public.terminals USING btree (agent_run_id) WHERE (agent_run_id IS NOT NULL);
CREATE UNIQUE INDEX idx_terminals_spawn_key ON public.terminals USING btree (spawn_key) WHERE (spawn_key IS NOT NULL);
CREATE UNIQUE INDEX idx_token_events_dedup ON public.token_events USING btree (session_id, message_id) WHERE (message_id IS NOT NULL);
CREATE INDEX idx_token_events_session ON public.token_events USING btree (session_id, event_at);
CREATE INDEX idx_tool_metrics_call_count ON public.tool_metrics USING btree (call_count DESC);
CREATE INDEX idx_tool_metrics_daily_date ON public.tool_metrics_daily USING btree (date);
CREATE INDEX idx_tool_metrics_daily_project ON public.tool_metrics_daily USING btree (project_id);
CREATE INDEX idx_tool_metrics_daily_server ON public.tool_metrics_daily USING btree (server_name);
CREATE INDEX idx_tool_metrics_last_called ON public.tool_metrics USING btree (last_called_at);
CREATE INDEX idx_tool_metrics_project ON public.tool_metrics USING btree (project_id);
CREATE INDEX idx_tool_metrics_server ON public.tool_metrics USING btree (server_name);
CREATE INDEX idx_tool_metrics_tool ON public.tool_metrics USING btree (tool_name);
CREATE INDEX idx_tool_results_created ON public.tool_results USING btree (created_at);
CREATE INDEX idx_tools_name ON public.tools USING btree (name);
CREATE INDEX idx_tools_server_id ON public.tools USING btree (mcp_server_id);
CREATE INDEX idx_unmodeled_observation_events_group_recompute ON public.unmodeled_observation_events USING btree (source, kind, name, server_name, tool_type, last_seen_at DESC, first_seen_at DESC);
CREATE INDEX idx_unmodeled_observation_events_last_seen ON public.unmodeled_observation_events USING btree (last_seen_at);
CREATE INDEX idx_unmodeled_observations_worklist ON public.unmodeled_observations USING btree (count DESC, last_seen_at DESC);
CREATE INDEX idx_validation_history_task ON public.task_validation_history USING btree (task_id);
CREATE UNIQUE INDEX idx_worktrees_branch ON public.worktrees USING btree (project_id, branch_name, machine_id);
CREATE UNIQUE INDEX idx_worktrees_path ON public.worktrees USING btree (machine_id, worktree_path);
CREATE INDEX idx_worktrees_project ON public.worktrees USING btree (project_id);
CREATE INDEX idx_worktrees_session ON public.worktrees USING btree (agent_session_id);
CREATE INDEX idx_worktrees_status ON public.worktrees USING btree (status);
CREATE INDEX idx_worktrees_task ON public.worktrees USING btree (task_id);
CREATE UNIQUE INDEX maintenance_epochs_one_open ON public.maintenance_epochs USING btree ((true)) WHERE (released_at IS NULL);
CREATE INDEX maintenance_epochs_open_lookup ON public.maintenance_epochs USING btree (opened_at, id) WHERE (released_at IS NULL);
CREATE INDEX memories_search_bm25 ON public.memories USING bm25 (id, content, tags_text) WITH (key_field=id);
CREATE INDEX skills_search_bm25 ON public.skills USING bm25 (id, name, description, content) WITH (key_field=id);
CREATE INDEX tasks_search_bm25 ON public.tasks USING bm25 (id, title, description) WITH (key_field=id);
CREATE INDEX tool_result_chunks_search_bm25 ON public.tool_result_chunks USING bm25 (id, content) WITH (key_field=id);
CREATE UNIQUE INDEX uq_agent_defs_live_name ON public.agent_definitions USING btree (name, project_id) NULLS NOT DISTINCT WHERE (deleted_at IS NULL);
CREATE UNIQUE INDEX uq_pipeline_defs_live_name ON public.pipeline_definitions USING btree (name, project_id) NULLS NOT DISTINCT WHERE (deleted_at IS NULL);
CREATE UNIQUE INDEX uq_rule_defs_live_name ON public.rule_definitions USING btree (name, project_id) NULLS NOT DISTINCT WHERE (deleted_at IS NULL);
CREATE UNIQUE INDEX uq_session_var_defs_live_name ON public.session_variable_defaults USING btree (name, project_id) NULLS NOT DISTINCT WHERE (deleted_at IS NULL);
CREATE UNIQUE INDEX uq_task_close_reviews_active_task ON public.task_close_reviews USING btree (task_id) WHERE (status = ANY (ARRAY['launching'::text, 'running'::text, 'finalizing'::text]));
CREATE UNIQUE INDEX uq_task_close_reviews_agent_run ON public.task_close_reviews USING btree (agent_run_id) WHERE (agent_run_id IS NOT NULL);
CREATE UNIQUE INDEX users_email_lower_key ON public.users USING btree (lower(email));
CREATE TRIGGER principal_lifetime_guard BEFORE INSERT OR UPDATE OF issued_at, expires_at ON gobby_agent_auth.principal_bindings FOR EACH ROW EXECUTE FUNCTION gobby_agent_auth.enforce_principal_lifetime();
CREATE TRIGGER task_stage_states_state_bucket_ad AFTER DELETE ON public.task_stage_states FOR EACH ROW EXECUTE FUNCTION public.refresh_task_state_bucket_from_stage();
CREATE TRIGGER task_stage_states_state_bucket_ai AFTER INSERT ON public.task_stage_states FOR EACH ROW EXECUTE FUNCTION public.refresh_task_state_bucket_from_stage();
CREATE TRIGGER task_stage_states_state_bucket_au AFTER UPDATE OF state, "position" ON public.task_stage_states FOR EACH ROW EXECUTE FUNCTION public.refresh_task_state_bucket_from_stage();
CREATE TRIGGER tasks_state_bucket_ai AFTER INSERT ON public.tasks FOR EACH ROW EXECUTE FUNCTION public.refresh_task_state_bucket_from_task();
CREATE TRIGGER tasks_state_bucket_au AFTER UPDATE OF closed_at, escalated_at, is_escalated ON public.tasks FOR EACH ROW EXECUTE FUNCTION public.refresh_task_state_bucket_from_task();
CREATE TRIGGER trg_chat_attachments_bound_at_write_once BEFORE UPDATE OF bound_at ON public.chat_attachments FOR EACH ROW EXECUTE FUNCTION public.enforce_chat_attachments_bound_at_write_once();
CREATE TRIGGER trg_chat_attachments_updated_at_touch BEFORE UPDATE ON public.chat_attachments FOR EACH ROW EXECUTE FUNCTION public.touch_chat_attachments_updated_at();
ALTER TABLE ONLY gobby_agent_auth.daemon_registry
    ADD CONSTRAINT daemon_registry_machine_id_fkey FOREIGN KEY (machine_id) REFERENCES public.machines(id) ON DELETE CASCADE;
ALTER TABLE ONLY gobby_agent_auth.principal_audit_events
    ADD CONSTRAINT principal_audit_events_binding_id_fkey FOREIGN KEY (binding_id) REFERENCES gobby_agent_auth.principal_bindings(id) ON DELETE SET NULL;
ALTER TABLE ONLY public.agent_definitions
    ADD CONSTRAINT agent_definitions_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.agent_runs
    ADD CONSTRAINT agent_runs_child_session_id_fkey FOREIGN KEY (child_session_id) REFERENCES public.sessions(id) DEFERRABLE;
ALTER TABLE ONLY public.agent_runs
    ADD CONSTRAINT agent_runs_claimed_session_id_fkey FOREIGN KEY (claimed_session_id) REFERENCES public.sessions(id) DEFERRABLE;
ALTER TABLE ONLY public.agent_runs
    ADD CONSTRAINT agent_runs_machine_id_fkey FOREIGN KEY (machine_id) REFERENCES public.machines(id);
ALTER TABLE ONLY public.agent_runs
    ADD CONSTRAINT agent_runs_parent_session_id_fkey FOREIGN KEY (parent_session_id) REFERENCES public.sessions(id) DEFERRABLE;
ALTER TABLE ONLY public.agent_runs
    ADD CONSTRAINT agent_runs_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE SET NULL DEFERRABLE;
ALTER TABLE ONLY public.agent_runs
    ADD CONSTRAINT agent_runs_terminal_id_fkey FOREIGN KEY (terminal_id) REFERENCES public.terminals(id);
ALTER TABLE ONLY public.agent_step_instances
    ADD CONSTRAINT agent_step_instances_agent_step_workflow_id_fkey FOREIGN KEY (agent_step_workflow_id) REFERENCES public.agent_step_workflows(id) ON DELETE SET NULL;
ALTER TABLE ONLY public.agent_step_instances
    ADD CONSTRAINT agent_step_instances_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.agent_step_workflows
    ADD CONSTRAINT agent_step_workflows_agent_definition_id_fkey FOREIGN KEY (agent_definition_id) REFERENCES public.agent_definitions(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.auth_sessions
    ADD CONSTRAINT auth_sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.bin_update_state
    ADD CONSTRAINT bin_update_state_machine_id_fkey FOREIGN KEY (machine_id) REFERENCES public.machines(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.build_history_events
    ADD CONSTRAINT build_history_events_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.build_history_events
    ADD CONSTRAINT build_history_events_root_task_id_fkey FOREIGN KEY (root_task_id) REFERENCES public.tasks(id) ON DELETE SET NULL DEFERRABLE;
ALTER TABLE ONLY public.build_history_events
    ADD CONSTRAINT build_history_events_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.build_runs(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.build_history_events
    ADD CONSTRAINT build_history_events_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE SET NULL DEFERRABLE;
ALTER TABLE ONLY public.build_profiles
    ADD CONSTRAINT build_profiles_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.build_runs
    ADD CONSTRAINT build_runs_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.build_runs
    ADD CONSTRAINT build_runs_root_task_id_fkey FOREIGN KEY (root_task_id) REFERENCES public.tasks(id) ON DELETE SET NULL DEFERRABLE;
ALTER TABLE ONLY public.chat_attachments
    ADD CONSTRAINT chat_attachments_machine_id_fkey FOREIGN KEY (machine_id) REFERENCES public.machines(id);
ALTER TABLE ONLY public.chat_attachments
    ADD CONSTRAINT chat_attachments_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.chat_attachments
    ADD CONSTRAINT chat_attachments_target_session_id_fkey FOREIGN KEY (target_session_id) REFERENCES public.sessions(id) ON DELETE SET NULL DEFERRABLE;
ALTER TABLE ONLY public.checkpoints
    ADD CONSTRAINT checkpoints_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.agent_runs(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.checkpoints
    ADD CONSTRAINT checkpoints_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.checkpoints
    ADD CONSTRAINT checkpoints_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.clones
    ADD CONSTRAINT clones_agent_session_id_machine_id_fkey FOREIGN KEY (agent_session_id, machine_id) REFERENCES public.sessions(id, machine_id) ON DELETE SET NULL (agent_session_id) DEFERRABLE;
ALTER TABLE ONLY public.clones
    ADD CONSTRAINT clones_machine_id_fkey FOREIGN KEY (machine_id) REFERENCES public.machines(id);
ALTER TABLE ONLY public.clones
    ADD CONSTRAINT clones_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.clones
    ADD CONSTRAINT clones_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE SET NULL DEFERRABLE;
ALTER TABLE ONLY public.code_calls
    ADD CONSTRAINT code_calls_content_fkey FOREIGN KEY (project_id, file_path, content_hash) REFERENCES public.code_indexed_files(project_id, file_path, content_hash) ON DELETE CASCADE;
ALTER TABLE ONLY public.code_content_chunks
    ADD CONSTRAINT code_content_chunks_content_fkey FOREIGN KEY (project_id, file_path, content_hash) REFERENCES public.code_indexed_files(project_id, file_path, content_hash) ON DELETE CASCADE;
ALTER TABLE ONLY public.code_imports
    ADD CONSTRAINT code_imports_content_fkey FOREIGN KEY (project_id, source_file, content_hash) REFERENCES public.code_indexed_files(project_id, file_path, content_hash) ON DELETE CASCADE;
ALTER TABLE ONLY public.code_index_prune_dirty_projects
    ADD CONSTRAINT code_index_prune_dirty_projects_machine_id_fkey FOREIGN KEY (machine_id) REFERENCES public.machines(id);
ALTER TABLE ONLY public.code_indexed_file_states
    ADD CONSTRAINT code_indexed_file_states_content_fkey FOREIGN KEY (project_id, file_path, content_hash) REFERENCES public.code_indexed_files(project_id, file_path, content_hash);
ALTER TABLE ONLY public.code_indexed_file_states
    ADD CONSTRAINT code_indexed_file_states_project_state_fkey FOREIGN KEY (machine_id, project_id) REFERENCES public.code_indexed_project_states(machine_id, project_id) ON DELETE CASCADE;
ALTER TABLE ONLY public.code_indexed_files
    ADD CONSTRAINT code_indexed_files_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.code_indexed_projects(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.code_indexed_project_states
    ADD CONSTRAINT code_indexed_project_states_machine_id_fkey FOREIGN KEY (machine_id) REFERENCES public.machines(id);
ALTER TABLE ONLY public.code_indexed_project_states
    ADD CONSTRAINT code_indexed_project_states_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.code_indexed_projects(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.code_inheritance
    ADD CONSTRAINT code_inheritance_content_fkey FOREIGN KEY (project_id, file_path, content_hash) REFERENCES public.code_indexed_files(project_id, file_path, content_hash) ON DELETE CASCADE;
ALTER TABLE ONLY public.code_symbols
    ADD CONSTRAINT code_symbols_content_fkey FOREIGN KEY (project_id, file_path, file_content_hash) REFERENCES public.code_indexed_files(project_id, file_path, content_hash) ON DELETE CASCADE;
ALTER TABLE ONLY public.comms_attachments
    ADD CONSTRAINT comms_attachments_machine_id_fkey FOREIGN KEY (machine_id) REFERENCES public.machines(id);
ALTER TABLE ONLY public.comms_attachments
    ADD CONSTRAINT comms_attachments_message_id_fkey FOREIGN KEY (message_id) REFERENCES public.comms_messages(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.comms_identities
    ADD CONSTRAINT comms_identities_channel_id_fkey FOREIGN KEY (channel_id) REFERENCES public.comms_channels(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.comms_identities
    ADD CONSTRAINT comms_identities_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE SET NULL DEFERRABLE;
ALTER TABLE ONLY public.comms_identities
    ADD CONSTRAINT comms_identities_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id) ON DELETE SET NULL DEFERRABLE;
ALTER TABLE ONLY public.comms_messages
    ADD CONSTRAINT comms_messages_channel_id_fkey FOREIGN KEY (channel_id) REFERENCES public.comms_channels(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.comms_messages
    ADD CONSTRAINT comms_messages_identity_id_fkey FOREIGN KEY (identity_id) REFERENCES public.comms_identities(id) ON DELETE SET NULL DEFERRABLE;
ALTER TABLE ONLY public.comms_messages
    ADD CONSTRAINT comms_messages_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id) ON DELETE SET NULL DEFERRABLE;
ALTER TABLE ONLY public.comms_routing_rules
    ADD CONSTRAINT comms_routing_rules_channel_id_fkey FOREIGN KEY (channel_id) REFERENCES public.comms_channels(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.comms_routing_rules
    ADD CONSTRAINT comms_routing_rules_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.comms_routing_rules
    ADD CONSTRAINT comms_routing_rules_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id) ON DELETE SET NULL DEFERRABLE;
ALTER TABLE ONLY public.cron_jobs
    ADD CONSTRAINT cron_jobs_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.cron_runs
    ADD CONSTRAINT cron_runs_cron_job_id_fkey FOREIGN KEY (cron_job_id) REFERENCES public.cron_jobs(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.cron_runs
    ADD CONSTRAINT cron_runs_machine_id_fkey FOREIGN KEY (machine_id) REFERENCES public.machines(id);
ALTER TABLE ONLY public.destructive_batches
    ADD CONSTRAINT destructive_batches_maintenance_epoch_id_fkey FOREIGN KEY (maintenance_epoch_id) REFERENCES public.maintenance_epochs(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE ONLY public.expansion_runs
    ADD CONSTRAINT expansion_runs_parent_task_id_fkey FOREIGN KEY (parent_task_id) REFERENCES public.tasks(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.expansion_runs
    ADD CONSTRAINT expansion_runs_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.expansion_runs
    ADD CONSTRAINT expansion_runs_triggering_session_id_fkey FOREIGN KEY (triggering_session_id) REFERENCES public.sessions(id) ON DELETE SET NULL DEFERRABLE;
ALTER TABLE ONLY public.external_issue_sync_status
    ADD CONSTRAINT external_issue_sync_status_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.gh_issues_triaged
    ADD CONSTRAINT gh_issues_triaged_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.gh_issues_triaged
    ADD CONSTRAINT gh_issues_triaged_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE SET NULL DEFERRABLE;
ALTER TABLE ONLY public.gh_triage_build_dispatches
    ADD CONSTRAINT gh_triage_build_dispatches_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.gh_triage_build_dispatches
    ADD CONSTRAINT gh_triage_build_dispatches_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.gh_triage_deliveries
    ADD CONSTRAINT gh_triage_deliveries_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.inter_session_messages
    ADD CONSTRAINT inter_session_messages_from_session_fkey FOREIGN KEY (from_session) REFERENCES public.sessions(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.inter_session_messages
    ADD CONSTRAINT inter_session_messages_to_session_fkey FOREIGN KEY (to_session) REFERENCES public.sessions(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.loop_progress
    ADD CONSTRAINT loop_progress_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.machines
    ADD CONSTRAINT machines_owner_user_id_fkey FOREIGN KEY (owner_user_id) REFERENCES public.users(id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.mcp_server_templates
    ADD CONSTRAINT mcp_server_templates_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.mcp_servers
    ADD CONSTRAINT mcp_servers_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.mcp_servers
    ADD CONSTRAINT mcp_servers_template_id_fkey FOREIGN KEY (template_id) REFERENCES public.mcp_server_templates(id) ON DELETE SET NULL;
ALTER TABLE ONLY public.memories
    ADD CONSTRAINT memories_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE RESTRICT DEFERRABLE;
ALTER TABLE ONLY public.memories
    ADD CONSTRAINT memories_source_session_id_fkey FOREIGN KEY (source_session_id) REFERENCES public.sessions(id) ON DELETE SET NULL DEFERRABLE;
ALTER TABLE ONLY public.memories
    ADD CONSTRAINT memories_source_task_id_fkey FOREIGN KEY (source_task_id) REFERENCES public.tasks(id) ON DELETE SET NULL DEFERRABLE;
ALTER TABLE ONLY public.memory_crossrefs
    ADD CONSTRAINT memory_crossrefs_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.memories(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.memory_crossrefs
    ADD CONSTRAINT memory_crossrefs_target_id_fkey FOREIGN KEY (target_id) REFERENCES public.memories(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.memory_dream_runs
    ADD CONSTRAINT memory_dream_runs_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.memory_dream_snapshots
    ADD CONSTRAINT memory_dream_snapshots_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.memory_dream_runs(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.merge_conflicts
    ADD CONSTRAINT merge_conflicts_resolution_id_fkey FOREIGN KEY (resolution_id) REFERENCES public.merge_resolutions(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.merge_resolutions
    ADD CONSTRAINT merge_resolutions_worktree_id_fkey FOREIGN KEY (worktree_id) REFERENCES public.worktrees(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.pending_interactions
    ADD CONSTRAINT pending_interactions_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.pipeline_definitions
    ADD CONSTRAINT pipeline_definitions_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.pipeline_executions
    ADD CONSTRAINT pipeline_executions_parent_execution_id_fkey FOREIGN KEY (parent_execution_id) REFERENCES public.pipeline_executions(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.pipeline_executions
    ADD CONSTRAINT pipeline_executions_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.pipeline_executions
    ADD CONSTRAINT pipeline_executions_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id) ON DELETE SET NULL DEFERRABLE;
ALTER TABLE ONLY public.plan_review_evidence
    ADD CONSTRAINT plan_review_evidence_dispatch_run_id_fkey FOREIGN KEY (dispatch_run_id) REFERENCES public.agent_runs(id) ON DELETE RESTRICT DEFERRABLE;
ALTER TABLE ONLY public.plan_review_evidence
    ADD CONSTRAINT plan_review_evidence_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.plan_review_evidence
    ADD CONSTRAINT plan_review_evidence_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.plan_review_evidence
    ADD CONSTRAINT plan_review_evidence_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.plans
    ADD CONSTRAINT plans_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) DEFERRABLE;
ALTER TABLE ONLY public.project_checkouts
    ADD CONSTRAINT project_checkouts_machine_id_fkey FOREIGN KEY (machine_id) REFERENCES public.machines(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.project_checkouts
    ADD CONSTRAINT project_checkouts_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.project_github_triage_configs
    ADD CONSTRAINT project_github_triage_configs_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.project_lifecycle_events
    ADD CONSTRAINT project_lifecycle_events_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.prompts
    ADD CONSTRAINT prompts_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.provider_capacity_snapshots
    ADD CONSTRAINT provider_capacity_snapshots_machine_id_fkey FOREIGN KEY (machine_id) REFERENCES public.machines(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.provider_model_routes
    ADD CONSTRAINT provider_model_routes_capability_fkey FOREIGN KEY (provider, canonical_model) REFERENCES public.provider_model_capabilities(provider, canonical_model) ON DELETE CASCADE;
ALTER TABLE ONLY public.recall_holdout_consumed
    ADD CONSTRAINT recall_holdout_consumed_holdout_consumption_key_fkey FOREIGN KEY (holdout_consumption_key) REFERENCES public.recall_gate_runs(holdout_consumption_key) ON DELETE RESTRICT;
ALTER TABLE ONLY public.rule_definitions
    ADD CONSTRAINT rule_definitions_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.secrets
    ADD CONSTRAINT secrets_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.session_feedback
    ADD CONSTRAINT session_feedback_review_run_id_fkey FOREIGN KEY (review_run_id) REFERENCES public.feedback_review_runs(id) ON DELETE SET NULL;
ALTER TABLE ONLY public.session_feedback
    ADD CONSTRAINT session_feedback_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.session_skills
    ADD CONSTRAINT session_skills_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.session_stop_signals
    ADD CONSTRAINT session_stop_signals_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.session_summary_revisions
    ADD CONSTRAINT session_summary_revisions_previous_same_session_fk FOREIGN KEY (previous_revision_id, session_id) REFERENCES public.session_summary_revisions(id, session_id) ON DELETE SET NULL (previous_revision_id) DEFERRABLE;
ALTER TABLE ONLY public.session_summary_revisions
    ADD CONSTRAINT session_summary_revisions_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.session_tasks
    ADD CONSTRAINT session_tasks_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.session_tasks
    ADD CONSTRAINT session_tasks_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.session_variable_defaults
    ADD CONSTRAINT session_variable_defaults_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.session_variables
    ADD CONSTRAINT session_variables_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_agent_run_id_fkey FOREIGN KEY (agent_run_id) REFERENCES public.agent_runs(id) ON DELETE SET NULL DEFERRABLE;
ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_machine_id_fkey FOREIGN KEY (machine_id) REFERENCES public.machines(id);
ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_parent_session_id_fkey FOREIGN KEY (parent_session_id) REFERENCES public.sessions(id) DEFERRABLE;
ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) DEFERRABLE;
ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_summary_revision_fk FOREIGN KEY (summary_revision_id, id) REFERENCES public.session_summary_revisions(id, session_id) ON DELETE SET NULL (summary_revision_id) DEFERRABLE;
ALTER TABLE ONLY public.skill_files
    ADD CONSTRAINT skill_files_skill_id_fkey FOREIGN KEY (skill_id) REFERENCES public.skills(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.skills
    ADD CONSTRAINT skills_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.step_executions
    ADD CONSTRAINT step_executions_execution_id_fkey FOREIGN KEY (execution_id) REFERENCES public.pipeline_executions(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.task_affected_files
    ADD CONSTRAINT task_affected_files_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.task_artifacts
    ADD CONSTRAINT task_artifacts_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.task_comments
    ADD CONSTRAINT task_comments_parent_comment_id_fkey FOREIGN KEY (parent_comment_id) REFERENCES public.task_comments(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.task_comments
    ADD CONSTRAINT task_comments_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.task_delivery_campaigns
    ADD CONSTRAINT task_delivery_campaigns_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.task_delivery_units
    ADD CONSTRAINT task_delivery_units_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.task_dependencies
    ADD CONSTRAINT task_dependencies_depends_on_fkey FOREIGN KEY (depends_on) REFERENCES public.tasks(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.task_dependencies
    ADD CONSTRAINT task_dependencies_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.task_dispatch_mutex
    ADD CONSTRAINT task_dispatch_mutex_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.task_lifecycle_events
    ADD CONSTRAINT task_lifecycle_events_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.task_selection_history
    ADD CONSTRAINT task_selection_history_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.task_stage_states
    ADD CONSTRAINT task_stage_states_stage_name_fkey FOREIGN KEY (stage_name) REFERENCES public.task_stages_registry(name) ON DELETE RESTRICT DEFERRABLE;
ALTER TABLE ONLY public.task_stage_states
    ADD CONSTRAINT task_stage_states_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.task_type_default_stages
    ADD CONSTRAINT task_type_default_stages_stage_name_fkey FOREIGN KEY (stage_name) REFERENCES public.task_stages_registry(name) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.task_validation_backoff
    ADD CONSTRAINT task_validation_backoff_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.task_validation_history
    ADD CONSTRAINT task_validation_history_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_claimed_by_session_id_fkey FOREIGN KEY (claimed_by_session_id) REFERENCES public.sessions(id) ON DELETE RESTRICT DEFERRABLE;
ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_closed_in_session_id_fkey FOREIGN KEY (closed_in_session_id) REFERENCES public.sessions(id) DEFERRABLE;
ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_created_in_session_id_fkey FOREIGN KEY (created_in_session_id) REFERENCES public.sessions(id) DEFERRABLE;
ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_parent_task_id_fkey FOREIGN KEY (parent_task_id) REFERENCES public.tasks(id) DEFERRABLE;
ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) DEFERRABLE;
ALTER TABLE ONLY public.terminals
    ADD CONSTRAINT terminals_agent_run_id_fkey FOREIGN KEY (agent_run_id) REFERENCES public.agent_runs(id) ON DELETE SET NULL DEFERRABLE;
ALTER TABLE ONLY public.terminals
    ADD CONSTRAINT terminals_machine_id_fkey FOREIGN KEY (machine_id) REFERENCES public.machines(id);
ALTER TABLE ONLY public.terminals
    ADD CONSTRAINT terminals_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);
ALTER TABLE ONLY public.terminals
    ADD CONSTRAINT terminals_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id) ON DELETE SET NULL DEFERRABLE;
ALTER TABLE ONLY public.token_events
    ADD CONSTRAINT token_events_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.tool_metrics_daily
    ADD CONSTRAINT tool_metrics_daily_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.tool_metrics
    ADD CONSTRAINT tool_metrics_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.tool_result_chunks
    ADD CONSTRAINT tool_result_chunks_result_id_fkey FOREIGN KEY (result_id) REFERENCES public.tool_results(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.tool_results
    ADD CONSTRAINT tool_results_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.tools
    ADD CONSTRAINT tools_mcp_server_id_fkey FOREIGN KEY (mcp_server_id) REFERENCES public.mcp_servers(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.workflow_audit_log
    ADD CONSTRAINT workflow_audit_log_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id) DEFERRABLE;
ALTER TABLE ONLY public.worktrees
    ADD CONSTRAINT worktrees_agent_session_id_machine_id_fkey FOREIGN KEY (agent_session_id, machine_id) REFERENCES public.sessions(id, machine_id) ON DELETE SET NULL (agent_session_id) DEFERRABLE;
ALTER TABLE ONLY public.worktrees
    ADD CONSTRAINT worktrees_machine_id_fkey FOREIGN KEY (machine_id) REFERENCES public.machines(id);
ALTER TABLE ONLY public.worktrees
    ADD CONSTRAINT worktrees_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE DEFERRABLE;
ALTER TABLE ONLY public.worktrees
    ADD CONSTRAINT worktrees_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE SET NULL DEFERRABLE;
ALTER TABLE public.code_calls ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.code_content_chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.code_imports ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.code_index_projection_cleanup_pending ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.code_index_prune_dirty_projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.code_indexed_file_states ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.code_indexed_files ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.code_indexed_project_states ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.code_indexed_projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.code_inheritance ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.code_symbols ENABLE ROW LEVEL SECURITY;
CREATE POLICY gobby_agent_project_scope ON public.projects TO gobby_gcode_capability USING ((id = gobby_agent_auth.current_project_id())) WITH CHECK ((id = gobby_agent_auth.current_project_id()));
CREATE POLICY gobby_daemon_runtime_access ON public.code_calls TO gobby_daemon_runtime USING (true) WITH CHECK (true);
CREATE POLICY gobby_daemon_runtime_access ON public.code_content_chunks TO gobby_daemon_runtime USING (true) WITH CHECK (true);
CREATE POLICY gobby_daemon_runtime_access ON public.code_imports TO gobby_daemon_runtime USING (true) WITH CHECK (true);
CREATE POLICY gobby_daemon_runtime_access ON public.code_index_projection_cleanup_pending TO gobby_daemon_runtime USING (true) WITH CHECK (true);
CREATE POLICY gobby_daemon_runtime_access ON public.code_index_prune_dirty_projects TO gobby_daemon_runtime USING (true) WITH CHECK (true);
CREATE POLICY gobby_daemon_runtime_access ON public.code_indexed_file_states TO gobby_daemon_runtime USING (true) WITH CHECK (true);
CREATE POLICY gobby_daemon_runtime_access ON public.code_indexed_files TO gobby_daemon_runtime USING (true) WITH CHECK (true);
CREATE POLICY gobby_daemon_runtime_access ON public.code_indexed_project_states TO gobby_daemon_runtime USING (true) WITH CHECK (true);
CREATE POLICY gobby_daemon_runtime_access ON public.code_indexed_projects TO gobby_daemon_runtime USING (true) WITH CHECK (true);
CREATE POLICY gobby_daemon_runtime_access ON public.code_inheritance TO gobby_daemon_runtime USING (true) WITH CHECK (true);
CREATE POLICY gobby_daemon_runtime_access ON public.code_symbols TO gobby_daemon_runtime USING (true) WITH CHECK (true);
CREATE POLICY gobby_daemon_runtime_access ON public.project_checkouts TO gobby_daemon_runtime USING (true) WITH CHECK (true);
CREATE POLICY gobby_daemon_runtime_access ON public.projects TO gobby_daemon_runtime USING (true) WITH CHECK (true);
CREATE POLICY gobby_gcode_project_delete ON public.code_calls FOR DELETE TO gobby_gcode_capability USING ((project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id())));
CREATE POLICY gobby_gcode_project_delete ON public.code_content_chunks FOR DELETE TO gobby_gcode_capability USING ((project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id())));
CREATE POLICY gobby_gcode_project_delete ON public.code_imports FOR DELETE TO gobby_gcode_capability USING ((project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id())));
CREATE POLICY gobby_gcode_project_delete ON public.code_index_projection_cleanup_pending FOR DELETE TO gobby_gcode_capability USING ((project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id())));
CREATE POLICY gobby_gcode_project_delete ON public.code_index_prune_dirty_projects FOR DELETE TO gobby_gcode_capability USING (((project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id())) AND (machine_id = gobby_agent_auth.current_machine_id())));
CREATE POLICY gobby_gcode_project_delete ON public.code_indexed_file_states FOR DELETE TO gobby_gcode_capability USING (((project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id())) AND (machine_id = gobby_agent_auth.current_machine_id())));
CREATE POLICY gobby_gcode_project_delete ON public.code_indexed_files FOR DELETE TO gobby_gcode_capability USING ((project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id())));
CREATE POLICY gobby_gcode_project_delete ON public.code_indexed_project_states FOR DELETE TO gobby_gcode_capability USING (((project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id())) AND (machine_id = gobby_agent_auth.current_machine_id())));
CREATE POLICY gobby_gcode_project_delete ON public.code_indexed_projects FOR DELETE TO gobby_gcode_capability USING ((id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id())));
CREATE POLICY gobby_gcode_project_delete ON public.code_inheritance FOR DELETE TO gobby_gcode_capability USING ((project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id())));
CREATE POLICY gobby_gcode_project_delete ON public.code_symbols FOR DELETE TO gobby_gcode_capability USING ((project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id())));
CREATE POLICY gobby_gcode_project_insert ON public.code_calls FOR INSERT TO gobby_gcode_capability WITH CHECK ((project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id())));
CREATE POLICY gobby_gcode_project_insert ON public.code_content_chunks FOR INSERT TO gobby_gcode_capability WITH CHECK ((project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id())));
CREATE POLICY gobby_gcode_project_insert ON public.code_imports FOR INSERT TO gobby_gcode_capability WITH CHECK ((project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id())));
CREATE POLICY gobby_gcode_project_insert ON public.code_index_projection_cleanup_pending FOR INSERT TO gobby_gcode_capability WITH CHECK ((project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id())));
CREATE POLICY gobby_gcode_project_insert ON public.code_index_prune_dirty_projects FOR INSERT TO gobby_gcode_capability WITH CHECK (((project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id())) AND (machine_id = gobby_agent_auth.current_machine_id())));
CREATE POLICY gobby_gcode_project_insert ON public.code_indexed_file_states FOR INSERT TO gobby_gcode_capability WITH CHECK (((project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id())) AND (machine_id = gobby_agent_auth.current_machine_id())));
CREATE POLICY gobby_gcode_project_insert ON public.code_indexed_files FOR INSERT TO gobby_gcode_capability WITH CHECK ((project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id())));
CREATE POLICY gobby_gcode_project_insert ON public.code_indexed_project_states FOR INSERT TO gobby_gcode_capability WITH CHECK (((project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id())) AND (machine_id = gobby_agent_auth.current_machine_id())));
CREATE POLICY gobby_gcode_project_insert ON public.code_indexed_projects FOR INSERT TO gobby_gcode_capability WITH CHECK ((id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id())));
CREATE POLICY gobby_gcode_project_insert ON public.code_inheritance FOR INSERT TO gobby_gcode_capability WITH CHECK ((project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id())));
CREATE POLICY gobby_gcode_project_insert ON public.code_symbols FOR INSERT TO gobby_gcode_capability WITH CHECK ((project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id())));
CREATE POLICY gobby_gcode_project_read ON public.code_calls FOR SELECT TO gobby_gcode_capability USING (((project_id = gobby_agent_auth.current_project_id()) OR (project_id = gobby_agent_auth.current_code_overlay_project_id())));
CREATE POLICY gobby_gcode_project_read ON public.code_content_chunks FOR SELECT TO gobby_gcode_capability USING (((project_id = gobby_agent_auth.current_project_id()) OR (project_id = gobby_agent_auth.current_code_overlay_project_id())));
CREATE POLICY gobby_gcode_project_read ON public.code_imports FOR SELECT TO gobby_gcode_capability USING (((project_id = gobby_agent_auth.current_project_id()) OR (project_id = gobby_agent_auth.current_code_overlay_project_id())));
CREATE POLICY gobby_gcode_project_read ON public.code_index_projection_cleanup_pending FOR SELECT TO gobby_gcode_capability USING (((project_id = gobby_agent_auth.current_project_id()) OR (project_id = gobby_agent_auth.current_code_overlay_project_id())));
CREATE POLICY gobby_gcode_project_read ON public.code_index_prune_dirty_projects FOR SELECT TO gobby_gcode_capability USING ((((project_id = gobby_agent_auth.current_project_id()) OR (project_id = gobby_agent_auth.current_code_overlay_project_id())) AND (machine_id = gobby_agent_auth.current_machine_id())));
CREATE POLICY gobby_gcode_project_read ON public.code_indexed_file_states FOR SELECT TO gobby_gcode_capability USING ((((project_id = gobby_agent_auth.current_project_id()) OR (project_id = gobby_agent_auth.current_code_overlay_project_id())) AND (machine_id = gobby_agent_auth.current_machine_id())));
CREATE POLICY gobby_gcode_project_read ON public.code_indexed_files FOR SELECT TO gobby_gcode_capability USING (((project_id = gobby_agent_auth.current_project_id()) OR (project_id = gobby_agent_auth.current_code_overlay_project_id())));
CREATE POLICY gobby_gcode_project_read ON public.code_indexed_project_states FOR SELECT TO gobby_gcode_capability USING ((((project_id = gobby_agent_auth.current_project_id()) OR (project_id = gobby_agent_auth.current_code_overlay_project_id())) AND (machine_id = gobby_agent_auth.current_machine_id())));
CREATE POLICY gobby_gcode_project_read ON public.code_indexed_projects FOR SELECT TO gobby_gcode_capability USING (((id = gobby_agent_auth.current_project_id()) OR (id = gobby_agent_auth.current_code_overlay_project_id())));
CREATE POLICY gobby_gcode_project_read ON public.code_inheritance FOR SELECT TO gobby_gcode_capability USING (((project_id = gobby_agent_auth.current_project_id()) OR (project_id = gobby_agent_auth.current_code_overlay_project_id())));
CREATE POLICY gobby_gcode_project_read ON public.code_symbols FOR SELECT TO gobby_gcode_capability USING (((project_id = gobby_agent_auth.current_project_id()) OR (project_id = gobby_agent_auth.current_code_overlay_project_id())));
CREATE POLICY gobby_gcode_project_read ON public.project_checkouts FOR SELECT TO gobby_gcode_capability USING (((project_id = gobby_agent_auth.current_project_id()) AND (machine_id = gobby_agent_auth.current_machine_id())));
CREATE POLICY gobby_gcode_project_update ON public.code_calls FOR UPDATE TO gobby_gcode_capability USING ((project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id()))) WITH CHECK ((project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id())));
CREATE POLICY gobby_gcode_project_update ON public.code_content_chunks FOR UPDATE TO gobby_gcode_capability USING ((project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id()))) WITH CHECK ((project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id())));
CREATE POLICY gobby_gcode_project_update ON public.code_imports FOR UPDATE TO gobby_gcode_capability USING ((project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id()))) WITH CHECK ((project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id())));
CREATE POLICY gobby_gcode_project_update ON public.code_index_projection_cleanup_pending FOR UPDATE TO gobby_gcode_capability USING ((project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id()))) WITH CHECK ((project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id())));
CREATE POLICY gobby_gcode_project_update ON public.code_index_prune_dirty_projects FOR UPDATE TO gobby_gcode_capability USING (((project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id())) AND (machine_id = gobby_agent_auth.current_machine_id()))) WITH CHECK (((project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id())) AND (machine_id = gobby_agent_auth.current_machine_id())));
CREATE POLICY gobby_gcode_project_update ON public.code_indexed_file_states FOR UPDATE TO gobby_gcode_capability USING (((project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id())) AND (machine_id = gobby_agent_auth.current_machine_id()))) WITH CHECK (((project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id())) AND (machine_id = gobby_agent_auth.current_machine_id())));
CREATE POLICY gobby_gcode_project_update ON public.code_indexed_files FOR UPDATE TO gobby_gcode_capability USING ((project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id()))) WITH CHECK ((project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id())));
CREATE POLICY gobby_gcode_project_update ON public.code_indexed_project_states FOR UPDATE TO gobby_gcode_capability USING (((project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id())) AND (machine_id = gobby_agent_auth.current_machine_id()))) WITH CHECK (((project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id())) AND (machine_id = gobby_agent_auth.current_machine_id())));
CREATE POLICY gobby_gcode_project_update ON public.code_indexed_projects FOR UPDATE TO gobby_gcode_capability USING ((id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id()))) WITH CHECK ((id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id())));
CREATE POLICY gobby_gcode_project_update ON public.code_inheritance FOR UPDATE TO gobby_gcode_capability USING ((project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id()))) WITH CHECK ((project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id())));
CREATE POLICY gobby_gcode_project_update ON public.code_symbols FOR UPDATE TO gobby_gcode_capability USING ((project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id()))) WITH CHECK ((project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id())));
CREATE POLICY gobby_gcode_project_update ON public.project_checkouts FOR UPDATE TO gobby_gcode_capability USING (((project_id = gobby_agent_auth.current_project_id()) AND (machine_id = gobby_agent_auth.current_machine_id()))) WITH CHECK (false);
CREATE POLICY gobby_migration_owner_access ON public.code_calls TO gobby_test USING (true) WITH CHECK (true);
CREATE POLICY gobby_migration_owner_access ON public.code_content_chunks TO gobby_test USING (true) WITH CHECK (true);
CREATE POLICY gobby_migration_owner_access ON public.code_imports TO gobby_test USING (true) WITH CHECK (true);
CREATE POLICY gobby_migration_owner_access ON public.code_index_projection_cleanup_pending TO gobby_test USING (true) WITH CHECK (true);
CREATE POLICY gobby_migration_owner_access ON public.code_index_prune_dirty_projects TO gobby_test USING (true) WITH CHECK (true);
CREATE POLICY gobby_migration_owner_access ON public.code_indexed_file_states TO gobby_test USING (true) WITH CHECK (true);
CREATE POLICY gobby_migration_owner_access ON public.code_indexed_files TO gobby_test USING (true) WITH CHECK (true);
CREATE POLICY gobby_migration_owner_access ON public.code_indexed_project_states TO gobby_test USING (true) WITH CHECK (true);
CREATE POLICY gobby_migration_owner_access ON public.code_indexed_projects TO gobby_test USING (true) WITH CHECK (true);
CREATE POLICY gobby_migration_owner_access ON public.code_inheritance TO gobby_test USING (true) WITH CHECK (true);
CREATE POLICY gobby_migration_owner_access ON public.code_symbols TO gobby_test USING (true) WITH CHECK (true);
CREATE POLICY gobby_migration_owner_access ON public.project_checkouts TO gobby_test USING (true) WITH CHECK (true);
CREATE POLICY gobby_migration_owner_access ON public.projects TO gobby_test USING (true) WITH CHECK (true);
ALTER TABLE public.project_checkouts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.projects ENABLE ROW LEVEL SECURITY;
GRANT USAGE ON SCHEMA gobby_agent_auth TO gobby_gcode_capability;
GRANT USAGE ON SCHEMA gobby_agent_auth TO gobby_daemon_runtime;
REVOKE USAGE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO gobby_agent_issuer;
GRANT USAGE ON SCHEMA public TO gobby_gcode_capability;
GRANT USAGE ON SCHEMA public TO gobby_daemon_runtime;
REVOKE ALL ON FUNCTION gobby_agent_auth.assert_interactive_overlay_registered(requested_machine_id uuid, requested_project_id uuid, requested_overlay_project_id uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION gobby_agent_auth.cancel_principal_rotation(p_execution_id uuid, p_predecessor_generation integer, p_successor_generation integer) FROM PUBLIC;
GRANT ALL ON FUNCTION gobby_agent_auth.cancel_principal_rotation(p_execution_id uuid, p_predecessor_generation integer, p_successor_generation integer) TO gobby_daemon_runtime;
REVOKE ALL ON FUNCTION gobby_agent_auth.code_index_project_id(root_path text) FROM PUBLIC;
REVOKE ALL ON FUNCTION gobby_agent_auth.current_code_overlay_project_id() FROM PUBLIC;
GRANT ALL ON FUNCTION gobby_agent_auth.current_code_overlay_project_id() TO gobby_gcode_capability;
REVOKE ALL ON FUNCTION gobby_agent_auth.current_machine_id() FROM PUBLIC;
GRANT ALL ON FUNCTION gobby_agent_auth.current_machine_id() TO gobby_gcode_capability;
REVOKE ALL ON FUNCTION gobby_agent_auth.current_project_id() FROM PUBLIC;
GRANT ALL ON FUNCTION gobby_agent_auth.current_project_id() TO gobby_gcode_capability;
REVOKE ALL ON FUNCTION gobby_agent_auth.drain_ephemeral_principals() FROM PUBLIC;
GRANT ALL ON FUNCTION gobby_agent_auth.drain_ephemeral_principals() TO gobby_daemon_runtime;
REVOKE ALL ON FUNCTION gobby_agent_auth.enforce_principal_lifetime() FROM PUBLIC;
REVOKE ALL ON FUNCTION gobby_agent_auth.heartbeat_daemon(p_machine_id uuid, p_lease_duration interval) FROM PUBLIC;
GRANT ALL ON FUNCTION gobby_agent_auth.heartbeat_daemon(p_machine_id uuid, p_lease_duration interval) TO gobby_daemon_runtime;
REVOKE ALL ON FUNCTION gobby_agent_auth.interactive_role_name(requested_deployment_token text, requested_machine_id uuid, requested_project_id uuid, generation integer) FROM PUBLIC;
GRANT ALL ON FUNCTION gobby_agent_auth.interactive_role_name(requested_deployment_token text, requested_machine_id uuid, requested_project_id uuid, generation integer) TO gobby_agent_issuer;
REVOKE ALL ON FUNCTION gobby_agent_auth.issue_maintenance_principal(p_execution_id uuid, p_project_id uuid, p_machine_id uuid, p_expires_at timestamp with time zone, p_password text, p_code_overlay_project_id uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION gobby_agent_auth.issue_maintenance_principal(p_execution_id uuid, p_project_id uuid, p_machine_id uuid, p_expires_at timestamp with time zone, p_password text, p_code_overlay_project_id uuid) TO gobby_daemon_runtime;
REVOKE ALL ON FUNCTION gobby_agent_auth.issue_or_reuse_interactive_principal(requested_deployment_token text, requested_machine_id uuid, requested_project_id uuid, requested_session_id uuid, requested_expires_at timestamp with time zone, requested_password text, requested_overlay_project_id uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION gobby_agent_auth.issue_or_reuse_interactive_principal(requested_deployment_token text, requested_machine_id uuid, requested_project_id uuid, requested_session_id uuid, requested_expires_at timestamp with time zone, requested_password text, requested_overlay_project_id uuid) TO gobby_daemon_runtime;
REVOKE ALL ON FUNCTION gobby_agent_auth.issue_principal(requested_execution_id uuid, requested_owner_kind text, requested_session_id uuid, requested_agent_run_id uuid, requested_machine_id uuid, requested_expires_at timestamp with time zone, requested_password text) FROM PUBLIC;
GRANT ALL ON FUNCTION gobby_agent_auth.issue_principal(requested_execution_id uuid, requested_owner_kind text, requested_session_id uuid, requested_agent_run_id uuid, requested_machine_id uuid, requested_expires_at timestamp with time zone, requested_password text) TO gobby_daemon_runtime;
REVOKE ALL ON FUNCTION gobby_agent_auth.issue_tool_principal(p_execution_id uuid, p_session_id uuid, p_machine_id uuid, p_expires_at timestamp with time zone, p_password text) FROM PUBLIC;
GRANT ALL ON FUNCTION gobby_agent_auth.issue_tool_principal(p_execution_id uuid, p_session_id uuid, p_machine_id uuid, p_expires_at timestamp with time zone, p_password text) TO gobby_daemon_runtime;
REVOKE ALL ON FUNCTION gobby_agent_auth.list_active_principals() FROM PUBLIC;
GRANT ALL ON FUNCTION gobby_agent_auth.list_active_principals() TO gobby_daemon_runtime;
REVOKE ALL ON FUNCTION gobby_agent_auth.load_interactive_credential_material(requested_deployment_token text, requested_machine_id uuid, requested_project_id uuid, requested_generation integer) FROM PUBLIC;
GRANT ALL ON FUNCTION gobby_agent_auth.load_interactive_credential_material(requested_deployment_token text, requested_machine_id uuid, requested_project_id uuid, requested_generation integer) TO gobby_daemon_runtime;
REVOKE ALL ON FUNCTION gobby_agent_auth.lookup_interactive_principal(requested_deployment_token text, requested_machine_id uuid, requested_project_id uuid, requested_generation integer) FROM PUBLIC;
GRANT ALL ON FUNCTION gobby_agent_auth.lookup_interactive_principal(requested_deployment_token text, requested_machine_id uuid, requested_project_id uuid, requested_generation integer) TO gobby_daemon_runtime;
REVOKE ALL ON FUNCTION gobby_agent_auth.managed_execution_is_login_capable(p_execution_id uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION gobby_agent_auth.managed_execution_is_login_capable(p_execution_id uuid) TO gobby_daemon_runtime;
REVOKE ALL ON FUNCTION gobby_agent_auth.principals_due_for_rotation(p_machine_id uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION gobby_agent_auth.principals_due_for_rotation(p_machine_id uuid) TO gobby_daemon_runtime;
REVOKE ALL ON FUNCTION gobby_agent_auth.reconcile_daemon(p_machine_id uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION gobby_agent_auth.reconcile_daemon(p_machine_id uuid) TO gobby_daemon_runtime;
REVOKE ALL ON FUNCTION gobby_agent_auth.reconcile_principal(requested_execution_id uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION gobby_agent_auth.reconcile_principal(requested_execution_id uuid) TO gobby_daemon_runtime;
REVOKE ALL ON FUNCTION gobby_agent_auth.replace_interactive_credential_material(requested_deployment_token text, requested_machine_id uuid, requested_project_id uuid, requested_generation integer, requested_ciphertext text, requested_aad_identity text) FROM PUBLIC;
GRANT ALL ON FUNCTION gobby_agent_auth.replace_interactive_credential_material(requested_deployment_token text, requested_machine_id uuid, requested_project_id uuid, requested_generation integer, requested_ciphertext text, requested_aad_identity text) TO gobby_daemon_runtime;
REVOKE ALL ON FUNCTION gobby_agent_auth.resolve_tool_session(p_session_id uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION gobby_agent_auth.resolve_tool_session(p_session_id uuid) TO gobby_daemon_runtime;
REVOKE ALL ON FUNCTION gobby_agent_auth.revoke_principal(requested_execution_id uuid, requested_generation integer) FROM PUBLIC;
GRANT ALL ON FUNCTION gobby_agent_auth.revoke_principal(requested_execution_id uuid, requested_generation integer) TO gobby_daemon_runtime;
REVOKE ALL ON FUNCTION gobby_agent_auth.rotate_interactive_principal(requested_deployment_token text, requested_machine_id uuid, requested_project_id uuid, requested_session_id uuid, requested_expires_at timestamp with time zone, requested_password text, requested_drain_until timestamp with time zone, requested_overlay_project_id uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION gobby_agent_auth.rotate_interactive_principal(requested_deployment_token text, requested_machine_id uuid, requested_project_id uuid, requested_session_id uuid, requested_expires_at timestamp with time zone, requested_password text, requested_drain_until timestamp with time zone, requested_overlay_project_id uuid) TO gobby_daemon_runtime;
REVOKE ALL ON FUNCTION gobby_agent_auth.rotate_principal(requested_execution_id uuid, requested_expires_at timestamp with time zone, requested_password text) FROM PUBLIC;
GRANT ALL ON FUNCTION gobby_agent_auth.rotate_principal(requested_execution_id uuid, requested_expires_at timestamp with time zone, requested_password text) TO gobby_daemon_runtime;
REVOKE ALL ON FUNCTION gobby_agent_auth.rotate_principal_if_generation(p_execution_id uuid, p_expected_generation integer, p_expires_at timestamp with time zone, p_password text) FROM PUBLIC;
GRANT ALL ON FUNCTION gobby_agent_auth.rotate_principal_if_generation(p_execution_id uuid, p_expected_generation integer, p_expires_at timestamp with time zone, p_password text) TO gobby_daemon_runtime;
REVOKE ALL ON FUNCTION public.compute_task_state_bucket(p_task_id uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION public.compute_task_state_bucket(p_task_id uuid) TO gobby_daemon_runtime;
REVOKE ALL ON FUNCTION public.enforce_chat_attachments_bound_at_write_once() FROM PUBLIC;
GRANT ALL ON FUNCTION public.enforce_chat_attachments_bound_at_write_once() TO gobby_daemon_runtime;
REVOKE ALL ON FUNCTION public.gobby_maintenance_epoch_login_guard() FROM PUBLIC;
GRANT ALL ON FUNCTION public.gobby_maintenance_epoch_login_guard() TO gobby_daemon_runtime;
REVOKE ALL ON FUNCTION public.memories_tags_to_text(tags jsonb) FROM PUBLIC;
GRANT ALL ON FUNCTION public.memories_tags_to_text(tags jsonb) TO gobby_daemon_runtime;
REVOKE ALL ON FUNCTION public.refresh_task_state_bucket(p_task_id uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION public.refresh_task_state_bucket(p_task_id uuid) TO gobby_daemon_runtime;
REVOKE ALL ON FUNCTION public.refresh_task_state_bucket_from_stage() FROM PUBLIC;
GRANT ALL ON FUNCTION public.refresh_task_state_bucket_from_stage() TO gobby_daemon_runtime;
REVOKE ALL ON FUNCTION public.refresh_task_state_bucket_from_task() FROM PUBLIC;
GRANT ALL ON FUNCTION public.refresh_task_state_bucket_from_task() TO gobby_daemon_runtime;
REVOKE ALL ON FUNCTION public.touch_chat_attachments_updated_at() FROM PUBLIC;
GRANT ALL ON FUNCTION public.touch_chat_attachments_updated_at() TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.agent_definitions TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.agent_runs TO gobby_daemon_runtime;
GRANT SELECT(id) ON TABLE public.agent_runs TO gobby_agent_issuer;
GRANT SELECT(machine_id) ON TABLE public.agent_runs TO gobby_agent_issuer;
GRANT SELECT(status) ON TABLE public.agent_runs TO gobby_agent_issuer;
GRANT SELECT(worktree_id) ON TABLE public.agent_runs TO gobby_agent_issuer;
GRANT SELECT(clone_id) ON TABLE public.agent_runs TO gobby_agent_issuer;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.agent_step_instances TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.agent_step_workflows TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.attention_states TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.auth_sessions TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.bin_update_state TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.build_history_events TO gobby_daemon_runtime;
GRANT ALL ON SEQUENCE public.build_history_events_id_seq TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.build_profiles TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.build_runs TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.chat_attachment_cleanup_fences TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.chat_attachments TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.chat_messages TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.checkpoints TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.clones TO gobby_daemon_runtime;
GRANT SELECT(id) ON TABLE public.clones TO gobby_agent_issuer;
GRANT SELECT(project_id) ON TABLE public.clones TO gobby_agent_issuer;
GRANT SELECT(machine_id) ON TABLE public.clones TO gobby_agent_issuer;
GRANT SELECT(clone_path) ON TABLE public.clones TO gobby_agent_issuer;
GRANT SELECT(agent_session_id) ON TABLE public.clones TO gobby_agent_issuer;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.code_calls TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.code_calls TO gobby_gcode_capability;
GRANT ALL ON SEQUENCE public.code_calls_id_seq TO gobby_daemon_runtime;
GRANT SELECT,USAGE ON SEQUENCE public.code_calls_id_seq TO gobby_gcode_capability;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.code_content_chunks TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.code_content_chunks TO gobby_gcode_capability;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.code_imports TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.code_imports TO gobby_gcode_capability;
GRANT ALL ON SEQUENCE public.code_imports_id_seq TO gobby_daemon_runtime;
GRANT SELECT,USAGE ON SEQUENCE public.code_imports_id_seq TO gobby_gcode_capability;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.code_index_projection_cleanup_pending TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.code_index_projection_cleanup_pending TO gobby_gcode_capability;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.code_index_prune_dirty_projects TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.code_index_prune_dirty_projects TO gobby_gcode_capability;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.code_indexed_file_states TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.code_indexed_file_states TO gobby_gcode_capability;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.code_indexed_files TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.code_indexed_files TO gobby_gcode_capability;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.code_indexed_project_states TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.code_indexed_project_states TO gobby_gcode_capability;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.code_indexed_projects TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.code_indexed_projects TO gobby_gcode_capability;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.code_inheritance TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.code_inheritance TO gobby_gcode_capability;
GRANT ALL ON SEQUENCE public.code_inheritance_id_seq TO gobby_daemon_runtime;
GRANT SELECT,USAGE ON SEQUENCE public.code_inheritance_id_seq TO gobby_gcode_capability;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.code_symbols TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.code_symbols TO gobby_gcode_capability;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.comms_attachments TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.comms_channels TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.comms_identities TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.comms_messages TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.comms_routing_rules TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.completion_subscribers TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.config_state TO gobby_daemon_runtime;
GRANT SELECT(id) ON TABLE public.config_state TO gobby_gcode_capability;
GRANT SELECT(revision) ON TABLE public.config_state TO gobby_gcode_capability;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.config_store TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.cron_jobs TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.cron_runs TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.definition_revisions TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.deployment_runtime TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.destructive_batches TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.detection_manifests TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.embedding_generation_acks TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.embedding_projection_changes TO gobby_daemon_runtime;
GRANT ALL ON SEQUENCE public.embedding_projection_changes_sequence_seq TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.expansion_runs TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.external_issue_sync_status TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.feedback_review_runs TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.gh_issues_triaged TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.gh_triage_build_dispatches TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.gh_triage_deliveries TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.hook_force_continue_budgets TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.hook_receipt_effects TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.integration_workspace_mutex TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.inter_session_messages TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.loop_progress TO gobby_daemon_runtime;
GRANT ALL ON SEQUENCE public.loop_progress_id_seq TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.machines TO gobby_daemon_runtime;
GRANT SELECT(id) ON TABLE public.machines TO gobby_agent_issuer;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.maintenance_epochs TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.mcp_server_templates TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.mcp_servers TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.memories TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.memory_crossrefs TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.memory_dream_runs TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.memory_dream_snapshots TO gobby_daemon_runtime;
GRANT ALL ON SEQUENCE public.memory_dream_snapshots_id_seq TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.memory_dream_truth_state TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.merge_conflicts TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.merge_resolutions TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.metric_snapshots TO gobby_daemon_runtime;
GRANT ALL ON SEQUENCE public.metric_snapshots_id_seq TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.metrics_events TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.metrics_events_archive TO gobby_daemon_runtime;
GRANT ALL ON SEQUENCE public.metrics_events_archive_id_seq TO gobby_daemon_runtime;
GRANT ALL ON SEQUENCE public.metrics_events_id_seq TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.model_metadata TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.pending_interactions TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.pipeline_definitions TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.pipeline_executions TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.plan_review_evidence TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.plans TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.project_checkouts TO gobby_daemon_runtime;
GRANT SELECT(machine_id),UPDATE(machine_id) ON TABLE public.project_checkouts TO gobby_gcode_capability;
GRANT SELECT(project_id),UPDATE(project_id) ON TABLE public.project_checkouts TO gobby_gcode_capability;
GRANT SELECT(root_path),UPDATE(root_path) ON TABLE public.project_checkouts TO gobby_gcode_capability;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.project_github_triage_configs TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.project_lifecycle_events TO gobby_daemon_runtime;
GRANT ALL ON SEQUENCE public.project_lifecycle_events_id_seq TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.projects TO gobby_daemon_runtime;
GRANT SELECT(id) ON TABLE public.projects TO gobby_gcode_capability;
GRANT SELECT(id) ON TABLE public.projects TO gobby_agent_issuer;
GRANT SELECT(name) ON TABLE public.projects TO gobby_gcode_capability;
GRANT SELECT(name) ON TABLE public.projects TO gobby_agent_issuer;
GRANT SELECT(deleted_at) ON TABLE public.projects TO gobby_gcode_capability;
GRANT SELECT(deleted_at) ON TABLE public.projects TO gobby_agent_issuer;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.prompts TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.provider_capability_refresh_state TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.provider_capacity_snapshots TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.provider_model_capabilities TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.provider_model_routes TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.recall_gate_runs TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.recall_holdout_consumed TO gobby_daemon_runtime;
GRANT ALL ON SEQUENCE public.recall_holdout_consumed_id_seq TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.recall_injection_outcomes TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.recall_shadow_audit_verdicts TO gobby_daemon_runtime;
GRANT ALL ON SEQUENCE public.recall_shadow_audit_verdicts_id_seq TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.recall_shadow_judge_state TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.recall_shadow_prompt_snapshot TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.recall_signal_hits TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.recall_signal_requests TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.recall_usefulness TO gobby_daemon_runtime;
GRANT ALL ON SEQUENCE public.recall_usefulness_id_seq TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.rule_definitions TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.schema_migrations TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.secret_key_material TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.secrets TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.session_feedback TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.session_skills TO gobby_daemon_runtime;
GRANT ALL ON SEQUENCE public.session_skills_id_seq TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.session_stop_signals TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.session_summary_revisions TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.session_tasks TO gobby_daemon_runtime;
GRANT ALL ON SEQUENCE public.session_tasks_id_seq TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.session_variable_defaults TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.session_variables TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.sessions TO gobby_daemon_runtime;
GRANT SELECT(id) ON TABLE public.sessions TO gobby_agent_issuer;
GRANT SELECT(project_id) ON TABLE public.sessions TO gobby_agent_issuer;
GRANT SELECT(status) ON TABLE public.sessions TO gobby_agent_issuer;
GRANT SELECT(agent_run_id) ON TABLE public.sessions TO gobby_agent_issuer;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.skill_files TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.skills TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.spans TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.step_executions TO gobby_daemon_runtime;
GRANT ALL ON SEQUENCE public.step_executions_id_seq TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.task_affected_files TO gobby_daemon_runtime;
GRANT ALL ON SEQUENCE public.task_affected_files_id_seq TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.task_artifacts TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.task_close_reviews TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.task_comments TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.task_delivery_campaigns TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.task_delivery_units TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.task_dependencies TO gobby_daemon_runtime;
GRANT ALL ON SEQUENCE public.task_dependencies_id_seq TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.task_dispatch_mutex TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.task_lifecycle_events TO gobby_daemon_runtime;
GRANT ALL ON SEQUENCE public.task_lifecycle_events_id_seq TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.task_selection_history TO gobby_daemon_runtime;
GRANT ALL ON SEQUENCE public.task_selection_history_id_seq TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.task_stage_states TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.task_stages_registry TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.task_type_default_stages TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.task_validation_backoff TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.task_validation_history TO gobby_daemon_runtime;
GRANT ALL ON SEQUENCE public.task_validation_history_id_seq TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.tasks TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.terminals TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.token_events TO gobby_daemon_runtime;
GRANT ALL ON SEQUENCE public.token_events_id_seq TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.tool_metrics TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.tool_metrics_daily TO gobby_daemon_runtime;
GRANT ALL ON SEQUENCE public.tool_metrics_daily_id_seq TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.tool_result_chunks TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.tool_results TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.tool_schema_hashes TO gobby_daemon_runtime;
GRANT ALL ON SEQUENCE public.tool_schema_hashes_id_seq TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.tools TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.unmodeled_observation_events TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.unmodeled_observations TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.users TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.workflow_audit_log TO gobby_daemon_runtime;
GRANT ALL ON SEQUENCE public.workflow_audit_log_id_seq TO gobby_daemon_runtime;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.worktrees TO gobby_daemon_runtime;
GRANT SELECT(id) ON TABLE public.worktrees TO gobby_agent_issuer;
GRANT SELECT(project_id) ON TABLE public.worktrees TO gobby_agent_issuer;
GRANT SELECT(machine_id) ON TABLE public.worktrees TO gobby_agent_issuer;
GRANT SELECT(worktree_path) ON TABLE public.worktrees TO gobby_agent_issuer;
GRANT SELECT(agent_session_id) ON TABLE public.worktrees TO gobby_agent_issuer;
ALTER DEFAULT PRIVILEGES FOR ROLE gobby_test IN SCHEMA public GRANT ALL ON SEQUENCES TO gobby_daemon_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE gobby_test IN SCHEMA public GRANT ALL ON FUNCTIONS TO gobby_daemon_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE gobby_test IN SCHEMA public GRANT SELECT,INSERT,DELETE,UPDATE ON TABLES TO gobby_daemon_runtime;
