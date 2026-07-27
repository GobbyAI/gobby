# CodeRabbit Fixes: Frontend, Documentation, and Bundled Content

Web UI, documentation, shared skills/workflows, packaging metadata, and manifest work.

Unresolved original findings: **61**

Original finding IDs: 101, 187, 270-272, 303-315, 325-326, 389-390, 403-415, 451, 465-466, 514-521, 599, 623-626, 636, 654, 733, 738-743, 764

## Finding #101

In @pyproject.toml around lines 128 - 131, Update the dependency pins in pyproject.toml so torch and torchaudio use the same release version. Change either torch==2.13.0 or torchaudio==2.11.0 to establish a matching supported pair, while preserving the existing dependency configuration.

## Finding #187

In @src/gobby/install/shared/detection/qwen.toml around lines 74 - 91, Update the queued_continuation rule’s line_regex so it no longer duplicates the queued_message pattern: remove the standalone queued messages alternative, or otherwise require continuation text together with the queued prompt. Keep queued_message responsible for plain queued-message prompts.

## Finding #270

In @src/gobby/install/shared/prompts/memory/dream.md at line 36, Update Rule 3 in the memory deletion guidance so assigning a delete verdict requires a concrete, citable obsolescence signal. Reframe high age_days, lack of recent access, and hard dates as corroborating evidence only, while preserving them as supporting factors when a concrete contradiction, supersession, completion, or time-bound-state signal is present.

## Finding #271

In @src/gobby/install/shared/skills/wiki-research/SKILL.md around lines 148 - 150, Update Step 6 and Step 8 in the wiki-research skill to ensure custom output contracts provide a stable kebab-case finding slug when accepted-note files are omitted, or derive it from a guaranteed source/item identifier. Use that slug, rather than assuming an accepted-note basename exists, for both hidden markers and retry idempotency.

## Finding #272

In @src/gobby/install/shared/workflows/pipelines/wiki-research.yaml around lines 99 - 105, Update the validation_criteria for the research task to conditionally require the create_tasks branch: when inputs.create_tasks is "true", require linked tasks for surviving findings and triaged-away items with their reasons, matching Skill Steps 9–10. Preserve the existing backlog, backlink, report, source-list, topic, and budget requirements for all runs.

## Finding #303

In @web/scripts/copy-ghostty-wasm.cjs around lines 7 - 30, Update the source resolution in the copy script around SRC to use require.resolve('@wterm/ghostty/package.json') (or the package’s resolvable root entry) and derive the wasm path from that package location instead of assuming web/node_modules. Replace the warning-and-skip behavior when the wasm source is absent with an explicit error and nonzero process exit, while preserving the existing destination creation and copy flow.

## Finding #304

In @web/src/components/activity/SessionsTab.helpers.tsx around lines 151 - 160, Update the blocked-count chip in the sessions activity rendering to include entry.attentionReasons in its accessible name, rather than exposing reasons only through the title attribute. Preserve the existing blocked count label and join multiple reasons consistently with the current display format.

## Finding #305

In @web/src/components/activity/SessionsTab.tsx at line 126, Extract the attention roster and agent-event handling currently used by useAgentRuns into a dedicated useSessionAttention hook, then have useAgentRuns consume that hook for attentionBySession. Update SessionsTab to use useSessionAttention directly and remove its useAgentRuns dependency, ensuring the tab no longer starts the agent-runs fetch or polling stream.

## Finding #306

In @web/src/components/activity/terminal/TerminalKeysBar.tsx at line 58, Add role="group" to the div with aria-label="Terminal quick keys" in the TerminalKeysBar component so assistive technologies recognize and announce its accessible name.

## Finding #307

In @web/src/components/activity/terminal/TerminalView.tsx at line 104, Remove the unused destroyed flag and its cleanup guard from the effect containing TerminalView’s cleanup logic, including the corresponding code around the additional referenced location; retain the actual cleanup operations unchanged.

## Finding #308

In @web/src/components/activity/terminal/__tests__/TerminalTab.test.tsx at line 15, Fix the TerminalTab renderer replacement test by either asserting that data-mount-id changes across the renderer replacement, with a target-switch-driven keyed remount, or removing the unused terminalViewState.mounts/data-mount-id scaffolding and renaming the test to reflect repeated onReady calls. Ensure the three readiness controls exercise distinguishable renderer instances rather than invoking identical callbacks on the same mount.

## Finding #309

