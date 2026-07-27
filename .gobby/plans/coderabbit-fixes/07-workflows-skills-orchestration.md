# CodeRabbit Fixes: Workflows, Skills, and Orchestration

Workflow state, skill hubs, orchestration wiring, and workflow contract tests.

Unresolved original findings: **55**

Original finding IDs: 201-204, 210-214, 245-246, 254-255, 287, 297, 364-365, 367-371, 384-385, 429-434, 488, 496-497, 510-511, 571-576, 585, 622, 663-664, 679-680, 711, 749, 752-755, 785-786

## Finding #201

In @src/gobby/runner_init/servers.py around lines 62 - 63, Update the `attention_manager` argument in `init_orchestration` to access `runner.attention_manager` directly, matching the existing `runner.detection_registry` access. Remove the `getattr` fallback so initialization-order regressions are surfaced instead of silently passing `None`.

## Finding #202

In @src/gobby/runner_init/storage.py around lines 243 - 253, Update the hub authentication setup before constructing HubManager to use resolve_hub_api_keys(skills_config.hubs, runner.secret_store) instead of manually reading os.environ and populating api_keys. Pass the resolved keys to HubManager while preserving the existing skills_config.hubs configuration.

## Finding #203

In @src/gobby/runner_lifecycle_subsystems.py around lines 410 - 423, Route the register_wiki_prune_cron call through await_run_db(runner, ...) so its synchronous database operations execute off the event loop. Preserve the existing cron_storage, executor, gateway, and project_id arguments, and follow the surrounding storage-access pattern in the lifecycle function.

## Finding #204

In @src/gobby/runner_maintenance.py around lines 208 - 221, Update rebuild_vector_store so the fallback path does not invoke the synchronous memory_dicts supplier on the event loop; execute the callable supplier via the project’s async thread/offloading mechanism, await its result, and then pass the resolved memories to vector_store.rebuild. Preserve direct use of list inputs and the existing rebuild_from_supplier path.

## Finding #210

In @src/gobby/skills/hubs/github_topic.py around lines 234 - 247, Update the GitHub provider request flow around_get to reuse a single httpx.AsyncClient for the provider lifetime or each discover() call instead of creating one per request. Pass that client through _search_page, _probe_repo, and _download_archive, and ensure it is properly closed after the owning lifecycle completes.

## Finding #211

In @src/gobby/skills/hubs/github_topic.py around lines 345 - 373, Update the discovery flow around discover() by adding an asyncio.Lock-backed single-flight refresh and moving the existing crawl/cache-update body into a private_refresh() method. Have discover() acquire the lock, re-check the cache after waiting, and only invoke _refresh() when discovery is still needed, preserving the existing rate-limit fallback and cache behavior.

## Finding #212

In @src/gobby/skills/hubs/github_topic.py around lines 277 - 300, Update_probe_repo to validate each response.json() result as a dict before using .get(), removing the unsafe cast assumptions for both commit_payload and tree_payload. Ensure JSONDecodeError/ValueError and invalid JSON shapes are converted into the existing per-repository skip path by expanding the local exception handling, so malformed repository responses cannot escape the crawl TaskGroup.

## Finding #213

In @src/gobby/skills/hubs/github_topic.py around lines 450 - 469, Update the httpx.AsyncClient construction in_download_archive to enable follow_redirects=True, while preserving the existing streaming, status handling, and archive-size validation behavior.

## Finding #214

In @src/gobby/skills/hubs/manager.py around lines 40 - 49, The type-specific auth secret selection is duplicated and omitted from auth-reporting paths. Add an `auth_secret_name` property to `HubConfig`, returning `auth_token_env` for `github-topic` and otherwise `auth_key_name`, then replace the conditional or direct `auth_key_name` reads in the manager’s secret loading, `_create_provider`, `auth_status`, `warn_missing_auth`, and `runner_init/storage.py` with this accessor.

## Finding #245

