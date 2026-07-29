# CodeRabbit Fixes: CLI, Communications, Hooks, and Integrations

CLI/configuration, communications adapters, hooks, and external synchronization fixes.

Unresolved original findings: **68**

Original finding IDs: 176-185, 219, 232, 268-269, 328, 359-360, 381, 425-426, 460-461, 512, 555-556, 596-598, 616-618, 637-653, 665-671, 696-699, 717, 734-736, 750-751, 760, 763, 776

## Finding #176

In @src/gobby/cli/embeddings.py around lines 88 - 93, Update the response handling around the status check and payload parsing to tolerate non-JSON or empty bodies: catch the JSON decoding failure, use response.text as the detail or payload fallback, and preserve the existing Error output with exit code 1 for HTTP failures. Ensure successful non-JSON responses also avoid an uncaught exception.

## Finding #177

In @src/gobby/cli/install_setup.py around lines 365 - 375, Update the SRT installation handling in run_daemon_setup so an SrtRuntimeError does not raise ClickException or abort the remaining setup steps. Emit a warning that includes the explicit agent_sandbox.backend = provider-native fallback hint, then continue with helper-binary, native-binary, tmux clipboard, and IDE integration setup.

## Finding #178

In @src/gobby/cli/install_setup_srt.py around lines 71 - 82, Update _install_srt_runtime to acquire a per-version filesystem lock before staging or promoting the SRT installation, and hold it through target/backup replacement and verification-related cleanup. Ensure concurrent installs and daemon verification serialize on the same lock, while preserving the existing promotion behavior and releasing the lock on all success and failure paths.

## Finding #179

In @src/gobby/cli/install_setup_srt.py around lines 113 - 132, The SRT receipt’s recorded node runtime is not enforced during verification. Update verify_srt_installation and its launch-time validation to resolve the active Node interpreter, compare it with receipt["node"] or enforce the package’s Node >=20.11 engine floor, and fail clearly before launching when incompatible; alternatively remove the node field from receipt.json if runtime validation is not implemented.

## Finding #180

In @src/gobby/cli/installers/embedding.py at line 500, Replace the direct call to the private VectorStore._ensure_initialized() in the installer with a public VectorStore accessor, preferably list_collection_names(), and use that API to obtain the required collection information. Add the public helper to VectorStore if it does not already exist, preserving the existing initialization and failure behavior without exposing private internals.

## Finding #181

In @src/gobby/cli/installers/embedding.py around lines 513 - 519, Update the exception handler around asyncio.run(_inspect()) to emit a structured log containing the inspection failure and relevant operation context before raising EmbeddingConfigMutationBlocked. Preserve the existing fail-closed exception and chaining behavior, while ensuring causes such as connectivity, authentication, and dimension errors are included in the log.

## Finding #182

In @src/gobby/cli/installers/embedding.py at line 483, Replace the Any annotation on config_store in_managed_embedding_collections_exist with ConfigStore, and add the ConfigStore import under the module’s TYPE_CHECKING guard to avoid the import cycle. Ensure load_config(config_store=...) receives the concrete type under strict mypy.

## Finding #183

In @src/gobby/cli/projects.py around lines 185 - 198, The purge_project command currently accepts a bare --yes, unlike the name-confirmation requirement used by projects delete. Replace this confirmation flow with a required project-name confirmation, validate it against the resolved project before performing the irreversible purge, and reject mismatches while preserving the existing project-not-found handling.

## Finding #184

In @src/gobby/cli/projects.py around lines 207 - 211, Update the response handling around the purge command to check response.status_code before calling response.json(). For error responses, use the existing response.text fallback when the body is non-JSON, then emit the “Purge failed” message and exit; only parse JSON for successful responses before passing the payload to json_dumps.

## Finding #185

In @src/gobby/config/app.py around lines 286 - 289, Update the daemon startup flow to preflight the default SRT agent sandbox, warning when its managed runtime or Node.js 20.11+ prerequisite is unavailable instead of allowing spawned agents to fail unexpectedly. Also document the provider-native rollback configuration for users who cannot use SRT, alongside the agent_sandbox default in the configuration documentation.

