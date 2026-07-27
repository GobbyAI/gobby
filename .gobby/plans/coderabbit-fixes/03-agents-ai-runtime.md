# CodeRabbit Fixes: Agents and AI Runtime

Agent lifecycle, detection, terminal delivery, AI endpoint, and tool-chat runtime fixes.

Unresolved original findings: **58**

Original finding IDs: 221-224, 228, 231, 263-266, 288, 329, 372, 391-395, 397-401, 423-424, 452-457, 490, 554, 592-595, 600, 613-615, 684-695, 712-716

## Finding #221

In @tests/agents/test_lifecycle_monitor_registry.py around lines 1 - 13, Mark the tests in tests/agents/test_lifecycle_monitor_registry.py as integration tests by adding the project’s established pytest integration marker at module scope, alongside the imports or module docstring. Keep the existing test behavior and temp_db-backed setup unchanged.

## Finding #222

In @tests/agents/test_provider_rotation_registry.py around lines 1 - 13, Add the pytest import and declare module-level pytestmark as pytest.mark.unit in tests/agents/test_provider_rotation_registry.py, matching the existing unit-test convention so marker-based selection includes this test module.

## Finding #223

In @tests/agents/test_provider_routing.py around lines 15 - 22, Rename the test stub class MutableRegistry to StaticRegistry (or FakeRegistry) and update every reference to it in tests/agents/test_provider_routing.py; keep its compile-on-initialization and lookup behavior unchanged.

## Finding #224

In @tests/agents/test_provider_routing.py around lines 1 - 13, Add the module-level pytest marker declaration in tests/agents/test_provider_routing.py using the existing pytest import: set pytestmark to pytest.mark.unit so all tests in the module are classified as unit tests.

## Finding #228

In @tests/agents/tmux/test_pane_monitor_registry.py around lines 31 - 39, The test around monitor._sync_interactive_attention should verify transition_async call arguments rather than relying only on await_count. Assert that the beta trust invocation triggered the transition and that the repeated stale alpha trust invocation did not, while preserving the existing registry identity assertion.

## Finding #231

In @tests/ai/test_embedding_switch_runner.py around lines 163 - 166, Rename the local variable assigned from store.operations[1][1] in the owner_set assertion block from config_entries to a name representing the run ID, such as run_id, and update its comparison with journal.run_id accordingly; leave the entries dictionary access at index [2] unchanged.

## Finding #263

In @src/gobby/agents/attention_metadata.py around lines 46 - 102, Add a clear/delete API to AttentionMetadataStore that removes the specified entry immediately and publishes the appropriate cursor-ordered update, then invoke it from the attention-resolution paths clear_attention and clear_attention_after_injection so resolved chips disappear before TTL expiry. Preserve existing set, get, and snapshot behavior for unaffected entries.

## Finding #264

In @src/gobby/agents/idle_check_handler.py around lines 18 - 25, The idle-check handler is hardcoded to Codex instead of using the provider-neutral reader contract. Replace direct CODEX_MODEL_CAPACITY_MESSAGE and read_codex_transcript_snapshot usage with WatchdogReaderRegistry().for_provider(run.provider), then use the resolved reader’s capacity_pane_message and read(...) methods while preserving the existing CapacityRecoveryState and WatchdogTranscriptSnapshot flow.

## Finding #265

In @src/gobby/agents/watchdog/codex.py around lines 45 - 126, Update _read_codex_snapshot to maintain per-transcript resume state keyed by transcript_path, including the last processed byte offset or line and accumulated snapshot fields such as tail, turn markers, provider errors, and activity metadata. On subsequent calls, seek to the saved cursor and scan only newly appended JSONL content, advancing and persisting the cursor; handle truncation or replacement by resetting state and scanning from the beginning while preserving the existing classification behavior.

## Finding #266

In @src/gobby/agents/watchdog/registry.py at line 15, Replace the assert comparing _READERS with KNOWN_WATCHDOG_PROVIDERS in the watchdog registry with an explicit conditional that raises an appropriate exception when the sets differ, ensuring the invariant is enforced even under Python optimization.

## Finding #288

In @tests/agents/test_attention_metadata.py around lines 127 - 143, Update the attention reconciliation or roster refresh flow associated with attention_changed events and epoch changes so clients also discover entries removed by passive TTL expiry, without relying solely on follow-up events. Ensure the client-side polling or scheduled refresh path clears stale blocked badges when an entry expires, while preserving existing event-driven reconciliation behavior.

