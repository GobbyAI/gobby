# CodeRabbit Fixes: Memory, Projects, Search, and Storage Lifecycle

Memory and project lifecycle, vector/search behavior, and related durable storage fixes.

Unresolved original findings: **57**

Original finding IDs: 195-200, 218, 220, 239-244, 260, 273-280, 290-292, 333-343, 348-358, 374-377, 379, 608, 620, 721, 747

## Finding #195

In @src/gobby/memory/manager.py at line 84, Replace the Any | None annotation for project_write_fence in the relevant constructor and usages with the existing VectorWriteFence/ExclusiveFence-style Protocol, reusing the established fenced_vector_store.VectorWriteFence interface so strict mypy can validate compatibility throughout the memory manager.

## Finding #196

In @src/gobby/memory/services/knowledge_graph/maintenance.py around lines 215 - 218, Update the purge flow around remove_orphaned_entities to use a non-catching orphan-cleanup variant that propagates connection and query failures instead of returning 0. Add or invoke a strict method alongside the existing cleanup behavior, ensuring purge failures remain visible and retryable after Memory node removal.

## Finding #197

In @src/gobby/projects/fenced_vector_store.py around lines 62 - 80, The batch_upsert method currently uses global_writer for multi-project batches, unnecessarily blocking on unrelated project purges. Replace that fallback with admission scoped to every distinct project_id: acquire writer() contexts for all project IDs in the batch and keep them held while calling_inner.batch_upsert; preserve the existing global behavior only for batches without project IDs, if required by the fence contract.

## Finding #198

In @src/gobby/projects/purge.py around lines 233 - 264, The purge handler processes candidates sequentially, allowing large runs to exceed practical cron durations. Update create_project_purge_handler’s_handler to purge projects with bounded concurrency, using an asyncio.Semaphore or equivalent limit, while preserving the existing success, protected, failed, and count aggregation behavior.

## Finding #199

In @src/gobby/projects/write_fence.py around lines 97 - 118, Update the drain-wait error handling in exclusive() so self._exclusive is removed and waiters are notified for every exception that occurs before yield, including cancellation, while preserving ProjectWriteDrainTimeout conversion for TimeoutError. Ensure cleanup also handles removal safely and does not allow the project to remain permanently marked exclusive.

## Finding #200

In @src/gobby/projects/write_fence.py around lines 39 - 56, The project lookup in the write-admission path currently performs a synchronous DB read while holding self._condition. Resolve project_id via self._project_lookup before entering the async condition block, then reuse that result inside the validation branch while preserving rejection for missing or deleted projects and the existing admission behavior.

## Finding #218

In @src/gobby/storage/cron.py around lines 623 - 636, Update the cron parking logic in the transaction to replace the pre-update SELECT and separate UPDATE with a single UPDATE ... RETURNING * statement that sets enabled to FALSE and next_run_at to NULL, then build CronJob objects from the returned post-update rows.

## Finding #220

In @src/gobby/wiki/prune_job.py around lines 102 - 128, Replace the "system" fallback in register_wiki_prune_cron when calling cron_storage.create_job with a schema-valid project identifier. Use a real project UUID or update the job/storage design to support a nullable or sentinel-safe system-job project reference, while preserving project_id when provided.

## Finding #239

In @tests/projects/test_purge_components.py around lines 216 - 221, Replace the inline snapshot supplier lambda in the ProjectFencedVectorStore rebuild_from_supplier call with a small named function that appends "snapshot" to events and explicitly returns an empty list. Keep the existing text embedding lambda and rebuild behavior unchanged.

## Finding #240

In @tests/projects/test_purge_service.py around lines 262 - 291, The test test_daily_handler_isolates_failures_and_bounds_id_lists does not exercise the handler’s list-size limit. Increase the generated candidate count and expected successful/failed results so at least one returned ID list exceeds the cap, or assert the configured cap explicitly; retain the failure-isolation assertions and ensure the test fails if bounding is removed.

## Finding #241

In @tests/projects/test_purge_service.py around lines 54 - 60, Update FakeTransaction.execute to handle statements without “FROM ” explicitly instead of indexing an unconditional split result. Preserve the existing table extraction and project hard-delete behavior for DELETE FROM statements, while raising a clear error for unsupported SQL statements such as UPDATE.