## Finding #219

In @src/gobby/sync/external_coordinator.py around lines 574 - 583, Update the rate-limit marker list used by the visible error-detection function to recognize hyphenated “rate-limit” text and generic HTTP 429 responses such as “429 Too Many Requests,” matching the behavior of github_issue_sync._is_rate_limit_error. Preserve the existing markers and ensure these cases use the rate-limit retry path with retry_at.

## Finding #232

In @tests/cli/test_lifecycle_daemon_commands.py at line 7, Add a module-level pytest unit marker in tests/cli/test_lifecycle_daemon_commands.py so the entire test module is categorized as unit tests, while preserving the existing pytest import and test behavior.

## Finding #268

In @src/gobby/cli/sessions.py at line 175, Move the _blocked_attention_by_session(manager) call inside the with session_manager_context() as manager block in the relevant session command flow, ensuring manager remains active while its database is accessed. Keep the existing attention_by_session assignment and subsequent behavior unchanged.

## Finding #269

In @src/gobby/cli/sessions.py around lines 67 - 88, Update _blocked_attention_by_session to deduplicate reasons per session before calculating the count, so repeated identical reasons do not inflate the badge count. Preserve all distinct reasons in a deterministic order by joining them for the displayed summary, and update _format_attention only as needed to render the resulting count and combined reason correctly.

## Finding #328

In @src/gobby/cli/daemon.py around lines 543 - 550, Update the startup-summary UI URL logic near ui_resolution so effective dev mode reports the development frontend endpoint on port 60889, while production mode continues using the configured http_port. Adjust the startup-summary test to assert the correct endpoint for each effective UI mode.

## Finding #359

In @src/gobby/sync/memories.py around lines 226 - 240, Update restore_owned and the corresponding restore flow around lines 300-316 to avoid serially awaiting reconciliation for each restored outcome. Batch these per-record operations or run them with bounded concurrency using a semaphore and asyncio.gather, while preserving the existing reconcile_memory_indices and schedule_write_mark_due calls and awaiting all scheduled work before returning the restored count.

## Finding #360

In @src/gobby/sync/memories.py around lines 241 - 251, The cancellation branch around the owned restore task in the memory-restore flow must reliably wait for and consume owned_task even after caller cancellation. Replace the fragile direct second shield await in the restore_owned handling with cancellation-safe completion logic, such as waiting for owned_task via asyncio.wait or suppressing cancellation before a final await, while preserving propagation of the original CancelledError and existing MemoryRestoreError handling.

## Finding #381

In @tests/sync/test_memory_sync.py around lines 18 - 23, Add type annotations to the parameters of the new test functions, including hub_db, tmp_path, and monkeypatch, and annotate the related and should_not_run callback parameters and return types. Apply the same typing consistently to the additional affected test and helper definitions while preserving their existing behavior.

## Finding #425

In @tests/hooks/test_agent_events_coverage.py at line 564, Wrap the long string literal in the assertion near the gobby-skills tool call across adjacent string literals, preserving the exact resulting message while keeping each source line within Ruff’s 100-character limit.

## Finding #426

In @tests/hooks/test_tool_handlers.py at line 594, Wrap all four long assertion string literals in the test cases around the affected assertions, including the build-coordinator and playwright variants, by splitting each literal across two adjacent string segments so Ruff’s 100-character limit is satisfied without changing the asserted text.

## Finding #460

In @src/gobby/hooks/session_coordinator.py around lines 359 - 382, The deferred terminalization flow around submit_terminal_delivery_offload must not skip completion follow-ups when future.result(timeout=5) times out. Ensure _notify_agent_completion() and release_session_worktrees() execute once_terminate_agent_run_inline persistence finishes by moving them into the offloaded operation or attaching a done callback, while preserving single execution and the existing unavailable-executor handling.

## Finding #461

