-- Run identity is supplied by the verified capability, never by the request body.
CREATE TABLE public.validation_run_records (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL,
    session_id uuid NOT NULL,
    agent_run_id text NOT NULL,
    argv jsonb NOT NULL,
    cwd text NOT NULL,
    repo_root text NOT NULL,
    started_at timestamptz NOT NULL,
    completed_at timestamptz NOT NULL,
    outcome text NOT NULL CHECK (outcome IN ('passed', 'failed', 'timed_out', 'interrupted')),
    exit_code integer,
    tree_start text,
    tree_end text,
    python_version text,
    executable text,
    gdaemon_identity text,
    reusable boolean NOT NULL,
    not_reusable_reason text,
    CHECK (reusable = (not_reusable_reason IS NULL)),
    CHECK (outcome NOT IN ('timed_out', 'interrupted') OR exit_code IS NULL)
);
CREATE INDEX validation_run_records_project_completed
    ON public.validation_run_records (project_id, completed_at DESC);
REVOKE ALL ON public.validation_run_records FROM PUBLIC;
-- The baseline's default table privileges include UPDATE; completed records are immutable.
REVOKE ALL ON public.validation_run_records FROM gobby_daemon_runtime;
GRANT SELECT, INSERT, DELETE ON public.validation_run_records TO gobby_daemon_runtime;
