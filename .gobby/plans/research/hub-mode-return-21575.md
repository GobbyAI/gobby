# hub-mode return from Orchestrator review (Plan Writer gobby#16490, 2026-10-14)

Source: Orchestrator gobby#15977 message 089e45ec-902d-4248-938b-3b73dd466247
(retrieve with gobby-agents get_inter_session_message). It returns stamped
hub-mode 7d988fab99 for amendments A-H. Line numbers in it refer to 7d988fab99.
After the repair: 16491 re-stamps, Security gobby#16011 reviews section D before the
re-stamp, then send 15977 the SHA, rendered hash and leaf count.

## Step 0: withdraw M1 (memory f5577ae0 route)

At 7d988fa the plan is 4124 lines; `## M1 Task Manifest` starts at line 3313 and runs
to EOF (blank line 3312 precedes it). One commit removes the whole block plus its
preceding blank line and adds one dated V1 line ("M1 withdrawn for the Orchestrator's
return"). Diff must remove only M1 and add only the changelog line. Then repair.

## Amendments and the facts found so far

A. No deferrals (memory 878d0c35).
- 5.1 must prove write, attention, cancel, merge on all five providers
  (claude, codex, grok, droid, agy), not only spawn. Today 5.1.3 is claude-only,
  5.1.4 one run, 5.1.5 one worktree and one clone.
- Grok and agy have no hooks (Decision 7 at L81-82, 5.1 research). State their remote
  attention path. node-halves 2.13 ports pane detection to Rust
  (`crates/gdaemon/src/pane_detection/attention.rs`, stall classifier), and 2.14 is the
  last node agent leaf; read node-halves 2.13/2.14 for how a node reports pane
  attention to the hub (report field? route?). Cover it here or by a cross-plan edge.
- Bring into scope as a leaf with an edge, or give 15977 a concrete reason:
  #24151 (S2.11 hub half: hook ingress and rule engine in Rust; Decision 18 L262,
  Constraints L417-420, 2.2 L1349-1351), #21549 (machine-scoped terminals view;
  Decision 5/9 L77, L86), #20202 (Remote gobby attach, L85), #24149 (S2.7 hub half Rust
  redo "later", Overview L45). Read each task with gobby-tasks get_task first.

B. Values.
- Node prepare deadline (1.1 text at L699 says "#24148 sets"): node-halves Decision 19
  (node-halves.md line 162) fixes it at 300 seconds. Write 300 s and confirm with 16767.
- 2.2 L1354-1355: enumerate the hook tests to fix instead of leaving them to the
  verification run.

C. Add a "Cross-plan edges set at expansion" table (leaf, external ref, type), one row
per edge, covering Constraints L378-403 plus 4.2 and 4.6 (they edit node-halves files).
Ask 16767 for the table format 15977 gave them so both match.

D. Security, fail closed (Security 16011 reviews D before re-stamp).
- owner_user_id check on every machine-targeting route: cancel 2.4, terminal write 2.3,
  attention respond 3.3, merge/delete by row id 4.3/4.4, node status 4.7; spawn already
  (1.3 `resolve_target_machine` checks the target shares owner_user_id with the origin
  machine). Roster 3.1 (L2318-2325) shows only the caller's machines. Research what
  identity a request carries (FrontDoorIdentity fields, user id?) and where
  machines.owner_user_id lives.
- 2.2 L1321-1326: a hook with no verified identity must bind only local-machine rows or
  be refused.