In @tests/runner_init/test_detection_registry_composition.py around lines 42 - 49, The test around create_agents_registry should explicitly verify that the spawn registrar was invoked before indexing captured_contexts. Add a readable assertion that captured_contexts is non-empty, then retain the existing context.detection_registry wiring assertion.

## Finding #246

In @tests/runner_init/test_detection_registry_composition.py around lines 1 - 16, Mark the test module as a unit test by adding pytestmark = pytest.mark.unit near the existing pytest import in test_detection_registry_composition.py. Use the imported pytest symbol and leave the rest of the test setup unchanged.

## Finding #254

In @tests/skills/hubs/test_github_topic.py around lines 137 - 138, Apply a category marker to the async tests test_sha_pinned_identity and the additional tests at the referenced locations, using the appropriate unit or integration marker consistent with their scope. Preserve the existing pytest.mark.asyncio decorators.

## Finding #255

In @tests/skills/hubs/test_github_topic.py around lines 272 - 296, The existing test only covers missing and traversal failures; add an end-to-end case for the install_skill provenance validation path. Stub download_skill to return a DownloadResult whose provenance has a mismatched repository or SHA relative to the requested item, then assert install_skill returns success=False with an item_unavailable error and does not persist the skill.

## Finding #287

In @src/gobby/storage/pipeline_history.py around lines 110 - 114, Update the transaction block in the pipeline-history deletion method to inspect the result of the project-row lock query and assert that the project exists before executing_history_query. If no row is returned, stop the delete flow using the method’s established missing-project behavior rather than proceeding without serialization.

## Finding #297

In @tests/skills/test_wiki_research_skill.py around lines 10 - 68, Add pytest marker support to the three contract tests in test_backlog_entry_contract_is_detailed_and_idempotent, test_investigation_tasks_require_opt_in_and_link_to_backlog, and test_run_report_records_triaged_away_items. Import pytest and apply the unit marker at class or individual-test scope so marker-based selection identifies them as unit tests.

## Finding #364

In @src/gobby/workflows/hooks.py around lines 575 - 587, The BEFORE_TOOL/AFTER_TOOL persistence path must retain projection deduplication from persist_verification_receipt instead of appending only the latest evidence item. Update the persistence block around append_to_bounded_list_variable to replace or merge the complete VERIFICATION_EVIDENCE_VARIABLE list, preserving removals and stale-entry cleanup while keeping bounded-list behavior where applicable.

## Finding #365

In @src/gobby/workflows/hooks.py around lines 407 - 417, Update the snapshot identity propagation after _match_tool_context in the hook flow to preserve any native verification_execution_id already present on event.data. Only copy snapshot["verification_execution_id"] when the event lacks its own non-empty identifier; continue propagating verification_source_event_id as currently handled and retain the existing no-snapshot path.

## Finding #367

In @src/gobby/workflows/observer_verification.py at line 61, Update the receipt construction around the "timestamp" field to use event.timestamp instead of datetime.now(UTC). Preserve the existing ISO-format serialization so receipt timestamps match the corresponding event and maintain consistent ordering.

## Finding #368

In @src/gobby/workflows/verification_evidence.py at line 43, Update the summary construction in the verification evidence workflow to format counts explicitly as stable key=value pairs joined by commas, instead of interpolating the Python dict directly. Preserve the existing count contents and summary wording while avoiding Python-specific quoting in the LLM-facing text.

## Finding #369

In @src/gobby/workflows/verification_evidence.py around lines 92 - 94, Update the outcome_counts field declaration to enforce strict validation, matching receipt_count and latest_receipt_id, so mapping values such as stringified integers are rejected rather than coerced.

## Finding #370

In @src/gobby/workflows/verification_receipt_ingestion.py around lines 9 - 22, Rename the underscore-prefixed symbols _SHELL_TOOLS, _extract_shell_command,_extract_shell_output_text, and_shell_tool_outcome to public names in their defining modules, then update all imports and references across both packages, including verification_receipt_ingestion, to use the renamed symbols consistently.