In @src/gobby/hooks/tool_outcomes.py around lines 329 - 344, Update the output-field selection in the outcome parsing flow to skip aliases whose values are None, so a populated later alias such as tool_result or tool_response is selected instead of being masked by tool_output. Preserve the existing _collect_output_signals call and trust handling once a non-null output field is found.

## Finding #512

In @src/gobby/hooks/_normalization_shell.py around lines 143 - 145, Update the normalization flow around the operator newline check and _skip_heredoc_bodies so heredoc bodies are skipped only after the complete logical command terminates. Track pending shell continuations from &&, ||, |, and line continuations across newlines, and do not treat those intermediate newlines as heredoc starts; preserve heredoc skipping at the terminating newline so commands such as redirected printf remain visible to normalization.

## Finding #555

In @tests/cli/test_test_types.py around lines 1 - 13, Add a module-level pytest marker declaration in tests/cli/test_test_types.py for the CLI test category, using the project's established marker symbol and placement alongside the imports. Keep the existing test command import and test behavior unchanged.

## Finding #556

In @tests/config/test_validation_detection.py around lines 68 - 78, Expand test_test_types_ratchet_requires_baseline_and_fail_on_new to classify commands containing only --baseline baseline.json and only --fail-on-new, asserting both return None; retain the existing assertions for both flags and neither flag.

## Finding #596

In @src/gobby/communications/inbound.py around lines 62 - 71, Update the duplicate-message branch in the inbound message handling flow to append the persisted existing record returned by get_message_by_platform_id to handled instead of the raw incoming message. Keep the duplicate detection and continue behavior unchanged, ensuring handled contains the valid database CommsMessage instance for duplicates.

## Finding #597

In @src/gobby/communications/lifecycle.py around lines 134 - 139, Update the update_config callback in the lifecycle adapter setup to prevent stale adapter generations from persisting changes after update_channel() replaces the active adapter. Serialize configuration updates or validate that the callback’s adapter generation is still current before storing; use an atomic config patch/CAS through manager._store.update_channel where available, and reject outdated callbacks without overwriting newer channel.config_json.

## Finding #598

In @src/gobby/hooks/event_handlers/_session_start/handoff.py around lines 8 - 13, Centralize the mandatory section-title list used by both_bound_handoff_summary and allocate_section_budget, and update each function to reference the shared symbol for “next steps” and “current state” instead of maintaining separate lists.

## Finding #616

In @tests/communications/test_attachments.py around lines 221 - 250, Extend test_create_message_with_attachments_links_rows_atomically to force attachment persistence to fail after message creation, then assert the operation raises and that neither the message row nor any attachment rows remain. Preserve the existing successful-linkage assertions in a separate test or successful path, and use the store’s existing attachment-persistence seam rather than adding unrelated setup.

## Finding #617

In @tests/communications/test_manager.py at line 1443, Add the required return type annotation to the async test function test_telegram_inbound_session_reply_resolves_chat_destination, using the appropriate coroutine return type for an async test that does not return a value.

## Finding #618

In @tests/config/test_validation_detection.py around lines 79 - 90, Add the @pytest.mark.unit decorator to test_test_types_ratchet_rejects_wrapped_commands_missing_required_flags so this pure classify_validation_command test is included in unit-test marker selection.

## Finding #637

In @src/gobby/communications/adapters/telegram.py around lines 311 - 316, Bound the growth of _edit_overflow_ids in the outbound send logic surrounding root_message_id, so multi-chunk messages that are never edited cannot accumulate indefinitely. Use an appropriate bounded structure or restrict recording to sends originating from editable streaming turns, while preserving overflow lookup for supported edit_message calls.

## Finding #638

In @src/gobby/communications/adapters/telegram.py around lines 296 - 316, Update the chunk-sending loop in the message-send method to stop processing immediately when any _post_json("sendMessage", payload) response is not ok, returning None (and preserving the existing successful mapping otherwise). Do not continue collecting later chunk IDs after a failed send, so _edit_overflow_ids remains keyed to the first successfully sent chunk.

