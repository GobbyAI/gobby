Plan artifact: `.gobby/plans/hub-mode.md`

# S4.2 Hub mode: the Python hub routes work to remote nodes

**Plan ID:** hub-mode

## Overview
`kind: framing`

This plan specifies the remaining scope of #21575 (Hub mode: the datastore owner
that runs everything database-backed). The mode gate already landed: #23111 (Node
runners skip hub-only loops) and the run-modes slice
(`.gobby/plans/completed/gdaemon-run-modes.md`). Josh approved decisions A-D on
2026-10-12, and C put Python hub-side routing to remote nodes in this epic.

When the plan closes:
- The hub sends a versioned command to a node over its node channel and gets the
  correlated reply, through a loopback-only gdaemon route.
- A spawn, a cancel, or a terminal write aimed at another machine reaches that
  machine's node, or fails with a typed `node_offline`. It never runs on the hub
  instead.
- The roster lists every machine's agents, each tagged with its machine. gclient's
  existing machine filter shows the current machine by default, and every
  machine's row carries its attention state.
- Hub loops that touch local panes, processes, or files act only on their own
  machine's rows.

The node-side handlers are #24148 (S2.7 node half: node-local agent spawn and
worktrees in gdaemon). Until they land, every test here drives a fake node, and
the path is end-to-end only after #24148. #24149 (S2.7 hub half) later redoes the
Python callers in Rust and reuses the envelope and the gdaemon route.

## Decision Record
`kind: framing`

Operator decisions (Josh, 2026-10-12, relayed by Orchestrator gobby#15977):

1. **Q0 a. Scope.** #21575 is Python hub-side remote-node routing.
2. **Q1 b. Command path.** This plan owns:
   - a versioned hub-to-node command envelope as a committed contract with a
     golden corpus (request id, command, payload, typed errors);
   - a loopback-only gdaemon hub route that sends one command over the machine's
     node channel and returns the correlated reply;
   - Python callers for spawn (with worktree or clone preparation), cancel, and
     terminal write.

   The envelope and the gdaemon route are durable, and #24149 reuses them. The
   Python callers live in the `src/gobby/hub/` package and are transitional.
3. **Q2 a. Eligibility.** A machine is eligible for a project when a
   `project_checkouts` row exists for (project, machine) and the gdaemon registry
   reports its channel live, read through the 1.2 route.
4. **Q3 a. Target machine.** A spawn defaults to the requesting session's
   machine. An optional explicit machine (id or node ref) is validated by Q2.
   There is never a fallback to the hub or to another machine (#17436 Node mode).
   Hub-originated dispatch uses the origin machine recorded when its run started.
5. **Q4. Roster scope.** Josh, verbatim: "Wouldn't the machine just show that
   there's an attention icon? We display all machines, we just don't show all
   agents and all projects on all machines at once. Switching machines switches
   the projects and agents and terminals view."
6. **Q5 a. Remote respond.** Responding to another machine's attention entry
   routes a terminal write to the owning node, with a typed `node_offline` when
   it is disconnected.
7. **Q6 a. Remote attention source.** Story B uses hook-driven attention only.
   Node pane detection and its reporting route are planned with #24148.
8. **Standalone and the hub's own machine** execute in-process, as today
   (ROADMAP decision 13).
9. **Remote terminal viewing** stays with S4.3 #20202 (Remote gobby attach) and
   S2.8. The terminals view per machine is #21549 (machine-scoped terminals).

Writer decisions:

10. **Q4 implementation.** gclient already implements the view Josh describes:
    - `crates/gclient/src/ui/sidebar/machines.rs::machine_rows` draws one row per
      machine with a state dot that rolls up its agents' states, attention
      included.
    - `crates/gclient/src/ui/chrome/sidebar_state.rs` keeps `machine_filter`:
      `None` lists the local machine, `ALL_MACHINES` every machine, and a click
      on a machine row selects it.
    - The local machine is the client's own (`crates/gclient/src/views/mod.rs`
      calls `read_local_machine_id`).

    It filters one roster on the client. The gap is on the hub:
    `AttentionStateManager.load_roster_rows` keeps only the hub machine's runs,
    and entries carry no machine. So the roster becomes hub-wide with a
    `machine_id` on every entry (3.1), and gclient reads it (3.2). One payload
    keeps gclient's subscribe-first reconciliation intact, because attention
    broadcasts are hub-wide. A per-machine summary endpoint plus a
    selected-machine roster would need machine-scoped broadcasts and a refetch on
    every switch. Machines with no live agent stay unlisted until a gclient
    consumer of `/api/machines` (#24091) exists; such a machine has no attention.
11. **Hub-originated runs.** Builds, cron runs, and pipelines execute on the hub
    runner. Their machine-local steps (integration workspace, merge, branch
    cleanup, pipeline exec steps) run on the hub's machine, so that is their
    origin machine. Cron runs already record it (`cron_runs.machine_id`). Their
    dispatched agents keep spawning locally, so this plan changes nothing for
    them. A build whose integration runs on a node needs git transport and
    node-side build steps; that is outside this plan.
12. **Python-to-gdaemon authentication.** gdaemon already gives its Python
    backend `GOBBY_FRONT_DOOR_SECRET` (`crates/gdaemon/src/state.rs`). The
    internal route admits a request only from a loopback peer that presents that
    secret in `X-Gobby-Backend-Secret`, compared in constant time. Every other
    request gets 404. The route is never proxied, and the header is not in
    `IDENTITY_HEADERS`.
13. **Spawn split.** Most of `spawn_agent_impl` touches the local machine: git,
    worktree preparation, cargo checks, placement, endpoint and provider
    preparation (`prepare_claude_spawn` reads `.mcp.json` and pre-approves the
    directory in the local provider config). So a remote spawn is its own flow in
    `src/gobby/hub/remote_spawn.py`:
    - The hub performs admission, the task spawn lease, and run and session
      pre-registration.
    - The node prepares the workspace and builds and launches the provider
      command at its SRT chokepoint (#24148).

    The two request-facing callers (the MCP `spawn_agent` tool and
    `POST /api/agents/spawn`) choose the remote flow. Dispatch, cron, and
    feedback spawns stay local (decision 11). A remote spawn refuses `resume`
    and placement with typed errors: resume needs the node's provider thread
    files, and placement is a gclient-local pane feature. A session on another
    machine that names this machine is refused too: the local flow resolves the
    project root from the parent session's machine and rejects a foreign one.
14. **No datastore credential crosses to a node** (ROADMAP decision 17). A remote
    spawn issues no managed credential and writes no prompt file on the hub. The
    `agent.spawn` env carries only allowlisted run-identity variables.
15. **Remote workspaces stay out of the hub's tables.** `worktrees` rows carry no
    `machine_id`, so a remote worktree row would expose the node's path to the
    hub's local worktree loops. A remote spawn records its workspace in the
    run's `resume_metadata_json` (`remote_workspace`). It removes the workspace
    with `workspace.remove` only when the spawn fails. Listing, merging, and
    removing finished remote workspaces is D1.
16. **Hook machine binding.** Q3 trusts `sessions.machine_id`, which comes from
    the hook envelope (`ClaudeCodeAdapter` reads `input_data["machine_id"]`).
    When a hook request carries a verified front-door identity, the hub rejects
    an envelope whose `machine_id` differs from the key's machine (2.2).
17. **#24090 dependency.** The 1.2 route extends the node channel's registry and
    handler, which #24090 (Node channel: hub registers, heartbeats, replaces,
    revokes) creates. This plan follows the node-channel precedent (Orchestrator
    ruling, 2026-10-05): the sections stay active, and expansion waits until
    #24090 closes. At that gate the `crates/gdaemon/src/nodes/` Targets are
    refreshed to exact symbols against the landed code. The Orchestrator records
    the S4.2 ← #24090 edge on #21575, and 1.1 adds it to `ROADMAP.md`.

## Constraints
`kind: framing`

- **Landed on `0.5.0`:** #23111 (`HUB_ONLY_PERIODIC_TASKS`,
  `HUB_ONLY_STARTUP_PHASES`), run-modes 2.1 (`/api/health` reports the mode),
  #23112 (node-local lifecycle), and #24075 (a node runs only gdaemon, never the
  Python daemon).