## Finding #329

In @src/gobby/llm/context_windows.py around lines 81 - 82, Replace the unbounded_UNKNOWN_CONTEXT_WINDOW_WARNED_MODELS set with bounded warning-dedup state so dynamically named models cannot grow memory usage indefinitely. Preserve one-warning-per-model behavior within the bound, and provide a clear reset hook for tests to prevent cross-test state leakage.

## Finding #372

In @tests/llm/test_context_window.py around lines 7 - 9, Add a regression test in the context-window test suite that passes a non-primitive provider_reported_context_window value, such as a dict or list, through coerce_context_length and resolve_context_window_with_source, and verifies the expected safe result without raising UnboundLocalError. Follow the existing test patterns and preserve current unknown/registry attribution behavior.

## Finding #391

In @src/gobby/agents/idle_check_handler.py at line 25, Promote _find_transcript_on_disk in gobby.sessions.transcript_paths to the public find_transcript_on_disk API, update idle_check_handler and other external callers to import the public name, and retain a thin private alias only if existing internal callers require it.

## Finding #392

In @src/gobby/agents/idle_check_handler.py around lines 712 - 726, Update the fallback transcript reads in _recover_reasoning_idle and_log_transcript_snapshot to call _resolve_transcript_path instead of accessing session.transcript_path directly. Preserve the existing no-path and read-failure handling, while allowing the resolver to discover on-disk transcripts and reject the "missing_transcript" sentinel consistently.

## Finding #393

In @src/gobby/agents/idle_check_handler.py around lines 245 - 283, The transcript resolver can continue returning an outdated cached file after the transcript is rotated during an active run. Update _resolve_transcript_path to revalidate cached paths against session.updated_at (for example, compare file mtime) and rediscover when the cached file is older, while preserving the existing cache lookup for current files and the fallback behavior when no valid transcript is found.

## Finding #394

In @src/gobby/agents/idle_check_handler.py at line 263, Define a shared constant for the "missing_transcript" sentinel and update the transcript path checks in the relevant idle-check handling code to reference it instead of repeating the literal. Ensure all existing comparisons preserve their current behavior.

## Finding #395

In @src/gobby/agents/watchdog/claude.py around lines 64 - 78, The lookup in _assistant_payload_type contains an unreachable "user_input" mapping because _assistant_activity_kind never returns that value. Remove the "user_input" entry while preserving the existing mappings and API-error handling.

## Finding #397

In @src/gobby/agents/watchdog/claude.py around lines 143 - 162, The user-record handling in the transcript scanner incorrectly starts a new turn for tool results. In the `record_type == "user"` branch, keep the existing `payload_type` distinction and summary creation, but only assign `turn_started_event`, `latest_turn_event`, `latest_turn_kind`, and `latest_activity_kind = "user_input"` when `block_types` does not include `"tool_result"`; tool-result records should still be appended and validated without changing turn-start bookkeeping.

## Finding #398

In @src/gobby/agents/watchdog/droid.py around lines 99 - 108, The Droid snapshot scanning logic around the session_end handling and the last-assistant-message path must populate latest_turn_event/latest_turn_kind for completed turn boundaries. Map session_end, and any applicable final assistant message, to turn_kind="completed" so_completed_turn_recovery_due can recover completed turns; otherwise explicitly preserve and document the intentional reduced capability for this Droid iteration.

## Finding #399

In @src/gobby/agents/watchdog/droid.py around lines 109 - 115, Add a brief explanatory comment beside the todo_state validation in the watchdog scan logic, specifically around valid_todos, documenting that persisted records intentionally support both a list and a dict containing a string todos value. Do not change the validation behavior.

## Finding #400

In @src/gobby/agents/watchdog/qwen.py at line 135, Update the event_type assignment in the record construction to preserve "tool_result" as event_type when record_type is "tool_result", rather than remapping it to "message"; leave other record types unchanged.

## Finding #401

In @src/gobby/agents/watchdog/registry.py around lines 11 - 17, Update the _READERS mapping annotation to use dict[str, TranscriptWatchdogReader] without the None union, since all registered providers have readers. Preserve for_provider’s existing behavior of returning None only when the provider is unknown.

## Finding #423

