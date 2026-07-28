# CodeRabbit Fixes: Servers, Sessions, WebSocket, and Runtime Boundaries

HTTP/WebSocket routes, session processing, attention state, and runtime ownership boundaries.

- Forward-port source: `e95f9be14bcd1d821de8aff8efdea73fdf3536ec`
- Original task: `#19007`
- Integration task: `#19173`
- Packet base: `9c34494c6ce5d1c6f8688fea7c042804907b3136`

Source findings: **53**

Original finding IDs: 205-207, 209, 215-216, 248-253, 267, 281-283, 295-296, 344-347, 419, 458, 481-483, 489, 495, 501-502, 545-546, 569-570, 602-607, 661, 676-677, 706-709, 722-723, 748, 781-782

## Finding #205

In @src/gobby/servers/routes/attention.py at line 578, Replace the direct private `tmux._base_args()` calls in the route with a public accessor on TmuxSessionManager, such as base_args() or cli_prefix(), and use that accessor at both affected tmux_cmd assignments. Preserve get_tmux_prefix_for_context for the session-context path and ensure the new public method returns the same argument sequence.

## Finding #206

In @src/gobby/servers/routes/attention.py around lines 396 - 412, Update the roster-building flow around _load_task_payload to avoid awaiting one task lookup per run sequentially. Collect distinct task_ids from runs, load their payloads concurrently or through a batched query before the entry loop, cache the results by task_id, and reuse that cache when assigning each entry’s task field.

## Finding #207

In @src/gobby/servers/routes/attention.py at line 133, Update the lock handling around the module-level locks map and the code at the referenced wait path so entry_id is resolved and validated before creating or retrieving a lock. Ensure locks are removed after the associated operation completes and no waiters remain, including legitimate finished runs, while preserving synchronization for concurrent requests targeting the same valid entry.

## Finding #209

In @src/gobby/sessions/tmux_context.py around lines 45 - 60, The helper get_tmux_pane_pid currently reads parent_pid, so either rename it to reflect the parent-process PID or update it to read the actual tmux pane PID field and keep its callers consistent. Add brief docstrings to both get_tmux_pane_pid and get_tmux_session_name describing their input and returned value.

## Finding #215

In @src/gobby/storage/agents/_sandbox_records.py around lines 53 - 70, Bound violation-log processing in _read_violations so include_events=False does not scan the entire file on every to_brief() serialization. Prefer using a persisted count with writer-side rotation/capping; otherwise return an explicitly bounded or truncated count while preserving recent-event collection behavior when include_events=True.

## Finding #216

In @src/gobby/storage/agents/_sandbox_records.py around lines 58 - 69, Update the file-reading logic in the sandbox record serialization flow to safely handle invalid UTF-8 by opening the file with replacement decoding or catching UnicodeError during iteration, treating affected lines as malformed records without aborting serialization. Preserve the existing JSONDecodeError skip behavior and add a regression test covering corrupt UTF-8 input.

## Finding #248

In @tests/servers/routes/test_projects_routes.py around lines 671 - 694, Extend test_purge_project_uses_runner_service with meaningful negative-path tests for POST /api/projects/{id}/purge: verify the expected HTTP status when project_purge_service is unavailable on server._runner, and when PurgeService returns protected or failed PurgeOutcome values. Assert both response status codes and outcome payloads, ensuring failed purges are not accepted as HTTP 200.

## Finding #249

In @tests/servers/routes/test_projects_routes.py at line 687, Update the test setup to use the public server wiring API by calling set_runner_getter(...) with the runner provider instead of assigning server._runner directly. Preserve the existing PurgeService-backed runner behavior while avoiding the backward-compatible_runner test hook.

## Finding #250

In @tests/servers/test_attention_respond.py around lines 158 - 160, Update the assertions around manager.get in the affected test cases, including the block near the accepted response and the matching case near line 279, to first assert the returned AttentionState is not None, then access .state on that narrowed result. Preserve the expected None state assertion and follow the existing strict-mypy pattern used elsewhere in the file.

## Finding #251

