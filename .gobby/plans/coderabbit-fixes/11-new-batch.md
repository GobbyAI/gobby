Fix the following issues. The issues can be from different files or can overlap on same lines in one file.

Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @src/gobby/adapters/codex_impl/execution_chain.py around lines 501 - 533, Update _set_pending to maintain _ambiguous_cells and _ambiguous_sessions as bounded FIFO ordered sets, such as dict[str, None], so eviction removes the oldest entry rather than an arbitrary one. Replace set-specific membership/add/pop usage with equivalent ordered-dict operations, preserving the existing ambiguity checks and ensuring the currently processed key is not evicted before it is recorded.

In @src/gobby/adapters/codex_impl/execution_chain.py around lines 256 - 266, Update extract_yielded_cell_id so a non-matching first non-blank line does not immediately return None; continue scanning subsequent lines and text blocks from_iter_output_text(output) until _YIELDED_CELL_RE matches, then return the captured cell ID, otherwise return None after all content is checked.

In @src/gobby/cli/tasks/ai.py around lines 180 - 188, Wrap the evidence admission and receipt packet construction flow in the same try/except pattern used by the surrounding CLI blocks, covering both admit_task_evidence and build_verification_receipt_packet. Convert any raised exception into a click.ClickException with the established contextual message, while preserving the existing receipt_store and successful packet-building behavior.

In @src/gobby/code_index/sync_worker.py around lines 456 - 462, The graph-sync cascade does not record gateway breaker failures for generic transport errors. Update the graph-sync exception handling around GcodeTimeoutError and GcodeUnavailableError to call gateway_breaker.record_failure() when the breaker exists, matching the vector-sync treatment, while preserving the existing pending-work and return behavior for each handler.

In @src/gobby/hooks/event_handlers/_tool.py around lines 57 - 63, Replace the inline tool-name set in the event handler’s codex validation condition with the exported FUNCTIONS_EXEC_NAMES constant from execution_chain.py, preserving the existing validate_functions_exec_wrapper and blocking behavior.

In @src/gobby/install/shared/prompts/validation/validate.md around lines 46 - 49, Remove the stale pending-status guidance from the validation prompt contract, including the instructions around the pending response cases. Ensure omitted-content and other incomplete-coverage cases return invalid with an appropriate criterion gap copied into blocking_reasons, preserving the valid/invalid-only status contract.

In @src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py around lines 337 - 341, Remove the vestigial should_skip alias initialized in the lifecycle close logic, and replace its uses at the validation gates and later cleanup location with skip_leaf_checks directly. Preserve the existing gating behavior, including simplifying the redundant combined condition and the always-true nested check.

In @src/gobby/mcp_proxy/tools/tasks/_lifecycle_validation.py around lines 715 - 717, Extract the repeated criterion_results list comprehension into a local helper named _serialize_criterion_results(result) within the current function. Replace all three inline comprehensions, including those near the existing criterion_results serializations, with calls to this helper while preserving the current to_dict() output.

In @src/gobby/sessions/processor_usage.py around lines 34 - 43, Update the later early-return in the processor usage flow to include the reported occupancy signal, using has_context_occupancy alongside has_usage and has_window_metadata when deciding whether to return. Ensure batches containing context_used_tokens proceed to build the occupancy snapshot even without window metadata.

In @src/gobby/storage/migrations/342_task_validation_epoch.sql around lines 17 - 28, Split the verification_receipts constraint migration into separate rollout steps so the provisional-to-pending backfill does not occur while an ACCESS EXCLUSIVE lock is held. Add the replacement normalized_outcome check with NOT VALID, perform the UPDATE after the DDL transaction, then validate it with VALIDATE CONSTRAINT while preserving writable-table availability.

In @src/gobby/storage/tasks/_manager.py around lines 355 - 364, Update update_task so get_task(task_id) is only called when task_type or validation_criteria is being changed and validation requires the current value; avoid fetching current_task for unrelated metadata updates. Preserve the existing invariant validation and final fresh-object fetch behavior, using the effective values already computed around require_validation_criteria.