## Finding #242

In @tests/projects/test_write_fence.py around lines 62 - 64, Bound all synchronization waits in the affected tests, including wait_for_exclusive_claim and the writer_entered, purge_task, and background-task gather waits, with finite asyncio.wait_for timeouts (or an equivalent pytest timeout). Ensure timeout failures propagate as test failures instead of allowing CI to hang.

## Finding #243

In @tests/projects/test_write_fence.py around lines 1 - 18, Add a module-level pytestmark assignment after the import block in tests/projects/test_write_fence.py, setting this suite’s marker to pytest.mark.unit. Keep the existing imports and test behavior unchanged so marker-based unit test selection includes all tests in the module.

## Finding #244

In @tests/projects/test_write_fence.py around lines 107 - 108, Replace the direct _condition/_exclusive wait in the test with the shared wait_for_exclusive_claim helper, and make that helper importable from a common tests/projects/fence_helpers.py location if needed. Update the duplicate waits in tests/github_triage/test_issue_index.py, tests/mcp_proxy/test_semantic_search.py, and tests/memory/test_indexing_service.py to use the same helper, keeping ProjectWriteFence private-state access centralized.

## Finding #260

In @tests/wiki/test_prune_job.py around lines 57 - 177, The new tests in test_register_wiki_prune_cron_creates_hourly_system_job, test_registered_wiki_prune_handler_is_callable, test_register_wiki_prune_cron_preserves_toggle_and_wakes_only_enabled_rows, test_wiki_prune_handler_reports_command_failure, and test_wiki_prune_handler_reports_timeout_and_unavailable should be marked with @pytest.mark.unit. For async tests, add the unit marker alongside the existing asyncio marker.

## Finding #273

In @src/gobby/memory/dream/related.py around lines 211 - 239, Update run_call so semaphore acquisition occurs outside the asyncio.timeout scope, leaving only the awaited operation task subject to RELATED_EVIDENCE_CALL_TIMEOUT_SECONDS. On TimeoutError, explicitly cancel the created task and await it with cancellation suppressed or handled before returning_CallOutcome(timed_out=True), while preserving the existing failure handling and task naming.

## Finding #274

In @src/gobby/memory/dream/service.py around lines 778 - 835, Bound the dry-run preview around the candidate pagination loop in the method containing list_dream_candidate_ids and build_raw_plan by applying a configurable maximum candidate count or page count, preserving the single-request preview limit. Keep collecting only up to the configured action sample for persisted plan data, record the total action count and whether results were truncated, and expose the truncation metadata in the returned summary/plan instead of storing all_actions unbounded.

## Finding #275

In @src/gobby/memory/services/maintenance.py at line 33, Update get_stats to log the exception when vector-count retrieval fails, immediately before assigning the fallback vector_count = -1. Preserve the existing fallback behavior while including the caught exception and useful context in the service’s established logging mechanism.

## Finding #276

In @src/gobby/memory/vectorstore_client.py around lines 100 - 112, Update the local branch of the client operation helper to calculate and validate the same timeout budget before offloading, then wrap the asyncio.to_thread call in asyncio.timeout(budget). Preserve the existing remote timeout behavior and ensure non-positive budgets raise the same TimeoutError.

## Finding #277

In @src/gobby/memory/vectorstore_maintenance.py around lines 174 - 184, Move the stale_delete_strategy validation to the start of prepare_collection_for_rebuild, before any lock acquisition, temporary collection creation, or rebuild-plan preparation. Keep the existing accepted values, "precompute" and "streaming", and remove the later duplicate check near batch_size and incoming_ids.

## Finding #278

In @src/gobby/memory/vectorstore_maintenance.py around lines 165 - 212, Prevent rebuild deadlocks caused by repeated initialization during batch writes: reuse the client returned by_ensure_initialized() throughout rebuild() instead of letting batch_upsert() call _ensure_initialized() while _collection_lifecycle_lock is held. Update both batch_upsert() calls in the rebuild loop and the final batch flush to use the initialized client or an equivalent no-reinitialization path, while preserving existing target collection selection.

