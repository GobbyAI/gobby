Fix the following issues. The issues can be from different files or can overlap on same lines in one file.
Some may have already been fixed in prior commits.

- Verify each finding against the current code and only fix it if needed.

In @.gobby/memories.jsonl at line 787, The memories.jsonl file contains corrupted/truncated memory records (for example the entry with id "6bf7e921-0388-5bb2-82ed-810f65bd54fd") which will degrade retrieval; open .gobby/memories.jsonl, locate and either remove or repair any malformed JSONL lines (ensure each line is a valid JSON object with fields id, content, type, tags, created_at, updated_at, source, source_id) and re-run your import/validation step to confirm all entries parse correctly and timestamps are ISO8601; after cleanup, re-index or re-import the file used by the memory loader to ensure downstream code (memory retrieval / hook_dispatcher ingestion) only sees well-formed memory entries.

- Verify each finding against the current code and only fix it if needed.

In @.gobby/memories.jsonl at line 1077, The entry with id "99baee92-e15d-564e-80f0-3a9b51827226" has the "tags" field as a single comma-delimited string; change that field to a JSON array of strings (e.g., ["daemon","config","restart"]) so it matches the rest of the file's string[] shape and consumers that iterate over tags will not break.

- Verify each finding against the current code and only fix it if needed.