## Finding #371

In @src/gobby/workflows/verification_receipt_ingestion.py around lines 160 - 168, Replace the list_for_task call in the task_id branch with a lightweight verification-outcome projection query that selects only normalized_outcome, id, and required timestamps, returning grouped counts plus the latest id/timestamp needed by project_verification_outcomes. Preserve the existing merge_receipt_projection_evidence and projection.ready flow while avoiding loading full receipt rows or output excerpts.

## Finding #384

In @tests/workflows/test_verification_receipt_ingestion.py around lines 55 - 64, Add the repository’s appropriate pytest marker to the new database-backed ingestion tests, including both parametrized test cases near the SessionSource list and the additional tests around the referenced second block. Use the existing integration marker convention and preserve the current parametrization and test behavior.

## Finding #385

In @tests/workflows/test_verification_receipt_ingestion.py around lines 18 - 24, Add complete type annotations in_session and the related fixture functions at the referenced locations: annotate temp_db, session_manager, and sample_project parameters, and add_session’s return type. Use the concrete existing fixture/session/project types already used in the test module so mypy strict can validate all functions.

## Finding #429

In @tests/workflows/test_active_progressive_discovery_guidance.py around lines 27 - 42, Update MANDATORY_ORDERED_CHAIN to recognize imperative inventory-first wording such as “First, call list_mcp_servers, then …” without requiring “discovery” or “chain” before the ordered calls. Preserve detection of existing progressive-discovery and mandatory-chain phrasing, and keep the ordered list_mcp_servers, list_tools, get_tool_schema, and call_tool sequence requirements intact.

## Finding #430

In @tests/workflows/test_agent_definitions.py around lines 52 - 76, Add explicit -> None return annotations to the new test functions test_close_task_success_handlers_ignore_preview_calls and test_agent_success_handlers_do_not_fabricate_verification_evidence, preserving their existing test logic.

## Finding #431

In @tests/workflows/test_context_handoff_rules.py around lines 157 - 171, Add type annotations to the newly added test functions, including fixture parameters such as db and manager and an explicit -> None return type. Apply the same typing consistently to the additional test function referenced by the comment, preserving their existing behavior.

## Finding #432

In @tests/workflows/test_progressive_discovery_rules.py around lines 722 - 752, Mark the async test_context_loss_clears_only_schema_leases with pytest.mark.asyncio in addition to its existing pytest.mark.parametrize decorator, ensuring all parametrized cases execute under the required async pytest configuration.

## Finding #433

In @tests/workflows/test_progressive_discovery_rules.py around lines 85 - 108, Add type hints to all newly introduced test functions, including fixture parameters and explicit None return annotations. Update test_sync_retires_legacy_gates_and_enables_renamed_gate and the other affected test definitions in this file, preserving their existing behavior.

## Finding #434

In @tests/workflows/test_review_workflow.py around lines 55 - 68, Update the test function declaration for test_spawn_step_passes_only_valid_spawn_agent_parameters to include the required -> None return annotation, preserving its existing behavior and body.

## Finding #488

In @src/gobby/storage/pipeline_subscribers.py around lines 44 - 56, Add the existing `# nosec B608` suppression annotation to the `self.db.execute` query in the subscriber insertion flow, mirroring the sibling query’s annotation while leaving the generated placeholders and bound parameters unchanged.

## Finding #496

In @tests/skills/test_plan_skill_delegated_mode.py around lines 63 - 80, Add the appropriate project test marker to test_spawned_run_waiting_policy_is_shared_and_wake_driven, using the existing unit/slow/integration/e2e marker convention so the test supports reliable targeted execution.

## Finding #497

In @tests/skills/test_removed_wait_tool_guidance.py around lines 12 - 17, Expand WAKE_DRIVEN_GUIDANCE with pytest parameters for the merge-expert and review skill files, plus the epic-reviewer.yaml, merge-orchestrator.yaml, and review.yaml workflow artifacts. Use the existing SKILLS_DIR and WORKFLOWS_DIR path conventions and descriptive IDs so all changed wake-driven guidance is covered.