In @src/gobby/storage/tasks/_manager.py around lines 355 - 364, Assign the return value of require_validation_criteria to effective_criteria in the task update flow, so the normalized or None value is passed to _update_task_metadata instead of the raw validation_criteria input. Preserve the existing handling for UNSET and non-string values while ensuring update behavior matches the normalized persistence used during creation.

In @src/gobby/tasks/criteria_contract.py around lines 33 - 67, Update split_validation_criteria so text accumulated in current before the first list marker is discarded once a list is detected, rather than appended to items. Preserve subsequent bullet handling, continuation lines, blank-line flushing, and the existing paragraph behavior when no list marker is present.

In @src/gobby/tasks/validation_verdict.py around lines 141 - 223, Normalize the criterion string with surrounding whitespace removed before validating membership in expected_set or using it as a by_criterion key. Preserve the existing rejection for non-string or otherwise unknown criteria, and ensure stored results, duplicate detection, and error messages use the normalized criterion.

In @tests/cli/test_pack.py around lines 190 - 219, Add the appropriate pytest unit marker to test_pack_preserves_primary_error_and_completes_cleanup so the test is selectable as a unit test in CI, preserving its existing assertions and behavior.

In @tests/config/test_mcp_config.py around lines 236 - 240, Annotate the tmp_path parameter in test_load_servers_skips_without_name with the pathlib.Path type, preserving the existing pytest fixture behavior and ensuring the function signature is fully typed.

In @tests/hooks/test_session_coordinator.py around lines 350 - 373, Add the appropriate pytest unit-test marker, such as @pytest.mark.unit, to test_reregister_logs_storage_failure_with_structured_context so CI can select it while preserving the existing test behavior.

In @tests/integration/test_hub_query.py around lines 323 - 335, Update the task insert near the existing hub query test to use the hub transaction API instead of hub_db.execute, and replace the four %s placeholders with $1–$4 while preserving the current values and SQL behavior.

In @tests/mcp_proxy/tools/test_tasks_lifecycle_coverage.py around lines 36 - 48, Scope the default AsyncMock validator in create_task_registry to tests that explicitly isolate lifecycle mechanics instead of applying it to every registry. Ensure at least one close-task test exercises the real validator or verifies the evidence-admission and per-criterion validation inputs, while preserving the existing mock behavior for targeted lifecycle tests.

In @tests/servers/routes/test_admin.py around lines 1093 - 1098, Add type annotations to the injected fixtures in test_reload_workflows_manager_exception and the corresponding test near the second referenced location, including client, mock_server, and mock_arm_cls. Use the established fixture or mock types already used elsewhere in the test module and ensure every function parameter is typed.

In @tests/sessions/test_codex_nested_exec_outcomes.py around lines 383 - 393, Update the test’s @pytest.mark.parametrize inputs to include an expected outcome value, such as expected_identity, for each tool_input case. Replace the tool_input-based if/else in the test with assertions driven by that parameter, asserting the existing outcome fields for expected cases and empty outcomes otherwise; do not branch on literal input strings.

In @tests/storage/test_migration_contract.py around lines 77 - 85, Update the migration contract assertions in the test to require the nonblank validation-criteria predicate `NULLIF(BTRIM(validation_criteria), '') IS NOT NULL` and verify the associated constraint is created with `NOT VALID`; retain the existing migration ordering and status assertions.

In @tests/tasks/test_validation.py around lines 558 - 560, Rename test_unsupported_invalid_is_pending_after_one_request to reflect that its assertion expects an invalid status, while preserving the existing test behavior and assertions.

In @tests/utils/test_db_validation.py around lines 97 - 106, Update the direct tasks INSERT statement in the test setup to use numbered $1–$6 placeholders instead of %s, while preserving the existing parameter order and values passed to the query.

In @tests/utils/test_validation.py around lines 202 - 214, Update both direct task INSERT statements in the affected test setup to use numbered $1–$5 placeholders instead of %s, while preserving the existing parameter order and NOW() timestamp expressions.

In @tests/workflows/test_condition_helpers.py around lines 42 - 47, Update the task helper around manager.create_task so validation_criteria is removed from kwargs before forwarding arguments, then pass the resolved value exactly once. Preserve the default criterion when callers do not provide one and allow caller-provided validation_criteria to be used without duplicate-keyword errors.

