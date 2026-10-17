# Hub-node: the task tree from today to ready-to-test

Orchestrator gobby#15977, Saturday 2026-10-17, about 15:45 CDT. Task #24648 (Hub-node path-to-test task tree report).

Updated Sat Oct 17 17:16:45 CDT 2026 by Orchestrator gobby#15977. Statuses are live as of this update.

Session references carry the `gobby#` prefix (gobby#15977 is the Orchestrator, gobby#16767 the Plan Writer). Every bare #N is a task.

## Bottom line

"Ready to test hub-node" here means two things. First, the live proof **#24604** (Live hub-to-node proof with isolated daemons and fake providers, under **#24593** P5: Live hub-to-node proof) can run. Second, **#21579** (Rust node duties) can close.

The path has two scope roots under **#21542** (Gobby 1.0: gdaemon front door, strangler port, hub and node):
- **#21546** (Stage 4: hub and node live, story B) holds hub mode #21575 and its P1-P5 phases.
- **#21544** (Stage 2: strangler absorption of the Python daemon behind the front door) holds the node halves #24148 and #24150, and #23275 (`gobby-mcp` crate).

**Counting rule.** The headline counts every entry in **Full tree on the path**: open tasks that #24604 or #21579 reach through dependency or parent edges. Closed tasks and the entries under **Other tasks this report mentions** are excluded. That tree has **80 open tasks**: 15 phase or epic containers, #21579 (a holder with no children), and **64 leaves**:

| Status | Leaves |
| --- | --- |
| Blocked | 54 |
| In review or landed with close pending | 5 |
| In progress | 2 |
| Ready to start | 2 |
| Needs planning | 1 |

- Ready: #24350 (`gobby-mcp` crate and its daemon dialer) and #24369 (Node spool drain, boot release and retention), which opened when #24368 (Node hook ingress arm) closed.
- In progress: #24381 (Detection manifest engine) and #24594 (gdaemon internal node-command route over the node channel).
- In review or landed: #24270 (Node routes: checkout registration and run reports), #24595 (Callers act only on machines they own), #24596 (Node-commands contract version 3), #24582 (Node ledger prune test) and #24584 (Node receipt sweep).
- Needs planning: #23276 (gcode index writes from a node need hub HTTP routes).

Every task this report names has a full entry (title, status, open blockers and planning need): tasks on the path in **Full tree on the path**, and the rest in **Other tasks this report mentions** at the end.

The hub-mode and node-half plans are fully expanded. **Correction, about 15:50 CDT:** #23275 (`gobby-mcp` crate) needs no planning. Its plan took your four rulings, reached consensus and was expanded on 10-13 into #24350 (`gobby-mcp` crate and its daemon dialer) through #24360 (OAuth 2.1 authorization for the HTTP transport). Its `needs-planning` flag and escalation were stale bookkeeping; both are now cleared. One of those leaves, #24360, keeps its own `needs-planning` label (deferred by gmcp-stdio decision D3); it is not on the hub-node path.

**One task on the path still needs planning: #23276.** Your "Drop links" ruling removed #21586 (Migrate code-index orchestration and shared dependencies) from the path.

## Tasks that need planning or expansion

1. **#23276** (gcode index writes from a node need hub HTTP routes): escalated `needs-planning`, now with no open blockers. #24380 (Node code-index preflight), in S2.7 node half P2, is blocked by it.
   - You ruled the node code-index routes native-narrow. Plan Writer gobby#16767 is drafting them in `.gobby/plans/code-index-family.md`; its P1 is reviewed first.
2. **#21586** (Migrate code-index orchestration and shared dependencies): **off the path since your "Drop links" ruling.** Its edges to #23276 and #24380 were removed. It is still a claimed epic with no children, and its entry is at the end.
   - Before that, it inherited the #21568 (Remaining route families) gate on #21558 (Native WS transport in gdaemon), #21560 (Tasks family) and #21559 (Config, runtime handshake, and grant issuing). You approved re-parenting it under Stage 2 #21544, which removed that gate first. Full entries for all four are at the end.
3. **#23275** (`gobby-mcp` crate: native MCP transports with OAuth and CIMD): no planning needed (see the correction above).
   - It is already planned and expanded: leaves #24350 through #24359 carry full specs from the approved gmcp-stdio plan. The placeholder acceptance text belongs only to the #23275 holder itself. The eleventh leaf, #24360 (OAuth 2.1 authorization for the HTTP transport), still carries `needs-planning` from decision D3 and is off the hub-node path.
   - Why it's on the path: two S2.7 node-half leaves wait on its leaves. #24372 (Worktree and clone preparation) and #24375 (Provider launch, exits and host reconciliation) wait on #24355 (Every launch site starts `~/.gobby/bin/gmcp`). #24373 (SRT policy for node runs) waits on #24357 (The sandbox drops the wrapper's read grant).
   - That puts six gmcp leaves on hub-node's critical path: #24350 → #24351 → #24352 → #24354 → #24358 → #24355.

