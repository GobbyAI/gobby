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

When the active sections close:
- A versioned hub-to-node command envelope and its loopback gdaemon route are a
  committed contract with a golden corpus.
- The Python hub sends a spawn, a cancel, or a terminal write aimed at another
  machine to that machine's node through the route's client, or fails with a
  typed code. It never runs the work on the hub instead.
- The hub settles each remote run from the owning node's inventory.
- The roster lists every machine's agents, each tagged with its machine and
  project, and every machine's row in gclient carries its attention state.
- Hub loops that touch local panes, processes, or files act only on their own
  machine's rows.

Four sections are deferred. D1 holds the lifecycle of a finished remote
workspace. D2 builds the gdaemon route once #24090 (Node channel) lands. D3
holds the machine-scoped project view. D4 is the live proof.
Until D2 lands, the client refuses every remote request with
`node_routing_unavailable`, and every test here drives a fake route or a fake
node. The node handlers are #24148 (S2.7 node half: node-local agent spawn and
worktrees in gdaemon). A node agent's hooks reach the hub through #24150 (S2.11
node half: the node-local envelope ledger forwards each envelope to the hub).
#24149 (S2.7 hub half) later redoes the Python callers in Rust and reuses the
envelope and the route.

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
   reports its channel live, read through the internal route (1.1 contract, D2).
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

10. **Q4 implementation.** Josh ruled on the stamped plan, verbatim: "gclient
    already has the option to display machines. Shouldn't it default to showing
    the current machine since the user can always switch to showing all
    machines?" gclient already has a per-machine view:
    - `crates/gclient/src/ui/sidebar/machines.rs::machine_rows` draws one row per
      machine with a state dot that rolls up its agents' states, attention
      included. `crates/gclient/src/app/sidebar_model.rs::build` lists every
      machine an agent runs on plus the local one.
    - `crates/gclient/src/ui/chrome/sidebar_state.rs` keeps `machine_filter`:
      `None` lists the local machine, `ALL_MACHINES` every machine, and a click
      on a machine row selects it. `SidebarState::default` sets
      `ALL_MACHINES`, so 3.2 changes the default to `None`, the current machine.
    - The local machine is the client's own (`crates/gclient/src/views/mod.rs`
      calls `read_local_machine_id`).

    It filters one roster on the client. The hub side is the gap:
    `AttentionStateManager.load_roster_rows` keeps only the hub machine's runs,
    and entries carry no machine or project. So 3.1 makes the roster hub-wide
    with each entry's machine and project, and 3.2 has gclient read both and
    default to the current machine. One payload keeps gclient's subscribe-first
    reconciliation intact, because attention broadcasts are hub-wide. A
    per-machine summary endpoint plus a selected-machine roster would need
    machine-scoped broadcasts and a refetch on every switch.

    Josh's ruling settles listing: the machine rows above are the machine list,
    so no `/api/machines` consumer is added. The projects pane does not follow
    the machine filter. Its cards show one checkout's branch and ahead/behind
    counts from the daemon's project status, and no v1 command reports a node's
    git state, so D3 holds that view for a planning pass.
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
    `POST /api/agents/spawn`) choose the remote flow. The origin machine is the
    calling session's, so a node agent that reports to a hub coordinator spawns
    on its node. The parent session is only the reporting relationship. Dispatch,
    cron, and feedback spawns stay local (decision 11). A remote spawn refuses
    `resume` and placement with typed errors: resume needs the node's provider
    thread files, and placement is a gclient-local pane feature.
14. **No datastore credential crosses to a node** (ROADMAP decision 17). A remote
    spawn issues no managed credential and writes no prompt file on the hub. The
    `agent.spawn` env carries only allowlisted run-identity variables.
15. **A remote workspace is a `worktrees` or `clones` row with the node's
    machine.** Both tables already carry `machine_id uuid NOT NULL`, with unique
    indexes on `(machine_id, worktree_path)`, `(project_id, branch_name,
    machine_id)`, and `(machine_id, clone_path)`. Every `LocalWorktreeManager`
    and `LocalCloneManager` method binds `require_machine_id()`, so the hub's
    workspace loops already skip a node's rows (2.1.5 pins it). Those managers
    stay this-machine-only. The remote flow is the only writer of another
    machine's workspace rows, so `src/gobby/hub/remote_workspaces.py` holds its
    insert, lookup, and delete and reads rows with `Worktree.from_row` and
    `Clone.from_row` (2.5). A machine keyword on the managers would instead
    widen every consumer's contract and force a split of the 852-line
    `src/gobby/storage/worktrees.py`. A remote spawn removes its workspace with
    `workspace.remove` only once the spawn has provably failed (2.5, 2.6).
    Before that failure settles the run or the terminal, the row is marked for
    removal with the tables' existing statuses, `abandoned` for a worktree and
    `cleanup` for a clone. 2.6 retries every marked row of a connected machine
    until the node answers `{removed: true}`, so the row keeps an owner after
    its run and terminal settle and across a hub restart. Listing, merging, and
    removing finished remote workspaces is D1.
16. **Hook machine binding.** Q3 trusts `sessions.machine_id`, which comes from
    the hook envelope (`ClaudeCodeAdapter` reads `input_data["machine_id"]`).
    When a hook request carries a verified front-door identity, the hub rejects
    an envelope whose `machine_id` differs from the key's machine (2.2).
17. **#24090 dependency.** The gdaemon route extends the node channel's registry
    and handler, which #24090 (Node channel: hub registers, heartbeats, replaces,
    revokes) creates. Per the plan-coverage contract, work gated on another task
    is a typed deferral, so the route is deferred section D2 (Orchestrator ruling,
    2026-10-12). 1.1 specifies the route's HTTP contract, so 1.3 builds its client
    against it and tests it against a fake route. Until D2 lands, the client
    refuses every remote request with `node_routing_unavailable`, and no active
    section claims a live hub-to-node path. D4 holds the live proof. 1.1 adds the
    S4.2 ← #24090 edge to `ROADMAP.md`.

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
- **Hooks from node agents.** A remote agent's hooks reach the hub's
  `POST /api/hooks/execute` only through #24150's node ledger (ROADMAP decision
  17: `ghook` posts through the local node). Its session-start and session-end
  hooks drive its run as they do locally. 2.6 settles a run whose agent left no
  session end.