In @tests/workflows/test_memory_lifecycle_rules.py around lines 143 - 165, Annotate the db and manager parameters in the new test methods, including test_event_and_effect and test_matches_plan_boundaries, with the imported HubDatabase and LocalWorkflowDefinitionManager types or the project’s established fixture protocols. Keep the existing return annotations and test behavior unchanged.
Fix the following issues. The issues can be from different files or can overlap on same lines in one file.

In @src/gobby/install/shared/workflows/agents/plan-adversary.yaml around lines 94 - 120, Add runtime-enforced session or turn-level timeout handling around the parallel lane research launched from the plan-review workflow, including facilities that do not expose their own deadline. Ensure a hung or slow internal subagent cannot stall the review indefinitely, while preserving the existing per-lane fallback to sequential execution and keeping the parent as the sole owner of verdicts and findings. Anchor the change to the parallel-mode internal collaboration flow and its lane execution orchestration.

In @src/gobby/sessions/summarize.py around lines 478 - 510, Document in the public session-summary function that callers joining an existing _summary_tasks entry share the first caller’s session_manager, llm_service, db, and session_summary_config, while set_handoff_ready and file-writing options remain per caller. Also replace the shallow dict(core_result.result) copy with the established deep-copy approach so nested session_wiki_file and context_summary values are not shared between callers.

In @src/gobby/storage/sessions/_field_update.py around lines 182 - 241, Derive reset_target_transcript from the freshly locked session in matching whose id equals session_id, rather than from the stale pre-lock current snapshot. Use that locked row’s status so revival reliably resets transcript_processed when the session is expired at reconciliation time.

In @tests/sessions/test_summarize.py around lines 504 - 507, Bound the event waits in this test and the corresponding cancellation, retry, and post-effects tests using asyncio.wait_for with a finite timeout, matching the established pattern in test_different_sessions_generate_in_parallel. Apply the timeout to generation_started and all_callers_joined waits so dedup regressions fail promptly while preserving the existing event ordering and gather flow.

In @tests/skills/test_plan_review_skill.py around lines 171 - 174, Update the assertions in the lane-research contract test to inspect the relevant section of body and verify complete policy clauses, not isolated keywords: require that all three lanes run concurrently, require one read-only provider-native internal subagent per lane, and require using gobby-agents:spawn_agent for lane research. Also assert the corresponding prohibitive wording is absent, including “do not run all three concurrently” and “never use gobby-agents:spawn_agent for lane research,” while preserving the existing taskless-agent check.

In @tests/tasks/test_validation_verdict.py around lines 17 - 40, Add a negative single-criterion test alongside test_single_paraphrased_criterion_is_canonicalized_to_exact_task_criterion, providing an unrelated non-empty criterion while keeping the expected criterion and validation inputs otherwise equivalent. Assert that_validation_result_from_data returns an invalid result and preserves the appropriate blocking reason, ensuring unrelated text is not accepted by the paraphrase fallback.

In @tests/test_failure_categories.py around lines 16 - 31, Extend test_persisted_validation_status’s parameterized cases with ("invalid", None, "invalid") and ("pending", None, "pending") to verify persisted_validation_status preserves statuses when no failure category is provided.

In @crates/gcode/src/commands/status/prune.rs around lines 1265 - 1601, Extract the duplicated PostgreSQL test fixtures from the serial_db modules in invalidate.rs and prune.rs into a shared cfg(test) status::test_support module. Move cleanup_project, seed_project_with_child_rows, project_child_row_count, count_rows, unique_test_project_id, test_uuid, ProjectCleanup, and its Drop implementation there, then import the shared symbols in both test modules while preserving existing test behavior.

In @crates/gwiki/src/commands/session_sync.rs around lines 91 - 119, The shared phase context in run_persistent_write_phases is currently passed as a positional tuple, making the three closures depend on destructuring order. Introduce a small named SyncPhaseContext structure containing conn, search_scope, and progress, then pass and access those named fields in both the transcript archive and vector/graph synchronization closures.

