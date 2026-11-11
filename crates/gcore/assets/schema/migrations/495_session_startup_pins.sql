-- A deferred startup pins the CLI its kernel-attested hook came from: the pid and that
-- process's start time, so a reused pid is never the same CLI. The first pin stands
-- until its row is created, or until the pinned process is proven dead.
ALTER TABLE session_startup_marks
    ADD COLUMN cli_pid integer,
    ADD COLUMN cli_create_time double precision,
    ADD CONSTRAINT session_startup_marks_pin_complete CHECK (
        (cli_pid IS NULL AND cli_create_time IS NULL)
        OR (cli_pid > 0 AND cli_create_time IS NOT NULL)
    );
GRANT UPDATE ON session_startup_marks TO gobby_daemon_runtime;

-- The shared Codex app-server worker that registered each hand-started thread. gmcp under
-- that worker binds a call to the thread only while this worker, proven by pid and start
-- time, is the one recorded. Each hook from the worker refreshes seen_at; the session
-- expiry job prunes rows no hook has refreshed for 7 days.
CREATE TABLE codex_thread_hosts (
    machine_id text NOT NULL,
    external_id text NOT NULL,
    host_pid integer NOT NULL CHECK (host_pid > 0),
    host_create_time double precision NOT NULL,
    seen_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (machine_id, external_id)
);
CREATE INDEX codex_thread_hosts_seen_at ON codex_thread_hosts (seen_at);
GRANT SELECT, INSERT, UPDATE, DELETE ON codex_thread_hosts TO gobby_daemon_runtime;