**#21579** (Rust node duties) has no children of its own and needs no expansion: it closes when #24148 and #24150 close. Every other task on the path is an expanded leaf with a written spec.

## Decisions (all decided by Josh)

1. **Do #24380's two code-index edges belong on the hub-node path?** **Decided by Josh:** #24380 (Node code-index preflight) stays on the path. "Drop links" removed the #21586 (Migrate code-index orchestration and shared dependencies) edges to #24380 and #23276. The node code-index routes are built native-narrow, so #24380 waits only on #23276 (gcode index writes from a node need hub HTTP routes) and its in-tree blockers.
   - The options were: keep both edges, with #21586 and #23276 planned first; or narrow, with #24380 moved to a follow-up leaf after the live proof.
2. **Do #24372, #24373 and #24375 need gmcp first?** **Decided by Josh: keep.** #24372 (Worktree and clone preparation, repair and removal), #24373 (SRT policy for node runs) and #24375 (Provider launch, exits and host reconciliation) keep their gmcp edges, because the sandbox read-grant change is real security work.
3. **Should the close cap exempt hub-node planning?** **Decided by Josh: exempt.** Option B (no new tasks until the close backlog is under 20) does not apply to planning and expansion on the hub-node path.
4. **Scope check: does #23277 (Hook envelope carries edited-file content for disk-reading rules) belong in "ready to test"?** **Decided by Josh: include.** Through #21579 → #24150 the path keeps the whole #23277 subtree: P1 node checkout reads and P2 hub sites read through the seam, 12 leaves. Its P3 #24532 (Live node-origin checkout reads) runs after the live proof.

## Critical path to the live proof (#24604)

This is the longest leaf chain, 21 steps, not counting the work #23276 (gcode index writes from a node need hub HTTP routes) adds once it is planned:

#24350 (`gobby-mcp` crate and its daemon dialer) → #24351 (`gmcp` serves the wrapper's tool surface over stdio) → #24352 (`call_tool` keeps the wrapper's canonicalization) → #24354 (E2e and server tests drive `gmcp`) → #24358 (`gmcp` starts a stopped local daemon) → #24355 (Every launch site starts gmcp) → #24372 (Worktree and clone preparation, repair and removal) → #24373 (SRT policy for node runs) → #24374 (SRT install verification and the launch chokepoint) → #24375 (Provider launch, exits and host reconciliation) → #24376 (Command dispatch over the node channel) → #24377 (Node reports) → #24378 (Checkout registration) → #24379 (Codex prompt delivery) → #24380 (Node code-index preflight) → #24383 (Node pane monitor) → #24384 (Node sandbox reaping and records) → #24597 (Node workspace.merge and workspace.remove handlers) → #24601 (Node checkout.status handler) → #24521 (Node `checkout.read` handler) → #24604 (Live hub-to-node proof).

The second spine starts where the ingress arm #24368 (Node hook ingress arm, now closed) left off: #24369 (Node spool drain, boot release and retention, now ready) → #24370 (Durable node agent state) → #24594 (gdaemon internal node-command route) and #24371 (Node terminal host) → #24372, where it joins the first chain.

## In flight right now

