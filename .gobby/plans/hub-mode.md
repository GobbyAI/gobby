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

Four sections are deferred. D1 holds finishing a remote workspace: merging
and removing it. D2 builds the gdaemon route once #24090 (Node channel) lands. D3
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
   - Python callers for spawn, cancel, and terminal write. A spawn names its
     worktree or clone, and the node prepares it (Josh's review of the stamped
     plan, Decision 13).

   The envelope and the gdaemon route are durable, and #24149 reuses them. The
   Python callers live in the `src/gobby/hub/` package and are transitional.
3. **Q2 a. Eligibility.** A machine is eligible for a project when a
   `project_checkouts` row exists for (project, machine) and the gdaemon registry
   reports its channel live, read through the internal route (1.1 contract, D2).
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
    cron, and feedback spawns stay local (decision 11). A remote spawn refuses
    `resume` and placement with typed errors: resume needs the node's provider
    thread files, and placement is a gclient-local pane feature.
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
      launched agent, and the hub sends no workspace command in v1.
    - Finishing a remote workspace, merging and removing it, is D1.
16. **Hook machine binding.** Q3 trusts `sessions.machine_id`, which comes from
    the hook envelope (`ClaudeCodeAdapter` reads `input_data["machine_id"]`).
    When a hook request carries a verified front-door identity, the hub rejects
    an envelope whose `machine_id` is missing or differs from the key's machine
    (2.2). A hook from another machine then binds only that machine's session
    rows: every hook lookup that matches a row by an id the event supplies
    treats another machine's row as a miss, before any write (2.2). A local
    hook binds any row, as today. The envelope machine is the marker because it
    reaches every adapter's event and every replayed envelope.
17. **#24090 dependency.** The gdaemon route extends the node channel's registry
    and handler, which #24090 (Node channel: hub registers, heartbeats, replaces,
    revokes) creates. Per the plan-coverage contract, work gated on another task
    is a typed deferral, so the route is deferred section D2 (Orchestrator ruling,
    2026-10-12). 1.1 specifies the route's HTTP contract, so 1.3 builds its client
    against it and tests it against a fake route. Until D2 lands, the client
    refuses every remote request with `node_routing_unavailable`, and no active
    section claims a live hub-to-node path. D4 holds the live proof. 1.1 adds the
    S4.2 ← #24090 edge to `ROADMAP.md`.
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
  route (D2), which sends a command frame over the node's channel (#24090)
  and returns the node's reply. The v1 commands are `agent.spawn`,
  `agent.cancel`, and `terminal.write`.
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
  PY -- "internal route (D2): agent.spawn, agent.cancel, terminal.write" --> FD
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
  Note over PY: the inbox keeps N's latest report
  PY->>DB: next health pass inserts the worktree row and promotes the terminal
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
    `not_stopped`, `spawn_in_doubt`, `remote_resume_unsupported`, and
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
    - Otherwise it inserts an `active` `worktrees` row (mode `worktree`) or
      `clones` row (mode `clone`) with a new id, the terminal's project and
      machine, the run's task, the run's child session as
      `agent_session_id`, and the entry's path, `branch_name`, and
      `base_branch`, with `ON CONFLICT DO NOTHING`. It sets no task artifact.
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
- `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py::create_spawn_agent_registry`
- `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py::_parent_session_project_context`
- `src/gobby/servers/routes/agent_spawn.py::AgentSpawnRequest`
- `src/gobby/servers/routes/agent_spawn.py::create_agent_spawn_router`
- `src/gobby/agents/spawn.py::prepare_terminal_spawn`
- `src/gobby/agents/spawn.py::_prepare_run_for_session`
- `src/gobby/agents/session.py::ChildSessionConfig`
- `src/gobby/agents/session.py::ChildSessionManager.create_child_session`
- `src/gobby/storage/sessions/_crud.py::_SessionCRUDMixin.register`
- `src/gobby/storage/agents/_lifecycle.py::_AgentRunLifecycleMixin.create`
- `src/gobby/config/app.py::DaemonConfig`
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: the derived carrier is regenerated from the registry and its bytes stay the same, because the generator emits no bounds
- `tests/hub/test_remote_spawn.py`
- `tests/config/test_runtime_config_contract.py::test_spawn_cap_default_and_minimum`

