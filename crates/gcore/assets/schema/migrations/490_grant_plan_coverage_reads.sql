-- Plan coverage in db mode reads task rows from a developer seat through the
-- sanctioned read role, gobby_gcode_capability. Before this migration no row
-- security applied to public.tasks or public.task_dependencies, so a bare SELECT
-- grant would expose every project's tasks to every bound seat. Enable row
-- security and scope the capability role to its bound project. Pass-through
-- policies keep the existing non-owner roles' access unchanged.

ALTER TABLE public.tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.task_dependencies ENABLE ROW LEVEL SECURITY;

CREATE POLICY gobby_daemon_runtime_access ON public.tasks AS PERMISSIVE FOR ALL TO gobby_daemon_runtime USING (true) WITH CHECK (true);
CREATE POLICY gobby_agent_issuer_access ON public.tasks FOR SELECT TO gobby_agent_issuer USING (true);
CREATE POLICY gobby_query_readonly_access ON public.tasks FOR SELECT TO gobby_query_readonly USING (true);
CREATE POLICY gobby_gcode_project_read ON public.tasks FOR SELECT TO gobby_gcode_capability USING (project_id = gobby_agent_auth.current_project_id());

CREATE POLICY gobby_daemon_runtime_access ON public.task_dependencies AS PERMISSIVE FOR ALL TO gobby_daemon_runtime USING (true) WITH CHECK (true);
CREATE POLICY gobby_query_readonly_access ON public.task_dependencies FOR SELECT TO gobby_query_readonly USING (true);
CREATE POLICY gobby_gcode_project_read ON public.task_dependencies FOR SELECT TO gobby_gcode_capability USING (EXISTS (SELECT 1 FROM public.tasks AS t WHERE t.id = task_dependencies.task_id));

GRANT SELECT ON TABLE public.tasks, public.task_dependencies TO gobby_gcode_capability;
