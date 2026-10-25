-- Content rows and their foreign keys ignore RLS, so content GC must see every
-- machine's file states for its own projects to keep content another machine
-- still references. Direct reads of the table are the contract the prune relies
-- on, so the project read widens here instead of through a SECURITY DEFINER
-- helper. Write policies keep their issuing-machine restriction, so cross-machine
-- rows grant no update or delete authority. This supersedes 474's machine term
-- for code_indexed_file_states only.
ALTER POLICY gobby_gcode_project_read ON public.code_indexed_file_states
    USING (project_id = ANY (gobby_agent_auth.current_read_code_project_ids()));