**Granularity:** fourteen acceptance items and eight production files, one
outcome: every remote spawn ends in one owned state, accepted, failed, or in
doubt. The items fall into three groups. Routing covers 2.5.3, 2.5.6, 2.5.8,
and 2.5.9. The launch covers 2.5.1, 2.5.2, 2.5.7, 2.5.10, 2.5.11, 2.5.12,
2.5.13, and 2.5.14. Settlement of its outcome covers 2.5.4 and 2.5.5. None
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
  only source of a routed machine. It refuses `resume_session_id` with
  `remote_resume_unsupported` and a placement request with
  `remote_placement_unsupported`. Otherwise:
  1. It runs the local flow's agent-body, provider, model, and depth checks,
     the `runner.can_spawn` gate, and `TaskSpawnLease` admission. The
     callers' per-project `reserve_agent_slot` still applies, so a remote
     spawn passes both the project cap and the machine ceiling.
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
  4. It sends one `agent.spawn` (1.1): the run, session, parent, project, and
     terminal ids, the child's `agent_depth`, `max_agent_depth`,
     `workflow_name`, `provider`, `model`, `provider_args`,
     `reasoning_effort`, `sandbox`, `timeout_seconds`, `prompt`, `title`,
     `source_root=routed.checkout_root`, and `workspace`:
     - `mode` is the request's isolation, else the agent body's, else
       `none`.
     - For `worktree` or `clone`, `branch_name` is the request's, else
       `agent/<run_id>`. `base_branch` is the request's, else the agent
       body's. `inherit` or no value sends `null`, and the node uses
       `source_root`'s current branch (1.1).
  5. `{accepted: true}` returns the run id, child session id, and terminal id
     with status `pending`. The hub records nothing more here. 2.6 promotes
     the run from the node's report.
  6. An error with `stage: "none"` is a definite failure. It fails the
     pending terminal (`fail_pending`) and settles the run with
     `_roll_back_run`, which also ends the lease, and returns the error's
     code. A hub restart between those calls leaves a pending terminal for
     2.6's fence or an active run for 2.6's run sweep.
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
- `tests/agents/test_spawn.py` — no-edit-reason: it patches `_prepare_run_for_session` or calls it by keyword without `routed_machine_id`, so its runs keep this machine and their environment.
- `tests/agents/test_spawn_resume.py` — no-edit-reason: it patches `_prepare_run_for_session`; `prepare_terminal_resume` keeps calling it without `routed_machine_id`.
- `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py` — no-edit-reason: its local flow passes no `routed_machine_id` to `prepare_terminal_spawn`, so its sessions keep the local guard and its runs keep this machine.
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
- `tests/mcp_proxy/tools/spawn_agent/test_spawn_worktree_reference_resolution.py` — no-edit-reason: its spawns omit `machine` and take the unchanged local path.
- `tests/mcp_proxy/tools/tasks/test_lifecycle_close_orchestration.py` — no-edit-reason: its spawns omit `machine` and take the unchanged local path.
- `tests/mcp_proxy/tools/test_parallel_dispatch.py` — no-edit-reason: its spawns omit `machine` and take the unchanged local path.
- `tests/mcp_proxy/tools/test_spawn_agent_resume.py` — no-edit-reason: its resumes omit `machine` and take the unchanged local path.
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
- `tests/agents/test_resume_executor.py` — no-edit-reason: it never sets `DaemonConfig.max_active_agents`, so `le=1000` changes nothing it uses.
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
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/hub/test_remote_spawn.py tests/mcp_proxy/tools/spawn_agent/ tests/servers/routes/test_agent_spawn_routes.py tests/agents/test_backend_ingress.py tests/agents/test_spawn.py tests/agents/test_spawn_resume.py tests/agents/test_agents_session.py tests/sessions/test_sessions_manager.py tests/agents/test_runbook_seat_titles.py tests/integration/test_terminal_mode_worktrees.py tests/storage/agents/test_active_run_scope.py tests/storage/test_machine_scope_writers.py tests/config/test_runtime_config_contract.py -v`.

**Acceptance:**

- 2.5.1 - Through a fake node client, a spawn from a session on another machine sends exactly one command, `agent.spawn`, to that machine, and its session, run, and terminal rows carry that `machine_id` before it is sent; `{accepted: true}` returns the run id with status `pending`. test: `tests/hub/test_remote_spawn.py::test_spawn_routes_to_requesting_machine`.
- 2.5.2 - A remote spawn issues no managed credential, writes no prompt file even for a prompt over `MAX_ENV_PROMPT_LENGTH`, never calls `get_terminal_env_vars`, and creates no Cargo target or spawn-cache directory on the hub. test: `tests/hub/test_remote_spawn.py::test_remote_spawn_touches_no_hub_disk`.
- 2.5.3 - An explicit `machine` is validated, and an ineligible one fails with its typed code without spawning anywhere. test: `tests/hub/test_remote_spawn.py::test_explicit_machine_is_validated`.
- 2.5.4 - A `stage: "none"` failure of `agent.spawn`, including `node_offline` and `node_routing_unavailable`, fails the pending terminal, cancels the run, and ends the lease, with no local process or pane call and no further command. test: `tests/hub/test_remote_spawn.py::test_definite_failure_settles_in_database`.
- 2.5.5 - A `node_timeout` or a `node_reply_lost` on `agent.spawn` returns `spawn_in_doubt` and leaves the run and terminal pending and the lease attached, with no rollback. test: `tests/hub/test_remote_spawn.py::test_in_doubt_spawn_keeps_state`.
- 2.5.6 - A node caller whose parent is on the hub spawns on the node, and a hub caller whose parent is on a node spawns locally in the hub's checkout. test: `tests/hub/test_remote_spawn.py::test_origin_is_the_calling_session`.
- 2.5.7 - Remote resume and placement are refused with typed codes. test: `tests/hub/test_remote_spawn.py::test_unsupported_routes_refused`.
- 2.5.8 - A spawn whose target is this machine takes the local path unchanged. test: `tests/hub/test_remote_spawn.py::test_local_target_unchanged`.
- 2.5.9 - With a checkout on the target and none on the hub, both the MCP tool and `POST /api/agents/spawn` spawn on the target. test: `tests/hub/test_remote_spawn.py::test_remote_target_needs_no_hub_checkout`.
- 2.5.10 - `register` for another machine without `routed_machine_id`, and `register_session` for another machine, raise `MachineOwnershipMismatchError`; a `routed_machine_id` that differs from `machine_id` raises it too; a routed registration for an unenrolled machine raises `MachineNotRegisteredError`; and none writes a row. test: `tests/hub/test_remote_spawn.py::test_foreign_session_needs_routed_machine`.
- 2.5.11 - An isolated remote spawn sends `workspace` with the requested mode, `branch_name` `agent/<run_id>` unless the request names a branch, and `base_branch` `null` for `inherit` or no value; the hub writes no `worktrees` or `clones` row and sets no task artifact. test: `tests/hub/test_remote_spawn.py::test_workspace_intent_names_run_branch`.
- 2.5.12 - With the target machine's pending, live, and orphaned terminal rows at `configured_max_active_agents()`, a remote spawn returns `machine_cap_reached` with `stage: "none"`, writes no session, run, or terminal row, sends nothing, and frees its lease; two concurrent spawns to a machine one below the ceiling admit exactly one; a spawn to another machine and a local spawn are unaffected. test: `tests/hub/test_remote_spawn.py::test_machine_ceiling_refuses_before_rows`.
- 2.5.13 - `DaemonConfig(max_active_agents=1000)` validates, `DaemonConfig(max_active_agents=1001)` raises `ValidationError`, and the default stays 20. test: `tests/config/test_runtime_config_contract.py::test_spawn_cap_default_and_minimum`.
- 2.5.14 - The checked-in runtime config contract still equals the generator's output after the bound. test: `tests/config/test_runtime_config_contract.py::test_checked_in_contract_matches_registry`.

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
  reports wait on D2 and #24148.

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

## D1 Remote workspace lifecycle (depends: 1.4, 2.6)
`kind: deferred`

A remote workspace's hub row is a record that 1.4's report route inserts from
the node's report before it answers 200 (Decision 15). The node removes a
workspace only when its spawn
fails before launch, so a finished remote workspace stays on the node, and its
row stays `active` with its `agent_session_id`. No existing path finishes it:
- `LocalWorktreeManager` and `LocalCloneManager` read and write only this
  machine's rows, which keeps the hub's loops off node rows (2.1.5). So
  `list_worktrees`, `delete_worktree`, expiry cleanup, and session-end release
  never act on a node's row.
- Merge and expiry cleanup run git in this machine's checkout. A node's branch
  exists only on the node, and no v1 command moves its commits or merges
  there.
- Local cleanup removes a worktree after its merge, so removal follows the
  merge design. A node command that removes a workspace returns with that
  design, and the hub deletes the row when the node confirms.

Acceptance item D1.1: the hub lists, merges, and removes finished remote
workspaces through their owning node.

```yaml
deferral:
  task_ref: "TBD-remote-workspace-lifecycle"
  reason: "needs-planning: merging a node's branch needs its commits moved between machines or a node-side merge, which no v1 command or design covers; listing and removal, including a node removal command, follow that design. Created at expansion per the plan-coverage contract."
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

Every active section drives a fake route, a fake node, or reports fed to the
inbox. The live path needs D2's route, #24148's node handlers (spawn,
checkout registration, and reports), and #24150's hook forwarding.

Original acceptance:
- D4.1: a node registers its checkout through `POST /api/nodes/checkouts`; a spawn from a node session runs the agent on that node; the node's report promotes its run live and records its workspace row; and its hooks start and end its run on the hub.
- D4.2: a cancel, a terminal write, and an attention response for that agent reach its node.

```yaml
deferral:
  task_ref: "TBD-hub-node-live-proof"
  reason: "Needs D2, #24148 (node-local spawn, checkout registration, and reports) and #24150 (node hook ledger), none landed; an external prerequisite is a typed deferral per the plan-coverage contract."
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

## V2: Verification
`kind: verification`

These are completion gates for the implementation. Except for plan validation,
none has run yet. Each must pass after its leaves land and before #21575 closes.

```bash
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/hub/ tests/agents/test_hub_local_loops.py tests/servers/test_node_routes.py tests/servers/test_hook_machine_binding.py tests/servers/test_attention_roster_machines.py tests/servers/test_attention_remote_respond.py -v
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
