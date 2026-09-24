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

### W1 Migration number (1.1) — FOLDED in `d42521cc` (writer pre-round commit)
Migrations 446 (`grant_agent_project_resolution`), 447, 448 and 449 (`workspace_default_project`) landed after the plan was written. 1.1 becomes `450_add_runbook_roles.sql`; the latest existing migration is `449_workspace_default_project.sql`; `latest_version` becomes 450 (`crates/gdaemon/tests/cli_contract.rs:58` asserts 449 today; `crates/gcore/src/schema/assets.rs:216` embeds 449). Constraint names unchanged.

### W2 Size guard re-measured (Constraints) — FOLDED in `d42521cc` (writer pre-round commit)
`src/gobby/terminals/workspace_ops.py` 990 (was 964), `src/gobby/storage/workspaces.py` 969, `crates/gclient/src/app/live_loop/actions.rs` 1002 (was 979; over the ceiling with no `#[cfg(test)]` boundary, so `crates/gclient/tests/source_size.rs::no_src_file_at_or_above_1000_lines` (lines 41-48, `>= 1000`) is red on HEAD; last touched by #22780 `132e42c8f5`, an ancestor of HEAD, blob 36133 bytes identical to the working tree; reported as found work to gobby#14069 (`33efc9bc`) and, after the assistant read a compacted `git show` count of 891 lines, to the PD with the byte evidence (`91ee37dd`); the PD closed it: the #22780 merge `3467854a4d`, right after `fff3192be0`, takes the file to 891 lines and source_size passes on the merged tree, so no action; the plan does not target that file), `menu.rs` 970, `live_loop.rs` 938, `app/mod.rs` 950, `daemon/live.rs` 987, `daemon/mod.rs` 989, `startup.rs` 691, `daemon/workspace.rs` 391, `apply_persona.py` 305, `_session_start/agents.py` 241, `agents_spawn_tools.py` 122.

### W3 1.2 pane I/O split re-derived — FOLDED in `d42521cc` (writer pre-round commit)
`PaneWrite` already moved to `src/gobby/terminals/workspace_writes.py` (#22722, with `WorkspacePaneWriteError` and `write_workspace_pane`; `WorkspaceOps._write` now delegates to it). The split list drops `PaneWrite` and moves `PaneOutputWait`, `IDEMPOTENCY_KEY_PATTERN`, `WAIT_CAPTURE_LINES`, `WAIT_CAPTURE_FAILURE_LIMIT`, `pane_send_text` (571-590), `pane_send_keys` (592-626), `pane_read` (628-638), `pane_wait_for_output` (640-689), `_pane_terminal` (931-943), `_runtime` (945-951), `_write` (953-990). New since the plan: `WorkspaceOps.workspace_list` (281-287) and `workspace_snapshot(project_id=)` (332-368); `Workspace.default_project_id` (migration 449) on the workspace row only. Refreshed hints: `tab_create` 372-402, `pane_split` 460-490, `pane_rename` 541-548, `_emit` 721-738, `_fill` 823-873; `WorkspaceManager.create_tab` 600-638, `list_panes` 570-580, `add_pane` 722-735, `remove_pane` 737-750, `swap_panes` 752-764, `move_pane` 766-805, `rename_pane` 817-822, `get_pane_for_terminal` 888-893. `TerminalManager.list_reconcilable_by_machine` was added (no effect on the JOIN).

### W4 2.1 drift — FOLDED in `d42521cc` (writer pre-round commit)
`crates/gclient/src/views/mod.rs:36-37` wraps `crate::startup::run()` (the validator warning): add it to Consumers unchanged. `attach_request(node, workspace, project_id)` now takes three arguments (`daemon/workspace.rs:336-350`); `WorkspaceOp::WorkspaceList` (`workspace.list`) and `WorkspaceRow.default_project_id` exist; `startup::run` is at 674-691. `WorkspaceModel::apply` lives at `crates/gclient/src/app/workspace_ops.rs:47-83`.

### W5 rb-2 verified (Golden Path ruling 10 asks the writer to verify it) — HOLDS
`pub enum WorkspaceEventKind` is a closed serde enum at `crates/gclient/src/daemon/workspace.rs:118`; `daemon_event` (`crates/gclient/src/daemon/live_reader.rs:372`) decodes with `serde_json::from_value(value).map_err(protocol_error)` at 414, so an unknown `pane.role_set` kind drops the socket. 2.1.7 stands.

### W6 Role-definition content (criteria 8-9) — PENDING FOLD (folded with the P3 rewrite after the Q1 ruling, not in the pre-round commit)
Josh's five standing instructions (memories 33cb3885, b8f8c9df, cdd18230, c3c59cb9) go verbatim into every P3 definition's persona text; the assistant definition carries the four communications-coordinator requirements explicitly. Mechanical; folded with P3 once Q1 is ruled.

### W7 bundle.rs non-postgres golden — found work, no plan edit beyond the 1.1 note
`crates/gcore/src/grant/bundle.rs` `#[cfg(not(feature = "postgres"))]` fallback: `GOLDEN_LATEST_CHECKSUM` is 449's sha256 (`41cfe81e...`) beside `latest_version: 447` (#22618 `2b7cf60e79` moved the checksum only). Reported to gobby#14069 (`5089ede8`), routed to the PD. 1.1's research context notes it so step (4) is not a surprise.

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

## Writer sweep after R1
Staged so a validating commit exists before the 21:00 cutover; each stage is one commit.
- S1 record: decisions 8 and 10 marked superseded in place, decisions 13-15 appended (Q1/Q6, Q2, Q3-Q5); V1 step 2 migration number; Constraints wording (sha only). Status: committed `eebfc0ad2db67f2b82168cf3517da46a5f18ed91`.
- S2 3.2 rewrite to the decision-13 roster with the five standing instructions and the shared tail verbatim, a divergence line per definition (criterion 10), the no-backend-named test (criterion 1), and D2 `original_acceptance_items` repointed. Status: pending.
- S3 wiring: 3.1 `agent_scope` lists and the program-director rename of the delegate nudge (keyed on a non-`.md` write path so the PD's merges pass; rule-text change under PD approval); 2.1 second script, split of 2.1.5; 1.2 target and 1.2.7; V1 steps 3-6. Status: pending.
- S4 3.3 (#22713 carrier) after lookup L3; Q4 script assertion after 472b0cbd; Q5 wording after L4. Status: waiting on gobby#14332.

## Lookup answers (gobby#14332, message `1e77d597`, 2026-09-23 20:36 CDT) and writer moves
- Q4 terminal backend: `WorkspaceOps._pane_source` (`src/gobby/terminals/workspace_ops.py:754-790`) resolves `native` unconditionally at 771 for every fresh pane; only `terminal_id` adoption (`_adoptable`, 792) can bring a pre-existing terminal of any backend into a pane. `terminals.backend` (`crates/gcore/assets/schema/baseline.sql:3929`, CHECK tmux|native) is the only backend column; `workspace_panes` has none (migration 440). `_identity_env` (228-242) exports no backend and `get_workspace` returns the `WorkspacePane` fields only. A missing host surfaces from `_fill` (823-873) as `WorkspaceOpError("terminal_failed", ...)`; `docs/guides/gterminal-development-guide.md:91-190` states there is no silent tmux fallback. Only `spawn_agent` resolves a backend (`resolve_terminal_backend`, `src/gobby/agents/spawn_models.py:26-38`; default `TerminalConfig.default_backend` = native). Move: decision 15's condition is met, so no script gterm assertion is added; S3 writes one criteria 1-7 map (1 as the 3.2 test, 3 as the registration line, 5 holds by construction through 2.1.3, and 2, 4, 6, 7 are #21565's per ruling 16).
- L3 #22713 carrier: no durable surface records `description`/`validation_criteria`/`labels` edits (`task_lifecycle_events`, baseline.sql:3742-3752, is state transitions with a text reason; `task_validation_history`, 3845-3858, is run outcomes). `task-close-reviewer.yaml` allows only `gobby-tasks:get_task` and `get_task_diff` (239-240), and `get_task(brief=false)` returns no artifacts. Rule-engine facts: `tool_input.server_name`/`tool_name` plus the promoted nested args (`src/gobby/workflows/engine/enforcement_checks.py:930-940`; pattern in `task-enforcement/block-reopen-task.yaml:10-19`); `claimed_tasks` variable `{uuid: '#N'}` written by `detect_task_claim`; effects set_variable, inject_context, observe, mcp_call, rewrite_input, load_skill, run_command, block (no `warn`). Move: 3.3 creates the carrier: `task_artifacts.claim_snapshot` (text holding JSON) in 1.1's migration 450, written inside `claim_task`/`claim_task_for_agent` (`src/gobby/storage/tasks/_transitions.py:177-251`) when ownership changes hands, surfaced by `get_task(brief=false)`; an `after_tool` `inject_context` rule delivers ruling B1's standing order at the edit; `task-close-reviewer.yaml` moves from Non-goals to Targets and bounces an undisclosed amendment. 3.3 is the follow-up #22713's own criterion asks for.
- L4 #22695: `apply_run` (`src/gobby/tasks/expansion/_apply.py:91-`) always mints children; its only existing-output check refuses a re-apply. Move (a position, not a question): 2.1 stays a deliverable; when expansion mints 2.1's task the Program Director closes #22695 as superseded by it and the lane slot passes to the minted task. The alternative, 2.1 as `kind: deferred` with `task_ref: "#22695"`, would drop 2.1's acceptance coverage (contract line 38 kinds; 291-352 `task_ref`). Named in the hash message.

## Resulting artifact
Pre-round fold commit `d42521cc0981f36430ff1f2505fcd1704b572498`; plan sha256 `aa897d8ce9cdab9740e6aa381723fac45017564283b8900c923a438c464ed150`; bare validate clean; `git diff --check` clean (2026-09-23 20:1x CDT). The enhancer hash is the one published after the sweep, not this one.
S1 commit `eebfc0ad2db67f2b82168cf3517da46a5f18ed91` (decisions 13-15 recorded; bare validate clean; `git diff --check` clean, 20:3x CDT).
Pending: final commit, final hash, validation output, the adversary's finalization message, the PD's review and hand-off to the assistant.