| Task | State |
| --- | --- |
| #24368 Node hook ingress arm | **Closed** (landed at cac3ed622b, gdaemon 0.4.52, activated by the 16:12 cutover). |
| #24270 Node routes: checkout registration and run reports | Landed (f0a600e55c), close pending. |
| #24595 Callers act only on machines they own | **Landed** at 9da71471e9 (reviewed 26ed65cab2: 412 passed, 0 failed; Code and Security LAND). Close pending; activation rides the next cutover. This one unblocks the most hub-mode leaves. |
| #24596 Node-commands contract version 3 | Landed (f770f3317d). Its close waits on #24642 (Close gate 10 refuses a fresh passing run on a clean integration checkout; see the end). |
| #24582 Node ledger prune test | Reviewer LAND; waiting on its activation receipt, then close. |
| #24584 Node receipt sweep | Clippy and nextest passed (25 passed); Code and Security LAND at 1a6f8b3a52. It lands after #24446 (`gobby-runtime` crate), with a gdaemon bump and the per-claim remove/restore fix folded in at 17:10. |
| #24594 gdaemon internal node-command route | Claimed, blocked behind #24370 (Durable node agent state). |
| #24381 Detection manifest engine | Claimed, in progress. |

## Edits made today while building this report

- **Fixed a dependency cycle.** #24604 (Live hub-to-node proof) depended on the whole #24150 epic (S2.11 node half). Inside #24150, #24532 (Live node-origin checkout reads) depends on #24604, so neither could ever close. #24604 now depends on #24361 (P1: Node hook ingress), #24517 (P1: Node checkout reads) and #24518 (P2: Hub sites read through the seam) instead. #21579's own edge to #24150 is unchanged.
- **Closed two superseded hub-mode holders as duplicates.** #24280 (Remote workspace lifecycle, plan section D1) and #24282 (Machine-scoped project view, D3) are now covered by the P4 #24592 leaves #24596 (Node-commands contract version 3) through #24603 (gclient projects pane follows the machine filter).

## Full tree on the path

The tree lists only open tasks; #24368 (Node hook ingress arm) left it when it closed. "Blocked by" names only open blockers. Statuses were refreshed live at 17:16 CDT. Statuses mean:
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
  - **#24595** Callers act only on machines they own. Status: landed (9da71471e9), close pending; activation at the next cutover. Planning: none: expanded leaf.
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
    - **#24381** Detection manifest engine. Status: in progress (claimed). Planning: none: expanded leaf.
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
    - **#24380** Node code-index preflight. Status: blocked; blocked by #24379 (Codex prompt delivery), #24389 (Hub routes for launch outcomes), #23276 (gcode index writes from a node need hub HTTP routes). Planning: none: expanded leaf.
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
    - **#24369** Node spool drain, boot release and retention. Status: ready (its blocker #24368, Node hook ingress arm, closed). Planning: none: expanded leaf.
    - **#24582** Node ledger prune test runs 2-4 minutes on a production-size fixture. Status: reviewer LAND at a501fa570c; escalated hand-off waiting on its activation receipt, then close. Planning: none: expanded leaf.
    - **#24584** Node receipt sweep survives one bad inbox entry. Status: in review (Code and Security LAND at 1a6f8b3a52; clippy and nextest passed). It lands after #24446 (`gobby-runtime` crate) with a gdaemon bump and a folded per-claim fix. Planning: none: expanded leaf.

### Pulled in from outside hub-node by dependency edges

