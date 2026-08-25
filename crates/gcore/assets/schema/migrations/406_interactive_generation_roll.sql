-- Roll aged interactive principals to a new generation instead of dead-ending.
--
-- The 403 reuse branch extended expires_at while leaving issued_at at first
-- issuance, so once a binding aged past 24 hours every extension tripped the
-- principal_lifetime_guard trigger ('managed principal lifetime exceeds 24
-- hours', ERRCODE 22023) and every interactive handshake on the project 403'd
-- until someone hand-edited the row. Interactive principals are deliberately
-- outside the generic rotate_due sweep (migration 404), and nothing else
-- rotated them across a day of activity, so an active session always hit the
-- wall.
--
-- Fix at the issuance point: reuse only while the requested expiry stays
-- within 24 hours of the existing binding's issued_at, preserving the
-- credential-age bound. An older binding rolls inline to the next generation,
-- mirroring rotate_interactive_principal: the predecessor is marked draining
-- until its own remaining validity runs out (it is never extended), and a
-- fresh role, binding, and audit row are created. The caller already passes a
-- fresh password on every call and stores it whenever reused is FALSE, so the
-- roll is transparent to issue_interactive.

DROP FUNCTION IF EXISTS gobby_agent_auth.issue_or_reuse_interactive_principal(
    TEXT, UUID, UUID, UUID, TIMESTAMPTZ, TEXT, UUID
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
