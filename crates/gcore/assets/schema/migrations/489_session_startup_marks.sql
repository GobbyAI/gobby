-- A CLI's own startup deferred its thread's row to the first activity hook. The mark
-- lets that first registration search for the thread's fresh seat, even across a daemon
-- restart. Registration consumes the mark; the session expiry job prunes old ones.
CREATE TABLE session_startup_marks (
    machine_id text NOT NULL,
    source text NOT NULL,
    external_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (machine_id, source, external_id)
);
CREATE INDEX session_startup_marks_created_at ON session_startup_marks (created_at);
GRANT SELECT, INSERT, DELETE ON session_startup_marks TO gobby_daemon_runtime;
