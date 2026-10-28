Plan artifact: `.gobby/plans/agents-family.md`

# Gobby 1.0 Stage 2: native agents, dispatch, worktrees and clones family

**Plan ID:** agents-family

## Overview
`kind: framing`

Port the agents, dispatch and worktrees family (epic #21564) from Python
ownership to native Rust services with machine scoping. The bounded
inventory is 124 proxied ledger rows: the 120 whose notes name #21564 after
the re-homes below, which this plan ports, and four node rows whose ownership
the Orchestrator assigned here without a port (Decision 14):

- `docs/contracts/parity-ledger-status.d/sessions-agents.json` holds 63:
  28 `MCP gobby-agents:*` tools, 21 `HTTP ... /api/agents/*` routes and 14
  `CLI gobby agents ...` commands.
- `docs/contracts/parity-ledger-status.d/git-workspaces.json` holds 57: 17
  `MCP gobby-worktrees:*` tools, 13 `MCP gobby-clones:*` tools, nine `HTTP
  ... /api/source-control/{worktrees,clones}...` routes, 11 `CLI gobby
  worktrees ...` commands and seven `CLI gobby clones ...` commands.
- `docs/contracts/parity-ledger-status.d/comms-cron-hub-nodes.json` holds 4:
  `HTTP POST /api/nodes/report`, `HTTP POST /api/nodes/runs/terminate`,
  `HTTP POST /api/nodes/runs/prompt-delivery-failure` and `HTTP POST
  /api/nodes/runs/code-index-warning`, whose notes name #24151 today.

The rendered `docs/contracts/parity-ledger.md` is generated. This plan does
not expand work or authorize a restart, cutover or push.

The work is necessary: all 120 ported rows are proxied, and gdaemon has no
`/api/agents` or `/api/source-control/{worktrees,clones}` handler and no
`gobby-agents`, `gobby-worktrees` or `gobby-clones` tool family. Native
pieces already exist and are reused: the hub terminal host
(`gobby_terminals::family::TerminalFamily`, started by
`crates/gdaemon/src/state_services.rs`), the terminal write path with its
wake injection (`crates/gterminals/src/write/wake_dispatch.rs::dispatch_wake`),
the node command channel
(`crates/gdaemon/src/nodes/registry.rs::Registry::command_sender` with the
frames in `crates/gdaemon/src/node_commands.rs`), node-side workspace and
sandbox launch code under `crates/gdaemon/src/nodes/agents/`, and the pane
detectors in `crates/gdaemon/src/pane_detection/`. Restraint stops at one
library crate, `crates/gagents` (package `gobby-agents`, lib
`gobby_agents`), statically linked into gdaemon; two `RouteFamily`
instances; three `ToolFamily` descriptors; one machine-effects seam that
gdaemon implements; and the existing tables. No new process, executable,
database table or second terminal host is added.

Two route families give the surface groups independent activation, each
with its own `front_door.routes.<name>` key and a default of Proxy:

- `agents` claims `/api/agents`: 21 HTTP rows.
- `checkouts` claims exactly `/api/source-control/worktrees` and
  `/api/source-control/clones`: nine HTTP rows.
  `crates/gdaemon/src/front_door/routes.rs::RouteTable::lookup` picks the
  longest matching prefix, so `/api/source-control/branches`, `/diff` and
  `/status` (#21584) never resolve to `checkouts`.

No family claims `/api/nodes/report` or `/api/nodes/runs`; those four rows
stay Python-served (Decision 14).

The 58 MCP rows gain native descriptors and handlers whose schema and result
parity is proven here; they stay proxied until #21570 flips the internal MCP
registries (Decision 4). The 32 CLI rows move to native `gdaemon agents`,
`gdaemon worktrees` and `gdaemon clones` commands, and the public `gobby`
groups forward to them one subcommand per verb (Decision 5). Activation order
is `checkouts` (11.1), which publishes its own ledger rows, then `agents`
(11.2), which also moves the hub pane steps; 11.3 records the remaining
ledger result. Two time-boxed loopback Python routes in 2.0 let native code
read the hub's in-memory reported pane store until #25269 ports it, and hand
native terminal transitions to Python's completion registry until D1 ports
completion delivery (Decisions 10 and 15). 11.2 also moves coordination-wait
delivery from Python to gdaemon (Decision 9).

**Re-homed and excluded rows.** Three rows that name #21564 belong to other
owners. The Orchestrator's AF-Q1 ruling (2026-10-28) moves `WS message
workspace_attach`, `WS message workspace_op` and `WS message
workspace_snapshot` (`docs/contracts/parity-ledger-status.d/websocket-chat-voice-ui.json`,
served by `src/gobby/servers/websocket/workspace_ws.py::WorkspaceWsMixin`
over `src/gobby/terminals/workspace_ops.py::WorkspaceOps`) to #21565, which
owns the 24 `gobby-workspaces` rows over the same operations; 11.3 rewrites
those three notes to cite #21565 alone. The four `/api/attention/*` rows
belong to #24149, and `CLI gobby nodes` and `CLI gobby nodes list` to
#21571; their notes do not change here.

**Boundaries.** The hub half of hub/node operation (#24149) owns
`attention_states` transitions, ordering, metadata and event publishing as a
native service that Python writers reach through a loopback route; this
family's pane monitor writes attention through it. Node
halves (#24148) own node-side launch (#24375), the node pane monitor
(#24383) and the node command contract; this plan reuses both and adds the
hub-local case. Hook ingress (#24151) keeps `GET
/api/nodes/detection-manifests`; the node report and run-outcome rows and
the reported pane store are owned here and stay Python-served (Decision 14). WS chat (#21592) owns the native web-chat
runtime and supplies the web-chat wake seam. Merge and source-control
status (#21584) own `gobby-merge` and the other `/api/source-control`
routes. Future messaging channels (#24173) extend the mailbox under their own
plan. The internal-MCP plan (#24841: #24883 descriptor type, #24884 gateway,
#21570 registry flip, #24885 compatibility backend) owns MCP transport.

**Non-goals.** The rest of
`src/gobby/agents/lifecycle_monitor.py::AgentLifecycleMonitor` stays Python:
pending-termination reconciliation, stale-pending reaping, daemon-stop orphan
reaping, terminal-run session expiry, health and memory watchdogs, the idle
decision, completed-task and autonomous-stuck sweeps, dispatch-mutex refresh
and stale-run sweeps (AF-Q2). So do `src/gobby/agents/interactive_attention_monitor.py`,
the transcript watchdog and every Python module this plan does not name as a
Target.

**Cross-plan contention.** `.gobby/plans/gdaemon-skills-family.md` leaf 5.1
and `.gobby/plans/gdaemon-ai-providers-family.md` leaf 7.5 also edit
`crates/gdaemon/src/main.rs::Command` and `::main`, which 10.1 edits, and
both plans push families in `crates/gdaemon/src/serve.rs`, which 1.1, 2.2,
11.1 and 11.2 edit. The landings serialize, and each later one rebases onto
the earlier. Under the same ruling as Decision 14, the S2.11 hub hooks plan
(`.gobby/plans/s2-11-hub-hooks-rules.md`) drops `/api/nodes/report` and
`/api/nodes/runs` from its `hooks` family, so no native family claims them.

## Decision Record
`kind: framing`

1. The Orchestrator assigned planning task #25255 under epic #21564. Plan
   Writer gobby#18292, Plan Enhancer gobby#18298 and Plan Adversary
   gobby#18295 run the team-planning runbook; Triage gobby#17480 is the
   coordination route to the Orchestrator gobby#17281.
2. Scope rulings, relayed by Triage on 2026-10-28. From the #24149 writer's
   questions: Q1(a) drops the dispatcher reading, so spawn eligibility and
   remote cancel move with their only consumers (`spawn_agent`, kill and
   stop) as rows owned here; Q3(a) gives Rust the attention store under
   #24149 and moves retirement of the Python pane monitor with this family;
   Q6 assigns the four attention rows to #24149 and the two nodes CLI rows
   to #21571. From this plan's questions: AF-Q1(a) re-homes the three
   workspace WS rows to #21565; AF-Q2(a) retires exactly the pane steps for
   hub-local runs and leaves the rest of the lifecycle monitor in Python;
   AF-Q3(a) ports the inter-session mailbox and the coordination-wait
   contract unchanged; AF-Q4 keeps all 23 clone rows. The clone CLI route
   failure is dropped through exact waivers (Debt not to port). Later
   rulings assign ownership of the node report and run-outcome rows here
   without a port (Decision 14) and keep remote live output Python-served
   (Decision 12).
3. One crate, `crates/gagents`, holds every family service: definitions,
   runs, checkouts (worktrees and clones), spawn admission and execution,
   termination, mailbox, wake decisions, coordination waits, seats, the MCP
   adapters and the CLI trees. Worktrees and clones share machine ownership,
   git effects and the node `workspace.*` and `checkout.*` commands with
   spawn, so a second crate would split one ownership model. Two route
   families keep the HTTP groups independently activatable.
4. MCP rows. The family publishes three descriptors through #24883's
   `crates/gcore/src/mcp.rs::ToolFamily` and proves catalog, schema and
   result parity for 57 tools (9.1 to 9.3); `wait_for_agent`'s result
   parity waits for D1 (Decision 15). Registering them with an
   active backend is #21570's registry flip; until then the rows stay
   `proxied` with the note `Registry flip: #21570`. No leaf here waits on
   #21570, #24884 or #24885.
5. CLI rows. Native `gdaemon agents|worktrees|clones` command trees (10.1 to
   10.3) carry the behavior. The public `gobby agents`, `gobby worktrees` and
   `gobby clones` stay thin Click groups with one forwarding subcommand per
   verb, so `scripts/generate_parity_ledger.py::cli_verbs` keeps every
   `CLI gobby ...` key (Orchestrator ruling of 2026-10-28, Skills #25006
   criterion 5.4.3). A single forwarding command would drop the group's
   children from the ledger.
6. Machine effects. `crates/gagents/src/effects.rs` defines
   `MachineEffects`: pane read, terminal write, spawn and cancel for any
   run, the completion handoff after a terminal transition (Decision 15),
   and workspace merge and remove and checkout status and read for a
   checkout on another machine. gdaemon implements it in
   `crates/gdaemon/src/agent_effects.rs`. On the hub's own machine it uses
   hub launch (4.1) and the hub terminal host; another machine receives the
   matching node command through `Registry::command_sender` and
   `CommandSender::request`, with the routing and hub-flow codes of
   `docs/contracts/node-commands.md` (v4). A pane on another machine is read
   from the hub's reported pane store through 2.0's pane route (Decision 12). The trait exists because gdaemon
   depends on gagents, and these effects need gdaemon's node registry,
   launch code and terminal host; tests use doubles. Worktree and clone git
   operations for the hub's own machine run in gagents with the `git`
   executable, as Python's hub runs them; node-side handlers are unchanged.
7. Hub-local launch reuses node launch. #24375 creates
   `crates/gdaemon/src/nodes/agents/launch.rs` with provider command,
   environment, trust and the SRT chokepoint over the node's `agents.json`
   state. 4.1 parameterizes that launch over a run-state store with two
   implementations: the node's `AgentState` and the hub's Postgres
   `agent_runs` rows. No launch code is copied.
8. Pane steps (AF-Q2). For runs on the hub's own machine, the native hub
   pane monitor (8.1) owns the eight steps that read a pane and act on it:
   trust, loop, approval and queued-continuation prompts, periodic Enter,
   attention classification, initialization timeout and provider stalls. It
   is #24383's node pass run over hub-local runs, with terminal writes to the
   hub terminal host, terminate decisions through 6.1 and attention through
   #24149. The idle step stays Python for every machine:
   `src/gobby/agents/idle_check_handler.py::IdleCheckHandler.check_idle_agents`
   is the hub-side decision (reprompt, fail, recovery, coordination holds),
   which node-halves Decision 4 also keeps on the hub for remote runs, and it
   reads the local pane as that decision's input. Its local branch stops
   syncing attention, which 8.1 then owns. 11.2 removes the eight steps from
   the Python `_check_loop` in the same shipping commit that starts the
   native monitor. The Python step implementations stay unscheduled as the
   corpus's baseline until global Python removal.
9. Messaging (AF-Q3). The native mailbox ports
   `src/gobby/sessions/mailbox.py::MailboxService.send` and the
   `inter_session_messages` contract unchanged. The native wake decision
   ports `src/gobby/events/wake.py::WakeDispatcher` for wakes the native
   mailbox and coordination waits produce. It injects through the terminal
   write path locally and `terminal.write` remotely, and reaches web-chat
   sessions through the seam #21592 supplies. A terminal on another machine
   takes a live wake only when its reported composer is empty, as
   `WakeDispatcher._composer_blocks_wake` decides; the native decision reads
   that composer state through 2.0's pane route. Python's dispatcher keeps
   serving Python callers. Both read the same durable session and mailbox
   state, so eligibility agrees. Coordination-wait delivery moves to gdaemon
   (D-AF-6, Orchestrator ruling relayed by Triage on 2026-10-28): 5.3's
   worker listens on `gobby_coordination_wait` and wakes waiters through this
   decision, and 11.2 stops starting Python's
   `src/gobby/events/coordination_waits.py::CoordinationWaitService` in the
   same shipping commit, so one owner delivers each wait. Registration and
   resolution stay in the shared rows and schema triggers, so a wait that
   Python's proxied `wait_for_coordination` registers is delivered natively
   too.
10. Python modules stay as the proxy backend. Route and tool modules, storage
    managers and the remote-routing helpers in `src/gobby/hub/` are unchanged
    until global Python removal, as in the AI providers plan. Only 2.0 (the
    loopback bridge routes), 10.4 (CLI forwarding) and 11.2 (pane-step
    scheduling, the idle path's local attention sync and the start of
    Python's coordination-wait service) edit Python. No Target names a Python
    production file of 850 lines or more except 11.2's typed `delete-lines`
    proof. 2.0's two routes are named, time-boxed exceptions (Orchestrator
    rulings relayed by Triage on 2026-10-28): the pane route (D-AF-5) retires
    when the node-runs port (#25269) gives the native side its own reported
    pane store, and the delivery route (D-AF-7) retires with native completion
    delivery (D1).
11. Admission. No `/api/agents` or `/api/source-control` route appears in
    `src/gobby/servers/auth_service.py::_AGENT_CAPABILITY_MATRIX` or
    `src/gobby/servers/grant_auth.py::_GRANT_ROUTES`, so every HTTP row
    admits API keys, the local operator and browser cookies only. 1.1
    declares that class per route and fails closed; 11.1 binds it to #25046's
    reusable native admission. MCP rows reach the tools through #24884's
    gateway after #21570.
12. Remote panes (D-AF-5). Python reads a pane on another machine from the
    hub's in-memory store of node-reported panes
    (`src/gobby/hub/remote_pane.py::RemotePaneStore`), never through a node
    command. `src/gobby/hub/remote_runtime.py::RemoteTerminalRuntime` serves
    the stored tail to live output, `wait_for_output` and capture before
    kill, `remote_composer_state` serves the composer to the wake gate, and a
    successful remote stop marks the entry stopped. The store lives in the
    Python process and stays there until #25269, so 2.0 adds one
    loopback-only internal Python route over it: read the tail and composer
    state, and mark an entry stopped. gdaemon's effects use it for every
    remote pane read; no node command is added. The two live-pane MCP rows,
    `get_agent_live_output` and `wait_for_output`, still stay proxied after
    #21570's flip until the node-runs port lands (Orchestrator ruling,
    2026-10-28). `get_agent_capture` reads the capture persisted in the run's
    result and never reads a pane.
13. Activation. 11.1 activates `checkouts` and 11.2 activates `agents`; each
    is a shipping commit with release bumps, promotion through
    `src/gobby/install/bin_set_coherence.py::promote_workspace_binary_set`
    and an Orchestrator-delegated cutover. 11.1 publishes the rows its
    cutover makes native; 11.3 records the rest of the ledger last.
14. Node report and run-outcome rows. The Orchestrator's ruling, relayed by
    Triage on 2026-10-28 (option A of the S2.11 hub plan's Decision R),
    assigns ownership of `HTTP POST /api/nodes/report`, the three `HTTP POST
    /api/nodes/runs/*` rows and the reported pane store to this family, with
    no native port here; #24151 keeps `GET /api/nodes/detection-manifests`.
    The routes stay Python-served until the reconciler and the idle check go
    native. Python's report route applies pane tails to its in-memory store
    and queues the report in an in-memory inbox that
    `src/gobby/hub/remote_reconcile.py::RemoteRunReconciler` drains; Python's
    idle check reads remote panes from that store, and AF-Q2 keeps both in
    Python. No family claims the two prefixes, and 11.3 records the four rows
    as owned here and still `proxied`. #25269 (Plan the node-runs native
    port, under #21542) owns the reconciler, remote idle and store port and
    the retirement of 2.0's pane route.
15. Agent completion delivery (D-AF-7, Orchestrator ruling relayed by Triage
    on 2026-10-28). `wait_for_agent`, pipelines and durable subscribers wait
    in Python's in-process
    `src/gobby/events/completion_registry.py::CompletionEventRegistry`, and no
    notify channel covers `agent_runs`. Python's own terminal producers (the
    idle check, the stale sweeps and remote run outcomes) stay through this
    plan (Decisions 8 and 14), so the registry stays the completion owner.
    Every native terminal transition, whether its parent delivery succeeded
    or failed, ends by handing the run id to gdaemon, which posts it to 2.0's
    delivery route after the commit; Python runs
    `src/gobby/agents/terminal_delivery.py::deliver_existing_terminal_run` on
    its own loop, as its own producers do. gdaemon keeps the run until the
    route acknowledges it. The route rereads the run's durable
    `completion_subscribers` rows after the delivery, and an acknowledgement
    is an answer with none left, because Python removes a row only when
    `src/gobby/events/completion_registry.py::wake_result_is_delivered`
    accepts the wake. A refused or unreachable route, a retained row or a
    failed reread keeps the run, and gdaemon posts it again every 5 seconds
    with no restart, for a remote run as for a hub-local one. The set of
    kept runs is in memory. A gdaemon restart always starts a fresh Python
    backend (`crates/gdaemon/src/lifecycle/backend.rs` hands each backend
    its secret at spawn), whose startup recovery
    (`src/gobby/runner_lifecycle_agents.py::_cleanup_terminal_agent_completion_subscribers`)
    redelivers the hub machine's runs. Another machine's run waits for its
    owning machine's recovery, as
    `.gobby/plans/completed/gdaemon-run-modes.md` ruled ("Per-run rows that
    carry a `machine_id` are recovered by the owning machine in every
    mode", acceptance 2.2.8) and as it does today when hub Python's single
    delivery of a remote cancel fails. `wait_for_agent` stays
    proxied after #21570's flip; its native port and native completion
    delivery are deferred to D1, owned under #21564 beside the run-lifecycle
    rows. No schema change.

## Debt not to port
`kind: framing`

Josh's debt-first ruling (memory `9066f1eb`) governs this plan. Keep all 120
public surfaces; parity matches their intentional contracts.

**KEEP**, with the reason each stays:

- All 23 clone surfaces (AF-Q4), although `docs/architecture/two-daemon-hub.md`
  records no task using clones. Clones are the isolation mode for work that
  needs a full repository.
- `gobby-agents:unregister_agent`. It is the documented reconcile path for a
  run whose process is gone.
- `gobby-agents:cancel_stale_helpers` with no age threshold. Its documented
  contract cancels every helper run of the caller that is not live.
- `gobby agents kill` without `--stop` leaves the run non-terminal. The CLI
  default is `stop=False` while the MCP default is `True`; both are
  documented.
- The worktree CLI's direct database reads (`list`, `show`, reference
  resolution) beside daemon-tool mutations through
  `/api/mcp/gobby-worktrees/tools/<tool>`. This is the current
  hub-authority behavior.
- HTTP spawn and MCP spawn share one implementation
  (`src/gobby/servers/routes/agent_spawn.py` wraps
  `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py::spawn_agent_impl`);
  the native routes share 4.3's service the same way.

**DROP**, each through an exact-surface #24963 waiver:

- **Clone CLI daemon route.** `src/gobby/cli/clones.py` posts `create`,
  `spawn`, `sync`, `merge` and `delete` to
  `{daemon_url}/mcp/gobby-clones/tools/<tool>` and
  `{daemon_url}/mcp/gobby-agents/tools/spawn_agent`. Python registers only
  the exact `Route("/mcp")`
  (`src/gobby/servers/app_factory.py::_register_mcp_http_route`), and
  gdaemon has no `/mcp/...` handler, so each command gets HTTP 404 and exits
  1. The evidence is code reading; no live run reproduced it. The native
  commands call `/api/mcp/gobby-clones/tools/<tool>` and
  `/api/mcp/gobby-agents/tools/spawn_agent`, as the worktree and agents CLIs
  do. Owner 10.3.

**Waiver inventory:**

| ID | Exact ledger surface | Owner |
| --- | --- | --- |
| `agents.cli-clones-create.daemon-tool-route` | `CLI gobby clones create` | 10.3 |
| `agents.cli-clones-spawn.daemon-tool-route` | `CLI gobby clones spawn` | 10.3 |
| `agents.cli-clones-sync.daemon-tool-route` | `CLI gobby clones sync` | 10.3 |
| `agents.cli-clones-merge.daemon-tool-route` | `CLI gobby clones merge` | 10.3 |
| `agents.cli-clones-delete.daemon-tool-route` | `CLI gobby clones delete` | 10.3 |

The owner adds these entries to `docs/contracts/parity-waivers.json` and
depends on #24963, which owns that file's schema, loader and consumers. Entry
fields are `id`, `surface`, `behaviour`, `decision` and `replacement`, all
required. IDs use `<family>.<surface-slug>.<behaviour-slug>`. Each `surface`
is exactly the key above. Each `decision` is
`.gobby/plans/agents-family.md#10.3`. Corpus vectors keep the Python 404
outcome and assert the native success outcome.

**S1 shared readiness:** ledger generation, corpus replay and compare consume
the one #24963 manifest. A changed behavior reports `Waived` with its ID,
decision and passing native assertions, never a mask or `Skipped`. At least
one ordinary `Passed` case is required. Zero-Passed, all-Skipped, unwaived
Skipped, unknown or stale IDs, missing decisions and unmatched differences
fail readiness.

**S2 shared error owner:** #24970 supplies the single seven-class native
classification (validation, missing, forbidden, conflict, capacity,
unavailable, internal) and the HTTP, MCP and CLI adapters. This family maps
its domain errors onto those classes and declares its existing envelopes and
codes, including the node routing codes `ambiguous_reference`,
`checkout_unresolved`, `machine_not_found` and `machine_not_owned` and the
hub-flow codes `machine_cap_reached`, `remote_loopback_endpoint`,
`spawn_in_doubt` and `not_stopped`, through those adapters. It adds no
family-local error framework.

**S3 compare safety:** compare mode runs Python and native side by side only
for read rows. Rows that spawn or stop agents, write terminals, send
messages, create, merge, sync or delete worktrees and clones, or mutate
definitions are proven against isolated stubs, temporary git repositories and
cloned database state, never by running the effect twice in production.

## Constraints
`kind: framing`

- Observed research baseline: HEAD `caec6f41053757484d2588b8413dee915df740e8`.
  Existing files and symbols were inspected with `gcode outline`, `gcode
  symbol-at` and bounded literal searches. Paths under `crates/gagents`,
  `crates/gdaemon/src/agent_effects.rs`, `crates/gdaemon/src/hub_launch.rs`,
  `crates/gdaemon/src/hub_pane_monitor.rs`, the new test files and proposed
  test symbols are new. `crates/gdaemon/src/nodes/agents/launch.rs` and
  `crates/gdaemon/src/nodes/agents/pane_monitor.rs` are created by #24375 and
  #24383 before the leaves that edit them.
- Platforms are macOS and Linux only. No acceptance item sets a latency
  gate.
- Placement follows Decision 3. `crates/gdaemon/src/serve.rs` builds
  `families` from `FAMILIES` and pushes `RuntimeFamily` inside `if let
  Some(pool) = &pool`; 1.1 adds the two families there through one
  `gobby_agents` call, so serve.rs (818 lines at baseline) gains a few lines.
  `crates/gdaemon/src/node_commands.rs` (979 lines) and
  `crates/gdaemon/src/nodes/agents/sandbox/paths.rs` (875 lines) are used,
  never edited.
- The code leaves form one dependency chain: 1.1 through 7.1, 9.1 to 9.3,
  10.1 to 10.3, 11.1, 10.4, 11.2 and 11.3. Every shared file
  (`crates/gagents/src/lib.rs`, `crates/gagents/Cargo.toml`, `Cargo.lock`,
  `crates/gdaemon/Cargo.toml`, `crates/gdaemon/src/serve.rs`,
  `crates/gdaemon/src/main.rs`, `crates/gdaemon/src/lib.rs`,
  `crates/gdaemon/src/agent_effects.rs` and its test file,
  `src/gobby/install/version_pins.py`, `docs/contracts/agents-native.md`,
  the parity shards and `docs/contracts/parity-ledger.md`) has one ordered
  owner sequence. 2.0 has no internal dependency and joins the chain at 2.2,
  the first consumer of its route; it edits the node-rows shard and the
  ledger before 11.1 and 11.3. 8.1 branches off the chain after 6.1, whose
  termination service it calls, and edits `crates/gdaemon/src/lib.rs` after
  its last chain owner, 4.1; 11.2 joins it. 10.4 follows 11.1 so the public
  commands forward only to a released gdaemon that carries the native
  command trees.
- Storage reuses the existing tables `agent_definitions`, `agent_runs`,
  `worktrees`, `clones`, `inter_session_messages` and `coordination_waits`
  (`crates/gcore/assets/schema/baseline.sql`) through the shared pool
  (#24443). No schema change.
- Source changes are built and tested before deployment. The activation
  leaves 11.1 and 11.2 are the shipping commits and own release bumps and
  promotion; earlier commits are unshipped source commits whose native routes
  stay on Proxy and whose pane monitor is not started. Read the current
  `project_policy.release_guidance` at execution and honor the published and
  unpublished binary distinction.
- Heavy Rust commands require the Release Manager's heavy-work admission and
  `CARGO_BUILD_JOBS=3`. Rust tests run through `cargo nextest run -p <package>`.
  No full pytest run.
- Python checks use `uv run`. Tests that touch the database use isolated
  temporary state and ports with
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test`
  and `GOBBY_TEST_PROTECT=1`, never the operator's daemon database.
- Provider CLIs are fake executables on a temporary `PATH`; git effects run in
  temporary repositories with a local bare remote; nodes are in-process
  command doubles; terminals are an isolated test terminal host. No test
  spawns a real provider, pushes to a real remote or touches an operator
  checkout.
- #24968 does not apply: no row in this family reads or writes `files_home`.

## Parity ownership
`kind: framing`

| Surface group | Rows | Shard | Row-owning leaves |
| --- | --- | --- | --- |
| `HTTP ... /api/agents/definitions...` reads | 3 | `sessions-agents.json` | 1.1 |
| `HTTP ... /api/agents/definitions...` writes | 8 | `sessions-agents.json` | 1.2 |
| `HTTP GET /api/agents/{running,runs...}` | 4 | `sessions-agents.json` | 2.1 |
| `HTTP ... /api/agents/{launch-defaults,spawn/prompt-preview}` | 2 | `sessions-agents.json` | 4.2 |
| `HTTP POST /api/agents/spawn`, `/spawn/batch` | 2 | `sessions-agents.json` | 4.3 |
| `HTTP POST /api/agents/runs/{run_id}/cancel` | 1 | `sessions-agents.json` | 6.1 |
| `HTTP POST /api/agents/cleanup` | 1 | `sessions-agents.json` | 6.2 |
| `HTTP ... /api/source-control/{worktrees,clones}...` | 9 | `git-workspaces.json` | 3.5 |
| `MCP gobby-agents:*` | 28 | `sessions-agents.json` | 9.1 |
| `MCP gobby-worktrees:*` | 17 | `git-workspaces.json` | 9.2 |
| `MCP gobby-clones:*` | 13 | `git-workspaces.json` | 9.3 |
| `CLI gobby agents ...` | 14 | `sessions-agents.json` | 10.1 |
| `CLI gobby worktrees ...` | 11 | `git-workspaces.json` | 10.2 |
| `CLI gobby clones ...` | 7 | `git-workspaces.json` | 10.3 |
| `HTTP POST /api/nodes/{report,runs/*}` (ownership only) | 4 | `comms-cron-hub-nodes.json` | 11.3 records |
| Total | 124 | | |
| `WS message workspace_*` (re-homed to #21565) | 3 | `websocket-chat-voice-ui.json` | 11.3 rewrites notes |
| `HTTP GET` and `HTTP POST /_gobby/remote-pane`, `HTTP POST /_gobby/agent-run-delivery` (new, private) | 3 | `comms-cron-hub-nodes.json` | 2.0 adds |

11.1 sets the nine `checkouts` HTTP rows to `delegated` and keeps the 30
checkouts MCP rows `proxied` with `Registry flip: #21570`. 11.3 sets the 35
agents HTTP and CLI rows and the 18 checkouts CLI rows to `delegated`, keeps
the 28 agents MCP rows `proxied` with `Registry flip: #21570`, and keeps the
four node rows `proxied` with notes citing this family's ownership and
Decision 14's condition. The three private rows 2.0 adds stay `proxied`:
the two pane rows name #25269 as their retirement owner and the delivery
row names D1. They are outside the 124, and their notes do not name
#21564.

## Cross-plan edges set at expansion
`kind: framing`

- #24963 (closed): waiver manifest and readiness. Every corpus leaf, 10.3 and
  both activation leaves depend on it.
- #24970 (closed): error classes and adapters. 1.1 and the descriptor and CLI
  leaves depend on it.
- #24443 (closed): shared pool. 1.1 depends on it.
- #24883 (closed): `ToolFamily` descriptor. 9.1 to 9.3 depend on it.
- #24375 (open): node launch, provider command, environment and trust
  modules. 4.1 depends on it.
- #24383 (open, blocked): node pane monitor. 4.1 depends on it because both
  edit `crates/gdaemon/src/nodes/agents/`; 8.1 generalizes its pass.
- #24149 (open): native attention store and loopback transition route. 8.1
  and 11.2 depend on it.
- #24151 (open, blocked): keeps `GET /api/nodes/detection-manifests`. No
  dependency: under Decision 14 this family owns the node report and
  run-outcome rows and the pane store, which stay Python-served.
- `.gobby/plans/s2-11-hub-hooks-rules.md` (#24151's hub hooks plan): its
  `hooks` family does not claim `/api/nodes/report` or `/api/nodes/runs`
  (Decision 14), and neither does this plan, so the prefixes fall through to
  Proxy.
- #25269 (Plan the node-runs native port: reported pane store, remote idle
  and the run reconciler, under #21542): retirement owner of 2.0's pane route
  and of the four node rows' port. No dependency.
- `.gobby/plans/apply-agent-definition.md`: its leaves may still change
  Python's `apply_agent_definition` and `apply_persona`. 0.1 freezes the
  behavior at collection; a later Python change to any corpus row requires
  re-collection before the leaf that proves that row.
- #21592 (open): native web-chat runtime and its wake seam. 5.2 defines the
  seam's use and 11.2 depends on #21592 for the runtime that supplies it.
- #25046 (open): reusable native admission. 11.1 depends on it.
- #21570, #24884 and #24885: MCP registry flip, gateway and compatibility
  backend. No dependency; 11.1 and 11.3 record `Registry flip: #21570`.
  Edge on #21570's side (Orchestrator ruling, 2026-10-28): its flip keeps
  `MCP gobby-agents:get_agent_live_output` and `MCP
  gobby-agents:wait_for_output` proxied to Python until #25269's node-runs
  port lands; empty remote tails are not accepted. Its flip also keeps `MCP
  gobby-agents:wait_for_agent` proxied until D1 ports completion delivery
  (Decision 15, Orchestrator ruling D-AF-7).
- `.gobby/plans/gobby-messaging.md` (#22340, parked): its D3 defers wake
  delivery, the mailbox and agent-run messages to #21564. D3.1's delivery
  half lands here: 5.3's worker listens on `gobby_coordination_wait`, and
  11.2 stops Python's listener (Decision 9, Orchestrator ruling D-AF-6). Its
  deletion half (the Python wait files), like the Python deletions D3.2 and
  D3.3 name, stays with #21564 as D3 records; no leaf here deletes them
  (Decision 10). `gobby_coordination_wait` is the only channel Python's
  listener serves at baseline; the draft's `gobby_agent_message_wait` does
  not exist, and a re-planned #22340 delivers any channel it adds. No
  dependency.
- #21565: owns the three re-homed WS rows. No dependency.
- Skills 5.1 and AI providers 7.5: contention on
  `crates/gdaemon/src/main.rs`, serialized by landing order.
- `.gobby/plans/gdaemon-projects-files-docs-family.md`: contention on
  `crates/gcore/src/postgres_pool/mod.rs` with 5.3, serialized by landing
  order.

## P0: Freeze the family contract
`kind: framing`

### 0.1 Definitions and runs characterization corpus [category: test] (depends: #24963)
`kind: deliverable`

Targets:
- `scripts/agents_parity_corpus.py`
- `crates/gagents/tests/fixtures/parity_definitions_runs.json`
- `tests/contracts/test_agents_parity_corpus_definitions_runs.py`

**Research context:** This leaf creates the shared collector and its first
group; 0.2 to 0.4 add groups to the same script. The group's 35 keys are the
15 HTTP rows in `src/gobby/servers/routes/agents.py::create_agents_router`
(the eight definition reads and writes, three definition patches and four
run reads); the ten MCP tools `list_agent_runs`, `list_running_agents`,
`get_running_agent`, `running_agent_stats`, `get_agent_result`,
`get_agent_capture`, `get_agent_live_output` and `wait_for_output` in
`src/gobby/mcp_proxy/tools/agents_query_tools.py`, and `apply_agent_definition`
and `apply_persona` in `src/gobby/mcp_proxy/tools/agents_spawn_tools.py`; and
the ten CLI rows `gobby agents`, `list`, `show`, `runs`, `runs list`, `runs
show`, `status`, `stats`, `steps` and `check` in `src/gobby/cli/agents.py`
and `src/gobby/cli/agents_steps.py`. Existing fixtures live in
`tests/servers/routes/test_agents_routes.py`,
`tests/mcp_proxy/tools/test_agent_capture_results.py`,
`tests/mcp_proxy/tools/test_agent_live_output.py`,
`tests/mcp_proxy/tools/test_apply_agent_definition.py`,
`tests/cli/test_agent_definitions_cli.py` and `tests/cli/test_agents_steps.py`.

Generate a committed, versioned JSON corpus from isolated Python fixtures:
method and path, tool and arguments, or command and argv; headers; seeded
`agent_definitions`, `agent_runs` and session rows; stub terminal content;
expected response, tool result, stdout, stderr and exit code; and effects,
with only enumerated volatile fields (timestamps, generated IDs, durations)
normalized. Each HTTP row records the admission matrix: no credential, API
key, local operator, ordinary and managed browser cookies, and an agent token,
which these routes refuse today. Each MCP vector records the tool's input
schema, description and definition hash. Live-output vectors cover a local
run and a remote run whose tail comes from a reported pane.

**Acceptance:**

- 0.1.1 - The group inventory equals the exact 35-key predicate over
  `sessions-agents.json`, every key has a success case and a representative
  failure case, and a key outside the group is rejected.
  test: `tests/contracts/test_agents_parity_corpus_definitions_runs.py::test_inventory_matches_group_predicate`.
- 0.1.2 - Running the collector twice against reset isolated state gives the
  same normalized fixtures, ordering, outputs and recorded effects.
  test: `tests/contracts/test_agents_parity_corpus_definitions_runs.py::test_collection_is_reproducible`.
- 0.1.3 - Each of the 15 HTTP rows records the full admission matrix.
  test: `tests/contracts/test_agents_parity_corpus_definitions_runs.py::test_http_admission_matrix`.
- 0.1.4 - Skipped-only reports, unwaived `Skipped`, stale or missing waiver
  decisions and an injected unrelated regression fail readiness.
  test: `tests/contracts/test_agents_parity_corpus_definitions_runs.py::test_readiness_rejects_skips_and_unmatched_diffs`.

Planned verification: the four named tests with the protected Python test
prefix; collector runs use temporary homes and the isolated test database.
No characterization test has run during drafting.

### 0.2 Checkouts characterization corpus [category: test] (depends: 0.1)
`kind: deliverable`

Targets:
- `scripts/agents_parity_corpus.py`
- `crates/gagents/tests/fixtures/parity_checkouts.json`
- `tests/contracts/test_agents_parity_corpus_checkouts.py`

**Research context:** The group's 57 keys are every row in
`git-workspaces.json` that names #21564: the 17 `gobby-worktrees` tools
(`src/gobby/mcp_proxy/tools/worktrees/_factory.py::create_worktrees_registry`
over `_crud.py`, `_create.py`, `_lifecycle.py`, `_sync.py` and `_cleanup.py`),
the 13 `gobby-clones` tools (`src/gobby/mcp_proxy/tools/clones.py::create_clones_registry`
over `_clones_read.py`, `_clones_creation.py`, `_clones_operations.py` and
`_clones_cleanup.py`), the nine routes in
`src/gobby/servers/routes/source_control_worktrees.py::create_source_control_worktrees_router`,
and the 18 CLI rows in `src/gobby/cli/worktrees.py` and
`src/gobby/cli/clones.py`. Hub remote routing wraps every tool:
`src/gobby/hub/remote_workspaces.py::route_node_workspace_tools` checks
machine ownership and sends get, merge and delete of a remote row to its node.
Existing fixtures live in `tests/mcp_proxy/tools/test_worktrees_*.py`,
`tests/mcp_proxy/tools/test_mcp_proxy_tools_clones.py`,
`tests/servers/routes/test_source_control_routes.py`,
`tests/cli/test_worktrees_cli.py` and `tests/cli/test_clones_cli.py`.

Vectors run against temporary git repositories with a local bare remote and
record repository effects (branches, worktree directories, refs, clone
directories) besides rows and outputs. Remote vectors use an in-process node
double that records each `workspace.merge`, `workspace.remove`,
`checkout.status` and `checkout.read` frame and returns scripted results,
including every routing code. The five clone CLI vectors in the waiver
inventory record Python's 404 exit and carry test-only `Waived` entries
asserting the native success outcome; 10.3 adds the production entries.

**Acceptance:**

- 0.2.1 - The group inventory equals the exact 57-key predicate over
  `git-workspaces.json`, every key has a success and a failure case, and a
  `/api/source-control/branches`, `/diff` or `/status` key is rejected.
  test: `tests/contracts/test_agents_parity_corpus_checkouts.py::test_inventory_matches_group_predicate`.
- 0.2.2 - Collection is reproducible against reset repositories and state,
  including recorded git and node-frame effects.
  test: `tests/contracts/test_agents_parity_corpus_checkouts.py::test_collection_is_reproducible`.
- 0.2.3 - Each of the nine HTTP rows records the full admission matrix.
  test: `tests/contracts/test_agents_parity_corpus_checkouts.py::test_http_admission_matrix`.
- 0.2.4 - Owned, foreign-machine, unknown-machine and ambiguous references
  record Python's ownership refusals and node routing codes for every
  mutating tool. test: `tests/contracts/test_agents_parity_corpus_checkouts.py::test_machine_ownership_vectors`.
- 0.2.5 - The five clone CLI route cases resolve to exact-surface test
  waivers with native assertions while other cases on those surfaces stay
  `Passed`. test: `tests/contracts/test_agents_parity_corpus_checkouts.py::test_clone_cli_waivers_are_behavior_scoped`.

Planned verification: the five named tests with the protected Python prefix
and temporary git repositories.

### 0.3 Launch and termination characterization corpus [category: test] (depends: 0.2)
`kind: deliverable`

Targets:
- `scripts/agents_parity_corpus.py`
- `crates/gagents/tests/fixtures/parity_launch.json`
- `tests/contracts/test_agents_parity_corpus_launch.py`

**Research context:** The group's 19 keys are the six HTTP rows `GET
/api/agents/launch-defaults`, `POST /api/agents/spawn/prompt-preview`, `POST
/api/agents/spawn`, `POST /api/agents/spawn/batch`
(`src/gobby/servers/routes/agent_spawn.py::create_agent_spawn_router`), `POST
/api/agents/runs/{run_id}/cancel` and `POST /api/agents/cleanup`; the nine
tools `can_spawn_agent`, `evaluate_spawn`, `spawn_agent`, `kill_agent`,
`stop_agent`, `end_agent_run`, `unregister_agent`, `cancel_stale_helpers`
and `checkpoint_agent_worktree`; and the four CLI rows `gobby agents spawn`,
`kill`, `stop` and `cleanup`. Spawn runs through
`src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py::spawn_agent_impl`;
remote spawn builds its frame with
`src/gobby/hub/spawn_payload.py::build_spawn_payload`; termination runs
through `src/gobby/mcp_proxy/tools/agent_cancellation.py::terminate_agent_run` and,
for a remote run, `src/gobby/hub/remote_cancel.py::cancel`. Existing
fixtures live in `tests/servers/routes/test_agent_spawn_routes.py`,
`tests/mcp_proxy/tools/spawn_agent/`,
`tests/mcp_proxy/tools/test_agents_spawn_evaluation.py`,
`tests/mcp_proxy/tools/test_agents_termination.py`,
`tests/mcp_proxy/tools/test_agent_cancellation.py` and
`tests/mcp_proxy/tools/test_agent_worktree_checkpoint.py`.

Fake `claude`, `codex`, `droid`, `grok`, `agy` and `srt` executables on a
temporary `PATH` record argv, environment keys (never values), working
directory and stdin. The isolated terminal host records spawned commands and
writes. Remote vectors record the `agent.spawn` and `agent.cancel` frames and
cover `machine_cap_reached`, `remote_loopback_endpoint`, `spawn_in_doubt`
and `not_stopped`. Each vector also records the run's final `agent_runs` row,
including the capture written before kill, and the parent's mailbox
delivery. Remote termination vectors seed the reported pane store
(`src/gobby/hub/remote_pane.py::RemotePaneStore`) with a tail and record the
captured tail and the entry's stopped flag.

Capture-barrier vectors record the stop's preconditions
(`src/gobby/agents/capture.py::capture_then_kill_async`): a storage failure
on the termination intent or the capture write returns
`CAPTURE_PERSIST_FAILED`, and a capture-slot write by another terminator
between the intent and the capture returns `CAPTURE_CONFLICT`; neither
stops the terminal. A stop interrupted after the capture persists is
retried to one terminal outcome and one parent delivery.

**Acceptance:**

- 0.3.1 - The group inventory equals the exact 19-key predicate, every key
  has a success and a failure case, and an out-of-group key such as `POST
  /api/nodes/report` is rejected.
  test: `tests/contracts/test_agents_parity_corpus_launch.py::test_inventory_matches_group_predicate`.
- 0.3.2 - Collection is reproducible, including recorded provider argv,
  terminal writes, node frames, run rows and captured remote tails.
  test: `tests/contracts/test_agents_parity_corpus_launch.py::test_collection_is_reproducible`.
- 0.3.3 - Each of the six agents HTTP rows records the full admission
  matrix.
  test: `tests/contracts/test_agents_parity_corpus_launch.py::test_http_admission_matrix`.
- 0.3.4 - Local and remote spawn, cancel and stop record every hub-flow code
  and the resulting run and mailbox state.
  test: `tests/contracts/test_agents_parity_corpus_launch.py::test_local_and_remote_lifecycle_vectors`.
- 0.3.5 - The capture-barrier vectors record no terminal stop for a
  persistence failure or a capture conflict, and one terminal outcome and
  one parent delivery for a retried interrupted stop.
  test: `tests/contracts/test_agents_parity_corpus_launch.py::test_capture_barrier_vectors`.

Planned verification: the five named tests with the protected Python prefix,
fake provider executables and the isolated terminal host.

### 0.4 Messaging, coordination and seats corpus and the family partition [category: test] (depends: 0.3)
`kind: deliverable`

Targets:
- `scripts/agents_parity_corpus.py`
- `crates/gagents/tests/fixtures/parity_messaging.json`
- `tests/contracts/test_agents_parity_corpus_messaging.py`

**Research context:** The group's nine keys are the tools `send_message`,
`get_inter_session_message` and `get_inter_session_messages`
(`src/gobby/mcp_proxy/tools/agent_messaging.py::add_messaging_tools`),
`wait_for_agent` (`agents_query_tools.py`), `wait_for_coordination` and
`cancel_coordination_wait`
(`src/gobby/mcp_proxy/tools/coordination.py::register_coordination_tools`),
`admit_team` and `check_runbook_seats`
(`src/gobby/mcp_proxy/tools/runbook_seat_tools.py`) and `cold_start_admin`
(`src/gobby/mcp_proxy/tools/admin_cold_start.py::cold_start_admin`). Send
runs through `src/gobby/sessions/mailbox.py::MailboxService.send`,
`src/gobby/sessions/mailbox_delivery.py::dispatch_mailbox_wakes` and
`src/gobby/events/wake.py::WakeDispatcher`; waits persist in
`src/gobby/storage/coordination_waits.py::CoordinationWaitManager`. Existing
fixtures live in `tests/mcp_proxy/tools/test_agent_messaging.py`,
`tests/mcp_proxy/tools/test_agent_messaging_broadcast.py`,
`tests/mcp_proxy/tools/test_agent_messaging_protocol_line.py`,
`tests/events/test_coordination_holds.py` and
`tests/mcp_proxy/tools/test_resume_paused_seat.py`.

Vectors cover every target kind (`session`, `parent`, `agent`, `project`,
`global`), the `wake` default and explicit values, refused recipients
(`message_recipient_not_allowed`), queued delivery to interrupted and
waiting sessions, terminal-injected and web-chat wakes recorded through
doubles, wakes to a node terminal under each reported composer state, and
coordination waits under each condition: a keyed `coordination_release`, an
owner status set (including a status already matched at registration) and a
reply. They also cover timeout, owner end, cancel, a duplicate registration
that returns the active wait, and each registration refusal. This leaf also adds the
partition check: the four group predicates are disjoint and their union is
exactly the 120 ported keys.

**Acceptance:**

- 0.4.1 - The group inventory equals the exact nine-key predicate, every key
  has a success and a failure case, and an out-of-group key is rejected.
  test: `tests/contracts/test_agents_parity_corpus_messaging.py::test_inventory_matches_group_predicate`.
- 0.4.2 - Collection is reproducible, including mailbox rows, wake records
  and coordination-wait rows.
  test: `tests/contracts/test_agents_parity_corpus_messaging.py::test_collection_is_reproducible`.
- 0.4.3 - The four groups are disjoint and cover exactly the 120 ported
  keys: the ones that name #21564, excluding by exact key the re-homed WS,
  attention and nodes CLI rows and the four node report and run-outcome
  rows, which stay outside every group.
  test: `tests/contracts/test_agents_parity_corpus_messaging.py::test_groups_partition_the_family`.

Planned verification: the three named tests with the protected Python prefix.

## P1: Native family and agent definitions
`kind: framing`

### 1.1 Crate, two route families, admission, effects seam and definition reads [category: code] (depends: 0.4, #24443, #24963, #24970)
`kind: deliverable`

Targets:
- `Cargo.toml`
- `Cargo.lock`
- `crates/gagents/Cargo.toml`
- `crates/gagents/src/lib.rs`
- `crates/gagents/src/admission.rs`
- `crates/gagents/src/effects.rs`
- `crates/gagents/src/definitions/mod.rs`
- `crates/gagents/src/definitions/reads.rs`
- `crates/gdaemon/Cargo.toml`
- `crates/gdaemon/src/serve.rs::*` — scope-reason: push the agents and checkouts families beside RuntimeFamily in the pool-gated block of both cfg-gated serve entry points
- `crates/gagents/tests/definitions.rs`

**Research context:** `crates/gruntime/src/lib.rs::RuntimeFamily` is the
pattern: a `NATIVE_ROUTES` table of method, path and handler rows,
`RouteFamily.claims` matching method, path and caller, and
`RouteFamily.check_proxied` for routes left to Python
(`crates/gcore/src/route_family.rs::RouteFamily`).
`crates/gdaemon/src/front_door/routes.rs::RouteTable::new` selects each
family's backend from `routes.get(family.name())` or the family default.
Create `AgentsFamily` (name `agents`, prefix `/api/agents`) and
`CheckoutsFamily` (name `checkouts`, prefixes `/api/source-control/worktrees`
and `/api/source-control/clones`), both defaulting to Proxy.
`crates/gagents/src/lib.rs` exposes one function that pushes both, called
from serve.rs only when the pool exists; a node composes neither.
`crates/gagents/Cargo.toml` declares the crate's complete dependency set,
including `clap` with `derive` for 10.1 to 10.3; git effects run the `git`
executable as Python does. Later leaves edit the manifest only where a Target
says so.

`admission.rs` holds `AdmissionClass::OperatorOrBrowser`, the one class every
route of this family declares (Decision 11). Its `admitted(parts, class)`
returns false in this leaf, so every claim fails closed and traffic stays
proxied; 11.1 binds it to #25046.

`effects.rs` defines `MachineEffects` (Decision 6) with the request and
outcome types for each effect and the node routing and hub-flow codes. No
implementation lands here; 2.2 creates gdaemon's.

`definitions/reads.rs` ports
`src/gobby/storage/definitions/agents.py::AgentDefinitionManager` reads over
`agent_definitions` (scope precedence: project, user, bundled; soft-deleted
rows) and the three read routes: `list_definitions`, `get_definition` and
`export_definition` in `src/gobby/servers/routes/agents.py`. Export keeps
Python's YAML form and file name. Map errors onto #24970's classes.

**Granularity:** The crate, both family shells, the admission class, the
effects seam and three read rows form one usable outcome: the first native
reads. The target count exceeds six because composition crosses the crate
boundary and the shells must exist before later leaves fill them.

**Acceptance:**

- 1.1.1 - `HTTP GET /api/agents/definitions` matches Python's listing, scope
  precedence, filters and deleted-row handling.
  test: `crates/gagents/tests/definitions.rs::list_definitions_matches_python`.
- 1.1.2 - `HTTP GET /api/agents/definitions/{name}` matches Python's
  resolved definition and 404.
  test: `crates/gagents/tests/definitions.rs::get_definition_matches_python`.
- 1.1.3 - `HTTP GET /api/agents/definitions/{name}/export` matches Python's
  YAML body, headers and file name.
  test: `crates/gagents/tests/definitions.rs::export_definition_matches_python`.
- 1.1.4 - Each family claims only its implemented rows, the longest prefix
  keeps #21584's `/api/source-control` routes off `checkouts`, absent
  configuration keeps both on Proxy, a node composes neither, and every claim
  fails closed until admission is bound.
  test: `crates/gagents/tests/definitions.rs::families_are_scoped_and_fail_closed`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-agents --test definitions`;
`cargo check -p gobby-daemon`. No implementation check has run during
drafting.

### 1.2 Definition writes [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `crates/gagents/src/definitions/mod.rs`
- `crates/gagents/src/definitions/writes.rs`
- `crates/gagents/tests/definition_writes.rs`

**Research context:** The eight write routes in
`src/gobby/servers/routes/agents.py::create_agents_router` are
`create_definition`, `update_definition`, `delete_definition`,
`restore_definition`, `import_definition`, `patch_rules`,
`patch_rule_selectors` and `patch_variables`, with request models
`CreateAgentDefinitionRequest` and `UpdateAgentDefinitionRequest` and the
operator check `_require_definition_operator`. Import reads a bundled
definition through `_bundled_definition_path`. Writes go through
`AgentDefinitionManager` in one transaction per request. Preserve validation
messages, the `terminal_backend` refusal that
`tests/agents/test_backend_ingress.py::test_every_agent_definition_write_and_read_rejects_backend`
pins, soft delete and restore, name collisions, and the patch merge rules for
rules, rule selectors and variables.

**Granularity:** Eight routes over one definition writer share validation and
one transaction shape; splitting them would duplicate that writer.

**Acceptance:**

- 1.2.1 - `HTTP POST /api/agents/definitions` matches Python's creation,
  validation and collision errors.
  test: `crates/gagents/tests/definition_writes.rs::create_definition_matches_python`.
- 1.2.2 - `HTTP PUT /api/agents/definitions/{definition_id}` matches
  Python's update and refusals.
  test: `crates/gagents/tests/definition_writes.rs::update_definition_matches_python`.
- 1.2.3 - `HTTP DELETE /api/agents/definitions/{definition_id}` matches
  Python's soft delete and missing-row error.
  test: `crates/gagents/tests/definition_writes.rs::delete_definition_matches_python`.
- 1.2.4 - `HTTP POST /api/agents/definitions/{definition_id}/restore`
  matches Python's restore and conflicts.
  test: `crates/gagents/tests/definition_writes.rs::restore_definition_matches_python`.
- 1.2.5 - `HTTP POST /api/agents/definitions/import/{name}` matches Python's
  bundled import and unknown-name error.
  test: `crates/gagents/tests/definition_writes.rs::import_definition_matches_python`.
- 1.2.6 - `HTTP PATCH /api/agents/definitions/{definition_id}/rules` matches
  Python's rule merge. test: `crates/gagents/tests/definition_writes.rs::patch_rules_matches_python`.
- 1.2.7 - `HTTP PATCH /api/agents/definitions/{definition_id}/rule-selectors`
  matches Python's selector merge.
  test: `crates/gagents/tests/definition_writes.rs::patch_rule_selectors_matches_python`.
- 1.2.8 - `HTTP PATCH /api/agents/definitions/{definition_id}/variables`
  matches Python's variable merge.
  test: `crates/gagents/tests/definition_writes.rs::patch_variables_matches_python`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-agents --test definition_writes`.

## P2: Agent runs and live output
`kind: framing`

### 2.0 Loopback bridge routes for the pane store and completion delivery [category: code]
`kind: deliverable`

Targets:
- `src/gobby/servers/routes/loopback_bridge.py`
- `src/gobby/servers/_app_routes.py::register_routes`
- `tests/servers/routes/test_loopback_bridge.py`
- `docs/contracts/parity-ledger-status.d/comms-cron-hub-nodes.json::*` — scope-reason: add the three private loopback bridge route records and retain every other record
- `docs/contracts/parity-ledger.md`

Consumers unchanged:
- `src/gobby/servers/app_factory.py` — no-edit-reason: calls register_routes with an unchanged signature.
- `tests/test_wiki_retirement_contract.py` — no-edit-reason: calls register_routes with an unchanged signature and asserts wiki routes only.

**Research context:** Decisions 10 and 12 (D-AF-5). Python's reads of a
pane on another machine live in `src/gobby/hub/remote_runtime.py`:
`_stored_tail` returns the entry's tail, or the empty string for a missing,
stopped or pane-less entry; `remote_composer_state` returns the entry's
composer state, or `unknown` in those cases; and
`RemoteTerminalRuntime.terminate` calls
`src/gobby/hub/remote_pane.py::RemotePaneStore.mark_stopped` after a
successful remote cancel. Add one router module. At `/_gobby/remote-pane`,
`GET` with the query parameters `terminal_id` and `machine_id` returns
`{"tail": ..., "composer": ...}`, computed by those two helpers, and `POST`
with a JSON body of `terminal_id` and `machine_id` calls `mark_stopped`
and returns 204.

Decision 15 (D-AF-7) adds `POST /_gobby/agent-run-delivery` to the same
module. Its JSON body carries `run_id`. While
`src/gobby/agents/readiness.py::spawn_readiness_blocker` names a blocker
for `server.services` (shutdown, startup, or restart recovery still
running), it returns 503, so no post races Python's startup recovery. A
missing or non-terminal run returns 409, and its subscribers stay.
Otherwise it calls
`src/gobby/agents/terminal_delivery.py::deliver_existing_terminal_run` with
the app's database, run manager, `completion_registry` and `run_db` from
`server.services`, the call that `cancel_agent_run` in
`src/gobby/servers/routes/agents.py` makes after its own commit. Then it
rereads the run's rows with `get_completion_subscribers` from
`src/gobby/storage/pipeline_subscribers.py` and returns
`{"pending": <rows left>}`. The helper's boolean says only that the run was
terminal and discards the per-subscriber result, so the reread is the
acknowledgement (Decision 15), and a failed reread returns 503. The helper
has no machine check, so another machine's terminal run is delivered the
same way.

Admission for all three methods copies the native WS bridge's pair proof
(`src/gobby/servers/websocket/native_bridge.py::mount_native_bridge`): the
peer must be loopback (`src/gobby/config/bootstrap.py::is_loopback_host`)
and `X-Gobby-Front-Door` must equal
`src/gobby/runner_backend.py::front_door_secret` under
`secrets.compare_digest`. Anything else gets 403 before the store or a run
is touched, and a Python daemon started without gdaemon has no secret and
refuses every request. gdaemon's front door hands every `/_gobby/` path to
`crates/gdaemon/src/websocket/publish.rs::handle`, which answers 404
outside its two paths, so no outside request reaches these routes through
the proxy. `scripts/generate_parity_ledger.py::http_surfaces` lists every
application route, so the three methods become ledger surfaces in this
commit: record them as `proxied` with a note naming this leaf and the pair
proof, never #21564. The pane notes name #25269 as the retirement owner and
the delivery note names native completion delivery (agents-family D1).
Regenerate the ledger.

**Acceptance:**

- 2.0.1 - A loopback request with the pair secret reads the tail and
  composer state that the two helpers return for a live, stopped,
  pane-less and missing entry.
  test: `tests/servers/routes/test_loopback_bridge.py::test_read_matches_remote_runtime`.
- 2.0.2 - A loopback request with the pair secret marks the entry stopped,
  and later reads return the empty tail and `unknown`.
  test: `tests/servers/routes/test_loopback_bridge.py::test_mark_stopped_matches_remote_runtime`.
- 2.0.3 - A non-loopback peer, a missing or wrong secret, and a daemon
  without a secret each get 403 from all three methods and leave the store
  and the run's subscribers untouched.
  test: `tests/servers/routes/test_loopback_bridge.py::test_refuses_without_pair_proof`.
- 2.0.4 - The ledger lists the three methods as `proxied` with their
  retirement notes. test: `tests/test_parity_ledger.py::test_committed_ledger_matches_current_public_inventory`.
- 2.0.5 - For a terminal run on this machine or another one, a delivery
  request with the pair secret wakes its in-memory waiter and its durable
  subscribers once, removes the acknowledged subscriber rows as Python's
  own producers do, and answers `pending: 0`; a missing or non-terminal run
  gets 409 and keeps its subscribers.
  test: `tests/servers/routes/test_loopback_bridge.py::test_agent_run_delivery_matches_python_producers`.
- 2.0.6 - When the first wake is not acknowledged, the request answers
  `pending: 1` and keeps that row, and a second request wakes the session
  and answers `pending: 0`. A request while restart recovery is pending, and
  a request whose reread fails, each get 503.
  test: `tests/servers/routes/test_loopback_bridge.py::test_agent_run_delivery_reports_pending_rows`.

Planned verification: the six named tests with the protected Python
prefix; `uv run python scripts/generate_parity_ledger.py` run twice with no
second-run diff; scoped Ruff and mypy; a changed-test type audit.

### 2.1 Run reads [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `crates/gagents/src/lib.rs`
- `crates/gagents/src/runs/mod.rs`
- `crates/gagents/src/runs/reads.rs`
- `crates/gagents/tests/runs.rs`

**Research context:** The four read routes in
`src/gobby/servers/routes/agents.py::create_agents_router` are
`list_running_agents`, `list_agent_runs`, `get_agent_run_detail` and
`get_agent_run_name`, over `agent_runs` through the query helpers in
`src/gobby/storage/agents/`. The same reads back the MCP tools
`list_agent_runs`, `list_running_agents`, `get_running_agent`,
`running_agent_stats` and `get_agent_result` in
`src/gobby/mcp_proxy/tools/agents_query_tools.py`; this leaf ports them as
gagents services, and 9.1 adapts them to MCP. Preserve filters, ordering,
limits, project scope, the machine column and the remote-run fields. Each
module exposes its own route rows and `crates/gagents/src/lib.rs` assembles
the family tables from them, as 1.2 did for the definition writes.

**Acceptance:**

- 2.1.1 - `HTTP GET /api/agents/running` matches Python's running-run list
  and filters. test: `crates/gagents/tests/runs.rs::list_running_matches_python`.
- 2.1.2 - `HTTP GET /api/agents/runs` matches Python's run list, filters and
  limits. test: `crates/gagents/tests/runs.rs::list_runs_matches_python`.
- 2.1.3 - `HTTP GET /api/agents/runs/{run_id}` matches Python's run detail
  and missing-run error. test: `crates/gagents/tests/runs.rs::run_detail_matches_python`.
- 2.1.4 - `HTTP GET /api/agents/runs/{run_id}/name` matches Python's name
  resolution. test: `crates/gagents/tests/runs.rs::run_name_matches_python`.
- 2.1.5 - The run read services return 0.1's recorded results for the five
  MCP read tools. test: `crates/gagents/tests/runs.rs::run_read_services_match_corpus`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-agents --test runs`.

### 2.2 Live output and gdaemon's machine effects [category: code] (depends: 2.1, 2.0)
`kind: deliverable`

Targets:
- `crates/gagents/src/runs/mod.rs`
- `crates/gagents/src/runs/output.rs`
- `crates/gagents/tests/run_output.rs`
- `crates/gterminals/src/control.rs::ControlClient`
- `crates/gdaemon/src/agent_effects.rs`
- `crates/gdaemon/src/lib.rs`
- `crates/gdaemon/src/serve.rs::*` — scope-reason: pass gdaemon's machine effects, with the Python backend address and pair secret, into the pool-gated agents and checkouts composition of both cfg-gated serve entry points
- `crates/gdaemon/tests/agent_effects.rs`

**Research context:** `get_agent_live_output` and `wait_for_output` in
`src/gobby/mcp_proxy/tools/agents_query_tools.py` read a run's pane;
`get_agent_capture` pages the capture persisted in the run's result
(`src/gobby/mcp_proxy/tools/agents_payloads.py::_agent_capture_parts`).
Python reads a local pane with the host's `snapshot` method
(`src/gobby/terminals/host_client.py`, line counts and text mode per tool)
and a remote pane from the reported store (Decision 12). The native
`crates/gterminals/src/control.rs::ControlClient` has no `snapshot` request
yet; add it with the same fields (`host_terminal_id`, `mode`, `max_bytes`,
`max_lines`) and the same refusal when the host does not echo the mode.
node-halves 2.10 (#24379) names the same method for the node pane monitor;
whichever lands first adds it and the other consumes it.

Create `crates/gdaemon/src/agent_effects.rs` with gdaemon's
`MachineEffects` implementation. This leaf implements the pane read: a
hub-local run is read through the hub terminal host's control client, and a
remote run's tail and composer state come from 2.0's `GET`, with the last
requested lines kept as `RemoteTerminalRuntime.snapshot` keeps them. The
effects call the Python backend address with the `X-Gobby-Front-Door` pair
secret, the address and secret gdaemon already gives its WS bridge leg
(`crates/gdaemon/src/websocket/transport.rs`), through the hyper client
gdaemon already depends on. A refused or unreachable route reads as a
missing entry, empty tail and `unknown` composer, which is what Python's own
read returns once its in-memory store is lost, and logs a warning. Every other effect returns the
`unavailable` class until its owning leaf (3.2, 4.1, 5.2, 6.1) implements
it; no route reaches those effects before then, because both families stay
on Proxy and fail closed. Declare the module in `crates/gdaemon/src/lib.rs`
and pass the implementation into the gagents composition in serve.rs only
when the pool exists. `wait_for_output` keeps Python's polling, pattern and
timeout semantics.

**Granularity:** The live-output services need a pane read, and the pane
read needs the effects implementation, its composition and the host
request; no part of that path is usable alone.

**Acceptance:**

- 2.2.1 - The capture service returns 0.1's recorded capture pages for
  local and remote runs. test: `crates/gagents/tests/run_output.rs::capture_matches_corpus`.
- 2.2.2 - The live-output service returns 0.1's recorded output, offsets
  and limits for local runs and for remote runs whose tail comes from the
  reported store. test: `crates/gagents/tests/run_output.rs::live_output_matches_corpus`.
- 2.2.3 - The wait-for-output service matches 0.1's matched, timed-out and
  ended-run outcomes. test: `crates/gagents/tests/run_output.rs::wait_for_output_matches_corpus`.
- 2.2.4 - gdaemon's effects read a hub-local pane through the host
  `snapshot` request and a remote pane's tail and composer through 2.0's pane
  route with the pair secret, read a refused or unreachable route as a
  missing entry, and return the `unavailable` class for unimplemented
  effects.
  test: `crates/gdaemon/tests/agent_effects.rs::pane_read_local_and_remote`.
- 2.2.5 - The effects reach the composition only on hub and standalone with
  a pool; a node composes none.
  test: `crates/gdaemon/tests/agent_effects.rs::effects_compose_only_with_pool`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-agents --test run_output`;
`CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon --test agent_effects`;
`cargo nextest run -p gobby-terminals`.

## P3: Worktrees and clones
`kind: framing`

### 3.1 Worktree records and machine ownership [category: code] (depends: 2.2)
`kind: deliverable`

Targets:
- `crates/gagents/src/lib.rs`
- `crates/gagents/src/checkouts/mod.rs`
- `crates/gagents/src/checkouts/ownership.rs`
- `crates/gagents/src/checkouts/worktree_records.rs`
- `crates/gagents/tests/worktree_records.rs`

**Research context:** Ten `gobby-worktrees` tools are record operations over
`worktrees` (`src/gobby/storage/worktrees.py`): `get_worktree`,
`list_worktrees`, `get_worktree_stats` and `get_worktree_by_task` in
`src/gobby/mcp_proxy/tools/worktrees/_crud.py`, and `claim_worktree`,
`release_worktree`, `abandon_worktree`, `reactivate_worktree`,
`mark_worktree_merged` and `link_task_to_worktree` in
`src/gobby/mcp_proxy/tools/worktrees/_lifecycle.py`. Hub machine ownership
wraps every tool: `src/gobby/hub/remote_workspaces.py::route_node_workspace_tools`
checks `_owned`, `owner_machine` and `machine_refusal`, and
`src/gobby/hub/machine_routing.py::resolve_target_machine` resolves a
reference to one machine with the routing codes `ambiguous_reference`,
`checkout_unresolved`, `machine_not_found` and `machine_not_owned`. List
tools keep their own machine filter. `ownership.rs` is shared by worktrees,
clones and spawn.

**Acceptance:**

- 3.1.1 - The ten worktree record services return 0.2's recorded rows and
  errors. test: `crates/gagents/tests/worktree_records.rs::record_services_match_corpus`.
- 3.1.2 - Owned, foreign-machine, unknown-machine and ambiguous references
  give Python's refusals and routing codes, mapped through #24970's classes.
  test: `crates/gagents/tests/worktree_records.rs::machine_ownership_matches_python`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-agents --test worktree_records`.

### 3.2 Worktree git effects and remote checkout frames [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `crates/gagents/src/checkouts/mod.rs`
- `crates/gagents/src/checkouts/git.rs`
- `crates/gagents/src/checkouts/worktree_git.rs`
- `crates/gagents/tests/worktree_git.rs`
- `crates/gdaemon/src/agent_effects.rs`
- `crates/gdaemon/tests/agent_effects.rs`

**Research context:** Seven `gobby-worktrees` tools change repositories:
`create_worktree` (`worktrees/_create.py`), `delete_worktree`
(`_lifecycle.py`), `sync_worktree`, `merge_worktree` and `push_branch`
(`_sync.py`), and `detect_stale_worktrees` and `cleanup_stale_worktrees`
(`_cleanup.py`), over `src/gobby/worktrees/git/manager.py`, `creation.py`
and `deletion.py`. For the hub's own machine, `git.rs` runs the `git`
executable with Python's arguments, environment and working directories
(Decision 6). A row owned by another machine goes to its node as Python's
`_remote_get`, `_remote_merge` and `_remote_delete` send it: gdaemon's
effects implement `workspace_merge`, `workspace_remove`, `checkout_status`
and `checkout_read` with `crates/gdaemon/src/node_commands.rs::CommandFrame`
through `crates/gdaemon/src/nodes/registry.rs::Registry::command_sender` and
`CommandSender::request`. `node_commands.rs` is used, not edited.

**Acceptance:**

- 3.2.1 - The seven worktree git services reproduce 0.2's recorded rows,
  branches, directories and refs in temporary repositories with a local
  bare remote. test: `crates/gagents/tests/worktree_git.rs::git_services_match_corpus`.
- 3.2.2 - Get, merge and delete of a row owned by another machine send 0.2's
  recorded frames and map every routing code.
  test: `crates/gagents/tests/worktree_git.rs::remote_rows_use_node_frames`.
- 3.2.3 - gdaemon's effects send `workspace.merge`, `workspace.remove`,
  `checkout.status` and `checkout.read` through the registry command sender
  and return the node's result or routing code.
  test: `crates/gdaemon/tests/agent_effects.rs::checkout_frames_use_command_sender`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-agents --test worktree_git`;
`CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon --test agent_effects`.

### 3.3 Clone records [category: code] (depends: 3.2)
`kind: deliverable`

Targets:
- `crates/gagents/src/checkouts/mod.rs`
- `crates/gagents/src/checkouts/clone_records.rs`
- `crates/gagents/tests/clone_records.rs`

**Research context:** Seven `gobby-clones` tools are record operations over
`clones` (`src/gobby/storage/clones.py::LocalCloneManager`): `get_clone`,
`list_clones`, `claim_clone`, `release_clone`, `get_clone_by_task`,
`link_task_to_clone` and `get_clone_stats` in
`src/gobby/mcp_proxy/tools/_clones_read.py`. They use 3.1's ownership
checks unchanged.

**Acceptance:**

- 3.3.1 - The seven clone record services return 0.2's recorded rows and
  errors. test: `crates/gagents/tests/clone_records.rs::record_services_match_corpus`.
- 3.3.2 - Clone references give the same ownership refusals and routing
  codes as worktree references.
  test: `crates/gagents/tests/clone_records.rs::machine_ownership_matches_python`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-agents --test clone_records`.

### 3.4 Clone git effects [category: code] (depends: 3.3)
`kind: deliverable`

Targets:
- `crates/gagents/src/checkouts/mod.rs`
- `crates/gagents/src/checkouts/clone_git.rs`
- `crates/gagents/tests/clone_git.rs`

**Research context:** Six `gobby-clones` tools change repositories:
`create_clone` (`_clones_creation.py`), `delete_clone`, `sync_clone` and
`merge_clone` (`_clones_operations.py`), and `detect_stale_clones` and
`cleanup_stale_clones` (`_clones_cleanup.py`), over
`src/gobby/clones/git.py::CloneGitManager`. They run through 3.2's `git.rs`
and, for a clone owned by another machine, 3.2's node frames.

**Acceptance:**

- 3.4.1 - The six clone git services reproduce 0.2's recorded rows and clone
  directories in temporary repositories with a local bare remote.
  test: `crates/gagents/tests/clone_git.rs::git_services_match_corpus`.
- 3.4.2 - Operations on a clone owned by another machine send 0.2's recorded
  frames and map every routing code.
  test: `crates/gagents/tests/clone_git.rs::remote_rows_use_node_frames`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-agents --test clone_git`.

### 3.5 Source-control worktree and clone routes [category: code] (depends: 3.4)
`kind: deliverable`

Targets:
- `crates/gagents/src/lib.rs`
- `crates/gagents/src/checkouts/mod.rs`
- `crates/gagents/src/checkouts/routes.rs`
- `crates/gagents/tests/checkout_routes.rs`

**Research context:** The nine routes are nested in
`src/gobby/servers/routes/source_control_worktrees.py::create_source_control_worktrees_router`:
`list_worktrees`, `create_client_worktree`, `get_worktree_stats`,
`delete_worktree`, `cleanup_worktrees`, `sync_worktree`, `list_clones`,
`delete_clone` and `sync_clone`, with the helpers `authorize_machine`,
`workspace_owner`, `remove_node_workspace` and `run_clone_operation`. They
call the services of 3.1 to 3.4 and become the `checkouts` family's claims.

**Granularity:** Nine route rows share one ownership check and the
services of 3.1 to 3.4; each acceptance item is one ledger row.

**Acceptance:**

- 3.5.1 - `HTTP GET /api/source-control/worktrees` matches Python's list
  and filters. test: `crates/gagents/tests/checkout_routes.rs::list_worktrees_matches_python`.
- 3.5.2 - `HTTP POST /api/source-control/worktrees` matches Python's
  creation, machine authorization and errors.
  test: `crates/gagents/tests/checkout_routes.rs::create_worktree_matches_python`.
- 3.5.3 - `HTTP GET /api/source-control/worktrees/stats` matches Python's
  counts. test: `crates/gagents/tests/checkout_routes.rs::worktree_stats_matches_python`.
- 3.5.4 - `HTTP DELETE /api/source-control/worktrees/{worktree_id}` matches
  Python's local and node removal.
  test: `crates/gagents/tests/checkout_routes.rs::delete_worktree_matches_python`.
- 3.5.5 - `HTTP POST /api/source-control/worktrees/cleanup` matches Python's
  stale cleanup. test: `crates/gagents/tests/checkout_routes.rs::cleanup_worktrees_matches_python`.
- 3.5.6 - `HTTP POST /api/source-control/worktrees/{worktree_id}/sync`
  matches Python's sync and conflicts.
  test: `crates/gagents/tests/checkout_routes.rs::sync_worktree_matches_python`.
- 3.5.7 - `HTTP GET /api/source-control/clones` matches Python's list.
  test: `crates/gagents/tests/checkout_routes.rs::list_clones_matches_python`.
- 3.5.8 - `HTTP DELETE /api/source-control/clones/{clone_id}` matches
  Python's removal. test: `crates/gagents/tests/checkout_routes.rs::delete_clone_matches_python`.
- 3.5.9 - `HTTP POST /api/source-control/clones/{clone_id}/sync` matches
  Python's sync. test: `crates/gagents/tests/checkout_routes.rs::sync_clone_matches_python`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-agents --test checkout_routes`.

## P4: Launch and spawn
`kind: framing`

### 4.1 Hub-local launch over a shared run-state store [category: code] (depends: 3.5, #24375, #24383)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/nodes/agents/launch.rs`
- `crates/gdaemon/src/nodes/agents/state.rs::*` — scope-reason: extract the run-state store trait that the node's run-state file implements
- `crates/gdaemon/src/hub_launch.rs`
- `crates/gdaemon/src/agent_effects.rs`
- `crates/gdaemon/src/lib.rs`
- `crates/gdaemon/tests/hub_launch.rs`
- `crates/gdaemon/tests/agent_effects.rs`

**Research context:** #24375 creates `crates/gdaemon/src/nodes/agents/launch.rs`
with provider command, environment, trust and the SRT chokepoint
(`crates/gdaemon/src/nodes/agents/sandbox/runtime.rs::require_managed_srt`
and `prepare_launch`) over the node's state in `state.rs`. Decision 7: take
the run-state store and the terminal host as parameters. The node passes its
run-state file and its host control; `hub_launch.rs` passes a store over
the hub's `agent_runs` rows and the hub terminal host. No launch code is
copied, and `sandbox/paths.rs` is not edited. Exit observation updates the
run through the store; 6.1 adds delivery to the parent and the completion
handoff (Decision 15). gdaemon's effects
implement `spawn`: a hub-local run goes to `hub_launch.rs`, and a run for
another machine sends `agent.spawn` with the payload 4.3 builds, mapping
`machine_cap_reached`, `remote_loopback_endpoint` and `spawn_in_doubt`.

**Granularity:** The store trait, its two implementations and the spawn
effect are one launch path; neither mode can launch without the shared
trait.

**Acceptance:**

- 4.1.1 - Node launch keeps its argv, environment, sandbox wrapping and
  run-state records through the store trait.
  test: `crates/gdaemon/tests/hub_launch.rs::node_launch_unchanged_through_store_trait`.
- 4.1.2 - A hub-local launch reproduces 0.3's recorded provider argv,
  environment keys, working directory, sandbox wrapping and `agent_runs`
  row for each provider.
  test: `crates/gdaemon/tests/hub_launch.rs::hub_local_launch_matches_corpus`.
- 4.1.3 - The spawn effect launches a hub-local run in process and sends
  `agent.spawn` for another machine with every hub-flow code.
  test: `crates/gdaemon/tests/agent_effects.rs::spawn_routes_local_and_remote`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon --test hub_launch --test agent_effects --test node_agents`.

### 4.2 Spawn admission, launch defaults and prompt preview [category: code] (depends: 4.1)
`kind: deliverable`

Targets:
- `crates/gagents/src/lib.rs`
- `crates/gagents/src/spawn/mod.rs`
- `crates/gagents/src/spawn/admission.rs`
- `crates/gagents/src/spawn/preview.rs`
- `crates/gagents/tests/spawn_admission.rs`

**Research context:** `get_launch_defaults` and `preview_spawn_prompt` in
`src/gobby/servers/routes/agent_spawn.py::create_agent_spawn_router`, and
`can_spawn_agent` (`agents_query_tools.py`) and `evaluate_spawn_tool`
(`agents_spawn_tools.py`). Admission keeps the agent depth limit of 5, the
per-machine cap (`machine_cap_reached`), definition resolution and the
`terminal_backend` refusal.

**Acceptance:**

- 4.2.1 - `HTTP GET /api/agents/launch-defaults` matches Python's defaults.
  test: `crates/gagents/tests/spawn_admission.rs::launch_defaults_matches_python`.
- 4.2.2 - `HTTP POST /api/agents/spawn/prompt-preview` matches Python's
  rendered prompt and errors.
  test: `crates/gagents/tests/spawn_admission.rs::prompt_preview_matches_python`.
- 4.2.3 - The admission services return 0.3's recorded verdicts for depth,
  machine cap, definitions and the `terminal_backend` refusal.
  test: `crates/gagents/tests/spawn_admission.rs::admission_services_match_corpus`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-agents --test spawn_admission`.

### 4.3 Spawn execution [category: code] (depends: 4.2)
`kind: deliverable`

Targets:
- `crates/gagents/src/spawn/mod.rs`
- `crates/gagents/src/spawn/execute.rs`
- `crates/gagents/src/spawn/payload.rs`
- `crates/gagents/tests/spawn_execute.rs`

**Research context:** HTTP `spawn_agent` and `spawn_batch` in
`src/gobby/servers/routes/agent_spawn.py` and MCP `spawn_agent` share
`src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py::spawn_agent_impl`.
Remote spawn resolves the machine with
`src/gobby/mcp_proxy/tools/spawn_agent/_remote_routing.py::resolve_spawn_route`
and sends with `spawn_remote_route`; the frame comes from
`src/gobby/hub/spawn_payload.py::build_spawn_payload`. One gagents service
serves all three surfaces: admission (4.2), isolation through 3.x's worktree
and clone services, the run row, and the spawn effect (4.1). `payload.rs`
reproduces Python's frame byte for byte. Failure cleanup captures the
terminal's tail through 2.2's pane read before teardown, as
`src/gobby/mcp_proxy/tools/spawn_agent/_failure_cleanup.py` does through
`snapshot_full`, so a remote run's tail comes from the reported store.

**Acceptance:**

- 4.3.1 - `HTTP POST /api/agents/spawn` matches Python's local and remote
  spawn results and errors.
  test: `crates/gagents/tests/spawn_execute.rs::spawn_route_matches_python`.
- 4.3.2 - `HTTP POST /api/agents/spawn/batch` matches Python's batch
  results and partial failures.
  test: `crates/gagents/tests/spawn_execute.rs::spawn_batch_matches_python`.
- 4.3.3 - The spawn service reproduces 0.3's isolation, run rows, remote
  frames and the `spawn_in_doubt` and `remote_loopback_endpoint` outcomes.
  test: `crates/gagents/tests/spawn_execute.rs::spawn_service_matches_corpus`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-agents --test spawn_execute`.

### 4.4 Apply agent definition and persona [category: code] (depends: 4.3)
`kind: deliverable`

Targets:
- `crates/gagents/src/spawn/mod.rs`
- `crates/gagents/src/spawn/apply.rs`
- `crates/gagents/tests/spawn_apply.rs`

**Research context:** `apply_agent_definition` and `apply_persona` in
`src/gobby/mcp_proxy/tools/agents_spawn_tools.py` apply a definition or
persona to the caller's session. `.gobby/plans/apply-agent-definition.md`
may still change them; 0.1 is re-collected before this leaf proves them if
it does.

**Acceptance:**

- 4.4.1 - The apply-definition service returns 0.1's recorded results and
  session effects. test: `crates/gagents/tests/spawn_apply.rs::apply_definition_matches_corpus`.
- 4.4.2 - The apply-persona service returns 0.1's recorded results and
  session effects. test: `crates/gagents/tests/spawn_apply.rs::apply_persona_matches_corpus`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-agents --test spawn_apply`.

## P5: Messaging and coordination
`kind: framing`

### 5.1 Inter-session mailbox [category: code] (depends: 4.4)
`kind: deliverable`

Targets:
- `crates/gagents/src/lib.rs`
- `crates/gagents/src/messaging/mod.rs`
- `crates/gagents/src/messaging/mailbox.rs`
- `crates/gagents/tests/mailbox.rs`

**Research context:** `send_message`, `get_inter_session_message` and
`get_inter_session_messages`
(`src/gobby/mcp_proxy/tools/agent_messaging.py::add_messaging_tools`) run
through `src/gobby/sessions/mailbox.py::MailboxService.send` over
`inter_session_messages` (`src/gobby/storage/inter_session_messages.py`).
Port the target kinds `session`, `parent`, `agent`, `project` and `global`,
fanout withholding of the Orchestrator, `message_recipient_not_allowed`, and
the parent send that records the child's result on `agent_runs` (AF-Q3).
Wake decisions are 5.2's.

**Acceptance:**

- 5.1.1 - The send service reproduces 0.4's recorded rows, recipients and
  refusals for every target kind.
  test: `crates/gagents/tests/mailbox.rs::send_matches_corpus`.
- 5.1.2 - The message read services return 0.4's recorded messages and
  errors. test: `crates/gagents/tests/mailbox.rs::message_reads_match_corpus`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-agents --test mailbox`.

### 5.2 Native wake decisions and terminal delivery [category: code] (depends: 5.1)
`kind: deliverable`

Targets:
- `crates/gagents/src/messaging/mod.rs`
- `crates/gagents/src/messaging/wake.rs`
- `crates/gagents/tests/wake.rs`
- `crates/gdaemon/src/agent_effects.rs`
- `crates/gdaemon/tests/agent_effects.rs`

**Research context:** `src/gobby/sessions/mailbox_delivery.py::dispatch_mailbox_wakes`
calls `src/gobby/events/wake.py::WakeDispatcher` (`wake`,
`dispatch_live_wakes`, `_should_send_live_wake`). Port its decision for
wakes the native mailbox and coordination waits produce (Decision 9): the
`wake` default per target kind, queued delivery for interrupted,
input-waiting and handoff-waiting sessions, and the live-wake choice. For a
terminal on another machine, `WakeDispatcher._composer_blocks_wake` lets
only a reported `empty` composer take the wake and skips the local activity
probe and turn checks; the native decision reads that composer from 2.2's
pane read, so a missing entry reads `unknown` and withholds the wake while
the durable message waits for hook delivery.
gdaemon's effects implement `terminal_write`: a hub-local terminal goes
through the native write path
(`crates/gterminals/src/write/wake_dispatch.rs::dispatch_wake`), and another
machine's terminal receives `terminal.write`. Web-chat sessions are reached
through a `WebChatWake` trait defined in `wake.rs`; 11.2 binds #21592's
runtime to it, before any native caller exists.

**Acceptance:**

- 5.2.1 - Wake decisions match 0.4's recorded outcome for each session state
  and `wake` value, and for a node terminal under each reported composer
  state. test: `crates/gagents/tests/wake.rs::wake_decisions_match_corpus`.
- 5.2.2 - Web-chat wakes go through the seam trait and nowhere else.
  test: `crates/gagents/tests/wake.rs::web_chat_wakes_use_seam`.
- 5.2.3 - The terminal-write effect writes a hub-local terminal through the
  native write path and sends `terminal.write` for another machine.
  test: `crates/gdaemon/tests/agent_effects.rs::terminal_write_routes_local_and_remote`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-agents --test wake`;
`CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon --test agent_effects`.

### 5.3 Coordination waits and their delivery worker [category: code] (depends: 5.2)
`kind: deliverable`

Targets:
- `crates/gagents/src/messaging/mod.rs`
- `crates/gagents/src/messaging/coordination.rs`
- `crates/gagents/src/messaging/coordination_delivery.rs`
- `crates/gagents/tests/coordination.rs`
- `crates/gcore/src/postgres_pool/mod.rs::*` — scope-reason: keep the pool's connect target and declare the listen module
- `crates/gcore/src/postgres_pool/listen.rs`
- `crates/gcore/src/postgres_pool/tests.rs::*` — scope-reason: add the notification listener test beside the pool's database tests

**Research context:** `wait_for_coordination` and `cancel_coordination_wait`
(`src/gobby/mcp_proxy/tools/coordination.py::register_coordination_tools`)
persist through
`src/gobby/storage/coordination_waits.py::CoordinationWaitManager`.
Keep owner-session, reply, timeout and cancel semantics and the
coordination holds `tests/events/test_coordination_holds.py` pins.
`wait_for_agent` is deferred to D1 (Decision 15).

Registration reproduces `CoordinationWaitManager.register`'s transaction:
exactly one of `coordination_key`, `statuses` or `reply`; the key length,
canonical status, timeout and self-owner refusals; a `SELECT ... FOR UPDATE`
on the owner session that fences message and status producers; reuse of the
active wait with the same `condition_key`; and `resolve_coordination_wait`
in the same transaction, so a status already matched resolves at
registration. Cancel reproduces `CoordinationWaitManager.cancel`.
Resolution stays in the schema: the `coordination_message_committed`
trigger on `inter_session_messages` inserts and the
`coordination_owner_changed` trigger on an owner's status change or
deletion resolve waits for writes from either runtime, and the
`coordination_wait_changed` trigger notifies `gobby_coordination_wait`.

Delivery (Decision 9, D-AF-6) ports
`src/gobby/events/coordination_waits.py::CoordinationWaitService` as
`coordination_delivery.rs`. The worker listens on `gobby_coordination_wait`
before it takes the recovery snapshot (`CoordinationWaitManager.pending_ids`:
undelivered waits whose waiter session is on this machine), and repeats both
after every reconnect with a one-second transport backoff. One consumer
serializes processing. Each notified or recovered id is refreshed as
`CoordinationWaitManager.refresh` does, under the waiter-machine join and
with `resolve_coordination_wait`. A `waiting` row is rescheduled for its
`expires_at`. A terminal row wakes its waiter through 5.2's decision with
the fields of `src/gobby/storage/coordination_waits.py::coordination_wait_payload`,
the wait id as `completion_id` and the message `Coordination wait <id>:
<outcome>`, as `src/gobby/events/completion_registry.py::CompletionEventRegistry.wake_sessions`
builds them; `mark_delivered` then stamps the row. A wake that is not
delivered retries after five seconds, and a delivered row is skipped.
Postgres sends a notification only to the connection that listens, and the
spawned connection task behind a pooled client drops it, so
`crates/gcore/src/postgres_pool/listen.rs` adds `Pool::listen`: one
connection outside the pool, opened from the pool's own connect target,
that yields payloads until it closes. Nothing starts the worker until 11.2.

**Acceptance:**

- 5.3.1 - Coordination waits match 0.4's recorded rows and outcomes for a
  keyed release, a status set (including one matched at registration), a
  reply, timeout, owner end and cancel, a duplicate registration and each
  registration refusal.
  test: `crates/gagents/tests/coordination.rs::coordination_waits_match_corpus`.
- 5.3.2 - The delivery worker wakes each terminal wait once: one notified
  after it listens, one completed while it was disconnected or stopped, one
  whose deadline resolves it to `timeout`, and one whose first wake fails
  and is retried. It skips delivered rows and waits whose waiter is on
  another machine.
  test: `crates/gagents/tests/coordination.rs::delivery_worker_matches_python_service`.
- 5.3.3 - A wait registered natively, or by Python's
  `CoordinationWaitManager.register` statements, and released by a native
  mailbox send wakes its waiter exactly once.
  test: `crates/gagents/tests/coordination.rs::mixed_registration_delivers_once`.
- 5.3.4 - `Pool::listen` yields notifications committed after it listens
  and ends when its connection closes.
  test: `crates/gcore/src/postgres_pool/tests.rs::listen_yields_committed_notifications`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-agents --test coordination`;
`CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-core listen_yields_committed_notifications`.

## P6: Termination and cleanup
`kind: framing`

### 6.1 Termination [category: code] (depends: 5.3)
`kind: deliverable`

Targets:
- `crates/gagents/src/lib.rs`
- `crates/gagents/src/termination/mod.rs`
- `crates/gagents/src/termination/terminate.rs`
- `crates/gagents/src/effects.rs`
- `crates/gagents/tests/termination.rs`
- `crates/gdaemon/src/agent_effects.rs`
- `crates/gdaemon/src/hub_launch.rs`
- `crates/gdaemon/tests/agent_effects.rs`
- `crates/gdaemon/tests/hub_launch.rs`

**Research context:** `cancel_agent_run` in
`src/gobby/servers/routes/agents.py`, and `kill_agent`, `stop_agent` and
`end_agent_run` (`agents_lifecycle_tools.py`) and `unregister_agent`
(`agents_query_tools.py`), run through
`src/gobby/mcp_proxy/tools/agent_cancellation.py::terminate_agent_run` and, for a
remote run, `src/gobby/hub/remote_cancel.py::cancel` with `not_stopped`.
Keep the documented `kill` default difference (Debt not to port), terminal
delivery of the result or handoff to the parent through 5.1, and
`unregister_agent`'s reconcile contract. Capture before kill follows
`src/gobby/agents/capture.py::terminate_managed_runtime_async`: the tail is
read through 2.2's pane read and written into the run's result between the
capture markers before the stop, so a remote run's capture comes from the
reported store. gdaemon's effects implement `cancel`: a hub-local run is
stopped through the hub terminal host, and another machine's run receives
`agent.cancel`; after a successful remote cancel the effect marks the entry
stopped through 2.0's `POST`, as `RemoteTerminalRuntime.terminate` does, and
skips that step with a warning when the route is refused or unreachable.

The capture barrier is durable SQL in
`src/gobby/storage/agents/_termination.py`: `record_termination_intent`
updates only a `pending` or `running` run, `replace_capture_slot`
compare-and-sets on `capture_id` and `capture_revision` with the marker
check, and the terminal transition compare-and-sets on status. The
`_capture_lock` in `capture.py` is process-local, so between a Python
terminator and a native one these predicates are the only fence. 6.1
reproduces them on the same columns and stops the terminal only after the
capture persists, as `capture_then_kill_async` does.

Completion handoff (Decision 15, D-AF-7). Every native terminal transition
ends the same way after its commit: delivery to the parent through 5.1,
then the `deliver_completion` effect, which this leaf adds to
`MachineEffects` in `crates/gagents/src/effects.rs`. The effect runs
whether the parent delivery succeeded or failed, as the `finally` in
`terminate_agent_run` runs Python's delivery. gdaemon implements it in
`crates/gdaemon/src/agent_effects.rs` as an in-memory set of run ids and
one task: the effect adds the run id and returns, and the task posts each
id to 2.0's delivery route through 2.2's loopback client and pair secret.
`pending: 0` removes the id, and 409 removes it with a warning. A 503,
retained rows, any other answer or an unreachable route keeps it, and the
task posts it again 5 seconds later (the retry interval of 5.3's worker),
warning once per run. Another machine's run is kept the same way, so a
transient refusal never needs a restart. Stopping gdaemon drops the set;
the fresh Python backend's startup recovery redelivers the hub machine's
runs, and another machine's run waits for its owning machine (Decision
15). The termination services here, 6.2's cleanup and 8.1's
terminations through them, and 4.1's exit observation in
`crates/gdaemon/src/hub_launch.rs` all end this way. Posting after the
commit keeps the order Python's own producers keep, which `wait_for_agent`'s
registration in `src/gobby/mcp_proxy/tools/agents_query_tools.py` relies
on: a waiter either rereads the terminal row itself or receives the
delivery. A repeated post for one run wakes nobody twice, because Python
removes acknowledged subscriber rows and evicts the registry entry after it
notifies.

**Acceptance:**

- 6.1.1 - `HTTP POST /api/agents/runs/{run_id}/cancel` matches Python's
  local and remote cancel results and errors.
  test: `crates/gagents/tests/termination.rs::cancel_route_matches_python`.
- 6.1.2 - The kill, stop, end and unregister services reproduce 0.3's run
  rows, parent deliveries and `not_stopped` outcomes.
  test: `crates/gagents/tests/termination.rs::termination_services_match_corpus`.
- 6.1.3 - The cancel effect stops a hub-local run in process, sends
  `agent.cancel` for another machine and then marks the reported entry
  stopped through 2.0's pane route; the run's capture holds the stored tail.
  test: `crates/gdaemon/tests/agent_effects.rs::cancel_routes_local_and_remote`.
- 6.1.4 - The termination service reproduces 0.3's capture-barrier vectors:
  a persistence failure or a capture conflict stops nothing. A competing
  terminator's capture-slot write, applied to the row between the native
  intent and the native capture, leaves the competitor's capture and stops
  nothing. A retried interrupted stop reaches one terminal outcome and one
  parent delivery.
  test: `crates/gagents/tests/termination.rs::capture_barrier_matches_corpus`.
- 6.1.5 - Each native terminal transition (cancel, kill, stop, end,
  unregister and cleanup) calls `deliver_completion` once after its commit
  and its parent delivery, including when the parent delivery fails.
  test: `crates/gagents/tests/termination.rs::terminal_transitions_hand_off_completion`.
- 6.1.6 - gdaemon's `deliver_completion` posts the run id to 2.0's delivery
  route with the pair secret and keeps it until the route answers
  `pending: 0` or 409. An unreachable route, a 503 and `pending: 1` each
  lead to another post 5 seconds later with no restart, the committed
  transition stays in place, and another machine's run is kept the same
  way. test: `crates/gdaemon/tests/agent_effects.rs::deliver_completion_retries_until_acknowledged`.
- 6.1.7 - A hub-local run's observed exit commits its terminal row, then
  delivers to the parent and calls `deliver_completion` once.
  test: `crates/gdaemon/tests/hub_launch.rs::exit_observation_hands_off_completion`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-agents --test termination`;
`CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon --test agent_effects --test hub_launch`.

### 6.2 Cleanup and worktree checkpoints [category: code] (depends: 6.1)
`kind: deliverable`

Targets:
- `crates/gagents/src/termination/mod.rs`
- `crates/gagents/src/termination/cleanup.rs`
- `crates/gagents/tests/cleanup.rs`

**Research context:** `cleanup_agent_runs` in
`src/gobby/servers/routes/agents.py`, `cancel_stale_helpers`
(`agents_lifecycle_tools.py`, no age threshold, kept) and
`checkpoint_agent_worktree`
(`src/gobby/mcp_proxy/tools/agents_checkpoint_tools.py`). Cleanup and
helper cancellation use 6.1's service; the checkpoint commits through 3.2's
`git.rs`.

**Acceptance:**

- 6.2.1 - `HTTP POST /api/agents/cleanup` matches Python's cleanup counts and
  run rows. test: `crates/gagents/tests/cleanup.rs::cleanup_route_matches_python`.
- 6.2.2 - The stale-helper and checkpoint services reproduce 0.3's run rows
  and repository effects.
  test: `crates/gagents/tests/cleanup.rs::cleanup_services_match_corpus`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-agents --test cleanup`.

## P7: Runbook seats and admin cold start
`kind: framing`

### 7.1 Seats, team admission and admin cold start [category: code] (depends: 6.2)
`kind: deliverable`

Targets:
- `crates/gagents/src/lib.rs`
- `crates/gagents/src/seats.rs`
- `crates/gagents/tests/seats.rs`

**Research context:** `admit_team_tool` and `check_runbook_seats_tool`
(`src/gobby/mcp_proxy/tools/runbook_seat_tools.py`, over
`src/gobby/agents/runbook_seats.py`) and `cold_start_admin`
(`src/gobby/mcp_proxy/tools/admin_cold_start.py::cold_start_admin_impl`).
They read runs and sessions and spawn through 4.3's service. Runbooks v4
(#24818) may still change them; 0.4 is re-collected before this leaf proves
them if it does.

**Acceptance:**

- 7.1.1 - The team admission service reproduces 0.4's recorded seats and
  refusals. test: `crates/gagents/tests/seats.rs::admit_team_matches_corpus`.
- 7.1.2 - The seat check service returns 0.4's recorded seat states.
  test: `crates/gagents/tests/seats.rs::check_runbook_seats_matches_corpus`.
- 7.1.3 - The admin cold-start service reproduces 0.4's recorded spawns and
  results. test: `crates/gagents/tests/seats.rs::cold_start_admin_matches_corpus`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-agents --test seats`.

## P8: Hub pane steps
`kind: framing`

### 8.1 Native hub pane monitor [category: code] (depends: 6.1, #24383, #24149)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/nodes/agents/pane_monitor.rs`
- `crates/gdaemon/src/hub_pane_monitor.rs`
- `crates/gdaemon/src/lib.rs`
- `crates/gdaemon/tests/hub_pane_monitor.rs`

**Research context:** #24383 creates
`crates/gdaemon/src/nodes/agents/pane_monitor.rs`: a 30-second pass over each
launched run that reads its pane with `ControlClient::snapshot` (15 lines for
trust, loop, approval and attention, 30 for queued continuation, 40 in ANSI
mode for periodic Enter, 8 for provider stalls, 50 for the timeout error
tail), runs the detectors in `crates/gdaemon/src/pane_detection/`, writes the
Python key sequences through `ControlClient::write`, reports pane facts and
posts terminate verdicts to the hub. Generalize that pass over a run source
and an action sink (Decision 8). The node keeps its run-state file as the
source and its report and terminate posts as the sink. The hub source is the `agent_runs` rows of
running runs whose terminal is on the hub's machine; the hub sink writes
through the hub terminal host, transitions attention through #24149's native
service and terminates through 6.1's service with the reasons of
`src/gobby/agents/agent_health.py::check_initialization_timeout` and
`check_provider_stalls`. The hub pass runs the eight steps and never the idle
step, which stays in Python for every machine. Python's steps live in
`src/gobby/agents/terminal_prompt_monitor.py`,
`src/gobby/agents/idle_check_handler.py::IdleCheckHandler.check_attention_agents`
and `agent_health.py`. Nothing starts the hub monitor until 11.2.

**Acceptance:**

- 8.1.1 - The node pass keeps its reads, writes, reports and terminate posts
  through the generalized source and sink.
  test: `crates/gdaemon/tests/hub_pane_monitor.rs::node_pass_unchanged_through_generalization`.
- 8.1.2 - For a hub-local run, each of the eight steps takes the action
  Python's step takes for the same pane fixture: the key sequence written,
  the attention transition, or the termination and its reason.
  test: `crates/gdaemon/tests/hub_pane_monitor.rs::hub_steps_match_python`.
- 8.1.3 - The hub pass skips runs on other machines and never runs the idle
  step. test: `crates/gdaemon/tests/hub_pane_monitor.rs::hub_pass_skips_remote_runs_and_idle`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon --test hub_pane_monitor --test node_agents`.

## P9: Internal-tool descriptors
`kind: framing`

### 9.1 gobby-agents descriptor and tool adapters [category: code] (depends: 7.1, #24883, #24970)
`kind: deliverable`

Targets:
- `crates/gagents/src/lib.rs`
- `crates/gagents/src/mcp/mod.rs`
- `crates/gagents/src/mcp/agents.rs`
- `crates/gagents/tests/mcp_agents.rs`

**Research context:** `src/gobby/mcp_proxy/tools/agents_registry.py::create_agents_registry`
registers the 28 tools; their schemas, descriptions and definition hashes
come from 0.1, 0.3 and 0.4. Publish one descriptor with #24883's
`crates/gcore/src/mcp.rs::ToolFamily`; request types derive schemas normalized
to JSON-object equality with Python. Each handler calls the service its
owning leaf built (2.1, 2.2, 4.2 to 4.4, 5.1 to 5.3, 6.1, 6.2, 7.1) and maps
errors through #24970's MCP adapter. P0 records intentional domain failures
only, so no error-redaction waiver applies. The gateway (#24884) supplies
policy, leases, context injection, timeouts and result offloading; the
descriptor is not registered with an active backend here (Decision 4). The
#24883 consumer check runs the whole `--test mcp_contract` target.

**Granularity:** 28 row items and one catalog item exercise one descriptor
and dispatch adapter over services that earlier leaves already prove.

**Acceptance:**

- 9.1.1 - `MCP gobby-agents:admit_team` matches Python. test: `crates/gagents/tests/mcp_agents.rs::admit_team_matches_python`.
- 9.1.2 - `MCP gobby-agents:apply_agent_definition` matches Python. test: `crates/gagents/tests/mcp_agents.rs::apply_agent_definition_matches_python`.
- 9.1.3 - `MCP gobby-agents:apply_persona` matches Python. test: `crates/gagents/tests/mcp_agents.rs::apply_persona_matches_python`.
- 9.1.4 - `MCP gobby-agents:can_spawn_agent` matches Python. test: `crates/gagents/tests/mcp_agents.rs::can_spawn_agent_matches_python`.
- 9.1.5 - `MCP gobby-agents:cancel_coordination_wait` matches Python. test: `crates/gagents/tests/mcp_agents.rs::cancel_coordination_wait_matches_python`.
- 9.1.6 - `MCP gobby-agents:cancel_stale_helpers` matches Python. test: `crates/gagents/tests/mcp_agents.rs::cancel_stale_helpers_matches_python`.
- 9.1.7 - `MCP gobby-agents:check_runbook_seats` matches Python. test: `crates/gagents/tests/mcp_agents.rs::check_runbook_seats_matches_python`.
- 9.1.8 - `MCP gobby-agents:checkpoint_agent_worktree` matches Python. test: `crates/gagents/tests/mcp_agents.rs::checkpoint_agent_worktree_matches_python`.
- 9.1.9 - `MCP gobby-agents:cold_start_admin` matches Python. test: `crates/gagents/tests/mcp_agents.rs::cold_start_admin_matches_python`.
- 9.1.10 - `MCP gobby-agents:end_agent_run` matches Python. test: `crates/gagents/tests/mcp_agents.rs::end_agent_run_matches_python`.
- 9.1.11 - `MCP gobby-agents:evaluate_spawn` matches Python. test: `crates/gagents/tests/mcp_agents.rs::evaluate_spawn_matches_python`.
- 9.1.12 - `MCP gobby-agents:get_agent_capture` matches Python for local and remote runs. test: `crates/gagents/tests/mcp_agents.rs::get_agent_capture_matches_python`.
- 9.1.13 - `MCP gobby-agents:get_agent_live_output` matches Python for local and remote runs. test: `crates/gagents/tests/mcp_agents.rs::get_agent_live_output_matches_python`.
- 9.1.14 - `MCP gobby-agents:get_agent_result` matches Python. test: `crates/gagents/tests/mcp_agents.rs::get_agent_result_matches_python`.
- 9.1.15 - `MCP gobby-agents:get_inter_session_message` matches Python. test: `crates/gagents/tests/mcp_agents.rs::get_inter_session_message_matches_python`.
- 9.1.16 - `MCP gobby-agents:get_inter_session_messages` matches Python. test: `crates/gagents/tests/mcp_agents.rs::get_inter_session_messages_matches_python`.
- 9.1.17 - `MCP gobby-agents:get_running_agent` matches Python. test: `crates/gagents/tests/mcp_agents.rs::get_running_agent_matches_python`.
- 9.1.18 - `MCP gobby-agents:kill_agent` matches Python, including its `stop` default. test: `crates/gagents/tests/mcp_agents.rs::kill_agent_matches_python`.
- 9.1.19 - `MCP gobby-agents:list_agent_runs` matches Python. test: `crates/gagents/tests/mcp_agents.rs::list_agent_runs_matches_python`.
- 9.1.20 - `MCP gobby-agents:list_running_agents` matches Python. test: `crates/gagents/tests/mcp_agents.rs::list_running_agents_matches_python`.
- 9.1.21 - `MCP gobby-agents:running_agent_stats` matches Python. test: `crates/gagents/tests/mcp_agents.rs::running_agent_stats_matches_python`.
- 9.1.22 - `MCP gobby-agents:send_message` matches Python for every target kind. test: `crates/gagents/tests/mcp_agents.rs::send_message_matches_python`.
- 9.1.23 - `MCP gobby-agents:spawn_agent` matches Python for local and remote spawns. test: `crates/gagents/tests/mcp_agents.rs::spawn_agent_matches_python`.
- 9.1.24 - `MCP gobby-agents:stop_agent` matches Python. test: `crates/gagents/tests/mcp_agents.rs::stop_agent_matches_python`.
- 9.1.25 - `MCP gobby-agents:unregister_agent` matches Python. test: `crates/gagents/tests/mcp_agents.rs::unregister_agent_matches_python`.
- 9.1.26 - `MCP gobby-agents:wait_for_agent`'s catalog entry and schema match Python, and its handler returns the `unavailable` class until D1. test: `crates/gagents/tests/mcp_agents.rs::wait_for_agent_schema_matches_python`.
- 9.1.27 - `MCP gobby-agents:wait_for_coordination` matches Python. test: `crates/gagents/tests/mcp_agents.rs::wait_for_coordination_matches_python`.
- 9.1.28 - `MCP gobby-agents:wait_for_output` matches Python. test: `crates/gagents/tests/mcp_agents.rs::wait_for_output_matches_python`.
- 9.1.29 - The tool inventory, descriptions, schemas, definition hashes and
  mutation classes match the frozen Python contract, and malformed arguments
  reach no service. test: `crates/gagents/tests/mcp_agents.rs::catalog_and_validation_match_python`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-agents --test mcp_agents`;
`CARGO_BUILD_JOBS=3 cargo test -p gobby-core --test mcp_contract`.

### 9.2 gobby-worktrees descriptor and tool adapters [category: code] (depends: 9.1, #24883, #24970)
`kind: deliverable`

Targets:
- `crates/gagents/src/mcp/mod.rs`
- `crates/gagents/src/mcp/worktrees.rs`
- `crates/gagents/tests/mcp_worktrees.rs`

**Research context:** `src/gobby/mcp_proxy/tools/worktrees/_factory.py::create_worktrees_registry`
registers the 17 tools over 3.1's and 3.2's services, with schemas and
hashes from 0.2. Hub machine ownership
(`src/gobby/hub/remote_workspaces.py::route_node_workspace_tools`) is part of
each handler's result. Same descriptor rules as 9.1.

**Granularity:** 17 row items and one catalog item exercise one descriptor
over 3.1's and 3.2's services.

**Acceptance:**

- 9.2.1 - `MCP gobby-worktrees:abandon_worktree` matches Python. test: `crates/gagents/tests/mcp_worktrees.rs::abandon_worktree_matches_python`.
- 9.2.2 - `MCP gobby-worktrees:claim_worktree` matches Python. test: `crates/gagents/tests/mcp_worktrees.rs::claim_worktree_matches_python`.
- 9.2.3 - `MCP gobby-worktrees:cleanup_stale_worktrees` matches Python. test: `crates/gagents/tests/mcp_worktrees.rs::cleanup_stale_worktrees_matches_python`.
- 9.2.4 - `MCP gobby-worktrees:create_worktree` matches Python. test: `crates/gagents/tests/mcp_worktrees.rs::create_worktree_matches_python`.
- 9.2.5 - `MCP gobby-worktrees:delete_worktree` matches Python for local and remote rows. test: `crates/gagents/tests/mcp_worktrees.rs::delete_worktree_matches_python`.
- 9.2.6 - `MCP gobby-worktrees:detect_stale_worktrees` matches Python. test: `crates/gagents/tests/mcp_worktrees.rs::detect_stale_worktrees_matches_python`.
- 9.2.7 - `MCP gobby-worktrees:get_worktree` matches Python for local and remote rows. test: `crates/gagents/tests/mcp_worktrees.rs::get_worktree_matches_python`.
- 9.2.8 - `MCP gobby-worktrees:get_worktree_by_task` matches Python. test: `crates/gagents/tests/mcp_worktrees.rs::get_worktree_by_task_matches_python`.
- 9.2.9 - `MCP gobby-worktrees:get_worktree_stats` matches Python. test: `crates/gagents/tests/mcp_worktrees.rs::get_worktree_stats_matches_python`.
- 9.2.10 - `MCP gobby-worktrees:link_task_to_worktree` matches Python. test: `crates/gagents/tests/mcp_worktrees.rs::link_task_to_worktree_matches_python`.
- 9.2.11 - `MCP gobby-worktrees:list_worktrees` matches Python. test: `crates/gagents/tests/mcp_worktrees.rs::list_worktrees_matches_python`.
- 9.2.12 - `MCP gobby-worktrees:mark_worktree_merged` matches Python. test: `crates/gagents/tests/mcp_worktrees.rs::mark_worktree_merged_matches_python`.
- 9.2.13 - `MCP gobby-worktrees:merge_worktree` matches Python for local and remote rows. test: `crates/gagents/tests/mcp_worktrees.rs::merge_worktree_matches_python`.
- 9.2.14 - `MCP gobby-worktrees:push_branch` matches Python. test: `crates/gagents/tests/mcp_worktrees.rs::push_branch_matches_python`.
- 9.2.15 - `MCP gobby-worktrees:reactivate_worktree` matches Python. test: `crates/gagents/tests/mcp_worktrees.rs::reactivate_worktree_matches_python`.
- 9.2.16 - `MCP gobby-worktrees:release_worktree` matches Python. test: `crates/gagents/tests/mcp_worktrees.rs::release_worktree_matches_python`.
- 9.2.17 - `MCP gobby-worktrees:sync_worktree` matches Python. test: `crates/gagents/tests/mcp_worktrees.rs::sync_worktree_matches_python`.
- 9.2.18 - The tool inventory, descriptions, schemas, definition hashes and
  mutation classes match the frozen Python contract, and malformed arguments
  reach no service. test: `crates/gagents/tests/mcp_worktrees.rs::catalog_and_validation_match_python`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-agents --test mcp_worktrees`;
`CARGO_BUILD_JOBS=3 cargo test -p gobby-core --test mcp_contract`.

### 9.3 gobby-clones descriptor and tool adapters [category: code] (depends: 9.2, #24883, #24970)
`kind: deliverable`

Targets:
- `crates/gagents/src/mcp/mod.rs`
- `crates/gagents/src/mcp/clones.rs`
- `crates/gagents/tests/mcp_clones.rs`

**Research context:** `src/gobby/mcp_proxy/tools/clones.py::create_clones_registry`
registers the 13 tools over 3.3's and 3.4's services, with schemas and
hashes from 0.2. Same descriptor rules as 9.1.

**Granularity:** 13 row items and one catalog item exercise one descriptor
over 3.3's and 3.4's services.

**Acceptance:**

- 9.3.1 - `MCP gobby-clones:claim_clone` matches Python. test: `crates/gagents/tests/mcp_clones.rs::claim_clone_matches_python`.
- 9.3.2 - `MCP gobby-clones:cleanup_stale_clones` matches Python. test: `crates/gagents/tests/mcp_clones.rs::cleanup_stale_clones_matches_python`.
- 9.3.3 - `MCP gobby-clones:create_clone` matches Python. test: `crates/gagents/tests/mcp_clones.rs::create_clone_matches_python`.
- 9.3.4 - `MCP gobby-clones:delete_clone` matches Python for local and remote clones. test: `crates/gagents/tests/mcp_clones.rs::delete_clone_matches_python`.
- 9.3.5 - `MCP gobby-clones:detect_stale_clones` matches Python. test: `crates/gagents/tests/mcp_clones.rs::detect_stale_clones_matches_python`.
- 9.3.6 - `MCP gobby-clones:get_clone` matches Python. test: `crates/gagents/tests/mcp_clones.rs::get_clone_matches_python`.
- 9.3.7 - `MCP gobby-clones:get_clone_by_task` matches Python. test: `crates/gagents/tests/mcp_clones.rs::get_clone_by_task_matches_python`.
- 9.3.8 - `MCP gobby-clones:get_clone_stats` matches Python. test: `crates/gagents/tests/mcp_clones.rs::get_clone_stats_matches_python`.
- 9.3.9 - `MCP gobby-clones:link_task_to_clone` matches Python. test: `crates/gagents/tests/mcp_clones.rs::link_task_to_clone_matches_python`.
- 9.3.10 - `MCP gobby-clones:list_clones` matches Python. test: `crates/gagents/tests/mcp_clones.rs::list_clones_matches_python`.
- 9.3.11 - `MCP gobby-clones:merge_clone` matches Python. test: `crates/gagents/tests/mcp_clones.rs::merge_clone_matches_python`.
- 9.3.12 - `MCP gobby-clones:release_clone` matches Python. test: `crates/gagents/tests/mcp_clones.rs::release_clone_matches_python`.
- 9.3.13 - `MCP gobby-clones:sync_clone` matches Python. test: `crates/gagents/tests/mcp_clones.rs::sync_clone_matches_python`.
- 9.3.14 - The tool inventory, descriptions, schemas, definition hashes and
  mutation classes match the frozen Python contract, and malformed arguments
  reach no service. test: `crates/gagents/tests/mcp_clones.rs::catalog_and_validation_match_python`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-agents --test mcp_clones`;
`CARGO_BUILD_JOBS=3 cargo test -p gobby-core --test mcp_contract`.

## P10: Native command trees and the public CLI
`kind: framing`

### 10.1 Native agents command tree [category: code] (depends: 9.3, #24970)
`kind: deliverable`

Targets:
- `crates/gagents/src/lib.rs`
- `crates/gagents/src/cli/mod.rs`
- `crates/gagents/src/cli/agents.rs`
- `crates/gdaemon/src/main.rs::Command`
- `crates/gdaemon/src/main.rs::main`
- `crates/gagents/tests/cli_agents.rs`

**Research context:** `src/gobby/cli/agents.py` (`agents`, `spawn_agent_cmd`,
`agent_runs`, `list_agent_definitions`, `show_agent_definition`,
`list_agent_runs`, `show_agent_run`, `agent_status`, `stop_agent`,
`kill_agent`, `agent_stats`, `cleanup_agents`) and
`src/gobby/cli/agents_steps.py` (`list_agent_steps`, `check_agent`). Add
`gdaemon agents` with one clap subcommand per Python verb and the same
options, output and exit codes; group rows print Python's usage. Each
command reaches the daemon over the route Python's command calls (for
example `spawn` posts to `/api/mcp/gobby-agents/tools/spawn_agent` with
`X-Gobby-CLI-Internal: 1`, `cleanup` posts to `/api/agents/cleanup`), and
reads the database through gagents' services where Python's command reads it
directly, with the URL from `crates/gdaemon/src/main.rs::resolve_database_url`.
`kill` keeps its `stop=False` default (Debt not to port), and `spawn` keeps
the `terminal_backend` refusal that
`tests/agents/test_backend_ingress.py::test_every_public_spawn_ingress_rejects_backend`
pins. CLI errors go through #24970's CLI adapter.

**Granularity:** 14 row items share one command tree, one daemon client and
the read services earlier leaves prove.

**Acceptance:**

- 10.1.1 - `CLI gobby agents` matches Python's group usage and exit code.
  test: `crates/gagents/tests/cli_agents.rs::agents_group_matches_python`.
- 10.1.2 - `CLI gobby agents check` matches Python. test: `crates/gagents/tests/cli_agents.rs::check_matches_python`.
- 10.1.3 - `CLI gobby agents cleanup` matches Python. test: `crates/gagents/tests/cli_agents.rs::cleanup_matches_python`.
- 10.1.4 - `CLI gobby agents kill` matches Python, including the `--stop`
  default. test: `crates/gagents/tests/cli_agents.rs::kill_matches_python`.
- 10.1.5 - `CLI gobby agents list` matches Python. test: `crates/gagents/tests/cli_agents.rs::list_matches_python`.
- 10.1.6 - `CLI gobby agents runs` matches Python's group usage. test: `crates/gagents/tests/cli_agents.rs::runs_group_matches_python`.
- 10.1.7 - `CLI gobby agents runs list` matches Python. test: `crates/gagents/tests/cli_agents.rs::runs_list_matches_python`.
- 10.1.8 - `CLI gobby agents runs show` matches Python. test: `crates/gagents/tests/cli_agents.rs::runs_show_matches_python`.
- 10.1.9 - `CLI gobby agents show` matches Python. test: `crates/gagents/tests/cli_agents.rs::show_matches_python`.
- 10.1.10 - `CLI gobby agents spawn` matches Python, including the
  `terminal_backend` refusal. test: `crates/gagents/tests/cli_agents.rs::spawn_matches_python`.
- 10.1.11 - `CLI gobby agents stats` matches Python. test: `crates/gagents/tests/cli_agents.rs::stats_matches_python`.
- 10.1.12 - `CLI gobby agents status` matches Python. test: `crates/gagents/tests/cli_agents.rs::status_matches_python`.
- 10.1.13 - `CLI gobby agents steps` matches Python. test: `crates/gagents/tests/cli_agents.rs::steps_matches_python`.
- 10.1.14 - `CLI gobby agents stop` matches Python. test: `crates/gagents/tests/cli_agents.rs::stop_matches_python`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-agents --test cli_agents`;
`cargo check -p gobby-daemon`.

### 10.2 Native worktrees command tree [category: code] (depends: 10.1)
`kind: deliverable`

Targets:
- `crates/gagents/src/cli/mod.rs`
- `crates/gagents/src/cli/worktrees.rs`
- `crates/gdaemon/src/main.rs::Command`
- `crates/gdaemon/src/main.rs::main`
- `crates/gagents/tests/cli_worktrees.rs`

**Research context:** `src/gobby/cli/worktrees.py` (`worktrees`,
`create_worktree`, `list_worktrees`, `show_worktree`, `delete_worktree`,
`claim_worktree`, `release_worktree`, `sync_worktree`, `detect_stale`,
`cleanup_worktrees`, `worktree_stats`). Mutations post to
`/api/mcp/gobby-worktrees/tools/<tool>` through `_call_worktree_tool`; `list`,
`show` and reference resolution read the database directly through
`worktree_manager_context` (kept, Debt not to port). Add `gdaemon worktrees`
with the same verbs, options, output and exit codes.

**Granularity:** 11 row items share one command tree and 10.1's daemon
client.

**Acceptance:**

- 10.2.1 - `CLI gobby worktrees` matches Python's group usage and exit code.
  test: `crates/gagents/tests/cli_worktrees.rs::worktrees_group_matches_python`.
- 10.2.2 - `CLI gobby worktrees claim` matches Python. test: `crates/gagents/tests/cli_worktrees.rs::claim_matches_python`.
- 10.2.3 - `CLI gobby worktrees cleanup` matches Python. test: `crates/gagents/tests/cli_worktrees.rs::cleanup_matches_python`.
- 10.2.4 - `CLI gobby worktrees create` matches Python. test: `crates/gagents/tests/cli_worktrees.rs::create_matches_python`.
- 10.2.5 - `CLI gobby worktrees delete` matches Python. test: `crates/gagents/tests/cli_worktrees.rs::delete_matches_python`.
- 10.2.6 - `CLI gobby worktrees list` matches Python's direct read. test: `crates/gagents/tests/cli_worktrees.rs::list_matches_python`.
- 10.2.7 - `CLI gobby worktrees release` matches Python. test: `crates/gagents/tests/cli_worktrees.rs::release_matches_python`.
- 10.2.8 - `CLI gobby worktrees show` matches Python's direct read. test: `crates/gagents/tests/cli_worktrees.rs::show_matches_python`.
- 10.2.9 - `CLI gobby worktrees stale` matches Python. test: `crates/gagents/tests/cli_worktrees.rs::stale_matches_python`.
- 10.2.10 - `CLI gobby worktrees stats` matches Python. test: `crates/gagents/tests/cli_worktrees.rs::stats_matches_python`.
- 10.2.11 - `CLI gobby worktrees sync` matches Python. test: `crates/gagents/tests/cli_worktrees.rs::sync_matches_python`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-agents --test cli_worktrees`.

### 10.3 Native clones command tree and route waivers [category: code] (depends: 10.2, #24963)
`kind: deliverable`

Targets:
- `crates/gagents/src/cli/mod.rs`
- `crates/gagents/src/cli/clones.rs`
- `crates/gdaemon/src/main.rs::Command`
- `crates/gdaemon/src/main.rs::main`
- `crates/gagents/tests/cli_clones.rs`
- `docs/contracts/parity-waivers.json::*` — scope-reason: add only the five clone CLI route waiver entries and retain every other entry

**Research context:** `src/gobby/cli/clones.py` (`clones`, `list_clones`,
`create_clone`, `spawn_agent`, `sync_clone`, `merge_clone`, `delete_clone`).
Five commands post to the unserved `{daemon_url}/mcp/...` route (Debt not to
port); the native commands post to `/api/mcp/gobby-clones/tools/<tool>` and
`/api/mcp/gobby-agents/tools/spawn_agent`. Add the five waiver entries of the
Debt section's inventory to `docs/contracts/parity-waivers.json`, replacing
0.2's test-only entries.

**Granularity:** Seven row items and the waiver item share one command tree;
the waivers describe exactly five of its rows.

**Acceptance:**

- 10.3.1 - `CLI gobby clones` matches Python's group usage and exit code.
  test: `crates/gagents/tests/cli_clones.rs::clones_group_matches_python`.
- 10.3.2 - `CLI gobby clones create` succeeds through the served route under
  `agents.cli-clones-create.daemon-tool-route`.
  test: `crates/gagents/tests/cli_clones.rs::create_uses_served_route`.
- 10.3.3 - `CLI gobby clones delete` succeeds through the served route under
  `agents.cli-clones-delete.daemon-tool-route`.
  test: `crates/gagents/tests/cli_clones.rs::delete_uses_served_route`.
- 10.3.4 - `CLI gobby clones list` matches Python. test: `crates/gagents/tests/cli_clones.rs::list_matches_python`.
- 10.3.5 - `CLI gobby clones merge` succeeds through the served route under
  `agents.cli-clones-merge.daemon-tool-route`.
  test: `crates/gagents/tests/cli_clones.rs::merge_uses_served_route`.
- 10.3.6 - `CLI gobby clones spawn` succeeds through the served route under
  `agents.cli-clones-spawn.daemon-tool-route`.
  test: `crates/gagents/tests/cli_clones.rs::spawn_uses_served_route`.
- 10.3.7 - `CLI gobby clones sync` succeeds through the served route under
  `agents.cli-clones-sync.daemon-tool-route`.
  test: `crates/gagents/tests/cli_clones.rs::sync_uses_served_route`.
- 10.3.8 - The five waiver entries carry their exact surfaces and the
  decision `.gobby/plans/agents-family.md#10.3`, and readiness reports each
  as `Waived` with passing native assertions beside ordinary `Passed` cases.
  test: `crates/gagents/tests/cli_clones.rs::clone_route_waivers_are_exact`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-agents --test cli_clones`.

### 10.4 Forward the public CLI groups and retire Python callbacks [category: code] (depends: 11.1)
`kind: deliverable`

Targets:
- `src/gobby/cli/agents.py::*` — scope-reason: replace the command implementations with a thin Click group whose subcommands each forward one verb to `gdaemon agents`
- `src/gobby/cli/agents_steps.py::*` — operation: delete — scope-reason: retire the steps and check callbacks that `gdaemon agents` now serves
- `src/gobby/cli/worktrees.py::*` — scope-reason: replace the command implementations with a thin Click group whose subcommands each forward one verb to `gdaemon worktrees`
- `src/gobby/cli/clones.py::*` — scope-reason: replace the command implementations with a thin Click group whose subcommands each forward one verb to `gdaemon clones`
- `src/gobby/cli/_native_forward.py`
- `tests/cli/test_agents_coverage.py::*` — scope-reason: replace callback mocks with forwarding checks and corpus ownership
- `tests/cli/test_agent_definitions_cli.py::*` — scope-reason: move definition-command expectations to the corpus and native tests
- `tests/cli/test_agents_steps.py::*` — operation: delete — scope-reason: retire tests of the deleted steps module
- `tests/cli/test_worktrees_cli.py::*` — scope-reason: move worktree-command expectations to the corpus and native tests
- `tests/cli/test_worktrees_coverage.py::*` — scope-reason: replace callback mocks with forwarding checks
- `tests/cli/test_cli_worktrees_coverage.py::*` — scope-reason: replace callback mocks with forwarding checks
- `tests/cli/test_clones_cli.py::*` — scope-reason: move clone-command expectations to the corpus and native tests
- `tests/agents/test_backend_ingress.py::test_every_public_spawn_ingress_rejects_backend`
- `tests/cli/test_agents_native_forwarding.py`

Consumers unchanged:
- `src/gobby/cli/__init__.py` — no-edit-reason: registers `agents`, `worktrees` and `clones` lazily by module and attribute name, which the thin groups keep.

**Research context:** Decision 5: one forwarding subcommand per verb keeps
every `CLI gobby ...` key in `scripts/generate_parity_ledger.py::cli_verbs`.
`_native_forward.py` holds the one forwarding function the three groups
share: it disables option parsing for forwarded arguments and runs the
installed gdaemon, resolved by
`src/gobby/utils/native_bin.py::resolve_native_bin`, with the group, the
verb, the literal arguments, the working directory and inherited streams,
returning gdaemon's exit status and keeping the missing-binary diagnostic.
This leaf follows 11.1, so the released gdaemon already carries the three
command trees. Importers of the retired code are the listed tests and
`agents.py` itself, which imports `agents_steps.py`; sweep imports, re-exports
and patch strings before deleting. The backend-ingress test keeps its other
ingress cases, and its CLI case asserts the forwarded command's refusal.

**Granularity:** Five production targets make one ownership switch for
three groups that share one forwarding function; the test targets migrate
or retire the callback tests that switch obsoletes, and the 32 row items of
10.1 to 10.3 already prove the forwarded behavior.

**Acceptance:**

- 10.4.1 - Each forwarding subcommand passes literal arguments, working
  directory and exit status to `gdaemon <group> <verb>`, and the groups import
  no agents, worktree or clone business module.
  test: `tests/cli/test_agents_native_forwarding.py::test_groups_only_forward`.
- 10.4.2 - `scripts/generate_parity_ledger.py::cli_verbs` yields exactly the
  32 owned `CLI gobby agents|worktrees|clones` keys.
  test: `tests/cli/test_agents_native_forwarding.py::test_ledger_keeps_every_cli_verb`.
- 10.4.3 - Every retired callback expectation maps to a corpus vector and a
  native row test, and sweeps find no reference to the deleted module.
  test: `tests/cli/test_agents_native_forwarding.py::test_retired_callbacks_have_native_coverage`.
- 10.4.4 - Every public spawn ingress, including the forwarded CLI command,
  still refuses `terminal_backend`.
  test: `tests/agents/test_backend_ingress.py::test_every_public_spawn_ingress_rejects_backend`.

Planned verification: the forwarding and migrated CLI tests with the
protected Python prefix; scoped Ruff and mypy on `src/gobby/cli/`; a
changed-test type audit.

## P11: Activation and ledger
`kind: framing`

### 11.1 checkouts activation, admission binding and coherent release [category: code] (depends: 10.3, #25046, #24963)
`kind: deliverable`

Targets:
- `crates/gagents/src/admission.rs`
- `crates/gdaemon/src/serve.rs::*` — scope-reason: pass #25046's native admission into the pool-gated agents and checkouts composition of both cfg-gated serve entry points
- `crates/gdaemon/Cargo.toml`
- `crates/gcode/Cargo.toml`
- `crates/ghook/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `crates/gdaemon/tests/checkouts_cutover.rs`
- `tests/e2e/test_checkouts_native.py`
- `crates/gagents/tests/fixtures/parity_checkouts.json`
- `docs/contracts/parity-ledger-status.d/git-workspaces.json::*` — scope-reason: set only the nine checkouts HTTP records to delegated and rewrite the 30 checkouts MCP notes, retaining every other record
- `docs/contracts/parity-ledger.md`
- `docs/contracts/agents-native.md`

Consumers unchanged:
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: dynamically reads its unchanged native version pin.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: dynamically reads its bumped native version pin.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: dynamically reads the bumped gdaemon pin.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: dynamically reads its bumped native version pin.
- `src/gobby/cli/install_setup_gmcp.py` — no-edit-reason: dynamically reads its unchanged native version pin.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: dynamically reads unchanged native version pins.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: resolves every pin dynamically by binary name.
- `src/gobby/code_index/gcode_versions.py` — no-edit-reason: both gcode maintenance floors derive from the bumped gcode pin.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: the hook compatibility floor derives from the bumped ghook pin.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: enumerates the map dynamically.
- `src/gobby/install/distribution.py` — no-edit-reason: resolves minimum versions dynamically.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: derives expected ghook versions from the map.
- `tests/cli/test_install_ghook.py` — no-edit-reason: derives expected versions from the map.
- `tests/cli/test_install_setup.py` — no-edit-reason: derives expected versions from the map.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: derives expected versions from the map.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: derives expected versions from the map.
- `tests/cli/test_install_setup_gmcp.py` — no-edit-reason: derives expected versions from the map.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: derives expected versions from the map.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: derives gcode expectations from the bumped pin.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: derives ghook expectations from the bumped pin.
- `tests/install/test_distribution.py` — no-edit-reason: dynamically parameterizes managed version expectations.
- `tests/install/test_version_pins.py` — no-edit-reason: already verifies each managed pin against its Cargo manifest.

**Research context:** Bind 1.1's `AdmissionClass::OperatorOrBrowser` to
#25046's reusable native admission for both families: API keys, the local
operator and ordinary and managed browser cookies are admitted, and agent
tokens and missing credentials are refused as 0.1 to 0.3 record. Activation
selects `front_door.routes.checkouts` through the existing bootstrap
mechanism; Proxy stays selectable for rollback, and `agents` stays on
Proxy. Compare runs only the read rows (`GET
/api/source-control/worktrees`, `GET /api/source-control/worktrees/stats`,
`GET /api/source-control/clones`); effectful rows are proven by 3.5's tests
and the isolated e2e run, never run twice (S3). Consume #24963's readiness
result with ordinary `Passed` cases.

Corpus freshness gates the cutover. From the integration checkout, rerun
`scripts/agents_parity_corpus.py` for 0.2's checkouts group against reset
isolated state and compare the result with the committed fixture. A
byte-identical result passes. A difference is committed here with the
refreshed fixture once every native proof that reads the group (3.1 to
3.5, 9.2, 9.3, 10.2 and 10.3) passes against it; a difference that a native
proof fails stops the activation and goes to the Dev Manager as a blocker
naming the failing proof. Record the integration SHA and the fixture hash.

Publication goes with the cutover, so the ledger never reports a native
row as proxied. Set the nine `checkouts` HTTP rows of `git-workspaces.json`
to `delegated` with notes citing their owning leaf, native tests and this
activation, and rewrite the 30 checkouts MCP notes to cite 9.2 or 9.3 and
`Registry flip: #21570` with status `proxied`. The 18 checkouts CLI rows
stay `proxied` until 10.4 forwards them; 11.3 records them. Regenerate
`docs/contracts/parity-ledger.md` with `uv run python
scripts/generate_parity_ledger.py`; a second run leaves it unchanged.

This is the first shipping commit since 1.1 added the `crates/gagents`
member to the root `Cargo.toml`.
`src/gobby/install/bin_release_identity.py::workspace_source_identity`
hashes the root manifest for every managed binary, so gdaemon, gcode and
ghook each take a 0.0.1 bump above their preceding released version, with
manifests, lock and pins updated. Rebuild gclient, gmcp and gterm alongside;
unpublished binaries keep their versions and use the existing component
promotion paths. Promote the coherent trio through
`promote_workspace_binary_set` and read installed hashes after signing. Only
the Orchestrator delegates cutover, with a global announcement, a quiet
window, a passing source, schema and installed-set preflight, and a health
report. No push is authorized here.

**Granularity:** The targets and items belong to one admission binding and
one family-specific activation and release, with the ledger rows that
activation makes native.

**Acceptance:**

- 11.1.1 - Both families admit API keys, the local operator and browser
  cookies through #25046 and refuse agent tokens and missing credentials as
  Python does. test: `crates/gdaemon/tests/checkouts_cutover.rs::admission_matches_python`.
- 11.1.2 - Proxy, compare and native states affect only the nine
  `checkouts` rows; compare runs read rows only, effectful rows run once, and
  rollback to Proxy preserves state.
  test: `crates/gdaemon/tests/checkouts_cutover.rs::checkouts_cutover_is_scoped_and_single_effect`.
- 11.1.3 - With the Python backend unavailable, the nine rows run natively on
  hub and standalone, and a node keeps its current backend.
  test: `tests/e2e/test_checkouts_native.py::test_checkouts_rows_work_without_python_backend`.
- 11.1.4 - Readiness rejects zero-Passed, all-Skipped and unwaived-Skipped
  reports and unknown or stale waivers.
  test: `crates/gdaemon/tests/checkouts_cutover.rs::waiver_readiness_gates_checkouts_activation`.
- 11.1.5 - Shipping manifests, lock and pins agree at the required patch
  increment. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.
- 11.1.6 - The runbook records build and installed hashes, compared cases,
  the rollback command, the announcement, health and native smoke evidence.
  behavior: "checkouts activation evidence" in `docs/contracts/agents-native.md`.
- 11.1.7 - The checkouts corpus rerun from the integration checkout matches
  the committed fixture, or the refreshed fixture is committed with every
  native proof that reads it passing; the integration SHA and fixture hash
  are recorded. behavior: "checkouts corpus freshness" in `docs/contracts/agents-native.md`.
- 11.1.8 - The nine `checkouts` HTTP rows are `delegated`, the 30 checkouts
  MCP rows `proxied` with `Registry flip: #21570`, and every other record is
  unchanged. behavior: "checkouts activation parity rows" in `docs/contracts/parity-ledger-status.d/git-workspaces.json`.
- 11.1.9 - The regenerated ledger matches the shards and the public
  inventory. test: `tests/test_parity_ledger.py::test_committed_ledger_matches_current_public_inventory`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon --test checkouts_cutover`;
the isolated e2e test and the ledger test with the protected Python prefix;
the corpus rerun; the ledger generator run twice with no second-run diff;
the targeted version-pin check; an admitted release build and
Orchestrator-directed cutover. These are execution gates, not results
observed while drafting.

### 11.2 agents activation, hub pane steps and gdaemon release [category: code] (depends: 10.4, 8.1, #24149, #21592)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/serve.rs::*` — scope-reason: start the hub pane monitor beside the hub terminal host, start the coordination-wait worker and bind #21592's web-chat wake seam in both cfg-gated serve entry points
- `src/gobby/runner_lifecycle.py::run_daemon`
- `tests/e2e/readiness_bootstrap.py::*` — scope-reason: drop the coordination listener readiness step that Python no longer starts
- `tests/test_runner_coordination_delivery.py`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `src/gobby/agents/lifecycle_monitor.py::*` — operation: delete-lines — base-blob: 7bfb8e1ec7cc32af3853ed3e57bfc79a4f80a3d1 — lines: 401-406, 411, 413 — scope-reason: remove from the monitor check loop the eight pane-step calls that the native hub monitor now runs
- `src/gobby/agents/idle_check_handler.py::IdleCheckHandler._handle_idle_check`
- `tests/agents/test_lifecycle_monitor.py::*` — scope-reason: assert that `_check_loop` no longer schedules the eight pane steps and still schedules the idle check and sweeps
- `tests/agents/test_attention_state.py::*` — scope-reason: replace the local idle path's attention-sync expectation with native monitor ownership
- `crates/gdaemon/tests/agents_cutover.rs`
- `tests/e2e/test_agents_native.py`
- `crates/gagents/tests/fixtures/parity_definitions_runs.json`
- `crates/gagents/tests/fixtures/parity_launch.json`
- `crates/gagents/tests/fixtures/parity_messaging.json`
- `docs/contracts/agents-native.md`

Consumers unchanged:
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: dynamically reads its unchanged native version pin.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: dynamically reads its unchanged native version pin.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: dynamically reads the bumped gdaemon pin.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: dynamically reads its unchanged native version pin.
- `src/gobby/cli/install_setup_gmcp.py` — no-edit-reason: dynamically reads its unchanged native version pin.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: dynamically reads unchanged native version pins.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: resolves every pin dynamically by binary name.
- `src/gobby/code_index/gcode_versions.py` — no-edit-reason: both gcode maintenance floors derive from the unchanged gcode pin.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: the hook compatibility floor derives from the unchanged ghook pin.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: enumerates the map dynamically.
- `src/gobby/install/distribution.py` — no-edit-reason: resolves minimum versions dynamically.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: derives expected ghook versions from the map.
- `tests/cli/test_install_ghook.py` — no-edit-reason: derives expected versions from the map.
- `tests/cli/test_install_setup.py` — no-edit-reason: derives expected versions from the map.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: derives expected versions from the map.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: derives expected versions from the map.
- `tests/cli/test_install_setup_gmcp.py` — no-edit-reason: derives expected versions from the map.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: derives expected versions from the map.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: derives gcode expectations from the unchanged pin.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: derives ghook expectations from the unchanged pin.
- `tests/install/test_distribution.py` — no-edit-reason: dynamically parameterizes managed version expectations.
- `tests/install/test_version_pins.py` — no-edit-reason: already verifies each managed pin against its Cargo manifest.
- `tests/agents/test_idle_check_transcript_paths.py` — no-edit-reason: patches the idle check whole and asserts transcript paths, not attention sync.
- `tests/agents/test_lifecycle_monitor_extra.py` — no-edit-reason: asserts idle outcomes that the attention-sync change leaves unchanged.
- `tests/hub/test_remote_idle_policy.py` — no-edit-reason: covers remote runs, whose attention sync is unchanged.
- `src/gobby/runner.py` — no-edit-reason: calls run_daemon with an unchanged signature and keeps the optional service attribute.
- `tests/test_runner_lifecycle.py` — no-edit-reason: calls run_daemon with an unchanged signature and never names the coordination service.
- `tests/test_runner_pid_file.py` — no-edit-reason: calls run_daemon with an unchanged signature and asserts PID file handling only.

**Research context:** Activation selects `front_door.routes.agents`; the
node report and run-outcome routes stay Python-served (Decision 14), and
2.0's pane route serves remote pane reads from Python's store. Compare runs only the read rows
(the three definition reads, the four run reads and `GET
/api/agents/launch-defaults`); spawn, cancel, cleanup, preview and definition
writes are proven by their leaves' stub tests and the isolated e2e run (S3).
Bind #21592's runtime to 5.2's `WebChatWake` seam.

Decision 8 moves the eight pane steps for hub-local runs in this one
shipping commit. serve.rs starts 8.1's monitor on hub and standalone with a
pool, never on a node. In the Python `_check_loop`, lines 401 to 406 call
the trust, loop, approval, queued-continuation, periodic-Enter and
attention steps, line 411 the initialization timeout and line 413 provider
stalls; the typed proof deletes exactly those lines and leaves line 412,
`check_idle_agents`, and every sweep. Without the attention pass, the idle
pass captures each local pane itself, as it already does when the attention
cache is empty. `IdleCheckHandler._handle_idle_check` then stops calling
`self._attention_tracker.sync` for a local run, because 8.1 owns local
attention; a remote run keeps its sync. The Python step implementations stay
unscheduled until global Python removal.

Decision 9 (D-AF-6) moves coordination-wait delivery in the same commit.
serve.rs starts 5.3's worker on hub and standalone with a pool, never on a
node, and `run_daemon` stops constructing and starting
`CoordinationWaitService`; the shutdown path already skips a service that
was never set. `tests/e2e/readiness_bootstrap.py` drops its
`CoordinationWaitService.start` readiness step. Route-only rollback leaves
the native worker delivering, because Python no longer starts its service;
full rollback restores Python's. Decision 15's delivery route is live from
2.0 onward, so every native terminal transition reaches Python's waiters
once the `agents` family is active.

Rollback has two scopes, and the runbook names both. Route-only rollback
selects Proxy for `front_door.routes.agents`; the 21 rows return to Python,
and the native monitor keeps the eight pane steps because Python no longer
schedules them. Full rollback restores Python ownership of the pane steps:
revert this shipping commit, reinstall the matching Python checkout and the
coherent gdaemon set through `promote_workspace_binary_set`, and run an
Orchestrator-delegated cutover. After the forward cutover and after a full
rollback rehearsed against isolated state, the evidence shows exactly one
owner scheduling each pane step.

Corpus freshness gates the cutover as in 11.1, for the groups of 0.1, 0.3
and 0.4: rerun the collector from the integration checkout against reset
isolated state; a byte-identical result passes; a difference is committed
with the refreshed fixture once every native proof that reads that group
passes against it; a difference a native proof fails stops the activation
and goes to the Dev Manager as a blocker naming the failing proof. Record
the integration SHA and the fixture hashes.

gdaemon changed since 11.1, so gdaemon takes a 0.0.1 bump; no leaf after
11.1 changes the source identity of gcode or ghook, whose pins stay. Promote
and cut over as in 11.1.

**Granularity:** The route activation, the pane-step move, the
coordination-delivery move and the release form one shipping state: the
Python steps and Python's coordination listener may stop only in the commit
that starts their native owners.

**Acceptance:**

- 11.2.1 - Proxy, compare and native states affect only the 21 `agents`
  rows; compare runs read rows only, effectful rows run once, and rollback
  to Proxy preserves state.
  test: `crates/gdaemon/tests/agents_cutover.rs::agents_cutover_is_scoped_and_single_effect`.
- 11.2.2 - With the Python backend unavailable, the 21 rows run natively on
  hub and standalone, remote pane reads fall back to a missing entry, and a
  node keeps its current backend.
  test: `tests/e2e/test_agents_native.py::test_agents_rows_work_without_python_backend`.
- 11.2.3 - The hub pane monitor starts only on hub and standalone with a
  pool, and web-chat wakes reach #21592's runtime.
  test: `crates/gdaemon/tests/agents_cutover.rs::pane_monitor_and_web_chat_wake_are_bound`.
- 11.2.4 - `_check_loop` no longer schedules the eight pane steps and still
  schedules the idle check and every sweep.
  test: `tests/agents/test_lifecycle_monitor.py::test_check_loop_leaves_pane_steps_to_native_monitor`.
- 11.2.5 - The idle check no longer syncs attention for a local run and
  still syncs it for a remote run.
  test: `tests/agents/test_attention_state.py::test_idle_check_syncs_attention_only_for_remote_runs`.
- 11.2.6 - Readiness rejects zero-Passed, all-Skipped and unwaived-Skipped
  reports and unknown or stale waivers.
  test: `crates/gdaemon/tests/agents_cutover.rs::waiver_readiness_gates_agents_activation`.
- 11.2.7 - Shipping manifests, lock and pins agree at the required patch
  increment. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.
- 11.2.8 - The runbook records build and installed hashes, compared cases,
  the route-only and full rollback sequences, the announcement, health and
  native smoke evidence, including a hub-local run whose trust prompt the
  native monitor answers and a remote run whose live output and capture come
  from the reported store.
  behavior: "agents activation evidence" in `docs/contracts/agents-native.md`.
- 11.2.9 - After the forward cutover and after a rehearsed full rollback,
  exactly one owner schedules each of the eight pane steps.
  behavior: "pane step ownership after cutover and rollback" in `docs/contracts/agents-native.md`.
- 11.2.10 - The agents corpus groups rerun from the integration checkout
  match the committed fixtures, or the refreshed fixtures are committed with
  every native proof that reads them passing; the integration SHA and
  fixture hashes are recorded.
  behavior: "agents corpus freshness" in `docs/contracts/agents-native.md`.
- 11.2.11 - The coordination-wait worker starts only on hub and standalone
  with a pool. test: `crates/gdaemon/tests/agents_cutover.rs::coordination_worker_is_bound`.
- 11.2.12 - Python startup no longer constructs or starts
  `CoordinationWaitService`, and shutdown still completes.
  test: `tests/test_runner_coordination_delivery.py::test_startup_leaves_coordination_delivery_to_gdaemon`.
- 11.2.13 - Against the isolated daemon pair, a Python `wait_for_agent`
  wakes once when the run ends by native cancel, by hub-local exit or by a
  native monitor termination, whether it registered before or after the
  transition; when the first wake fails, the durable subscriber row stays
  and gdaemon's next post delivers it once with both daemons still
  running. A Python-registered coordination
  wait released by a native send wakes once.
  test: `tests/e2e/test_agents_native.py::test_python_waiters_wake_once_across_runtimes`.
- 11.2.14 - After the forward cutover and after a rehearsed full rollback,
  exactly one owner delivers coordination waits.
  behavior: "coordination delivery ownership after cutover and rollback" in `docs/contracts/agents-native.md`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon --test agents_cutover --test hub_pane_monitor`;
the three Python tests and the isolated e2e tests with the protected Python
prefix; scoped Ruff and mypy; a changed-test type audit; the corpus rerun;
the targeted version-pin check; an admitted release build, a full-rollback
rehearsal against isolated state and Orchestrator-directed cutover. These
are execution gates, not results observed while drafting.

### 11.3 Publish the family's parity ownership [category: docs] (depends: 11.2)
`kind: deliverable`

Targets:
- `docs/contracts/parity-ledger-status.d/sessions-agents.json::*` — scope-reason: update only the 63 agents family status records and retain every other record
- `docs/contracts/parity-ledger-status.d/git-workspaces.json::*` — scope-reason: set only the 18 checkouts CLI records to delegated and retain every other record
- `docs/contracts/parity-ledger-status.d/comms-cron-hub-nodes.json::*` — scope-reason: rewrite only the four node report and run-outcome notes and retain every other record
- `docs/contracts/parity-ledger-status.d/websocket-chat-voice-ui.json::*` — scope-reason: rewrite only the three workspace WS notes to cite #21565 and retain every other record
- `docs/contracts/parity-ledger.md`
- `docs/contracts/agents-native.md`
- `docs/guides/agents.md`
- `docs/guides/worktrees.md`

**Research context:** `scripts/generate_parity_ledger.py` accepts
`proxied`, `delegated` and `dropped by decision #N`; objects carry exactly
`status` and `note`. 11.1 already published the nine checkouts HTTP rows
and the 30 checkouts MCP notes. Set the 21 HTTP and 14 CLI rows of
`sessions-agents.json` and the 18 CLI rows of `git-workspaces.json` to
`delegated` with notes citing the owning leaf, its native tests and any
waiver ID. Keep the 28 agents MCP rows `proxied` with notes citing 9.1 and
`Registry flip: #21570`; the `wait_for_agent` note also says it stays
proxied until D1 (Decision 15). Keep the four node rows `proxied` with notes
saying the agents family (#21564) owns them, that they stay Python-served
until the reconciler and the idle check go native (Decision 14), and that
#25269 owns that port. Rewrite the three workspace WS notes to cite #21565
alone. Regenerate `docs/contracts/parity-ledger.md` with `uv
run python scripts/generate_parity_ledger.py`; a second run leaves it
unchanged. The guides say that gdaemon serves the agents and checkouts
routes and the hub pane steps, and that the public groups forward to
`gdaemon agents|worktrees|clones`, with their last-verified dates updated.

**Granularity:** Four shard files, the regenerated ledger and three
documents record one ownership state; splitting them would publish a ledger
that disagrees with its shards.

**Acceptance:**

- 11.3.1 - The 35 HTTP and CLI rows are `delegated` and the 28 MCP rows
  `proxied` with `Registry flip: #21570`, and every other record is
  unchanged. behavior: "63 agents family parity rows" in `docs/contracts/parity-ledger-status.d/sessions-agents.json`.
- 11.3.2 - The 18 checkouts CLI rows are `delegated`, and every other
  record, including 11.1's nine HTTP and 30 MCP records, is unchanged.
  behavior: "57 checkouts family parity rows" in `docs/contracts/parity-ledger-status.d/git-workspaces.json`.
- 11.3.3 - The four node rows stay `proxied` with notes citing this
  family's ownership, Decision 14's condition and #25269, and every other
  record is unchanged.
  behavior: "4 node report and run-outcome rows" in `docs/contracts/parity-ledger-status.d/comms-cron-hub-nodes.json`.
- 11.3.4 - The three workspace WS notes cite #21565 alone with status
  unchanged. behavior: "3 re-homed workspace WS rows" in `docs/contracts/parity-ledger-status.d/websocket-chat-voice-ui.json`.
- 11.3.5 - The regenerated ledger matches the shards and the public
  inventory. test: `tests/test_parity_ledger.py::test_committed_ledger_matches_current_public_inventory`.
- 11.3.6 - The final row ownership map is recorded.
  behavior: "agents family row ownership" in `docs/contracts/agents-native.md`.
- 11.3.7 - The agents guide describes native operation, the hub pane steps
  and the forwarding CLI. behavior: "native agents operation" in `docs/guides/agents.md`.
- 11.3.8 - The worktrees guide describes native operation and the
  forwarding CLI. behavior: "native worktrees and clones operation" in `docs/guides/worktrees.md`.

Planned verification: `uv run python scripts/generate_parity_ledger.py` run
twice with no second-run diff; `tests/test_parity_ledger.py` with the
protected Python prefix.

## E1 End-to-end verification
`kind: verification`

The acceptance items own the 120 ported rows, one item per row. HTTP, 30:
1.1 owns the three definition reads, 1.2 the eight definition writes, 2.1
the four run reads, 3.5 the nine source-control routes, 4.2 launch defaults
and prompt preview, 4.3 spawn and batch spawn, 6.1 run cancel and 6.2
cleanup. MCP, 58: 9.1 owns the 28 `gobby-agents` tools, 9.2 the 17
`gobby-worktrees` tools and 9.3 the 13 `gobby-clones` tools. CLI, 32: 10.1
owns the 14 agents commands, 10.2 the 11 worktrees commands and 10.3 the
seven clones commands, and 10.4 forwards the public groups to them. Per
shard: `sessions-agents.json` 63 (21 HTTP, 28 MCP, 14 CLI) and
`git-workspaces.json` 57 (9 HTTP, 30 MCP, 18 CLI). The four node report and
run-outcome rows in `comms-cron-hub-nodes.json` are owned without a port:
11.3.3 records them, and #25269 owns their port (Decision 14). 0.1 to 0.4
freeze the Python contract, and 0.4.3 rejects missing, extra or overlapping
keys.

Service leaves support the row owners: 2.0 (the loopback reported-pane
route), 2.2 (live output and gdaemon's effects), 3.1 to 3.4 (checkout
records, git effects and machine ownership), 4.1 (hub-local launch), 4.4
(apply definition and persona), 5.1 to 5.3 (mailbox, wake decisions,
coordination waits), 7.1 (seats) and 8.1 (hub pane monitor). Native reads
of a pane on another machine go through 2.0's pane route, so remote live output,
capture before kill, the wake composer gate and the stopped mark match
Python. Every HTTP claim fails closed until 11.1 binds #25046's admission;
`checkouts` activates in 11.1 with its HTTP and MCP ledger rows, and
`agents` in 11.2, with both corpus freshness gates. The hub pane monitor
starts only in 11.2, the commit that removes the eight Python steps, and
11.2 records both rollback scopes. 11.3 records the remaining 53 rows
`delegated`, the 28 agents MCP rows `proxied` with `Registry flip: #21570`,
the four node rows `proxied` under Decision 14 and the three re-homed WS
notes. The three private route rows 2.0 adds stay `proxied`: the pane rows
until #25269 and the delivery row until D1. 5.3's worker delivers
coordination waits from 11.2 on, and every native terminal transition hands
its completion to Python's registry (Decisions 9 and 15).

The #24963 readiness gate requires ordinary `Passed` cases and exactly
cited, native-asserted `Waived` cases for the five clone CLI waivers, and
fails skipped-only and unwaived reports. #24970 supplies the shared error
classes and adapters. Shared target files are dependency-ordered throughout,
and no section leaves a product choice to its executor.

Draft validation command:
`uv run gobby plans validate .gobby/plans/agents-family.md -p /Users/josh/Projects/gobby`.
Expansion validation is unrun for the narrative draft and becomes mandatory
after the Adversary derives and applies M1. No implementation tests,
production cutover or task expansion have been performed during planning.

## D1 Native completion delivery and wait_for_agent (depends: 11.2)
`kind: deferred`

`wait_for_agent`, pipelines and durable subscribers wait in Python's
in-process `CompletionEventRegistry`, and Python's terminal producers (the
idle check, the stale sweeps and remote run outcomes) stay in Python past
this plan (Decisions 8, 14 and 15). Native completion delivery can replace
the registry only once no Python producer remains; until then native
terminal transitions reach it through 2.0's delivery route. The
Orchestrator's D-AF-7 ruling keeps this work with #21564 beside the
run-lifecycle rows.

```yaml
deferral:
  task_ref: "created-at-expansion"
  reason: "A native wait_for_agent has no wake source while Python's idle check, stale sweeps and remote run outcomes still end runs; the Orchestrator's D-AF-7 ruling keeps native completion delivery with #21564 beside the run-lifecycle rows."
  owner: "orchestrator"
  original_acceptance_items:
    - D1.1
    - D1.2
```

- D1.1 - Native `wait_for_agent` returns 0.4's recorded completion,
  failure and timeout outcomes and serves `MCP gobby-agents:wait_for_agent`.
- D1.2 - Native completion delivery wakes in-memory waiters and durable
  subscribers for every terminal producer, and `POST
  /_gobby/agent-run-delivery` is retired with its ledger row.

## V1 Plan Changelog
`kind: framing`

- 2026-10-28 — Initial narrative by Plan Writer gobby#18292 for #25255;
  awaiting enhancement by gobby#18298 and adversarial review by gobby#18295;
  no consensus claimed.
- 2026-10-28 — Consensus. Plan Writer gobby#18292 and Plan Adversary
  gobby#18295 agree on this narrative as of a4b54aeee1. The Enhancer
  gobby#18298's edits were applied as the coordinator dispositioned them.
  AF-ADV-001 (no owner for coordination-wait delivery) is resolved by the
  Orchestrator's D-AF-6 ruling: 5.3 ports the delivery worker and 11.2
  moves ownership in one shipping commit (Decision 9). AF-ADV-002 (native
  terminal transitions never reach Python's completion waiters) is resolved
  by the D-AF-7 ruling and its follow-up: 2.0's loopback delivery route
  acknowledges by rereading durable subscriber rows, gdaemon reposts until
  acknowledged, the handoff runs whether parent delivery succeeded or
  failed, and
  `wait_for_agent` stays proxied until D1 (Decision 15). The machine-owner
  recovery boundary from `.gobby/plans/completed/gdaemon-run-modes.md`
  (acceptance 2.2.8) is retained: another machine's run whose handoff is
  pending across a hub restart waits for its owning machine's startup
  recovery. AF-ADV-003 (capture barrier untested) is resolved by 0.3.5 and
  6.1.4. No blocking finding or open decision remains, and base validation
  passes. The adversary derives `## M1 Task Manifest` from the commit that
  adds this entry. Consensus is not implementation approval; operator
  approval precedes expansion.
