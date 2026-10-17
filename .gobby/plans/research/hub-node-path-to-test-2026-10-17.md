# Hub-node: the task tree from today to ready-to-test

Orchestrator gobby#15977, Saturday 2026-10-17, about 15:45 CDT. Task #24648 (Hub-node path-to-test task tree report).

## Bottom line

"Ready to test hub-node" here means two things. First, the live proof **#24604** (Live hub-to-node proof with isolated daemons and fake providers, under **#24593** P5: Live hub-to-node proof) can run. Second, **#21579** (Rust node duties) can close.

The dependency edges pull in **78 open tasks**, **65** of them leaves:

| Status | Leaves |
| --- | --- |
| Blocked | 56 |
| In review or landed with close pending | 5 |
| In progress | 2 |
| Ready to start | 2 |

The ready leaves are #24381 (Detection manifest engine) and #24350 (`gobby-mcp` crate and its daemon dialer). The landed or in-review leaves are #24368 (Node hook ingress arm), #24270 (Node routes: checkout registration and run reports), #24595 (Callers act only on machines they own), #24596 (Node-commands contract version 3) and #24582 (Node ledger prune test).

The hub-mode and node-half plans are fully expanded. **Correction, 16:05 CDT:** #23275 (`gobby-mcp` crate) needs no planning. Its plan took your four rulings, reached consensus and was expanded into #24350 through #24360 on 10-13. Its `needs-planning` flag and escalation were stale bookkeeping; both are now cleared. Only #21586 and #23276 still need planning.

**Two tasks the edges pull in from outside hub-node still need planning before the path can finish.** Expanding #21586 would create new tasks, and the close cap you chose blocks that for now (option B: no new tasks until the close backlog is under 20).

## Tasks that need planning or expansion

1. **#21586** (Migrate code-index orchestration and shared dependencies): an epic with **no children**. It needs a plan and an expansion.
   - Why it's on the path: #24380 (Node code-index preflight), in S2.7 node half P2, is blocked by it.
   - Its parent #21568 (Remaining route families) is itself gated on #21558 (Native WS transport in gdaemon) and #21560 (Tasks family). #21560 is gated on #21559 (Config, runtime handshake, and grant issuing), which has about 30 open leaves.
   - So as the edges stand, hub-node's test date waits on most of the rest of Stage 2.
2. **#23276** (gcode index writes from a node need hub HTTP routes): escalated `needs-planning`, deferred behind the same code-index family gate. #24380 is also blocked by it.
3. **#23275** (`gobby-mcp` crate: native MCP transports with OAuth and CIMD): no planning needed (see the correction above).
   - It is already planned and expanded: leaves #24350 through #24360 carry full specs from the approved gmcp-stdio plan. The placeholder acceptance text belongs only to the #23275 holder itself.
   - Why it's on the path: two S2.7 node-half leaves wait on its leaves. #24372 (Worktree and clone preparation) and #24375 (Provider launch, exits and host reconciliation) wait on #24355 (Every launch site starts `~/.gobby/bin/gmcp`). #24373 (SRT policy for node runs) waits on #24357 (The sandbox drops the wrapper's read grant).
   - That puts six gmcp leaves on hub-node's critical path: #24350 → #24351 → #24352 → #24354 → #24358 → #24355.

**#21579** (Rust node duties) has no children of its own and needs no expansion: it closes when #24148 and #24150 close. Every other task on the path is an expanded leaf with a written spec.

## Decisions for you

1. **Do #24380's two code-index edges belong on the hub-node path?**
   - **Keep them:** #21586 and #23276 need planning and expansion first, and the live proof also waits on the Native WS, Tasks family and Config chains.
   - **Narrow them:** #24380's node code-index preflight moves to a follow-up leaf after the live proof, and the path shortens by the whole code-index family.
   - **My recommendation is to narrow.** The live proof runs with fake providers and doesn't need node-side indexing.
2. **Do #24372, #24373 and #24375 need gmcp first?**
   - **Keep them:** #23275's plan refresh and approval come first.
   - **Narrow them:** the node runs keep the current Python MCP wrapper for the proof, and gmcp lands on its own schedule.
   - **I recommend keeping these edges** if the plan refresh can be approved this week, because the sandbox read-grant change is real security work. Otherwise narrow them.