In @src/gobby/sessions/summarize.py around lines 496 - 500, Document in the shared summarization task’s docstring that joiners reuse the first caller’s resolved llm_service, session_summary_config, and db dependencies, and must provide identical daemon wiring. Keep the existing task-sharing behavior unchanged unless the implementation already exposes a dependency-identity key that can safely distinguish callers.

In @src/gobby/sessions/summarize.py at line 73, Make the in-flight summary registry loop-aware in the summarize flow: update _summary_tasks and the logic around the summary task creation and lookup (including the code near lines 479-496) to key entries by the current event loop and session_id, and only await an existing task when its loop matches the running loop. Ensure cleanup removes the loop-specific entry even when the task’s loop is closed, preventing stale session entries from affecting later loops.

In @src/gobby/storage/external_issue_sync.py around lines 47 - 50, Update the JSONDecodeError handler in the external issue statistics parsing flow to emit a warning containing the discarded raw_statistics payload before replacing it with {}. Preserve the existing fallback behavior while making corrupt last_statistics data visible to operators.

In @src/gobby/storage/sessions/_field_update.py around lines 208 - 222, Update the reset_target_transcript calculation in the matching-row update loop so transcript_processed is reset only when the expired current row is the owner (current.id == owner.id). Preserve the existing candidate.id == session_id check and status-transition behavior, ensuring superseded rows remain expired without being re-queued.

In @src/gobby/storage/sessions/_field_update.py around lines 186 - 205, Move the fallback get(session_id) call out of the db.transaction() block in the session update flow. In the matching check around terminal_session_identity, record that no match was found, exit the transaction, then call self.get(session_id) afterward; preserve the existing return behavior while shortening the lock duration.

In @src/gobby/storage/tasks/_plan_enhancement.py at line 131, Make the `get_task(db, task_id)` call’s purpose explicit by documenting that it intentionally validates task existence before stage validation, while preserving its unused-result behavior and exception propagation.

In @src/gobby/storage/tasks/_stage_state_transitions.py at line 81, Rename the public extension-point parameter_transaction_mutation to transaction_mutation consistently across transition, _transition, and route_enhancement, including all forwarding and invocation sites. Document that the callback performs transaction-scoped side effects, must not commit or roll back, and that exceptions roll back the entire stage transition.

In @src/gobby/sync/external_coordinator.py around lines 90 - 94, Update the shutdown cleanup in the coordinator’s task-cancellation flow so in-flight runs do not leave status rows as “running.” Before cancelling tasks, briefly drain them to completion, or ensure cancellation is caught and a terminal status is written for each affected run; preserve the existing task gathering and cleanup behavior.

In @src/gobby/sync/external_coordinator.py around lines 584 - 593, Update the rate-limit detection logic around the existing 429 check to require HTTP/status-code context before treating 429 as rate limiting, rather than matching any standalone 429 in the full exception text. Preserve the existing rate-limit, quota, and request markers while preventing unrelated issue IDs, task numbers, or byte counts from triggering the rate_limited state.

In @src/gobby/sync/github_issue_sync.py around lines 264 - 267, Update the pagination loop around the `for ... else` block in the GitHub issue sync flow so exhausting the page cap is distinguished from parse or transport failures. When all permitted full pages are consumed, record truncation through the existing appropriate mechanism without incrementing `stats["errors"]` for a legitimately complete boundary case; preserve the current early break for short pages and genuine error accounting.

In @tests/mcp_proxy/tools/test_memory_tools.py around lines 654 - 659, Update test_promote_global_memory_requires_owner_project to add explicit type annotations for the memory_registry and mock_memory_manager fixture parameters, reusing the imported InternalToolRegistry and the appropriate existing memory-manager type used elsewhere in the test module.

In @tests/servers/routes/test_memory_routes.py at line 547, Add type annotations to test_promote_rejects_extraneous_fields for both client and mock_server fixtures, and retain an explicit None return annotation in line with the project’s testing conventions.

In @tests/sessions/test_codex_nested_exec_outcomes.py at line 341, Update the test function test_nested_exec_write_stdin_accepts_native_terminal_envelope with the explicit None return type annotation, ensuring it follows the project requirement that all functions have type hints.

