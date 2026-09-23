-- Agent-invokable CLI paths resolve the scoped project through
-- LocalProjectManager.get_by_name(), whose SELECT * requires table-level SELECT.
-- Row-level security still restricts the capability role to current_project_id().
GRANT SELECT ON TABLE public.projects TO gobby_gcode_capability;