In @tests/servers/test_attention_roster.py around lines 110 - 158, Replace the timing-based negative assertions on second_done and transition_done with deterministic mutual-exclusion checks, such as tracking concurrent critical-section entries and asserting the count never exceeds one. Keep the existing synchronization and completion assertions, and ensure the test validates blocking through the invariant rather than a fixed 0.05-second timeout.

## Finding #252

In @tests/servers/test_attention_roster.py around lines 262 - 265, Reorder the assertions in the roster response test so `roster.status_code == 200` is validated before calling `roster.json()` or accessing `["entries"]`. Keep the existing sequence assertion and entry lookups unchanged after the status check.

## Finding #253

In @tests/servers/test_attention_roster.py around lines 99 - 115, Capture exceptions from both worker-thread targets, including failures from _open or manager.transition, in a shared errors collection; wrap first and second_transition so each records its exception instead of allowing threading to swallow it. After both joins, assert that no worker errors were collected before validating event order and sequence.

## Finding #267

In @src/gobby/app_context.py at line 84, Update the attention_metadata_store annotation in the app context class to use AttentionMetadataStore | None instead of Any | None, and add the AttentionMetadataStore import under TYPE_CHECKING consistent with the existing type-hint import pattern.

## Finding #281

In @src/gobby/servers/_app_ui.py around lines 59 - 73, Update the WebSocket handling flow around WebSocketServer._handle_connection so handler exceptions are caught, logged, and followed by the existing fallback close whenever the adapter remains open or connected. Prefer adding and using a public WebSocketServer entry point instead of calling the private _handle_connection directly, while preserving the clean-return behavior.

## Finding #282

In @src/gobby/servers/websocket/asgi_adapter.py around lines 35 - 41, Update WebSocketAdapter.close to tolerate Starlette websocket close failures caused by an already-disconnected peer: preserve the existing closed/disconnected guard and state updates, but catch and suppress the expected close exception from_websocket.close so fallback cleanup cannot raise.

## Finding #283

In @src/gobby/servers/websocket/broadcast.py at line 16, Move the AttentionMetadataStore import in BroadcastMixin’s module into a TYPE_CHECKING-only block, since it is used only for annotations. Preserve annotation resolution at runtime using the module’s existing annotation strategy, without changing BroadcastMixin behavior.

## Finding #295

In @tests/servers/test_ws_asgi_endpoint.py around lines 107 - 120, Expand the WebSocket endpoint tests around_WebSocketServer and _handle_connection so they cover an actual handler exception rather than only return_immediately, including the expected 1011 behavior. Add cases for websocket_server being None with the 1013 response and for authentication-disabled configuration, while retaining the existing clean-return coverage.

## Finding #296

In @tests/sessions/test_acp_lifecycle_service.py around lines 164 - 196, Add a pytest-asyncio test covering ACPSessionLifecycleService.delete when session_manager.delete returns False: ensure the initial session lookup succeeds, then configure the fake manager’s delete behavior to return False, invoke delete, and assert ACPSessionNotFoundError. Reuse the existing _FakeSessionManager and_FakeSession patterns without changing the existing deletion tests.

## Finding #344

In @src/gobby/servers/routes/sessions/lifecycle.py around lines 191 - 202, Extract the duplicated context_window_overrides resolution into a shared helper such as resolve_context_window_overrides(config), preserving the existing dict-or-None coercion. Update this lifecycle code and session_observe_proxy.handle_attach_to_session to use the helper while retaining their distinct config-source resolution.

## Finding #345

In @src/gobby/servers/websocket/chat/backends/codex_turns.py around lines 260 - 268, Update the lifecycle deduplication logic around_apply_post_tool_lifecycle so lifecycle_completed_tool_call_ids is consulted and updated only when tool_call_id is non-empty. Ensure id-less tool events still invoke_apply_post_tool_lifecycle independently, while preserving deduplication for valid tool-call IDs.

## Finding #346

In @src/gobby/servers/websocket/handlers/session_observe_proxy.py around lines 176 - 189, Update the context-window lookup in the session observation handler to execute effective_context_window_for_session through await run_db(mixin, ...), preserving the existing session, live_variables, db, and context_window_overrides arguments and assigning the returned value to context_window.

