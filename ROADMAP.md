# Gobby Roadmap

Gobby is fleet management for AI coding agents, a local-first control plane:
persistent sessions, task graphs, workflows, hooks, MCP proxying, agents, memory,
and deterministic automation around the tools developers already use.

Last refreshed: 2026-10-06 (stage status and decisions 19–23). This document
is the roadmap and the architecture decision record. The live tracker is
#21542 Gobby 1.0: front door, strangler port, hub and node.

## Where we are (2026-10-06)

- **Runtime**: the 0.5.0 Python backend provides sessions, tasks, memory,
  workflows, rules, pipelines, agents, and MCP proxying behind the Rust
  front door. Operators start it with `gobby start`; public HTTP `:60887`
  and WS `:60888` belong to `gdaemon` (#21551 front door). The bootstrap
  (#23041), Rust proxy (#23043), hook 503 handling (#23044), and runner
  integration (#23045) have landed.
- **Data**: PostgreSQL is the runtime hub; FalkorDB graph; Qdrant vectors.
  Schema authority lives in Rust: `gcore` embeds flattened `baseline@420`
  plus migrations through **459** on `0.5.0`. Migration **460**, the
  #23437 checkout-mode rename, is queued in #23655 checkout-mode landing
  for integration and cutover.
  `gdaemon schema apply/verify` owns DDL.
- **Rust foundations**: `gcode`, `ghook`, `gdaemon`, `gterm`, and
  `gclient` share `gcore`. Stage 0 #21334 terminal client is complete.
  The #22411 heartbeat host landed through #23464 heartbeat landing; family-owned Python
  timer loops retire as their Stage 2 families move.
- **Front-door authentication**: #23271 API-key schema, issuance, and local
  adoption, #23272 auth login/key display, #23269 node bootstrap refusal,
  and #23270 remote-peer TLS with loopback plaintext have landed. S1.4
  #21555 remains open: #23273 key validation/shared-token cutover and
  #23274 node channel are outstanding; its deferred work remains open.
- **Completed foundations**: daemon-native runtime boundary (#18902),
  reactive config store (#19645), account/machine ownership (#19650),
  hub-owned files home (#20330 / #20238), and path-independent project
  identity (#19651).
- **Shared datastores**: M0 code has landed. The Python-era operating model
  remains one active daemon per shared hub, with standbys exposing lease
  control only. PRE remains open for #19600 two-machine acceptance, which
  now follows #21575 hub mode and #17436 node mode.
- **Terminals**: native PTY is the default (#22104; acceptance in
  `docs/evidence/native-backend-flip.md`). gterm is primary for agents;
  tmux is a fallback agents cannot select (decision 20). `gclient`
  supports direct local-host semantic frames, the daemon-WS cell-mode
  proxy, and remote attach over `--daemon-url`.

Stage status is derived from the task DB on 2026-10-06. Counts below are
closed direct children of the stage epic, not all descendant leaves; lane
reparenting is called out explicitly.

| Stage | Current status |
| --- | --- |
| PRE | #21547 stability checkpoint, #21548 database-wide lease, #21549 machine-scoped terminals, and #21550 hook fallbacks are closed; #19600 physical two-machine acceptance remains open |
| Stage 0 · #21334 | Complete: 42/42 direct children closed; Windows cross-toolchain #22447 moved to post-1.0 |
| Stage 1 · #21543 | 16/18 direct children closed; S1.3 #21554 lifecycle and S1.4 #21555 keys/node registration remain open |
| Stage 2 · #21544 | In progress: S2.1 #21557 async Postgres and S2.8 #21565 gterm adoption began early under Lane 5; all 13 family epics remain open, including those two reparented lane epics (11 direct children remain under Stage 2) |
| Stage 3 · #21545 | 0/4 direct children closed; S3.1 #21571 operator verbs waits on Stage 2 (#21544) plus S1.1 and S1.3 #21554 for the lifecycle verbs |
| Stage 4 · #21546 | 1/8 direct children closed (#21577 node gcode configuration); hub and node work remain open |

## Naming

Today, `gobby` is the Python daemon and the CLI that operates it. That is the
one name that changes hands, and it changes hands once, at S3.2 — well into
Stage 3, after the front door has landed and Stage 2 absorption is complete.
Until that moment `gobby` means what it means today.

At S3.2 the Rust client takes the name. `gclient` — the herdr-derived terminal
client, package `gobby-client`, built through Stage 0 under #21334 — ships as
`gobby` and becomes the interface people run all the time: the terminal
workspace plus the daily operator verbs, carried over the public API. The
`gclient` binary name disappears at that point. The gate is capability, not a
date: the client must genuinely carry the verbs first, which is why S3.2 depends
on S3.1.

The Python package steps aside rather than disappearing. Its console script and
entry point rename to `gobby-backend`; `gdaemon` spawns and supervises it as an
internal backend, and humans stop typing it in normal use. Operator-only Python
commands the Rust client has not absorbed yet stay reachable as
`gobby-backend <cmd>`. That transitional name lives from S3.2 until S3.4 removes
the package entirely.

Nothing else moves. `gdaemon` (package `gobby-daemon`) is the Rust daemon
throughout, growing from today's bridgehead into the front door at Stage 1 and
into `standalone`/`hub`/`node` modes across Stages 1 and 4. `gterm` (package
`gobby-terminal`) is the herdr-based PTY host, permanently a separate supervised
process. `gcode` and `ghook` keep their names unchanged.

## Destination

### Three user stories

**A. Solo self-hosted (one machine).** One install. `gobby start` brings up the
daemon; it adopts or spawns the `gterm` PTY host. Bare `gobby` opens the
terminal workspace: roster and attention over localhost, frames over a local
Unix socket, keystrokes through the daemon's lease. A daemon restart adopts the
running host — no agent terminal dies. The user never learns the word
"topology."

**B. Multi-machine homelab (Tailscale).** A home server runs the hub daemon and
the data stack. Workstation and laptop each run `gdaemon` in node mode plus a
gterm host; agents and PTYs live where the checkout lives. The roster shows
every machine's agents (hub data); an attention response routes to the owning
machine's node daemon over its authenticated channel; remote terminal viewing
rides the daemon WS proxy. Coming home, `gobby` attaches locally at full
fidelity to the same terminals.

**C. Hosted Gobby — deferred.** gobby.ai runs the data stack and hub daemon as
one compose unit, one hub per customer. Story C differs from story B only in who
hosts the hub and in WAN latency; the datastores never leave the hub network in
either story, so the only prerequisite is TLS on the front door. Deferred until
Gobby is proven in local and Tailscale production.

### Target architecture

- **One daemon binary, `gdaemon`, three modes.** `standalone` (default; hub and
  node on one box — story A), `hub` (owns the datastores and everything
  database-backed, and performs node duties for the hub machine), `node`
  (per-machine: registers to a hub, authenticates with a user-issued API key
  bound to the machine, never holds datastore credentials, forwards every
  semantic call, runs only machine-local duties — agents, worktrees, gterm
  supervision, hook ingress and its ledger). A node requires a hub connection;
  offline is a typed error, never a fallback. Hub-owned launchers coordinate
  dispatch; the selected local or remote node owns child execution. One service
  container is assembled per mode.
- **`gterm` is permanently a separate supervised process.** It survives daemon
  restarts, upgrades, and lease handoffs; the daemon adopts it by epoch. Folding
  PTY ownership into the daemon would kill every agent terminal on every daemon
  restart.
- **`gobby` is permanently a separate interactive process.** Zero-to-N viewers,
  each living exactly as long as a human is looking; a client crash must never
  take PTYs down. It builds without the VT engine (`vt-engine` is host-only) and
  couples to the daemon exclusively through the public HTTP/WS API.
- **Protocols are the durable contract; implementations are disposable.** The
  JSON-lines control protocol (daemon ↔ host), the bincode frame protocol
  (clients ← host), and the backend-neutral WS terminal messages survive the
  Python→Rust migration byte-for-byte. Committed golden wire corpora are the
  enforcement.
- **The public CLI plus daemon API is the plugin surface.** Manifest-driven
  external processes on the public API only, in Rust, so plugins survive every
  migration stage (#20201).
- **The absorbed daemon is composed from family crates.** Each Stage 2 route
  family is a workspace-private crate statically linked into `gdaemon`,
  exporting a `RouteFamily` (claimed prefixes, router, service trait) that the
  front door's routing table composes. One release, one lockfile, one schema
  identity pin; the routing-table flip is the stability mechanism (decision 16).
- **HTTP splits machine checkouts from hub documents.** `/api/files` is the
  checkout browser on this daemon; hub-owned `files_home` content is under
  `/api/hub/...`; nodes reach hub routes through `hub_daemon_url` (one hop).
  There is no shared mount of `$GOBBY_HOME/files`.

### Destination HTTP and files_home

| Job | Destination | Today | Bytes |
| --- | --- | --- | --- |
| Project checkout browser | `/api/files/*` | `/api/files/*` (except `user-md`) | Local repo on this machine |
| Working profile | `GET`/`PUT /api/hub/user` | `GET`/`PUT /api/files/user-md` | Hub `files_home/USER.md` |
| Hub chat uploads | `/api/hub/chat/attachments` | `/api/chat/attachments` | Hub `files_home/attachments/<project-id>/...` |
| Telegram inbound media | hub `files_home` at S4.8 | machine-local | `~/.gobby/comms_attachments` until then |

Destination on-disk tree on the hub host (`$GOBBY_HOME/files` standalone;
`/var/lib/gobby/files` allowed on a dedicated server):

```text
<files_home>/
  USER.md
  _personal/                 # life-admin only; not a git repo
  attachments/               # all hub chat uploads, keyed by project id
    <project-id>/<id[:2]>/<id>/<filename>
```

Reserved names at `<files_home>`: `USER.md`, `_personal`, and
`attachments`. Chat uploads for a gobby-repo
conversation are hub documents but not personal files; they belong under
`attachments/<project-id>/`, and today's `_personal/attachments/...` writers are
transitional.

## The path

Live tracker: epic #21542. Nothing in this tree is dispatched by
`gobby build`; every task carries `allow_automation=false`.

Two standards apply to every stage and are stated once here:

- **Per-boundary validation gate.** Every absorption plan must satisfy fixture
  parity, error-path parity, side-by-side execution, route-scoped rollback via
  the proxy table, and observability.
- **Atomic-task standard.** One boundary, one rollback story, one validation
  target per task. Boundary-first; no long-lived Rust branch.

### PRE — pre-flight for the two-machine smoke

The original found-work preflight leaves are complete: #21548 database-wide
singleton lease, #21549 machine-scoped terminal list/get/attach, and #21550
removal of dormant hook machine-ID fallbacks. #21547 daemon stability
checkpoint is also closed. The #21363 feedback burndown is the stability feed,
not a dependency.

**M0: shared datastores bridge and two-machine acceptance (#19585).**
#19600 two-machine end-to-end acceptance is its only open deliverable:
the physical smoke in `.gobby/plans/completed/hub-pc-datastore-move.md`
R0–R7 and `docs/guides/remote-docker-acceptance.md` Phases 1–9.
It now waits on #21575 hub mode and #17436 node mode. The completed
#21547, #21548, and #21549 edges remain satisfied. PRE closes after the
smoke passes; it no longer gates Stage 4 (decision 23).

### Stage 0 — terminal client and native PTY runtime (#21334)

Complete: all 42 direct children are closed. The client plan is at
`.gobby/plans/completed/herdr-client-completion.md`. #22447 Windows cross
toolchain moved out of Stage 0 to post-1.0, so it no longer blocks the path
(decision 22).

The #21357 native-runtime planning work and #21908 workspace-sidebar planning
are closed; the sidebar plan is at
`.gobby/plans/completed/gclient-workspace-sidebar.md`. #21355 follow-on
planning/roadmap seed is closed. #20202 remote roster and attach planning
remains open and waits on #19600 two-machine acceptance.

### Stage 1 — the gdaemon front door owns the network boundary (#21543)

The front door comes **first**. `gdaemon` takes `:60887`/`:60888`, reverse-
proxies HTTP and WS to the Python daemon on an internal loopback port, and owns
the mode enum, the singleton lease, machine registration, and API keys from day
one.
Subsystem absorption then happens behind that boundary, one routing-table change
at a time. This inverts the older "Python front door delegating to a `:60890`
sidecar" framing: no Python-side `rust_migration` flags, no `APIRoute` compare
wrapper, no mismatch latch. Compare mode is a proxy feature.

| Ref | Scope |
| --- | --- |
| **S1.1** · #21551 | `gdaemon serve`: axum front door on `:60887`/`:60888` proxying HTTP and WS to Python on loopback; native `GET /api/health`; bearer pass-through; the WS proxy passes the `terminal_ws_golden` corpus and chat WS unchanged; defines the `RouteFamily` seam and the per-family `Proxy | Native | Compare` backend read from bootstrap |
| **S1.2** · #21553 | Mode enum `standalone`/`hub`/`node` and the mode-assembled service container; boundary semantics only, duties come in Stage 4 |
| **S1.3** · #21554 | Singleton lease and Python backend lifecycle in Rust; retires the Python lease modules; `hub` and `standalone` lease, a node registers instead |
| **S1.4** · #21555 | API keys and node registration: `api_keys` (user, machine, hash, label, revocation) replaces the shared `local_cli_token`; front-door middleware resolves a key to user and machine; the runtime handshake bootstraps a fresh machine's first key; node registration over WS; `machines` gains platform/capabilities/heartbeat/endpoint columns; `/api/machines`; revocation drops the node's channel |
| **S1.5** · #21552 | HTTP contract corpus for the proxied surface (`tests/contracts/http/`), dual-consumed by pytest and Rust; the parity gate for every Stage 2 takeover |

Edges: `S1.2` ← `S1.1`; `S1.3` ← `S1.2`, #21548 database-wide lease;
`S1.4` ← `S1.2`;
`S1.5` independent.
Stage 1 is in progress under its owning lanes. S1.1 front door, S1.2 mode
boundary, and S1.5 contract corpus are closed. S1.3 still needs #23457
native standby/lifecycle cutover and #23458 Python lease retirement.
S1.4 still needs #23273 key validation/shared-token cutover and #23274
node channel. TLS and API-key issuance have landed; those two remaining
epics are not complete.

### Stage 2 — strangler absorption behind the front door (#21544)

Ordered so story B emerges mid-port; hook ingress lands late because the rule
engine is entangled with sessions and MCP dispatch. Each family lands as its own
workspace-private crate (decision 16), added to the workspace when its epic is
claimed; S2.3 is the template.

| Ref | Family |
| --- | --- |
| **S2.1** · #21557 | `gcore` async Postgres layer — pool vs `spawn_blocking`, the repository seam, grant-role compatibility with `baseline@420` |
| **S2.2** · #21558 | Native WS transport in `gdaemon` — accept, auth handshake, subscription filter, broadcast envelope |
| **S2.3** · #21559 | Config, runtime handshake, and grant issuing — the first takeover, and the template for every later one |
| **S2.4** · #21560 | Tasks family — reduced list, then get, then the write path (#21561, which hosts the hub authority contract #20822), stages, dependencies, the MCP server |
| **S2.5** · #21562 | Sessions and transcripts — including the resumability decision |
| **S2.6** · #21563 | Memory and search — repositories, recall injection, dream |
| **S2.7** · #21564 | Attention, agents, dispatch, and worktrees with machine scoping — where node semantics become HTTP-only |
| **S2.8** · #21565 | Daemon-side `gterm` adoption and terminal WS |
| **S2.9** · #21567 | Workflows, rules, pipelines, build, and validation |
| **S2.10** · #21566 | External-MCP transport multiplexer as a delegated backend |
| **S2.11** · #21569 | Hook ingress and the node-local envelope ledger |
| **S2.12** · #21570 | MCP front door flip — the last MCP step |
| **S2.13** · #21568 | Remaining route families — thirteen children, enumerated now so nothing is discovered late |

Stage 2 has started early under Lane 5: S2.1 #21557 async Postgres has
completed #23081 pool/budget work, and S2.8 #21565 has completed #23207
terminal-family crate work. The stage-level S1 edge remains; individual
dependency-ready work can proceed while Stage 1 is open (decision 21).

Edges: `S2` ← `S1`; `S2.3` ← `S1.5`, `S2.1`;
`S2.4` ← `S2.3`; `S2.5` ← `S2.4`, `S2.2`;
`S2.6` ← `S2.4`; `S2.7` ← `S2.4`, `S1.4`;
`S2.9` ← `S2.5`, `S2.7`;
`S2.10` ← `S2.4`; `S2.11` ← `S2.5`, `S2.9`, `S2.10`;
`S2.12` ← `S2.10`, `S2.11`; `S2.13` ← `S2.4`, `S2.2`.

S2.8's M1 adoption/epoch work has no S2.2 transport dependency. Its D2
#23219 terminal WS/relay waits on S2.2 #21558 native WS transport.
The former Stage 0 hardening prerequisite is complete.

### Stage 3 — the client takes the name, then Python retires (#21545)

| Ref | Step | Exit |
| --- | --- | --- |
| **S3.1** · #21571 | Operator verbs on the client | The client carries the daily verbs over the public API; the 175-module `src/gobby/cli/` tail is inventoried with a keep, absorb, or drop decision each |
| **S3.2** · #21573 | The naming switch: `gclient` → `gobby`, Python → `gobby-backend` | `gobby` on PATH is the Rust client, and `gobby start` brings up story A end to end |
| **S3.3** · #21572 | Planned parity ledger at `docs/contracts/parity-ledger.md` | Every row `delegated` or dropped by a recorded decision |
| **S3.4** · #21574 | Retire `gobby-backend` | One commit; all golden corpora green against `gdaemon` alone |

Edges: `S3.1` ← #21334 terminal client, `S1.1`, `S1.3` (the four lifecycle
verbs), `S2`; `S3.2` ← `S3.1`; `S3.4` ← `S3.3`, `S2`.
S3.1 starts after Stage 2 (#21544) completes and lands before S3.2 to S3.4
begin. The naming switch and the Python retirement follow it.

### Stage 4 — hub and node live, story B (#21546)

| Ref | Piece |
| --- | --- |
| **S4.1** · #17436 | Node mode: the per-machine daemon that holds no datastore credentials and runs only local duties |
| **S4.1b** · #21579 | Rust node duties — cross-reference to S2.7, S2.8, S2.11; closes when a node runs `gdaemon` alone |
| **S4.2** · #21575 | Hub mode: everything database-backed runs only in `hub` and `standalone` |
| **S4.3** · #20202 | Remote `gobby` attach — planning waits on #19600 two-machine acceptance |
| **S4.4** · #17769 | Per-user auth and multi-user; labeled `later` |
| **S4.5** · #21577 | Configure `gcode` on nodes (closed) |
| **S4.6** · #19652 | Hub transcript archive research |
| **S4.7** · #20203 | Hosted terminal-relay privacy stance — `hosted`, off-spine |
| **S4.8** · #21576 | Move Telegram and comms attachments onto hub `files_home` |

Edges: `S4` ← `S1`; `S4.1b` ← `S2.7`, `S2.8`, `S2.11`, #21549 machine-scoped terminals;
`S4.2` ← `S1.2`; `S4.4` ← `S1.4`.

**Story B is testable when Stage 1, S4.2, and S4.1b close.** S4.2 runs right
after S1.2 so the first hub-node pair test in S1.4 has a node running no
maintenance. Stage 4 as a whole closes with S4.1b. `gcode` on a node writes its
index through hub HTTP routes owned by whichever Stage 2 family absorbs the
code-index routes (decision 17).

## Side quests

Documented, allowed to run concurrently, labeled `sidequest` in the task graph,
and **never a blocker of anything on the path**.

- Legacy wiki retirement: #21771; replacement planning follows retirement
- Plugin system on the public API: #20201
- Hosted privacy stance and story C: #20203; datastore TLS and Qdrant auth are
  not yet tasks
- Gobby Pro fleet: #17438 (and #19582)
- UI design elevation: #19880
- Fast-mode controls: #19564
- Post-0.5.0 enhancement keeps: #18498
- SWE-bench evaluation: `docs/plans/SWE-BENCH.md`

The feedback-findings burndown **#21363** is not a side quest and not a
dependency. It is the stability work that feeds #21547 and hosts the
`found-work` leaves gating #19600. It is recreated nightly by title, so nothing
may depend on it — depend on its leaves.

## Experiment lab — requires further planning

Deferred design work. The feedback and Dream evidence/reporting repairs do not
implement an experiment scheduler, autonomous skill mutation, wiki integration,
or build consolidation.

Preserve these working preferences for the next planning effort: a stable
controller, containerized candidate stacks, an unchanged baseline plus three
candidates, paired trials, bounded campaigns, and human approval for live changes.
The proposed roles are experiment generation, evaluation design, execution,
synthesis, independent audit, gatekeeping, and follow-up evaluation.

Failure learning must preserve rejected hypotheses, inconclusive results, scoped
lessons, and complete attempt history. A successful retry must not erase evidence
of failed attempts, and a lesson must retain the conditions under which it applies.

Resolve runtime topology, orchestration recovery, evaluation validity, budget
enforcement, lesson activation, and possible build/pipeline consolidation in a
separate planning effort before implementation.

## Ports and the proxied surface

- `:60887` HTTP and `:60888` WS are public and belong to `gdaemon`;
  the Python backend uses an internal loopback port set in bootstrap.
- `:60889` dev web UI. `:60891` managed PostgreSQL. `:60890` is released — the
  sidecar it was reserved for is not being built.
- Freeze set for the port: the three terminal protocols, the WS event envelope,
  and the HTTP contract corpus (S1.5).
- Error-envelope quirk that parity must preserve: internal failures return
  HTTP 200 with `{"status":"error","message":"Internal error occurred but
  request acknowledged","error_logged":true}`.
- Deferred boundaries, carried by the proxy until their family moves:
  `/api/admin/status`, `POST /api/hooks/execute`, and sessions
  `include_resumability`.
- The external-MCP transport multiplexer moves before any internal `gobby-*`
  server (S2.10 before S2.12).
- Second pattern for CLI-shaped surfaces: a versioned CLI contract plus a thin
  gateway.

## Decision record

1. **Fork herdr once at v0.8.0; no upstream tracking** (2026-08-13). Post-fork
   fixes are deliberate per-commit cherry-picks logged in `UPSTREAM.md`.
2. **Keep the Ghostty VT engine**, vendored with Zig, behind `vt-engine` so only
   host builds pay for it (2026-08-13).
3. **Import herdr's UI chrome and make it Gobby's** — rewired to daemon data,
   restyled to the deutan-safe `.impeccable.md` token system (2026-08-13).
4. **One daemon binary named `gdaemon`, three modes** (2026-08-13; amended
   2026-09-01 to **mode boundary first, mode semantics last**). The boundary —
   ports, datastore ownership, whether to register to a hub — lands at Stage 1;
   the per-mode duties land at Stage 4.
5. **gterm and the client stay separate processes and separate binaries**,
   permanently (2026-08-13; amended 2026-09-01: the client ships as `gclient`
   until the S3.2 rename, not "until the Stage-2 rename").
6. **SRT sandbox wrapping has one chokepoint** at the TerminalRuntime spawn seam
   (2026-08-13).
7. **Plugins target the public API only** and live client-side in Rust
   (2026-08-13).
8. **Hub documents and machine checkouts are separate HTTP trees** (2026-08-19).
   `/api/files` stays the local checkout browser; `/api/hub/*` is the hub
   `files_home` surface.
9. **The hook-envelope ledger stays node-local**; move it at the hook-route port,
   not before (2026-09-01). The `ghook` inbox spool stays on the filesystem
   permanently: its writer runs inside the SRT sandbox with no datastore
   credentials. A hub-PostgreSQL table would put a hub round trip on every tool
   call in story B and has no home in story C.
10. **gdaemon is the front door first** (2026-09-01). It takes the public ports
    and reverse-proxies to Python; absorption happens behind that boundary, one
    routing-table change at a time. Compare mode is a proxy feature; the
    Python-side migration flags are not built.
11. **The M0 lease is database-wide — one active daemon per shared hub**, with
    standbys exposing lease control only (2026-09-01). This is the Python-era
    transition shape, not the destination.
12. **`ROADMAP.md` is canonical** (2026-09-01). The separate
    architecture-evolution document that used to hold the staged path is retired
    and deleted; its durable content is absorbed here, and this file is both the
    roadmap and the decision record. Everything off this path is a side quest:
    documented here, labeled `sidequest`, concurrent, never a blocker.
13. **Hub and node are the same daemon on every machine** (2026-09-01; clarified
    2026-09-03). `hub` owns the datastores and everything database-backed while
    also performing node duties for the hub machine. Hub-owned launchers
    coordinate dispatch; the selected local or remote node owns child execution.
    `node` registers, authenticates with a user-issued API key bound to the
    machine, holds no datastore credential, forwards every semantic call, runs
    only machine-local duties, and requires a hub connection — offline is a
    typed error, never a fallback.
    `standalone` is both on one box. This settles the authority matrix #19647 was
    chartered to research.
14. **The Python daemon can be a node** (2026-09-01; superseded by decision 17
    on 2026-09-09). The datastore tunnel, the machine-scoped PostgreSQL role
    under row-level security, and the transitional Python node were dropped
    before any of them was built.
15. **Naming: `gobby` is the client and interface** (2026-09-01), taken from
    `gclient` as soon as it carries the daily operator verbs (S3.2). The
    Python package becomes `gobby-backend` until it retires at S3.4;
    `gdaemon` and `gterm` keep their names.
16. **The absorbed daemon is composed from family crates** (2026-09-09).
    Mechanism: workspace crates statically linked into `gdaemon` — no dylib
    plugins (no stable ABI; tokio across a dylib boundary yields two runtimes)
    and no per-family processes (only `gterm` earns one, for PTY survival);
    third-party pluggability stays external on the public API (decision 7).
    Versioning: family crates are `publish = false` and inherit the workspace
    version — one release, one lockfile, one schema identity pin; stability
    comes from the routing-table flip and the S1.5 corpus, never version pins.
    Route seam: each family exports a static `RouteFamily` (claimed prefixes,
    `axum::Router` over shared state, service trait); `gdaemon` holds one
    routing table whose per-family backend `Proxy | Native | Compare` is read
    from bootstrap, so a flip or rollback is a config change with no redeploy.
    Data model: per-family row types and repositories over the S2.1 `gcore`
    async pool and transaction seam; cross-family reads go through the owning
    crate's public API, and Cargo's refusal of dependency cycles is the
    untangling force (expect the sessions/tasks/rules triangle to need a small
    shared trait-or-event crate). Granularity: one crate per Stage 2 route
    family, created when its epic is claimed; S2.3 is the template. Naming:
    `crates/g<family>` → package `gobby-<family>`, after `crates/gcode` →
    `gobby-code`.
17. **The node is thin and the datastores stay home** (2026-09-09; supersedes
    decision 14). The Python daemon is chatty with the database, so a node that
    tunnels queries to the hub puts the WAN on every query and is slower than
    calling the hub directly. No tunnel, no machine-scoped role, no row-level
    security, no datastore TLS: PostgreSQL, Qdrant, and FalkorDB never leave the
    hub network, and the public surface is HTTP and WS behind the front door.
    A node is `gdaemon` in node mode with machine-local duties only (decision
    13); per-invocation binaries (`ghook`, `gcode`, `gobby`) call the hub over
    HTTP with the machine's key. Story B therefore arrives with S4.1b, after the
    Stage 2 families for agents, terminals, and hook ingress. Credentials: an
    API key is a user credential bound to one machine at issue (`user_id`,
    `machine_id`, hash, label, revocation); there is no separate machine key,
    and the single shared `local_cli_token` retires when every caller holds a
    key. Browser sessions and API keys both carry a user; hub-internal work runs
    as a system principal. Tenancy: the hub is the tenant. Hosted Gobby
    provisions one hub per customer (one database, one Qdrant collection set,
    one FalkorDB graph set, one `gdaemon`) on shared datastore instances; no org
    or tenant columns. Machine identity stays per `GOBBY_HOME`, so a second OS
    user on one box is a second machine. Deferred, in order of cost: login
    providers (login is the seam and issues the same browser session), key
    scopes and unbound tokens, teams on one hub and per-user privacy inside a
    hub. Open at S2.11: whether `ghook` posts to the hub directly or through
    the local node; the envelope spool stays local either way (decision 9).
18. **All timer-driven periodic work in the daemon runs on the gdaemon
    heartbeat host** (2026-09-16; `.gobby/plans/gdaemon-front-door.md` §1.2
    runtime). The host lands in Stage 1 (#21543) with an empty roster as the
    scheduling foundation; an absorbed family attaches its interval jobs
    through the `AppState` heartbeat handle during composition (Stage 2,
    #21544), and its Python timer loops (`_check_loop`, `_poll_loop` variants,
    the `start_periodic_tasks` maintenance loops) retire with the family — no
    Python bridge, no dual-run window. Event-driven and coalescing loops and
    I/O pumps keep their own mechanisms, and the node-channel keepalive
    (`gdaemon-front-door.md §4.4`) is unrelated.

19. Ask was built and retired (recorded 2026-10-06). Ask was implemented through about 220
    commits and later retired. The roadmap records both milestones as project history.

20. gterm is the primary terminal backend (2026-09-22). Agent spawning uses gterm. tmux
    remains an operator-controlled fallback; agents cannot select it as their spawn backend.

21. Lanes own path epics (recorded 2026-10-06). Each path epic has an assigned lane. Stage 2
    may begin while Stage 1 remains open where the work's dependencies are satisfied. Lane 5
    started S2.1 and S2.8, the gterminals family crate, early.

22. Windows support is post-1.0 (2026-10-06). Josh: "Go with orchestrator's recommendation.
    Windows support for Gobby is untested and shouldn't be a blocker." Move #22447 Windows cross
    toolchain to post-1.0; it does not gate the current path.

23. Two-machine acceptance follows hub and node (2026-10-06). Josh: "I'm not wanting to test
    two machines until hub and node is working, otherwise it's a waste of effort." #19600
    Two-machine end-to-end acceptance runs after #21575 Hub mode and #17436 Node mode. PRE remains
    open until that smoke passes. #19600 Two-machine acceptance no longer gates #21546 Stage 4 hub
    and node live (story B).

24. Operator verbs follow Stage 2 and precede the rest of Stage 3 (2026-10-06). Josh, on
    #21571 Operator verbs on the client: "Ok, it needs to be after stage 2 but before stage 3."
    This supersedes his earlier Stage 4 idea. S3.1 starts after #21544 Stage 2 completes and
    lands before S3.2 to S3.4 begin.

## References

Completed plans:

- `.gobby/plans/completed/daemon-native-runtime-boundary.md`
- `.gobby/plans/completed/shared-remote-stack.md`
- `.gobby/plans/completed/machine-scoped-worktrees-clones.md`
- `.gobby/plans/completed/project-checkout-identity.md`
- `.gobby/plans/completed/two-daemon-hub.md`
- `.gobby/plans/completed/hub-owned-files-home.md`
- `.gobby/plans/completed/account-identity-machine-ownership.md`
- `.gobby/plans/completed/reactive-config-store.md`
- `.gobby/plans/completed/herdr-terminal-client.md`
- `.gobby/plans/completed/herdr-terminal-client-qa-fixes.md`
- `.gobby/plans/completed/herdr-foundation-landing.md`
- `.gobby/plans/completed/herdr-client-completion.md`
- `.gobby/plans/completed/hub-pc-datastore-move.md`
- `.gobby/plans/completed/gclient-workspace-sidebar.md`
- `.gobby/plans/completed/gcode-agentic-ask-pipeline.md`
- `.gobby/plans/completed/retire-legacy-wiki.md`

Live path and related stability plans:

- `.gobby/plans/m0-shared-datastores-bridge.md`
- `.gobby/plans/gdaemon-front-door.md`
- `.gobby/plans/gdaemon-api-keys-nodes.md`
- `.gobby/plans/gdaemon-key-cutover.md`
- `.gobby/plans/gdaemon-node-channel.md`
- `.gobby/plans/gmcp-stdio.md`
- `.gobby/plans/gcore-async-postgres.md`
- `.gobby/plans/daemon-side-gterm-adoption-and-terminal-ws.md`
- `.gobby/plans/gterm-host-handover.md`
- `.gobby/plans/gclient-daemon-resilience.md`

Architecture and guides: `docs/architecture/hub-owned-files-home.md`,
`docs/guides/shared-stack.md`, `docs/guides/remote-docker-acceptance.md`,
`docs/guides/dispatch.md`, `docs/contracts/plan-coverage.md`.

Retired and deleted 2026-09-01: the architecture-evolution document under
`docs/architecture/`, whose durable content is absorbed above, together with the
three superseded Rust-migration plan documents under `docs/plans/`. See commit
`c0f1276acd` for the exact paths. Umbrella #17488 is closed as a duplicate of
S4.8; its history stays in the task graph.
