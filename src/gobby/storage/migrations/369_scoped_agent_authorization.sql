-- Scoped PostgreSQL authorization substrate for managed gcode executions.
-- Migration slots 367 and 368 are reserved by tasks #19404 and #19421.

SELECT pg_advisory_xact_lock(hashtextextended('gobby:agent-authorization:v1', 0));

DO $roles$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gobby_agent_issuer') THEN
        CREATE ROLE gobby_agent_issuer;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gobby_gcode_capability') THEN
        CREATE ROLE gobby_gcode_capability;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gobby_daemon_runtime') THEN
        CREATE ROLE gobby_daemon_runtime;
    END IF;

    ALTER ROLE gobby_agent_issuer
        NOLOGIN NOSUPERUSER INHERIT CREATEROLE NOCREATEDB
        NOREPLICATION NOBYPASSRLS;
    ALTER ROLE gobby_gcode_capability
        NOLOGIN NOSUPERUSER INHERIT NOCREATEROLE NOCREATEDB
        NOREPLICATION NOBYPASSRLS;
    ALTER ROLE gobby_daemon_runtime
        NOLOGIN NOSUPERUSER INHERIT NOCREATEROLE NOCREATEDB
        NOREPLICATION NOBYPASSRLS;
END
$roles$;

GRANT pg_signal_backend TO gobby_agent_issuer
    WITH ADMIN FALSE, INHERIT TRUE, SET FALSE;
GRANT gobby_gcode_capability TO gobby_agent_issuer
    WITH ADMIN TRUE, INHERIT FALSE, SET FALSE;

DO $runtime_membership$
BEGIN
    EXECUTE format(
        'GRANT %I TO %I WITH ADMIN FALSE, INHERIT FALSE, SET TRUE',
        'gobby_daemon_runtime',
        current_user
    );
END
$runtime_membership$;

CREATE SCHEMA IF NOT EXISTS gobby_agent_auth;

CREATE TABLE IF NOT EXISTS gobby_agent_auth.principal_bindings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    role_name NAME NOT NULL UNIQUE,
    owner_kind TEXT NOT NULL CHECK (owner_kind IN ('agent_run', 'tool_chat')),
    managed_execution_id UUID NOT NULL,
    agent_run_id UUID,
    session_id UUID NOT NULL,
    project_id UUID NOT NULL,
    issuing_machine_id UUID NOT NULL,
    issued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    credential_generation INTEGER NOT NULL CHECK (credential_generation > 0),
    CONSTRAINT principal_bindings_execution_generation
        UNIQUE (managed_execution_id, credential_generation),
    CONSTRAINT principal_bindings_expiry_after_issue CHECK (expires_at > issued_at),
    CONSTRAINT principal_bindings_revoke_after_issue
        CHECK (revoked_at IS NULL OR revoked_at >= issued_at)
);

CREATE INDEX IF NOT EXISTS idx_principal_bindings_active_role
ON gobby_agent_auth.principal_bindings(role_name)
WHERE revoked_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_principal_bindings_execution
ON gobby_agent_auth.principal_bindings(managed_execution_id, credential_generation DESC);

CREATE INDEX IF NOT EXISTS idx_principal_bindings_expiry
ON gobby_agent_auth.principal_bindings(expires_at)
WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS gobby_agent_auth.principal_audit_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    binding_id UUID REFERENCES gobby_agent_auth.principal_bindings(id) ON DELETE SET NULL,
    event_type TEXT NOT NULL CHECK (event_type IN ('issue', 'rotate', 'revoke', 'reconcile')),
    managed_execution_id UUID NOT NULL,
    role_name NAME NOT NULL,
    credential_generation INTEGER NOT NULL,
    project_id UUID NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    details JSONB NOT NULL DEFAULT '{}'::JSONB
);

ALTER SCHEMA gobby_agent_auth OWNER TO gobby_agent_issuer;
ALTER TABLE gobby_agent_auth.principal_bindings OWNER TO gobby_agent_issuer;
ALTER TABLE gobby_agent_auth.principal_audit_events OWNER TO gobby_agent_issuer;
ALTER SEQUENCE gobby_agent_auth.principal_audit_events_id_seq OWNER TO gobby_agent_issuer;

REVOKE ALL ON SCHEMA gobby_agent_auth FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA gobby_agent_auth FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA gobby_agent_auth FROM PUBLIC;
GRANT USAGE ON SCHEMA gobby_agent_auth TO gobby_gcode_capability, gobby_daemon_runtime;

CREATE OR REPLACE FUNCTION gobby_agent_auth.current_project_id()
RETURNS UUID
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = gobby_agent_auth, pg_temp
AS $function$
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
$function$;