In @web/src/components/activity/useSessionProviderOptions.ts around lines 46 - 49, Update the fetch error handler in useSessionProviderOptions to log non-abort failures with structured context before setting registryLoaded to false. Keep AbortError handling silent and preserve the existing state update for all other errors.

## Finding #310

In @web/src/components/chat/ChatMainColumn.tsx around lines 163 - 176, Extract the gobby:show-activity-tab CustomEvent dispatch into a shared typed showActivityTab(tab, sessionId?) helper, matching the existing event detail contract. Update ChatMainColumn’s onOpenTerminal handler and the corresponding useActivityPanel dispatches to call this helper, removing duplicated event names and detail-shape literals.

## Finding #311

In @web/src/hooks/useAgentRuns.ts around lines 184 - 190, Update the buffered-event handling in the roster snapshot flow: partition pending events by matching data.epoch, discard non-matching stale events after a single resync attempt, and avoid the unbounded zero-delay fetch loop by tracking attempts with a resyncAttemptedRef alongside the other refs and applying a small delay/retry cap. Use the matching events in the replay loop instead of pending, while preserving normal replay for events with the current epoch.

## Finding #312

In @web/src/hooks/useAgentRuns.ts around lines 262 - 282, Resync the attention roster when the WebSocket reconnects by updating the useEffect that invokes fetchAttentionRoster, using useWebSocketConnected() as a dependency or resetting attentionCursorRef on reconnection. Ensure the roster is fetched again after a socket outage so missed attention_changed events cannot leave attentionBySession stale.

## Finding #313

In @web/src/hooks/useTmuxSessions.ts around lines 163 - 181, Guard the setIsLoading(false) calls in handleMessage for both the tmux_sessions_list and tmux_kill_result cases with pendingRequestRef.current === null. Keep loading active while any request is pending, and only clear it when no pending request remains.

## Finding #314

In @web/src/hooks/useTmuxSessions.ts around lines 119 - 161, Implement a pending-request deadline for attach, detach, and create-session operations: after each corresponding ws.send call in beginAttachRequest, beginDetachRequest, and createSession, schedule a timeout that verifies the same request is still pending, clears it, resets requestPending/loading state as appropriate, and sets attachError. Store the timer so clearPendingRequest and unmount cleanup cancel it, while preserving correlated-response and socket-close handling.

## Finding #315

In @web/tests/terminal-colors.spec.ts around lines 250 - 255, Update chooseTerminalSession to use an exact accessible-name matcher for the option instead of constructing a RegExp from the name variable, ensuring only the intended terminal session is selected and satisfying the regexp-from-variable lint rule.

## Finding #325

In @docs/guides/llm-features.md around lines 10 - 12, Align the later profile_defaults.feature_low YAML example with the documented table by adding codex/gpt-5.6-luna and matching the listed order, or explicitly label the YAML as an intentional override. Ensure copy-paste users see consistent defaults.

## Finding #326

In @pyproject.toml at line 6, Update the pyproject.toml license metadata from the custom LicenseRef-FSL-1.1-ALv2 value to the canonical SPDX identifier FSL-1.1-ALv2, preserving machine-readable package metadata.

## Finding #389

In @docs/guides/cron-scheduler.md around lines 171 - 175, Update the earlier MCP example for the known create_cron_job tool to call get_tool_schema directly, removing the preceding list_tools call. Preserve the existing schema lookup and subsequent tool invocation, while following the known-unleased-tool flow demonstrated elsewhere in the guide.

## Finding #390

In @docs/guides/mcp-tools.md around lines 27 - 36, Update the discovery bullet in the MCP tools guide to say “unknown server or registry,” restricting list_mcp_servers usage to cases where either the server or registry is unknown. Keep the surrounding tool-discovery instructions unchanged.

## Finding #403

In @src/gobby/install/shared/workflows/agents/goal-taskmaster.yaml at line 88, Update the progressive-discovery guidance in the workflow to state that list_mcp_servers is used only for unknown servers or registries, not general server inspection. Apply the same wording correction to the identical guidance wherever it appears in the repository.

## Finding #404

In @src/gobby/install/shared/workflows/agents/merge-orchestrator.yaml at line 174, Update the progressive-discovery guidance in the workflow instructions to restrict list_mcp_servers to unknown servers or registries, replacing the broader server-inspection wording. Apply the same wording correction to the identical guidance wherever it appears, while preserving the existing rules for leased, known, and unknown tools.

## Finding #405

