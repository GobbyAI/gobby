# Gobby Architecture Evolution

The big-picture path from today's local-first Python daemon to the
hub-and-node Rust platform, with the decisions that shape it and citations to the plans and
tasks that carry each stage. `ROADMAP.md` tracks what/when; this document is
the why/how. Decided 2026-08-13 during the herdr-terminal-client planning
session (task #18802); update it when a stage decision changes, not for
routine progress.

## Where we are (2026-08)

- **Runtime**: the 0.5.0 Python daemon is the supported local-first runtime —
  sessions, tasks, memory, workflows, rules, pipelines, agents, MCP proxy.
  Operators start it with `gobby start`. HTTP `:60887`, WS `:60888`.
- **Data**: PostgreSQL is the runtime hub; FalkorDB graph; Qdrant vectors.
  Schema authority already lives in Rust — `gcore` embeds baseline 375 and
  `gdaemon schema apply/verify` owns DDL (`crates/gcore/src/schema/assets.rs`).
  M0 shared-datastore groundwork (advisory-lease active/standby daemons,
  two-daemon convergence) has landed (`.gobby/plans/m0-shared-datastores-bridge.md`,
  `.gobby/plans/two-daemon-hub.md`, `.gobby/plans/hub-pc-datastore-move.md`).
- **Rust bridgehead**: `crates/` ships `gcode`, `gwiki`, `ghook`, `gdaemon`
  over the shared `gcore` library — the strangler beachhead for the daemon
  port.
- **Terminals**: tmux-based, with documented defects for Gobby-managed
  terminals (cross-socket identity collisions, smallest-client sizing,
  duplicated VT replies — `.gobby/plans/completed/activity-panel-live-terminal.md`).

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
  worktrees, agent spawning). The interactive product people run is `gobby`
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
The earlier idea-only exploration was epic #18520
(`.gobby/plans/completed/herdr-interface-backend-foundation.md`, superseded
on licensing by the fork).

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
route families per the roadmap. The terminal stack is untouched by
construction: the new daemon adopts the same gterm host over the same
protocols.

**Stage 2 — standalone parity.** The Rust `gdaemon` replaces the Python
daemon on a single machine; the Python package retires, freeing the `gobby`
name: `gclient` is renamed `gobby` and carries the operator verbs (`start`,
`stop`, `status`, `tasks`, …) as subcommands over the public API — the binary
is Zig-free, so carrying the CLI costs nothing. Story A is fully
served by `gdaemon` plus `gterm` and `gobby`. Related in-flight foundations:
`.gobby/plans/daemon-native-runtime-boundary.md`,
`.gobby/plans/reactive-config-store.md`.

**Stage 3 — mode split and the hub.** `hub` and `node` modes, machine
registration over WS, API keys minted at the network boundary (the boundary
recorded in the account-identity work:
`.gobby/plans/account-identity-machine-ownership.md`,
`.gobby/plans/machine-scoped-worktrees-clones.md`,
`.gobby/plans/shared-remote-stack.md`). Stories B and C go live: self-hosted
hubs first, then the gobby.ai-provided data stack + hub. The terminal plan's
deferrals land here — remote `gobby` attach with capability tokens (#20202)
and the hosted relay privacy stance (#20203).

**Alongside, stage-independent:** the plugin system (#20201) once `gobby` and
the public API surface exist; Gobby Pro fleet surfaces per `ROADMAP.md`
0.6.0+/0.7.0+.

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

## Citations

| Ref | What |
| --- | --- |
| #18802 | This planning task (herdr-derived terminal client + native PTY migration) |
| #18520 | Closed idea-stage epic (herdr interface backend foundation) |
| #20201 | Plugin-system plan (deferred from herdr-terminal-client D1) |
| #20202 | Remote `gobby` attach plan (deferred, D2) |
| #20203 | Hosted terminal-relay privacy stance (deferred, D3) |
| `.gobby/plans/herdr-terminal-client.md` | Stage 0 epic plan |
| `.gobby/plans/two-daemon-hub.md`, `.gobby/plans/m0-shared-datastores-bridge.md`, `.gobby/plans/hub-pc-datastore-move.md` | Shared-datastore / lease groundwork |
| `.gobby/plans/daemon-native-runtime-boundary.md`, `.gobby/plans/reactive-config-store.md` | Rust-daemon boundary foundations |
| `.gobby/plans/account-identity-machine-ownership.md`, `.gobby/plans/machine-scoped-worktrees-clones.md`, `.gobby/plans/shared-remote-stack.md` | Stage 3 identity/machine/remote groundwork |
| `ROADMAP.md` | Release-line what/when; the 0.6.0 sidecar on `:60890` is the transition vehicle; destination daemon is `gdaemon` |