3. **Should the close cap exempt hub-node planning?** Option B blocks new tasks, so expanding #21586, or splitting #24380 into a follow-up, waits until the backlog is under 20. I recommend exempting planning and expansion on the hub-node path only.
4. **Scope check.** Through #21579 → #24150, the path includes the whole #23277 subtree (Hook envelope carries edited-file content for disk-reading rules): P1 node checkout reads and P2 hub sites read through the seam, 12 leaves. Its P3 #24532 (Live node-origin checkout reads) runs after the live proof. Please confirm #23277 belongs in "ready to test". If it doesn't, #24604 can drop its #24517/#24518 edges and the path loses 12 leaves.

## Critical path to the live proof (#24604)

This is the longest leaf chain, 21 steps, not counting #21586's unplanned work:

#24350 (`gobby-mcp` crate and its daemon dialer) → #24351 (`gmcp` serves the wrapper's tool surface over stdio) → #24352 (`call_tool` keeps the wrapper's canonicalization) → #24354 (E2e and server tests drive `gmcp`) → #24358 (`gmcp` starts a stopped local daemon) → #24355 (Every launch site starts gmcp) → #24372 (Worktree and clone preparation, repair and removal) → #24373 (SRT policy for node runs) → #24374 (SRT install verification and the launch chokepoint) → #24375 (Provider launch, exits and host reconciliation) → #24376 (Command dispatch over the node channel) → #24377 (Node reports) → #24378 (Checkout registration) → #24379 (Codex prompt delivery) → #24380 (Node code-index preflight) → #24383 (Node pane monitor) → #24384 (Node sandbox reaping and records) → #24597 (Node workspace.merge and workspace.remove handlers) → #24601 (Node checkout.status handler) → #24521 (Node `checkout.read` handler) → #24604 (Live hub-to-node proof).

The second spine starts at the just-landed ingress arm: #24368 (Node hook ingress arm) → #24369 (Node spool drain, boot release and retention) → #24370 (Durable node agent state) → #24594 (gdaemon internal node-command route) and #24371 (Node terminal host) → #24372, where it joins the first chain.

## In flight right now

| Task | State |
| --- | --- |
| #24368 Node hook ingress arm | Landed at cac3ed622b (gdaemon 0.4.52). Its cutover activation runs after this report; the focused retest and close follow. |
| #24270 Node routes: checkout registration and run reports | Landed (f0a600e55c), close pending. |
| #24595 Callers act only on machines they own | In review at 6059529d0e; its 24-file rerun is waiting on a test slot. This one unblocks the most hub-mode leaves. |
| #24596 Node-commands contract version 3 | Landed (f770f3317d). Its close waits on the #24642 close gate 10 fix. |
| #24582 Node ledger prune test | Reviewer LAND; waiting on its activation receipt, then close. |
| #24584 Node receipt sweep | Final-source clippy and nextest waiting on test slots. |
| #24594 gdaemon internal node-command route | Claimed, blocked behind #24370. |

## Edits made today while building this report

- **Fixed a dependency cycle.** #24604 (Live hub-to-node proof) depended on the whole #24150 epic (S2.11 node half). Inside #24150, #24532 (Live node-origin checkout reads) depends on #24604, so neither could ever close. #24604 now depends on #24361 (P1: Node hook ingress), #24517 (P1: Node checkout reads) and #24518 (P2: Hub sites read through the seam) instead. #21579's own edge to #24150 is unchanged.
- **Closed two superseded hub-mode holders as duplicates.** #24280 (Remote workspace lifecycle, plan section D1) and #24282 (Machine-scoped project view, D3) are now covered by the P4 #24592 leaves #24596 through #24603.

## Full tree on the path

The tree lists only open tasks. "Blocked by" names only open blockers. The status snapshot is from about 15:10 CDT, updated for later landings. Statuses mean:
- **ready:** no open blockers, unclaimed.
- **in progress:** claimed.
- **in review:** a candidate was sent to a reviewer.
- **landed:** merged to 0.5.0, close pending.
- **blocked:** waiting on the listed tasks.