In @src/gobby/install/shared/workflows/agents/merge-worker.yaml at line 127, Update the progressive-discovery guidance in the merge-worker workflow so list_mcp_servers is permitted only for unknown servers or registries, replacing the broader server or registry inspection wording. Apply the same wording correction to the identical workflow guidance elsewhere, while leaving the tool-handling rules unchanged.

## Finding #406

In @src/gobby/install/shared/workflows/agents/nightly-linter.yaml at line 50, Update the progressive-discovery guidance in the nightly-linter workflow and the identical guidance elsewhere to restrict list_mcp_servers to unknown servers or registries, replacing the broader server or registry inspection wording. Preserve the existing rules for leased, known, and unknown tools.

## Finding #407

In @src/gobby/install/shared/workflows/agents/nightly-test-fixer.yaml at line 50, Update the progressive discovery guidance in the nightly test fixer workflow to state that list_mcp_servers is used only for unknown servers or registries, not general server inspection. Apply the same wording correction to the identical workflow guidance elsewhere.

## Finding #408

In @src/gobby/install/shared/workflows/agents/plan-adversary-taskless.yaml at line 86, Update the progressive-discovery guidance in the workflow task text to restrict list_mcp_servers to unknown servers or registries, replacing the broader “server or registry inspection” wording. Apply the same wording correction to the identical guidance in the other workflow location, while leaving the list_tools and known-tool behavior unchanged.

## Finding #409

In @src/gobby/install/shared/workflows/agents/plan-adversary.yaml at line 178, Update the progressive-discovery guidance in the workflow instruction to say list_mcp_servers is only for unknown servers or registries, not general server inspection. Apply the same wording correction to the identical guidance elsewhere, while preserving the rules for leased, known, and unknown tools.

## Finding #410

In @src/gobby/install/shared/workflows/agents/plan-enhancer-taskless.yaml at line 67, Update the progressive discovery guidance in the workflow instruction to restrict list_mcp_servers to unknown servers or registries, replacing the broader server or registry inspection wording. Apply the same wording correction to the identical guidance in the other workflow location, while preserving the existing rules for leased, known, and unknown tools.

## Finding #411

In @src/gobby/install/shared/workflows/agents/plan-enhancer.yaml at line 99, Update the progressive-discovery guidance in the workflow instruction containing “Use context-aware progressive discovery” so list_mcp_servers is permitted only for unknown servers or registries, not general server inspection. Apply the same wording correction to the identical guidance elsewhere, preserving the existing rules for leased, known, and unknown tools.

## Finding #412

In @src/gobby/install/shared/workflows/agents/planner.yaml at line 106, Update the progressive-discovery guidance at the affected workflow instruction to state that list_mcp_servers is used only for unknown servers or registries, replacing the broader “server or registry inspection” wording. Apply the same wording correction to the identical guidance elsewhere, while preserving the existing rules for leased, known, and unknown tools.

## Finding #413

In @src/gobby/install/shared/workflows/agents/product-manager.yaml at line 61, Update the progressive-discovery guidance in the product-manager workflow to state that list_mcp_servers is used only for unknown servers or registries, not general server inspection. Apply the same wording correction to the identical workflow guidance elsewhere, preserving the existing rules for known and unknown tools.

## Finding #414

In @src/gobby/install/shared/workflows/agents/qa-dev.yaml at line 40, Update the progressive-discovery guidance in the QA developer workflow so list_mcp_servers is permitted only for unknown servers or registries, replacing the broader server or registry inspection wording. Apply the same wording correction to the identical workflow guidance elsewhere, while preserving the existing rules for known and unknown tools.

## Finding #415

In @src/gobby/install/shared/workflows/agents/tech-writer.yaml at line 150, Update the completion condition in the close_task callback to require both a non-preview call and task_id matching assigned_task_id. Preserve the existing behavior for the assigned task while preventing successful closure of another task from completing the workflow.

## Finding #451

In @docs/reviews/agents.md around lines 207 - 208, Add a blank line immediately after the heading about subscribe_agent_completion re-registering unconditionally, before the “Where” list item, to satisfy markdownlint MD022.

## Finding #465

In @src/gobby/install/shared/skills/review/SKILL.md around lines 41 - 51, Update the “Boundaries” guidance in SKILL.md to scope its prohibition on approving, rejecting, or escalating to spawned mode only, or explicitly exempt the in-line epic-reviewer persona. Ensure the in-line mode instructions can execute the required “## Epic Findings” verdict and create remediation tasks for blocking findings.

## Finding #466

