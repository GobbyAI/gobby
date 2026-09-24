# Runbooks — council round 2

Plan: `.gobby/plans/runbooks.md` (Plan ID: runbooks; epic #22691 "Runbooks: role-bound multi-agent tabs loaded from gclient"; lane epic #22772 "Lane: runbooks (Golden Path track 3)").
Planning task: #22808 "Runbooks plan council round: enhance, adversarial review and gate .gobby/plans/runbooks.md".
Pre-round hash: `ab259dde9ddcd1c1e4201f4fabcc6212040a83fe635adfb29f59f0baca59df92` (writer commit `2893fa41f9473daf6e2529415cf58c5ee2decfd8`, the 2026-09-22 bytes with enhancement round 1 folded under V1, no edits). `uv run gobby plans validate .gobby/plans/runbooks.md` (bare) passes with one non-blocking `consumer-coverage` warning (2.1, `crates/gclient/src/views/mod.rs`).

Flow (Josh, verbatim in #22808, superseding the prompt book's two-round cap): "I was wanting the agents to debate and come to consensus, then the adversary finalizes and sends to PD for review, then the PD sends it to you so I can review it." No round cap. The writer is the only editor of the plan and of this file. Expansion waits for Josh.

Participants:
- Writer: gobby#14425 (`912b3959-4924-4d93-a092-1a18636f2e31`), claude-fable-5-1, pane "plan writer".
- Enhancer: gobby#14422 (Codex gpt-5.6-sol), on hold until the 21:00 CDT gterm cutover; relaunched on the newer model with a new ref supplied by the PD.
- Adversary: gobby#14423 (Grok 4.7), attacks the folded hash after the enhancer round.
- Program Director: gobby#14018 (`44738b98-9535-4ec5-8be5-57b345780be5`); gate and finalization recipient.
- Symbol lookup: gobby#14332 (read-only). Comms hub: gobby#14069.

Priority rule: a signed source outranks either reviewer: `ROADMAP.md`, `~/Desktop/gobby-golden-path-2026-09-22.md` (v14 rulings), `.impeccable.md`, the #22691 description and validation criteria (Josh's five standing instructions; terminal-backend policy; role-definition content), the plan's Decision Record, and the prompt book `~/Desktop/gobby-agent-prompts-2026-09-22.md` (criterion 10: input of record for the role definitions). Later Josh rulings supersede earlier ones.

## Writer pre-round read-through (2026-09-23, 19:1x-20:0x CDT)

Every fact below was verified on 0.5.0 HEAD `218ea813` (the parent of the pre-round commit). Mechanical drift is folded by the writer as a logged pre-round commit (W-items); direction-level conflicts went to the Program Director as one batched list (Q-items) and are not folded until ruled.

### W1 Migration number (1.1) — PENDING FOLD (applied in the writer pre-round commit; sha recorded under Resulting artifact)
Migrations 446 (`grant_agent_project_resolution`), 447, 448 and 449 (`workspace_default_project`) landed after the plan was written. 1.1 becomes `450_add_runbook_roles.sql`; the latest existing migration is `449_workspace_default_project.sql`; `latest_version` becomes 450 (`crates/gdaemon/tests/cli_contract.rs:58` asserts 449 today; `crates/gcore/src/schema/assets.rs:216` embeds 449). Constraint names unchanged.

### W2 Size guard re-measured (Constraints) — PENDING FOLD (applied in the writer pre-round commit; sha recorded under Resulting artifact)
`src/gobby/terminals/workspace_ops.py` 990 (was 964), `src/gobby/storage/workspaces.py` 969, `crates/gclient/src/app/live_loop/actions.rs` 1002 (was 979; over the ceiling with no `#[cfg(test)]` boundary, so `crates/gclient/tests/source_size.rs::no_src_file_at_or_above_1000_lines` (lines 41-48, `>= 1000`) is red on HEAD; introduced by #22696 "gclient lease-holder aware pane clicks", f6a010c94c; handed to the assistant gobby#14069 as found work, message `33efc9bc`; the plan does not target that file), `menu.rs` 970, `live_loop.rs` 938, `app/mod.rs` 950, `daemon/live.rs` 987, `daemon/mod.rs` 989, `startup.rs` 691, `daemon/workspace.rs` 391, `apply_persona.py` 305, `_session_start/agents.py` 241, `agents_spawn_tools.py` 122.

### W3 1.2 pane I/O split re-derived — PENDING FOLD (applied in the writer pre-round commit; sha recorded under Resulting artifact)
`PaneWrite` already moved to `src/gobby/terminals/workspace_writes.py` (#22722, with `WorkspacePaneWriteError` and `write_workspace_pane`; `WorkspaceOps._write` now delegates to it). The split list drops `PaneWrite` and moves `PaneOutputWait`, `IDEMPOTENCY_KEY_PATTERN`, `WAIT_CAPTURE_LINES`, `WAIT_CAPTURE_FAILURE_LIMIT`, `pane_send_text` (571-590), `pane_send_keys` (592-626), `pane_read` (628-638), `pane_wait_for_output` (640-689), `_pane_terminal` (931-943), `_runtime` (945-951), `_write` (953-990). New since the plan: `WorkspaceOps.workspace_list` (281-287) and `workspace_snapshot(project_id=)` (332-368); `Workspace.default_project_id` (migration 449) on the workspace row only. Refreshed hints: `tab_create` 372-402, `pane_split` 460-490, `pane_rename` 541-548, `_emit` 721-738, `_fill` 823-873; `WorkspaceManager.create_tab` 600-638, `list_panes` 570-580, `add_pane` 722-735, `remove_pane` 737-750, `swap_panes` 752-764, `move_pane` 766-805, `rename_pane` 817-822, `get_pane_for_terminal` 888-893. `TerminalManager.list_reconcilable_by_machine` was added (no effect on the JOIN).

### W4 2.1 drift — PENDING FOLD (applied in the writer pre-round commit; sha recorded under Resulting artifact)
`crates/gclient/src/views/mod.rs:36-37` wraps `crate::startup::run()` (the validator warning): add it to Consumers unchanged. `attach_request(node, workspace, project_id)` now takes three arguments (`daemon/workspace.rs:336-350`); `WorkspaceOp::WorkspaceList` (`workspace.list`) and `WorkspaceRow.default_project_id` exist; `startup::run` is at 674-691. `WorkspaceModel::apply` lives at `crates/gclient/src/app/workspace_ops.rs:47-83`.

### W5 rb-2 verified (Golden Path ruling 10 asks the writer to verify it) — HOLDS
`pub enum WorkspaceEventKind` is a closed serde enum at `crates/gclient/src/daemon/workspace.rs:118`; `daemon_event` (`crates/gclient/src/daemon/live_reader.rs:372`) decodes with `serde_json::from_value(value).map_err(protocol_error)` at 414, so an unknown `pane.role_set` kind drops the socket. 2.1.7 stands.

### W6 Role-definition content (criteria 8-9) — PENDING FOLD (folded with the P3 rewrite after the Q1 ruling, not in the pre-round commit)
Josh's five standing instructions (memories 33cb3885, b8f8c9df, cdd18230, c3c59cb9) go verbatim into every P3 definition's persona text; the assistant definition carries the four communications-coordinator requirements explicitly. Mechanical; folded with P3 once Q1 is ruled.

### Direction-level conflicts (batched to the PD; not folded until ruled)
Q1 roster (decision 8 vs. #22691 criterion 10, Golden Path track 3, rulings 15/21/22/23); Q2 layout (decision 10 vs. the live tabs); Q3 #22713 fold (rulings 7, 10); Q4 terminal-backend criteria 1-7 (ruling 16; daemon side is #21565's); Q5 #22695 placement (ruling 10); Q6 which council flow the definitions encode (#22808 FLOW vs. ruling 21). Full text in the PD message recorded below.

## Enhancer items (round 2)
Pending the enhancer's relaunch after the 21:00 CDT cutover. Format per item: enhancer position; writer verification; move (FOLD with the change named, or CONTEST with reasoning); resolution (folded or withdrawn).

## Adversary findings
Pending; the adversary attacks the folded hash. Same format, plus `check_keys`.

## Program Director rulings

### R1 Rulings on Q1-Q6 (PD message `62c8f875`, 2026-09-23 20:10 CDT)
Verbatim: "PD rulings on the six runbooks questions for #22808 (Runbooks plan council round). Josh sees them at final review and may override. Josh's latest word (09-23 19:5x) supersedes earlier decisions and rulings. The mechanical folds are fine as a logged pre-round commit.

Q6 FLOW: encode #22808's flow. Debate to consensus with no round cap, the adversary finalizes to the PD, the PD may send it back, then the assistant takes it to Josh, and expansion waits for Josh. This supersedes ruling 21's two rounds and verifier report. The fourth pane is a read-only lookup helper (researcher definition, today gobby#14332). plan-verifier is dropped. Its mechanical checks move into the adversary's finalization report: `gobby plans validate`, `git diff --check`, sha256 and coverage-contract checks.

Q1 ROSTER: accept your proposal minus plan-verifier. Definitions: program-director, assistant, lane-manager, researcher, reviewer, archivist, log-monitor, elicitor. Writer, enhancer and adversary become personas on the existing planner, plan-enhancer-taskless and plan-adversary-taskless definitions. Drop post-epic-reviewer (ruling 23).

Q2 LAYOUT: two bundled scripts from 2.1's verbs. orchestration-v1.sh builds control (PD, assistant) and monitors (log monitor, Archivist). plan-council-v1.sh <plan> builds the four-pane council tab. Lane panes stay out: the PD opens them per lane with /goal.

Q3 #22713: no block (Josh rejected a). Ruling 7 requires every amendment reviewed before close, so a session variable is insufficient: it dies with the session and the reviewer cannot see it. The claim-time values must land somewhere durable that the reviewer reads. First ask gobby#14332 whether an existing durable surface already records update_task diffs (task history or audit). task-close-reviewer.yaml stays a Non-goal only if the close reviewer already receives that surface. Otherwise it is in scope with the least change. Keep the injected disclosure order.

Q4 BACKEND: accept, pending gobby#14332. If workspace panes cannot fall back to tmux at all, drop the runbook-script gterm assertion as dead mechanism. Keep the 3.2 no-backend-named test and the registration text.

Q5 #22695 (gclient command mode): mint no duplicate. #22695 stays 2.1's leaf and stays in the gclient lane queue (after #22780, the #22698 leaves and #22805). Ask gobby#14332 whether start_expansion_run can adopt an existing leaf. If it can, adopt. If not, 2.1 cites #22695 as existing work outside expansion in its coverage.

Set_handoff when you need to. The enhancer starts on your refreshed hash after the 21:00 Codex upgrade."

Writer moves on R1:
- Q1/Q2/Q6: FOLD as one P3 + decisions sweep (decisions 7, 8, 10; 3.1 `agent_scope`; 3.2 roster; 2.1 gains `plan-council-v1.sh`; V1 steps 4-6; D2 `original_acceptance_items`). W6 folds inside it. Recorded below under "Writer sweep after R1" with the diff summary and commit.
- Q3: FOLD pending the gobby#14332 answer on a durable update_task diff surface (lookup `3b96fc7d` asked for the rules-engine fields; a follow-up asks for the durable surface). 3.3 is drafted either way with the injected disclosure order; the durable carrier is chosen from the answer.
- Q4: FOLD pending lookup `472b0cbd`. The 3.2 no-backend-named test and registration text are folded now.
- Q5: FOLD. Memory 4430ab84 (expansion-run binding) says a run resolves registrations by task UUID/sequence/path ancestry and derives child stage manifests; it does not say whether an existing leaf under the root is adopted or duplicated. Follow-up lookup sent to gobby#14332. Until answered, 2.1 cites #22695 as existing work.

## Resulting artifact
Pending: final commit, final hash, validation output, the adversary's finalization message, the PD's review and hand-off to the assistant.