- **No backward compatibility.** Nothing is migrated.
- **Monolith ceiling.** `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py`
  (988 lines), `src/gobby/agents/lifecycle_monitor.py` (959), and
  `src/gobby/runner_lifecycle_shutdown.py` (895) are not edited. The remote flow
  and the reconciler are new modules, and the reconciler runs inside the
  existing health pass. `src/gobby/storage/terminals.py` (933 lines) changes
  only through 2.1's split.
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
    `unsupported_version`, `unknown_command`, `invalid_payload`, `not_found`,
    `cancelled`, and `command_failed`.
  - **Hub-client codes.** 1.3's Python client raises `node_routing_unavailable`
    (`stage: "none"`) and `node_reply_lost` (`stage: null`); neither crosses
    the wire. Its machine routing refuses with `checkout_unresolved`,
    `machine_not_found`, and `machine_not_owned`, each `stage: "none"` before
    any command, and reuses `node_offline`. Hub-flow result codes such as
    `not_stopped`, `spawn_in_doubt`, and `remote_resume_unsupported` stay in
    their own sections.
  - **Stage.** Every error carries `stage`. A node sets `"none"` when the
    command changed nothing and `"partial"` when it may have. gdaemon sets
    `"none"` on `node_offline`, because it writes nothing without a live
    channel, and `null` on `node_timeout` and `channel_closed`, because the
    frame may have reached the node. Only `"none"` proves a command had no
    effect.
  - **Strictness.** Payloads and results reject unknown fields.
  - **v1 commands:**
    - `workspace.prepare`: `{run_id, project_id, mode: "worktree"|"clone", source_root, path, branch_name, base_branch}` returns `{path, branch_name, base_commit_sha}`.
    - `workspace.remove`: `{project_id, mode, source_root, path, branch_name, delete_branch}` returns `{removed}`.
    - `agent.spawn`: `{run_id, session_id, project_id, terminal_id, provider, model, cwd, prompt, env, provider_args, reasoning_effort, sandbox: {enabled, network: "none"|"trusted", extra_write_paths}, timeout_seconds, title}` returns `{pid, locator, locator_key, host_epoch}`.
    - `agent.cancel`: `{run_id, terminal_id, signal: "TERM"|"KILL", close_terminal}` returns `{stopped}`.
    - `terminal.write`: `{terminal_id, kind: "text"|"key"|"input"|"paste", data, submit}` returns `{}`.
    - `node.inventory`: `{}` returns `{host_epoch, agents: [{run_id, terminal_id, pid, locator, locator_key}]}`.
  - **Node duties (#24148).**
    - `agent.spawn`: `env` keys must come from the seven identity variables
      above; anything else is `invalid_payload`. The node sets
      `GOBBY_DAEMON_URL` to its own front door, writes `prompt` to a node-local
      file and sets `GOBBY_PROMPT_FILE`, builds the provider command, applies its
      SRT chokepoint, enforces `timeout_seconds`, and closes the pane when the
      agent process exits. It answers `stage: "none"` only when it launched
      nothing.
    - Commands that name the same `run_id` run one at a time in arrival order.
    - `workspace.remove` is idempotent. A path or branch that is already
      absent counts as removed, so a retry after a lost reply answers
      `{removed: true}`. `{removed: false}` means part of the workspace is
      still present. It names no run, because 2.6 retries it from the
      workspace row alone.
    - `agent.cancel` records the `run_id` as cancelled in node-local durable
      state before it replies, so the record survives a node daemon restart. A
      later `workspace.prepare` or `agent.spawn` for it answers `cancelled`
      with `stage: "none"`. A cancel for a run with no live agent answers
      `not_found`, which also proves that nothing runs for that run now or
      later. `{stopped: false}` means the agent may still run.
    - `node.inventory` is a complete snapshot of the live agents the node
      launched. It reads them from its terminal host, so agents that outlive a
      node daemon restart stay listed. `host_epoch` names the terminal host's
      current incarnation.
  - **Internal route (gdaemon, built in D2).**
    - `GET /api/internal/nodes/connected` returns `{"machine_ids":[...]}`, the
      machines with a live channel.
    - `POST /api/internal/nodes/{machine_id}/commands` takes
      `{"command","payload","timeout_ms"}` (default 30 000, maximum 600 000). It
      mints `request_id`, sends the command frame, and answers 200 with the reply
      frame, or with a gdaemon error frame for `node_offline`, `node_timeout`,
      or `channel_closed`.
    - gdaemon admits a request only from a loopback peer whose
      `X-Gobby-Backend-Secret` equals `GOBBY_FRONT_DOOR_SECRET`, compared in
      constant time. Every other request, and every request to a standalone or
      node front door, gets 404. gdaemon answers 404 only before it sends any
      frame.
    - A pending command ends on its reply, its timeout, its channel's close or
      replacement, or its HTTP caller going away. A reply that arrives after
      that is dropped and logged.
  - **Versioning.** A new command or field bumps `v`. A node answers an unknown
    `v` with `unsupported_version`.
- `node_commands.rs` defines serde types for both frames and every payload and
  result, with `deny_unknown_fields`. It is `pub mod node_commands` in `lib.rs`.
- `src/gobby/hub/node_commands.py` defines the matching pydantic models with
  `extra="forbid"`.
- In both languages `agent.spawn`'s `env` is a type with the seven identity
  variables as optional fields, so an unknown key fails to parse.
- `corpus.json` holds one named message per frame shape: each command, each
  command's ok reply, one error reply per code with its stage, and a
  `terminal.write` error with `stage: "partial"`. Both tests parse every
  message, re-encode it, and compare it to the corpus value.
- `ROADMAP.md`: the S4.2 row gains the ← #24090 edge.

Verification planned:
- `cargo nextest run -p gobby-daemon -E 'binary(node_command_corpus)'`.
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/hub/test_node_command_corpus.py -v`.
- `uv run ruff check src/gobby/hub` and `uv run mypy src/gobby/hub`.

**Acceptance:**

- 1.1.1 - The contract specifies both frames, all v1 commands with payload and result fields, the error codes with their producers and stage rule, including the hub-client codes 1.3 raises, the node duties, the internal route, and the versioning rule. file: `docs/contracts/node-commands.md`.
- 1.1.2 - The Rust types round-trip every corpus message byte-equivalently and reject an unknown field. test: `crates/gdaemon/tests/node_command_corpus.rs::corpus_round_trips`.
- 1.1.3 - The Python models round-trip every corpus message and reject an unknown field. test: `tests/hub/test_node_command_corpus.py::test_corpus_round_trips`.
- 1.1.4 - An `agent.spawn` payload whose `env` has a key outside the allowlist fails validation in the Python models. test: `tests/hub/test_node_command_corpus.py::test_spawn_env_allowlist`.
- 1.1.5 - `ROADMAP.md` shows S4.2 depending on #24090. file: `ROADMAP.md`.
- 1.1.6 - An `agent.spawn` payload whose `env` has a key outside the allowlist fails to deserialize in the Rust types. test: `crates/gdaemon/tests/node_command_corpus.rs::spawn_env_allowlist`.

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
- The internal route is built in D2. These tests drive a fake HTTP route, and
  this section claims no live node delivery.

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
  - `send` uses `stage="none"` only when no command can have left the hub. It
    raises `NodeCommandError("node_routing_unavailable", stage="none")` without
    the secret (no request is made), on a failed connect, and on a 404, which
    gdaemon sends only before any frame (1.1). Every request gets that 404
    before D2 lands.
  - Any other failure after the request is sent raises
    `NodeCommandError("node_reply_lost", stage=None)`. That covers a 5xx, a 200
    whose body is not a valid reply frame, a read timeout, and a connection
    reset. The read timeout is `timeout_ms` plus five seconds, so gdaemon's
    `node_timeout` normally arrives first.
  - `connected_machine_ids()` changes nothing, so any failure there raises
    `node_routing_unavailable`.
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

- 1.3.1 - Against a fake route, `send` posts the secret header and maps an ok reply to its result and an error reply to `NodeCommandError` with code and stage. test: `tests/hub/test_node_client.py::test_send_maps_replies`.
- 1.3.2 - Without `GOBBY_FRONT_DOOR_SECRET`, calls raise `node_routing_unavailable` and make no request; a 404 or a refused connection raises it too, with `stage="none"`. test: `tests/hub/test_node_client.py::test_unavailable_routing_is_typed`.
- 1.3.3 - The origin machine is the default target, and a node ref resolves to its machine. test: `tests/hub/test_machine_routing.py::test_default_and_explicit_targets`.
- 1.3.4 - A remote target without a checkout, with no live channel, or with another owner is refused with its typed code and never replaced by another machine. test: `tests/hub/test_machine_routing.py::test_ineligible_targets_never_fall_back`.
- 1.3.5 - After the fake route accepts a command, a 5xx, a truncated or malformed 200, a read timeout, or a connection reset raises `node_reply_lost` with `stage=None`. test: `tests/hub/test_node_client.py::test_post_send_failures_are_uncertain`.

## P2: Machine-correct hub loops and remote callers
`kind: framing`

**Goal**: remote rows are safe from local loops, spawn, cancel, and terminal
write go to the owning node, and the hub settles remote runs from the node's
inventory.

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
  (`list_active_for_machine(require_machine_id())`) and
  `InteractiveAttentionMonitor._list_active_runs`.
- Workspace loops are already scoped, so left alone. `worktrees` and `clones`
  carry `machine_id`, and every `LocalWorktreeManager` and `LocalCloneManager`
  method binds `require_machine_id()`. That covers these loops:
  - `src/gobby/runner_maintenance/isolation.py::cleanup_expired_isolation_loop`
    (`find_expired`);
  - its missing-record sweeps `_delete_missing_worktree_records` and
    `_delete_missing_clone_records`, which delete a record whose path is
    missing on this disk;
  - `reconcile_isolation_registry` (`require_local_machine_id`);
  - `SessionCoordinator.release_session_worktrees`.

  `reactivate_cited_worktrees` updates status by task on every machine and
  touches no file or process. 2.1.5 pins the scoping, because 2.5 writes a
  node's rows.

**Implementation:**
- Split: `list_stale_pending` moves from `TerminalManager` in
  `src/gobby/storage/terminals.py` into `TerminalReapQueryMixin` in
  `src/gobby/storage/terminal_reap_queries.py`, modeled on
  `TerminalSettlementMixin`. `TerminalManager` inherits it, so its caller keeps
  calling it on the manager. No other method moves.
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
- 2.1.5 - The expired-isolation cleanup, the missing-record sweeps, and session-end worktree release leave another machine's `worktrees` and `clones` rows untouched while still acting on local rows. test: `tests/agents/test_hub_local_loops.py::test_workspace_loops_skip_remote_rows`.

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
- `_dispatch` turns host refusals into `TerminalWriteError(stage=...)`.
  `_write_locked` clears the latch on `stage="none"` and keeps it on
  `stage="partial"` and on an `IndeterminateWrite` outcome.
  `_idempotent_replay` answers a same-key, same-payload retry of a latched write
  with `IndeterminateWrite` and sends nothing.
- `tests/terminals/fakes.py::make_memory_terminal`, used by about fifty test
  files, gives every fake terminal a random `machine_id`. Under a local-machine
  check those terminals would all read as remote.
- The tests drive a fake node client; live delivery waits on D2 and #24148.

**Implementation:**
- `_dispatch` records the composer-ledger observation as today. Then, when
  `terminal.machine_id` is not local, it returns
  `remote_terminal.write(terminal, request)` and skips `runtime_for`.
- `remote_terminal.write` sends `terminal.write` and maps the outcome by the
  1.1 stage rule:
  - `ok` returns `Delivered()`.
  - An error with `stage: "none"` raises `NodeTerminalWriteError`, a
    `TerminalWriteError` subclass with `stage="none"` that carries the code.
    `_write_locked` then clears the latch. This covers `node_offline`,
    `node_routing_unavailable`, and node refusals.
  - Every error whose stage is `null` returns `IndeterminateWrite(detail=<code>)`,
    whatever its code, because the command may have reached the node. That
    covers `node_timeout`, `channel_closed`, and `node_reply_lost`. The latch
    stays.
  - An error with `stage: "partial"` raises `NodeTerminalWriteError` with
    `stage="partial"`. The latch stays.
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

- 2.3.1 - Through a fake node client, a write to another machine's terminal sends one `terminal.write` with the request's kind, data, and submit, and returns `Delivered`. test: `tests/hub/test_remote_terminal_write.py::test_remote_write_uses_node_command`.
- 2.3.2 - An offline node raises `NodeTerminalWriteError` with `node_offline` and `stage="none"`, and the latch is cleared. test: `tests/hub/test_remote_terminal_write.py::test_offline_node_clears_latch`.
- 2.3.3 - A partial node failure surfaces `stage="partial"` and keeps the latch. test: `tests/hub/test_remote_terminal_write.py::test_partial_stage_propagates`.
- 2.3.4 - A local terminal write never contacts the client. test: `tests/hub/test_remote_terminal_write.py::test_local_write_unchanged`.
- 2.3.5 - When a fake node takes the write and its reply is lost (`node_timeout`) or unreadable (`node_reply_lost`), the write returns `IndeterminateWrite`, the latch stays, and a retry with the same action key sends no second `terminal.write`. test: `tests/hub/test_remote_terminal_write.py::test_lost_reply_is_indeterminate`.

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
- The tests drive a fake node client; live delivery waits on D2 and #24148.

**Implementation:**
- At the top of `kill_agent`, a run whose `machine_id` is set and not local
  returns `remote_cancel.cancel(run, db, signal_name=..., close_terminal=..., terminal_action=..., terminal_reason=...)`.
  `AgentRun.machine_id` defaults to `require_machine_id()`, so existing local
  runs never reach the guard.
- `cancel` sends `agent.cancel`. A `{stopped: true}` reply returns
  `{"success": True, "method": "node_command", "message": ...}`. A
  `{stopped: false}` reply returns `{"success": False, "error_code": "not_stopped"}`
  and settles nothing. When
  `close_terminal` is set, it settles the terminal row through the same
  `TerminalManager` transition that `_close_managed_terminal` applies after a
  local close.
- A `not_found` error counts as stopped: the node runs nothing for that run and
  has recorded it as cancelled (1.1).
- Any other error reply returns `{"success": False, "error": <message>, "error_code": <code>}`.

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
- 2.4.4 - A `not_found` reply returns success like a stopped one. test: `tests/hub/test_remote_cancel.py::test_not_found_counts_as_stopped`.
- 2.4.5 - A `{stopped: false}` reply returns `error_code: not_stopped` and leaves the terminal row unchanged. test: `tests/hub/test_remote_cancel.py::test_not_stopped_is_failure`.

### 2.5 Remote spawn goes through the node [category: code] (depends: 1.3, 2.1, 2.2, 2.4)
`kind: deliverable`

Targets:
- `src/gobby/hub/remote_spawn.py`
- `src/gobby/hub/remote_workspaces.py`
- `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py::create_spawn_agent_registry`
- `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py::_parent_session_project_context`
- `src/gobby/servers/routes/agent_spawn.py::AgentSpawnRequest`
- `src/gobby/servers/routes/agent_spawn.py::create_agent_spawn_router`
- `src/gobby/agents/spawn.py::prepare_terminal_spawn`
- `src/gobby/agents/spawn.py::_prepare_run_for_session`
- `src/gobby/storage/agents/_lifecycle.py::_AgentRunLifecycleMixin.create`
- `tests/hub/test_remote_spawn.py`

**Granularity:** eleven acceptance items and six production files, one
outcome: every remote spawn ends in one owned state, launched, failed, or in
doubt. The items fall into three groups. Routing covers 2.5.3, 2.5.6, 2.5.8,
and 2.5.9. The launch covers 2.5.1, 2.5.2, 2.5.7, and 2.5.11. Settlement of its
outcome covers 2.5.4, 2.5.5, and 2.5.10. None closes alone:
- Routing without the launch only adds refusals for remote targets.
- A launch without settlement leaves runs, leases, and workspaces with no owner
  after a failure, the HM-F2 defect.
- Threading the run's machine and writing a node's workspace row have no
  consumer outside this flow and 2.6.

The periodic reconciler has its own lifecycle owner, `AgentHealthMonitor`, so
it is already its own section, 2.6.

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
  managed credential, and `prompt=None` writes no prompt file. Its `machine_id`
  reaches only the child session: `_prepare_run_for_session` calls
  `agent_run_mgr.create(...)` without a machine, and
  `_AgentRunLifecycleMixin.create` sets `machine_id = get_machine_id()`.
- `src/gobby/agents/spawn_executor_providers.py::ProviderSpawnPlan` is what the
  local flow builds before `_runtime_spawn`. A remote spawn never builds it;
  the node does (#24148).
- In `create_spawn_agent_registry`'s nested `spawn_agent`, the project context
  resolves first, then `enforce_spawn_caller` returns `caller_session_id`, the
  session that is spawning. It can differ from `parent_session_id` when an
  agent names the coordinator it reports to. A session's machine is
  `sessions.machine_id`, bound to its key by 2.2.
- `_parent_session_project_context` passes the parent session's machine
  through `require_local_machine_id` before `require_root`, so a parent on
  another machine yields the unresolved-project sentinel today. Its tests pin
  the session machine to the local machine.
- `TaskSpawnLease` (`_spawn_guards.py`) is admitted, then attached to the run;
  `release_unattached` frees it only before attachment, and an attached lease
  ends with its run.
- `_failure_cleanup._roll_back_run` settles a run in the database only, through
  `terminalize_cancelled_agent_run` and `cleanup_agent_runtime_state`.
  `cleanup_failed_spawn` also reaches local processes and panes
  (`_terminate_spawn_process`, `_kill_spawn_terminal`), so the remote flow never
  calls it.
- A terminal agent's run turns running when its session-start hook arrives
  (`SessionCoordinator` calls `start`); the spawn path records `pid` and
  `terminal_id` through `update_runtime`.
- `handle_session_end` completes the run, then calls `mark_exited` on the row
  that `get_live_for_session` returns. `mark_exited` is a CAS from live or
  orphaned (`terminal_settlement.py`), so a still-pending row stays pending.
- Without a hub checkout, `_parent_session_project_context` returns
  `_unresolved_parent_project_context(project_id)`, which keeps the project id.
- Workspace rows (Decision 15): `worktrees` and `clones` carry `machine_id`.
  The local flow records its row after creating the directory
  (`WorktreeIsolationHandler.prepare_environment` calls
  `LocalWorktreeManager.create`), and `cleanup_environment` deletes the row
  after removing the files.
  - `LocalWorktreeManager.has_path_on_other_machine` makes the hub's
    `handle_worktree_remove` hook and `register_adopted` skip a local path that
    another machine's row also names.
  - `release_session_worktrees` releases only this machine's rows, so a
    remote row keeps its `agent_session_id` after the session ends.
  - A task's `worktree_path` artifact is read by hub-side build paths that run
    git in that path (`src/gobby/build/claim_recovery.py`,
    `src/gobby/build/observability.py`).
  - `require_root(db, project_id, machine_id)` returns a machine's checkout
    root.
  - `worktrees.status` includes `abandoned` and `clones.status` includes
    `cleanup`. Only `LocalWorktreeManager` and `LocalCloneManager` set them,
    and their readers, such as `list_cleanup_pending`, bind this machine.
    `reactivate_cited_worktrees` reactivates only `merged` rows.
  - `agent_session_id` references `sessions(id, machine_id)` with
    `ON DELETE SET NULL`, so a retained row cannot be relied on to reach its
    run.
- The tests drive a fake node client; live delivery waits on D2 and #24148.

**Implementation:**
- `AgentSpawnRequest` and the MCP `spawn_agent` tool each gain an optional
  `machine: str | None` (id or node ref). Batch requests inherit it through
  `spawns`.
- Origin machine:
  - The MCP tool reads it after `enforce_spawn_caller`: the `machine_id` of the
    `caller_session_id` session.
  - `POST /api/agents/spawn` uses the verified front-door identity's machine.
  - An absent value counts as this machine. The parent session never sets the
    origin.
- Each caller then calls `resolve_target_machine` with the context's project id,
  `machine`, and the origin. A local result follows today's path. A remote
  result calls `spawn_remote_agent`. A refusal returns its typed code as the
  spawn error.
- A remote spawn needs no hub checkout. The MCP tool takes `project_id` from the
  spawn project context, and the unresolved-parent context keeps that id, so
  the remote branch reads it from there. It ignores `effective_project_path`
  and uses `RoutedMachine.checkout_root`. `_do_spawn` resolves the target before
  its `require_root`, and only a local result calls `require_root`. No new
  project resolver is added.
- `_parent_session_project_context` resolves `require_root` with this machine's
  id in place of the parent session's. A hub caller whose parent is on a node
  then spawns locally in the hub's checkout.
- `_AgentRunLifecycleMixin.create` gains `machine_id: str | None = None`, which
  defaults to `get_machine_id()`. `_prepare_run_for_session` gains the same
  keyword and passes it on, and `prepare_terminal_spawn` passes its own
  `machine_id`. Session, run, and terminal rows then all name the target.
- `spawn_remote_agent` refuses `resume_session_id` with
  `remote_resume_unsupported` and a placement request with
  `remote_placement_unsupported`. Otherwise:
  1. It runs the local flow's agent-body, provider, model, and depth checks,
     the `runner.can_spawn` gate, and `TaskSpawnLease` admission.
  2. It names the node workspace: with isolation a new path and branch, both
     embedding the run id so no hub row shares the path, otherwise the node's
     `checkout_root`. It calls `prepare_terminal_spawn` with
     `machine_id=<target>`, `prompt=None`, `credential_manager=None`, and that
     path. It attaches the lease to the run and creates the pending terminal row
     with `machine_id=<target>`. It writes nothing about the workspace into the
     run's resume metadata and sets no task artifact.
  3. With isolation, it calls `record_remote_workspace`, then sends
     `workspace.prepare` with the node's `checkout_root` as `source_root`. The
     row is written before the command, unlike the local order, so an
     in-doubt prepare still leaves a row for 2.6 to remove.
  4. It sends `agent.spawn`. `env` is the allowlisted subset of
     `PreparedSpawn.env_vars`.
  5. On `ok` it calls `record_spawn_identity(run_id, terminal_id, reply)`.
     That helper promotes the terminal with `promote_to_live` (the reply's
     `locator`, `locator_key`, and `host_epoch`), then re-reads the run. An
     active run gets `pid` and `terminal_id` through `update_runtime`. A run
     that already ended has had its session end arrive first. That session end
     could not exit the still-pending terminal. So the helper calls
     `mark_exited` and records nothing on the run. `promote_to_live` is a
     pending-to-live CAS, so an exited row is never promoted again.
  6. A step 3 or 4 error with `stage: "none"` is a definite failure. Before it
     settles anything, a failed `workspace.prepare`, which created nothing,
     deletes the row, and after a prepared workspace it calls
     `mark_remote_workspace_removal`. It then fails the pending terminal
     (`fail_pending`) and settles the run with `_roll_back_run`, which also
     ends the lease. Last, it calls `remove_remote_workspace` on the marked
     row. A hub restart between these steps leaves a pending terminal for
     2.6's fence, an active run for 2.6's run sweep, or a marked row for 2.6's
     removal retry.
  7. Any other outcome leaves the spawn in doubt. That covers a `null` or
     `"partial"` stage and a caller that goes away after a send. The run and
     terminal stay pending, the lease stays attached, the workspace and its row
     stay, and the caller gets `spawn_in_doubt` with the run id. 2.6 resolves
     the spawn.
- `src/gobby/hub/remote_workspaces.py`:
  - `record_remote_workspace(db, *, mode, machine_id, project_id, path,
    branch_name, base_branch, task_id, session_id)` inserts an active
    `worktrees` row (mode `worktree`) or `clones` row (mode `clone`) with that
    machine and the child session as `agent_session_id`.
  - `mark_remote_workspace_removal(db, *, machine_id, session_id)` sets that
    machine's row for the session to `abandoned` in `worktrees` or `cleanup`
    in `clones` and returns it through `Worktree.from_row` or
    `Clone.from_row`, or `None` when there is no row.
  - `remove_remote_workspace(db, client, workspace)` sends `workspace.remove`
    to the row's machine with `delete_branch: true` and `source_root` from
    `require_root(db, project_id, machine_id)`. On `{removed: true}` it
    deletes the row by its id and machine while the row is still marked. Any
    other reply or error leaves the row marked for 2.6.

Consumers unchanged:
- `src/gobby/mcp_proxy/tools/agents_spawn_tools.py` — no-edit-reason: it registers `create_spawn_agent_registry(...)`, whose signature is unchanged.
- `src/gobby/servers/_app_routes.py` — no-edit-reason: it mounts `create_agent_spawn_router(server)`, whose signature is unchanged.
- `src/gobby/servers/routes/__init__.py` — no-edit-reason: it re-exports `create_agent_spawn_router` by name.
- `tests/config/test_live_policy_consumers.py` — no-edit-reason: it inspects the router's launch methods, which are unchanged.
- `tests/agents/test_backend_ingress.py` — no-edit-reason: it asserts `terminal_backend` is absent and rejected; an added optional `machine` field changes neither.
- `tests/mcp_proxy/tools/spawn_agent/test_execution.py` — no-edit-reason: its spawns omit `machine` and take the unchanged local path; re-run as verification.
- `tests/mcp_proxy/tools/spawn_agent/test_factory.py` — no-edit-reason: it asserts named schema properties, which an added optional property leaves intact, and its `_parent_session_project_context` tests pin the session machine to the local machine, so resolving with this machine's id returns the same checkout.
- `tests/agents/test_spawn.py` — no-edit-reason: it patches `_prepare_run_for_session` or calls it by keyword without `machine_id`, which defaults to this machine.
- `tests/agents/test_spawn_resume.py` — no-edit-reason: it patches `_prepare_run_for_session`; `prepare_terminal_resume` keeps calling it without `machine_id`.
- `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py` — no-edit-reason: its local flow passes `machine_id=get_machine_id()` to `prepare_terminal_spawn`, so its runs keep this machine.
- `tests/mcp_proxy/tools/spawn_agent/conftest.py` — no-edit-reason: its wrapper forwards every argument to `prepare_terminal_spawn` unchanged.
- `tests/agents/test_spawn_executor.py` — no-edit-reason: it patches `prepare_terminal_spawn`.
- `tests/agents/test_srt_spawn.py` — no-edit-reason: it patches `prepare_terminal_spawn`.
- `tests/agents/test_runbook_seat_titles.py` — no-edit-reason: it asserts session titles; its runs now carry the parent's machine, which no assertion reads.
- `tests/integration/test_terminal_mode_worktrees.py` — no-edit-reason: it asserts sessions, env, and paths; its runs now carry the passed machine, which no assertion reads.
- `tests/storage/agents/test_active_run_scope.py` — no-edit-reason: it calls `create` without `machine_id` and patches the module's `get_machine_id`, which stays the default.
- `tests/storage/test_machine_scope_writers.py` — no-edit-reason: it calls `create` without `machine_id` and patches the module's `get_machine_id`, which stays the default.
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
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/hub/test_remote_spawn.py tests/mcp_proxy/tools/spawn_agent/ tests/servers/routes/test_agent_spawn_routes.py tests/agents/test_backend_ingress.py tests/agents/test_spawn.py tests/agents/test_spawn_resume.py tests/agents/test_runbook_seat_titles.py tests/integration/test_terminal_mode_worktrees.py tests/storage/agents/test_active_run_scope.py tests/storage/test_machine_scope_writers.py -v`.

**Acceptance:**

- 2.5.1 - Through a fake node client, a spawn from a session on another machine sends `workspace.prepare` and then `agent.spawn` to that machine, and its session, run, and terminal rows carry that `machine_id` before `agent.spawn` is sent. test: `tests/hub/test_remote_spawn.py::test_spawn_routes_to_requesting_machine`.
- 2.5.2 - The `agent.spawn` payload carries no managed credential and no env key outside the allowlist, and the hub writes no prompt file. test: `tests/hub/test_remote_spawn.py::test_payload_carries_no_credential`.
- 2.5.3 - An explicit `machine` is validated, and an ineligible one fails with its typed code without spawning anywhere. test: `tests/hub/test_remote_spawn.py::test_explicit_machine_is_validated`.
- 2.5.4 - A `stage: "none"` failure of `agent.spawn` marks the workspace row for removal before it fails the pending terminal, cancels the run, and ends the lease, then sends `workspace.remove` and deletes the row after `{removed: true}`, with no local process or pane call. A `stage: "none"` failure of `workspace.prepare` deletes the row before it settles anything and sends no `workspace.remove`. test: `tests/hub/test_remote_spawn.py::test_definite_failure_settles_in_database`.
- 2.5.5 - A `node_timeout` or a `node_reply_lost` on `agent.spawn` returns `spawn_in_doubt` and leaves the run and terminal pending, the lease attached, and the workspace and its row in place, with no rollback. test: `tests/hub/test_remote_spawn.py::test_in_doubt_spawn_keeps_state`.
- 2.5.6 - A node caller whose parent is on the hub spawns on the node, and a hub caller whose parent is on a node spawns locally in the hub's checkout. test: `tests/hub/test_remote_spawn.py::test_origin_is_the_calling_session`.
- 2.5.7 - Remote resume and placement are refused with typed codes. test: `tests/hub/test_remote_spawn.py::test_unsupported_routes_refused`.
- 2.5.8 - A spawn whose target is this machine takes the local path unchanged. test: `tests/hub/test_remote_spawn.py::test_local_target_unchanged`.
- 2.5.9 - With a checkout on the target and none on the hub, both the MCP tool and `POST /api/agents/spawn` spawn on the target. test: `tests/hub/test_remote_spawn.py::test_remote_target_needs_no_hub_checkout`.
- 2.5.10 - When the agent's session end arrives before the spawn reply, the reply leaves the run ended, exits the terminal, and records no `pid`. test: `tests/hub/test_remote_spawn.py::test_session_end_before_reply_stays_settled`.
- 2.5.11 - Before `workspace.prepare` is sent, an isolated remote spawn has a `worktrees` row, or a `clones` row in clone mode, carrying the target's `machine_id`, the path, the branch, the task, and the child session; the run's resume metadata and the task's artifacts name no remote workspace, and the hub's `list_worktrees()` does not return the row. test: `tests/hub/test_remote_spawn.py::test_remote_workspace_is_a_machine_row`.

### 2.6 The hub settles remote runs from the node's inventory [category: code] (depends: 1.3, 2.1, 2.5)
`kind: deliverable`

Targets:
- `src/gobby/hub/remote_reconcile.py`
- `src/gobby/hub/remote_workspaces.py`
- `src/gobby/agents/agent_health.py::AgentHealthMonitor.__init__`
- `src/gobby/agents/agent_health.py::AgentHealthMonitor.check_unhealthy_agents`
- `tests/hub/test_remote_reconcile.py`

**Granularity:** seven acceptance items and three production files, one
outcome: after a remote spawn leaves 2.5, every run, terminal, and failed-spawn
workspace on a connected machine reaches a settled state with one owner,
`RemoteRunReconciler` under `AgentHealthMonitor`. Terminal settlement covers
2.6.1 to 2.6.5, the removal retry 2.6.6, and the run sweep 2.6.7. None is safe
alone: the fence marks the row that the retry removes, and it exits the
terminal whose run the sweep settles after a hub restart.

**Research context:**
- Every local run monitor lists runs with `require_machine_id()`:
  `check_unhealthy_agents` uses `list_active_for_machine`, and
  `cleanup_stale_pending_runs` and `_get_active_terminal_runs` are scoped the
  same way. A remote run has no owner after the spawn reply. A lost reply leaves
  its terminal pending, and an agent that dies without a session end leaves its
  run running.
- A remote agent's normal exit already settles. Its session-end hook reaches
  `handle_session_end`, and `session_end_targets_live_cli` is false for a
  session on another machine. So `complete_agent_run` and `mark_exited` run.
- `check_unhealthy_agents` settles a local dead terminal through
  `_dead_terminal_reason`, which completes a provider that exits without a
  session end, and through `AgentCleanupHandler.cleanup_agent`. That chain
  fails the run, recovers its task, and runs `post_terminal_cleanup`, which
  skips local phases for a remote run (2.1). `_unsettled_completions` gives a
  queued completion one more pass.
- `list_active_for_machine(machine_id)` returns only queued, pending, and
  running runs. A run that a session end settled drops out of it even while
  its terminal is still pending, because `mark_exited` is a CAS from live or
  orphaned only.
- `TerminalManager.list_reconcilable_by_machine(machine_id)` returns that
  machine's pending, live, and orphaned rows with no limit; `host_reconcile.py`
  uses it locally. `TerminalManager` also has `promote_to_live` (pending to
  live), `fail_pending` (pending to exited), and `mark_exited`.
  `spawn_in_doubt_seconds` (default 150) is the local spawn deadline.
  `list_for_session(session_id)` returns a session's terminal rows.
- `cleanup_agent` (`src/gobby/agents/agent_cleanup.py`) recovers a failed
  run's task after checking its terminal's liveness, so every path below exits
  the terminal before it calls `cleanup_agent`. A hub restart between the two
  leaves an active run whose terminal has exited, which no terminal-row pass
  selects.
- The tests drive a fake node client; live inventory waits on D2 and #24148.

**Implementation:**
- `RemoteRunReconciler` takes the run manager, the terminal manager, a
  `cleanup_agent` callable, a `dead_reason` callable, and `run_db`.
  `AgentHealthMonitor.__init__` builds it from its own
  `_cleanup_handler.cleanup_agent` and `_dead_terminal_reason`.
  `check_unhealthy_agents` adds the result of `reconcile()` to its count after
  the local pass.
- `reconcile()` reads `connected_machine_ids()` and drops this machine. A
  `node_routing_unavailable` refusal ends the pass. Machines without a live
  channel are never visited, so their rows stay untouched.
- The terminal pass selects terminal rows, not runs, so a terminal outlives
  its run's settlement. For each connected machine it reads
  `list_reconcilable_by_machine(machine_id)`: pending, live, and orphaned rows,
  in full. It keeps the rows with an `agent_run_id` whose run is not
  recovery-protected. If any remain, it sends `node.inventory`; an error skips
  that machine for the pass. For each row it re-reads the owning run. "Ended"
  means the run has a terminal status.
  - Listed and pending: `record_spawn_identity` (2.5) with the inventory entry.
    For an ended run, that exits the terminal, as in 2.5.10.
  - Listed and live or orphaned: the row clears its absent record.
  - Absent and live or orphaned: a live row whose `host_epoch` differs from the
    inventory's also counts as absent. The first absent pass only records the
    row; a forwarded session end can settle the run in between. On the second
    consecutive absent pass:
    - An ended run gets `mark_exited` only.
    - For an active run, the reconciler calls `dead_reason`. `None` means the
      run has just settled, so it calls `mark_exited` only. Otherwise it calls
      `mark_exited`, then `cleanup_agent(run, terminal_payload=<reason>)`.
  - Absent and pending, with `attempt_started_at` older than
    `spawn_in_doubt_seconds`, for an active or an ended run: the launch is
    uncertain either way. An ended status does not prove that a queued
    `agent.spawn` cannot still arrive. The reconciler sends `agent.cancel` with
    `signal: "KILL"` and `close_terminal: true` as a fence. On
    `{stopped: true}` or `not_found`, for an active run it first calls
    `mark_remote_workspace_removal` (2.5) for the run's child session. It then
    calls `fail_pending`, the pending-to-exited CAS. For an active run it then
    calls `cleanup_agent` with `"remote spawn did not start on <machine>"` and
    `remove_remote_workspace` on the marked row. An ended run gets nothing
    more: its run, task, and lease keep their settlement, and its workspace
    and row stay unmarked (D1).
    `{stopped: false}` and every other reply leave the row for the next pass.
    The node's cancel record is durable (1.1), so a delayed `agent.spawn`
    cannot start the run after this settlement.
- Run sweep: for each connected machine the pass also reads every active run,
  paging `list_active_for_machine(machine_id, limit=100, offset=...)` until a
  page comes back short, because the method returns only its first 100 rows by
  default. It collects every page before it settles anything, so a settlement
  cannot shift a later page's offset. A run that is not recovery-protected,
  has at least one terminal row (`list_for_session` on its child session), and
  has none pending, live, or orphaned is recorded on its first pass. On the
  second consecutive pass the reconciler re-reads the run and its terminal
  rows. If the run is still active and still has no pending, live, or
  orphaned terminal, it gets `cleanup_agent` with `"remote terminal exited
  before its run settled"`. Otherwise its record clears. The wait lets a flow
  that is already settling the run finish first.
- Removal retry: for each connected machine the pass reads
  `list_remote_removals(db, machine_id=...)` and calls
  `remove_remote_workspace` for each row, whether or not the machine has
  terminal rows. An error or `{removed: false}` leaves the row marked for the
  next pass.
- `src/gobby/hub/remote_workspaces.py` gains
  `list_remote_removals(db, *, machine_id)`, which returns that machine's
  `abandoned` worktrees and `cleanup` clones through `Worktree.from_row` and
  `Clone.from_row`.

Consumers unchanged:
- `src/gobby/agents/lifecycle_monitor.py` — no-edit-reason: it constructs `AgentHealthMonitor` with unchanged arguments and calls `check_unhealthy_agents()`, whose signature and return type are unchanged.
- `tests/agents/test_lifecycle_monitor.py` — no-edit-reason: without `GOBBY_FRONT_DOOR_SECRET` the reconciler gets `node_routing_unavailable` before any request or query and adds 0; re-run as verification.

Verification planned:
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/hub/test_remote_reconcile.py tests/agents/test_lifecycle_monitor.py -v`.

**Acceptance:**

- 2.6.1 - A listed run with a pending terminal is promoted live with the inventory's identity, and its `pid` is recorded. test: `tests/hub/test_remote_reconcile.py::test_listed_pending_run_is_promoted`.
- 2.6.2 - An unlisted run with a live terminal, or a live row from an older `host_epoch`, is left on its first absent pass and settled on the second through `mark_exited` and `cleanup_agent`; a run that settles in between is not failed, and its terminal still exits. test: `tests/hub/test_remote_reconcile.py::test_absent_live_run_settles_after_one_pass`.
- 2.6.3 - An unlisted pending run past the deadline is sent `agent.cancel` first. `{stopped: true}` or `not_found` then marks its workspace row for removal, fails its terminal and run, and sends `workspace.remove`, and `node_timeout` or `{stopped: false}` changes nothing. test: `tests/hub/test_remote_reconcile.py::test_in_doubt_spawn_is_fenced_before_settlement`.
- 2.6.4 - A machine without a live channel gets no command and its rows stay as they are, and local rows are never inventoried. test: `tests/hub/test_remote_reconcile.py::test_disconnected_and_local_rows_untouched`.
- 2.6.5 - In this case the spawn's effect lands and its reply is lost, and the agent's session start and session end both reach the hub before the first pass. Past the deadline the pending terminal is fenced with `agent.cancel`. After `not_found` it exits, the run stays ended, and there is no `cleanup_agent` and no `workspace.remove`. A later `agent.spawn` for that run is refused `cancelled` by the fake node. After `{stopped: false}` or `node_timeout` the terminal stays pending. The same row on a disconnected machine stays untouched. test: `tests/hub/test_remote_reconcile.py::test_ended_run_terminal_still_settles`.
- 2.6.6 - In worktree and clone mode, a failed spawn's marked row survives a first `workspace.remove` that answers `node_timeout`, an error, or `{removed: false}`. A new reconciler, standing in for a hub restart, retries it on its next pass after the terminal has exited and the run has ended. When the removal's effect landed and only its reply was lost, the retry answers `{removed: true}` and the row is deleted. A disconnected machine's marked row and an ended run's unmarked row are never sent `workspace.remove`. test: `tests/hub/test_remote_reconcile.py::test_marked_workspace_removal_is_retried`.
- 2.6.7 - An active remote run whose terminal rows have all exited, as a hub restart between `fail_pending` and the run's settlement leaves it, is left on the first pass and settled through `cleanup_agent` on the second, including when 100 healthy active runs on that machine put it past the first page. A run that completes, or gains a pending or live terminal, between the two passes is not swept, and a run with no terminal row is never swept. test: `tests/hub/test_remote_reconcile.py::test_run_left_active_after_terminal_exit_settles`.

## P3: Machine-aware roster and attention
`kind: framing`

**Goal**: every machine's agents and attention reach the client, and a response
reaches the owning machine.

### 3.1 Hub-wide roster with each entry's machine and project [category: code]
`kind: deliverable`

Targets:
- `src/gobby/storage/attention.py::AttentionStateManager.load_roster_rows`
- `src/gobby/storage/attention.py::AttentionRosterRow`
- `src/gobby/servers/routes/attention_roster.py::_load_roster_entries`
- `src/gobby/servers/routes/attention.py::create_attention_router`
- `src/gobby/servers/routes/attention.py::_resolve_prompt_detector`
- `tests/servers/test_attention_roster.py::test_bounded_roster_query_joins_task_payload`
- `tests/servers/test_attention_roster.py::test_running_roster_projects_child_lifecycle_and_seat_title`
- `tests/servers/test_attention_roster.py::test_roster_recovers_older_seat_titles_without_definition_title_variable`
- `tests/servers/test_attention_roster_machines.py`

**Research context:**
- `load_roster_rows(machine_id, *, live_session_statuses)` unions runs filtered
  by `run.machine_id = %s` with live sessions filtered only by status. It has
  two callers, both passing `require_machine_id()`: `roster()` in
  `create_attention_router`, which caches the payload by `(epoch, seq)`, and
  `_resolve_prompt_detector`, which finds a respond's provider.
- The run half joins the child session as `child`; `agent_runs` has no project
  column. A row keeps its terminal's machine
  (`AttentionRosterTerminal.machine_id`), but `_load_roster_entries` emits no
  machine or project.
- gclient derives an agent's machine and project from its session or run rows,
  fetched per project with `limit=50`, and falls back to the focused project.

**Implementation:**
- `load_roster_rows` drops its `machine_id` parameter and the run filter. Each
  half selects the subject's machine and project. Runs use `run.machine_id`
  and `COALESCE(child.project_id, parent.project_id)`, through an added join of
  the parent session. Sessions use `session.machine_id` and
  `session.project_id`. `AttentionRosterRow` gains two last fields,
  `machine_id: str | None = None` and `project_id: str | None = None`, so
  keyword constructors stay valid.
- `_load_roster_entries` emits `machine_id` and `project_id` on every entry.
- `roster()` and `_resolve_prompt_detector` stop passing `require_machine_id()`.
- `tests/servers/test_attention_roster.py` drops the `require_machine_id()`
  argument from its three direct `load_roster_rows` calls.

Consumers unchanged:
- `tests/agents/test_attention_metadata.py` — no-edit-reason: it patches `load_roster_rows` with `lambda *_args, **_kwargs` and builds `AttentionRosterRow` by keyword; the defaulted field keeps both valid.
- `src/gobby/servers/_app_routes.py` — no-edit-reason: it mounts `create_attention_router(server)`, whose signature is unchanged.
- `src/gobby/servers/routes/__init__.py` — no-edit-reason: it re-exports `create_attention_router` by name.
- `tests/servers/test_attention_native_roster.py` — no-edit-reason: its rows are on the local machine, so the hub-wide roster returns the same entries plus `machine_id`; re-run as verification.
- `tests/servers/test_attention_respond.py` — no-edit-reason: its entries are local, so `_resolve_prompt_detector` finds the same rows in the hub-wide list; re-run as verification.

Verification planned:
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/test_attention_roster_machines.py tests/servers/test_attention_roster.py tests/servers/test_attention_native_roster.py tests/servers/test_attention_respond.py tests/agents/test_attention_metadata.py tests/storage/test_attention.py -v`.

**Acceptance:**

- 3.1.1 - The roster lists live runs and sessions from every machine. test: `tests/servers/test_attention_roster_machines.py::test_roster_is_hub_wide`.
- 3.1.2 - Every entry carries the machine and project of its run or session. test: `tests/servers/test_attention_roster_machines.py::test_entries_carry_machine_and_project`.
- 3.1.3 - Responding to a local entry still resolves its provider's detector through the hub-wide rows. test: `tests/servers/test_attention_roster_machines.py::test_local_respond_resolves_detector`.

### 3.2 gclient attributes agents by the roster's machine and project [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `crates/gclient/src/daemon/roster.rs::RosterEntry`
- `crates/gclient/src/app/sidebar_model.rs::build_agents`
- `crates/gclient/src/ui/chrome/sidebar_state.rs::SidebarState::default`
- `crates/gclient/tests/roster_machines.rs`

**Research context:**
- `build_agents` sets each agent's `machine_id` from its session, then its run,
  then the local machine. It sets `project_id` from the session's project,
  then the run's, then `inputs.focused_project`. Those rows come from 50-row
  per-project windows, so an agent outside both windows lands in the focused
  project.
- `crates/gclient/src/ui/sidebar/agents.rs` admits an agent when its
  `project_id` equals the focused project and `machine_admits` passes.
  `machine_rows` rolls up agent states per machine.
- `SidebarState::default` sets `machine_filter: Some(ALL_MACHINES)`, though
  its doc says `None` lists the local machine. Its one caller is
  `Chrome::new` in `crates/gclient/src/ui/chrome.rs`. Tests that need every
  machine set the filter themselves (`crates/gclient/tests/parity/sidebar.rs`,
  `crates/gclient/src/ui/panes/tests.rs`).

**Implementation:**
- `RosterEntry` gains `#[serde(default)]` fields `machine_id: Option<String>`
  and `project_id: Option<String>`.
- `build_agents` prefers a non-empty `entry.machine_id` and a non-empty
  `entry.project_id`, then keeps today's fallbacks.
- `SidebarState::default` sets `machine_filter: None`, so a new window shows
  the current machine (Josh's Q4 ruling, Decision 10). The existing filter
  still switches to every machine or one machine.

Consumers unchanged:
- `crates/gclient/src/ui/sidebar/agents.rs` — no-edit-reason: it reads the agent's `project_id` and machine, which `build_agents` now fills from the roster.
- `crates/gclient/src/ui/chrome.rs` — no-edit-reason: `Chrome::new` takes `SidebarState::default()` as is; only the default value changes.

Verification planned:
- `cargo nextest run -p gobby-client`, the whole crate, because the default
  reaches every view test.
- `cargo clippy -p gobby-client` and `cargo fmt -p gobby-client -- --check`.

**Acceptance:**

- 3.2.1 - An agent whose session row is outside the fetched window is listed under its roster machine. test: `crates/gclient/tests/roster_machines.rs::agents_use_roster_machine`.
- 3.2.2 - A remote machine's row shows attention when one of its agents needs attention. test: `crates/gclient/tests/roster_machines.rs::remote_machine_row_shows_attention`.
- 3.2.3 - A remote agent in project B, absent from both fetched windows while project A is focused, stays in project B and is not admitted to project A's view. test: `crates/gclient/tests/roster_machines.rs::agents_use_roster_project`.
- 3.2.4 - A new window lists only the current machine's agents, and choosing all machines lists every machine's agents. test: `crates/gclient/tests/roster_machines.rs::new_window_defaults_to_current_machine`.

### 3.3 Attention respond on another machine's entry [category: code] (depends: 2.3, 3.1)
`kind: deliverable`

Targets:
- `src/gobby/servers/routes/attention.py::AttentionPane`
- `src/gobby/servers/routes/attention.py::_resolve_attention_pane`
- `src/gobby/servers/routes/attention.py::create_attention_router`
- `src/gobby/servers/routes/attention.py::_injection_http_error`
- `src/gobby/agents/tmux/text_injection.py::AttentionInjectionError`
- `tests/servers/test_attention_remote_respond.py`

**Research context:**
- `respond` (nested in `create_attention_router`) holds a per-entry lock,
  checks the request against the stored episode with
  `_require_current_identity`, validates the answer, captures the pane, and
  compares the detector's fingerprint. Without a pane or a capture it answers
  502 `injection_failed`.
- The nested `inject_answer` writes through
  `write_coordinator.write(WriteRequest(terminal_id=pane.target, action_key=f"attention-respond:{attention_id}", origin="attention", …))`.
  It turns a `TerminalWriteError` into
  `AttentionInjectionError(stage=exc.stage)`, dropping any code, and an
  `IndeterminateWrite` into `stage="partial"`.
- With `injector` unset, `respond` claims the episode before injecting. With
  `claimed_before_injection=False` it injects first and clears afterwards:
  `stage="none"` keeps the episode, and a partial outcome retires it.
  `_injection_http_error` answers 502 `injection_failed` or
  `injection_indeterminate`.
- `_resolve_attention_pane` reads the terminal row with
  `TerminalManager.get_live_for_session` or the run's `terminal_id`; neither is
  machine-filtered. Its `capture` snapshots through the local runtime.
- 2.3 routes the write to the owning node. The tests drive a fake node client;
  live delivery waits on D2 and #24148.

**Implementation:**
- `AttentionPane` gains `remote: bool = False`. For a row whose `machine_id` is
  not local, `_resolve_attention_pane` returns `AttentionPane(target=row.id,
  remote=True)` with a capture that returns `None`.
- `AttentionInjectionError` gains an optional `code`. `inject_answer` copies
  `NodeTerminalWriteError.code` into it.
- For a remote pane, `respond` keeps the lock, the identity check, and the
  option check. It skips the capture and the detector, because hook state is
  authoritative (Q6). It always injects first and clears afterwards:
  - Success clears the episode against its stored identity and answers
    `accepted`.
  - `stage="none"` keeps the episode.
  - A partial or indeterminate outcome retires the episode without
    redetection. The kept write latch on `attention-respond:{attention_id}`
    blocks a second send.
- `_injection_http_error` takes an optional `code`. A coded `stage="none"`
  failure answers 503 `{"code": <code>, "stage": "none"}`, such as
  `node_offline`. Every other failure keeps today's 502 codes.

Consumers unchanged:
- `src/gobby/servers/_app_routes.py` — no-edit-reason: it mounts `create_attention_router(server)`, whose signature is unchanged.
- `src/gobby/servers/routes/__init__.py` — no-edit-reason: it re-exports `create_attention_router` by name.
- `tests/agents/test_attention_metadata.py` — no-edit-reason: it reads the roster only, which 3.3 does not change.
- `tests/servers/test_attention_native_roster.py` — no-edit-reason: it reads the roster only, which 3.3 does not change.
- `tests/servers/test_attention_respond.py` — no-edit-reason: its terminals come from `make_memory_terminal`, which 2.3 makes local, so respond keeps the local path and its 502 codes; re-run as verification.
- `tests/terminals/test_tmux_runtime.py` — no-edit-reason: it asserts only `stage`, and the added `code` defaults to `None`.

Verification planned:
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/test_attention_remote_respond.py tests/servers/test_attention_respond.py -v`.

**Acceptance:**

- 3.3.1 - Through a fake node client, responding to another machine's entry sends one `terminal.write` with the answer to that machine's terminal, clears the entry, and never captures a local pane or runs a detector. test: `tests/servers/test_attention_remote_respond.py::test_remote_respond_writes_through_node`.
- 3.3.2 - An offline owner node yields 503 `{"code": "node_offline", "stage": "none"}`, and the entry stays open. test: `tests/servers/test_attention_remote_respond.py::test_offline_owner_is_typed`.
- 3.3.3 - A request naming a stale episode gets 409 and sends nothing. test: `tests/servers/test_attention_remote_respond.py::test_stale_episode_sends_nothing`.
- 3.3.4 - A lost reply answers 502 `injection_indeterminate` and retires the entry, and a retry sends no second `terminal.write`. test: `tests/servers/test_attention_remote_respond.py::test_lost_reply_retires_entry`.
- 3.3.5 - Two concurrent responses to one entry send one `terminal.write`. test: `tests/servers/test_attention_remote_respond.py::test_concurrent_responses_send_once`.

## D1 Remote workspace lifecycle (depends: 2.5)
`kind: deferred`

A remote spawn's workspace is a `worktrees` or `clones` row with the node's
machine (Decision 15), and the hub removes it only when the spawn fails. No
existing worktree path covers a finished remote workspace:
- `LocalWorktreeManager` and `LocalCloneManager` read and write only this
  machine's rows, which keeps the hub's loops off node rows (2.1.5). So
  `list_worktrees`, `delete_worktree`, expiry cleanup, and session-end release
  never act on a node's row, and the row keeps its `agent_session_id`.
- Merge and expiry cleanup run git in this machine's checkout. A node's branch
  exists only on the node, and no v1 command moves its commits or merges
  there.
- Local cleanup removes a worktree after its merge, so removal follows the
  merge design.

Acceptance item D1.1: the hub lists, merges, and removes finished remote
workspaces through their owning node.

```yaml
deferral:
  task_ref: "TBD-remote-workspace-lifecycle"
  reason: "needs-planning: merging a node's branch needs its commits moved between machines or a node-side merge, which no v1 command or design covers; listing and removal follow that design. Created at expansion per the plan-coverage contract."
  owner: "orchestrator"
  original_acceptance_items:
    - D1.1
```

## D2 gdaemon internal node-command route over the node channel (depends: 1.1)
`kind: deferred`

The route extends `crates/gdaemon/src/nodes/` (`Registry`, `ChannelHandle`, and
the hub channel handler), which #24090 creates and which has not landed. Its
contract is 1.1's internal route. The design to carry into the task:
- `ChannelHandle` gains `commands: mpsc::Sender<OutboundCommand>`, where
  `OutboundCommand` is `{ frame: CommandFrame, reply: oneshot::Sender<ReplyFrame> }`.
  `Registry::command_sender(machine_id)` returns the sender only for a live
  handle.
- The hub channel handler writes each command frame and keeps a
  `HashMap<Uuid, oneshot::Sender<ReplyFrame>>`. It removes an entry on its
  reply, its timeout, the channel's close or replacement, or a dropped HTTP
  caller (a drop guard). It drops and logs a reply with no entry.
- `FrontDoor::handle` (`crates/gdaemon/src/front_door/mod.rs`) routes
  `/api/internal/nodes/` to `nodes::internal_api` on a hub, after the runtime
  gates and before `auth.authenticate`. `serve.rs::plaintext_allowed` shows the
  loopback test (`peer.to_canonical().is_loopback()`), and `auth.rs::AuthState`
  holds the secret.

Original acceptance:
- D2.1: a fake node on a live channel receives the command frame, and its reply reaches the caller unchanged.
- D2.2: with no live channel the route answers `node_offline` with `stage: "none"`, and a node that never replies yields `node_timeout` with `stage: null`.
- D2.3: closing or replacing the channel ends every pending command with `channel_closed`, and a reply after a timeout or a dropped caller is dropped and logged.
- D2.4: a non-loopback peer, a missing or wrong secret, or a standalone or node front door gets 404.
- D2.5: `connected` lists only machines with a live channel.

```yaml
deferral:
  task_ref: "TBD-node-command-route"
  reason: "Extends the node channel registry and handler that #24090 creates, which has not landed; an external prerequisite is a typed deferral per the plan-coverage contract."
  owner: "orchestrator"
  original_acceptance_items:
    - D2.1
    - D2.2
    - D2.3
    - D2.4
    - D2.5
```

## D3 Machine-scoped project view (depends: 3.2)
`kind: deferred`

Q4 says switching machines switches the projects, agents, and terminals view.
3.1 and 3.2 attribute every roster entry to its machine and project, and 3.2
defaults the agents view to the current machine. Josh's ruling (Decision 10)
makes the existing machine rows the machine list. The projects pane does not
follow the machine filter, and its cards show one checkout's branch and
ahead/behind counts from the daemon's project status. A node's cards need the
node's git state, which no v1 command reports. The terminals view per machine
is #21549.

Original acceptance:
- D3.1: selecting a machine limits the projects pane to projects with a checkout on that machine and shows that checkout's branch and ahead/behind counts.

```yaml
deferral:
  task_ref: "TBD-q4-machine-view"
  reason: "needs-planning: a node's project cards need its checkout's git state, which no v1 command reports; Josh's Q4 ruling (Decision 10) settled the default and the machine list."
  owner: "orchestrator"
  original_acceptance_items:
    - D3.1
```

## D4 Live hub-to-node proof (depends: 2.5, 2.6, 3.3)
`kind: deferred`

Every active section drives a fake route or a fake node. The live path needs
D2's route, #24148's node handlers, and #24150's hook forwarding.

Original acceptance:
- D4.1: a spawn from a node session runs the agent on that node, its hooks start and end its run on the hub, and the reconciler leaves it alone.
- D4.2: a cancel, a terminal write, and an attention response for that agent reach its node.

```yaml
deferral:
  task_ref: "TBD-hub-node-live-proof"
  reason: "Needs D2, #24148 (node-local spawn) and #24150 (node hook ledger), none landed; an external prerequisite is a typed deferral per the plan-coverage contract."
  owner: "orchestrator"
  original_acceptance_items:
    - D4.1
    - D4.2
```

## V1: Plan Changelog
`kind: framing`

- 2026-10-12: First draft from Josh's Q0-Q6 answers, relayed by gobby#15977.
- 2026-10-12: Revised for adversary findings HM-F1 to HM-F11 and the
  coordinator's rulings. 1.2 became deferred section D2 (#24090), the open Q4
  parts became D3, the live proof became D4, and 2.6 was added. Only proven
  pre-dispatch failures carry `stage: "none"`; post-send HTTP failures are
  `node_reply_lost` with a null stage. 2.5 records its granularity decision.
  2.6 selects unsettled terminal rows, so a terminal outlives its run.
- 2026-10-12: Consensus. Plan Adversary gobby#16491 and Plan Writer gobby#16490
  agree that HM-F1 to HM-F11 are resolved and that no blocking finding remains.
  Coordinator gobby#15977 accepted D2, D3, and D4 as typed deferrals. D3 goes to
  Josh with the stamped plan.
- 2026-10-12: Revised for Josh's review of the stamped plan, relayed by
  gobby#15977; the M1 manifest is withdrawn for re-derivation. Decision 15 had
  claimed `worktrees` rows carry no `machine_id`. Both `worktrees` and
  `clones` carry it, and every local manager method binds this machine. A
  remote workspace is now a `worktrees` or `clones` row with the node's
  machine (2.5.11), and the `resume_metadata_json` side channel is gone. 2.1.5
  pins the hub's workspace loops to this machine. D1 stays deferred as
  needs-planning: its stated prerequisite was the false premise, and merging a
  node's commits still has no command or design. Josh's Q4 ruling folds in:
  gclient defaults to the current machine (3.2.4), the existing machine rows
  are the machine list, and D3 keeps only the machine-scoped project view as
  needs-planning. 1.1 adds the Rust allowlist test (1.1.6) and the hub-client
  error codes.
- 2026-10-12: Revised for HM-F12 from Plan Adversary gobby#16491. A failed
  spawn's workspace row is marked for removal (`abandoned` worktree, `cleanup`
  clone) before its run and terminal settle, and 2.6 retries every marked row
  until the node confirms (2.6.6). `workspace.remove` is idempotent and names
  no run. The writer closed the matching run-side gap: 2.6's run sweep settles
  an active remote run whose terminals have all exited (2.6.7).
- 2026-10-12: Consensus on the revision for Josh's review. Plan Adversary
  gobby#16491 and Plan Writer gobby#16490 agree that HM-F12 and HM-F13 are
  resolved at 88619241b1. The sweep pages every active run, collects them
  before settling, and re-reads each candidate. No blocking finding remains.
  Coordinator gobby#15977 raised no objection to the five readings. Those
  readings are: no separate machine list, D3.1 and D1 deferred as
  needs-planning, the hub module for remote rows, and the row written before
  `workspace.prepare`.

## V2: Verification
`kind: verification`

These are completion gates for the implementation. Except for plan validation,
none has run yet. Each must pass after its leaves land and before #21575 closes.

```bash
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/hub/ tests/agents/test_hub_local_loops.py tests/servers/test_hook_machine_binding.py tests/servers/test_attention_roster_machines.py tests/servers/test_attention_remote_respond.py -v
cargo nextest run -p gobby-daemon -E 'binary(node_command_corpus)'
cargo nextest run -p gobby-client
uv run ruff format --check src/ && uv run ruff check src/ && uv run mypy src/
uv run gobby plans validate .gobby/plans/hub-mode.md -p .
```

- The plan validation command must pass at every revision; it is the only check
  that runs during planning.
- These gates cover the active sections, which drive a fake route or a fake
  node. D2's route checks and D4's live proof run in their own tasks.
- Run these from the worktree root. Do not run the full pytest suite.

## M1 Task Manifest
`kind: manifest`

```yaml
- title: 'Node command envelope: contract, golden corpus, and typed models'
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '1.1.1: The contract specifies both frames, all v1 commands
    with payload and result fields, the error codes with their producers and stage
    rule, including the hub-client codes 1.3 raises, the node duties, the internal
    route, and the versioning rule. file: `docs/contracts/node-commands.md`.

    1.1.2: The Rust types round-trip every corpus message byte-equivalently and reject
    an unknown field. test: `crates/gdaemon/tests/node_command_corpus.rs::corpus_round_trips`.

    1.1.3: The Python models round-trip every corpus message and reject an unknown
    field. test: `tests/hub/test_node_command_corpus.py::test_corpus_round_trips`.

    1.1.4: An `agent.spawn` payload whose `env` has a key outside the allowlist fails
    validation in the Python models. test: `tests/hub/test_node_command_corpus.py::test_spawn_env_allowlist`.

    1.1.5: `ROADMAP.md` shows S4.2 depending on #24090. file: `ROADMAP.md`.

    1.1.6: An `agent.spawn` payload whose `env` has a key outside the allowlist fails
    to deserialize in the Rust types. test: `crates/gdaemon/tests/node_command_corpus.rs::spawn_env_allowlist`.'
  labels:
  - covers:hub-mode:1.1:1.1.1
  - covers:hub-mode:1.1:1.1.2
  - covers:hub-mode:1.1:1.1.3
  - covers:hub-mode:1.1:1.1.4
  - covers:hub-mode:1.1:1.1.5
  - covers:hub-mode:1.1:1.1.6
  tdd: true
  source_section: '1.1'
  implementation_domain: backend
- title: Python node-command client and machine eligibility
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '1.3.1: Against a fake route, `send` posts the secret header
    and maps an ok reply to its result and an error reply to `NodeCommandError` with
    code and stage. test: `tests/hub/test_node_client.py::test_send_maps_replies`.

    1.3.2: Without `GOBBY_FRONT_DOOR_SECRET`, calls raise `node_routing_unavailable`
    and make no request; a 404 or a refused connection raises it too, with `stage="none"`.
    test: `tests/hub/test_node_client.py::test_unavailable_routing_is_typed`.

    1.3.3: The origin machine is the default target, and a node ref resolves to its
    machine. test: `tests/hub/test_machine_routing.py::test_default_and_explicit_targets`.

    1.3.4: A remote target without a checkout, with no live channel, or with another
    owner is refused with its typed code and never replaced by another machine. test:
    `tests/hub/test_machine_routing.py::test_ineligible_targets_never_fall_back`.

    1.3.5: After the fake route accepts a command, a 5xx, a truncated or malformed
    200, a read timeout, or a connection reset raises `node_reply_lost` with `stage=None`.
    test: `tests/hub/test_node_client.py::test_post_send_failures_are_uncertain`.'
  labels:
  - covers:hub-mode:1.3:1.3.1
  - covers:hub-mode:1.3:1.3.2
  - covers:hub-mode:1.3:1.3.3
  - covers:hub-mode:1.3:1.3.4
  - covers:hub-mode:1.3:1.3.5
  tdd: true
  source_section: '1.3'
  implementation_domain: backend
- title: Hub machine-local loops skip other machines' rows
  category: code
  task_type: bug
  depends_on: []
  validation_criteria: '2.1.1: The stale-pending reaper leaves another machine''s
    pending terminal untouched and still reaps a local one. test: `tests/agents/test_hub_local_loops.py::test_reaper_skips_remote_pending`.

    2.1.2: Terminal tmux cleanup lists only this machine''s runs. test: `tests/agents/test_hub_local_loops.py::test_tmux_cleanup_is_machine_scoped`.

    2.1.3: Post-terminal cleanup of a remote run runs the delivery, attention, and
    ownership phases and skips tmux, SRT, and sandbox reaping. test: `tests/agents/test_hub_local_loops.py::test_post_cleanup_skips_local_phases_for_remote_run`.

    2.1.4: The interactive attention monitor ignores another machine''s interactive
    session. test: `tests/agents/test_hub_local_loops.py::test_interactive_monitor_is_machine_scoped`.

    2.1.5: The expired-isolation cleanup, the missing-record sweeps, and session-end
    worktree release leave another machine''s `worktrees` and `clones` rows untouched
    while still acting on local rows. test: `tests/agents/test_hub_local_loops.py::test_workspace_loops_skip_remote_rows`.'
  labels:
  - covers:hub-mode:2.1:2.1.1
  - covers:hub-mode:2.1:2.1.2
  - covers:hub-mode:2.1:2.1.3
  - covers:hub-mode:2.1:2.1.4
  - covers:hub-mode:2.1:2.1.5
  tdd: true
  source_section: '2.1'
  implementation_domain: backend
- title: Hook ingress binds the envelope machine to the caller's key
  category: code
  task_type: bug
  depends_on: []
  validation_criteria: '2.2.1: A key-authenticated hook whose envelope names another
    machine is refused with `hook_machine_mismatch`, and one naming the key''s machine
    is processed. test: `tests/servers/test_hook_machine_binding.py::test_envelope_machine_must_match_key`.

    2.2.2: A hook without a front-door identity keeps today''s behavior. test: `tests/servers/test_hook_machine_binding.py::test_local_hook_unchanged`.'
  labels:
  - covers:hub-mode:2.2:2.2.1
  - covers:hub-mode:2.2:2.2.2
  tdd: true
  source_section: '2.2'
  implementation_domain: backend
- title: Remote terminal writes go through the node
  category: code
  task_type: feature
  depends_on:
  - '1.3'
  validation_criteria: '2.3.1: Through a fake node client, a write to another machine''s
    terminal sends one `terminal.write` with the request''s kind, data, and submit,
    and returns `Delivered`. test: `tests/hub/test_remote_terminal_write.py::test_remote_write_uses_node_command`.

    2.3.2: An offline node raises `NodeTerminalWriteError` with `node_offline` and
    `stage="none"`, and the latch is cleared. test: `tests/hub/test_remote_terminal_write.py::test_offline_node_clears_latch`.

    2.3.3: A partial node failure surfaces `stage="partial"` and keeps the latch.
    test: `tests/hub/test_remote_terminal_write.py::test_partial_stage_propagates`.

    2.3.4: A local terminal write never contacts the client. test: `tests/hub/test_remote_terminal_write.py::test_local_write_unchanged`.

    2.3.5: When a fake node takes the write and its reply is lost (`node_timeout`)
    or unreadable (`node_reply_lost`), the write returns `IndeterminateWrite`, the
    latch stays, and a retry with the same action key sends no second `terminal.write`.
    test: `tests/hub/test_remote_terminal_write.py::test_lost_reply_is_indeterminate`.'
  labels:
  - covers:hub-mode:2.3:2.3.1
  - covers:hub-mode:2.3:2.3.2
  - covers:hub-mode:2.3:2.3.3
  - covers:hub-mode:2.3:2.3.4
  - covers:hub-mode:2.3:2.3.5
  tdd: true
  source_section: '2.3'
  implementation_domain: backend
- title: Remote cancel goes through the node
  category: code
  task_type: feature
  depends_on:
  - '1.3'
  validation_criteria: '2.4.1: Killing another machine''s run sends `agent.cancel`
    with the signal and `close_terminal`, and never signals a local PID. test: `tests/hub/test_remote_cancel.py::test_remote_run_cancels_through_node`.

    2.4.2: An offline node returns `error_code: node_offline`. test: `tests/hub/test_remote_cancel.py::test_offline_node_is_typed`.

    2.4.3: With `close_terminal`, a stopped reply settles the terminal row as a local
    close would. test: `tests/hub/test_remote_cancel.py::test_close_terminal_settles_row`.

    2.4.4: A `not_found` reply returns success like a stopped one. test: `tests/hub/test_remote_cancel.py::test_not_found_counts_as_stopped`.

    2.4.5: A `{stopped: false}` reply returns `error_code: not_stopped` and leaves
    the terminal row unchanged. test: `tests/hub/test_remote_cancel.py::test_not_stopped_is_failure`.'
  labels:
  - covers:hub-mode:2.4:2.4.1
  - covers:hub-mode:2.4:2.4.2
  - covers:hub-mode:2.4:2.4.3
  - covers:hub-mode:2.4:2.4.4
  - covers:hub-mode:2.4:2.4.5
  tdd: true
  source_section: '2.4'
  implementation_domain: backend
- title: Remote spawn goes through the node
  category: code
  task_type: feature
  depends_on:
  - '1.3'
  - '2.1'
  - '2.2'
  - '2.4'
  validation_criteria: '2.5.1: Through a fake node client, a spawn from a session
    on another machine sends `workspace.prepare` and then `agent.spawn` to that machine,
    and its session, run, and terminal rows carry that `machine_id` before `agent.spawn`
    is sent. test: `tests/hub/test_remote_spawn.py::test_spawn_routes_to_requesting_machine`.

    2.5.2: The `agent.spawn` payload carries no managed credential and no env key
    outside the allowlist, and the hub writes no prompt file. test: `tests/hub/test_remote_spawn.py::test_payload_carries_no_credential`.

    2.5.3: An explicit `machine` is validated, and an ineligible one fails with its
    typed code without spawning anywhere. test: `tests/hub/test_remote_spawn.py::test_explicit_machine_is_validated`.

    2.5.4: A `stage: "none"` failure of `agent.spawn` marks the workspace row for
    removal before it fails the pending terminal, cancels the run, and ends the lease,
    then sends `workspace.remove` and deletes the row after `{removed: true}`, with
    no local process or pane call. A `stage: "none"` failure of `workspace.prepare`
    deletes the row before it settles anything and sends no `workspace.remove`. test:
    `tests/hub/test_remote_spawn.py::test_definite_failure_settles_in_database`.

    2.5.5: A `node_timeout` or a `node_reply_lost` on `agent.spawn` returns `spawn_in_doubt`
    and leaves the run and terminal pending, the lease attached, and the workspace
    and its row in place, with no rollback. test: `tests/hub/test_remote_spawn.py::test_in_doubt_spawn_keeps_state`.

    2.5.6: A node caller whose parent is on the hub spawns on the node, and a hub
    caller whose parent is on a node spawns locally in the hub''s checkout. test:
    `tests/hub/test_remote_spawn.py::test_origin_is_the_calling_session`.

    2.5.7: Remote resume and placement are refused with typed codes. test: `tests/hub/test_remote_spawn.py::test_unsupported_routes_refused`.

    2.5.8: A spawn whose target is this machine takes the local path unchanged. test:
    `tests/hub/test_remote_spawn.py::test_local_target_unchanged`.

    2.5.9: With a checkout on the target and none on the hub, both the MCP tool and
    `POST /api/agents/spawn` spawn on the target. test: `tests/hub/test_remote_spawn.py::test_remote_target_needs_no_hub_checkout`.

    2.5.10: When the agent''s session end arrives before the spawn reply, the reply
    leaves the run ended, exits the terminal, and records no `pid`. test: `tests/hub/test_remote_spawn.py::test_session_end_before_reply_stays_settled`.

    2.5.11: Before `workspace.prepare` is sent, an isolated remote spawn has a `worktrees`
    row, or a `clones` row in clone mode, carrying the target''s `machine_id`, the
    path, the branch, the task, and the child session; the run''s resume metadata
    and the task''s artifacts name no remote workspace, and the hub''s `list_worktrees()`
    does not return the row. test: `tests/hub/test_remote_spawn.py::test_remote_workspace_is_a_machine_row`.'
  labels:
  - covers:hub-mode:2.5:2.5.1
  - covers:hub-mode:2.5:2.5.2
  - covers:hub-mode:2.5:2.5.3
  - covers:hub-mode:2.5:2.5.4
  - covers:hub-mode:2.5:2.5.5
  - covers:hub-mode:2.5:2.5.6
  - covers:hub-mode:2.5:2.5.7
  - covers:hub-mode:2.5:2.5.8
  - covers:hub-mode:2.5:2.5.9
  - covers:hub-mode:2.5:2.5.10
  - covers:hub-mode:2.5:2.5.11
  tdd: true
  source_section: '2.5'
  implementation_domain: backend
- title: The hub settles remote runs from the node's inventory
  category: code
  task_type: feature
  depends_on:
  - '1.3'
  - '2.1'
  - '2.5'
  validation_criteria: '2.6.1: A listed run with a pending terminal is promoted live
    with the inventory''s identity, and its `pid` is recorded. test: `tests/hub/test_remote_reconcile.py::test_listed_pending_run_is_promoted`.

    2.6.2: An unlisted run with a live terminal, or a live row from an older `host_epoch`,
    is left on its first absent pass and settled on the second through `mark_exited`
    and `cleanup_agent`; a run that settles in between is not failed, and its terminal
    still exits. test: `tests/hub/test_remote_reconcile.py::test_absent_live_run_settles_after_one_pass`.

    2.6.3: An unlisted pending run past the deadline is sent `agent.cancel` first.
    `{stopped: true}` or `not_found` then marks its workspace row for removal, fails
    its terminal and run, and sends `workspace.remove`, and `node_timeout` or `{stopped:
    false}` changes nothing. test: `tests/hub/test_remote_reconcile.py::test_in_doubt_spawn_is_fenced_before_settlement`.

    2.6.4: A machine without a live channel gets no command and its rows stay as they
    are, and local rows are never inventoried. test: `tests/hub/test_remote_reconcile.py::test_disconnected_and_local_rows_untouched`.

    2.6.5: In this case the spawn''s effect lands and its reply is lost, and the agent''s
    session start and session end both reach the hub before the first pass. Past the
    deadline the pending terminal is fenced with `agent.cancel`. After `not_found`
    it exits, the run stays ended, and there is no `cleanup_agent` and no `workspace.remove`.
    A later `agent.spawn` for that run is refused `cancelled` by the fake node. After
    `{stopped: false}` or `node_timeout` the terminal stays pending. The same row
    on a disconnected machine stays untouched. test: `tests/hub/test_remote_reconcile.py::test_ended_run_terminal_still_settles`.

    2.6.6: In worktree and clone mode, a failed spawn''s marked row survives a first
    `workspace.remove` that answers `node_timeout`, an error, or `{removed: false}`.
    A new reconciler, standing in for a hub restart, retries it on its next pass after
    the terminal has exited and the run has ended. When the removal''s effect landed
    and only its reply was lost, the retry answers `{removed: true}` and the row is
    deleted. A disconnected machine''s marked row and an ended run''s unmarked row
    are never sent `workspace.remove`. test: `tests/hub/test_remote_reconcile.py::test_marked_workspace_removal_is_retried`.

    2.6.7: An active remote run whose terminal rows have all exited, as a hub restart
    between `fail_pending` and the run''s settlement leaves it, is left on the first
    pass and settled through `cleanup_agent` on the second, including when 100 healthy
    active runs on that machine put it past the first page. A run that completes,
    or gains a pending or live terminal, between the two passes is not swept, and
    a run with no terminal row is never swept. test: `tests/hub/test_remote_reconcile.py::test_run_left_active_after_terminal_exit_settles`.'
  labels:
  - covers:hub-mode:2.6:2.6.1
  - covers:hub-mode:2.6:2.6.2
  - covers:hub-mode:2.6:2.6.3
  - covers:hub-mode:2.6:2.6.4
  - covers:hub-mode:2.6:2.6.5
  - covers:hub-mode:2.6:2.6.6
  - covers:hub-mode:2.6:2.6.7
  tdd: true
  source_section: '2.6'
  implementation_domain: backend
- title: Hub-wide roster with each entry's machine and project
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '3.1.1: The roster lists live runs and sessions from every
    machine. test: `tests/servers/test_attention_roster_machines.py::test_roster_is_hub_wide`.

    3.1.2: Every entry carries the machine and project of its run or session. test:
    `tests/servers/test_attention_roster_machines.py::test_entries_carry_machine_and_project`.

    3.1.3: Responding to a local entry still resolves its provider''s detector through
    the hub-wide rows. test: `tests/servers/test_attention_roster_machines.py::test_local_respond_resolves_detector`.'
  labels:
  - covers:hub-mode:3.1:3.1.1
  - covers:hub-mode:3.1:3.1.2
  - covers:hub-mode:3.1:3.1.3
  tdd: true
  source_section: '3.1'
  implementation_domain: backend
- title: gclient attributes agents by the roster's machine and project
  category: code
  task_type: feature
  depends_on:
  - '3.1'
  validation_criteria: '3.2.1: An agent whose session row is outside the fetched window
    is listed under its roster machine. test: `crates/gclient/tests/roster_machines.rs::agents_use_roster_machine`.

    3.2.2: A remote machine''s row shows attention when one of its agents needs attention.
    test: `crates/gclient/tests/roster_machines.rs::remote_machine_row_shows_attention`.

    3.2.3: A remote agent in project B, absent from both fetched windows while project
    A is focused, stays in project B and is not admitted to project A''s view. test:
    `crates/gclient/tests/roster_machines.rs::agents_use_roster_project`.

    3.2.4: A new window lists only the current machine''s agents, and choosing all
    machines lists every machine''s agents. test: `crates/gclient/tests/roster_machines.rs::new_window_defaults_to_current_machine`.'
  labels:
  - covers:hub-mode:3.2:3.2.1
  - covers:hub-mode:3.2:3.2.2
  - covers:hub-mode:3.2:3.2.3
  - covers:hub-mode:3.2:3.2.4
  tdd: true
  source_section: '3.2'
  implementation_domain: frontend
- title: Attention respond on another machine's entry
  category: code
  task_type: feature
  depends_on:
  - '2.3'
  - '3.1'
  validation_criteria: '3.3.1: Through a fake node client, responding to another machine''s
    entry sends one `terminal.write` with the answer to that machine''s terminal,
    clears the entry, and never captures a local pane or runs a detector. test: `tests/servers/test_attention_remote_respond.py::test_remote_respond_writes_through_node`.

    3.3.2: An offline owner node yields 503 `{"code": "node_offline", "stage": "none"}`,
    and the entry stays open. test: `tests/servers/test_attention_remote_respond.py::test_offline_owner_is_typed`.

    3.3.3: A request naming a stale episode gets 409 and sends nothing. test: `tests/servers/test_attention_remote_respond.py::test_stale_episode_sends_nothing`.

    3.3.4: A lost reply answers 502 `injection_indeterminate` and retires the entry,
    and a retry sends no second `terminal.write`. test: `tests/servers/test_attention_remote_respond.py::test_lost_reply_retires_entry`.

    3.3.5: Two concurrent responses to one entry send one `terminal.write`. test:
    `tests/servers/test_attention_remote_respond.py::test_concurrent_responses_send_once`.'
  labels:
  - covers:hub-mode:3.3:3.3.1
  - covers:hub-mode:3.3:3.3.2
  - covers:hub-mode:3.3:3.3.3
  - covers:hub-mode:3.3:3.3.4
  - covers:hub-mode:3.3:3.3.5
  tdd: true
  source_section: '3.3'
  implementation_domain: backend
```
