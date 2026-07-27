# CodeRabbit Fixes: Runner, Dispatch, Storage, and Core Contracts

Runner lifecycle, dispatch/adapters, migrations, core storage, test tooling, and remaining infrastructure fixes.

Unresolved original findings: **57**

Original finding IDs: 133, 135-137, 257, 289, 293-294, 327, 380, 459, 472-480, 484-487, 491, 498, 547, 552-553, 577-578, 581-584, 590-591, 609-612, 619, 621, 662, 678, 683, 745-746, 756-759, 761-762, 767, 777, 783

## Finding #133

In @tests/adapters/test_provider_contract_fixtures.py around lines 205 - 213, Add an assertion for results[3]["tool_outcome"]["status"] in the existing result validation, requiring it to equal "unknown" like results[1], while preserving the current command-correlation and final-outcome assertions.

## Finding #135

In @tests/dispatch/test_prompts.py around lines 145 - 148, Remove the redundant test_epic_reviewer_prompt_builder_registered test, since test_dispatch_prompt_builder_keys_present already verifies the same registration. Keep coverage focused on distinct behavior rather than duplicating the PROMPT_BUILDERS key assertion.

## Finding #136

In @tests/dispatch/test_rules.py around lines 823 - 825, Remove the ineffective description.count assertion from the test, since description is never mutated and the existing_evaluate(repeated_task, changed_context) is None assertions already verify non-duplication behavior.

## Finding #137

In @tests/e2e/test_build_dispatcher_autonomy.py around lines 187 - 195, Remove the redundant dispatch_idle assignment and trailing assert in the dispatch handoff test, and await wait_for_async_condition directly while preserving its existing condition, timeout, and description arguments.

## Finding #257

In @tests/storage/test_agent_sandbox_records.py around lines 13 - 67, Add the module-level unit test marker to tests/storage/test_agent_sandbox_records.py so both sandbox_record tests are consistently classified as unit coverage, using the existing pytest marker convention without changing their assertions or behavior.

## Finding #289

In @tests/fixtures/regressions/task_close_evidence_18689/assembled_close_packet.json around lines 6 - 33, Update the test covering the assembled close packet fixture to explicitly assert that its close_limit matches CLOSE_VALIDATION_EVIDENCE_CONTEXT_LIMIT, while preserving the existing packet command assertions. Ensure a limit change produces a clear invariant failure instead of only an opaque fixture diff.

## Finding #293

In @tests/regressions/test_task_close_evidence_18689.py at line 66, Add the appropriate category marker to each of the four tests in this file, including test_more_than_fifty_commands_drop_relevant_early_results and the tests at the referenced locations; use the existing project marker convention and choose the category that matches each test’s scope.

## Finding #294

In @tests/regressions/test_task_close_evidence_18689.py around lines 66 - 82, Mark the reproduction tests for bounded_session_evidence_loss, selector_outcome_weakening, and readiness_close_projection_divergence as intentional known-defect snapshots, using the project’s established marker or strict xfail with a clear reason. Apply the marker to the relevant test functions, including test_more_than_fifty_commands_drop_relevant_early_results, without changing their buggy-outcome assertions.

## Finding #327

In @src/gobby/adapters/codex_impl/item_normalization.py around lines 306 - 309, Update _mark_ambiguous and its callers to use an insertion-ordered mapping for ambiguous keys instead of a set, and replace arbitrary pop eviction with FIFO removal via the oldest key, matching _set_pending’s ordering. Update _set_pending’s ambiguous parameter annotation accordingly while preserving existing membership checks and fail-closed behavior.

## Finding #380

In @tests/storage/test_verification_receipts.py around lines 58 - 70, Add a module-level pytestmark using the unit marker, matching the sibling verification test module, and annotate the _session helper with its concrete return type while preserving its existing registration behavior.

## Finding #459

In @src/gobby/dispatch/spawn_actions.py around lines 11 - 15, Expose a public alias for_deliver_existing_terminal_run_unshielded in agent_cleanup, such as deliver_existing_terminal_run_in_scope, and update the cross-module imports and call sites in spawn_actions.py, agents_lifecycle_tools.py, agent_cancellation.py, and resume_executor.py to use it. Keep the underscored helper internal and preserve its existing behavior.