- **#23275** `gobby-mcp` crate: native MCP transports with OAuth and CIMD. Status: open container (0/11 children closed), not escalated; blocked by its eleven leaves #24350 through #24360, including #24356 (The Python wrapper and its setting are gone), #24359 (`gmcp` is a published managed binary) and #24360 (OAuth 2.1 authorization for the HTTP transport), and by #21570 (MCP front door flip). Only the eight leaves below are on the path. Planning: none: planned and expanded on 10-13; the stale needs-planning flag and escalation were cleared at about 15:50. Its leaf #24360 keeps its own needs-planning label.
  - **#24350** `gobby-mcp` crate and its daemon dialer. Status: ready. Planning: none: expanded leaf from the approved gmcp-stdio plan.
  - **#24351** `gmcp` serves the wrapper's tool surface over stdio. Status: blocked; blocked by #24350 (`gobby-mcp` crate and its daemon dialer). Planning: none: expanded leaf from the approved gmcp-stdio plan.
  - **#24353** `gobby install` builds, promotes, and tracks `gmcp`. Status: blocked; blocked by #24351 (`gmcp` serves the wrapper's tool surface over stdio). Planning: none: expanded leaf from the approved gmcp-stdio plan.
  - **#24352** `call_tool` keeps the wrapper's canonicalization, wait guard, and heartbeat. Status: blocked; blocked by #24351 (`gmcp` serves the wrapper's tool surface over stdio). Planning: none: expanded leaf from the approved gmcp-stdio plan.
  - **#24354** E2e and server tests drive `gmcp` or the routes, not the wrapper. Status: blocked; blocked by #24352 (`call_tool` keeps the wrapper's canonicalization, wait gu...). Planning: none: expanded leaf from the approved gmcp-stdio plan.
  - **#24358** `gmcp` starts a stopped local daemon at launch. Status: blocked; blocked by #24353 (`gobby install` builds, promotes, and tracks `gmcp`), #24354 (E2e and server tests drive `gmcp` or the routes, not the...). Planning: none: expanded leaf from the approved gmcp-stdio plan.
  - **#24355** Every launch site starts `~/.gobby/bin/gmcp`. Status: blocked; blocked by #24358 (`gmcp` starts a stopped local daemon at launch), #24353 (`gobby install` builds, promotes, and tracks `gmcp`), #24352 (`call_tool` keeps the wrapper's canonicalization, wait gu...), #24354 (E2e and server tests drive `gmcp` or the routes, not the...). Planning: none: expanded leaf from the approved gmcp-stdio plan.
  - **#24357** The sandbox drops the wrapper's read grant. Status: blocked; blocked by #24355 (Every launch site starts `~/.gobby/bin/gmcp`). Planning: none: expanded leaf from the approved gmcp-stdio plan.
- **#23276** gcode index writes from a node need hub HTTP routes. Status: escalated, no open blockers since "Drop links" removed its #21586 edge. Planning: NEEDS PLANNING (escalated needs-planning); Plan Writer gobby#16767 is drafting the native-narrow routes in .gobby/plans/code-index-family.md.

## Other tasks this report mentions

These tasks are named above but sit outside the path tree: scope roots, parent epics, off-path blockers and coordination tasks. They are not counted in the headline. Statuses were refreshed live at 17:16 CDT. Blockers named in parentheses carry their titles; any blocker on the path also has its own entry in the tree.

### Scope roots

- **#21542** Gobby 1.0: gdaemon front door, strangler port, hub and node. Status: open epic, the 1.0 root; no open blockers. Planning: none: its stages exist as child epics.
- **#21544** Stage 2: strangler absorption of the Python daemon behind the front door. Status: open epic, unclaimed; no open blockers. Planning: none: expanded into the Stage 2 family epics; #21586 (Migrate code-index orchestration and shared dependencies) still needs its own plan.
- **#21546** Stage 4: hub and node live (story B). Status: open epic, unclaimed; no open blockers. Planning: none: holds hub mode #21575, which is planned and expanded.

### Parent epics, off-path blockers and coordination tasks