In @.gobby/memories.jsonl around lines 1 - 1109, Malformed lines in .gobby/memories.jsonl mean the import/sync path needs a validation gate; add strict per-line JSON + schema validation in the memory import routine (e.g., the _import_memories_sync / import_sync path in the memory sync/maintenance flow) to reject invalid entries and surface line numbers. Specifically: parse each line with json.loads, verify required keys (id, content, memory_type/type, tags, created_at, updated_at, source, source_id), assert tags is a list (not a string), and add a short-content heuristic (content length < 40 or abrupt/truncated token pattern) that emits clear errors listing offending line numbers; wire this validation into the import path used by MemorySyncManager.import_sync() and the CLI/web import endpoints and fail fast (non-destructively) so pre-commit / sync jobs can block bad files before DB write.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/config/voice.py around lines 13 - 17, The docstring promises automatic normalization but the model_validator is missing and the secret field was renamed from elevenlabs_api_key to ELEVENLABS_API_KEY without backward compatibility; update the model that defines the secret field (the class containing ELEVENLABS_API_KEY) to import and use pydantic's @model_validator (or equivalent root validator for your pydantic version) to detect lowercase keys like "elevenlabs_api_key" in incoming data, map them to "ELEVENLABS_API_KEY", and populate the canonical field, or alternatively restore backward-compatibility by accepting both names (e.g., check for "elevenlabs_api_key" and set ELEVENLABS_API_KEY if present) so old persisted configs keep working.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/mcp_proxy/tools/agent_messaging.py around lines 463 - 464, Add a log message when you sanitize an invalid poll_interval to avoid silently masking caller errors: in the code path that checks "if poll_interval <= 0: poll_interval = 5" emit a debug or warning log that includes the original invalid value and the replacement (e.g. "invalid poll_interval=%s, defaulting to 5"), using the module/class logger used elsewhere in this file (refer to the existing logger instance or add one named logger) so callers can trace why their value was overridden.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/search/embeddings.py around lines 244 - 248, SearchConfig uses an uppercase field EMBEDDING_API_KEY which breaks the class naming convention; rename the field to embedding_api_key in SearchConfig and update all references (including where config.EMBEDDING_API_KEY is used in the call to is_embedding_available inside embeddings.py) to use config.embedding_api_key, or alternatively document that uppercase denotes an env var and provide a clear accessor that maps the env var to a lowercase attribute; ensure consistency with existing fields embedding_model and embedding_api_base and update any imports/tests that reference EMBEDDING_API_KEY.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/search/models.py around lines 71 - 74, SearchConfig defines EMBEDDING_API_KEY in SCREAMING_CASE while other fields use snake_case; rename the field to embedding_api_key (or add a snake_case alias) and update usages accordingly: change EMBEDDING_API_KEY to embedding_api_key on the SearchConfig model and, if you must preserve the environment variable name, add a Pydantic alias (e.g., Field(..., alias="EMBEDDING_API_KEY") or use Config.env_prefix) so the env var still maps to the field; update all references to the field name in code to use embedding_api_key.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/servers/chat_session.py at line 206, The downstream consumers still expect "prompt_text", so restore backward compatibility by including both keys when building the event payload in chat_session.py (the dict creation at data = {"prompt": ...}); set data["prompt_text"] = data["prompt"] so handlers like context_actions.py (handler around line referencing event_data.get("prompt_text")), memory_actions.py (fallbacks at .get("prompt") or .get("prompt_text")), hook_manager.py, mcp_dispatch.py, and broadcaster.py continue to work without changing all consumers; alternatively, if you choose to complete the migration, update those referenced handlers (functions in src/gobby/workflows/context_actions.py, memory_actions.py, hook_manager.py, mcp_dispatch.py, and broadcaster.py) to read "prompt" only and remove any manual mappings or fallback logic consistently across the codebase.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/servers/routes/agent_spawn.py around lines 207 - 208, Replace broad "except Exception" handlers in agent_spawn.py with specific exception types for each operation: where task_manager.get_task(req.task_id) currently uses except (ValueError, Exception) change to except (ValueError, KeyError, AttributeError) as e, log the failure (e.g., logger.warning(f"Failed to get task {req.task_id}: {e}")) and set task = None; similarly update handlers around task_manager.get_comments(req.task_id), dependency/assignee fetches, and other occurrences (lines referenced in the review) to catch only expected exceptions (ValueError, KeyError, AttributeError as appropriate), log with context and the exception variable, and preserve existing fallback behavior instead of swallowing all exceptions.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/servers/routes/agent_spawn.py around lines 101 - 103, Replace the permissive Any types on_build_task_prompt with concrete types: either import the real Task model used elsewhere or declare a small Protocol (e.g., TaskLike with seq_num: int, title: str, description: str) and a CommentLike/DependencyLike protocol that exposes the attributes your code reads (e.g., content: str or seq_num/title if deps are tasks). Update the signature to _build_task_prompt(task: TaskLike, deps: list[TaskLike] | None = None, comments: list[CommentLike] | None = None), add the Protocol definitions (from typing import Protocol) near related types, and adjust imports so mypy recognizes the new types; if a concrete Task class exists, prefer importing it instead of creating a Protocol.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/servers/routes/mcp/endpoints/server.py around lines 274 - 280, The broadcast currently sends only the string "bulk"; update the call to websocket_server.broadcast_mcp_event (accessed as ws / server.services.websocket_server) to include descriptive data from the import result—e.g., pass a list of imported server names or an object with relevant metadata extracted from result (IDs/names/count/timestamp); ensure you build that payload from the variable result used earlier in this function and send it instead of the literal "bulk" (or, if result can be very large, send a summarized payload such as names and count).

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/servers/routes/sessions.py around lines 161 - 168, The _broadcast_session helper currently logs broadcast failures with logger.debug; change this to logger.warning so session broadcast errors are more visible in production. In the async function _broadcast_session (which uses server.services.websocket_server and calls ws.broadcast_session_event), update the exception handler to call logger.warning and include the event, session_id and the caught exception details in the message for better observability.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/servers/routes/skills.py around lines 93 - 100, The helper _broadcast_skill currently swallows all exceptions and logs them at debug level; update the except block to log at warning (e.g., logger.warning) and include the exception details and context (event, skill_id, and exception info) when ws.broadcast_skill_event fails (server.services.websocket_server and ws.broadcast_skill_event are the targets) so failures are visible in production logs.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/servers/routes/voice.py around lines 103 - 140, The tts_config function's signature declares -> dict[str, Any] but returns fastapi.responses.JSONResponse on error paths (in functions named tts_config and where JSONResponse is used), causing type errors; update the return annotation to a union that includes JSONResponse (for example Union[dict[str, Any], JSONResponse]) and add the corresponding import (typing.Union and fastapi.responses.JSONResponse or use fastapi.Response) so the signature matches the actual return types.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/servers/routes/voice.py around lines 131 - 132, The /tts-config endpoint (tts_config function) returns the ElevenLabs api_key in plaintext without enforcing the assumed localhost-only trust boundary; update tts_config to enforce access controls by either rejecting non-local requests (check request.client.host and allow only 127.0.0.1/::1) or require authentication regardless of global optional auth (consult AuthMiddleware behavior and explicitly require auth for this route), and add a clear error response when access is denied; also update any route metadata/comments to reflect the enforced requirement.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/servers/routes/workflows.py around lines 300 - 301, The broadcast call to _broadcast_workflow("workflows_bulk_changed", "bulk") uses a non-descriptive definition_id; update the call site in workflows.py where rows are present to pass a descriptive identifier or payload via kwargs (e.g., include the number of rows and/or template type) — e.g., call_broadcast_workflow("workflows_bulk_changed", definition_id=f"bulk:{len(rows)}", count=len(rows), template_type=template_type) or similar so consumers can trace bulk events; ensure you only change the arguments to _broadcast_workflow and keep the event name "workflows_bulk_changed" intact.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/servers/websocket/server.py at line 97, Replace the weakly-typed Any annotations on the hook integration fields with the concrete types: import WorkflowHookHandler from gobby.workflows.hooks and EventHandlers from gobby.hooks.event_handlers, then change the instance attributes so workflow_handler uses WorkflowHookHandler | None and event_handlers uses EventHandlers | None (e.g., update the declarations for self.workflow_handler and self.event_handlers accordingly) to satisfy mypy and project typing rules.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/servers/websocket/session_control.py at line 485, The log uses an f-string which breaks structured logging consistency; replace the f-string logger.info call with a structured logging call that uses a format string and passes context via the extra parameter (include conversation_id and agent_name, and keep the short conversation id in the message), mirroring how_handle_continue_in_chat uses extra={...}; update the logger.info invocation in session_control.py (the logger.info that currently references conversation_id and agent_name) to supply placeholders and an extra dict so downstream aggregation can consume the fields.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/sessions/token_tracker.py around lines 37 - 38, Replace the vague Any for session_storage with a Protocol that declares the required interface: create a SessionStorageProtocol (or similarly named Protocol) that includes the signature for get_sessions_since(self, since: datetime.datetime) -> Iterable[Session] (use appropriate Session type or Any if needed), import typing.Protocol and datetime, and then annotate the TokenTracker.session_storage field with that Protocol instead of Any so static type checkers can validate calls to get_sessions_since; update any imports and type hints (e.g., from typing import Protocol, Iterable) accordingly and ensure the Protocol is accessible where TokenTracker is defined.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/sessions/token_tracker.py around lines 49 - 50, The call to self.session_storage.get_sessions_since(...) is currently synchronous but performs DB I/O (self.db.fetchall()); change get_sessions_since in src/gobby/storage/sessions.py to be async and await self.db.fetchall(), then change get_usage_summary in src/gobby/sessions/token_tracker.py to be async and await self.session_storage.get_sessions_since(since); update all call sites (including where get_usage_summary is invoked) to await the new async methods and adjust any function signatures to async accordingly to propagate async usage through the call chain.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/skills/search.py around lines 359 - 362, The code repeatedly constructs set(filters.allowed_names) inside_passes_filters for each skill; instead compute the set once and reuse it—either add a cached attribute like allowed_names_set to the SearchFilters object (populate it when filters are created) or compute allowed_set once in search_async and pass it into_passes_filters; then replace set(filters.allowed_names) with membership checks against that prebuilt allowed set (still comparing meta.name) to avoid per-iteration allocations.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/storage/sessions.py around lines 270 - 271, Update the inline comment above the seen: set[str] = set() in the cycle-detection code to correctly describe the performance benefit: replace "Using mutable set for O(1) insertions during cycle detection" with something like "Using a set for O(1) membership checks during cycle detection (to test parent_id in seen)"; reference the seen variable in your edit so reviewers can locate it.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/storage/sessions.py around lines 312 - 313, Add an explicit type annotation for the class constant _VALID_CHAT_MODES (e.g., declare it as a ClassVar[FrozenSet[str]] or ClassVar[Set[str]]), and update imports to include ClassVar and FrozenSet/Set from typing if not already present; keep the value as {"plan", "accept_edits", "normal", "bypass"} but annotate it (reference symbol: _VALID_CHAT_MODES).

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/workflows/agent_resolver.py around lines 118 - 120, Replace the broad except Exception handler around the json.loads()/Pydantic model instantiation with explicit exception handlers: catch json.JSONDecodeError as e for JSON parsing failures and pydantic.ValidationError as e for model validation failures, log each with logger.debug including agent_name and the exception details, and continue to return None; ensure you import JSONDecodeError (from json) and ValidationError (from pydantic) if not already imported.

- Verify each finding against the current code and only fix it if needed.

In @tests/config/test_memory_config.py around lines 25 - 28, Update the test docstring to reference the actual attribute name used in the assertion: change the docstring in test_qdrant_api_key_defaults_to_none() to mention that MemoryConfig.QDRANT_API_KEY should default to None (instead of "qdrant_api_key"), so the description matches the assertion using config.QDRANT_API_KEY.

- Verify each finding against the current code and only fix it if needed.

In @tests/mcp_proxy/tools/test_wait_for_command.py around lines 214 - 239, The test TestWaitForCommandTimeout::test_timeout_returns_none currently hard-codes mock_time.side_effect = [0.0, 0.0, 601.0, 601.0], which breaks if the implementation changes the number of time.monotonic() calls; replace that fixed list with a stateful side-effect function (or iterator) that returns 0.0 for the initial calls and 601.0 afterwards so the patched time.monotonic used in the patch("time.monotonic") block reliably models "before timeout" then "after timeout" regardless of exact call count; keep the rest of the test logic (mock_command_manager.list_commands, messaging_registry.call with wait_for_command) unchanged.

