-- A managed generation is revoked once its binding is revoked or gone. A requested
-- revocation alone does not count: a draining predecessor stays usable.
CREATE FUNCTION gobby_agent_auth.managed_principal_revoked(p_execution_id uuid, p_generation integer)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path TO 'pg_catalog', 'gobby_agent_auth'
AS $$
    SELECT NOT EXISTS (
        SELECT 1 FROM gobby_agent_auth.principal_bindings pb
        WHERE pb.managed_execution_id = p_execution_id
          AND pb.credential_generation = p_generation
          AND pb.revoked_at IS NULL
    )
$$;
ALTER FUNCTION gobby_agent_auth.managed_principal_revoked(uuid, integer) OWNER TO gobby_agent_issuer;
REVOKE ALL ON FUNCTION gobby_agent_auth.managed_principal_revoked(uuid, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION gobby_agent_auth.managed_principal_revoked(uuid, integer) TO gobby_daemon_runtime;
