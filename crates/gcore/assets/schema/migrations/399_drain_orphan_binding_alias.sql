-- 399: drain_ephemeral_principals orphan sweep aliased its principal_bindings
-- subquery as `binding`, which PL/pgSQL resolves against the function's own
-- `binding RECORD` variable (record "binding" has no field "role_name").
-- Re-create the function with a non-conflicting alias.

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
$function$;