## Finding #279

In @src/gobby/memory/vectorstore_queries.py around lines 136 - 161, The vector query batching in the shown method hardcodes the batch size 50 in both the range step and slice window. Define a module-level constant such as_STORED_VECTOR_BATCH_SIZE and use it for both expressions, preserving the existing batching behavior while making the size adjustable.

## Finding #280

In @src/gobby/projects/vector_cleanup.py around lines 37 - 43, Replace the client-type dispatch in_managed_physical_collections with a VectorStore list_collections() helper that delegates through_call_client, preserving request timeout handling. Update both this method and the corresponding collection-listing logic in the embedding installer to use the helper instead of inspecting AsyncQdrantClient directly.

## Finding #290

In @tests/memory/test_dream.py around lines 285 - 364, Update both size calculations in test_batch_split_guard and test_single_item_oversize_dispatches_intact to use the planner’s_render_candidates_json renderer instead of directly calling json.dumps with hardcoded formatting options. Add the import for _render_candidates_json and preserve the existing batch-size and oversize assertions using the renderer’s returned JSON length.

## Finding #291

In @tests/memory/test_dream_related.py at line 34, Update the pytest markers for test_keyword_scope_sql_contract, test_async_keyword_and_hydration_use_dedicated_statements, and test_keyword_global_not_starved, along with the other postgres_db-backed tests, so they use the integration marker instead of pytest.mark.unit. Keep genuinely unit-only tests marked as unit.

## Finding #292

In @tests/memory/test_dream_related.py around lines 299 - 319, Update test_vector_floor_boundaries to derive the expected retained score from VECTOR_EVIDENCE_MIN_SCORE plus the same 0.05 offset used by the “above” fixture, rather than asserting the binary-float literal 0.39999999999999997. Keep the existing filtering and result structure unchanged.

## Finding #333

In @src/gobby/memory/dream/apply.py around lines 405 - 432, Update the transactional action dispatch around _apply_fenced_action so refresh actions without content follow the same cursor-advance behavior as_apply_action_legacy, while content-bearing refresh actions continue through the fenced path; then revise the stale defensive comment to describe the remaining fallback cases.

## Finding #334

In @src/gobby/memory/dream/apply.py around lines 601 - 606, Remove the unreachable membership check and its ValueError after the action_name cast in the fenced dream action handling, or move validation to action.action before casting. Keep the existing Literal cast and supported action set behavior, avoiding a redundant post-cast guard.

## Finding #335

In @src/gobby/memory/dream/models.py at line 81, Remove dream_due_version from the dictionary returned by to_prompt_dict(), keeping the internal optimistic-concurrency field available on the model but excluding it from planner prompt data unless an existing prompt template explicitly requires it.

## Finding #336

In @src/gobby/memory/dream/protocols.py around lines 42 - 43, Align the protocol contract with its callers: update _apply_fenced_action,_advance_cursor, and the revert path in apply.py to invoke memory_manager.notify_memory_changed() directly instead of probing with getattr. If implementations are intentionally allowed to omit this method, remove it from the required protocol contract instead.

## Finding #337

In @src/gobby/memory/dream/storage.py around lines 490 - 499, Replace the bare else in the action-handling chain with an explicit `elif action == "promote"` branch containing the existing promotion update. Add a trailing `raise ValueError(...)` for any unsupported action so new values cannot silently promote memories.

## Finding #338

In @src/gobby/memory/dream/storage.py around lines 457 - 469, Update the duplicate query in the refresh flow to filter for active memories by adding the existing soft-delete condition (`deleted_at IS NULL`) alongside the current content, project, scope, and ID predicates, so soft-hidden rows do not block refresh.

## Finding #339

In @src/gobby/memory/services/crossref.py around lines 213 - 238, Replace per-candidate calls to _current_stored_similarity with one batched stored-vector lookup for all candidate_ids before the cursor/transaction block, then reuse the returned scores while rebuilding cross-references. Preserve fallback behavior when stored-vector search is unsupported or fails, and update the candidate-processing logic to consume the batch results without issuing additional vector-store queries.