In @tests/agents/test_lifecycle_monitor_watchdog_diagnostics.py around lines 200 - 296, Update test_idle_reasoning_watchdog_interrupts_supported_reader_and_records_task_event to either clearly identify Codex-only coverage in its name or parameterize it across readers whose supports_reasoning_interrupt is true. Add a negative case covering the newly supported Claude, Droid, Grok, and Qwen readers with supports_reasoning_interrupt set to false, verifying the watchdog does not interrupt them.

## Finding #424

In @tests/agents/test_lifecycle_monitor_watchdog_diagnostics.py around lines 183 - 197, Decouple the diagnostics assertions from `_log_transcript_snapshot`’s exact format strings and positional argument indexes. In the affected test sections, render each matching warning using the logger’s format string and remaining arguments (or reuse module-level format constants shared with the handler), then assert against the rendered payload while preserving the existing diagnostic-content checks.

## Finding #452

In @src/gobby/agents/agent_cleanup.py around lines 85 - 98, Update submit_terminal_delivery_offload so the _terminal_delivery_submit-unset path dispatches callback asynchronously through a small dedicated executor instead of executing it inline on the calling thread. Preserve the Future[T] return contract and propagate callback results and BaseException failures through that future, while reusing an existing executor if one is available.

## Finding #453

In @src/gobby/agents/agent_cleanup.py around lines 184 - 220, Update the completion notification flow around completion_registry.notify and cleanup so completion_registry.cleanup(run_id) is not called when notify raises. Track whether notification succeeded and only evict registry state after a successful notification, while preserving subscriber cleanup and return behavior.

## Finding #454

In @src/gobby/agents/capture.py around lines 344 - 354, Update _async_storage_call to accept an explicit_run_id parameter and pass that value to shielded_terminal_delivery instead of deriving run_id by scanning args; update every call site, including the _persist_capture_sync capture-persist path, to provide the appropriate run id. Preserve the existing operation callback behavior, and handle the None result from shielded_terminal_delivery explicitly rather than hiding it with cast(ResultT, ...), so closed admission is not treated as a normal storage result.

## Finding #455

In @src/gobby/agents/completion_subscribers.py around lines 87 - 121, The strict and non-strict branches duplicate persistence and registry logic. Extract the shared CompletionSubscriberManager construction and add_completion_subscribers call into a local_persist(db) helper, then keep strict ordering as persist before register and non-strict ordering as register before best-effort persist, preserving their existing error handling and inserted_session_ids behavior.

## Finding #456

In @src/gobby/agents/resume_executor.py around lines 54 - 55, Replace the broad Any annotations for completion_registry and _RunStorage.db with CompletionEventRegistry and HubDatabase, respectively, including the corresponding optional type where applicable. Import these concrete contracts under TYPE_CHECKING to avoid runtime circular imports, and update the affected terminal-delivery signatures such as _deliver_existing_terminal_run_unshielded consistently.

## Finding #457

In @src/gobby/agents/run_completion.py around lines 50 - 54, Update the result payload construction in the run completion flow to copy any notify_result before normalization, always preserve or inject the current run_id, and include error only when it has a non-None value. Remove the fallback’s unconditional error field so delivered payloads have the same normalized shape whether notify_result is supplied or absent.

## Finding #490

In @tests/agents/test_agent_cleanup.py around lines 22 - 65, Add an autouse fixture in the terminal-delivery tests that resets delivery state before each test and ensures terminal-delivery admission is open, using reset_terminal_delivery_offload() and the existing admission-control API. Keep the fixture cleanup robust so failures cannot leave_terminal_delivery_admission_open or_in_flight_terminal_deliveries affecting later tests.

## Finding #554

In @tests/agents/test_plan_adversary_manifest.py around lines 132 - 143, Add tool-level enforcement coverage to TestCoordinatorOwnedWrites by asserting the review step’s blocked_mcp_tools configuration includes apply_plan_review_manifest, finalize_plan_review_evidence, and checkpoint_plan_review_lesson_mint. Keep the existing instruction-text assertions and use the manifest/configuration symbols already exposed by the test fixtures rather than relying only on agent prose.

## Finding #592

In @src/gobby/agents/resume_executor.py around lines 152 - 156, Update the provider handling in the resume executor around the provider-specific branch so non-codex providers set the endpoint base URL and API token using the environment-variable names expected by each provider’s_resume_api_base() and prepare_sandbox_launch flow. Keep codex on its existing codex-specific path, and ensure droid, grok, and qwen receive the selected endpoint under their matching provider-specific base-url and API-key variables.

