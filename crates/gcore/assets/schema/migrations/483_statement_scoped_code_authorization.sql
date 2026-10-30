-- Authorization inputs depend on the current principal, not the scanned row.
-- Scalar subqueries make them InitPlans: once per statement, including prepared
-- executions, instead of once per tuple. Keep every read/write scope unchanged,
-- including registered sibling reads, cross-machine content-GC reads, and frozen
-- issuing-project/machine writes. The uuid[] cast retains a single scalar ANY
-- qual so pg_search still pushes the search predicate into its BM25 scan.
ALTER POLICY "gobby_gcode_project_delete" ON public."code_calls" USING ((project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))));

ALTER POLICY "gobby_gcode_project_insert" ON public."code_calls" WITH CHECK ((project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))));

ALTER POLICY "gobby_gcode_project_read" ON public."code_calls" USING ((project_id = ANY ((SELECT gobby_agent_auth.current_read_code_project_ids())::uuid[])));

ALTER POLICY "gobby_gcode_project_update" ON public."code_calls" USING ((project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id())))) WITH CHECK ((project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))));

ALTER POLICY "gobby_gcode_project_delete" ON public."code_communities" USING (((project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))) AND (machine_id = (SELECT gobby_agent_auth.current_machine_id()))));

ALTER POLICY "gobby_gcode_project_insert" ON public."code_communities" WITH CHECK (((project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))) AND (machine_id = (SELECT gobby_agent_auth.current_machine_id()))));

ALTER POLICY "gobby_gcode_project_read" ON public."code_communities" USING (((project_id = ANY ((SELECT gobby_agent_auth.current_read_code_project_ids())::uuid[])) AND (machine_id = (SELECT gobby_agent_auth.current_machine_id()))));

ALTER POLICY "gobby_gcode_project_update" ON public."code_communities" USING (((project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))) AND (machine_id = (SELECT gobby_agent_auth.current_machine_id())))) WITH CHECK (((project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))) AND (machine_id = (SELECT gobby_agent_auth.current_machine_id()))));

ALTER POLICY "gobby_gcode_project_delete" ON public."code_content_chunks" USING ((project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))));

ALTER POLICY "gobby_gcode_project_insert" ON public."code_content_chunks" WITH CHECK ((project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))));

ALTER POLICY "gobby_gcode_project_read" ON public."code_content_chunks" USING ((project_id = ANY ((SELECT gobby_agent_auth.current_read_code_project_ids())::uuid[])));

ALTER POLICY "gobby_gcode_project_update" ON public."code_content_chunks" USING ((project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id())))) WITH CHECK ((project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))));

ALTER POLICY "gobby_gcode_project_delete" ON public."code_imports" USING ((project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))));

ALTER POLICY "gobby_gcode_project_insert" ON public."code_imports" WITH CHECK ((project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))));

ALTER POLICY "gobby_gcode_project_read" ON public."code_imports" USING ((project_id = ANY ((SELECT gobby_agent_auth.current_read_code_project_ids())::uuid[])));

ALTER POLICY "gobby_gcode_project_update" ON public."code_imports" USING ((project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id())))) WITH CHECK ((project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))));

ALTER POLICY "gobby_gcode_project_delete" ON public."code_index_projection_cleanup_pending" USING ((project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))));

ALTER POLICY "gobby_gcode_project_insert" ON public."code_index_projection_cleanup_pending" WITH CHECK ((project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))));

ALTER POLICY "gobby_gcode_project_read" ON public."code_index_projection_cleanup_pending" USING ((project_id = ANY ((SELECT gobby_agent_auth.current_read_code_project_ids())::uuid[])));

ALTER POLICY "gobby_gcode_project_update" ON public."code_index_projection_cleanup_pending" USING ((project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id())))) WITH CHECK ((project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))));

ALTER POLICY "gobby_gcode_project_delete" ON public."code_index_prune_dirty_projects" USING (((project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))) AND (machine_id = (SELECT gobby_agent_auth.current_machine_id()))));

ALTER POLICY "gobby_gcode_project_insert" ON public."code_index_prune_dirty_projects" WITH CHECK (((project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))) AND (machine_id = (SELECT gobby_agent_auth.current_machine_id()))));

