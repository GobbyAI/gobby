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

When every section closes:
- A versioned hub-to-node command envelope and its loopback gdaemon route are a
  committed contract with a golden corpus.
- The Python hub sends a spawn, a cancel, or a terminal write aimed at another
  machine to that machine's node through the route's client, or fails with a
  typed code. It never runs the work on the hub instead. A spawn is one
  command: the node prepares the worktree or clone and launches the agent.
- A node writes nothing to the database. It registers its checkouts and
  reports its accepted runs, live agents, workspaces, and exits through two
  hub routes authenticated by its machine-bound key, and the hub settles each
  remote run from those reports.
- The roster lists every machine's agents, each tagged with its machine and
  project, and every machine's row in gclient carries its attention state.
- Hub loops that touch local panes, processes, or files act only on their own
  machine's rows.
- The hub lists, merges, and removes a node's finished worktrees and clones
  through that node, and expiry cleanup reaches them (P4).
- Selecting a machine in gclient switches the projects pane to that
  machine's checkouts, with their branch and ahead/behind counts (P4).
- A live test pairs an isolated hub with an isolated node and drives a
  spawn, its hooks, a terminal write, an attention response, a cancel, a
  merge and a removal end to end for each of the five providers (P5).

1.2 builds the gdaemon route on the node channel that #24090 (Node channel)
landed. The node handlers are #24148 (S2.7 node half: node-local agent spawn
and worktrees in gdaemon), planned in `.gobby/plans/node-halves.md`. A node
agent's hooks reach the hub through #24150 (S2.11 node half: the node-local
envelope ledger forwards each envelope to the hub), planned there too. P1 to
P4 test against a fake route or a fake node, and P5 runs the real pair.
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
   - Python callers for spawn, cancel, and terminal write. A spawn names its
     worktree or clone, and the node prepares it (Josh's review of the stamped
     plan, Decision 13).

   The envelope and the gdaemon route are durable, and #24149 reuses them. The
   Python callers live in the `src/gobby/hub/` package and are transitional.
3. **Q2 a. Eligibility.** A machine is eligible for a project when a
   `project_checkouts` row exists for (project, machine) and the gdaemon registry
   reports its channel live, read through the internal route (1.1 contract, 1.2).
   A node registers its checkout rows through 1.4's route, and 1.3 reads them
   (Decision 18).
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
    so no `/api/machines` consumer is added. The projects pane follows the
    machine filter too (4.7, 4.8). Its cards show one checkout's branch and
    ahead/behind counts, and for a node's checkout the hub asks the node
    (Decision 21).
11. **Hub-originated runs.** Builds, cron runs, and pipelines execute on the hub
    runner. Their machine-local steps (integration workspace, merge, branch
    cleanup, pipeline exec steps) run on the hub's machine, so that is their
    origin machine. Cron runs already record it (`cron_runs.machine_id`). Their
    dispatched agents keep spawning locally, so this plan changes nothing for
    them. Their integration stays on the hub's machine by design: a build's
    integration workspace, merge, and branch cleanup run git in the project's
    checkout on the machine that runs the build, and the hub runner runs every
    build.
12. **Python-to-gdaemon authentication.** gdaemon already gives its Python
    backend `GOBBY_FRONT_DOOR_SECRET` (`crates/gdaemon/src/state.rs`). The
    internal route admits a request only from a loopback peer that presents that
    secret in `X-Gobby-Backend-Secret`, compared in constant time. Every other
    request gets 404. The route is never proxied, and the header is not in
    `IDENTITY_HEADERS`.