- 1.1 L693 node-local prompt file: owner-only mode (0600), removed after delivery.
  1.1 is landed (#24268); the contract doc change belongs in 4.1 (contract v3 leaf) or
  node-halves; decide.
- 2.5 L1753-1755 (hub trusts node SRT): cite node-halves' node-side refusal of non-srt
  payloads (15977 asked 16767 for it) as the defense.

E. Validation.
- 4.8.5 (L3068) needs a machine_id test.
- 4.1.1 and 4.1.2 need their own tests, not 1.1's (L2527-2528).
- 5.1 must fail, not skip, when SRT is absent (Decision 22 L351, 5.1 L3091).
- Doc-only items 1.1.1, 1.1.5, 4.1.5 must name an exact file and section.

F. Manifest (routing decisions are the adversary's; tell 16491).
- 1.1 landed as #24268: mark covered by #24268, not a new tdd task. The coverage
  contract has no field for this; state it in 1.1's narrative and ask 16491/15977 how
  derivation/expansion should reuse #24268.
- 5.1: drop assigned_agent and fix task_type. derive_manifest_entries
  (src/gobby/plans/manifest_emitter.py) always sets assigned_agent for non-code
  categories (test maps to developer), so dropping it means changing 5.1's heading to
  `[category: code]` (backend domain). Valid task types: task, bug, feature, epic, chore,
  refactor, simple_fix, research_spike, architecture_doc, prd_doc; alias test maps to task.
  task_type, tdd and assigned_agent come from the adversary's routing decisions.
- 2.1 and 2.2 are task_type bug in M1; 15977 asks to recheck. Neither defect is reachable
  until a node writes rows or sends hooks (node-halves 1.5 unlanded), so feature is the
  likely answer; confirm by reading 2.1/2.2 research text.

G. Contract halves: put resume_session_id and the `existing` workspace mode into the
contract leaf (4.1) or an explicit edge to node-halves 3.1/3.2 (node-halves line 189 and
line 1619 mention both). Add the gclient spawn `machine` field (Story 1, L490) as a leaf.

H. State supersession in the plan: #24280 (D1) by 4.1-4.5, #24281 (D2) by 1.2, #24282
(D3) by 4.6-4.8 plus 3.2.4, #24283 (D4) by 5.1. Check #24280 and #24282 task text
against the sections.

Coordinate deadline, edges and payload fields with node-halves Writer 16767 so both plans
match.

## Progress (epoch 2, 2026-10-14)

- Step 0 DONE: M1 withdrawn, commit 32d97bc (2 lines in, 813 out), base validation clean.
  Plan now ends at V2 (line ~3313). No A-H edit made yet.
- 16767 message 5be734b3-3188-4c76-9a63-21ae4dc1e7c2 (unanswered yet):
  1. Deadline text to mirror at old L699 (1.1 node duties): "Preparation has a 300-second
     node-side deadline, from acceptance to the host's spawn commit (node-halves Decision
     19); passing it is a failure with reason `prepare_timeout`." No hub deadline change
     (a run in `accepted` is never fenced, 2.6.4).
  2. Edges (NH = node-halves): NEW NH 1.3 blocks HM 1.4 (both edit
     `src/gobby/servers/_app_routes.py::register_routes`); NH 2.1 blocks HM 1.2 (also orders
     NH 1.5); NH 3.1 blocks HM 1.2, 1.3, 1.4, 4.1; HM 1.2 blocks NH 2.7; NH 3.2 blocks HM
     2.5; HM 1.4 blocks NH 3.2, NH 3.3; HM 2.3 and 2.4 block NH 3.4; HM 2.6 blocks NH 3.6;
     NH 2.14 blocks HM 4.2, 4.6; every NH leaf (28) blocks HM 5.1. Their table columns:
     leaf, external ref, edge, reason.
  3. Payload: `resume_session_id` = provider-native thread id
     (`SpawnRequest.resume_session_id`, src/gobby/agents/spawn_models.py:40), set by NH 3.2
     to resume_metadata["provider_native_session_id"] when resume_existing_session, else
     null; Gobby session ref never in payload. `existing` workspace =
     {mode:"existing", path, branch_name, base_branch:null} from the resumed session's most
     recently updated active worktree/clone row on that machine (NH 3.2 step 7); no row:
     request isolation maps as a new spawn. Node report names it with parent root's mode.
     Rewrite 2.5.7: "sends the session's provider thread id as `resume_session_id` and, when
     the session has an active workspace row on that node, that workspace as `existing`";
     same no-row fallback in 2.5 step-4 bullet (old L1790-1792).
  Answer their ids question: #24281 (D2) and #24283 (D4) are deferral holders 15977 retires
  at expansion; 1.2 and 5.1 get fresh leaves. P4 (4.1-4.8) has no tasks yet. Existing
  leaves: #24268 1.1, #24269 1.3, #24270 1.4, #24273 2.3, #24274 2.4, #24275 2.5, #24276
  2.6, #24278 3.2, #24279 3.3 (2.1/2.2/3.1 are #24271/#24272/#24277, verify).
- Task facts: #24151 = epic "S2.11 hub half: hook ingress and rule engine in Rust" (Lane 5,
  blocked by #21562 Sessions, #21567 Workflows, #21566 MCP multiplexer). #21549 = CLOSED
  2026-09-02 bug "Machine-scope terminal list, get, and attach_locator" (landed). #20202 =
  planning/architecture_doc S4.3 "Plan hub-wide terminal roster, attach routing, and
  capability tokens for remote gclient attach": 4 items (hub-wide terminal roster from hub
  data, terminal.machine_id to daemon endpoint, per-terminal attach tokens, hub-node
  relay with backpressure); also carries gclient-workspace-sidebar 3.2.4 deferral. #24149 =
  epic "S2.7 hub half ... in Rust" (Lane 5, blocked by #21560 Tasks family, #21555 API
  keys); it replaces this plan's Python. #24280 D1 text = remote workspace lifecycle
  (merge design, listing, removal) -> 4.1-4.5. #24282 D3 = machine-scoped project view
  (node checkout git state) -> 4.6-4.8 + 3.2.4.