## Finding #472

In @src/gobby/runner.py around lines 210 - 220, Remove the single-use ConstructionRollbackLedger from the runner initialization flow and replace it with direct exception handling around init_storage_and_config, init_services, init_orchestration, and init_servers: call rollback_runner_resources(self) in the BaseException handler, then re-raise the original exception. Preserve the existing initialization order and rollback behavior.

## Finding #473

In @src/gobby/runner_gate.py around lines 196 - 206, Update_settle_reap_under_cancellation to capture non-cancellation exceptions from awaiting the shielded_kill_and_reap task, then re-raise the recorded cancellation when present instead of letting the reap failure replace it. Preserve propagation of the reap exception when no cancellation was observed.

## Finding #474

In @src/gobby/runner_gate.py around lines 240 - 248, Increase the parent watchdog timeout used by asyncio.wait_for around process.communicate in the runner gate so it exceeds the child’s budget_seconds by a small grace margin. Keep the child’s budget unchanged, allowing_diagnose_gate_wait and its RunnerGateError details to complete before the parent raises its watchdog timeout.

## Finding #475

In @src/gobby/runner_lifecycle_agents.py around lines 107 - 152, Update the terminal replay startup flow to iterate over distinct run IDs from the completion-subscriber table rather than paging every status in TERMINAL_AGENT_RUN_STATUSES. Fetch subscribers and their associated runs only for those IDs, skip missing or non-terminal runs, then redeliver and remove acknowledged subscribers using the existing wake and cleanup logic.

## Finding #476

In @src/gobby/runner_lifecycle_shutdown.py around lines 597 - 598, Replace the ad-hoc db_executor._gobby_shutdown_joined state in the shutdown flow with an explicit DatabaseExecutor join/is_joined contract or a module-level WeakSet of joined executors. Ensure repeated shutdown calls cannot spawn duplicate join threads, and document whether post-timeout calls intentionally retry shutdown or are treated as already joined.

## Finding #477

In @src/gobby/runner_lifecycle_subsystems.py around lines 777 - 782, Capture the return value from recover_agent_completion_subscribers in the post-startup initialization flow, then log the number of rehydrated/redelivered subscriptions using the runner’s existing logging facilities. Preserve the current invocation and await behavior while making the recovery count observable.

## Finding #478

In @src/gobby/runner_pid_file.py around lines 61 - 68, Add a no-op release() method to FailOpenPidOwnership so it conforms to the same ownership protocol as PidFileClaim. Then simplify runner.main() and run_daemon’s cleanup_owned_pid_file to call release() directly without isinstance-based branching, preserving existing cleanup behavior.

## Finding #479

In @src/gobby/runner_rollback.py around lines 31 - 36, Update the rollback loop in the callback cleanup method to log failures while re-raising non-Exception control-flow signals such as KeyboardInterrupt and CancelledError; continue processing callbacks only for ordinary Exception failures, then preserve the existing callback clearing behavior.

## Finding #480

In @src/gobby/runner_rollback.py around lines 39 - 65, Replace the cross-loop execution in_settle_async_close with loop-aware settlement: require rollback compensations to be synchronous when no async scheduling is available, and when a loop is already running, schedule the coroutine on that original loop via run_coroutine_threadsafe or create_task rather than asyncio.run in a helper thread. Ensure timeout handling cancels and fully settles the scheduled coroutine so no daemon thread or unclosed loop remains.

## Finding #484

In @src/gobby/storage/executor.py around lines 116 - 118, Update Executor.join to set the internal _shutdown flag to True before or alongside shutting down the underlying executor, so calling join() alone closes admission and makes stats().shutdown report the correct state. Preserve the existing blocking shutdown behavior and cancel_futures=False option.

## Finding #485

In @src/gobby/storage/hub/postgres.py around lines 266 - 273, Update bounded_transaction to handle nested use safely by capturing the ambient transaction’s existing statement_timeout and lock_timeout before issuing SET LOCAL, then restoring both values on exit; preserve the current bounds for outer transactions and ensure restoration occurs even when the nested block raises.