CREATE OR REPLACE FUNCTION gobby_agent_auth.issue_principal(
    requested_execution_id UUID,
    requested_owner_kind TEXT,
    requested_session_id UUID,
    requested_agent_run_id UUID,
    requested_machine_id UUID,
    requested_expires_at TIMESTAMPTZ,
    requested_password TEXT
)
RETURNS TABLE(role_name NAME, credential_generation INTEGER)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = gobby_agent_auth, pg_temp
SET createrole_self_grant = ''
AS $function$
DECLARE
    resolved_project_id UUID;
    resolved_agent_run_id UUID;
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
    IF requested_agent_run_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM public.agent_runs WHERE id = requested_agent_run_id
    ) THEN
        RAISE EXCEPTION 'managed principal agent run does not exist'
            USING ERRCODE = '23503';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.machines WHERE id = requested_machine_id) THEN
        RAISE EXCEPTION 'managed principal issuing machine does not exist'
            USING ERRCODE = '23503';
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
$function$;

CREATE OR REPLACE FUNCTION gobby_agent_auth.rotate_principal(
    requested_execution_id UUID,
    requested_expires_at TIMESTAMPTZ,
    requested_password TEXT
)
RETURNS TABLE(role_name NAME, credential_generation INTEGER)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = gobby_agent_auth, pg_temp
SET createrole_self_grant = ''
AS $function$
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
$function$;

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
                RAISE EXCEPTION 'managed principal still has active database sessions'
                    USING ERRCODE = '55006';
            END IF;
            EXECUTE format('DROP ROLE %I', binding.role_name);
        END IF;
        UPDATE principal_bindings
        SET revoked_at = clock_timestamp()
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

CREATE OR REPLACE FUNCTION gobby_agent_auth.reconcile_principal(
    requested_execution_id UUID
)
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = gobby_agent_auth, pg_temp
SET createrole_self_grant = ''
AS $function$
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
$function$;

ALTER FUNCTION gobby_agent_auth.current_project_id() OWNER TO gobby_agent_issuer;
ALTER FUNCTION gobby_agent_auth.issue_principal(UUID, TEXT, UUID, UUID, UUID, TIMESTAMPTZ, TEXT)
    OWNER TO gobby_agent_issuer;
ALTER FUNCTION gobby_agent_auth.rotate_principal(UUID, TIMESTAMPTZ, TEXT)
    OWNER TO gobby_agent_issuer;
ALTER FUNCTION gobby_agent_auth.revoke_principal(UUID, INTEGER)
    OWNER TO gobby_agent_issuer;
ALTER FUNCTION gobby_agent_auth.reconcile_principal(UUID)
    OWNER TO gobby_agent_issuer;

REVOKE ALL ON FUNCTION gobby_agent_auth.current_project_id() FROM PUBLIC;
REVOKE ALL ON FUNCTION
    gobby_agent_auth.issue_principal(UUID, TEXT, UUID, UUID, UUID, TIMESTAMPTZ, TEXT)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION gobby_agent_auth.rotate_principal(UUID, TIMESTAMPTZ, TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION gobby_agent_auth.revoke_principal(UUID, INTEGER) FROM PUBLIC;
REVOKE ALL ON FUNCTION gobby_agent_auth.reconcile_principal(UUID) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION gobby_agent_auth.current_project_id()
    TO gobby_gcode_capability;
GRANT EXECUTE ON FUNCTION
    gobby_agent_auth.issue_principal(UUID, TEXT, UUID, UUID, UUID, TIMESTAMPTZ, TEXT),
    gobby_agent_auth.rotate_principal(UUID, TIMESTAMPTZ, TEXT),
    gobby_agent_auth.revoke_principal(UUID, INTEGER),
    gobby_agent_auth.reconcile_principal(UUID)
    TO gobby_daemon_runtime;

DO $issuer_source_grants$
BEGIN
    GRANT USAGE ON SCHEMA public TO gobby_agent_issuer;
    IF to_regclass('public.sessions') IS NOT NULL THEN
        GRANT SELECT (id, project_id, agent_run_id) ON public.sessions TO gobby_agent_issuer;
    END IF;
    IF to_regclass('public.agent_runs') IS NOT NULL THEN
        GRANT SELECT (id) ON public.agent_runs TO gobby_agent_issuer;
    END IF;
    IF to_regclass('public.machines') IS NOT NULL THEN
        GRANT SELECT (id) ON public.machines TO gobby_agent_issuer;
    END IF;
END
$issuer_source_grants$;

DO $rls$
DECLARE
    target_schema NAME := current_schema();
    migration_role NAME := current_user;
    table_name TEXT;
    project_expression TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'projects',
        'code_indexed_projects',
        'code_indexed_files',
        'code_symbols',
        'code_imports',
        'code_calls',
        'code_content_chunks',
        'code_index_projection_cleanup_pending',
        'code_index_prune_dirty_projects'
    ]
    LOOP
        IF to_regclass(format('%I.%I', target_schema, table_name)) IS NULL THEN
            RAISE EXCEPTION 'authorization substrate requires relation %.%',
                target_schema,
                table_name;
        END IF;
        IF table_name IN ('projects', 'code_indexed_projects') THEN
            project_expression := 'id = gobby_agent_auth.current_project_id()';
        ELSE
            project_expression := 'project_id = gobby_agent_auth.current_project_id()';
        END IF;
        EXECUTE format('ALTER TABLE %I.%I ENABLE ROW LEVEL SECURITY', target_schema, table_name);
        EXECUTE format('ALTER TABLE %I.%I FORCE ROW LEVEL SECURITY', target_schema, table_name);
        EXECUTE format(
            'DROP POLICY IF EXISTS gobby_agent_project_scope ON %I.%I',
            target_schema,
            table_name
        );
        EXECUTE format(
            'CREATE POLICY gobby_agent_project_scope ON %I.%I TO %I '
            'USING (%s) WITH CHECK (%s)',
            target_schema,
            table_name,
            'gobby_gcode_capability',
            project_expression,
            project_expression
        );
        EXECUTE format(
            'DROP POLICY IF EXISTS gobby_daemon_runtime_access ON %I.%I',
            target_schema,
            table_name
        );
        EXECUTE format(
            'CREATE POLICY gobby_daemon_runtime_access ON %I.%I TO %I '
            'USING (TRUE) WITH CHECK (TRUE)',
            target_schema,
            table_name,
            'gobby_daemon_runtime'
        );
        EXECUTE format(
            'DROP POLICY IF EXISTS gobby_migration_owner_access ON %I.%I',
            target_schema,
            table_name
        );
        EXECUTE format(
            'CREATE POLICY gobby_migration_owner_access ON %I.%I TO %I '
            'USING (TRUE) WITH CHECK (TRUE)',
            target_schema,
            table_name,
            migration_role
        );
    END LOOP;