## Finding #639

In @src/gobby/communications/adapters/telegram.py around lines 325 - 376, The edit_message method should treat Telegram’s “message is not modified” response from editMessageText as a successful no-op. Detect that specific HTTP/API error around the_post_json edit call, suppress only this case, and continue processing remaining chunks while propagating all other errors unchanged.

## Finding #640

In @src/gobby/communications/adapters/telegram_formatting.py around lines 41 - 134, Add a depth parameter and small maximum nesting limit to_parse_inline, incrementing it for recursive emphasis and link-body parsing. When the limit is reached, stop recursing and render the remaining input as plain text so pathological nested formatting cannot raise RecursionError; preserve existing parsing behavior below the limit.

## Finding #641

In @src/gobby/communications/adapters/telegram_formatting.py around lines 198 - 226, Update the chunk finalization flow around finish_chunk() so it does not append or retain a trailing chunk when the current parts contain no visible text, including cases where remainder becomes empty after lstrip(). Preserve chunks with actual text and ensure reopened/closed formatting tags are not emitted as a standalone message.

## Finding #642

In @src/gobby/communications/chat_backend.py around lines 68 - 77, Update the cleanup in stop_turn so cancellation does not interrupt the awaited typing-task shutdown or transport.finalize flush. After absorbing the cancellation, shield these cleanup awaits or clear the current task’s cancellation state, ensuring the final partially streamed text is delivered before removing the active turn.

## Finding #643

In @src/gobby/communications/chat_transport.py around lines 122 - 138, The_finalize method can resend the full text when the initial message was delivered without a platform message ID. Before the fallback _send path, check_last_delivered_text and avoid sending again when text has already been delivered; preserve editing when _platform_message_id is available and update delivery state consistently.

## Finding #644

In @src/gobby/communications/responder.py around lines 62 - 116, Add a bounded pending-turn limit to ConversationTurnQueue so enqueue rejects or answers busy when a conversation already has the maximum queued depth, preventing unbounded task creation. Track per-conversation pending counts alongside _tails, decrement them when tasks finish or are dropped, and preserve serialization for accepted callbacks; update callers to handle the enqueue rejection as the busy response.

## Finding #645

In @src/gobby/communications/voice.py around lines 37 - 66, Update apply_voice_transcription around transcriber.transcribe to enforce the configured transcription_timeout_seconds, and catch transcription failures or timeouts locally. On failure, preserve the message and set voice_transcription_status to a failed/degraded status consistent with existing conventions, rather than propagating the exception; keep unavailable, empty, and completed outcomes unchanged.

## Finding #646

In @src/gobby/hooks/envelope_dedupe.py around lines 168 - 182, Update release_envelope_processing_claim so marker removal cannot race with finalization and delete a terminal record. Use an atomic rename-then-inspect or equivalent compare-and-delete operation around the record read, and only remove the marker when its contents still match the processing claim; otherwise preserve the finalized marker and return False.

## Finding #647

In @src/gobby/hooks/event_handlers/_tool.py around lines 11 - 13, Remove the redundant local SessionVariableManager import from _track_session_edited_file, reusing the existing module-level import while preserving the method’s behavior.

## Finding #648

In @src/gobby/hooks/hook_manager.py around lines 353 - 357, Update HookManager.handle() and handle_async() around ingest_hook_verification_receipt() to catch VerificationReceiptIngestionError for non-HTTP hook paths, preventing receipt-ingestion failures from aborting hook execution; preserve the existing fail-closed behavior used by the HTTP adapter.

## Finding #649

In @src/gobby/hooks/memory_recall_dispatcher.py around lines 70 - 92, The lifecycle logs in the deferred recall scheduling flow should expose session_id and parent_turn_seq as structured fields rather than interpolated message arguments. Update the relevant logger calls around_prune_tasks, the duplicate-task message, and the scheduling-failure handler to use the project’s structured logging API or extra context, preserving the existing messages and including both identifiers where available.

## Finding #650

