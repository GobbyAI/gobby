-- Read-only role for query_readonly. The tool runs each statement under
-- SET LOCAL ROLE gobby_query_readonly, so PostgreSQL checks privileges as this
-- role rather than the daemon runtime. The role receives column-level SELECT on
-- base tables in the migrated schema only: no views, no agent_auth schema, no
-- credential tables, and no credential-named columns. Tables created later get
-- no grant.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gobby_query_readonly') THEN
        CREATE ROLE gobby_query_readonly NOLOGIN NOINHERIT;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO gobby_query_readonly;

DO $$
DECLARE
    rel record;
    granted_columns text;
BEGIN
    FOR rel IN
        SELECT c.oid, n.nspname, c.relname
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = current_schema()
          AND c.relkind IN ('r', 'p')
          AND c.relname NOT IN ('secrets', 'secret_key_material', 'api_keys', 'auth_sessions')
    LOOP
        SELECT string_agg(format('%I', a.attname), ', ' ORDER BY a.attnum)
          INTO granted_columns
          FROM pg_attribute a
         WHERE a.attrelid = rel.oid
           AND a.attnum > 0
           AND NOT a.attisdropped
           AND a.attname !~ '(secret|encrypt|ciphertext|wrapped_dek|(^|_)kek_|key_hash|token_hash|password|credential|signing|(^|_)token$)'
           -- Connection URLs and args can carry tokens (query strings, userinfo,
           -- argv flags), so they are withheld like the credential-bearing columns.
           AND (rel.relname, a.attname) NOT IN (
               ('config_store', 'value'),
               ('mcp_servers', 'env'),
               ('mcp_servers', 'headers'),
               ('mcp_servers', 'template_values'),
               ('mcp_servers', 'url'),
               ('mcp_servers', 'args'),
               ('clones', 'remote_url'),
               ('comms_channels', 'config_json'),
               ('comms_attachments', 'platform_url'),
               ('validation_run_records', 'argv')
           );
        IF granted_columns IS NOT NULL THEN
            EXECUTE format(
                'GRANT SELECT (%s) ON TABLE %I.%I TO gobby_query_readonly',
                granted_columns, rel.nspname, rel.relname
            );
        END IF;
    END LOOP;
END
$$;

GRANT gobby_query_readonly TO gobby_daemon_runtime;
