# CodeRabbit Fixes: MCP Proxy and Hub Services

MCP proxy execution, result handling, connection lifecycle, and PostgreSQL Hub services.

Unresolved original findings: **53**

Original finding IDs: 188-193, 233-237, 284-286, 299-302, 330, 373, 427-428, 468-471, 492, 494, 522-523, 557-558, 601, 655-660, 672-675, 701-704, 718-720, 744, 779-780

## Finding #188

In @src/gobby/mcp_proxy/connection_cleanup.py around lines 101 - 106, Bound the sequential disconnect loop in the shutdown cleanup flow so total shutdown time does not scale without limit with connection count. Update the loop using disconnect_connection to enforce an overall wall-clock budget or a shorter per-connection timeout, while preserving task-affine caller-task execution and marking each processed connection as DISCONNECTED.

## Finding #189

In @src/gobby/mcp_proxy/tools/agents_query_tools.py around lines 189 - 190, Replace the assert-based narrowing in the timeout and interval handling flow with explicit None checks or a typed helper return. Preserve the existing early-return behavior established by the preceding error check, and ensure timeout_value and interval_value are narrowed without relying on assertions that disappear under Python optimization.

## Finding #190

In @src/gobby/mcp_proxy/tools/agents_query_tools.py around lines 205 - 214, Update the exception handlers around tmux pane capture and session checks to log the caught exception using structured logging before setting the failure state. Include the run ID and tmux session name as contextual fields, while preserving CancelledError propagation and the existing capture_failed behavior.

## Finding #191

In @src/gobby/mcp_proxy/tools/agents_query_tools.py around lines 224 - 225, Update the matched-result branch in the relevant query tool to bound the excerpt instead of returning the full pane_output buffer. Return the matched region with limited surrounding context, or truncate pane_output to a reasonable maximum, while preserving the existing success and matched fields.

## Finding #192

In @src/gobby/mcp_proxy/tools/agents_query_tools.py at line 197, Raise the minimum interval in the wait_for_output polling logic from 0.01 seconds to 0.1 seconds, matching the clamp used by wait_for_agent. Update affected tests that pass poll_interval_seconds=0.01 to use short timeouts while preserving their intended behavior.

## Finding #193

In @src/gobby/mcp_proxy/tools/skills/install_skill.py around lines 116 - 133, Restrict the provenance validation block in the skill installation flow to the TopicHub provider that supplies item_id, repo, path, and sha, rather than applying it whenever DownloadResult.provenance is present. Keep other providers’ provenance provider-agnostic, while preserving the existing normalize_topic_item_id and item_unavailable validation for TopicHub downloads.

## Finding #233

In @tests/mcp_proxy/test_manager_disconnect_cancellation.py around lines 36 - 45, Replace the BlockingConnection instance in this test with a minimal local stub object exposing is_connected = True and the instrumented async disconnect method that records the current task. Remove the unused BlockingConnection setup and preserve registration under the "stdio-server" connection key.

## Finding #234

In @tests/mcp_proxy/test_wait_for_output.py around lines 161 - 169, Reduce the pathological input size in the test around _invoke and compile_safe_regex to the smallest buffer that still produces the expected "pattern_timeout" error, avoiding the full regex time budget during normal unit runs. If a smaller input cannot reliably trigger the timeout, mark this specific test as slow instead.

## Finding #235

In @tests/mcp_proxy/test_wait_for_output.py around lines 43 - 221, Split test_wait_branches into focused tests, separating each independent wait_for_output scenario such as matching, timeout, terminal status, validation errors, pane loss, capture failure, pattern timeout, collision precedence, and cancellation cleanup. Keep each scenario’s existing setup and assertions intact, using parametrization only for genuinely similar payload-validation cases so failures identify the affected branch.

## Finding #236

In @tests/mcp_proxy/test_wait_for_output.py around lines 181 - 192, Make the capture-versus-deadline test deterministic by removing its dependence on real wall-clock scheduling. Update the _run() invocation around deadline_collision_tmux to use a fake or patched agents.time.monotonic timeline, or otherwise provide a generous timeout so the intended capture_failed result and three capture_pane attempts are guaranteed without changing the production behavior.

