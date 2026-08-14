"""Install interactive-principal helpers that 1.1 sealed incompletely.

Applied through the privileged hub DSN so gdaemon identity stays unchanged.
"""

from __future__ import annotations

import threading

import psycopg

_LOCK = threading.Lock()
_APPLIED: set[str] = set()

_STATEMENTS = (
    """
    DROP INDEX IF EXISTS gobby_agent_auth.uq_interactive_principal_active
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_interactive_principal_active
    ON gobby_agent_auth.principal_bindings(
        deployment_token, issuing_machine_id, project_id
    )
    WHERE owner_kind = 'interactive'
      AND revoked_at IS NULL
      AND predecessor_drain_deadline IS NULL
      AND deployment_token IS NOT NULL
    """,
    """
    DROP FUNCTION IF EXISTS gobby_agent_auth.issue_or_reuse_interactive_principal(
        TEXT, UUID, UUID, UUID, TIMESTAMPTZ, TEXT
    )
    """,
    r"""
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
        existing_binding gobby_agent_auth.principal_bindings%ROWTYPE;
        derived_role_name NAME;
        next_generation INTEGER;
        binding_id UUID;
        token_slug TEXT;
    BEGIN
        PERFORM pg_advisory_xact_lock(
            hashtextextended(
                requested_deployment_token
                || requested_machine_id::TEXT
                || requested_project_id::TEXT,
                0
            )
        );
        IF requested_deployment_token IS NULL OR requested_deployment_token = '' THEN
            RAISE EXCEPTION 'interactive principal requires a deployment token'
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
        SELECT *
          INTO existing_binding
          FROM gobby_agent_auth.principal_bindings
         WHERE owner_kind = 'interactive'
           AND deployment_token = requested_deployment_token
           AND issuing_machine_id = requested_machine_id
           AND project_id = requested_project_id
           AND revoked_at IS NULL
           AND predecessor_drain_deadline IS NULL
           AND expires_at > clock_timestamp()
         ORDER BY credential_generation DESC
         LIMIT 1;
        IF FOUND THEN
            RETURN QUERY SELECT existing_binding.role_name,
                existing_binding.credential_generation, TRUE,
                existing_binding.managed_execution_id;
            RETURN;
        END IF;

        SELECT COALESCE(MAX(pb.credential_generation), 0) + 1
          INTO next_generation
          FROM gobby_agent_auth.principal_bindings AS pb
         WHERE pb.owner_kind = 'interactive'
           AND pb.deployment_token = requested_deployment_token
           AND pb.issuing_machine_id = requested_machine_id
           AND pb.project_id = requested_project_id;
        token_slug := regexp_replace(requested_deployment_token, '[^a-zA-Z0-9]', '', 'g');
        derived_role_name := (
            'gobby_ix_'
            || substr(token_slug, 1, 8)
            || '_'
            || substr(replace(requested_machine_id::TEXT, '-', ''), 1, 8)
            || '_'
            || substr(replace(requested_project_id::TEXT, '-', ''), 1, 8)
            || '_'
            || next_generation::TEXT
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

        INSERT INTO gobby_agent_auth.principal_bindings (
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

        RETURN QUERY SELECT derived_role_name, next_generation, FALSE,
            (SELECT pb.managed_execution_id FROM gobby_agent_auth.principal_bindings AS pb
              WHERE pb.id = binding_id);
    END
    $function$
    """,
    """
    CREATE OR REPLACE FUNCTION gobby_agent_auth.load_interactive_credential_material(
        requested_deployment_token TEXT,
        requested_machine_id UUID,
        requested_project_id UUID,
        requested_generation INTEGER
    )
    RETURNS TABLE(ciphertext TEXT, aad_identity TEXT)
    LANGUAGE sql
    SECURITY DEFINER
    SET search_path = gobby_agent_auth, pg_temp
    AS $function$
        SELECT ciphertext, aad_identity
          FROM interactive_credential_material
         WHERE deployment_token = requested_deployment_token
           AND machine_id = requested_machine_id
           AND project_id = requested_project_id
           AND credential_generation = requested_generation
    $function$
    """,
    """
    CREATE OR REPLACE FUNCTION gobby_agent_auth.lookup_interactive_principal(
        requested_deployment_token TEXT,
        requested_machine_id UUID,
        requested_project_id UUID,
        requested_generation INTEGER
    )
    RETURNS TABLE(
        managed_execution_id UUID,
        role_name NAME,
        credential_generation INTEGER,
        revoked_at TIMESTAMPTZ
    )
    LANGUAGE sql
    SECURITY DEFINER
    SET search_path = gobby_agent_auth, pg_temp
    AS $function$
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
    $function$
    """,
    r"""
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
        existing_binding gobby_agent_auth.principal_bindings%ROWTYPE;
        derived_role_name NAME;
        next_generation INTEGER;
        binding_id UUID;
        token_slug TEXT;
        drain_until TIMESTAMPTZ;
    BEGIN
        PERFORM pg_advisory_xact_lock(
            hashtextextended(
                requested_deployment_token
                || requested_machine_id::TEXT
                || requested_project_id::TEXT,
                0
            )
        );
        IF requested_expires_at <= clock_timestamp() THEN
            RAISE EXCEPTION 'managed principal expiry must be in the future'
                USING ERRCODE = '22023';
        END IF;
        SELECT *
          INTO existing_binding
          FROM gobby_agent_auth.principal_bindings
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
        UPDATE gobby_agent_auth.principal_bindings
           SET predecessor_drain_deadline = drain_until,
               revocation_requested_at = COALESCE(revocation_requested_at, clock_timestamp())
         WHERE id = existing_binding.id;
        SELECT COALESCE(MAX(pb.credential_generation), 0) + 1
          INTO next_generation
          FROM gobby_agent_auth.principal_bindings AS pb
         WHERE pb.owner_kind = 'interactive'
           AND pb.deployment_token = requested_deployment_token
           AND pb.issuing_machine_id = requested_machine_id
           AND pb.project_id = requested_project_id;
        token_slug := regexp_replace(requested_deployment_token, '[^a-zA-Z0-9]', '', 'g');
        derived_role_name := (
            'gobby_ix_'
            || substr(token_slug, 1, 8)
            || '_'
            || substr(replace(requested_machine_id::TEXT, '-', ''), 1, 8)
            || '_'
            || substr(replace(requested_project_id::TEXT, '-', ''), 1, 8)
            || '_'
            || next_generation::TEXT
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
        INSERT INTO gobby_agent_auth.principal_bindings (
            role_name, owner_kind, managed_execution_id, session_id, project_id,
            issuing_machine_id, deployment_token, expires_at, credential_generation
        ) VALUES (
            derived_role_name, 'interactive', gen_random_uuid(), requested_session_id,
            requested_project_id, requested_machine_id, requested_deployment_token,
            requested_expires_at, next_generation
        ) RETURNING id INTO binding_id;
        RETURN QUERY SELECT derived_role_name, next_generation,
            (SELECT pb.managed_execution_id FROM gobby_agent_auth.principal_bindings AS pb
              WHERE pb.id = binding_id);
    END
    $function$
    """,
    """
    ALTER FUNCTION gobby_agent_auth.issue_or_reuse_interactive_principal(
        TEXT, UUID, UUID, UUID, TIMESTAMPTZ, TEXT
    ) OWNER TO gobby_agent_issuer
    """,
    """
    GRANT EXECUTE ON FUNCTION gobby_agent_auth.issue_or_reuse_interactive_principal(
        TEXT, UUID, UUID, UUID, TIMESTAMPTZ, TEXT
    ) TO gobby_daemon_runtime
    """,
    """
    ALTER FUNCTION gobby_agent_auth.load_interactive_credential_material(
        TEXT, UUID, UUID, INTEGER
    ) OWNER TO gobby_agent_issuer
    """,
    """
    GRANT EXECUTE ON FUNCTION gobby_agent_auth.load_interactive_credential_material(
        TEXT, UUID, UUID, INTEGER
    ) TO gobby_daemon_runtime
    """,
    """
    ALTER FUNCTION gobby_agent_auth.lookup_interactive_principal(
        TEXT, UUID, UUID, INTEGER
    ) OWNER TO gobby_agent_issuer
    """,
    """
    GRANT EXECUTE ON FUNCTION gobby_agent_auth.lookup_interactive_principal(
        TEXT, UUID, UUID, INTEGER
    ) TO gobby_daemon_runtime
    """,
    """
    ALTER FUNCTION gobby_agent_auth.rotate_interactive_principal(
        TEXT, UUID, UUID, UUID, TIMESTAMPTZ, TEXT, TIMESTAMPTZ
    ) OWNER TO gobby_agent_issuer
    """,
    """
    GRANT EXECUTE ON FUNCTION gobby_agent_auth.rotate_interactive_principal(
        TEXT, UUID, UUID, UUID, TIMESTAMPTZ, TEXT, TIMESTAMPTZ
    ) TO gobby_daemon_runtime
    """,
)


def ensure_interactive_sql(conninfo: str) -> None:
    """Install interactive helper functions on the privileged hub connection."""
    with _LOCK:
        if conninfo in _APPLIED:
            return
        with psycopg.connect(conninfo, autocommit=True) as connection:
            for statement in _STATEMENTS:
                connection.execute(statement)
        _APPLIED.add(conninfo)