## Finding #593

In @src/gobby/ai/_tool_chat_spawn.py around lines 476 - 488, In the wire_api == "responses" branch of the tool-chat spawning flow, replace direct indexing of self._config.ai.generation.endpoints with the existing resolve_generation_endpoint helper. Preserve the current endpoint validation and pass the resolved endpoint into codex_endpoint_config_overrides and codex_endpoint_env so missing endpoints raise the helper’s descriptive ValueError.

## Finding #594

In @src/gobby/ai/endpoint_activation.py around lines 53 - 67, Wrap the complete activation probe chain in a single overall timeout at the caller around the text, tool, and vision probes, rather than adding separate per-attempt limits in_retry_activation. Ensure the timeout covers all serial probe execution and causes the synchronous PUT to terminate when exceeded, while preserving existing retry behavior within the allotted duration.

## Finding #595

In @src/gobby/ai/vision.py around lines 146 - 147, Update CodexEndpointVisionExtractAdapter.stop() to check self._client.is_connected before calling self._client.stop(), matching the guarded cleanup behavior in CodexWebChatBackend.stop(). Ensure never-started or failed-start clients are skipped while connected clients still stop normally.

## Finding #600

In @src/gobby/llm/sdk_utils.py around lines 258 - 268, The_section_priority function uses a bare 25 for preamble sections; define a module-level_PREAMBLE_PRIORITY constant and return it for empty section titles. Keep the constant aligned with the intended priority relative to unknown_priority.

## Finding #613

In @tests/agents/test_merge_orchestrator_contract.py at line 396, Update the allowed MCP tools assertions in the merge orchestrator contract test to verify that the obsolete gobby-sessions:record_verification_evidence tool is absent, while preserving the existing assertion for gobby-merge:verify_in_worktree.

## Finding #614

In @tests/agents/test_resume_executor.py around lines 128 - 230, Add the required unit pytest marker to test_resume_responses_endpoint_rebuilds_child_scoped_codex_config alongside pytest.mark.asyncio, preserving the test’s existing behavior and other markers.

## Finding #615

In @tests/ai/test_capability_registry.py at line 428, Update the VISION_EXTRACT assertion in the capability registry tests to use the generic endpoint provider key, matching the TEXT_GENERATE test, and verify that this key is not registered. Remove the obsolete "local" lookup so the test remains meaningful after the provider-key migration.

## Finding #684

In @src/gobby/ai/_tool_chat_codex.py around lines 238 - 274, The turn limit is only enforced in handle_dynamic_tool, allowing tool-free turns to exceed limits.max_turns. Update record_raw_response to detect when the incremented turns reaches the configured cap and schedule an interrupt for the active turn, preserving the existing stop_reason and interrupt behavior used by handle_dynamic_tool.

## Finding #685

In @src/gobby/ai/_tool_chat_codex.py around lines 306 - 312, Update the teardown in the chat client lifecycle around client.start() and start_thread so cleanup is only attempted when startup completed successfully, or safely handles cleanup failures without replacing the original exception. Guard remove_notification_handler, remove_request_handler, and await client.stop() while preserving the original startup error.

## Finding #686