## Finding #237

In @tests/mcp_proxy/test_wait_for_output.py around lines 197 - 202, Update the return annotation of the blocking_capture helper to reflect that it never returns a value, while preserving its existing capture_started signaling, indefinite wait, and capture_finished cleanup behavior.

## Finding #284

In @src/gobby/storage/hub/async_ops.py around lines 73 - 78, The dynamic SET LOCAL statements in the timeout setup should use psycopg.sql composition to satisfy static analysis while preserving integer timeout values. Update both statement_timeout and lock_timeout branches to build the SQL with psycopg.sql.SQL(...).format(psycopg.sql.Literal(timeout_ms)) before connection.execute, keeping _timeout_milliseconds and _require_remaining unchanged.

## Finding #285

In @src/gobby/storage/hub/async_ops.py around lines 145 - 155, Update_result_or_raise so QueryCanceled and LockNotAvailable are mapped to _raise_timeout only when no commit is in flight or the commit outcome has already been observed; when state.commit_submitted is true and state.commit_observed is false, route these exceptions through _raise_indeterminate instead. Preserve existing handling for _WorkBudgetExpired and deterministic query failures.

## Finding #286

In @src/gobby/storage/hub/postgres.py at line 40, Make the cross-module postgres_pool API intentional by promoting or explicitly re-exporting_advisory_lock_keys, _PostgresCursor,_conninfo_with_utc_session_timezone, and _validate_identifier, then update their consumers in the hub module to use the public names consistently. Preserve existing behavior while eliminating direct access to underscore-prefixed postgres_pool members.

## Finding #299

In @tests/storage/hub/test_async_ops.py around lines 314 - 427, Split test_termination_matrix into separate pytest-parametrized cases covering blocked connect, fake-connection SET LOCAL blocking, fake-connection statement blocking, foreign-row lock waiting, and proxy-blocked cancellation. Give each case isolated setup and teardown while preserving the existing bounded-timeout, cancellation-count, callback-quiescence, and thread-cleanup assertions.

## Finding #300

In @tests/storage/hub/test_async_ops.py around lines 23 - 27, Add the appropriate pytest category markers to the async tests in this module: mark the live PostgreSQL/socket-dependent tests as integration and the timing-sensitive tests as slow, using the existing pytest marker conventions. Keep pytestmark for asyncio and apply markers at the narrowest test or module scope that covers the affected tests.

## Finding #301

In @tests/storage/hub/test_postgres_placeholder_remap.py around lines 27 - 28, Annotate the_postgres_pool_module helper with a ModuleType return type, and import ModuleType from types alongside the existing imports.

## Finding #302

In @tests/storage/hub/test_protocol_contract.py around lines 146 - 152, Update the conninfo assertion in the PostgresHubDatabase test to compare conninfo_to_dict(database.conninfo) with the expected key-value mapping instead of relying on exact string ordering. Keep the concrete_property immutability assertion unchanged.

## Finding #330

In @src/gobby/mcp_proxy/tools/sessions/_verification.py around lines 121 - 160, Update the manual attestation flow around `receipt_store.upsert` and the subsequent `append_to_bounded_list_variable` call to execute both writes within a single hub database transaction, using parameterized `$N` placeholders for database access. Ensure either both the verification receipt and variable append commit together or both roll back when either operation fails.

## Finding #373

In @tests/mcp_proxy/test_validation_integration.py around lines 505 - 538, Extend the existing_verification_receipt helper to accept an optional cwd parameter, preserving its current default behavior. Replace the inline VerificationReceipt construction loop with calls to_verification_receipt using the per-index command, index, and repo_path cwd, while retaining the existing 303-receipt sequence and ordering.

## Finding #427

