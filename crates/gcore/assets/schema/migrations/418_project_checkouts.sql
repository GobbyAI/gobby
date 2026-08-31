-- 412: machine-owned project_checkouts (path-independent project identity).
--
-- Live schema head was 411. BASELINE_VERSION stays 375; do not add this table
-- to baseline.sql. Fresh lineages execute this file after the baseline, so
-- every statement that can meet an existing object is guarded.
--
-- Do not add project_checkouts to the $gcode_rls$ write-policy inventory:
-- FORCE RLS plus a SELECT-only capability policy keeps mutation denied.
-- Capability UPDATE is lock-only (SELECT ... FOR SHARE). Do not apply this
-- migration to the live hub until P6.

CREATE TABLE IF NOT EXISTS project_checkouts (
    machine_id uuid NOT NULL,
    project_id uuid NOT NULL,
    root_path text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY project_checkouts ENABLE ROW LEVEL SECURITY;
ALTER TABLE ONLY project_checkouts FORCE ROW LEVEL SECURITY;

DO $constraints$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'project_checkouts_pkey'
          AND conrelid = 'project_checkouts'::regclass
    ) THEN
        ALTER TABLE ONLY project_checkouts
            ADD CONSTRAINT project_checkouts_pkey PRIMARY KEY (machine_id, project_id);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'project_checkouts_machine_id_root_path_key'
          AND conrelid = 'project_checkouts'::regclass
    ) THEN
        ALTER TABLE ONLY project_checkouts
            ADD CONSTRAINT project_checkouts_machine_id_root_path_key
            UNIQUE (machine_id, root_path);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'project_checkouts_machine_id_fkey'
          AND conrelid = 'project_checkouts'::regclass
    ) THEN
        ALTER TABLE ONLY project_checkouts
            ADD CONSTRAINT project_checkouts_machine_id_fkey
            FOREIGN KEY (machine_id) REFERENCES machines(id) ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'project_checkouts_project_id_fkey'
          AND conrelid = 'project_checkouts'::regclass
    ) THEN
        ALTER TABLE ONLY project_checkouts
            ADD CONSTRAINT project_checkouts_project_id_fkey
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE;
    END IF;
END
$constraints$;

DROP POLICY IF EXISTS gobby_agent_project_scope ON project_checkouts;
DROP POLICY IF EXISTS gobby_daemon_runtime_access ON project_checkouts;
CREATE POLICY gobby_daemon_runtime_access ON project_checkouts
    TO gobby_daemon_runtime USING (TRUE) WITH CHECK (TRUE);

DROP POLICY IF EXISTS gobby_migration_owner_access ON project_checkouts;
CREATE POLICY gobby_migration_owner_access ON project_checkouts
    TO CURRENT_USER USING (TRUE) WITH CHECK (TRUE);

DROP POLICY IF EXISTS gobby_gcode_project_read ON project_checkouts;
CREATE POLICY gobby_gcode_project_read ON project_checkouts
    FOR SELECT TO gobby_gcode_capability
    USING (
        project_id = gobby_agent_auth.current_project_id()
        AND machine_id = gobby_agent_auth.current_machine_id()
    );

-- SELECT ... FOR SHARE is authorized as UPDATE under FORCE RLS. USING matches
-- the SELECT policy; WITH CHECK (false) keeps actual row mutation denied.
DROP POLICY IF EXISTS gobby_gcode_project_update ON project_checkouts;
CREATE POLICY gobby_gcode_project_update ON project_checkouts
    FOR UPDATE TO gobby_gcode_capability
    USING (
        project_id = gobby_agent_auth.current_project_id()
        AND machine_id = gobby_agent_auth.current_machine_id()
    )
    WITH CHECK (false);

GRANT SELECT, INSERT, DELETE, UPDATE ON TABLE project_checkouts TO gobby_daemon_runtime;
GRANT SELECT (deleted_at) ON TABLE projects TO gobby_gcode_capability;
GRANT SELECT (machine_id, project_id, root_path),
      UPDATE (machine_id, project_id, root_path)
    ON TABLE project_checkouts TO gobby_gcode_capability;