## Finding #347

In @src/gobby/sessions/processor_lifecycle.py around lines 152 - 153, Update the close-time Codex handling around register_session and flush_session so sessions newly registered for reconciliation are unregistered after the flush completes. Track whether the session was already registered, preserve existing registrations, and call unregister_session only for sessions introduced by this path.

## Finding #419

In @src/gobby/sessions/transcript_index_sidecar.py around lines 255 - 281, Update the append-validation logic in the transcript index validation function to persist and compare a digest of the indexed byte prefix before accepting append mode, in addition to the existing device/inode and size checks. Ensure truncate-and-rewrite files with matching identity metadata are rejected unless the previously indexed prefix still matches, and add a regression test covering an in-place rewrite that produces a larger file.

## Finding #458

In @src/gobby/deployment.py around lines 11 - 21, Memoize the no-argument deployment token in deployment_token, while continuing to compute and resolve explicitly supplied data_root values normally. Preserve the existing stable hash output and ensure deployment_advisory_key still uses the cached default token when token is omitted.

## Finding #481

In @src/gobby/servers/websocket/handlers/session_observe_continue.py around lines 61 - 79, Update the kill_and_deliver/shielded_terminal_delivery flow so killed is set only when terminal delivery admission succeeds and kill_agent actually runs. Have kill_and_deliver return a truthy success sentinel, inspect shielded_terminal_delivery’s result, and preserve the existing failure behavior when it returns None so_release_source_session cannot report success or resume while the agent remains alive.

## Finding #482

In @src/gobby/storage/agents/_lifecycle.py around lines 164 - 167, Remove the duplicated unreachable `return self.get(run_id)` in the lifecycle method, keeping the single return after the `_positive_rowcount(cursor)` guard unchanged.

## Finding #483

In @src/gobby/storage/agents/_lifecycle.py around lines 41 - 50, Update the terminal-transition flow around bounded_transaction and host.get so the final AgentRun read uses the already-registered ambient transaction instead of opening a separate fetchone transaction. Preserve the session expiry update and return the post-update row reflecting the terminal transition.

## Finding #489

In @src/gobby/telemetry/providers.py around lines 95 - 104, Update get_tracer_provider and get_meter_provider to retain references to locally constructed providers or their lifecycle-owned BatchSpanProcessors and metric readers, then update shutdown_providers to flush and shut down those resources before clearing them. Leave reused interpreter-global providers untouched, while ensuring locally created exporters’ background threads terminate and buffered spans/metrics are delivered.

## Finding #495

In @tests/servers/websocket/test_resume_blocked.py around lines 50 - 51, Define module-level constants for the run and source-session UUIDs, then replace every duplicated literal in the helper, the three tests, and the parametrize list with those constants. Keep the existing values and test behavior unchanged.

## Finding #501

In @tests/telemetry/test_providers.py around lines 28 - 33, Update the teardown logic around shutdown_providers to track which tracer and meter providers this fixture constructed locally, and call shutdown only for those instances. Do not shut down reused interpreter-global providers; still clear the provider references after teardown.

## Finding #502

In @tests/test_runner_gate.py around lines 74 - 79, Update the assertion around the runner gate connection test to compare application_name against the explicit contracted gate value, not connect.call_args.kwargs["application_name"]. Use a value distinct from the successor’s name so the test verifies the gate is not terminated by its own fence, while preserving the other expected connection arguments.

## Finding #545

In @src/gobby/servers/routes/configuration_effective.py around lines 130 - 148, The get_effective_config docstring claims a transport constraint that the endpoint does not enforce. Remove the unsupported “non-loopback bind hosts require trusted transport” claim from the docstring, keeping it focused on serving the resolved client configuration.

## Finding #546

In @src/gobby/servers/routes/configuration_effective.py around lines 23 - 28, Update the_is_served_key predicate and _EXCLUDED_KEYS definition so the .routing exclusion is handled by a single mechanism; remove the redundant ai.routing entry from _EXCLUDED_KEYS while preserving exclusion of every key ending in .routing.

