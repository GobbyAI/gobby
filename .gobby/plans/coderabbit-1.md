Fix the following issues. The issues can be from different files or can overlap on same lines in one file.

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @src/gobby/cli/install_setup.py at line 11, Remove the duplicate import by replacing "from shutil import copy2" with usage of the already-imported "shutil" module; update all direct references to copy2 to call shutil.copy2 instead (ensure any helper modules that expect the top-level shutil import still work), i.e., keep "import shutil" and change calls to copy2(...) to shutil.copy2(...).

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @src/gobby/cli/tasks/_utils/claims.py around lines 90 - 92, The except block handling RuntimeError, json.JSONDecodeError, and KeyError in the claimed-owner resolution path (the except in get_claimed_task_owners / claim-owner resolution code) currently logs only f"Failed to get claimed task owners: {e}" which loses traceback context; change the logger.debug call to include exc_info=True (i.e., logger.debug("Failed to get claimed task owners", exc_info=True)) so the exception traceback is recorded while keeping the same return {} behavior.

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @src/gobby/config/feature_base.py around lines 61 - 63, The parsed candidate is returned raw for non-`claude` providers so surrounding whitespace/case can bypass dedupe; instead canonicalize before returning — use the same normalization applied to `model_label` (e.g., model.strip().lower()) and return that normalized value rather than the original `candidate`. Update the branch that checks `if provider != "claude": return candidate` to return the normalized candidate (use `candidate.strip().lower()` or a shared normalize helper), and make the identical change in the other analogous blocks referenced (around the `model_label` logic and the code at the other locations noted: lines 70 and 94-105) so all parsed candidates are normalized consistently before deduplication.

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @src/gobby/install/shared/workflows/rules/skill-discovery/require-rust-skill.yaml at line 24, The endswith pattern change in the rule's condition altered matching semantics: restore the leading slash in the tuple passed to event.data.get('canonical_file_path', '').endswith(...) so it matches files under a .cargo directory only; update the tuple elements back to '/.cargo/config' and '/.cargo/config.toml' in the expression (the call to event.data.get('canonical_file_path', '').endswith(...) is the unique symbol to edit).

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @src/gobby/install/version_pins.py around lines 6 - 11, The pinned package versions for the keys "ghook", "gcode", "gsqz", "gloc", and "gwiki" do not match published GobbyAI/gobby-cli release tags; update these pins by querying the repository's actual release tags (or tag naming convention) and replace the invalid strings ("0.4.6", "1.0.0", "0.1.4", "0.3.0") with the correct release tag names, or if the project uses a different tag format (e.g., vX.Y.Z or commit hashes) normalize to that convention and document the choice in the same module where the pins are defined.

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @src/gobby/mcp_proxy/tools/spawn_agent/_local_endpoint.py around lines 21 - 28, The signature of resolve_spawn_local_endpoint uses overly broad types for daemon_config and registry (daemon_config: Any | None, registry: Any | None); replace these with the correct, specific types from your codebase (for example DaemonConfig | None and Registry | None or the appropriate classes/interfaces used elsewhere) to restore type safety and improve IDE/static checking; update the function signature and any callers/annotations (and import the types) so resolve_spawn_local_endpoint, and related references to daemon_config and registry inside the function, use the concrete types.

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py around lines 169 - 176, When session_var_manager.get_variables(resolved_session_id) fails, the code currently continues with an empty dict which can make target_task_has_edits return false and later allow remove_claimed_task to clobber claimed_tasks/task_edited_files; change this to fail-closed: in the get_variables exception handler for get_variables (called via ctx.session_var_manager.get_variables) do not default to {} — instead surface the error (rethrow or return an error/early-exit from the surrounding lifecycle_close task) so the path that calls target_task_has_edits and later remove_claimed_task is not executed with a stale/empty snapshot; ensure the surrounding caller checks for the failure and aborts the commit/cleanup flow when session_vars could not be retrieved.

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @src/gobby/servers/routes/configuration_import_export.py around lines 372 - 380, Guard the nested access to request.config by first extracting and validating the "databases" value before calling .get("falkordb"); e.g. assign databases = request.config.get("databases") and if databases is None set falkordb_config = {}, if isinstance(databases, dict) set falkordb_config = databases.get("falkordb", {}), otherwise raise a ValueError (so it maps to a 422) — then continue to call validate_falkordb_secret(FALKOR_PASSWORD_KEY, falkordb_password) as before and preserve the later loop over flat.items.

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @src/gobby/servers/websocket/chat/local_openai_warmup.py around lines 221 - 232, The current _match_local_generation_endpoint returns the first endpoint with a positive _candidate_match_score and matching _base_origin, making selection order-dependent; change it to iterate all endpoints and choose the endpoint with the highest positive _candidate_match_score among those whose _base_origin(base_url) equals _base_origin(endpoint.api_base). Implement a best_score/best_endpoint selection pattern (initialize best_score = 0 and best_endpoint = None), call _candidate_match_score(request_model, endpoint.model) for each endpoint, skip if <= 0 or origin mismatches, update best_* when score > best_score, and finally return best_endpoint (type LocalGenerationEndpointConfig | None).

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @src/gobby/sessions/transcripts/typed_json.py around lines 379 - 382, The parser currently assumes elements of "thoughts" and "toolCalls" are dicts and directly accesses fields (see the loop over thoughts calling _extract_thought_parts and the handling of toolCalls); update the parsing to validate each element with isinstance(elem, dict) before casting/field access and skip or safely handle non-dict entries (e.g., None, str) to avoid AttributeError; apply this check inside the loop where "tp" is used and likewise where "toolCalls" elements are iterated, and ensure _extract_thought_parts either accepts only validated dicts or performs the same guard itself.

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @src/gobby/sessions/transcripts/typed_json.py around lines 318 - 327, The loop in parse_session_json (where messages = data.get("messages", [])) assumes messages is a list and each msg is a dict; validate the shape before iterating by checking isinstance(messages, list) and skipping (and optionally logging) if not, and inside the loop guard each msg with isinstance(msg, dict) before calling _parse_session_message(msg, index); only call parsed.extend(result) and index += len(result) when result is a sequence (e.g., list) to avoid exceptions from malformed entries.

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @src/gobby/storage/migrations/279_session_summary_revision_integrity.sql around lines 81 - 87, The ALTER TABLE statement adding constraint sessions_summary_revision_fk is using invalid syntax by appending "(summary_revision_id)" to the ON DELETE clause; update the FOREIGN KEY definition in the migration (the ALTER TABLE sessions ... ADD CONSTRAINT sessions_summary_revision_fk FOREIGN KEY (summary_revision_id, id) REFERENCES session_summary_revisions(id, session_id) ...) to remove "(summary_revision_id)" so the clause becomes just "ON DELETE SET NULL" (keep the DEFERRABLE INITIALLY IMMEDIATE part unchanged).

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @tests/ai/test_text_generation.py around lines 1082 - 1094, In FakeCodexAppServerClient.__init__, the current self.events = events or [...] overwrites an explicitly passed empty list; change the assignment to preserve empty lists by using an explicit None check (e.g., self.events = events if events is not None else [...]) so tests can pass an empty events list and get no default events.

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @tests/mcp_proxy/tools/test_task_sync.py around lines 815 - 870, The test test_task_sync_git_helper_calls_follow_repo_path_resolution uses fragile AST inspection to enforce that _get_task_and_repo_path runs before any Git-invoking helpers; add an explicit docstring comment to that test (inside test_task_sync_git_helper_calls_follow_repo_path_resolution) explaining the intentional fragility and security trade-off so future refactors know the test is meant to fail on structural changes and requires manual review to preserve the invariant.

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @tests/sessions/transcripts/test_gemini_thinking_collapse.py around lines 12 - 14, The test fixture `parser` was narrowed to only return GeminiTranscriptParser, removing coverage for Qwen's thinking-collapse behavior; restore test coverage by either parametrizing the `parser` fixture to yield both GeminiTranscriptParser(session_id="s1") and QwenTranscriptParser(session_id="s1") so the same tests exercise both implementations (they share the collapse logic via TypedJsonTranscriptParser and the "gemini" message type), or add equivalent thinking-collapse tests for Qwen by copying the Gemini test cases into test_qwen_transcript_parser.py and instantiating QwenTranscriptParser there.

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @tests/workflows/test_skill_discovery_rules.py around lines 576 - 578, The suffix checks using event.data.get('canonical_file_path', '') and raw endswith(...) are too permissive; change both occurrences to perform path-segment-aware matching by normalizing the path and inspecting its path parts (e.g., with pathlib.Path or splitting on os.sep) and then compare the last one or two segments (e.g., last segment == 'config' for plain config files, or last two segments == ('.cargo','config') / ('.cargo','config.toml')) instead of using endswith so you only match exact config file paths.

Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @docs/guides/code-index.md at line 149, The docs currently only mention the blast-radius `--depth` flag but the implementation in src/gobby/code_index/gcode_gateway.py passes both ["--depth", str(depth), "--limit", str(limit)]; update docs/guides/code-index.md to document that blast-radius supports both `--depth` and `--limit` (include brief usage examples or parameter descriptions) so the README matches the gcode_gateway.py implementation.

Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @docs/guides/code-index.md at line 149, The docs currently only mention the blast-radius `--depth` flag but the implementation in src/gobby/code_index/gcode_gateway.py passes both ["--depth", str(depth), "--limit", str(limit)]; update docs/guides/code-index.md to document that blast-radius supports both `--depth` and `--limit` (include brief usage examples or parameter descriptions) so the README matches the gcode_gateway.py implementation.

