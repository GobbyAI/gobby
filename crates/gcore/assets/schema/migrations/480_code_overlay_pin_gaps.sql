-- Tracked paths an overlay pin leaves unpinned, so the overlay indexer can index them.
-- Gaps have no content version to reference, so they stay out of code_overlay_pins.
CREATE TABLE code_overlay_pin_gaps (
    machine_id uuid NOT NULL,
    overlay_project_id uuid NOT NULL,
    file_path text NOT NULL,
    CONSTRAINT code_overlay_pin_gaps_pkey PRIMARY KEY (machine_id, overlay_project_id, file_path)
);
ALTER TABLE code_overlay_pin_gaps ENABLE ROW LEVEL SECURITY;
ALTER TABLE code_overlay_pin_gaps FORCE ROW LEVEL SECURITY;
CREATE POLICY gobby_daemon_runtime_access ON code_overlay_pin_gaps TO gobby_daemon_runtime USING (true) WITH CHECK (true);
CREATE POLICY gobby_migration_owner_access ON code_overlay_pin_gaps TO CURRENT_USER USING (true) WITH CHECK (true);
CREATE POLICY gobby_gcode_project_read ON code_overlay_pin_gaps FOR SELECT TO gobby_gcode_capability USING ((overlay_project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id())) AND (machine_id = gobby_agent_auth.current_machine_id()));
CREATE POLICY gobby_gcode_project_insert ON code_overlay_pin_gaps FOR INSERT TO gobby_gcode_capability WITH CHECK ((overlay_project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id())) AND (machine_id = gobby_agent_auth.current_machine_id()));
CREATE POLICY gobby_gcode_project_delete ON code_overlay_pin_gaps FOR DELETE TO gobby_gcode_capability USING ((overlay_project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id())) AND (machine_id = gobby_agent_auth.current_machine_id()));
REVOKE ALL ON code_overlay_pin_gaps FROM PUBLIC;
GRANT SELECT, INSERT, DELETE ON code_overlay_pin_gaps TO gobby_daemon_runtime;
GRANT SELECT, INSERT, DELETE ON code_overlay_pin_gaps TO gobby_gcode_capability;