In @tests/mcp_proxy/services/test_codex_close_reconciliation.py around lines 23 - 24, Replace the private constant assertion in test_codex_close_reconciliation_default_allows_large_receipt_batches with a behavioral test of the reconciliation path, verifying that the configured timeout is passed to the awaited reconciliation call when processing a large receipt batch. Avoid asserting the literal value or directly inspecting _CODEX_RECONCILE_TIMEOUT_SECONDS.

## Finding #428

In @tests/mcp_proxy/tools/sessions/test_compact_self.py around lines 426 - 429, Strengthen the assertions in the compact prompt test around captured_prompts[0] to verify the actual direct skill-loading instruction: require the get_skill tool reference and its guidance to call it directly. Keep the existing exclusions for list_mcp_servers, list_tools, and get_tool_schema, while replacing the vacuous “directly” substring assertion with checks that fail if the required-skill instruction is removed.

## Finding #468

In @src/gobby/mcp_proxy/tools/agents_lifecycle_tools.py around lines 254 - 304, After the run ID None check, bind it to a local `resolved_run_id` so the nested `kill_and_deliver` closure retains the narrowed `str` type. Replace the relevant `run_id` references in `terminalize_killed_agent_run`, `_deliver_existing_terminal_run_unshielded`, and `shielded_terminal_delivery` with `resolved_run_id`, while preserving the existing behavior.

## Finding #469

In @src/gobby/mcp_proxy/tools/agents_query_tools.py around lines 121 - 221, Add a regression guard for the no-await critical region in wait_for_agent, covering the span from the second ctx.runner.get_run through conditional cleanup. Assert that subscribe_agent_completion, remove_agent_completion_subscribers, and completion_registry.cleanup remain synchronous and no await is introduced before the critical-region marker.

## Finding #470

In @src/gobby/mcp_proxy/tools/spawn_agent/_failure_cleanup.py around lines 34 - 48, The synchronous database operations inside _fail_run must be offloaded from the event loop. Update _fail_run and its call site in the failure-cleanup flow to use the existing run_terminal_delivery_offload seam for run_storage.fail and run_storage.get, preserving the current failure and child-session handling behavior.

## Finding #471

In @src/gobby/mcp_proxy/tools/spawn_agent/_health.py around lines 117 - 136, Widen the exception handling around deliver_existing_terminal_run in the immediate-spawn failure path so subscriber delivery exceptions are caught and logged instead of escaping the health-check task. Preserve the existing run failure flow and psycopg-specific handling, while ensuring unexpected delivery errors produce a diagnostic logger warning.

## Finding #492

In @tests/mcp_proxy/tools/sessions/test_compact_self.py around lines 716 - 720, Annotate the temp_db and sample_project parameters in test_delayed_archival_refresh_preserves_resumed_session_claim with the appropriate fixture types, preserving the existing return annotation and test behavior.

## Finding #494

In @tests/mcp_proxy/tools/spawn_agent/test_dedup.py around lines 163 - 174, The parametrization for test_allow_closed_task_permits_review_spawn_unless_escalated lacks the closed-and-escalated scenario named by the test invariant. Add a third case with both closed_at and escalated_at set, expecting no spawn, while preserving the existing open-escalated and closed-reviewable cases.

## Finding #522

In @src/gobby/mcp_proxy/tools/plans/review_evidence.py around lines 66 - 81, Update get_plan_review_snapshot so the non-bytes snapshot validation returns_error_payload directly instead of raising ReviewEvidenceError inside the try block. Keep the existing error code and message, and preserve exception handling for service errors and UnicodeDecodeError.

## Finding #523

In @src/gobby/mcp_proxy/tools/plans/review_evidence.py around lines 151 - 156, Update verify_plan_unchanged, bind_evidence_run, expire_plan_review_evidence, and finalize_plan_review_evidence to catch their expected OSError and psycopg.Error failures and return the existing _error_payload structured response, while preserving the current ReviewEvidenceError handling and success payloads.

## Finding #557