ALTER POLICY "gobby_gcode_project_read" ON public."code_index_prune_dirty_projects" USING (((project_id = ANY ((SELECT gobby_agent_auth.current_read_code_project_ids())::uuid[])) AND (machine_id = (SELECT gobby_agent_auth.current_machine_id()))));

ALTER POLICY "gobby_gcode_project_update" ON public."code_index_prune_dirty_projects" USING (((project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))) AND (machine_id = (SELECT gobby_agent_auth.current_machine_id())))) WITH CHECK (((project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))) AND (machine_id = (SELECT gobby_agent_auth.current_machine_id()))));

ALTER POLICY "gobby_gcode_project_delete" ON public."code_indexed_file_states" USING (((project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))) AND (machine_id = (SELECT gobby_agent_auth.current_machine_id()))));

ALTER POLICY "gobby_gcode_project_insert" ON public."code_indexed_file_states" WITH CHECK (((project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))) AND (machine_id = (SELECT gobby_agent_auth.current_machine_id()))));

ALTER POLICY "gobby_gcode_project_read" ON public."code_indexed_file_states" USING ((project_id = ANY ((SELECT gobby_agent_auth.current_read_code_project_ids())::uuid[])));

ALTER POLICY "gobby_gcode_project_update" ON public."code_indexed_file_states" USING (((project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))) AND (machine_id = (SELECT gobby_agent_auth.current_machine_id())))) WITH CHECK (((project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))) AND (machine_id = (SELECT gobby_agent_auth.current_machine_id()))));

ALTER POLICY "gobby_gcode_project_delete" ON public."code_indexed_files" USING ((project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))));

ALTER POLICY "gobby_gcode_project_insert" ON public."code_indexed_files" WITH CHECK ((project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))));

ALTER POLICY "gobby_gcode_project_read" ON public."code_indexed_files" USING ((project_id = ANY ((SELECT gobby_agent_auth.current_read_code_project_ids())::uuid[])));

ALTER POLICY "gobby_gcode_project_update" ON public."code_indexed_files" USING ((project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id())))) WITH CHECK ((project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))));

ALTER POLICY "gobby_gcode_project_delete" ON public."code_indexed_project_states" USING (((project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))) AND (machine_id = (SELECT gobby_agent_auth.current_machine_id()))));

ALTER POLICY "gobby_gcode_project_insert" ON public."code_indexed_project_states" WITH CHECK (((project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))) AND (machine_id = (SELECT gobby_agent_auth.current_machine_id()))));

ALTER POLICY "gobby_gcode_project_read" ON public."code_indexed_project_states" USING (((project_id = ANY ((SELECT gobby_agent_auth.current_read_code_project_ids())::uuid[])) AND (machine_id = (SELECT gobby_agent_auth.current_machine_id()))));

ALTER POLICY "gobby_gcode_project_update" ON public."code_indexed_project_states" USING (((project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))) AND (machine_id = (SELECT gobby_agent_auth.current_machine_id())))) WITH CHECK (((project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))) AND (machine_id = (SELECT gobby_agent_auth.current_machine_id()))));

ALTER POLICY "gobby_gcode_project_delete" ON public."code_indexed_projects" USING ((id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))));

ALTER POLICY "gobby_gcode_project_insert" ON public."code_indexed_projects" WITH CHECK ((id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))));

ALTER POLICY "gobby_gcode_project_read" ON public."code_indexed_projects" USING ((id = ANY ((SELECT gobby_agent_auth.current_read_code_project_ids())::uuid[])));

ALTER POLICY "gobby_gcode_project_update" ON public."code_indexed_projects" USING ((id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id())))) WITH CHECK ((id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))));

ALTER POLICY "gobby_gcode_project_delete" ON public."code_inheritance" USING ((project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))));

ALTER POLICY "gobby_gcode_project_insert" ON public."code_inheritance" WITH CHECK ((project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))));

ALTER POLICY "gobby_gcode_project_read" ON public."code_inheritance" USING ((project_id = ANY ((SELECT gobby_agent_auth.current_read_code_project_ids())::uuid[])));

