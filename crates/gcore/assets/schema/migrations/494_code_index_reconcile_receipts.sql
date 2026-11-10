-- One receipt per project per nightly vector reconcile run. The daemon writes every
-- row and no gcode capability role touches receipts, so the table grants the daemon
-- runtime alone. Keyed by run so a rerun of the same run cannot double-count a project.
CREATE TABLE code_index_reconcile_receipts (
    machine_id uuid NOT NULL REFERENCES machines(id),
    run_id text NOT NULL,
    project_id uuid NOT NULL REFERENCES code_indexed_projects(id) ON DELETE CASCADE,
    mode text NOT NULL CHECK (mode IN ('dry_run', 'apply')),
    digest text,
    class_counts jsonb NOT NULL CHECK (jsonb_typeof(class_counts) = 'object'),
    outcome text NOT NULL CHECK (outcome IN ('completed', 'failed')),
    retries integer NOT NULL CHECK (retries >= 0),
    error text,
    started_at timestamp with time zone NOT NULL,
    finished_at timestamp with time zone NOT NULL,
    PRIMARY KEY (machine_id, run_id, project_id),
    CHECK (outcome = 'failed' OR error IS NULL)
);

CREATE INDEX idx_code_index_reconcile_receipts_project
    ON code_index_reconcile_receipts (machine_id, project_id, finished_at DESC);

ALTER TABLE code_index_reconcile_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE code_index_reconcile_receipts FORCE ROW LEVEL SECURITY;

CREATE POLICY gobby_daemon_runtime_access
    ON code_index_reconcile_receipts
    TO gobby_daemon_runtime
    USING (true)
    WITH CHECK (true);

CREATE POLICY gobby_migration_owner_access
    ON code_index_reconcile_receipts
    TO CURRENT_USER
    USING (true)
    WITH CHECK (true);

GRANT SELECT, INSERT ON TABLE code_index_reconcile_receipts TO gobby_daemon_runtime;