## Finding #486

In @src/gobby/storage/hub/postgres.py around lines 274 - 279, Replace the interpolated SET LOCAL statements in the transaction context with parameterized set_config calls using the validated timeout values and local scope enabled. Preserve the existing positive-value validation and transaction behavior in the surrounding transaction method.

## Finding #487

In @src/gobby/storage/hub/postgres_pool.py around lines 67 - 75, The retry path in the pooled connection acquisition flow should not immediately call pool.connection() again after PoolTimeout without recovery. Update the PoolTimeout handling around pool.connection() to perform pool repair via pool.check() or an equivalent out-of-band mechanism, then retry with bounded backoff while preserving the existing single-retry limit and timeout behavior.

## Finding #491

In @tests/events/test_wake_wiring.py around lines 244 - 302, The test test_wait_for_agent_registration_wakes_on_completion should invoke wait_for_agent through the registry’s public tool-dispatch or call API instead of accessing registry._tools directly. Pass run_id through that public path so argument validation is exercised while preserving the existing assertions and completion notification flow.

## Finding #498

In @tests/storage/hub/test_postgres_baseline_application.py around lines 488 - 495, Add a parametrized test for PostgresHubDatabase.bounded_transaction that passes zero or negative statement_timeout_ms/lock_timeout_ms and asserts ValueError is raised before entering the transaction body. Keep the existing valid-bounds test unchanged.

## Finding #547

In @src/gobby/storage/migrations/339_expired_plan_review_round_retry.sql around lines 1 - 11, Remove migration 339 because its index definitions duplicate the final predicates already introduced by migration 338. Keep the expired_at IS NULL predicates in 338 and do not add compatibility handling or replacement drop/create operations; only retain 339 if the project requires supporting environments where 338 was already applied, documenting that reason in its header.

## Finding #552

In @src/gobby/test_types/cli.py around lines 113 - 121, Update the output-writing flow in the CLI command so an output path matching either write_baseline or the baseline path cannot overwrite the baseline JSON produced by write_baseline_file. Detect this path conflict before writing rendered output and preserve the baseline file, while leaving normal --output behavior unchanged.

## Finding #553

In @src/gobby/test_types/render.py around lines 53 - 56, Update the diagnostic heading in the rendering flow around detailed_issues and _append_new_errors so it accurately reflects whether diff is absent or present: label the no-baseline report as errors, and the baseline comparison as new failing errors. Preserve the existing issue selection and rendering behavior.

## Finding #577

In @tests/storage/test_stage_review_findings.py around lines 355 - 497, Split test_pre_spawn_snapshot_transport into three independent tests covering successful snapshot transport, DispatchSpawnFailed evidence expiry, and wrong-lineage bind failure. Reuse stage_review_setup and introduce a small_spawn_with(impl) helper for shared spawn setup and monkeypatching, while preserving each scenario’s existing assertions and behavior.

## Finding #578

In @tests/storage/test_stage_review_findings.py around lines 507 - 513, Update the crash_finalize stub’s return annotation from None to Never and add the corresponding typing import, matching the existing Never usage in the test suite.

## Finding #581

In @tests/test_quality/test_baseline.py around lines 1 - 10, Add a module-level pytestmark in tests/test_quality/test_baseline.py using the appropriate marker for these baseline tests, while preserving the existing imports and test behavior.

## Finding #582

In @tests/test_types/test_audit.py around lines 1 - 13, Add a module-level pytestmark in tests/test_types/test_audit.py using the appropriate existing marker for these type-audit tests, placing it alongside the imports or other module configuration. Ensure all tests in the module receive that marker without changing their test behavior.

## Finding #583

In @tests/test_types/test_mypy_parser.py around lines 1 - 14, Add a module-level pytestmark to tests/test_types/test_mypy_parser.py using the appropriate existing marker for these tests, such as unit. Keep the current imports and test behavior unchanged.

## Finding #584

In @tests/test_types/test_render.py around lines 1 - 6, Add a module-level pytestmark in tests/test_types/test_render.py identifying these tests with the appropriate marker, such as unit, consistent with the project’s existing marker conventions.

## Finding #590