In @tests/sessions/test_summarize.py around lines 504 - 505, Bound every generation_started.wait() and all_callers_joined.wait() in the affected session tests, including the occurrences near the referenced lines, with asyncio.wait_for using an appropriate timeout such as the existing 1-second pattern in test_different_sessions_generate_in_parallel. Preserve the current synchronization order and assertions while ensuring regressions fail with a timeout instead of hanging CI.

In @tests/storage/sessions/test_lifecycle.py around lines 420 - 421, Update the transcript_processed assertions in the affected lifecycle tests to use identity comparison with False instead of equality to 0, matching the existing assertions in the same file and validating that the database driver returns a boolean.

In @tests/sync/test_external_coordinator.py around lines 260 - 261, Update the test around the coordinator.wait_for_idle() task to yield control to the event loop before checking draining.done(), ensuring the task has a chance to execute and the assertion verifies that wait_for_idle() is actually blocking.

In @tests/sync/test_github_issue_sync.py around lines 288 - 313, Update the record_to_thread helper’s callable-name recording to use a total fallback when both_mock_name and __name__ are unavailable, so plain MagicMock instances do not raise AttributeError. Preserve recording named mocks and regular callables as before, and use a safe generic value for unnamed callables.

In @tests/workflows/test_skill_discovery_rules.py around lines 3421 - 3424, Update the INSERT statement in the test’s db.execute call to use PostgreSQL-style $N placeholders instead of %s, while preserving the existing project_id and project name parameter order.

In @src/gobby/cli/github.py around lines 75 - 97, Ensure per-project isolation in _check_github_access_result and _gather_github_access for all exceptions raised during readiness checks, not only GitHubRepositoryReadinessError. Catch and convert unexpected failures into that check’s existing error-result shape, or configure asyncio.gather to return and normalize exceptions while preserving successful sibling results; keep the documented batch behavior and tuple return contract intact.

In @src/gobby/install/shared/workflows/rules/memory-lifecycle/digest-on-response.yaml around lines 15 - 27, Add selector tags to the digest-prior-codex-turn-on-start rule definition, including the required gobby and memory-lifecycle functional-group tags and the default tag if this bundled rule targets interactive sessions. Preserve the existing event, condition, priority, and effects.

In @src/gobby/memory/digest.py around lines 254 - 284, The inline active-turn truncation logic in _read_undigested_turns should be extracted into a helper named_truncate_segment_before_active_turn(segment). Move the backward task_started scan and pre-task_started truncation into that helper, preserving its current empty-result behavior and leaving segment_turn_offset computed before truncation so pair-index calculations remain unchanged.

In @src/gobby/runner_lifecycle_periodic.py around lines 163 - 171, Harden _has_enabled_external_issue_integration so failures from get_server_config(provider) and missing enabled attributes cannot escape the predicate. Safely read each provider configuration and its enabled value, treating exceptions, absent attributes, and non-enabled values as false, while preserving the existing GitHub/Linear provider check and boolean result.

In @src/gobby/servers/provider_model_defaults.py around lines 305 - 320, Update the labels for the glm-5.2 and glm-5.2-fast entries in the provider model defaults list to use the consistent “Droid Core (<model>)” format, matching the existing GLM-5.1 and Kimi K2.6 entries while leaving their values and reasoning settings unchanged.

In @src/gobby/sessions/summarize.py around lines 73 - 84, Make the summary task cache event-loop aware around _summary_tasks and its lookup/creation flow: associate each cached task with the running event loop, or discard entries belonging to a different loop before awaiting them. Ensure _remove_summary_task removes only the matching task entry and preserve sharing for tasks created on the current loop.

In @src/gobby/sessions/summarize.py around lines 497 - 510, Add an explicit note to the concurrency paragraph of the relevant summarization function’s docstring: concurrent joiners deduplicated by session_id receive the originator’s result and inherit its llm_service, db, and session_manager, regardless of their supplied parameters. Do not change the existing deduplication or result-handling logic.

In @src/gobby/storage/external_issue_sync.py around lines 121 - 123, Update both fetchone call sites in the external issue sync flow to check the returned row for None before casting or passing it to from_row, and preserve the existing explicit RuntimeError failure path for missing rows.