In @src/gobby/hooks/memory_recall_dispatcher.py around lines 72 - 90, Refactor the task tracking around _prune_tasks and the scheduling flow to retain active futures separately from a bounded or evictable per-session deduplication watermark. Preserve deduplication for repeated session_id/parent_turn_seq submissions while allowing completed futures from inactive or single-turn sessions to be removed, and ensure shutdown iterates only active work; add coverage for many single-turn sessions.

## Finding #651

In @src/gobby/hooks/tool_error_tracker.py around lines 190 - 224, Update _mapping_error_text to accept and propagate a bounded recursion-depth parameter through all recursive calls for error, tool_output, tool_response, tool_result, and structuredContent mappings. Stop descending once the budget is exhausted while preserving the existing text extraction behavior for values at permitted depths.

## Finding #652

In @src/gobby/hooks/tool_error_tracker.py around lines 346 - 370, Update track_proxy_outcome so unknown outcome_class values are logged with the module-level logger and then return without raising, keeping tool dispatch fail-open. Add logger = logging.getLogger(__name__) at module scope and use it for the diagnostic; preserve existing handling for policy_denied, invalid_call, failed_pre_dispatch, and executed outcomes.

## Finding #653

In @src/gobby/hooks/tool_error_tracker.py around lines 97 - 119, Update _normalize_hash_value and its callers, including_canonical_json, to track recursion depth and stop normalizing once a safe maximum is reached, returning a stable type marker at the cap. Ensure deeply nested or self-referential mappings, sequences, and sets cannot raise RecursionError while preserving normal canonical normalization for shallow payloads.

## Finding #665

In @tests/communications/test_manager.py around lines 670 - 725, Mark the async test function test_handle_inbound_transcribes_voice_note_before_event with pytest.mark.asyncio, preserving its existing test body and behavior.

## Finding #666

In @tests/communications/test_responder.py around lines 182 - 195, Add the @pytest.mark.asyncio decorator to the async test function test_access_gate_rejects_sender_outside_allowlist, matching the sibling asynchronous tests so its coroutine body and assertions are executed.

## Finding #667

In @tests/config/test_tool_result_offload_config.py at line 13, Add the appropriate pytest markers to the tests in test_tool_result_offload_defaults_and_app_accessor and the DB-backed migration test, using the established unit marker for config-only coverage and integration for the Postgres-dependent test. Ensure the markers are applied directly to the relevant test functions so they can be selected or excluded.

## Finding #668

In @tests/config/test_tool_result_offload_config.py around lines 60 - 66, Update test_tool_results_migration_is_unique_and_applied to sort migration paths by their parsed numeric version, and remove the assertion requiring migration_paths[-1] to be 340_tool_results.sql. Retain the versions.count(340) == 1 assertion to verify uniqueness.

## Finding #669

In @tests/hooks/test_inbox.py around lines 400 - 407, Extend the tests around test_release_envelope_processing_claim_allows_retry with a negative case for finalized and absent markers. Create a claimed marker, change its status to processed, then assert release_envelope_processing_claim returns False and preserves the marker; also assert releasing an empty envelope ID returns False.

## Finding #670

In @tests/hooks/test_memory_recall_dispatcher.py around lines 88 - 99, Update the_create_session fixture to perform both setup inserts through the repository’s Hub transaction pattern, replacing the %s placeholders with positional $1…$N parameters. Preserve the existing project and session values and conflict behavior while matching the production database access contract.

## Finding #671

In @tests/hooks/test_tool_error_tracker.py around lines 1 - 30, Add the repository-standard pytest marker declaration near the imports in the test module, using pytest.mark.unit so all tests in tests/hooks/test_tool_error_tracker.py are categorized as unit tests.

## Finding #696

In @src/gobby/communications/adapters/telegram.py around lines 464 - 488, The send flow in the chunk loop registers the callback keyboard before any sendMessage call can succeed. Change the `reply_markup`/`_callback_registry.register_keyboard` handling so callback tokens are registered only after all chunks send successfully, or ensure the exception path explicitly removes the registration; preserve attaching the resulting markup to the final chunk.