In @tests/mcp_proxy/tools/test_agents.py around lines 1954 - 2002, Extract the duplicated _KillDeliveryRegistry and_record_removals scaffolding into a shared tests/completion_delivery_helpers.py module, exposing reusable DeliveryRegistry and record_removals symbols. Update test_agents.py, test_agent_cancellation.py, spawn_agent/test_health.py, and spawn_agent/test_error_handling.py to import and use these helpers, removing their local duplicate definitions while preserving existing notify, cleanup, and subscriber-removal behavior.

## Finding #558

In @tests/mcp_proxy/tools/test_review_learning.py around lines 126 - 128, Update the context-count assertion in the test to require at least two contexts rather than exactly two, while preserving the existing shared review_learning_service identity assertion.

## Finding #601

In @src/gobby/mcp_proxy/tools/spawn_agent/_generation_endpoint.py around lines 73 - 79, Update the exception handler around ensure_local_model in the spawn-agent generation endpoint to catch only the documented LocalModelError, preserving its ValueError wrapping and exception chaining while allowing unrelated programming errors such as AttributeError or TypeError to propagate unchanged. Import or reference LocalModelError from the appropriate local-model module.

## Finding #655

In @src/gobby/mcp_proxy/server.py at line 51, Update the constructor path around RecommendationService and PromptLoader so PromptLoader never receives a missing database: either make the server’s db parameter required as a HubDatabase, or explicitly handle None in RecommendationService before rendering recommendations. Preserve the existing recommendation flow for valid HubDatabase instances.

## Finding #656

In @src/gobby/mcp_proxy/services/result_offload.py around lines 233 - 246, Update the envelope budget check in the result-building method containing _fit_structure, _fit_text_field, and_fit_matches so exceeding the working budget is handled without raising AssertionError to the caller. Log the over-budget condition and return a degraded but valid envelope, preserving successful tool-call results; ensure this handling covers both normal and fallback paths in_maybe_offload_sync and _execute_tool_dispatch.

## Finding #657

In @src/gobby/mcp_proxy/services/tool_execution.py around lines 44 - 54, Eliminate the synchronous full-payload deepcopy in _identity_arguments and the second copy around track_proxy_outcome. Build the tracking identity from a normalized or hashed representation, or defer any required copying into the existing asyncio.to_thread call, while preserving identity generation from the effective enforcement-modified arguments.

## Finding #658

In @src/gobby/mcp_proxy/stdio_proxy.py around lines 316 - 317, Bound the model-generated intent before assigning it to request_kwargs["params"] in the non-wait-tool path. Define and use a module-level_MAX_INTENT_QUERY_CHARS limit (for example, 1024) to truncate the query-string intent, while leaving the wait-tool body JSON path unchanged.

## Finding #659

In @src/gobby/mcp_proxy/tools/results.py around lines 216 - 225, Update_validate_search_arguments to use the existing _MAX_SEARCH_LIMIT constant instead of the literal 50 when validating limit, keeping the lower bound and error behavior unchanged and aligning validation with the schema bound.

## Finding #660

In @src/gobby/mcp_proxy/tools/results.py around lines 244 - 269, Update_hydrate_matches to replace the per-hit db.fetchone calls with one bulk query for all hit.id values scoped to result_id, then map the returned rows by chunk id and build matches in the original hits order. Preserve skipping hits without a matching row and the existing field conversions and output shape.

## Finding #672

In @tests/mcp_proxy/services/test_result_offload.py around lines 20 - 24, Add a module-level pytest unit marker in test_result_offload.py alongside the existing imports and constants, using the same pytest.mark.unit declaration as sibling tests. Ensure pytest is imported if needed so marker-based selection includes all tests in this module.

## Finding #673

In @tests/mcp_proxy/test_results_tools.py around lines 18 - 19, Register and apply a module-level pytest marker in tests/mcp_proxy/test_results_tools.py to categorize the contained tests, using the project’s existing marker convention (unit, slow, integration, or e2e) and preserving the distinction needed for the DB-backed and pure-mock cases.

## Finding #674

In @tests/mcp_proxy/test_stdio_proxy.py around lines 25 - 46, The tool-capturing logic is duplicated between_capture_stdio_tools and TestMCPToolsWrapper._register_tools. Extract the shared MagicMock .tool decorator registration helper into a common test fixture or utility, then update both callers to reuse it while preserving their existing captured-name and callable behavior.

