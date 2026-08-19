-- replace_interactive_credential_material deleted the sealed material of every
-- other credential_generation for the same (deployment_token, machine_id,
-- project_id) before upserting the requested generation. Rotation therefore
-- wiped the still-draining predecessor's material (unloadable after a rotation
-- rollback), and a late store for an older generation wiped the live
-- generation's row, failing every reuse handshake with
-- credential_issuance_failed. Material now lives exactly as long as its
-- binding: the store locks the requested generation's live binding (skipping
-- the write entirely when that binding is revoked or absent, so a late store
-- cannot resurrect material for a revoked generation), prunes only rows with
-- no unrevoked interactive binding, and revoke_principal deletes a binding's
-- material the moment the binding is revoked. A one-time repair drops rows
-- already orphaned by revoked bindings; draining predecessors keep theirs.

DROP FUNCTION IF EXISTS gobby_agent_auth.replace_interactive_credential_material(
    TEXT, UUID, UUID, INTEGER, TEXT, TEXT
);
CREATE FUNCTION gobby_agent_auth.replace_interactive_credential_material(
    requested_deployment_token TEXT,
    requested_machine_id UUID,
    requested_project_id UUID,
    requested_generation INTEGER,
    requested_ciphertext TEXT,
    requested_aad_identity TEXT
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = gobby_agent_auth, pg_temp
AS $function$
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
$function$;

ALTER FUNCTION gobby_agent_auth.replace_interactive_credential_material(
    TEXT, UUID, UUID, INTEGER, TEXT, TEXT
) OWNER TO gobby_agent_issuer;
REVOKE ALL ON FUNCTION gobby_agent_auth.replace_interactive_credential_material(
    TEXT, UUID, UUID, INTEGER, TEXT, TEXT
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION gobby_agent_auth.replace_interactive_credential_material(
    TEXT, UUID, UUID, INTEGER, TEXT, TEXT
) TO gobby_daemon_runtime;

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
$function$;

-- One-time repair of the invariant on live hubs: drop sealed rows whose
-- interactive binding is already revoked. Rows whose binding is merely
-- draining (revoked_at IS NULL) keep their material.
DELETE FROM gobby_agent_auth.interactive_credential_material AS icm
 WHERE NOT EXISTS (
     SELECT 1
       FROM gobby_agent_auth.principal_bindings AS pb
      WHERE pb.owner_kind = 'interactive'
        AND pb.deployment_token = icm.deployment_token
        AND pb.issuing_machine_id = icm.machine_id
        AND pb.project_id = icm.project_id
        AND pb.credential_generation = icm.credential_generation
        AND pb.revoked_at IS NULL
 );
