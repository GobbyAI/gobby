# Hub mode (#21575) working context — plan writer gobby#16490

Live working notes for drafting `.gobby/plans/hub-mode.md` (Plan ID `hub-mode`,
root #21575). Refresh in place; not plan authority.

## Decisions (Josh via Orchestrator gobby#15977, 2026-10-12)

- A-D approved as presented. C: Python hub dispatch/roster/attention routing to
  remote nodes is #21575 scope; #24149 (S2.7 hub half) redoes it in Rust later.
- Q0 a: #21575 = Python hub-side remote-node routing.
- Q1 b: this plan owns (1) a versioned hub-to-node command envelope as a committed
  contract plus golden corpus (request id, command, payload, typed errors);
  (2) a loopback-only gdaemon hub route that sends one command over the machine's
  channel and returns the correlated reply; (3) Python callers for spawn (with
  worktree/clone prep), cancel, terminal write. Depends on #24090 node channel;
  end-to-end only after #24148 node handlers. Tests use a fake node. gdaemon route
  and envelope are durable (reused by #24149); Python callers labeled transitional.
- Q2 a: eligibility = `project_checkouts` row for (project, machine) + live state
  from the gdaemon registry read through the Q1 route.
- Q3 a: default = requesting session's machine; optional explicit machine (id or
  node ref) validated by Q2; never fall back to hub or another machine (#17436);
  hub-originated dispatch (build workers, cron, pipelines) uses the origin machine
  recorded when the run started.
- Q4 DECIDED (Josh via gobby#15977, 2026-10-12; research request dropped). Verbatim:
  "Wouldn't the machine just show that there's an attention icon? We display all
  machines, we just don't show all agents and all projects on all machines at once.
  Switching machines switches the projects and agents and terminals view." Spec: the
  machines list shows all machines, each with an attention indicator; agents,
  projects and terminals views default to the current machine; switching machines
  switches those views. Roster API supplies per-machine attention summaries for
  every machine plus the full roster for the selected machine. All Q0-Q6 answered:
  draft, validate, pass to Adversary 16491, report stamped plan to 15977.
- Q5 a: attention respond on another machine's entry routes a terminal-write
  command to the owning node; typed `node_offline` when disconnected.
- Q6 a: hook-driven attention only for story B; node pane detection and its
  reporting route are planned with #24148.
- ROADMAP already updated by #24152 (closed, 5bc34397c7). S4.2 edges unchanged; the
  plan adds S4.2 <- #24090 edge (ROADMAP edit belongs in this plan if needed).

## Already landed (do not re-plan)

#23111 loop/phase gate (`HUB_ONLY_PERIODIC_TASKS`, `HUB_ONLY_STARTUP_PHASES`);
run-modes 2.1 `/api/health` mode; #23112 node-local lifecycle; #24075 node refuses
the Python daemon (a node runs only gdaemon). Legacy wiki retired.

## Code facts (Python hub)

- Roster: `src/gobby/storage/attention.py::AttentionStateManager.load_roster_rows`
  (line 287) filters runs by `run.machine_id = %s`, sessions only by status.
  Called from `src/gobby/servers/routes/attention.py` `roster()` (line 228) with
  `require_machine_id()`; also line 476.
- `attention_states` has no machine_id (`AttentionState`, attention.py:73). Owner
  machine derivable from `agent_runs.machine_id` / `sessions.machine_id`.
- Respond: `create_attention_router` `respond` (attention.py routes line 350) ->
  `inject_answer` (line 189) -> `write_coordinator.write(WriteRequest(terminal_id=
  pane.target, action_key=f"attention-respond:{attention_id}", origin="attention",
  kind, payload, submit))`; pane from `_resolve_attention_pane` (line 665) via local
  `terminal_manager` + `terminal_runtime_registry`.
- Attention producers: `agents/attention_tracker.py`, `agents/interactive_attention_monitor.py`
  (pane-based, machine-local), `agents/terminal_cleanup.py`,
  `agents/watchdog/interactive_capacity.py`, `sessions/turn_lifecycle.py` (hooks),
  `hooks/terminal_handoff_delivery.py`.
- Dispatch locality: `src/gobby/storage/workspace_machine_scope.py::require_local_machine_id`
  raises `MachineOwnershipMismatchError` for a foreign machine; callers
  `dispatch/spawn.py:151`, `dispatch/spawn_artifacts.py:553`,
  `dispatch/workspace_merge.py:676`. `project_checkouts.require_root(db, project_id,
  machine_id)` (`src/gobby/storage/project_checkouts.py:361`). `servers/routes/agent_spawn.py`
  uses `require_machine_id()` at 253 and 362; `build/control_runtime.py:163`.
- Machines: `src/gobby/storage/machines.py` (`Machine`, `LocalMachineManager`,
  `parse_node_ref`); columns incl. `last_heartbeat_at`, `node_version`.

## Code facts (gdaemon)

- No `crates/gdaemon/src/nodes/` yet: #24090 (node channel) open, #24088-#24092 open.
  Spec: `.gobby/plans/gdaemon-node-channel.md` Decision 9 (line ~182: hello/ack/ping
  only; "S2.7 and S2.8 add commands"), section 1.4 (line 1039: `Registry`,
  `Slot`, `ChannelHandle {connection_id, close, live}`, `register`, `remove`,
  `persist`, `mark_live`, `is_connected`, `connected_ids`; handler select loop).
- `crates/gdaemon/src/front_door/mod.rs::FrontDoor::handle` (68): runtime gates,
  challenge, `auth.authenticate`, WS splice or `RouteTable::dispatch`.
  `FrontDoorState {runtime, target, backend_state, client, auth}`.
- `front_door/auth.rs::AuthState::authenticate` strips `IDENTITY_HEADERS`
  (x-gobby-user-id, -machine-id, -key-id, -front-door) and sets them on key auth;
  the front-door secret value is `self.secret`. Python verifies it in
  `src/gobby/servers/auth_service.py` (`verified_front_door_identity`), secret from
  env `GOBBY_FRONT_DOOR_SECRET` (`runner_init/servers.py:540`).
- `front_door/routes.rs`: `RouteFamily` trait, `FAMILIES = [HealthFamily,
  TerminalFamily]`, per-family Proxy|Native|Compare.
- Proposed Python-to-gdaemon auth: Python presents the front-door secret on a
  loopback-only internal route (least mechanism; secret known only to gdaemon and
  its backend).

## Contract constraints

- External prerequisites must be `kind: deferred` sections, never prose blockers or
  manifest edges. Node-channel precedent: Orchestrator ruled 2026-10-05 to keep
  sections active and delay expansion until the prerequisite closed.
- Every Target with exact symbols needs consumer sweep; Research context per
  section must stand alone; acceptance test artifacts as `path::test_symbol`.

## Draft status (2026-10-12, gobby#16490)

- Draft written: `.gobby/plans/hub-mode.md` (uncommitted). Deliverables 1.1-1.3,
  2.1-2.5, 3.1-3.3, D1. `uv run gobby plans validate .gobby/plans/hub-mode.md -p .`
  (run bare, no pipes; rule no-wrapped-validation-command) PASSES with only
  consumer-coverage warnings. Next: add a "Consumers unchanged:" block with
  `- path — no-edit-reason: ...` lines per section (format as in
  completed/gdaemon-run-modes.md 2.1) for every listed consumer, or add them to
  Targets where they change (test files that call changed signatures:
  tests/storage/test_agents_terminal_migration.py for list_terminal_with_tmux;
  tests/servers/test_attention_roster.py + tests/agents/test_attention_metadata.py
  for load_roster_rows signature/AttentionRosterRow new field (give it a None
  default); tests/agents/test_interactive_attention_monitor.py). Re-run validation.
- Then: commit plan (code-review skill + `ocr delegate rule --format json <path>`
  self-review in its own call, separate git add/commit), send SHA to Adversary
  gobby#16491; send gobby#15977 the writer interpretations to object to:
  Decision 10 (Q4 = hub-wide roster + machine_id, no per-machine summary API),
  11 (builds/cron/pipelines hub-hosted, so no remote build), 13 (separate remote
  spawn flow; resume/placement refused), 15 + D1 (remote workspaces not in hub
  tables; lifecycle deferred), 16 (hook machine binding), 17 (#24090 precedent).
- Research done (do not redo): gclient machine filter already exists
  (machines.rs::machine_rows, sidebar_state.rs machine_filter, views/mod.rs
  read_local_machine_id); spawn seam (spawn_agent_impl 988 lines, 5 callers;
  prepare_terminal_spawn has machine_id/credential_manager/prompt params);
  kill_agent result dict; WriteCoordinator._dispatch seam; unscoped loops
  (list_stale_pending caller, list_terminal_with_tmux, _list_interactive_sessions,
  post_terminal_cleanup phases); worktrees table lacks machine_id; hooks take
  machine_id from input_data; gdaemon state.rs passes GOBBY_FRONT_DOOR_SECRET;
  FrontDoor::handle has peer; serve.rs::plaintext_allowed.

## Coverage pass (2026-10-12, post-compaction)

- Validation CLEAN (no warnings). Changes: 2.1 filter moved into
  `TerminalManager.list_stale_pending` SQL plus split of reap queries into new
  `src/gobby/storage/terminal_reap_queries.py` (validator size rule; split
  paragraph must name both paths); 2.3 targets `tests/terminals/fakes.py::*`
  (make_memory_terminal random machine_id would read as remote); 2.5 origin rule
  = non_empty_string(parent.machine_id), absent = local, and new refusal
  `remote_parent_local_target_unsupported`; 2.5 verify path fixed to
  `tests/servers/routes/test_agent_spawn_routes.py`; 3.1 targets three
  test_attention_roster tests. Consumers-unchanged lists added to every section.
- #24088 landed (df008c8c76) with no `nodes/` module; Constraints updated.
- Orchestrator 15977: edit-ledger refusals are accepted noise; do not claim a
  task; commit with [gobby-#21575]; daemon-dependent steps wait for DAEMON BACK.

## Adversary round 1 (16491 msg 8259edc0, vs 8a1d5708ad): 5 blocking, all ACCEPT

Orchestrator 15977: no objection to D10-D17/D12; adversary stamps then 15977 reviews.
Draft designs (not yet in plan; advisor check pending):

- HM-F1 (2.3, 1.2) lost reply after write. gdaemon: `node_offline` ONLY when no
  live channel at enqueue (never written). `node_timeout`/`channel_closed` =
  possibly sent. §1.2 adds: pending entry removed on reply, timeout, channel
  close, and caller-future drop (drop guard); late reply with no pending entry is
  dropped + logged. Python remote_terminal.write: node_offline ->
  NodeTerminalUnavailable(stage none, code) -> `_write_locked` clears latch;
  node_timeout/channel_closed -> return `IndeterminateWrite(detail=code)` -> latch
  retained, `_idempotent_replay` answers a same-action_key retry with
  IndeterminateWrite (no resend). Node refusals unsupported_version/
  unknown_command/invalid_payload/not_found -> stage none; command_failed carries
  node stage none|partial. Test: fake node writes then drops reply.
  Code: write_coordinator.py `_write_locked` (latch persist/clear), `_idempotent_replay`.
- HM-F3 (3.3) remote respond. Remote branch skips capture+detector; identity =
  stored episode via `_require_current_identity` (hook state authoritative, Q6;
  node does not check freshness). Use inject-then-clear order (existing
  `claimed_before_injection=False` path) so stage none keeps the episode;
  partial -> retire without redetect. Per-entry lock + latch on
  `attention-respond:{attention_id}` block duplicates while unknown.
  `AttentionInjectionError` (src/gobby/agents/tmux/text_injection.py) gains
  optional `code`; inject_answer passes NodeTerminalUnavailable.code; respond
  maps coded error to 503 {code, stage}. Tests: stale 409, offline 503 keeps
  entry, success clears, lost reply 502 + retired + retry blocked, concurrent.
- HM-F4 (2.5) origin. MCP origin = machine of `caller_session_id` from
  `enforce_spawn_caller` (_factory.py ~409-413), parent = reporting only. HTTP
  origin = verified front-door identity machine else this machine; parent not
  used for routing. Remote branch runs before `_parent_session_project_context`
  (remote project = session project_id + Q2 checkout row of target). Local
  branch: `_parent_session_project_context` resolves `require_root` with the
  routed (this) machine instead of rejecting a foreign parent -> DROP
  `remote_parent_local_target_unsupported` + D13 refusal sentence. Check
  tests/mcp_proxy/tools/spawn_agent/test_factory.py 611-680 for foreign-parent
  expectations (target them). Tests: node caller+hub parent -> node; hub
  caller+node parent -> local; explicit target.
- HM-F2 + HM-F5 (2.5 + NEW 2.6). Outcomes: definite failure (node_offline;
  node refusal stage none) -> DB-only settlement: release lease,
  `terminalize_cancelled_agent_run` + `cleanup_agent_runtime_state` (as
  `_failure_cleanup.py::_roll_back_run`), terminal row settled; NEVER
  `cleanup_failed_spawn` (reaches local PID/terminal kill:
  `_terminate_spawn_process`, `_kill_spawn_terminal`). workspace.remove only
  after definite non-effect. In-doubt (timeout/channel_closed, stage partial,
  caller cancel after send, hub crash) -> keep run pending, lease, workspace.
  NEW 2.6 remote run reconciler (hub-only periodic task; add to
  HUB_ONLY_PERIODIC_TASKS): new envelope command `node.inventory` -> node lists
  live agents {agent_run_id, terminal_id, pid, locator, locator_key,
  host_epoch}. Per connected machine with remote rows pending/live/orphaned:
  present+pending -> `TerminalManager.promote_to_live` + run running; absent +
  pending past spawn in-doubt deadline -> send `agent.cancel` (fence) then on
  stopped/not_found settle definite failure + workspace.remove; absent+live ->
  `mark_exited` + existing terminal-agent completion path (FIND IT: how local
  dead terminal agents settle runs, lifecycle_monitor/AgentHealthMonitor).
  Disconnected machine: rows untouched. agent.spawn reply carries identity
  (locator, locator_key, host_epoch) so the normal path promotes at once.
  #24148 node obligations: inventory, identity in spawn reply, agent.cancel of
  unknown run id tombstones it so a late agent.spawn refuses `cancelled`.
  Terminal transitions available: create_pending, promote_to_live (pending->
  live CAS, needs locator+locator_key), mark_exited (live|orphaned->exited),
  mark_exited_attempt, mark_orphaned (live->orphaned).

## Adversary round 1b (16491 msg 875188a6): 2 more blocking, ACCEPT

- HM-F6 (2.5) run machine: `prepare_terminal_spawn(machine_id=target)` sets only
  the child session machine; `_prepare_run_for_session` (spawn.py ~603) calls
  `agent_run_mgr.create(...)` with no machine_id, and storage/agents/_lifecycle.py
  create (~273) hardcodes `machine_id = get_machine_id()`. Fix: thread validated
  target machine through `prepare_terminal_spawn` -> `_prepare_run_for_session`
  -> `_AgentRunLifecycleMixin.create` (verify exact class name), local default
  unchanged; exact Targets + consumer sweep; acceptance asserts session, run
  and terminal rows all carry the remote machine before any node launch.
- HM-F7 (3.1) `src/gobby/servers/routes/attention.py::_resolve_prompt_detector`
  (~475) also calls `load_roster_rows(require_machine_id(), ...)`: add to 3.1
  Targets, drop the arg, pin a local respond regression with real detector
  resolution. Also 2.1: add exact Targets for `TerminalManager` moved methods
  (list_orphaned_by_epoch, list_live_by_epoch, list_live_by_machine,
  list_reconcilable_by_machine) and write their Consumers unchanged (call
  syntax unchanged through inheritance), or shrink the split.
- Adversary notes: get_live_for_session accepts pending+live rows (terminals.py
  639-644); F5 defect is missing promotion/exit/recovery ownership. RosterEntry
  Rust constructors use Default::default(), no breakage.

## Adversary round 1c (16491 msg 8973e947): HM-F8 blocking, ACCEPT

- HM-F8 (3.2) project attribution: sidebar_model.rs ~297-300 project_id =
  session.or(run).or(focused_project); agents.rs ~128 in_scope by project. Fix:
  carry subject project_id in AttentionRosterRow / roster entry / RosterEntry,
  prefer it in build_agents before fetched-row/focused fallback. Test: remote
  agent in project B absent from both 50-row windows while A focused stays in B.
  Ask 15977 whether machine-list completeness / project-pane filtering is owned
  elsewhere (Decision 10 is not product approval).
- F6 symbol confirmed: `_AgentRunLifecycleMixin.create`.
- 2.6 caveat: bound inventory absence to a node incarnation / complete snapshot;
  cancel must fence a later delayed spawn (absence alone is not proof a queued
  command cannot still execute).
- Coordinator ruling (via 16491): D17 must use the coverage contract's typed
  deferral for unlanded #24090 Node channel on affected §1.2 items; no informal
  hold. Downstream remote-routing leaves must not claim runnable behavior
  without the prerequisite.

## Revision pass (2026-10-12, uncommitted edits in hub-mode.md)

Advisor consulted. Edits DONE in .gobby/plans/hub-mode.md (not yet validated or
committed; cutover hold began mid-pass):
- Overview, D3/D10/D13/D15/D17, Constraints (#24150 hook ledger; size facts).
- 1.1: stage rule (node none|partial; gdaemon node_offline none, timeout/closed
  null), `cancelled` code, run_id on workspace cmds, agent.spawn returns
  {pid, locator, locator_key, host_epoch}, node.inventory, node duties
  (per-run ordering, cancel tombstone, not_found proof, complete inventory),
  internal route contract (moved from 1.2).
- 1.2 deliverable REMOVED -> `## D2` typed deferral (#24090), D2.1-D2.5.
- 1.3: 404/refused -> node_routing_unavailable; fake-route note.
- 2.1: split shrunk to list_stale_pending only.
- 2.3: NodeTerminalWriteError; timeout/closed -> IndeterminateWrite; 2.3.5.
- 2.4: not_found counts as stopped; 2.4.4.
- 2.5: new targets (_parent_session_project_context, prepare_terminal_spawn,
  _prepare_run_for_session, _AgentRunLifecycleMixin.create); origin = caller
  session machine after enforce_spawn_caller; run created before node commands;
  definite (stage none) -> fail_pending + _roll_back_run + workspace.remove;
  in-doubt -> spawn_in_doubt; 2.5.1-2.5.8.
- NEW 2.6 RemoteRunReconciler inside AgentHealthMonitor (__init__ +
  check_unhealthy_agents), node.inventory, two-pass absence, cancel fence.
- 3.1: project_id + machine_id, _resolve_prompt_detector target, 3.1.3.
- 3.2: RosterEntry project_id/machine_id, 3.2.3, agents.rs consumer.
- 3.3: remote branch, AttentionPane.remote, AttentionInjectionError.code,
  _injection_http_error code -> 503; 3.3.1-3.3.5.
- D3 (Q4 needs-decision), D4 (live proof), V1 revision line, V2 updated.

## Next (post-compaction)
1. After DAEMON BACK: gcode grep plan for stale "1.2"/"NodeTerminalUnavailable"
   refs; verify prepare_terminal_spawn takes resume_metadata_json.
2. Validate bare: uv run gobby plans validate /Users/josh/Projects/gobby/.gobby/plans/hub-mode.md -p /Users/josh/Projects/gobby
   (fix consumer warnings, e.g. callers of _AgentRunLifecycleMixin.create).
3. Commit (code-review skill, ocr rule call, git add, git commit [gobby-#21575]).
4. Send 16491 per-finding dispositions HM-F1..F8 + SHA; tell 15977 the D2/D3/D4
   structure (Q4 D3 for Josh).

## Still to research (superseded; see Draft status)

- Spawn pipeline split (hub vs node): `agents/spawn.py`, `agents/launcher_session.py`,
  TerminalRuntime spawn seam, prompt files, worktree/clone creation, session
  pre-registration; what the `spawn` command payload must carry (structured argv,
  env map, cwd; never shell strings, per #17436).
- Cancel/kill path (`kill_agent`, agent runner cancel).
- Origin machine for build/cron/pipeline runs (is it recorded?).
- gclient machine display option (for Q4 answer).

## Josh-review revision (current, 2026-10-12 evening)

Status: ALL narrative edits below are DONE in .gobby/plans/hub-mode.md (uncommitted): Overview, Decisions 10 and 15, 1.1 (hub-client codes, typed env, 1.1.1/1.1.4/1.1.6), 2.1 (scoping bullets, 2.1.5), 2.5 (target remote_workspaces.py, granularity, research bullets, steps 2/3/6/7, remote_workspaces.py functions, 2.5.4/2.5.5/2.5.11), 2.6 (remove_remote_workspace), 3.2 (target SidebarState::default, research, impl, consumer chrome.rs, full-crate nextest, 3.2.1 trimmed, 3.2.4), D1 (needs-planning), D3 (single item D3.1, needs-planning), V1 line, V2 gobby-client command.
REMAINING: (1) delete the `## M1 Task Manifest` block = lines 1294 (blank) through EOF 1614 using Edit in chunks (Bash writes to the plan are blocked by require-task-before-edit; Edit works). (2) `gcode grep -n "remote_workspace\b|resume_metadata" .gobby/plans/hub-mode.md` must show no stale side-channel text (remote_workspaces.py / remove_remote_workspace OK). (3) Item-id sweep: every `- N.N.N - ` on its own line (#24200 gap). (4) `uv run gobby plans validate .gobby/plans/hub-mode.md -p .` base mode only (expansion must fail until restamp). (5) Load code-review skill, `ocr delegate rule --format json .gobby/plans/hub-mode.md` own call, `git add .gobby/plans/hub-mode.md`, `git commit -m "[gobby-#21575] docs: revise hub-mode plan for Josh's review and withdraw M1" -- .gobby/plans/hub-mode.md` (Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>). (6) Send SHA to 16491 for review; tell 15977 the readings to object to: D3.1 machine list dropped per Josh answer 1, projects-pane view stays deferred (needs-planning), D1 stays deferred (needs-planning), hub module instead of widening Local managers, row written before prepare. (7) Consensus later: dated V1 consensus entry, commit, SHA to 16491, commit its M1 bytes unchanged, send to 15977. Then #24215 (Jev plan amendment for LM Studio /v1/systemone + /v1/decisions; WebFetch lmstudio.ai is egress-blocked; mark unverified shapes).
Route (16491 + 15977): ONE commit = narrative revision + whole `## M1 Task Manifest` block (and its preceding blank line) removed + V1 line recording the operator revision (NOT consensus). Base validation only; expansion must fail until restamp. Send SHA to 16491; consensus entry later; then 16491 derives M1; commit bytes unchanged; send to 15977. Then claim #24215 (Jev plan / LM Studio) per 15977.

Findings (evidence):
- worktrees and clones have `machine_id uuid NOT NULL` (crates/gcore/assets/schema/baseline.sql); unique idx_worktrees_path (machine_id, worktree_path), idx_worktrees_branch (project_id, branch_name, machine_id), idx_clones_path (machine_id, clone_path).
- Every LocalWorktreeManager/LocalCloneManager method binds require_machine_id(); get raises machine_ownership_mismatch for a foreign row (workspace_machine_scope.get_owned_workspace_row). So hub loops already skip node rows: runner_maintenance/isolation.py cleanup_expired_isolation_loop (find_expired), _delete_missing_worktree_records / _delete_missing_clone_records (list_worktrees/list_clones; deletes records whose path is missing on disk!), reconcile_isolation_registry (require_local_machine_id), SessionCoordinator.release_session_worktrees (list_worktrees(agent_session_id)). reactivate_cited_worktrees (_stage_state_transitions.py) updates status by task_id unscoped: DB status only.
- has_path_on_other_machine (worktrees.py:275) used by hooks/_misc.py handle_worktree_remove and register_adopted: path collision would make hub skip local cleanup -> remote branch/path embed run id.
- Task artifact worktree_path readers run git on hub (build/claim_recovery.py, build/observability.py) -> remote flow sets no task artifacts.
- worktrees.py is 852 lines (>=850 production-size-growth needs split if targeted) -> do NOT target it.
- gclient SidebarState::default sets machine_filter Some(ALL_MACHINES) (sidebar_state.rs:82); callers: chrome.rs Chrome::new (line 378) + two tests in same file. Parity tests set filter explicitly.

Design decisions:
- Decision 15 rewrite: remote workspace = worktrees/clones row with node machine_id, written by new `src/gobby/hub/remote_workspaces.py` (record_remote_workspace before workspace.prepare; remote_workspace_for_session(machine, session) reading both tables via Worktree.from_row/Clone.from_row; remove_remote_workspace sends workspace.remove with source_root=require_root(db, project, machine) and deletes the row on {removed:true}, else keeps it). Local managers stay this-machine-only. Row written BEFORE prepare (deliberate deviation from local order) so an in-doubt prepare leaves a row for 2.6.
- 2.5: add target remote_workspaces.py; step 2 drop resume_metadata; step 3 record row then prepare; step 6: prepare definite failure deletes row only; spawn definite failure -> remove_remote_workspace; new 2.5.11 test_remote_workspace_is_a_machine_row; Granularity -> eleven items, six production files; launch group adds 2.5.11.
- 2.6: replace "workspace.remove for a recorded remote_workspace" with remove_remote_workspace for the run's child session.
- 2.1: research bullet + Already-scoped list; new 2.1.5 test_workspace_loops_skip_remote_rows (expired cleanup, missing-record sweep, session-end release leave another machine's worktree and clone rows untouched).
- D1 stays deferred: reason "needs-planning:" — existing paths act on this machine's rows and run git here; merging a node's commits has no v1 command/design; remote row keeps agent_session_id after session end (release is this-machine only).
- 3.2: target SidebarState::default -> machine_filter None; consumer unchanged chrome.rs; 3.2.1 drop "still defaults" clause; new 3.2.4 test new_window_defaults_to_current_machine; verification full `cargo nextest run -p gobby-client`; research bullet fix (default was ALL_MACHINES).
- D3: drop D3.1 (Josh: existing machine display); keep projects-pane view as D3.1 renumbered? -> keep single item, reason "needs-planning:".
- 1.1: 1.1.4 "fails validation in the Python models"; 1.1.6 Rust test spawn_env_allowlist; env typed as struct of 7 optional identity vars in both languages; Hub-client codes bullet after Error codes; 1.1.1 "including the hub-client codes 1.3 raises".

## Josh-review revision 2 (2026-10-12 night; stamped at 0cfa47a4b6; no plan edits yet)

Josh chose Request changes (via 15977, msg b2a55773 + 22f661fe). Verbatim: "Why
wouldn't the hub just send a command to the node to spawn_agent?" and "I'm still
wondering why the node can't access the hub api with an api key? Does it need
database writes?" Plus: "Have them make a mermaid diagram of the hub and node
architecture with some user story examples."
15977 requirements: (1) remote spawn = ONE hub-to-node command; node prepares its
own worktree/clone, spawns, cleans up its own workspace on failure; replaces 2.5's
workspace.prepare/agent.spawn/hub-driven workspace.remove and 2.6's cleanup
retries. (2) node reports results (spawn identity, workspace rows, exits,
inventory) to the hub over HTTP with its machine-bound key (ROADMAP decision 17);
no datastore access; hub records from reports. (3) hub records intent (pending
run/terminal) before sending; lost reply reconciles from the node's reported
inventory. Restraint on 2.5/2.6/envelope; recheck D1. Mermaid component diagram
(components, API-key HTTP paths, command channel, where data lives) + user stories
with sequence diagrams: spawn on another machine from gclient; answer an attention
entry for a node's agent; node offline mid-run. Then 16491 stamp, then 15977.

Advisor-approved design (record "ack + report" as a reading for 15977):
- Envelope v1 commands: `agent.spawn`, `agent.cancel`, `terminal.write` only.
  Drop `workspace.prepare`, `workspace.remove`, `node.inventory` command, and the
  `env` map (delete 1.1.4 and 1.1.6). `agent.spawn` payload gains typed
  `parent_session_id`, `agent_depth`, `max_agent_depth`, `workflow_name`,
  `source_root` (hub's checkout row for that machine), `workspace: {mode:
  none|worktree|clone, branch_name, base_branch}`; drops `cwd` and `env`. Reply
  `{accepted: true}` or typed refusal with stage. Node duties: workspace path
  embeds run_id (has_path_on_other_machine reason), prepare then launch, remove
  what it prepared on failure (node owns retries), list a workspace in a report
  only after its agent launched; node sets the seven GOBBY_* identity vars from
  typed fields, GOBBY_DAEMON_URL to its front door, writes the prompt file.
- Node report: `POST /api/nodes/report` (Python hub route behind the front door;
  gdaemon natively owns only `/api/nodes/channel` and `/api/machines`, node-channel
  plan lines 169-173). Body, versioned, extra=forbid / deny_unknown_fields, in the
  1.1 contract + corpus: `{v, machine_id, host_epoch, accepted: [run_id],
  agents: [{run_id, terminal_id, pid, locator, locator_key, workspace: {mode,
  path, branch_name, base_commit_sha} | null}], exits: [{run_id, terminal_id,
  exit_code, reason}]}`; complete snapshot every 30 s (node channel ping
  interval, gdaemon-node-channel.md line 190) and immediately after a change;
  exits repeat until a report gets 200. Route mirrors 2.2: requires
  `verified_front_door_identity` (src/gobby/servers/auth_service.py:189), refuses
  a body machine_id differing from the key's, validates, stores in a module-level
  inbox (`src/gobby/hub/remote_reconcile.py::node_report_inbox()`, like 1.3's
  cached client): latest snapshot per machine replaces, exits merge by
  terminal_id until consumed. Own deliverable (route owner = server app;
  reconciler owner = AgentHealthMonitor). Avoid editing lifecycle_monitor.py (959).
- 2.5: one send. Keep admission, lease, pre-registration with machine target,
  prompt None, no credential, refusals. Remove record_remote_workspace before
  send, mark_remote_workspace_removal, remove_remote_workspace, step-6 workspace
  sequence; drop remote_workspaces.py (fold row insert into reconciler/
  record helper). stage none -> DB-only settlement (fail_pending +
  _roll_back_run). accepted -> return run id pending. in doubt -> spawn_in_doubt.
  FOUND: `_prepare_run_for_session` (spawn.py ~545-680) always calls
  `get_terminal_env_vars` (constants.py:195), which runs
  `ensure_agent_cargo_target_dir` (cargo_target.py:113 mkdir) and
  `build_spawn_cache_env` (spawn_cache_policy.py:103, per-session cache dirs):
  hub-disk mkdirs for a node run. Remote flow must skip env building (flag on the
  already-targeted `_prepare_run_for_session`).
  Session workspace_path: pre-register with RoutedMachine.checkout_root.
- 2.6: input = latest consumed report per machine (no connected_machine_ids pass,
  no node.inventory). A machine with no new report is not reconciled. Keep
  fence-before-settle and two-consecutive-report absence. accepted entries left
  alone. Listed agent: record_spawn_identity (promote CAS) + insert workspace row
  if CAS wins / absent (ON CONFLICT DO NOTHING; unique (machine_id,
  worktree_path)). Exit for a pending row = pre-launch failure: fail_pending +
  cleanup_agent(run, reason) at once (no fence). Exit for live/orphaned counts as
  absent (two-pass, dead_reason). Run sweep (2.6.7) unchanged. Remove removal
  retry and 2.6.6. Check cleanup_agent idempotence for duplicate exits.
- 2.4: keep `{stopped}`; exit report also settles; mark_exited is a CAS.
- Decisions 2 (Q1 b list), 13, 14, 15 rewritten; new decision for node reports;
  17 unchanged. Overview, Constraints. D1 rewritten: hub row is a reported
  record; finishing (merge/remove) needs node-side merge design;
  `workspace.remove` returns as a D1 command. D2/D4 refs, V2, V1 entry, withdraw
  whole M1 + preceding blank line (memory f5577ae0).
- `## Architecture` framing section after Constraints: Mermaid component diagram
  + three sequence diagrams (spawn from gclient; attention respond; node offline
  mid-run: channel drops, reports stop, rows untouched, respond 503 node_offline,
  hooks queue in the node ledger, reconnect, next report reconciles).

BLOCKING FINDING (stamped plan, missed by Writer and Adversary; report to 15977
before drafting, likely needs a scope ruling): the Python hub's storage refuses
foreign-machine rows by design.
- `src/gobby/storage/sessions/_crud.py:95` `_SessionCRUDMixin.register` calls
  `require_local_machine_id(machine_id, ...)` (workspace_machine_scope.py:54,
  raises MachineOwnershipMismatchError for any explicit non-local machine). So
  `prepare_terminal_spawn` -> `create_child_session` (agents/session.py:181
  `self._storage.register(machine_id=config.machine_id)`) fails for a node
  machine: 2.5 cannot pre-register a node session.
- `src/gobby/storage/sessions/_manager.py:247` `register_session` (hook-driven,
  writes workspace_path=project_path at :267) also refuses: node hooks forwarded
  by #24150 cannot register sessions on the Python hub; the Constraints claim
  "session-start and session-end hooks drive its run as they do locally" is
  unproven/false.
- `src/gobby/storage/project_checkouts.py:361` `require_root` ->
  `_session_machine_id` (:289-294) -> require_local_machine_id: raises for a node
  machine, so 1.3's `resolve_target_machine` checkout check and 2.5/2.6's
  `require_root(db, project, node)` raise mismatch, not CheckoutNotFoundError.
- No path creates a node's `project_checkouts` row: registration sources are
  `gobby init` and local hook ingress (`hooks/project_checkout_ingress.py:53`),
  both require_local_machine_id; `POST /api/projects/{id}/checkouts` registers the
  hub's machine (completed/project-checkout-identity.md:47,54). Q2 eligibility
  can never pass.
- ~40 other require_local_machine_id call sites (gcode grep -w) are local-only
  consumers; most are fine for remote rows. Candidate owners: #21585 (Projects,
  checkouts route family), #21562 (Sessions family), #24151 (S2.11 hub half),
  #21555 (API keys/node registration). Proposed reading: #21575 adds a
  key-verified foreign-machine write path for exactly the rows it needs (node
  session pre-registration by the hub's remote flow; node checkout registration
  through a key-bound route mirroring 2.2), or files/defers to the owning family.
  Ask 15977.

## Revision 2 drafting progress (15977 ruled Reading A, 2026-10-12)

Ruling: three uses only; machine only from the verified key, never a body;
a node writes only its own rows; every other caller keeps
require_local_machine_id; name #21585, #21562, #24151 as reusing consumers;
security review; keep the Cargo/cache mkdir fix; ruling goes to Josh with the
stamped plan. #24215 is DONE (stamped a4de350, sent to 15977 and 16491).

DONE in hub-mode.md (uncommitted): Overview; Decisions 2, 3, 13, 14, 15, new
18 (cites 1.4.4 and 2.5.10 as the guard pins; keep those ids); Constraints
hook bullet (pre-created path: terminal_context.gobby_session_id ->
handle_pre_created_session, no register; /clear successor and node-own CLI
refused, #24151 owns); `## Architecture` (component + 3 sequence diagrams;
base validation PASSED with mermaid); P1 renamed "Hub-node interface"; 1.1
(3 commands, node bodies without machine_id, report lists max 1,000, exit
`launched` + reason enum, node duties incl. cancel-during-prepare and
prepare deadline, acceptance 1.1.1-1.1.5); 1.3 (remote checkout via
LocalProjectCheckoutManager.get, require_root unchanged, new 1.3.6); new 1.4
(nodes.py router, hub/node_reports.py inbox, register_routes mount,
1.4.1-1.4.4).

REMAINING, in order:
1. 2.5 rewrite. Targets: drop remote_workspaces.py; add
   `src/gobby/storage/sessions/_crud.py::_SessionCRUDMixin.register`,
   `src/gobby/agents/session.py::ChildSessionConfig`,
   `src/gobby/agents/session.py::ChildSessionManager.create_child_session`.
   Depends 1.3, 2.1, 2.2, 2.4. Mechanism: keyword `routed_machine_id: str |
   None = None` on register (must equal machine_id else
   MachineOwnershipMismatchError; replaces require_local_machine_id; then
   require_registered_machine still runs and raises for an unenrolled
   machine), on ChildSessionConfig (create_child_session passes it), on
   prepare_terminal_spawn (must equal machine_id), and on
   _prepare_run_for_session (passes machine_id=routed_machine_id to
   _AgentRunLifecycleMixin.create, skips get_terminal_env_vars -> env_vars {}).
   Local spawns pass nothing, so runs/env are unchanged (drop old "runs now
   carry the passed machine" consumer notes). Flow: checks, admission, lease;
   resolve_target_machine (Q2) first, then prepare_terminal_spawn(machine_id=
   target, routed_machine_id=target, prompt=None, credential_manager=None,
   workspace_path=RoutedMachine.checkout_root); attach lease; pending terminal
   row machine=target; send ONE agent.spawn (typed identity fields,
   source_root=checkout_root, workspace intent: mode none or path/branch
   embedding run_id chosen by node; branch_name embeds run_id). Reply
   {accepted} -> return run id pending. stage none -> fail_pending +
   _roll_back_run (DB only, no cleanup_failed_spawn). Other -> spawn_in_doubt,
   state kept for 2.6. record_spawn_identity moves to 2.6. Acceptance
   renumber (11 -> about 10): routes-to-requesting-machine (one agent.spawn,
   rows carry machine before send, no hub mkdir/env/prompt/credential),
   explicit machine validated, definite failure DB-only, in-doubt keeps state,
   origin is calling session, resume/placement refused, local unchanged,
   remote needs no hub checkout, 2.5.10 = guard pin: register(foreign) and
   register_session(foreign) without routed id raise; mismatched routed id
   raises; unenrolled machine raises MachineNotRegisteredError. Granularity
   block recomputed.
2. 2.6 rewrite (reconciler, depends 1.3, 1.4, 2.1, 2.5): input
   node_report_inbox().take() per machine; machines without a new report not
   visited. Per row of list_reconcilable_by_machine (agent_run_id, not
   recovery-protected): listed in agents & pending -> insert workspace row
   (raw INSERT ... ON CONFLICT DO NOTHING; worktrees: id, project_id,
   machine_id, task_id, branch_name, worktree_path, base_branch,
   agent_session_id, status active; clones same with clone_path; project from
   session, task from run) then record_spawn_identity (promote_to_live, then
   update_runtime pid/terminal_id if run active, else mark_exited);
   listed & live -> clear absent record; in accepted -> leave; exit with
   launched false -> fail_pending/mark_exited now, active run ->
   cleanup_agent("remote spawn failed on <machine>: <reason>"); exit with
   launched true counts as absent; absent live/orphaned (or host_epoch
   differs) two consecutive reports -> mark_exited, active run ->
   _dead_terminal_reason (DB-only, works remote) None -> done else
   cleanup_agent; absent pending past spawn_in_doubt_seconds for two reports
   -> agent.cancel KILL close_terminal fence -> stopped/not_found ->
   fail_pending, active run cleanup_agent("remote spawn did not start"),
   ended run nothing more. Run sweep (old 2.6.7) unchanged but per machine
   with a report. Remove removal retry, list_remote_removals, 2.6.6; renumber.
   Note promotion latency: AgentLifecycleMonitor check_interval_seconds
   default 30.0 (lifecycle_monitor.py:86, no override) -> mention in 2.6 and
   3.3 research. Old 2.5.10 test (session end before identity) moves here.
3. 2.1 research: "because 2.5 writes a node's rows" -> 2.6.
4. D1 rewrite: hub row is a reported record; merge/remove need node-side
   design; a removal command returns there; row keeps agent_session_id.
   D4: add checkout registration via 1.4 and report promotion; reason adds
   #24148.
5. V1 dated entry (Josh review 2: one command, reports, Reading A, M1
   withdrawn); V2 pytest list adds tests/servers/test_node_routes.py.
6. Withdraw whole `## M1 Task Manifest` block plus its preceding blank line
   (Edit in chunks; Bash writes blocked).
7. Sweep: grep workspace.prepare|workspace.remove|node.inventory|
   remote_workspaces|record_remote_workspace|env map|1.1.6 -> none stale.
STATUS: steps 1-7 DONE (2.5, 2.6, 2.1, D1, D4, V1, V2, M1 withdrawn, sweep);
base validation clean. Extra edits: 1.1 null base_branch rule; the four
`_app_routes.py` consumer lines moved into 1.4's implementation (validator
conflict); `src/gobby/agents/__init__.py` consumer added. 2.5 passes the real
prompt (no file written); remote branch `agent/<run_id>`; pending terminal
backend `native`. 2.6 settles every exited run through dead_reason, except
`launched: false` exits, which use the node's reason.
8. Validate base; ocr; commit `[gobby-#21575] docs: revise hub-mode for
   Josh's second review and withdraw M1`; send SHA to 16491 with readings for
   15977: (a) use 3 via LocalProjectCheckoutManager.get, require_root
   unchanged; (b) routes refuse hub's own machine; (c) report lists capped at
   1,000; (d) inbox in memory, lost exits degrade to absence rules; (e)
   accepted runs bounded by node prepare deadline, hub leaves them alone.
