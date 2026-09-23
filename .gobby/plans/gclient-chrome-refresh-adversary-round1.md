# gclient-chrome-refresh plan: adversary round 1 (pending votes)

Adversary: plan-adversary-taskless, provider grok, model grok-4.7, reasoning high, no native
subagents (#22711). Run 2b73b50b-548c-46a6-9874-f58631ba1918, evidence
996c11d2-3783-46f4-a910-9dc104ae0a7a, plan hash b996672ea2fe, round 1 of 1. Delivered
2026-09-22 16:12Z (message 50bc1432). The verdict is **needs_review**, with 7 blocking findings. All
three lanes finished; the shadow manifest is valid with 27 entries (attestation 3963f2cf). The first
attempt, b8fba33c (evidence 3d70d0b7), was killed by #22711 and expired.

Canonical JSON (kept byte-exact for finalization):
`/private/tmp/claude-501/-Users-josh-Projects-gobby/b2108bff-76f2-4528-baff-2b06f47ced7a/scratchpad/chrome-adv-r1.json`.

Six of the seven findings independently confirm pending enhancer suggestions from
`gclient-chrome-refresh-enhancement-round1.md`. One vote per row covers both the finding and its
matching cr item. Once Josh votes, the assistant runs the protocol steps: append the changelog
round, finalize the evidence, then apply the typed repairs. The planner gobby#14171 folds the
accepted prose fixes and the cr votes, revalidates and refreshes the hash.

| # | Finding | Section | Category | Matches | Recommendation |
| --- | --- | --- | --- | --- | --- |
| 1 | gcr-r1-provider-label | 3.1 | missing-requirement | cr-3 | Accept |
| 2 | gcr-r1-menu-title-set | 3.2b, 3.11 | bad-sequencing | new | Accept, with a corrected fix (keep Edit) |
| 3 | gcr-r1-pin-sidebar | 3.11, 3.3 | missing-requirement | cr-6 | Accept |
| 4 | gcr-r1-menu-line-ceiling | 3.2b | bad-sequencing | cr-5 | Accept |
| 5 | gcr-r1-roster-field | 2.1 | traceability | cr-1 (2.1 part) | Accept |
| 6 | gcr-r1-scripted-harness | 3.11 | weak-testability | cr-4 | Accept (carries a typed add_acceptance repair) |
| 7 | gcr-r1-dialog-match | 3.12, 3.13 | unhandled-edge | cr-2 | Accept |
| - | cr-1, rest (1.2, 1.3, 2.2 consumers unchanged) | 1.2, 1.3, 2.2 | enhancement | - | Accept (clears the validator warnings) |
| - | cr-7 hold_attach Notify gate | 3.10 | enhancement | - | Accept |
| - | cr-8 always move project_workspace | 3.10 | enhancement | - | Accept |

All seven findings are blocking severity. As specified, each one would fail to compile, fail a
test, or ship wrong behavior.

## 1. gcr-r1-provider-label (blocking, missing-requirement), 3.1 (also 1.1)

check_key provider-label-matches-daemon-table. Location: the 3.1 research parenthetical and acceptance 3.1.3.
Problem: 3.1 says definition_label mirrors `_PROVIDER_TITLE_LABELS`
(src/gobby/storage/sessions/_title_defaults.py), then retypes it wrongly: claude becomes "Claude
Code", and pipeline, system and unknown are left out. The daemon maps claude to "Claude" and
claude_code to "Claude Code". A claude session would show "Claude Code" in the client while its
provisional title from 1.1 says "Claude".
Fix: quote the real dict, including its fallback. Tests: claude_code expects "Claude Code", and claude expects "Claude".

## 2. gcr-r1-menu-title-set (blocking, bad-sequencing), 3.2b and 3.11. New; not in cr-1 to cr-8

check_key menu-bar-title-set. 3.2b publishes `MENU_TITLES: [&str; 7] = ["Gobby", "File", "Edit", "View",
"Window", "Agent", "Help"]`, and acceptance 3.2b.1 requires seven titles. 3.11 defines
`MenuBarMenu { Gobby, File, Agent, View, Window, Help }` (six, with no Edit, in a different order) and
makes the row read `MenuBarMenu::ALL`. After 3.11 the row shows six titles, 3.2b.1 becomes false,
and the Edit items disappear.
**The adversary's fix drops Edit. It argues that Decision item 10 never names an Edit menu. That
contradicts your signed-off canvas.** The canvas Map shows `Edit: Copy Mode; Rename Pane, Tab,
Terminal; Clear Pane Name; right-click passthrough`. The menu bar row in every render is
Gobby File Edit View Window Agent Help
(~/.claude/plans/gclient-chrome-refresh-sources/render/Map.html and Menus.html).
**Recommended fix:** keep 3.2b's seven titles. 3.11's `MenuBarMenu` gains `Edit` and uses the canvas
order `Gobby, File, Edit, View, Window, Agent, Help`. 3.11 fills Edit with the canvas items. The
title list is defined once, in `MenuBarMenu::ALL`, and 3.2b's row reads it.

## 3. gcr-r1-pin-sidebar (blocking, missing-requirement), 3.11 (also 3.3)

check_key pin-sidebar-has-no-keymap-action. 3.3 rebinds `Action::ToggleSidebar` and adds no pin
action. 3.11 dispatches Pin Sidebar as "the keymap action 3.3 introduces", which doesn't exist.
Fix, same as cr-6: 3.3 adds `MenuAction::PinSidebar`. The View item and the SidebarPinned settings
row call one toggle: flip pinned, persist sidebar_pinned, and clear the overlay when the result is
pinned. There is no new binding. Show Sidebar stays `Act(ToggleSidebar)`.

## 4. gcr-r1-menu-line-ceiling (blocking, bad-sequencing), 3.2b (also 3.3, 3.11)

check_key menu-rs-split-before-regroup. The gclient size test counts every line, cfg(test)
included. menu.rs is 970 lines; its tests start at line 450. 3.2b and 3.3 grow the file before
3.11 moves the tests out, so about 30 added lines fail the size test at 3.2b's close.
Fix, same as cr-5: 3.2b moves the inline tests to `crates/gclient/src/app/live_loop/menu/tests.rs`
first and adds that file to its Targets. 3.11 then moves only the item builders.

## 5. gcr-r1-roster-field (blocking, traceability), 2.1

check_key roster-row-task-title-default. `AttentionRosterRow` has no field defaults. Adding a required
`task_title` breaks three keyword constructors outside the targets (TypeError):
tests/agents/test_attention_metadata.py:111, tests/config/test_live_policy_consumers.py:214 and
tests/servers/routes/test_config_startup_stragglers.py:24.
Fix, the 2.1 part of cr-1: declare `task_title: str | None = None`, and list the three files as consumers
unchanged. The adversary rates the rest of cr-1 (1.2, 1.3, 2.2) as validator warnings only, since those callers don't
break. Accepting it anyway clears the 12 warnings.

## 6. gcr-r1-scripted-harness (blocking, weak-testability), 3.11. Typed repair

check_key menu-bar-test-uses-live-dispatch. The planned test activates items through the scripted loop, but
`apply_scripted_menu_action` sends every non-Act item to `apply_local_menu_action`, whose
wildcard returns false. So MarkSeen, ShowAlerts, DestroyOrphans, Arrange, OpenNewGrid, ShowDaemon
and ShowAbout never reach `apply_live_menu_action`. The test passes only by weakening the
assertion.
Fix, same as cr-4: the test calls `apply_live_menu_action` for non-Act items.
Typed repair (applied by apply_plan_review_repairs if accepted): add_acceptance on 3.11: "The
menu-bar dispatch test calls apply_live_menu_action for every enabled non-Act item, and an
Ok(false) fall-through from apply_scripted_menu_action is a failure." The artifact is
`test: crates/gclient/tests/menu_bar.rs::every_menu_bar_item_dispatches_to_a_handler`.

## 7. gcr-r1-dialog-match (blocking, unhandled-edge), 3.12 (also 3.13)

check_key dialog-keys-match-in-project-dialog-key. `route_modal_key` matches `Mode`, so it can't take a
`Dialog::NewGrid` arm. `project_dialog_key` (projects.rs, 816 lines) matches `Dialog`
exhaustively, so the new NewGrid, Daemon and About variants don't compile until it has arms for them.
Neither section targets projects.rs.
Fix, same as cr-2: add the arms inside `project_dialog_key` beside `Dialog::Alerts`, and add projects.rs
to the Targets of 3.12 and 3.13.

## Votes

Josh: reply "accept as recommended", or list the numbers or cr items you decline.

Vote: accept as recommended — Program Director Decision 37: findings 1 through 7 accepted; finding 2 folded as the corrected fix (the signed gclient chrome canvas shows Edit, so the title set is Gobby, File, Edit, View, Window, Agent, Help, with `MenuBarMenu::ALL` defined once in `crates/gclient/src/ui/menu_bar.rs` by 3.2b); finding 6's typed `add_acceptance` repair landed verbatim as 3.11.7. Folded by #22720 (writer gobby#14278).
