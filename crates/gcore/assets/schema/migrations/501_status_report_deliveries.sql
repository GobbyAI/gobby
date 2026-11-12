CREATE TABLE status_report_runs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    machine_id uuid NOT NULL,
    destination_key text NOT NULL,
    mode text NOT NULL CHECK (mode IN ('full', 'delta')),
    created_at timestamptz NOT NULL,
    snapshot jsonb,
    file_path text UNIQUE,
    artifact_digest text,
    delivered_at timestamptz,
    message_id uuid,
    CHECK ((delivered_at IS NULL) = (message_id IS NULL)),
    CHECK (delivered_at IS NULL OR (snapshot IS NOT NULL AND artifact_digest IS NOT NULL))
);
CREATE INDEX status_report_delivered_full_idx
    ON status_report_runs (project_id, machine_id, destination_key, id DESC)
    WHERE mode = 'full' AND delivered_at IS NOT NULL;
GRANT SELECT, INSERT, UPDATE ON status_report_runs TO gobby_daemon_runtime;
GRANT USAGE, SELECT ON SEQUENCE status_report_runs_id_seq TO gobby_daemon_runtime;