## Finding #697

In @src/gobby/communications/responder.py at line 15, Update the command handling around _COMMANDS and_run_command so the recognized "start" command has an explicit branch with the intended onboarding or welcome response, rather than falling through to backend.help(context). If no distinct start behavior is intended, remove "start" from_COMMANDS so it is not recognized separately.

## Finding #698

In @src/gobby/communications/sticker_vision.py around lines 51 - 57, Replace the AssertionError-based narrowing after selecting the sticker image in the surrounding generator with normal control-flow narrowing. Assign or validate image.local_path within the loop, or use a helper returning str | None, so image_path is known to be a string without the unreachable raise while preserving the existing unsupported-image fallback.

## Finding #699

In @src/gobby/communications/sticker_vision.py around lines 68 - 84, Bound the await of service.extract in the sticker vision flow with an asyncio timeout, adding the suggested module-level timeout constant and import. Keep the existing exception handler so timeout failures log, mark sticker_vision_status as failed, append the fallback content, and return.

## Finding #717

In @tests/communications/adapters/test_telegram.py around lines 913 - 923, Update the mock_get assertion for the Telegram getUpdates request to parse params["allowed_updates"] with json.loads and compare the result to the expected update-type list, rather than asserting the serialized JSON string and its spacing. Keep the existing offset, timeout, URL, and request timeout checks unchanged.

## Finding #734

In @src/gobby/communications/adapters/telegram.py around lines 456 - 469, The Telegram command synchronization in initialize must not prevent the channel from starting when setMyCommands fails. Update the setMyCommands request and response validation around _raise_for_status_with_redacted_token to catch synchronization errors, log a warning, and continue initialization; preserve successful synchronization logging and keep getMe/connectivity failures fail-fast.

## Finding #735

In @src/gobby/communications/adapters/telegram.py at line 436, Wrap the long assignment in the Telegram adapter by formatting the call to resolve_telegram_proxy_url across multiple lines, preserving the existing arguments and behavior while keeping each line within Ruff’s 100-character limit.

## Finding #736

In @src/gobby/communications/manager.py around lines 342 - 348, Update the access-control flow in the surrounding channel message handler to call evaluate_group_message only after confirming channel.channel_type is "telegram". Preserve the existing authorization rejection and passive_context behavior for Telegram group messages, while allowing non-Telegram messages to skip group evaluation entirely.

## Finding #750

In @tests/communications/test_identities.py around lines 246 - 248, Add @pytest.mark.asyncio and the project’s unit-test category marker directly above test_inbound_access_policy_rejection_logs_at_debug, preserving the existing async test implementation.

## Finding #751

In @tests/communications/test_responder.py around lines 227 - 236, In the responder logging test, bind the expected “Ignoring group message…” text to a local variable before filtering caplog.records, use that variable in the filter predicate, and remove the duplicate records[0].getMessage() assertion while preserving the count and log-level checks.

## Finding #760

In @src/gobby/cli/tasks/crud.py around lines 190 - 199, Update the create_task_impl call and the corresponding implementation call near the additionally affected location to pass all arguments as keyword arguments, especially the same-typed fields such as title, validation_criteria, task_type, and project_ref. Preserve the existing values and ordering semantics while preventing future parameter insertions from causing positional misbinding.

## Finding #763

In @src/gobby/hooks/event_handlers/_tool.py around lines 57 - 62, Update the condition in the event handler around validate_functions_exec_wrapper to use the exported FUNCTIONS_EXEC_NAMES constant instead of the inline {"exec", "functions.exec"} set, preserving the existing Codex source check and blocking behavior.

## Finding #776

In @tests/cli/tasks/test_task_id_resolution.py around lines 299 - 302, Add an explicit assertion in the hash-format close-task test that the #3 input is passed to resolve_task_reference, while retaining the existing contract-failure, exit-code, output, and close_task-not-called assertions. Anchor the new verification to the test’s runner.invoke call and the resolve_task_reference mock.