- Verify each finding against the current code and only fix it if needed.

In @tests/sessions/test_token_tracker.py around lines 239 - 261, Add a new unit test mirroring test_can_spawn_agent_unlimited_budget but using a negative daily_budget_usd to ensure negative budgets are treated as unlimited; create a MagicMock session (e.g., id="sess-expensive" with usage_total_cost_usd > 0 and token fields present), set mock_session_storage.get_sessions_since.return_value to that session, instantiate SessionTokenTracker with daily_budget_usd=-1.0 and call can_spawn_agent(), then assert can_spawn is True and reason is None; name the test something like test_can_spawn_agent_negative_budget_is_unlimited to match existing style and use the SessionTokenTracker class and its can_spawn_agent() method.

- Verify each finding against the current code and only fix it if needed.

In @tests/sessions/test_token_tracker.py around lines 28 - 53, The mock sessions in the sessions list use MagicMock without setting usage_cache_creation_tokens and usage_cache_read_tokens, so attribute access yields truthy MagicMock objects and doesn't exercise the "or 0" fallback; update each MagicMock in the sessions list (or add a new dedicated test case) to explicitly set usage_cache_creation_tokens and usage_cache_read_tokens to None (or 0) so the code paths handling None/zero are tested, referencing the existing sessions list and MagicMock instances in test_token_tracker.py to locate where to make the change.

- Verify each finding against the current code and only fix it if needed.

In @web/src/hooks/useCronJobs.ts at line 96, debouncedRefetchRef holds a timeout ID but isn't cleared on unmount; add a cleanup to prevent leaked timers by adding a useEffect with no deps (or extending an existing effect) that on cleanup calls clearTimeout(debouncedRefetchRef.current) and sets debouncedRefetchRef.current = null. Update any places that reset/start the timer (where you call setTimeout and assign to debouncedRefetchRef.current) to coexist with this cleanup so timeouts are reliably cleared when the component unmounts or when starting a new timer.

- Verify each finding against the current code and only fix it if needed.