### End points

- **#24593** P5: Live hub-to-node proof. Status: open container (0/1 children closed). Planning: none: expanded.
  - **#24604** Live hub-to-node proof with isolated daemons and fake providers. Status: blocked; blocked by #24602 (Hub projects and source-control status take machine_id), #24279 (Attention respond on another machine's entry), #24595 (Callers act only on machines they own), #24599 (REST workspace routes list by machine and finish through...), #24276 (The hub settles remote runs from the node's reports), #24600 (Remote workspace expiry), #24594 (gdaemon internal node-command route over the node channel), #24601 (Node checkout.status handler), #24148 (S2.7 node half: node-local agent spawn and worktrees in g...), #24275 (Remote spawn goes through the node), #24597 (Node workspace.merge and workspace.remove handlers), #24361 (P1: Node hook ingress (#24150)), #24517 (P1: Node checkout reads), #24518 (P2: Hub sites read through the seam). Planning: none: expanded leaf.
- **#21579** Rust node duties. Status: open, no children; blocked by #24148 (S2.7 node half: node-local agent spawn and worktrees in g...), #24150 (S2.11 node half: gdaemon node-local envelope ledger forwa...). Planning: none: no children of its own; closes when #24148 and #24150 close.

### Hub mode #21575 (Hub mode: the datastore owner that runs everything database-backed), phases P1-P4

- **#24265** P1: Hub-node interface. Status: open container (2/5 children closed). Planning: none: expanded.
  - **#24270** Node routes: checkout registration and run reports. Status: landed (f0a600e55c), close pending. Planning: none: expanded leaf.
  - **#24594** gdaemon internal node-command route over the node channel. Status: in progress (claimed, blocked); blocked by #24370 (Durable node agent state). Planning: none: expanded leaf.
  - **#24595** Callers act only on machines they own. Status: in review (6059529d0e; 24-file rerun waiting on a test slot). Planning: none: expanded leaf.
- **#24266** P2: Machine-correct hub loops and remote callers. Status: open container (3/7 children closed). Planning: none: expanded.
  - **#24274** Remote cancel goes through the node. Status: blocked; blocked by #24595 (Callers act only on machines they own). Planning: none: expanded leaf.
  - **#24273** Remote terminal writes go through the node. Status: blocked; blocked by #24595 (Callers act only on machines they own). Planning: none: expanded leaf.
  - **#24275** Remote spawn goes through the node. Status: blocked; blocked by #24274 (Remote cancel goes through the node), #24595 (Callers act only on machines they own), #24386 (Hub spawn payload builder). Planning: none: expanded leaf.
  - **#24276** The hub settles remote runs from the node's reports. Status: blocked; blocked by #24275 (Remote spawn goes through the node), #24270 (Node routes: checkout registration and run reports). Planning: none: expanded leaf.
- **#24267** P3: Machine-aware roster and attention. Status: open container (2/3 children closed). Planning: none: expanded.
  - **#24279** Attention respond on another machine's entry. Status: blocked; blocked by #24595 (Callers act only on machines they own), #24273 (Remote terminal writes go through the node). Planning: none: expanded leaf.
- **#24592** P4: Remote workspaces and the machine-scoped project view. Status: open container (0/8 children closed). Planning: none: expanded.
  - **#24596** Node-commands contract version 3: workspace and checkout commands. Status: landed (f770f3317d), close waits on the #24642 close gate 10 fix. Planning: none: expanded leaf.
  - **#24598** Hub remote workspace module and tool routing. Status: blocked; blocked by #24595 (Callers act only on machines they own), #24270 (Node routes: checkout registration and run reports), #24596 (Node-commands contract version 3: workspace and checkout...). Planning: none: expanded leaf.
  - **#24597** Node workspace.merge and workspace.remove handlers. Status: blocked; blocked by #24596 (Node-commands contract version 3: workspace and checkout...), #24384 (Node sandbox reaping and records). Planning: none: expanded leaf.
  - **#24602** Hub projects and source-control status take machine_id. Status: blocked; blocked by #24598 (Hub remote workspace module and tool routing), #24595 (Callers act only on machines they own). Planning: none: expanded leaf.
  - **#24599** REST workspace routes list by machine and finish through the node. Status: blocked; blocked by #24598 (Hub remote workspace module and tool routing), #24595 (Callers act only on machines they own). Planning: none: expanded leaf.
  - **#24600** Remote workspace expiry. Status: blocked; blocked by #24598 (Hub remote workspace module and tool routing). Planning: none: expanded leaf.
  - **#24601** Node checkout.status handler. Status: blocked; blocked by #24597 (Node workspace.merge and workspace.remove handlers), #24596 (Node-commands contract version 3: workspace and checkout...), #24384 (Node sandbox reaping and records). Planning: none: expanded leaf.