- Grok/agy attention: NH 2.13 detectors, NH 2.14 pane monitor puts a `pane` block (attention,
  injections, composer, idle, 15-line tail) on each report agent entry, NH 3.3
  (`src/gobby/hub/remote_pane.py`) applies it on the hub via `AgentAttentionTracker.apply`
  from HM 1.4's report route. Applies to all five providers. Decision 7 must change.
- Prompt file: NH 2.6 launch step 3 writes `<gobby_home>/node/prompts/<run_id>.md` (0600);
  NH has no removal. Local Python (`spawners/prompt_manager.py::create_prompt_file`):
  tempdir/gobby-prompts 0700, file 0600 O_CREAT, removed on failed spawn (spawn.py) and at
  daemon exit. Ask 16767 to add dir 0700 + removal after delivery; HM cites NH.
- Identity: `FrontDoorIdentity(user_id, machine_id, key_id)`; `AuthMiddleware.dispatch`
  (src/gobby/servers/middleware/auth.py) runs `auth_service.authenticate` ->
  `AuthDecision(user_id, machine_id, key_id)` then call_next. Precedent: storage/workspaces.py
  `resolve_node` treats the local machine's `owner_user_id` as the operator; api_keys.py
  issues keys only for the principal's own machine. Attention WS broadcasts are hub-wide
  (`broadcast_agent_event` -> `broadcast`); WS auth_callback = `verify_ws_identity` (user_id).

## Design draft (not yet applied)

- D ownership: new section 1.5 "Callers act only on their own machines": new
  `src/gobby/hub/ownership.py` (contextvar bound in `AuthMiddleware.dispatch` from
  decision.user_id; `caller_user_id(db)` = bound user, else the hub machine's
  owner_user_id; `require_machine_owned(db, machine_id)` -> NodeCommandError
  machine_not_found / machine_not_owned, stage none, for any machine other than the hub's;
  `owned_machine_ids`). Guards: 2.3 remote_terminal.write, 2.4 remote_cancel.cancel, 3.1
  roster (hub rows + caller-owned machines), 3.3 respond remote branch (before claim), 4.3
  wrapper remote branch + list machine_id, 4.4 routes, 4.7 machine_id routes. Internal
  loops (2.6 fence, 4.5 expiry) call the node client / remove_remote directly. Hub's own
  rows keep today's auth (tests don't register machines). Open: WS attention broadcasts
  leak other users' entries; decide filter or state.
- D hook (2.2): no-identity hook binds only hub-machine rows; envelope machine must be empty
  or the hub's, else 403 hook_machine_mismatch. Tests putting pre-created rows on
  "unknown-machine" with local events must move rows to the local machine: ENUMERATE (B).
- F: 5.1 heading `[category: code]`; 2.1/2.2 likely feature.
- A: 5.1 per-provider write/attention/cancel/merge; attention via hooks (claude/codex/droid?)
  and pane reports (grok/agy, NH 2.14/3.3). #24151/#24149: separate Lane 5 Rust-port
  epics, concrete reason. #21549 closed (landed). #20202: S4.3 own stage item; decide.
- G: gclient spawn `machine` field leaf (gclient spawn dialog/REST) - research needed.

## Epoch 3 findings (2026-10-14, after compaction)

