# Gobby Architecture Evolution

The big-picture path from today's local-first Python daemon to the
hub-and-node Rust platform, with the decisions that shape it and citations to the plans and
tasks that carry each stage. `ROADMAP.md` tracks what/when; this document is
the why/how. Decided 2026-08-13 during the herdr-terminal-client planning
session (task #18802); update it when a stage decision changes, not for
routine progress. Remaining-path snapshots below record execution state
against those decisions so it is not lost in task history.

## Where we are (2026-08-22)

- **Runtime**: the 0.5.0 Python daemon is the supported local-first runtime —
  sessions, tasks, memory, workflows, rules, pipelines, agents, MCP proxy.
  Operators start it with `gobby start`. HTTP `:60887`, WS `:60888`.
- **Data**: PostgreSQL is the runtime hub; FalkorDB graph; Qdrant vectors.
  Schema authority already lives in Rust — `gcore` embeds one flattened
  `baseline@419` with no stacked migrations, and `gdaemon schema apply/verify`
  owns DDL (`crates/gcore/src/schema/assets.rs`).
  M0 shared-datastore **code** has landed (leases, remote DSNs, `machine_id`
  scoping). The remaining M0 gate is the real two-machine smoke (#19600).
  Plans: `.gobby/plans/m0-shared-datastores-bridge.md`,
  `.gobby/plans/two-daemon-hub.md`, `.gobby/plans/hub-pc-datastore-move.md`.
- **Identity / hub files**: account/machine ownership (#19650), hub-owned
  files home (#20330 / #20238), the reactive config store (#19645), and
  path-independent project identity (#19651, closed 2026-09-01) are on
  `0.5.0`.
- **Rust bridgehead**: `crates/` ships `gcode`, `gwiki`, `ghook`, `gdaemon`
  over the shared `gcore` library — the strangler beachhead for the daemon
  port. gcode/gwiki daemon-native grants (#18902) and checkout grants
  (#19651 P4, #20300) closed. Grant-surface reservation: the
  `gobby_gcode_capability` role keeps `SELECT` on `projects(id, name,
  deleted_at)` in `baseline@419` although landed gcode reads none of it —
  project resolution goes through the daemon's `/api/projects` and the index
  write fence keys on `project_checkouts.root_path`. Kept on purpose for the
  wiki information-model redesign (#19664): #21504 (`needs-decision`, the
  #21438 follow-up) settles consume-or-drop, and a drop lands as a migration
  ≥ 420, never a baseline edit.
- **Product that landed on `0.5.0` and is off the architecture spine**:
  class-hierarchy graph views (#17680, including P3), clear-self durable
  handoff (#20539), vLLM runtime support (#20488) plus follow-up parity
  (`wire_api=responses`, embeddings installer, status probes).
- **Terminals**: `0.5.0` is still tmux-backed, with the documented Gobby-managed
  defects (cross-socket identity collisions, smallest-client sizing,
  duplicated VT replies — `.gobby/plans/completed/activity-panel-live-terminal.md`).
  The Stage 0 native stack exists only on worktree `wt-task-20255-m4`
  (HEAD `518cec5c41`, not merged) and is being reworked.

## Where we're going — three user stories

These are the destination experience; every stage below is judged against
them.

**A. Solo self-hosted (one machine).** One install. `gobby start` brings up
the daemon; it adopts or spawns the `gterm` PTY host. Bare `gobby` opens the
Rust terminal workspace: roster and attention from the daemon over localhost,
terminal frames over a local Unix socket, keystrokes through the daemon's
lease. A daemon restart adopts the running host — no agent terminal dies. The
user never learns the word "topology."

**B. Multi-machine homelab (Tailscale).** A home server runs the hub daemon
and the data stack — the authority for tasks, sessions, memory, machine
registration, and API keys. Workstation and laptop each run `gdaemon` in
node mode plus a gterm host; agents and PTYs live where the
checkout lives. From anywhere, the roster shows every machine's agents (hub
data); an attention response routes to the owning machine's node daemon over
its authenticated channel; remote terminal viewing rides the daemon WS proxy.
Coming home, `gobby` attaches locally at full fidelity to the same terminals.

**C. Hosted Gobby.** gobby.ai runs the data stack and hub daemon. The user
installs `gdaemon` in node mode and `gobby`, registers over
WebSocket, receives an API key, and routes all semantics through the hub —
the machine never holds datastore credentials. Everything physical (code,
worktrees, agents, PTYs) stays local; day-to-day feels identical to story A.
Remote browser viewing relays frames hub-ward — governed by an explicit
privacy stance (#20203).

## Target architecture

- **One daemon binary named `gdaemon` (package `gobby-daemon`)**, three
  modes: `standalone` (default; hub + node capabilities on one box — story
  A), `hub` (semantic authority, auth/API keys, datastore ownership), `node`
  (registers to a hub; owns machine-local duties: gterm host supervision,
  worktrees, agent spawning, hook ingress and its envelope ledger). The
  interactive product people run is `gobby`
  (package `gobby-client`). One service container assembled per daemon mode; `gcore`
  stays the shared library. Rationale: the roles share ~90% of their
  substance — a binary split would fork identical semantics or hide a library
  behind two thin mains, and the solo topology is simply both capability sets
  on one machine. Precedent: `GOBBY_RUNTIME_MODE` already selects config
  authority per process on the Rust side.
- **`gterm` is permanently a separate supervised process.** The PTY host is
  the tmux-server replacement: it survives daemon restarts, upgrades, and M0
  active/standby lease handoffs, and the daemon adopts it by epoch instead of
  replacing it. Folding PTY ownership into the daemon would make every daemon
  restart kill every agent terminal — forfeiting the property the native
  terminal work exists to buy.
- **`gobby` is permanently a separate interactive process.** Zero-to-N
  viewers, each living exactly as long as a human is looking; a TUI crash must
  never take PTYs down. Users type `gobby` at the destination (crate/package
  `gobby-client`; the binary ships as `gclient` until the Stage-2 rename frees
  the name). It builds without the VT
  engine (the `vt-engine` feature is host-only), couples to the daemon
  exclusively through the public HTTP/WS API, and reads frames through a
  frame-source trait (local Unix socket today; a daemon-WS remote source
  arrives with #20202).
- **Protocols are the durable contract; implementations are disposable.** The
  JSON-lines control protocol (daemon ↔ host), the bincode frame protocol
  (clients ← host), and the backend-neutral WS terminal messages survive the
  Python→Rust migration byte-for-byte — a committed golden wire corpus is the
  enforcement. The WS message set doubles as the future hub↔node-daemon
  relay contract for hosted web viewing.
- **The public CLI + daemon API is the plugin surface.** The herdr-style
  plugin system (manifest-driven external processes declaring actions, event
  hooks, and panes) hosts on the public API only, in Rust, so plugins survive
  every migration stage (#20201).
- **HTTP splits machine checkouts from hub documents.** `/api/files` is the
  checkout browser on this daemon. Hub-owned files_home content is under
  `/api/hub/...`. Nodes reach those hub routes through `hub_daemon_url`
  (one hop). There is no shared mount of `$GOBBY_HOME/files`. Detail below.

### Destination HTTP API and files_home layout

Decided 2026-08-19. Current Python routes still use the transitional prefixes
in the table; 0.5.0 is unshipped, so the destination names replace them
rather than aliasing.

| Job | Destination | Today | Bytes |
| --- | --- | --- | --- |
| Project checkout browser | `/api/files/*` | `/api/files/*` (except `user-md`) | Local repo on this machine |
| Working profile | `GET`/`PUT /api/hub/user` | `GET`/`PUT /api/files/user-md` | Hub `files_home/USER.md` |
| Hub wiki (personal, topic; project vaults after #18779) | `/api/hub/wiki/*` | `/api/wiki/*` with topic or personal scope | Hub `files_home/wiki/` |
| Project / CodeWiki vault | `/api/wiki/*` with a real project id until #18779, then `/api/hub/wiki/*` | `/api/wiki/*` with project scope | `<checkout>/wiki` until #18779, then `files_home/wiki/<project.name>` |
| Hub chat uploads (any project, including a repo agent chat) | `/api/hub/chat/attachments` | `/api/chat/attachments` | Hub `files_home/attachments/<project-id>/...` |
| Telegram inbound media | unchanged until Stage 3 | machine-local | `~/.gobby/comms_attachments` until #17488 / Stage 3 |

`/api/hub/wiki` must not claim CodeWiki while the vault is still
checkout-adjacent. Personal and topic scopes are hub-owned now; a node
already proxies those to the hub.

Destination on-disk tree on the hub host (`$GOBBY_HOME/files` standalone;
`/var/lib/gobby/files` is still allowed on a dedicated server):

```text
<files_home>/
  USER.md
  _personal/                 # life-admin only; not a git repo; not a vault
    .gobby/project.json
    notes/
    reminders/
  wiki/                      # wiki home; not itself a vault
    wikis.json
    personal/                # personal vault
    <topic>/
    <project.name>/          # after #18779 only
  attachments/               # all hub chat uploads, keyed by project id
    <project-id>/<id[:2]>/<id>/<filename>
```

Reserved names at `<files_home>`: `USER.md`, `_personal`, `wiki`,
`attachments`. Reserved vault name: `personal`.

`_personal` is the life-admin project. It is not the dump for every hub
blob. Chat uploads for a gobby-repo conversation are hub documents, but
they are not personal files; they belong under `attachments/<project-id>/`.
Today's writers still persist
`_personal/attachments/<project-id>/...` — that path is transitional and
must move with the `/api/hub/chat/attachments` cutover.

## The staged path

**Stage 0 — terminal client and native PTY runtime (now).**
`.gobby/plans/herdr-terminal-client.md` (planned under #18802; plugin,
remote-attach, and relay-privacy deferrals seeded as #20201/#20202/#20203).
Fork herdr once at its Apache-tagged v0.8.0 release and own the code: the
`gobby-terminal` core (Ghostty VT engine kept, vendored with Zig builds
gated host-only), the `gterm` host, the `gclient` workspace TUI
(`gobby-client` — the binary that becomes `gobby` at Stage 2)
importing herdr's UI chrome restyled to the `.impeccable.md` design system, a
durable `terminals` resource, and a backend-neutral `TerminalRuntime` contract
with tmux wrapped first and native launches opt-in behind an evidence-gated
default flip. Users run `gclient` directly: the Python `gobby` CLI neither
wraps nor execs the TUI (plan 3.5 — startup checks live in the binary itself),
and `gobby start`, `gobby stop`, and the other operator commands stay on the
Python entry point until Stage 2. tmux remains first-class for
externally discovered sessions.
A first implementation of #20255 landed on `wt-task-20255-m4` (`518cec5c41`,
leaves #20263–#20285) and is **not** on `0.5.0`. QA found the client side
incomplete and the native default-flip evidence fabricated. Rework is
`.gobby/plans/herdr-terminal-client-qa-fixes.md`: isolation worktree from
current `0.5.0`, absorb `wt-task-20255-m4` as leaf 1.1, land only when the
accumulated guard set is green. The earlier idea-only exploration was epic
#18520 (`.gobby/plans/completed/herdr-interface-backend-foundation.md`,
superseded on licensing by the fork).

**Stage 1 — the Rust daemon absorbs subsystems.** Behind the existing HTTP/WS
surface, subsystem by subsystem, with Python remaining the behavioral
reference until each boundary passes parity (route-contract freeze per
`ROADMAP.md` 0.5.0+/0.6.0). This stage reconciles the roadmap's earlier
strangler-sidecar framing (the `gobby-daemon` sidecar on `:60890`, compare
mode): the port and
compare/delegation mechanics stand — `:60890` is the Stage-1 `gdaemon`
sidecar port — and the destination daemon is `gdaemon` in standalone mode,
not a permanently distinct sidecar product.
Schema authority (done), config runtime (done, `GOBBY_RUNTIME_MODE`), then
route families per the roadmap. When the hook-ingress route family moves, the
envelope claim/dedupe ledger (`~/.gobby/hooks/inbox/processed/` markers today)
becomes a `gdaemon`-owned node-local store — PostgreSQL only where standalone
mode makes the local hub the node's own store — and never hub data (decision
9). The `ghook` spool is untouched: it is the transport buffer for a
credential-free sandboxed writer. The terminal stack is untouched by
construction: the new daemon adopts the same gterm host over the same
protocols.

**Stage 2 — standalone parity.** The Rust `gdaemon` replaces the Python
daemon on a single machine; the Python package retires, freeing the `gobby`
name: `gclient` is renamed `gobby` and carries the operator verbs (`start`,
`stop`, `status`, `tasks`, …) as subcommands over the public API — the binary
is Zig-free, so carrying the CLI costs nothing. Story A is fully
served by `gdaemon` plus `gterm` and `gobby`. Related foundations:
`.gobby/plans/daemon-native-runtime-boundary.md` (still open) and
`.gobby/plans/completed/reactive-config-store.md` (landed).

**Stage 3 — mode split and the hub.** `hub` and `node` modes, machine
registration over WS, API keys minted at the network boundary (the boundary
recorded in the account-identity work:
`.gobby/plans/completed/account-identity-machine-ownership.md`,
`.gobby/plans/machine-scoped-worktrees-clones.md`,
`.gobby/plans/shared-remote-stack.md`). Stories B and C go live: self-hosted
hubs first, then the gobby.ai-provided data stack + hub. The terminal plan's
deferrals land here — remote `gobby` attach with capability tokens (#20202)
and the hosted relay privacy stance (#20203).

**Alongside, stage-independent:** the plugin system (#20201) once `gobby` and
the public API surface exist; Gobby Pro fleet surfaces per `ROADMAP.md`
0.6.0+/0.7.0+.

## Remaining path (snapshot 2026-08-22)

Not a stage-decision change. Checked `0.5.0` (`9e0730d46e`) and
`wt-task-20255-m4` (`518cec5c41`). Implement the children, not umbrella
#17488 (escalated planning container).

1. **Stage 0 rework — `#20255` / `herdr-terminal-client-qa-fixes`.** Named
   current stage. Without a merged native PTY host, stories A–C still die on
   tmux. Do not merge `wt-task-20255-m4` as-is.
2. **`#19651` project-checkout-identity** — closed 2026-09-01 (last #17488
   identity brick; `project_checkouts` lives in the flattened `baseline@419`;
   review findings under #21459). Two-machine testing and the wiki grant
   chain are unblocked. Do not pair schema leaves with herdr P2 — both
   rewrite `crates/gcore` baseline/catalog identity files.
3. **When a second physical machine is available: `#19600`, then `#19647`.**
   M0 close, then residual hub/node authority research. Not coding you can
   do on one box.
4. **Repo-intel / wiki, not the spine.** #17680 is merged. Next is #19664
   wiki-output (blocked on finishing #17678), then #18779 empty-vault
   production cutover. Checkout P4 (gcode grants, #20300) landed; #21504
   settles the `projects` grant reservation before grant-consuming wiki work.
5. **Stages 1–3 stay later.** Rust daemon strangler (`:60890` sidecar),
   Python retirement / `gclient`→`gobby`, then `hub`/`node` with #17436
   (gated on #19651 + #19647), #17769 (gated on #19647), #17440 (gated on
   #17436). Telegram attachments stay machine-local until Stage 3.

vLLM and clear-self already landed on `0.5.0`; they do not unlock Stages 1–3.

## Decision record (2026-08-13, #18802 planning session)

1. **Fork herdr once at v0.8.0; no upstream tracking.** First Apache-tagged
   release with the relicense in its released changelog; post-fork fixes are
   deliberate per-commit cherry-picks logged in `UPSTREAM.md`. Reference
   clone: `~/.gobby/clones/herdr`.
2. **Keep the Ghostty VT engine**, vendored full-tree with Zig — quality over
   build convenience — gated behind `vt-engine` so only host builds pay for
   it.
3. **Import herdr's UI chrome and make it Gobby's** — rewired to daemon data,
   restyled to the deutan-safe `.impeccable.md` token system.
4. **One daemon binary named `gdaemon`, three modes (standalone/hub/node);
   the product command is `gobby` (the TUI).** Mode split is the last
   migration step, not the first. At Stage 2 the retiring Python package
   frees the `gobby` name for the renamed client, which also carries the
   operator verbs over the public API. (Revised 2026-08-13 at the plan's
   enhancement round from the earlier "daemon named `gobby`" form; `node`
   replaces the "spoke"/"client-mode" wording.)
5. **gterm and the TUI stay separate processes and separate binaries**,
   permanently (terminal durability; crash isolation; VT payload is
   host-only). The TUI people run is `gobby`; the binary ships as `gclient`
   until the Stage-2 rename.
6. **SRT sandbox wrapping has one chokepoint** at the TerminalRuntime spawn
   seam (was six per-provider call sites), so new providers and backends
   inherit the invariant structurally.
7. **Plugins target the public API only** and live client-side in Rust.
8. **Hub documents vs machine checkouts are separate HTTP trees**
   (2026-08-19). `/api/files` stays the local checkout browser.
   `/api/hub/user`, `/api/hub/wiki`, and `/api/hub/chat/attachments` are
   the hub files_home surfaces. Hub chat bytes live at
   `files_home/attachments/<project-id>/`, not under `_personal`.
   Project/CodeWiki vaults stay checkout-adjacent until #18779.
9. **The hook-envelope ledger stays node-local; move it at the Stage 1
   hook-route port, not before** (2026-09-01). The `ghook` inbox spool stays
   on the filesystem permanently: its writer runs inside the SRT sandbox with
   no datastore credentials and write access to exactly `hooks/inbox`. The
   daemon-side claim/dedupe/replay/STOP-epoch ledger is machine-local
   transport state that no other machine reads; a hub-PostgreSQL table would
   put a hub round trip on every tool call in story B and has no home in
   story C, and a Python port retires at Stage 2. Nothing is broken today
   (#20851), so the migration-419 proposal was declined (#21490). The settled
   semantics the port carries live in project memory: atomic
   claim/renew/finalize/release on the 15s lease, the 409/503
   `{"status":"retry"}` duplicate and backpressure contracts, the per-machine
   STOP replay epoch, 24h retention, boot release of this machine's
   `processing` rows under the daemon singleton, and batched drain lookups.

## Citations

| Ref | What |
| --- | --- |
| #18802 | This planning task (herdr-derived terminal client + native PTY migration) |
| #18520 | Closed idea-stage epic (herdr interface backend foundation) |
| #20201 | Plugin-system plan (deferred from herdr-terminal-client D1) |
| #20202 | Remote `gobby` attach plan (deferred, D2) |
| #20203 | Hosted terminal-relay privacy stance (deferred, D3) |
| `.gobby/plans/herdr-terminal-client.md` | Stage 0 epic plan |
| `.gobby/plans/herdr-terminal-client-qa-fixes.md` | Stage 0 rework of `wt-task-20255-m4` |
| `.gobby/plans/project-checkout-identity.md` | #19651 path-independent project identity |
| `.gobby/plans/two-daemon-hub.md`, `.gobby/plans/m0-shared-datastores-bridge.md`, `.gobby/plans/hub-pc-datastore-move.md` | Shared-datastore / lease groundwork |
| `.gobby/plans/daemon-native-runtime-boundary.md` | Still-open Stage 2 foundation |
| `.gobby/plans/completed/reactive-config-store.md` | Landed Stage 2 foundation |
| `.gobby/plans/completed/account-identity-machine-ownership.md` | Landed identity |
| `.gobby/plans/machine-scoped-worktrees-clones.md` | Stage 3 machine-scoped worktrees |
| `.gobby/plans/shared-remote-stack.md` | Stage 3 remote-stack strategy |
| `docs/architecture/hub-owned-files-home.md`, #20238 | Hub-owned `USER.md`, wiki home, `_personal` life-admin, and `attachments/`; destination HTTP under `/api/hub/...` |
| `ROADMAP.md` | Release-line what/when; the 0.6.0 sidecar on `:60890` is the transition vehicle; destination daemon is `gdaemon` |
| #21490 | Hook-envelope ledger deferral (decision 9) |