### S2.7 node half #24148 (node-local agent spawn and worktrees in gdaemon)

- **#24148** S2.7 node half: node-local agent spawn and worktrees in gdaemon. Status: open container (0/2 children closed); blocked by #24361 (P1: Node hook ingress (#24150)). Planning: none: expanded.
  - **#24362** P2: Node agent duties (#24148). Status: open container (0/15 children closed). Planning: none: expanded.
    - **#24370** Durable node agent state. Status: blocked; blocked by #24369 (Node spool drain, boot release and retention). Planning: none: expanded leaf.
    - **#24381** Detection manifest engine. Status: ready. Planning: none: expanded leaf.
    - **#24371** Node terminal host. Status: blocked; blocked by #24370 (Durable node agent state). Planning: none: expanded leaf.
    - **#24382** Pane detectors. Status: blocked; blocked by #24381 (Detection manifest engine). Planning: none: expanded leaf.
    - **#24372** Worktree and clone preparation, repair and removal. Status: blocked; blocked by #24355 (Every launch site starts `~/.gobby/bin/gmcp`), #24371 (Node terminal host). Planning: none: expanded leaf.
    - **#24373** SRT policy for node runs. Status: blocked; blocked by #24372 (Worktree and clone preparation, repair and removal), #24357 (The sandbox drops the wrapper's read grant). Planning: none: expanded leaf.
    - **#24374** SRT install verification and the launch chokepoint. Status: blocked; blocked by #24373 (SRT policy for node runs). Planning: none: expanded leaf.
    - **#24375** Provider launch, exits and host reconciliation. Status: blocked; blocked by #24355 (Every launch site starts `~/.gobby/bin/gmcp`), #24374 (SRT install verification and the launch chokepoint), #24381 (Detection manifest engine). Planning: none: expanded leaf.
    - **#24376** Command dispatch over the node channel. Status: blocked; blocked by #24594 (gdaemon internal node-command route over the node channel), #24375 (Provider launch, exits and host reconciliation). Planning: none: expanded leaf.
    - **#24377** Node reports. Status: blocked; blocked by #24376 (Command dispatch over the node channel). Planning: none: expanded leaf.
    - **#24378** Checkout registration. Status: blocked; blocked by #24368 (Node hook ingress arm), #24377 (Node reports). Planning: none: expanded leaf.
    - **#24379** Codex prompt delivery. Status: blocked; blocked by #24389 (Hub routes for launch outcomes), #24378 (Checkout registration). Planning: none: expanded leaf.
    - **#24380** Node code-index preflight. Status: blocked; blocked by #24379 (Codex prompt delivery), #24389 (Hub routes for launch outcomes), #23276 (gcode index writes from a node need hub HTTP routes), #21586 (Migrate code-index orchestration and shared dependencies). Planning: none: expanded leaf.
    - **#24383** Node pane monitor. Status: blocked; blocked by #24391 (Detection manifest route), #24390 (Terminate route), #24380 (Node code-index preflight), #24382 (Pane detectors). Planning: none: expanded leaf.
    - **#24384** Node sandbox reaping and records. Status: blocked; blocked by #24383 (Node pane monitor), #24393 (Node sandbox records on the hub). Planning: none: expanded leaf.
  - **#24363** P3: Contract version 2 and its hub consumers (#24148). Status: open container (1/9 children closed). Planning: none: expanded.
    - **#24386** Hub spawn payload builder. Status: blocked; blocked by #24270 (Node routes: checkout registration and run reports). Planning: none: expanded leaf.
    - **#24387** Node pane state on the hub. Status: blocked; blocked by #24270 (Node routes: checkout registration and run reports). Planning: none: expanded leaf.
    - **#24393** Node sandbox records on the hub. Status: blocked; blocked by #24387 (Node pane state on the hub). Planning: none: expanded leaf.
    - **#24388** Remote terminal runtime. Status: blocked; blocked by #24274 (Remote cancel goes through the node), #24273 (Remote terminal writes go through the node), #24387 (Node pane state on the hub). Planning: none: expanded leaf.
    - **#24389** Hub routes for launch outcomes. Status: blocked; blocked by #24386 (Hub spawn payload builder), #24388 (Remote terminal runtime). Planning: none: expanded leaf.
    - **#24392** Hub idle policy for node runs. Status: blocked; blocked by #24388 (Remote terminal runtime). Planning: none: expanded leaf.
    - **#24390** Terminate route. Status: blocked; blocked by #24276 (The hub settles remote runs from the node's reports), #24389 (Hub routes for launch outcomes). Planning: none: expanded leaf.
    - **#24391** Detection manifest route. Status: blocked; blocked by #24390 (Terminate route). Planning: none: expanded leaf.