Fix the following issues. The issues can be from different files or can overlap on same lines in one file.

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @.gobby/plans/one-surface-activity-panel-migration-v2.md around lines 976 - 1040, Several manifest entries for web UI tasks (e.g., items with titles "Build the draft-based detail-field family", "Add ProjectSelectField and DateTimeField", "Build the shared QuickMenu primitive and retrofit existing menus", etc., which reference web/src/... validation_criteria files) are incorrectly labeled implementation_domain: backend; change those implementation_domain values to a frontend/web domain (e.g., "frontend" or "web") for every entry that targets web/src/* files so planner/dispatcher routes and validation tooling match the UI deliverables; apply the same change to the other ranges called out (lines around the entries you flagged: 1099-1117, 1141-1159, 1172-1190, 1203-1221, 1250-1280, 1293-1408, 1421-1446) where validation_criteria points to web/src files.

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @docs/architecture/coding-standards.md around lines 595 - 596, Update the guidance to require the @pytest.mark.asyncio decorator for async tests to match repository policy: change the sentence that suggests the marker is optional and explicitly state that async test functions must be annotated with @pytest.mark.asyncio even if asyncio_mode = "auto" is set in pyproject.toml (reference pytest-asyncio and @pytest.mark.asyncio in the docs).

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @docs/architecture/coding-standards.md around lines 33 - 34, Update the documented test command to include the required protected pytest prefix by prepending the environment variable GOBBY_TEST_PROTECT=1 to the existing command shown (the line starting with "uv run pytest --cov=gobby --cov-report=term-missing --cov-fail-under=80"); ensure the docs explicitly show "GOBBY_TEST_PROTECT=1 uv run pytest ..." so readers cannot bypass the repo's test isolation requirement.

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @docs/architecture/source-tree.md at line 34, Replace each Markdown fenced code block that currently uses only three backticks (```) with a language-labeled fence (for example ```text) so markdownlint MD040 is satisfied; locate the naked fence occurrences (the literal "```" blocks) and change them to use a neutral language token like text for each instance referenced in the review.

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @docs/guides/mcp-tools.md around lines 109 - 111, Update the docs table so the `url` field requirement covers SSE as well: change the `url` description from "For http/ws" to "For http/ws/sse" (or similar) so that `name`, `transport`, and `url` consistently reflect that `transport` can be `http`, `stdio`, `websocket`, or `sse` and that SSE entries must include a `url`; adjust only the table cell mentioning `url` to include `sse`.

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @src/gobby/hooks/event_handlers/_misc.py around lines 42 - 48, Replace the bare except in the pause-path around _session_manager.update_session_status with a specific storage-related exception (e.g., SessionStorageError or the concrete exception exported by the session/storage module) instead of Exception; import that exception and catch it (except SessionStorageError as e), and call self.logger.warning with a message that includes the session_id and context (e.g., "Failed to update session status for session_id=%s"), pass session_id as context and set exc_info=True so the traceback is recorded. Ensure the change is applied to the branch guarded by _skip_session_status_update_during_shutdown and reference _session_manager.update_session_status and self.logger.warning in the fix.

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @src/gobby/llm/resolver.py at line 25, The code still references the removed "local" provider even though SUPPORTED_PROVIDERS no longer includes it; update the logic and tests to use a supported provider. In src/gobby/ai/registry.py replace the conditional that sets provider = "local" during embeddings capability binding with either a valid provider from SUPPORTED_PROVIDERS (e.g., "claude" or "codex") or derive it from the existing configuration/default provider logic used elsewhere (ensure the symbol provider in that function matches SUPPORTED_PROVIDERS). In tests/llm/test_context_window.py change the resolve_context_window(..., provider="local") call to use a supported provider (e.g., "claude") or update the test to omit the provider so it uses the canonical default; ensure both changes reference SUPPORTED_PROVIDERS/SUPPORTED_PROVIDERS usage so the code and tests remain consistent.

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @src/gobby/mcp_proxy/tools/sessions/_handoff.py around lines 323 - 344, The current combined conditional conflates a missing child session with a project mismatch; update the logic in the block using _resolve_session_id and session_manager.get so that if child_session is None you immediately return a distinct "not found" response (e.g., success=False, found=False, session_id=parent_session.id, has_context=True, error indicating child session not found, context=context) before computing child_project_id or performing the project equality checks involving child_project_id and parent_project_id; ensure references to _resolve_session_id, session_manager.get, child_project_id, parent_project_id, parent_session.id, and context are used to locate and implement this early-return.

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @src/gobby/mcp_proxy/tools/sessions/_terminal.py around lines 288 - 307, The function _summary_digest_metadata_matches currently only checks that summary_source_context_hash exists but doesn't verify it matches the current source; compute the current source-context hash (using the same helper used elsewhere in the codebase — e.g., the function that produces the value stored in session.summary_source_context_hash or a helper like get_summary_source_context_hash(session)/compute_summary_source_context_hash(session)), compare that computed hash to the stored source_hash (session.summary_source_context_hash), and only return True if they are equal; update imports or call the existing helper and replace the final return so previous_count == current_count && source_hash == current_source_hash.

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py around lines 171 - 178, The session-variable lookup exception is currently ignored which causes target_task_has_edits(session_vars, resolved_id) to run with missing/empty data and falsely allow task closure; change the except branch around session_var_manager.get_variables(resolved_session_id) to mark the load as failed and ensure that when a load failure occurs you treat the task as having edits (i.e., set a flag or arrange session_vars so target_task_has_edits yields True) so the code "fails closed" instead of bypassing commit enforcement; apply the same pattern to the other occurrences that call session_var_manager.get_variables and target_task_has_edits (the blocks around lines referencing session_vars, resolved_session_id, resolved_id).

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py around lines 407 - 414, The code uses an earlier-captured session_vars when calling remove_claimed_task which can overwrite newer claimed_tasks/task_edited_files when merged; before calling remove_claimed_task(session_vars, resolved_id) fetch the latest session variables for resolved_session_id from ctx.session_var_manager (e.g., call the session var getter to obtain a fresh session_vars), pass that fresh session_vars into remove_claimed_task, then call ctx.session_var_manager.merge_variables(resolved_session_id, merge_dict) with the result so you don't base modifications on a stale snapshot; ensure you reference remove_claimed_task, resolved_session_id, resolved_id, session_vars and ctx.session_var_manager in your change.

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @src/gobby/memory/dream/apply.py around lines 173 - 175, The current guard skips "refresh" actions when action.content is falsy, dropping tag-only refreshes; change the conditional so that when action.action == "refresh" you always call _required_memory_id(action) and await _refresh(memory_manager, store, run_id, memory_id, action) regardless of action.content, ensuring tag-only updates are handled; locate the check using action.action and action.content and update it to only test action.action == "refresh" so _refresh continues to run for tag-only mutations.

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @src/gobby/memory/recall.py around lines 166 - 173, The check against PARENT_USER_PROMPT_SOURCES is using event.source (which may be a raw string) instead of the normalized value; replace usages of event.source in the containment check with the normalized source returned by _source_value(event.source) (the local variable source) so comparisons use the same SessionSource enum values—update the line that currently reads `if event.source not in PARENT_USER_PROMPT_SOURCES:` to use `source` and ensure subsequent calls that log or pass the source use the normalized variable as well.

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @src/gobby/servers/routes/configuration_import_export.py around lines 372 - 376, The code assumes request.config["databases"] is a dict and calls .get() directly, causing AttributeError for non-dict values; update the lookup to first retrieve databases = request.config.get("databases") and ensure isinstance(databases, dict) before accessing databases.get("falkordb", {}), then proceed with the existing checks and call to validate_falkordb_secret(FALKOR_PASSWORD_KEY, falkordb_password) only when falkordb_config is a dict and the password is present.

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @src/gobby/servers/websocket/chat/local_openai_warmup.py around lines 221 - 232, The function _match_local_generation_endpoint currently returns the first endpoint with a positive _candidate_match_score and matching _base_origin, which can pick the wrong endpoint by config order; change it to iterate all endpoints, compute _candidate_match_score for each endpoint whose _base_origin(endpoint.api_base) equals _base_origin(base_url), track the highest positive score and corresponding endpoint, and after the loop return that best endpoint, but if two endpoints tie for the highest score reject the match (return None) to avoid ambiguous resolution; keep using the same symbols _match_local_generation_endpoint, _candidate_match_score, _base_origin and the endpoints dict.

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @src/gobby/sessions/summarize.py around lines 433 - 440, The hashing in _source_hash_payload currently uses raw last_turn_markdown and last_assistant_content causing injected context to affect the hash; update _source_hash_payload to sanitize those inputs by passing last_turn_markdown and last_assistant_content through _strip_injected_context_from_value (which handles str/list/dict recursively) before adding them to the payload to be hashed, preserving their types; ensure any place that builds the payload for hashing uses the sanitized values so injected-only changes produce the same hash.

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @src/gobby/sessions/transcripts/typed_json.py around lines 60 - 64, The current use of desc.lstrip("\\n") corrupts text because lstrip("\\n") removes any leading backslashes or 'n' chars; to fix, replace that call with explicit handling: if desc.startswith("\\n"): desc = desc[2:] to strip a literal backslash-n sequence, then remove real newline chars with desc = desc.lstrip("\n").strip(); update the code around the desc variable (the block that sets desc from thought.get("description", "") and the subsequent lstrip/strip calls) to use this explicit sequence-removal instead of lstrip("\\n").

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @src/gobby/sessions/transcripts/typed_json.py around lines 268 - 280, When encountering an inline functionCall in the content branch (the code that sets content_type = "tool_use", and extracts tool_name/tool_input), generate a new tool_use_id (e.g., via your existing ID helper or uuid4), assign it to a local variable tool_use_id and set self._last_tool_use_id = tool_use_id so subsequent tool_result events can correlate; ensure this tool_use_id is included in the resulting encoded message/event payload alongside content_type/tool_name/tool_input.

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @src/gobby/sessions/workspace_context.py at line 45, Replace the generic Any on enrich_git_context's handoff_ctx parameter with a concrete type that exposes the attributes used (git_status and git_commits); either import the existing HandoffContext model if one exists, or define a small typing.Protocol (e.g., class HandoffCtxProtocol(Protocol): git_status: ..., git_commits: ...) and use that as the parameter type for enrich_git_context so the function signature is fully typed and reflects the actual attributes accessed.

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @src/gobby/sessions/workspace_context.py at line 29, Change the loose Any on the resolve_session_workspace signature to a concrete session type or protocol that exposes the terminal_context attribute (e.g., Session or a SessionProtocol with terminal_context: TerminalContext), import or define that type and use it in the function signature instead of Any so static type checkers can verify access to terminal_context; keep transcript_path as str | None. Ensure the chosen type matches the session model used elsewhere in the codebase (or create a small Protocol in the same module if no single class is appropriate) and update the import list accordingly.

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @src/gobby/sessions/workspace_context.py around lines 62 - 83, Replace the bare "except Exception" around the git log subprocess with explicit exception handlers: catch asyncio.TimeoutError for the wait_for timeout, asyncio.SubprocessError (or asyncio.CancelledError if needed) and OSError/FileNotFoundError for process creation failures, and log the caught exception (using logger.debug as before) while leaving other exceptions to propagate; target the try block around asyncio.create_subprocess_exec / asyncio.wait_for / proc.communicate and reference the identifiers create_subprocess_exec, asyncio.wait_for, proc.communicate, logger.debug, and handoff_ctx.git_commits when making the change.

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @src/gobby/sessions/workspace_context.py around lines 47 - 60, Replace the broad "except Exception" in the git status block with explicit exception handlers: catch asyncio.TimeoutError and asyncio.CancelledError for the wait_for call, OSError for subprocess creation failures, and subprocess.SubprocessError for subprocess-related errors (import subprocess if needed); log each specific exception with the same debug message (e.g., logger.debug("Failed to get git status for %s: %s", cwd, e)) and do not swallow other exceptions—let unexpected exceptions propagate. Ensure this change is applied around the asyncio.create_subprocess_exec / asyncio.wait_for usage that sets handoff_ctx.git_status.

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @src/gobby/sessions/workspace_context.py around lines 45 - 46, enrich_git_context currently runs git commands against cwd without validating it; first check that cwd.exists() and cwd.is_dir() and if not, log an error on the context (e.g. handoff_ctx.logger.error or set a git error field) and return early; then verify the directory is a git repo (run git rev-parse --is-inside-work-tree via subprocess.run with cwd=cwd and check returncode or catch CalledProcessError); only if that check succeeds proceed to run git status/commits, otherwise log the validation failure to avoid silently swallowing errors in enrich_git_context.

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @src/gobby/storage/migrations/279_session_summary_revision_integrity.sql around lines 81 - 87, The FOREIGN KEY clause for constraint sessions_summary_revision_fk uses invalid PostgreSQL syntax `ON DELETE SET NULL (summary_revision_id)`; update the constraint on table sessions (constraint name sessions_summary_revision_fk, columns (summary_revision_id, id)) to use the standard `ON DELETE SET NULL` clause (no column list), ensuring the REFERENCES clause still targets session_summary_revisions(id, session_id) and that the referenced column order matches the FK column order.

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @src/gobby/storage/migrations/279_session_summary_revision_integrity.sql around lines 52 - 58, The ALTER TABLE statement adding constraint session_summary_revisions_previous_same_session_fk uses invalid syntax `ON DELETE SET NULL (previous_revision_id)`; locate the ALTER TABLE block that references session_summary_revisions and change the foreign key clause to use the standard PostgreSQL form `ON DELETE SET NULL` (i.e., remove the parenthesized column list) so the DB will correctly nullify the FK columns on delete.

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @src/gobby/storage/session_models.py around lines 107 - 110, Session.from_row currently forces is_local to False when the "is_local" key is absent or null, which overrides the legacy inference from the model name; change the logic so you only override is_local when "is_local" exists and is not None (i.e., keep the prior inferred value otherwise). In practice, inside Session.from_row leave is_local alone unless row.keys() contains "is_local" and row["is_local"] is not None (then set is_local = bool(row["is_local"])); otherwise preserve the previously computed value (e.g., from model_name or the existing is_local_model helper) instead of setting it to False.

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @src/gobby/storage/sessions/_registration_cache.py around lines 240 - 249, The current ambiguity guard uses len({candidate.project_id for candidate in candidates}) > 1 which treats multiple None project_ids as a single value; change the check in the block that logs "Ambiguous cross-project session recovery" so that when project_id is None you also consider multiple candidates with None as ambiguous — e.g., compute the set of non-None project_ids and separately detect if any candidate has project_id is None and there is more than one candidate, then trigger the warning and return None; update the condition that references project_id and candidates (and keep the existing log using external_id, source, machine_id, [candidate.id for candidate in candidates]) so behavior is correct for None project_id cases.

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @src/gobby/utils/injected_context.py around lines 39 - 69, The function _strip_sentinel_blocks currently clears all accumulated parts when it sees an unmatched end sentinel (end before begin); change that defensive behavior to surface the error instead: in the branch where end_index != -1 and (begin_index == -1 or end_index < begin_index) replace parts.clear() with raising a ValueError (or logging and raising) that includes the offending sentinel (INJECTED_CONTEXT_END) and a short slice of the surrounding text to aid debugging; ensure the exception path short-circuits (return/raise) rather than silently dropping content, update any callers/tests to expect the error, and add a unit test for malformed input to cover the unmatched-end case.

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @src/gobby/workflows/condition_helpers.py around lines 185 - 196, The current scan inspects nested bodies before their taint updates run, causing false positives; to fix, change the logic in _statements_use_direct_project_path_cwd to apply taint updates before scanning for uses: for each statement call _update_project_path_taint(statement, local_tainted) (and ensure _nested_statements_use_direct_project_path_cwd updates taint for any inner blocks first) and only then call _statement_uses_tainted_cwd; additionally modify _statement_uses_tainted_cwd so it does not ast.walk into nested bodies (or restrict its walk to the statement's top-level nodes) to avoid inspecting inner blocks that haven't been tainted-resolved yet.

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @src/gobby/workflows/state_manager.py around lines 351 - 356, The new transaction path in record_edited_file() uses json.loads(...) directly on session_variables, which can be a native dict on some hubs; update this to reuse the same normalization logic as get_variables() (or call get_variables(session_id)) so the column is treated as either a native dict or a JSON string before decoding; specifically, inside the with self.db.transaction_immediate(SessionVariableMutation(session_id=session_id)) block replace the raw json.loads(row["variables"]) usage with the shared decoder logic used by get_variables() (or invoke get_variables) to produce current_vars and avoid TypeError.

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @tests/config/test_app_config.py around lines 802 - 862, The test's preservation assertion is vacuous because TaskExpansionConfig.profile defaults to FeatureProfile.HIGH; change the in-memory default in the test to a different value so we actually verify the DB row is applied: update values["gobby_tasks.expansion.profile"] to a different profile (e.g., FeatureProfile.LOW or its string/JSON representation), keep the rows entry that contains the persisted "feature_high" value, and then assert config.gobby_tasks.expansion.profile == FeatureProfile.HIGH after calling load_config (using DummyConfigStore and DummyDB as in the diff) so the test proves the DB value overrides the default.

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @tests/fixtures/transcripts/qwen/session.json at line 13, The fixture has a mismatched message type: the JSON object with "sessionId": "qwen-session" currently has "type": "gemini" but should be "qwen"; update the "type" field value from "gemini" to "qwen" so the message type matches the Qwen transcript fixture (look for the JSON object containing the "sessionId": "qwen-session" and change its "type" property).

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @tests/mcp_proxy/tools/test_handoff_coverage.py around lines 105 - 143, The two tests in TestGetHandoffContextProjectScope should seed a caller session context so the fallback path actually reads caller-session project info: wrap the registry.get_tool("get_handoff_context")() invocation in session_context_for_test(...) supplying a caller session (e.g., parent with project_id "proj-1") and then assert the lookup path uses the caller-derived project (mock_session_manager.find_parent was called with project_id "proj-1") and that mock_session_manager.list remains unused; do this for both test_fallback_uses_caller_project_context and test_fallback_without_project_context_fails_closed to ensure caller-session resolution is exercised.

- Verify each finding against current code. Fix only still-valid issues, skip the rest with a brief reason, keep changes minimal, and validate.

In @tests/sessions/transcripts/test_gemini_thinking_collapse.py around lines 12 - 14, The test fixture named parser currently returns only GeminiTranscriptParser, removing coverage for QwenTranscriptParser; either restore parameterization of the parser fixture to yield both GeminiTranscriptParser and QwenTranscriptParser (so tests run for both implementations) or add a new test file (e.g., test_qwen_thinking_collapse.py) that defines a parser fixture returning QwenTranscriptParser and duplicates the thinking-collapse tests; update references to the parser fixture accordingly so the thinking-collapse behavior is exercised for QwenTranscriptParser as well.