## Finding #569

In @tests/servers/routes/test_agent_spawn_routes.py around lines 312 - 314, Add an explicit precondition in the test before the identity assertion to verify that server.services.completion_registry is not None, then retain the kwargs["completion_registry"] identity check. Anchor the change in the test’s create_http_server setup and mock_spawn assertions.

## Finding #570

In @tests/servers/routes/test_configuration_effective_routes.py around lines 83 - 88, Mark all four tests using the real hub database fixture, including test_effective_config_filters_resolves_stringifies_and_overlays and the tests at the referenced locations, with the appropriate integration pytest marker. Preserve their existing behavior and signatures.

## Finding #602

In @src/gobby/servers/routes/configuration_generation_endpoints.py around lines 43 - 50, The generation endpoint flow persists request.api_key before GenerationConfig validation and probe_responses_endpoint succeed. Move config_store.set_named_secret out of the pre-validation block and execute it only after successful endpoint validation/probing and activation; preserve the existing secret metadata and skip persistence when no API key is provided.

## Finding #603

In @src/gobby/servers/routes/configuration_values.py around lines 76 - 103, The validation flow for POST /values/validate must apply the same responses-endpoint restriction as saving. Invoke_reject_unprobed_responses_endpoint_updates with the submitted updates and prospective DaemonConfig during validation, ensuring responses endpoints are rejected there just as PUT /values rejects them.

## Finding #604

In @src/gobby/servers/routes/providers.py around lines 195 - 202, The endpoint filtering comprehensions in the affected provider route paths should safely handle raw or unvalidated endpoint values. Replace direct endpoint.wire_api access with a guarded attribute lookup using None as the fallback, including the corresponding logic around the other endpoint filters at the referenced locations, while preserving the existing chat-completions matching behavior.

## Finding #605

In @src/gobby/servers/routes/providers.py around lines 304 - 356, Extract the shared services → config → ai → generation → endpoints lookup and dict guard into a helper named _configured_endpoints(server, wire_api). Have it yield or return only endpoints matching the supplied wire_api, then update _local_generation_model_groups, _configured_endpoint_provider_entries, and _responses_endpoint_models to iterate through_configured_endpoints with their respective API values and remove their duplicated lookup and filtering logic.

## Finding #606

In @src/gobby/servers/websocket/chat/runtime_manager.py around lines 72 - 93, Update WebChatRuntimeManager.__init__ in the wire_api == "responses" branch to catch ValueError from codex_endpoint_config_overrides or codex_endpoint_env and continue to the next endpoint, matching the existing local-endpoint handling. Keep valid Responses endpoint client construction unchanged while skipping misconfigured endpoints without aborting manager startup.

## Finding #607

In @src/gobby/storage/communications.py around lines 304 - 315, When the deduplication path in the message persistence method receives inserted=False, log that attachment persistence was skipped, including the message and platform identifiers. Keep returning the existing persisted message and empty saved_attachments unchanged, and do not alter attachment insertion for newly inserted messages.

## Finding #661

In @src/gobby/sessions/processor_transcripts.py at line 330, Gate the parser state snapshot in the incremental batch processing flow to Codex sessions only, initializing parser_state to None for other parsers to avoid deepcopy overhead. In the rollback path, call parser.hydrate_state(parser_state) only when parser_state is not None, while preserving existing Codex failure recovery.

## Finding #676

In @tests/prompts/fixtures/handoff_session_end_golden.md around lines 75 - 76, Insert one blank line immediately after the “## Unresolved Errors” heading in the fixture, before the following explanatory text, to satisfy Markdown heading-spacing linting.

## Finding #677

In @tests/servers/routes/mcp_endpoints/test_execution_offload.py at line 15, Replace the local MAX_ENVELOPE_CHARS definition in the execution-offload tests with the shared constant used by test_stdio_proxy.py and test_gobby_daemon_tools.py, preferably importing the production-defined cap when available. Remove the duplicate literal so all tests stay synchronized automatically.

