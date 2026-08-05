-- gobby:destructive
-- Operator-safe managed-role inventory and maintenance drains.

CREATE OR REPLACE FUNCTION gobby_agent_auth.list_active_principals()
RETURNS TABLE (
    role_name NAME,
    managed_execution_id UUID,
    owner_kind TEXT,
    agent_run_id UUID,
    session_id UUID,
    project_id UUID,
    expires_at TIMESTAMPTZ,
    login_capable BOOLEAN,
    active_sessions BIGINT
)
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, gobby_agent_auth
AS $function$
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
        WHERE role.rolname ~ '^gobby_agent_[0-9a-f]{32}_[1-9][0-9]*$'
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

ALTER FUNCTION gobby_agent_auth.list_active_principals()
OWNER TO gobby_agent_issuer;
ALTER FUNCTION gobby_agent_auth.drain_ephemeral_principals()
OWNER TO gobby_agent_issuer;

REVOKE ALL ON FUNCTION gobby_agent_auth.list_active_principals() FROM PUBLIC;
REVOKE ALL ON FUNCTION gobby_agent_auth.drain_ephemeral_principals() FROM PUBLIC;

GRANT EXECUTE ON FUNCTION gobby_agent_auth.list_active_principals()
TO gobby_daemon_runtime;
GRANT EXECUTE ON FUNCTION gobby_agent_auth.drain_ephemeral_principals()
TO gobby_daemon_runtime;