## Finding #510

In @src/gobby/dispatch/prompts.py around lines 190 - 207, Update the prompt construction around the plan-review snapshot to prevent snapshot_text from breaking the evidence framing. Derive nonce-suffixed opening and closing delimiters from plan_hash, use them consistently in the generated prompt, and ensure the snapshot content cannot reproduce those delimiters; preserve the existing metadata and structured-verdict guidance.

## Finding #511

In @src/gobby/dispatch/spawn.py around lines 293 - 300, Wrap the synchronous _prepare_plan_adversary_evidence call in spawn_agent with asyncio.to_thread, preserving its existing arguments and tuple assignment. Ensure the blocking prepare_plan_review_round and snapshot_bytes operations execute off the event loop while the returned prompt, evidence_service, and evidence_id values remain unchanged.

## Finding #571

In @tests/skills/test_development_discipline_skill.py around lines 36 - 37, Update the assertion in the required_contract loop to include the current phrase in its failure message, so pytest identifies which contract phrase is missing while preserving the existing containment check.

## Finding #572

In @tests/skills/test_development_discipline_skill.py around lines 1 - 10, Add an appropriate pytest marker to test_validation_lesson_contract in the development-discipline contract test module, using the project’s established marker conventions so marker-based selection includes this test.

## Finding #573

In @tests/skills/test_epic_review_skill.py around lines 94 - 133, The test locally filters incomplete entries before calling service.record, so it does not exercise production completeness gating. Remove the duplicated required-field condition from the loop and assert the incomplete-entry behavior through the actual recorder/validator enforcement; alternatively remove the incomplete fixture and document that only the existing doc-phrase assertion covers this rule.

## Finding #574

In @tests/skills/test_review_learning_skill.py around lines 385 - 634, Split test_interactive_approval_sequence into independent tests for contract assertions, happy-path apply/idempotency, post-apply drift and finalization, pre-apply drift rejection, pending-drift revocation, and crash-restart recovery. Add fixtures for the shared _review_setup and _approval context, and use scoped monkeypatch fixtures/context handling for atomic_write_bytes in the crash scenarios so setup, cleanup, and failures remain isolated.

## Finding #575

In @tests/skills/test_review_learning_skill.py around lines 65 - 89, Replace the hand-built YAML serialization in_manifest_yaml with yaml.safe_dump applied directly to _manifest_entries(stem), preserving all covers and labels entries and correctly quoting scalar values. Return the dumped YAML as the function’s expected list-of-lines representation, keeping the parsed output consistent with the source dictionaries.

## Finding #576

In @tests/skills/test_review_learning_skill.py around lines 385 - 389, Mark test_interactive_approval_sequence with the integration test marker instead of the unit marker, preserving its existing test behavior and setup.

## Finding #585

In @tests/workflows/test_review_learning_rules.py around lines 304 - 314, In the test assertion block, assert that resolved_effects contains exactly one effect before accessing resolved_effects[0]. Then retain the existing effect property assertions, so both missing and unexpected additional effects are reported clearly.

## Finding #622

In @tests/workflows/test_rewrite_rules.py around lines 223 - 225, Replace the duplicated _ACTION_FIRST_PREFIXES,_GET_SKILL_RE, _COMMAND_CALL_RE, and_is_action_first_reason logic in the tests with the production redirect classifier from claude_code.py. Import and call the adapter’s existing predicate, or expose it through a shared helper, so the framing assertions directly track production behavior.

## Finding #663

In @src/gobby/workflows/state_manager.py around lines 383 - 433, Extract the repeated session-variable read-modify-write/insert logic from upsert_bounded_list_variable, upsert_open_tool_error, merge_variables, append_to_bounded_list_variable, record_edited_file, and the other affected methods into a shared _mutate_variables(session_id, mutator) helper. Have the helper handle transaction setup, SELECT, payload decoding, mutation, and UPDATE/INSERT persistence, while each caller supplies only its variable transformation and preserves its existing return behavior.