## Disposition ledger

Reviewed against the packet implementation and focused validation evidence below. No
finding was silently dropped: 64 were resolved, findings #219, #637, and #665 were
already satisfied in the packet baseline, and finding #670 was resolved with its
placeholder recommendation corrected to match the production psycopg transaction
contract.

| Finding | Disposition | Resolution evidence |
| --- | --- | --- |
| #176 | Resolved | CLI embedding responses now fall back to response text for empty or non-JSON bodies on both success and failure paths. |
| #177 | Resolved | Setup warns with the explicit provider-native fallback and continues helper, tmux, and IDE setup after an SRT installation failure. |
| #178 | Resolved | SRT staging, promotion, verification, and cleanup now share a per-version filesystem lock. |
| #179 | Resolved | SRT verification resolves the active Node executable and enforces Node 20.11 or newer before launch. |
| #180 | Resolved | Added and used the public async `VectorStore.list_collection_names()` accessor instead of calling a private initializer. |
| #181 | Resolved | Managed-collection inspection failures now emit structured operation context before the fail-closed exception is raised. |
| #182 | Resolved | `config_store` now uses the concrete `ConfigStore` type under `TYPE_CHECKING`. |
| #183 | Resolved | Project purge now requires `--confirm <project-name>` and refuses a mismatched name before mutation. |
| #184 | Resolved | Project purge checks HTTP status before JSON parsing and uses response text for non-JSON failures. |
| #185 | Resolved | Daemon startup preflights configured SRT, warns without aborting, and the sandbox guide documents Node and provider-native recovery. |
| #219 | Already satisfied | Rate-limit detection already recognized HTTP 429, “too many requests,” and hyphenated `rate-limit` text. |
| #232 | Resolved | Added the module-level unit marker to lifecycle daemon command tests. |
| #268 | Resolved | Blocked-attention aggregation now executes while the session-manager context is active. |
| #269 | Resolved | Attention reasons are deduplicated per session, sorted deterministically, and counted after deduplication. |
| #328 | Resolved | Startup summaries report port 60889 for dev UI mode and the configured HTTP port for production. |
| #359 | Resolved | Restored-memory reconciliation runs through bounded concurrency with a semaphore of eight and awaits all work. |
| #360 | Resolved | Caller cancellation now waits for and consumes the owned reconciliation task before propagating cancellation. |
| #381 | Resolved | Added concrete fixture, callback-parameter, and return annotations to the memory-sync tests. |
| #425 | Resolved | Wrapped the long gobby-skills assertion without changing its value. |
| #426 | Resolved | Wrapped all affected coordinator and Playwright assertion strings within Ruff’s line limit. |
| #460 | Resolved | Terminal persistence now owns agent notification and worktree release so caller timeout cannot skip follow-ups. |
| #461 | Resolved | Outcome parsing skips aliases whose value is `None` and selects a populated later alias. |
| #512 | Resolved | Shell normalization tracks logical continuations and defers heredoc-body skipping until the complete command terminates. |
| #555 | Resolved | Added the module-level CLI marker to test-types command tests. |
| #556 | Resolved | Added baseline-only and fail-on-new-only ratchet classification cases. |
| #596 | Resolved | Duplicate inbound messages append the persisted database record rather than the raw incoming object. |
| #597 | Resolved | Channel updates use per-channel serialization, adapter generations, and current-row patches to reject stale callbacks. |
| #598 | Resolved | Exported one mandatory handoff-title constant and reused it in both handoff summary functions. |
| #616 | Resolved | Added rollback coverage proving message and attachment rows remain atomic when attachment persistence fails. |
| #617 | Resolved | Added the async test’s explicit `None` return annotation. |
| #618 | Resolved | Marked the wrapped-command validation test directly as a unit test. |
| #637 | Already satisfied | Telegram overflow IDs already used a bounded `OrderedDict` capped at 1024 entries. |
| #638 | Resolved | Telegram chunk sending stops and returns `None` immediately after the first failed send. |
| #639 | Resolved | Telegram’s “message is not modified” response is treated as a successful edit no-op only for that error. |
| #640 | Resolved | Inline Telegram formatting has a bounded recursion depth with plain-text fallback at the cap. |
| #641 | Resolved | Chunk finalization suppresses formatting-only trailing chunks while retaining chunks with visible text. |
| #642 | Resolved | Turn cancellation cannot interrupt typing shutdown or the final partial-response transport flush. |
| #643 | Resolved | Finalization does not resend text already delivered without a platform message ID. |
| #644 | Resolved | Conversation queues cap pending turns at eight and return the busy response when the cap is exceeded. |
| #645 | Resolved | Voice transcription enforces the configured timeout and degrades the message locally on timeout or failure. |
| #646 | Resolved | Envelope claim release uses rename-then-inspect and preserves newer finalized markers. |
| #647 | Resolved | Removed the redundant local `SessionVariableManager` import and reused the module import. |
| #648 | Resolved | Local hook receipt ingestion is fail-open by default, while the HTTP adapter explicitly requests strict behavior. |
| #649 | Resolved | Deferred recall lifecycle logs carry session and parent-turn identifiers as structured fields. |
| #650 | Resolved | Active recall futures are separate from a bounded 4096-entry deduplication watermark store. |
| #651 | Resolved | Recursive error-text extraction has a propagated depth budget. |
| #652 | Resolved | Unknown proxy outcome classes are logged and ignored so tool dispatch remains fail-open. |
| #653 | Resolved | Hash normalization bounds depth and cycles with stable type markers for pathological payloads. |
| #665 | Already satisfied | The voice-note inbound test already had the required asyncio marker. |
| #666 | Resolved | Added the asyncio marker to the access-gate async test. |
| #667 | Resolved | Applied direct unit and integration markers to config and Postgres migration coverage. |
| #668 | Resolved | Migration ordering now uses parsed numeric versions and checks version 340 only for uniqueness. |
| #669 | Resolved | Added finalized-marker preservation and empty-envelope release cases. |
| #670 | Resolved with correction | Both inserts now use `HubDatabase.transaction()`; `%s` placeholders were retained because the production psycopg transaction executes SQL directly and does not accept `$1…$N`. |
| #671 | Resolved | Added the module-level unit marker to tool-error tracker tests. |
| #696 | Resolved | Telegram callback tokens are discarded whenever a registered keyboard send fails. |
| #697 | Resolved | Removed unsupported `start` command recognition so `/start` follows the normal backend-turn path. |
| #698 | Resolved | Sticker image selection uses ordinary control-flow narrowing instead of an assertion. |
| #699 | Resolved | Sticker vision extraction is bounded by an async timeout and uses the existing failed fallback. |
| #717 | Resolved | Telegram `allowed_updates` assertions parse JSON before comparing update types. |
| #734 | Resolved | `setMyCommands` failures warn and allow channel initialization to continue; connectivity checks remain fail-fast. |
| #735 | Resolved | Wrapped the Telegram proxy-resolution call within Ruff’s line limit. |
| #736 | Resolved | Group-message evaluation now runs only for Telegram channels. |
| #750 | Resolved | Added direct asyncio and unit markers to the inbound access-policy logging test. |
| #751 | Resolved | Bound the expected ignore message once and reused it for log filtering. |
| #760 | Resolved | Both task-create and task-close implementation calls now pass all arguments by keyword. |
| #763 | Resolved | Tool wrapper validation now uses the exported `FUNCTIONS_EXEC_NAMES` constant. |
| #776 | Resolved | The hash-close test asserts the required project-scoped call `resolve_task_reference("#3", "proj-123")`. |

Focused evidence: 245 CLI tests, 563 hook tests, 214 communications tests, and 75
sync/runtime/vector/config tests passed. Final Ruff, mypy, test-quality, and
test-types gates are recorded by the task’s validation receipts.