## Finding #340

In @src/gobby/memory/services/crossref.py around lines 167 - 211, Update the crossref rebuild logic around the DELETE query and insertion loop: limit deletion to rows where memory.id is the source, since the loop only recreates source-owned edges. Preserve inbound crossrefs so get_related can continue reading relationships created by other memories.

## Finding #341

In @src/gobby/memory/services/indexing.py around lines 581 - 607, Optimize _sweep_rebuild_snapshot to avoid one database transaction per memory by applying snapshot-clearing and reindex-needed updates in set-based, page-sized batches keyed by the existing identity tuple. Preserve CAS semantics and handle the boolean result of mark_vector_snapshot_reindexed so any failed CAS rows are explicitly re-marked for reindexing, while retaining rowless cleanup behavior.

## Finding #342

In @src/gobby/memory/services/lifecycle.py around lines 524 - 572, The reconcile flow currently holds a process-wide advisory lock during embedding and vector-store I/O, serializing unrelated memory writes. Update reconcile and the analogous purge_secondary_indices path to use the existing per-memory lock-key strategy from _memory_lock_key instead of MEMORY_PROJECTION_FENCE_LOCK_KEY, preserving synchronization for the same memory while allowing unrelated memories to proceed.

## Finding #343

In @src/gobby/memory/services/lifecycle.py around lines 699 - 710, Update restore_memory_indices around the _run_storage(self.storage.get_memory, ...) call to catch ValueError for missing or hard-deleted rows and return False, matching reconcile_memory_indices behavior. Preserve the existing field comparisons and return semantics for successfully retrieved memories.

## Finding #348

In @src/gobby/storage/memories_crud.py around lines 229 - 232, Keep the parameterized query in the locked-row retrieval flow unchanged, and add a brief comment adjacent to the existing nosec marker explaining that the interpolated SQL contains only generated %s placeholders while locked_ids values are passed as bound parameters.

## Finding #349

In @src/gobby/storage/memories_crud.py around lines 151 - 156, Resolve the final_memory_id, including deduplication and collision handling, before acquiring any advisory locks in the memory write flow. Update the locking logic around the initial lock set and the later final_memory_id handling so the complete union of supersedes_ids, current_memory_id, and resolved final_memory_id is acquired exactly once in sorted order before row work begins; remove the later out-of-order lock acquisition.

## Finding #350

In @src/gobby/storage/memories_crud.py around lines 714 - 737, Update mark_vector_snapshot_reindexed to capture cursor.rowcount inside the self.db.transaction() context, store it in a local result, and use that value for notify_changed() and the boolean return after the transaction exits.

## Finding #351

In @src/gobby/storage/memories_dreams.py at line 94, Standardize change notifications in mark_project_memories_due, mark_global_memories_due, and purge_dream_hidden by replacing their private_notify_listeners() calls with the public notify_changed() method, matching the updated paths and preserving the existing notification timing.

## Finding #352

In @src/gobby/storage/memories_dreams.py around lines 158 - 163, Update the restore/re-queue SQL executed in the memories flow to set graph_attempts = 0 alongside graph_processed = FALSE and graph_status = 'pending'. Match the reset behavior used by the comparable lifecycle.py stamp path while preserving the existing fields and parameters.

## Finding #353

In @src/gobby/storage/migrations/334_verification_receipts.sql around lines 5 - 7, Document in the migration why verification_receipts.session_id intentionally has no foreign-key constraint, noting that receipts must survive session deletion and therefore may contain dangling session UUIDs. Keep session_id NOT NULL and the existing task_id foreign key unchanged.

## Finding #354

In @src/gobby/storage/migrations/335_memories_dream_due_version.sql around lines 1 - 2, Change the dream_due_version column definition in migration 335_memories_dream_due_version.sql from INTEGER to BIGINT, preserving its NOT NULL constraint and DEFAULT 0.

## Finding #355

In @src/gobby/storage/model_metadata.py around lines 45 - 113, Reset the module-level_stale_warning_emitted flag to False after populate() successfully commits refreshed model metadata, before returning the inserted count. Keep the flag unchanged when models are unavailable or the transaction fails, so subsequent stale-cache cycles can emit a warning.