ALTER POLICY "gobby_gcode_project_update" ON public."code_inheritance" USING ((project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id())))) WITH CHECK ((project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))));

ALTER POLICY "gobby_gcode_project_delete" ON public."code_overlay_pin_gaps" USING (((overlay_project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))) AND (machine_id = (SELECT gobby_agent_auth.current_machine_id()))));

ALTER POLICY "gobby_gcode_project_insert" ON public."code_overlay_pin_gaps" WITH CHECK (((overlay_project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))) AND (machine_id = (SELECT gobby_agent_auth.current_machine_id()))));

ALTER POLICY "gobby_gcode_project_read" ON public."code_overlay_pin_gaps" USING (((overlay_project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))) AND (machine_id = (SELECT gobby_agent_auth.current_machine_id()))));

ALTER POLICY "gobby_gcode_project_delete" ON public."code_overlay_pins" USING (((overlay_project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))) AND (machine_id = (SELECT gobby_agent_auth.current_machine_id()))));

ALTER POLICY "gobby_gcode_project_insert" ON public."code_overlay_pins" WITH CHECK (((overlay_project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))) AND (machine_id = (SELECT gobby_agent_auth.current_machine_id())) AND ((source_project_id = (SELECT gobby_agent_auth.current_project_id())) OR (source_project_id = (SELECT gobby_agent_auth.current_code_overlay_project_id())))));

ALTER POLICY "gobby_gcode_project_read" ON public."code_overlay_pins" USING ((((overlay_project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))) AND (machine_id = (SELECT gobby_agent_auth.current_machine_id()))) OR (source_project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id())))));

ALTER POLICY "gobby_gcode_project_update" ON public."code_overlay_pins" USING (((overlay_project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))) AND (machine_id = (SELECT gobby_agent_auth.current_machine_id())))) WITH CHECK (((overlay_project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))) AND (machine_id = (SELECT gobby_agent_auth.current_machine_id())) AND ((source_project_id = (SELECT gobby_agent_auth.current_project_id())) OR (source_project_id = (SELECT gobby_agent_auth.current_code_overlay_project_id())))));

ALTER POLICY "gobby_gcode_project_delete" ON public."code_symbols" USING ((project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))));

ALTER POLICY "gobby_gcode_project_insert" ON public."code_symbols" WITH CHECK ((project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))));

ALTER POLICY "gobby_gcode_project_read" ON public."code_symbols" USING ((project_id = ANY ((SELECT gobby_agent_auth.current_read_code_project_ids())::uuid[])));

ALTER POLICY "gobby_gcode_project_update" ON public."code_symbols" USING ((project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id())))) WITH CHECK ((project_id = COALESCE((SELECT gobby_agent_auth.current_code_overlay_project_id()), (SELECT gobby_agent_auth.current_project_id()))));

ALTER POLICY "gobby_gcode_project_read" ON public."project_checkouts" USING (((project_id = (SELECT gobby_agent_auth.current_project_id())) AND (machine_id = (SELECT gobby_agent_auth.current_machine_id()))));

ALTER POLICY "gobby_gcode_project_update" ON public."project_checkouts" USING (((project_id = (SELECT gobby_agent_auth.current_project_id())) AND (machine_id = (SELECT gobby_agent_auth.current_machine_id())))) WITH CHECK (false);

-- Stop-hook wait checks filter by session; the primary key leads with completion.
CREATE INDEX idx_completion_subscribers_session ON public.completion_subscribers (session_id);

-- Pane lookup needs a session's terminal history; reconciliation needs only the
-- small unsettled tail, even when all historical terminals belong to one machine.
CREATE INDEX idx_terminals_session ON public.terminals (session_id, updated_at DESC, id DESC);
CREATE INDEX idx_terminals_reconcilable ON public.terminals (machine_id, created_at)
    WHERE state IN ('pending', 'live', 'orphaned');

-- LIMIT alone does not bound retention's scan/sort over all delivered messages.
CREATE INDEX idx_ism_delivered_retention ON public.inter_session_messages (delivered_at, id)
    WHERE delivered_at IS NOT NULL;
