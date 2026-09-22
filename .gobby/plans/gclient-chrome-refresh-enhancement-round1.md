# gclient-chrome-refresh plan: enhancement round 1 (pending votes)

Enhancer: plan-enhancer-taskless, provider grok, model grok-4.7, reasoning high.
Run d60a53a9-129f-434a-86ad-fe249147d191, child session a421113a (gobby#14246), round 1 of 1,
converged: false, 8 suggestions. Delivered 2026-09-22 15:22Z (message 3c9a79cf). No edits applied yet;
record accept/decline per item, then fold accepted items into `.gobby/plans/gclient-chrome-refresh.md`
and add the `kind: enhancement` provenance under V1. The planner gobby#14171 owns folding the votes.

Note: the enhancer could not re-run `gobby plans validate` in its sandbox ("the CLI role was denied
the projects table"). It checked consumers with gcode instead. Base validate before the round exited 0
with consumer-coverage warnings on 1.2, 1.3, 2.1, 2.2 and 3.3; cr-1 answers those.

Text below is the enhancer's, verbatim.

## cr-1 (enh-consumer-coverage) [better / clarity] 1.2, 1.3, 2.1, 2.2
impact high, effort medium, risk low

Description: Restraint rung 2: the coverage contract already has Consumers unchanged and file-wide ::* targets; point those at the consumers the index actually has. Checked with gcode usages and gcode grep. plans validate could not be re-run here because the CLI role was denied the projects table. Exact symbol targets in these sections have owned consumers that are neither Targets nor Consumers unchanged. 3.3 render_tab_bar does not: its four mention sites are tabs.rs (the target), tab_surface.rs (already unchanged), parity/chrome.rs (a 3.3 target), and parity/tabs.rs (a 3.5 target; coverage counts every plan target file).

Suggested enhancement: 1.2 Consumers unchanged, for handle_before_agent callers whose signature stays the same and that do not assert heuristic titles: src/gobby/hooks/event_handlers/__init__.py (re-exports the mixin); src/gobby/servers/websocket/chat/_lifecycle.py (dispatches by event type); tests/hooks/test_agent_events_coverage.py; tests/hooks/test_agent_handlers.py; tests/hooks/test_handler_execution.py; tests/hooks/test_immediate_help.py; tests/hooks/test_session_activation_reconciliation.py; tests/hooks/test_transcript_path_derivation.py. Reason: the method keeps its signature and a heuristic grep of these files is empty. 1.3: replace src/gobby/storage/sessions/_manager.py::SessionManager with src/gobby/storage/sessions/_manager.py::* and scope-reason: drop HEURISTIC_TITLE_SOURCE from SessionManager._VALID_TITLE_SOURCES (the classvar is not its own indexed symbol) and its import. Call-graph consumers of the class are 198 files that do not read the set. Keep acceptance symbol SessionManager. Drop session_models.py::Session and keep session_models.py::Session.from_row. The only Session(heuristic_title=) constructor is the models test already targeted. Add Consumers unchanged for from_row callers that read .ref or terminal_context and do not pass heuristic_title: src/gobby/cli/tasks/_utils/rendering.py, src/gobby/servers/routes/tasks.py, src/gobby/sessions/clear_continuation.py, src/gobby/sessions/compact_continuation.py, src/gobby/sessions/compact_identity.py, src/gobby/storage/agent_resume.py, src/gobby/storage/agents/_lifecycle.py, src/gobby/storage/session_activity.py, src/gobby/storage/session_lifecycle.py, src/gobby/storage/sessions/_discovery.py, src/gobby/storage/sessions/_identity_crud.py, src/gobby/storage/sessions/_lineage_discovery.py, src/gobby/storage/sessions/_query.py, src/gobby/storage/sessions/_terminal.py, src/gobby/storage/sessions/_terminal_revival.py, src/gobby/storage/sessions/_transcript.py, src/gobby/storage/terminals.py, tests/storage/sessions/test_compact_identity_reconciliation.py, tests/storage/sessions/test_edge_cases.py, tests/storage/test_local_model_flags.py. _bulk_update.py and the models test are already covered. 2.1: declare task_title: str | None = None on AttentionRosterRow. The dataclass has no defaults, and three keyword constructors sit outside the roster tests: tests/agents/test_attention_metadata.py, tests/config/test_live_policy_consumers.py, tests/servers/routes/test_config_startup_stragglers.py. Add those three to Consumers unchanged with that reason. The startup-straggler test imports _run_tmux_payload from attention.py; the section re-import keeps that path. from_row still sets task_title explicitly. 2.2: add Consumers unchanged for spawn_web_terminal callers whose signature does not change and that do not assert process keys: tests/servers/test_terminal_ws_create.py, tests/terminals/test_backend_selection.py, tests/terminals/test_tmux_runtime.py. tests/agents/test_spawn_executor.py is already a plan target file. terminal_ws_create.py and workspace_ops.py are already unchanged.

Vote: accept — Program Director Decision 37; folded into 1.2, 1.3 (`_manager.py` and `session_models.py` as `::*` scopes, since exact and wildcard cannot mix), 2.1 and 2.2 by #22720 (writer gobby#14278).

## cr-2 (enh-project-dialog-keys) [better / clarity] 3.12, 3.13
impact high, effort small, risk low

Description: Restraint rung 2: dialog keys already live in one match. route_modal_key (modal_input.rs:93) matches Mode and always calls project_dialog_key. That function is in projects.rs:528 and matches Dialog, including Dialog::Alerts at line 637. A Dialog::NewGrid arm cannot sit ahead of the Mode arm. New Dialog variants also fail exhaustiveness until project_dialog_key matches them. projects.rs is 816 lines, under the 850 growth lint.

Suggested enhancement: Replace the 3.12 and 3.13 assumption with this: New Grid, Daemon, and About key arms are added inside project_dialog_key, beside Dialog::Alerts. Add crates/gclient/src/app/live_loop/projects.rs to both sections' Targets. 3.13 already depends on 3.12, so the shared file is ordered. route_modal_key stays the Mode match; it does not gain a Dialog arm. Mode::ProjectDialog is still set the way open_alerts_dialog does at modal_input.rs:120.

Vote: accept — Decision 37; folded into 3.12 and 3.13 (`project_dialog_key` arms, `projects.rs` targeted, `modal_input.rs` unchanged) by #22720.

## cr-3 (enh-provider-label-table) [better / clarity] 3.1
impact high, effort small, risk low

Description: Restraint rung 2: copy the daemon table that already produces the persisted provisional title. _PROVIDER_TITLE_LABELS in src/gobby/storage/sessions/_title_defaults.py:12 maps claude to Claude and claude_code to Claude Code, plus pipeline to Pipeline, system to System, and unknown to Unknown. Section 3.1 currently says claude maps to Claude Code and omits those three. definition_label is supposed to match the provisional title. Acceptance 3.1.3's Claude Code result is the claude_code source.

Suggested enhancement: In 3.1 research, replace the mapping sentence with that dict, including pipeline, system, and unknown, falling back to the raw source the way provider_title_label does. The new test builds a session-only entry with provider claude_code to expect definition_label() == Claude Code, and a claude entry expects Claude. Do not add a second client-only table.

Vote: accept — Decision 37; folded into 3.1 research and acceptance 3.1.3 (`_PROVIDER_TITLE_LABELS` mirrored, `claude` → `Claude`, `claude_code` → `Claude Code`) by #22720.

## cr-4 (enh-scripted-menu-dispatch) [better / testability] 3.11
impact med, effort small, risk low

Description: Restraint rung 2: the fall-through already exists; the test should call the dispatcher that actually runs non-Act items. apply_local_menu_action (menu.rs:195) has a wildcard arm that returns false. apply_scripted_menu_action (run_loop.rs:273) sends every non-Act variant there and returns Ok(false). focus_menu_target (actions.rs:386) already has a wildcard arm, so ContextMenuKind::MenuBar compiles without a new arm. A scripted activation of MarkSeen, ShowAlerts, DestroyOrphans, Arrange, OpenNewGrid, ShowDaemon, or ShowAbout does not reach apply_live_menu_action.

Suggested enhancement: Replace the 3.11 verify sentences with those three sites. The new menu_bar test calls apply_live_menu_action, the function this section moves to menu_dispatch.rs, for non-Act items, and may use the scripted loop for Act items. Leave the scripted runner as it is. An explicit MenuBar arm in focus_menu_target stays empty. 3.12's confirmation test can keep asserting ModalOutcome::Menu; create_grid stays a direct call, because scripted Menu outcomes also return false today.

Vote: accept — Decision 37; folded into 3.11 (test calls `apply_live_menu_action` for non-Act items; three dispatch sites named; acceptance 3.11.7) by #22720.

## cr-5 (enh-menu-rs-size) [better / sequencing] 3.2b, 3.11
impact med, effort small, risk low

Description: Restraint rung 2: 3.11 already extracts this test module; the extract has to happen before 3.2b grows the file. source_size.rs counts every line with text.lines().count() and does not stop at cfg(test). menu.rs is 970 lines and cfg(test) starts at line 450. 3.2b says production is 449 lines so no split is needed, then edits the file. 3.11, which depends on 3.2b, is what moves the tests. A regroup that adds 30 lines fails the size test at 3.2b's close.

Suggested enhancement: In 3.2b, before regrouping items, move the inline test module from menu.rs line 450 to crates/gclient/src/app/live_loop/menu/tests.rs and add that file to 3.2b Targets. 3.11 already depends on 3.2b, so sharing the file is ordered. 3.11 then moves only the item builders, and its research says the tests were already moved. 3.2b drops the sentence that production-line count exempts the file.

Vote: accept — Decision 37; folded into 3.2b (test module moved to `menu/tests.rs` before regrouping, false production-only exemption removed, acceptance 3.2b.6) and 3.11 (Targets, body, 3.11.5) by #22720.

## cr-6 (enh-pin-sidebar-contract) [better / clarity] 3.3, 3.11
impact med, effort small, risk low

Description: Restraint rung 2: settings rows already mutate prefs inline, and ToggleSidebar is the only sidebar Action. Action::ToggleSidebar at actions.rs:505 flips collapsed and persists sidebar_collapsed. The binding is toggle_sidebar, prefix+b. SettingsRow::PaneBorders at modal_input.rs:392 writes the pref directly and is not an Action. 3.3 research says rebind that action and add none, so pin has no chord.

Suggested enhancement: Settle 3.3 as follows. actions/sidebar.rs gains toggle_sidebar_pin, which flips pinned, persists sidebar_pinned, and clears overlay when the result is pinned. The SidebarPinned settings row and View > Pin Sidebar both call it. The menu item is MenuAction::PinSidebar, added in 3.3 because 3.3 already targets menu.rs and lands before 3.11. Show sidebar stays Act(ToggleSidebar). Delete 3.11's pin_sidebar assumption. Acceptance 3.11.4 says show sidebar is Act(ToggleSidebar) and pin sidebar is MenuAction::PinSidebar. No new keymap binding.

Vote: accept — Decision 37; folded into 3.3 (`toggle_sidebar_pin`, `MenuAction::PinSidebar` via `apply_local_menu_action`, acceptance 3.3.7) and 3.11 (assumption removed, 3.11.4) by #22720.

## cr-7 (enh-mock-attach-hold) [better / clarity] 3.10
impact med, effort small, risk low

Description: Restraint rung 2: the mock already has Notify gates; workspace_attach has none. websocket_reply returns the attach body immediately (mock_daemon/mod.rs:895) and the read loop sends it at once (lines 687-694). suppress_ws of workspace_attach returns None, which drops the reply. pause_websocket_reads stalls every following read, including subscribe and health. The file is 1055 lines and is a test, so the size ceiling does not apply.

Suggested enhancement: Replace the 3.10 assumption with this: hold_attach() stores an Arc<Notify> on MockState, checked in the async send loop only when the request type is workspace_attach, using the same Notify pattern as websocket_read_gate. The test holds, asserts the first frame, then notifies. suppress_ws drops the reply, so it is the wrong gate.

Vote: accept — Decision 37; folded into 3.10 (`hold_attach` as an `Arc<Notify>` gate on the `workspace_attach` reply) by #22720.

## cr-8 (enh-project-workspace-split) [better / clarity] 3.10
impact low, effort small, risk low

Description: Restraint rung 6: the conditional is a false branch an executor can take. 3.2a moves SidebarState into chrome/sidebar_state.rs. It does not move Chrome::project_workspace or project_node. Acceptance 3.10.6 requires crates/gclient/src/ui/chrome/project_workspace.rs.

Suggested enhancement: Delete the sentence that says the split is satisfied if 3.2a already moved those functions. State that 3.10 always moves Chrome::project_workspace and project_node into chrome/project_workspace.rs, and that the chrome.rs edit on top of that move is the connection field.

Vote: accept — Decision 37; folded into 3.10 (the conditional sentence deleted; the move is unconditional) by #22720.