## Finding #356

In @src/gobby/storage/verification_receipts.py around lines 24 - 30, Update_bounded_output to treat an empty output string the same as None, returning all-None values before encoding or hashing; preserve the existing bounded excerpt and digest behavior for non-empty output.

## Finding #357

In @src/gobby/storage/verification_receipts.py around lines 254 - 277, Reflow the overlong SQL lines in the verification-receipt upsert statement, especially the conflict-condition line and output_sha256 assignment, so every source line stays within Ruff’s 100-character limit. Preserve the existing SQL behavior and formatting alignment around the outcome update and output field assignments.

## Finding #358

In @src/gobby/storage/verification_receipts.py around lines 404 - 460, Update assign_unassigned to return the rows produced by the UPDATE directly: execute UPDATE ... RETURNING * inside the existing transaction, capture those rows, and build VerificationReceipt objects from them after the transaction. Remove the post-commit fetchall by ID so the method returns exactly the state committed by this operation.

## Finding #374

In @tests/memory/test_create_supersedes.py around lines 36 - 37, Add the required pytest category markers throughout tests/memory/test_create_supersedes.py: mark all real-Postgres tests as integration, and additionally mark fencing or concurrency tests with slow. Apply markers consistently to each relevant test, including test_auto_mark_due and the referenced cases, without changing their behavior.

## Finding #375

In @tests/memory/test_create_supersedes.py around lines 1113 - 1147, Replace the timing-based assertions in test_supersedes_row_lock_fencing with deterministic synchronization: use explicit events or an equivalent observable signal to confirm restore_memory is blocked while the replacement purge holds the row lock, then release the purge and await both tasks without sub-100 ms or fixed cleanup timeouts. Preserve the test’s verification that restore succeeds and old.id is no longer deleted.

## Finding #376

In @tests/memory/test_create_supersedes.py around lines 770 - 809, Update test_supersedes_rollback_on_failure to use the concrete psycopg exception type in pytest.raises, and wrap the failure-inducing manager.create_memory call in a finally block that drops fail_supersession_test_trigger and fail_supersession_test from temp_db. Ensure cleanup runs whether the operation raises as expected or unexpectedly, before the state assertions execute.

## Finding #377

In @tests/storage/test_memories_dreams.py around lines 28 - 30, Add the repository’s unit-test marker decorator to test_mark_memories_due so it is categorized as a unit test and can be selected with the unit test suite.

## Finding #379

In @tests/storage/test_memories_dreams.py at line 12, Add concrete type hints for the untyped db and temp_db parameters in_insert_project and the other affected functions around the referenced lines, using the appropriate database or fixture protocol types while preserving their existing return annotations.

## Finding #608

In @src/gobby/storage/config_store.py around lines 569 - 573, Move the stored-value lookup and secret-name resolution into the existing mutation transaction/lock that performs the config-row deletion, using the transaction’s consistent read before deleting. Update the method containing `stored_value` and `config_key_to_secret_name` so concurrent set_secret changes cannot occur between reference resolution and removal; preserve the fallback mapping for non-secret or empty references.

## Finding #620

In @tests/memory/test_knowledge_graph.py around lines 512 - 518, Add @pytest.mark.asyncio to both new async tests, including test_add_to_graph_maps_valid_duplicate_malformed_and_unknown_relation_ids and the async test beginning at the second referenced location, matching neighboring async tests in the class.

## Finding #721

In @tests/memory/test_digest.py around lines 551 - 555, Remove the nondeterministic scheduler_checkpoint helper and its await/assertion from the serialization test. Rely on the existing turn_num ordering and digest sentinel assertions, or replace the checkpoint with a deterministic asyncio.Event gate tied to the mocked LLM entry if an explicit scheduling checkpoint is required.

## Finding #747

In @src/gobby/search/backends/embedding.py at line 137, Update the embedding-index completion log in the relevant indexing method to pass the indexed item count as structured logging context rather than interpolating it into the message arguments. Preserve the existing debug level and completion message while using the logger’s supported contextual field convention.
