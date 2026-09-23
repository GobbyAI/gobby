-- Agent-invokable CLI paths resolve the scoped project and its registered checkout
-- through managers whose SELECT * queries require table-level SELECT. Row-level
-- security still restricts both relations to current_project_id().
GRANT SELECT ON TABLE public.projects TO gobby_gcode_capability;
GRANT SELECT ON TABLE public.project_checkouts TO gobby_gcode_capability;
