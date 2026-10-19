# hub-mode D1-D4 repair: research and design notes (Plan Writer gobby#16490, 2026-10-14)

Ruling (15977): remove every deferred D-section and TBD task_ref; plan D1-D4 in scope;
D1 choose merge design + listing, merge, node removal command, hub row deletion; D2 leaf
(#24090 closed, nodes/ landed); D3 producer+consumer of node git state; D4 fully.
Advisor: preserve existing section IDs (expanded tasks carry covers labels); restore 1.2 for
D2; new P4/P5 for the rest; confirm labels with get_task #24269 after DAEMON BACK.
Do not rewrite 1.1 acceptance (landed #24268). Validate only after DAEMON BACK.

## Facts (verified 2026-10-14)

Landed:
- Contract v1 (#24268, commits 489a86cbc1, dd349ff314): docs/contracts/node-commands.md,
  crates/gdaemon/src/node_commands.rs, src/gobby/hub/node_commands.py, corpus.
  node-halves 3.1 bumps to v2 (CONTRACT_VERSION=2; every handler/route refuses other v).
- nodes/ (#24090, #24091): registry.rs `Registry{slots: Mutex<HashMap<Uuid,Slot>>}`,
  `Slot{current: Option<ChannelHandle>, gate}`, private `ChannelHandle{connection_id,
  close: oneshot::Sender<(u16,&'static str)>, live}`; `register(machine)->(Uuid,
  oneshot::Receiver)`, `remove`, `mark_live`, `is_connected`, `connected_ids`,
  `connection_id`, `persist`. channel.rs: `handle(nodes: Arc<HubNodes>, request)` reads
  x-gobby-machine-id/x-gobby-key-id, upgrades; `hub_channel(socket, nodes, machine, key)`
  hello -> register -> heartbeat persist -> key check -> send_ack -> loop select{close,
  idle, rechecks, socket.recv()} ignoring every frame but Close/Ping; `run_client(node,
  timing)` node side. mod.rs `HubNodes{db, registry, timing}`, `NativeRoutes{Standalone,
  Hub(Arc<HubNodes>)}`. machines_api.rs. No command_sender.
- Constraints line "Not landed: nodes/" is now false -> rewrite.
- e2e harness tests/e2e/test_hub_node_pair.py: fixtures `pair_hub` (bootstrap hub:true,
  spawn_daemon_instance tls self-signed) and `pair_node` (EnrolledNode{home, http_port,
  websocket_port, machine_id, api_key, key_id, stderr_path}; bare `gdaemon serve`,
  GOBBY_HOME=home); helpers `_wait_listener`, `_wait_connection`.

Workspace rows and routing chokepoint:
- `LocalWorktreeManager.get`/`LocalCloneManager.get` -> `get_owned_workspace_row`
  (src/gobby/storage/workspace_machine_scope.py) RAISES `MachineOwnershipMismatchError
  (resource_kind, resource_id, owner_machine_id, current_machine_id)` for a foreign id.
  `resolve_reference` resolves prefixes across all machines (full UUID passes through).
- `InternalToolRegistry.call`/`call_sync` (src/gobby/mcp_proxy/tools/internal.py) catch it
  and return `exc.to_dict()` (error_code machine_ownership_mismatch).
- CLI `gobby worktrees|clones` posts to daemon `/api/mcp/gobby-worktrees/tools/{tool}`
  (registry.call) -> same chokepoint.
- REST src/gobby/servers/routes/source_control_worktrees.py: `delete_worktree` and
  `run_clone_operation` (delete_clone, sync_clone) call storage.get -> 409 to_dict;
  `list_worktrees`/`list_clones` machine-bound.
- Registries: `create_worktrees_registry` (worktrees/_factory.py, 73 lines) merges
  sub-registries; `create_clones_registry` (tools/clones.py, 57 lines).
- Local merge `_sync.py` (908 lines; do not edit) `_merge_worktree_impl`: ref checks;
  already-merged reconcile; target worktree via `git worktree list`; checkout target if
  needed (error if target registered at another checkout on another branch); dirty =
  non-.gobby porcelain + staged; overlap with `diff --name-only HEAD...source` refuses
  (overlapping_dirty_paths); stash .gobby/ keep-index; staged paths -> fast-forward
  landing; else `git merge <source_ref> --no-ff --no-edit`; conflict -> has_conflicts,
  conflicted_files; `mark_merged` (cleanup_days 0); result merge_sha/target_head_sha/
  commit_sha; schema-identity cutover advisory. Clone merge (_clones_operations.py 814):
  fetch clone branch into refs/heads/clone-merge/<branch>, same landing, delete temp
  branch, merged cleanup_after +7 days.
- Expiry: runner_maintenance/isolation.py::cleanup_expired_isolation_loop (hourly),
  `find_expired` (merged, this machine, agent_session_id IS NULL, cleanup_after < now,
  CLOSED_TASK_CLEANUP_PREDICATE).
- Node workspaces (node-halves 2.3): `<home>/worktrees/<project_id>/<run_id>` or
  `<home>/clones/<project_id>/<run_id>`; branch `<branch_name>-<run_id>`;
  `nodes/agents/workspace.rs::remove(record)` (worktree remove --force + branch -D; clone
  remove_dir_all; missing counts removed). Node drops launched record after exit acked.
  node-halves Decision 23: node never removes an `existing` (resume) workspace; 3.2
  `resumed_workspace` reads the active row by agent_session_id on run.machine_id, so D1
  must never NULL agent_session_id on node rows.
- node-halves 2.7 `crates/gdaemon/src/nodes/agents/commands.rs::dispatch` (per-run queues);
  2.9 in-memory `registered` set.
- node-halves Constraints/Decisions cite hub-mode by letter: "3.1 blocks hub-mode 1.3
  (#24269), 1.4 (#24270), D2 (#24281)"; "hub-mode D4 (#24283) proves this plan";
  Decisions 13, 20; 3.1 field closure "hub-mode D1 (Remote workspace lifecycle)".
  -> notify 16767 (copy 15977) of new IDs once drafted.
- Existing hub-mode tasks: #24268 1.1 (landed), #24269 1.3, #24270 1.4, #24273 2.3,
  #24274 2.4, #24275 2.5, #24276 2.6, #24280 D1, #24281 D2, #24282 D3, #24283 D4.

D3 sources:
- gclient crates/gclient/src/daemon/projects.rs: `ProjectRow{checkout: Option<Checkout
  {machine_id, root_path}>}`, `SourceStatus{current_branch, ahead, behind, repo_path,
  worktree_count}`, `WorktreeRow{machine_id,...}`; app/sidebar_model.rs builds
  `ProjectEntry{branch, ahead, behind, worktrees}`; ui/chrome/sidebar_state.rs
  `machine_filter` (None = local, ALL_MACHINES).
- Hub: /api/projects (servers/routes/projects.py 524 lines: `_local_checkouts(db, ids)`
  uses list_for_machine(this); `_build_project_response` reads approval rules and
  validation detection from the checkout root on disk -> must skip for remote).
  /api/source-control/status (servers/routes/source_control.py::get_status 73-147;
  `_resolve_project` in source_control_git.py uses require_local_machine_id + require_root;
  `git for-each-ref --format=%(HEAD)\t%(refname:short)\t%(upstream:short)\t%(upstream:track)
  refs/heads/`; `parse_upstream_track`; 30 s cache `_get_cached/_set_cached`;
  worktree_count/clone_count from machine-bound storage).

D4 sources:
- Hook attention: `handle_permission_request` -> `_enter_turn_wait(event, "approval")`
  (src/gobby/hooks/event_handlers/_misc.py).
- TODO before drafting D4: read node-halves 2.4/2.5/2.6 for whether sandbox.enabled=false
  skips SRT verification, and the claude argv 2.6 builds, so a fake `claude` on the node's
  PATH (execs ghook for session-start, PermissionRequest, stop; echoes stdin to a file)
  passes the chokepoint. If it cannot, stop and send 15977 that item and reason.

## Decisions to write

- Section map: D2 -> 1.2 (restored, depends 1.1); D1 + D3 -> P4; D4 -> P5. Cross-plan edges
  listed under Constraints for the Orchestrator to set at expansion (node-halves pattern).
- D1 merge: node-side. Reason: branch, worktree and base checkout live on the node; merge
  lands where the work branched, as local merge does; no git transport or credentials
  cross machines; the hub may hold no checkout of the project. Rejected: moving commits
  (bundle/push) lands work in a different checkout and adds a transport.
- Node merge is stricter than local: refuse when the target checkout has staged entries or
  dirty paths overlapping incoming; no .gobby stash; no fast-forward landing; no
  schema-identity cutover advisory (node runs no Python daemon). Conflict: abort, restore
  original branch, ok result outcome "conflict" + paths. Outcomes: merged | already_merged
  | conflict | target_dirty. Errors: not_found, workspace_busy, invalid_payload,
  command_failed ("partial" only if target may have moved).
- workspace.remove {project_id, mode, path, branch_name} -> {removed:true}; idempotent;
  `workspace_busy` stage none while an accepted or live run uses the path (distinct from
  node-halves' launch-failure removal and Decision 23's in-place resume).
- Payloads carry path, branch, base, source_root from hub rows (node keeps no record).
- Contract v3 leaf after node-halves 3.1 (shared Targets): workspace.merge,
  workspace.remove, checkout.status (if D3 on-demand).
- Hub module src/gobby/hub/remote_workspaces.py: reader by id on any machine, list by
  machine/project, merge (source_root from LocalProjectCheckoutManager.get), mark merged
  (worktree cleanup 0 d, clone 7 d), remove -> delete row on removed:true, expiry step for
  node rows (merged, cleanup_after past, closed-task predicate, no agent_session_id
  predicate; node busy refusal guards).
- Routing: wrap gobby-worktrees merge_worktree/delete_worktree and gobby-clones
  merge_clone/delete_clone at registry build (_factory.py, clones.py): on
  MachineOwnershipMismatchError of that kind call the remote op. REST delete routes same.
  Listing: machine_id query param on REST list routes (gclient passes machine filter).
- D3: decide on-demand `checkout.status` vs report field (advisor: offline behavior is the
  discriminator). gclient projects pane follows machine_filter (None/ALL = client's local
  machine); hub routes take machine_id; remote reads skip disk.
- D4: e2e `tests/e2e/test_hub_node_live.py` reusing pair_hub/pair_node.
- Sweep: Overview "Four sections are deferred", Decisions 2, 3, 10, 15, 17, Constraints
  "Not landed", Architecture "(D2)" labels and "v1 commands", 1.3 research "built in D2"
  and "before D2 lands", every D1-D4/TBD/deferred/"no v1 command"; V1 dated line; V2 new
  test files.

## Findings added 2026-10-14 (after DAEMON BACK)

- get_task #24269: covers labels are `covers:hub-mode:1.3:1.3.N`, so section IDs stay.
- #24281 (old D2 stub) carries #24268 review requirements (16011): (1) live tests prove
  constant-time `X-Gobby-Backend-Secret` admission from loopback and 404 before any frame for
  every other peer and standalone/node front doors; (2) handler validation never infers
  authorization from a parse; provider_args, prompt, source_root, extra_write_paths are plain
  strings and sandbox.enabled may be false, so the node executor applies its SRT and
  credential chokepoints; (3) LOW parity: Python NodeModel lacks strict=True, Rust Option
  fields accept omission, Python nullable fields required -> malformed-input parity cases.
  Carry (1)(3-route body) into 1.2, (3-models) into 4.1. #24280/#24282/#24283 carry nothing.
- D4 chokepoint: spawn_agent always requires managed SRT (`sandbox_gate.require_managed_srt`:
  enabled srt + `verify_srt_installation()`), so the fake provider runs SRT-wrapped on the node.
  e2e precedent `tests/e2e/conftest.py::link_operator_srt(home)` symlinks the operator's
  `~/.gobby/tools/srt/<ver>` into an isolated home, skips when absent; `e2e_srt_spawn_home`
  keeps GOBBY_HOME outside the workspace. `pair_node` sets GOBBY_HOME only, not HOME -> the
  live test must give both daemons a temp HOME (node-halves 2.6 seeds ~/.claude.json and
  ~/.codex/config.toml). Precedent for a provider under SRT calling ghook:
  `tests/e2e/test_composer_live_proof.py` (real providers, admission-gated).
- hub-mode 2.5 gap: step 4 says "the resolvers' results" but 2.5 never names which resolvers
  the remote flow runs; node-halves 3.2 lists resolve_spawn_generation_endpoint
  (spawn_agent/_generation_endpoint.py), resolve_spawn_reasoning (agents/reasoning.py),
  resolve_spawn_sandbox (spawn_agent/_sandbox_gate.py), code_index_preflight_mode
  (spawn_agent/_code_index.py). resolve_spawn_sandbox verifies the HUB's SRT install for a
  node run (wrong machine). Fix in 2.5 step 1: remote flow builds
  `apply_write_grant(definition_sandbox_config(daemon_config, agent_body), write_grant)` and
  refuses `sandbox_required` unless enabled and backend srt, with no hub install check (the node
  verifies its own, node-halves 2.5), as step 1 already skips the hub agy gate. New item 2.5.15.
- D1 routing: the four tools check git manager before the row lookup (`_merge_clone_impl`
  returns "Clone tools require a git repository context" first), so catching
  MachineOwnershipMismatchError is unreliable. Wrapper resolves ownership FIRST:
  worktrees via `ctx.resolve_worktree_id` (`LocalWorktreeManager.resolve_reference`, cross-
  machine prefix), clones by full id; read owner machine; foreign -> remote op; else original.
  All four tools are async; wrap with functools.wraps so `_prepare_call`'s
  `inspect.signature` keeps the schema. Path-addressed deletes (worktree_path, clone_path)
  stay local. merge_worktree push/prefer_remote are refused locally too (unsupported).
  delete_worktree force (uncommitted) and force_delete_branch (unmerged) and merged_into
  carry to workspace.remove. MCP list tools: `_crud.py::create_crud_registry` (nested
  list_worktrees), `_clones_read.py::create_clone_read_registry` (nested list_clones) gain
  `machine_id`. Nested route/tool functions have bare qualified names; target the outer factory.
- D3 decided: on-demand `checkout.status {project_id, root_path}` -> `{current_branch,
  branch_count, ahead, behind}`. Hub get_status(project_id, machine_id) remote path: checkout row
  via LocalProjectCheckoutManager.get(machine, project), command, 30 s cache per (machine,
  project); node_offline or any NodeCommandError -> 200 with git fields null (offline card
  shows no branch, no stale state). Counts from hub rows for that machine. Rejected report
  field: inbox take() empties snapshots so a new store is needed, node runs git for every
  registered checkout every 30 s unread, and offline shows stale state.
  /api/projects?machine_id: `_projects_to_responses` (also called by files.py:210),
  `_local_checkouts` -> list_for_machine(machine), `_build_project_response` skips disk reads
  (approval_rules [], validation_detection None) for a remote checkout. REST list_worktrees /
  list_clones (source_control_worktrees.py, nested in create_source_control_worktrees_router)
  gain machine_id.
- gclient: Daemon trait (daemon/mod.rs:338, file 926 lines, no cfg(test)) projects(),
  source_status(project), worktrees(project) gain `machine: Option<&str>`; implementors
  LiveDaemon (live_connect.rs:160), ScriptedDaemon (mod.rs:698), ReconnectDaemon
  (tests/client_loop.rs:585), TraceDaemon (tests/teardown.rs:523); RestClient::projects,
  source_status, worktrees (rest.rs); callers live_sidebar.rs:69 fetch_sidebar_rows,
  SidebarRequest::run (:378), project_paths.rs:30 (passes None), rest/tests.rs:127. mod.rs split:
  move ScriptedState/ScriptedDaemon (399-926) to new daemon/scripted.rs. Filter sites
  (actions/sidebar.rs:53, menu.rs:321, mouse/pointer.rs:247) untouched: run_live_loop
  (live_loop.rs:561 area) calls new `Workspace<LiveDaemon>::scope_projects_to(machine)` each
  tick; None/ALL_MACHINES -> None (client's local), else the machine; on change clear
  projects/statuses/worktrees, bump stamps, queue projects + rows. SidebarRequest/SidebarFetch
  carry machine; apply_sidebar_fetch drops a fetch for another machine.
- Cross-plan file sharing: node-halves targets nodes/mod.rs::* (1.2 hooks, 2.1 agents),
  channel.rs::run_client (2.7), nodes/agents/commands.rs (2.7+), nodes/agents/mod.rs (12
  sections), node_commands.{rs,py}, corpus (3.1). Edges: node-halves 2.1 and 3.1 block 1.2;
  1.2 blocks node-halves 2.7; node-halves 3.1 blocks 4.1; node-halves 2.14 (last P2 leaf)
  blocks 4.2 and 4.6 (node handlers); all node-halves leaves block 5.1.
