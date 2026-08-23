-- Bind interactive principals to one registered isolation overlay.
--
-- Interactive callers (gcode from a terminal, Claude Code / Codex sessions)
-- working inside a registered worktree or clone index that workspace under its
-- own code-index project id. The code_* RLS policies already admit writes to
-- current_code_overlay_project_id(); this migration lets the interactive issuer
-- populate that binding column when the caller proves the overlay is a
-- registered isolation workspace of the session's project on this machine.
--
-- Reuse and rotation are keyed per (deployment token, machine, project,
-- overlay); the generation counter stays project-wide so credential material,
-- AAD, and role names remain unique per project.

DROP INDEX IF EXISTS gobby_agent_auth.uq_interactive_principal_active;
CREATE UNIQUE INDEX IF NOT EXISTS uq_interactive_principal_active
    ON gobby_agent_auth.principal_bindings(
        deployment_token, issuing_machine_id, project_id, code_overlay_project_id
    ) NULLS NOT DISTINCT
    WHERE owner_kind = 'interactive'
      AND revoked_at IS NULL
      AND predecessor_drain_deadline IS NULL;

CREATE OR REPLACE FUNCTION gobby_agent_auth.assert_interactive_overlay_registered(
    requested_machine_id UUID,
    requested_project_id UUID,
    requested_overlay_project_id UUID
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = gobby_agent_auth, pg_temp
AS $function$
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
$function$;

ALTER FUNCTION gobby_agent_auth.assert_interactive_overlay_registered(UUID, UUID, UUID)
    OWNER TO gobby_agent_issuer;
REVOKE ALL ON FUNCTION gobby_agent_auth.assert_interactive_overlay_registered(UUID, UUID, UUID)
    FROM PUBLIC;

DROP FUNCTION IF EXISTS gobby_agent_auth.issue_or_reuse_interactive_principal(
    TEXT, UUID, UUID, UUID, TIMESTAMPTZ, TEXT
);
CREATE OR REPLACE FUNCTION gobby_agent_auth.issue_or_reuse_interactive_principal(
    requested_deployment_token TEXT,
    requested_machine_id UUID,
    requested_project_id UUID,
    requested_session_id UUID,
    requested_expires_at TIMESTAMPTZ,
    requested_password TEXT,
    requested_overlay_project_id UUID
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

ALTER FUNCTION gobby_agent_auth.issue_or_reuse_interactive_principal(
    TEXT, UUID, UUID, UUID, TIMESTAMPTZ, TEXT, UUID
) OWNER TO gobby_agent_issuer;
REVOKE ALL ON FUNCTION gobby_agent_auth.issue_or_reuse_interactive_principal(
    TEXT, UUID, UUID, UUID, TIMESTAMPTZ, TEXT, UUID
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION gobby_agent_auth.issue_or_reuse_interactive_principal(
    TEXT, UUID, UUID, UUID, TIMESTAMPTZ, TEXT, UUID
) TO gobby_daemon_runtime;

DROP FUNCTION IF EXISTS gobby_agent_auth.rotate_interactive_principal(
    TEXT, UUID, UUID, UUID, TIMESTAMPTZ, TEXT, TIMESTAMPTZ
);
CREATE OR REPLACE FUNCTION gobby_agent_auth.rotate_interactive_principal(
    requested_deployment_token TEXT,
    requested_machine_id UUID,
    requested_project_id UUID,
    requested_session_id UUID,
    requested_expires_at TIMESTAMPTZ,
    requested_password TEXT,
    requested_drain_until TIMESTAMPTZ,
    requested_overlay_project_id UUID
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
$function$;

ALTER FUNCTION gobby_agent_auth.rotate_interactive_principal(
    TEXT, UUID, UUID, UUID, TIMESTAMPTZ, TEXT, TIMESTAMPTZ, UUID
) OWNER TO gobby_agent_issuer;
REVOKE ALL ON FUNCTION gobby_agent_auth.rotate_interactive_principal(
    TEXT, UUID, UUID, UUID, TIMESTAMPTZ, TEXT, TIMESTAMPTZ, UUID
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION gobby_agent_auth.rotate_interactive_principal(
    TEXT, UUID, UUID, UUID, TIMESTAMPTZ, TEXT, TIMESTAMPTZ, UUID
) TO gobby_daemon_runtime;