### S2.11 node half #24150 (node-local envelope ledger forwarding hooks to the hub)

- **#24150** S2.11 node half: gdaemon node-local envelope ledger forwarding hooks to the hub. Status: open container (0/2 children closed). Planning: none: expanded.
  - **#23277** Hook envelope carries edited-file content for disk-reading rules. Status: open container (0/3 children closed). Planning: none: expanded.
    - **#24517** P1: Node checkout reads. Status: open container (0/4 children closed). Planning: none: expanded.
      - **#24520** Node-commands contract version 4: `checkout.read`. Status: blocked; blocked by #24596 (Node-commands contract version 3: workspace and checkout...). Planning: none: expanded leaf.
      - **#24521** Node `checkout.read` handler. Status: blocked; blocked by #24601 (Node checkout.status handler), #24520 (Node-commands contract version 4: `checkout.read`). Planning: none: expanded leaf.
      - **#24522** Hub checkout-read seam and delivery origin. Status: blocked; blocked by #24594 (gdaemon internal node-command route over the node channel), #24520 (Node-commands contract version 4: `checkout.read`). Planning: none: expanded leaf.
      - **#24523** Node-origin git runs on the node. Status: blocked; blocked by #24522 (Hub checkout-read seam and delivery origin). Planning: none: expanded leaf.
    - **#24518** P2: Hub sites read through the seam. Status: open container (0/8 children closed). Planning: none: expanded.
      - **#24525** Path primitives and write scope use node facts. Status: blocked; blocked by #24523 (Node-origin git runs on the node). Planning: none: expanded leaf.
      - **#24528** Code navigation reads node files. Status: blocked; blocked by #24523 (Node-origin git runs on the node). Planning: none: expanded leaf.
      - **#24529** Node edits record the node's checkout, and ledger owners are machine-scoped. Status: blocked; blocked by #24522 (Hub checkout-read seam and delivery origin). Planning: none: expanded leaf.
      - **#24524** Node events take their project from the node's checkout rows. Status: blocked; blocked by #24523 (Node-origin git runs on the node), #24270 (Node routes: checkout registration and run reports). Planning: none: expanded leaf.
      - **#24526** Monolith guard reads node files. Status: blocked; blocked by #24522 (Hub checkout-read seam and delivery origin). Planning: none: expanded leaf.
      - **#24531** Node events skip hub-only effects. Status: blocked; blocked by #24522 (Hub checkout-read seam and delivery origin). Planning: none: expanded leaf.
      - **#24527** Rust TDD evidence reads node files. Status: blocked; blocked by #24522 (Hub checkout-read seam and delivery origin). Planning: none: expanded leaf.
      - **#24530** Shell classification reads node executables. Status: blocked; blocked by #24525 (Path primitives and write scope use node facts). Planning: none: expanded leaf.
    - **#24519** P3: Live proof. Status: open container (0/1 children closed). Planning: none: expanded.
      - **#24532** Live node-origin checkout reads. Status: blocked; blocked by #24525 (Path primitives and write scope use node facts), #24521 (Node `checkout.read` handler), #24604 (Live hub-to-node proof with isolated daemons and fake pro...), #24524 (Node events take their project from the node's checkout rows), #24526 (Monolith guard reads node files). Planning: none: expanded leaf.
  - **#24361** P1: Node hook ingress (#24150). Status: open container (4/8 children closed). Planning: none: expanded.
    - **#24368** Node hook ingress arm. Status: landed (cac3ed622b, gdaemon 0.4.52), cutover activation and close pending. Planning: none: expanded leaf.
    - **#24369** Node spool drain, boot release and retention. Status: blocked; blocked by #24368 (Node hook ingress arm). Planning: none: expanded leaf.
    - **#24582** Node ledger prune test runs 2-4 minutes on a production-size fixture. Status: reviewer LAND at a501fa570c; escalated hand-off waiting on its activation receipt, then close. Planning: none: expanded leaf.
    - **#24584** Node receipt sweep survives one bad inbox entry. Status: in progress (final-source clippy and nextest waiting on test slots). Planning: none: expanded leaf.