- **#21558** Native WS transport in gdaemon. Status: open epic (0/5 children closed), blocked; blocked by #23881 (Auth-integrated private bridge and native WS activation), #24619 (P2: Auth-integrated private bridge and native WS activation). Planning: none: expanded into #23878 (Shared subscribe/unsubscribe/broadcast corpus) through #23881 (Auth-integrated private bridge and native WS activation) and P2 #24619.
- **#21559** Config, runtime handshake, and grant issuing. Status: open epic, claimed, blocked; blocked by #24438 (P3: `gobby-runtime` family crate), #24439 (P4: Managed principals), #24440 (P5: Config routes), #24441 (P6: Native default, parity evidence and the template docs). Planning: none: planned (gdaemon-runtime-family) and expanded.
- **#21560** Tasks family. Status: open epic (0/1 children closed), blocked; blocked by #21559 (Config, runtime handshake, and grant issuing). Planning: partly expanded: one child, #21561 (Tasks write path and lifecycle transitions in Rust).
- **#21561** Tasks write path and lifecycle transitions in Rust. Status: open epic under #21560 (Tasks family), unclaimed, with one open child; no open blockers of its own. Planning: partly expanded (one child); not on the hub-node path.
- **#21566** External-MCP transport multiplexer as a delegated backend. Status: open epic, unclaimed, no children; blocked by #21560 (Tasks family). Planning: needs planning and expansion; not on the hub-node path.
- **#21568** Remaining route families. Status: open epic (0/12 children closed), blocked; blocked by #21560 (Tasks family), #21558 (Native WS transport in gdaemon). Planning: holder for 12 route-family epics, #21580 (Skills route family) through #21592 (WS chat route family), each still to be planned. The #24649 audit is checking this family-wide gate per family.
- **#21569** Hook ingress and the node-local envelope ledger. Status: open epic, unclaimed; blocked by #24150 (S2.11 node half, in the tree) and #24151 (S2.11 hub half: hook ingress and rule engine in Rust). Planning: none: a holder with no children of its own; it closes when its two halves close.
- **#21570** MCP front door flip. Status: open epic (no children), blocked; blocked by #21566 (External-MCP transport multiplexer as a delegated backend), #21569 (Hook ingress and the node-local envelope ledger). Planning: needs planning and expansion (no children).
- **#21575** Hub mode: the datastore owner that runs everything database-backed. Status: open epic, blocked; blocked by #24265 (P1: Hub-node interface), #24266 (P2: Machine-correct hub loops and remote callers), #24267 (P3: Machine-aware roster and attention), #24592 (P4: Remote workspaces and the machine-scoped project view), #24593 (P5: Live hub-to-node proof). Planning: none: planned (hub-mode) and expanded.
- **#21580** Skills route family. Status: open epic under #21568 (Remaining route families), unclaimed, no children; no open blockers of its own. Planning: needs planning and expansion; not on the hub-node path.
- **#21586** Migrate code-index orchestration and shared dependencies. Status: open epic, claimed, no children; no open blockers. Off the hub-node path since your "Drop links" ruling removed its edges to #23276 and #24380. Its re-parent from #21568 (Remaining route families) to Stage 2 #21544 was done on your approval. Planning: NEEDS PLANNING AND EXPANSION; the code-index family plan (.gobby/plans/code-index-family.md, Plan Writer gobby#16767) covers it.
- **#21592** WS chat route family. Status: open epic under #21568 (Remaining route families), unclaimed, no children; no open blockers of its own. Planning: needs planning and expansion; not on the hub-node path.
- **#23878** Shared subscribe/unsubscribe/broadcast corpus. Status: closed (first leaf of #21558, Native WS transport in gdaemon). Planning: none (closed).
- **#23881** Auth-integrated private bridge and native WS activation. Status: open holder under #21558 (Native WS transport in gdaemon), unclaimed; blocked by #24620 (Python private bridge and native publisher), #24621 (gdaemon native transport and private endpoints), #24622 (Native WS activation). Planning: none: deferred from native-ws-transport decision D1 and expanded as P2 #24619.
- **#24151** S2.11 hub half: hook ingress and rule engine in Rust. Status: open epic, unclaimed; blocked by the Sessions and transcripts and the Workflows, rules, pipelines, build, and validation epics, by #21566 (External-MCP transport multiplexer as a delegated backend), and by the #23277 P1 and P2 leaves in the tree. Planning: no children of its own; its work arrives through those blockers. Not on the hub-node path.
- **#24280** Remote workspace lifecycle. Status: closed today as a duplicate of the P4 #24592 leaves. Planning: none (closed).
- **#24282** Machine-scoped project view. Status: closed today as a duplicate of the P4 #24592 leaves. Planning: none (closed).
- **#24356** The Python wrapper and its setting are gone. Status: blocked; blocked by #24354 (E2e and server tests drive `gmcp` or the routes, not the wrapper), #24355 (Every launch site starts `~/.gobby/bin/gmcp`), #24358 (`gmcp` starts a stopped local daemon at launch). Planning: none: expanded leaf (gmcp-stdio 1.7); not on the hub-node path.
- **#24359** `gmcp` is a published managed binary. Status: blocked; blocked by #24353 (`gobby install` builds, promotes, and tracks `gmcp`). Planning: none: expanded leaf (gmcp-stdio 1.10), labelled needs-decision; not on the hub-node path.
- **#24360** OAuth 2.1 authorization for the HTTP transport. Status: ready (no open blockers, unclaimed); it is one of the eleven leaves blocking #23275 (`gobby-mcp` crate). Planning: NEEDS PLANNING (needs-planning label, deferred from gmcp-stdio decision D3). It is not on the hub-node path.
- **#24368** Node hook ingress arm. Status: closed (landed at cac3ed622b, gdaemon 0.4.52, activated by the 16:12 cutover); it left the path tree when it closed. Planning: none (closed).
- **#24438** P3: `gobby-runtime` family crate. Status: open container under #21559 (Config, runtime handshake, and grant issuing), blocked by its 5 open leaves, including #24446 (`gobby-runtime` crate and native `GET /api/config/effective`). Planning: none: expanded (gdaemon-runtime-family).
- **#24439** P4: Managed principals. Status: open container under #21559, blocked by its 7 open leaves. Planning: none: expanded (gdaemon-runtime-family).
- **#24440** P5: Config routes. Status: open container under #21559, blocked by its 15 open leaves. Planning: none: expanded (gdaemon-runtime-family).
- **#24441** P6: Native default, parity evidence and the template docs. Status: open container under #21559, blocked by its 4 open leaves. Planning: none: expanded (gdaemon-runtime-family).
- **#24446** `gobby-runtime` crate and native `GET /api/config/effective`. Status: in review (claimed; Code LAND, waiting on its version-only gdaemon bump commit before landing); no open blockers. Planning: none: expanded leaf (gdaemon-runtime-family 3.1).
- **#24603** gclient projects pane follows the machine filter. Status: blocked; blocked by #24599 (REST workspace routes list by machine and finish through the node), #24602 (Hub projects and source-control status take machine_id). Planning: none: expanded leaf (hub-mode 4.8).
- **#24619** P2: Auth-integrated private bridge and native WS activation. Status: open container under #21558 (Native WS transport in gdaemon); blocked by #24620 (Python private bridge and native publisher), #24621 (gdaemon native transport and private endpoints), #24622 (Native WS activation). Planning: none: expanded (native-ws-transport).
- **#24620** Python private bridge and native publisher. Status: in progress (claimed, candidate 6a2eb284c3 parked); no open blockers. It lands after #24595 (Callers act only on machines they own) once a merge-result test run passes. Planning: none: expanded leaf (native-ws-transport B1).
- **#24621** gdaemon native transport and private endpoints. Status: in progress (claimed, lint and test runs under way); no open blockers. Planning: none: expanded leaf (native-ws-transport B2).
- **#24622** Native WS activation. Status: blocked; blocked by #24620 (Python private bridge and native publisher), #24621 (gdaemon native transport and private endpoints). Planning: none: expanded leaf (native-ws-transport B3).
- **#24642** Close gate 10 refuses a fresh passing run on a clean integration checkout that contains the already-landed candidate. Status: open, unclaimed after its developer's session expired; that developer (gobby#16031) resumed at 17:05 with its staged work and waits on a test run. No open blockers. Planning: none: found-work bug fix.
- **#24648** Hub-node path-to-test task tree report for Josh (2026-10-17). Status: in progress (this report's task). Planning: none.
- **#24649** Audit the 1.0 tree's dependency edges, execution order and needs-planning labels (2026-10-17). Status: open research spike, unclaimed; researcher gobby#16490 is running it, due about 19:00 CDT. No open blockers. Planning: none: operator-directed research.