## Finding #664

In @src/gobby/workflows/state_manager.py around lines 503 - 537, Update resolve_open_tool_errors so it compares the normalized error records before and after removing the canonical tool-and-target pair, and returns without issuing the UPDATE when the records are unchanged. Avoid materializing open_tool_errors or changing updated_at for sessions with no matching record; retain the existing write behavior when a record is actually removed.

## Finding #679

In @tests/workflows/test_session_variable_manager.py around lines 603 - 628, Update both barrier synchronization points in test_open_tool_error_concurrent_upserts_merge_counts and the additionally affected test block to pass a finite timeout to threading.Barrier.wait(). Apply the timeout for worker and main-thread waits so any worker failure breaks the barrier and causes the test to fail promptly instead of hanging.

## Finding #680

In @tests/workflows/test_summary_actions.py around lines 168 - 175, Add a parametrized test case alongside the existing summary action assertions that supplies enough unresolved-error records for format_unresolved_errors(records) alone to exceed TRANSCRIPT_FALLBACK_MAX_CHARS. Verify the resulting structured_context respects the cap and preserves the intended degenerate-path behavior when base_budget becomes negative, while retaining the existing assertions for the normal 10-record case.

## Finding #711

In @src/gobby/workflows/summary_actions.py around lines 807 - 812, Update the summary-generation logger call in the relevant workflow action to keep the message static and pass mode, reason, and output_chars through structured extra context, matching _write_summary_file and the LLM-failure log. Update test_generate_summary_success to assert the structured logging fields rather than the embedded formatted message.

## Finding #749

In @src/gobby/workflows/condition_helpers.py around lines 32 - 33, Remove the local MEMORY_RECALL_DELIVERIES_VARIABLE definition in condition_helpers.py and import and reuse the shared constant from memory_recall_delivery.py, ensuring all existing references continue using that single source of truth.

## Finding #752

In @tests/workflows/test_memory_recall_gate_rules.py around lines 61 - 66, Update the fixture setup around the workflow_definitions mutations to execute both the disable-all and per-rule enable updates within a Hub database transaction. In the rule loop, replace the %s parameter marker with the required $1 placeholder while continuing to bind rule_name as the parameter.

## Finding #753

In @tests/workflows/test_skill_loaded_call_tool_path.py around lines 136 - 140, Add the appropriate integration marker to test_oversized_get_skill_wrapper_result_survives_codex_normalization_and_compaction so marker-based selection categorizes it correctly, while preserving the existing asyncio marker and test behavior.

## Finding #754

In @tests/workflows/test_skill_loaded_call_tool_path.py around lines 157 - 162, Replace the untyped callback lambda in the get_skill registry registration with a typed function or callable that annotates its name parameter and oversized_skill return value, while preserving the existing callback behavior.

## Finding #755

In @tests/workflows/test_step_enforcement.py around lines 347 - 350, Update the assertions around response.reason in the step-enforcement test to first verify that the exact “During this skill-loading step:” delimiter is present, then extract guidance using the existing split logic. Keep the existing assertions preventing list_tools and get_tool_schema while still requiring the plan-review get_skill call.

## Finding #785

In @tests/workflows/test_condition_helpers.py around lines 42 - 47, Update the task helper containing the manager.create_task call so it inserts the default validation_criteria into kwargs only when the caller has not supplied one, then expand kwargs without passing validation_criteria separately. Preserve caller-provided test-specific criteria.

## Finding #786

In @tests/workflows/test_memory_lifecycle_rules.py around lines 143 - 163, Update the new test methods test_event_and_effect and test_matches_plan_boundaries to annotate db as HubDatabase and manager as LocalWorkflowDefinitionManager, preserving the existing return and parameter annotations.
