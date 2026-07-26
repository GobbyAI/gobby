Fix the following issues. The issues can be from different files or can overlap on same lines in one file.

In @.gobby/plans/herdr-interface-backend-foundation.md at line 22, Update the database-work guidance in the migration plan to require hub Postgres transactions with numbered $N placeholders instead of psycopg %s placeholders, and revise any related SQL examples in the document to follow that contract.

In @crates/gwiki/src/ingest/mod.rs around lines 153 - 168, Update source-note discovery around source_notes_root and the entry path checks to reject symlinks, using DirEntry::file_type() or symlink_metadata rather than is_dir/is_file. Canonicalize the accepted source-note path and enforce that it remains contained within the canonical vault_root before reading or indexing it; preserve the existing Markdown-file filtering and error mapping.

In @crates/gwiki/src/ingest/session_archive.rs around lines 416 - 431, Make the deletion flow around remove_session_page and delete_derived_rows retry-safe by ensuring the store cleanup is durably recorded or completed before the manifest entry and source files are irreversibly removed. Preserve the Deleted WikiIngestion record so failed cleanup can be retried during later reconciliation, and avoid leaving stale derived rows when the store operation fails.

In @docs/guides/shared-stack.md around lines 55 - 62, Update the trusted tailnet entry in the runtime CORS configuration example to use the documented <http://gobby-box.tailnet.ts.net>:* origin, matching the remote UI and GOBBY_DAEMON_URL examples while preserving the existing localhost origins.

In @docs/guides/system-requirements.md around lines 61 - 68, Add PostgreSQL as a row in the managed-stack service table, including its managed image, default local endpoint, and purpose. Keep the existing Qdrant and FalkorDB entries unchanged so the table documents all three required profiles stated above.

In @src/gobby/cli/pack.py around lines 154 - 167, Adjust the cleanup flow that invokes _start_services so a Docker restart failure is caught and recorded without aborting remaining cleanup. Preserve the original _do_pack() or _do_unpack() exception, and ensure unpack still reaches _start_daemon() even when _start_services raises ClickException.

In @src/gobby/cli/tasks/ai.py around lines 63 - 72, Update the inspection summary formatting around inspected_count and manifest_total in the result display flow to coerce only valid integer values, falling back to 0 for None, strings, or other non-int values. Preserve the existing Mapping guard, sample handling, and output format so the line always contains stable numeric counts.

In @src/gobby/config/bootstrap.py around lines 149 - 152, Update the validation flow around _validate_managed_database_url and the explicit_hub_backend condition so every resolve_database_url=True path requires a non-empty database_url, including existing bootstrap configurations that omit hub_backend or contain {}. Preserve managed URL validation when a URL is provided, and add coverage for an existing empty or partial bootstrap.yaml with runtime resolution enabled.

In @src/gobby/config/mcp.py around lines 175 - 176, Update the missing-name warning in the MCP server validation flow to include structured context via the logger’s extra fields, using a safe entry index or the validation reason. Keep the existing warning message and behavior unchanged, and match the adjacent invalid-entry warning’s structured logging pattern.

In @src/gobby/dispatch/stage_pipeline.py around lines 163 - 177, Update_spawn_on_main_loop so its asyncio.create_task failure path closes coro when task creation raises before spawned is assigned, mirroring the same-loop path’s cleanup while preserving spawned cancellation and registration.set_exception behavior.

In @src/gobby/dispatch/stage_pipeline.py around lines 152 - 178, Guard the main-loop scheduling call in the cross-loop branch of start_pipeline_action with RuntimeError handling, covering closure between is_closed() and call_soon_threadsafe(). On failure, close coro and route the error through the existing fail_start path so the mutex is released and the execution does not remain pending; preserve the existing registration behavior for successful scheduling.

In @src/gobby/dispatch/stage_pipeline.py around lines 179 - 195, Update the registration-timeout handling in the pipeline start flow to close the race between registration becoming RUNNING and its spawned task being published: after cancellation fails, briefly wait for the registration future to complete, retrieve the spawned task, and cancel it before calling fail_start("pipeline_start_registration_timeout"). Preserve coroutine closing only when registration.cancel() succeeds, and ensure exceptions or cancelled registrations remain safely handled.

In @src/gobby/hooks/session_coordinator.py around lines 537 - 542, Update the exception logging in the session stats flush handling near the coordinator’s logger warning to use a fixed message, pass the session identifier through extra={"session_id": session_id}, and retain exc_info=True; do not interpolate the exception or session ID into the message.

In @src/gobby/install/bundled_content_manifest.json around lines 5 - 12, Update the bundled content manifest to resolve the stale entries for rules/build/build-agent-safety.yaml and skills/build/SKILL.md: either restore both referenced files with their expected content and hashes, or remove their manifest entries so bundled-content validation passes.

In @src/gobby/install/shared/skills/bridge/SKILL.md around lines 163 - 166, Update the compaction-resume instructions in the processed-set reconstruction flow so only entries with status "done" are treated as already processed. Explicitly reconcile "doing" entries by either completing their work or resetting them to "to do" before continuing, while preserving the existing handling for completed entries.

In @src/gobby/install/shared/skills/bridge/SKILL.md around lines 147 - 150, Replace the bounded polling example in the “Other CLIs” guidance with an allowed watcher/wait mechanism that works when no-bash-sleep.yaml blocks Bash sleep. Preserve the existing behavior: detect a change to .moat/moat-tasks-detail.json and stop after approximately two minutes.

In @src/gobby/mcp_proxy/services/result_handling.py around lines 174 - 194, Move the Codex close-task reconciliation flow involving _reconcile_codex_close_transcript before the _workflow_handler missing-handler early return. Construct the required event and apply the existing failed-reconciliation response regardless of handler availability; only workflow evaluation should remain conditional on _workflow_handler.

In @src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py around lines 642 - 646, Update the exception handling around get_variables in the verification-evidence helper to catch only KeyError, ValueError, and TypeError, matching close_task’s existing handling. Keep the debug log and empty-tuple fallback unchanged, while allowing unrelated programming errors to propagate.

In @src/gobby/runner_service_readiness.py around lines 26 - 29, In the PostgreSQL readiness check, replace the broad Exception handler with a psycopg.Error handler so only database failures are wrapped as ManagedServiceReadinessError; allow non-database errors to propagate with their original traceback.

In @src/gobby/servers/routes/admin/_lifecycle.py around lines 346 - 348, Update the error logging in the reload_cache exception handler to use a fixed message without interpolating e, while retaining exc_info=True and adding structured context fields such as error_type derived from the caught exception. Preserve the existing error-level audit behavior.

In @src/gobby/storage/migrations.py around lines 146 - 150, Update the migration log call in the non-transactional PostgreSQL migration flow to emit version and name as structured logging context rather than only positional message arguments. Preserve the existing migration-identifying values and message semantics while using the logger’s supported contextual fields.

In @src/gobby/storage/migrations/325_recall_usefulness_shadow_index.sql around lines 2 - 3, Update the migration creating idx_recall_usefulness_request_source_protocol so an existing matching index marked INVALID is removed or rebuilt before the CREATE INDEX CONCURRENTLY step, while preserving the valid-index no-op behavior. Add a PostgreSQL migration retry test that simulates a failed concurrent creation and verifies the retry leaves a usable index with the required columns.

In @src/gobby/storage/tasks/_plan_enhancement.py around lines 96 - 130, Wrap set_artifacts_atomic, stages.route_enhancement, and update_task in one database transaction in the enhancement flow. Reuse the transaction pattern from de_escalate_task so any failure rolls back the artifact counters, stage routing, and description update together, preserving consistent retry behavior.

In @src/gobby/sync/task_github_import.py around lines 121 - 124, Update the issue-number validation in the GitHub import loop to accept integer subclasses while explicitly rejecting booleans, using an isinstance-based check. Before continuing past malformed issue numbers, emit structured logging with relevant issue context so skipped records are traceable.

In @src/gobby/sync/task_github_import.py around lines 140 - 148, Harden issue normalization in the import flow fed by _fetch_github_issues_mcp: parse labels defensively so string or malformed entries are ignored or handled without calling mapping methods on them, while preserving valid label names. Validate createdAt before passing it to _parse_timestamp and fall back to the existing current-time behavior when the value is missing or malformed, preventing one bad issue from failing the entire batch.

In @src/gobby/tasks/diff_paging.py around lines 33 - 38, Update diff_manifest to expose encode_bytes and ManifestParser as public names, then change diff_paging imports to use those public symbols rather than re-exporting the private _encode_bytes and _ManifestParser names. Update any references or exports accordingly while preserving existing behavior.

In @src/gobby/tasks/validation_coverage.py around lines 362 - 388, Ensure_logical_manifest_items receives the complete manifest rather than independently paged results, so adjacent old/new rename entries cannot be split across page boundaries. Update the assembling caller to combine all manifest pages before invoking it, and document or assert this full-manifest invariant at _logical_manifest_items.

In @src/gobby/tasks/validation_prompts.py around lines 14 - 27, Rename the module entry point from_build_prompt to the public symbol build_prompt, preserving its signature and behavior. Update every cross-module import and call site to reference build_prompt, including the importer currently using the underscored name.

In @src/gobby/tasks/validation_tool_loop.py around lines 553 - 562, Update the tool registration around BuiltinToolSpec name="list_verification_evidence" so it is added only when verification_items is non-empty. Preserve the existing handler and schema for cases with available evidence, while omitting the tool entirely when no verification items exist to prevent empty evidence artifacts and unnecessary calls.

In @src/gobby/tasks/validation_tool_loop.py around lines 447 - 457, Update list_verification_evidence_handler to explicitly reject negative offset values and non-positive limit values before slicing recorded_verification, returning the same invalid-pagination error behavior used by the diff handlers. Preserve the existing out-of-range check for offsets beyond the available evidence and ensure only valid paging values reach the emitted cursor range.

In @src/gobby/tasks/validation_tool_loop.py around lines 41 - 47, Extract the build_validation_builtins function and its directly related helpers or constants from validation_tool_loop.py into a dedicated validation builtins module, then update imports and call sites to use the new module while preserving behavior.

In @src/gobby/tasks/validation_tool_loop.py around lines 877 - 879, Update the verification planning logic around verification_pages and list_verification_evidence to calculate pages from the number of verification items using a shared VERIFICATION_PAGE_LIMIT constant. Extract the existing 50-item cap from verification_properties into that constant, reuse it for the tool’s limit and page calculation, and preserve zero pages when verification_items is empty.

In @src/gobby/workflows/definitions.py around lines 360 - 376, Update split_rule_definition_data so metadata preserves only fields explicitly present in the input, avoiding RuleDefinitionMetadata.model_dump() defaults that overwrite existing values during merges. Remove the unreachable field != "name" condition from raw_metadata collection, while continuing to handle name separately and validate the remaining metadata.

In @tests/cli/test_import.py around lines 153 - 173, Update test_github_cli_subprocess_timeouts_are_bounded to import and use the production timeout constant instead of hardcoding 180 in the TimeoutExpired instances, error-message assertion, and subprocess timeout assertions. Ensure all expected timeout values remain bound to that shared constant.

In @tests/code_index/test_gcode_storage_conformance.py at line 91, Update the DSN construction for scoped_database_url to preserve existing query parameters in postgres_database_url: append the schema options with “&” when a query string is already present, otherwise use “?”. Keep the existing postgres_schema search-path behavior unchanged.

In @tests/dispatch/test_dispatcher.py around lines 678 - 730, Extend test_stage_pipeline_spawn_fails_when_target_loop_does_not_acknowledge to exercise the case where_spawn_on_main_loop has transitioned its registration future to RUNNING but has not completed when PIPELINE_START_ACK_TIMEOUT_SECONDS expires. Verify start_pipeline_action performs fallback cleanup and cancels the spawned task, while preserving the existing timeout result, escalation reason, and execution status update assertions.

In @tests/mcp_proxy/tools/test_internal_action_tools.py around lines 177 - 187, Update test_backup_returns_structured_failure to annotate task_backup_registry with its concrete type, using the existing registry type definition, and add the @pytest.mark.unit decorator alongside @pytest.mark.asyncio. Preserve the test’s current setup and assertions.

In @tests/mcp_proxy/tools/test_task_commits.py at line 53, Annotate every patched_project_context fixture parameter in the listed test methods with MagicMock, including test_link_commit_success and all other affected methods, while preserving the existing return type hints and test behavior.

In @tests/mcp_proxy/tools/test_tasks_ops_artifacts.py around lines 15 - 17, Update the _registry helper’s return annotation to use the concrete InternalToolRegistry type instead of object, while preserving its existing LocalTaskManager creation and create_task_ops_registry return value.

In @tests/sessions/test_lifecycle.py around lines 741 - 743, Annotate the mock_db and mock_config parameters in test_pending_graph_memory_db_work_uses_memory_run_db and the other referenced test signatures at lines 1224, 1246, 1261, and 1281 with their concrete fixture types or approved protocol types, while preserving the existing -> None return annotations.

In @tests/skills/test_bridge_skill.py around lines 33 - 50, Add a regression assertion to test_bridge_skill_live_mode_contract that verifies live-session recovery reconciles interrupted "doing" entries rather than treating them as completed. Assert the relevant recovery/reconciliation wording or symbol exposed by_body(), alongside the existing "to do" and "processed" contract checks.

In @tests/storage/hub/test_postgres_baseline_application.py around lines 735 - 737, The fake class initializer must type its new inputs and the wiring test must verify the injected factory. Add appropriate type hints to __init__ parameters hub and autocommit_connection, capture the fake runner created by the wiring test, and assert its factory is db._open_advisory_lock_connection while preserving the existing test setup.

In @tests/tasks/test_validation_tool_loop.py around lines 332 - 360, Convert test_complete_evidence_verdict_survives_adapter_budget_flag from async to a synchronous test, remove its asyncio marker if present, and preserve the existing synchronous assertions and calls to ValidationVerdictSink.submit and normalize_tool_loop_result.

In @tests/tasks/test_validation_tool_loop.py around lines 670 - 684, Extend the list_verification_evidence pagination test to call the handler with offset 2, equal to the total of 2 items, and assert the intended boundary behavior for that response. Keep the existing offset 3 out-of-range assertion to preserve coverage of both boundary cases.

In @tests/test_import_pathing_trap.py at line 33, Update test_runner_uses_patched_config to add type annotations for both fixture parameters and retain the None return annotation, using the appropriate fixture types already established in the test suite.

In @tests/test_runner_service_readiness.py around lines 145 - 161, The test test_later_qdrant_or_falkordb_degradation_does_not_request_shutdown currently validates only the initial readiness check because its False health results are never consumed. Either add a daemon-lifecycle step that performs a later probe and asserts no shutdown request, or rename the test and update its assertions to describe a successful one-time readiness gate.
Fix the following issues. The issues can be from different files or can overlap on same lines in one file.

In @crates/gwiki/src/ingest/session_archive.rs around lines 416 - 428, Replace the manual PathBuf construction in the deletion flow with the existing derived_markdown_path helper, passing the appropriate entry identifier and preserving its validation/error propagation. Use the helper’s returned path in WikiIngestion while leaving remove_session_page and the surrounding deletion behavior unchanged.

In @src/gobby/adapters/codex_impl/item_normalization.py around lines 107 - 117, Update extract_yielded_cell_id so a non-matching non-blank line continues scanning all remaining lines and text blocks instead of returning None; return only when_YIELDED_CELL_RE matches, otherwise return None after the complete output has been examined.

In @src/gobby/cli/__init__.py at line 37, Update load_full_config_from_db and its callers to accept and reuse the existing database handle obtained through CliRuntime.require_database(), rather than creating a separate runtime hub or ConfigStore connection. Preserve the current configuration-loading behavior while ensuring each CLI invocation uses a single shared database connection.

In @src/gobby/cli/agents.py around lines 41 - 43, Update the stale unit test for get_agent_run_manager in tests/cli/test_cli_agents.py to provide the required CLI database context: either patch require_cli_database to return the expected database or execute the call within a CliRuntime context. Preserve the existing assertion that LocalAgentRunManager receives the returned database.

In @src/gobby/cli/daemon.py at line 777, Update the daemon status flow around collect_all_deps(require_cli_database(ctx)) to catch failures acquiring the runtime database, including unavailable or unconfigured PostgreSQL. On acquisition failure, continue rendering daemon PID and HTTP health while marking dependency storage as unavailable, preserving normal dependency collection when database access succeeds.

In @src/gobby/cli/rules.py around lines 46 - 60, Update the docstrings for _get_audit_manager and_audit_manager_context to state that the managers borrow the CLI runtime database rather than own or close it. Keep the context manager’s behavior unchanged and ensure its documentation no longer claims ownership or cleanup of the database.

In @src/gobby/cli/services.py around lines 138 - 149, Replace the Any annotation on db in is_falkordb_installed and the corresponding function around the second referenced block with a concrete HubDatabase type or minimal Protocol exposing the interface required by ConfigStore. Preserve both functions’ behavior while ensuring strict mypy can validate their database arguments.

In @src/gobby/cli/tasks/_utils/claims.py around lines 25 - 36, Update get_claimed_task_owners to import psycopg and include psycopg.Error in the existing exception handler covering db.fetchall, so query or connection failures return the established empty mapping fallback alongside the currently handled errors.

In @src/gobby/cli/utils_config.py around lines 187 - 192, Update the migration and project setup flow around hub_db.apply_migrations() and ensure_personal_project() to use a success flag with finally: mark the operation successful only after both calls complete, and close hub_db in finally when it was not successful. Remove the bare catch-all exception handler and preserve propagation of the original application exception.

In @src/gobby/runner_service_readiness.py around lines 57 - 69, Update the readiness-check cleanup around client.close() so a close failure cannot replace an existing ManagedServiceReadinessError or ping exception; preserve and propagate the original readiness failure while handling cleanup errors appropriately. Anchor the change to the client.close() finally block in the FalkorDB readiness check.

In @src/gobby/skills/sync.py around lines 254 - 268, Update sync_bundled_skills() to catch the filesystem and validation exceptions that SkillLoader.load_skill() can raise directly, including OSError and ValueError, in addition to SkillLoadError. Route each such failure through the existing error logging and load_errors accumulation so one inaccessible or invalid bundled skill does not abort the full sync.

In @src/gobby/storage/workflow_audit.py around lines 42 - 46, Update the documentation at the WorkflowAuditManager call sites, especially the short-lived manager description in rules.py, to state that the database is shared/borrowed rather than owned. Review every WorkflowAuditManager(...) construction and ensure callers pass the shared database and do not close it through the manager.