### Pulled in from outside hub-node by dependency edges

- **#23275** `gobby-mcp` crate: native MCP transports with OAuth and CIMD. Status: open container (0/11 children closed), escalated; blocked by #21570 (MCP front door flip). Planning: none: planned and expanded on 10-13; the stale needs-planning flag and escalation were cleared at 16:05.
  - **#24350** `gobby-mcp` crate and its daemon dialer. Status: ready. Planning: none: expanded leaf from the approved gmcp-stdio plan.
  - **#24351** `gmcp` serves the wrapper's tool surface over stdio. Status: blocked; blocked by #24350 (`gobby-mcp` crate and its daemon dialer). Planning: none: expanded leaf from the approved gmcp-stdio plan.
  - **#24353** `gobby install` builds, promotes, and tracks `gmcp`. Status: blocked; blocked by #24351 (`gmcp` serves the wrapper's tool surface over stdio). Planning: none: expanded leaf from the approved gmcp-stdio plan.
  - **#24352** `call_tool` keeps the wrapper's canonicalization, wait guard, and heartbeat. Status: blocked; blocked by #24351 (`gmcp` serves the wrapper's tool surface over stdio). Planning: none: expanded leaf from the approved gmcp-stdio plan.
  - **#24354** E2e and server tests drive `gmcp` or the routes, not the wrapper. Status: blocked; blocked by #24352 (`call_tool` keeps the wrapper's canonicalization, wait gu...). Planning: none: expanded leaf from the approved gmcp-stdio plan.
  - **#24358** `gmcp` starts a stopped local daemon at launch. Status: blocked; blocked by #24353 (`gobby install` builds, promotes, and tracks `gmcp`), #24354 (E2e and server tests drive `gmcp` or the routes, not the...). Planning: none: expanded leaf from the approved gmcp-stdio plan.
  - **#24355** Every launch site starts `~/.gobby/bin/gmcp`. Status: blocked; blocked by #24358 (`gmcp` starts a stopped local daemon at launch), #24353 (`gobby install` builds, promotes, and tracks `gmcp`), #24352 (`call_tool` keeps the wrapper's canonicalization, wait gu...), #24354 (E2e and server tests drive `gmcp` or the routes, not the...). Planning: none: expanded leaf from the approved gmcp-stdio plan.
  - **#24357** The sandbox drops the wrapper's read grant. Status: blocked; blocked by #24355 (Every launch site starts `~/.gobby/bin/gmcp`). Planning: none: expanded leaf from the approved gmcp-stdio plan.
- **#21586** Migrate code-index orchestration and shared dependencies. Status: open, no children. Planning: NEEDS PLANNING AND EXPANSION: epic with no children; parent #21568 is gated on #21558 Native WS and #21560 Tasks family.
- **#23276** gcode index writes from a node need hub HTTP routes. Status: blocked, escalated; blocked by #21586 (Migrate code-index orchestration and shared dependencies). Planning: NEEDS PLANNING (escalated needs-planning: deferred behind the Stage 2 native code-index family gate, #21586).