In @src/gobby/storage/external_issue_sync.py around lines 47 - 50, Update the JSON decoding error handler in the external issue sync statistics parsing flow to retain the existing {} fallback while emitting a structured log for json.JSONDecodeError. Include relevant context identifying the affected last_statistics data or row, using the module’s established logger.

In @src/gobby/storage/sessions/_field_update.py around lines 186 - 207, Update the ownership-candidate query in the session update flow around Session.from_row and terminal_session_creation_order to exclude deleted sessions with the same status guard used by the other lifecycle paths. Keep the existing identity matching and owner selection behavior unchanged for non-deleted sessions.

In @src/gobby/storage/sessions/_field_update.py around lines 259 - 274, Update the notification in the status_changes loop so sessions whose desired_status is "expired" emit "session_expired" through_notify_session_change, matching update_status and expire_if_active; retain "session_updated" for all other status changes.

In @src/gobby/storage/sessions/_field_update.py around lines 199 - 205, Update the no-match branch in the transaction flow containing the matching list comprehension so it exits the transaction context without calling self.get(session_id) inside it. After the with block commits, perform the fallback self.get(session_id) read, preserving the existing behavior for matching branches.

In @src/gobby/sync/external_coordinator.py around lines 582 - 594, Consolidate rate-limit detection into one shared helper module by moving the regex-based logic from_is_rate_limit and the retry_after/retry_after_seconds attribute checks from github_issue_sync._is_rate_limit_error. Update both call sites to use the shared helper, preserving word-boundary matching for 429 and the combined marker and attribute detection behavior.

In @src/gobby/sync/github_issue_sync.py around lines 27 - 34, The issue-number bound and normalization are duplicated between _normalize_issue_number in github_issue_sync.py and the task_github_import validation path. Consolidate them into one shared validator and_MAX_GITHUB_ISSUE_NUMBER definition, then update both callers to reuse it while preserving the current accepted integer/string inputs and range checks.

In @src/gobby/sync/task_github_import.py at line 25, Extract the GitHub issue-number bound and validation from the inline check in the import flow and _normalize_issue_number into one shared validator and constant. Update both GitHub sync paths to import and reuse that validator, preserving the existing type and positive-range behavior while removing the duplicated_MAX_GITHUB_ISSUE_NUMBER definitions and validation logic.

In @tests/dispatch/test_skill_composition.py around lines 180 - 182, Explicitly decorate the async test function test_spawn_and_explain_share_unknown_skill_failure with pytest.mark.asyncio, while preserving its existing module-level integration marker and test behavior.

In @tests/sessions/test_summarize.py around lines 504 - 506, Update the coordination waits in the affected session summarization tests, including the block around generation_started and all_callers_joined, to wrap each Event.wait() with asyncio.wait_for(..., timeout=1), matching test_different_sessions_generate_in_parallel. Apply the same one-second bound to the additionally referenced waits so regressions fail promptly instead of hanging.

In @tests/storage/test_storage_tasks.py around lines 1605 - 1624, Move the `CREATE OR REPLACE FUNCTION fail_plan_enhancement_update_fn` and `CREATE TRIGGER fail_plan_enhancement_update` statements inside the test’s existing `try` block, or execute them as one atomic statement, so cleanup in `finally` runs even when trigger creation fails.

In @tests/sync/test_external_coordinator.py around lines 268 - 284, Add a bounded timeout around the await of coordinator.run(shutdown) in test_run_survives_recoverable_refresh_failure, matching the timeout pattern used by sibling tests so retry/backoff hangs fail fast while preserving the existing two-attempt assertion.

In @tests/sync/test_linear_sync.py around lines 113 - 116, Update the test injection helper _replace_for_test to use pytest monkeypatch.setattr or unittest.mock.patch.object instead of object.__setattr__. Ensure each replacement is automatically restored during test teardown while preserving the helper’s explicit test-double assignment behavior.

In @tests/tasks/test_diff_paging.py around lines 132 - 141, Update test_manifest_parser_and_encoder_are_public to assert the expected emitted-item count before indexing emitted[0], ensuring both missing and extra parser emissions fail with a clear diagnostic while preserving the existing content assertions.
