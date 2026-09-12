-- Converge existing databases on the baseline ANY(ARRAY[...]) form of the gcode
-- read policies, so ParadeDB keeps its BM25 pushdown under RLS.
--
-- `gobby_gcode_project_read` was written as a disjunction:
--
--     (project_id = gobby_agent_auth.current_project_id())
--     OR (project_id = gobby_agent_auth.current_code_overlay_project_id())
--
-- ParadeDB's custom scan cannot render an OR of two non-constant legs as a
-- Tantivy filter, so it degrades each leg to `"indexed_query":"all"` -- every
-- document in the index instead of the documents matching the BM25 term. That
-- costs about 0.3s on its own, but it also destroys the scan's row estimate
-- (rows=1 planned against 3,278 actual), and the planner then puts the ParadeDB
-- scan on the inner side of a nested loop and re-executes it once per indexed
-- file. EXPLAIN (ANALYZE, BUFFERS) of gcode's content-count query as a scoped
-- capability role: 173 loops x 315ms, 176 million buffer hits, 54.5s total. The
-- same query as the table owner, whose policy is `USING true`, takes 0.06s.
--
-- Every Ask run died here: `ensure_isolation_code_index` runs a
-- `gcode search-content` smoke probe with a 10s cap, and it measured 65-119s.
--
-- Written as a single `= ANY (ARRAY[...])` qual the pushdown survives, and the
-- same replica measures 35.44s -> 0.38s. PostgreSQL already performs this
-- rewrite itself for btree index conditions in the same plan; only ParadeDB's
-- custom scan needs it spelled out.
--
-- The predicate is unchanged, NULLs included: `x = ANY(ARRAY[a, NULL])` is true
-- on a match and NULL otherwise, exactly like `x = a OR x = NULL`. No role, no
-- grant and no index changes, so a scoped principal sees precisely the rows it
-- saw before. These statements are what the baseline now expresses, so a fresh
-- database is unchanged and re-running this migration is a no-op.

ALTER POLICY gobby_gcode_project_read ON public.code_calls
    USING (project_id = ANY (ARRAY[gobby_agent_auth.current_project_id(), gobby_agent_auth.current_code_overlay_project_id()]));

ALTER POLICY gobby_gcode_project_read ON public.code_content_chunks
    USING (project_id = ANY (ARRAY[gobby_agent_auth.current_project_id(), gobby_agent_auth.current_code_overlay_project_id()]));

ALTER POLICY gobby_gcode_project_read ON public.code_imports
    USING (project_id = ANY (ARRAY[gobby_agent_auth.current_project_id(), gobby_agent_auth.current_code_overlay_project_id()]));

ALTER POLICY gobby_gcode_project_read ON public.code_index_projection_cleanup_pending
    USING (project_id = ANY (ARRAY[gobby_agent_auth.current_project_id(), gobby_agent_auth.current_code_overlay_project_id()]));

ALTER POLICY gobby_gcode_project_read ON public.code_index_prune_dirty_projects
    USING ((project_id = ANY (ARRAY[gobby_agent_auth.current_project_id(), gobby_agent_auth.current_code_overlay_project_id()]))
           AND (machine_id = gobby_agent_auth.current_machine_id()));

ALTER POLICY gobby_gcode_project_read ON public.code_indexed_file_states
    USING ((project_id = ANY (ARRAY[gobby_agent_auth.current_project_id(), gobby_agent_auth.current_code_overlay_project_id()]))
           AND (machine_id = gobby_agent_auth.current_machine_id()));

ALTER POLICY gobby_gcode_project_read ON public.code_indexed_files
    USING (project_id = ANY (ARRAY[gobby_agent_auth.current_project_id(), gobby_agent_auth.current_code_overlay_project_id()]));

ALTER POLICY gobby_gcode_project_read ON public.code_indexed_project_states
    USING ((project_id = ANY (ARRAY[gobby_agent_auth.current_project_id(), gobby_agent_auth.current_code_overlay_project_id()]))
           AND (machine_id = gobby_agent_auth.current_machine_id()));

ALTER POLICY gobby_gcode_project_read ON public.code_indexed_projects
    USING (id = ANY (ARRAY[gobby_agent_auth.current_project_id(), gobby_agent_auth.current_code_overlay_project_id()]));

ALTER POLICY gobby_gcode_project_read ON public.code_inheritance
    USING (project_id = ANY (ARRAY[gobby_agent_auth.current_project_id(), gobby_agent_auth.current_code_overlay_project_id()]));

ALTER POLICY gobby_gcode_project_read ON public.code_symbols
    USING (project_id = ANY (ARRAY[gobby_agent_auth.current_project_id(), gobby_agent_auth.current_code_overlay_project_id()]));