In @src/gobby/install/shared/workflows/agents/epic-reviewer.yaml around lines 40 - 46, Update the epic review state machine around the initial claim transition to detect an already-closed epic and route directly to the CLOSED-EPIC REVIEW flow without requiring task_claimed. Modify the review-step action permissions/transitions so gobby-tasks:reopen_task is allowed for this closed-epic path, preserving the documented post-hoc review, remediation, and end_agent_run behavior.

## Finding #514

In @src/gobby/install/shared/skills/python/references/testing.md around lines 156 - 158, Update the validation guidance near the test-quality audit instructions to document the uv-backed command using “uv run gobby test-types audit <paths> --baseline .gobby/test-types-baseline.json --fail-on-new” instead of bare gobby, while preserving the requirement to run the audit for changed test files.

## Finding #515

In @src/gobby/install/shared/workflows/agents/epic-reviewer.yaml around lines 39 - 42, Update the epic-reviewer workflow’s load phase to include review-learning in required_skills and fetch it in the get_skill sequence before the lesson workflow is required. Preserve the existing epic-review finding/confirmation behavior and use the established review-learning skill identifier consistently.

## Finding #516

In @src/gobby/install/shared/workflows/agents/plan-adversary-taskless.yaml around lines 233 - 249, Update test_lane_success_hook_records_the_canonical_marker in tests/agents/test_plan_review_researcher_definition.py to parameterize the workflow definitions, covering both plan-adversary.yaml and plan-adversary-taskless.yaml. Assert the same canonical lane-capture behavior for each file so divergent edits to either duplicated on_mcp_success expression are detected.

## Finding #517

In @src/gobby/install/shared/workflows/agents/plan-adversary.yaml around lines 122 - 130, Restore the wording in the planner-side validation instruction so it contains the existing test’s expected phrase “Do NOT re-run the parser pre-verdict,” while preserving the instruction not to repeat validation. Update the relevant text near the `validate_plan_file` and `approve_review` guidance; do not modify the test unless intentionally standardizing terminology across the related documentation.

## Finding #518

In @src/gobby/install/shared/workflows/rules/review-learning/inject-plan-enhancer-lessons.yaml around lines 1 - 20, Update the inject-plan-enhancer-lessons rule to match the canonical schema: retain the required metadata, add the appropriate when condition, and replace the effects wrapper with the required singular effect containing type: mcp_call and its existing call configuration. Add a focused schema/load test covering this rule’s required fields and effect.type.

## Finding #519

In @src/gobby/install/shared/workflows/rules/review-learning/inject-plan-reviewer-lessons.yaml around lines 1 - 20, The inject-plan-reviewer-lessons rule uses the wrong schema: add the required when condition and replace effects with the canonical singular effect structure containing effect.type, preserving the existing MCP call configuration. Ensure the definition includes the required rule-name and all canonical fields, and add a focused schema/load test verifying the rule loads and exposes effect.type for plan adversaries.

## Finding #520

In @src/gobby/install/shared/workflows/rules/review-learning/inject-planner-lessons.yaml around lines 1 - 20, Update the inject-planner-lessons rule to match the canonical rule-definition schema: add the required rule-level when condition, replace effects with the singular effect field, and preserve the MCP call under effect.type with its existing configuration. Add a focused schema/load test covering this rule’s required tags, rule name, description, event, enabled, priority, when, and effect.type fields.

## Finding #521

In @src/gobby/install/shared/workflows/rules/review-learning/inject-qa-reviewer-lessons.yaml around lines 1 - 20, The inject-qa-reviewer-lessons rule must match the canonical rule-definition schema: add the required when condition, replace the effects collection with the singular effect field while preserving the MCP call configuration under effect.type, and retain the existing metadata and scope. Add a focused schema/load test covering this rule’s required fields and successful loading.

## Finding #599

In @src/gobby/install/shared/workflows/rules/worker-safety/no-full-test-suite.yaml around lines 16 - 17, Update the pytest remediation examples in the no-full-test-suite rule to prefix every command with GOBBY_TEST_PROTECT=1, including both targeted path and -k pattern examples. Preserve the existing guidance that full-suite runs are reserved for the user.

## Finding #623

In @web/src/components/chat/ChatInput.tsx at line 216, Update the ChatInput attachment handling around useChatInputAttachments so that when imagesDisabled changes to true, all queued image entries are removed and their associated resources are cleaned up. Preserve non-image attachments and normal behavior while image support remains available.

## Finding #624