In @tests/agents/test_tmux_text_injection_integration.py around lines 79 - 90, The tmux text-injection test’s event timing logic must not depend on read-chunk boundaries. Update the capture/assertion flow around the paste terminator handling and the corresponding later assertion block so bytes after \x1b[201~ are identified by their event position or an explicit synchronization boundary, ensuring enter_at is assigned when the terminator and delayed carriage return arrive in one read.

In @tests/cli/test_services.py around lines 53 - 58, Replace test_injected_database_is_required with a meaningful contract test that calls is_falkordb_installed without the required db argument and asserts the expected failure. Remove the duplicated configuration setup or retain only the minimal setup needed to isolate the missing-database behavior, while leaving test_installed_when_config_store_host_and_port_exist unchanged.

In @tests/mcp_proxy/test_validation_integration.py around lines 24 - 30, Move the duplicated_task_validator helper from the validation test modules into a shared pytest fixture in tests/tasks/conftest.py or the existing common fixture module. Expose it as make_task_validator, preserve the TaskValidator construction and MagicMock(spec=HubDatabase) wiring, and update all affected tests to use the fixture instead of local helper definitions.

In @tests/sessions/test_codex_outcome_reconciliation.py around lines 101 - 109, Refetch verification_evidence from hook_manager.variables after reconcile_codex_transcript("platform-session") and before the final length assertion. Keep the initial evidence reference for the pre-replay check, but assert the post-replay count against the newly retrieved list.

In @tests/storage/hub/test_pool_ownership_boundaries.py around lines 40 - 52, Extend test_operational_layers_do_not_acquire_postgres_pools to also inspect ast.Import aliases and module-qualified constructor calls, tracking aliases for the PostgreSQL pool module and flagging accesses such as pg.PostgresHubDatabase(...). Preserve the existing ImportFrom detection and violation reporting, including the source path and line number.

In @web/src/components/chat/useChatPagePlans.ts around lines 145 - 151, Update clearCurrentConversationPendingPlan to assign nextPlanState to planStateRef.current before calling setPlanState, keeping the synchronous ref state aligned with the pending-plan state that React will render.
Fix the following issues. The issues can be from different files or can overlap on same lines in one file.

In @.gobby/plans/herdr-interface-backend-foundation.md at line 22, The migration numbers referenced throughout the plan are stale and collide with existing migration 329. Replace both planned migration numbers with the next currently free consecutive pair, then update every occurrence in targets, acceptance criteria, changelog entries, task-manifest references, and related plan text consistently.

In @src/gobby/cli/install.py around lines 606 - 610, Update the database/secret-store initialization failure warning in the install flow to use the repository’s structured logging convention rather than interpolating error details into the message. Preserve the warning event while supplying the exception type and message as named context fields, using the existing logger API and conventions.

In @src/gobby/dispatch/spawn.py around lines 115 - 127, Update the spawn flow after inspect_skill_composition in the dispatch handler to propagate skill_composition.allowed_tools into the spawned agent configuration or execution request. Preserve the existing failure_reason check and ensure the computed per-skill tool union, rather than only action.additional_skills, is used by the downstream spawn path.

In @src/gobby/failure_categories.py at line 69, Update the environment-command detection around_ENVIRONMENT_COMMAND_MARKERS and its use near the additional referenced lines so generic Git commands are not classified as ENVIRONMENT. Remove or narrow the broad "git " marker, retaining only the explicit setup/transport failure markers, while preserving classification of genuine infrastructure failures and allowing source-related git diff failures to trigger validation retries.

In @src/gobby/install/bundled_content_manifest.json at line 13, Regenerate the bundled-content manifest so it reflects the current files under src/gobby/install/shared, removing stale entries for rules/build/build-agent-safety.yaml and skills/build/SKILL.md. Ensure installer sync no longer attempts to bundle those missing paths while preserving hashes for existing bundled content.

In @src/gobby/install/shared/workflows/agents/merge-worker.yaml around lines 253 - 273, The merge_start, record_merge_result, and close_linked_github_issue error handlers incorrectly set merge_worker_ready_to_terminate before the required recovery and durable outcome exist. Remove termination from merge_start so the corrected source/target retry runs; keep the worker active after record_merge_result failures until merge_result_recorded is true; and gate close_linked_github_issue termination on merge_result_recorded so the worker only exits after a recorded merge result.

In @src/gobby/install/shared/workflows/agents/trajectory-monitor.yaml around lines 174 - 187, Update the on_mcp_success condition for gobby-tasks get_task to safely handle null result, state, and current_stage values by defaulting each nested lookup to an empty mapping before accessing is_closed, name, or state. Preserve the existing review_stale predicate behavior for populated responses.

In @src/gobby/mcp_proxy/tools/memory.py around lines 465 - 487, Require owner-project authorization in demote_memory_from_global and move_memory before calling the mutation methods: after retrieving existing, reject when existing.project_id does not equal current_project_id, while retaining _memory_allowed_in_current_project for read access checks. Preserve the existing not-found response and only allow demotion or movement for memories owned by the current project.

In @src/gobby/mcp_proxy/tools/tasks/_lifecycle_validation.py around lines 567 - 571, Remove the redundant classify_failure fallback in the validation handling block and use result.failure_category directly after checking validation_status. Keep blocking_reasons extraction and the existing non-valid status flow unchanged, relying on_validation_result_from_data to populate the category.

In @src/gobby/mcp_proxy/tools/tasks/_lifecycle_validation.py around lines 594 - 606, Update the infrastructure-failure ValidationResult branch in the surrounding lifecycle validation function to use error_type "validation_infrastructure_unavailable", matching the other infrastructure-unavailable returns in that function. Leave the existing failure_category and retry metadata unchanged.

In @src/gobby/memory/dream/service.py around lines 320 - 322, Update the exception log in the memory dream failure handler to include the project id when the target scope is PROJECT_ONLY, while retaining the existing scope kind context and behavior for other scopes. Use the project identifier already available on target_scope or the surrounding run context, and ensure the id is present directly in the logger.exception message.

In @src/gobby/memory/protocol.py around lines 252 - 255, Update MemoryRecord.from_dict() to read project_id with the PERSONAL_PROJECT_ID fallback when the field is absent, replacing the direct data["project_id"] access while preserving explicitly provided project IDs.

In @src/gobby/memory/services/knowledge_graph/maintenance.py around lines 26 - 46, The batch cleanup in remove_memories_from_graph() must preserve each memory’s is_global value instead of inferring global scope from project_id being None. Include m.is_global in the scope query, group deleted memories by the (project_id, is_global) pair, and pass both values to remove_orphaned_entities so global entities receive global cleanup.

In @src/gobby/memory/services/knowledge_graph/writer.py around lines 49 - 54, Thread the is_global value through KnowledgeGraphService.recluster_entities, recluster_project_entities, and write_entity_clusters, and include it in the parameter dictionaries for both self._falkor.query calls in write_entity_clusters. Ensure the value reaches every _project_scope-generated Cypher query, including cluster-set and cluster-clear operations, alongside project_id.

In @src/gobby/memory/services/projection_repair.py around lines 109 - 136, Batch the FalkorDB updates in_repair_falkor_memories using a single UNWIND-based query over the memories collection, following the existing batching pattern in write_entity_clusters or merge_cooccurrence_edges, while preserving the mismatch predicates and repaired-count result. Also verify that the underlying LocalMemoryManager.list_memories accepts limit=None as unbounded; adjust the call or implementation only if it does not.

In @src/gobby/storage/memories_crossrefs.py around lines 133 - 168, Update the memory_graph route’s get_all_crossrefs call to use the new scope-based signature instead of passing project_id. Construct or reuse the appropriate MemoryScope for the requested project, while preserving the existing memory_limit * 10 limit and route behavior.

In @src/gobby/storage/migrations/328_memory_global_visibility.sql around lines 11 - 16, Review the migration’s unconditional UPDATE of the memories table and avoid triggering a full vector reindex and graph-reprocessing sweep at deployment; limit the flags to rows requiring visibility backfill, using the migration’s relevant visibility/backfill condition while preserving the pending graph state for affected rows.

In @src/gobby/storage/migrations/328_memory_global_visibility.sql around lines 21 - 29, Update the foreign-key creation in migration 328 to add memories_project_id_fkey with NOT VALID, then validate it in a separate ALTER TABLE ... VALIDATE CONSTRAINT statement. Keep the existing ON DELETE RESTRICT and DEFERRABLE INITIALLY IMMEDIATE behavior unchanged.

In @src/gobby/storage/migrations/328_memory_global_visibility.sql around lines 18 - 19, Replace the direct ALTER COLUMN project_id SET NOT NULL in migration 328 with a PostgreSQL zero-downtime constraint-based approach: add a NOT VALID CHECK constraint for project_id IS NOT NULL, validate it separately, then enforce the not-null requirement using the validated constraint without a full table scan under ACCESS EXCLUSIVE. Preserve the final non-null invariant on memories.project_id.

In @src/gobby/storage/migrations/328_memory_global_visibility.sql around lines 31 - 41, Move the memories index drop-and-recreate operations from migration 328 into a separate non-transactional migration path that permits concurrent index changes. Preserve the existing index definitions for idx_memories_project_live and idx_memories_global_live, and ensure the transactional migration no longer executes these DROP INDEX or CREATE INDEX statements.

In @src/gobby/storage/migrations/329_memory_type_enum.sql around lines 81 - 85, Update the memories_memory_type_check creation in migration 329 to add the CHECK constraint with NOT VALID, then issue a separate VALIDATE CONSTRAINT statement for memories_memory_type_check. Preserve the existing allowed memory_type values and constraint replacement behavior.

In @src/gobby/tasks/validation_history.py around lines 194 - 198, Guard the FailureCategory conversion used by get_iteration_history/get_latest_iteration with a helper such as_safe_failure_category: return None for null values, convert recognized values, and catch ValueError for unknown stored categories while logging a warning. Replace the direct FailureCategory(row["failure_category"]) expression with this helper so stale history rows do not interrupt validation reads.

In @tests/dispatch/test_skill_composition.py at line 6, Add a module-level pytestmark using pytest.mark.integration in tests/dispatch/test_skill_composition.py, alongside the existing pytest import, so all database-backed tests in the module are categorized as integration tests.

In @tests/dispatch/test_skill_composition.py around lines 22 - 35, Annotate the temp_db parameter in the_skill helper and each of the four affected test functions with the repository’s expected fixture type. Use the existing type symbol used for temporary database fixtures, preserving all current test behavior and helper signatures otherwise.

In @tests/servers/routes/test_memory_routes.py around lines 505 - 516, Remove target_project_id from the promote endpoint request model and ensure POST /api/memories/{id}/promote rejects unexpected body fields rather than accepting and ignoring them. Keep the promote route and promote_memory invocation scoped to the memory id, and update test_promote_calls_explicit_operation accordingly.

In @tests/storage/test_migration_contract.py around lines 373 - 382, Update test_failure_category_taxonomy_is_closed_in_baseline_and_migration to normalize each schema with the existing_normalize_sql_whitespace helper before counting CHECK constraints and category text. Preserve the current assertions and category values while making matching independent of SQL formatting.

In @tests/test_failure_categories.py around lines 3 - 5, Add pytestmark = pytest.mark.unit near the imports in tests/test_failure_categories.py, applying the unit marker to the entire module while preserving the existing FailureCategory, classify_exception, and classify_failure imports.
Fix the following issues. The issues can be from different files or can overlap on same lines in one file.

In @crates/gcode/src/commands/status/invalidate.rs around lines 59 - 82, Update the Qdrant closure passed to run_projection_first in the project invalidation flow to treat a missing ctx.qdrant as an invariant error instead of returning Ok(0). Preserve the existing delete_project_collection call for present clients and propagate a descriptive error for the unreachable None case.

In @crates/gcode/src/commands/status/invalidate.rs around lines 266 - 270, Add #[cfg_attr(not(gcode_postgres_tests), ignore = "requires a PostgreSQL test database URL")] to both project_id_invalidation_needs_no_project_root and busy_project_lock_leaves_sql_discovery_row_untouched, keeping their existing serial test attributes and behavior unchanged.

In @crates/gcode/src/commands/status/prune.rs around lines 681 - 693, Update print_optional_reconcile_totals so the configured=true and totals=None branch emits an explicit operator-facing status line, distinguishing it from no work. Preserve print_reconcile_totals for Some totals and the existing skipped message when the service is not configured.

In @crates/gcode/src/commands/status/prune.rs around lines 587 - 601, The orphaned total in mutate_orphan_collections and the corresponding mutate_stale_projects path combines existing and would-be orphan IDs even though each sweep handles only one bucket. Report these buckets separately, using existing_orphan_ids for the collection sweep and the appropriate stale-project orphan IDs for mutate_stale_projects, so totals reconcile with the mutations each function performs.

In @crates/gcode/src/commands/status/prune.rs around lines 1207 - 1211, Add the same PostgreSQL test gate and serial_db marker used by orphan_project_discovery_and_sql_deletion_counts to both global_prune_collection_recheck_retains_row_inserted_after_discovery and global_prune_busy_lock_defers_entire_stale_project, preserving their existing test bodies.

In @crates/gcode/src/commands/status/prune.rs around lines 356 - 378, Update prune_project_scoped to resolve project_override before discovery and restrict the discovered stale and orphan project sets to that project when provided. Ensure mutate_stale_projects and reconcile_orphan_projects only receive records for the selected project, while preserving global cleanup behavior when no override is supplied.

In @crates/gcode/tests/projection_stale.rs around lines 181 - 216, Apply the existing gcode_postgres_tests ignore gate and serial_db lock attributes to both prune_unreachable_falkor_aborts_before_stale_sql_mutation and prune_qdrant_enumeration_failure_aborts_before_stale_sql_mutation, matching the adjacent PostgreSQL integration tests.

In @docs/reviews/build.md at line 41, Add a blank line immediately after the `explain_dispatch` blocker heading in `docs/reviews/build.md`, before the following list, to satisfy markdownlint MD022.

In @docs/reviews/dispatch.md at line 34, Add blank lines after the `epic_qa` review-round exhaustion heading and the corresponding heading at line 70, before their following list items, to satisfy markdownlint MD022.

In @pyproject.toml around lines 128 - 131, Update the dependency pins in pyproject.toml so torch and torchaudio use the same release version. Change either torch==2.13.0 or torchaudio==2.11.0 to establish a matching supported pair, while preserving the existing dependency configuration.

In @src/gobby/cli/github.py around lines 176 - 189, Update the status loop around GitHubIssueSyncService readiness checks to skip check_access when both config.sync_enabled and config.triage_enabled are false. For enabled projects, collect the asynchronous readiness checks and execute them together in a single asyncio.run call using gathered tasks, while preserving each project’s ready, repositories, and GitHubRepositoryReadinessError handling.

In @src/gobby/cli/github.py around lines 209 - 210, Update the JSON output branch in the CLI command to handle an empty payloads list before indexing payloads[0]. Preserve the existing all_projects behavior, and for single-project requests return a clear no-matching-project result consistent with the text path instead of raising IndexError.

In @src/gobby/cli/installers/container_restart.py around lines 39 - 51, Update the Docker availability check in the container restart flow to retain the path returned by shutil.which("docker") and pass that resolved executable path as the first argv element to subprocess.run, instead of the literal "docker". Keep the existing argument-list invocation, policy validation, and error behavior unchanged.

In @src/gobby/cli/linear.py around lines 33 - 54, The project resolution logic in get_linear_deps is duplicated with get_github_deps. Add a shared resolve_cli_project helper in gobby/cli/runtime.py that preserves the existing project_ref, resolve_ref, cwd-context, require_project, empty-string, and error-message behavior, then update both dependency functions to use it while retaining their distinct MCP manager factories.

In @src/gobby/cli/linear.py around lines 297 - 298, Guard the JSON output branch in the status command before indexing payloads[0]. When all_projects is false and payloads is empty, emit the command’s established empty-result representation instead of indexing; preserve the existing payloads[0] output for non-empty single-project results and the all-projects behavior.

In @src/gobby/cli/tasks/ai.py around lines 197 - 206, Extract the invalid infrastructure-category mapping from the validation_updates construction into a shared persisted_validation_status(status, failure_category) helper, then use that helper in both the CLI validation flow around validation_updates and the MCP validation logic in_lifecycle_validation.py. Preserve the existing behavior for all other statuses and failure categories.

In @src/gobby/dispatch/context.py around lines 174 - 175, Update the query’s task_id parameter in the surrounding dispatch context query to use the HubDatabase numbered placeholder $1 instead of %s, while preserving the existing LIKE conditions and transaction behavior.

In @src/gobby/hooks/event_handlers/_dispatch.py around lines 84 - 98, Guard the schedule_dispatcher_continuation_for_task call in the completed-stage review path after submit_for_review succeeds. Handle scheduler or create_task failures locally so they do not propagate after the persisted transition, while preserving the existing updated return value.

In @src/gobby/install/bundled_content_manifest.json around lines 49 - 51, Regenerate src/gobby/install/bundled_content_manifest.json using the current contents of src/gobby/install/shared: remove entries for missing rules/build/build-agent-safety.yaml and skills/build/SKILL.md, and update the SHA-256 for workflows/agents/merge-worker.yaml to its current content hash. Preserve all valid manifest entries.

In @src/gobby/install/shared/skills/review/SKILL.md around lines 3 - 6, Remove the duplicate “epic review” entry from the triggers metadata in SKILL.md, leaving each trigger term listed only once.

In @src/gobby/install/shared/workflows/agents/epic-reviewer.yaml around lines 161 - 209, Add mcp_error_policy: stay to the review step definition alongside its existing name and description, matching claim and load_skill so failed terminal verdict calls remain in review for retry. Do not alter the review transitions or verdict-tool behavior.

In @src/gobby/runner_lifecycle_periodic.py around lines 356 - 376, Gate startup of ExternalIssueSyncCoordinator in the runner lifecycle so it only creates and schedules the coordinator when at least one project has a Linear or GitHub integration enabled, while preserving cleanup of existing coordinator state. Also expose coordinator cycle duration through an appropriate metric or log, and avoid the current unconditional 5-second polling for deployments without integrations.

In @src/gobby/runner_lifecycle_periodic.py around lines 364 - 370, Update the ExternalIssueSyncCoordinator construction in the runner lifecycle setup to retrieve runner.memory_manager and runner.secret_store defensively with getattr(..., None), matching the existing optional-service access for mcp_proxy and task_manager. Preserve the current coordinator initialization and guard behavior while preventing AttributeError for partially initialized runners.

In @src/gobby/servers/routes/projects.py around lines 378 - 379, Update the GitHub triage status flow around GitHubTriageStore.get_config to pass the project’s canonical repository as fallback_repo, matching the GET /{project_id}/github-triage endpoint and coordinator behavior so fallback projects populate repositories consistently.

In @src/gobby/servers/routes/projects.py around lines 427 - 463, Update status_payload so the nested linear and github responses expose a consistent shape without project_id or provider: remove those metadata fields from the dictionary produced from status.to_dict(), while preserving the linked/pending counts and all other status fields. Ensure the fallback payload remains aligned with the filtered status payload.

In @src/gobby/servers/routes/projects.py around lines 187 - 195, Update the validation around the project PATCH handling so it evaluates the effective post-update linear_sync_enabled value, including the project's existing value when the field is omitted. When sync is effectively enabled, validate the effective linear_team_id and linear_project_id values after applying the payload, rejecting null or missing bindings while preserving the existing error response.

In @src/gobby/storage/external_issue_sync.py around lines 43 - 61, The ExternalIssueSyncStatus.from_row method must tolerate malformed last_statistics JSON values. Wrap json.loads for string inputs in decode-failure handling and fall back to an empty dictionary when parsing fails, while preserving valid parsed statistics and the existing non-string behavior.

In @src/gobby/storage/external_issue_sync.py around lines 118 - 156, Update the upsert statement in the status-writing method to append RETURNING * and execute it through self.db.fetchone(...), then convert the returned row with ExternalIssueSyncStatus.from_row(row). Remove the follow-up self.get(...) call and the now-unreachable “status disappeared” RuntimeError, returning the upserted status directly.

In @src/gobby/storage/tasks/_automation.py around lines 38 - 45, Update the explicit-ID branch in the task automation query construction to use the hub database’s numbered placeholder format instead of %s. Number that placeholder based on the constructed params list, and ensure subsequent mutex/project placeholders remain correctly numbered after the explicit-ID parameter.

In @src/gobby/storage/tasks/_epic_gate.py around lines 79 - 130, Update both gate lookup queries in the task gate implementation, including the lookup containing the recursive descendants CTE and the additional lookup noted in the review, to use the async Hub database access path. Execute them through an awaited Hub transaction, replace positional %s parameters with the transaction’s $N placeholders, and preserve the existing query behavior and result handling.

In @src/gobby/storage/tasks/_epic_gate.py around lines 212 - 213, Update _current_stage to select the non-done stage with the lowest manifest position, rather than returning the first matching entry in task.stages. Reuse the existing stage-position field or ordering symbol used by dispatch, while preserving None when no pending stages exist.

In @src/gobby/sync/external_coordinator.py around lines 84 - 85, Update the run() shutdown path to cancel all child tasks in self._tasks when run() is being cancelled, before invoking wait_for_idle(). Ensure the cancellation is awaited or drained so provider tasks cannot continue against the shutting-down DB/MCP stack, while preserving normal completion behavior.

In @src/gobby/sync/external_coordinator.py around lines 567 - 569, The_is_rate_limit predicate should recognize provider-specific 429 and rate-limit error formats instead of relying on a few exact phrases. Broaden its detection to match the established github_issue_sync._is_rate_limit_error behavior, including a generic “429” check and available retry-after attributes, or reuse that shared predicate if appropriate.

In @src/gobby/sync/external_coordinator.py around lines 75 - 85, Update the coordinator’s run method to catch recoverable exceptions from refresh() and continue the refresh loop instead of allowing one transient failure to terminate run(). Preserve shutdown handling and the existing finally block so wait_for_idle() still drains active runs when the daemon stops.

In @src/gobby/sync/external_coordinator.py around lines 515 - 519, Replace the type-narrowing assert in the surrounding coordinator method with explicit non-optional local assignments after the conditional counts lookup. Ensure linked and pending are typed or assigned so mypy narrows them without relying on assert, while preserving the existing counts behavior and values.

In @src/gobby/sync/github_issue_sync.py around lines 100 - 102, Update the issue-number validation near issue.get("number", issue_number) so malformed or non-int-coercible values are converted into the same intended validation error as mismatched numbers. Preserve the requested-issue comparison and ensure callers such as recover_project receive the validation error rather than a raw int-conversion ValueError.

In @src/gobby/sync/github_issue_sync.py around lines 160 - 202, Bound the pagination loop in the issue-listing flow with a hard maximum page count, and track issue identifiers or otherwise detect when a page yields no new issues so pagination stops even when the API repeats results. Preserve existing rate-limit propagation, error statistics, issue syncing, and the normal short-page termination behavior around sync_issue.

In @src/gobby/sync/github_issue_sync.py around lines 104 - 146, Move all synchronous database and manager calls in async sync_issue to asyncio.to_thread, including both self.db.fetchone queries, task_manager.create_task, and every task_manager.reconcile_task_state invocation; apply the same offloading to project_manager.get and config_store.get_config in the surrounding async flow. Preserve existing arguments, exception handling, and return behavior.

In @src/gobby/sync/linear_task_ops.py around lines 533 - 537, Add focused tests for the limit validation in the Linear issue creation method, covering both limit=0 and a negative limit. Assert each raises ValueError and that no Linear issues are created, while preserving the existing positive-limit ordering test.

In @src/gobby/sync/linear_task_ops.py around lines 542 - 551, Update the task query flow in the surrounding task operation method to execute within the Hub database transaction boundary, replacing the direct task_manager.db.fetchall call. Convert all SQL placeholders to PostgreSQL-style $N parameters, including the optional LIMIT parameter, while preserving the existing filtering, ordering, and limit behavior.

In @src/gobby/sync/linear_task_ops.py around lines 529 - 551, Update create_missing_issues to execute the synchronous task_manager.db.fetchall query through the existing async database runner, or asyncio.to_thread when no runner is available, and await its result before processing rows. Keep the SQL, parameters, and returned issue behavior unchanged while ensuring the event loop is not blocked.

In @tests/adapters/test_provider_contract_fixtures.py around lines 205 - 213, Add an assertion for results[3]["tool_outcome"]["status"] in the existing result validation, requiring it to equal "unknown" like results[1], while preserving the current command-correlation and final-outcome assertions.

In @tests/agents/test_epic_reviewer_definition.py around lines 13 - 18, Update the_agent function’s return annotation from bare dict to dict[str, Any], and import Any from typing so the yaml.safe_load result is accurately typed.

In @tests/dispatch/test_prompts.py around lines 145 - 148, Remove the redundant test_epic_reviewer_prompt_builder_registered test, since test_dispatch_prompt_builder_keys_present already verifies the same registration. Keep coverage focused on distinct behavior rather than duplicating the PROMPT_BUILDERS key assertion.

In @tests/dispatch/test_rules.py around lines 823 - 825, Remove the ineffective description.count assertion from the test, since description is never mutated and the existing_evaluate(repeated_task, changed_context) is None assertions already verify non-duplication behavior.

In @tests/e2e/test_build_dispatcher_autonomy.py around lines 187 - 195, Remove the redundant dispatch_idle assignment and trailing assert in the dispatch handoff test, and await wait_for_async_condition directly while preserving its existing condition, timeout, and description arguments.

In @tests/mcp_proxy/test_mcp_tools_session_messages.py around lines 721 - 723, Add type annotations to the mock_session_manager and full_sessions_registry parameters of test_wait_for_summary_clamps_timeout_to_wrapper_limit, using the appropriate fixture types already established in the test module. Keep the test behavior unchanged.

In @tests/sessions/test_codex_nested_exec_outcomes.py around lines 281 - 298, Add the repository’s unit-test marker to test_pty_write_stdin_chain_survives_repeated_yields_and_parser_hydration, using the existing pytest marker conventions so it is included in categorized test runs.

In @tests/storage/test_external_issue_sync.py around lines 1 - 8, Mark the new test module as a unit test by adding the repository’s standard pytestmark assignment near the module imports in tests/storage/test_external_issue_sync.py. Use the existing unit marker convention so marker-based selection includes the ExternalIssueSyncStatusStore test suite.

In @tests/sync/test_external_coordinator.py around lines 107 - 148, Reduce the test’s coupling to private scheduling state in test_linear_backfill_runs_ordered_batches_every_five_seconds and the additionally referenced test around the second occurrence: avoid asserting on or manipulating coordinator._due and coordinator._start directly, and verify the five-second scheduling behavior through the coordinator’s public refresh, idle-wait, and observable service-call behavior instead. Preserve the existing batch ordering and timing assertions.

In @tests/sync/test_github_issue_sync.py around lines 45 - 261, The async tests in this diff leave the github_sync fixture parameter untyped. Define a reusable fixture type alias (or the established tuple type) and annotate the github_sync parameter in every affected test function, including test_webhook_issue_is_created_once_and_then_updated and the other tests shown, while preserving their existing behavior.

In @tests/sync/test_github_issue_sync.py around lines 1 - 16, Add the module-level pytestmark declaration in tests/sync/test_github_issue_sync.py using the existing pytest import, marking the suite with pytest.mark.unit so all tests in the module are classified as unit tests.

In @tests/sync/test_linear_sync.py around lines 1558 - 1568, Mark the async test method test_create_missing_issues_applies_ordered_batch_limit with pytest.mark.asyncio so pytest-asyncio executes it and validates the ordering and limit contract.
Fix the following issues. The issues can be from different files or can overlap on same lines in one file.

In @crates/gcore/src/qdrant.rs around lines 347 - 359, Update the Qdrant collection-response parsing around the `data`/`collections` construction to validate that `result.collections` exists and is an array, returning an error for missing or unexpected shapes instead of producing an empty list. Preserve the existing name extraction, sorting, and deduplication behavior for valid responses.

In @crates/gcore/src/qdrant/tests.rs around lines 525 - 555, Add a negative test alongside collection_listing_is_sorted_and_deduplicated that makes the mocked Qdrant response omit result.collections and verifies list_collections returns the defined error behavior, while preserving the existing request assertion pattern.

In @crates/gwiki/src/commands/project_admission/tests.rs around lines 65 - 89, Update project_sync_sessions_is_fenced_through_every_persistent_write_phase to invoke the actual PostgreSQL, Qdrant, and Falkor sync phase helpers, or an existing test seam that records each backend’s admission, within run_with_project_lock. Assert each phase executes while the project guard is held and retain the final assertion that the guard releases only after all phases complete.

In @crates/gwiki/src/falkor_graph/sync.rs around lines 144 - 152, Update project_scope_query to constrain scope discovery using the established gwiki label or an indexed scope_kind property for both nodes and relationships. Ensure the node and relationship matches remain semantically equivalent while allowing FalkorDB to avoid scanning the entire graph.

In @src/gobby/agents/detection/matcher.py around lines 158 - 170, Update _last_prompt_box so it first determines whether the most recently rendered prompt box is still open; if the latest opening corner (╭/┌) occurs after the latest closing corner (╰/└), return an empty string instead of scanning for an older closed box. Preserve returning only the newest completed opening-to-closing box when the corners are properly paired.

In @src/gobby/agents/detection/matcher.py around lines 237 - 243, Update compile_manifest to avoid the redundant str encode/decode round-trip: preserve bytes input for fingerprinting, while passing str input directly to_compile_fingerprint and deriving its SHA-256 fingerprint from the corresponding UTF-8 bytes.

In @src/gobby/agents/detection/registry.py around lines 153 - 154, Introduce a distinct MAX_STALENESS_SECONDS constant for the validation ceiling in the staleness_seconds check, and use it instead of DEFAULT_STALENESS_SECONDS while retaining the existing default constant for default values. Update the validation message to reflect the maximum represented by the new constant.

In @src/gobby/agents/detection/registry.py around lines 181 - 197, Extract the shared fingerprint dictionary construction from _refresh_at and _reload_at into a small helper method, then call that helper in both methods after _read_rows(). Preserve the existing comparison, early return, and _install_rows behavior.

In @src/gobby/agents/idle_check_handler.py around lines 156 - 216, Update sync_attention so stall classifications use a dedicated payload containing the classification reason instead of scraping arbitrary pane text through prompt_payload. Also derive fingerprints from normalized prompt content (excerpt/options, or the stall reason) rather than the full pane tail, while preserving distinct fingerprints for materially changed attention episodes and avoiding transition_async publishes for redraw-only changes.

In @src/gobby/agents/idle_check_handler.py around lines 290 - 312, The lifecycle cycle currently captures and synchronizes each active pane twice. Update the coordination between check_attention_agents and _handle_idle_check/check_idle_agents so the existing pane_output is reused or the later sync_attention call is skipped for runs already processed by the attention sweep, while preserving attention classification for all active runs and avoiding duplicate StallClassifier updates.

In @src/gobby/agents/idle_detector.py around lines 98 - 125, Update IdleCheckHandler._handle_idle_check to immediately return 0 when detect() returns "unknown", before entering the reprompt/failure ladder. Keep "unknown" as the detector result for missing manifests, and update the relevant docstring to include it in the documented return set.

In @src/gobby/agents/prompt_detector.py around lines 126 - 134, Update detect_prompt so question detection is not triggered solely by two numbered items anywhere in pane_output. Gate it with the configured manifest question_prompt rule, or restrict _enumerated_options scanning to the recent prompt/dialog region, while preserving the existing approval and trust detection behavior.

In @src/gobby/agents/prompt_detector.py around lines 149 - 158, Update_enumerated_options to emit the collected options sorted by their numeric option keys rather than dictionary insertion order. Preserve the existing deduplication and label handling, and apply sorting only when constructing the returned tuple.

In @src/gobby/agents/sandbox.py at line 634, Update the domain computation in the sandbox SRT configuration so operator-configured config.allowed_domains are included even when provider is absent. Preserve provider-specific allowed_domains behavior when provider is supplied, while ensuring strictAllowlist and the unconditional denied_domains, loopback_ports, and SRT renderer receive the combined allowlist.

In @src/gobby/agents/sandbox_policy.py around lines 138 - 145, The_nearest_package_root function walks too far up the filesystem and may select a package.json in a shared installation prefix. Limit its parent traversal to a fixed shallow depth of one or two parents from executable, while retaining the existing package.json file check and returning None when no nearby package root is found.

In @src/gobby/agents/spawn_executor.py around lines 67 - 77, Update_prepare_provider_sandbox and its callers to avoid accessing request.session_manager._storage by using a public accessor or explicitly passing the run manager. Replace the nullable tuple contract with a type-safe result shape: return a non-null SandboxLaunch on success and propagate an identified SpawnAborted exception carrying SpawnResult, or return SpawnResult | SandboxLaunch and branch with isinstance; remove the five dependent launch assertions.

In @src/gobby/agents/srt_runner.mjs around lines 61 - 65, Update the preflight command construction in the command selection logic to remove the hardcoded `/usr/bin/true` path. Use a portable approach, preferably `process.execPath` with `--version`, or resolve `true` through PATH, while preserving the existing Windows argument handling and missing-command validation.

In @src/gobby/agents/srt_runner.mjs around lines 81 - 97, Update the child outcome await flow around the signalHandlers registration and child.once handlers so cleanup always runs in a finally block, including when the child emits an error. Ensure every registered process signal handler is removed and unsubscribe() is called before propagating the rejection or returning the exit outcome.

In @src/gobby/agents/srt_runtime.py around lines 96 - 138, Update verify_srt_installation and the related preflight rejection paths to emit logger.warning messages before raising SrtRuntimeError, covering receipt, package, runner, lockfile, and Node validation failures. Use the project’s structured logging mechanism and include run_id, provider, and policy_hash context for each sandbox lockout; propagate that context into these paths where necessary without changing the fail-closed behavior.

In @src/gobby/agents/srt_runtime.py around lines 241 - 252, Update the SandboxLaunch construction in the SRT launch flow to set provider_env conditionally: use CLAUDE_CODE_TMPDIR only when provider == "claude", and set TMPDIR to temp_path for all other providers. Preserve the existing temp-path value and ensure_preflight_srt receives the selected provider environment.

In @src/gobby/agents/srt_runtime.py around lines 141 - 142, Update render_srt_settings to accept ResolvedSandboxPaths instead of Any, importing ResolvedSandboxPaths under TYPE_CHECKING consistently with the other sandbox types. Preserve the existing settings translation while enabling strict mypy to validate the resolved-path fields.

In @src/gobby/agents/stall_classifier.py around lines 201 - 215, The_match_pane_provider_error method repeatedly scans every normalized pane line against both manifest rules, making classification unnecessarily expensive. Reuse the already-resolved manifest context instead of calling_manifest() on each invocation, and limit matching to the last N non-empty lines while preserving source_shaped exclusion and pane_provider_error detection.

In @src/gobby/agents/stall_classifier.py around lines 193 - 199, Update_match_provider_error to return a short, stable provider-error rule label rather than the full matched text. Preserve None when no manifest or match exists, and ensure classification.reason continues passing only the bounded label through error_msg and cleanup_agent terminal_payload.

In @src/gobby/agents/terminal_prompt_monitor.py around lines 87 - 91, In the prompt injection flow around the awaited _on_prompt_injected callback, add a separate guarded block so callback failures are handled independently from the prompt-detection try/except and are not logged as trust or approval prompt errors. Apply the same separation to the corresponding callback block near the other referenced location, preserving the existing callback invocation and prompt state updates.

In @src/gobby/agents/tmux/pane_monitor.py around lines 265 - 291, Update the session lookup in the monitor’s polling method so active_interactive_ids represents all active and paused interactive sessions rather than only the first 500; paginate session_manager.list to exhaustion using its existing pagination contract, then retain the attention-clearing logic only for session IDs positively absent from the complete result.

In @src/gobby/agents/tmux/pane_monitor.py around lines 258 - 316, Wrap the blocking reads in_check_attention_panes and _clear_attention_if_current with asyncio.to_thread before awaiting them: session_manager.list, manager.list_blocked, and manager.get. Preserve their existing arguments and control flow, and continue using the existing async transition path for writes.

In @src/gobby/agents/tmux/pane_monitor.py around lines 318 - 325, Update_sync_interactive_attention to accept the provider/source value from its caller instead of calling _lookup_session(session_id). In_check_attention_panes, pass the current session.source or an empty string when invoking it, and use that value for provider-specific detector and classifier selection.

In @src/gobby/ai/embedding_switch.py around lines 100 - 109, Update the embedding switch read/write logic around internal_get and the corresponding internal setter to require the lifecycle methods on the store class. Remove the fallback to config_store.get() and config_store.set(); when get_internal_lifecycle or set_internal_lifecycle is unavailable, fail immediately with the established error behavior instead of silently reading None or using the public API.

In @src/gobby/ai/embedding_switch_runner.py around lines 499 - 511, Guard _finish_aborted_cleanup before calling_cleanup_staged_collections: only delete physical collections when the journal is in a pre-flip phase, or apply the same alias-target protection used by gc(). Preserve completion of the aborted switch while ensuring already-flipped journals cannot delete collections currently targeted by aliases.

In @src/gobby/ai/embedding_switch_service.py around lines 130 - 138, Update abort() so it does not wait indefinitely for the switch task: await the shielded task through a bounded timeout, then return the existing aborted status if the task remains unfinished. Catch exceptions raised while awaiting the task and return SwitchOperationStatus with “failed” rather than propagating them; preserve the existing result.error handling for completed tasks.

In @src/gobby/ai/embedding_switch_service.py around lines 154 - 161, Update _launch to attach a done callback to the task created for runner.run(journal), using a module-level logger to record any exception and clear_run_id when the background task completes unsuccessfully. Ensure the callback safely handles cancellation and successful completion, while preserving abort()’s existing await behavior.

In @src/gobby/cli/embeddings.py around lines 88 - 93, Update the response handling around the status check and payload parsing to tolerate non-JSON or empty bodies: catch the JSON decoding failure, use response.text as the detail or payload fallback, and preserve the existing Error output with exit code 1 for HTTP failures. Ensure successful non-JSON responses also avoid an uncaught exception.

In @src/gobby/cli/install_setup.py around lines 365 - 375, Update the SRT installation handling in run_daemon_setup so an SrtRuntimeError does not raise ClickException or abort the remaining setup steps. Emit a warning that includes the explicit agent_sandbox.backend = provider-native fallback hint, then continue with helper-binary, native-binary, tmux clipboard, and IDE integration setup.

In @src/gobby/cli/install_setup_srt.py around lines 71 - 82, Update _install_srt_runtime to acquire a per-version filesystem lock before staging or promoting the SRT installation, and hold it through target/backup replacement and verification-related cleanup. Ensure concurrent installs and daemon verification serialize on the same lock, while preserving the existing promotion behavior and releasing the lock on all success and failure paths.

In @src/gobby/cli/install_setup_srt.py around lines 113 - 132, The SRT receipt’s recorded node runtime is not enforced during verification. Update verify_srt_installation and its launch-time validation to resolve the active Node interpreter, compare it with receipt["node"] or enforce the package’s Node >=20.11 engine floor, and fail clearly before launching when incompatible; alternatively remove the node field from receipt.json if runtime validation is not implemented.

In @src/gobby/cli/installers/embedding.py at line 500, Replace the direct call to the private VectorStore._ensure_initialized() in the installer with a public VectorStore accessor, preferably list_collection_names(), and use that API to obtain the required collection information. Add the public helper to VectorStore if it does not already exist, preserving the existing initialization and failure behavior without exposing private internals.

In @src/gobby/cli/installers/embedding.py around lines 513 - 519, Update the exception handler around asyncio.run(_inspect()) to emit a structured log containing the inspection failure and relevant operation context before raising EmbeddingConfigMutationBlocked. Preserve the existing fail-closed exception and chaining behavior, while ensuring causes such as connectivity, authentication, and dimension errors are included in the log.

In @src/gobby/cli/installers/embedding.py at line 483, Replace the Any annotation on config_store in_managed_embedding_collections_exist with ConfigStore, and add the ConfigStore import under the module’s TYPE_CHECKING guard to avoid the import cycle. Ensure load_config(config_store=...) receives the concrete type under strict mypy.

In @src/gobby/cli/projects.py around lines 185 - 198, The purge_project command currently accepts a bare --yes, unlike the name-confirmation requirement used by projects delete. Replace this confirmation flow with a required project-name confirmation, validate it against the resolved project before performing the irreversible purge, and reject mismatches while preserving the existing project-not-found handling.

In @src/gobby/cli/projects.py around lines 207 - 211, Update the response handling around the purge command to check response.status_code before calling response.json(). For error responses, use the existing response.text fallback when the body is non-JSON, then emit the “Purge failed” message and exit; only parse JSON for successful responses before passing the payload to json_dumps.

In @src/gobby/config/app.py around lines 286 - 289, Update the daemon startup flow to preflight the default SRT agent sandbox, warning when its managed runtime or Node.js 20.11+ prerequisite is unavailable instead of allowing spawned agents to fail unexpectedly. Also document the provider-native rollback configuration for users who cannot use SRT, alongside the agent_sandbox default in the configuration documentation.

In @src/gobby/config/skills.py around lines 21 - 26, Update the description of the hub type field in the skills configuration model to include `github-topic`, matching the accepted values in its `Literal` type while preserving the existing descriptions for the other hub types.

In @src/gobby/install/shared/detection/qwen.toml around lines 74 - 91, Update the queued_continuation rule’s line_regex so it no longer duplicates the queued_message pattern: remove the standalone queued messages alternative, or otherwise require continuation text together with the queued prompt. Keep queued_message responsible for plain queued-message prompts.

In @src/gobby/mcp_proxy/connection_cleanup.py around lines 101 - 106, Bound the sequential disconnect loop in the shutdown cleanup flow so total shutdown time does not scale without limit with connection count. Update the loop using disconnect_connection to enforce an overall wall-clock budget or a shorter per-connection timeout, while preserving task-affine caller-task execution and marking each processed connection as DISCONNECTED.

In @src/gobby/mcp_proxy/tools/agents_query_tools.py around lines 189 - 190, Replace the assert-based narrowing in the timeout and interval handling flow with explicit None checks or a typed helper return. Preserve the existing early-return behavior established by the preceding error check, and ensure timeout_value and interval_value are narrowed without relying on assertions that disappear under Python optimization.

In @src/gobby/mcp_proxy/tools/agents_query_tools.py around lines 205 - 214, Update the exception handlers around tmux pane capture and session checks to log the caught exception using structured logging before setting the failure state. Include the run ID and tmux session name as contextual fields, while preserving CancelledError propagation and the existing capture_failed behavior.

In @src/gobby/mcp_proxy/tools/agents_query_tools.py around lines 224 - 225, Update the matched-result branch in the relevant query tool to bound the excerpt instead of returning the full pane_output buffer. Return the matched region with limited surrounding context, or truncate pane_output to a reasonable maximum, while preserving the existing success and matched fields.

In @src/gobby/mcp_proxy/tools/agents_query_tools.py at line 197, Raise the minimum interval in the wait_for_output polling logic from 0.01 seconds to 0.1 seconds, matching the clamp used by wait_for_agent. Update affected tests that pass poll_interval_seconds=0.01 to use short timeouts while preserving their intended behavior.

In @src/gobby/mcp_proxy/tools/skills/install_skill.py around lines 116 - 133, Restrict the provenance validation block in the skill installation flow to the TopicHub provider that supplies item_id, repo, path, and sha, rather than applying it whenever DownloadResult.provenance is present. Keep other providers’ provenance provider-agnostic, while preserving the existing normalize_topic_item_id and item_unavailable validation for TopicHub downloads.

In @src/gobby/mcp_proxy/tools/spawn_agent/_factory.py around lines 411 - 429, Update the fallback-agent handling in the task/provider check around get_failed_providers_for_task to emit a diagnostic log when a configured fallback_agent chain is skipped because detection_registry is None, distinguishing missing wiring from no failed providers. Preserve the existing guarded behavior, and ensure the broad exception handler’s logger.debug path includes sufficient error context rather than silently obscuring failures.

In @src/gobby/memory/manager.py at line 84, Replace the Any | None annotation for project_write_fence in the relevant constructor and usages with the existing VectorWriteFence/ExclusiveFence-style Protocol, reusing the established fenced_vector_store.VectorWriteFence interface so strict mypy can validate compatibility throughout the memory manager.

In @src/gobby/memory/services/knowledge_graph/maintenance.py around lines 215 - 218, Update the purge flow around remove_orphaned_entities to use a non-catching orphan-cleanup variant that propagates connection and query failures instead of returning 0. Add or invoke a strict method alongside the existing cleanup behavior, ensuring purge failures remain visible and retryable after Memory node removal.

In @src/gobby/projects/fenced_vector_store.py around lines 62 - 80, The batch_upsert method currently uses global_writer for multi-project batches, unnecessarily blocking on unrelated project purges. Replace that fallback with admission scoped to every distinct project_id: acquire writer() contexts for all project IDs in the batch and keep them held while calling_inner.batch_upsert; preserve the existing global behavior only for batches without project IDs, if required by the fence contract.

In @src/gobby/projects/purge.py around lines 233 - 264, The purge handler processes candidates sequentially, allowing large runs to exceed practical cron durations. Update create_project_purge_handler’s_handler to purge projects with bounded concurrency, using an asyncio.Semaphore or equivalent limit, while preserving the existing success, protected, failed, and count aggregation behavior.

In @src/gobby/projects/write_fence.py around lines 97 - 118, Update the drain-wait error handling in exclusive() so self._exclusive is removed and waiters are notified for every exception that occurs before yield, including cancellation, while preserving ProjectWriteDrainTimeout conversion for TimeoutError. Ensure cleanup also handles removal safely and does not allow the project to remain permanently marked exclusive.

In @src/gobby/projects/write_fence.py around lines 39 - 56, The project lookup in the write-admission path currently performs a synchronous DB read while holding self._condition. Resolve project_id via self._project_lookup before entering the async condition block, then reuse that result inside the validation branch while preserving rejection for missing or deleted projects and the existing admission behavior.

In @src/gobby/runner_init/servers.py around lines 62 - 63, Update the `attention_manager` argument in `init_orchestration` to access `runner.attention_manager` directly, matching the existing `runner.detection_registry` access. Remove the `getattr` fallback so initialization-order regressions are surfaced instead of silently passing `None`.

In @src/gobby/runner_init/storage.py around lines 243 - 253, Update the hub authentication setup before constructing HubManager to use resolve_hub_api_keys(skills_config.hubs, runner.secret_store) instead of manually reading os.environ and populating api_keys. Pass the resolved keys to HubManager while preserving the existing skills_config.hubs configuration.

In @src/gobby/runner_lifecycle_subsystems.py around lines 410 - 423, Route the register_wiki_prune_cron call through await_run_db(runner, ...) so its synchronous database operations execute off the event loop. Preserve the existing cron_storage, executor, gateway, and project_id arguments, and follow the surrounding storage-access pattern in the lifecycle function.

In @src/gobby/runner_maintenance.py around lines 208 - 221, Update rebuild_vector_store so the fallback path does not invoke the synchronous memory_dicts supplier on the event loop; execute the callable supplier via the project’s async thread/offloading mechanism, await its result, and then pass the resolved memories to vector_store.rebuild. Preserve direct use of list inputs and the existing rebuild_from_supplier path.

In @src/gobby/servers/routes/attention.py at line 578, Replace the direct private `tmux._base_args()` calls in the route with a public accessor on TmuxSessionManager, such as base_args() or cli_prefix(), and use that accessor at both affected tmux_cmd assignments. Preserve get_tmux_prefix_for_context for the session-context path and ensure the new public method returns the same argument sequence.

In @src/gobby/servers/routes/attention.py around lines 396 - 412, Update the roster-building flow around _load_task_payload to avoid awaiting one task lookup per run sequentially. Collect distinct task_ids from runs, load their payloads concurrently or through a batched query before the entry loop, cache the results by task_id, and reuse that cache when assigning each entry’s task field.

In @src/gobby/servers/routes/attention.py at line 133, Update the lock handling around the module-level locks map and the code at the referenced wait path so entry_id is resolved and validated before creating or retrieving a lock. Ensure locks are removed after the associated operation completes and no waiters remain, including legitimate finished runs, while preserving synchronization for concurrent requests targeting the same valid entry.

In @src/gobby/servers/routes/embeddings.py around lines 125 - 134, Update embedding_switch_start to import EmbeddingConfigMutationBlocked and catch it alongside EmbeddingSwitchTaskActive and SwitchAlreadyActiveError, mapping all three contention errors to HTTP 409 with their existing detail message.

In @src/gobby/sessions/tmux_context.py around lines 45 - 60, The helper get_tmux_pane_pid currently reads parent_pid, so either rename it to reflect the parent-process PID or update it to read the actual tmux pane PID field and keep its callers consistent. Add brief docstrings to both get_tmux_pane_pid and get_tmux_session_name describing their input and returned value.

In @src/gobby/skills/hubs/github_topic.py around lines 234 - 247, Update the GitHub provider request flow around_get to reuse a single httpx.AsyncClient for the provider lifetime or each discover() call instead of creating one per request. Pass that client through _search_page, _probe_repo, and _download_archive, and ensure it is properly closed after the owning lifecycle completes.

In @src/gobby/skills/hubs/github_topic.py around lines 345 - 373, Update the discovery flow around discover() by adding an asyncio.Lock-backed single-flight refresh and moving the existing crawl/cache-update body into a private_refresh() method. Have discover() acquire the lock, re-check the cache after waiting, and only invoke _refresh() when discovery is still needed, preserving the existing rate-limit fallback and cache behavior.

In @src/gobby/skills/hubs/github_topic.py around lines 277 - 300, Update_probe_repo to validate each response.json() result as a dict before using .get(), removing the unsafe cast assumptions for both commit_payload and tree_payload. Ensure JSONDecodeError/ValueError and invalid JSON shapes are converted into the existing per-repository skip path by expanding the local exception handling, so malformed repository responses cannot escape the crawl TaskGroup.

In @src/gobby/skills/hubs/github_topic.py around lines 450 - 469, Update the httpx.AsyncClient construction in_download_archive to enable follow_redirects=True, while preserving the existing streaming, status handling, and archive-size validation behavior.

In @src/gobby/skills/hubs/manager.py around lines 40 - 49, The type-specific auth secret selection is duplicated and omitted from auth-reporting paths. Add an `auth_secret_name` property to `HubConfig`, returning `auth_token_env` for `github-topic` and otherwise `auth_key_name`, then replace the conditional or direct `auth_key_name` reads in the manager’s secret loading, `_create_provider`, `auth_status`, `warn_missing_auth`, and `runner_init/storage.py` with this accessor.

In @src/gobby/storage/agents/_sandbox_records.py around lines 53 - 70, Bound violation-log processing in _read_violations so include_events=False does not scan the entire file on every to_brief() serialization. Prefer using a persisted count with writer-side rotation/capping; otherwise return an explicitly bounded or truncated count while preserving recent-event collection behavior when include_events=True.

In @src/gobby/storage/agents/_sandbox_records.py around lines 58 - 69, Update the file-reading logic in the sandbox record serialization flow to safely handle invalid UTF-8 by opening the file with replacement decoding or catching UnicodeError during iteration, treating affected lines as malformed records without aborting serialization. Preserve the existing JSONDecodeError skip behavior and add a regression test covering corrupt UTF-8 input.

In @src/gobby/storage/config_store.py around lines 113 - 123, The journal parsing flow around_journal_run_id and _active_switch_run_id must not turn malformed or non-dictionary journal payloads into a persistent "unknown" lock. Treat undecodable entries as absent and emit an appropriate warning, or provide an explicit force-clear path, while preserving valid run IDs and ensuring _assert_embedding_mutation_allowed, set_internal_lifecycle, and delete_internal_lifecycle can recover without requiring the literal "unknown" value.

In @src/gobby/storage/cron.py around lines 623 - 636, Update the cron parking logic in the transaction to replace the pre-update SELECT and separate UPDATE with a single UPDATE ... RETURNING * statement that sets enabled to FALSE and next_run_at to NULL, then build CronJob objects from the returned post-update rows.

In @src/gobby/sync/external_coordinator.py around lines 574 - 583, Update the rate-limit marker list used by the visible error-detection function to recognize hyphenated “rate-limit” text and generic HTTP 429 responses such as “429 Too Many Requests,” matching the behavior of github_issue_sync._is_rate_limit_error. Preserve the existing markers and ensure these cases use the rate-limit retry path with retry_at.

In @src/gobby/wiki/prune_job.py around lines 102 - 128, Replace the "system" fallback in register_wiki_prune_cron when calling cron_storage.create_job with a schema-valid project identifier. Use a real project UUID or update the job/storage design to support a nullable or sentinel-safe system-job project reference, while preserving project_id when provided.

In @tests/agents/test_lifecycle_monitor_registry.py around lines 1 - 13, Mark the tests in tests/agents/test_lifecycle_monitor_registry.py as integration tests by adding the project’s established pytest integration marker at module scope, alongside the imports or module docstring. Keep the existing test behavior and temp_db-backed setup unchanged.

In @tests/agents/test_provider_rotation_registry.py around lines 1 - 13, Add the pytest import and declare module-level pytestmark as pytest.mark.unit in tests/agents/test_provider_rotation_registry.py, matching the existing unit-test convention so marker-based selection includes this test module.

In @tests/agents/test_provider_routing.py around lines 15 - 22, Rename the test stub class MutableRegistry to StaticRegistry (or FakeRegistry) and update every reference to it in tests/agents/test_provider_routing.py; keep its compile-on-initialization and lookup behavior unchanged.

In @tests/agents/test_provider_routing.py around lines 1 - 13, Add the module-level pytest marker declaration in tests/agents/test_provider_routing.py using the existing pytest import: set pytestmark to pytest.mark.unit so all tests in the module are classified as unit tests.

In @tests/agents/test_srt_runtime.py around lines 32 - 267, Mark the focused tests in tests/agents/test_srt_runtime.py as unit coverage by applying the project’s established unit pytest marker to the relevant test functions or module. Preserve all existing test behavior and assertions.

In @tests/agents/test_srt_spawn.py around lines 21 - 186, Add a module-level pytest.mark.unit marker to tests/agents/test_srt_spawn.py, and update test_auth_cli_inference_looks_through_srt_wrapper() to declare a -> None return type. Preserve the existing test behavior and other annotations.

In @tests/agents/test_stall_classifier.py around lines 16 - 20, Add a module-level unit-test marker in tests/agents/test_stall_classifier.py, alongside the existing imports and before the tests, using the project’s established pytest marker convention. Leave make_classifier and the test behavior unchanged.

In @tests/agents/tmux/test_pane_monitor_registry.py around lines 31 - 39, The test around monitor._sync_interactive_attention should verify transition_async call arguments rather than relying only on await_count. Assert that the beta trust invocation triggered the transition and that the repeated stale alpha trust invocation did not, while preserving the existing registry identity assertion.

In @tests/ai/test_embedding_switch_daemon_lifecycle.py around lines 39 - 53, Extract the shared events list, runners list, runner factory, and EmbeddingSwitchCoordinator construction into a pytest fixture returning (coordinator, runners, events). Replace the duplicated setup in all three affected tests with the fixture, preserving each test’s existing access to the coordinator, runners, and events.

In @tests/ai/test_embedding_switch_daemon_lifecycle.py around lines 95 - 101, Extend the test after awaiting task to assert the terminal result returned by task, and verify coordinator.task and its status afterward. Preserve the existing too_late abort assertion and cleanup signaling, ensuring the final assertions confirm the flip result was propagated and the coordinator completed cleanly without a dirty journal.

In @tests/ai/test_embedding_switch_runner.py around lines 163 - 166, Rename the local variable assigned from store.operations[1][1] in the owner_set assertion block from config_entries to a name representing the run ID, such as run_id, and update its comparison with journal.run_id accordingly; leave the entries dictionary access at index [2] unchanged.

In @tests/cli/test_lifecycle_daemon_commands.py at line 7, Add a module-level pytest unit marker in tests/cli/test_lifecycle_daemon_commands.py so the entire test module is categorized as unit tests, while preserving the existing pytest import and test behavior.

In @tests/mcp_proxy/test_manager_disconnect_cancellation.py around lines 36 - 45, Replace the BlockingConnection instance in this test with a minimal local stub object exposing is_connected = True and the instrumented async disconnect method that records the current task. Remove the unused BlockingConnection setup and preserve registration under the "stdio-server" connection key.

In @tests/mcp_proxy/test_wait_for_output.py around lines 161 - 169, Reduce the pathological input size in the test around _invoke and compile_safe_regex to the smallest buffer that still produces the expected "pattern_timeout" error, avoiding the full regex time budget during normal unit runs. If a smaller input cannot reliably trigger the timeout, mark this specific test as slow instead.

In @tests/mcp_proxy/test_wait_for_output.py around lines 43 - 221, Split test_wait_branches into focused tests, separating each independent wait_for_output scenario such as matching, timeout, terminal status, validation errors, pane loss, capture failure, pattern timeout, collision precedence, and cancellation cleanup. Keep each scenario’s existing setup and assertions intact, using parametrization only for genuinely similar payload-validation cases so failures identify the affected branch.

In @tests/mcp_proxy/test_wait_for_output.py around lines 181 - 192, Make the capture-versus-deadline test deterministic by removing its dependence on real wall-clock scheduling. Update the _run() invocation around deadline_collision_tmux to use a fake or patched agents.time.monotonic timeline, or otherwise provide a generous timeout so the intended capture_failed result and three capture_pane attempts are guaranteed without changing the production behavior.

In @tests/mcp_proxy/test_wait_for_output.py around lines 197 - 202, Update the return annotation of the blocking_capture helper to reflect that it never returns a value, while preserving its existing capture_started signaling, indefinite wait, and capture_finished cleanup behavior.

In @tests/mcp_proxy/tools/spawn_agent/test_fallback_agent.py around lines 12 - 14, Add a module-level pytest.mark.unit marker to test_fallback_agent.py, alongside the existing DETECTION_REGISTRY setup, so all tests in the module are categorized as unit tests. Ensure pytest is imported as needed and preserve the existing test behavior.

In @tests/projects/test_purge_components.py around lines 216 - 221, Replace the inline snapshot supplier lambda in the ProjectFencedVectorStore rebuild_from_supplier call with a small named function that appends "snapshot" to events and explicitly returns an empty list. Keep the existing text embedding lambda and rebuild behavior unchanged.

In @tests/projects/test_purge_service.py around lines 262 - 291, The test test_daily_handler_isolates_failures_and_bounds_id_lists does not exercise the handler’s list-size limit. Increase the generated candidate count and expected successful/failed results so at least one returned ID list exceeds the cap, or assert the configured cap explicitly; retain the failure-isolation assertions and ensure the test fails if bounding is removed.

In @tests/projects/test_purge_service.py around lines 54 - 60, Update FakeTransaction.execute to handle statements without “FROM ” explicitly instead of indexing an unconditional split result. Preserve the existing table extraction and project hard-delete behavior for DELETE FROM statements, while raising a clear error for unsupported SQL statements such as UPDATE.

In @tests/projects/test_write_fence.py around lines 62 - 64, Bound all synchronization waits in the affected tests, including wait_for_exclusive_claim and the writer_entered, purge_task, and background-task gather waits, with finite asyncio.wait_for timeouts (or an equivalent pytest timeout). Ensure timeout failures propagate as test failures instead of allowing CI to hang.

In @tests/projects/test_write_fence.py around lines 1 - 18, Add a module-level pytestmark assignment after the import block in tests/projects/test_write_fence.py, setting this suite’s marker to pytest.mark.unit. Keep the existing imports and test behavior unchanged so marker-based unit test selection includes all tests in the module.

In @tests/projects/test_write_fence.py around lines 107 - 108, Replace the direct _condition/_exclusive wait in the test with the shared wait_for_exclusive_claim helper, and make that helper importable from a common tests/projects/fence_helpers.py location if needed. Update the duplicate waits in tests/github_triage/test_issue_index.py, tests/mcp_proxy/test_semantic_search.py, and tests/memory/test_indexing_service.py to use the same helper, keeping ProjectWriteFence private-state access centralized.

In @tests/runner_init/test_detection_registry_composition.py around lines 42 - 49, The test around create_agents_registry should explicitly verify that the spawn registrar was invoked before indexing captured_contexts. Add a readable assertion that captured_contexts is non-empty, then retain the existing context.detection_registry wiring assertion.

In @tests/runner_init/test_detection_registry_composition.py around lines 1 - 16, Mark the test module as a unit test by adding pytestmark = pytest.mark.unit near the existing pytest import in test_detection_registry_composition.py. Use the imported pytest symbol and leave the rest of the test setup unchanged.

In @tests/servers/routes/test_embeddings_routes.py around lines 227 - 261, Add a POST request to /api/embeddings/switch/resume in test_embedding_switch_routes_delegate_to_daemon_coordinator, assert its response status is "resumed", and retain the existing delegation assertions. Also add a focused test for a runner without embedding_switch_coordinator that verifies the endpoint returns the expected unavailable-service response.

In @tests/servers/routes/test_projects_routes.py around lines 671 - 694, Extend test_purge_project_uses_runner_service with meaningful negative-path tests for POST /api/projects/{id}/purge: verify the expected HTTP status when project_purge_service is unavailable on server._runner, and when PurgeService returns protected or failed PurgeOutcome values. Assert both response status codes and outcome payloads, ensuring failed purges are not accepted as HTTP 200.

In @tests/servers/routes/test_projects_routes.py at line 687, Update the test setup to use the public server wiring API by calling set_runner_getter(...) with the runner provider instead of assigning server._runner directly. Preserve the existing PurgeService-backed runner behavior while avoiding the backward-compatible_runner test hook.

In @tests/servers/test_attention_respond.py around lines 158 - 160, Update the assertions around manager.get in the affected test cases, including the block near the accepted response and the matching case near line 279, to first assert the returned AttentionState is not None, then access .state on that narrowed result. Preserve the expected None state assertion and follow the existing strict-mypy pattern used elsewhere in the file.

In @tests/servers/test_attention_roster.py around lines 110 - 158, Replace the timing-based negative assertions on second_done and transition_done with deterministic mutual-exclusion checks, such as tracking concurrent critical-section entries and asserting the count never exceeds one. Keep the existing synchronization and completion assertions, and ensure the test validates blocking through the invariant rather than a fixed 0.05-second timeout.

In @tests/servers/test_attention_roster.py around lines 262 - 265, Reorder the assertions in the roster response test so `roster.status_code == 200` is validated before calling `roster.json()` or accessing `["entries"]`. Keep the existing sequence assertion and entry lookups unchanged after the status check.

In @tests/servers/test_attention_roster.py around lines 99 - 115, Capture exceptions from both worker-thread targets, including failures from _open or manager.transition, in a shared errors collection; wrap first and second_transition so each records its exception instead of allowing threading to swallow it. After both joins, assert that no worker errors were collected before validating event order and sequence.

In @tests/skills/hubs/test_github_topic.py around lines 137 - 138, Apply a category marker to the async tests test_sha_pinned_identity and the additional tests at the referenced locations, using the appropriate unit or integration marker consistent with their scope. Preserve the existing pytest.mark.asyncio decorators.

In @tests/skills/hubs/test_github_topic.py around lines 272 - 296, The existing test only covers missing and traversal failures; add an end-to-end case for the install_skill provenance validation path. Stub download_skill to return a DownloadResult whose provenance has a mismatched repository or SHA relative to the requested item, then assert install_skill returns success=False with an item_unavailable error and does not persist the skill.

In @tests/skills/test_wiki_research_skill.py around lines 33 - 46, Update test_run_report_records_triaged_away_items in tests/skills/test_wiki_research_skill.py to import pytest and apply the pytest.mark.unit marker so this static skill-contract test is selectable by the repository’s required test markers.

In @tests/storage/test_agent_sandbox_records.py around lines 13 - 67, Add the module-level unit test marker to tests/storage/test_agent_sandbox_records.py so both sandbox_record tests are consistently classified as unit coverage, using the existing pytest marker convention without changing their assertions or behavior.

In @tests/sync/test_external_coordinator.py around lines 205 - 206, Update test_usage_limit_uses_maximum_backoff to add the unit pytest marker alongside its existing asyncio marker, preserving the test’s current behavior and setup.

In @tests/test_runner_lifecycle_subsystems.py around lines 82 - 136, Add @pytest.mark.unit to both test_global_wiki_prune_registers_before_empty_project_return and test_startup_vector_rebuild_includes_project_id_payload so the isolated lifecycle tests are included in unit-test selection.

In @tests/wiki/test_prune_job.py around lines 57 - 177, The new tests in test_register_wiki_prune_cron_creates_hourly_system_job, test_registered_wiki_prune_handler_is_callable, test_register_wiki_prune_cron_preserves_toggle_and_wakes_only_enabled_rows, test_wiki_prune_handler_reports_command_failure, and test_wiki_prune_handler_reports_timeout_and_unavailable should be marked with @pytest.mark.unit. For async tests, add the unit marker alongside the existing asyncio marker.

In @crates/gwiki/src/audit.rs at line 30, Restrict the audit exemption currently matching the “Later review” heading in the relevant audit logic instead of globally skipping every section with that title. Apply it only to generated navigation entries or the specific backlog flow, and add a regression test confirming substantive claims under a “Later review” section are still audited.

In @package.json around lines 11 - 12, Update the root package.json scripts.test entry to remove the guaranteed-failing placeholder and invoke the repository’s intended test command; if no test suite exists, remove the script instead of retaining a command that always exits with an error.

In @src/gobby/agents/attention_metadata.py around lines 46 - 102, Add a clear/delete API to AttentionMetadataStore that removes the specified entry immediately and publishes the appropriate cursor-ordered update, then invoke it from the attention-resolution paths clear_attention and clear_attention_after_injection so resolved chips disappear before TTL expiry. Preserve existing set, get, and snapshot behavior for unaffected entries.

In @src/gobby/agents/idle_check_handler.py around lines 18 - 25, The idle-check handler is hardcoded to Codex instead of using the provider-neutral reader contract. Replace direct CODEX_MODEL_CAPACITY_MESSAGE and read_codex_transcript_snapshot usage with WatchdogReaderRegistry().for_provider(run.provider), then use the resolved reader’s capacity_pane_message and read(...) methods while preserving the existing CapacityRecoveryState and WatchdogTranscriptSnapshot flow.

In @src/gobby/agents/watchdog/codex.py around lines 45 - 126, Update _read_codex_snapshot to maintain per-transcript resume state keyed by transcript_path, including the last processed byte offset or line and accumulated snapshot fields such as tail, turn markers, provider errors, and activity metadata. On subsequent calls, seek to the saved cursor and scan only newly appended JSONL content, advancing and persisting the cursor; handle truncation or replacement by resetting state and scanning from the beginning while preserving the existing classification behavior.

In @src/gobby/agents/watchdog/registry.py at line 15, Replace the assert comparing _READERS with KNOWN_WATCHDOG_PROVIDERS in the watchdog registry with an explicit conditional that raises an appropriate exception when the sets differ, ensuring the invariant is enforced even under Python optimization.

In @src/gobby/app_context.py at line 84, Update the attention_metadata_store annotation in the app context class to use AttentionMetadataStore | None instead of Any | None, and add the AttentionMetadataStore import under TYPE_CHECKING consistent with the existing type-hint import pattern.

In @src/gobby/cli/sessions.py at line 175, Move the _blocked_attention_by_session(manager) call inside the with session_manager_context() as manager block in the relevant session command flow, ensuring manager remains active while its database is accessed. Keep the existing attention_by_session assignment and subsequent behavior unchanged.

In @src/gobby/cli/sessions.py around lines 67 - 88, Update _blocked_attention_by_session to deduplicate reasons per session before calculating the count, so repeated identical reasons do not inflate the badge count. Preserve all distinct reasons in a deterministic order by joining them for the displayed summary, and update _format_attention only as needed to render the resulting count and combined reason correctly.

In @src/gobby/install/shared/prompts/memory/dream.md at line 36, Update Rule 3 in the memory deletion guidance so assigning a delete verdict requires a concrete, citable obsolescence signal. Reframe high age_days, lack of recent access, and hard dates as corroborating evidence only, while preserving them as supporting factors when a concrete contradiction, supersession, completion, or time-bound-state signal is present.

In @src/gobby/install/shared/skills/wiki-research/SKILL.md around lines 148 - 150, Update Step 6 and Step 8 in the wiki-research skill to ensure custom output contracts provide a stable kebab-case finding slug when accepted-note files are omitted, or derive it from a guaranteed source/item identifier. Use that slug, rather than assuming an accepted-note basename exists, for both hidden markers and retry idempotency.

In @src/gobby/install/shared/workflows/pipelines/wiki-research.yaml around lines 99 - 105, Update the validation_criteria for the research task to conditionally require the create_tasks branch: when inputs.create_tasks is "true", require linked tasks for surviving findings and triaged-away items with their reasons, matching Skill Steps 9–10. Preserve the existing backlog, backlink, report, source-list, topic, and budget requirements for all runs.

In @src/gobby/memory/dream/related.py around lines 211 - 239, Update run_call so semaphore acquisition occurs outside the asyncio.timeout scope, leaving only the awaited operation task subject to RELATED_EVIDENCE_CALL_TIMEOUT_SECONDS. On TimeoutError, explicitly cancel the created task and await it with cancellation suppressed or handled before returning_CallOutcome(timed_out=True), while preserving the existing failure handling and task naming.

In @src/gobby/memory/dream/service.py around lines 778 - 835, Bound the dry-run preview around the candidate pagination loop in the method containing list_dream_candidate_ids and build_raw_plan by applying a configurable maximum candidate count or page count, preserving the single-request preview limit. Keep collecting only up to the configured action sample for persisted plan data, record the total action count and whether results were truncated, and expose the truncation metadata in the returned summary/plan instead of storing all_actions unbounded.

In @src/gobby/memory/services/maintenance.py at line 33, Update get_stats to log the exception when vector-count retrieval fails, immediately before assigning the fallback vector_count = -1. Preserve the existing fallback behavior while including the caught exception and useful context in the service’s established logging mechanism.

In @src/gobby/memory/vectorstore_client.py around lines 100 - 112, Update the local branch of the client operation helper to calculate and validate the same timeout budget before offloading, then wrap the asyncio.to_thread call in asyncio.timeout(budget). Preserve the existing remote timeout behavior and ensure non-positive budgets raise the same TimeoutError.

In @src/gobby/memory/vectorstore_maintenance.py around lines 174 - 184, Move the stale_delete_strategy validation to the start of prepare_collection_for_rebuild, before any lock acquisition, temporary collection creation, or rebuild-plan preparation. Keep the existing accepted values, "precompute" and "streaming", and remove the later duplicate check near batch_size and incoming_ids.

In @src/gobby/memory/vectorstore_maintenance.py around lines 165 - 212, Prevent rebuild deadlocks caused by repeated initialization during batch writes: reuse the client returned by_ensure_initialized() throughout rebuild() instead of letting batch_upsert() call _ensure_initialized() while _collection_lifecycle_lock is held. Update both batch_upsert() calls in the rebuild loop and the final batch flush to use the initialized client or an equivalent no-reinitialization path, while preserving existing target collection selection.

In @src/gobby/memory/vectorstore_queries.py around lines 136 - 161, The vector query batching in the shown method hardcodes the batch size 50 in both the range step and slice window. Define a module-level constant such as_STORED_VECTOR_BATCH_SIZE and use it for both expressions, preserving the existing batching behavior while making the size adjustable.

In @src/gobby/projects/vector_cleanup.py around lines 37 - 43, Replace the client-type dispatch in_managed_physical_collections with a VectorStore list_collections() helper that delegates through_call_client, preserving request timeout handling. Update both this method and the corresponding collection-listing logic in the embedding installer to use the helper instead of inspecting AsyncQdrantClient directly.

In @src/gobby/servers/_app_ui.py around lines 59 - 73, Update the WebSocket handling flow around WebSocketServer._handle_connection so handler exceptions are caught, logged, and followed by the existing fallback close whenever the adapter remains open or connected. Prefer adding and using a public WebSocketServer entry point instead of calling the private _handle_connection directly, while preserving the clean-return behavior.

In @src/gobby/servers/websocket/asgi_adapter.py around lines 35 - 41, Update WebSocketAdapter.close to tolerate Starlette websocket close failures caused by an already-disconnected peer: preserve the existing closed/disconnected guard and state updates, but catch and suppress the expected close exception from_websocket.close so fallback cleanup cannot raise.

In @src/gobby/servers/websocket/broadcast.py at line 16, Move the AttentionMetadataStore import in BroadcastMixin’s module into a TYPE_CHECKING-only block, since it is used only for annotations. Preserve annotation resolution at runtime using the module’s existing annotation strategy, without changing BroadcastMixin behavior.

In @src/gobby/storage/hub/async_ops.py around lines 73 - 78, The dynamic SET LOCAL statements in the timeout setup should use psycopg.sql composition to satisfy static analysis while preserving integer timeout values. Update both statement_timeout and lock_timeout branches to build the SQL with psycopg.sql.SQL(...).format(psycopg.sql.Literal(timeout_ms)) before connection.execute, keeping _timeout_milliseconds and _require_remaining unchanged.

In @src/gobby/storage/hub/async_ops.py around lines 145 - 155, Update_result_or_raise so QueryCanceled and LockNotAvailable are mapped to _raise_timeout only when no commit is in flight or the commit outcome has already been observed; when state.commit_submitted is true and state.commit_observed is false, route these exceptions through _raise_indeterminate instead. Preserve existing handling for _WorkBudgetExpired and deterministic query failures.

In @src/gobby/storage/hub/postgres.py at line 40, Make the cross-module postgres_pool API intentional by promoting or explicitly re-exporting_advisory_lock_keys, _PostgresCursor,_conninfo_with_utc_session_timezone, and _validate_identifier, then update their consumers in the hub module to use the public names consistently. Preserve existing behavior while eliminating direct access to underscore-prefixed postgres_pool members.

In @src/gobby/storage/pipeline_history.py around lines 110 - 114, Update the transaction block in the pipeline-history deletion method to inspect the result of the project-row lock query and assert that the project exists before executing_history_query. If no row is returned, stop the delete flow using the method’s established missing-project behavior rather than proceeding without serialization.

In @tests/agents/test_attention_metadata.py around lines 127 - 143, Update the attention reconciliation or roster refresh flow associated with attention_changed events and epoch changes so clients also discover entries removed by passive TTL expiry, without relying solely on follow-up events. Ensure the client-side polling or scheduled refresh path clears stale blocked badges when an entry expires, while preserving existing event-driven reconciliation behavior.

In @tests/fixtures/regressions/task_close_evidence_18689/assembled_close_packet.json around lines 6 - 33, Update the test covering the assembled close packet fixture to explicitly assert that its close_limit matches CLOSE_VALIDATION_EVIDENCE_CONTEXT_LIMIT, while preserving the existing packet command assertions. Ensure a limit change produces a clear invariant failure instead of only an opaque fixture diff.

In @tests/memory/test_dream.py around lines 285 - 364, Update both size calculations in test_batch_split_guard and test_single_item_oversize_dispatches_intact to use the planner’s_render_candidates_json renderer instead of directly calling json.dumps with hardcoded formatting options. Add the import for _render_candidates_json and preserve the existing batch-size and oversize assertions using the renderer’s returned JSON length.

In @tests/memory/test_dream_related.py at line 34, Update the pytest markers for test_keyword_scope_sql_contract, test_async_keyword_and_hydration_use_dedicated_statements, and test_keyword_global_not_starved, along with the other postgres_db-backed tests, so they use the integration marker instead of pytest.mark.unit. Keep genuinely unit-only tests marked as unit.

In @tests/memory/test_dream_related.py around lines 299 - 319, Update test_vector_floor_boundaries to derive the expected retained score from VECTOR_EVIDENCE_MIN_SCORE plus the same 0.05 offset used by the “above” fixture, rather than asserting the binary-float literal 0.39999999999999997. Keep the existing filtering and result structure unchanged.

In @tests/regressions/test_task_close_evidence_18689.py at line 66, Add the appropriate category marker to each of the four tests in this file, including test_more_than_fifty_commands_drop_relevant_early_results and the tests at the referenced locations; use the existing project marker convention and choose the category that matches each test’s scope.

In @tests/regressions/test_task_close_evidence_18689.py around lines 66 - 82, Mark the reproduction tests for bounded_session_evidence_loss, selector_outcome_weakening, and readiness_close_projection_divergence as intentional known-defect snapshots, using the project’s established marker or strict xfail with a clear reason. Apply the marker to the relevant test functions, including test_more_than_fifty_commands_drop_relevant_early_results, without changing their buggy-outcome assertions.

In @tests/servers/test_ws_asgi_endpoint.py around lines 107 - 120, Expand the WebSocket endpoint tests around_WebSocketServer and _handle_connection so they cover an actual handler exception rather than only return_immediately, including the expected 1011 behavior. Add cases for websocket_server being None with the 1013 response and for authentication-disabled configuration, while retaining the existing clean-return coverage.

In @tests/sessions/test_acp_lifecycle_service.py around lines 164 - 196, Add a pytest-asyncio test covering ACPSessionLifecycleService.delete when session_manager.delete returns False: ensure the initial session lookup succeeds, then configure the fake manager’s delete behavior to return False, invoke delete, and assert ACPSessionNotFoundError. Reuse the existing _FakeSessionManager and_FakeSession patterns without changing the existing deletion tests.

In @tests/skills/test_wiki_research_skill.py around lines 10 - 68, Add pytest marker support to the three contract tests in test_backlog_entry_contract_is_detailed_and_idempotent, test_investigation_tasks_require_opt_in_and_link_to_backlog, and test_run_report_records_triaged_away_items. Import pytest and apply the unit marker at class or individual-test scope so marker-based selection identifies them as unit tests.

In @tests/storage/hub/test_async_ops.py around lines 89 - 101, Widen the elapsed-time upper-bound tolerance used by _assert_bounded_timeout, including the corresponding assertion at the additionally affected range, while retaining the existing deadline check and sentinel.done() event-loop progress assertion.

In @tests/storage/hub/test_async_ops.py around lines 314 - 427, Split test_termination_matrix into separate pytest-parametrized cases covering blocked connect, fake-connection SET LOCAL blocking, fake-connection statement blocking, foreign-row lock waiting, and proxy-blocked cancellation. Give each case isolated setup and teardown while preserving the existing bounded-timeout, cancellation-count, callback-quiescence, and thread-cleanup assertions.

In @tests/storage/hub/test_async_ops.py around lines 23 - 27, Add the appropriate pytest category markers to the async tests in this module: mark the live PostgreSQL/socket-dependent tests as integration and the timing-sensitive tests as slow, using the existing pytest marker conventions. Keep pytestmark for asyncio and apply markers at the narrowest test or module scope that covers the affected tests.

In @tests/storage/hub/test_postgres_placeholder_remap.py around lines 27 - 28, Annotate the_postgres_pool_module helper with a ModuleType return type, and import ModuleType from types alongside the existing imports.

In @tests/storage/hub/test_protocol_contract.py around lines 146 - 152, Update the conninfo assertion in the PostgresHubDatabase test to compare conninfo_to_dict(database.conninfo) with the expected key-value mapping instead of relying on exact string ordering. Keep the concrete_property immutability assertion unchanged.

In @web/scripts/copy-ghostty-wasm.cjs around lines 7 - 30, Update the source resolution in the copy script around SRC to use require.resolve('@wterm/ghostty/package.json') (or the package’s resolvable root entry) and derive the wasm path from that package location instead of assuming web/node_modules. Replace the warning-and-skip behavior when the wasm source is absent with an explicit error and nonzero process exit, while preserving the existing destination creation and copy flow.

In @web/src/components/activity/SessionsTab.helpers.tsx around lines 151 - 160, Update the blocked-count chip in the sessions activity rendering to include entry.attentionReasons in its accessible name, rather than exposing reasons only through the title attribute. Preserve the existing blocked count label and join multiple reasons consistently with the current display format.

In @web/src/components/activity/SessionsTab.tsx at line 126, Extract the attention roster and agent-event handling currently used by useAgentRuns into a dedicated useSessionAttention hook, then have useAgentRuns consume that hook for attentionBySession. Update SessionsTab to use useSessionAttention directly and remove its useAgentRuns dependency, ensuring the tab no longer starts the agent-runs fetch or polling stream.

In @web/src/components/activity/terminal/TerminalKeysBar.tsx at line 58, Add role="group" to the div with aria-label="Terminal quick keys" in the TerminalKeysBar component so assistive technologies recognize and announce its accessible name.

In @web/src/components/activity/terminal/TerminalView.tsx at line 104, Remove the unused destroyed flag and its cleanup guard from the effect containing TerminalView’s cleanup logic, including the corresponding code around the additional referenced location; retain the actual cleanup operations unchanged.

In @web/src/components/activity/terminal/__tests__/TerminalTab.test.tsx at line 15, Fix the TerminalTab renderer replacement test by either asserting that data-mount-id changes across the renderer replacement, with a target-switch-driven keyed remount, or removing the unused terminalViewState.mounts/data-mount-id scaffolding and renaming the test to reflect repeated onReady calls. Ensure the three readiness controls exercise distinguishable renderer instances rather than invoking identical callbacks on the same mount.

In @web/src/components/activity/useSessionProviderOptions.ts around lines 46 - 49, Update the fetch error handler in useSessionProviderOptions to log non-abort failures with structured context before setting registryLoaded to false. Keep AbortError handling silent and preserve the existing state update for all other errors.

In @web/src/components/chat/ChatMainColumn.tsx around lines 163 - 176, Extract the gobby:show-activity-tab CustomEvent dispatch into a shared typed showActivityTab(tab, sessionId?) helper, matching the existing event detail contract. Update ChatMainColumn’s onOpenTerminal handler and the corresponding useActivityPanel dispatches to call this helper, removing duplicated event names and detail-shape literals.

In @web/src/hooks/useAgentRuns.ts around lines 184 - 190, Update the buffered-event handling in the roster snapshot flow: partition pending events by matching data.epoch, discard non-matching stale events after a single resync attempt, and avoid the unbounded zero-delay fetch loop by tracking attempts with a resyncAttemptedRef alongside the other refs and applying a small delay/retry cap. Use the matching events in the replay loop instead of pending, while preserving normal replay for events with the current epoch.

In @web/src/hooks/useAgentRuns.ts around lines 262 - 282, Resync the attention roster when the WebSocket reconnects by updating the useEffect that invokes fetchAttentionRoster, using useWebSocketConnected() as a dependency or resetting attentionCursorRef on reconnection. Ensure the roster is fetched again after a socket outage so missed attention_changed events cannot leave attentionBySession stale.

In @web/src/hooks/useTmuxSessions.ts around lines 163 - 181, Guard the setIsLoading(false) calls in handleMessage for both the tmux_sessions_list and tmux_kill_result cases with pendingRequestRef.current === null. Keep loading active while any request is pending, and only clear it when no pending request remains.

In @web/src/hooks/useTmuxSessions.ts around lines 119 - 161, Implement a pending-request deadline for attach, detach, and create-session operations: after each corresponding ws.send call in beginAttachRequest, beginDetachRequest, and createSession, schedule a timeout that verifies the same request is still pending, clears it, resets requestPending/loading state as appropriate, and sets attachError. Store the timer so clearPendingRequest and unmount cleanup cancel it, while preserving correlated-response and socket-close handling.

In @web/tests/terminal-colors.spec.ts around lines 250 - 255, Update chooseTerminalSession to use an exact accessible-name matcher for the option instead of constructing a RegExp from the name variable, ensuring only the intended terminal session is selected and satisfying the regexp-from-variable lint rule.

In @crates/gcode/src/commands/codewiki/generation.rs around lines 49 - 53, Correct the aggregate generator documentation in the module containing the tool-loop aggregate generator so it only claims tool-loop production and hard-failure behavior for repo overview and architecture pages; remove curated navigation, concept, and narrative pages unless they are actually routed through tool_loop. Update the corresponding module comment in codewiki/mod.rs to match the implemented generation paths and fallback behavior.

In @crates/gcode/src/commands/codewiki/mod.rs around lines 56 - 58, Update the tool-loop description near the module-level comments in codewiki to remove curated navigation, concept, and narrative pages from the claimed output. Keep the description limited to pages actually produced by the tool loop, including its frontmatter and failure behavior.

In @crates/gcore/src/ai/generation/one_shot.rs around lines 187 - 201, The Direct-route path creates a new reqwest blocking Client for every generate_text_with_target call, preventing connection reuse. Update the surrounding generation flow to reuse a shared Client across calls, while preserving the existing request construction, authentication, timeout, retry, and response parsing behavior.

In @crates/gcore/src/ai/generation/one_shot.rs around lines 14 - 44, Move the use declarations for ChatCompletionRequest, ChatMessage, ToolChoice, and build_request_body above budget_for_tier so all imports remain together; leave budget_for_tier and its behavior unchanged.

In @crates/gcore/src/ai/generation/tests/profile.rs around lines 162 - 219, Add a test alongside profile_resolves_api_keys_from_recognized_provider_environment covering a recognized provider such as "openai" with a custom, non-default TEXT_GENERATE_API_BASE. Set OPENAI_API_KEY and assert resolve_direct_generation_target returns no API key for that custom base, preserving environment-key resolution only for approved provider/base combinations.

In @crates/gcore/src/search.rs around lines 146 - 159, Refactor the unescaped quote counting in the surrounding search logic to use an explicit fold or loop instead of mutating quote_backslash_run inside the filter predicate. Preserve the existing handling of backslash runs and the balanced_quotes calculation, including treating quotes as unescaped only when preceded by an even-length backslash run.

In @crates/gwiki/src/commands/generation_routes.rs around lines 58 - 61, Add a test alongside the existing generation-route policy tests named daemon_agentic_max_turns_matches_tool_loop_default. Assert that DAEMON_AGENTIC_MAX_TURNS equals ToolLoopLimits::default().max_turns, preserving the documented coupling between daemon and direct-route turn budgets.

In @crates/gwiki/src/search/bm25.rs around lines 426 - 434, Update the skip message in postgres_test_database_url to mention both accepted environment variables, GWIKI_POSTGRES_TEST_DATABASE_URL and GCODE_POSTGRES_TEST_DATABASE_URL, so users know either can configure the test database.

In @crates/gwiki/src/search/graph_boost.rs around lines 229 - 261, Unify the backward-link scoring used by the ranking logic in the shown function and MemoryWikiGraph::related_paths_with_options. Extract the shared resolved-link/outdegree calculation and backward-weight formula, or at minimum define and reuse a shared BACKWARD_LINK_WEIGHT constant, while preserving options.backward_link_weight behavior where configured so both backends cannot silently diverge.

In @docs/guides/llm-features.md around lines 10 - 12, Align the later profile_defaults.feature_low YAML example with the documented table by adding codex/gpt-5.6-luna and matching the listed order, or explicitly label the YAML as an intentional override. Ensure copy-paste users see consistent defaults.

In @pyproject.toml at line 6, Update the pyproject.toml license metadata from the custom LicenseRef-FSL-1.1-ALv2 value to the canonical SPDX identifier FSL-1.1-ALv2, preserving machine-readable package metadata.

In @src/gobby/adapters/codex_impl/item_normalization.py around lines 306 - 309, Update _mark_ambiguous and its callers to use an insertion-ordered mapping for ambiguous keys instead of a set, and replace arbitrary pop eviction with FIFO removal via the oldest key, matching _set_pending’s ordering. Update _set_pending’s ambiguous parameter annotation accordingly while preserving existing membership checks and fail-closed behavior.

In @src/gobby/cli/daemon.py around lines 543 - 550, Update the startup-summary UI URL logic near ui_resolution so effective dev mode reports the development frontend endpoint on port 60889, while production mode continues using the configured http_port. Adjust the startup-summary test to assert the correct endpoint for each effective UI mode.

In @src/gobby/llm/context_windows.py around lines 81 - 82, Replace the unbounded_UNKNOWN_CONTEXT_WINDOW_WARNED_MODELS set with bounded warning-dedup state so dynamically named models cannot grow memory usage indefinitely. Preserve one-warning-per-model behavior within the bound, and provide a clear reset hook for tests to prevent cross-test state leakage.

In @src/gobby/mcp_proxy/tools/sessions/_verification.py around lines 121 - 160, Update the manual attestation flow around `receipt_store.upsert` and the subsequent `append_to_bounded_list_variable` call to execute both writes within a single hub database transaction, using parameterized `$N` placeholders for database access. Ensure either both the verification receipt and variable append commit together or both roll back when either operation fails.

In @src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py around lines 359 - 368, Update the receipt_packet.error branch in close_task to make the message actionable: state how to remediate the budget overflow, such as assigning or unassigning receipts, splitting the task, or retrying after adjustment, and include offending receipt IDs from disclosure when available. Preserve the existing failure response and evidence_completeness payload.

In @src/gobby/mcp_proxy/tools/tasks/_verification_receipts.py around lines 95 - 122, Update assign_verification_receipts to catch TaskNotFoundError alongside ValueError around resolve_task_id_for_mcp and the related task lookup, returning the existing structured failure response with the exception message. Preserve the current success response and project-scope validation behavior.

In @src/gobby/memory/dream/apply.py around lines 405 - 432, Update the transactional action dispatch around _apply_fenced_action so refresh actions without content follow the same cursor-advance behavior as_apply_action_legacy, while content-bearing refresh actions continue through the fenced path; then revise the stale defensive comment to describe the remaining fallback cases.

In @src/gobby/memory/dream/apply.py around lines 601 - 606, Remove the unreachable membership check and its ValueError after the action_name cast in the fenced dream action handling, or move validation to action.action before casting. Keep the existing Literal cast and supported action set behavior, avoiding a redundant post-cast guard.

In @src/gobby/memory/dream/models.py at line 81, Remove dream_due_version from the dictionary returned by to_prompt_dict(), keeping the internal optimistic-concurrency field available on the model but excluding it from planner prompt data unless an existing prompt template explicitly requires it.

In @src/gobby/memory/dream/protocols.py around lines 42 - 43, Align the protocol contract with its callers: update _apply_fenced_action,_advance_cursor, and the revert path in apply.py to invoke memory_manager.notify_memory_changed() directly instead of probing with getattr. If implementations are intentionally allowed to omit this method, remove it from the required protocol contract instead.

In @src/gobby/memory/dream/storage.py around lines 490 - 499, Replace the bare else in the action-handling chain with an explicit `elif action == "promote"` branch containing the existing promotion update. Add a trailing `raise ValueError(...)` for any unsupported action so new values cannot silently promote memories.

In @src/gobby/memory/dream/storage.py around lines 457 - 469, Update the duplicate query in the refresh flow to filter for active memories by adding the existing soft-delete condition (`deleted_at IS NULL`) alongside the current content, project, scope, and ID predicates, so soft-hidden rows do not block refresh.

In @src/gobby/memory/services/crossref.py around lines 213 - 238, Replace per-candidate calls to _current_stored_similarity with one batched stored-vector lookup for all candidate_ids before the cursor/transaction block, then reuse the returned scores while rebuilding cross-references. Preserve fallback behavior when stored-vector search is unsupported or fails, and update the candidate-processing logic to consume the batch results without issuing additional vector-store queries.

In @src/gobby/memory/services/crossref.py around lines 167 - 211, Update the crossref rebuild logic around the DELETE query and insertion loop: limit deletion to rows where memory.id is the source, since the loop only recreates source-owned edges. Preserve inbound crossrefs so get_related can continue reading relationships created by other memories.

In @src/gobby/memory/services/indexing.py around lines 581 - 607, Optimize _sweep_rebuild_snapshot to avoid one database transaction per memory by applying snapshot-clearing and reindex-needed updates in set-based, page-sized batches keyed by the existing identity tuple. Preserve CAS semantics and handle the boolean result of mark_vector_snapshot_reindexed so any failed CAS rows are explicitly re-marked for reindexing, while retaining rowless cleanup behavior.

In @src/gobby/memory/services/lifecycle.py around lines 524 - 572, The reconcile flow currently holds a process-wide advisory lock during embedding and vector-store I/O, serializing unrelated memory writes. Update reconcile and the analogous purge_secondary_indices path to use the existing per-memory lock-key strategy from _memory_lock_key instead of MEMORY_PROJECTION_FENCE_LOCK_KEY, preserving synchronization for the same memory while allowing unrelated memories to proceed.

In @src/gobby/memory/services/lifecycle.py around lines 699 - 710, Update restore_memory_indices around the _run_storage(self.storage.get_memory, ...) call to catch ValueError for missing or hard-deleted rows and return False, matching reconcile_memory_indices behavior. Preserve the existing field comparisons and return semantics for successfully retrieved memories.

In @src/gobby/servers/routes/sessions/lifecycle.py around lines 191 - 202, Extract the duplicated context_window_overrides resolution into a shared helper such as resolve_context_window_overrides(config), preserving the existing dict-or-None coercion. Update this lifecycle code and session_observe_proxy.handle_attach_to_session to use the helper while retaining their distinct config-source resolution.

In @src/gobby/servers/websocket/chat/backends/codex_turns.py around lines 260 - 268, Update the lifecycle deduplication logic around_apply_post_tool_lifecycle so lifecycle_completed_tool_call_ids is consulted and updated only when tool_call_id is non-empty. Ensure id-less tool events still invoke_apply_post_tool_lifecycle independently, while preserving deduplication for valid tool-call IDs.

In @src/gobby/servers/websocket/handlers/session_observe_proxy.py around lines 176 - 189, Update the context-window lookup in the session observation handler to execute effective_context_window_for_session through await run_db(mixin, ...), preserving the existing session, live_variables, db, and context_window_overrides arguments and assigning the returned value to context_window.

In @src/gobby/sessions/processor_lifecycle.py around lines 152 - 153, Update the close-time Codex handling around register_session and flush_session so sessions newly registered for reconciliation are unregistered after the flush completes. Track whether the session was already registered, preserve existing registrations, and call unregister_session only for sessions introduced by this path.

In @src/gobby/storage/memories_crud.py around lines 229 - 232, Keep the parameterized query in the locked-row retrieval flow unchanged, and add a brief comment adjacent to the existing nosec marker explaining that the interpolated SQL contains only generated %s placeholders while locked_ids values are passed as bound parameters.

In @src/gobby/storage/memories_crud.py around lines 151 - 156, Resolve the final_memory_id, including deduplication and collision handling, before acquiring any advisory locks in the memory write flow. Update the locking logic around the initial lock set and the later final_memory_id handling so the complete union of supersedes_ids, current_memory_id, and resolved final_memory_id is acquired exactly once in sorted order before row work begins; remove the later out-of-order lock acquisition.

In @src/gobby/storage/memories_crud.py around lines 714 - 737, Update mark_vector_snapshot_reindexed to capture cursor.rowcount inside the self.db.transaction() context, store it in a local result, and use that value for notify_changed() and the boolean return after the transaction exits.

In @src/gobby/storage/memories_dreams.py at line 94, Standardize change notifications in mark_project_memories_due, mark_global_memories_due, and purge_dream_hidden by replacing their private_notify_listeners() calls with the public notify_changed() method, matching the updated paths and preserving the existing notification timing.

In @src/gobby/storage/memories_dreams.py around lines 158 - 163, Update the restore/re-queue SQL executed in the memories flow to set graph_attempts = 0 alongside graph_processed = FALSE and graph_status = 'pending'. Match the reset behavior used by the comparable lifecycle.py stamp path while preserving the existing fields and parameters.

In @src/gobby/storage/migrations/334_verification_receipts.sql around lines 5 - 7, Document in the migration why verification_receipts.session_id intentionally has no foreign-key constraint, noting that receipts must survive session deletion and therefore may contain dangling session UUIDs. Keep session_id NOT NULL and the existing task_id foreign key unchanged.

In @src/gobby/storage/migrations/335_memories_dream_due_version.sql around lines 1 - 2, Change the dream_due_version column definition in migration 335_memories_dream_due_version.sql from INTEGER to BIGINT, preserving its NOT NULL constraint and DEFAULT 0.

In @src/gobby/storage/model_metadata.py around lines 45 - 113, Reset the module-level_stale_warning_emitted flag to False after populate() successfully commits refreshed model metadata, before returning the inserted count. Keep the flag unchanged when models are unavailable or the transaction fails, so subsequent stale-cache cycles can emit a warning.

In @src/gobby/storage/verification_receipts.py around lines 24 - 30, Update_bounded_output to treat an empty output string the same as None, returning all-None values before encoding or hashing; preserve the existing bounded excerpt and digest behavior for non-empty output.

In @src/gobby/storage/verification_receipts.py around lines 254 - 277, Reflow the overlong SQL lines in the verification-receipt upsert statement, especially the conflict-condition line and output_sha256 assignment, so every source line stays within Ruff’s 100-character limit. Preserve the existing SQL behavior and formatting alignment around the outcome update and output field assignments.

In @src/gobby/storage/verification_receipts.py around lines 404 - 460, Update assign_unassigned to return the rows produced by the UPDATE directly: execute UPDATE ... RETURNING * inside the existing transaction, capture those rows, and build VerificationReceipt objects from them after the transaction. Remove the post-commit fetchall by ID so the method returns exactly the state committed by this operation.

In @src/gobby/sync/memories.py around lines 226 - 240, Update restore_owned and the corresponding restore flow around lines 300-316 to avoid serially awaiting reconciliation for each restored outcome. Batch these per-record operations or run them with bounded concurrency using a semaphore and asyncio.gather, while preserving the existing reconcile_memory_indices and schedule_write_mark_due calls and awaiting all scheduled work before returning the restored count.

In @src/gobby/sync/memories.py around lines 241 - 251, The cancellation branch around the owned restore task in the memory-restore flow must reliably wait for and consume owned_task even after caller cancellation. Replace the fragile direct second shield await in the restore_owned handling with cancellation-safe completion logic, such as waiting for owned_task via asyncio.wait or suppressing cancellation before a final await, while preserving propagation of the original CancelledError and existing MemoryRestoreError handling.

In @src/gobby/tasks/verification_receipt_packet.py around lines 121 - 128, The _aggregate function labels priority-ordered receipt endpoints as an ID range. Rename receipt_id_range to sample_receipt_ids and preserve the existing first/last values, or explicitly sort receipts by ID before deriving true bounds; ensure consumers no longer interpret the values as an inclusive interval.

In @src/gobby/tasks/verification_receipt_packet.py around lines 204 - 241, The packet-building loops should avoid repeatedly rendering the entire payload and rebuilding the full tail for each candidate. Update the detail and catalog selection logic around _render to maintain incremental serialized-size accounting for each added entry, using the running total to enforce budget_chars while preserving _DETAIL_LIMIT, mandatory catalog entries, and existing ordering/outcome behavior.

In @src/gobby/tasks/verification_receipt_packet.py around lines 40 - 59, Update _priority to remove the redundant leading explicit-ID tuple element and return only group, the negated timestamp, and receipt.id; preserve the existing group assignment and remaining tie-break ordering.

In @src/gobby/workflows/hooks.py around lines 575 - 587, The BEFORE_TOOL/AFTER_TOOL persistence path must retain projection deduplication from persist_verification_receipt instead of appending only the latest evidence item. Update the persistence block around append_to_bounded_list_variable to replace or merge the complete VERIFICATION_EVIDENCE_VARIABLE list, preserving removals and stale-entry cleanup while keeping bounded-list behavior where applicable.

In @src/gobby/workflows/hooks.py around lines 407 - 417, Update the snapshot identity propagation after _match_tool_context in the hook flow to preserve any native verification_execution_id already present on event.data. Only copy snapshot["verification_execution_id"] when the event lacks its own non-empty identifier; continue propagating verification_source_event_id as currently handled and retain the existing no-snapshot path.

In @src/gobby/workflows/observer_verification.py at line 23, Reduce_OUTPUT_LIMIT from 8,192 to a substantially smaller evidence excerpt size so the maximum verification evidence retained in session variables remains bounded; apply the same limit at both referenced usages and preserve the existing truncation behavior.

In @src/gobby/workflows/observer_verification.py at line 61, Update the receipt construction around the "timestamp" field to use event.timestamp instead of datetime.now(UTC). Preserve the existing ISO-format serialization so receipt timestamps match the corresponding event and maintain consistent ordering.

In @src/gobby/workflows/verification_evidence.py at line 43, Update the summary construction in the verification evidence workflow to format counts explicitly as stable key=value pairs joined by commas, instead of interpolating the Python dict directly. Preserve the existing count contents and summary wording while avoiding Python-specific quoting in the LLM-facing text.

In @src/gobby/workflows/verification_evidence.py around lines 92 - 94, Update the outcome_counts field declaration to enforce strict validation, matching receipt_count and latest_receipt_id, so mapping values such as stringified integers are rejected rather than coerced.

In @src/gobby/workflows/verification_receipt_ingestion.py around lines 9 - 22, Rename the underscore-prefixed symbols _SHELL_TOOLS, _extract_shell_command,_extract_shell_output_text, and_shell_tool_outcome to public names in their defining modules, then update all imports and references across both packages, including verification_receipt_ingestion, to use the renamed symbols consistently.

In @src/gobby/workflows/verification_receipt_ingestion.py around lines 160 - 168, Replace the list_for_task call in the task_id branch with a lightweight verification-outcome projection query that selects only normalized_outcome, id, and required timestamps, returning grouped counts plus the latest id/timestamp needed by project_verification_outcomes. Preserve the existing merge_receipt_projection_evidence and projection.ready flow while avoiding loading full receipt rows or output excerpts.

In @tests/llm/test_context_window.py around lines 7 - 9, Add a regression test in the context-window test suite that passes a non-primitive provider_reported_context_window value, such as a dict or list, through coerce_context_length and resolve_context_window_with_source, and verifies the expected safe result without raising UnboundLocalError. Follow the existing test patterns and preserve current unknown/registry attribution behavior.

In @tests/mcp_proxy/test_validation_integration.py around lines 505 - 538, Extend the existing_verification_receipt helper to accept an optional cwd parameter, preserving its current default behavior. Replace the inline VerificationReceipt construction loop with calls to_verification_receipt using the per-index command, index, and repo_path cwd, while retaining the existing 303-receipt sequence and ordering.

In @tests/memory/test_create_supersedes.py around lines 36 - 37, Add the required pytest category markers throughout tests/memory/test_create_supersedes.py: mark all real-Postgres tests as integration, and additionally mark fencing or concurrency tests with slow. Apply markers consistently to each relevant test, including test_auto_mark_due and the referenced cases, without changing their behavior.

In @tests/memory/test_create_supersedes.py around lines 1113 - 1147, Replace the timing-based assertions in test_supersedes_row_lock_fencing with deterministic synchronization: use explicit events or an equivalent observable signal to confirm restore_memory is blocked while the replacement purge holds the row lock, then release the purge and await both tasks without sub-100 ms or fixed cleanup timeouts. Preserve the test’s verification that restore succeeds and old.id is no longer deleted.

In @tests/memory/test_create_supersedes.py around lines 770 - 809, Update test_supersedes_rollback_on_failure to use the concrete psycopg exception type in pytest.raises, and wrap the failure-inducing manager.create_memory call in a finally block that drops fail_supersession_test_trigger and fail_supersession_test from temp_db. Ensure cleanup runs whether the operation raises as expected or unexpectedly, before the state assertions execute.

In @tests/storage/test_memories_dreams.py around lines 28 - 30, Add the repository’s unit-test marker decorator to test_mark_memories_due so it is categorized as a unit test and can be selected with the unit test suite.

In @tests/storage/test_memories_dreams.py around lines 12 - 16, Update the database helpers and statements in tests/storage/test_memories_dreams.py, including _insert_project and the additionally referenced query blocks, to execute through the repository’s Hub transaction API instead of direct database access. Replace every %s placeholder in these statements with consistently numbered $1, $2, etc. placeholders while preserving the existing query parameters and behavior.

In @tests/storage/test_memories_dreams.py at line 12, Add concrete type hints for the untyped db and temp_db parameters in_insert_project and the other affected functions around the referenced lines, using the appropriate database or fixture protocol types while preserving their existing return annotations.

In @tests/storage/test_verification_receipts.py around lines 58 - 70, Add a module-level pytestmark using the unit marker, matching the sibling verification test module, and annotate the _session helper with its concrete return type while preserving its existing registration behavior.

In @tests/sync/test_memory_sync.py around lines 18 - 23, Add type annotations to the parameters of the new test functions, including hub_db, tmp_path, and monkeypatch, and annotate the related and should_not_run callback parameters and return types. Apply the same typing consistently to the additional affected test and helper definitions while preserving their existing behavior.

In @tests/tasks/test_verification_outcome_projection.py around lines 53 - 91, Add a focused test alongside test_projection_requires_a_durable_success that supplies receipts ordered with a successful outcome followed by a failure, then assert project_verification_outcomes reports ready as True and preserves the expected outcome counts and latest receipt identity. This should pin the intended durable-success behavior when the newest receipt is failing.

In @tests/tasks/test_verification_receipt_packet.py around lines 1 - 12, Add the module-level pytest marker `pytestmark = pytest.mark.unit` in tests/tasks/test_verification_receipt_packet.py, alongside the existing imports, so all tests in the module are classified as unit tests.

In @tests/workflows/test_verification_receipt_ingestion.py around lines 55 - 64, Add the repository’s appropriate pytest marker to the new database-backed ingestion tests, including both parametrized test cases near the SessionSource list and the additional tests around the referenced second block. Use the existing integration marker convention and preserve the current parametrization and test behavior.

In @tests/workflows/test_verification_receipt_ingestion.py around lines 18 - 24, Add complete type annotations in_session and the related fixture functions at the referenced locations: annotate temp_db, session_manager, and sample_project parameters, and add_session’s return type. Use the concrete existing fixture/session/project types already used in the test module so mypy strict can validate all functions.

In @crates/gcode/src/commands/codewiki/build_parts/architecture.rs around lines 101 - 104, Update the observability aggregation in both affected paths to treat a missing generated observability turn count as zero, preserving the existing aggregate total from earlier successful generations. Keep the aggregate unset only when observability collection itself is not configured, and apply the change to the logic around the visible turns aggregation.

In @crates/gwiki/src/commands/ask/deep.rs around lines 362 - 385, Update deep_citation_check so zero-citation answers use a dedicated status or warning representation rather than inserting “answer contains no wiki citations” into unsupported_claims. Adjust the corresponding record_synthesis handling to render that condition as a dedicated no-citations warning, while preserving normal unsupported-claim reporting for answers containing citations.

In @crates/gwiki/src/commands/ask/deep.rs around lines 440 - 474, Update deep_citation_check and page_with_stem_exists so the vault directory is traversed at most once per deep_citation_check call: build a reusable case-insensitive page-stem index from the walk, then resolve each bare single-segment citation against that index instead of recursively scanning the vault per link. Preserve the existing markdown-file and recursive-directory matching behavior.

In @docs/guides/cron-scheduler.md around lines 171 - 175, Update the earlier MCP example for the known create_cron_job tool to call get_tool_schema directly, removing the preceding list_tools call. Preserve the existing schema lookup and subsequent tool invocation, while following the known-unleased-tool flow demonstrated elsewhere in the guide.

In @docs/guides/mcp-tools.md around lines 27 - 36, Update the discovery bullet in the MCP tools guide to say “unknown server or registry,” restricting list_mcp_servers usage to cases where either the server or registry is unknown. Keep the surrounding tool-discovery instructions unchanged.

In @src/gobby/agents/idle_check_handler.py at line 25, Promote _find_transcript_on_disk in gobby.sessions.transcript_paths to the public find_transcript_on_disk API, update idle_check_handler and other external callers to import the public name, and retain a thin private alias only if existing internal callers require it.

In @src/gobby/agents/idle_check_handler.py around lines 712 - 726, Update the fallback transcript reads in _recover_reasoning_idle and_log_transcript_snapshot to call _resolve_transcript_path instead of accessing session.transcript_path directly. Preserve the existing no-path and read-failure handling, while allowing the resolver to discover on-disk transcripts and reject the "missing_transcript" sentinel consistently.

In @src/gobby/agents/idle_check_handler.py around lines 245 - 283, The transcript resolver can continue returning an outdated cached file after the transcript is rotated during an active run. Update _resolve_transcript_path to revalidate cached paths against session.updated_at (for example, compare file mtime) and rediscover when the cached file is older, while preserving the existing cache lookup for current files and the fallback behavior when no valid transcript is found.

In @src/gobby/agents/idle_check_handler.py at line 263, Define a shared constant for the "missing_transcript" sentinel and update the transcript path checks in the relevant idle-check handling code to reference it instead of repeating the literal. Ensure all existing comparisons preserve their current behavior.

In @src/gobby/agents/watchdog/claude.py around lines 64 - 78, The lookup in _assistant_payload_type contains an unreachable "user_input" mapping because _assistant_activity_kind never returns that value. Remove the "user_input" entry while preserving the existing mappings and API-error handling.

In @src/gobby/agents/watchdog/claude.py around lines 1 - 15, Add from __future__ import annotations at the top of the Claude watchdog module to match the sibling readers, and explicitly declare its reader class as implementing the TranscriptWatchdogReader protocol. Apply the same protocol declaration to the reader classes in the additionally referenced sections, preserving their existing behavior and signatures.

In @src/gobby/agents/watchdog/claude.py around lines 143 - 162, The user-record handling in the transcript scanner incorrectly starts a new turn for tool results. In the `record_type == "user"` branch, keep the existing `payload_type` distinction and summary creation, but only assign `turn_started_event`, `latest_turn_event`, `latest_turn_kind`, and `latest_activity_kind = "user_input"` when `block_types` does not include `"tool_result"`; tool-result records should still be appended and validated without changing turn-start bookkeeping.

In @src/gobby/agents/watchdog/droid.py around lines 99 - 108, The Droid snapshot scanning logic around the session_end handling and the last-assistant-message path must populate latest_turn_event/latest_turn_kind for completed turn boundaries. Map session_end, and any applicable final assistant message, to turn_kind="completed" so_completed_turn_recovery_due can recover completed turns; otherwise explicitly preserve and document the intentional reduced capability for this Droid iteration.

In @src/gobby/agents/watchdog/droid.py around lines 109 - 115, Add a brief explanatory comment beside the todo_state validation in the watchdog scan logic, specifically around valid_todos, documenting that persisted records intentionally support both a list and a dict containing a string todos value. Do not change the validation behavior.

In @src/gobby/agents/watchdog/qwen.py at line 135, Update the event_type assignment in the record construction to preserve "tool_result" as event_type when record_type is "tool_result", rather than remapping it to "message"; leave other record types unchanged.

In @src/gobby/agents/watchdog/registry.py around lines 11 - 17, Update the _READERS mapping annotation to use dict[str, TranscriptWatchdogReader] without the None union, since all registered providers have readers. Preserve for_provider’s existing behavior of returning None only when the provider is unknown.

In @src/gobby/install/bundled_content_manifest.json around lines 14 - 17, The bundled content manifest contains stale entries for missing build safety and build skill files. Regenerate or update the manifest entries for rules/build/build-agent-safety.yaml and skills/build/SKILL.md so it includes only content paths present under shared, with hashes matching the current bundled files.

In @src/gobby/install/shared/workflows/agents/goal-taskmaster.yaml at line 88, Update the progressive-discovery guidance in the workflow to state that list_mcp_servers is used only for unknown servers or registries, not general server inspection. Apply the same wording correction to the identical guidance wherever it appears in the repository.

In @src/gobby/install/shared/workflows/agents/merge-orchestrator.yaml at line 174, Update the progressive-discovery guidance in the workflow instructions to restrict list_mcp_servers to unknown servers or registries, replacing the broader server-inspection wording. Apply the same wording correction to the identical guidance wherever it appears, while preserving the existing rules for leased, known, and unknown tools.

In @src/gobby/install/shared/workflows/agents/merge-worker.yaml at line 127, Update the progressive-discovery guidance in the merge-worker workflow so list_mcp_servers is permitted only for unknown servers or registries, replacing the broader server or registry inspection wording. Apply the same wording correction to the identical workflow guidance elsewhere, while leaving the tool-handling rules unchanged.

In @src/gobby/install/shared/workflows/agents/nightly-linter.yaml at line 50, Update the progressive-discovery guidance in the nightly-linter workflow and the identical guidance elsewhere to restrict list_mcp_servers to unknown servers or registries, replacing the broader server or registry inspection wording. Preserve the existing rules for leased, known, and unknown tools.

In @src/gobby/install/shared/workflows/agents/nightly-test-fixer.yaml at line 50, Update the progressive discovery guidance in the nightly test fixer workflow to state that list_mcp_servers is used only for unknown servers or registries, not general server inspection. Apply the same wording correction to the identical workflow guidance elsewhere.

In @src/gobby/install/shared/workflows/agents/plan-adversary-taskless.yaml at line 86, Update the progressive-discovery guidance in the workflow task text to restrict list_mcp_servers to unknown servers or registries, replacing the broader “server or registry inspection” wording. Apply the same wording correction to the identical guidance in the other workflow location, while leaving the list_tools and known-tool behavior unchanged.

In @src/gobby/install/shared/workflows/agents/plan-adversary.yaml at line 178, Update the progressive-discovery guidance in the workflow instruction to say list_mcp_servers is only for unknown servers or registries, not general server inspection. Apply the same wording correction to the identical guidance elsewhere, while preserving the rules for leased, known, and unknown tools.

In @src/gobby/install/shared/workflows/agents/plan-enhancer-taskless.yaml at line 67, Update the progressive discovery guidance in the workflow instruction to restrict list_mcp_servers to unknown servers or registries, replacing the broader server or registry inspection wording. Apply the same wording correction to the identical guidance in the other workflow location, while preserving the existing rules for leased, known, and unknown tools.

In @src/gobby/install/shared/workflows/agents/plan-enhancer.yaml at line 99, Update the progressive-discovery guidance in the workflow instruction containing “Use context-aware progressive discovery” so list_mcp_servers is permitted only for unknown servers or registries, not general server inspection. Apply the same wording correction to the identical guidance elsewhere, preserving the existing rules for leased, known, and unknown tools.

In @src/gobby/install/shared/workflows/agents/planner.yaml at line 106, Update the progressive-discovery guidance at the affected workflow instruction to state that list_mcp_servers is used only for unknown servers or registries, replacing the broader “server or registry inspection” wording. Apply the same wording correction to the identical guidance elsewhere, while preserving the existing rules for leased, known, and unknown tools.

In @src/gobby/install/shared/workflows/agents/product-manager.yaml at line 61, Update the progressive-discovery guidance in the product-manager workflow to state that list_mcp_servers is used only for unknown servers or registries, not general server inspection. Apply the same wording correction to the identical workflow guidance elsewhere, preserving the existing rules for known and unknown tools.

In @src/gobby/install/shared/workflows/agents/qa-dev.yaml at line 40, Update the progressive-discovery guidance in the QA developer workflow so list_mcp_servers is permitted only for unknown servers or registries, replacing the broader server or registry inspection wording. Apply the same wording correction to the identical workflow guidance elsewhere, while preserving the existing rules for known and unknown tools.

In @src/gobby/install/shared/workflows/agents/tech-writer.yaml at line 150, Update the completion condition in the close_task callback to require both a non-preview call and task_id matching assigned_task_id. Preserve the existing behavior for the assigned task while preventing successful closure of another task from completing the workflow.

In @src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py around lines 354 - 438, Update the preview path in the close-task validation flow so preview=true returns mechanical blocking reasons without invoking validate_leaf_task_with_llm for code leaves. Preserve the existing gate results and normal full validation for non-preview closes, and ensure read-only previews still honor the existing provider backoff window when an LLM check is reached.

In @src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_preview.py around lines 161 - 165, Update the ValueError handler in the task commit-linking flow to set error to the stable code invalid_commit_sha, while retaining the exception text in message for diagnostics. Keep the existing success=false response and surrounding commit tracking unchanged.

In @src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_preview.py around lines 106 - 125, Update the exception handler around resolve_task_tagged_commits in the close-task lifecycle flow to log the caught exception with useful context before returning the existing failure response. Add and use a module-level logger for this diagnostic, while preserving the current error payload and control flow.

In @src/gobby/sessions/transcript_index_sidecar.py around lines 255 - 281, Update the append-validation logic in the transcript index validation function to persist and compare a digest of the indexed byte prefix before accepting append mode, in addition to the existing device/inode and size checks. Ensure truncate-and-rewrite files with matching identity metadata are rejected unless the previously indexed prefix still matches, and add a regression test covering an in-place rewrite that produces a larger file.

In @src/gobby/tasks/commits.py around lines 539 - 575, Extract the shared git-log command construction and sha|message parsing from resolve_task_tagged_commits and auto_link_commits into a reusable helper, then have both flows call it. Preserve each function’s existing branch, since, working directory, and task-ID filtering behavior while ensuring commit extraction is maintained in only one implementation.

In @src/gobby/tasks/commits.py around lines 603 - 614, Avoid the duplicate and uncaught lookup in the task-linking flow: update _resolve_task_filter to return the already-fetched task object alongside its existing results, then use that object for seq_num and commits instead of calling task_manager.get_task again. Preserve the existing empty AutoLinkResult behavior when resolution fails.

In @src/gobby/tasks/commits.py around lines 615 - 636, Update the single-task commit-processing flow around resolve_task_tagged_commits so unresolved commit references are detected and recorded in result.skipped_refs before applying the task_id filter. Preserve the existing duplicate, link-error, linked-task, and total-count behavior for resolved commits.

In @tests/agents/test_lifecycle_monitor_watchdog_diagnostics.py around lines 200 - 296, Update test_idle_reasoning_watchdog_interrupts_supported_reader_and_records_task_event to either clearly identify Codex-only coverage in its name or parameterize it across readers whose supports_reasoning_interrupt is true. Add a negative case covering the newly supported Claude, Droid, Grok, and Qwen readers with supports_reasoning_interrupt set to false, verifying the watchdog does not interrupt them.

In @tests/agents/test_lifecycle_monitor_watchdog_diagnostics.py around lines 183 - 197, Decouple the diagnostics assertions from `_log_transcript_snapshot`’s exact format strings and positional argument indexes. In the affected test sections, render each matching warning using the logger’s format string and remaining arguments (or reuse module-level format constants shared with the handler), then assert against the rendered payload while preserving the existing diagnostic-content checks.

In @tests/hooks/test_agent_events_coverage.py at line 564, Wrap the long string literal in the assertion near the gobby-skills tool call across adjacent string literals, preserving the exact resulting message while keeping each source line within Ruff’s 100-character limit.

In @tests/hooks/test_tool_handlers.py at line 594, Wrap all four long assertion string literals in the test cases around the affected assertions, including the build-coordinator and playwright variants, by splitting each literal across two adjacent string segments so Ruff’s 100-character limit is satisfied without changing the asserted text.

In @tests/mcp_proxy/services/test_codex_close_reconciliation.py around lines 23 - 24, Replace the private constant assertion in test_codex_close_reconciliation_default_allows_large_receipt_batches with a behavioral test of the reconciliation path, verifying that the configured timeout is passed to the awaited reconciliation call when processing a large receipt batch. Avoid asserting the literal value or directly inspecting _CODEX_RECONCILE_TIMEOUT_SECONDS.

In @tests/mcp_proxy/tools/sessions/test_compact_self.py around lines 426 - 429, Strengthen the assertions in the compact prompt test around captured_prompts[0] to verify the actual direct skill-loading instruction: require the get_skill tool reference and its guidance to call it directly. Keep the existing exclusions for list_mcp_servers, list_tools, and get_tool_schema, while replacing the vacuous “directly” substring assertion with checks that fail if the required-skill instruction is removed.

In @tests/workflows/test_active_progressive_discovery_guidance.py around lines 27 - 42, Update MANDATORY_ORDERED_CHAIN to recognize imperative inventory-first wording such as “First, call list_mcp_servers, then …” without requiring “discovery” or “chain” before the ordered calls. Preserve detection of existing progressive-discovery and mandatory-chain phrasing, and keep the ordered list_mcp_servers, list_tools, get_tool_schema, and call_tool sequence requirements intact.

In @tests/workflows/test_agent_definitions.py around lines 52 - 76, Add explicit -> None return annotations to the new test functions test_close_task_success_handlers_ignore_preview_calls and test_agent_success_handlers_do_not_fabricate_verification_evidence, preserving their existing test logic.

In @tests/workflows/test_context_handoff_rules.py around lines 157 - 171, Add type annotations to the newly added test functions, including fixture parameters such as db and manager and an explicit -> None return type. Apply the same typing consistently to the additional test function referenced by the comment, preserving their existing behavior.

In @tests/workflows/test_progressive_discovery_rules.py around lines 722 - 752, Mark the async test_context_loss_clears_only_schema_leases with pytest.mark.asyncio in addition to its existing pytest.mark.parametrize decorator, ensuring all parametrized cases execute under the required async pytest configuration.

In @tests/workflows/test_progressive_discovery_rules.py around lines 85 - 108, Add type hints to all newly introduced test functions, including fixture parameters and explicit None return annotations. Update test_sync_retires_legacy_gates_and_enables_renamed_gate and the other affected test definitions in this file, preserving their existing behavior.

In @tests/workflows/test_review_workflow.py around lines 55 - 68, Update the test function declaration for test_spawn_step_passes_only_valid_spawn_agent_parameters to include the required -> None return annotation, preserving its existing behavior and body.

In @.gobby/plans/feedback-lesson-loop.md around lines 304 - 377, Rename the plan review evidence migration from version 337 to an unused migration version that does not conflict with 337_verification_receipts_default.sql. Update all references to the migration filename and version in the plan documentation and related schema/migration metadata, while preserving the existing migration contents and baseline schema changes.

In @crates/gcode/src/commands/codewiki/build_parts/curated_content.rs around lines 722 - 746, Refactor the DiagramOutcome handling around the emitted branch into a single match that records the CuratedFlow pass exactly once for each outcome path. Keep the emitted outcome flowing into the existing block-processing logic, and preserve the non-emitted containment fallback with its “pass 3 containment fallback” label, without relying on recorded_slots deduplication.

In @crates/gcode/src/commands/codewiki/build_parts/modules.rs around lines 126 - 153, Precompute the loop-invariant component metadata and module-level dependency edge set once before the per-module rendering loop, then pass those results into render_module_dependency_mermaid and render_module_call_sequence. Update the renderers and their helpers so they reuse the precomputed indexes and only perform module/page-scoped filtering and bounding inside each iteration.

In @crates/gcode/src/commands/codewiki/diagram_compose.rs around lines 61 - 74, Replace the `DiagramKind::LABELS` lookup table and `label()` search with an exhaustive `match` on `self`, returning the corresponding stable label for `ModuleDependency`, `ModuleCallSequence`, and `CuratedFlow`. Mirror the pattern used by `DiagramOutcome::label` so new enum variants require compiler-checked handling and no runtime `expect` remains.

In @crates/gcode/src/commands/codewiki/generation.rs around lines 336 - 337, Update the diagram statistics flow around the early return in the scoped generation path and the unconditional run.rs sink.set_diagram_stats call so scoped or reused runs cannot overwrite whole-vault telemetry with partial results. Preserve existing stats by merging with the previous metadata, or record an explicit scope/partial marker that prevents consumers from treating the result as full-vault statistics.

In @crates/gcode/src/commands/codewiki/mod.rs around lines 231 - 238, Move the comment “Rendered markdown and graph-derived narrative analysis.” from above the compare re-exports to directly above the render re-export group beginning with pub(crate) use render, leaving the compare exports unchanged.

In @crates/gcode/src/commands/codewiki/render/diagrams.rs around lines 164 - 187, Optimize the cyclic-page fallback by building a single adjacency map from all_edges in render_module_call_sequence, then update directed_call_distances to traverse only each dequeued node’s outgoing neighbors instead of rescanning all edges. Pass this index through both the root_seeds traversal and the in_page_components fallback while preserving the existing depth limit and SparseEvidence behavior.

In @crates/gcode/src/commands/codewiki/render/diagrams.rs around lines 297 - 299, Update the caption string appended in the diagram rendering flow to remove the stray ellipsis after “call depth,” while preserving the surrounding wording and punctuation.

In @crates/gcode/src/commands/codewiki/render/diagrams.rs around lines 475 - 490, Update mermaid_label to retain the existing "repo" fallback for empty modules, but delegate non-empty module escaping to gobby_core::vault::mermaid::escape_label instead of maintaining the local replacement chain.

In @crates/gcode/src/commands/codewiki/run.rs around lines 592 - 620, Update capture_commit_stamp to determine dirty state using only tracked modifications, replacing the current git status --porcelain invocation with an appropriate tracked-files status check. Preserve the existing failure handling and CommitStamp construction, while ensuring untracked files do not set dirty and are not scanned.

In @crates/gcode/src/commands/codewiki/run.rs around lines 436 - 467, Harden capture_commit_stamp_detects_dirty_worktrees_and_non_git_roots against ambient Git configuration by pinning the repository to SHA-1 object format and disabling commit signing in the relevant git invocations. Replace the fixed clean.sha length assertion with a shape assertion that accepts the configured SHA-1 format without relying on global settings.

In @crates/gcode/src/commands/codewiki/tests/incremental.rs around lines 834 - 1102, Add a test alongside compare_to_distinguishes_bad_ref_and_invalid_baseline_metadata that invokes compare_to with an out value containing path traversal, such as "../escape", and asserts it fails with "requires --out to be inside the source repository". Reuse the existing committed_compare_repo setup and metadata fixtures.

In @crates/gcode/src/commands/codewiki/tests/reuse.rs around lines 2351 - 2357, Extend the normalization refresh test around third.persist and third.finish to assert that the persisted metadata entry meta.docs[doc.path] still contains the original commit and generated timestamp. Keep the existing byte-for-byte page assertion, and compare against the original metadata captured before the refresh rather than the third sink’s values.

In @crates/gwiki/src/health.rs around lines 909 - 953, Refactor duplicate_concepts to bucket pages by normalized exact-title and shared-key values, emitting one grouped DuplicateConcept per duplicate group with all matching paths instead of pairwise entries. Retain distinct_pairs filtering when forming groups, then restrict title_prefix detection to sorted-title adjacent candidates rather than scanning every concept pair, preserving the existing reason and title formatting semantics.

In @crates/gwiki/src/links.rs around lines 134 - 166, Extend concept_worthiness_accepts_technical_terms_and_rejects_artifacts with a path-shaped key case, asserting is_concept_worthy returns false and concept_rejection_reason returns Some("path_shaped"). Use a representative path-like value that exercises the path_shaped branch in the existing rejection logic.

In @crates/gwiki/src/upkeep.rs around lines 1071 - 1075, Update run_with_clock and find_unworthy_concepts to reuse the already-collected page list instead of calling lint::collect_pages(vault_root) again. Pass the existing pages (preferably as a slice) into find_unworthy_concepts and iterate them while preserving the current filtering and archive behavior.

In @docs/reviews/agents.md around lines 207 - 208, Add a blank line immediately after the heading about subscribe_agent_completion re-registering unconditionally, before the “Where” list item, to satisfy markdownlint MD022.

In @src/gobby/agents/agent_cleanup.py around lines 85 - 98, Update submit_terminal_delivery_offload so the _terminal_delivery_submit-unset path dispatches callback asynchronously through a small dedicated executor instead of executing it inline on the calling thread. Preserve the Future[T] return contract and propagate callback results and BaseException failures through that future, while reusing an existing executor if one is available.

In @src/gobby/agents/agent_cleanup.py around lines 184 - 220, Update the completion notification flow around completion_registry.notify and cleanup so completion_registry.cleanup(run_id) is not called when notify raises. Track whether notification succeeded and only evict registry state after a successful notification, while preserving subscriber cleanup and return behavior.

In @src/gobby/agents/capture.py around lines 344 - 354, Update _async_storage_call to accept an explicit_run_id parameter and pass that value to shielded_terminal_delivery instead of deriving run_id by scanning args; update every call site, including the _persist_capture_sync capture-persist path, to provide the appropriate run id. Preserve the existing operation callback behavior, and handle the None result from shielded_terminal_delivery explicitly rather than hiding it with cast(ResultT, ...), so closed admission is not treated as a normal storage result.

In @src/gobby/agents/completion_subscribers.py around lines 87 - 121, The strict and non-strict branches duplicate persistence and registry logic. Extract the shared CompletionSubscriberManager construction and add_completion_subscribers call into a local_persist(db) helper, then keep strict ordering as persist before register and non-strict ordering as register before best-effort persist, preserving their existing error handling and inserted_session_ids behavior.

In @src/gobby/agents/resume_executor.py around lines 54 - 55, Replace the broad Any annotations for completion_registry and _RunStorage.db with CompletionEventRegistry and HubDatabase, respectively, including the corresponding optional type where applicable. Import these concrete contracts under TYPE_CHECKING to avoid runtime circular imports, and update the affected terminal-delivery signatures such as _deliver_existing_terminal_run_unshielded consistently.

In @src/gobby/agents/run_completion.py around lines 50 - 54, Update the result payload construction in the run completion flow to copy any notify_result before normalization, always preserve or inject the current run_id, and include error only when it has a non-None value. Remove the fallback’s unconditional error field so delivered payloads have the same normalized shape whether notify_result is supplied or absent.

In @src/gobby/deployment.py around lines 11 - 21, Memoize the no-argument deployment token in deployment_token, while continuing to compute and resolve explicitly supplied data_root values normally. Preserve the existing stable hash output and ensure deployment_advisory_key still uses the cached default token when token is omitted.

In @src/gobby/dispatch/spawn_actions.py around lines 11 - 15, Expose a public alias for_deliver_existing_terminal_run_unshielded in agent_cleanup, such as deliver_existing_terminal_run_in_scope, and update the cross-module imports and call sites in spawn_actions.py, agents_lifecycle_tools.py, agent_cancellation.py, and resume_executor.py to use it. Keep the underscored helper internal and preserve its existing behavior.

In @src/gobby/hooks/session_coordinator.py around lines 359 - 382, The deferred terminalization flow around submit_terminal_delivery_offload must not skip completion follow-ups when future.result(timeout=5) times out. Ensure _notify_agent_completion() and release_session_worktrees() execute once_terminate_agent_run_inline persistence finishes by moving them into the offloaded operation or attaching a done callback, while preserving single execution and the existing unavailable-executor handling.

In @src/gobby/hooks/tool_outcomes.py around lines 329 - 344, Update the output-field selection in the outcome parsing flow to skip aliases whose values are None, so a populated later alias such as tool_result or tool_response is selected instead of being masked by tool_output. Preserve the existing _collect_output_signals call and trust handling once a non-null output field is found.

In @src/gobby/install/bundled_content_manifest.json at line 73, Remove the stale manifest entries for rules/build/build-agent-safety.yaml and skills/build/SKILL.md from the bundled-content manifest, since those files no longer exist under the shared install content. Keep existing entries, including skills/build-coordinator/SKILL.md, unchanged.

In @src/gobby/install/shared/skills/build-coordinator/SKILL.md around lines 109 - 112, Update the coordinator instructions around gobby-agents:wait_for_agent to collect every active run_id and subscribe to each one before ending the turn, rather than choosing a single run. On daemon wake, process the woken run ID first, then perform the full status and health sweep for all remaining runs.

In @src/gobby/install/shared/skills/goal/SKILL.md around lines 209 - 212, Update both worker-wait procedures to subscribe to every outstanding run_id instead of only one. Retain the complete batch of run IDs across idle/wake cycles, re-call wait_for_agent for each run to collect terminal snapshots, and perform the full status and health sweep only after all subscribed runs have been checked.

In @src/gobby/install/shared/skills/review/SKILL.md around lines 41 - 51, Update the “Boundaries” guidance in SKILL.md to scope its prohibition on approving, rejecting, or escalating to spawned mode only, or explicitly exempt the in-line epic-reviewer persona. Ensure the in-line mode instructions can execute the required “## Epic Findings” verdict and create remediation tasks for blocking findings.

In @src/gobby/install/shared/workflows/agents/epic-reviewer.yaml around lines 40 - 46, Update the epic review state machine around the initial claim transition to detect an already-closed epic and route directly to the CLOSED-EPIC REVIEW flow without requiring task_claimed. Modify the review-step action permissions/transitions so gobby-tasks:reopen_task is allowed for this closed-epic path, preserving the documented post-hoc review, remediation, and end_agent_run behavior.

In @src/gobby/install/shared/workflows/agents/goal-taskmaster.yaml around lines 82 - 87, Update the taskmaster workflow’s worker-wait instructions to persist every active worker run ID rather than subscribing to only one run_id. Call gobby-agents:wait_for_agent for each outstanding subscription after initiating them, and after each daemon wake sweep all subscribed workers before proceeding with status and health checks.

In @src/gobby/mcp_proxy/tools/agents_lifecycle_tools.py around lines 254 - 304, After the run ID None check, bind it to a local `resolved_run_id` so the nested `kill_and_deliver` closure retains the narrowed `str` type. Replace the relevant `run_id` references in `terminalize_killed_agent_run`, `_deliver_existing_terminal_run_unshielded`, and `shielded_terminal_delivery` with `resolved_run_id`, while preserving the existing behavior.

In @src/gobby/mcp_proxy/tools/agents_query_tools.py around lines 121 - 221, Add a regression guard for the no-await critical region in wait_for_agent, covering the span from the second ctx.runner.get_run through conditional cleanup. Assert that subscribe_agent_completion, remove_agent_completion_subscribers, and completion_registry.cleanup remain synchronous and no await is introduced before the critical-region marker.

In @src/gobby/mcp_proxy/tools/spawn_agent/_failure_cleanup.py around lines 34 - 48, The synchronous database operations inside _fail_run must be offloaded from the event loop. Update _fail_run and its call site in the failure-cleanup flow to use the existing run_terminal_delivery_offload seam for run_storage.fail and run_storage.get, preserving the current failure and child-session handling behavior.

In @src/gobby/mcp_proxy/tools/spawn_agent/_health.py around lines 117 - 136, Widen the exception handling around deliver_existing_terminal_run in the immediate-spawn failure path so subscriber delivery exceptions are caught and logged instead of escaping the health-check task. Preserve the existing run failure flow and psycopg-specific handling, while ensuring unexpected delivery errors produce a diagnostic logger warning.

In @src/gobby/runner.py around lines 210 - 220, Remove the single-use ConstructionRollbackLedger from the runner initialization flow and replace it with direct exception handling around init_storage_and_config, init_services, init_orchestration, and init_servers: call rollback_runner_resources(self) in the BaseException handler, then re-raise the original exception. Preserve the existing initialization order and rollback behavior.

In @src/gobby/runner_gate.py around lines 196 - 206, Update_settle_reap_under_cancellation to capture non-cancellation exceptions from awaiting the shielded_kill_and_reap task, then re-raise the recorded cancellation when present instead of letting the reap failure replace it. Preserve propagation of the reap exception when no cancellation was observed.

In @src/gobby/runner_gate.py around lines 240 - 248, Increase the parent watchdog timeout used by asyncio.wait_for around process.communicate in the runner gate so it exceeds the child’s budget_seconds by a small grace margin. Keep the child’s budget unchanged, allowing_diagnose_gate_wait and its RunnerGateError details to complete before the parent raises its watchdog timeout.

In @src/gobby/runner_lifecycle_agents.py around lines 107 - 152, Update the terminal replay startup flow to iterate over distinct run IDs from the completion-subscriber table rather than paging every status in TERMINAL_AGENT_RUN_STATUSES. Fetch subscribers and their associated runs only for those IDs, skip missing or non-terminal runs, then redeliver and remove acknowledged subscribers using the existing wake and cleanup logic.

In @src/gobby/runner_lifecycle_shutdown.py around lines 597 - 598, Replace the ad-hoc db_executor._gobby_shutdown_joined state in the shutdown flow with an explicit DatabaseExecutor join/is_joined contract or a module-level WeakSet of joined executors. Ensure repeated shutdown calls cannot spawn duplicate join threads, and document whether post-timeout calls intentionally retry shutdown or are treated as already joined.

In @src/gobby/runner_lifecycle_subsystems.py around lines 777 - 782, Capture the return value from recover_agent_completion_subscribers in the post-startup initialization flow, then log the number of rehydrated/redelivered subscriptions using the runner’s existing logging facilities. Preserve the current invocation and await behavior while making the recovery count observable.

In @src/gobby/runner_pid_file.py around lines 61 - 68, Add a no-op release() method to FailOpenPidOwnership so it conforms to the same ownership protocol as PidFileClaim. Then simplify runner.main() and run_daemon’s cleanup_owned_pid_file to call release() directly without isinstance-based branching, preserving existing cleanup behavior.

In @src/gobby/runner_rollback.py around lines 31 - 36, Update the rollback loop in the callback cleanup method to log failures while re-raising non-Exception control-flow signals such as KeyboardInterrupt and CancelledError; continue processing callbacks only for ordinary Exception failures, then preserve the existing callback clearing behavior.

In @src/gobby/runner_rollback.py around lines 39 - 65, Replace the cross-loop execution in_settle_async_close with loop-aware settlement: require rollback compensations to be synchronous when no async scheduling is available, and when a loop is already running, schedule the coroutine on that original loop via run_coroutine_threadsafe or create_task rather than asyncio.run in a helper thread. Ensure timeout handling cancels and fully settles the scheduled coroutine so no daemon thread or unclosed loop remains.

In @src/gobby/servers/websocket/handlers/session_observe_continue.py around lines 61 - 79, Update the kill_and_deliver/shielded_terminal_delivery flow so killed is set only when terminal delivery admission succeeds and kill_agent actually runs. Have kill_and_deliver return a truthy success sentinel, inspect shielded_terminal_delivery’s result, and preserve the existing failure behavior when it returns None so_release_source_session cannot report success or resume while the agent remains alive.

In @src/gobby/storage/agents/_lifecycle.py around lines 164 - 167, Remove the duplicated unreachable `return self.get(run_id)` in the lifecycle method, keeping the single return after the `_positive_rowcount(cursor)` guard unchanged.

In @src/gobby/storage/agents/_lifecycle.py around lines 41 - 50, Update the terminal-transition flow around bounded_transaction and host.get so the final AgentRun read uses the already-registered ambient transaction instead of opening a separate fetchone transaction. Preserve the session expiry update and return the post-update row reflecting the terminal transition.

In @src/gobby/storage/executor.py around lines 116 - 118, Update Executor.join to set the internal _shutdown flag to True before or alongside shutting down the underlying executor, so calling join() alone closes admission and makes stats().shutdown report the correct state. Preserve the existing blocking shutdown behavior and cancel_futures=False option.

In @src/gobby/storage/hub/postgres.py around lines 266 - 273, Update bounded_transaction to handle nested use safely by capturing the ambient transaction’s existing statement_timeout and lock_timeout before issuing SET LOCAL, then restoring both values on exit; preserve the current bounds for outer transactions and ensure restoration occurs even when the nested block raises.

In @src/gobby/storage/hub/postgres.py around lines 274 - 279, Replace the interpolated SET LOCAL statements in the transaction context with parameterized set_config calls using the validated timeout values and local scope enabled. Preserve the existing positive-value validation and transaction behavior in the surrounding transaction method.

In @src/gobby/storage/hub/postgres_pool.py around lines 67 - 75, The retry path in the pooled connection acquisition flow should not immediately call pool.connection() again after PoolTimeout without recovery. Update the PoolTimeout handling around pool.connection() to perform pool repair via pool.check() or an equivalent out-of-band mechanism, then retry with bounded backoff while preserving the existing single-retry limit and timeout behavior.

In @src/gobby/storage/pipeline_subscribers.py around lines 44 - 56, Add the existing `# nosec B608` suppression annotation to the `self.db.execute` query in the subscriber insertion flow, mirroring the sibling query’s annotation while leaving the generated placeholders and bound parameters unchanged.

In @src/gobby/telemetry/providers.py around lines 95 - 104, Update get_tracer_provider and get_meter_provider to retain references to locally constructed providers or their lifecycle-owned BatchSpanProcessors and metric readers, then update shutdown_providers to flush and shut down those resources before clearing them. Leave reused interpreter-global providers untouched, while ensuring locally created exporters’ background threads terminate and buffered spans/metrics are delivered.

In @tests/agents/test_agent_cleanup.py around lines 22 - 65, Add an autouse fixture in the terminal-delivery tests that resets delivery state before each test and ensures terminal-delivery admission is open, using reset_terminal_delivery_offload() and the existing admission-control API. Keep the fixture cleanup robust so failures cannot leave_terminal_delivery_admission_open or_in_flight_terminal_deliveries affecting later tests.

In @tests/events/test_wake_wiring.py around lines 244 - 302, The test test_wait_for_agent_registration_wakes_on_completion should invoke wait_for_agent through the registry’s public tool-dispatch or call API instead of accessing registry._tools directly. Pass run_id through that public path so argument validation is exercised while preserving the existing assertions and completion notification flow.

In @tests/mcp_proxy/tools/sessions/test_compact_self.py around lines 716 - 720, Annotate the temp_db and sample_project parameters in test_delayed_archival_refresh_preserves_resumed_session_claim with the appropriate fixture types, preserving the existing return annotation and test behavior.

In @tests/mcp_proxy/tools/sessions/test_compact_self.py around lines 731 - 741, Update the setup query executed by temp_db in the test to use Hub’s numbered $N placeholders instead of %s, matching the parameter positions for digest_markdown, summary_markdown, and session_id while preserving the existing update behavior.

In @tests/mcp_proxy/tools/spawn_agent/test_dedup.py around lines 163 - 174, The parametrization for test_allow_closed_task_permits_review_spawn_unless_escalated lacks the closed-and-escalated scenario named by the test invariant. Add a third case with both closed_at and escalated_at set, expecting no spawn, while preserving the existing open-escalated and closed-reviewable cases.

In @tests/servers/websocket/test_resume_blocked.py around lines 50 - 51, Define module-level constants for the run and source-session UUIDs, then replace every duplicated literal in the helper, the three tests, and the parametrize list with those constants. Keep the existing values and test behavior unchanged.

In @tests/skills/test_plan_skill_delegated_mode.py around lines 63 - 80, Add the appropriate project test marker to test_spawned_run_waiting_policy_is_shared_and_wake_driven, using the existing unit/slow/integration/e2e marker convention so the test supports reliable targeted execution.

In @tests/skills/test_removed_wait_tool_guidance.py around lines 12 - 17, Expand WAKE_DRIVEN_GUIDANCE with pytest parameters for the merge-expert and review skill files, plus the epic-reviewer.yaml, merge-orchestrator.yaml, and review.yaml workflow artifacts. Use the existing SKILLS_DIR and WORKFLOWS_DIR path conventions and descriptive IDs so all changed wake-driven guidance is covered.

In @tests/storage/hub/test_postgres_baseline_application.py around lines 488 - 495, Add a parametrized test for PostgresHubDatabase.bounded_transaction that passes zero or negative statement_timeout_ms/lock_timeout_ms and asserts ValueError is raised before entering the transaction body. Keep the existing valid-bounds test unchanged.

In @tests/storage/tasks/test_sweep_stale_claims.py around lines 70 - 75, Annotate the pytest fixtures in all affected test functions, including test_sweep_reclaims_task_claimed_by_terminal_session and the functions around the other cited locations. Add the appropriate existing types for temp_db and sample_project while preserving the current test behavior and parametrization.

In @tests/storage/tasks/test_sweep_stale_claims.py at line 95, Update the direct database operation in the test to use the Hub transaction convention: replace the `%s` placeholder in the sessions DELETE query with the numbered `$1` placeholder while preserving the existing session_id parameter binding.

In @tests/telemetry/test_providers.py around lines 28 - 33, Update the teardown logic around shutdown_providers to track which tracer and meter providers this fixture constructed locally, and call shutdown only for those instances. Do not shut down reused interpreter-global providers; still clear the provider references after teardown.

In @tests/test_runner_gate.py around lines 74 - 79, Update the assertion around the runner gate connection test to compare application_name against the explicit contracted gate value, not connect.call_args.kwargs["application_name"]. Use a value distinct from the successor’s name so the test verifies the gate is not terminated by its own fence, while preserving the other expected connection arguments.

In @crates/gcode/src/commands/codewiki/compare.rs around lines 169 - 188, Update normalize_explicit_git_meta to normalize backslash separators before iterating path components and performing traversal validation, so "..\\_meta/codewiki.json" is rejected rather than converted after validation. Extend the invalid-path tests for normalize_explicit_git_meta to cover this Windows-style traversal input.

In @crates/gcode/src/config/context.rs around lines 314 - 333, Extract the duplicated falkordb, qdrant, embedding, indexing, and code-vector resolution logic into a shared resolve_services(&mut conn, &layers, services, quiet) helper returning the existing tuple. Replace both resolver blocks with calls to this helper, preserving each service’s conditional behavior and error propagation.

In @crates/gcode/src/config/layers.rs around lines 139 - 172, The test daemon_service_source_orders_env_served_and_routing must also verify the routing fallback tier. Add a ServiceSource::daemon case with a served map that omits databases.qdrant.url and the existing routing config, then assert config_value returns <http://routing.example:6333>.

In @crates/gcode/src/config/services.rs around lines 380 - 385, Remove the unused _quiet parameter from resolve_falkordb_config, resolve_qdrant_config, and resolve_embedding_config, then update every caller in context.rs and elsewhere to stop passing quiet. Preserve each resolver’s existing behavior and avoid adding compatibility shims.

In @crates/gcode/src/vector/code_symbols/embedding.rs around lines 148 - 174, Consolidate the duplicated AI source resolution failure handling in resolve_embedding_ai_context: extract or reuse a small helper for logging “failed to resolve effective AI config” and returning None, then apply it to both ai_source_for_conn and ai_source_without_primary error branches while preserving their existing success paths.

In @crates/gcode/tests/effective_config.rs around lines 58 - 84, Update spawn_effective_config_server to apply a finite read timeout to the accepted TcpStream before the request-reading loop, so stalled or malformed requests fail promptly instead of hanging CI. Preserve the existing request parsing and response behavior for valid requests.

In @crates/gcore/src/ai/effective_config.rs around lines 86 - 114, Extract the duplicated local-token bearer-header setup into a helper such as local_token::apply_bearer_header(request), reusing read_local_cli_token, AUTHORIZATION_HEADER, and authorization_bearer. Update the effective-config request flow and the corresponding request construction in probe.rs to call this helper, preserving the behavior of leaving requests unchanged when no local token is available.

In @src/gobby/dispatch/prompts.py around lines 190 - 207, Update the prompt construction around the plan-review snapshot to prevent snapshot_text from breaking the evidence framing. Derive nonce-suffixed opening and closing delimiters from plan_hash, use them consistently in the generated prompt, and ensure the snapshot content cannot reproduce those delimiters; preserve the existing metadata and structured-verdict guidance.

In @src/gobby/dispatch/spawn.py around lines 293 - 300, Wrap the synchronous _prepare_plan_adversary_evidence call in spawn_agent with asyncio.to_thread, preserving its existing arguments and tuple assignment. Ensure the blocking prepare_plan_review_round and snapshot_bytes operations execute off the event loop while the returned prompt, evidence_service, and evidence_id values remain unchanged.

In @src/gobby/hooks/_normalization_shell.py around lines 143 - 145, Update the normalization flow around the operator newline check and _skip_heredoc_bodies so heredoc bodies are skipped only after the complete logical command terminates. Track pending shell continuations from &&, ||, |, and line continuations across newlines, and do not treat those intermediate newlines as heredoc starts; preserve heredoc skipping at the terminating newline so commands such as redirected printf remain visible to normalization.

In @src/gobby/install/bundled_content_manifest.json at line 18, Synchronize the bundled content manifest with the files present under the shared bundle by removing the stale entries for rules/build/build-agent-safety.yaml and skills/build/SKILL.md, or restore those files if they are still required. Update the manifest generation or source represented by the visible manifest entry so install-time paths match the bundled tree.

In @src/gobby/install/shared/skills/python/references/testing.md around lines 156 - 158, Update the validation guidance near the test-quality audit instructions to document the uv-backed command using “uv run gobby test-types audit <paths> --baseline .gobby/test-types-baseline.json --fail-on-new” instead of bare gobby, while preserving the requirement to run the audit for changed test files.

In @src/gobby/install/shared/workflows/agents/epic-reviewer.yaml around lines 39 - 42, Update the epic-reviewer workflow’s load phase to include review-learning in required_skills and fetch it in the get_skill sequence before the lesson workflow is required. Preserve the existing epic-review finding/confirmation behavior and use the established review-learning skill identifier consistently.

In @src/gobby/install/shared/workflows/agents/plan-adversary-taskless.yaml around lines 233 - 249, Update test_lane_success_hook_records_the_canonical_marker in tests/agents/test_plan_review_researcher_definition.py to parameterize the workflow definitions, covering both plan-adversary.yaml and plan-adversary-taskless.yaml. Assert the same canonical lane-capture behavior for each file so divergent edits to either duplicated on_mcp_success expression are detected.

In @src/gobby/install/shared/workflows/agents/plan-adversary.yaml around lines 122 - 130, Restore the wording in the planner-side validation instruction so it contains the existing test’s expected phrase “Do NOT re-run the parser pre-verdict,” while preserving the instruction not to repeat validation. Update the relevant text near the `validate_plan_file` and `approve_review` guidance; do not modify the test unless intentionally standardizing terminology across the related documentation.

In @src/gobby/install/shared/workflows/rules/review-learning/inject-plan-enhancer-lessons.yaml around lines 1 - 20, Update the inject-plan-enhancer-lessons rule to match the canonical schema: retain the required metadata, add the appropriate when condition, and replace the effects wrapper with the required singular effect containing type: mcp_call and its existing call configuration. Add a focused schema/load test covering this rule’s required fields and effect.type.

In @src/gobby/install/shared/workflows/rules/review-learning/inject-plan-reviewer-lessons.yaml around lines 1 - 20, The inject-plan-reviewer-lessons rule uses the wrong schema: add the required when condition and replace effects with the canonical singular effect structure containing effect.type, preserving the existing MCP call configuration. Ensure the definition includes the required rule-name and all canonical fields, and add a focused schema/load test verifying the rule loads and exposes effect.type for plan adversaries.

In @src/gobby/install/shared/workflows/rules/review-learning/inject-planner-lessons.yaml around lines 1 - 20, Update the inject-planner-lessons rule to match the canonical rule-definition schema: add the required rule-level when condition, replace effects with the singular effect field, and preserve the MCP call under effect.type with its existing configuration. Add a focused schema/load test covering this rule’s required tags, rule name, description, event, enabled, priority, when, and effect.type fields.

In @src/gobby/install/shared/workflows/rules/review-learning/inject-qa-reviewer-lessons.yaml around lines 1 - 20, The inject-qa-reviewer-lessons rule must match the canonical rule-definition schema: add the required when condition, replace the effects collection with the singular effect field while preserving the MCP call configuration under effect.type, and retain the existing metadata and scope. Add a focused schema/load test covering this rule’s required fields and successful loading.

In @src/gobby/mcp_proxy/tools/plans/review_evidence.py around lines 66 - 81, Update get_plan_review_snapshot so the non-bytes snapshot validation returns_error_payload directly instead of raising ReviewEvidenceError inside the try block. Keep the existing error code and message, and preserve exception handling for service errors and UnicodeDecodeError.

In @src/gobby/mcp_proxy/tools/plans/review_evidence.py around lines 151 - 156, Update verify_plan_unchanged, bind_evidence_run, expire_plan_review_evidence, and finalize_plan_review_evidence to catch their expected OSError and psycopg.Error failures and return the existing _error_payload structured response, while preserving the current ReviewEvidenceError handling and success payloads.

In @src/gobby/mcp_proxy/tools/tasks/_lifecycle_validation.py around lines 409 - 435, Update_recall_validation_lessons to bound recall_review_lessons_by_class with the established async timeout mechanism, and catch all ordinary recall/provider failures so advisory enrichment cannot abort close-task validation. Preserve cancellation propagation where required, and return an empty message plus the existing lesson-recall-failed diagnostic containing the exception detail for handled failures.

In @src/gobby/mcp_proxy/tools/tasks/_plan_review_approval.py around lines 28 - 31, Move the PlanReviewEvidenceService construction and get_evidence call inside the replay branch in the approval flow. Keep plan_review_mint_result(evidence) unchanged for replay requests, while allowing non-replay approvals with a recorder to skip the unnecessary database fetch.

In @src/gobby/mcp_proxy/tools/tasks/_plan_review_approval.py around lines 62 - 72, Update _checkpoint_failure to guard the checkpoint_plan_review_lesson_mint call against ReviewEvidenceError and psycopg.Error. If recording the failure checkpoint raises, return a degraded plan_review_mint_result-compatible result instead of propagating the exception, preserving the fail-open behavior of complete_plan_review_mint after approval commits.

In @src/gobby/mcp_proxy/tools/tasks/_plan_review_backfill.py around lines 19 - 40, Update backfill_plan_review_lessons to catch errors from resolve_task_id_for_mcp before mint_plan_review_lessons runs, converting unknown task IDs and invalid formats into the same structured error dict used by the existing ReviewEvidenceError handling. Preserve the current unavailable-service response and successful lesson backfill flow.

In @src/gobby/mcp_proxy/tools/tasks/_stage_review.py around lines 372 - 388, Move the planning-stage evidence_id validation before ctx.task_manager.approve_review(...) mutates state, and replace the RuntimeError in the planning branch with the handler’s existing structured error response format. Keep complete_plan_review_mint execution unchanged for valid planning approvals, and update the schema validation around the evidence_id definition if needed to preserve the planning-specific requirement.

In @src/gobby/plans/manifest_emitter.py around lines 139 - 168, In the manifest synthesis logic around the category routing branch, remove tdd from the earlier tuple assignment so entry["tdd"] is assigned only by the final decision.get expression. Validate non-null assigned_agent and implementation_domain override values as strings before storing them in entry, rejecting invalid types consistently with the existing ManifestSynthesisError handling.

In @src/gobby/plans/manifest_emitter.py around lines 127 - 129, Validate reviewed routing overrides before copying them in the manifest emission flow around the loop over task_type, depends_on, and tdd: normalize depends_on through the same dependency validation used by _synthesized_dependencies, rejecting unknown, empty, self-referential, and non-list values while preserving valid existing behavior. Also validate task_type against its expected type before assigning it, and only write validated override fields into the entry.

In @src/gobby/plans/review_coverage.py around lines 516 - 530, Update the exception handling around path resolution in the review evidence validation flow to catch FileNotFoundError separately as retryable source_drift, while reporting other OSError cases such as permission or directory errors as non-retryable evidence failures with appropriate IO-error details. Remove the redundant FileNotFoundError entry from the OSError tuple and preserve the existing invalid_source_path handling for ValueError.

In @src/gobby/plans/review_coverage.py around lines 569 - 576, Update_required_string to accept an explicit error-code argument in addition to the human-readable owner, and use that argument when constructing ReviewEvidenceError. Update every call site to pass the fixed codes already used by this module, including invalid_lane_results, invalid_candidate, and invalid_dispositions, while retaining owner for the validation message.

In @src/gobby/plans/review_coverage.py around lines 41 - 55, Define each review-complexity threshold once and reuse those named values in both the complex_review decision and the returned thresholds payload. Update the surrounding review coverage logic without changing the existing threshold values or routing behavior.

In @src/gobby/plans/review_evidence.py around lines 661 - 708, Keep the per-plan mutation lock from before reading and verifying current_bytes through rendering, writing, and complete_manifest_apply, using the existing transaction_immediate(mutation) flow in the manifest-apply method. Move the verify/render/atomic_write_bytes sequence and completion update into the same locked transaction, while preserving the existing pending, revoked, applied, and payload-conflict checks.

In @src/gobby/plans/review_evidence.py around lines 551 - 563, Update the interactive evidence validation around parse_checkpoints and render_v1_round_checkpoint to read plan_path.read_bytes() once, store the bytes, and reuse them for both checkpoint parsing and expected-checkpoint validation; preserve the existing missing_v1_checkpoint error behavior.

In @src/gobby/plans/review_evidence_io.py around lines 319 - 337, The _section_span function currently selects the first matching heading when duplicate manifest keys exist. Track matches for wanted_key while scanning headings, reject the section with the existing ReviewEvidenceError mechanism if more than one match is found, and only return a span when exactly one matching heading exists.

In @src/gobby/plans/review_evidence_io.py around lines 243 - 268, Refactor render_manifest_plan so the _section_span(text, "M1") lookup and missing-section fallback are handled explicitly before validating the suffix. Keep the invalid_manifest error for non-final M1 sections, but remove the broad try/except around that validation so it cannot be re-raised conditionally by error code; preserve the existing body, suffix, rendering, and parsing behavior.

In @src/gobby/plans/review_evidence_io.py around lines 291 - 316, Update _parse_rendered_plan to create its temporary plan file inside a TemporaryDirectory, following the existing_snapshot_document pattern, instead of using NamedTemporaryFile in plan_path.parent. Keep the parse_plan calls and exception behavior unchanged, and remove the manual temp_path cleanup since the temporary directory should manage lifecycle cleanup.

In @src/gobby/plans/review_findings.py around lines 84 - 114, Update_validate_finding to reject adversary-supplied description, fix, and prevention values containing Markdown code-fence markers or leading “#” before they are rendered by the findings formatter. Preserve the existing non-empty validation and use the established_invalid validation error path.

In @src/gobby/review_learning/class_recall.py around lines 154 - 165, Update recall_review_lessons_by_class to validate limit before converting it, catching None, non-numeric, and otherwise invalid values and raising a clear ValueError consistent with the existing argument validation. Preserve the bounded range of 1 through 5 for valid numeric limits.

In @src/gobby/review_learning/lessons.py around lines 213 - 236, Update recorders._lesson_finding to canonicalize the plan-review category with slugify(category) when constructing pattern_id, matching _validate_class_scoped_identity’s expected format; preserve the existing lesson_type and check_key components.

In @src/gobby/review_learning/recorders.py around lines 39 - 60, Update mint_plan_review_lessons to run the synchronous database operations store.list_for_task_stage, get_task, and review_learning_service.checkpoint_plan_review_lesson_mint via asyncio.to_thread, following the existing class_recall.py pattern, while preserving their current arguments and result handling.

In @src/gobby/review_learning/round_diff.py around lines 79 - 93, Replace the identity-based deduplication in the selection flow with index-based tracking derived from the positions of candidates chosen from ranked. Use those indices when extending selected so candidates are excluded by position rather than id(candidate), while preserving the existing ordering and capped_limit behavior.

In @src/gobby/review_learning/round_diff.py around lines 121 - 136, The _validated_findings function currently suppresses all malformed round-result failures without any diagnostic signal. Add a debug or warning log for invalid payloads, non-list findings, non-mapping entries, and validation exceptions, including row.evidence_id and the relevant exception; preserve the existing None return behavior after logging.

In @src/gobby/servers/routes/configuration_effective.py around lines 130 - 148, The get_effective_config docstring claims a transport constraint that the endpoint does not enforce. Remove the unsupported “non-loopback bind hosts require trusted transport” claim from the docstring, keeping it focused on serving the resolved client configuration.

In @src/gobby/servers/routes/configuration_effective.py around lines 23 - 28, Update the_is_served_key predicate and _EXCLUDED_KEYS definition so the .routing exclusion is handled by a single mechanism; remove the redundant ai.routing entry from _EXCLUDED_KEYS while preserving exclusion of every key ending in .routing.

In @src/gobby/storage/migrations/339_expired_plan_review_round_retry.sql around lines 1 - 11, Remove migration 339 because its index definitions duplicate the final predicates already introduced by migration 338. Keep the expired_at IS NULL predicates in 338 and do not add compatibility handling or replacement drop/create operations; only retain 339 if the project requires supporting environments where 338 was already applied, documenting that reason in its header.

In @src/gobby/storage/tasks/_transitions.py around lines 572 - 601, Move the apply_plan_review_manifest call into the db.transaction_immediate(StageReviewApprovalMutation(...)) block so it executes under the task’s serialization lock before authorize_current_attempt and replay checks. Preserve the existing arguments and manifest validation, ensuring concurrent approvals cannot both apply the same plan review manifest.

In @src/gobby/storage/tasks/_transitions.py around lines 576 - 577, Remove the `or ""` fallback from the `run_id` argument in the manifest apply call, passing `dispatch_run_id` directly. Preserve the existing `authorize_current_attempt` validation and let its non-None contract enforce the required run ID.

In @src/gobby/storage/tasks/_transitions.py around lines 864 - 877, Reuse the existing_replace_round_section helper in reject_review instead of maintaining a separate inline round-heading replacement, passing the current description, round_number, and replacement section so both paths remain consistent. Leave the escaped integer-derived regex construction in _replace_round_section unchanged.

In @src/gobby/storage/tasks/_transitions_facade.py around lines 301 - 306, Update the affected transition method signature so round_number, findings, manifest_entries, routing_decisions, coverage_attestation, and evidence_id are declared after the * marker as keyword-only parameters. Preserve their existing defaults and types, and keep the existing keyword-based pass-through call sites unchanged.

In @src/gobby/test_types/cli.py around lines 113 - 121, Update the output-writing flow in the CLI command so an output path matching either write_baseline or the baseline path cannot overwrite the baseline JSON produced by write_baseline_file. Detect this path conflict before writing rendered output and preserve the baseline file, while leaving normal --output behavior unchanged.

In @src/gobby/test_types/render.py around lines 53 - 56, Update the diagnostic heading in the rendering flow around detailed_issues and _append_new_errors so it accurately reflects whether diff is absent or present: label the no-baseline report as errors, and the baseline comparison as new failing errors. Preserve the existing issue selection and rendering behavior.

In @tests/agents/test_plan_adversary_manifest.py around lines 132 - 143, Add tool-level enforcement coverage to TestCoordinatorOwnedWrites by asserting the review step’s blocked_mcp_tools configuration includes apply_plan_review_manifest, finalize_plan_review_evidence, and checkpoint_plan_review_lesson_mint. Keep the existing instruction-text assertions and use the manifest/configuration symbols already exposed by the test fixtures rather than relying only on agent prose.

In @tests/cli/test_test_types.py around lines 1 - 13, Add a module-level pytest marker declaration in tests/cli/test_test_types.py for the CLI test category, using the project's established marker symbol and placement alongside the imports. Keep the existing test command import and test behavior unchanged.

In @tests/config/test_validation_detection.py around lines 68 - 78, Expand test_test_types_ratchet_requires_baseline_and_fail_on_new to classify commands containing only --baseline baseline.json and only --fail-on-new, asserting both return None; retain the existing assertions for both flags and neither flag.

In @tests/mcp_proxy/tools/test_agents.py around lines 1954 - 2002, Extract the duplicated _KillDeliveryRegistry and_record_removals scaffolding into a shared tests/completion_delivery_helpers.py module, exposing reusable DeliveryRegistry and record_removals symbols. Update test_agents.py, test_agent_cancellation.py, spawn_agent/test_health.py, and spawn_agent/test_error_handling.py to import and use these helpers, removing their local duplicate definitions while preserving existing notify, cleanup, and subscriber-removal behavior.

In @tests/mcp_proxy/tools/test_review_learning.py around lines 126 - 128, Update the context-count assertion in the test to require at least two contexts rather than exactly two, while preserving the existing shared review_learning_service identity assertion.

In @tests/plans/test_review_evidence.py around lines 481 - 710, Split test_manifest_compare_and_apply into focused scenario-scoped tests covering canonical/tampered validation, pre-write crash recovery, idempotent re-application and payload changes, post-write checkpoint recovery, and drift revocation. Reuse the existing canonical_approval setup/helper and isolate each scenario with its own fixture state so failures identify the affected behavior without masking later coverage.

In @tests/plans/test_review_evidence.py around lines 380 - 384, Update the migration execution setup around temp_db.execute and migration so it does not split SQL with raw split(";"). Run the migration as one script or use the project’s SQL-aware statement splitter, preserving execution of all migration statements including quoted semicolons and function bodies before validating catalog().

In @tests/plans/test_review_evidence.py around lines 26 - 30, Add the pytest integration marker to the test module containing review_setup so tests in tests/plans/test_review_evidence.py are collected by -m integration. Apply the marker at module scope rather than only to the review_setup fixture.

In @tests/review_coverage_helpers.py around lines 12 - 16, Extract the shared canonical JSON-plus-SHA-256 digest logic into a helper in gobby.plans, then update production functions manifest_digest and attestation_digest in gobby.plans.review_evidence and the test helper manifest_digest to delegate to it. Preserve the existing canonicalization options and digest outputs while eliminating duplicated hashing logic.

In @tests/review_learning/test_feedback_loop_e2e.py around lines 125 - 127, Update the plan-review assertions around _class_finding to verify that record() does not mutate the caller-provided finding dictionary: deep-copy the finding before the record call, then compare the original input with that snapshot after recording. Keep separate assertions for the recorded payload’s rule_id and absence of path only if those validate the persisted output.

In @tests/review_learning/test_feedback_loop_e2e.py around lines 169 - 178, Remove the self-asserting checks in the end-to-end test around the candidate serialization and validation-finding literals: delete the equality assertion comparing objects derived from the same candidate bytes and the assertions rechecking locally defined prevention, root_cause, path, and symbol values. Preserve the meaningful recorded-result assertions for pattern_id, finding_fingerprint, and differing occurrence_key.

In @tests/review_learning/test_lessons.py around lines 82 - 86, Replace the Any annotations on fake_memory_manager and fake_task_manager in test_domain_and_check_key_tags and the additionally affected test with the concrete FakeMemoryManager and FakeTaskManager fixture types imported from tests/review_learning/conftest.py. Preserve the existing fixture behavior and test logic.

In @tests/review_learning/test_recall_context.py at line 183, Annotate the fake_task_manager parameter in test_code_domain_excludes_plan_lessons with the FakeTaskManager type, preserving the test’s existing behavior.

In @tests/review_learning/test_retirement.py around lines 24 - 27, Add the module-level pytest marker declaration to tests/review_learning/test_retirement.py, using pytest.mark.unit consistently with sibling tests so the module is included in unit-marker selection. Ensure the required pytest import is present and leave the existing test constants unchanged.

In @tests/review_learning/test_round_diff.py around lines 25 - 31, Add pytest markers in the test module so pure classification tests are marked unit and DB-backed round/approval tests are marked integration and/or slow, following the repository’s existing marker conventions. Apply the markers to the relevant test functions or classes without changing their behavior.

In @tests/servers/routes/test_agent_spawn_routes.py around lines 312 - 314, Add an explicit precondition in the test before the identity assertion to verify that server.services.completion_registry is not None, then retain the kwargs["completion_registry"] identity check. Anchor the change in the test’s create_http_server setup and mock_spawn assertions.

In @tests/servers/routes/test_configuration_effective_routes.py around lines 83 - 88, Mark all four tests using the real hub database fixture, including test_effective_config_filters_resolves_stringifies_and_overlays and the tests at the referenced locations, with the appropriate integration pytest marker. Preserve their existing behavior and signatures.

In @tests/skills/test_development_discipline_skill.py around lines 36 - 37, Update the assertion in the required_contract loop to include the current phrase in its failure message, so pytest identifies which contract phrase is missing while preserving the existing containment check.

In @tests/skills/test_development_discipline_skill.py around lines 1 - 10, Add an appropriate pytest marker to test_validation_lesson_contract in the development-discipline contract test module, using the project’s established marker conventions so marker-based selection includes this test.

In @tests/skills/test_epic_review_skill.py around lines 94 - 133, The test locally filters incomplete entries before calling service.record, so it does not exercise production completeness gating. Remove the duplicated required-field condition from the loop and assert the incomplete-entry behavior through the actual recorder/validator enforcement; alternatively remove the incomplete fixture and document that only the existing doc-phrase assertion covers this rule.

In @tests/skills/test_review_learning_skill.py around lines 385 - 634, Split test_interactive_approval_sequence into independent tests for contract assertions, happy-path apply/idempotency, post-apply drift and finalization, pre-apply drift rejection, pending-drift revocation, and crash-restart recovery. Add fixtures for the shared _review_setup and _approval context, and use scoped monkeypatch fixtures/context handling for atomic_write_bytes in the crash scenarios so setup, cleanup, and failures remain isolated.

In @tests/skills/test_review_learning_skill.py around lines 65 - 89, Replace the hand-built YAML serialization in_manifest_yaml with yaml.safe_dump applied directly to _manifest_entries(stem), preserving all covers and labels entries and correctly quoting scalar values. Return the dumped YAML as the function’s expected list-of-lines representation, keeping the parsed output consistent with the source dictionaries.

In @tests/skills/test_review_learning_skill.py around lines 385 - 389, Mark test_interactive_approval_sequence with the integration test marker instead of the unit marker, preserving its existing test behavior and setup.

In @tests/storage/test_stage_review_findings.py around lines 355 - 497, Split test_pre_spawn_snapshot_transport into three independent tests covering successful snapshot transport, DispatchSpawnFailed evidence expiry, and wrong-lineage bind failure. Reuse stage_review_setup and introduce a small_spawn_with(impl) helper for shared spawn setup and monkeypatching, while preserving each scenario’s existing assertions and behavior.

In @tests/storage/test_stage_review_findings.py around lines 507 - 513, Update the crash_finalize stub’s return annotation from None to Never and add the corresponding typing import, matching the existing Never usage in the test suite.

In @tests/tasks/test_validation_issues.py around lines 257 - 271, Update test_validation_prompt_structured_issue_contract to construct the validation prompt path from Path(__file__).resolve().parents[2] instead of the current working directory, and pass encoding="utf-8" to read_text().

In @tests/tasks/test_validator_lesson_injection.py at line 36, Update the module-level_PROMPT_PATH in test_validator_lesson_injection.py to resolve from __file__ rather than the current working directory, matching the path-resolution approach used by test_validation_issues.py while preserving the existing prompt target.

In @tests/test_quality/test_baseline.py around lines 1 - 10, Add a module-level pytestmark in tests/test_quality/test_baseline.py using the appropriate marker for these baseline tests, while preserving the existing imports and test behavior.

In @tests/test_types/test_audit.py around lines 1 - 13, Add a module-level pytestmark in tests/test_types/test_audit.py using the appropriate existing marker for these type-audit tests, placing it alongside the imports or other module configuration. Ensure all tests in the module receive that marker without changing their test behavior.

In @tests/test_types/test_mypy_parser.py around lines 1 - 14, Add a module-level pytestmark to tests/test_types/test_mypy_parser.py using the appropriate existing marker for these tests, such as unit. Keep the current imports and test behavior unchanged.

In @tests/test_types/test_render.py around lines 1 - 6, Add a module-level pytestmark in tests/test_types/test_render.py identifying these tests with the appropriate marker, such as unit, consistent with the project’s existing marker conventions.

In @tests/workflows/test_review_learning_rules.py around lines 304 - 314, In the test assertion block, assert that resolved_effects contains exactly one effect before accessing resolved_effects[0]. Then retain the existing effect property assertions, so both missing and unexpected additional effects are reported clearly.

In @crates/gwiki/src/commands/read.rs around lines 125 - 131, Add a read_title_with_max_bytes delegator alongside read_title, accepting an explicit byte limit and passing it through the title-reading flow to read_existing_path; update read_title to resolve configured_read_max_bytes() and delegate to it, keeping title reads symmetric with read_path and independently testable.

In @crates/gwiki/src/commands/search.rs around lines 128 - 146, Consolidate the repeated ai_source_for_conn calls in the embedding, qdrant, and falkor configuration blocks into one shared mutable source initialized once with the existing WikiError::Config mapping. Reuse that source sequentially for resolve_semantic_embedding, resolve_qdrant_config, and resolve_falkordb_config, preserving each resolver’s current behavior and error context.

In @crates/gwiki/src/support/config.rs around lines 153 - 185, The two layer-resolution helpers duplicate layer selection and source construction; replace them with one generic helper that accepts a terminal resolver callback and returns its generic result. Update resolve_index_options_from_layers and resolve_shared_code_graph_limits_from_layers to delegate to this helper while preserving their existing resolver calls and standalone-loading behavior.

In @crates/gwiki/src/support/test_env.rs around lines 51 - 64, Update daemon_config_disabled() and its callers so reads of DAEMON_CONFIG_DISABLE_ENV are serialized with ENV_TEST_LOCK, or move the override behind process isolation; ensure every var_os() read is protected against concurrent EnvGuard mutations while preserving existing configuration behavior.

In @src/gobby/adapters/claude_code.py around lines 110 - 128, The redirect branch in the function containing_DENY_REASON_MAX_CHARS must enforce the 300-character cap even when the action itself is too long. Truncate the action segment before constructing action_message, reserving space for the ellipsis as needed, then retain the existing short-reason budgeting and formatting behavior.

In @src/gobby/adapters/codex_impl/client_lifecycle.py around lines 87 - 88, Guard the redaction loop in the client lifecycle error handling so empty strings from client._redacted_env_values are skipped before calling failure_detail.replace. Preserve redaction for non-empty secret values and prevent empty-value replacements from altering or inflating failure_detail.

In @src/gobby/agents/resume_executor.py around lines 152 - 156, Update the provider handling in the resume executor around the provider-specific branch so non-codex providers set the endpoint base URL and API token using the environment-variable names expected by each provider’s_resume_api_base() and prepare_sandbox_launch flow. Keep codex on its existing codex-specific path, and ensure droid, grok, and qwen receive the selected endpoint under their matching provider-specific base-url and API-key variables.

In @src/gobby/ai/_tool_chat_spawn.py around lines 476 - 488, In the wire_api == "responses" branch of the tool-chat spawning flow, replace direct indexing of self._config.ai.generation.endpoints with the existing resolve_generation_endpoint helper. Preserve the current endpoint validation and pass the resolved endpoint into codex_endpoint_config_overrides and codex_endpoint_env so missing endpoints raise the helper’s descriptive ValueError.

In @src/gobby/ai/endpoint_activation.py around lines 53 - 67, Wrap the complete activation probe chain in a single overall timeout at the caller around the text, tool, and vision probes, rather than adding separate per-attempt limits in_retry_activation. Ensure the timeout covers all serial probe execution and causes the synchronous PUT to terminate when exceeded, while preserving existing retry behavior within the allotted duration.

In @src/gobby/ai/vision.py around lines 146 - 147, Update CodexEndpointVisionExtractAdapter.stop() to check self._client.is_connected before calling self._client.stop(), matching the guarded cleanup behavior in CodexWebChatBackend.stop(). Ensure never-started or failed-start clients are skipped while connected clients still stop normally.

In @src/gobby/communications/inbound.py around lines 62 - 71, Update the duplicate-message branch in the inbound message handling flow to append the persisted existing record returned by get_message_by_platform_id to handled instead of the raw incoming message. Keep the duplicate detection and continue behavior unchanged, ensuring handled contains the valid database CommsMessage instance for duplicates.

In @src/gobby/communications/lifecycle.py around lines 134 - 139, Update the update_config callback in the lifecycle adapter setup to prevent stale adapter generations from persisting changes after update_channel() replaces the active adapter. Serialize configuration updates or validate that the callback’s adapter generation is still current before storing; use an atomic config patch/CAS through manager._store.update_channel where available, and reject outdated callbacks without overwriting newer channel.config_json.

In @src/gobby/hooks/event_handlers/_session_start/handoff.py around lines 8 - 13, Centralize the mandatory section-title list used by both_bound_handoff_summary and allocate_section_budget, and update each function to reference the shared symbol for “next steps” and “current state” instead of maintaining separate lists.

In @src/gobby/install/shared/workflows/rules/worker-safety/no-full-test-suite.yaml around lines 16 - 17, Update the pytest remediation examples in the no-full-test-suite rule to prefix every command with GOBBY_TEST_PROTECT=1, including both targeted path and -k pattern examples. Preserve the existing guidance that full-suite runs are reserved for the user.

In @src/gobby/llm/sdk_utils.py around lines 258 - 268, The_section_priority function uses a bare 25 for preamble sections; define a module-level_PREAMBLE_PRIORITY constant and return it for empty section titles. Keep the constant aligned with the intended priority relative to unknown_priority.

In @src/gobby/mcp_proxy/tools/spawn_agent/_generation_endpoint.py around lines 73 - 79, Update the exception handler around ensure_local_model in the spawn-agent generation endpoint to catch only the documented LocalModelError, preserving its ValueError wrapping and exception chaining while allowing unrelated programming errors such as AttributeError or TypeError to propagate unchanged. Import or reference LocalModelError from the appropriate local-model module.

In @src/gobby/servers/routes/configuration_generation_endpoints.py around lines 43 - 50, The generation endpoint flow persists request.api_key before GenerationConfig validation and probe_responses_endpoint succeed. Move config_store.set_named_secret out of the pre-validation block and execute it only after successful endpoint validation/probing and activation; preserve the existing secret metadata and skip persistence when no API key is provided.

In @src/gobby/servers/routes/configuration_values.py around lines 76 - 103, The validation flow for POST /values/validate must apply the same responses-endpoint restriction as saving. Invoke_reject_unprobed_responses_endpoint_updates with the submitted updates and prospective DaemonConfig during validation, ensuring responses endpoints are rejected there just as PUT /values rejects them.

In @src/gobby/servers/routes/providers.py around lines 195 - 202, The endpoint filtering comprehensions in the affected provider route paths should safely handle raw or unvalidated endpoint values. Replace direct endpoint.wire_api access with a guarded attribute lookup using None as the fallback, including the corresponding logic around the other endpoint filters at the referenced locations, while preserving the existing chat-completions matching behavior.

In @src/gobby/servers/routes/providers.py around lines 304 - 356, Extract the shared services → config → ai → generation → endpoints lookup and dict guard into a helper named _configured_endpoints(server, wire_api). Have it yield or return only endpoints matching the supplied wire_api, then update _local_generation_model_groups, _configured_endpoint_provider_entries, and _responses_endpoint_models to iterate through_configured_endpoints with their respective API values and remove their duplicated lookup and filtering logic.

In @src/gobby/servers/websocket/chat/runtime_manager.py around lines 72 - 93, Update WebChatRuntimeManager.__init__ in the wire_api == "responses" branch to catch ValueError from codex_endpoint_config_overrides or codex_endpoint_env and continue to the next endpoint, matching the existing local-endpoint handling. Keep valid Responses endpoint client construction unchanged while skipping misconfigured endpoints without aborting manager startup.

In @src/gobby/storage/communications.py around lines 304 - 315, When the deduplication path in the message persistence method receives inserted=False, log that attachment persistence was skipped, including the message and platform identifiers. Keep returning the existing persisted message and empty saved_attachments unchanged, and do not alter attachment insertion for newly inserted messages.

In @src/gobby/storage/config_store.py around lines 569 - 573, Move the stored-value lookup and secret-name resolution into the existing mutation transaction/lock that performs the config-row deletion, using the transaction’s consistent read before deleting. Update the method containing `stored_value` and `config_key_to_secret_name` so concurrent set_secret changes cannot occur between reference resolution and removal; preserve the fallback mapping for non-secret or empty references.

In @src/gobby/test_types/audit.py around lines 99 - 100, Update the directory-target handling in the audit path around_walk_python_files and _is_excluded_directory so targets outside root are skipped before calling_is_excluded_directory. Preserve the existing behavior for in-root directories and ensure out-of-root directory arguments produce no candidates rather than raising, including the corresponding logic at the other affected occurrence.

In @tests/adapters/test_claude_code_adapter.py around lines 143 - 179, Remove the import-time assertions from_bundled_before_tool_block_reasons and make the loader skip malformed rules while collecting valid block reasons. Move validation for exactly one block effect, string reasons, and duplicate rule names into a dedicated test or session-scoped fixture that reports the offending rule clearly, and avoid invoking validation during module initialization via _BUNDLED_BEFORE_TOOL_BLOCK_REASONS.

In @tests/adapters/test_claude_code_adapter.py around lines 30 - 139, Update test_live_corpus_is_exhaustively_classified_once to replace the combined set-equality assertion with separate checks for missing and unexpected rule names, covering both_REDIRECT_BLOCK_RULES and _TRUE_RESTRICTION_BLOCK_RULES. Include the relevant set difference in each assertion message so added, renamed, or deleted bundled before_tool rules identify the exact classification change required.

In @tests/adapters/test_claude_code_adapter.py at line 140, Reformat the_SKILL_FETCH_REASON_TEMPLATE assignment to keep each source line within the 100-character limit, using adjacent implicitly concatenated string literals while preserving the exact resulting template value.

In @tests/agents/test_merge_orchestrator_contract.py at line 396, Update the allowed MCP tools assertions in the merge orchestrator contract test to verify that the obsolete gobby-sessions:record_verification_evidence tool is absent, while preserving the existing assertion for gobby-merge:verify_in_worktree.

In @tests/agents/test_resume_executor.py around lines 128 - 230, Add the required unit pytest marker to test_resume_responses_endpoint_rebuilds_child_scoped_codex_config alongside pytest.mark.asyncio, preserving the test’s existing behavior and other markers.

In @tests/ai/test_capability_registry.py at line 428, Update the VISION_EXTRACT assertion in the capability registry tests to use the generic endpoint provider key, matching the TEXT_GENERATE test, and verify that this key is not registered. Remove the obsolete "local" lookup so the test remains meaningful after the provider-key migration.

In @tests/communications/test_attachments.py around lines 221 - 250, Extend test_create_message_with_attachments_links_rows_atomically to force attachment persistence to fail after message creation, then assert the operation raises and that neither the message row nor any attachment rows remain. Preserve the existing successful-linkage assertions in a separate test or successful path, and use the store’s existing attachment-persistence seam rather than adding unrelated setup.

In @tests/communications/test_manager.py at line 1443, Add the required return type annotation to the async test function test_telegram_inbound_session_reply_resolves_chat_destination, using the appropriate coroutine return type for an async test that does not return a value.

In @tests/config/test_validation_detection.py around lines 79 - 90, Add the @pytest.mark.unit decorator to test_test_types_ratchet_rejects_wrapped_commands_missing_required_flags so this pure classify_validation_command test is included in unit-test marker selection.

In @tests/e2e/test_inter_agent_messages.py around lines 291 - 316, Update the test around the first and second get_inter_session_messages calls to capture the message id from the first result, then assert that exact id is present in the second result’s messages. Preserve the existing checks that both reads return at least one message while verifying the same message survives.

In @tests/memory/test_knowledge_graph.py around lines 512 - 518, Add @pytest.mark.asyncio to both new async tests, including test_add_to_graph_maps_valid_duplicate_malformed_and_unknown_relation_ids and the async test beginning at the second referenced location, matching neighboring async tests in the class.

In @tests/test_quality/test_baseline.py around lines 54 - 111, Add the @pytest.mark.unit decorator to each newly added test function: test_chmod_failure_preserves_existing_baseline_and_removes_temporary_file, test_write_baseline_honors_process_umask, test_load_baseline_rejects_unsupported_schema, and test_load_baseline_rejects_non_positive_occurrences.

In @tests/workflows/test_rewrite_rules.py around lines 223 - 225, Replace the duplicated _ACTION_FIRST_PREFIXES,_GET_SKILL_RE, _COMMAND_CALL_RE, and_is_action_first_reason logic in the tests with the production redirect classifier from claude_code.py. Import and call the adapter’s existing predicate, or expose it through a shared helper, so the framing assertions directly track production behavior.

In @web/src/components/chat/ChatInput.tsx at line 216, Update the ChatInput attachment handling around useChatInputAttachments so that when imagesDisabled changes to true, all queued image entries are removed and their associated resources are cleaned up. Preserve non-image attachments and normal behavior while image support remains available.

In @web/src/components/chat/__tests__/ProviderPicker.test.tsx around lines 45 - 64, Rename the helper function buildLocalCatalog to buildEndpointCatalog and update all five test call sites to use the new name, preserving its catalog construction behavior unchanged.

Fix the following issues. The issues can be from different files or can overlap on same lines in one file.

In @.github/workflows/cla.yml around lines 3 - 7, Add a workflow-level concurrency configuration near the top-level triggers in the CLA workflow, using one shared group for all pull_request_target and issue_comment runs and setting cancel-in-progress to false. Ensure every signature update is queued and processed rather than canceled or running concurrently.

In @.gobby/plans/context-mode-borrowings.md around lines 307 - 318, Update the tool_results schema’s total_chars field to use a 64-bit-compatible type, and align any corresponding application model, migration, or persistence types that write or read it. Preserve the existing max_stored_chars behavior while ensuring serialized inputs exceeding the signed 32-bit range can be persisted.

In @crates/gcode/tests/graph_standalone/support.rs at line 83, Replace the literal "GOBBY_RUNTIME_MODE" in the test environment setup with gobby_core::runtime_mode::RUNTIME_MODE_ENV, and apply the same constant wherever this environment variable is referenced in the related standalone and stale projection tests.

In @crates/gcore/src/runtime_mode.rs around lines 55 - 73, Change parse_requested_mode and select_runtime_mode_with_probe so the parser returns a standalone-specific result type, such as an existing or new StandaloneOverride, rather than RuntimeMode; update the match to handle only the standalone override and absence, removing the Some(RuntimeMode::Daemon) unreachable arm while preserving the existing daemon URL, service registration, and standalone fallback behavior.

In @crates/gwiki/src/commands/index.rs around lines 238 - 241, Clamp the max_age_hours value at the IngestUrl CLI argument boundary in cli.rs to the inclusive range 0..=8760, ensuring direct gwiki ingest-url invocations cannot request unbounded cache reuse. Locate the IngestUrl argument definition and apply the same bound used by the gateway before execute_ingest_url receives the value.

In @crates/gwiki/src/health/tests.rs around lines 1 - 21, Move the source_reference_is_present helper below the complete use/import block, keeping its implementation unchanged and preserving the existing imported symbols from aho_corasick and the citations module.

In @crates/gwiki/src/ingest/url/tests.rs around lines 305 - 345, Add assertions in within_ttl_uses_manifest_cache_without_fetch_or_store for the cached-only result’s UrlBatchIngest status() and exit_code(), expecting "ingested" and 0. Add focused coverage for a cached-plus-failed batch, asserting status() is "partial" and exit_code() is 0, using the existing batch/result construction patterns and symbols.

In @crates/gwiki/src/ingest/url/tests.rs around lines 432 - 523, Use separate MemoryWikiStore instances for the HTML and PDF ingestion flows in missing_url_artifacts_and_invalid_freshness_refetch_for_self_healing: create an HTML-specific store for the first ingest and a PDF-specific store for all PDF ingests, rather than reusing store across distinct vault roots.

In @crates/gwiki/src/upkeep/runner.rs at line 36, Document the purpose of the MIN_CLUSTER_REMAINING_SECONDS constant, noting that its approximately 20-minute reservation preserves tail execution budget and causes runs with less remaining time to defer every cluster on the first pass.

In @crates/gwiki/src/upkeep/tests.rs around lines 1087 - 1109, The time-budget test around run_with_clock should document the intentional relationship between the 1320-second budget and the 111-second clock jump after the second call, including the per-cluster projection ratio being exercised. Add a concise comment near these magic values without changing the test behavior.

In @crates/gwiki/src/upkeep/tests.rs around lines 683 - 687, Replace the vacuous disjunction in the dry-run assertions with a direct assertion that report.reconciled_no_synthesis is empty. Keep the existing pending-count and planned-create assertions unchanged.

In @docs/guides/gcode-development-guide.md around lines 96 - 111, Update the earlier PostgreSQL Bootstrap section to match the runtime-mode contract: document daemon-mode DSN precedence as GCODE_DATABASE_URL/GOBBY_POSTGRES_DSN, daemon effective configuration, then bootstrap.yaml, without falling back to full gcore.yaml or standalone resolution on daemon failure. Document standalone mode separately with its full gcore.yaml fallback, preserving the established symbols and precedence.

In @src/gobby/communications/adapters/telegram.py around lines 311 - 316, Bound the growth of _edit_overflow_ids in the outbound send logic surrounding root_message_id, so multi-chunk messages that are never edited cannot accumulate indefinitely. Use an appropriate bounded structure or restrict recording to sends originating from editable streaming turns, while preserving overflow lookup for supported edit_message calls.

In @src/gobby/communications/adapters/telegram.py around lines 296 - 316, Update the chunk-sending loop in the message-send method to stop processing immediately when any _post_json("sendMessage", payload) response is not ok, returning None (and preserving the existing successful mapping otherwise). Do not continue collecting later chunk IDs after a failed send, so _edit_overflow_ids remains keyed to the first successfully sent chunk.

In @src/gobby/communications/adapters/telegram.py around lines 325 - 376, The edit_message method should treat Telegram’s “message is not modified” response from editMessageText as a successful no-op. Detect that specific HTTP/API error around the_post_json edit call, suppress only this case, and continue processing remaining chunks while propagating all other errors unchanged.

In @src/gobby/communications/adapters/telegram_formatting.py around lines 41 - 134, Add a depth parameter and small maximum nesting limit to_parse_inline, incrementing it for recursive emphasis and link-body parsing. When the limit is reached, stop recursing and render the remaining input as plain text so pathological nested formatting cannot raise RecursionError; preserve existing parsing behavior below the limit.

In @src/gobby/communications/adapters/telegram_formatting.py around lines 198 - 226, Update the chunk finalization flow around finish_chunk() so it does not append or retain a trailing chunk when the current parts contain no visible text, including cases where remainder becomes empty after lstrip(). Preserve chunks with actual text and ensure reopened/closed formatting tags are not emitted as a standalone message.

In @src/gobby/communications/chat_backend.py around lines 68 - 77, Update the cleanup in stop_turn so cancellation does not interrupt the awaited typing-task shutdown or transport.finalize flush. After absorbing the cancellation, shield these cleanup awaits or clear the current task’s cancellation state, ensuring the final partially streamed text is delivered before removing the active turn.

In @src/gobby/communications/chat_transport.py around lines 122 - 138, The_finalize method can resend the full text when the initial message was delivered without a platform message ID. Before the fallback _send path, check_last_delivered_text and avoid sending again when text has already been delivered; preserve editing when _platform_message_id is available and update delivery state consistently.

In @src/gobby/communications/responder.py around lines 62 - 116, Add a bounded pending-turn limit to ConversationTurnQueue so enqueue rejects or answers busy when a conversation already has the maximum queued depth, preventing unbounded task creation. Track per-conversation pending counts alongside _tails, decrement them when tasks finish or are dropped, and preserve serialization for accepted callbacks; update callers to handle the enqueue rejection as the busy response.

In @src/gobby/communications/voice.py around lines 37 - 66, Update apply_voice_transcription around transcriber.transcribe to enforce the configured transcription_timeout_seconds, and catch transcription failures or timeouts locally. On failure, preserve the message and set voice_transcription_status to a failed/degraded status consistent with existing conventions, rather than propagating the exception; keep unavailable, empty, and completed outcomes unchanged.

In @src/gobby/hooks/envelope_dedupe.py around lines 168 - 182, Update release_envelope_processing_claim so marker removal cannot race with finalization and delete a terminal record. Use an atomic rename-then-inspect or equivalent compare-and-delete operation around the record read, and only remove the marker when its contents still match the processing claim; otherwise preserve the finalized marker and return False.

In @src/gobby/hooks/event_handlers/_tool.py around lines 11 - 13, Remove the redundant local SessionVariableManager import from _track_session_edited_file, reusing the existing module-level import while preserving the method’s behavior.

In @src/gobby/hooks/hook_manager.py around lines 353 - 357, Update HookManager.handle() and handle_async() around ingest_hook_verification_receipt() to catch VerificationReceiptIngestionError for non-HTTP hook paths, preventing receipt-ingestion failures from aborting hook execution; preserve the existing fail-closed behavior used by the HTTP adapter.

In @src/gobby/hooks/memory_recall_dispatcher.py around lines 70 - 92, The lifecycle logs in the deferred recall scheduling flow should expose session_id and parent_turn_seq as structured fields rather than interpolated message arguments. Update the relevant logger calls around_prune_tasks, the duplicate-task message, and the scheduling-failure handler to use the project’s structured logging API or extra context, preserving the existing messages and including both identifiers where available.

In @src/gobby/hooks/memory_recall_dispatcher.py around lines 72 - 90, Refactor the task tracking around _prune_tasks and the scheduling flow to retain active futures separately from a bounded or evictable per-session deduplication watermark. Preserve deduplication for repeated session_id/parent_turn_seq submissions while allowing completed futures from inactive or single-turn sessions to be removed, and ensure shutdown iterates only active work; add coverage for many single-turn sessions.

In @src/gobby/hooks/tool_error_tracker.py around lines 190 - 224, Update _mapping_error_text to accept and propagate a bounded recursion-depth parameter through all recursive calls for error, tool_output, tool_response, tool_result, and structuredContent mappings. Stop descending once the budget is exhausted while preserving the existing text extraction behavior for values at permitted depths.

In @src/gobby/hooks/tool_error_tracker.py around lines 346 - 370, Update track_proxy_outcome so unknown outcome_class values are logged with the module-level logger and then return without raising, keeping tool dispatch fail-open. Add logger = logging.getLogger(__name__) at module scope and use it for the diagnostic; preserve existing handling for policy_denied, invalid_call, failed_pre_dispatch, and executed outcomes.

In @src/gobby/hooks/tool_error_tracker.py around lines 97 - 119, Update _normalize_hash_value and its callers, including_canonical_json, to track recursion depth and stop normalizing once a safe maximum is reached, returning a stable type marker at the cap. Ensure deeply nested or self-referential mappings, sequences, and sets cannot raise RecursionError while preserving normal canonical normalization for shallow payloads.

In @src/gobby/install/shared/prompts/validation/validate.md around lines 85 - 88, Refresh the corresponding entry for src/gobby/install/shared/prompts/validation/validate.md in bundled_content_manifest.json so its recorded content hash or metadata matches the updated prompt, preserving the manifest’s existing format and ordering.

In @src/gobby/mcp_proxy/server.py at line 51, Update the constructor path around RecommendationService and PromptLoader so PromptLoader never receives a missing database: either make the server’s db parameter required as a HubDatabase, or explicitly handle None in RecommendationService before rendering recommendations. Preserve the existing recommendation flow for valid HubDatabase instances.

In @src/gobby/mcp_proxy/services/result_offload.py around lines 233 - 246, Update the envelope budget check in the result-building method containing _fit_structure, _fit_text_field, and_fit_matches so exceeding the working budget is handled without raising AssertionError to the caller. Log the over-budget condition and return a degraded but valid envelope, preserving successful tool-call results; ensure this handling covers both normal and fallback paths in_maybe_offload_sync and _execute_tool_dispatch.

In @src/gobby/mcp_proxy/services/tool_execution.py around lines 44 - 54, Eliminate the synchronous full-payload deepcopy in _identity_arguments and the second copy around track_proxy_outcome. Build the tracking identity from a normalized or hashed representation, or defer any required copying into the existing asyncio.to_thread call, while preserving identity generation from the effective enforcement-modified arguments.

In @src/gobby/mcp_proxy/stdio_proxy.py around lines 316 - 317, Bound the model-generated intent before assigning it to request_kwargs["params"] in the non-wait-tool path. Define and use a module-level_MAX_INTENT_QUERY_CHARS limit (for example, 1024) to truncate the query-string intent, while leaving the wait-tool body JSON path unchanged.

In @src/gobby/mcp_proxy/tools/results.py around lines 216 - 225, Update_validate_search_arguments to use the existing _MAX_SEARCH_LIMIT constant instead of the literal 50 when validating limit, keeping the lower bound and error behavior unchanged and aligning validation with the schema bound.

In @src/gobby/mcp_proxy/tools/results.py around lines 244 - 269, Update_hydrate_matches to replace the per-hit db.fetchone calls with one bulk query for all hit.id values scoped to result_id, then map the returned rows by chunk id and build matches in the original hits order. Preserve skipping hits without a matching row and the existing field conversions and output shape.

In @src/gobby/sessions/processor_transcripts.py at line 330, Gate the parser state snapshot in the incremental batch processing flow to Codex sessions only, initializing parser_state to None for other parsers to avoid deepcopy overhead. In the rollback path, call parser.hydrate_state(parser_state) only when parser_state is not None, while preserving existing Codex failure recovery.

In @src/gobby/storage/tool_results.py around lines 70 - 93, Remove the retention DELETE from the save() transaction in the tool-results storage implementation, leaving save() write-only with its INSERT operation. Move or invoke that expiry cleanup through the existing cron/background sweep mechanism instead, using the same retention_days cutoff there.

In @src/gobby/workflows/state_manager.py around lines 383 - 433, Extract the repeated session-variable read-modify-write/insert logic from upsert_bounded_list_variable, upsert_open_tool_error, merge_variables, append_to_bounded_list_variable, record_edited_file, and the other affected methods into a shared _mutate_variables(session_id, mutator) helper. Have the helper handle transaction setup, SELECT, payload decoding, mutation, and UPDATE/INSERT persistence, while each caller supplies only its variable transformation and preserves its existing return behavior.

In @src/gobby/workflows/state_manager.py around lines 503 - 537, Update resolve_open_tool_errors so it compares the normalized error records before and after removing the canonical tool-and-target pair, and returns without issuing the UPDATE when the records are unchanged. Avoid materializing open_tool_errors or changing updated_at for sessions with no matching record; retain the existing write behavior when a record is actually removed.

In @tests/communications/test_manager.py around lines 670 - 725, Mark the async test function test_handle_inbound_transcribes_voice_note_before_event with pytest.mark.asyncio, preserving its existing test body and behavior.

In @tests/communications/test_responder.py around lines 182 - 195, Add the @pytest.mark.asyncio decorator to the async test function test_access_gate_rejects_sender_outside_allowlist, matching the sibling asynchronous tests so its coroutine body and assertions are executed.

In @tests/config/test_tool_result_offload_config.py at line 13, Add the appropriate pytest markers to the tests in test_tool_result_offload_defaults_and_app_accessor and the DB-backed migration test, using the established unit marker for config-only coverage and integration for the Postgres-dependent test. Ensure the markers are applied directly to the relevant test functions so they can be selected or excluded.

In @tests/config/test_tool_result_offload_config.py around lines 60 - 66, Update test_tool_results_migration_is_unique_and_applied to sort migration paths by their parsed numeric version, and remove the assertion requiring migration_paths[-1] to be 340_tool_results.sql. Retain the versions.count(340) == 1 assertion to verify uniqueness.

In @tests/hooks/test_inbox.py around lines 400 - 407, Extend the tests around test_release_envelope_processing_claim_allows_retry with a negative case for finalized and absent markers. Create a claimed marker, change its status to processed, then assert release_envelope_processing_claim returns False and preserves the marker; also assert releasing an empty envelope ID returns False.

In @tests/hooks/test_memory_recall_dispatcher.py around lines 88 - 99, Update the_create_session fixture to perform both setup inserts through the repository’s Hub transaction pattern, replacing the %s placeholders with positional $1…$N parameters. Preserve the existing project and session values and conflict behavior while matching the production database access contract.

In @tests/hooks/test_tool_error_tracker.py around lines 1 - 30, Add the repository-standard pytest marker declaration near the imports in the test module, using pytest.mark.unit so all tests in tests/hooks/test_tool_error_tracker.py are categorized as unit tests.

In @tests/mcp_proxy/services/test_result_offload.py around lines 20 - 24, Add a module-level pytest unit marker in test_result_offload.py alongside the existing imports and constants, using the same pytest.mark.unit declaration as sibling tests. Ensure pytest is imported if needed so marker-based selection includes all tests in this module.

In @tests/mcp_proxy/test_results_tools.py around lines 18 - 19, Register and apply a module-level pytest marker in tests/mcp_proxy/test_results_tools.py to categorize the contained tests, using the project’s existing marker convention (unit, slow, integration, or e2e) and preserving the distinction needed for the DB-backed and pure-mock cases.

In @tests/mcp_proxy/test_stdio_proxy.py around lines 25 - 46, The tool-capturing logic is duplicated between_capture_stdio_tools and TestMCPToolsWrapper._register_tools. Extract the shared MagicMock .tool decorator registration helper into a common test fixture or utility, then update both callers to reuse it while preserving their existing captured-name and callable behavior.

In @tests/mcp_proxy/test_stdio_proxy.py around lines 132 - 185, The envelope size limit is duplicated as a local literal across related tests, allowing assertions to drift from production behavior. Replace max_envelope_chars in test_stdio_final_wait_envelope_stays_within_shared_cap and test_stdio_final_retrieval_response_stays_within_shared_cap with the actual shared production cap or one shared test constant, and reuse that same symbol in the corresponding daemon-tools and execution-offload tests.

In @tests/prompts/fixtures/handoff_session_end_golden.md around lines 75 - 76, Insert one blank line immediately after the “## Unresolved Errors” heading in the fixture, before the following explanatory text, to satisfy Markdown heading-spacing linting.

In @tests/servers/routes/mcp_endpoints/test_execution_offload.py at line 15, Replace the local MAX_ENVELOPE_CHARS definition in the execution-offload tests with the shared constant used by test_stdio_proxy.py and test_gobby_daemon_tools.py, preferably importing the production-defined cap when available. Remove the duplicate literal so all tests stay synchronized automatically.

In @tests/storage/test_tool_results.py around lines 190 - 207, The test’s expected IDs are ordered by ordinal while search results are ranked by BM25 score, making the positional comparison fragile. Update the assertions around expected and hits to compare the chunk IDs as unordered sets, while preserving the existing monotonic score check.

In @tests/workflows/test_session_variable_manager.py around lines 603 - 628, Update both barrier synchronization points in test_open_tool_error_concurrent_upserts_merge_counts and the additionally affected test block to pass a finite timeout to threading.Barrier.wait(). Apply the timeout for worker and main-thread waits so any worker failure breaks the barrier and causes the test to fail promptly instead of hanging.

In @tests/workflows/test_summary_actions.py around lines 168 - 175, Add a parametrized test case alongside the existing summary action assertions that supplies enough unresolved-error records for format_unresolved_errors(records) alone to exceed TRANSCRIPT_FALLBACK_MAX_CHARS. Verify the resulting structured_context respects the cap and preserves the intended degenerate-path behavior when base_budget becomes negative, while retaining the existing assertions for the normal 10-record case.

In @crates/gcore/src/ai/generation/tests/tool_loop.rs around lines 701 - 748, Increase the SlowExecutor sleep duration in tool_timeout_is_recoverable_and_worker_drains_after_loop_continues to provide substantially more than 250 ms between the 1-second tool timeout and worker completion, and raise loop_timeout_seconds accordingly so the loop still completes before its overall deadline. Preserve the existing timeout recovery and pre-drain assertion behavior.

In @crates/gcore/src/ai_context.rs around lines 80 - 87, Update the non-strict error arm in the ToolLoopLimits::resolve match within the tool-loop limit setup to emit a log::warn! containing the configuration error before returning ToolLoopLimits::default(). Keep strict mode propagating the original error unchanged.

In @src/gobby/adapters/codex_impl/item_normalization.py at line 46, Use _DIRECT_EXEC_COMMAND_NAMES in item_normalization.py as the single direct-exec tool-name allowlist, and remove the duplicate_DIRECT_EXEC_NAMES definition from the codex.py transcript reconciliation flow. Update codex.py to import and reference the shared constant alongside extract_direct_exec_command and extract_direct_exec_terminal_result, preserving existing normalization behavior.

In @src/gobby/ai/_tool_chat_codex.py around lines 238 - 274, The turn limit is only enforced in handle_dynamic_tool, allowing tool-free turns to exceed limits.max_turns. Update record_raw_response to detect when the incremented turns reaches the configured cap and schedule an interrupt for the active turn, preserving the existing stop_reason and interrupt behavior used by handle_dynamic_tool.

In @src/gobby/ai/_tool_chat_codex.py around lines 306 - 312, Update the teardown in the chat client lifecycle around client.start() and start_thread so cleanup is only attempted when startup completed successfully, or safely handles cleanup failures without replacing the original exception. Guard remove_notification_handler, remove_request_handler, and await client.stop() while preserving the original startup error.

In @src/gobby/ai/_tool_chat_codex.py around lines 135 - 136, Update_is_tool_error to reuse _tool_result_is_error instead of checking for the obsolete `"ok":false` field, while preserving the existing “[error” detection. Ensure builtin results containing `"success": false` are classified as failures consistently with the other adapter.

In @src/gobby/ai/_tool_chat_contracts.py around lines 118 - 123, Add an effective_limits accessor to ToolChatRequest that returns the configured limits or a default ToolLoopLimits instance, then update the referenced adapters and spawn implementations to use it instead of duplicating request.limits fallback logic.

In @src/gobby/ai/_tool_chat_droid.py around lines 233 - 260, The request-handling flow around_NATIVE_TOOL_METHODS must respond to every server-initiated message with type "request". Preserve the existing disabled-native response, and add a fallback response for unhandled request methods using the original request_id, JSON-RPC error code -32601, and an appropriate method-not-found message before reaching the notification handling.

In @src/gobby/ai/_tool_chat_droid.py around lines 140 - 152, Enforce limits.loop_timeout_seconds across the Droid request and notification flow, including request() and the while-loop awaiting client.next_notification(). Apply the configured deadline so stalled JSON-RPC futures or notification waits terminate instead of hanging indefinitely, and ensure timeout cleanup removes the pending request and stops or propagates the timeout consistently through ToolLoopController.

In @src/gobby/ai/_tool_chat_droid.py around lines 469 - 473, Update the cleanup in the surrounding tool-chat flow so server.stop() runs even when client.stop() raises. Nest or otherwise independently guard the teardown operations, preserving both cleanup attempts and the existing exception propagation behavior.

In @src/gobby/ai/_tool_chat_mcp_server.py around lines 186 - 196, Centralize ToolRuntime.execute result classification in a new shared predicate in _tool_chat_tools.py that recognizes bracketed errors, JSON failures such as {"success":false,...}, and the existing typed failure formats. Replace the local checks in this MCP handler,_tool_chat_codex.py, and _tool_result_is_error in _tool_chat_adapters.py with that predicate, preserving each surface’s existing response behavior.

In @src/gobby/ai/_tool_chat_mcp_server.py around lines 93 - 100, Update the stop method to explicitly close the saved _socket during teardown, using a finally block around runner.cleanup() so the socket closes even if cleanup raises; preserve the existing state reset and only close the socket when it is present.

In @src/gobby/ai/_tool_chat_service.py around lines 109 - 119, Define and export a shared limit stop-reason constant in_tool_chat_contracts.py alongside ToolLoopLimits, containing max_turns, max_tool_calls, and timeout. Update the tool-chat service condition around result.stop_reason to use this constant instead of an inline set, and have adapters reuse it where they produce these stop reasons.

In @src/gobby/ai/_tool_chat_spawn.py around lines 48 - 57, Update the module-level __all__ to include the locally defined GrokSpawnToolChatAdapter and QwenSpawnToolChatAdapter alongside the existing CodexSpawnToolChatAdapter and DroidSpawnToolChatAdapter re-exports, or remove __all__ entirely; preserve all intended public adapters.

In @src/gobby/ai/codex_endpoint.py around lines 87 - 96, Update codex_endpoint_app_server_env to accept endpoint_name and use a distinct child directory under the Codex endpoints home for each endpoint, while preserving the existing base environment. Pass endpoint_name through all activation and vision call sites and update affected tests to validate the endpoint-specific CODEX_HOME path.

In @src/gobby/communications/adapters/telegram.py around lines 464 - 488, The send flow in the chunk loop registers the callback keyboard before any sendMessage call can succeed. Change the `reply_markup`/`_callback_registry.register_keyboard` handling so callback tokens are registered only after all chunks send successfully, or ensure the exception path explicitly removes the registration; preserve attaching the resulting markup to the final chunk.

In @src/gobby/communications/responder.py at line 15, Update the command handling around _COMMANDS and_run_command so the recognized "start" command has an explicit branch with the intended onboarding or welcome response, rather than falling through to backend.help(context). If no distinct start behavior is intended, remove "start" from_COMMANDS so it is not recognized separately.

In @src/gobby/communications/sticker_vision.py around lines 51 - 57, Replace the AssertionError-based narrowing after selecting the sticker image in the surrounding generator with normal control-flow narrowing. Assign or validate image.local_path within the loop, or use a helper returning str | None, so image_path is known to be a string without the unreachable raise while preserving the existing unsupported-image fallback.

In @src/gobby/communications/sticker_vision.py around lines 68 - 84, Bound the await of service.extract in the sticker vision flow with an asyncio timeout, adding the suggested module-level timeout constant and import. Keep the existing exception handler so timeout failures log, mark sticker_vision_status as failed, append the fallback content, and return.

In @src/gobby/install/bundled_content_manifest.json around lines 48 - 53, Remove the stale manifest entries for rules/build/build-agent-safety.yaml and skills/build/SKILL.md from bundled_content_manifest.json, leaving all existing entries for files that still exist under the shared content directory unchanged.

In @src/gobby/mcp_proxy/tools/communications.py around lines 70 - 125, The send_attachment function currently permits arbitrary readable files; constrain resolved_path to an allow-listed workspace root before calling communications_manager.send_attachment. Validate that the resolved path remains within the configured workspace directory, reject paths outside it with the existing failure response, and preserve normal file and attachment handling for allowed paths.

In @src/gobby/mcp_proxy/tools/communications.py around lines 258 - 259, Update the replace call creating updated in the responder-project persistence flow to set updated_at to the current UTC time, matching the admit_inbound_message channel update in manager.py; preserve the existing config_json update and pass the refreshed channel to communications_manager.update_channel.

In @src/gobby/mcp_proxy/tools/sessions/_registration.py at line 96, Update the session registration title-source assignment to use the shared manual_title_source helper from _title_defaults instead of duplicating the raw "manual" literal and blank-string check. Ensure the helper returns MANUAL_TITLE_SOURCE only for non-blank string titles and None otherwise, then reuse it in this call site.

In @src/gobby/mcp_proxy/tools/spawn_agent/_health.py around lines 167 - 187, Update schedule_tmux_health_check and the related _health_check_tasks/cancel_health_checks flow to track the returned pending TimerHandle before it fires, cancel those handles during shutdown, and remove each handle when its callback starts. Ensure cancellation prevents_start_tmux_health_check from creating tasks or mutating run state after shutdown begins.

In @src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py at line 84, Update the response_detail default in the lifecycle-close task transition flow so preview=true retains diagnostic fields including mechanical_gates, selected_evidence, evidence_completeness, and unassigned_receipts. Either default preview requests to diagnostic or update every preview caller to pass response_detail="diagnostic", ensuring no preview path silently uses concise output.

In @src/gobby/runner_init/servers.py around lines 165 - 169, Move the vision extractor wiring from the WebSocket-gated block into the unconditional runner.communications_manager block, alongside the other communications setup. Hoist the build_daemon_vision_extract_service import to module scope, while preserving the existing set_vision_extract_service call and runner.config argument.

In @src/gobby/servers/websocket/chat/backends/codex.py around lines 436 - 462, Update clear_session_context to reattach endpoint-backed sessions using session._model_selector rather than the canonical session._model value, while continuing to use session._model for native Codex sessions. Preserve the existing selector through detach/reattach so _apply_requested_model does not interpret the canonical endpoint model as a native-model switch.

In @src/gobby/servers/websocket/handlers/session_observe_continue.py at line 409, Update the session_continued payload’s title selection near session_observe continuation handling to use the persisted title from the created session, such as _resolved_session_title for session.db_session_id, instead of relying on manual_source_title for non-resume continuations. Preserve source_title for resume_in_place and ensure digest/provisional registrations emit their assigned session title rather than null.

In @src/gobby/sessions/processor_transcripts.py around lines 120 - 124, Call ProcessorHost._filter_session_title_messages directly instead of routing it through_run_db, since it only performs an in-memory filter. Update its typing and all usages accordingly, and revise test_process_session_runs_index_append_on_db_executor to remove the expectation that this method is dispatched via the database executor while preserving executor coverage for the remaining DB work.

In @src/gobby/storage/sessions/_summary_update.py around lines 154 - 184, Update the digest and revision database queries, including the dynamically assembled statement in the session summary update flow around the assignments list and conn.execute call, to use the hub’s numbered $N placeholders instead of %s. Renumber all placeholders consistently across each query and preserve the existing parameter ordering and transaction behavior.

In @src/gobby/workflows/summary_actions.py around lines 807 - 812, Update the summary-generation logger call in the relevant workflow action to keep the message static and pass mode, reason, and output_chars through structured extra context, matching _write_summary_file and the LLM-failure log. Update test_generate_summary_success to assert the structured logging fields rather than the embedded formatted message.

In @tests/ai/test_tool_chat_builtins.py around lines 262 - 265, Resolve the type contract mismatch in ToolLoopLimits and the related tests without using cast to pass float values as ints. Since tool_timeout_seconds is passed to asyncio.wait_for and run_argv, keep or restore its float-compatible annotation and update the affected parametrized tests, including the cases around test_nonpositive_outer_timeout_is_rejected and the additional occurrences, to pass values directly while preserving validation of nonpositive timeouts.

In @tests/ai/test_tool_chat_protocols.py around lines 486 - 488, Replace the two fixed asyncio.sleep(0) calls before_call_mcp in the test fixture with deterministic readiness synchronization: poll the adapter’s pending-call registration or await an asyncio.Event set when the handshake is ready, then invoke_call_mcp only after readiness is confirmed.

In @tests/ai/test_tool_chat_protocols.py at line 571, Update the assertion in the test covering factory.options["cwd"] to compare equivalent path representations, such as converting the configured string path to a Path before comparing with tmp_path. Assert the intended working-directory isolation property rather than relying on a str-versus-Path comparison.

In @tests/ai/test_tool_chat_service.py around lines 227 - 231, Replace the private_default_limits round-trip assertion in test_tool_chat_service_uses_one_canonical_request_deadline with an end-to-end chat_result test using ToolLoopLimits(loop_timeout_seconds=1) and a _SlowAdapter. Exercise at least two candidates, make the first slow enough to consume part of the budget, and assert the second receives the remaining timeout rather than a fresh one-second deadline; also assert total elapsed time remains near the single shared budget.

In @tests/ai/test_tool_chat_tools.py around lines 176 - 179, Resolve the contradictory assertions around the truncation result in the test: if marker preservation is intended, assert the marker and the payload separately while retaining the UTF-8 byte-length check; otherwise remove the stale truncation-marker comment and redundant split assertion. Align the test with the intended behavior of the truncation logic in the tool chat output path.

In @tests/communications/adapters/test_telegram.py around lines 913 - 923, Update the mock_get assertion for the Telegram getUpdates request to parse params["allowed_updates"] with json.loads and compare the result to the expected update-type list, rather than asserting the serialized JSON string and its spacing. Keep the existing offset, timeout, URL, and request timeout checks unchanged.

In @tests/mcp_proxy/services/test_session_context.py around lines 25 - 44, Mark test_should_synthesize_direct_after_tool with @pytest.mark.unit alongside its existing @pytest.mark.parametrize decorator so marker-based selection includes this pure unit test.

In @tests/mcp_proxy/tools/spawn_agent/test_health.py around lines 132 - 147, Mark test_scheduled_health_check_does_not_create_a_sleeping_task with the pytest unit marker by adding @pytest.mark.unit alongside its existing asyncio marker.

In @tests/mcp_proxy/tools/test_communications.py around lines 434 - 459, Add the pytest-asyncio marker to test_send_message_exposes_inline_keyboard_metadata, matching the other async tests in the file so pytest executes its coroutine body and assertions.

In @tests/memory/test_digest.py around lines 551 - 555, Remove the nondeterministic scheduler_checkpoint helper and its await/assertion from the serialization test. Rely on the existing turn_num ordering and digest sentinel assertions, or replace the checkpoint with a deterministic asyncio.Event gate tied to the mocked LLM entry if an explicit scheduling checkpoint is required.

In @tests/servers/test_tool_approvals.py around lines 36 - 44, Add the required @pytest.mark.unit decorator to test_is_builtin_auto_exempt_allows_known_gobby_servers so this policy test is selectable under the unit marker taxonomy, preserving its existing assertions.

In @tests/sessions/test_sessions_processor_unit.py around lines 2121 - 2122, Rename the test class TestExtractNativeTitles to reflect that it covers_filter_session_title_messages, using TestFilterSessionTitleMessages or an equivalent metadata-filter-focused name; update any references consistently.

In @tests/tasks/test_validation.py around lines 2001 - 2006, Update the async test method test_validate_with_validation_criteria_only by adding type annotations for config and mock_llm and an explicit return type, using the appropriate existing project types and the established async test annotation convention.

In @crates/gcode/src/commands/codewiki/text/generation/one_shot.rs around lines 230 - 257, Update generate_with_bounded_retry to honor the retry_after_ms value carried by AiError::RateLimited, using that delay when present instead of the fixed GENERATION_RETRY_BACKOFF value; retain the existing bounded retry behavior and fallback backoff when no server hint is available.

In @crates/gcode/src/commands/codewiki/text/generation/outcome.rs around lines 144 - 217, Extract the duplicated content-classification match from from_tool_loop and from_daemon_agentic into a private classify_content helper accepting optional content, prompt, and GenerationObservability. Have both constructors pass their content and observability to this helper, preserving the existing None, prompt-echo, refusal, cleaning, and rejection behavior exactly.

In @crates/gcode/src/commands/codewiki/text/generation/routing.rs around lines 74 - 106, Update resolve_direct_tier_targets so both db::connect_readonly and ai_source_for_conn failures bind their errors and emit diagnostics unless ctx.quiet is enabled, while preserving the existing default DirectTierTargets fallback. Include the underlying error in each log message so connection and configuration failures can be diagnosed.

In @crates/gcode/src/commands/codewiki/text/generation/routing.rs around lines 64 - 68, Rename the Routing method has_usable_target to all_tiers_usable (or an equivalent name explicitly conveying that every tier is required), and update all callers and references accordingly. Preserve the existing requirement that aggregate, module, and standard each have an api_base.

In @crates/gcode/src/commands/codewiki/text/generation/tool_loop.rs around lines 159 - 173, The direct-generation target resolution chain is duplicated in resolve_aggregate_direct_target and routing::resolve_direct_tier_targets. Extract the shared connect_readonly, ai_source_for_conn, and resolve_direct_generation_target logic with its silent default handling into a pub(super) helper in routing.rs, then update both call sites to use that helper.

In @crates/gcode/src/commands/codewiki/text/generation/tool_loop.rs around lines 138 - 157, Preserve the existing unavailable-result behavior while binding and logging errors from CodewikiToolExecutor::new, run_tool_loop, daemon_agentic_chat, and DirectChatTransport::new before degrading. Route diagnostics through the available context logger, honoring ctx.quiet, and include the original error details so transport, executor, and model failures remain distinguishable.

In @crates/gwiki/src/commands/search.rs around lines 256 - 272, The search evidence construction around page_excluded_from_surfaces and std::fs::read_to_string should skip stale hits whose files no longer exist instead of propagating a WikiError::Io. Detect a missing-file NotFound result for the current page and continue to the next search result, while preserving existing error propagation for other I/O failures and keeping valid evidence processing unchanged.

In @crates/gwiki/src/page_version.rs around lines 64 - 70, Update yaml_closing_delimiter_start to use the shared frontmatter delimiter parsing rules instead of exact “---\n” checks, including for the opening and closing delimiters. Preserve the existing Option return behavior while recognizing CRLF line endings and delimiters with permitted surrounding whitespace so stamp_generated_page updates the existing frontmatter.

In @docs/guides/telegram.md around lines 12 - 15, Update the opening summary under “Quick path: a private DM bot” to state that ownership is enrolled only when the exact private /start command is received, matching the enforced behavior described later in the guide.

In @src/gobby/communications/adapters/telegram.py around lines 456 - 469, The Telegram command synchronization in initialize must not prevent the channel from starting when setMyCommands fails. Update the setMyCommands request and response validation around _raise_for_status_with_redacted_token to catch synchronization errors, log a warning, and continue initialization; preserve successful synchronization logging and keep getMe/connectivity failures fail-fast.

In @src/gobby/communications/adapters/telegram.py at line 436, Wrap the long assignment in the Telegram adapter by formatting the call to resolve_telegram_proxy_url across multiple lines, preserving the existing arguments and behavior while keeping each line within Ruff’s 100-character limit.

In @src/gobby/communications/manager.py around lines 342 - 348, Update the access-control flow in the surrounding channel message handler to call evaluate_group_message only after confirming channel.channel_type is "telegram". Preserve the existing authorization rejection and passive_context behavior for Telegram group messages, while allowing non-Telegram messages to skip group evaluation entirely.

In @src/gobby/install/bundled_content_manifest.json at line 72, Update the bundled content manifest to remove the stale entries for rules/build/build-agent-safety.yaml and skills/build/SKILL.md, unless those files are restored to the bundle. Ensure the manifest references only files that are actually included in the bundled content.

In @src/gobby/install/shared/skills/tasks/SKILL.md around lines 40 - 50, Add language identifiers to all four fenced code blocks in the task skill documentation: use python for executable call_tool examples and text for intentionally pseudocode blocks, including the blocks near the referenced sections. Ensure every fence satisfies markdownlint MD040.

In @src/gobby/install/shared/skills/tasks/SKILL.md around lines 16 - 18, Update the task lifecycle instructions in SKILL.md to require fetching a known unleased schema with get_tool_schema("gobby-tasks-ops", "<tool>") before the first gobby-tasks-ops call, unless that server is explicitly exempt. Preserve the existing gobby-tasks schema lookup guidance and autonomous review-transition usage.

In @src/gobby/install/shared/skills/tasks/references/creation.md around lines 55 - 65, Add an explicit language identifier to the fenced code blocks containing the call_tool examples, including the examples around the create_task snippet and lines 70–81. Use python for executable Python examples and text for pseudocode, preserving the example contents unchanged.

In @src/gobby/install/shared/skills/tasks/references/evidence-provider-recovery.md around lines 34 - 42, Add a language identifier to the fenced code example containing the call_tool invocation, using python or text so markdownlint MD040 passes. Leave the example content unchanged.

In @src/gobby/install/shared/skills/tasks/references/no-work-closures.md around lines 17 - 25, Add a language identifier to the fenced code example containing the call_tool invocation, using python or text to satisfy markdownlint MD040 while leaving the example content unchanged.

In @src/gobby/install/shared/skills/tasks/references/review-flows.md around lines 9 - 14, Add language identifiers to the fenced code examples in the review-transition documentation, including the examples around submit_for_review and the other two referenced sections. Use python for executable call_tool examples and text where the block is non-code, preserving their existing contents.

In @src/gobby/mcp_proxy/tools/memory_recall.py around lines 28 - 43, Wrap the queue lookups in get_recall_memories, including queue.get and queue.pending, with the existing try/except used around _retrieve_memories. On lookup exceptions, return the standard failure payload containing recall_request_id and the error details instead of allowing the exception to propagate.

In @src/gobby/runner_broadcasting.py around lines 344 - 351, The_format_cron_run_message function embeds unbounded run.output and run.error, allowing chat messages to exceed channel limits and fail permanently. Truncate each included output or error to a safe maximum before constructing the notification, while preserving the existing status text and concise-message behavior.

In @src/gobby/runner_init/orchestration.py around lines 544 - 556, Track whether memory-scope enumeration via list_dream_scopes completed successfully, distinguishing a genuine empty result from an exception that leaves memory_scopes empty. When enumeration fails, skip register_codewiki_nightly_crons so its stale-job pruning cannot disable existing project cron jobs; continue registering normally for successful enumeration, including the valid no-projects case.

In @src/gobby/search/backends/embedding.py at line 137, Update the embedding-index completion log in the relevant indexing method to pass the indexed item count as structured logging context rather than interpolating it into the message arguments. Preserve the existing debug level and completion message while using the logger’s supported contextual field convention.

In @src/gobby/storage/session_lifecycle.py at line 235, Update the debug log in the session pruning flow to record skipped as structured logging context rather than interpolating it into the message string. Preserve the existing message meaning and use the logger’s supported context-argument convention.

In @src/gobby/workflows/condition_helpers.py around lines 32 - 33, Remove the local MEMORY_RECALL_DELIVERIES_VARIABLE definition in condition_helpers.py and import and reuse the shared constant from memory_recall_delivery.py, ensuring all existing references continue using that single source of truth.

In @tests/communications/test_identities.py around lines 246 - 248, Add @pytest.mark.asyncio and the project’s unit-test category marker directly above test_inbound_access_policy_rejection_logs_at_debug, preserving the existing async test implementation.

In @tests/communications/test_responder.py around lines 227 - 236, In the responder logging test, bind the expected “Ignoring group message…” text to a local variable before filtering caplog.records, use that variable in the filter predicate, and remove the duplicate records[0].getMessage() assertion while preserving the count and log-level checks.

In @tests/workflows/test_memory_recall_gate_rules.py around lines 61 - 66, Update the fixture setup around the workflow_definitions mutations to execute both the disable-all and per-rule enable updates within a Hub database transaction. In the rule loop, replace the %s parameter marker with the required $1 placeholder while continuing to bind rule_name as the parameter.

In @tests/workflows/test_skill_loaded_call_tool_path.py around lines 136 - 140, Add the appropriate integration marker to test_oversized_get_skill_wrapper_result_survives_codex_normalization_and_compaction so marker-based selection categorizes it correctly, while preserving the existing asyncio marker and test behavior.

In @tests/workflows/test_skill_loaded_call_tool_path.py around lines 157 - 162, Replace the untyped callback lambda in the get_skill registry registration with a typed function or callable that annotates its name parameter and oversized_skill return value, while preserving the existing callback behavior.

In @tests/workflows/test_step_enforcement.py around lines 347 - 350, Update the assertions around response.reason in the step-enforcement test to first verify that the exact “During this skill-loading step:” delimiter is present, then extract guidance using the existing split logic. Keep the existing assertions preventing list_tools and get_tool_schema while still requiring the plan-review get_skill call.

In @src/gobby/adapters/codex_impl/execution_chain.py around lines 507 - 532, Replace the ambiguity tracking sets used by the pending cell/session flow with an insertion-ordered bounded structure, such as a dict keyed by execution key, and update membership/addition/eviction logic around _ambiguous_cells, _ambiguous_sessions, and this pending-correlation block. Evict the oldest ambiguity entry deterministically rather than calling set.pop(), while preserving the existing max-size and early-return behavior.

In @src/gobby/adapters/codex_impl/execution_chain.py around lines 441 - 444, Update the execution correlation logic around the assignments to data["_original_tool_name"] and data["tool_input"] so a missing execution.literal_command does not produce {"command": None}; omit the command key or use the existing unknown-result shape when the command is absent, while preserving the current Bash payload for present commands.

In @src/gobby/adapters/codex_impl/execution_chain.py around lines 375 - 391, Refactor the results selection in resolve_output to replace the walrus-based conditional expression with an explicit if/else block: when execution.direct is true, extract the direct terminal result and use it only when non-None; otherwise fall back to decoded_exec_results(output). Preserve the existing result values and subsequent terminal_results filtering.

In @src/gobby/adapters/codex_impl/execution_chain.py around lines 256 - 266, Update extract_yielded_cell_id to scan every text block and non-blank line until a valid yielded-cell marker is found, rather than returning None after the first non-blank line. Preserve fail-closed behavior by returning None when no marker is found or when the marker is ambiguous, and keep the existing_iter_output_text and _YIELDED_CELL_RE usage.

In @src/gobby/cli/tasks/crud.py around lines 190 - 199, Update the create_task_impl call and the corresponding implementation call near the additionally affected location to pass all arguments as keyword arguments, especially the same-typed fields such as title, validation_criteria, task_type, and project_ref. Preserve the existing values and ordering semantics while preventing future parameter insertions from causing positional misbinding.

In @src/gobby/code_index/sync_worker.py around lines 189 - 196, The gateway SyncCircuitBreaker is configured with a hardcoded failure_threshold of 1, causing one transient daemon failure to pause all gcode projections. Update the gateway_breaker configuration in the sync worker to use a configurable threshold, or at least a threshold of 2, while preserving the existing backoff settings.

In @src/gobby/code_index/sync_worker.py around lines 70 - 74, Update the sync attempt flow around _breakers_allow_attempt to track which active breakers successfully consumed a probe, then resolve every armed breaker exactly once on every terminal path, including daemon-config failures, G-code timeout/unavailable errors, graph failures, and per-file errors. Record success or failure for each consumed breaker according to the attempt outcome, while preserving existing gateway/vector/graph outcome semantics and avoiding resolution for breakers that were not armed.

In @src/gobby/hooks/event_handlers/_tool.py around lines 57 - 62, Update the condition in the event handler around validate_functions_exec_wrapper to use the exported FUNCTIONS_EXEC_NAMES constant instead of the inline {"exec", "functions.exec"} set, preserving the existing Codex source check and blocking behavior.

In @src/gobby/install/shared/prompts/validation/validate.md around lines 104 - 115, Update the validation prompt’s earlier instruction that refers to “invalid or pending” verdicts so it only refers to the supported “invalid” status. Keep the existing guidance for populating issues unchanged and ensure the status wording matches the valid/invalid contract described in the final format specification.

In @src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py at line 361, Replace the runtime asserts in the lifecycle-close tool, including the checks for resolved_session_id, receipt_packet, admission, and evidence, with explicit validation that returns blocked(...) or raises RuntimeError before validator calls. Preserve the existing success flow while ensuring missing values produce structured errors even when Python runs with -O.

In @src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py at line 337, Remove the redundant should_skip alias in the close flow and use skip_leaf_checks directly in the guards around the leaf validation logic. Simplify the final status expression to use validation_status or the skipped/valid result based on skip_leaf_checks, preserving the existing behavior.

In @src/gobby/storage/migrations/342_task_validation_epoch.sql around lines 17 - 28, Update the verification_receipts normalized_outcome constraint recreation in migration 342_task_validation_epoch.sql to add the CHECK constraint with NOT VALID, then validate it in a separate VALIDATE CONSTRAINT step after the provisional-to-pending update. Preserve the existing allowed outcome values and constraint name.

In @src/gobby/storage/tasks/_manager.py around lines 355 - 364, Update the task update flow around require_validation_criteria and_update_task_metadata so the validator’s normalized return value is assigned to the effective validation criteria and persisted. Preserve the existing UNSET handling and task-type validation behavior, ensuring whitespace-padded criteria are stored in normalized form.

In @src/gobby/sync/github_issue_sync.py around lines 106 - 109, Update the task lookup query in the GitHub issue sync flow to use the hub database’s positional $1, $2, and $3 placeholders instead of %s, while preserving the existing parameter order and query behavior.

In @src/gobby/sync/linear_task_ops.py around lines 279 - 292, Update the two modified task lookup queries in the surrounding sync operation to use the hub database’s $N placeholder dialect instead of %s, while preserving their existing parameter ordering and lookup conditions.

In @src/gobby/tasks/criteria_contract.py around lines 33 - 67, Update split_validation_criteria so that once a list marker is detected, free-text accumulated before the first marker is discarded rather than appended as a criterion. Preserve the existing handling of list items, continuation lines, blank lines, and non-list prose when no marker is present.

In @src/gobby/tasks/evidence_admission.py around lines 61 - 71, Update_criteria_accept_actor_attestation to avoid accepting criteria solely through naive substring matches, especially when matched phrases are negated. Prefer the existing criteria contract’s explicit structured flag or tag for actor-attestation/manual-review acceptance; if unavailable, add proximity-aware checks that reject phrases preceded by negation terms such as “not,” “cannot,” or “insufficient,” while preserving acceptance for clearly affirmative criteria.

In @src/gobby/tasks/task_state_evidence.py at line 17, The excerpt, digest, and length logic in task-state evidence duplicates verification_receipts._bounded_output. Extract or reuse a shared helper, preferably through a common storage output-bounds module or a public bounded-output symbol, then update both call sites to use it while preserving the existing truncation, digest, and length behavior.

In @src/gobby/workflows/state_manager.py around lines 709 - 713, Update the task update query in the state manager’s epoch-update flow to use PostgreSQL-style $N placeholders instead of %s markers, ensuring the parameter references match the existing (now, task_id) argument order.

In @src/gobby/workflows/verification_receipt_ingestion.py around lines 184 - 187, Update the task lookup in the verification receipt ingestion flow to use the hub database’s $N placeholder dialect instead of %s, preserving the existing task_id and project_id parameter ordering and query behavior.

In @tests/cli/tasks/test_task_id_resolution.py around lines 299 - 302, Add an explicit assertion in the hash-format close-task test that the #3 input is passed to resolve_task_reference, while retaining the existing contract-failure, exit-code, output, and close_task-not-called assertions. Anchor the new verification to the test’s runner.invoke call and the resolve_task_reference mock.

In @tests/dispatch/test_dispatcher.py around lines 2138 - 2141, Replace the untyped lambda passed to monkeypatch.setattr for_prepare_plan_adversary_evidence with a local helper function defined before the monkeypatch. Add explicit parameter and return type annotations matching the expected kwargs and tuple result, then use that helper as the replacement while preserving its current behavior.

In @tests/integration/test_hub_query.py around lines 325 - 335, Update the INSERT query in the task setup to use numbered Hub placeholders $1 through $4 instead of %s, while preserving the existing parameter order and timestamp expressions.

In @tests/mcp_proxy/tools/test_tasks_create_coverage.py around lines 531 - 540, Strengthen the create_task test around registry.call so it verifies the explicit validation_criteria is forwarded unchanged through create_task_with_decomposition. Inspect the resulting task or relevant mock call arguments and assert the value is exactly “Test task completion is observable.” while preserving the existing update_task not-called assertion.

In @tests/mcp_proxy/tools/test_tasks_lifecycle_coverage.py around lines 465 - 467, Update the failure assertions in the task lifecycle test so the configured link_commit failure is verified by asserting mock_task_manager.link_commit was called once (optionally with expected arguments), while retaining close_task.assert_not_called() and the existing invalid_commit_sha result checks.

In @tests/servers/routes/test_tasks_routes.py around lines 444 - 448, Add the appropriate pytest marker, preferably @pytest.mark.integration, to test_create_requires_validation_criteria so it participates in selective test runs consistently with the repository’s test-marker guidelines.

In @tests/sessions/test_codex_nested_exec_outcomes.py around lines 383 - 393, Update the parametrization for this test to include an expected outcomes value for each tool_input case, then replace the tool_input branch in the test body with a direct assertion against that parameter. Preserve the existing expected outcome for the two recognized exec_command inputs and use the empty-outcomes expectation for other inputs, so every parameter explicitly defines its result.

In @tests/storage/test_storage_tasks.py around lines 58 - 60, Introduce a shared test constant or fixture for the repeated non-empty validation criteria, preferably in tests/conftest.py, and replace the literal in this create_task call and the other occurrences noted in the comment, including sibling test files where applicable. Reuse that shared value consistently without changing task behavior.

In @tests/tasks/contract_validator.py around lines 51 - 56, Update validate_task to detect and handle validation_criteria supplied positionally before injecting kwargs["validation_criteria"], preserving the caller-provided value and preventing duplicate argument errors when delegating to super().validate_task. Keep the existing default criterion behavior for calls that provide neither positional nor keyword criteria, and continue synchronizing self._contract_llm.criteria.

In @tests/workflows/test_condition_helpers.py around lines 42 - 47, Update the task helper containing the manager.create_task call so it inserts the default validation_criteria into kwargs only when the caller has not supplied one, then expand kwargs without passing validation_criteria separately. Preserve caller-provided test-specific criteria.

In @tests/workflows/test_memory_lifecycle_rules.py around lines 143 - 163, Update the new test methods test_event_and_effect and test_matches_plan_boundaries to annotate db as HubDatabase and manager as LocalWorkflowDefinitionManager, preserving the existing return and parameter annotations.