In @src/gobby/adapters/claude_code.py around lines 110 - 128, The redirect branch in the function containing_DENY_REASON_MAX_CHARS must enforce the 300-character cap even when the action itself is too long. Truncate the action segment before constructing action_message, reserving space for the ellipsis as needed, then retain the existing short-reason budgeting and formatting behavior.

## Finding #591

In @src/gobby/adapters/codex_impl/client_lifecycle.py around lines 87 - 88, Guard the redaction loop in the client lifecycle error handling so empty strings from client._redacted_env_values are skipped before calling failure_detail.replace. Preserve redaction for non-empty secret values and prevent empty-value replacements from altering or inflating failure_detail.

## Finding #609

In @src/gobby/test_types/audit.py around lines 99 - 100, Update the directory-target handling in the audit path around_walk_python_files and _is_excluded_directory so targets outside root are skipped before calling_is_excluded_directory. Preserve the existing behavior for in-root directories and ensure out-of-root directory arguments produce no candidates rather than raising, including the corresponding logic at the other affected occurrence.

## Finding #610

In @tests/adapters/test_claude_code_adapter.py around lines 143 - 179, Remove the import-time assertions from_bundled_before_tool_block_reasons and make the loader skip malformed rules while collecting valid block reasons. Move validation for exactly one block effect, string reasons, and duplicate rule names into a dedicated test or session-scoped fixture that reports the offending rule clearly, and avoid invoking validation during module initialization via _BUNDLED_BEFORE_TOOL_BLOCK_REASONS.

## Finding #611

In @tests/adapters/test_claude_code_adapter.py around lines 30 - 139, Update test_live_corpus_is_exhaustively_classified_once to replace the combined set-equality assertion with separate checks for missing and unexpected rule names, covering both_REDIRECT_BLOCK_RULES and _TRUE_RESTRICTION_BLOCK_RULES. Include the relevant set difference in each assertion message so added, renamed, or deleted bundled before_tool rules identify the exact classification change required.

## Finding #612

In @tests/adapters/test_claude_code_adapter.py at line 140, Reformat the_SKILL_FETCH_REASON_TEMPLATE assignment to keep each source line within the 100-character limit, using adjacent implicitly concatenated string literals while preserving the exact resulting template value.

## Finding #619

In @tests/e2e/test_inter_agent_messages.py around lines 291 - 316, Update the test around the first and second get_inter_session_messages calls to capture the message id from the first result, then assert that exact id is present in the second result’s messages. Preserve the existing checks that both reads return at least one message while verifying the same message survives.

## Finding #621

In @tests/test_quality/test_baseline.py around lines 54 - 111, Add the @pytest.mark.unit decorator to each newly added test function: test_chmod_failure_preserves_existing_baseline_and_removes_temporary_file, test_write_baseline_honors_process_umask, test_load_baseline_rejects_unsupported_schema, and test_load_baseline_rejects_non_positive_occurrences.

## Finding #662

In @src/gobby/storage/tool_results.py around lines 70 - 93, Remove the retention DELETE from the save() transaction in the tool-results storage implementation, leaving save() write-only with its INSERT operation. Move or invoke that expiry cleanup through the existing cron/background sweep mechanism instead, using the same retention_days cutoff there.

## Finding #678

In @tests/storage/test_tool_results.py around lines 190 - 207, The test’s expected IDs are ordered by ordinal while search results are ranked by BM25 score, making the positional comparison fragile. Update the assertions around expected and hits to compare the chunk IDs as unordered sets, while preserving the existing monotonic score check.

## Finding #683

In @src/gobby/adapters/codex_impl/item_normalization.py at line 46, Use _DIRECT_EXEC_COMMAND_NAMES in item_normalization.py as the single direct-exec tool-name allowlist, and remove the duplicate_DIRECT_EXEC_NAMES definition from the codex.py transcript reconciliation flow. Update codex.py to import and reference the shared constant alongside extract_direct_exec_command and extract_direct_exec_terminal_result, preserving existing normalization behavior.

## Finding #745