In @web/src/hooks/useMcp.ts around lines 254 - 260, The WebSocket handler currently calls refreshAll() directly on every 'mcp_event' causing redundant fetches; change the handler to debounce refreshAll by 500ms (matching useCronJobs.ts) and pass the debounced function into useWebSocketEvent instead of the raw callback. Create the debounced callback (e.g., using lodash.debounce or your project's useDebouncedCallback) inside useMcp.ts, reference refreshAll when building the debounced function, memoize it with useCallback/useRef to avoid re-creating the debounced instance, and ensure you cancel/cleanup the debounce on unmount; replace the existing useWebSocketEvent('mcp_event', useCallback(() => { refreshAll() }, [refreshAll])) with the debounced variant.

- Verify each finding against the current code and only fix it if needed.

In @web/src/hooks/useSkills.ts around lines 517 - 527, The WebSocket handler registered via useWebSocketEvent reuses debounceRef (also used by searchSkills) which causes incoming skill_event timeouts to cancel active search debounces; create and use a separate ref (e.g., wsDebounceRef) inside the same hook for the WebSocket refresh logic, replace debounceRef with wsDebounceRef in the useWebSocketEvent callback, and ensure you clear and set wsDebounceRef the same way the existing debounceRef is handled so fetchSkills() and fetchStats() scheduling no longer interferes with searchSkills.

- Verify each finding against the current code and only fix it if needed.

In @web/src/hooks/useVoice.ts around lines 367 - 379, Update the useVoice hook to surface TTS failures via the existing (or a new) voiceError state: when fetching '/api/voice/tts-config' returns !res.ok or the fetch throws, call setVoiceError with a user-friendly message and still allow STT/voice mode to remain enabled; when the fetch succeeds, clear voiceError (setVoiceError(null)), assign ttsConfigRef.current and call connectTTS(config) as before; ensure both the error path and success path reference the same state updater so the UI can read voiceError and show that TTS is unavailable while STT continues to work.

- Verify each finding against the current code and only fix it if needed.

In @web/src/hooks/useWebSocketEvent.ts around lines 56 - 61, The reconnect logic in ws.onclose currently uses a fixed 3s delay; change it to use exponential backoff by tracking a reconnectAttempts counter (reset it on successful connect in connect() or on open) and scheduling reconnects with delay = Math.min(baseDelay * 2 ** reconnectAttempts, maxDelay) before calling connect; use reconnectTimer to setTimeout and increment reconnectAttempts on each scheduled retry, and reset reconnectAttempts to 0 when connection succeeds or when closed is set to true to avoid unbounded growth.

- Verify each finding against the current code and only fix it if needed.

In @web/src/hooks/useWebSocketEvent.ts around lines 124 - 136, The cleanup currently sets closed = true then immediately resets closed = false, creating a race; change the reset so closed remains true until the connection is safely allowed to reconnect—either remove the immediate closed = false and instead reset closed inside ensureConnection(), or defer the reset to the next tick (e.g., setTimeout(() => { closed = false }, 0)) after clearing reconnectTimer and closing ws; ensure ensureConnection() and any reconnect timer callbacks check the closed flag before acting. Update references in useWebSocketEvent (handlers, closed, reconnectTimer, ws, ensureConnection) accordingly.

- Verify each finding against the current code and only fix it if needed.

In @web/src/utils/sentenceBuffer.ts around lines 16 - 24, The loop using re.exec(this.buffer) never advances because re lacks the global flag and the lookbehind used is not universally supported; update the splitting logic in sentence buffering (the regex variable re and the loop that reads this.buffer and pushes into sentences) to avoid lookbehind and infinite loops—either use a safe split approach on sentence boundaries (split this.buffer by a pattern that matches a sentence terminator followed by whitespace) or make the regex global and remove the lookbehind (e.g., match the terminator with /[.!?\n]\s+/g) and iterate with matchAll or by tracking indices, then trim and push each sentence and update this.buffer accordingly. Ensure you update code paths that reference re, the exec loop, and this.buffer so Safari/older engines won’t throw and the loop terminates.

- Verify each finding against the current code and only fix it if needed.

In @web/src/utils/sentenceBuffer.ts around lines 1 - 6, The regex in sentenceBuffer.ts uses a lookbehind (?<=...) which is not supported in older Safari; either document this requirement (note that lookbehind needs Safari 16.4+ in the file header or project docs) OR replace the lookbehind-based split with a compatible approach: use a capture-group-based split (e.g., split on /([.!?\n])\s+/ and reattach the captured delimiter to the preceding chunk) or implement manual scanning in the function that emits sentences so no lookbehind is required; update the function that performs sentence splitting (the regex variable / splitting logic in sentenceBuffer.ts) accordingly and add the compatibility note to comments or README.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/agents/runner.py at line 80, Replace the redundant assignment using "or None": in the Runner (or relevant) class __init__ where you set self._workflow_loader = workflow_loader or None, change it to directly assign self._workflow_loader = workflow_loader (remove the unnecessary "or None") so the attribute reflects the passed-in value or None by default.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/agents/runner.py at line 60, Define a small Protocol for the expected loader interface and use it instead of Any for the workflow_loader parameter: create a WorkflowLoaderProtocol with a load_workflow_sync(self, workflow_name: str, project_path: str | None = None) -> Any | None signature and change the runner function/class constructor parameter type from Any to WorkflowLoaderProtocol so calls to workflow_loader.load_workflow_sync are properly typed; import typing.Protocol where you place the protocol and update the type annotation for workflow_loader in runner.py.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/agents/runner.py around lines 232 - 243, The current inline import of PipelineDefinition inside the if workflow_definition: block is acceptable; no change required—leave the import of PipelineDefinition as-is and keep the logging behavior that uses workflow_definition, effective_workflow, child_session, and self.logger unchanged so the conditional message for PipelineDefinition vs other workflow types remains intact.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/mcp_proxy/tools/spawn_agent/_factory.py around lines 200 - 210, When loading the workflow via WorkflowLoader (wf_loader.load_workflow_sync) returns no wf_def for a non-empty effective_workflow, add a warning so failures aren’t silent: after calling wf_loader.load_workflow_sync(effective_workflow, project_path=project_path) check for falsy wf_def and log a warning that the workflow failed to load (include effective_workflow and project_path); also wrap the load_workflow_sync call in a try/except to log exceptions if it raises, before proceeding to the existing PipelineDefinition check and setting initial_variables["_assigned_pipeline"].

- Verify each finding against the current code and only fix it if needed.

In @tests/workflows/test_hooks.py around lines 796 - 798, Move the local imports of json and WorkflowState into the module-level imports: add "import json" with the other stdlib imports and "from gobby.workflows.definitions import WorkflowState" with the first-party imports, then delete the in-function imports so the test uses the module-level names; if those imports were placed locally to avoid a circular import, preserve the local import and add a short comment explaining why.

Fix the following issues. The issues can be from different files or can overlap on same lines in one file.

- Verify each finding against the current code and only fix it if needed.

In @.gobby/memories.jsonl at line 1, Deduplicate the memories by keeping the record with "source": "import" (id "847a25a6-1910-5750-a993-1192f1a920eb") and remove the other identical entry(ies) that repeat the same content about deprecated init-*rules; ensure only the import-sourced JSON object describing "init-* rules ... deprecated/inactive" remains in .gobby/memories.jsonl and update any index or timestamp metadata as needed.

- Verify each finding against the current code and only fix it if needed.

In @.gobby/memories.jsonl around lines 2 - 5, The .gobby/memories.jsonl contains active bug records that should be tracked as issues; create formal tracker issues for each memory entry (IDs: 2edc772c-5ef8-59f9-ad44-5461c62b87e1, c5441415-4242-532e-a26a-7923bc55720d, f7e20807-20e7-5fc4-b025-7ebcc9eaf528, fd92a266-8036-5da4-ab9f-69bcf110ac22) with clear titles (e.g., "Frontend sends /skill: but backend expects /gobby:"), full repro/impact, and suggested fix, then update the corresponding memory JSON objects to include a new field (e.g., "tracked_issue_url") pointing to the created issue and add an "status" (open/in-progress/closed) so the memory remains a reference while the issue drives remediation; ensure unique symbols referenced in each issue (use file/useSkills.ts, migrations.py BASELINE_VERSION/BASELINE_SCHEMA, _discover_models in litellm, POST /api/agents/definitions and AgentWorkflowsBody) for quick lookup.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/agents/runner.py around lines 452 - 455, Replace the bare except Exception with a more specific handler: either list the actual exceptions raised by the AFTER_TOOL hook evaluation (e.g., the exceptions thrown by evaluate) or at minimum capture the exception as a variable (except Exception as e) and include the exception type/name in the log call; update the logger.debug in the except block (the one using "AFTER_TOOL hook eval failed for %s (fail-open)" and tool_name) to include type(e).__name__ (and keep exc_info=True if you still want the traceback) so the code references the exception object instead of swallowing its type.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/agents/runner.py around lines 93 - 106, Replace the non-specific Any type for the workflow handler with a concrete type or Protocol: define a WorkflowHandler Protocol (or import the actual handler class) describing the methods used by the runner (e.g., hook evaluation method signatures), then update the attribute, property return type and setter parameter from Any to WorkflowHandler (or Optional[WorkflowHandler] if it can be unset); update references to self._workflow_handler and the workflow_handler property to use the new type so mypy can validate usage (symbols to change: the attribute_workflow_handler, the property workflow_handler, and its setter).

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/cli/daemon.py around lines 181 - 189, The change introduces implicit auto-start of Neo4j when the compose file exists; add an explicit opt-out flag (e.g., --no-neo4j) and use it to guard the auto-start logic so installation no longer implies startup. Update the CLI option parsing to declare a no_neo4j_flag (mirror pattern used for --no-ui/--no-watchdog), then modify the block that checks neo4j_flag and compose_file to require not no_neo4j_flag before calling _neo4j_start(gobby_dir); reference neo4j_flag, no_neo4j_flag, compose_file,_neo4j_start and gobby_dir when implementing the change.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/install/shared/agents/pipeline-worker.yaml at line 22, Update the wording in pipeline-worker.yaml to match the actual allowed tools by changing the sentence that currently reads "Do not attempt to use any tools besides MCP discovery and call_tool" to explicitly list all permitted tools; reference the existing rule pipeline-restrict-call-tool and include "MCP discovery, call_tool, run_pipeline, kill_agent, and send_message" so the human-readable policy matches the rule's allowances.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/install/shared/rules/pipeline-enforcement/enforce-pipeline-tools.yaml around lines 12 - 14, Update the reason text in the enforce-pipeline-tools.yaml rule (the reason field currently describing "Pipeline agent: only MCP discovery and pipeline execution tools are allowed") to include the full progressive disclosure step sequence used by other pipeline-enforcement rules: list the steps (e.g., 1) perform discovery with MCP tools only, 2) progressively disclose additional server details as needed, 3) request explicit user confirmation before any action that changes state, and 4) invoke run_pipeline to execute tasks). Edit the reason string to incorporate this step-by-step sequence so it matches the style and level of detail in other progressive-disclosure rules.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/install/shared/rules/pipeline-enforcement/inject-pipeline-instructions.yaml around lines 17 - 20, The server name is inconsistent: inject-pipeline-instructions.yaml references "gobby-pipelines" while restrict-pipeline-call-tool.yaml uses "gobby-workflows"; update inject-pipeline-instructions.yaml to use the canonical server name "gobby-workflows" (change the occurrences of "gobby-pipelines" in the list_tools/get_tool_schema/call_tool steps that reference the "run_pipeline" tool), then run the provided grep check to verify all pipeline-enforcement rules consistently use "gobby-workflows".

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/install/shared/rules/pipeline-enforcement/restrict-pipeline-call-tool.yaml around lines 16 - 18, The reason text in the restrict-pipeline-call-tool.yaml has a truncated sentence ending with "call_tool(server_name='gobby-workflows', run_pipeline." — update the reason field to complete that sentence so it clearly instructs agents to call the run_pipeline operation on the gobby-workflows tool (include the closing parenthesis and any required argument mention), e.g., finish the phrase to describe calling run_pipeline via call_tool with the appropriate parameter format and punctuation so the guidance is no longer cut off or ambiguous.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/install/shared/workflows/command-listener.yaml at line 73, The file ends without a trailing newline (seen at the last line with the _current_iteration assignment), so add a final newline character at the end of src/gobby/install/shared/workflows/command-listener.yaml so the last line "_current_iteration: \"${{ inputs._current_iteration + 1 }}\"" is followed by a newline; no other changes required.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/install/shared/workflows/command-listener.yaml around lines 39 - 47, The handle_timeout step currently only sends a timeout message and lets the pipeline fall through to next_iteration (which expects a command), so change the workflow to re-invoke/loop the listener after a timeout: add a new step (e.g., retry_after_timeout) with condition "${{ steps.wait_command.output.timed_out }}" that uses invoke_pipeline to call command-listener with parent_session_id, wait_timeout, max_iterations and incremented_current_iteration (same argument names used elsewhere), ensuring the pipeline continues waiting rather than terminating; reference the existing steps wait_command, handle_timeout and next_iteration when adding this retry step.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/install/shared/workflows/command-listener.yaml around lines 9 - 13, The workflow currently defaults inputs.parent_session_id to null but later steps (e.g., notify_ready and handle_timeout) send MCP messages to that session; add an initial validation step that checks inputs.parent_session_id is present and non-null at the top of the workflow and fail early if it is not (return an error/exit the workflow before reaching notify_ready or handle_timeout). Locate the inputs block and insert the validation step immediately after it (or before any use of notify_ready/handle_timeout), and ensure the validation produces a clear failure message so downstream MCP calls are never invoked with a null session id.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/install/shared/workflows/command-listener.yaml around lines 55 - 63, The complete_command step currently unconditionally uses steps.process_command.output.response which can be undefined on failure; update the step to check process_command's outcome and supply a safe fallback—e.g., only run or populate result when steps.process_command.outcome/conclusion is "success" and otherwise set result to a descriptive fallback like "ERROR: no response" or the process_command error message; reference the complete_command step, the process_command step and steps.wait_command.output.command/session_id when making this change so the workflow either guards execution with a condition (based on steps.process_command.outcome/conclusion) or uses a conditional expression to default the result value when response is empty.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/install/shared/workflows/command-listener.yaml around lines 65 - 73, The recursive invoke_pipeline block (id: next_iteration, invoke_pipeline: name: command-listener) currently propagates max_iterations from inputs and uses 50 which equals common framework recursion limits; change this so the pipeline cannot reach the framework recursion depth by either lowering the hardcoded default (e.g., reduce max_iterations to a safer value such as 40) or add a cap/validation when passing inputs.max_iterations into the invoke (ensure max_iterations = min(inputs.max_iterations, SAFE_LIMIT) where SAFE_LIMIT is below the framework limit) and add a short comment documenting the chosen SAFE_LIMIT and the framework recursion limit to avoid future regressions.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/llm/claude_streaming.py around lines 206 - 207, Replace the f-string in the logger call so the message is lazily formatted; specifically, in claude_streaming.py where logger.error is called with the exception variable e (the block that currently logs "Failed to stream with MCP tools: {e}"), change it to use percent-style formatting and pass the exception as an argument (e.g., logger.error("Failed to stream with MCP tools: %s", e, exc_info=True)) while leaving the subsequent yield TextChunk(content=f"Generation failed: {_sanitize_error(e)}") unchanged.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/mcp_proxy/tools/agent_messaging.py around lines 471 - 508, Extract the duplicated "command found" block into a helper function (e.g. _build_command_response or_handle_found_command) that accepts the Command instance, resolved_id, auto_activate and start_time and returns the response dict; inside it call_activate_command_impl(cmd, resolved_id) when auto_activate is True and compute "wait_time" with time.monotonic() - start_time and "command" with cmd.to_dict(); then replace both duplicated blocks (the first pending branch and the pending branch inside the polling loop) with a single call to that helper and return its result.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/mcp_proxy/tools/agent_messaging.py around lines 441 - 446, The wait_for_command coroutine currently defaults poll_interval when <= 0 but does not guard timeout, which allows timeout <= 0 to immediately mark timed_out; inside wait_for_command add a guard like "if timeout <= 0: timeout = 600" (or use the original default) or alternatively set "timeout = max(timeout, poll_interval)" so timeout is never non-positive and is at least the poll interval; modify the function parameters handling near the start of wait_for_command to enforce this floor.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/mcp_proxy/tools/skills/__init__.py around lines 248 - 251, The duplicate constant_MAX_SKILL_INDEX is defined inside create_skills_registry() and duplicates _MAX_SKILL_FETCH used in skill_manager.py; extract a single shared constant (e.g., MAX_SKILL_LIMIT) into a new module (suggest gobby.skills.constants), move the value (10_000) to module level there, replace_MAX_SKILL_INDEX in create_skills_registry and _MAX_SKILL_FETCH in skill_manager.py with an import from that constants module, and ensure names are adjusted and imports updated so both create_skills_registry and any code referencing _MAX_SKILL_FETCH use the centralized constant.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/runner.py around lines 583 - 589, The warning logs the raw neo4j_url which may include credentials; redact credentials before logging by parsing neo4j_url (use urllib.parse.urlparse/urlunparse) and reconstructing a sanitized_url that omits username/password (use parsed.hostname and parsed.port for netloc) then call logger.warning with sanitized_url instead of neo4j_url; keep the rest of the logic (is_neo4j_healthy check and clearing self.memory_manager._neo4j_client and _kg_service) unchanged and reference the symbols is_neo4j_healthy, neo4j_url, logger.warning, and self.memory_manager._neo4j_client/_kg_service when making the change.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/servers/chat_session.py around lines 565 - 567, In the except ExceptionGroup as eg block, deduplicate the sanitized error messages before joining them so repeated generic messages from multiple internal errors aren’t shown multiple times; call_sanitize_error on each exception in eg.exceptions, remove duplicates while preserving order (e.g. using an ordered-uniquing approach) to produce a unique errors list, then join that unique list in the TextChunk content (i.e., update the handling around the errors = [_sanitize_error(exc) for exc in eg.exceptions] and the subsequent join).

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/servers/chat_session.py around lines 67 - 73, The_sanitize_error function should log the original exception at debug level before returning the sanitized message so developers can debug without exposing internals; update_sanitize_error to call the module logger (e.g., logging.getLogger(__name__) or an existing logger) and log the exception/stacktrace at debug (include the Exception object or exc_info=True) immediately before returning the generic "An internal error occurred. Please try again." message when matching "litellm", "model isn't mapped", or "custom_llm_provider".

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/servers/chat_session_permissions.py around lines 305 - 306, Replace the ad-hoc formatted warning in the except block with structured logging: call logger.warning with a clear message and pass the context via the extra parameter (e.g., include mode/chat_mode) and include the exception details via exc_info=True (or an 'exception' field in extra) so aggregation tools receive the structured fields; update the except block that currently does logger.warning("Failed to persist chat_mode=%s: %s", mode, e) to use logger.warning(..., extra={...}, exc_info=True) referencing the same logger and the local variable mode.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/servers/routes/agent_spawn.py around lines 207 - 209, The broad except in the agent spawn route swallows all errors when calling get_task; update the handler around the get_task call to catch the specific exceptions get_task can raise (e.g., ValueError, KeyError, or the project-specific TaskNotFoundError) instead of except Exception, and set task = None in those branches; also convert the logger.debug to structured logging with context (include req.task_id and the caught exception) so failure reasons are preserved for debugging while avoiding a bare/broad catch.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/servers/routes/configuration.py around lines 242 - 245, The WebSocket config propagation logic currently present in save_config_values is missing from save_config_template; after save_config_template updates server.services.config, locate the save_config_template function and add the same propagation: retrieve ws_server via getattr(server.services, "websocket_server", None) and if ws_server is not None and hasattr(ws_server, "daemon_config") assign ws_server.daemon_config = server.services.config so the WebSocket server stays synchronized when templates are saved (mirror the logic used in save_config_values).

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/servers/routes/rules.py around lines 94 - 101, The helper _broadcast_rule uses getattr/hasattr to access the websocket server, causing an inconsistent access pattern with _broadcast_workflow; change _broadcast_rule to access server.services.websocket_server directly (like in workflows.py), check it for truthiness before calling broadcast_workflow_event, and keep the existing try/except logger.debug behavior and the same parameters (event, definition_id, **kwargs) to preserve functionality.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/servers/routes/voice.py around lines 118 - 161, The tts_config endpoint currently returns the raw ElevenLabs API key (voice_config.elevenlabs_api_key) to the browser which can be exfiltrated; change this by not exposing the key directly from the tts_config function: either remove the "api_key" field (return null or masked value) and implement a server-side proxy endpoint that performs TTS requests using elevenlabs_api_key on behalf of the client, or add an explicit config flag (e.g., voice.expose_api_key) that must be true to include the key and otherwise omit it; also ensure the route enforces localhost-only access or requires auth before revealing any sensitive config and add a log warning when the key is returned.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/servers/websocket/chat.py around lines 591 - 597, Looping over undelivered messages and calling inter_session_msg_manager.mark_delivered for each causes many DB round-trips; instead collect all msg.id values from undelivered and call a new batch API inter_session_msg_manager.mark_delivered_batch(ids), falling back to the per-message mark_delivered in a try/except if mark_delivered_batch is not implemented or fails. Update call site around the undelivered handling (where lines = ["[Pending inter-session messages]:"], the undelivered loop, and msg.id) to invoke the batch method once, handle/ log exceptions from the batch call, and only then append to lines as before.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/sessions/token_tracker.py around lines 37 - 38, Replace the overly broad session_storage: Any with a typed Protocol that describes the expected interface: create a SessionStorageProtocol (import Protocol from typing) that declares get_sessions_since(self, since: datetime) -> list[Any] (or a more specific session type) and then change the field to session_storage: SessionStorageProtocol; update any type imports (datetime, Any) and adjust LocalSessionManager to implement this Protocol if needed so mypy can validate calls to get_sessions_since in this module.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/sessions/token_tracker.py around lines 104 - 111, The return branch in the token tracker when self.daily_budget_usd <= 0 uses float("inf") for "remaining_usd", which will break JSON serialization; change that value to a JSON-safe sentinel (e.g., None or a large numeric cap) in the same return dict for the method that builds the budget summary (the branch referencing self.daily_budget_usd, used_today and the "remaining_usd" key), and update any related type hints/comments or callers that expect a float to accept None if necessary.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/skills/search.py around lines 359 - 362, The code recreates set(filters.allowed_names) inside the per-skill filter check causing repeated allocations; fix by precomputing the allowed-names set once and using that in the per-skill check — either add an attribute on SearchFilters (e.g., ensure SearchFilters.allowed_names is a set) or compute allowed_set = set(filters.allowed_names) once in the caller (e.g., in search_async) and pass/use allowed_set inside the filter function (the location that performs the meta.name membership check currently shown), then replace set(filters.allowed_names) with a membership test against the precomputed set.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/storage/migrations.py around lines 1340 - 1342, The existence check uses db.fetchone(...) outside the transaction, causing a TOCTOU race; change the SELECT to run on the active transaction connection (conn) so the check and subsequent delete/update use the same transactional connection — e.g. replace db.fetchone("SELECT key FROM config_store WHERE key = ?", (lower_key,)) with a query executed on conn (using conn.execute(...).fetchone() or conn.fetchone if available) so the SELECT, delete, and update occur within the same transaction context for key lower_key in config_store.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/storage/migrations.py around lines 1352 - 1356, The current regex in migrations.py that normalizes secret references (the _re.sub call assigning to value using pattern r'\$secret:([A-Z_]+)') only matches all-uppercase names; update the pattern to match mixed-case and digits (e.g. r'\$secret:([A-Za-z0-9_]+)') or make it case-insensitive (use_re.I) and keep the replacement lambda calling .lower() on m.group(1) so names like $secret:ElevenLabs_Key or $secret:API_KEY_2 are correctly normalized to lowercase; mirror the approach used by _normalize_secret_names_lowercase to ensure consistent behavior.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/storage/workflow_definitions.py around lines 276 - 277, The filter that excludes templates using "source != 'template'" is not NULL-safe and will drop rows where source IS NULL; update each such predicate (the ones near the include_templates handling and the occurrences at the other noted spots) to explicitly allow NULLs—e.g. replace "source != 'template'" with a NULL-safe check like "(source IS NULL OR source != 'template')" or, if using SQLAlchemy, "workflow_definition.c.source.is_(None) | (workflow_definition.c.source != 'template')", applied to the same conditions that reference include_templates so legacy rows with NULL source are preserved.

- Verify each finding against the current code and only fix it if needed.

In @src/gobby/workflows/rule_engine.py around lines 98 - 116, The duplicated AFTER_TOOL cleanup (checking RuleEvent.AFTER_TOOL, computing is_failure from event.metadata.get("is_failure") or event.data.get("is_error"), and clearing variables["tool_block_pending"]) should be extracted into a small helper (e.g., _clear_tool_block_pending or clear_tool_block_pending) and invoked from both places: before the early return that returns HookResponse(decision="allow") and from the pre-evaluation cleanup block; ensure the helper accepts the event and variables (or reads the same context) and performs the same logic so the behavior and comments around symmetry with BEFORE_TOOL remain unchanged.

- Verify each finding against the current code and only fix it if needed.

In @tests/agents/test_runner.py around lines 1137 - 1164, The async test method test_hook_enriches_failed_tool_error (and other async tests in TestAgentRunnerHookIntegration) are missing the @pytest.mark.asyncio decorator; add @pytest.mark.asyncio above each async def (e.g., test_hook_enriches_failed_tool_error) so pytest runs them as asyncio tests, and import pytest at the top of the test file if not already present.

- Verify each finding against the current code and only fix it if needed.

In @tests/e2e/test_daemon_lifecycle.py at line 50, Tests in tests/e2e/test_daemon_lifecycle.py use mixed endpoint paths; change all uses of "/admin/status" to the new "/api/admin/status" so they match the call using daemon_client.get("/api/admin/status"). Update the httpx.get calls that build URLs with http_port and daemon_instance.http_port (the lines calling httpx.get(..."/admin/status"...)) to use "/api/admin/status" instead, ensuring every reference (including the calls at the locations that reference http_port and daemon_instance.http_port) is consistent.

- Verify each finding against the current code and only fix it if needed.

In @tests/e2e/test_mcp_proxy_e2e.py around lines 103 - 106, Update the stale inline comment that mentions the old endpoint path so it matches the actual request being made; change the comment above the POST call that references "/mcp/tools/call" to refer to "/api/mcp/tools/call" (the same endpoint string used in the daemon_client.post call) so the comment and the request are consistent in tests/e2e/test_mcp_proxy_e2e.py around the daemon_client.post invocation.

- Verify each finding against the current code and only fix it if needed.

In @tests/e2e/test_mcp_proxy_e2e.py around lines 313 - 315, The inline comment above the request in tests/e2e/test_mcp_proxy_e2e.py is outdated—update the comment that currently says "Schema endpoint is POST /mcp/tools/schema with JSON body" to reflect the actual endpoint used by the test ("/api/mcp/tools/schema") so the comment matches the call to daemon_client.post("/api/mcp/tools/schema", ...); keep the rest of the comment (that it is a POST with JSON body) intact.

- Verify each finding against the current code and only fix it if needed.

In @tests/mcp_proxy/tools/test_wait_for_command.py around lines 1 - 10, Add a new async test (e.g., class TestWaitForCommandErrors with test_returns_error_on_exception) that forces an exception path by making the session resolver or command listing raise (set mock_session_manager.resolve_session_reference.side_effect = ValueError(...) or make the mock for list_commands raise) and then invoke messaging_registry.call("wait_for_command", {"session_id": "bad-session"}) and assert the result has success == False and contains an "error" key to cover the exception handling code paths in _resolve / list_commands.

- Verify each finding against the current code and only fix it if needed.

In @tests/mcp_proxy/tools/test_wait_for_command.py around lines 29 - 54, Extract the duplicated MockCommand dataclass into a shared test fixture module (e.g., conftest.py) and import it in both test_wait_for_command.py and the other test that currently defines it (test_agent_messaging.py); update references to use the shared MockCommand class and remove the local duplicate from test_wait_for_command.py to avoid redundancy and ease maintenance.

- Verify each finding against the current code and only fix it if needed.

In @tests/mcp_proxy/tools/test_workflow_circumvention.py around lines 36 - 53, Add type hints to the pytest fixture and its helper functions: annotate the fixture function mock_session_var_manager with a return type (e.g., MagicMock or more specific Protocol/Mock type), and add parameter and return type annotations to the internal helpers set_var(session_id, name, value) and get_vars(session_id) (e.g., session_id: str, name: str, value: Any, return types like None and Dict[str, Any]). Ensure the MagicMock assignments (manager.set_variable.side_effect and manager.get_variables.side_effect) remain consistent with the annotated signatures.

- Verify each finding against the current code and only fix it if needed.

In @tests/servers/test_fire_lifecycle_parity.py around lines 375 - 468, Add the pytest integration marker to the test class by decorating TestFireLifecycleFullParity with @pytest.mark.integration (i.e., insert @pytest.mark.integration immediately above the "class TestFireLifecycleFullParity:" line); ensure pytest is imported in the file if not already so the decorator resolves.

- Verify each finding against the current code and only fix it if needed.

In @tests/sessions/test_token_tracker.py around lines 239 - 261, Add a new unit test to cover negative budget handling and include the missing token attributes on the mock: create a test (e.g., test_can_spawn_agent_negative_budget) that constructs a MagicMock session with usage_input_tokens, usage_output_tokens, usage_total_cost_usd, usage_cache_creation_tokens, usage_cache_read_tokens, model and created_at, set mock_session_storage.get_sessions_since to return it, instantiate SessionTokenTracker with daily_budget_usd=-1.0 and call can_spawn_agent(), then assert can_spawn is True and reason is None to verify SessionTokenTracker.can_spawn_agent treats negative budgets as unlimited.

- Verify each finding against the current code and only fix it if needed.

In @tests/sessions/test_token_tracker.py around lines 28 - 53, The mock session objects in tests/sessions/test_token_tracker.py are missing numeric attributes usage_cache_creation_tokens and usage_cache_read_tokens that SessionTokenTracker.get_usage_summary reads; update each MagicMock in the sessions list to include these attributes (e.g., usage_cache_creation_tokens=0 and usage_cache_read_tokens=0) so the aggregations use real numeric values instead of nested MagicMocks and produce correct totals.

- Verify each finding against the current code and only fix it if needed.

In @tests/utils/test_status.py around lines 12 - 13, The async test methods (test_fetch_rich_status_success, test_fetch_rich_status_failure, test_fetch_rich_status_connection_error, test_fetch_rich_status_other_error) need the @pytest.mark.asyncio decorator added above each async def; update each test to include that decorator and ensure pytest is imported at the top of the file if not already present so the marker is available.

- Verify each finding against the current code and only fix it if needed.

In @web/src/App.tsx around lines 686 - 690, The useEffect that wires TTS should not include the ref objects in its dependency array; keep only the stable callback values from the voice object. Update the effect which sets feedTTSTextRef.current = voice.feedTTSText and flushTTSRef.current = voice.flushTTS so its dependency array contains voice.feedTTSText and voice.flushTTS (remove feedTTSTextRef and flushTTSRef), ensuring the effect still updates when the voice functions change.

- Verify each finding against the current code and only fix it if needed.

In @web/src/components/ConfigurationPage.tsx around lines 944 - 946, The restart button's onClick handler performs a fetch without error handling; update the handler in ConfigurationPage.tsx (the inline onClick that calls fetch(.../api/admin/restart) and then setShowRestart(false)) to handle failures by catching errors and responding appropriately—only call setShowRestart(false) on successful response, and in the catch block log the error (e.g., console.error) and surface an error to the user (toast or state) so failures are visible.

- Verify each finding against the current code and only fix it if needed.

In @web/src/components/ConfigurationPage.tsx around lines 312 - 314, The current onClick handler for the "Restart Now" button makes a POST to `${import.meta.env.VITE_API_BASE_URL || ''}/api/admin/restart` and calls setShowRestart(false) unconditionally; change it so errors and non-2xx responses are handled: perform fetch in an async handler (or promise chain), check response.ok and only call setShowRestart(false) on success, and in the catch or non-ok branch set an error state (e.g., setRestartError or reuse existing state) to surface the failure to the user; update the onClick to call this handler instead of the inline fetch so network errors and HTTP errors do not hide the banner.

- Verify each finding against the current code and only fix it if needed.

In @web/src/components/chat/ChatInput.tsx around lines 193 - 204, The if/else inside the Enter key handler is redundant because both branches call handlePaletteSelect(selected); simplify by removing the conditional and directly calling handlePaletteSelect(selected) after computing const selected = paletteItems[selectedIndex] (the Enter/Shift check and early return stay the same); this keeps behavior consistent with the internal logic already implemented in handlePaletteSelect.

- Verify each finding against the current code and only fix it if needed.

In @web/src/components/command-browser/ToolBrowserModal.tsx around lines 91 - 103, The modal currently only calls onSendMessage when res.success is true, so failures are not added to chat; update the execute handler (the async callback that uses callTool and checks res.success) to also invoke onSendMessage for failures and exceptions: when res.success is false send a message like `Failed /${selectedServer}.${selectedTool}` with the stringified res.result or res.error, and in the catch block call onSendMessage with the error string before calling setResult({ success: false, error: String(e) }); ensure these changes are applied alongside existing setResult and setExecuting state updates so chat and UI stay consistent.

- Verify each finding against the current code and only fix it if needed.

In @web/src/components/memory/MemoryPage.css around lines 875 - 994, The .knowledge-graph-*rules duplicate the .memory-graph-* rules (e.g., .knowledge-graph-container, .knowledge-graph-svg, .knowledge-graph-controls, .knowledge-graph-ctrl-btn, .knowledge-graph-ctrl-label, .knowledge-graph-legend, .knowledge-graph-legend-item, .knowledge-graph-legend-dot, .knowledge-graph-empty, .knowledge-graph-info); extract the common declarations into shared selectors (e.g., .graph-container, .graph-svg, .graph-controls, .graph-ctrl-btn, .graph-ctrl-label, .graph-legend, .graph-legend-item, .graph-legend-dot, .graph-empty, .graph-info) or a SASS mixin, remove the duplicated .knowledge-graph-*blocks, and update markup to include both classes where needed (e.g., class="graph-container memory-graph-container" or class="graph-container knowledge-graph-container") so only unique overrides remain in .memory-graph-* and .knowledge-graph-*.

- Verify each finding against the current code and only fix it if needed.

In @web/src/components/terminals/TerminalsPage.css around lines 317 - 336, Add a keyboard-visible focus style for the toolbar buttons to improve accessibility: update the CSS around .terminals-mobile-toolbar button (which currently defines the base and the :active state) by adding a .terminals-mobile-toolbar button:focus-visible rule that provides a clear outline or border-color and ensures sufficient contrast (matching or complementing --accent/--border variables) and does not rely on :active; keep focus-visible and :active styles consistent for hybrid devices and keyboard users.

- Verify each finding against the current code and only fix it if needed.

In @web/src/components/workflows/PipelineEditor.tsx around lines 548 - 553, The input handler unconditionally calls wrapTemplateExpr which double-wraps when the user already typed the template wrapper; update the onChange handler (the arrow handling e in the PipelineEditor component) to first strip any existing wrapper using stripTemplateWrapper on e.target.value (or on the trimmed val) and then call wrapTemplateExpr only on that stripped string (or set condition to undefined if empty), so stored condition will never become `${{ ${{ ... }} }}`; keep the existing value prop usage of stripTemplateWrapper.

- Verify each finding against the current code and only fix it if needed.

In @web/src/hooks/useVoice.ts around lines 159 - 231, The reconnect uses a fixed 1s delay in connectTTS; replace it with an exponential backoff using a retry counter (e.g., ttsReconnectAttemptsRef) so each reconnect waits Math.min(baseMs * 2^attempts, MAX_MS) before retrying, increment the counter each failed attempt, and cap the delay (and attempts) to avoid unbounded growth; reset the attempts counter to 0 on successful ws.onopen and clear it when a connection is intentionally closed, and keep using ttsReconnectRef/voiceModeRef/ttsConfigRef to schedule and manage the reconnect timer.

- Verify each finding against the current code and only fix it if needed.

In @web/src/hooks/useVoice.ts around lines 175 - 188, Update the generation_config.chunk_length_schedule in the ws.onopen BOS payload: replace the current array [50, 120, 160, 250] with the documented example [120, 160, 250, 290], or if you intentionally chose custom values, add a brief inline comment next to generation_config (referencing ws.onopen and generation_config.chunk_length_schedule) explaining why the custom schedule is used and how it impacts latency/quality for streaming TTS.

- Verify each finding against the current code and only fix it if needed.

In @web/src/hooks/useWebSocketEvent.ts around lines 30 - 44, In onMessage, wrap each invocation of handlers (the loop over typeHandlers in useWebSocketEvent.ts) in a try/catch so a thrown exception from one handler doesn't stop subsequent handlers; catch the error and handle it (e.g., call a provided onError/logger or console.error with context including the event type and error) while continuing to the next handler.

- Verify each finding against the current code and only fix it if needed.

In @web/src/hooks/useWebSocketEvent.ts around lines 123 - 136, The transient toggling of the closed flag creates a race: when handlers.size goes to 0 the code sets closed = true then immediately closed = false, which can confuse concurrent mounts; to fix, stop using this transient toggle—either remove the initial closed = true and the immediate reset, or set closed = true only until the WebSocket actually emits its close (use ws.onclose to clear closed) or defer the reset (e.g., setTimeout(() => closed = false, 0)) so a mounting routine sees a consistent state; update the cleanup block that references handlers, closed, reconnectTimer, and ws accordingly to ensure closed reflects the real connection lifecycle rather than a momentary value.

- Verify each finding against the current code and only fix it if needed.

In @web/src/utils/sentenceBuffer.ts around lines 15 - 26, The sentence-splitting regex (const re = /([.!?\n])\s+/g) in the SentenceBuffer logic will wrongly split on abbreviations (e.g., "Dr. Smith", "U.S. Army"); update the codebase by adding a short documentation comment in the SentenceBuffer class/method (where this.buffer, sentences and re are defined) that calls out this known edge case and why it was accepted for TTS streaming, and include a TODO recommending a stricter approach (e.g., using the `sbd` library or a maintained abbreviation list) if higher accuracy is needed in the future.