13. **A remote spawn is one command.** Josh asked of the stamped plan,
    verbatim: "Why wouldn't the hub just send a command to the node to
    spawn_agent?" Most of `spawn_agent_impl` touches the local machine: git,
    worktree preparation, cargo checks, placement, endpoint and provider
    preparation (`prepare_claude_spawn` reads `.mcp.json` and pre-approves the
    directory in the local provider config). So a remote spawn is its own flow
    in `src/gobby/hub/remote_spawn.py`:
    - The hub performs admission and the task spawn lease, pre-registers the
      session, the run, and a pending terminal row, then sends one
      `agent.spawn`.
    - The node accepts it and replies at once. It then prepares the worktree or
      clone, launches the provider command at its SRT chokepoint, and removes
      what it prepared when a step before launch fails (#24148). Results reach
      the hub in the node's reports (Decision 18), so a slow clone never holds
      the command open.

    The two request-facing callers (the MCP `spawn_agent` tool and
    `POST /api/agents/spawn`) choose the remote flow. The origin machine is the
    calling session's, so a node agent that reports to a hub coordinator spawns
    on its node. The parent session is only the reporting relationship. Dispatch,
    cron, and feedback spawns stay local (decision 11). A resume routes to the
    resumed session's machine, overriding `machine` and the origin, because
    the provider thread files live there, and it runs in the workspace that
    session's earlier run used (node-halves Decision 23). A remote spawn
    refuses placement with a typed error, because placement is a
    gclient-local pane feature.
14. **No datastore credential crosses to a node** (ROADMAP decision 17). A
    remote spawn issues no managed credential, writes no prompt file, and
    creates no directory on the hub. `agent.spawn` carries the run's identity
    as typed fields, and the node sets the `GOBBY_*` variables from them, so
    there is no environment map. The local flow's `get_terminal_env_vars`
    creates the Cargo target and spawn-cache directories
    (`ensure_agent_cargo_target_dir`, `build_spawn_cache_env`), so the remote
    flow never calls it (2.5).
15. **A remote workspace is a `worktrees` or `clones` row with the node's
    machine.** Both tables already carry `machine_id uuid NOT NULL`, with unique
    indexes on `(machine_id, worktree_path)`, `(project_id, branch_name,
    machine_id)`, and `(machine_id, clone_path)`. Every `LocalWorktreeManager`
    and `LocalCloneManager` method binds `require_machine_id()`, so the hub's
    workspace loops already skip a node's rows (2.1.5 pins it). Those managers
    stay this-machine-only; a machine keyword on them would widen every
    consumer's contract and force a split of the 852-line
    `src/gobby/storage/worktrees.py`.
    - The node creates the workspace, and it removes the workspace when its
      spawn fails before launch.
    - 1.4's report route inserts the row before it answers 200, from a live
      agent's entry or a launched exit that names the workspace. The node
      repeats an exit until a report carrying it gets 200, so an agent that
      launches and exits between two health passes, or a hub restart after
      the 200, loses no row. A row exists only for a workspace that held a
      launched agent.
    - The hub finishes a remote workspace through its node (P4). The node
      merges the branch into its own checkout and removes the workspace, and
      the hub updates or deletes the row only after the node answers
      (Decision 20). The managers above stay this-machine-only, so
      `src/gobby/hub/remote_workspaces.py` reads and writes node rows
      directly, and the workspace tools route a node's row there.
16. **Hook machine binding.** Q3 trusts `sessions.machine_id`, which comes from
    the hook envelope (`ClaudeCodeAdapter` reads `input_data["machine_id"]`).
    When a hook request carries a verified front-door identity, the hub rejects
    an envelope whose `machine_id` is missing or differs from the key's machine
    (2.2). A hook from another machine then binds only that machine's session
    rows: every hook lookup that matches a row by an id the event supplies
    treats another machine's row as a miss, before any write (2.2). A local
    hook binds any row, as today. The envelope machine is the marker because it
    reaches every adapter's event and every replayed envelope.
17. **Every section is in scope (Josh's "No deferrals" ruling, memory
    878d0c35, relayed by gobby#15977 on 2026-10-13).** The gdaemon route is
    1.2: it extends the node channel's registry and handler, which #24090
    (Node channel: hub registers, heartbeats, replaces, revokes) landed. 1.1
    specifies the route's HTTP contract, so 1.3 builds its client against it
    and tests it against a fake route. The client answers
    `node_routing_unavailable` whenever the route answers 404, which includes
    a standalone or node front door. The node half of every command is in
    `.gobby/plans/node-halves.md` (#24148, #24150), and work this plan needs
    from it is a cross-plan edge under Constraints, which the Orchestrator
    sets at expansion. P5 is the live proof. 1.1 added the S4.2 ← #24090
    edge to `ROADMAP.md`.
18. **A node writes through key-bound hub routes, and the hub owns every
    row.** Josh asked of the stamped plan, verbatim: "I'm still wondering why
    the node can't access the hub api with an api key? Does it need database
    writes?" It needs none. A node calls two hub routes with its
    machine-bound key (ROADMAP decision 17), and the hub writes the rows:
    - `POST /api/nodes/checkouts` registers the node's checkout of a project,
      which Q2 eligibility reads (1.4).
    - `POST /api/nodes/report` carries the node's accepted runs, live agents
      with their workspaces, and exits, every 30 s and after each change
      (1.4). The route admits only a report built against the current hub
      process's `hub_epoch` with a revision above the machine's high-water,
      records the reported workspace rows, and only then answers 200. The
      reconciler settles remote runs from it (2.6).

    The hub's storage refuses another machine's rows by design:
    `_SessionCRUDMixin.register`, `register_session`, and `require_root` call
    `require_local_machine_id`. Orchestrator gobby#15977 ruled on 2026-10-12
    that this plan adds the key-verified foreign-machine path, limited to three
    uses:
    - The hub pre-registers a node session only after Q2 routing validation
      (2.5).
    - A node registers its own checkout through the key-bound route (1.4).
    - Routing reads a validated remote machine's checkout row with no local
      guard and no filesystem access (1.3).
      `LocalProjectCheckoutManager.get(machine_id, project_id)` already does
      exactly that, so `require_root` and its callers stay unchanged.

    The report route's workspace rows (1.4, Decision 15) add no fourth use.
    Each takes its machine and project from a terminal row the hub created
    under the first use, on the key's machine and naming the reported run, and
    the route inserts it directly without calling a guarded manager.

    P4's remote workspace writes add no fourth use either. They act only on
    `worktrees` and `clones` rows the report route created, keyed by the
    row's own `machine_id`. The hub marks a row merged or deletes it only
    after that machine's node answered the outcome over its channel, whose
    key is the machine's (1.2). No P4 write reads the node's filesystem or
    calls a guarded manager, and a user request names a row by id, never a
    machine to write.

    The machine comes only from the verified key
    (`FrontDoorIdentity.machine_id`). No node-to-hub body carries a machine
    id, so a node writes only its own machine's rows. Both routes refuse the
    hub's own machine, whose rows keep their on-disk validation. The hub cannot
    verify a node's filesystem and does not try: the key is the authority for
    its machine's rows. Every other caller keeps `require_local_machine_id`,
    and 1.4.4 and 2.5.10 pin it. These consumers reuse this path rather than
    build their own:
    - #21585 (Projects, /api/files, and hub documents route family);
    - #21562 (Sessions and transcripts);
    - #24151 (S2.11 hub half: hook ingress and rule engine in Rust), which
      owns registering a forwarded session the hub did not pre-register.

    The implementation of 1.4 and 2.5 needs a security review.
19. **A node's agent ceiling is `max_active_agents`, and a report always fits
    its bound.** Josh ruled on HM-F16 through Orchestrator gobby#15977 on
    2026-10-12, verbatim: "Cap at 100, configurable. We already have a value
    for this in the config store."
    - The key is the existing `max_active_agents` (`DaemonConfig`). Its code
      default stays 20, and the live config store sets 100 (desired and
      active, revision 151). It keeps its per-project cap
      (`agent_slot_cap_refusal`), and 2.5 also applies it per remote machine:
      under a per-machine lock, a remote spawn counts the target's pending,
      live, and orphaned terminal rows and, at the ceiling, refuses with
      `machine_cap_reached` and `stage: "none"` before writing any row. A
      node runs only gdaemon and reads no config store, so the hub enforces
      it. No key is added.
    - Each list in a node report keeps a fixed protocol bound of 1,000 entries
      in the Rust and Python models (1.1). `max_active_agents` gains
      `le=1000`, so the configured ceiling never exceeds what one complete
      report can carry. A value above 1,000 fails config validation. Lowering
      the value below a node's live count only gates new admissions, and the
      runs already there were admitted under an earlier value of at most
      1,000.
    - The protocol bound is fixed, not tied to the live value, because
      lowering a tied bound while runs are live would make a node's complete
      reports invalid and stop that machine's reconciliation.
    - The hub's count can trail the node's by the agents whose session end
      reached the hub before their process exited (2.5). So the node also
      refuses `agent.spawn` with `command_failed` and `stage: "none"` while
      its accepted and live runs total 1,000 (1.1). That keeps `accepted` and
      `agents` within the bound in every case, and the hub's ceiling is the
      operator's policy, exact at admission.
    - `exits` is not a snapshot: the node sends at most 1,000 per report,
      oldest first, and the rest follow in later reports.
20. **A node merges and removes its own workspaces (P4).** The branch, the
    workspace, and the checkout it branched from all live on the node, so the
    node merges into its own checkout, as a local merge lands in the local
    checkout. No git transport or credential crosses machines, and the hub
    may hold no checkout of the project. Rejected: moving the node's commits
    to the hub by bundle or push, which lands the work in a different
    checkout than it branched from and adds a transport.
    - The node's merge is stricter than `_merge_worktree_impl`. It refuses
      when the target checkout has staged entries or dirty paths that the
      incoming change touches, it stashes nothing (the local merge stashes
      `.gobby/`), and it gives no schema-identity cutover advisory, because a
      node runs no Python daemon. On a conflict it aborts the merge and
      restores the checkout's original branch.
    - Expected refusals are ok results with an `outcome`, so callers branch
      on data: `workspace.merge` answers `merged`, `already_merged`,
      `conflict`, `target_dirty`, or `busy`, and `workspace.remove` answers
      `removed`, `dirty`, `unmerged`, or `busy`. Error replies are only
      `invalid_payload`, `not_found`, and `command_failed` with its stage.
    - A workspace is `busy` while an accepted or live run, or a launched
      workspace record whose exit no report has delivered yet, names its
      path, or while another merge or removal holds its path or its
      checkout. A launched record lasts until a report carrying the run's
      exit gets 200 (1.1), so after a removal no report names the path again
      and 1.4's route never re-inserts a removed row.
    - The hub marks a row `merged` on `merged` or `already_merged`, with
      `cleanup_after` now for a worktree and seven days on for a clone, as
      the local tools do. It deletes the row on `removed`. It never clears
      `agent_session_id` on a node's row, because node-halves 3.2's
      `resumed_workspace` reads it.
    - The gobby-worktrees and gobby-clones get, list, merge, and delete
      tools, the REST list and delete routes, and the `gobby worktrees` and
      `gobby clones` CLI, which posts to those tools, reach a node's row
      through the hub module. Every other workspace tool keeps answering
      `machine_ownership_mismatch` for a node's row by design: those tools
      create, claim, release, link, sync, push, or reclassify a workspace on
      the hub's machine, and a node's workspace changes only through its own
      agents and the two commands above. Publishing a node's branch would
      use the node's git credentials, which the hub never directs (ROADMAP
      decision 17).
21. **The hub asks a node for its checkout's git state on demand (P4).**
    `checkout.status` runs the same `git for-each-ref` on the node that
    `get_status` runs locally. The hub caches each machine and project's
    answer for 30 s, as the local status is cached. When the node is offline
    or the command fails, the status answers 200 with null git fields, so an
    offline node's card shows no branch and never a stale one. Rejected: a
    git-state field in the node's report. The inbox's `take` empties each
    snapshot (1.4), so it needs a new store; the node would run git for every
    checkout every 30 s whether or not anyone looks; and an offline node
    would keep showing its last state.
22. **The live proof pairs isolated daemons and fake providers (P5).** Every
    spawn runs under managed SRT (`require_managed_srt`), so the live test
    links the operator's installed SRT into both isolated homes, as
    `tests/e2e/conftest.py::link_operator_srt` does, and skips when it is
    absent. Each of the five providers is a fake script on the node's
    `PATH` that the real launch path starts. Rejected: the real provider
    CLIs, which need credentials, network access, and admission. The hub
    and the node run as test instances with temporary `HOME` and
    `GOBBY_HOME`, so the checkout, project, and rows the test registers live
    only in the isolated hub's database, and nothing touches the operator's
    hub or `~/.gobby` (memory 68359d5c's live-daemon gate does not apply).

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
- **Landed `nodes/` module:** #24090 (Node channel) and #24091 landed
  `crates/gdaemon/src/nodes/`: `Registry`, `Slot`,
  `ChannelHandle { connection_id, close, live }`, `register`, `remove`,
  `persist`, `mark_live`, `is_connected`, `connected_ids`, and the hub and
  node channel loops. Only hello, ack, and ping/pong cross the channel
  today (`.gobby/plans/gdaemon-node-channel.md`, Decision 9); 1.2 adds the
  command frames. #24268 (1.1) landed the version 1 contract.
- **Cross-plan edges with `.gobby/plans/node-halves.md`.** The Orchestrator
  sets these at expansion; the coverage grammar carries no cross-plan
  dependency.
  - node-halves 3.1 blocks 1.2, 1.3, and 1.4, which build or check `v`
    against the `CONTRACT_VERSION` it sets to 2. node-halves 2.1 also
    blocks 1.2, because both declare a module in
    `crates/gdaemon/src/nodes/mod.rs`.
  - 1.2 blocks node-halves 2.7, because both change
    `crates/gdaemon/src/nodes/channel.rs` (1.2 `hub_channel`, 2.7
    `run_client`).
  - node-halves 3.2 blocks 2.5, which calls its `build_spawn_payload`.
  - 1.4 blocks node-halves 3.2, whose `resumed_workspace` orders a
    session's workspace rows by the `updated_at` that 1.4 moves (1.4.7),
    and node-halves 3.3, which adds one call to 1.4's report route.
  - 2.3 and 2.4 block node-halves 3.4, whose runtime relies on 2.3's write
    guard and calls 2.4's `remote_cancel`. 2.6 blocks node-halves 3.6, so a
    run its terminate route kills settles through 2.6's reconciler.
  - node-halves 3.1 blocks 4.1, which bumps the same contract to version 3
    in the same files.
  - node-halves 2.14, the last node agent leaf, blocks 4.2 and 4.6, which
    add handlers to `crates/gdaemon/src/nodes/agents/commands.rs` and
    `crates/gdaemon/src/nodes/agents/mod.rs`.
  - Every node-halves leaf blocks 5.1, which runs the real node.
- **Hooks from node agents.** A remote agent's hooks reach the hub's
  `POST /api/hooks/execute` only through #24150's node ledger (ROADMAP decision
  17: `ghook` posts through the local node).
  - The node sets `GOBBY_SESSION_ID` from the spawn's `session_id` (1.1), and
    `ghook` copies it into `terminal_context.gobby_session_id`
    (`crates/ghook/src/terminal_context.rs`).
  - `handle_session_start` finds the hub's pre-registered row by that id and
    takes `handle_pre_created_session`. That path updates the row (`update`,
    `backfill_terminal_context`) and starts the run. It never calls `register`,
    and neither method checks the machine, so 2.2 checks it at every hook
    lookup: a hook from another machine binds only that machine's rows, and a
    foreign row counts as a miss. Session end settles the run as it does
    locally.
  - A node session that misses that lookup reaches `register_session`, which
    keeps `require_local_machine_id` and is refused (Decision 18). Examples are
    a `/clear` successor and an operator's own CLI on the node; #24151 owns
    that path.
  - 2.6 settles a run whose agent left no session end.
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

## Architecture
`kind: framing`

Josh asked for a Mermaid diagram of the hub and node architecture with user
story examples. The hub owns the database. A node keeps only machine-local
state: its terminal host and agent processes, its worktrees and clones, its
durable cancel records, and its hook ledger (#24150).

Two paths cross between machines:
- **Commands, hub to node.** The Python hub calls gdaemon's loopback internal
  route (1.2), which sends a command frame over the node's channel (#24090)
  and returns the node's reply. The commands are `agent.spawn`,
  `agent.cancel`, and `terminal.write` (1.1), and `workspace.merge`,
  `workspace.remove`, and `checkout.status` (4.1).
- **HTTP, node to hub.** The node calls the hub's front door with its
  machine-bound API key: checkout registration, run reports, and forwarded
  hooks. gdaemon authenticates the key and passes the verified identity to
  Python, which writes the rows.

```mermaid
flowchart LR
  subgraph HubMachine["Hub machine"]
    GC["gclient"]
    FD["gdaemon front door, hub mode"]
    PY["Python hub: routes, remote spawn, reconciler"]
    DB[("PostgreSQL: sessions, agent_runs, terminals, worktrees, clones, project_checkouts")]
  end
  subgraph NodeMachine["Node machine"]
    NG["gdaemon, node mode: node handlers (#24148)"]
    TH["terminal host and agent CLI"]
    GH["ghook"]
    WS["worktree or clone on the node's disk"]
  end
  GC -- "HTTP with API key" --> FD
  FD -- "proxy with verified identity" --> PY
  PY --> DB
  PY -- "internal route (1.2): agent, terminal, workspace, and checkout commands" --> FD
  FD == "node channel, WebSocket (#24090)" ==> NG
  NG -- "API key: POST /api/nodes/checkouts, POST /api/nodes/report" --> FD
  GH -- "hook envelope" --> NG
  NG -- "forwarded hooks (#24150): POST /api/hooks/execute" --> FD
  NG --> TH
  NG --> WS
```

**Story 1: spawn on another machine from gclient.** Josh works in gclient
on the hub machine and starts an agent for project P on his laptop, node N.
N registered its checkout of P earlier. The spawn returns a pending run at
once; the agent appears in the roster under N after N's next report.

```mermaid
sequenceDiagram
  actor U as Josh in gclient
  participant FD as Hub front door
  participant PY as Python hub
  participant DB as Hub database
  participant N as Node N gdaemon
  U->>FD: POST /api/agents/spawn with machine N
  FD->>PY: proxied with the user's verified identity
  PY->>DB: read N's checkout row for P
  PY->>FD: GET /api/internal/nodes/connected
  PY->>DB: pre-register session, run, and pending terminal on N
  PY->>FD: agent.spawn with the run identity and workspace intent
  FD->>N: command frame on the node channel
  N-->>FD: reply accepted
  FD-->>PY: reply frame
  PY-->>U: run id, pending
  N->>N: prepare the worktree, launch the agent
  N->>FD: POST /api/nodes/report listing the agent and its worktree
  FD->>PY: proxied with N's verified identity
  PY->>DB: insert the worktree row, then answer 200
  Note over PY: the inbox keeps N's latest report
  PY->>DB: next health pass promotes the terminal
  N->>FD: forwarded session-start hook
  FD->>PY: POST /api/hooks/execute
  PY->>DB: pre-created session found, run running
```

**Story 2: answer an attention entry for a node's agent.** The agent on N
asks a question. Josh sees N's machine row in gclient show attention, opens
it, and answers. The answer is typed into the agent's pane on N.

```mermaid
sequenceDiagram
  actor U as Josh in gclient
  participant PY as Python hub
  participant DB as Hub database
  participant N as Node N gdaemon
  participant A as Agent on N
  A->>N: hook, waiting for input
  N->>PY: forwarded hook
  PY->>DB: attention entry for the agent's session on N
  U->>PY: GET roster, hub-wide, each entry with its machine
  Note over U: N's machine row shows attention
  U->>PY: POST attention respond
  PY->>DB: check the stored episode
  PY->>N: terminal.write through the internal route and channel
  N->>A: type the answer into the pane
  N-->>PY: ok
  PY->>DB: clear the episode
  PY-->>U: accepted
```

**Story 3: node offline mid-run.** N loses its network while its agent keeps
working. The hub changes nothing for N's rows, refuses commands for N with a
typed `node_offline`, and catches up from N's first report after the channel
returns.

```mermaid
sequenceDiagram
  actor U as Josh in gclient
  participant PY as Python hub
  participant DB as Hub database
  participant FD as Hub front door
  participant N as Node N gdaemon
  participant A as Agent on N
  Note over FD,N: the channel drops
  N-xFD: reports and hooks fail
  A->>N: hooks queue in the node ledger
  Note over PY: health passes get no report from N
  PY->>DB: N's rows stay as they are
  U->>PY: respond to an entry on N
  PY->>FD: terminal.write
  FD-->>PY: node_offline, stage none
  PY-->>U: 503 node_offline, the entry stays open
  Note over FD,N: the channel returns
  N->>FD: queued hooks forwarded
  N->>FD: POST /api/nodes/report with agents and exits
  PY->>DB: next pass settles reported exits and keeps live agents
```

**Story 4: merge and remove a node's worktree.** The agent on N finished.
Josh asks the hub to merge its worktree; N merges the branch into its own
checkout of P, and the hub marks the row merged. The hub's next expiry pass
asks N to remove the worktree and deletes the row when N confirms.

```mermaid
sequenceDiagram
  actor U as Josh
  participant PY as Python hub
  participant DB as Hub database
  participant N as Node N gdaemon
  U->>PY: merge_worktree with the row's id
  PY->>DB: read the row, owned by N, and N's checkout of P
  PY->>N: workspace.merge through the internal route
  N->>N: merge the branch into its checkout of P
  N-->>PY: outcome merged
  PY->>DB: mark the row merged, cleanup now
  Note over PY: next expiry pass
  PY->>N: workspace.remove
  N->>N: remove the worktree and its branch
  N-->>PY: outcome removed
  PY->>DB: delete the row
```

## P1: Hub-node interface
`kind: framing`

**Goal**: the hub can send one command to a connected node and get its reply,
and a node can register its checkouts and report its runs to the hub.

### 1.1 Hub-node contract: command envelope, node bodies, golden corpus, and typed models [category: code]
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
  `GOBBY_MANAGED_EXECUTION_BOOTSTRAP` points at a credential grant. The same
  function creates the Cargo target and spawn-cache directories, so it runs
  only on the machine that launches the agent.
- `ghook` copies `GOBBY_SESSION_ID` into `terminal_context.gobby_session_id`
  (`crates/ghook/src/terminal_context.rs`), which is how a spawned agent's
  session-start hook finds its pre-registered session.
- gdaemon serves `/api/nodes/channel` and `/api/machines` natively by exact
  path; other paths go to route families, which proxy to Python by default
  (`.gobby/plans/gdaemon-node-channel.md`, Decision 8). So
  `POST /api/nodes/checkouts` and `POST /api/nodes/report` reach Python with
  the verified identity, as `POST /api/hooks/execute` does.
- The node channel pings every 30 s (`.gobby/plans/gdaemon-node-channel.md`,
  Decision 9), which sets the report interval.
- `worktrees` needs `id`, `project_id`, `machine_id`, `worktree_path`,
  `branch_name`, `base_branch`, `task_id`, and `agent_session_id`; `clones`
  needs the same with `clone_path`
  (`crates/gcore/assets/schema/baseline.sql`). The report's workspace entry
  carries the node-side fields; the hub takes the rest from the run.

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
    `not_stopped`, `spawn_in_doubt`, `remote_placement_unsupported`, and
    `machine_cap_reached` (2.5's per-machine `max_active_agents` ceiling)
    stay in their own sections.
  - **Stage.** Every error carries `stage`. A node sets `"none"` when the
    command changed nothing and `"partial"` when it may have. gdaemon sets
    `"none"` on `node_offline`, because it writes nothing without a live
    channel, and `null` on `node_timeout` and `channel_closed`, because the
    frame may have reached the node. Only `"none"` proves a command had no
    effect.
  - **Hub route codes.** 1.4's routes answer `node_identity_required` (403),
    `machine_is_local` (403), `unsupported_version` (422), `invalid_body`
    (422), `project_not_found` (404), `project_deleted` (409),
    `checkout_conflict` (409), `checkout_root_taken` (409),
    `checkout_rejected` (422), and `stale_report` (409, with the body
    `{code, hub_epoch}`).
  - **Strictness.** Payloads, results, and node bodies reject unknown fields.
  - **v1 commands:**
    - `agent.spawn`: `{run_id, session_id, parent_session_id, project_id, terminal_id, agent_depth, max_agent_depth, workflow_name, provider, model, prompt, provider_args, reasoning_effort, sandbox: {enabled, network: "none"|"trusted", extra_write_paths}, timeout_seconds, title, source_root, workspace: {mode: "none"|"worktree"|"clone", branch_name, base_branch}}` returns `{accepted: true}`. With `mode: "none"` the agent runs in `source_root`, and `branch_name` and `base_branch` are null. With `worktree` or `clone`, `branch_name` is set, and a null `base_branch` means `source_root`'s current branch.
    - `agent.cancel`: `{run_id, terminal_id, signal: "TERM"|"KILL", close_terminal}` returns `{stopped}`.
    - `terminal.write`: `{terminal_id, kind: "text"|"key"|"input"|"paste", data, submit}` returns `{}`.
  - **Node bodies, node to hub.** Both go through the hub's front door with
    the node's API key, and neither carries a machine id.
    - `POST /api/nodes/checkouts`: `{v, project_id, root_path}` returns `{root_path, created}`.
    - `POST /api/nodes/report`: `{v, hub_epoch, revision, host_epoch, accepted: [run_id], agents: [{run_id, terminal_id, pid, locator, locator_key, workspace}], exits: [{run_id, terminal_id, launched, exit_code, reason: "prepare_failed"|"prepare_timeout"|"launch_failed"|"cancelled"|"exited", workspace}]}` returns `{}`. Each `workspace` is `{mode: "worktree"|"clone", path, branch_name, base_branch}` or null. `hub_epoch` is the epoch the node last learned from a `stale_report` refusal, or null. `revision` is a positive integer. Each list holds at most 1,000 entries, a bound on what one request can make the hub hold.
  - **Node duties (#24148).**
    - `agent.spawn`:
      - The node validates the payload, records the run as accepted, and
        replies `{accepted: true}` before it prepares anything. It answers
        `stage: "none"` only when it recorded nothing.
      - While its accepted and live runs already total 1,000, the node answers
        `command_failed` with `stage: "none"` and records nothing. So
        `accepted` and `agents` never exceed their report bound. The hub's
        ceiling (2.5) normally refuses first; this rule covers agents whose
        session end reached the hub before their process exited.
      - It then prepares the workspace from `source_root`: a worktree or a
        clone whose path and branch embed `run_id`, so no other machine's row
        names the same path. It launches the provider there. It sets the
        seven identity variables from the typed fields, `GOBBY_DAEMON_URL` to
        its own front door, and `GOBBY_PROMPT_FILE` to a node-local file
        holding `prompt`. It builds the provider command, applies its SRT
        chokepoint, enforces `timeout_seconds`, and closes the pane when the
        agent process exits.
      - A failure before launch removes what it prepared and reports an exit
        with `launched: false`. Preparation has a node-side deadline, which
        #24148 sets; passing it is a failure with reason `prepare_timeout`.
      - Before its first filesystem effect, the node writes a preparation
        record for the run, naming the workspace path and branch it will
        create, to the same node-local durable state as its cancel records.
        Removal is idempotent: a path and branch already gone count as
        removed. A failed removal, or a node daemon restart during
        preparation or removal, leaves the record, and the node retries the
        removal at startup and on each report interval until the path is
        gone. Only then does it delete the record. Reporting the
        `launched: false` exit does not clear it, and a cancel record for the
        same run leaves it in place.
      - At launch the record becomes the launched workspace's record. The
        node keeps it until a report carrying the run's exit gets 200, so
        every report until then can name the workspace.
    - Commands that name the same `run_id` run one at a time in arrival order.
    - `agent.cancel`:
      - The node records the `run_id` as cancelled in node-local durable
        state before it replies, so the record survives a node daemon
        restart. A later `agent.spawn` for it answers `cancelled` with
        `stage: "none"`.
      - For an accepted run that has not launched, it stops preparation,
        removes what it prepared under the preparation record's retry rule,
        answers `{stopped: true}`, and reports an exit with
        `launched: false` and reason `cancelled`.
      - A cancel for a run with nothing accepted or live answers `not_found`,
        which also proves that nothing runs for that run now or later.
        `{stopped: false}` means the agent may still run.
    - Checkouts: the node registers each checkout it serves before the hub can
      route a spawn of that project to it.
    - Reports:
      - The node posts a report every 30 s and immediately after any change.
        `accepted` and `agents` are complete in every report.
      - The node sends one report at a time. It builds each one fresh from
        its current state with the next `revision`, which it keeps in its
        durable state, so a node daemon restart never repeats a revision. It
        never resends a report's bytes: a retry is a new report.
      - A `stale_report` refusal carries the hub's current `hub_epoch`. The
        node adopts it and sends a fresh report.
      - `accepted` lists runs accepted and not yet launched or exited. A run
        leaves `accepted` only by moving to `agents` or `exits`.
      - `agents` lists the live agents the node launched, read from its
        terminal host, so agents that outlive a node daemon restart stay
        listed. `host_epoch` names the terminal host's current incarnation. A
        workspace appears only after its agent launched.
      - `exits` lists each run's end, oldest first and at most 1,000 per
        report, and each repeats in every report until a report carrying it
        gets 200. A `launched: true` exit names the run's workspace, and a
        `launched: false` exit names none.
      - The contract lists, as #24148 test cases, out-of-order and retried
        reports, a `stale_report` refusal, more than 1,000 pending exits, a
        spawn at 1,000 accepted and live runs, a failed removal, and a node
        daemon restart at each preparation and removal boundary.
  - **Internal route (gdaemon, built in 1.2).**
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
    `v` with `unsupported_version`, and the hub answers a node body with an
    unknown `v` the same way.
- `node_commands.rs` defines serde types for both frames, every payload and
  result, and both node bodies, with `deny_unknown_fields`, so #24148 builds
  its bodies from them. It is `pub mod node_commands` in `lib.rs`.
- `src/gobby/hub/node_commands.py` defines the matching pydantic models with
  `extra="forbid"`. The report's lists carry `max_length=1000`. Both define
  the `stale_report` refusal body `{code, hub_epoch}`, which the node parses.
- `corpus.json` holds one named message per shape: each command, each
  command's ok reply, one error reply per code with its stage, a
  `terminal.write` error with `stage: "partial"`, a checkout registration
  body, a report with all three lists filled and a launched exit naming its
  workspace, an empty report, and a `stale_report` refusal. Both tests
  parse every message, re-encode it, and compare it to the corpus value.
- `ROADMAP.md`: the S4.2 row gains the ← #24090 edge.

Verification planned:
- `cargo nextest run -p gobby-daemon -E 'binary(node_command_corpus)'`.
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/hub/test_node_command_corpus.py -v`.
- `uv run ruff check src/gobby/hub` and `uv run mypy src/gobby/hub`.

**Acceptance:**

- 1.1.1 - The contract specifies both frames, the three v1 commands with payload and result fields, both node bodies, the error codes with their producers and stage rule, including the hub-client codes 1.3 raises and the route codes 1.4 answers, the node duties with the report ordering rule, the durable preparation record, and the 1,000-run spawn refusal, the internal route, and the versioning rule. file: `docs/contracts/node-commands.md`.
- 1.1.2 - The Rust types round-trip every corpus message byte-equivalently and reject an unknown field. test: `crates/gdaemon/tests/node_command_corpus.rs::corpus_round_trips`.
- 1.1.3 - The Python models round-trip every corpus message and reject an unknown field. test: `tests/hub/test_node_command_corpus.py::test_corpus_round_trips`.
- 1.1.4 - In the Python models, a checkout or report body naming `machine_id`, an `agent.spawn` payload carrying `env` or `cwd`, and a report list of 1,001 entries all fail validation. test: `tests/hub/test_node_command_corpus.py::test_node_bodies_name_no_machine`.
- 1.1.5 - `ROADMAP.md` shows S4.2 depending on #24090. file: `ROADMAP.md`.

### 1.2 gdaemon internal node-command route over the node channel [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/nodes/registry.rs::Registry`
- `crates/gdaemon/src/nodes/registry.rs::Registry::register`
- `crates/gdaemon/src/nodes/registry.rs::ChannelHandle`
- `crates/gdaemon/src/nodes/channel.rs::hub_channel`
- `crates/gdaemon/src/nodes/internal_api.rs`
- `crates/gdaemon/src/nodes/mod.rs::*` — scope-reason: declares the new `internal_api` submodule
- `crates/gdaemon/src/front_door/mod.rs::FrontDoor::handle`
- `crates/gdaemon/tests/node_internal_route.rs`

**Research context:**
- #24090 (Node channel) and #24091 landed `crates/gdaemon/src/nodes/`:
  - `registry.rs`: `Registry { slots }` maps each machine to a `Slot` whose
    `current` is a private `ChannelHandle { connection_id, close, live }`.
    `register(machine)` returns the connection id and the close receiver;
    `remove`, `mark_live`, `is_connected`, `connected_ids`,
    `connection_id` and `persist` complete it.
  - `channel.rs::hub_channel(socket, nodes, machine, key)` runs hello,
    register, heartbeat persist, key check and `send_ack`, then loops on
    `select!` over the close receiver, the idle timer, the key rechecks and
    `socket.recv()`, ignoring every frame except Close and Ping.
  - `mod.rs` holds `HubNodes { db, registry, timing }` and
    `NativeRoutes { Standalone, Hub(Arc<HubNodes>) }`.
  - Nothing sends a command yet.
- `FrontDoor::handle(request, peer, https)` (`crates/gdaemon/src/front_door/mod.rs`)
  runs `backend_state.auth.authenticate` before it routes `/api/nodes/channel`
  to `nodes::channel::handle` on a hub. `serve.rs::plaintext_allowed` shows
  the loopback test (`peer.to_canonical().is_loopback()`), and
  `front_door/auth.rs` holds `GOBBY_FRONT_DOOR_SECRET`, which `state.rs`
  hands to Python.
- 1.1's contract fixes the route: `GET /api/internal/nodes/connected` and
  `POST /api/internal/nodes/{machine_id}/commands` with
  `{command, payload, timeout_ms}`, the admission rule, the 404 rule, and
  the stage of each gdaemon error.
- node-halves 3.1 sets `CONTRACT_VERSION` to 2, and every handler refuses
  another `v` (Constraints, Cross-plan edges).
- The review of #24268 (session 16011, 2026-10-13) asked for three things
  here: live proof of constant-time secret admission and of the 404 before
  any frame; no authorization inferred from a successful parse, because
  `prompt`, `provider_args`, `source_root` and `extra_write_paths` are
  plain strings and the node applies its own SRT and credential
  chokepoints; and malformed-input cases for every field the route reads.

**Implementation:**
- `registry.rs`: `ChannelHandle` gains `commands: mpsc::Sender<OutboundCommand>`,
  where `OutboundCommand` is `{ frame: CommandFrame, reply: oneshot::Sender<ReplyFrame> }`
  from 1.1's `node_commands.rs`. `register` also returns the command
  receiver. `Registry::command_sender(machine_id)` returns the sender only
  for a live handle.
- `hub_channel` adds the command receiver to its `select!`. It writes each
  frame and keeps `HashMap<Uuid, oneshot::Sender<ReplyFrame>>` keyed by
  `request_id`. A text frame that parses as a reply resolves its entry, and
  a reply with no entry is dropped and logged. On close or replacement it
  answers every pending entry with `channel_closed`.
- `internal_api.rs` (new) serves both routes:
  - It admits a request only from a loopback peer whose
    `X-Gobby-Backend-Secret` equals the front-door secret, compared in
    constant time by an XOR fold over equal-length bytes (no crate in
    `crates/gdaemon/Cargo.toml` provides one). Every other request,
    and every request on a standalone or node front door, gets 404 before
    any frame.
  - The body parses with `deny_unknown_fields`. A missing `command`, a
    non-object `payload`, an unknown field, or `timeout_ms` outside 1 to
    600,000 answers 400 `invalid_request` before any frame. The payload
    must parse as the named command's 1.1 type; that parse only rejects,
    and it grants nothing.
  - It mints `request_id`, sends through `command_sender`, and answers 200
    with the reply frame. With no live channel it answers `node_offline`
    with `stage: "none"`; past `timeout_ms` it answers `node_timeout` with
    `stage: null` and removes the entry. A drop guard removes the entry when
    the HTTP caller goes away, so a later reply is dropped and logged.
  - `connected` answers `{"machine_ids": [...]}` from `connected_ids`.
- `FrontDoor::handle` routes `/api/internal/nodes/` to
  `nodes::internal_api::handle` on a hub after the runtime gates and before
  `authenticate`, and answers 404 for that prefix on any other door.

Verification planned:
- `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_internal_route) + binary(nodes) + binary(front_door)'`.

**Acceptance:**

- 1.2.1 - A fake node on a live channel receives the command frame, and its reply reaches the HTTP caller unchanged. test: `crates/gdaemon/tests/node_internal_route.rs::command_round_trips`.
- 1.2.2 - With no live channel the route answers `node_offline` with `stage: "none"`, and a node that never replies yields `node_timeout` with `stage: null`. test: `crates/gdaemon/tests/node_internal_route.rs::offline_and_timeout`.
- 1.2.3 - Closing or replacing the channel ends every pending command with `channel_closed`, and a reply after a timeout or a dropped caller is dropped and logged. test: `crates/gdaemon/tests/node_internal_route.rs::pending_commands_end_with_channel`.
- 1.2.4 - A non-loopback peer, a missing or wrong secret, a secret differing only in its last byte, and a standalone or node front door each get 404, and the fake node receives no frame. test: `crates/gdaemon/tests/node_internal_route.rs::admission_is_loopback_and_secret`.
- 1.2.5 - A missing `command`, a non-object `payload`, an unknown field, an out-of-range `timeout_ms`, and a payload that does not parse as its command's type each answer 400 `invalid_request` with no frame sent. test: `crates/gdaemon/tests/node_internal_route.rs::malformed_requests_send_nothing`.
- 1.2.6 - `connected` lists only machines with a live channel. test: `crates/gdaemon/tests/node_internal_route.rs::connected_lists_live_channels`.

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
  passes the machine through `_session_machine_id`, which calls
  `require_local_machine_id`, so it raises `MachineOwnershipMismatchError` for
  a node's machine. `LocalProjectCheckoutManager.get(machine_id, project_id)`
  selects that machine's `project_checkouts` row with no machine guard and no
  filesystem access. Decision 18 uses it for remote targets and leaves
  `require_root` and its callers unchanged.
- No path writes a node's checkout row today: `gobby init` and hook ingress
  (`src/gobby/hooks/project_checkout_ingress.py`) bind this machine. 1.4's
  route adds that path; these tests seed the rows directly.
- 1.2 builds the internal route. These tests drive a fake HTTP route, and
  5.1 proves live node delivery.

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
    gdaemon sends only before any frame (1.1, 1.2), including on a
    standalone or node front door.
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
  - A remote target must have a `project_checkouts` row, read with
    `LocalProjectCheckoutManager(db).get(machine_id, project_id)`
    (`checkout_unresolved` when absent). Its `root_path` becomes
    `checkout_root`, and the hub never touches that path. The target must also
    be in `connected_machine_ids()` (`node_offline`) and share `owner_user_id`
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
- 1.3.6 - A remote target's `checkout_root` is its own `project_checkouts` root, read for a path that does not exist on the hub, while `require_root` for that machine still raises `MachineOwnershipMismatchError`. test: `tests/hub/test_machine_routing.py::test_remote_checkout_read_keeps_local_guard`.

### 1.4 Node routes: checkout registration and run reports [category: code] (depends: 1.1, 2.1)
`kind: deliverable`

Targets:
- `src/gobby/servers/routes/nodes.py`
- `src/gobby/hub/node_reports.py`
- `src/gobby/servers/_app_routes.py::register_routes`
- `tests/servers/test_node_routes.py`

**Granularity:** seven acceptance items across three production files, one
outcome: a node's two key-bound routes. Both share the identity and body
checks that 1.4.4 pins and one router. The report items (1.4.3, 1.4.5,
1.4.6, 1.4.7) are one admission and recording sequence: the epoch and
revision barrier decides whether the workspace rows are written, and the
200 follows the write. Splitting registration from reports would leave the
shared refusals pinned twice.

**Research context:**
- `server.auth_service.verified_front_door_identity(request)` returns
  `FrontDoorIdentity(user_id, machine_id, key_id)` only when gdaemon's
  front-door secret and all three identity headers are present
  (`src/gobby/servers/auth_service.py`). `src/gobby/servers/routes/api_keys.py`
  and `src/gobby/servers/routes/runtime_handshake.py` already read it this
  way, and 2.2 does for hooks.
- `LocalProjectCheckoutManager(db).register(machine_id, project_id, root_path)`
  takes an explicit machine with no local guard. It rejects sentinel projects
  and registered overlays, inserts with `ON CONFLICT (machine_id, project_id)
  DO NOTHING`, returns `(checkout, created)`, and raises
  `CheckoutConflictError` for a different root and `CheckoutRootTakenError`
  for a root another project holds.
- Hook ingress registration (`register_cwd_marker_checkout`) also runs
  `validate_checkout_root`, `link_checkout_cargo_target`, and a marker
  refresh. All three act on the hub's disk, so the node route uses none of
  them (Decision 18).
- `LocalProjectManager(db).get(project_id)` returns the project, including a
  soft-deleted one with `deleted_at` set.
- `register_routes` imports `create_api_keys_router` from its own module
  rather than from `gobby.servers.routes`; the nodes router follows that
  pattern.
- 1.3's `node_command_client()` caches one module-level instance; the inbox
  follows that pattern.
- Workspace rows (Decision 15): `worktrees` has `id`, `project_id`,
  `machine_id`, `task_id`, `branch_name`, `worktree_path`, `base_branch`,
  `agent_session_id`, and `status` (default `active`); `clones` has the same
  with `clone_path` (`crates/gcore/assets/schema/baseline.sql`). Unique
  indexes `idx_worktrees_path (machine_id, worktree_path)` and
  `idx_clones_path (machine_id, clone_path)` make a repeated insert a no-op
  under `ON CONFLICT DO NOTHING`.
  - `agent_session_id` references `sessions(id, machine_id)` with
    `ON DELETE SET NULL`, so a retained row cannot be relied on to reach its
    run.
  - A task's `worktree_path` artifact is read by hub-side build paths that run
    git in that path (`src/gobby/build/claim_recovery.py`,
    `src/gobby/build/observability.py`), so the route sets none.
  - `LocalWorktreeManager` and `LocalCloneManager` bind
    `require_machine_id()`, so the hub's workspace loops skip a node's rows
    once 2.1.5 pins it. That is why this section depends on 2.1.

**Implementation:**
- `src/gobby/servers/routes/nodes.py::create_nodes_router(server)` serves both
  routes. Each one first reads the verified identity:
  - No identity answers 403 `node_identity_required`.
  - An identity whose `machine_id` is this machine's answers 403
    `machine_is_local`, so the hub's own rows keep their on-disk validation.
  - The body parses with 1.1's models. An unknown `v` answers 422
    `unsupported_version`, and any other failure, including a `machine_id`
    field, answers 422 `invalid_body`.
  - Storage calls run through `server.run_db`.
- `POST /api/nodes/checkouts`:
  - An unknown project answers 404 `project_not_found`, and a soft-deleted
    one 409 `project_deleted`. A `root_path` that is not absolute answers 422
    `invalid_body`.
  - It calls `register(identity.machine_id, project_id, root_path)` and
    answers `{root_path, created}`. `CheckoutConflictError` answers 409
    `checkout_conflict`, `CheckoutRootTakenError` 409 `checkout_root_taken`,
    and a sentinel or overlay refusal 422 `checkout_rejected`.
  - It never reads or writes the path.
- `POST /api/nodes/report` runs these steps in order:
  1. `node_report_inbox().admit(identity.machine_id, hub_epoch, revision)`
     answers 409 `stale_report` with the current `hub_epoch` when the body's
     epoch is not the inbox's, or when its revision is not above the
     machine's high-water. Otherwise it raises the high-water to the
     revision. The refusal comes before any write.
  2. `record_remote_workspaces` records the report's workspaces in one
     transaction and commits.
  3. `put(identity.machine_id, report)` stores the report, and the route
     answers `{}`. So a 200 means every reported workspace row is durable.
- `src/gobby/hub/node_reports.py`:
  - `NodeReportInbox` mints `hub_epoch` once, at construction, as a fresh
    random UUID, so each hub process has its own and never reuses one. It
    holds, per machine, the admission high-water, the published revision
    (the revision of the last snapshot `put` installed), the snapshot
    installed since the last `take`, and the exits gathered since the last
    `take`.
    - `put` installs the report's snapshot only when its revision is above
      the published revision, and then raises the published revision to it.
      So an older admitted report that finishes later, before or after a
      `take`, never installs its snapshot. Every `put` merges exits by
      `(run_id, terminal_id)`. An exit stays true once it happens, so a late
      merge is safe; a late snapshot would present absence that newer
      reports already contradict.
    - `take()` returns, for each machine with a snapshot installed since the
      last `take`, that snapshot and the gathered exits, and empties both. A
      machine with gathered exits and no new snapshot keeps them until its
      next installed snapshot, so every entry 2.6 reads is a complete
      report. High-waters and published revisions survive `take`.
    - One lock guards `admit`, `put`, and `take`.
  - A module-level `node_report_inbox()` caches one instance.
  - `record_remote_workspaces(db, machine_id, report)` reads each
    `agents[].workspace` and `exits[].workspace` entry.
    - It skips and logs an entry unless its `terminal_id` row is on
      `machine_id` and names that `run_id`.
    - It records `exits[]` entries before `agents[]` entries.
    - For each entry it keeps, it first runs `UPDATE worktrees` (mode
      `worktree`) or `UPDATE clones` (mode `clone`)
      `SET updated_at = clock_timestamp() WHERE machine_id = %s AND worktree_path = %s`
      (`clone_path` for clones). When that matches no row, it inserts an
      `active` `worktrees` or `clones` row with a new id, the terminal's
      project and machine, the run's task, the run's child session as
      `agent_session_id`, the entry's path, `branch_name`, and
      `base_branch`, and `updated_at = clock_timestamp()`, with
      `ON CONFLICT DO NOTHING`. It sets no task artifact.
    - So `updated_at` is the last time a report named the workspace: a
      repeated path moves it, and within one report an exited run's
      workspace is older than a live run's. node-halves 3.2's
      `resumed_workspace` orders a session's rows by it (node-halves
      adversary finding NH-12, requested by gobby#16767). The update runs
      before the insert because `worktrees` has two unique indexes,
      `idx_worktrees_path` and `idx_worktrees_branch`
      (`crates/gcore/assets/schema/baseline.sql`). An
      `ON CONFLICT (machine_id, worktree_path) DO UPDATE` would abort the
      report's transaction on a branch conflict, which `DO NOTHING` skips.
  - The inbox lives in memory. A hub restart loses it, and the new process's
    epoch refuses every report built before the restart. Workspace rows are
    already durable, the next fresh report refills the snapshot, and a lost
    exit falls to 2.6's absence rules.
- `register_routes` mounts `create_nodes_router(server)`. Its other mounts
  stay as they are, including `create_hooks_router` (2.2),
  `create_agent_spawn_router` (2.5), and `create_attention_router` (3.1,
  3.3), whose signatures those sections leave unchanged.

Consumers unchanged:
- `src/gobby/servers/app_factory.py` — no-edit-reason: it calls `register_routes(app, server)`, whose signature is unchanged.
- `tests/test_wiki_retirement_contract.py` — no-edit-reason: it calls `register_routes` and asserts retired wiki paths, which the added router does not serve.

Verification planned:
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/test_node_routes.py tests/test_wiki_retirement_contract.py -v`.

**Acceptance:**

- 1.4.1 - A key-authenticated checkout registration stores a `project_checkouts` row for the key's machine and the body's root, a path that does not exist on the hub, and a repeat answers `created: false`. test: `tests/servers/test_node_routes.py::test_checkout_registration_uses_key_machine`.
- 1.4.2 - An unknown project answers 404, a soft-deleted project 409 `project_deleted`, a different root for the same project 409 `checkout_conflict`, and a relative root 422, and none writes a row. test: `tests/servers/test_node_routes.py::test_checkout_registration_refusals`.
- 1.4.3 - A key-authenticated report with the inbox's epoch lands in the inbox under the key's machine. A higher-revision report replaces the first's snapshot, the exits of both stay until `take` returns them, a report of 1,000 exits and a later report of 1 both reach `take`, and the inbox is empty after `take`. test: `tests/servers/test_node_routes.py::test_report_inbox_replaces_snapshot_and_merges_exits`.
- 1.4.4 - Each route answers 403 `node_identity_required` without a verified identity, 403 `machine_is_local` for a key bound to the hub's machine, 422 for a body naming `machine_id`, and 422 `unsupported_version` for an unknown `v`, and none writes a row or an inbox entry. test: `tests/servers/test_node_routes.py::test_node_routes_refuse_unverified_and_local_identity`.
- 1.4.5 - A report with a null epoch, or with the epoch of a replaced inbox standing in for a hub restart, answers 409 `stale_report` with the current epoch. So does a report whose revision is at or below the machine's high-water, whether it is a duplicate or arrives out of order. None of them writes a workspace row or touches the snapshot. When two admitted reports finish out of order, the higher revision's snapshot stays. In the intervening-take schedule, revisions r1 and r2 both pass `admit`, r2 commits and `put`s, `take` returns r2's snapshot, and r1 then commits and `put`s: r1's snapshot is never installed, its exits merge, the next `take` returns nothing for that machine, and after a report above r2 is `put` the next `take` returns that report's snapshot with r1's exits. test: `tests/servers/test_node_routes.py::test_report_order_and_epoch_barrier`.
- 1.4.6 - Before the route answers 200, a live agent entry with a workspace and a `launched: true` exit with a workspace each insert one `worktrees` row, or a `clones` row in clone mode. Each row carries the key's machine, the path, the branch, the base branch, the run's task, and the child session. A repeated entry inserts nothing more. An entry whose terminal is on another machine or names another run inserts none. The row survives a replaced inbox, the task gains no artifact, and the hub's `list_worktrees()` does not return the row. test: `tests/servers/test_node_routes.py::test_reported_workspace_is_durable_before_200`.
- 1.4.7 - A report naming a recorded path again moves that row's `updated_at` and inserts no row, and in one report carrying an exited run's workspace and a live run's, the live run's row has the later `updated_at`. test: `tests/servers/test_node_routes.py::test_reported_workspace_moves_updated_at`.

## P2: Machine-correct hub loops and remote callers
`kind: framing`

**Goal**: remote rows are safe from local loops, spawn, cancel, and terminal
write go to the owning node, and the hub settles remote runs from the node's
reports.

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
  touches no file or process. 2.1.5 pins the scoping, because 2.6 writes a
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
- `src/gobby/hooks/session_binding.py`
- `src/gobby/hooks/event_handlers/_session_start/flow.py::handle_session_start`
- `src/gobby/hooks/event_handlers/_session_start/handoff.py::resolve_session_start_identity`
- `src/gobby/hooks/session_lookup.py::SessionLookupService._resolve_metadata_platform_session`
- `src/gobby/hooks/session_lookup.py::SessionLookupService.resolve`
- `src/gobby/hooks/session_lookup.py::SessionLookupService._recover_compact_session`
- `src/gobby/hooks/project_context.py::resolve_hook_project_context`
- `src/gobby/hooks/project_context.py::_project_id_from_session`
- `src/gobby/hooks/project_context.py::_cached_session`
- `src/gobby/hooks/project_context.py::_project_id_from_existing_session`
- `tests/servers/test_hook_machine_binding.py`
- `tests/hooks/test_session_binding.py`
- `tests/hooks/test_session_start_handlers.py::TestSessionStartNewSession.test_resume_of_a_known_session_is_never_promoted_to_clear`
- `tests/hooks/test_session_events_coverage.py::*` — scope-reason: tests that model a local hook from a fixed machine id pin the hub's machine to it
- `tests/hooks/test_handler_execution.py::*` — scope-reason: tests that model a local hook from a fixed machine id pin the hub's machine to it
- `tests/hooks/test_session_coordinator.py::*` — scope-reason: tests that model a local hook from a fixed machine id pin the hub's machine to it
- `tests/hooks/test_transcript_path_derivation.py::*` — scope-reason: tests that model a local hook from a fixed machine id pin the hub's machine to it

**Granularity:** nine acceptance items and six production files, one outcome:
a hook from another machine can neither claim another machine's identity nor
bind another machine's session row. The ingress check without the lookup guard
leaves HM-F17 open, because a node with its correct envelope machine could
still name a hub row's id. The lookup guard without the ingress check would
trust an envelope machine no key binds. The six files are the one ingress and
every hook lookup that binds a row from an id the event supplies.

**Research context:**
- `execute_hook` is nested in `create_hooks_router` and serves
  `POST /api/hooks/execute`. Adapters take `machine_id` from the envelope's
  `input_data` (`src/gobby/adapters/claude_code.py`), and session registration
  stores it (`require_hook_machine_id`, `src/gobby/hooks/events.py`, returns
  `event.machine_id`).
- `server.auth_service.verified_front_door_identity(request)` returns
  `FrontDoorIdentity(user_id, machine_id, key_id)` only for a key-authenticated
  request that came through gdaemon (`src/gobby/servers/auth_service.py`).
- The envelope machine is the only remote marker that reaches every lookup.
  `src/gobby/adapters/agy.py` and `src/gobby/adapters/grok.py` do not call
  `_copy_platform_session_metadata`, so a metadata flag set at ingress would
  not reach their events. Hook envelopes are durable and can be processed
  outside the request, so a request-scoped marker would not reach a replay.
- `sessions.machine_id` is NOT NULL.
  `gobby.sessions.machine_scope.is_local_machine_owner(owner, local)` is true
  only for equal non-empty ids, and `gobby.utils.machine_id.get_machine_id()`
  is the hub's machine.
- These lookups bind a row from an id the event supplies, and none checks the
  row's machine (gcode, 2026-10-12):
  - `handle_session_start` (`_session_start/flow.py`): `get(external_id)`;
    then `get(terminal_context.gobby_session_id)`, whose hit has its
    `external_id` updated; then the web-chat `find_by_external_id`. Each hit
    goes to `_handle_pre_created_session`, which updates the row and backfills
    its terminal context. A compact resolution reaches
    `reconcile_compact_session_activity`, which reactivates the row.
  - `resolve_session_start_identity` (`_session_start/handoff.py`):
    `find_by_external_id`, `find_by_external_id_any_project`, and
    `resolve_compact_continuation`, which selects by source and terminal
    process identity on any machine (`src/gobby/sessions/compact_identity.py`).
    Its clear-continuation lookups already compare `session.machine_id`
    (`resolve_clear_continuation`).
  - `SessionLookupService._resolve_metadata_platform_session` accepts the
    `X-Gobby-Session-Id` header's row after an existence check, and `resolve`
    then runs `_apply_session_mutations` and `_enrich_task_context` on it.
  - `resolve` takes `_resolve_session_id`'s id, which comes from the cached
    row or `get_session_id`, neither machine-scoped, and runs the same
    mutations. `_recover_compact_session` reactivates a
    `resolve_compact_continuation` row through
    `reconcile_compact_session_activity` before it returns.
    `_bind_inherited_tty_native_subagent` already passes the event machine to
    `find_live_interactive_pane_owner`.
  - `resolve_hook_project_context` (`src/gobby/hooks/project_context.py`) takes
    the project from `_project_id_from_session` (terminal-context and metadata
    ids), `_cached_session`, and `_project_id_from_existing_session`
    (`find_active_by_external_id`, then `recover_session`, which caches the
    mapping before it returns). The explicit-project and cwd paths read no row.
- A session-start miss reaches `register_session`, which keeps
  `require_local_machine_id` and refuses another machine (Decision 18).
- Tests:
  - The pre-created tests in `tests/hooks/test_session_start_handlers.py` put
    the row on "unknown-machine" and send a local event, so they bind as
    today.
  - The test session's machine is the developer's or one minted in the temp
    home (`safe_gobby_home_dir`, `tests/conftest.py`). A test that sends
    events from a fixed id such as `21000000-0000-4000-8000-000000000001`
    without pinning `gobby.utils.machine_id._cached_machine_id` therefore
    models a remote event. `tests/hooks/test_hooks_manager.py` pins it.
  - A literal sweep (2026-10-12) found four files that use such an id with no
    pin: `test_session_events_coverage.py`, `test_handler_execution.py`,
    `test_session_coordinator.py`, and `test_transcript_path_derivation.py`.
    `test_resume_of_a_known_session_is_never_promoted_to_clear` binds a mock
    row with no machine from `…0008`.

**Implementation:**
- After parsing the body, `execute_hook` reads the verified identity. When it is
  present, `input_data.machine_id` must be present and equal the key's machine,
  or the hook answers 403 `{"code":"hook_machine_mismatch"}` before any adapter
  runs. A request without an identity (local ghook on the hub's own machine) is
  unchanged.
- `src/gobby/hooks/session_binding.py` defines
  `session_binds_event(row_machine_id: str | None, event_machine_id: str | None) -> bool`.
  It is true when `event_machine_id` is empty or equals `get_machine_id()`, or
  when `is_local_machine_owner(row_machine_id, event_machine_id)`. A local hook
  binds any row, as today; a hook from another machine binds only that
  machine's rows.
- Each lookup above applies it to the row it matched, before any write. A row
  that does not bind counts as a miss:
  - `handle_session_start`: both `get` results, before the `external_id`
    update, and the web-chat result.
  - `resolve_session_start_identity`: both external-id results and the compact
    candidate.
  - `_resolve_metadata_platform_session`: drops the header's row and its
    metadata, as it does for an unknown id.
  - `resolve`: for an event from another machine, it reads the resolved id's
    row, or uses `cached_session` when that is the row. A foreign row yields no
    platform session id, no mutation, no enrichment, and no
    `_platform_session_id`. A local event reads nothing more.
  - `_recover_compact_session`: a foreign compact row is no match, before
    `reconcile_compact_session_activity`.
  - `resolve_hook_project_context`: each of its three session paths treats a
    foreign row as no project, so resolution continues to the cwd path.
    `_project_id_from_session` gains the event's machine as a parameter.
    `recover_session` may still cache a foreign mapping, and every later read
    of it goes through `_cached_session` or `resolve`, which apply the
    predicate, so the cache never binds it.
- A remote miss at session start reaches `register_session`, which refuses
  another machine. So a node session that misses its pre-created row is
  refused, as the Constraints say, and #24151 owns registering it.
- Tests that model a local hook from a fixed machine id pin the hub's machine
  to that id, as `tests/hooks/test_hooks_manager.py` does. Tests that model a
  remote event give their rows the event's machine. The verification run over
  `tests/hooks/` finds any further test of either kind.

Consumers unchanged:
- `src/gobby/servers/routes/mcp/__init__.py` — no-edit-reason: it re-exports `create_hooks_router` by name.
- `tests/servers/routes/mcp_endpoints/test_execution_session_end_cleanup.py` — no-edit-reason: its hook requests carry no front-door identity, so they take the unchanged path; re-run as verification.
- `src/gobby/sessions/machine_scope.py` — no-edit-reason: `is_local_machine_owner` is reused as is.
- `src/gobby/sessions/compact_identity.py` — no-edit-reason: `resolve_compact_continuation` stays machine-agnostic, and both of its hook callers apply the predicate to its result.
- `src/gobby/storage/sessions/_registration_recovery.py` — no-edit-reason: `recover_session` is unchanged, and its result is checked by its hook caller.
- `tests/hooks/test_hooks_manager.py` — no-edit-reason: it pins the hub's machine to its event machine, so its events stay local.
- `src/gobby/hooks/hook_manager.py` — no-edit-reason: it calls `SessionLookupService.resolve` and `resolve_hook_project_context`, whose signatures are unchanged.
- `tests/hooks/test_session_lookup_metadata.py` — no-edit-reason: it pins the hub's machine to `_REAL_MACHINE_ID`; the verification run covers it, and any test there that models a local hook from another fixed id takes the pin rule above.
- `tests/hooks/test_spawned_grok_child_binding.py` — no-edit-reason: it calls `resolve` with an unchanged signature; the verification run covers it under the same pin rule.
- `tests/hooks/test_hook_extracted_helpers.py` — no-edit-reason: it calls `resolve_hook_project_context` with an unchanged signature; the verification run covers it under the same pin rule.
- `tests/hooks/test_session_activation_reconciliation.py` — no-edit-reason: it pins the hub's machine to `LOCAL_MACHINE_ID`, so its events stay local.
- `tests/hooks/test_hook_manager.py` — no-edit-reason: it pins the hub's machine, so its events stay local; re-run as verification.
- `tests/events/test_coordination_holds.py` — no-edit-reason: it calls `resolve` with an unchanged signature; re-run as verification.
- `tests/servers/routes/mcp_endpoints/test_terminal_session_revival.py` — no-edit-reason: it pins the hub's machine, so its events stay local; re-run as verification.

Verification planned:
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/test_hook_machine_binding.py tests/hooks/ tests/servers/routes/mcp_endpoints/ tests/events/test_coordination_holds.py -v`.

**Acceptance:**

- 2.2.1 - A key-authenticated hook whose envelope names another machine, or names none, is refused with `hook_machine_mismatch` before any adapter runs, and one naming the key's machine is processed. test: `tests/servers/test_hook_machine_binding.py::test_envelope_machine_must_match_key`.
- 2.2.2 - A hook without a front-door identity keeps today's behavior. test: `tests/servers/test_hook_machine_binding.py::test_local_hook_unchanged`.
- 2.2.3 - `session_binds_event` binds any row for an empty or hub-machine event, and for any other event machine only a row on that machine. test: `tests/hooks/test_session_binding.py::test_binding_predicate`.
- 2.2.4 - A remote session start that names another machine's row by `external_id`, by `terminal_context.gobby_session_id`, by a web-chat `external_id`, or by compact terminal identity changes nothing on that row and reaches `register_session`, which refuses it. test: `tests/hooks/test_session_binding.py::test_session_start_skips_foreign_rows`.
- 2.2.5 - On start and non-start events, an `X-Gobby-Session-Id` header that names another machine's row is dropped with no mutation and no task enrichment. test: `tests/hooks/test_session_binding.py::test_session_header_skips_foreign_row`.
- 2.2.6 - A remote non-start event whose `external_id` or compact terminal identity matches another machine's row gets no platform session id, and that row is not revived, backfilled, or reactivated. test: `tests/hooks/test_session_binding.py::test_non_start_lookup_skips_foreign_row`.
- 2.2.7 - With no explicit project, a remote event whose session ids name another machine's rows takes its project from its cwd and never from those rows. test: `tests/hooks/test_session_binding.py::test_project_context_skips_foreign_rows`.
- 2.2.8 - A remote session start that names its own machine's pre-created row by `terminal_context.gobby_session_id` takes `handle_pre_created_session` and updates that row. test: `tests/hooks/test_session_binding.py::test_same_machine_precreated_row_binds`.
- 2.2.9 - A pending clear attempt still cannot claim a resume whose persisted row is on the event's machine. test: `tests/hooks/test_session_start_handlers.py::TestSessionStartNewSession.test_resume_of_a_known_session_is_never_promoted_to_clear`.

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
- The tests drive a fake node client; 5.1 proves live delivery through 1.2 and the node.

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
- The tests drive a fake node client; 5.1 proves live delivery through 1.2 and the node.

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
- `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py::create_spawn_agent_registry`
- `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py::_parent_session_project_context`
- `src/gobby/servers/routes/agent_spawn.py::AgentSpawnRequest`
- `src/gobby/servers/routes/agent_spawn.py::create_agent_spawn_router`
- `src/gobby/agents/spawn.py::prepare_terminal_spawn`
- `src/gobby/agents/spawn.py::prepare_terminal_resume`
- `src/gobby/agents/spawn.py::_prepare_run_for_session`
- `src/gobby/agents/session.py::ChildSessionConfig`
- `src/gobby/agents/session.py::ChildSessionManager.create_child_session`
- `src/gobby/storage/sessions/_crud.py::_SessionCRUDMixin.register`
- `src/gobby/storage/agents/_lifecycle.py::_AgentRunLifecycleMixin.create`
- `src/gobby/config/app.py::DaemonConfig`
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: the derived carrier is regenerated from the registry and its bytes stay the same, because the generator emits no bounds
- `tests/hub/test_remote_spawn.py`
- `tests/config/test_runtime_config_contract.py::test_spawn_cap_default_and_minimum`

**Granularity:** fifteen acceptance items and eight production files, one
outcome: every remote spawn ends in one owned state, accepted, failed, or in
doubt. The items fall into three groups. Routing covers 2.5.3, 2.5.6, 2.5.8,
and 2.5.9. The launch covers 2.5.1, 2.5.2, 2.5.7, 2.5.10, 2.5.11, 2.5.12,
2.5.13, 2.5.14, and 2.5.15. Settlement of its outcome covers 2.5.4 and 2.5.5. None
closes alone:
- Routing without the launch only adds refusals for remote targets.
- A launch without settlement leaves runs and leases with no owner after a
  failure, the HM-F2 defect.
- The routed-machine keyword has no consumer outside this flow, and 2.5.10
  pins that every other caller keeps the local guard.
- The machine ceiling and the `le=1000` bound exist only to keep a node's
  report complete under its 1,000-entry list bound (Decision 19).

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
- `src/gobby/agents/spawn.py::prepare_terminal_spawn(session_manager, parent_session_id, project_id, machine_id, …, prompt, …, agent_run_id, …, workspace_path, credential_manager, config_snapshot)`
  pre-registers the child session and run, minting the run id when
  `agent_run_id` is `None`. `credential_manager=None` issues no managed
  credential.
  - It builds `ChildSessionConfig(machine_id=...)`, and
    `ChildSessionManager.create_child_session` passes `config.machine_id` to
    `_SessionCRUDMixin.register`. `register` calls `require_local_machine_id`
    first, so a node's machine raises `MachineOwnershipMismatchError` today,
    and then `require_registered_machine`, which refreshes the machine's
    last-seen time and raises `MachineNotRegisteredError` for an unenrolled
    machine.
  - `_prepare_run_for_session` calls `agent_run_mgr.create(...)` without a
    machine, and `_AgentRunLifecycleMixin.create` sets
    `machine_id = get_machine_id()`. A fresh run id reaches only that create
    branch.
  - `_prepare_run_for_session` then always calls `get_terminal_env_vars`,
    which creates the Cargo target and spawn-cache directories
    (`ensure_agent_cargo_target_dir`, `build_spawn_cache_env`), and writes a
    prompt file for a prompt over `MAX_ENV_PROMPT_LENGTH`. Both act on the
    hub's disk.
- `TerminalManager.create_pending(terminal_id, project_id, backend, ownership, spawn_key, *, machine_id, session_id, agent_run_id, …)`
  inserts a pending row and defaults `machine_id` to this machine.
  `terminals_backend_check` allows `tmux` and `native`, and a live `native`
  row needs a `host_epoch`, which the node's report carries (1.1).
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
- The local flow resolves the base branch in `spawn_agent_impl`: the request's
  `base_branch`, else the agent body's, where `inherit` means none, else the
  target checkout's current branch through git, else `main`. The hub cannot
  run git in a node's checkout. Without an explicit name or a task,
  `src/gobby/agents/isolation_models.py::generate_branch_name` falls back to
  `agent/<timestamp>-<hex>`.
- The hub inserts a remote workspace's row from the node's report (Decision
  15, 1.4), so this flow writes no `worktrees` or `clones` row and sets no
  task artifact.
- A resume (`resume_session_id`) runs inside `spawn_agent_impl` today:
  - `src/gobby/mcp_proxy/tools/spawn_agent/_resume.py::resolve_resume_target`
    resolves the reference in the project and refuses a session in another
    project, a provider without native resume or other than the requested
    one, a live session or one with a live run, a deleted session, a
    session with no recorded provider thread, and a refused seat retype. It
    returns the `Session`, whose `machine_id` names where its provider
    thread files are.
  - The provider is the session's `source`, and the model and reasoning
    effort default to the session's.
  - The resume metadata gains `provider_native_session_id`,
    `resume_existing_session: True` and the `resume_previous_*` fields.
  - `prepare_terminal_resume(existing_session_id=, original_run_id=, relaunch=True, …)`
    rebinds the session to a successor run through `_prepare_run_for_session`
    and passes `credential_manager` to `_issue_prelaunch_credential`, which
    issues nothing for `None`.
  - When `cleanup_agent_runtime_state` (`src/gobby/agents/runtime_cleanup.py`)
    settles a run whose metadata has `resume_existing_session`, it keeps the
    session.
- `spawn_agent_impl` gates agy on the hub's own install (`ensure_agy_support`)
  before any side effect. A node gates agy on its own install
  (node-halves 2.6).
- `max_active_agents` (`DaemonConfig`, `src/gobby/config/app.py`) is
  `Field(default=20, ge=1, description="Maximum active agents per project.")`
  with no upper bound. The live config store sets it to 100 (desired and
  active, revision 151); Josh, verbatim: "Cap at 100, configurable. We already
  have a value for this in the config store." The spawn flows read it through
  `configured_max_active_agents()` (`_spawn_guards.py`).
  `agent_slot_cap_refusal` counts one project's active runs, and
  `reserve_agent_slot` holds a per-project `asyncio.Lock` from `_SLOT_LOCKS`
  across the count and the spawn, so concurrent spawns cannot overshoot.
- The config contracts carry the key's default and description but no bounds
  (`tests/contracts/http/config_schema.json`,
  `crates/gcore/assets/config/runtime_config_contract.json`).
  `tests/config/test_runtime_config_contract.py::test_spawn_cap_default_and_minimum`
  pins the default and the minimum.
- No indexed symbol narrower than `DaemonConfig` covers the field, so
  Consumers unchanged lists each of its 244 consumers that the validator
  reports. In `src/`, only `configured_max_active_agents` reads
  `DaemonConfig.max_active_agents` (`gcode grep -w max_active_agents`,
  2026-10-12); the build flow's `max_active_agents` is `BuildConfig`'s own
  field (`src/gobby/config/build.py`). No test sets it above 20. The
  registry reads each field's annotation, default, and description, which
  `le=1000` leaves unchanged, so neither config contract changes. The live
  value is 100.
- A node runs only gdaemon and reads no config store (#24075), so only the hub
  can enforce the configured ceiling per node. `TerminalManager.list_reconcilable_by_machine`
  returns a machine's pending, live, and orphaned terminal rows. A remote
  agent's session end runs `mark_exited` on its row
  (`TerminalManager.get_live_for_session` selects pending or live rows with no
  machine filter; 2.6 research), while the node lists the agent in `agents`
  until its process exits (1.1). So the hub's count can trail the node's by
  the agents still exiting, and only the node can bound its own lists
  exactly (1.1's 1,000-run refusal).
- The tests drive a fake node client; 5.1 proves live delivery through 1.2 and the node.

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
- A request with `resume_session_id` resolves the reference in the project
  (`SessionManager.resolve_session_reference`) and passes the session's
  `machine_id` as `requested`, overriding `machine` and the origin
  (Decision 13). A reference that does not resolve takes the local path,
  where `spawn_agent_impl` returns its error. A registry built with no
  session manager counts the reference as unresolved and takes the local
  path, as `_parent_session_project_context` already returns `None` without
  one.
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
- The routed-machine keyword (Decision 18, first use). Each of these gains
  `routed_machine_id: str | None = None`, and every existing caller omits it:
  - `_SessionCRUDMixin.register`: when the keyword is set, it must equal
    `machine_id`, or `register` raises `MachineOwnershipMismatchError`, and it
    replaces the `require_local_machine_id` call. `require_registered_machine`
    still runs, so an unenrolled machine raises `MachineNotRegisteredError`.
    The target is connected (Q2), so the last-seen refresh holds.
  - `ChildSessionConfig` carries it, and `create_child_session` passes it to
    `register`.
  - `prepare_terminal_spawn` puts it on the config and passes it to
    `_prepare_run_for_session`.
  - `_prepare_run_for_session`, when it is set, passes
    `machine_id=routed_machine_id` to `_AgentRunLifecycleMixin.create` and
    skips the prompt-file and `get_terminal_env_vars` step, returning
    `env_vars={}` and `prompt_file=None`. So the hub creates no directory and
    writes no file for a node's run.
  - `_AgentRunLifecycleMixin.create` gains `machine_id: str | None = None`,
    which defaults to `get_machine_id()`.

  Local spawns pass none of these, so their sessions keep the local guard and
  their runs and environment are unchanged.
- `DaemonConfig.max_active_agents` gains `le=1000` (Decision 19). Its default
  (20) and description stay. Regenerating
  `crates/gcore/assets/config/runtime_config_contract.json` with
  `scripts/generate_runtime_config_contract.py` leaves its bytes unchanged,
  because the generator emits no bounds; the HTTP `config_schema.json`
  contract carries none either. A new description would change the gcore
  contract asset, so the per-machine use is stated in Decision 19 and the
  node-commands contract instead.
- `spawn_remote_agent(..., routed: RoutedMachine)` takes the 1.3 result, the
  only source of a routed machine. It refuses a placement request with
  `remote_placement_unsupported`. A resume first calls
  `resolve_resume_target` and returns its error as the local flow does,
  takes the provider, model and reasoning defaults from the session, and
  builds the resume metadata as `spawn_agent_impl` does. Then:
  1. It runs the local flow's agent-body, provider, model, and depth checks,
     the `runner.can_spawn` gate, and `TaskSpawnLease` admission. The
     callers' per-project `reserve_agent_slot` still applies, so a remote
     spawn passes both the project cap and the machine ceiling. The hub's
     agy gate is not among these checks, because the node gates agy on its
     own install (node-halves 2.6). It then runs the resolvers whose
     results step 4 sends:
     - `resolve_spawn_generation_endpoint`
       (`src/gobby/mcp_proxy/tools/spawn_agent/_generation_endpoint.py`);
     - `resolve_spawn_reasoning` (`src/gobby/agents/reasoning.py`);
     - `code_index_preflight_mode`
       (`src/gobby/mcp_proxy/tools/spawn_agent/_code_index.py`);
     - the sandbox:
       `apply_write_grant(definition_sandbox_config(daemon_config, agent_body), write_grant)`
       (`src/gobby/agents/external_write_grants.py`,
       `src/gobby/agents/sandbox_network.py`). Unless that config is
       enabled with backend `srt`, it returns `sandbox_required` with
       `stage: "none"`, as `resolve_spawn_sandbox` does locally. It does
       not call `resolve_spawn_sandbox`, whose `require_managed_srt` runs
       `verify_srt_installation()` on the hub's machine; the node verifies
       its own install (node-halves 2.5), as it gates agy.
  2. Machine ceiling (Decision 19). It takes a per-machine `asyncio.Lock`,
     keyed by `routed.machine_id` in a module-level dict in `remote_spawn.py`,
     and holds it through step 3's `create_pending`. Lock order is project
     (`reserve_agent_slot`, held by the caller) then machine. Under the lock it
     counts `list_reconcilable_by_machine(routed.machine_id)`. At or above
     `configured_max_active_agents()` it releases the unattached lease and
     returns `machine_cap_reached` with `stage: "none"`, writing no session,
     run, or terminal row and sending nothing. `_SLOT_LOCKS` and
     `agent_slot_cap_refusal` are unchanged.
  3. It calls `prepare_terminal_spawn` with `machine_id` and
     `routed_machine_id` both set to `routed.machine_id`, `agent_run_id=None`,
     the request's `prompt`, `credential_manager=None`, and
     `workspace_path=routed.checkout_root`. It attaches the lease to the run.
     It calls `create_pending(terminal_id, project_id, "native", "gobby",
     terminal_id, machine_id=<target>, session_id=<child>,
     agent_run_id=<run>)`. Session, run, and terminal rows then all name the
     target before anything is sent. It then releases the machine lock.
     A resume calls `prepare_terminal_resume` in place of
     `prepare_terminal_spawn`, with `existing_session_id`,
     `original_run_id` and `relaunch=True` as the local flow passes them,
     and the same `routed_machine_id`, `credential_manager=None` and
     `workspace_path`. `prepare_terminal_resume` passes `routed_machine_id`
     to `_prepare_run_for_session`, so the successor run is on the target
     and the hub writes no file or directory for it.
  4. It sends one `agent.spawn` (1.1) whose payload node-halves 3.2's
     `build_spawn_payload` builds from the run, the request, the
     resolvers' results and the resume metadata, with
     `source_root=routed.checkout_root`. The builder maps `workspace`:
     - `mode` is the request's isolation, else the agent body's, else
       `none`.
     - For `worktree` or `clone`, `branch_name` is the request's, else
       `agent/<run_id>`. `base_branch` is the request's, else the agent
       body's. `inherit` or no value sends `null`, and the node uses
       `source_root`'s current branch (1.1).
     - A resume whose request names no isolation names the workspace its
       session's earlier run used on the node as `existing` (node-halves
       Decision 23).
  5. `{accepted: true}` returns the run id, child session id, and terminal id
     with status `pending`. The hub records nothing more here. 2.6 promotes
     the run from the node's report.
  6. An error with `stage: "none"` is a definite failure. It fails the
     pending terminal (`fail_pending`) and settles the run with
     `_roll_back_run`, which also ends the lease, and returns the error's
     code. A hub restart between those calls leaves a pending terminal for
     2.6's fence or an active run for 2.6's run sweep. A resumed run's
     metadata has `resume_existing_session`, so its rollback keeps the
     session, as a local resume's rollback does.
  7. Any other outcome leaves the spawn in doubt. That covers a `null` or
     `"partial"` stage and a caller that goes away after the send. The run and
     terminal stay pending, the lease stays attached, and the caller gets
     `spawn_in_doubt` with the run id. 2.6 resolves the spawn.

Consumers unchanged:
- `src/gobby/mcp_proxy/tools/agents_spawn_tools.py` — no-edit-reason: it registers `create_spawn_agent_registry(...)`, whose signature is unchanged.
- `src/gobby/servers/routes/__init__.py` — no-edit-reason: it re-exports `create_agent_spawn_router` by name.
- `tests/config/test_live_policy_consumers.py` — no-edit-reason: it inspects the router's launch methods, which are unchanged.
- `tests/agents/test_backend_ingress.py` — no-edit-reason: it asserts `terminal_backend` is absent and rejected; an added optional `machine` field changes neither.
- `tests/mcp_proxy/tools/spawn_agent/test_execution.py` — no-edit-reason: its spawns omit `machine` and take the unchanged local path; re-run as verification.
- `tests/mcp_proxy/tools/spawn_agent/test_factory.py` — no-edit-reason: it asserts named schema properties, which an added optional property leaves intact, and its `_parent_session_project_context` tests pin the session machine to the local machine, so resolving with this machine's id returns the same checkout.
- `tests/agents/test_spawn.py` — no-edit-reason: it patches `_prepare_run_for_session` or calls it, `prepare_terminal_spawn` and `prepare_terminal_resume` by keyword without `routed_machine_id`, so its runs keep this machine and their environment.
- `src/gobby/agents/resume_executor.py` — no-edit-reason: its daemon-stop resume calls `prepare_terminal_resume` without `routed_machine_id`, so its runs keep this machine.
- `tests/agents/test_spawn_prepare_resume.py` — no-edit-reason: it calls `prepare_terminal_resume` without `routed_machine_id`, so its runs keep this machine; re-run as verification.
- `tests/agents/test_spawn_resume.py` — no-edit-reason: it patches `_prepare_run_for_session` and calls `prepare_terminal_resume` without `routed_machine_id`, which then passes `None`, so its runs keep this machine.
- `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py` — no-edit-reason: its local flow passes no `routed_machine_id` to `prepare_terminal_spawn` or `prepare_terminal_resume`, so its sessions keep the local guard and its runs keep this machine.
- `src/gobby/agents/__init__.py` — no-edit-reason: it re-exports `ChildSessionConfig` by name.
- `src/gobby/agents/spawn_executor_support.py` — no-edit-reason: it checks only that `create_child_session` is callable by name.
- `tests/agents/test_agents_session.py` — no-edit-reason: it builds `ChildSessionConfig` without `routed_machine_id` and calls `create_child_session` for the local machine; re-run as verification.
- `tests/e2e/test_grok_session_deferral.py` — no-edit-reason: it builds `ChildSessionConfig` without `routed_machine_id` for the local machine.
- `tests/agents/test_verified_review_regressions.py` — no-edit-reason: it stubs `create_child_session` with a lambda that takes any arguments.
- `tests/mcp_proxy/tools/spawn_agent/conftest.py` — no-edit-reason: its wrapper forwards every argument to `prepare_terminal_spawn` unchanged.
- `tests/agents/test_spawn_executor.py` — no-edit-reason: it patches `prepare_terminal_spawn`.
- `tests/agents/test_srt_spawn.py` — no-edit-reason: it patches `prepare_terminal_spawn`.
- `tests/agents/test_runbook_seat_titles.py` — no-edit-reason: it asserts session titles through the local path, which passes no `routed_machine_id`.
- `tests/integration/test_terminal_mode_worktrees.py` — no-edit-reason: it asserts sessions, env, and paths through the local path and calls `register` without `routed_machine_id`.
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
- `tests/mcp_proxy/tools/spawn_agent/test_spawn_worktree_reference_resolution.py` — no-edit-reason: its spawns omit `machine` and take the unchanged local path, and its resume patches `prepare_terminal_resume` on that path.
- `tests/mcp_proxy/tools/tasks/test_lifecycle_close_orchestration.py` — no-edit-reason: its spawns omit `machine` and take the unchanged local path.
- `tests/mcp_proxy/tools/test_parallel_dispatch.py` — no-edit-reason: its spawns omit `machine` and take the unchanged local path.
- `tests/mcp_proxy/tools/test_spawn_agent_resume.py` — no-edit-reason: its resume tests call `spawn_agent_impl` directly, below the callers' routing, and patch `prepare_terminal_resume`; `test_pipeline_input_passes_resume_or_fresh` builds the registry with no session manager, so its resume reference counts as unresolved and takes the unchanged local path to its patched `spawn_agent_impl`; re-run as verification.
- `tests/skills/test_reference_library.py` — no-edit-reason: it validates documented launch arguments against the schema, and an added optional property accepts them.
- `src/gobby/mcp_proxy/tools/spawn_agent/_spawn_guards.py` — no-edit-reason: the remote flow calls `configured_max_active_agents()` as is, and `agent_slot_cap_refusal`, `reserve_agent_slot`, and `_SLOT_LOCKS` keep the per-project cap.
- `tests/contracts/http/config_schema.json` — no-edit-reason: it carries the key's default and description but no bounds, and both stay.
- `tests/mcp_proxy/tools/spawn_agent/test_spawn_guards.py` — no-edit-reason: its caps are at most the default 20, all within `le=1000`.
- `src/gobby/config/registry.py` — no-edit-reason: it reads each `DaemonConfig` field's annotation, default, and description, and `le=1000` changes none of them.
- `src/gobby/agents/spawn_models.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/ai/_text_generation_adapters.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/ai/_text_generation_builder.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/ai/_tool_chat_builder.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/ai/_tool_chat_codex.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/ai/_tool_chat_spawn.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/ai/audio.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/ai/embedding_switch_runner.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/ai/embedding_switch_service.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/ai/endpoint_activation.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/ai/registry.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/ai/registry_builder.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/ai/vision.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/cli/memory/__init__.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/cli/runtime.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/cli/ui.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/cli/ui_mode.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/cli/utils.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/cli/utils_ui.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/config/__init__.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/config/_loading.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/config/documents.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/config/runtime.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/config/runtime_activation.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/config/runtime_contracts.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/config/runtime_models.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/config/values.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/config/voice_secrets.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/feedback/agent.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/hooks/broadcaster.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/hooks/factory.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/llm/claude.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/llm/factory.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/llm/service.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/mcp_proxy/importer.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/mcp_proxy/registries.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/mcp_proxy/server.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/mcp_proxy/services/server_mgmt.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/mcp_proxy/tools/agents_context.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/mcp_proxy/tools/agents_registry.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/mcp_proxy/tools/sessions/_factory.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/mcp_proxy/tools/tasks/_context.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/mcp_proxy/tools/tasks/_factory.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/mcp_proxy/tools/tasks/_ops_factory.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/providers/capabilities/local_context_config.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/runner.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/runner_init/orchestration.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/runner_init/servers.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/runner_init/services.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/runner_init/storage.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/runner_init/terminal_wiring.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/servers/http.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/servers/routes/configuration_generation_endpoints.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/servers/routes/dependencies.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/servers/routes/embeddings.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/servers/routes/llm.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/servers/websocket/chat/_messaging.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/servers/websocket/chat/runtime_manager.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/servers/websocket/server.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/servers/websocket/session_control.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/sessions/lifecycle.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/sessions/transcript_processing.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/storage/config_repository.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/storage/sessions/_manager.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/system_automation.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`; its own `max_active_agents` arguments carry the build cap, so `le=1000` changes nothing it uses.
- `src/gobby/tasks/expansion_service.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `src/gobby/workflows/engine/core.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/agents/test_agent_command_builder.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/agents/test_local_context_setup.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/agents/test_resume_executor.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses, and it patches `prepare_terminal_resume` in `resume_executor`, whose call is unchanged.
- `tests/agents/test_resume_sandbox_gate.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/agents/test_sandbox.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/agents/test_sandbox_network.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/agents/test_tmux.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/ai/test_agy_probe.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/ai/test_capability_registry.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/ai/test_embedding_switch_runner.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/ai/test_endpoint_activation.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/ai/test_endpoints.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/ai/test_text_generation.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/ai/test_tool_chat_builder.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/ai/test_tool_chat_protocols.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/ai/test_tool_chat_service.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/ai/test_tool_chat_spawn.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/ai/test_vision_extraction.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/build/test_dispatcher_stage_wake.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/cli/hub_backup/test_cli_hub_backup_cli.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/cli/installers/test_qdrant_installer.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/cli/installers/test_router_carriers.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/cli/test_backend_restart.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/cli/test_cli_mcp_proxy.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/cli/test_cli_runtime.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/cli/test_cli_runtime_config.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/cli/test_cli_skills_coverage.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/cli/test_daemon_preflight.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/cli/test_daemon_remote_mode.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/cli/test_embeddings_doctor.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/cli/test_memory_coverage.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/cli/test_operational_config_consumers.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/cli/test_projects_refresh_verification.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/cli/test_status_health_agent_role.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/cli/test_tasks_cli.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/cli/test_ui_coverage.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/cli/test_ui_mode.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/cli/test_validation_cli.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/config/test_app_config.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/config/test_config_communications.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/config/test_config_consolidation.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/config/test_config_export_protection.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/config/test_config_registry.py` — no-edit-reason: it walks `DaemonConfig` field paths by annotation, and `le=1000` leaves the field's path and `int` annotation unchanged.
- `tests/config/test_config_runtime.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/config/test_config_runtime_config_resolution.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/config/test_config_sessions.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/config/test_daemon_sandbox.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/config/test_feature_base.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/config/test_files_home.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/config/test_indexing_config.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/config/test_logging_settings.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/config/test_memory_feature_configs.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/config/test_remote_ui_auth.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/config/test_removed_config_fields.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`; its `max_active_agents` is `BuildConfig`'s, so `le=1000` changes nothing it uses.
- `tests/config/test_restart_config_consumers.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/config/test_runtime_loop_consumers.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/config/test_skills.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/config/test_stateful_config_subscribers.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/config/test_terminal_config.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/config/test_terminal_host.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/config/test_tool_result_offload_config.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/config_runtime_helpers.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/conftest.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/dispatch/test_bundled_agent_contract.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/dispatch/test_daemon_resume.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/dispatch/test_services_daemon_config.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/e2e/test_build_dispatcher_autonomy.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/feedback/test_feedback_agent.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/hooks/test_broadcaster.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/hooks/test_config_hook_extensions.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/hooks/test_hook_manager_factory_injection.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/hooks/test_hooks_context.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/hooks/test_hooks_manager.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/hooks/test_staged_effects_receipt_route.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/install/test_hook_timeout_staging.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/llm/test_claude.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/llm/test_claude_provider.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/llm/test_context_window.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/llm/test_llm_claude.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/llm/test_llm_local.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/llm/test_llm_service.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/mcp_proxy/test_gobby_daemon_tools.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/mcp_proxy/test_mcp_proxy_importer.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/mcp_proxy/test_mcp_proxy_runtime_config_resolution.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/mcp_proxy/test_proxy_server.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/mcp_proxy/test_workspaces_registry.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/mcp_proxy/tools/spawn_agent/test_local_endpoint.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/mcp_proxy/tools/spawn_agent/test_sandbox_gate.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/mcp_proxy/tools/tasks/test_close_task_attributed_cleanliness.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/mcp_proxy/tools/test_config_values.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/mcp_proxy/tools/test_review_learning.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/mcp_proxy/tools/test_spawn_agent_impl_provider.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/mcp_proxy/tools/test_voice.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/providers/capabilities/test_local_context_config.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/providers/test_version_gate.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/runner_helpers.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/runner_init/test_config_runtime_startup.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/runner_init/test_runner_init_storage.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/runtime_grants/support.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/scheduler/test_cron_config.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/scheduler/test_system_automation_loop.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/conftest.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/routes/mcp_endpoints/test_bridge.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/routes/mcp_endpoints/test_execution_session_end_cleanup.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/routes/test_agent_spawn_routes.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/routes/test_agents_routes.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/routes/test_api_key_routes.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/routes/test_auth_routes.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/routes/test_config_values_api.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/routes/test_config_yaml_replace.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/routes/test_configuration_effective_routes.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/routes/test_configuration_routes.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/routes/test_embeddings_routes.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/routes/test_hooks_agy_dispatch.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/routes/test_hub_files_proxy.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/routes/test_llm_routes.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/routes/test_rules_routes.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/routes/test_runtime_config.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/routes/test_runtime_handshake.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/routes/test_servers_routes_chat_attachments.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/routes/test_servers_routes_communications.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/routes/test_servers_routes_pipeline_definitions.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/routes/test_servers_routes_providers.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/routes/test_session_variables.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/routes/test_skills_routes.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/routes/test_tasks_create.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/routes/test_tasks_list_response_shape.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/routes/test_tasks_owner_ref.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/routes/test_tasks_routes.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/routes/test_traces.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/routes/test_variable_definitions.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/routes/test_voice_routes.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/test_app_factory_ui_modes.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/test_app_factory_vite_proxy.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/test_chat_session.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/test_http_broadcaster.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/test_http_endpoints.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/test_http_init.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/test_http_middleware.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/test_local_context_consumers.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/test_local_llm.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/test_local_provider_models.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/test_managed_clear_identity.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/test_mcp_routes.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/test_servers_sessions_routes.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/websocket/chat/test_agy_backend.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/websocket/chat/test_launch_contracts.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/websocket/chat/test_launch_contracts_codex.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/websocket/chat/test_runtime_manager.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/websocket/chat/test_servers_websocket_chat_provider_routing.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/websocket/chat/test_session_launch.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/websocket/test_server.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/websocket/test_servers_websocket_auth.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/servers/websocket/test_servers_websocket_chat_attachments.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/skills/reference_library_helpers.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/storage/test_schema_divergence.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/telemetry/test_flow_instrumentation.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/terminals/test_backend_selection.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/terminals/test_host_shutdown_preservation.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/test_runner_approval_timeout.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/test_runner_bin_freshness.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/test_runner_init.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/test_runner_lifecycle.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/test_runner_lifecycle_periodic.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/test_runner_lifecycle_ui_mode.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/test_runner_maintenance_startup.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/test_runner_resource_monitor.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/test_runner_skill_maintenance.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/test_runner_workflow_audit_maintenance.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/test_wiki_retirement_contract.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/voice/test_tts_chatterbox.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/voice/test_voice_config.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/workflows/test_rule_engine.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/workflows/test_rule_engine_aggregate_blocks.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/workflows/test_session_feedback_rules.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
- `tests/workflows/test_workflow_hooks.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.

Verification planned:
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/hub/test_remote_spawn.py tests/mcp_proxy/tools/spawn_agent/ tests/servers/routes/test_agent_spawn_routes.py tests/agents/test_backend_ingress.py tests/agents/test_spawn.py tests/agents/test_spawn_resume.py tests/agents/test_spawn_prepare_resume.py tests/agents/test_resume_executor.py tests/mcp_proxy/tools/test_spawn_agent_resume.py tests/agents/test_agents_session.py tests/sessions/test_sessions_manager.py tests/agents/test_runbook_seat_titles.py tests/integration/test_terminal_mode_worktrees.py tests/storage/agents/test_active_run_scope.py tests/storage/test_machine_scope_writers.py tests/config/test_runtime_config_contract.py -v`.

**Acceptance:**

- 2.5.1 - Through a fake node client, a spawn from a session on another machine sends exactly one command, `agent.spawn`, to that machine, and its session, run, and terminal rows carry that `machine_id` before it is sent; `{accepted: true}` returns the run id with status `pending`. test: `tests/hub/test_remote_spawn.py::test_spawn_routes_to_requesting_machine`.
- 2.5.2 - A remote spawn issues no managed credential, writes no prompt file even for a prompt over `MAX_ENV_PROMPT_LENGTH`, never calls `get_terminal_env_vars`, and creates no Cargo target or spawn-cache directory on the hub. test: `tests/hub/test_remote_spawn.py::test_remote_spawn_touches_no_hub_disk`.
- 2.5.3 - An explicit `machine` is validated, and an ineligible one fails with its typed code without spawning anywhere. test: `tests/hub/test_remote_spawn.py::test_explicit_machine_is_validated`.
- 2.5.4 - A `stage: "none"` failure of `agent.spawn`, including `node_offline` and `node_routing_unavailable`, fails the pending terminal, cancels the run, and ends the lease, with no local process or pane call and no further command. test: `tests/hub/test_remote_spawn.py::test_definite_failure_settles_in_database`.
- 2.5.5 - A `node_timeout` or a `node_reply_lost` on `agent.spawn` returns `spawn_in_doubt` and leaves the run and terminal pending and the lease attached, with no rollback. test: `tests/hub/test_remote_spawn.py::test_in_doubt_spawn_keeps_state`.
- 2.5.6 - A node caller whose parent is on the hub spawns on the node, and a hub caller whose parent is on a node spawns locally in the hub's checkout. test: `tests/hub/test_remote_spawn.py::test_origin_is_the_calling_session`.
- 2.5.7 - A placement request is refused with `remote_placement_unsupported`. A resume of a node session routes to that node whatever `machine` and the origin say, rebinds the session to a successor run on that node with no hub file or credential, and sends `resume_session_id` with the session's earlier workspace as `existing`; a resume that `resolve_resume_target` refuses sends nothing and returns its error; and a remote agy spawn does not consult the hub's agy gate. test: `tests/hub/test_remote_spawn.py::test_resume_routes_and_placement_refused`.
- 2.5.8 - A spawn whose target is this machine takes the local path unchanged. test: `tests/hub/test_remote_spawn.py::test_local_target_unchanged`.
- 2.5.9 - With a checkout on the target and none on the hub, both the MCP tool and `POST /api/agents/spawn` spawn on the target. test: `tests/hub/test_remote_spawn.py::test_remote_target_needs_no_hub_checkout`.
- 2.5.10 - `register` for another machine without `routed_machine_id`, and `register_session` for another machine, raise `MachineOwnershipMismatchError`; a `routed_machine_id` that differs from `machine_id` raises it too; a routed registration for an unenrolled machine raises `MachineNotRegisteredError`; and none writes a row. test: `tests/hub/test_remote_spawn.py::test_foreign_session_needs_routed_machine`.
- 2.5.11 - An isolated remote spawn sends `workspace` with the requested mode, `branch_name` `agent/<run_id>` unless the request names a branch, and `base_branch` `null` for `inherit` or no value; the hub writes no `worktrees` or `clones` row and sets no task artifact. test: `tests/hub/test_remote_spawn.py::test_workspace_intent_names_run_branch`.
- 2.5.12 - With the target machine's pending, live, and orphaned terminal rows at `configured_max_active_agents()`, a remote spawn returns `machine_cap_reached` with `stage: "none"`, writes no session, run, or terminal row, sends nothing, and frees its lease; two concurrent spawns to a machine one below the ceiling admit exactly one; a spawn to another machine and a local spawn are unaffected. test: `tests/hub/test_remote_spawn.py::test_machine_ceiling_refuses_before_rows`.
- 2.5.13 - `DaemonConfig(max_active_agents=1000)` validates, `DaemonConfig(max_active_agents=1001)` raises `ValidationError`, and the default stays 20. test: `tests/config/test_runtime_config_contract.py::test_spawn_cap_default_and_minimum`.
- 2.5.14 - The checked-in runtime config contract still equals the generator's output after the bound. test: `tests/config/test_runtime_config_contract.py::test_checked_in_contract_matches_registry`.
- 2.5.15 - With `verify_srt_installation` failing on the hub, a remote spawn whose agent body resolves to enabled `srt` still sends `agent.spawn` with that sandbox, the resolved model, effort, and preflight mode; a body whose sandbox is disabled or `provider-native` returns `sandbox_required` with `stage: "none"` and sends nothing. test: `tests/hub/test_remote_spawn.py::test_remote_resolvers_skip_hub_srt_install`.

### 2.6 The hub settles remote runs from the node's reports [category: code] (depends: 1.3, 1.4, 2.1, 2.5)
`kind: deliverable`

Targets:
- `src/gobby/hub/remote_reconcile.py`
- `src/gobby/agents/agent_health.py::AgentHealthMonitor.__init__`
- `src/gobby/agents/agent_health.py::AgentHealthMonitor.check_unhealthy_agents`
- `tests/hub/test_remote_reconcile.py`

**Granularity:** seven acceptance items and two production files, one
outcome: after a remote spawn leaves 2.5, every run and terminal row on a
reporting machine reaches a settled state with one owner,
`RemoteRunReconciler` under `AgentHealthMonitor`. Promotion covers 2.6.1,
terminal settlement 2.6.2 to 2.6.6, and the run sweep 2.6.7. 1.4 records
workspace rows. None is safe alone: all three read the same report, and the fence
exits the terminal whose run the sweep settles after a hub restart.

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
- `_dead_terminal_reason` reads only database state. It returns `None` while
  a provider that exits without a session end has its completion settling,
  and `"terminal session died unexpectedly"` for a run whose session never
  started a turn.
- Workspace rows (Decision 15) are 1.4's: its report route records them
  before answering 200, so the reconciler writes none. The local flow records
  its row after creating the directory
  (`WorktreeIsolationHandler.prepare_environment` calls
  `LocalWorktreeManager.create`).
  - `LocalWorktreeManager.has_path_on_other_machine` makes the hub's
    `handle_worktree_remove` hook and `register_adopted` skip a local path that
    another machine's row also names.
  - `release_session_worktrees` releases only this machine's rows, so a
    remote row keeps its `agent_session_id` after the session ends.
- `node_report_inbox().take()` (1.4) returns, for each machine with a
  snapshot installed since the last `take`, that snapshot and the exits
  gathered since the last `take`. A late older report never installs a
  snapshot, so every entry is the newest complete report.
  `AgentLifecycleMonitor` runs the health pass every 30 s
  (`check_interval_seconds`, `src/gobby/agents/lifecycle_monitor.py`, with no
  override), and a node reports right after each change. So a remote run
  turns live within one health pass, about 30 s, of its launch.
- The tests feed the inbox directly and drive a fake node client; live
  reports are proved in 5.1.

**Implementation:**
- `RemoteRunReconciler` takes the run manager, the terminal manager, a
  `cleanup_agent` callable, a `dead_reason` callable, `run_db`, and the
  database. `AgentHealthMonitor.__init__` builds it from its own
  `_cleanup_handler.cleanup_agent` and `_dead_terminal_reason`.
  `check_unhealthy_agents` adds the result of `reconcile()` to its count after
  the local pass.
- `reconcile()` calls `node_report_inbox().take()` and drops this machine's
  entry. Only machines with an entry are visited, so a node that stops
  reporting keeps its rows as they are. "Consecutive reports" means
  consecutive passes that each took a newly admitted report for that machine.
  1.4 admits only fresh, ordered reports, so a delayed or replayed report
  never counts.
- The terminal pass selects terminal rows, not runs, so a terminal outlives
  its run's settlement. For each reporting machine it reads
  `list_reconcilable_by_machine(machine_id)`: pending, live, and orphaned rows,
  in full. It keeps the rows with an `agent_run_id` whose run is not
  recovery-protected and matches each to the report by `terminal_id`. For each
  row it re-reads the owning run. "Ended" means the run has a terminal status.
  To settle a run after its terminal exits: an ended run gets nothing more,
  and for an active run the reconciler calls `dead_reason`, where `None` means
  a completion is settling the run, and otherwise calls
  `cleanup_agent(run, terminal_payload=<reason>)`.
  - Listed in `agents` and pending: 1.4's route already recorded the
    entry's workspace row before it answered 200, so the reconciler calls
    `record_spawn_identity`.
    - `record_spawn_identity(run_id, terminal_id, entry, host_epoch)` promotes
      the terminal with `promote_to_live` (the entry's `locator` and
      `locator_key`, and the report's `host_epoch`), then re-reads the run. An
      active run gets `pid` and `terminal_id` through `update_runtime`. A run
      that already ended had its session end arrive first, which could not
      exit the still-pending terminal, so the helper calls `mark_exited` and
      records nothing on the run. `promote_to_live` is a pending-to-live CAS,
      so an exited row is never promoted again.
  - Listed in `agents` and live or orphaned with the report's `host_epoch`:
    the row clears its absent record.
  - Listed in `accepted`: the row clears its absent record. The node's
    prepare deadline (1.1) bounds how long a run stays there.
  - Pending with an exit whose `launched` is false: the spawn failed before
    launch, and the node removed what it prepared. The reconciler calls
    `fail_pending` at once, and for an active run
    `cleanup_agent(run, terminal_payload="remote spawn failed on <machine>: <reason>")`.
  - Every other row is absent. A live row whose `host_epoch` differs from the
    report's and a row with a `launched: true` exit count as absent too, so a
    forwarded session end can settle the run first. The first absent report
    only records the row. On the second consecutive one:
    - A live or orphaned row gets `mark_exited`, then the run settles.
    - A pending row whose `attempt_started_at` is older than
      `spawn_in_doubt_seconds` gets a fence, for an active or an ended run. An
      ended status does not prove that a queued `agent.spawn` cannot still
      arrive. The reconciler sends `agent.cancel` with `signal: "KILL"` and
      `close_terminal: true` through `node_command_client()`. On
      `{stopped: true}` or `not_found` it calls `fail_pending`, the
      pending-to-exited CAS, then the run settles. `{stopped: false}` and
      every other reply or error leave the row for the next report. The
      node's cancel record is durable (1.1), so a delayed `agent.spawn`
      cannot start the run after this settlement.
  - Exits that match no reconcilable row are dropped.
- Run sweep: for each reporting machine the pass also reads every active run,
  paging `list_active_for_machine(machine_id, limit=100, offset=...)` until a
  page comes back short, because the method returns only its first 100 rows by
  default. It collects every page before it settles anything, so a settlement
  cannot shift a later page's offset. A run that is not recovery-protected,
  has at least one terminal row (`list_for_session` on its child session), and
  has none pending, live, or orphaned is recorded on the first report. On the
  second consecutive report the reconciler re-reads the run and its terminal
  rows. If the run is still active and still has no pending, live, or
  orphaned terminal, it gets `cleanup_agent` with `"remote terminal exited
  before its run settled"`. Otherwise its record clears. The wait lets a flow
  that is already settling the run finish first.

Consumers unchanged:
- `src/gobby/agents/lifecycle_monitor.py` — no-edit-reason: it constructs `AgentHealthMonitor` with unchanged arguments and calls `check_unhealthy_agents()`, whose signature and return type are unchanged.
- `tests/agents/test_lifecycle_monitor.py` — no-edit-reason: its inbox holds no report, so the reconciler visits no machine, sends nothing, and adds 0; re-run as verification.

Verification planned:
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/hub/test_remote_reconcile.py tests/agents/test_lifecycle_monitor.py -v`.

**Acceptance:**

- 2.6.1 - A report listing a pending run's agent promotes its terminal live with the entry's locator and the report's `host_epoch` and records `pid` and `terminal_id` on the run; when the agent's session end arrived first, the terminal exits, the run stays ended, and no `pid` is recorded. test: `tests/hub/test_remote_reconcile.py::test_listed_pending_run_is_promoted`.
- 2.6.2 - A run with a live terminal that a report omits, or a live row from an older `host_epoch`, is left on its first absent report and settled on the second through `mark_exited` and `cleanup_agent`; a run that settles in between is not failed, and its terminal still exits. test: `tests/hub/test_remote_reconcile.py::test_absent_live_run_settles_after_two_reports`.
- 2.6.3 - An exit with `launched: false` fails the pending terminal and the active run at once with the node's reason and sends no command, and an exit with `launched: true` for a live row waits for a second report like an absent row. test: `tests/hub/test_remote_reconcile.py::test_exits_settle_by_launch`.
- 2.6.4 - A pending run past the deadline and absent from two consecutive reports is sent `agent.cancel` first. `{stopped: true}` or `not_found` then fails its terminal and run, `node_timeout` or `{stopped: false}` changes nothing, and a run listed in `accepted` is never fenced. test: `tests/hub/test_remote_reconcile.py::test_in_doubt_spawn_is_fenced_before_settlement`.
- 2.6.5 - A machine with no new report gets no command and its rows stay as they are, and local rows are never reconciled. test: `tests/hub/test_remote_reconcile.py::test_unreported_and_local_rows_untouched`.
- 2.6.6 - In this case the spawn's effect lands and its reply is lost, the agent exits between reports so none lists it, and its session start and session end both reach the hub. Past the deadline the pending terminal is fenced with `agent.cancel`. After `not_found` it exits, the run stays ended, and there is no `cleanup_agent`. A later `agent.spawn` for that run is refused `cancelled` by the fake node. After `{stopped: false}` or `node_timeout` the terminal stays pending. The same row on a machine with no new report stays untouched. test: `tests/hub/test_remote_reconcile.py::test_ended_run_terminal_still_settles`.
- 2.6.7 - An active remote run whose terminal rows have all exited, as a hub restart between `fail_pending` and the run's settlement leaves it, is left on the first report and settled through `cleanup_agent` on the second, including when 100 healthy active runs on that machine put it past the first page. A run that completes, or gains a pending or live terminal, between the two reports is not swept, and a run with no terminal row is never swept. test: `tests/hub/test_remote_reconcile.py::test_run_left_active_after_terminal_exit_settles`.

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
  5.1 proves live delivery.

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

## P4: Remote workspaces and the machine-scoped project view
`kind: framing`

**Goal**: the hub lists, merges, removes, and expires a node's worktrees and
clones through that node (Decision 20), and gclient's projects pane follows
the machine filter with each checkout's git state (Decision 21).

### 4.1 Node-commands contract version 3: workspace and checkout commands [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `docs/contracts/node-commands.md`
- `crates/gdaemon/src/node_commands.rs::*` — scope-reason: sets `CONTRACT_VERSION` to 3, adds the three commands' payload and result types, and makes every nullable field required
- `src/gobby/hub/node_commands.py::*` — scope-reason: the same model changes on the Python side, `strict=True` on the base model, and `_PARSERS` entries
- `crates/gdaemon/tests/fixtures/node_commands/corpus.json::*` — scope-reason: every message changes to `v: 3`, the new commands gain messages, and an `invalid` list is added
- `crates/gdaemon/tests/node_command_corpus.rs::corpus_round_trips`
- `tests/hub/test_node_command_corpus.py::test_corpus_round_trips`
- `crates/gdaemon/tests/node_command_parity.rs`
- `tests/hub/test_node_command_parity.py`

**Research context:**
- node-halves 3.1 sets `CONTRACT_VERSION` to 2 in both models, and every
  handler and route refuses another `v` (cross-plan edge under
  Constraints). The corpus is `{"messages": [{name, kind, json}]}`, and
  Python's `parse_message(kind, raw)` dispatches through `_PARSERS`.
- The review of #24268 (session 16011, 2026-10-13) found three parity gaps:
  Python's `NodeModel` lacks `strict=True`, so `"1"` parses as an integer
  and `1` as a boolean; a Rust `Option` field accepts omission, because
  serde treats a missing `Option` as `None`; and a Python nullable field
  without a default is required. So a message omitting a nullable field
  parses in Rust and fails in Python.
- Decision 20 sets the merge and removal outcomes and Decision 21 the
  status fields. `get_status` (`src/gobby/servers/routes/source_control.py`)
  returns `current_branch`, `branch_count`, `ahead`, and `behind` from
  `git for-each-ref` and `parse_upstream_track`.
- A node's workspace path is `<home>/worktrees/<project_id>/<run_id>` or
  `<home>/clones/<project_id>/<run_id>` (node-halves 2.3).

**Implementation:**
- Both sides set `CONTRACT_VERSION = 3`, and every corpus message carries
  `v: 3`. The refusal rule for another `v` is unchanged.
- New commands, hub to node:
  - `workspace.merge`: `{project_id, mode: "worktree"|"clone", path, branch_name, target_branch, source_root}`
    returns `{outcome: "merged"|"already_merged"|"conflict"|"target_dirty"|"busy", merge_sha, target_head_sha, conflicted_files, dirty_paths}`.
    `merge_sha` is set only with `merged`, `target_head_sha` with `merged`
    and `already_merged`, `conflicted_files` only with `conflict`, and
    `dirty_paths` only with `target_dirty`; otherwise they are null or
    empty.
  - `workspace.remove`: `{project_id, mode, path, branch_name, source_root, force, force_delete_branch, merged_into}`
    returns `{outcome: "removed"|"dirty"|"unmerged"|"busy"}`. `merged_into`
    is a branch name or null.
  - `checkout.status`: `{project_id, root_path}` returns
    `{current_branch, branch_count, ahead, behind}`, where
    `current_branch`, `ahead`, and `behind` may be null.
- The contract states each command's node rules from 4.2 and 4.6: the path
  and root checks, the `busy` rule, the merge and removal steps, and that
  `checkout.status` is read-only, so every error it answers carries
  `stage: "none"`.
- Parity: the Python base model gains `strict=True`. Every Rust `Option`
  field carries `#[serde(deserialize_with = "Option::deserialize")]`, so a
  missing field fails and an explicit null parses, matching Python's
  required nullable fields.
- `corpus.json` gains each new command, each outcome's reply, and an
  `invalid` list of `{name, kind, json}` messages that both sides must
  refuse: a string for an integer, an integer for a boolean, an omitted
  nullable field, an unknown field, and a `v` of 0.

Verification planned:
- `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_command_corpus) + binary(node_command_parity)'`.
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/hub/test_node_command_corpus.py tests/hub/test_node_command_parity.py -v`.

**Acceptance:**

- 4.1.1 - Every corpus message, including the three new commands and one reply per outcome, round-trips through the Rust models with `v` equal to `CONTRACT_VERSION`. test: `crates/gdaemon/tests/node_command_corpus.rs::corpus_round_trips`.
- 4.1.2 - The same corpus round-trips through the Python models with `v` equal to `CONTRACT_VERSION`. test: `tests/hub/test_node_command_corpus.py::test_corpus_round_trips`.
- 4.1.3 - The Rust models refuse every message in the corpus's `invalid` list. test: `crates/gdaemon/tests/node_command_parity.rs::invalid_messages_fail`.
- 4.1.4 - The Python models refuse every message in the corpus's `invalid` list. test: `tests/hub/test_node_command_parity.py::test_invalid_messages_fail`.
- 4.1.5 - The contract specifies version 3's three commands with their payloads, results, outcomes, node rules, and stage rule, and the strictness rule for nullable fields. file: `docs/contracts/node-commands.md`.

### 4.2 Node workspace.merge and workspace.remove handlers [category: code] (depends: 4.1)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/nodes/agents/commands.rs`
- `crates/gdaemon/src/nodes/agents/mod.rs`
- `crates/gdaemon/src/nodes/agents/workspace.rs`
- `crates/gdaemon/src/nodes/agents/finish.rs`
- `crates/gdaemon/tests/node_workspace_finish.rs`

**Granularity:** one leaf with seven acceptance items. Merge and removal
share one claim on a workspace's path and checkout (`finishing`) and one
`busy` rule, and removal follows merge in every lifecycle. Splitting them
would leave the claim with one user and a second leaf that must re-prove it.

**Research context:**
- node-halves creates the three existing targets: 2.3 `workspace.rs`
  (`plan_workspace`, `prepare`, `remove(record)`, `retry_removals`, and the
  `existing` root check), 2.7 `commands.rs::dispatch` (it parses
  `request_id`, `v`, and `command` first, and runs per-run queues), and 2.1
  `mod.rs` (`NodeAgents` and its durable state, including the launched
  workspace record kept until a report carrying the run's exit gets 200).
  This leaf lands after node-halves 2.14 (Constraints).
- The local merge oracle is `_merge_worktree_impl`
  (`src/gobby/mcp_proxy/tools/worktrees/_sync.py`) and `_merge_clone_impl`
  (`src/gobby/mcp_proxy/tools/_clones_operations.py`): the ref checks, the
  already-merged check, the dirty and overlap check against
  `git diff --name-only <target>...<source>`, `git merge --no-ff --no-edit`,
  and, for a clone, a fetch into `refs/heads/clone-merge/<branch>` deleted
  afterwards. Decision 20 states where the node is stricter.
- A run with workspace mode `none` runs in `source_root` (1.1), so a merge
  into that checkout would move files under a live agent.
- The review of #24268 asked that no handler infer authorization from a
  successful parse, so every path the hub names is checked against the
  node's own roots.

**Implementation:**
- `dispatch` sends `workspace.merge` and `workspace.remove`, which name no
  run, to `finish::merge` and `finish::remove` outside the per-run queues,
  after its `v`, command, and payload checks.
- Checks before any effect, each answering `invalid_payload` with
  `stage: "none"`:
  - `path` canonicalizes to a directory name directly under
    `<home>/worktrees/<project_id>/` for `worktree` or
    `<home>/clones/<project_id>/` for `clone`. `workspace.rs` exposes the
    `existing` mode's root check as `owned_workspace_path`, and both use it.
  - `source_root` is absolute and equals
    `git -C <source_root> rev-parse --show-toplevel`.
  - `branch_name`, `target_branch`, and `merged_into` pass
    `git check-ref-format --branch`.
- `NodeAgents` gains `finishing: Mutex<HashSet<PathBuf>>`. Under the agents'
  state lock, a merge or removal answers `busy` when an accepted or live
  run's workspace record or a launched workspace record names `path`, when
  a run with mode `none` runs in `source_root`, or when `finishing` holds
  `path` or `source_root`. Otherwise it inserts both, and a drop guard
  removes them. `plan_workspace` fails an `existing` path that `finishing`
  holds or a pending removal record names (`prepare_failed`, naming the
  path), so a resume never starts in a workspace being finished.
- `finish::merge`:
  1. Clone mode fetches `git -C <source_root> fetch <path> <branch_name>:refs/heads/clone-merge/<branch_name>`
     and merges that ref; worktree mode merges `branch_name`. A missing
     branch or clone path answers `not_found`. The temporary ref is deleted
     on every path after the fetch.
  2. When the source is an ancestor of `target_branch`
     (`git merge-base --is-ancestor`), it answers `already_merged`.
  3. Staged entries in `source_root`, or unstaged and untracked paths that
     `git diff --name-only <target_branch>...<source>` names, answer
     `target_dirty` with those paths. `.gobby/` paths count too.
  4. It records the current branch. A detached HEAD answers
     `command_failed` with `stage: "none"`. When the current branch is not
     `target_branch`, it checks out `target_branch`; a refusal answers
     `command_failed` with `stage: "none"`.
  5. `git merge --no-ff --no-edit <source>` lands `merged` with
     `merge_sha` and `target_head_sha`. On a conflict it reads
     `git diff --name-only --diff-filter=U`, runs `git merge --abort`, and
     answers `conflict` with those files. Any other failure after step 4
     aborts a merge in progress and answers `command_failed` with
     `stage: "partial"`.
  6. It checks out the recorded branch again in every case after step 4.
- `finish::remove`:
  1. A missing path answers `removed`; worktree mode also runs
     `git worktree prune` and deletes a remaining branch.
  2. Without `force`, `git -C <path> status --porcelain` output answers
     `dirty`.
  3. Without `force_delete_branch`, the branch head must be an ancestor of
     `merged_into` in `source_root`, or with `merged_into` null, of another
     local branch there (`git for-each-ref --contains`); else `unmerged`. A
     clone's branch head that `source_root` does not hold is unmerged.
  4. It commits a pending removal record for the path and branch, then
     runs 2.3's `remove(record)`. Success deletes the record and answers
     `removed`. A failure keeps the record for `retry_removals` and answers
     `command_failed` with `stage: "partial"`.

Verification planned:
- `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_workspace_finish) + binary(node_agents)'`.

**Acceptance:**

- 4.2.1 - A worktree merge lands a `--no-ff` merge commit on the target in the node's checkout, restores the checkout's original branch, and answers `merged` with both shas. test: `crates/gdaemon/tests/node_workspace_finish.rs::merge_lands_in_node_checkout`.
- 4.2.2 - A clone merge fetches into `refs/heads/clone-merge/<branch>`, merges it, and leaves no temporary ref, including after a conflict. test: `crates/gdaemon/tests/node_workspace_finish.rs::clone_merge_uses_temporary_ref`.
- 4.2.3 - An already-merged branch answers `already_merged`, and staged entries or overlapping dirty paths answer `target_dirty` with those paths, each leaving the checkout's branch and files unchanged. test: `crates/gdaemon/tests/node_workspace_finish.rs::merge_refusals_change_nothing`.
- 4.2.4 - A conflict answers `conflict` with the conflicted files, and the checkout ends clean on its original branch. test: `crates/gdaemon/tests/node_workspace_finish.rs::conflict_aborts_and_restores`.
- 4.2.5 - A merge or removal answers `busy` while an accepted or live run or an unacknowledged launched record names the path, while a mode `none` run uses the checkout, or while another merge or removal holds either, and an `existing` spawn for a path being finished fails preparation. test: `crates/gdaemon/tests/node_workspace_finish.rs::busy_while_in_use`.
- 4.2.6 - Removal answers `removed` for a clean merged worktree or clone and for a missing path, `dirty` and `unmerged` without the force flags, and `removed` with them; a failed removal keeps its record, answers `command_failed` with `stage: "partial"`, and the retry removes it. test: `crates/gdaemon/tests/node_workspace_finish.rs::remove_outcomes`.
- 4.2.7 - A path outside the project's node roots, another project's root, a relative or non-top-level `source_root`, and an invalid branch name each answer `invalid_payload` with `stage: "none"` and touch nothing. test: `crates/gdaemon/tests/node_workspace_finish.rs::paths_outside_node_roots_refused`.

### 4.3 Hub remote workspace module and tool routing [category: code] (depends: 1.3, 1.4, 4.1)
`kind: deliverable`

Targets:
- `src/gobby/hub/remote_workspaces.py`
- `src/gobby/mcp_proxy/tools/worktrees/_factory.py::create_worktrees_registry`
- `src/gobby/mcp_proxy/tools/clones.py::create_clones_registry`
- `src/gobby/mcp_proxy/tools/worktrees/_crud.py::create_crud_registry`
- `src/gobby/mcp_proxy/tools/_clones_read.py::create_clone_read_registry`
- `tests/hub/test_remote_workspaces.py`

**Research context:**
- `LocalWorktreeManager.get` and `LocalCloneManager.get` call
  `get_owned_workspace_row` (`src/gobby/storage/workspace_machine_scope.py`),
  which raises `MachineOwnershipMismatchError` for another machine's row.
  `InternalToolRegistry.call` and `call_sync`
  (`src/gobby/mcp_proxy/tools/internal.py`) return its `to_dict()`, and the
  `gobby worktrees` and `gobby clones` CLI post to
  `/api/mcp/<server>/tools/<tool>`, which reaches `call`.
- `merge_worktree` (`worktrees/_sync.py`), `delete_worktree`
  (`worktrees/_lifecycle.py`), `merge_clone`, and `delete_clone`
  (`_clones_operations.py`) are async and check the git manager before the
  row lookup (`_merge_clone_impl` first returns "Clone tools require a git
  repository context"), so catching the ownership error is unreliable.
  Ownership is resolved first.
- `ctx.resolve_worktree_id` calls `LocalWorktreeManager.resolve_reference`,
  which resolves an id prefix across all machines; clone tools take a full
  id. `delete_worktree` and `delete_clone` also accept a path, which names
  a path on this machine.
- `InternalToolRegistry.get_tool_metadata(name)` returns the `InternalTool`
  whose `func` `_prepare_call` inspects with `inspect.signature`, which
  follows `functools.wraps`'s `__wrapped__`. `merge_clone` registers an
  explicit `input_schema`; the others infer theirs.
- The local tools mark a merged worktree with `mark_merged(cleanup_days=0)`
  and a merged clone with `cleanup_after` seven days on. `worktrees` and
  `clones` carry `status`, `merged_at`, `cleanup_after`, and `updated_at`
  (`crates/gcore/assets/schema/baseline.sql`).
- `LocalProjectCheckoutManager(db).get(machine_id, project_id)` reads a
  node's checkout row (1.3), and `node_command_client()` sends commands
  (1.3).

**Implementation:**
- `remote_workspaces.py` reads and writes `worktrees` and `clones` rows by
  id with no machine guard (Decision 18):
  - `owner_machine(db, kind, workspace_id)`, `get_remote(db, kind, id)`, and
    `list_remote(db, kind, machine_id, project_id=None, status=None)`, which
    return `Worktree` or `Clone` rows.
  - `merge_remote(db, kind, row, *, target_branch, client)`: the target is
    `target_branch`, else the row's `base_branch`; with neither it returns
    `{"success": False, "error_code": "target_branch_required"}` before any
    command. The node's checkout row gives `source_root`, and without one
    it returns `checkout_unresolved`. It sends `workspace.merge`. On
    `merged` or `already_merged` it sets `status = 'merged'`, `merged_at`,
    and `cleanup_after` (now for a worktree, seven days on for a clone)
    `WHERE id = %s AND machine_id = %s`, and returns the local tool's
    success shape with `merge_sha`, `target_head_sha`, and `machine_id`.
    `conflict` returns `has_conflicts` and `conflicted_files`,
    `target_dirty` returns `dirty_paths`, and `busy` returns
    `workspace_busy`; each keeps the row.
  - `remove_remote(db, kind, row, *, force, force_delete_branch, merged_into, client)`
    sends `workspace.remove` and deletes the row
    `WHERE id = %s AND machine_id = %s` only on `removed`. `dirty`,
    `unmerged`, and `busy` return their codes and keep the row.
  - A `NodeCommandError` returns `{"success": False, "error_code", "stage"}`
    and changes no row. No function clears `agent_session_id`.
  - `route_node_workspace_tools(registry, *, kind, db, resolve_id)` wraps
    `get_<kind>`, `merge_<kind>`, and `delete_<kind>`: it sets each tool's
    `func` to an async wrapper made with `functools.wraps(original)`. The
    wrapper resolves the id first; a row on another machine goes to the
    remote function with the call's arguments, and everything else,
    including a path-addressed call and an unresolved id, calls the original
    unchanged. A remote `merge_worktree` with `push` or `prefer_remote` set,
    or a `source_branch` other than the row's branch, returns
    `remote_merge_option_unsupported` naming the option before any command
    (Decision 20). `project_path` only selects a local checkout, so a
    remote call ignores it.
- `create_worktrees_registry` and `create_clones_registry` call
  `route_node_workspace_tools` after merging their sub-registries.
- `list_worktrees` (in `create_crud_registry`) and `list_clones` (in
  `create_clone_read_registry`) gain `machine_id: str | None`, added to an
  explicit `input_schema` where one exists. Null or this machine's id keeps
  today's query; another machine reads `list_remote`.

Verification planned:
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/hub/test_remote_workspaces.py tests/mcp_proxy/tools/ -k "worktree or clone" -v`.

**Acceptance:**

- 4.3.1 - `merge_worktree` with a prefix of a node's row sends one `workspace.merge` with the row's path and branch, the target, and that machine's checkout root, and on `merged` marks the row merged with `cleanup_after` now; `merge_clone` does the same with `cleanup_after` seven days on. test: `tests/hub/test_remote_workspaces.py::test_remote_merge_marks_row_merged`.
- 4.3.2 - `conflict`, `target_dirty`, and `busy` return their typed failures and keep the row, and a missing target, `push`, `prefer_remote`, or another `source_branch` is refused before any command. test: `tests/hub/test_remote_workspaces.py::test_remote_merge_refusals_keep_row`.
- 4.3.3 - `delete_worktree` and `delete_clone` on a node's row send `workspace.remove` with the force flags and `merged_into`, delete the row only on `removed`, keep it on `dirty`, `unmerged`, `busy`, or a node error, and never clear `agent_session_id`. test: `tests/hub/test_remote_workspaces.py::test_remote_delete_follows_node_outcome`.
- 4.3.4 - `get_worktree` and `get_clone` return a node's row, a local row and a path-addressed call reach the original tool unchanged, and every wrapped tool keeps its input schema. test: `tests/hub/test_remote_workspaces.py::test_local_rows_and_schemas_unchanged`.
- 4.3.5 - `list_worktrees` and `list_clones` with a node's `machine_id` list only that machine's rows, and without it only this machine's. test: `tests/hub/test_remote_workspaces.py::test_list_tools_take_machine_id`.
- 4.3.6 - An offline node returns `node_offline` with `stage: "none"` and changes no row, and `sync_worktree` and `release_worktree` on a node's row still return `machine_ownership_mismatch`. test: `tests/hub/test_remote_workspaces.py::test_offline_and_other_tools_refuse`.

### 4.4 REST workspace routes list by machine and finish through the node [category: code] (depends: 4.3)
`kind: deliverable`

Targets:
- `src/gobby/servers/routes/source_control_worktrees.py::create_source_control_worktrees_router`
- `tests/servers/test_remote_workspace_routes.py`

**Research context:**
- `create_source_control_worktrees_router` nests `list_worktrees` and
  `list_clones`, which list this machine's rows; `delete_worktree`
  (`DELETE /worktrees/{worktree_id}?merged_into=`), which answers 409 with
  `MachineOwnershipMismatchError.to_dict()` for another machine's row; and
  `run_clone_operation`, which serves `delete_clone` and `sync_clone`,
  calls `clone_storage.get` first, and builds a clones registry from this
  machine's checkout.
- gclient removes a worktree through `DELETE /worktrees/{worktree_id}`
  (`crates/gclient/src/daemon/rest.rs`), and 4.8 lists a node's worktrees.

**Implementation:**
- `GET /worktrees` and `GET /clones` take `machine_id`; another machine's id
  reads 4.3's `list_remote`.
- `delete_worktree` reads the row's owner first. A node's row goes to
  `remove_remote(force=False, force_delete_branch=False, merged_into=merged_into)`:
  `removed` answers 200 `{success: true, id, git_deleted: true}`, `dirty`,
  `unmerged`, and `busy` answer 409 `{success: false, id, error_code}`,
  `node_offline` answers 503, and any other `NodeCommandError` answers 502,
  each with `{error_code, stage}`.
- `run_clone_operation` reads the owner first. `delete_clone` on a node's
  clone goes to `remove_remote` with the same mapping, and `sync_clone`
  keeps its 409 (Decision 20).

Verification planned:
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/test_remote_workspace_routes.py -v`.

**Acceptance:**

- 4.4.1 - `GET /worktrees` and `GET /clones` with a node's `machine_id` list that machine's rows, and without it this machine's. test: `tests/servers/test_remote_workspace_routes.py::test_list_routes_take_machine_id`.
- 4.4.2 - `DELETE /worktrees/{id}` and the clone delete on a node's row remove through the node, answer 200 and delete the row on `removed`, and answer 409, 503, or 502 with the code and keep the row otherwise. test: `tests/servers/test_remote_workspace_routes.py::test_delete_routes_finish_through_node`.
- 4.4.3 - The clone sync route on a node's clone answers 409 `machine_ownership_mismatch` and sends nothing. test: `tests/servers/test_remote_workspace_routes.py::test_sync_route_keeps_ownership_refusal`.

### 4.5 Remote workspace expiry [category: code] (depends: 4.3)
`kind: deliverable`

Targets:
- `src/gobby/hub/remote_workspaces.py`
- `src/gobby/runner_maintenance/isolation.py::cleanup_expired_isolation_loop`
- `tests/hub/test_remote_workspace_expiry.py`

**Research context:**
- `cleanup_expired_isolation_loop` runs hourly on the Python daemon, which
  runs only on a hub or a standalone machine (#24075). It reaps
  `LocalWorktreeManager.find_expired` and `LocalCloneManager.find_expired`
  rows: `status = 'merged'`, this machine, `agent_session_id IS NULL`,
  `cleanup_after < now`, and `CLOSED_TASK_CLEANUP_PREDICATE`
  (`src/gobby/storage/isolation_cleanup.py`).
- A node's row keeps its `agent_session_id` (Decision 20), and the node's
  `busy` rule guards a workspace in use (4.2).

**Implementation:**
- `expire_remote_workspaces(db, *, client, run_db)` selects up to 1,000
  merged `worktrees` and `clones` rows on other machines whose
  `cleanup_after` has passed, with `CLOSED_TASK_CLEANUP_PREDICATE` and no
  `agent_session_id` predicate. For each it calls `remove_remote` with
  `force=False`, `force_delete_branch=False`, and `merged_into=None`. A
  non-`removed` outcome or a node error keeps the row for the next pass and
  logs at debug, as the local reaper does.
- `cleanup_expired_isolation_loop` calls it after the local clone reaping,
  inside the same per-pass `try`.

Verification planned:
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/hub/test_remote_workspace_expiry.py tests/runner_maintenance/ -v`.

**Acceptance:**

- 4.5.1 - An expired merged node row of a closed task gets one `workspace.remove` without force and is deleted on `removed`, whatever its `agent_session_id`. test: `tests/hub/test_remote_workspace_expiry.py::test_expired_node_rows_removed_through_node`.
- 4.5.2 - An unexpired, unmerged, or open-task row gets no command, and `busy`, `dirty`, `unmerged`, or an offline node keeps the row for the next pass. test: `tests/hub/test_remote_workspace_expiry.py::test_expiry_keeps_rows_the_node_refuses`.
- 4.5.3 - The loop runs the remote step after local reaping in each pass. test: `tests/hub/test_remote_workspace_expiry.py::test_loop_runs_remote_step`.

### 4.6 Node checkout.status handler [category: code] (depends: 4.1, 4.2)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/nodes/agents/commands.rs`
- `crates/gdaemon/src/nodes/agents/mod.rs`
- `crates/gdaemon/src/nodes/agents/checkout_status.rs`
- `crates/gdaemon/tests/fixtures/node_commands/upstream_track.json`
- `crates/gdaemon/tests/node_checkout_status.rs`
- `tests/hub/test_upstream_track_golden.py`

**Research context:**
- `get_status` runs `git for-each-ref --format=%(HEAD)\t%(refname:short)\t%(upstream:short)\t%(upstream:track) refs/heads/`
  with a 10-second timeout (`source_control_git.py::_run_git`), counts
  branches, takes the `*` line as the current branch, and reads ahead and
  behind with `parse_upstream_track` only when the branch has an upstream.
- `parse_upstream_track` reads `[ahead N`, `behind N`, both, and `[gone]`,
  and yields 0 for a count it cannot read.
- 4.2 also edits `commands.rs` and `mod.rs`.

**Implementation:**
- `dispatch` sends `checkout.status` to `checkout_status::status` outside
  the per-run queues. `root_path` must be absolute and equal its
  `git rev-parse --show-toplevel`, else `invalid_payload`.
- `status` runs the same `for-each-ref` with a 10-second deadline and
  returns `{current_branch, branch_count, ahead, behind}` as `get_status`
  computes them. A failed or late git answers `command_failed`; every error
  carries `stage: "none"`.
- `upstream_track.json` (new) lists track strings with their ahead and
  behind counts. `test_upstream_track_golden.py` checks them against
  `parse_upstream_track`, and the Rust parser must match every entry.

Verification planned:
- `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_checkout_status)'`.
- `GOBBY_TEST_PROTECT=1 uv run pytest tests/hub/test_upstream_track_golden.py -v`.

**Acceptance:**

- 4.6.1 - For a checkout with three branches, the current one tracking an upstream two ahead and one behind, `checkout.status` answers `{current_branch, branch_count: 3, ahead: 2, behind: 1}`, and a branch with no upstream answers null counts. test: `crates/gdaemon/tests/node_checkout_status.rs::status_matches_local_fields`.
- 4.6.2 - The Rust track parser matches every entry of the golden. test: `crates/gdaemon/tests/node_checkout_status.rs::track_parser_matches_golden`.
- 4.6.3 - `parse_upstream_track` produces every entry of the golden. test: `tests/hub/test_upstream_track_golden.py::test_golden_matches_python_parser`.
- 4.6.4 - A relative or non-top-level `root_path` answers `invalid_payload`, and a failing git answers `command_failed`, both with `stage: "none"`. test: `crates/gdaemon/tests/node_checkout_status.rs::status_refusals_change_nothing`.

### 4.7 Hub projects and source-control status take machine_id [category: code] (depends: 1.3, 4.3)
`kind: deliverable`

Targets:
- `src/gobby/servers/routes/projects.py::_projects_to_responses`
- `src/gobby/servers/routes/projects.py::_build_project_response`
- `src/gobby/servers/routes/projects.py::create_projects_router`
- `src/gobby/servers/routes/source_control.py::create_source_control_router`
- `tests/servers/test_machine_scoped_projects.py`

**Research context:**
- `list_projects` (in `create_projects_router`) calls
  `_projects_to_responses(server, projects)`, which reads
  `_local_checkouts` (this machine's `list_for_machine`) and calls
  `_build_project_response`. That function reads approval rules and
  validation detection from the checkout root on disk.
  `src/gobby/servers/routes/files.py` calls `_projects_to_responses`
  positionally.
- `get_status` (in `create_source_control_router`) resolves this
  machine's checkout through `_resolve_project`, caches git fields for
  30 s with `_get_cached` and `_set_cached` (`_STATUS_TTL`), and counts
  this machine's worktrees and clones.
- Decision 21 rules the remote status.

**Implementation:**
- `_projects_to_responses` gains keyword-only `machine_id: str | None = None`.
  Null or this machine keeps today's behavior. Another machine lists only
  projects with a checkout row on that machine, read with
  `LocalProjectCheckoutManager(db).list_for_machine(machine_id)`, each with
  that checkout, and `_build_project_response(..., read_checkout_files=False)`
  answers `approval_rules` `[]` and `validation_detection` null without
  touching the path. `list_projects` takes the `machine_id` query parameter.
- `get_status` takes `machine_id`. Another machine needs `project_id`
  (422 `project_id_required`) and its checkout row (404
  `checkout_unresolved`). Git fields come from the cache key
  `remote-status:<machine_id>:<project_id>`, else from one
  `checkout.status` with a 15-second timeout, cached only on success. A
  `NodeCommandError` answers 200 with `current_branch`, `ahead`, and
  `behind` null and `branch_count` 0. `worktree_count` and `clone_count`
  come from 4.3's `list_remote`, and `repo_path` is the node's root.

Consumers unchanged:
- `src/gobby/servers/routes/files.py` — no-edit-reason: it calls `_projects_to_responses(server, visible)`, whose new keyword defaults to today's behavior.

Verification planned:
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/test_machine_scoped_projects.py tests/servers/routes/test_files.py -v`.

**Acceptance:**

- 4.7.1 - `/api/projects?machine_id=<node>` lists only projects with that node's checkout, each with the node's root, empty approval rules, and null validation detection, and reads no path. test: `tests/servers/test_machine_scoped_projects.py::test_projects_for_node_machine`.
- 4.7.2 - `/api/source-control/status?machine_id=<node>&project_id=<p>` answers the node's git fields and that machine's worktree and clone counts, and a second call within 30 s sends no command. test: `tests/servers/test_machine_scoped_projects.py::test_remote_status_cached`.
- 4.7.3 - An offline node answers 200 with null git fields and is asked again on the next call. test: `tests/servers/test_machine_scoped_projects.py::test_offline_node_shows_no_stale_state`.
- 4.7.4 - Without `machine_id`, both routes answer exactly as before. test: `tests/servers/test_machine_scoped_projects.py::test_local_routes_unchanged`.

### 4.8 gclient projects pane follows the machine filter [category: code] (depends: 3.2, 4.4, 4.7)
`kind: deliverable`

Targets:
- `crates/gclient/src/daemon/mod.rs::*` — scope-reason: three `Daemon` methods gain `machine`, and `ScriptedState` and `ScriptedDaemon` move to `scripted.rs`
- `crates/gclient/src/daemon/scripted.rs`
- `crates/gclient/src/daemon/live_connect.rs::LiveDaemon::projects`
- `crates/gclient/src/daemon/live_connect.rs::LiveDaemon::source_status`
- `crates/gclient/src/daemon/live_connect.rs::LiveDaemon::worktrees`
- `crates/gclient/src/daemon/rest.rs::RestClient::projects`
- `crates/gclient/src/daemon/rest.rs::RestClient::source_status`
- `crates/gclient/src/daemon/rest.rs::RestClient::worktrees`
- `crates/gclient/src/daemon/rest/tests.rs::one_deadline_spans_send_and_body`
- `crates/gclient/src/app/mod.rs::*` — scope-reason: `Workspace` gains `project_scope`, `Workspace::scripted` sets it, and the worktree re-exports name `live_loop::worktrees`
- `crates/gclient/src/app/live.rs::*` — scope-reason: `Workspace::live` sets `project_scope`
- `crates/gclient/src/app/live_sidebar.rs::*` — scope-reason: the sidebar fetch carries the scope through `fetch_sidebar_rows`, `start_sidebar_refetch`, `apply_sidebar_fetch`, `SidebarFetch`, `SidebarRequest`, and `SidebarRequest::run`, and the file gains `scope_projects_to`
- `crates/gclient/src/app/live_loop.rs::*` — scope-reason: declares `worktrees`, and `run_live_loop` calls `scope_projects_to` each tick
- `crates/gclient/src/app/live_loop/project_paths.rs::resolve_project`
- `crates/gclient/src/app/live_loop/projects.rs::*` — scope-reason: the worktree actions move to `worktrees.rs`
- `crates/gclient/src/app/live_loop/worktrees.rs`
- `crates/gclient/src/app/live_loop/actions.rs::*` — scope-reason: imports the worktree actions from `worktrees`
- `crates/gclient/src/app/live_loop/menu_dispatch.rs::*` — scope-reason: imports the worktree actions from `worktrees`
- `crates/gclient/tests/client_loop.rs::ReconnectDaemon::projects`
- `crates/gclient/tests/client_loop.rs::ReconnectDaemon::source_status`
- `crates/gclient/tests/client_loop.rs::ReconnectDaemon::worktrees`
- `crates/gclient/tests/teardown.rs::TraceDaemon::projects`
- `crates/gclient/tests/teardown.rs::TraceDaemon::source_status`
- `crates/gclient/tests/teardown.rs::TraceDaemon::worktrees`
- `crates/gclient/tests/projects_machine_scope.rs`

**Granularity:** one leaf with five acceptance items across eleven
production files. The `Daemon` trait change, its four implementors, and its
callers compile only together. The two splits are required by the line
ceiling, not separate outcomes: `crates/gclient/src/daemon/mod.rs` is 926
lines and `crates/gclient/src/app/live_loop/projects.rs` counts 875 before
its `#[cfg(test)]`, both over the 850-line decomposition trigger.

**Research context:**
- `Daemon` (`crates/gclient/src/daemon/mod.rs`) declares `projects()`,
  `source_status(project)`, and `worktrees(project)`. `LiveDaemon` calls
  `RestClient`, and `ScriptedDaemon` (lines 399 to 926 with
  `ScriptedState`), `ReconnectDaemon`, and `TraceDaemon` implement it too.
- `fetch_sidebar_rows` and `SidebarRequest::run`
  (`crates/gclient/src/app/live_sidebar.rs`) fetch projects, then each
  checked-out project's status and worktrees; `apply_sidebar_fetch`
  installs a fetch by its stamps. `resolve_project`
  (`crates/gclient/src/app/live_loop/project_paths.rs`) reads `projects()` for the local path
  dialogs.
- `chrome.sidebar.machine_filter` is `None` for the local machine,
  `ALL_MACHINES` for every machine, or a machine id (3.2 defaults it to
  `None`). The client's own machine is `read_local_machine_id`
  (`crates/gclient/src/views/mod.rs`).
- `open_worktree` (`crates/gclient/src/app/live_loop/projects.rs`) starts a local shell at the
  worktree's path, and `open_new_worktree_dialog` and `create_worktree`
  create a worktree through the hub's REST create, which runs on the hub's
  machine. Neither can act on a node's workspace (Decision 15).
  `remove_worktree` calls `DELETE /worktrees/{id}`, which 4.4 routes
  through the node.

**Implementation:**
- `projects`, `source_status`, and `worktrees` gain `machine: Option<&str>`
  across the trait, `RestClient`, and every implementor. `RestClient` adds
  `machine_id` to the query when it is set.
- `ScriptedState` and `ScriptedDaemon` move to `crates/gclient/src/daemon/scripted.rs`, which
  `mod.rs` declares and re-exports.
- `Workspace` gains `project_scope: Option<String>`, null for the client's
  own machine. `scope_projects_to(filter)` maps `None`, `ALL_MACHINES`, and
  the local machine's id to null, and any other machine to itself. On a
  change it clears projects, statuses, and worktrees, bumps every sidebar
  stamp, and queues the projects and their rows. `run_live_loop` calls it
  with `chrome.sidebar.machine_filter` each tick.
- `SidebarRequest` and `SidebarFetch` carry the scope, every fetch passes
  it, and `apply_sidebar_fetch` drops a fetch made for another scope.
  `resolve_project` passes `None`, because its dialogs open local paths.
- `crates/gclient/src/app/live_loop/worktrees.rs` (new) takes `open_worktree`,
  `open_new_worktree_dialog`, `create_worktree`,
  `open_open_worktree_dialog`, `open_remove_worktree_dialog`,
  `remove_worktree`, and `worktree_entry` from `projects.rs`. With a
  remote scope, `open_worktree`, `open_open_worktree_dialog`, and
  `open_new_worktree_dialog` show `Toast::warning` naming the machine and
  open nothing. `remove_worktree` works in every scope.

Verification planned:
- `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-client`.

**Acceptance:**

- 4.8.1 - Selecting a node in the machine filter refetches projects, statuses, and worktrees with that node's `machine_id`, and the pane shows the node's projects with its branch and ahead/behind counts. test: `crates/gclient/tests/projects_machine_scope.rs::selecting_a_node_scopes_the_projects_pane`.
- 4.8.2 - `None`, `ALL_MACHINES`, and the local machine's id scope the pane to the local machine. test: `crates/gclient/tests/projects_machine_scope.rs::local_and_all_scope_to_this_machine`.
- 4.8.3 - A fetch started before a scope change does not replace the new scope's rows. test: `crates/gclient/tests/projects_machine_scope.rs::stale_scope_fetch_dropped`.
- 4.8.4 - With a node scoped, opening a worktree or the new and open worktree dialogs shows a warning toast and starts no shell and no request, and removing a worktree sends `DELETE /worktrees/{id}`. test: `crates/gclient/tests/projects_machine_scope.rs::node_worktree_actions`.
- 4.8.5 - A REST project, status, and worktree request carries `machine_id` only when a machine is given. test: `crates/gclient/src/daemon/rest/tests.rs::one_deadline_spans_send_and_body`.

## P5: Live hub-to-node proof
`kind: framing`

**Goal**: a real hub and a real node, both isolated test instances, carry
every command and report in this plan end to end (Decision 22).

### 5.1 Live hub-to-node proof with isolated daemons and fake providers [category: test] (depends: 2.5, 2.6, 3.3, 4.4, 4.5, 4.7)
`kind: deliverable`

Targets:
- `tests/e2e/test_hub_node_live.py`
- `tests/e2e/fake_provider.py`

**Research context:**
- `tests/e2e/test_hub_node_pair.py` holds `pair_hub` (a hub-mode
  `spawn_daemon_instance` with a self-signed certificate over the e2e
  harness's isolated config and database) and `pair_node` (an enrolled bare
  `gdaemon serve` with `GOBBY_HOME` in `tmp_path`, from
  `prepare_daemon_env`'s native bin directory), with `_wait_listener` and
  `_wait_connection`. `pair_node` sets `GOBBY_HOME` and not `HOME`.
- `tests/e2e/conftest.py::link_operator_srt(home)` links the operator's
  `~/.gobby/tools/srt/<version>` into an isolated home and skips when it is
  absent; `e2e_srt_spawn_home` keeps `GOBBY_HOME` outside the workspace.
  `tests/e2e/test_composer_live_proof.py` runs providers under SRT that
  call `ghook`.
- Every spawn runs under managed SRT, and the hub never sends a disabled
  sandbox (2.5). node-halves 2.6 seeds `~/.claude.json` and
  `~/.codex/config.toml` under `HOME`, so both daemons need a temporary
  `HOME`.
- Hook-driven attention: `handle_permission_request` calls
  `_enter_turn_wait(event, "approval")`
  (`src/gobby/hooks/event_handlers/_misc.py`). Grok and agy have no hook
  directory (node-halves 2.3.3), so their runs settle through the node's
  exit report (2.6). The node's agy gate needs version 1.1.18 (node-halves
  2.6), and codex receives its prompt after the composer marker
  (node-halves 2.10).
- node-halves 2.9 registers a checkout on the first hook from its root.
  A hook from an operator's own CLI on the node is refused at session
  registration (Constraints), which the test expects.

**Implementation:**
- Setup: the test gives the hub and the node a temporary `HOME` and
  `GOBBY_HOME` under `tmp_path`, links SRT and the native `ghook` into both
  homes, and creates a git checkout of a test project on the node side. It
  never touches the operator's hub, database, or `~/.gobby`, and teardown
  stops both daemons and removes `tmp_path`.
- `fake_provider.py` is one script that the test installs on the node's
  `PATH` as `claude`, `codex`, `grok`, `droid`, and `agy`. It answers
  `--version` (agy with 1.1.18). Otherwise it runs in its pane: codex
  prints the composer marker from the pane-detection corpus and reads its
  prompt; claude, codex, and droid pipe a session-start envelope into
  `ghook --cli=<provider> --type=session-start` with the run's
  `GOBBY_SESSION_ID`; claude can send a permission request and wait for a
  line; each writes what it read to a file in its workspace, commits it,
  sends its stop and session-end hooks where it has hooks, and exits.
- Steps:
  1. One `ghook` session-start from the checkout root registers the node's
     checkout.
  2. For each provider, `POST /api/agents/spawn` with `machine` set to the
     node and worktree isolation; the report promotes the run live and
     records the worktree row, and the run settles completed.
  3. For claude: a terminal write reaches the pane, its permission request
     raises an attention entry under the node's machine, and the respond
     route types the answer into the pane.
  4. A waiting grok run is cancelled through `kill_agent` and settles
     cancelled.
  5. `merge_worktree` on claude's worktree merges on the node, and
     `delete_worktree` removes it and its row; `merge_clone` and
     `delete_clone` do the same for a clone run.
  6. `/api/projects?machine_id=<node>` and
     `/api/source-control/status?machine_id=<node>` show the node's
     checkout and branch, and after the node stops the status shows null
     git fields.

Verification planned:
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/e2e/test_hub_node_live.py -v -m e2e`.

**Acceptance:**

- 5.1.1 - The node's first hook from its checkout root registers that checkout on the hub under the node's machine. test: `tests/e2e/test_hub_node_live.py::test_node_registers_checkout`.
- 5.1.2 - For each of claude, codex, grok, droid, and agy, a spawn aimed at the node runs the fake there under SRT, the node's report promotes the run live and records its worktree row on the node's machine, and the run settles completed, through hooks for claude, codex, and droid and through the exit report for grok and agy. test: `tests/e2e/test_hub_node_live.py::test_spawn_runs_each_provider_on_node`.
- 5.1.3 - A terminal write reaches the claude fake's pane, and its permission request raises an attention entry on the node's machine whose response the fake receives. test: `tests/e2e/test_hub_node_live.py::test_terminal_write_and_attention_respond_reach_node`.
- 5.1.4 - A cancel stops a waiting run on the node, and the run settles cancelled. test: `tests/e2e/test_hub_node_live.py::test_cancel_stops_node_agent`.
- 5.1.5 - A worktree and a clone each merge into the node's checkout and are removed from the node, with the hub row marked merged and then deleted. test: `tests/e2e/test_hub_node_live.py::test_merge_and_remove_through_node`.
- 5.1.6 - The projects view and status for the node's machine show its checkout and branch, and null git fields once the node stops. test: `tests/e2e/test_hub_node_live.py::test_projects_view_for_node_machine`.

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
- 2026-10-12: Revised for Josh's second review of the stamped plan, relayed by
  gobby#15977; the M1 manifest is withdrawn for re-derivation. Josh asked why
  the hub does not send the node one spawn command, and why the node cannot
  call the hub API with its key instead of needing database writes. A remote
  spawn is now one `agent.spawn` (2.5): the node accepts it at once, prepares
  its own worktree or clone, launches, and removes what it prepared when a
  step before launch fails. `workspace.prepare`, `workspace.remove`,
  `node.inventory`, the spawn's environment map, and the hub's removal retry
  are gone. A node registers its checkout and posts run reports through two
  key-bound hub routes (1.4), and 2.6 promotes and settles runs and inserts
  workspace rows from those reports. The writer found that the hub's storage
  refuses another machine's session and checkout rows. gobby#15977 ruled on
  2026-10-12 that this plan adds the key-verified path for three uses only
  (Decision 18). The remote flow no longer calls `get_terminal_env_vars`, so
  the hub creates no directory for a node's run. The Architecture section adds
  a component diagram and three user stories.
- 2026-10-12: Repaired for Plan Adversary gobby#16491's HM-F14 to HM-F18 on
  base 991a6afb96. Orchestrator gobby#15977 ruled on HM-F15 and HM-F16.
  - HM-F14: 1.4's report route records workspace rows from live entries and
    launched exits before it answers 200, so an agent that launches and exits
    between passes, or a hub restart after the 200, loses no row. 1.4 now
    depends on 2.1, and 2.6 no longer inserts rows.
  - HM-F15: each hub process mints a fresh random `hub_epoch` that is never
    reused, and the node sends one fresh report at a time with a durable
    revision. A stale epoch or a revision at or below the high-water gets 409
    `stale_report` before any write. The inbox keeps each machine's published
    snapshot revision across `take`, so an older admitted report that
    finishes after a `take` merges its exits and never installs its snapshot
    (1.4.5 pins that schedule). No migration.
  - HM-F16: Josh's ruling sets the ceiling to the existing
    `max_active_agents`, applied per remote machine in 2.5, with `le=1000`
    so a full node fits one complete report (Decision 19). The writer found
    that a remote session end exits the hub's terminal row before the node's
    process exits, so the node also refuses a spawn at 1,000 accepted and
    live runs.
  - HM-F17: a key-authenticated hook must name the key's machine, and a hook
    from another machine binds only that machine's session rows at every hook
    lookup (2.2, Decision 16). The writer's sweep added the web-chat,
    compact-continuation, and session-start identity lookups.
  - HM-F18: the node keeps a durable preparation record and retries removal
    until the path is gone.
- 2026-10-13: Consensus on the HM-F14 to HM-F18 repair. Plan Adversary
  gobby#16491 verified 41f2ac4a0f and Plan Writer gobby#16490 agrees that
  HM-F14 to HM-F18 and 2.5's explicit `DaemonConfig` consumer inventory are
  resolved, including the published revision kept across `take` and the
  intervening-take schedule in 1.4.5. Standard validation is warning-free,
  and no blocking finding remains.
- 2026-10-13: Cross-plan amendment from node-halves Plan Writer gobby#16767
  for Josh's "No deferrals" ruling (node-halves Decision 23), relayed by
  gobby#15977. A resume routes to the resumed session's machine and is no
  longer refused: 2.5 resolves the session, rebinds it to a successor run on
  the node through `prepare_terminal_resume`, and builds `agent.spawn` with
  node-halves 3.2's `build_spawn_payload`, which names the session's earlier
  workspace. Decision 13, the hub-flow code list and 2.5.7 change to match,
  and the hub's agy gate stays on the local path. The M1 manifest below
  predates this amendment and must be re-derived.
- 2026-10-13: Plan Writer gobby#16490 withdrew the M1 manifest that
  0dc6b3bae4 applied, stale against 6415e4b926, by the memory f5577ae0
  route, at gobby#15977's direction. Plan Adversary gobby#16491 re-derives
  M1 from this narrative.
- 2026-10-13: 2.5 states that a spawn registry built with no session manager
  counts a resume reference as unresolved and takes the local path, and
  corrects the `test_spawn_agent_resume.py` no-edit reason to match
  (gobby#15977's finding for the 2.5 leaf).
- 2026-10-14: Planned the four deferred sections in scope under Josh's "No
  deferrals" ruling (memory 878d0c35), relayed by gobby#15977, which also
  upheld Plan Adversary gobby#16491's HM-F19 to HM-F21. D2 is 1.2 again,
  with the #24268 review's admission and malformed-input requirements. D1
  and D3 are P4: a node merges and removes its own workspaces (Decision
  20), and the hub asks it for its checkout's git state on demand
  (Decision 21). D4 is P5's live proof with isolated daemons and fake
  providers (Decision 22). 2.5 names the resolvers the remote flow runs
  and stops verifying the hub's SRT install for a node run (2.5.15). 1.4
  moves `updated_at` on each reported workspace for node-halves 3.2
  (1.4.7, node-halves adversary finding NH-12, requested by gobby#16767).
  Constraints lists the cross-plan edges with node-halves. The M1 manifest
  is re-derived from this narrative.

## V2: Verification
`kind: verification`

These are completion gates for the implementation. Except for plan validation,
none has run yet. Each must pass after its leaves land and before #21575 closes.

```bash
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/hub/ tests/agents/test_hub_local_loops.py tests/servers/test_node_routes.py tests/servers/test_hook_machine_binding.py tests/servers/test_attention_roster_machines.py tests/servers/test_attention_remote_respond.py tests/servers/test_remote_workspace_routes.py tests/servers/test_machine_scoped_projects.py -v
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/e2e/test_hub_node_live.py -v -m e2e
cargo nextest run -p gobby-daemon -E 'binary(node_command_corpus) + binary(node_command_parity) + binary(node_internal_route) + binary(node_workspace_finish) + binary(node_checkout_status)'
cargo nextest run -p gobby-client
uv run ruff format --check src/ && uv run ruff check src/ && uv run mypy src/
uv run gobby plans validate .gobby/plans/hub-mode.md -p .
```

- The plan validation command must pass at every revision; it is the only check
  that runs during planning.
- P1 to P4 drive a fake route or a fake node; 5.1 runs a real isolated hub
  and node.
- Run these from the worktree root. Do not run the full pytest suite.