- Replied to 16767 (msg 504fcb0a): agreed 300 s, all edges incl NH 1.3 -> HM 1.4, payload
  wording; HM 4.1 writes deadline + prompt-file rule into node-commands.md "Node duties";
  NH 3.3 owns `redact_report_panes` (HM 1.4 gets one line). Asked them: NH 2.6 prompts/
  dir 0700 + remove file after delivery/exit/pre-launch failure; confirm Decision 32 (2.7
  enforce, 2.5 chokepoint) as the non-srt refusal cite. node-halves now at 1c7d256584 with
  its own "Cross-plan edges set at expansion" table (| Leaf | External work | Edge | Reason |)
  plus "Relations that set no edge". NH 3.1 (contract v2) carries resume_session_id and
  the `existing` workspace mode.
- Task ids verified: 3.1 = #24277 (feature/code). 2.1 = #24271, 2.2 = #24272 per NH text.
- Advisor (epoch 3): #24151 reason = Decision 18's three-use limit (15977 ruling 2026-10-12)
  + #24151 is the assigned owner with blockers #21562/#21567/#21566; #24149 = Josh decision B
  split (memory 217e0dc7 item 4); #20202 = Josh Q0 a / decision C (routing only; S4.3 is its
  own stage, four-item design). Contract doc edits land in 4.1. Mirror NH table exactly.
- ROADMAP decision 17: "Tenancy: the hub is the tenant ... Deferred: ... teams on one hub and
  per-user privacy inside a hub" = S4.4 #17769 (labeled later). => roster/WS visibility
  per user is S4.4's; action checks (owner_user_id) are in scope (spawn precedent 1.3).
- AuthMiddleware.dispatch does not put the decision on request.state; authenticate() sets
  user_id only from a verified front-door identity (else None). Every WS connection has a
  user_id (auth.py rejects otherwise). Attention WS events = agent_event "attention_changed"
  with entry_id/run_id/session_id, no machine. `Machine.owner_user_id: str` (required).
- D design chosen: contextvar for caller user bound in AuthMiddleware.dispatch from
  decision.user_id; unbound = hub-internal/in-process caller (no check); bound -> target
  machine row must exist (machine_not_found) and be owned by the user (machine_not_owned),
  for every machine incl. the hub's. Guard in kill_agent (2.4) and WriteCoordinator._dispatch
  (2.3) for all runs/terminals, 3.3 respond before injection (403), 4.3/4.4/4.7 routes.
- Hook rule (2.2): symmetric predicate session_binds_event(row, event) =
  is_local_machine_owner(row.machine_id, event.machine_id) (no hub special case); ingress:
  no verified identity -> envelope machine must equal the hub's, else 403. Pins no longer
  needed. Static AST audit (scratchpad audit_rows*.py) enumerated 31 tests whose matched
  rows lack the event machine:
  test_session_start_handlers.py: TestSessionStartPreCreatedSession.{found_and_updated,
  backfills_terminal_context, renames_empty_title_with_cwd, with_parent, with_agent_run_id,
  agent_run_start_error, message_processor_error, coordinator_error,
  test_live_pre_created_session_omits_startup_persona,
  test_first_pre_created_pickup_defers_startup_persona_to_first_prompt,
  test_context_loss_sources_reset_first_prompt_agent_context}; TestSessionStartNewSession.{
  test_handoff_db_error_still_returns_session_banner, test_new_session_start_renames_captured_tmux_pane,
  test_compact_terminal_identity_conflict_blocks_before_registration,
  test_resume_of_a_known_session_is_never_promoted_to_clear};
  test_session_lookup_metadata.py: 10 module tests (valid_platform..., terminal_context_backfill...,
  revival_judges..., root_cwd_platform..., root_cwd_terminal_context..., user_prompt_submit_weak...,
  grok_tool_hook_from_another_process..., hand_started_grok_parent..., task_context_uses_stage...,
  task_context_preserves_explicit...);
  test_session_events_coverage.py: TestSessionStartAndHelpers.test_handle_session_start_pre_created,
  TestSessionMoreCoverage.{test_handle_session_start_gobby_session_id_terminal, ..._codex_terminal};
  test_hook_manager.py::TestHookCheckoutIngress.test_non_cwd_resolutions_do_not_register_rebind_or_refresh;
  test_hooks_context.py::test_session_start_context_injection;
  test_handler_execution.py::TestReturnValues.test_session_banner_in_system_message_not_context.
  test_session_coordinator.py and test_transcript_path_derivation.py: rows/events share fixed
  ids, no change needed under the symmetric rule (move to Consumers unchanged).