In @src/gobby/ai/_tool_chat_codex.py around lines 135 - 136, Update_is_tool_error to reuse _tool_result_is_error instead of checking for the obsolete `"ok":false` field, while preserving the existing “[error” detection. Ensure builtin results containing `"success": false` are classified as failures consistently with the other adapter.

## Finding #687

In @src/gobby/ai/_tool_chat_contracts.py around lines 118 - 123, Add an effective_limits accessor to ToolChatRequest that returns the configured limits or a default ToolLoopLimits instance, then update the referenced adapters and spawn implementations to use it instead of duplicating request.limits fallback logic.

## Finding #688

In @src/gobby/ai/_tool_chat_droid.py around lines 233 - 260, The request-handling flow around_NATIVE_TOOL_METHODS must respond to every server-initiated message with type "request". Preserve the existing disabled-native response, and add a fallback response for unhandled request methods using the original request_id, JSON-RPC error code -32601, and an appropriate method-not-found message before reaching the notification handling.

## Finding #689

In @src/gobby/ai/_tool_chat_droid.py around lines 140 - 152, Enforce limits.loop_timeout_seconds across the Droid request and notification flow, including request() and the while-loop awaiting client.next_notification(). Apply the configured deadline so stalled JSON-RPC futures or notification waits terminate instead of hanging indefinitely, and ensure timeout cleanup removes the pending request and stops or propagates the timeout consistently through ToolLoopController.

## Finding #690

In @src/gobby/ai/_tool_chat_droid.py around lines 469 - 473, Update the cleanup in the surrounding tool-chat flow so server.stop() runs even when client.stop() raises. Nest or otherwise independently guard the teardown operations, preserving both cleanup attempts and the existing exception propagation behavior.

## Finding #691

In @src/gobby/ai/_tool_chat_mcp_server.py around lines 186 - 196, Centralize ToolRuntime.execute result classification in a new shared predicate in _tool_chat_tools.py that recognizes bracketed errors, JSON failures such as {"success":false,...}, and the existing typed failure formats. Replace the local checks in this MCP handler,_tool_chat_codex.py, and _tool_result_is_error in _tool_chat_adapters.py with that predicate, preserving each surface’s existing response behavior.

## Finding #692

In @src/gobby/ai/_tool_chat_mcp_server.py around lines 93 - 100, Update the stop method to explicitly close the saved _socket during teardown, using a finally block around runner.cleanup() so the socket closes even if cleanup raises; preserve the existing state reset and only close the socket when it is present.

## Finding #693

In @src/gobby/ai/_tool_chat_service.py around lines 109 - 119, Define and export a shared limit stop-reason constant in_tool_chat_contracts.py alongside ToolLoopLimits, containing max_turns, max_tool_calls, and timeout. Update the tool-chat service condition around result.stop_reason to use this constant instead of an inline set, and have adapters reuse it where they produce these stop reasons.

## Finding #694

In @src/gobby/ai/_tool_chat_spawn.py around lines 48 - 57, Update the module-level __all__ to include the locally defined GrokSpawnToolChatAdapter and QwenSpawnToolChatAdapter alongside the existing CodexSpawnToolChatAdapter and DroidSpawnToolChatAdapter re-exports, or remove __all__ entirely; preserve all intended public adapters.

## Finding #695

In @src/gobby/ai/codex_endpoint.py around lines 87 - 96, Update codex_endpoint_app_server_env to accept endpoint_name and use a distinct child directory under the Codex endpoints home for each endpoint, while preserving the existing base environment. Pass endpoint_name through all activation and vision call sites and update affected tests to validate the endpoint-specific CODEX_HOME path.

## Finding #712

In @tests/ai/test_tool_chat_builtins.py around lines 262 - 265, Resolve the type contract mismatch in ToolLoopLimits and the related tests without using cast to pass float values as ints. Since tool_timeout_seconds is passed to asyncio.wait_for and run_argv, keep or restore its float-compatible annotation and update the affected parametrized tests, including the cases around test_nonpositive_outer_timeout_is_rejected and the additional occurrences, to pass values directly while preserving validation of nonpositive timeouts.

## Finding #713

In @tests/ai/test_tool_chat_protocols.py around lines 486 - 488, Replace the two fixed asyncio.sleep(0) calls before_call_mcp in the test fixture with deterministic readiness synchronization: poll the adapter’s pending-call registration or await an asyncio.Event set when the handshake is ready, then invoke_call_mcp only after readiness is confirmed.

## Finding #714

In @tests/ai/test_tool_chat_protocols.py at line 571, Update the assertion in the test covering factory.options["cwd"] to compare equivalent path representations, such as converting the configured string path to a Path before comparing with tmp_path. Assert the intended working-directory isolation property rather than relying on a str-versus-Path comparison.

## Finding #715

In @tests/ai/test_tool_chat_service.py around lines 227 - 231, Replace the private_default_limits round-trip assertion in test_tool_chat_service_uses_one_canonical_request_deadline with an end-to-end chat_result test using ToolLoopLimits(loop_timeout_seconds=1) and a _SlowAdapter. Exercise at least two candidates, make the first slow enough to consume part of the budget, and assert the second receives the remaining timeout rather than a fresh one-second deadline; also assert total elapsed time remains near the single shared budget.

## Finding #716

In @tests/ai/test_tool_chat_tools.py around lines 176 - 179, Resolve the contradictory assertions around the truncation result in the test: if marker preservation is intended, assert the marker and the payload separately while retaining the UTF-8 byte-length check; otherwise remove the stale truncation-marker comment and redundant split assertion. Align the test with the intended behavior of the truncation logic in the tool chat output path.
