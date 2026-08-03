-- Grant the daemon runtime role EXECUTE on public-schema functions.
-- Migration 369 revoked EXECUTE from PUBLIC while granting the runtime role
-- only table/sequence DML, so trigger-invoked functions (e.g.
-- refresh_task_state_bucket via the tasks-table trigger) failed with
-- InsufficientPrivilege once the daemon pool assumed gobby_daemon_runtime.
-- The capability role is untouched: its tables carry no triggers and its
-- only function surface is gobby_agent_auth.current_project_id().

GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO gobby_daemon_runtime;

DO $default_execute$
BEGIN
    EXECUTE format(
        'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
        'GRANT EXECUTE ON FUNCTIONS TO gobby_daemon_runtime',
        current_user
    );
END
$default_execute$;