- G (gclient spawn surface), partial: live gclient's `DaemonApi::spawn` (live_connect.rs:242)
  sends WS `terminal_create` (a shell), not an agent spawn. `POST /api/agents/spawn` appears
  only in the scripted/fake daemon (`crates/gclient/src/daemon/mod.rs:658 spawn_agent`, and
  :808 `spawn(SpawnRequest)` maps to it) and `App::spawn_agent(body)` (app/mod.rs:710), used by
  tests/client_loop.rs:3539-3627. NEXT: confirm which type mod.rs:658/808 belong to (fake vs
  live), whether any live UI path calls App::spawn_agent, and the REST body model
  (src/gobby/servers/routes/agents.py spawn request: does it take `machine`?). The G leaf =
  gclient agent spawn carries `machine` (and the REST route accepts it, if 2.5 doesn't).
- Restart hold began ~07:5x (BEFORE NOTICE from 15977); pause validation/messages until
  DAEMON BACK.

## Epoch 4 progress (2026-10-14, applying A-H; DAEMON BACK ~08:05Z, hold lifted)

APPLIED to hub-mode.md (uncommitted):
- Overview: #24149 text points at "Relations that set no edge".
- Decision 7 rewritten: attention via hooks (claude/codex PermissionRequest, droid
  Notification, forwarded by #24150) and node pane reports (NH 2.13/2.14/3.3,
  AgentAttentionTracker.apply from 1.4's route); grok/agy no hook dir (NH 2.3.3).
- Decision 9: #21549 landed; gclient terminals.rs::terminal_rows follows machine_admits.
- Decision 16: symmetric binding; unverified hook must name hub machine.
- Decision 22: 5.1 turns link_operator_srt's pytest.skip into pytest.fail.
- Constraints: cross-plan bullet -> pointer; NEW supersession bullet (H: #24280 by
  4.1-4.5, #24281 by 1.2, #24282 by 4.6-4.8 + 3.2.4, #24283 by 5.1); hooks bullet symmetric.
- NEW framing section "## Cross-plan edges set at expansion" (table mirrors NH working
  copy: NH 2.15 is now last node agent leaf; NH leaves 1.1-1.6, 2.1-2.15, 3.1-3.9) incl.
  rows 2.2 blocks #24151, 1.2/2.3/2.4/2.5/3.1/3.3 block #24149; "Relations that set no
  edge" with reasons for #24151, #24149, #20202 (#19600 closed 2026-10-08; scope Q0 a +
  open attach-token security design), #21549, per-user visibility (S4.4 #17769), NH 2.9,
  NH 3.5/3.9, #24268.
- 1.4: one paragraph before step 1 about NH 3.3 redact_report_panes + test.
- NEW 1.5 "Callers act only on machines they own" (depends 1.3): src/gobby/hub/caller_scope.py
  (CallerScope(user_id, db), bind_caller/reset_caller, require_machine_owned -> NodeCommandError
  machine_not_found/machine_not_owned stage none; unbound skips; db None fails closed);
  AuthMiddleware.dispatch binds decision.user_id with require_hub_database(server.services.database);
  WebSocketServer._handle_connection binds websocket.user_id with terminal_services.manager.db.
  Consumers unchanged + 4 acceptance items written.
- 2.2: Targets now list the 31 exact tests (pins removed); Granularity says ten items.

REMAINING (in order):
1. 2.2 body: Research "Tests" bullets -> static audit (2026-10-14) of every test feeding a
   session lookup at a binding site; Implementation: ingress no-identity rule (403
   hook_machine_mismatch unless envelope machine == get_machine_id()), predicate =
   is_local_machine_owner(row, event) only, `resolve` applies to every event, test fix rule
   = give the matched row the event's machine (no pins); Consumers unchanged: drop
   test_session_lookup_metadata.py + test_hook_manager.py (now Targets), add
   test_session_coordinator.py + test_transcript_path_derivation.py (rows/events share
   fixed ids), reword pin reasons; Acceptance: 2.2.2 -> test_unverified_hook_must_name_hub_machine,
   2.2.3 symmetric predicate, NEW 2.2.10 test_local_hook_skips_node_rows (hub-machine start
   naming node row by terminal_context.gobby_session_id leaves it, registers new hub session).
2. 2.3 (+depends 1.5): _dispatch calls require_machine_owned(terminal.machine_id) before
   branching -> NodeTerminalWriteError(code, stage none); 2.3.6 test_write_needs_owned_machine.
3. 2.4 (+depends 1.5): kill_agent check (run.machine_id or require_machine_id()) -> error dict;
   2.4.6 test_cancel_needs_owned_machine.
4. 2.5: cite NH Decision 32 (node refuses non-srt, invalid_payload stage none, NH 2.7; chokepoint
   NH 2.5) where hub trusts node SRT; 2.5.7 wording "sends the session's provider thread id as
   `resume_session_id` and, when the session has an active workspace row on that node, that
   workspace as `existing`"; step-4 bullet same no-row fallback.
5. 3.3 (+depends 1.5): respond checks before injection -> 403 machine_not_owned; replace "skips
   capture and detector because hook state is authoritative (Q6)" with hooks or pane reports.
6. 1.1: deadline sentence (300 s, NH Decision 19, prepare_timeout) at "#24148 sets"; prompt file
   0600 in 0700 prompts/, removed once prepare_launch returns (NH 2.6 steps 3-4, 2.6.10; run-root
   copy removed by NH 2.15); 1.1.1/1.1.5 name docs/contracts/node-commands.md + heading; narrative
   "delivered by #24268".
7. 4.1: writes deadline + prompt-file rule into node-commands.md "Node duties"; 4.1.1/4.1.2 own
   tests; 4.1.5 exact section.
8. 4.3/4.4 require_machine_owned(row.machine_id) + items; 4.7 require_machine_owned(machine_id) +
   item; 4.8.5 machine_id test.
9. 5.1: heading [category: code], depends add 1.5; all five providers x write, attention (hooks for
   claude/codex/droid; pane report for grok/agy: fake prints a pane-detection corpus prompt),
   cancel, merge; SRT fail; items per capability.
10. Story 1 (Architecture ~L490) + Decision 13: no client posts an agent spawn today (live
    gclient Daemon::spawn = shell via WS terminal_create; App::spawn_agent only for ScriptedDaemon;
    web has no launch dialog; AgentSpawnRequest needs task_id) -> actor = MCP spawn_agent or REST
    with machine; G reason for 15977 (a gclient agent launcher is new UI; Q0 a scope).
11. Sweep: "2.14" (NH last leaf now 2.15), "binds any row", "skips when", "hook-driven attention
    only", "Q6", "pin". V1 entry. Validate base, commit, send 16491 (SHA; routing: 5.1 code/task,
    1.1 by #24268, 2.1/2.2 feature, new 1.5), 16011 (D), 16767 (SHA), 15977 (G + #20202 reasons).

16767 msg 263205ff (node-halves committed 19d3e8ba41) CHANGES the remaining steps:
- Deadline: NH 3.1 writes "300 seconds" into node-commands.md node duties with a doc test.
  hub-mode 4.1 does NOT write it; 1.1 and 4.1 cite node-halves 3.1 and Decision 19.
- Prompt file wording: "removed once `prepare_launch` has copied it" (NH 2.6, 2.6.10; 0700
  prompts/; run-root copy removed by NH 2.15 reaper; start-time sweep in 2.15). So 4.1 writes
  no prompt-file rule either; cite NH 2.6.
- Decision 32 final: NH 2.5 = chokepoint check (prepare_launch always runs srt steps), NH 2.7
  = dispatch refusal invalid_payload before anything is recorded.
- Edges confirmed as already in the table (1.2 rows "no task yet, #24281 holds it"; 5.1 row
  every leaf 1.1-1.6, 2.1-2.15, 3.1-3.9; 4.2/4.6 on NH 2.15, whose chain is 2.15 <- 3.9 <- 3.3
  <- HM 1.4). Add that chain note to the 4.2/4.6 rows. Reaping split (HM 2.1.3 vs NH 2.15,
  NH Decision 33) is a no-edge relation; add it to hub-mode's list. NH 3.9 shares nodes.py and
  test_node_routes.py with HM 1.4, ordered through 3.3 (already noted).
- Adversary 16791 re-stamps node-halves after Security 16011 clears B1/B2.
Reply to 16767 with hub-mode's SHA after the commit.

## Epoch 5 progress (2026-10-14, after compaction)

DONE (uncommitted; base validation was clean after 2.2, not rerun since):
- Step 1 (2.2): strict ingress kept (advisor: ghook always stamps machine_id or
  machine_id_error, dispatch.rs inject_machine_identity; require_hook_machine_id calls a
  missing machine malformed). Check sits after hook_type/source/HookManager/supported
  source/response-capability checks, before claim_envelope_processing. Research: lookup
  audit = mocked lookups only (31 tests); second audit (scratchpad audit_route.py,
  audit_route_out.txt) of route tests; agy startup_claim_preflight already compares
  machine in _hint_mismatch (Consumer unchanged). Targets added: envelope helpers
  (_hook_envelope x3, _envelope x2, _post_claude_hook, _agy_pre_invocation_envelope,
  _agy_post_invocation_envelope), 7 inline test_hook_session_metadata tests, agy/droid
  inline dispatch tests, mcp_routes critical_metadata test, 3 agy machine-agy row tests,
  cleanup replay test, e2e _hook_payload + _protected_requests. Consumers unchanged
  reworded; 2.2.2/2.2.3 rewritten; 2.2.10 added; verification widened. Removed six
  DaemonConfig inventory lines from 2.5 (files now Targets) and reworded "244 consumers".
- Step 2/3: 2.3 (+1.5) _dispatch check + 2.3.6; 2.4 (+1.5) kill_agent check + 2.4.6.
- Step 4: 2.5 Decision 32 cite in sandbox bullet; step-4 resume bullet; 2.5.7 wording.
- Step 5: 3.3 (+1.5) AttentionPane machine_id optional (None = hub), respond check
  before claim -> 403; Q6 sentence replaced; 3.3.6.
- Step 6: 1.1 "Delivered by #24268" para; prompt file NH 2.6 wording; deadline NH 3.1
  300 s; 1.1.1/1.1.5 exact sections.
- Step 7: 4.1 research cites NH 3.1 deadline; "v3 commands" section; 4.1.1/4.1.2 new tests
  in node_command_parity files; 4.1.5 exact sections.
- Step 8: 4.3 (+1.5) ownership bullet + 4.3.7; 4.4 (+1.5) + 4.4.4; 4.7 (+1.5) + 4.7.5;
  4.8 rest/tests.rs::* scope-reason + 4.8.5 machine_id_query_only_when_given.

- Step 9 (5.1) DONE: [category: code], +1.5, Granularity (8 items), require_srt helper,
  per-provider steps, 5.1.3-5.1.8 rewritten. Base validation clean after it.
REMAINING: step 10
(Story 1 + Decision 13), step 11 (sweep, 4.2/4.6 chain note, reaping-split relation, V1
entry, validate, commit, messages).

## Epoch 6 (2026-10-14): steps 10-11 DONE
- Commits 79868ca6c1 (A-H) and 5f618fd3d5 (2.5 spawn ownership: require_machine_owned on
  routed machine, 2.5.16, 1.5 research corrected; 1.3's check compared machines and
  skipped local targets). Base validation clean.
- Story 1 starts from an agent session (MCP spawn_agent); no client UI posts an agent
  spawn (gclient live Daemon::spawn = terminal_create; CLI posts to MCP tool route
  src/gobby/cli/agents.py:251). New no-edge relations: gclient/web launcher; NH 2.15 vs
  HM 2.1.3 reaping split. 4.2/4.6 chain note. V2 adds test_caller_scope_binding.py,
  test_session_binding.py, e2e test_http_auth_matrix.
- Sent SHA to 16767, 16011 (D review incl. roster-by-reason), 15977 (G, #20202, roster
  reasons; 15977 accepted all three), 16491 (review; hold M1 until 16011 clears D;
  routing asks).
- Waiting: 16011 D clear, 16767 table check, 16491 review/re-stamp. Then commit rendered
  bytes unchanged and send 15977 SHA, rendered hash, leaf count.
