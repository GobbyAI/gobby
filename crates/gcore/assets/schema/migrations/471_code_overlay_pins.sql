-- Clean tracked selectors carry a Git blob ID that a content-hash change clears.
ALTER TABLE code_indexed_file_states ADD COLUMN git_blob_id text;

CREATE FUNCTION clear_code_indexed_file_states_git_blob()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
IF NEW.content_hash IS DISTINCT FROM OLD.content_hash THEN
NEW.git_blob_id := NULL;
END IF;
RETURN NEW;
END;
$function$;

CREATE TRIGGER trg_code_indexed_file_states_git_blob_clear
BEFORE UPDATE OF content_hash ON code_indexed_file_states
FOR EACH ROW EXECUTE FUNCTION clear_code_indexed_file_states_git_blob();

-- Overlay-owned pins of a source project's immutable content version, with projection state.
CREATE TABLE code_overlay_pins (
    machine_id uuid NOT NULL,
    overlay_project_id uuid NOT NULL,
    file_path text NOT NULL,
    source_project_id uuid NOT NULL,
    content_hash text NOT NULL,
    git_blob_id text,
    graph_projected boolean DEFAULT false NOT NULL,
    vector_projected boolean DEFAULT false NOT NULL,
    graph_attempted_at timestamp with time zone,
    vector_attempted_at timestamp with time zone,
    CONSTRAINT code_overlay_pins_pkey PRIMARY KEY (machine_id, overlay_project_id, file_path),
    CONSTRAINT code_overlay_pins_source_content_fkey FOREIGN KEY (source_project_id, file_path, content_hash) REFERENCES code_indexed_files(project_id, file_path, content_hash)
);
ALTER TABLE code_overlay_pins ENABLE ROW LEVEL SECURITY;
ALTER TABLE code_overlay_pins FORCE ROW LEVEL SECURITY;
CREATE POLICY gobby_daemon_runtime_access ON code_overlay_pins TO gobby_daemon_runtime USING (true) WITH CHECK (true);
CREATE POLICY gobby_migration_owner_access ON code_overlay_pins TO CURRENT_USER USING (true) WITH CHECK (true);
CREATE POLICY gobby_gcode_project_read ON code_overlay_pins FOR SELECT TO gobby_gcode_capability USING ((overlay_project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id())) AND (machine_id = gobby_agent_auth.current_machine_id()));
CREATE POLICY gobby_gcode_project_insert ON code_overlay_pins FOR INSERT TO gobby_gcode_capability WITH CHECK ((overlay_project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id())) AND (machine_id = gobby_agent_auth.current_machine_id()));
CREATE POLICY gobby_gcode_project_update ON code_overlay_pins FOR UPDATE TO gobby_gcode_capability USING ((overlay_project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id())) AND (machine_id = gobby_agent_auth.current_machine_id())) WITH CHECK ((overlay_project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id())) AND (machine_id = gobby_agent_auth.current_machine_id()));
CREATE POLICY gobby_gcode_project_delete ON code_overlay_pins FOR DELETE TO gobby_gcode_capability USING ((overlay_project_id = COALESCE(gobby_agent_auth.current_code_overlay_project_id(), gobby_agent_auth.current_project_id())) AND (machine_id = gobby_agent_auth.current_machine_id()));
REVOKE ALL ON code_overlay_pins FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON code_overlay_pins TO gobby_daemon_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON code_overlay_pins TO gobby_gcode_capability;
