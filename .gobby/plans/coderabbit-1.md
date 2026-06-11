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