- **Landed without a `nodes/` module:** #24088 (node startup, merge
  `df008c8c76`) changed `front_door/mod.rs`, `serve.rs`, and `state.rs`;
  `FrontDoor::handle`, `plaintext_allowed`, and the `GOBBY_FRONT_DOOR_SECRET`
  hand-off are unchanged.
- **Not landed:** `crates/gdaemon/src/nodes/` (#24090, #24089-#24092). Its design
  is `.gobby/plans/gdaemon-node-channel.md` §1.4: `Registry`, `Slot`,
  `ChannelHandle { connection_id, close, live }`, `register`, `remove`,
  `persist`, `mark_live`, `is_connected`, `connected_ids`, and Decision 9
  ("Only hello, ack, and ping/pong exist in Stage 1; S2.7 and S2.8 add
  commands").
- **No backward compatibility.** Nothing is migrated.
- **Monolith ceiling.** `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py`
  (988 lines) is not edited; the remote flow is a new module.
  `src/gobby/storage/terminals.py` (933 lines) changes only through 2.1's split,
  which moves its reap queries to a new module.
- **Test isolation.** pytest runs use
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1`
  and never the full suite. Rust tests run through `cargo nextest run -p <crate>`.

## P1: Hub-to-node command path
`kind: framing`

**Goal**: the hub can send one command to a connected node and get its reply.

### 1.1 Node command envelope: contract, golden corpus, and typed models [category: code]
`kind: deliverable`

Targets:
- `docs/contracts/node-commands.md`
- `crates/gdaemon/src/node_commands.rs`
- `crates/gdaemon/src/lib.rs`
- `crates/gdaemon/tests/node_command_corpus.rs`
- `crates/gdaemon/tests/fixtures/node_commands/corpus.json`
- `src/gobby/hub/__init__.py`
- `src/gobby/hub/node_commands.py`
- `tests/hub/test_node_command_corpus.py`
- `ROADMAP.md`

**Research context:**
- Contract precedent: `docs/contracts/gterm-protocols.md` pairs a protocol
  contract with golden messages that every encoder round-trips
  (`crates/gterminal/tests/fixtures/wire_golden/`).
- The node channel carries JSON text frames. Stage 1 has only `hello`, `ack`, and
  ping/pong (`.gobby/plans/gdaemon-node-channel.md`, Decision 9).
- `crates/gdaemon/src/lib.rs` has no indexed symbols; it declares modules.
- Spawn identity variables come from
  `src/gobby/agents/constants.py::get_terminal_env_vars`: `GOBBY_SESSION_ID`,
  `GOBBY_AGENT_RUN_ID`, `GOBBY_PROJECT_ID`, `GOBBY_AGENT_DEPTH`,
  `GOBBY_MAX_AGENT_DEPTH`, `GOBBY_PARENT_SESSION_ID`, `GOBBY_WORKFLOW_NAME`.
  `GOBBY_DAEMON_URL` and `GOBBY_PROMPT_FILE` are machine-local, and
  `GOBBY_MANAGED_EXECUTION_BOOTSTRAP` points at a credential grant.

**Implementation:**
- The contract specifies:
  - **Command frame**, hub to node:
    `{"type":"command","v":1,"request_id":"<uuid>","command":"<name>","payload":{...}}`.
  - **Reply frame**, node to hub:
    `{"type":"reply","v":1,"request_id":"<uuid>","ok":true,"result":{...}}`, or
    `{"type":"reply","v":1,"request_id":"<uuid>","ok":false,"error":{"code":"<code>","message":"<text>","stage":"none"|"partial"|null}}`.
  - **Error codes.** gdaemon produces `node_offline`, `node_timeout`, and
    `channel_closed`; a node never sends them. A node produces
    `unsupported_version`, `unknown_command`, `invalid_payload`, `not_found`, and
    `command_failed`. `stage` is set only by `terminal.write`.
  - **Strictness.** Payloads and results reject unknown fields.
  - **v1 commands:**
    - `workspace.prepare`: `{project_id, mode: "worktree"|"clone", source_root, path, branch_name, base_branch}` returns `{path, branch_name, base_commit_sha}`.
    - `workspace.remove`: `{project_id, mode, source_root, path, branch_name, delete_branch}` returns `{removed}`.
    - `agent.spawn`: `{run_id, session_id, project_id, terminal_id, provider, model, cwd, prompt, env, provider_args, reasoning_effort, sandbox: {enabled, network: "none"|"trusted", extra_write_paths}, timeout_seconds, title}` returns `{pid}`.
    - `agent.cancel`: `{run_id, terminal_id, signal: "TERM"|"KILL", close_terminal}` returns `{stopped}`.
    - `terminal.write`: `{terminal_id, kind: "text"|"key"|"input"|"paste", data, submit}` returns `{}`.
  - **`agent.spawn` duties.** `env` keys must come from the seven identity
    variables above; anything else is `invalid_payload`. The node sets
    `GOBBY_DAEMON_URL` to its own front door, writes `prompt` to a node-local
    file and sets `GOBBY_PROMPT_FILE`, builds the provider command, applies its
    SRT chokepoint, and closes the pane when the agent process exits.
  - **Versioning.** A new command or field bumps `v`. A node answers an unknown
    `v` with `unsupported_version`.
- `node_commands.rs` defines serde types for both frames and every payload and
  result, with `deny_unknown_fields`. It is `pub mod node_commands` in `lib.rs`.
- `src/gobby/hub/node_commands.py` defines the matching pydantic models with
  `extra="forbid"`.
- `corpus.json` holds one named message per frame shape: each command, each
  command's ok reply, one error reply per code, and a `terminal.write` error with
  `stage: "partial"`. Both tests parse every message, re-encode it, and compare
  it to the corpus value.
- `ROADMAP.md`: the S4.2 row gains the ← #24090 edge.

Verification planned:
- `cargo nextest run -p gobby-daemon -E 'binary(node_command_corpus)'`.
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/hub/test_node_command_corpus.py -v`.
- `uv run ruff check src/gobby/hub` and `uv run mypy src/gobby/hub`.

**Acceptance:**

- 1.1.1 - The contract specifies both frames, all v1 commands with payload and result fields, the error codes and who produces them, the env allowlist, and the versioning rule. file: `docs/contracts/node-commands.md`.
- 1.1.2 - The Rust types round-trip every corpus message byte-equivalently and reject an unknown field. test: `crates/gdaemon/tests/node_command_corpus.rs::corpus_round_trips`.
- 1.1.3 - The Python models round-trip every corpus message and reject an unknown field. test: `tests/hub/test_node_command_corpus.py::test_corpus_round_trips`.
- 1.1.4 - An `agent.spawn` payload whose `env` has a key outside the allowlist fails validation in both languages. test: `tests/hub/test_node_command_corpus.py::test_spawn_env_allowlist`.
- 1.1.5 - `ROADMAP.md` shows S4.2 depending on #24090. file: `ROADMAP.md`.

### 1.2 gdaemon internal node-command route over the node channel [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/nodes/internal_api.rs`
- `crates/gdaemon/src/nodes/registry.rs`
- `crates/gdaemon/src/nodes/channel.rs`
- `crates/gdaemon/src/nodes/mod.rs`
- `crates/gdaemon/src/front_door/mod.rs::*` — scope-reason: `FrontDoor::handle` routes `/api/internal/nodes/` to `nodes::internal_api` on a hub, after the runtime gates and before `auth.authenticate`
- `crates/gdaemon/tests/node_commands.rs`

**Research context:**
- `crates/gdaemon/src/front_door/mod.rs::FrontDoor::handle(…, peer: SocketAddr, …)`
  runs the runtime gates, the challenge, and `auth.authenticate`, then
  `ws::splice` or `RouteTable::dispatch`. `serve.rs::plaintext_allowed` tests a
  loopback peer with `peer.to_canonical().is_loopback()`.
- `front_door/auth.rs::AuthState` keeps the front-door secret as `secret`
  (`GOBBY_FRONT_DOOR_SECRET`, which `crates/gdaemon/src/state.rs` passes to
  Python). `authenticate` strips `IDENTITY_HEADERS` on every path.
- The `nodes` module, `Registry`, and the channel handler come from #24090 (see
  Constraints). These Targets are refreshed to exact symbols at the expansion
  gate (Decision 17).

**Implementation:**
- `ChannelHandle` gains `commands: mpsc::Sender<OutboundCommand>`.
  `OutboundCommand` is `{ frame: CommandFrame, reply: oneshot::Sender<ReplyFrame> }`.
  `Registry::command_sender(machine_id)` returns the sender only for a live
  handle.
- The hub channel handler's select loop does three things:
  - It writes each `OutboundCommand` frame and keeps a
    `HashMap<Uuid, oneshot::Sender<ReplyFrame>>`.
  - It resolves an inbound `reply` frame by `request_id`. An unknown id is
    dropped and logged.
  - When the channel closes or is replaced, every pending sender gets a
    `channel_closed` error reply.
- `internal_api.rs` serves two routes on a hub only:
  - `GET /api/internal/nodes/connected` returns `{"machine_ids":[...]}` from
    `connected_ids`.
  - `POST /api/internal/nodes/{machine_id}/commands` takes
    `{"command","payload","timeout_ms"}`. It mints `request_id`, sends the frame,
    and waits up to `timeout_ms` (default 30 000, maximum 600 000). It returns the
    reply frame as JSON with status 200. Without a live channel it returns
    `node_offline`, and after the wait it returns `node_timeout`, both as reply
    frames.
- Admission requires a loopback peer and an `X-Gobby-Backend-Secret` equal to the
  secret, compared in constant time. Anything else gets 404, as does any
  standalone or node request.

Verification planned:
- `cargo nextest run -p gobby-daemon -E 'binary(node_commands)'`.
- `cargo clippy -p gobby-daemon` and `cargo fmt -p gobby-daemon -- --check`.

**Acceptance:**

- 1.2.1 - A fake node on a live channel receives the command frame and its reply reaches the caller unchanged. test: `crates/gdaemon/tests/node_commands.rs::command_round_trips_over_the_channel`.
- 1.2.2 - With no live channel the route returns `node_offline`, and a node that never replies yields `node_timeout`. test: `crates/gdaemon/tests/node_commands.rs::offline_and_timeout_are_typed`.
- 1.2.3 - Replacing or closing the channel completes every pending command with `channel_closed`. test: `crates/gdaemon/tests/node_commands.rs::channel_close_fails_pending`.
- 1.2.4 - A non-loopback peer, a missing or wrong secret, or a standalone or node front door gets 404. test: `crates/gdaemon/tests/node_commands.rs::internal_route_admits_only_loopback_backend`.
- 1.2.5 - `connected` lists only live machines. test: `crates/gdaemon/tests/node_commands.rs::connected_lists_live_channels`.

### 1.3 Python node-command client and machine eligibility [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/hub/node_client.py`
- `src/gobby/hub/machine_routing.py`
- `tests/hub/test_node_client.py`
- `tests/hub/test_machine_routing.py`

**Research context:**
- `src/gobby/config/bootstrap.py::load_bootstrap` yields `daemon_port`, the front
  door's port. Python itself binds `127.0.0.1` at a port offset.
- `src/gobby/utils/machine_id.py::get_machine_id` and `require_machine_id`
  return this machine's id.
- `src/gobby/storage/machines.py::parse_node_ref` resolves a node ref, and
  `machines.owner_user_id` names the owner.
- `src/gobby/storage/project_checkouts.py::require_root(db, project_id, machine_id)`
  raises `CheckoutNotFoundError` when no checkout row exists.

**Implementation:**
- `node_client.py`:
  - `NodeCommandClient.from_environment()` reads `GOBBY_FRONT_DOOR_SECRET` and
    `load_bootstrap().daemon_port`.
  - `send(machine_id, command, payload, timeout_ms)` posts to
    `http://127.0.0.1:<port>/api/internal/nodes/{machine_id}/commands` with
    `X-Gobby-Backend-Secret`. It parses the reply with the 1.1 models, returns
    the result on `ok`, and raises `NodeCommandError(code, message, stage)`
    otherwise.
  - `connected_machine_ids()` reads `GET /api/internal/nodes/connected`.
  - Without the secret, both raise `NodeCommandError("node_routing_unavailable")`.
  - A module-level `node_command_client()` caches one instance.
- `machine_routing.py`:
  - `is_local_machine(machine_id)` is true for `None` or this machine's id.
  - `resolve_target_machine(db, *, project_id, requested, origin_machine_id)`
    takes `requested` (an id or node ref) or else `origin_machine_id`. It returns
    `RoutedMachine(machine_id, local, checkout_root)`. For a local target it
    returns `local=True` with no further checks.
  - A remote target must have a `project_checkouts` row (`checkout_unresolved`),
    be in `connected_machine_ids()` (`node_offline`), and share `owner_user_id`
    with the origin machine (`machine_not_owned`).
  - An unknown ref raises `machine_not_found`. Every refusal is a
    `NodeCommandError`; nothing falls back.

Verification planned:
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/hub/test_node_client.py tests/hub/test_machine_routing.py -v`.

**Acceptance:**

- 1.3.1 - `send` posts the secret header and maps an ok reply to its result and an error reply to `NodeCommandError` with code and stage. test: `tests/hub/test_node_client.py::test_send_maps_replies`.
- 1.3.2 - Without `GOBBY_FRONT_DOOR_SECRET`, calls raise `node_routing_unavailable` and make no request. test: `tests/hub/test_node_client.py::test_missing_secret_is_typed`.
- 1.3.3 - The origin machine is the default target, and a node ref resolves to its machine. test: `tests/hub/test_machine_routing.py::test_default_and_explicit_targets`.
- 1.3.4 - A remote target without a checkout, with no live channel, or with another owner is refused with its typed code and never replaced by another machine. test: `tests/hub/test_machine_routing.py::test_ineligible_targets_never_fall_back`.

## P2: Machine-correct hub loops and remote callers
`kind: framing`

**Goal**: remote rows are safe from local loops, and spawn, cancel, and terminal
write reach the owning node.

### 2.1 Hub machine-local loops skip other machines' rows [category: code]
`kind: deliverable`

Targets:
- `src/gobby/storage/terminals.py::TerminalManager.list_stale_pending`
- `src/gobby/storage/terminal_reap_queries.py`
- `src/gobby/storage/agents/_queries.py::_AgentRunQueryMixin.list_terminal_with_tmux`
- `src/gobby/agents/terminal_cleanup.py::TerminalResourceCleaner.cleanup_terminal_tmux_sessions`
- `src/gobby/agents/terminal_cleanup.py::TerminalResourceCleaner.post_terminal_cleanup`
- `src/gobby/agents/interactive_attention_monitor.py::InteractiveAttentionMonitor._list_interactive_sessions`
- `tests/storage/test_agents_terminal_migration.py::test_storage_readers_use_terminal_rows`
- `tests/storage/test_agents_terminal_migration.py::test_orphaned_terminal_rows_stay_eligible_for_the_sweep`
- `tests/agents/test_hub_local_loops.py`

**Research context:**
- `src/gobby/agents/spawn_executor.py::reap_stale_pending_terminals` iterates
  `TerminalManager.list_stale_pending`. That query has no machine filter, and
  the reaper kills local spawn keys. A remote pending terminal would be reaped
  while its node launches. The same module already compares rows against
  `require_machine_id()` (`attach_locator_for_terminal`), and its storage tests
  pin the local machine and create rows on it.
- `src/gobby/storage/terminals.py` is 933 lines. `TerminalManager` already
  inherits `TerminalSettlementMixin` from `src/gobby/storage/terminal_settlement.py`.
- `list_terminal_with_tmux` selects terminal runs with pending, live, or orphaned
  terminals on every machine, and `cleanup_terminal_tmux_sessions` closes a
  local tmux session for each. Remote rows would also fill its 100-row window
  forever, because the hub never settles them.
- `post_terminal_cleanup` runs these phases: `delivery`, `attention`, `tmux`,
  `srt_reap`, `sandbox_reap`, `ownership_runtime`, and `merge_artifacts`. The
  `tmux`, `srt_reap`, and `sandbox_reap` phases act on local processes and
  panes.
- `_list_interactive_sessions` pages every live interactive session through
  `session_manager.list`, which already accepts `machine_id`, and the monitor
  captures each one's local pane.
- `AgentRun.machine_id` defaults to `require_machine_id()`, so test runs built
  without one are local.
- Already scoped, so left alone: `AgentHealthMonitor._get_active_terminal_runs`
  (`list_active_for_machine(require_machine_id())`), clone storage queries, and
  `InteractiveAttentionMonitor._list_active_runs`.

**Implementation:**
- Split: the reap queries `list_stale_pending`, `list_orphaned_by_epoch`,
  `list_live_by_epoch`, `list_live_by_machine`, and
  `list_reconcilable_by_machine` move from `TerminalManager` in
  `src/gobby/storage/terminals.py` into `TerminalReapQueryMixin` in
  `src/gobby/storage/terminal_reap_queries.py`,
  modeled on `TerminalSettlementMixin`. `TerminalManager` inherits it, so every
  caller keeps calling them on the manager.
- `list_stale_pending` adds `AND machine_id = %s` bound to
  `require_machine_id()`. Its signature is unchanged.
- `list_terminal_with_tmux` gains a required `machine_id` argument and filters
  `ar.machine_id = %s`. `cleanup_terminal_tmux_sessions` passes
  `require_machine_id()`. The migration test's four calls pass its pinned
  `LOCAL_MACHINE_ID`.
- `post_terminal_cleanup` skips its `tmux`, `srt_reap`, and `sandbox_reap` phases
  when `run.machine_id` is not this machine's.
- `_list_interactive_sessions` passes `machine_id=require_machine_id()` to
  `session_manager.list`.

Consumers unchanged:
- `src/gobby/agents/spawn_executor.py` — no-edit-reason: `reap_stale_pending_terminals` calls `list_stale_pending(in_doubt_seconds)` with the unchanged signature and receives only local rows.
- `tests/storage/test_terminals.py` — no-edit-reason: it pins the local machine and creates its stale rows with `require_machine_id()`, so the filtered query returns the same rows; re-run as verification.
- `tests/terminals/test_in_doubt_kill_truth.py` — no-edit-reason: its store defines its own `list_stale_pending`; the SQL filter does not reach it.
- `src/gobby/agents/agent_cleanup.py` — no-edit-reason: it delegates to `cleanup_terminal_tmux_sessions()` and `post_terminal_cleanup(...)`, whose signatures are unchanged.
- `tests/agents/test_terminal_cleanup.py` — no-edit-reason: its `_run()` runs default to the local machine, so every phase still runs; re-run as verification.
- `tests/agents/test_interactive_attention_monitor.py` — no-edit-reason: its fake `list` takes `**kwargs` and asserts only the cursor, so the added `machine_id` keyword passes through.

Verification planned:
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/agents/test_hub_local_loops.py tests/agents/test_terminal_cleanup.py tests/agents/test_interactive_attention_monitor.py tests/storage/test_terminals.py tests/storage/test_agents_terminal_migration.py -v`.

**Acceptance:**

- 2.1.1 - The stale-pending reaper leaves another machine's pending terminal untouched and still reaps a local one. test: `tests/agents/test_hub_local_loops.py::test_reaper_skips_remote_pending`.
- 2.1.2 - Terminal tmux cleanup lists only this machine's runs. test: `tests/agents/test_hub_local_loops.py::test_tmux_cleanup_is_machine_scoped`.
- 2.1.3 - Post-terminal cleanup of a remote run runs the delivery, attention, and ownership phases and skips tmux, SRT, and sandbox reaping. test: `tests/agents/test_hub_local_loops.py::test_post_cleanup_skips_local_phases_for_remote_run`.
- 2.1.4 - The interactive attention monitor ignores another machine's interactive session. test: `tests/agents/test_hub_local_loops.py::test_interactive_monitor_is_machine_scoped`.

### 2.2 Hook ingress binds the envelope machine to the caller's key [category: code]
`kind: deliverable`

Targets:
- `src/gobby/servers/routes/mcp/hooks.py::create_hooks_router`
- `tests/servers/test_hook_machine_binding.py`

**Research context:**
- `execute_hook` is nested in `create_hooks_router` and serves
  `POST /api/hooks/execute`. Adapters take `machine_id` from the envelope's
  `input_data` (`src/gobby/adapters/claude_code.py`), and session registration
  stores it (`require_hook_machine_id`).
- `server.auth_service.verified_front_door_identity(request)` returns
  `FrontDoorIdentity(user_id, machine_id, key_id)` only for a key-authenticated
  request that came through gdaemon (`src/gobby/servers/auth_service.py`).

**Implementation:**
- After parsing the body, `execute_hook` reads the verified identity. When it is
  present and `input_data.machine_id` is present and differs, the hook answers
  403 `{"code":"hook_machine_mismatch"}` before any adapter runs. A request
  without an identity (local ghook on the hub's own machine) is unchanged.

Consumers unchanged:
- `src/gobby/servers/_app_routes.py` — no-edit-reason: it mounts `create_hooks_router(server)`, whose signature is unchanged.
- `src/gobby/servers/routes/mcp/__init__.py` — no-edit-reason: it re-exports `create_hooks_router` by name.
- `tests/servers/routes/mcp_endpoints/test_execution_session_end_cleanup.py` — no-edit-reason: its hook requests carry no front-door identity, so they take the unchanged path; re-run as verification.

Verification planned:
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/test_hook_machine_binding.py -v`.

**Acceptance:**

- 2.2.1 - A key-authenticated hook whose envelope names another machine is refused with `hook_machine_mismatch`, and one naming the key's machine is processed. test: `tests/servers/test_hook_machine_binding.py::test_envelope_machine_must_match_key`.
- 2.2.2 - A hook without a front-door identity keeps today's behavior. test: `tests/servers/test_hook_machine_binding.py::test_local_hook_unchanged`.

### 2.3 Remote terminal writes go through the node [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `src/gobby/terminals/write_coordinator.py::WriteCoordinator._dispatch`
- `src/gobby/hub/remote_terminal.py`
- `tests/terminals/fakes.py::*` — scope-reason: only `make_memory_terminal` changes, gaining a `machine_id` keyword whose default is the local machine; its callers keep their calls and get local terminals.
- `tests/hub/test_remote_terminal_write.py`

**Research context:**
- Every terminal write passes `WriteCoordinator.write`, then `_write_locked`
  (idempotency, latches, lease checks, all hub-side), then `_dispatch`.
  `_dispatch` resolves `runtime_for(terminal)` and calls `write_text`,
  `write_key`, `write_input`, or `write_paste`. Callers include attention
  respond, `send_keys`, and message wakes.
- `_dispatch` turns host refusals into `TerminalWriteError(stage=...)`, and
  `_write_locked` clears the latch on `stage="none"`.
- `tests/terminals/fakes.py::make_memory_terminal`, used by about fifty test
  files, gives every fake terminal a random `machine_id`. Under a local-machine
  check those terminals would all read as remote.

**Implementation:**
- `_dispatch` records the composer-ledger observation as today. Then, when
  `terminal.machine_id` is not local, it returns
  `remote_terminal.write(terminal, request)` and skips `runtime_for`.
- `remote_terminal.write` sends `terminal.write` and returns `Delivered()`.
  Node errors map to `TerminalWriteError(stage=<reply stage or "none">)`.
  `node_offline`, `node_timeout`, and `channel_closed` raise
  `NodeTerminalUnavailable`, a `TerminalWriteError` subclass with
  `stage="none"` that carries the code.
- `client_fd` input never targets a remote terminal; a remote request with
  `client_fd` raises `TerminalWriteError(stage="none")`.
- `make_memory_terminal` defaults `machine_id` to `require_machine_id()` and
  takes an optional `machine_id` keyword for remote cases.

Consumers unchanged:
- `src/gobby/terminals/pane_io.py` — no-edit-reason: its `_dispatch` methods belong to its own classes and do not call `WriteCoordinator._dispatch`.
- `tests/e2e/composer_proof_runner.py` — no-edit-reason: it wraps `WriteCoordinator._dispatch` and forwards the unchanged arguments; local terminals take the unchanged path.
- `tests/e2e/test_build_dispatcher_autonomy.py` — no-edit-reason: it calls `gobby.hooks.event_handlers._dispatch`, an unrelated module.

Verification planned:
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/hub/test_remote_terminal_write.py tests/terminals/test_write_coordinator.py tests/terminals/test_write_input.py tests/terminals/test_write_outcomes.py tests/servers/test_attention_respond.py -v`.

**Acceptance:**

- 2.3.1 - A write to another machine's terminal sends one `terminal.write` with the request's kind, data, and submit, and returns `Delivered`. test: `tests/hub/test_remote_terminal_write.py::test_remote_write_uses_node_command`.
- 2.3.2 - An offline node raises `NodeTerminalUnavailable` with `node_offline`, and the latch is cleared. test: `tests/hub/test_remote_terminal_write.py::test_offline_node_clears_latch`.
- 2.3.3 - A partial node failure surfaces `stage="partial"`. test: `tests/hub/test_remote_terminal_write.py::test_partial_stage_propagates`.
- 2.3.4 - A local terminal write never contacts the client. test: `tests/hub/test_remote_terminal_write.py::test_local_write_unchanged`.

### 2.4 Remote cancel goes through the node [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `src/gobby/agents/kill.py::kill_agent`
- `src/gobby/hub/remote_cancel.py`
- `tests/hub/test_remote_cancel.py`

**Research context:**
- `kill_agent(run, db, *, signal_name, timeout, close_terminal, terminal_action, terminal_reason, terminal_services)`
  signals a local PID and closes a managed terminal (`_close_managed_terminal`).
  It returns `{"success", "message", "method", ...}` or
  `{"success": False, "error", "error_code"}` and leaves run status to its
  callers. Callers include the MCP and CLI `kill_agent`, the dispatcher, and the
  watchdog.

**Implementation:**
- At the top of `kill_agent`, a run whose `machine_id` is set and not local
  returns `remote_cancel.cancel(run, db, signal_name=..., close_terminal=..., terminal_action=..., terminal_reason=...)`.
  `AgentRun.machine_id` defaults to `require_machine_id()`, so existing local
  runs never reach the guard.
- `cancel` sends `agent.cancel`. A `stopped` reply returns
  `{"success": True, "method": "node_command", "message": ...}`. When
  `close_terminal` is set, it settles the terminal row through the same
  `TerminalManager` transition that `_close_managed_terminal` applies after a
  local close.
- An error reply returns `{"success": False, "error": <message>, "error_code": <code>}`.

Consumers unchanged:
- `src/gobby/agents/lifecycle_monitor.py` — no-edit-reason: it kills runs it lists with `require_machine_id()`, which are local; signature and result keys are unchanged.
- `src/gobby/agents/memory_watchdog.py` — no-edit-reason: it kills runs from `list_active_for_machine(require_machine_id())`, which are local.
- `src/gobby/build/control_runtime.py` — no-edit-reason: it kills local build agents (Decision 11) and only logs a result whose `success` is false.
- `src/gobby/dispatch/spawn_actions.py` — no-edit-reason: dispatch spawns are local (Decision 11); the call is unchanged.
- `src/gobby/mcp_proxy/tools/agent_cancellation.py` — no-edit-reason: it injects `kill_agent` as the process killer; a remote run takes the node path inside it and returns the same dict shape.
- `src/gobby/mcp_proxy/tools/agents.py` — no-edit-reason: it imports `kill_agent` as `_kill_agent_process` and passes it through unchanged.
- `src/gobby/mcp_proxy/tools/workflows/_pipeline_execution.py` — no-edit-reason: it calls `kill_agent(run, db, signal_name="KILL")` per pipeline run; a remote run takes the node path inside it.
- `src/gobby/servers/routes/agents.py` — no-edit-reason: it returns the result dict as `result`, whose shape is unchanged.
- `tests/agents/test_kill.py` — no-edit-reason: its runs default to the local machine and take the unchanged path; re-run as verification.

Verification planned:
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/hub/test_remote_cancel.py tests/agents/test_kill.py -v`.

**Acceptance:**

- 2.4.1 - Killing another machine's run sends `agent.cancel` with the signal and `close_terminal`, and never signals a local PID. test: `tests/hub/test_remote_cancel.py::test_remote_run_cancels_through_node`.
- 2.4.2 - An offline node returns `error_code: node_offline`. test: `tests/hub/test_remote_cancel.py::test_offline_node_is_typed`.
- 2.4.3 - With `close_terminal`, a stopped reply settles the terminal row as a local close would. test: `tests/hub/test_remote_cancel.py::test_close_terminal_settles_row`.

### 2.5 Remote spawn goes through the node [category: code] (depends: 1.3, 2.1, 2.2, 2.4)
`kind: deliverable`

Targets:
- `src/gobby/hub/remote_spawn.py`
- `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py::create_spawn_agent_registry`
- `src/gobby/servers/routes/agent_spawn.py::AgentSpawnRequest`
- `src/gobby/servers/routes/agent_spawn.py::create_agent_spawn_router`
- `tests/hub/test_remote_spawn.py`

**Research context:**
- `spawn_agent_impl` (`src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py`,
  988 lines) has five callers. Two are request-facing: the MCP tool
  (`create_spawn_agent_registry` nests `spawn_agent`) and `_do_spawn` in
  `create_agent_spawn_router`. The others are hub-originated: dispatch
  (`src/gobby/dispatch/spawn.py`), cron (`src/gobby/scheduler/executor.py`), and
  feedback (`src/gobby/feedback/agent.py`). `_do_spawn` resolves
  `require_root(db, project_id, require_machine_id())` before calling it.
- `src/gobby/agents/spawn.py::prepare_terminal_spawn(session_manager, parent_session_id, project_id, machine_id, …, prompt, …, workspace_path, credential_manager, config_snapshot)`
  pre-registers the child session and run. `credential_manager=None` issues no
  managed credential, and `prompt=None` writes no prompt file.
- `src/gobby/agents/spawn_executor_providers.py::ProviderSpawnPlan` is what the
  local flow builds before `_runtime_spawn`. A remote spawn never builds it;
  the node does (#24148).
- The requesting session's machine is `sessions.machine_id`, bound to its key
  by 2.2. A `POST /api/agents/spawn` without a parent session uses the verified
  front-door identity's machine, or else this machine.

**Implementation:**
- Both callers gain an optional `machine` (id or node ref) and call
  `resolve_target_machine` with the origin machine. A local result follows
  today's path unchanged; a remote result calls `spawn_remote_agent`, and a
  refusal returns its typed code as the spawn error.
- `spawn_remote_agent` refuses `resume_session_id` with
  `remote_resume_unsupported` and a placement request with
  `remote_placement_unsupported`. Otherwise:
  1. It runs the same agent-body, provider, model, and depth checks, the
     `runner.can_spawn` gate, and the task spawn lease as the local flow.
  2. With isolation, it sends `workspace.prepare` with the node's
     `checkout_root` as `source_root`.
  3. It calls `prepare_terminal_spawn` with `machine_id=<target>`,
     `prompt=None`, `credential_manager=None`, and the node workspace path.
  4. It creates the pending terminal row with `machine_id=<target>` and sends
     `agent.spawn`. `env` is the allowlisted subset of
     `PreparedSpawn.env_vars`.
  5. On success it records the PID and marks the run running, and stores
     `remote_workspace: {machine_id, mode, path, branch_name}` in
     `resume_metadata_json`. It writes no `worktrees` or `clones` row.
  6. On any failure it releases the lease, fails the run through
     `cleanup_failed_spawn` without local isolation cleanup, and sends
     `workspace.remove` for a workspace step 2 created.
- `AgentSpawnRequest` and the MCP `spawn_agent` tool each gain an optional
  `machine: str | None`. Batch requests inherit it through `spawns`.
- The origin machine is `non_empty_string(getattr(parent_session, "machine_id", None))`.
  An absent value counts as this machine, the rule
  `require_local_machine_id` already applies.
- A request whose origin is another machine and whose target resolves to this
  machine is refused with `remote_parent_local_target_unsupported` before the
  local path runs. `_parent_session_project_context` stays unchanged, so the
  local path sees only local parents.

Consumers unchanged:
- `src/gobby/mcp_proxy/tools/agents_spawn_tools.py` — no-edit-reason: it registers `create_spawn_agent_registry(...)`, whose signature is unchanged.
- `src/gobby/servers/_app_routes.py` — no-edit-reason: it mounts `create_agent_spawn_router(server)`, whose signature is unchanged.
- `src/gobby/servers/routes/__init__.py` — no-edit-reason: it re-exports `create_agent_spawn_router` by name.
- `tests/config/test_live_policy_consumers.py` — no-edit-reason: it inspects the router's launch methods, which are unchanged.
- `tests/agents/test_backend_ingress.py` — no-edit-reason: it asserts `terminal_backend` is absent and rejected; an added optional `machine` field changes neither.
- `tests/mcp_proxy/tools/spawn_agent/test_execution.py` — no-edit-reason: its spawns omit `machine` and take the unchanged local path; re-run as verification.
- `tests/mcp_proxy/tools/spawn_agent/test_factory.py` — no-edit-reason: it asserts named schema properties, and an added optional property leaves them intact.
- `tests/mcp_proxy/tools/spawn_agent/test_fallback_agent.py` — no-edit-reason: its spawns omit `machine` and take the unchanged local path.
- `tests/mcp_proxy/tools/spawn_agent/test_initial_variables.py` — no-edit-reason: its spawns omit `machine` and take the unchanged local path.
- `tests/mcp_proxy/tools/spawn_agent/test_mcp_proxy_tools_spawn_agent_dedup.py` — no-edit-reason: its spawns omit `machine` and take the unchanged local path.
- `tests/mcp_proxy/tools/spawn_agent/test_placement.py` — no-edit-reason: its placed spawns omit `machine` and take the unchanged local path.
- `tests/mcp_proxy/tools/spawn_agent/test_project_scope.py` — no-edit-reason: its spawns omit `machine` and take the unchanged local path.
- `tests/mcp_proxy/tools/spawn_agent/test_seat_adoption.py` — no-edit-reason: its spawns omit `machine` and take the unchanged local path.
- `tests/mcp_proxy/tools/spawn_agent/test_spawn_project_context.py` — no-edit-reason: its spawns omit `machine` and take the unchanged local path.
- `tests/mcp_proxy/tools/spawn_agent/test_spawn_scope_proxy.py` — no-edit-reason: its spawns omit `machine` and take the unchanged local path.
- `tests/mcp_proxy/tools/spawn_agent/test_spawn_worktree_reference_resolution.py` — no-edit-reason: its spawns omit `machine` and take the unchanged local path.
- `tests/mcp_proxy/tools/tasks/test_lifecycle_close_orchestration.py` — no-edit-reason: its spawns omit `machine` and take the unchanged local path.
- `tests/mcp_proxy/tools/test_parallel_dispatch.py` — no-edit-reason: its spawns omit `machine` and take the unchanged local path.
- `tests/mcp_proxy/tools/test_spawn_agent_resume.py` — no-edit-reason: its resumes omit `machine` and take the unchanged local path.
- `tests/skills/test_reference_library.py` — no-edit-reason: it validates documented launch arguments against the schema, and an added optional property accepts them.

Verification planned:
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/hub/test_remote_spawn.py tests/mcp_proxy/tools/spawn_agent/ tests/servers/routes/test_agent_spawn_routes.py tests/agents/test_backend_ingress.py -v`.

**Acceptance:**

- 2.5.1 - A spawn from a session on another machine sends `workspace.prepare` and then `agent.spawn` to that machine, and the run and session rows carry its `machine_id`. test: `tests/hub/test_remote_spawn.py::test_spawn_routes_to_requesting_machine`.
- 2.5.2 - The `agent.spawn` payload carries no managed credential and no env key outside the allowlist, and the hub writes no prompt file. test: `tests/hub/test_remote_spawn.py::test_payload_carries_no_credential`.
- 2.5.3 - An explicit `machine` is validated, and an ineligible one fails with its typed code without spawning anywhere. test: `tests/hub/test_remote_spawn.py::test_explicit_machine_is_validated`.
- 2.5.4 - An `agent.spawn` failure releases the lease, fails the run, and sends `workspace.remove` for the workspace it prepared. test: `tests/hub/test_remote_spawn.py::test_failed_spawn_rolls_back`.
- 2.5.5 - Remote resume and placement are refused with typed codes, and so is a request from another machine's session that targets this machine. test: `tests/hub/test_remote_spawn.py::test_unsupported_routes_refused`.
- 2.5.6 - A spawn whose target is this machine takes the local path unchanged. test: `tests/hub/test_remote_spawn.py::test_local_target_unchanged`.

## P3: Machine-aware roster and attention
`kind: framing`

**Goal**: every machine's agents and attention reach the client, and a response
reaches the owning machine.

### 3.1 Hub-wide roster with each entry's machine [category: code]
`kind: deliverable`

Targets:
- `src/gobby/storage/attention.py::AttentionStateManager.load_roster_rows`
- `src/gobby/storage/attention.py::AttentionRosterRow`
- `src/gobby/servers/routes/attention_roster.py::_load_roster_entries`
- `src/gobby/servers/routes/attention.py::create_attention_router`
- `tests/servers/test_attention_roster.py::test_bounded_roster_query_joins_task_payload`
- `tests/servers/test_attention_roster.py::test_running_roster_projects_child_lifecycle_and_seat_title`
- `tests/servers/test_attention_roster.py::test_roster_recovers_older_seat_titles_without_definition_title_variable`
- `tests/servers/test_attention_roster_machines.py`

**Research context:**
- `load_roster_rows(machine_id, *, live_session_statuses)` unions runs filtered
  by `run.machine_id = %s` with live sessions filtered only by status. `roster()`
  in `create_attention_router` passes `require_machine_id()`, and caches the
  payload by `(epoch, seq)`.
- `_load_roster_entries` builds the entries, which carry no machine today.
- gclient currently derives an agent's machine from its session or run rows,
  fetched per project with `limit=50`.

**Implementation:**
- `load_roster_rows` drops its `machine_id` parameter and the run filter. Both
  halves select `machine_id`: `run.machine_id` for runs and `session.machine_id`
  for sessions. `AttentionRosterRow` gains a last field
  `machine_id: str | None = None`, so keyword constructors stay valid.
- `_load_roster_entries` emits `machine_id` on every entry.
- `roster()` stops passing `require_machine_id()`.
- `tests/servers/test_attention_roster.py` drops the `require_machine_id()`
  argument from its three direct `load_roster_rows` calls.

Consumers unchanged:
- `tests/agents/test_attention_metadata.py` — no-edit-reason: it patches `load_roster_rows` with `lambda *_args, **_kwargs` and builds `AttentionRosterRow` by keyword; the defaulted field keeps both valid.
- `src/gobby/servers/_app_routes.py` — no-edit-reason: it mounts `create_attention_router(server)`, whose signature is unchanged.
- `src/gobby/servers/routes/__init__.py` — no-edit-reason: it re-exports `create_attention_router` by name.
- `tests/servers/test_attention_native_roster.py` — no-edit-reason: its rows are on the local machine, so the hub-wide roster returns the same entries plus `machine_id`; re-run as verification.
- `tests/servers/test_attention_respond.py` — no-edit-reason: it exercises `respond`, which 3.1 does not change.

Verification planned:
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/test_attention_roster_machines.py tests/servers/test_attention_roster.py tests/servers/test_attention_native_roster.py tests/agents/test_attention_metadata.py tests/storage/test_attention.py -v`.

**Acceptance:**

- 3.1.1 - The roster lists live runs and sessions from every machine. test: `tests/servers/test_attention_roster_machines.py::test_roster_is_hub_wide`.
- 3.1.2 - Every entry carries the machine of its run or session. test: `tests/servers/test_attention_roster_machines.py::test_entries_carry_machine_id`.

### 3.2 gclient attributes agents by the roster's machine [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `crates/gclient/src/daemon/roster.rs::RosterEntry`
- `crates/gclient/src/app/sidebar_model.rs::build_agents`
- `crates/gclient/tests/roster_machines.rs`

**Research context:**
- `build_agents` sets each agent's `machine_id` from its session, then its run,
  then the local machine. `machine_rows` rolls up agent states per machine, and
  the machine filter defaults to the local machine (Decision 10).

**Implementation:**
- `RosterEntry` gains `machine_id: Option<String>`.
- `build_agents` prefers a non-empty `entry.machine_id`, then keeps today's
  fallbacks.

Verification planned:
- `cargo nextest run -p gobby-client -E 'binary(roster_machines)'`.
- `cargo clippy -p gobby-client` and `cargo fmt -p gobby-client -- --check`.

**Acceptance:**

- 3.2.1 - An agent whose session row is outside the fetched window is listed under its roster machine, and the agents view still defaults to the local machine. test: `crates/gclient/tests/roster_machines.rs::agents_use_roster_machine`.
- 3.2.2 - A remote machine's row shows attention when one of its agents needs attention. test: `crates/gclient/tests/roster_machines.rs::remote_machine_row_shows_attention`.

### 3.3 Attention respond on another machine's entry [category: code] (depends: 2.3, 3.1)
`kind: deliverable`

Targets:
- `src/gobby/servers/routes/attention.py::_resolve_attention_pane`
- `src/gobby/servers/routes/attention.py::create_attention_router`
- `tests/servers/test_attention_remote_respond.py`

**Research context:**
- `respond` (nested in `create_attention_router`) resolves the pane, validates
  the answer, and injects it through `inject_answer`, which calls
  `write_coordinator.write(WriteRequest(terminal_id=pane.target, action_key=f"attention-respond:{attention_id}", origin="attention", …))`.
  It then redetects through the pane's `capture`.
- `_resolve_attention_pane` reads the terminal row with
  `TerminalManager.get_live_for_session` or the run's `terminal_id`; neither is
  machine-filtered. Its `capture` snapshots through the local runtime.
- 2.3 routes the write to the owning node.

**Implementation:**
- For a row whose `machine_id` is not local, `_resolve_attention_pane` returns
  `AttentionPane(target=row.id, capture=<returns None>)`.
- `respond` skips local redetection for a remote row; the hook-driven lifecycle
  settles the state (Q6).
- It maps `NodeTerminalUnavailable` to 503 with the reply's code, such as
  `{"code":"node_offline"}`.

Consumers unchanged:
- `src/gobby/servers/_app_routes.py` — no-edit-reason: it mounts `create_attention_router(server)`, whose signature is unchanged.
- `src/gobby/servers/routes/__init__.py` — no-edit-reason: it re-exports `create_attention_router` by name.
- `tests/agents/test_attention_metadata.py` — no-edit-reason: it reads the roster only, which 3.3 does not change.
- `tests/servers/test_attention_native_roster.py` — no-edit-reason: it reads the roster only, which 3.3 does not change.
- `tests/servers/test_attention_respond.py` — no-edit-reason: its terminals come from `make_memory_terminal`, which 2.3 makes local, so respond keeps the local path; re-run as verification.

Verification planned:
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/test_attention_remote_respond.py tests/servers/test_attention_respond.py -v`.

**Acceptance:**

- 3.3.1 - Responding to another machine's entry sends one `terminal.write` with the answer to that machine's terminal. test: `tests/servers/test_attention_remote_respond.py::test_remote_respond_writes_through_node`.
- 3.3.2 - An offline owner node yields 503 `node_offline`, and the entry stays open. test: `tests/servers/test_attention_remote_respond.py::test_offline_owner_is_typed`.
- 3.3.3 - A remote entry never snapshots a local pane. test: `tests/servers/test_attention_remote_respond.py::test_remote_entry_skips_local_capture`.

## D1 Remote workspace lifecycle (depends: 2.5)
`kind: deferred`

A remote spawn records its workspace only in the run's resume metadata
(Decision 15), and removes it only when the spawn fails. Listing finished remote
workspaces, merging their branches, and removing them needs machine-scoped
`worktrees` rows (#24149, S2.7 hub half) and a way to move commits between
machines.

Acceptance item D1.1: the hub lists, merges, and removes finished remote
workspaces through their owning node.

```yaml
deferral:
  task_ref: "TBD-remote-workspace-lifecycle"
  reason: "Needs machine-scoped worktree rows (#24149) and cross-machine git transport, neither in Python hub routing; created at expansion per the plan-coverage contract."
  owner: "orchestrator"
  original_acceptance_items:
    - D1.1
```

## V1: Plan Changelog
`kind: framing`

- 2026-10-12: First draft from Josh's Q0-Q6 answers, relayed by gobby#15977.

## V2: Verification
`kind: verification`

These are completion gates for the implementation. Except for plan validation,
none has run yet. Each must pass after its leaves land and before #21575 closes.

```bash
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/hub/ tests/agents/test_hub_local_loops.py tests/servers/test_hook_machine_binding.py tests/servers/test_attention_roster_machines.py tests/servers/test_attention_remote_respond.py -v
cargo nextest run -p gobby-daemon -E 'binary(node_command_corpus) | binary(node_commands)'
cargo nextest run -p gobby-client -E 'binary(roster_machines)'
uv run ruff format --check src/ && uv run ruff check src/ && uv run mypy src/
uv run gobby plans validate .gobby/plans/hub-mode.md -p .
```

- The plan validation command must pass at every revision; it is the only check
  that runs during planning.
- The end-to-end hub-to-node path must pass #24148's acceptance once its node
  handlers land; this plan's tests drive a fake node.
- Run these from the worktree root. Do not run the full pytest suite.