## Finding #706

In @src/gobby/runner_init/servers.py around lines 165 - 169, Move the vision extractor wiring from the WebSocket-gated block into the unconditional runner.communications_manager block, alongside the other communications setup. Hoist the build_daemon_vision_extract_service import to module scope, while preserving the existing set_vision_extract_service call and runner.config argument.

## Finding #707

In @src/gobby/servers/websocket/chat/backends/codex.py around lines 436 - 462, Update clear_session_context to reattach endpoint-backed sessions using session._model_selector rather than the canonical session._model value, while continuing to use session._model for native Codex sessions. Preserve the existing selector through detach/reattach so _apply_requested_model does not interpret the canonical endpoint model as a native-model switch.

## Finding #708

In @src/gobby/servers/websocket/handlers/session_observe_continue.py at line 409, Update the session_continued payload’s title selection near session_observe continuation handling to use the persisted title from the created session, such as _resolved_session_title for session.db_session_id, instead of relying on manual_source_title for non-resume continuations. Preserve source_title for resume_in_place and ensure digest/provisional registrations emit their assigned session title rather than null.

## Finding #709

In @src/gobby/sessions/processor_transcripts.py around lines 120 - 124, Call ProcessorHost._filter_session_title_messages directly instead of routing it through_run_db, since it only performs an in-memory filter. Update its typing and all usages accordingly, and revise test_process_session_runs_index_append_on_db_executor to remove the expectation that this method is dispatched via the database executor while preserving executor coverage for the remaining DB work.

## Finding #722

In @tests/servers/test_tool_approvals.py around lines 36 - 44, Add the required @pytest.mark.unit decorator to test_is_builtin_auto_exempt_allows_known_gobby_servers so this policy test is selectable under the unit marker taxonomy, preserving its existing assertions.

## Finding #723

In @tests/sessions/test_sessions_processor_unit.py around lines 2121 - 2122, Rename the test class TestExtractNativeTitles to reflect that it covers_filter_session_title_messages, using TestFilterSessionTitleMessages or an equivalent metadata-filter-focused name; update any references consistently.

## Finding #748

In @src/gobby/storage/session_lifecycle.py at line 235, Update the debug log in the session pruning flow to record skipped as structured logging context rather than interpolating it into the message string. Preserve the existing message meaning and use the logger’s supported context-argument convention.

## Finding #781

In @tests/servers/routes/test_tasks_routes.py around lines 444 - 448, Add the appropriate pytest marker, preferably @pytest.mark.integration, to test_create_requires_validation_criteria so it participates in selective test runs consistently with the repository’s test-marker guidelines.

## Finding #782

In @tests/sessions/test_codex_nested_exec_outcomes.py around lines 383 - 393, Update the parametrization for this test to include an expected outcomes value for each tool_input case, then replace the tool_input branch in the test body with a direct assertion against that parameter. Preserve the existing expected outcome for the two recognized exec_command inputs and use the empty-outcomes expectation for other inputs, so every parameter explicitly defines its result.

## Disposition Ledger

