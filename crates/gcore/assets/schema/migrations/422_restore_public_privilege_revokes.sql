-- Restore the PUBLIC privilege revokes lost in the baseline@420 flatten.
--
-- Before #21479 the baseline ran a `DO $privileges$` block that hardened the
-- database and schema against the PUBLIC pseudo-role. `scripts/flatten_schema.py`
-- regenerated the baseline as a pg_dump, and pg_dump cannot carry three of those
-- statements:
--
--   * database-level ACLs are only emitted under `--create`, so
--     `REVOKE CONNECT, TEMPORARY ON DATABASE ... FROM PUBLIC` vanished and every
--     login role regained TEMPORARY;
--   * the dumped schema ACL kept `REVOKE USAGE` but dropped `REVOKE CREATE`;
--   * the default-privilege revokes vanished, so EXECUTE on any function created
--     in the schema *after* apply is granted to PUBLIC by PostgreSQL's own
--     default. The per-function `REVOKE ALL ON FUNCTION ... FROM PUBLIC` lines
--     pg_dump did emit cover only the functions that existed at dump time.
--
-- The observable hole: an issued agent principal could `CREATE TEMP TABLE` and
-- `EXECUTE` any function created after the schema was applied, both of which
-- `tests/storage/test_postgres_agent_authorization.py` requires be denied.
--
-- Agent principals keep CONNECT because `issue_principal` grants them
-- `gobby_gcode_capability` with `INHERIT TRUE`, and that role is granted CONNECT
-- below — the same path the pre-flatten baseline used.
--
-- Everything here is idempotent, and only the statements pg_dump could not carry
-- are restored; the grants baseline@420 already expresses are left alone.

DO $restore_public_revokes$
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

    EXECUTE format('REVOKE CREATE ON SCHEMA %I FROM PUBLIC', target_schema);

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
END
$restore_public_revokes$;

ALTER DEFAULT PRIVILEGES FOR ROLE gobby_agent_issuer
    REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE gobby_agent_issuer IN SCHEMA gobby_agent_auth
    REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE gobby_agent_issuer IN SCHEMA gobby_agent_auth
    REVOKE ALL ON SEQUENCES FROM PUBLIC;