In @src/gobby/runner_broadcasting.py around lines 344 - 351, The_format_cron_run_message function embeds unbounded run.output and run.error, allowing chat messages to exceed channel limits and fail permanently. Truncate each included output or error to a safe maximum before constructing the notification, while preserving the existing status text and concise-message behavior.

## Finding #746

In @src/gobby/runner_init/orchestration.py around lines 544 - 556, Track whether memory-scope enumeration via list_dream_scopes completed successfully, distinguishing a genuine empty result from an exception that leaves memory_scopes empty. When enumeration fails, skip register_codewiki_nightly_crons so its stale-job pruning cannot disable existing project cron jobs; continue registering normally for successful enumeration, including the valid no-projects case.

## Finding #756

In @src/gobby/adapters/codex_impl/execution_chain.py around lines 507 - 532, Replace the ambiguity tracking sets used by the pending cell/session flow with an insertion-ordered bounded structure, such as a dict keyed by execution key, and update membership/addition/eviction logic around _ambiguous_cells, _ambiguous_sessions, and this pending-correlation block. Evict the oldest ambiguity entry deterministically rather than calling set.pop(), while preserving the existing max-size and early-return behavior.

## Finding #757

In @src/gobby/adapters/codex_impl/execution_chain.py around lines 441 - 444, Update the execution correlation logic around the assignments to data["_original_tool_name"] and data["tool_input"] so a missing execution.literal_command does not produce {"command": None}; omit the command key or use the existing unknown-result shape when the command is absent, while preserving the current Bash payload for present commands.

## Finding #758

In @src/gobby/adapters/codex_impl/execution_chain.py around lines 375 - 391, Refactor the results selection in resolve_output to replace the walrus-based conditional expression with an explicit if/else block: when execution.direct is true, extract the direct terminal result and use it only when non-None; otherwise fall back to decoded_exec_results(output). Preserve the existing result values and subsequent terminal_results filtering.

## Finding #759

In @src/gobby/adapters/codex_impl/execution_chain.py around lines 256 - 266, Update extract_yielded_cell_id to scan every text block and non-blank line until a valid yielded-cell marker is found, rather than returning None after the first non-blank line. Preserve fail-closed behavior by returning None when no marker is found or when the marker is ambiguous, and keep the existing_iter_output_text and _YIELDED_CELL_RE usage.

## Finding #761

In @src/gobby/code_index/sync_worker.py around lines 189 - 196, The gateway SyncCircuitBreaker is configured with a hardcoded failure_threshold of 1, causing one transient daemon failure to pause all gcode projections. Update the gateway_breaker configuration in the sync worker to use a configurable threshold, or at least a threshold of 2, while preserving the existing backoff settings.

## Finding #762

In @src/gobby/code_index/sync_worker.py around lines 70 - 74, Update the sync attempt flow around _breakers_allow_attempt to track which active breakers successfully consumed a probe, then resolve every armed breaker exactly once on every terminal path, including daemon-config failures, G-code timeout/unavailable errors, graph failures, and per-file errors. Record success or failure for each consumed breaker according to the attempt outcome, while preserving existing gateway/vector/graph outcome semantics and avoiding resolution for breakers that were not armed.

## Finding #767

In @src/gobby/storage/migrations/342_task_validation_epoch.sql around lines 17 - 28, Update the verification_receipts normalized_outcome constraint recreation in migration 342_task_validation_epoch.sql to add the CHECK constraint with NOT VALID, then validate it in a separate VALIDATE CONSTRAINT step after the provisional-to-pending update. Preserve the existing allowed outcome values and constraint name.

## Finding #777

In @tests/dispatch/test_dispatcher.py around lines 2138 - 2141, Replace the untyped lambda passed to monkeypatch.setattr for_prepare_plan_adversary_evidence with a local helper function defined before the monkeypatch. Add explicit parameter and return type annotations matching the expected kwargs and tuple result, then use that helper as the replacement while preserving its current behavior.

## Finding #783

In @tests/storage/test_storage_tasks.py around lines 58 - 60, Introduce a shared test constant or fixture for the repeated non-empty validation criteria, preferably in tests/conftest.py, and replace the literal in this create_task call and the other occurrences noted in the comment, including sibling test files where applicable. Reuse that shared value consistently without changing task behavior.