END
$rls$;

DO $privileges$
DECLARE
    target_schema NAME := current_schema();
    target_database NAME := current_database();
    migration_role NAME := current_user;
BEGIN
    EXECUTE format('REVOKE CONNECT, TEMPORARY ON DATABASE %I FROM PUBLIC', target_database);
    EXECUTE format(
        'GRANT CONNECT ON DATABASE %I TO %I, %I',
        target_database,
        'gobby_gcode_capability',
        'gobby_daemon_runtime'
    );
    EXECUTE format('REVOKE CREATE, USAGE ON SCHEMA %I FROM PUBLIC', target_schema);
    EXECUTE format(
        'GRANT USAGE ON SCHEMA %I TO %I, %I',
        target_schema,
        'gobby_gcode_capability',
        'gobby_daemon_runtime'
    );
    EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM PUBLIC', target_schema);
    EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM PUBLIC', target_schema);
    EXECUTE format('REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA %I FROM PUBLIC', target_schema);
    EXECUTE format(
        'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA %I TO %I',
        target_schema,
        'gobby_daemon_runtime'
    );
    EXECUTE format(
        'GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA %I TO %I',
        target_schema,
        'gobby_daemon_runtime'
    );
    EXECUTE format(
        'GRANT SELECT (id, name, repo_path) ON %I.projects TO %I',
        target_schema,
        'gobby_gcode_capability'
    );
    EXECUTE format(
        'GRANT SELECT, INSERT, UPDATE, DELETE ON '
        '%I.code_indexed_projects, %I.code_indexed_files, %I.code_symbols, '
        '%I.code_imports, %I.code_calls, %I.code_content_chunks, '
        '%I.code_index_projection_cleanup_pending, %I.code_index_prune_dirty_projects TO %I',
        target_schema,
        target_schema,
        target_schema,
        target_schema,
        target_schema,
        target_schema,
        target_schema,
        target_schema,
        'gobby_gcode_capability'
    );
    EXECUTE format(
        'GRANT USAGE, SELECT ON SEQUENCE %I.code_imports_id_seq, %I.code_calls_id_seq TO %I',
        target_schema,
        target_schema,
        'gobby_gcode_capability'
    );
    EXECUTE format(
        'ALTER DEFAULT PRIVILEGES FOR ROLE %I REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC',
        migration_role
    );
    EXECUTE format(
        'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I REVOKE ALL ON TABLES FROM PUBLIC',
        migration_role,
        target_schema
    );
    EXECUTE format(
        'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I REVOKE ALL ON SEQUENCES FROM PUBLIC',
        migration_role,
        target_schema
    );
    EXECUTE format(
        'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I '
        'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I',
        migration_role,
        target_schema,
        'gobby_daemon_runtime'
    );
    EXECUTE format(
        'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I '
        'GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO %I',
        migration_role,
        target_schema,
        'gobby_daemon_runtime'
    );
END
$privileges$;

ALTER DEFAULT PRIVILEGES FOR ROLE gobby_agent_issuer
    REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE gobby_agent_issuer IN SCHEMA gobby_agent_auth
    REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE gobby_agent_issuer IN SCHEMA gobby_agent_auth
    REVOKE ALL ON SEQUENCES FROM PUBLIC;