In @web/src/components/chat/__tests__/ProviderPicker.test.tsx around lines 45 - 64, Rename the helper function buildLocalCatalog to buildEndpointCatalog and update all five test call sites to use the new name, preserving its catalog construction behavior unchanged.

## Finding #625

In @.github/workflows/cla.yml around lines 3 - 7, Add a workflow-level concurrency configuration near the top-level triggers in the CLA workflow, using one shared group for all pull_request_target and issue_comment runs and setting cancel-in-progress to false. Ensure every signature update is queued and processed rather than canceled or running concurrently.

## Finding #626

In @.gobby/plans/context-mode-borrowings.md around lines 307 - 318, Update the tool_results schema’s total_chars field to use a 64-bit-compatible type, and align any corresponding application model, migration, or persistence types that write or read it. Preserve the existing max_stored_chars behavior while ensuring serialized inputs exceeding the signed 32-bit range can be persisted.

## Finding #636

In @docs/guides/gcode-development-guide.md around lines 96 - 111, Update the earlier PostgreSQL Bootstrap section to match the runtime-mode contract: document daemon-mode DSN precedence as GCODE_DATABASE_URL/GOBBY_POSTGRES_DSN, daemon effective configuration, then bootstrap.yaml, without falling back to full gcore.yaml or standalone resolution on daemon failure. Document standalone mode separately with its full gcore.yaml fallback, preserving the established symbols and precedence.

## Finding #654

In @src/gobby/install/shared/prompts/validation/validate.md around lines 85 - 88, Refresh the corresponding entry for src/gobby/install/shared/prompts/validation/validate.md in bundled_content_manifest.json so its recorded content hash or metadata matches the updated prompt, preserving the manifest’s existing format and ordering.

## Finding #733

In @docs/guides/telegram.md around lines 12 - 15, Update the opening summary under “Quick path: a private DM bot” to state that ownership is enrolled only when the exact private /start command is received, matching the enforced behavior described later in the guide.

## Finding #738

In @src/gobby/install/shared/skills/tasks/SKILL.md around lines 40 - 50, Add language identifiers to all four fenced code blocks in the task skill documentation: use python for executable call_tool examples and text for intentionally pseudocode blocks, including the blocks near the referenced sections. Ensure every fence satisfies markdownlint MD040.

## Finding #739

In @src/gobby/install/shared/skills/tasks/SKILL.md around lines 16 - 18, Update the task lifecycle instructions in SKILL.md to require fetching a known unleased schema with get_tool_schema("gobby-tasks-ops", "<tool>") before the first gobby-tasks-ops call, unless that server is explicitly exempt. Preserve the existing gobby-tasks schema lookup guidance and autonomous review-transition usage.

## Finding #740

In @src/gobby/install/shared/skills/tasks/references/creation.md around lines 55 - 65, Add an explicit language identifier to the fenced code blocks containing the call_tool examples, including the examples around the create_task snippet and lines 70–81. Use python for executable Python examples and text for pseudocode, preserving the example contents unchanged.

## Finding #741

In @src/gobby/install/shared/skills/tasks/references/evidence-provider-recovery.md around lines 34 - 42, Add a language identifier to the fenced code example containing the call_tool invocation, using python or text so markdownlint MD040 passes. Leave the example content unchanged.

## Finding #742

In @src/gobby/install/shared/skills/tasks/references/no-work-closures.md around lines 17 - 25, Add a language identifier to the fenced code example containing the call_tool invocation, using python or text to satisfy markdownlint MD040 while leaving the example content unchanged.

## Finding #743

In @src/gobby/install/shared/skills/tasks/references/review-flows.md around lines 9 - 14, Add language identifiers to the fenced code examples in the review-transition documentation, including the examples around submit_for_review and the other two referenced sections. Use python for executable call_tool examples and text where the block is non-code, preserving their existing contents.

## Finding #764

In @src/gobby/install/shared/prompts/validation/validate.md around lines 104 - 115, Update the validation prompt’s earlier instruction that refers to “invalid or pending” verdicts so it only refers to the supported “invalid” status. Keep the existing guidance for populating issues unchanged and ensure the status wording matches the valid/invalid contract described in the final format specification.

## Finding D1

In @src/gobby/install/bundled_content_manifest.json and @src/gobby/install/shared/workflows/rules/memory-lifecycle/digest-on-plan-turn-end.yaml, regenerate the bundled-content manifest after all shared-content changes. The focused synchronization invariant currently reports hash drift for digest-on-plan-turn-end.yaml.
