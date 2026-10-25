-- Content versions are shared across machines. Their owner must see every pin
-- referencing them so content GC treats those pins as roots under FORCE RLS.
-- Overlay mutation authority stays frozen in the existing write policies.
ALTER POLICY gobby_gcode_project_read ON code_overlay_pins
USING (
    (
        overlay_project_id = COALESCE(
            gobby_agent_auth.current_code_overlay_project_id(),
            gobby_agent_auth.current_project_id()
        )
        AND machine_id = gobby_agent_auth.current_machine_id()
    )
    OR source_project_id = COALESCE(
        gobby_agent_auth.current_code_overlay_project_id(),
        gobby_agent_auth.current_project_id()
    )
);