| Finding | Disposition | Accounting |
| --- | --- | --- |
| #205 | Carried | added and used the public `TmuxSessionManager.base_args()` accessor. |
| #206 | Carried | roster task payloads are loaded concurrently by distinct task ID and cached. |
| #207 | Carried | attention entry locks are created only for valid entries and removed after the last user or waiter. |
| #209 | Carried | renamed the helper and payload field to describe the terminal parent PID and documented both tmux helpers. |
| #215 | Carried | brief sandbox serialization caps violation counting and marks truncated counts. |
| #216 | Carried | violation logs use replacement decoding and skip malformed records; corrupt UTF-8 is covered. |
| #248 | Carried | purge route tests cover unavailable, protected, and failed outcomes with status and payload assertions. |
| #249 | Carried | project route tests wire the runner through `set_runner_getter`. |
| #250 | Carried | attention response tests narrow optional state values before member access. |
| #251 | Carried | roster concurrency tests assert a deterministic maximum critical-section occupancy. |
| #252 | Carried | roster tests assert the HTTP status before decoding the response. |
| #253 | Carried | roster worker exceptions are captured and asserted absent after joins. |
| #267 | Carried | app context uses the precise type-only `AttentionMetadataStore` annotation. |
| #281 | Carried | ASGI uses a public WebSocket handler entry point and logs handler failures before fallback close. |
| #282 | Carried | ASGI close tolerates a peer disconnect and records the disconnected state. |
| #283 | Carried | the broadcast metadata store import is type-checking-only. |
| #295 | Carried | ASGI endpoint tests cover handler failure, clean return, unavailable server, and disabled authentication. |
| #296 | Carried | ACP deletion now has regression coverage for a false manager result. |
| #344 | Carried | context-window override resolution is shared by lifecycle and observation paths. |
| #345 | Carried | Codex lifecycle dedup applies only to non-empty tool-call IDs. |
| #346 | Carried | observed-session context-window lookup runs through the database executor. |
| #347 | Carried | temporary Codex reconciliation registrations are removed while existing registrations are preserved. |
| #419 | Carried | sidecars persist and verify the indexed prefix digest before append-mode reuse. |
| #458 | Carried | default deployment tokens are memoized while explicit roots remain independently resolved. |
| #481 | Carried | terminal delivery must admit and run the kill callback before release reports success. |
| #482 | Already satisfied | the current lifecycle implementation contains only one reachable return. |
| #483 | Already satisfied | the final lifecycle read already reuses the ambient transaction. |
| #489 | Carried | locally owned telemetry providers and processors are flushed and shut down without touching reused globals. |
| #495 | Carried | resume-blocked UUID literals are centralized as module constants. |
| #501 | Carried | telemetry tests distinguish locally owned providers from reused interpreter globals. |
| #502 | Carried | runner-gate tests assert the explicit gate application name using distinct values. |
| #545 | Carried | the effective-config docstring no longer claims an unenforced transport constraint. |
| #546 | Carried | routing exclusions use the suffix predicate without a redundant exact key. |
| #569 | Carried | spawn-route tests establish the non-null completion-registry precondition. |
| #570 | Carried | all real-database effective-config route tests carry the integration marker. |
| #602 | Carried | endpoint API keys are persisted only after validation and probing succeed. |
| #603 | Carried | config validation applies the same unprobed Responses-endpoint rejection as saving. |
| #604 | Carried | provider endpoint filtering safely handles unvalidated values. |
| #605 | Carried | provider routes share `_configured_endpoints` for guarded endpoint traversal. |
| #606 | Carried | runtime startup skips invalid Responses endpoint configurations without aborting. |
| #607 | Carried | deduplicated attachment persistence emits structured message and platform identifiers. |
| #661 | Carried | parser snapshots and rollback hydration are limited to Codex, including downstream batch failures. |
| #676 | Carried | the unresolved-errors fixture heading now has the required blank line. |
| #677 | Carried | execution-offload tests use the shared envelope-size constant. |
| #706 | Carried | vision extraction wiring is unconditional for the communications manager. |
| #707 | Carried | endpoint-backed Codex context clearing reattaches with the endpoint selector. |
| #708 | Carried | continuation events use the persisted created-session title outside in-place resume. |
| #709 | Carried | title-message filtering is called directly as an in-memory operation. |
| #722 | Carried | the built-in tool approval policy test has the unit marker. |
| #723 | Carried | the title-filter test class now reflects the behavior under test. |
| #748 | Carried | skipped session-prune counts are structured logging context. |
| #781 | Carried | validation-criteria route coverage has the integration marker. |
| #782 | Carried | nested-exec outcome parametrization carries explicit expected outcomes. |

## Reconciliation audit

- All four paths shared with current `0.5.0` work were reviewed, including the three clean
  production auto-merges and the provider-route test conflict.
- Provider catalog coverage uses the source branch's catalog-derived count while retaining
  the current `glm-5.2` regression assertion.
- This packet changes no migration files. Migrations `339`, `342`, `345`, and `346` remain
  byte-for-byte at the packet base, preserving the current migration chain.