## Finding #675

In @tests/mcp_proxy/test_stdio_proxy.py around lines 132 - 185, The envelope size limit is duplicated as a local literal across related tests, allowing assertions to drift from production behavior. Replace max_envelope_chars in test_stdio_final_wait_envelope_stays_within_shared_cap and test_stdio_final_retrieval_response_stays_within_shared_cap with the actual shared production cap or one shared test constant, and reuse that same symbol in the corresponding daemon-tools and execution-offload tests.

## Finding #701

In @src/gobby/mcp_proxy/tools/communications.py around lines 70 - 125, The send_attachment function currently permits arbitrary readable files; constrain resolved_path to an allow-listed workspace root before calling communications_manager.send_attachment. Validate that the resolved path remains within the configured workspace directory, reject paths outside it with the existing failure response, and preserve normal file and attachment handling for allowed paths.

## Finding #702

In @src/gobby/mcp_proxy/tools/communications.py around lines 258 - 259, Update the replace call creating updated in the responder-project persistence flow to set updated_at to the current UTC time, matching the admit_inbound_message channel update in manager.py; preserve the existing config_json update and pass the refreshed channel to communications_manager.update_channel.

## Finding #703

In @src/gobby/mcp_proxy/tools/sessions/_registration.py at line 96, Update the session registration title-source assignment to use the shared manual_title_source helper from _title_defaults instead of duplicating the raw "manual" literal and blank-string check. Ensure the helper returns MANUAL_TITLE_SOURCE only for non-blank string titles and None otherwise, then reuse it in this call site.

## Finding #704

In @src/gobby/mcp_proxy/tools/spawn_agent/_health.py around lines 167 - 187, Update schedule_tmux_health_check and the related _health_check_tasks/cancel_health_checks flow to track the returned pending TimerHandle before it fires, cancel those handles during shutdown, and remove each handle when its callback starts. Ensure cancellation prevents_start_tmux_health_check from creating tasks or mutating run state after shutdown begins.

## Finding #718

In @tests/mcp_proxy/services/test_session_context.py around lines 25 - 44, Mark test_should_synthesize_direct_after_tool with @pytest.mark.unit alongside its existing @pytest.mark.parametrize decorator so marker-based selection includes this pure unit test.

## Finding #719

In @tests/mcp_proxy/tools/spawn_agent/test_health.py around lines 132 - 147, Mark test_scheduled_health_check_does_not_create_a_sleeping_task with the pytest unit marker by adding @pytest.mark.unit alongside its existing asyncio marker.

## Finding #720

In @tests/mcp_proxy/tools/test_communications.py around lines 434 - 459, Add the pytest-asyncio marker to test_send_message_exposes_inline_keyboard_metadata, matching the other async tests in the file so pytest executes its coroutine body and assertions.

## Finding #744

In @src/gobby/mcp_proxy/tools/memory_recall.py around lines 28 - 43, Wrap the queue lookups in get_recall_memories, including queue.get and queue.pending, with the existing try/except used around _retrieve_memories. On lookup exceptions, return the standard failure payload containing recall_request_id and the error details instead of allowing the exception to propagate.

## Finding #779

In @tests/mcp_proxy/tools/test_tasks_create_coverage.py around lines 531 - 540, Strengthen the create_task test around registry.call so it verifies the explicit validation_criteria is forwarded unchanged through create_task_with_decomposition. Inspect the resulting task or relevant mock call arguments and assert the value is exactly “Test task completion is observable.” while preserving the existing update_task not-called assertion.

## Finding #780

In @tests/mcp_proxy/tools/test_tasks_lifecycle_coverage.py around lines 465 - 467, Update the failure assertions in the task lifecycle test so the configured link_commit failure is verified by asserting mock_task_manager.link_commit was called once (optionally with expected arguments), while retaining close_task.assert_not_called() and the existing invalid_commit_sha result checks.
