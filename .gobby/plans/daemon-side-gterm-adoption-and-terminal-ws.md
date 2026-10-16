Plan artifact: `.gobby/plans/daemon-side-gterm-adoption-and-terminal-ws.md`

# Daemon-side gterm adoption and terminal WS

**Plan ID:** daemon-side-gterm-adoption-and-terminal-ws

## A1 Planning authority and decision record

`kind: framing`

Director brief, verbatim:

'Scope the plan as two milestones: M1 gterm adoption by epoch (host_manager, leases, host_reconcile: a daemon restart never kills an agent terminal; no dependency on #21558), M2 terminal WS plus proxy relay (after #21558 Native WS transport in gdaemon). Implementation starts after #22722 lands. gclient/gterm is the primary and default backend for every agent; tmux is a bounded fallback with the fallback, visibility and recovery criteria the task carries.'

Superseded in part: #22856 (closed 2026-10-04) removed every tmux spawn path ("no spawn, launch
or agent path may create, use or reach a tmux session"), and the Orchestrator's option-B ruling
under it (2026-10-14) replaced the brief's bounded tmux fallback with fail-closed spawn. The
visibility and recovery criteria stand (1.7).

This plan ports the daemon-owned half of the terminal stack behind the Stage 2
`RouteFamily` seam without changing `gterm`, gclient production/runtime code, or any wire
protocol. It has two milestones and deliberately keeps the #21558 dependency off every M1
deliverable.

Decision record:

- **Family boundary — restraint rung 2 (reuse):** create `crates/gterminals` with package
  `gobby-terminals`, `publish = false`, workspace-inherited version, and a public terminal
  `RouteFamily`, exactly following #21544 and `crates/AGENTS.md`. Do not put terminal-family
  state in `gdaemon` or create a plugin mechanism.
- **Protocol ownership — restraint rung 1 (does not need to exist):** do not change or
  generalize the JSON-lines control protocol, bincode frame protocol, or backend-neutral WS
  messages. The Rust daemon client implements the already-observed bytes and replays the
  canonical corpus; it does not move types into `gterminal` or make gterm internals public.
- **Strangler ownership — restraint rung 2 (reuse):** reuse the Stage 1
  `Proxy | Compare | Native` route-family mode. In `Proxy` and `Compare`, the Rust host core is
  an observer: it may connect, authenticate, validate the epoch, and report health, but it may
  not spawn, restart, drain, rotate the token, or reconcile rows. Python remains the sole
  supervisor. In `Native`, Rust is the sole supervisor and Python terminal wiring does not
  construct `TerminalHostManager` or `WriteCoordinator`, and owns no terminal lease. Its one
  `TerminalLeaseRegistry` only sequences workspace events for the Python websocket server and
  claims no attachment (2.10). This avoids a second supervisor and adds no terminal-specific mode
  knob.
- **Restart semantics — restraint rung 1 (remove a contradictory option):** ordinary stop or
  restart closes daemon clients and leaves the gterm host and its terminals alive. Remove the
  `terminals.stop_host_on_shutdown` override because it contradicts the required invariant.
  Only the existing explicit `gobby stop --terminals` / restart-with-terminals intent may drain
  the host.
- **Backend choice — restraint rung 1 (remove authoring surface):** remove the user/config
  backend choice. Every producer supplies an internal terminal lifetime, not a backend; native
  gterm is the only spawn backend. An agent definition containing `terminal_backend` is rejected
  both when stored and when a legacy row is resolved.
- **Host-unavailable reasons — Orchestrator ruling (2026-10-14, option B, under #22856),
  restraint rung 6:** the host manager records a closed typed reason for its latest failed start
  or adoption: `host_start_timeout`, `epoch_refused`, `gterm_missing`, or `other`. The first
  three fail a spawn closed, before any terminal row, process, or message exists, with a
  diagnostic code naming the reason; `other` keeps the spawn path's existing codes.
  `epoch_refused` is emitted only after a reachable host completes authenticated hello, protocol
  validation, ping, and pid-identity validation but its coherent returned epoch is explicitly
  rejected by the durable adoption matrix. Token, pid, protocol, authentication,
  pre/post-adoption I/O, and all later runtime failures remain distinct fail-closed results. No
  spawn path reaches tmux.
- **Lifetime producer — restraint rung 2 (reuse):** a non-user-settable lifetime enum has only
  `run` and `persistent_role`. Every bare/public `SpawnRequest` shape materializes omission as
  `run`. Leaf 1.9 adds nullable `workspace_panes.role`; the exact role-bound producer is
  `WorkspaceOps._fill`: a non-empty durable `WorkspacePane.role` emits `persistent_role`, while a
  missing/empty role emits `run`. Since #22856 no lifetime reaches tmux, so both fail closed
  alike when the host is unavailable (1.7).
- **Role column — PD ruling (gobby#14972, 2026-10-01):** this plan owns the column as one M1
  leaf (1.9): a gcore migration, `WorkspacePane.role`, and an optional `role` argument on the
  `tab.create` and `pane.split` workspace ops. #22895, `Plan runbooks as existing pipelines with
  placed agent launch`, consumes that contract and adds no second column or op argument.
- **Tmux writes — Orchestrator ruling (2026-10-15, option A), restraint rung 2:** tmux rows exist
  only for hand-started panes (#22856), and #23214 (1.8, the complete Rust tmux adapter) closed
  obsolete on 2026-10-14. 2.11 adds the write-only `crates/gterminals/src/tmux_write.rs` (Text,
  Key, Paste, Input, resize, and release_size on the row's recorded socket, with a generation
  check before each operation), and Python keeps the spawn-less `TmuxTerminalRuntime` for reads,
  liveness, and termination. Do not add a generic runtime framework or change gclient production
  code.
- **Recovery — restraint rung 1 (no migration daemon):** each spawn reads the host's current
  state. Once the health loop re-adopts the host, the next ordinary spawn uses gterm with no
  operator action, and a failed spawn leaves nothing to clean up. Existing tmux rows
  (hand-started panes only, #22856) are never mutated or migrated.
- **Inventory visibility — restraint rung 2 (reuse):** preserve the existing `backend` field in
  HTTP and WS inventory and pin the existing gclient and web renderers with contract tests. Add
  no new badge system or client setting.
- **Adoption wait — restraint rung 6 (minimum code):** use a fixed 2.5-second startup/adoption
  decision deadline, matching the existing terminal attach startup wait. It is a constant, not a
  new config knob. Health retries continue after a degraded decision.
- **Cutover proof — restraint rung 2 (reuse):** the canonical fixture corpus and isolated
  restart and fail-closed integration tests are the comparison mechanism. Do not dual-execute live
  writes in `Compare`; duplicate input is unsafe and the byte corpus already supplies the
  deterministic comparison.

## A2 Dependency evidence and lifecycle disposition

`kind: framing`

The live task graph and task rows were checked on 2026-09-22 and rechecked on 2026-10-01
against 0.5.0 at 7f402248e9. Exact Stage 0 evidence:

- #21357, `Plan native runtime completion: daemon/host hardening and the native default`,
  closed at `2026-09-10T20:23:28.944113+00:00` (commit `1ce889441d`). It has no descendants.
- #22076, `Native runtime completion: daemon/host hardening and the native default`, closed at
  `2026-09-17T15:29:06.052236+00:00`. A recursive child query returned `count=28` and
  `open_count=0`.
- #22086, `Typed native spawn failure outcomes and singleflight host respawn`, closed at
  `2026-09-12T23:28:08.271052+00:00`. This is the named host-respawn hardening leaf.
- #21214, `Resolve the terminal runtime per Terminal.backend in WriteCoordinator`, closed at
  `2026-08-29T15:14:29.679922+00:00` (commit `b80ae87f35`). This is the named
  WriteCoordinator hardening leaf.
- #22447 remains open, but it is a Windows cross-toolchain/package decision, not Stage 0 host
  respawn or WriteCoordinator hardening.

**Supported #21334 disposition — restraint rung 1:** keep the already-removed #21334 → #21565
edge removed. Its complete Stage 0 reason no longer blocks this task, and no task lifecycle
mutation is required during planning.

**Global implementation gate: satisfied.** #22722, `Terminal write layer fails silently: an
unreadable composer draft scores as submitted, and send_keys pastes key names as text on native
panes`, closed at `2026-09-23T05:56:14.889398+00:00` with commits `181fb16c66`, `32efc36c05`,
`68a9f35f06`, and `3cdcd8874f`. The rest of the frozen restart-8 set is closed: #22708, #22709,
#22710, #22712, and #22716. The Program Director (gobby#14972) recorded restart #8 complete on
2026-10-01: all four #22722 commits are ancestors of `340cba4495`, which the live daemon runs.
No gate edge is attached on expansion. The implementation ports the landed fail-closed write
layer as 1.5 scopes it; it must not port a pre-#22722 snapshot.

**Correction — Adversary F3 (2026-10-01):** one #22722 promise did not land.
`pane_io.py::submit_text` (512-526) still returns `SubmitResult(True)` when the composer is
unreadable after write and Enter, and `3cdcd8874f` already carries that branch. The PD routed
the fix as #23188, `pane_io.submit_text reports success when the composer is unreadable after
write and Enter`. Its chosen contract is the oracle for 1.5 and 2.7.1.

**#21558 disposition — Orchestrator ruling (gobby#15977, 2026-10-15) under Josh's no-deferral
rule (memory 878d0c35):** #21558, `Native WS transport in gdaemon`, gates only the WS work: 2.2,
2.3, 2.12, and 2.13. Those are in-scope deliverables. 2.2 carries a cross-plan "blocked by"
edge to #21558 (Cross-plan edges set at expansion), and 2.3, 2.12, and 2.13 follow 2.2 through
heading dependencies. This replaces the 2026-10-01 PD ruling's D2 deferral. #23219, the D2
holder task, is superseded by those four deliverables and is retired at expansion. Its carried
obligations land in 2.2.7 (LOW-2), 2.8.6 (LOW-1), and 2.13.7 (the five restored tests).

**Role column ownership:** leaf 1.9 owns `workspace_panes.role`, `WorkspacePane.role`, and the
optional `role` op argument (A1 ruling). 1.6 and 1.7 depend on 1.9 inside M1, so no external
role blocker remains and #21558 stays out of M1.

**#23076 lease dependency:** #23076, `gclient reconnects straight to the host`, closed on
2026-10-03 with the input-grant finalization split and its durable handoff record. 1.4 ports
the registry, and 1.10 ports the grant split and handoff record. #23218 executed 1.10 (closed
2026-10-14, landing `6322c1874f`), so 1.10 records landed work and replaces the D1 deferral.

**1.7 disposition — Orchestrator ruling (gobby#15977, 2026-10-14, option B):** #22856 removed
every tmux spawn path, so #23213 replaced the bounded tmux fallback with fail-closed spawn on
typed host reasons and closed on 2026-10-15. 1.7 records that landed contract. The fallback
decision, the attempting-fallback project message, `locator.fallback_reason`, and the planned
`spawn_executor_terminal.py` module are dropped.

**1.8 disposition:** #23214, `Rust tmux runtime parity for live fallback rows`, closed obsolete
on 2026-10-14 under the same ruling, because no live fallback row can exist. 1.8 is framing that
names the replacement owners: 2.11 for tmux writes and sizing, Python's spawn-less
`TmuxTerminalRuntime` for reads, liveness, and termination, and 2.2 for WS attach.

## A3 Scope, invariants, and observed evidence

`kind: framing`

In scope:

- A workspace-private `gobby-terminals` crate over the #21557 gcore async pool/transaction seam.
- Epoch-authenticated gterm control/event/frame clients, host adoption/supervision, terminal rows,
  reconciliation, attachment leases, and the post-#22722 write coordinator.
- The current agent-spawn ingress cleanup and the fail-closed host-unavailable spawn policy,
  before the agent family is absorbed by #21564. The fixed policy is a required contract of that
  later `TerminalRuntime` seam.
- The Rust terminal inventory reads and `/api/terminals` handlers (2.14), and the `Native` lane
  viewport read from the Rust lease registry (2.15).
- After #21558, the native terminal WS handler, proxy relay, family routing, and cutover proof
  (2.2, 2.3, 2.12, and 2.13).

Out of scope:

- Any change under `crates/gterminal/`; any production/runtime change under
  `crates/gclient/src/`. The only allowed gclient edit is the named
  `crates/gclient/tests/client_loop.rs` backend-visibility contract update.
- Any change to the three existing wire protocols or to canonical fixture bytes.
- Live migration of an existing tmux terminal, a selectable backend in agent definitions, any
  spawn path that creates, uses, or reaches a tmux session (#22856), an operator recovery
  action, or a new terminal configuration knob.
- Windows cross-toolchain resolution tracked by #22447.

Observed source evidence used by executors:

- `TerminalHostManager._try_adopt` currently keeps protocol/token/pid-identity mismatch hosts alive
  and retries; it has no explicit semantic epoch-refusal subtype. The Rust port adds that narrow
  subtype only after a successful handshake and durable epoch check. `stop(drain_host=False)`
  closes clients but preserves the host; `reconcile_host_inventory` owns row settlement.
- `TerminalLeaseRegistry` stamps lifecycle messages with daemon epoch and ordered sequence,
  bounds its publication queue, and fails closed; `WriteCoordinator` serializes/revalidates each
  write and resolves the runtime from the row's backend. #22722's fail-closed submit semantics
  landed one layer above it, in `src/gobby/terminals/pane_io.py` and
  `src/gobby/terminals/workspace_writes.py`; `write_coordinator.py` changed after the original
  draft only for #22877.
- The task's original golden path is stale. The canonical corpus is
  `tests/fixtures/terminal_ws_golden/manifest.json`, replayed by
  `tests/servers/test_terminal_ws_golden.py` and `crates/gclient/tests/ws_golden.rs`.
- `TerminalManager` already stores `backend`, `host_epoch`, and backend-owned `locator` JSON;
  `ws_protocol.inventory_item`, `/api/terminals`, gclient inventory/pane/orphan rendering, and the
  web terminal session list already carry or display backend.
- The exact current backend-option producer sweep covers agent spawn models/executor, MCP
  factory/implementation/request, HTTP agent spawn, CLI agents, dispatch actions/spawn, scheduler
  execution, config, host shutdown, and agent-definition JSON storage/resolution. The Ask
  subsystem was retired by #23055 (`30d66635c1`) and is no longer a producer.
- Leaf 1.9 adds the durable `workspace_panes.role` column and `WorkspacePane.role` projection;
  the existing terminal creation producer is `WorkspaceOps._fill`, which calls
  `spawn_web_terminal`. That exact producer becomes the sole `persistent_role` source.
- A repository-wide Rust tmux sweep found no runtime adapter. The current behavior oracle is
  `src/gobby/terminals/tmux_runtime.py::TmuxTerminalRuntime`: per-row socket routing, raw input
  and paste/write, resize, bounded snapshot/history, live attach locator, pane/session liveness,
  and termination. Under the Orchestrator's tmux ruling (2026-10-15), 2.11's `tmux_write.rs`
  ports its writes, resize, and release_size; the rest stays in Python's spawn-less runtime.

## Cross-plan edges set at expansion

`kind: framing`

Each row is one edge between a leaf of this plan and work outside it. With "blocked by", the
external work closes before the leaf starts. With "blocks", the leaf closes before the external
work starts. Edges between this plan's own leaves are heading dependencies and reach the
manifest. The Orchestrator sets these edges at expansion and owns their direction; a row whose
external work has not started can be flipped without changing this plan.

| Leaf | External work | Edge | Reason |
| --- | --- | --- | --- |
| 2.2 | #21558 (Native WS transport in gdaemon) | blocked by | 2.2's handler rides #21558's native WS accept, auth, subscription, and broadcast transport and its outer envelope. 2.3, 2.12, and 2.13 follow 2.2. |
| 2.4 | node-halves 2.2 (Node terminal host, #24371) | blocks | Both change `crates/gdaemon/src/serve.rs::run`. |
| 2.6 | native-ws-transport A3 (Bounded native broadcast fanout, #23880) | blocked by | Both change `crates/gdaemon/src/front_door/mod.rs::FrontDoor::handle`. |
| 2.6 | node-halves 1.5 (Node hook ingress arm, #24368) | blocks | Both change `crates/gdaemon/src/front_door/mod.rs::FrontDoor::handle`. |

Relations that set no edge:
- node-halves 2.1 (Durable node agent state, #24370) changes `crates/gdaemon/src/state.rs`
  through `NodeServices`; 2.4 and 2.6 change `RuntimeState`, a different struct.
- node-halves 2.10 (#24379) adds a snapshot method to `crates/gterminals/src/control.rs`; 2.5,
  2.8, and 2.2 add `write_batch`, `revoke_input`, and `grant_input`, distinct new methods.
- hub-mode 1.2 (gdaemon internal node-command route over the node channel) changes
  `FrontDoor::handle` after node-halves 1.5 through node-halves 2.1, so it follows 2.6 through
  the 2.6 to node-halves 1.5 row.
- #23219 (#21558 terminal WS operations, relay, and family cutover) is retired at expansion,
  superseded by 2.2, 2.3, 2.12, and 2.13 (A2).

## P1: Milestone M1 — gterm adoption by epoch

`kind: framing`

M1 has no dependency on #21558. It establishes the Rust terminal core, epoch-preserving daemon
restart behavior, current-agent backend policy, and fail-closed spawn. It may merge while the
terminal WS route remains proxied. In `Proxy`/`Compare` the Rust host core is observer-only; the
full owner state machine is exercised in isolated tests and becomes live only with the M2
`Native` route cutover.

### 1.1 Workspace-private terminal crate and unchanged host-wire clients [category: code]

`kind: deliverable`

Targets:
- `Cargo.toml`
- `crates/gdaemon/Cargo.toml`
- `crates/gterminals/Cargo.toml`
- `crates/gterminals/src/lib.rs`
- `crates/gterminals/src/control.rs`
- `crates/gterminals/src/frames.rs`
- `crates/gterminals/tests/protocol_contract.rs`

Research context:

- #21544 requires one workspace-private crate per family and a `RouteFamily` export. The current
  host reads newline-delimited JSON in `crates/gterminal/src/host/control.rs` and length-prefixed
  bincode frames in `crates/gterminal/src/host/frames.rs`; those files are read-only references.
- The Python reference clients are `src/gobby/terminals/host_client.py`,
  `host_events.py`, and `frame_client.py`. Control authentication, protocol version, request ID,
  event cursor, and frame limits are trust boundaries and are not simplified.
- **Choice — restraint rung 2:** reuse workspace dependencies already used by gdaemon/gterminal
  (`tokio`, `serde`, `serde_json`, `bincode`, `anyhow`/typed errors); add no protocol generator or
  new serialization dependency.
- **Granularity:** seven target files are retained as one leaf because crate registration and the
  two read-only wire clients form one independently testable compile/protocol boundary; splitting
  registration would create a non-runnable leaf. The control and frame state machines are small,
  independently unit-tested modules behind that boundary.

Implement bounded clients that authenticate using the existing control token, validate protocol
version and safe response IDs, resume the event stream by cursor, and encode/decode frames exactly
as gterm does. No symbol in gterminal is made public merely for reuse; the wire contract, not the
server implementation, is the seam.

**Acceptance:**

- 1.1.1 - `gobby-terminals` is a workspace member, is `publish = false`, inherits workspace
  version/edition/lints, exports the terminal family API, and is linked by gdaemon. file:
  `crates/gterminals/Cargo.toml`.
- 1.1.2 - The control client round-trips hello, ping, inventory, spawn prepare/commit/abort,
  terminate, write, resize, observer reservation, and event subscription over the existing
  newline JSON contract without changing gterm. test:
  `crates/gterminals/tests/protocol_contract.rs::control_json_lines_match_gterm`.
- 1.1.3 - The event client rejects a changed epoch or non-monotonic sequence, resumes from the
  last committed cursor, and reports an unreplayable cursor as a reconcile requirement rather
  than silently skipping events. test:
  `crates/gterminals/tests/protocol_contract.rs::event_epoch_and_cursor_are_fail_closed`.
- 1.1.4 - The frame client enforces the existing length/queue ceilings and reproduces bincode
  attach, input, output, history, lifecycle, and close frames byte-for-byte. test:
  `crates/gterminals/tests/protocol_contract.rs::frame_bytes_match_gterm`.
- 1.1.5 - The implementation diff contains no change under `crates/gterminal/`, no
  production/runtime change under `crates/gclient/src/`, and no protocol or fixture-byte change.
  The only allowed gclient edit in the whole plan is the named
  `crates/gclient/tests/client_loop.rs` backend-visibility contract update. behavior:
  `gterm-gclient-protocol-scope-guard`.

### 1.2 Epoch adoption, single-owner supervision, and restart preservation [category: code] (depends: 1.1, 1.6)

`kind: deliverable`

Targets:
- `crates/gterminals/src/host.rs`
- `crates/gterminals/src/family.rs`
- `crates/gdaemon/src/serve.rs::*` — scope-reason: start the terminal family host lifecycle and close its clients on ordinary stop
- `crates/gdaemon/src/front_door/routes.rs::*` — scope-reason: register the terminal family in observer or owner mode by route mode
- `src/gobby/runner_init/terminal_wiring.py::init_terminal_wiring`
- `src/gobby/terminals/host_manager.py::*` — scope-reason: extract shutdown helpers into host_shutdown.py and remove the config-driven drain read while preserving the Proxy-mode manager
- `src/gobby/config/terminals.py::TerminalConfig`
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerate the TerminalConfig carrier after removing stop_host_on_shutdown
- `tests/contracts/http/config_schema.json::*` — scope-reason: regenerate the HTTP config schema contract without stop_host_on_shutdown
- `tests/contracts/http/config_values.json::*` — scope-reason: regenerate the HTTP config values contract without stop_host_on_shutdown
- `tests/config/test_terminal_host.py::*` — scope-reason: remove ordinary-shutdown drain configuration expectations
- `docs/guides/cli-commands.md`
- `src/gobby/terminals/host_shutdown.py`
- `tests/terminals/test_host_shutdown_preservation.py::*` — scope-reason: replace the contradictory config-opt-in case with the invariant and explicit-drain cases
- `tests/terminals/test_host_manager.py::*` — scope-reason: pin the extracted Python shutdown seam while Proxy mode remains available
- `crates/gterminals/tests/host_lifecycle.rs`

Research context:

- `TerminalHostManager._try_adopt`, `_spawn_candidate`, `_health_loop`, and
  `stop(drain_host=False)` are the behavior oracle. The manager is 913 lines, so it is not edited
  or copied as one Rust module; transport is already isolated in 1.1 and reconciliation in 1.3.
- A reachable host that refuses adoption remains alive. `epoch_refused` is narrower than the
  Python oracle's current catch-all mismatch: connection, authenticated hello, compatible
  protocol, ping, and pid identity must all succeed before the durable adoption matrix can reject
  the returned epoch. Token/pid/protocol/auth/I/O failures retain their own typed failures. The
  daemon must not rotate credentials, unlink sockets, kill a reachable host, or spawn a second
  host. Confirmed absence is the only path to spawn.
- **Choice — restraint rung 2:** use the existing route-family mode as the ownership switch;
  observer mode has no mutation capability. Do not coordinate two active supervisors with a new
  lease or lock.
- **Choice — restraint rung 1:** delete `stop_host_on_shutdown` with its one reader
  (`host_manager.py:270`) and its documentation (`docs/guides/cli-commands.md:120`); it is the
  only non-explicit path that violates restart preservation. Preserve the existing explicit
  drain intent.
- **Granularity:** the listed targets are one lifecycle leaf because the owner/observer switch, Python
  wiring exclusion, and gdaemon stop behavior must change together. Splitting them creates a
  double-supervisor or no-supervisor interval.

**Decomposition:** `host_manager.py` is 913 lines. Move the existing drain request, host-exit wait,
process liveness/reap, and explicit-shutdown helpers into new `host_shutdown.py`; leave
`TerminalHostManager.stop` as the lifecycle coordinator. The extracted helper accepts only the
explicit drain decision, so removal of `stop_host_on_shutdown` cannot be bypassed inside the large
manager.

Implement one host lifecycle task. It probes the installed socket first, authenticates, records
the returned epoch/pid/version, and only spawns `~/.gobby/bin/gterm host` after confirmed absence.
The startup decision settles within 2.5 seconds for callers, while health retry continues with
bounded backoff. Normal shutdown cancels event/health tasks and closes clients only. Explicit
terminal drain sends the existing shutdown request and waits for the host process boundary.

Consumers unchanged:
- `src/gobby/runner_init/orchestration.py` — no-edit-reason: It calls `init_terminal_wiring` with the unchanged runner/config signature; ownership selection remains inside the wiring function.
- `src/gobby/app_context.py` — no-edit-reason: It stores TerminalConfig as an aggregate type and reads no removed field.
- `src/gobby/config/app.py` — no-edit-reason: The nested TerminalConfig field and loader contract remain unchanged.
- `src/gobby/runner.py` — no-edit-reason: It passes TerminalConfig but chooses no shutdown policy.
- `tests/terminals/acceptance/conftest.py` — no-edit-reason: It builds terminal-runtime requests and reads no removed TerminalConfig field.
- `tests/terminals/test_composition_roots.py` — no-edit-reason: Composition reads TerminalConfig as a whole and no removed field.
- `tests/test_runner_lifecycle_processes.py` — no-edit-reason: Runner lifecycle supplies explicit shutdown intent and reads no removed config field.
- `tests/e2e/conftest.py` — no-edit-reason: It builds a default `TerminalConfig()` and reads no removed field.
- `tests/terminals/test_host_upgrade.py` — no-edit-reason: It builds a default `TerminalConfig()` and reads no removed field.
- `tests/terminals/test_input_grant_rebind.py` — no-edit-reason: It builds a default `TerminalConfig()` and reads no removed field.

**Acceptance:**

- 1.2.1 - Same-epoch startup adopts the existing gterm pid and epoch without spawning, rotating
  credentials, or rewriting sockets; two concurrent callers share one adoption/spawn attempt.
  test: `crates/gterminals/tests/host_lifecycle.rs::same_epoch_adopts_singleflight`.
- 1.2.2 - Only a reachable, authenticated, protocol-compatible, pid-verified host whose coherent
  epoch the durable adoption matrix explicitly rejects yields `epoch_refused`; it stays alive,
  health retry remains armed, and no second host is spawned or killed. test:
  `crates/gterminals/tests/host_lifecycle.rs::semantic_epoch_refusal_is_non_destructive`.
- 1.2.3 - An exhaustive subtype matrix proves token mismatch, pid mismatch, protocol mismatch,
  authentication failure, pre-adoption I/O, and post-adoption I/O never map to `epoch_refused`;
  confirmed absence alone may launch once, a missing executable yields `gterm_missing`,
  and the 2.5-second ready deadline yields `host_start_timeout`. test:
  `crates/gterminals/tests/host_lifecycle.rs::adoption_result_subtypes_are_exhaustive`.
- 1.2.4 - In `Proxy` and `Compare`, Rust performs observation only and Python is the active
  supervisor; in `Native`, Rust is the active supervisor and Python constructs none of its host,
  lease, or write owners. test:
  `crates/gterminals/tests/host_lifecycle.rs::route_mode_has_exactly_one_supervisor`.
- 1.2.5 - Ordinary gdaemon stop/start/restart preserves the host pid, epoch, terminal id, and
  bidirectional terminal I/O; `TerminalConfig` has no `stop_host_on_shutdown`, config containing
  it is rejected as unknown, and no ordinary config can request a drain. test:
  `crates/gterminals/tests/host_lifecycle.rs::daemon_restart_preserves_live_terminal`.
- 1.2.6 - Only the explicit terminals-drain shutdown intent terminates the host and its native
  terminals; the next start creates a fresh epoch. test:
  `tests/terminals/test_host_shutdown_preservation.py::test_explicit_terminal_drain_is_the_only_host_shutdown_path`.

### 1.3 Terminal repository and epoch reconciliation [category: code] (depends: 1.2)

`kind: deliverable`

Targets:
- `crates/gterminals/src/repository.rs`
- `crates/gterminals/src/reconcile.rs`
- `crates/gterminals/tests/reconcile.rs`

Research context:

- Port the row/state semantics observed in `TerminalManager` and
  `reconcile_host_inventory`: durable terminal/spawn identity, machine/project ownership,
  pending/live/exited/orphaned settlement, process reap evidence, and host epoch constraints.
- #21544 requires the family to own its row types and repositories over the #21557 gcore async
  transaction seam. There is no schema change: the current terminal columns and constraints are
  sufficient.
- **Choice — restraint rung 2:** reuse the existing table and gcore transactions. Do not add an
  adoption ledger or shadow table.
- **Failure-boundary check (`edge-case-coverage`):** terminal promotion/failure and the matching
  durable spawn/reap evidence commit in one transaction; no separately invoked hook may leave a
  prepared process without its paired row transition.

Implement typed Rust terminal rows and repository queries, then reconcile one host inventory under
the observed epoch. Reconciliation is idempotent and settles by durable identity under a per-row
transaction/lock. It must never act on another machine or a newly changed epoch.

**Acceptance:**

- 1.3.1 - Row decoding/encoding preserves backend, machine/project/session/run identity,
  host_epoch, locator, dimensions, lifecycle state, pending identity, and post-#22722 unresolved
  write/quarantine fields. test:
  `crates/gterminals/tests/reconcile.rs::terminal_row_round_trips_all_runtime_fields`.
- 1.3.2 - Inventory entries matching durable id and spawn key promote pending rows to live in the
  observed epoch; replaying the same inventory is idempotent. test:
  `crates/gterminals/tests/reconcile.rs::matching_inventory_promotes_once`.
- 1.3.3 - Missing inventory settles stale pending attempts and marks only same-machine,
  same-epoch live rows exited/orphaned according to the existing evidence rules; it never mutates
  another machine or a row rebound to a new epoch. test:
  `crates/gterminals/tests/reconcile.rs::absence_is_scoped_to_observed_machine_and_epoch`.
- 1.3.4 - Duplicate/foreign durable identities are killed or rejected exactly as the Python
  oracle specifies, while a reachable refused-adoption host is not reconciled at all. test:
  `crates/gterminals/tests/reconcile.rs::identity_conflicts_fail_closed_without_touching_refused_host`.
- 1.3.5 - Prepared-process settlement and its terminal/run evidence are atomic: injected failure
  at every former boundary leaves either the complete pre-state or complete post-state, never an
  untracked child or live row without evidence. test:
  `crates/gterminals/tests/reconcile.rs::spawn_settlement_is_atomic_across_failure_boundaries`.

### 1.4 Attachment leases and ordered lifecycle publication [category: code] (depends: 1.3)

`kind: deliverable`

Targets:
- `crates/gterminals/src/leases.rs`
- `crates/gterminals/tests/leases.rs`

Research context:

- `TerminalLeaseRegistry` is the complete behavior oracle: daemon epoch plus monotonic lifecycle
  sequence, one controller, deterministic web-over-gclient sizing election, attachment generation,
  scroll state, write sequence/idempotency, and bounded publication.
- #23076, `gclient reconnects straight to the host`, closed on 2026-10-03 with the input-grant
  finalization split and durable handoff record. This leaf ports the
  registry; 1.10 ports the grant split and handoff record.
- **Choice — restraint rung 6:** use one in-memory registry owned by the terminal family. Leases
  are daemon-connection state and do not need a database table or distributed lock. The only
  durable lease fact is 1.10's preserved-handoff record.
- **Failure-boundary check (`edge-case-coverage`):** a holder change and its lifecycle publication
  are one registry transition; queue reservation happens before the lease mutation so saturation
  cannot publish only half of the transition.
- **Granularity:** five acceptance items, one production Target, and one state machine, the
  registry. The durable handoff expiry is a second lifecycle owner (repository CAS and reconcile
  retry), so it is its own deliverable, 1.10.

Port the lease registry as an explicit state machine. All methods validate attachment generation
and terminal identity under the terminal lock. Lifecycle queue saturation/failure faults the
registry and refuses further state changes until the owning WS connection is closed.

**Acceptance:**

- 1.4.1 - Attach/detach/take/release/finalize permits one controller, increments generation, and
  returns the same deterministic control outcomes as Python. test:
  `crates/gterminals/tests/leases.rs::control_holder_transitions_are_deterministic`.
- 1.4.2 - Web wins the sizing tie over gclient, remaining viewers are re-elected on detach, and
  viewport/scroll offsets are isolated by attachment. test:
  `crates/gterminals/tests/leases.rs::sizing_and_scroll_are_attachment_scoped`.
- 1.4.3 - Write admission rejects stale attachment generation, duplicate-conflicting payloads,
  and non-holder input while replaying an identical completed write idempotently. test:
  `crates/gterminals/tests/leases.rs::write_admission_is_generation_and_payload_bound`.
- 1.4.4 - Every lifecycle event carries the current daemon epoch and strictly increasing sequence;
  restart begins a new epoch and clients can unambiguously discard the old stream. test:
  `crates/gterminals/tests/leases.rs::lifecycle_order_is_epoch_scoped`.
- 1.4.5 - A full/failed lifecycle queue leaves the lease state unchanged, closes the affected
  connection through one fault path, and never drops a holder-loss event silently. test:
  `crates/gterminals/tests/leases.rs::publication_failure_is_atomic_and_fail_closed`.

### 1.5 Post-#22722 fail-closed write coordinator [category: code] (depends: 1.3, 1.4)

`kind: deliverable`

Targets:
- `crates/gterminals/src/write.rs`
- `crates/gterminals/tests/write_coordinator.rs`

Research context:

- The oracle is the landed `src/gobby/terminals/write_coordinator.py::WriteCoordinator`:
  per-terminal lock, lease revalidation, runtime-per-row resolution, action-key idempotency,
  unresolved evidence, quarantine, operator release, `run_sequence`, and
  `run_native_wake_batch`. Since the original draft it changed only for #22877.
- #22722's submit layer sits above the coordinator and is not ported:
  `src/gobby/terminals/pane_io.py` (`composer_verdict`, `submit_text`,
  `submit_coordinated_text`, `CoordinatorPaneIO`, and the held `_verified_submits` retry records
  bounded at 32) and `src/gobby/terminals/workspace_writes.py::write_workspace_pane`. Composer
  reads need the Python `DetectionRegistry` CLI manifests (`pane_io.composer_reader`) and no Rust
  composer exists, so a second verdict ladder would drift. The submit layer stays the single
  implementation and drives whichever coordinator is live through `CoordinatorPaneIO`. Its
  unreadable-composer branch follows #23188's contract (A2). The
  held-submit records are per-operation retry state; the coordinator keeps the only action-key
  store.
- The Rust coordinator reports the per-step outcomes that ladder reads: accepted, uncertain with
  the failing step, and typed refusals. 2.7 binds the Python callers to it in `Native` mode.
- Preserve the established runtime-per-row invariant: every write resolves native versus tmux
  from the terminal row, never from a global default. Preserve lease revalidation, idempotency,
  unresolved evidence, automatic-write quarantine, and operator-input release semantics.
- **Choice — restraint rung 2:** reuse terminal repository fields and the lease registry; add no
  write queue table or second idempotency store.
- **Invalidation-path check (`edge-case-coverage`):** specify persist, clear, replace, quarantine,
  operator-release, and exit cleanup separately so a broad invalidation does not erase fresh
  evidence created by the same operation.

Port the final coordinator behind one per-terminal lock. Re-read the terminal and lease after the
lock is acquired, persist unresolved evidence before returning an uncertain result, and never
retry a non-idempotent write merely because the daemon restarted.

**Acceptance:**

- 1.5.1 - Native and tmux rows dispatch through their own runtime; unavailable/unknown backend
  fails closed and no global/config default can redirect a row. test:
  `crates/gterminals/tests/write_coordinator.rs::runtime_is_resolved_from_terminal_row`.
- 1.5.2 - The coordinator serializes per terminal, revalidates row/lease generation after lock
  acquisition, and refuses stale controllers before bytes are emitted. test:
  `crates/gterminals/tests/write_coordinator.rs::stale_lease_never_emits_bytes`.
- 1.5.3 - Same action key plus same payload replays the recorded outcome; same key plus different
  payload conflicts; timeout/ambiguous dispatch persists unresolved evidence and does not retry.
  test: `crates/gterminals/tests/write_coordinator.rs::idempotency_and_uncertainty_are_fail_closed`.
- 1.5.4 - Successful confirmed writes clear only their matching unresolved item; a new uncertain
  write replaces/preserves its own evidence atomically; operator input releases automatic
  quarantine without erasing unrelated evidence; terminal exit clears all terminal-local state.
  test: `crates/gterminals/tests/write_coordinator.rs::each_evidence_mutation_path_is_explicit`.
- 1.5.5 - Native wake batches and multi-step sequences preserve #22722's fail-closed boundaries,
  stop after the first uncertain/failing step, and report the exact step with its accepted,
  uncertain, or typed-refusal outcome.
  test: `crates/gterminals/tests/write_coordinator.rs::batch_and_sequence_stop_at_first_uncertain_step`.

### 1.6 Remove backend authoring and make terminal lifetime explicit [category: code] (depends: 1.9)

`kind: deliverable`

Targets:
- `src/gobby/config/terminals.py::TerminalConfig`
- `src/gobby/install/shared/config/config.yaml::*` — scope-reason: delete the default_backend key (landed in #23212)
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerate the TerminalConfig carrier after removing default_backend
- `src/gobby/storage/definitions/agents.py::parent_body`
- `src/gobby/agents/spawn_models.py::*` — scope-reason: delete resolve_terminal_backend (landed in #23212) and carry the internal lifetime on SpawnRequest
- `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py::create_spawn_agent_registry`
- `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py::_load_agent_body`
- `tests/mcp_proxy/tools/spawn_agent/test_load_agent_body.py::*` — scope-reason: cover rejection of a legacy terminal_backend body on load
- `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py::spawn_agent_impl`
- `src/gobby/mcp_proxy/tools/spawn_agent/_terminal_contract.py`
- `src/gobby/mcp_proxy/tools/spawn_agent/_request.py::build_spawn_request`
- `src/gobby/servers/routes/agent_spawn.py::AgentSpawnRequest`
- `src/gobby/servers/routes/agent_spawn.py::create_agent_spawn_router`
- `src/gobby/servers/websocket/terminal_ws_create.py::TerminalCreateMixin._handle_terminal_create`
- `src/gobby/terminals/lifetime.py`
- `src/gobby/terminals/workspace_ops.py::WorkspaceOps._fill`
- `src/gobby/terminals/web_spawn.py::spawn_web_terminal`
- `src/gobby/agents/spawn_executor.py::resolve_terminal_services`
- `src/gobby/cli/agents.py::spawn_agent_cmd`
- `src/gobby/dispatch/actions.py::SpawnAgentAction`
- `src/gobby/dispatch/spawn.py::spawn_agent`
- `src/gobby/scheduler/executor.py::CronExecutor._execute_agent_spawn`
- `tests/agents/test_backend_ingress.py::*` — scope-reason: cover every public producer and internal lifetime producer
- `tests/agents/test_local_context_setup.py::*` — scope-reason: remove legacy backend arguments from direct MCP spawn calls
- `tests/agents/test_native_spawn.py::*` — scope-reason: replace configurable backend fixtures with native-first lifetime fixtures
- `tests/agents/test_spawn_executor_droid.py::*` — scope-reason: update direct SpawnRequest construction for the internal lifetime contract
- `tests/agents/test_spawn_executor_providers.py::*` — scope-reason: update provider request fixtures and prove providers cannot choose a backend
- `tests/agents/test_srt_spawn.py::*` — scope-reason: update sandboxed spawn fixtures without exposing backend choice
- `tests/agents/test_verified_review_regressions.py::*` — scope-reason: preserve reviewed spawn regressions under native-first selection
- `tests/contracts/http/config_schema.json::*` — scope-reason: regenerate the HTTP config schema contract without default_backend
- `tests/contracts/http/config_values.json::*` — scope-reason: regenerate the HTTP config values contract without default_backend
- `docs/guides/gterminal-development-guide.md`
- `tests/cli/test_agents_coverage.py::*` — scope-reason: remove backend CLI-option coverage
- `tests/cli/test_cli_agents.py::*` — scope-reason: assert the CLI has no backend selector
- `tests/config/test_native_backend_flip.py::*` — scope-reason: replace configurable backend flip cases with fixed native behavior
- `tests/config/test_terminal_config.py::*` — scope-reason: assert removed keys are rejected and regenerate config expectations
- `tests/dispatch/test_spawn_forwarding.py::*` — scope-reason: remove dispatch backend forwarding and pin native-first execution
- `tests/e2e/test_terminal_client_stack.py::*` — scope-reason: update end-to-end config fixture to the fixed backend policy
- `tests/mcp_proxy/tools/spawn_agent/test_agy_gate.py::*` — scope-reason: remove direct backend request construction
- `tests/mcp_proxy/tools/spawn_agent/test_error_handling.py::*` — scope-reason: reject legacy backend input at the MCP boundary
- `tests/mcp_proxy/tools/spawn_agent/test_event_loop.py::*` — scope-reason: remove backend parameter from tool invocations
- `tests/mcp_proxy/tools/spawn_agent/test_factory.py::*` — scope-reason: assert generated MCP schema omits terminal_backend
- `tests/mcp_proxy/tools/spawn_agent/test_worktree_reference_resolution.py::*` — scope-reason: remove backend parameter from worktree spawn fixtures
- `tests/mcp_proxy/tools/test_agents_spawn_tools.py::*` — scope-reason: update tool surface contract after backend removal
- `tests/mcp_proxy/tools/test_spawn_agent_impl_provider.py::*` — scope-reason: update implementation calls and preserve provider selection only
- `tests/servers/test_terminal_list_watermark.py::*` — scope-reason: replace default_backend server fixture with fixed native service wiring
- `tests/servers/test_terminal_ws_create.py::*` — scope-reason: assert terminal creation is native-first with no config selector
- `tests/servers/test_terminal_ws_golden.py::*` — scope-reason: remove default_backend fixture while preserving golden bytes
- `tests/tasks/test_plan_gate.py::*` — scope-reason: remove backend parameter from spawned reviewer fixtures
- `tests/terminals/test_backend_selection.py::*` — scope-reason: replace configurable selection with fixed-native and lifetime behavior
- `tests/terminals/test_runtime_contract.py::*` — scope-reason: remove shutdown/backend config fixtures while preserving runtime-per-row behavior
- `tests/terminals/test_workspace_ops.py::*` — scope-reason: prove durable role-bound panes emit persistent_role while bare panes emit run

Research context:

- A bounded exact-literal sweep found all current `terminal_backend` producers/consumers listed
  above. Storage writes all pass through `parent_body` on create, update, and both upsert paths;
  MCP resolution also loads legacy definition JSON through `_load_agent_body`.
- `spawn_executor.py` (612 lines) remains the patchable facade. This section deletes the
  `request.terminal_backend` and `default_backend` reads from `resolve_terminal_services`;
  #22856 (0048987b32) already deleted the uncalled `_default_backend`. 1.7 moves the resolver
  into a support module.
- `docs/guides/gterminal-development-guide.md` (§Backend status) documents `default_backend` and
  the tmux rollback command; this leaf rewrites that section to fixed native-first selection.
- **Choice — restraint rung 1:** delete the config field, CLI option, MCP parameter, HTTP field,
  dispatch action field, and forwarding arguments. A one-value backend knob does not need to
  exist.
- **Choice — restraint rung 6:** add one internal `TerminalLifetime` discriminator to
  `SpawnRequest`, materialized as `run` by the model when omitted. Put the enum and the narrow
  role-to-lifetime helper in new `src/gobby/terminals/lifetime.py`; it is never exposed in agent
  definitions or public request schemas. 1.9 adds nullable `workspace_panes.role` and its
  `WorkspacePane.role` projection; `WorkspaceOps._fill` is the exact role-bound producer: it passes `persistent_role` to `spawn_web_terminal` for a non-empty durable
  role and `run` for a missing/empty role. Every existing agent/public producer shape resolves to
  `run` without signature churn in unrelated provider code.
- **Granularity:** the listed production targets exceed the target heuristic but remain one atomic
  trust-boundary leaf. Leaving any producer able to emit or accept backend metadata would violate
  “not selectable” and silently reintroduce the option. Focused tests enumerate every producer
  rather than hiding the sweep.

**Decomposition:** `_implementation.py` is 881 lines. Move agent-definition terminal contract
validation into new `_terminal_contract.py`; keep `spawn_agent_impl` as the orchestrating caller
and delete its backend-resolution body. `_load_agent_body` rejects a legacy body containing
`terminal_backend` with the same stable validation error `parent_body` raises on write.
`workspace_ops.py` (815 lines) is not split. The lifetime
enum and role-to-lifetime helper live in new
`src/gobby/terminals/lifetime.py` because `SpawnRequest` and `WorkspaceOps._fill` both import
them; `_fill` only reads the durable role and calls that helper before `spawn_web_terminal`.

Remove backend choice at every ingress and reject, rather than ignore, legacy payloads or agent
definitions that name it. The spawn request carries only internally derived lifetime. Native is
therefore structural, not a default that callers can override.

Consumers unchanged:
- `src/gobby/app_context.py` — no-edit-reason: It stores TerminalConfig as an aggregate type and reads neither removed field.
- `src/gobby/config/app.py` — no-edit-reason: The nested TerminalConfig field and loader contract remain unchanged.
- `src/gobby/runner.py` — no-edit-reason: It passes TerminalConfig but does not choose a backend or shutdown policy.
- `src/gobby/agents/sync.py` — no-edit-reason: It calls parent_body through the unchanged signature and inherits the new rejection.
- `src/gobby/workflows/imports.py` — no-edit-reason: It calls parent_body through the unchanged signature and inherits the new rejection.
- `src/gobby/agents/resume_executor.py` — no-edit-reason: Omitted internal lifetime materializes as run; resume supplies no backend selector.
- `src/gobby/agents/spawn_executor_providers.py` — no-edit-reason: Provider preparation consumes SpawnRequest but does not construct backend policy.
- `src/gobby/agents/spawn_executor_support.py` — no-edit-reason: General spawn helpers consume SpawnRequest without selecting a backend.
- `src/gobby/mcp_proxy/tools/agents_spawn_tools.py` — no-edit-reason: Registry construction signature is unchanged; only the generated inner schema loses a field.
- `src/gobby/feedback/agent.py` — no-edit-reason: Its direct spawn implementation call supplies no backend and materializes run lifetime.
- `src/gobby/servers/_app_routes.py` — no-edit-reason: Router construction signature is unchanged.
- `src/gobby/servers/routes/__init__.py` — no-edit-reason: It re-exports the unchanged router factory name.
- `src/gobby/servers/websocket/server.py` — no-edit-reason: It hosts TerminalCreateMixin and dispatches the unchanged handler signature; selection changes inside the handler.
- `src/gobby/build/observability.py` — no-edit-reason: It observes SpawnAgentAction but never reads or writes the removed backend field.
- `src/gobby/dispatch/__init__.py` — no-edit-reason: Public action re-exports keep the same class name.
- `src/gobby/dispatch/_planning_enhancement.py` — no-edit-reason: It constructs SpawnAgentAction without a backend selector.
- `src/gobby/dispatch/_rule_actions.py` — no-edit-reason: It constructs SpawnAgentAction without a backend selector.
- `src/gobby/dispatch/daemon_resume.py` — no-edit-reason: It constructs SpawnAgentAction without a backend selector.
- `src/gobby/dispatch/dispatcher.py` — no-edit-reason: It dispatches stable action/spawn names and supplies no backend.
- `src/gobby/dispatch/spawn_actions.py` — no-edit-reason: It forwards spawn actions without authoring backend metadata.
- `src/gobby/dispatch/spawn_artifacts.py` — no-edit-reason: Serialized spawn artifacts do not include terminal backend selection.
- `src/gobby/dispatch/write_set_guard.py` — no-edit-reason: It inspects write sets, not terminal backend fields.
- `tests/agents/conftest.py` — no-edit-reason: Shared fixtures import SpawnRequest but rely on its run materialization.
- `tests/terminals/acceptance/conftest.py` — no-edit-reason: It builds terminal-runtime requests and reads neither removed TerminalConfig field.
- `tests/terminals/fakes.py` — no-edit-reason: Runtime fakes observe backend rows and do not author agent backend selection.
- `tests/terminals/test_composition_roots.py` — no-edit-reason: Composition reads TerminalConfig as a whole and neither removed field.
- `tests/terminals/test_tmux_runtime.py` — no-edit-reason: It tests bounded tmux runtime behavior, not agent backend selection.
- `tests/test_runner_lifecycle_processes.py` — no-edit-reason: Runner lifecycle supplies explicit shutdown intent and reads neither removed config field.
- `tests/mcp_proxy/tools/spawn_agent/test_execution.py` — no-edit-reason: Execution tests call the stable registry/tool surface without legacy backend input.
- `tests/mcp_proxy/tools/spawn_agent/test_fallback_agent.py` — no-edit-reason: Agent-definition fallback supplies no terminal backend field.
- `tests/mcp_proxy/tools/spawn_agent/test_initial_variables.py` — no-edit-reason: Initial-variable forwarding supplies no terminal backend field.
- `tests/mcp_proxy/tools/tasks/test_lifecycle_close_orchestration.py` — no-edit-reason: It constructs the registry but does not inspect the removed tool field.
- `tests/mcp_proxy/tools/test_parallel_dispatch.py` — no-edit-reason: Parallel dispatch supplies no backend selector.
- `tests/skills/test_reference_library.py` — no-edit-reason: It constructs the registry for reference loading, not spawn parameters.
- `tests/workflows/test_step_snapshot_semantics.py` — no-edit-reason: It patches the stable spawn implementation name and supplies no backend.
- `tests/config/test_live_policy_consumers.py` — no-edit-reason: Router construction name/signature remain stable; field rejection is covered by focused tests.
- `tests/storage/test_stage_review_findings.py` — no-edit-reason: It patches dispatch spawn by stable name and supplies no backend.
- `tests/dispatch/test_actions_surface.py` — no-edit-reason: SpawnAgentAction remains public; removed selection is covered by focused ingress tests.
- `tests/dispatch/test_daemon_resume.py` — no-edit-reason: Resume action construction supplies no backend.
- `tests/dispatch/test_delivery_chain.py` — no-edit-reason: Delivery chaining supplies no backend.
- `tests/dispatch/test_dispatch_actions.py` — no-edit-reason: Non-spawn dispatch actions are unaffected.
- `tests/dispatch/test_dispatcher.py` — no-edit-reason: Spawn patch points and callable names remain unchanged.
- `tests/dispatch/test_merge_rule.py` — no-edit-reason: Merge actions are unrelated to terminal selection.
- `tests/dispatch/test_planning_enhancement.py` — no-edit-reason: Enhancement action construction supplies no backend.
- `tests/dispatch/test_pr_full_walk.py` — no-edit-reason: PR orchestration supplies no backend.
- `tests/dispatch/test_pr_rules.py` — no-edit-reason: PR rule action construction supplies no backend.
- `tests/dispatch/test_rules.py` — no-edit-reason: Rule dispatch supplies no backend.
- `tests/dispatch/test_skill_composition.py` — no-edit-reason: Skill composition patches stable spawn names and supplies no backend.
- `tests/dispatch/test_spawn_actions_cleanup.py` — no-edit-reason: Cleanup observes spawn lifecycle, not backend selection.
- `tests/dispatch/test_spawn_isolation.py` — no-edit-reason: Isolation action construction supplies no backend.
- `tests/scheduler/test_cron_executor.py` — no-edit-reason: Cron requests omit backend and materialize run/native-first behavior.
- `src/gobby/agents/spawn_executor_codex.py` — no-edit-reason: It consumes SpawnRequest without selecting a backend; omitted lifetime materializes as run.
- `src/gobby/agents/spawn_placed.py` — no-edit-reason: It constructs SpawnRequest without a backend selector; omitted lifetime materializes as run.
- `tests/mcp_proxy/tools/spawn_agent/test_mcp_proxy_tools_spawn_agent_dedup.py` — no-edit-reason: It builds the registry and passes no backend argument.
- `tests/mcp_proxy/tools/spawn_agent/test_spawn_project_context.py` — no-edit-reason: It builds the registry and passes no backend argument.
- `tests/mcp_proxy/tools/spawn_agent/test_project_scope.py` — no-edit-reason: It builds the registry and passes no backend argument.
- `tests/terminals/test_web_spawn.py` — no-edit-reason: Its calls omit the new lifetime keyword, which defaults to run.
- `tests/e2e/conftest.py` — no-edit-reason: It builds a default `TerminalConfig()` and reads neither removed field.
- `tests/terminals/test_host_upgrade.py` — no-edit-reason: It builds a default `TerminalConfig()` and reads neither removed field.
- `tests/terminals/test_input_grant_rebind.py` — no-edit-reason: It builds a default `TerminalConfig()` and reads neither removed field.
- `tests/mcp_proxy/tools/spawn_agent/test_placement.py` — no-edit-reason: It builds the registry and calls `spawn_agent_impl` with no backend argument, and it asserts the materialized run lifetime.
- `tests/mcp_proxy/tools/spawn_agent/test_seat_adoption.py` — no-edit-reason: It builds the registry and patches `_load_agent_body` and `spawn_agent_impl` by their unchanged names.
- `tests/mcp_proxy/tools/spawn_agent/test_spawn_scope_proxy.py` — no-edit-reason: It builds the registry and patches `_load_agent_body` by its unchanged name.
- `tests/mcp_proxy/tools/spawn_agent/test_spawn_worktree_reference_resolution.py` — no-edit-reason: It builds the registry and patches `_load_agent_body` by its unchanged name.
- `tests/mcp_proxy/tools/test_spawn_agent_resume.py` — no-edit-reason: It calls `spawn_agent_impl` and builds the registry with no backend argument.
- `tests/agents/test_agents_sync.py` — no-edit-reason: Its agent bodies carry no `terminal_backend`, so `_load_agent_body` loads them unchanged.
- `tests/agents/test_sandbox_network.py` — no-edit-reason: It calls `spawn_agent_impl` with no backend argument.
- `tests/mcp_proxy/tools/spawn_agent/test_sandbox_gate.py` — no-edit-reason: It calls `spawn_agent_impl` with no backend argument.
- `tests/agents/test_spawn_native_only.py` — no-edit-reason: It calls `resolve_terminal_services` with a request that carries no backend selector.
- `tests/workflows/test_agent_workflow_runtime_cleanup.py` — no-edit-reason: It records `SpawnAgentAction` instances and reads no removed backend field.

**Acceptance:**

- 1.6.1 - `TerminalConfig`, installed config, and the regenerated runtime contract have no
  `default_backend`; config containing it is rejected as unknown. test:
  `tests/terminals/test_backend_selection.py::test_default_backend_config_is_removed`.
- 1.6.2 - Agent-definition create, update, sync/upsert, and legacy resolution all reject a top-level
  `terminal_backend` with one stable validation error. test:
  `tests/agents/test_backend_ingress.py::test_every_agent_definition_write_and_read_rejects_backend`.
- 1.6.3 - MCP, HTTP, CLI, dispatch, and scheduler paths expose no backend selector and
  forward no backend string; literal legacy MCP/HTTP/dispatch payloads are rejected. test:
  `tests/agents/test_backend_ingress.py::test_every_public_spawn_ingress_rejects_backend`.
- 1.6.4 - Every bare/public agent producer shape materializes
  `SpawnRequest.terminal_lifetime` as literal `run`; omission has the observable run result in the
  model rather than leaking a public `auto`/backend sentinel. test:
  `tests/agents/test_backend_ingress.py::test_all_spawn_request_producers_emit_run_lifetime`.
- 1.6.5 - `WorkspaceOps._fill` reads the durable `WorkspacePane.role` that 1.9 adds: a non-empty
  role is the only `persistent_role` producer, while an empty or
  absent role emits `run`. Neither value changes backend selection, and both fail closed alike
  without a host (1.7). test:
  `tests/terminals/test_workspace_ops.py::test_role_bound_pane_is_persistent_and_bare_pane_is_run`.
- 1.6.6 - A post-change literal sweep has no production `terminal_backend` authoring field or
  `default_backend`; remaining backend occurrences are row/runtime observation or the internal
  selected result. behavior: `backend-option-consumer-sweep`.

### 1.7 Fail-closed spawn with typed host reasons, durable backend visibility, and automatic recovery [category: code] (depends: 1.2, 1.3, 1.6, 1.9)

`kind: deliverable`

Targets:
- `src/gobby/terminals/host_client.py::*` — scope-reason: add HostUnavailableReason, FAIL_CLOSED_HOST_REASONS, and HostReasonUnavailableError (landed in #23213)
- `src/gobby/terminals/host_manager.py::*` — scope-reason: record, expose in health_state, and clear the typed host reason (landed in #23213)
- `src/gobby/storage/terminals.py::*` — scope-reason: report epoch_refused from the durable adoption matrix (landed in #23213)
- `src/gobby/terminals/native_runtime.py::classify_native_spawn_failure`
- `src/gobby/agents/spawn_executor_runtime.py::_runtime_spawn`
- `src/gobby/terminals/web_spawn.py::spawn_web_terminal`
- `src/gobby/terminals/workspace_ops.py::WorkspaceOps._fill`
- `src/gobby/servers/websocket/terminal_ws_create.py::TerminalCreateMixin._handle_terminal_create`
- `tests/agents/test_spawn_executor.py::*` — scope-reason: cover the fail-closed matrix, no side effects, and recovery
- `tests/terminals/test_host_manager.py::*` — scope-reason: pin the typed reason's record, exposure, and clear
- `tests/storage/test_terminals.py::*` — scope-reason: pin the adoption matrix's epoch_refused report
- `tests/servers/test_terminals_routes.py::*` — scope-reason: pin backend on HTTP inventory rows
- `tests/servers/test_terminal_ws_list.py::*` — scope-reason: pin backend on WS inventory rows
- `tests/servers/test_terminal_ws_create.py::*` — scope-reason: pin the bounded fail-closed code on WS terminal create
- `tests/terminals/test_backend_selection.py::*` — scope-reason: update fixtures for the typed host reason
- `tests/terminals/test_workspace_ops.py::*` — scope-reason: prove a role-bound pane fails closed without a host
- `web/src/components/activity/terminal/__tests__/TerminalSessionList.test.tsx::*` — scope-reason: pin the backend label on every session
- `crates/gclient/tests/client_loop.rs::*` — scope-reason: pin backend rendering on pane, sidebar, and orphan inventory flows

Research context:

- #23213 landed this contract (commits `6d08ba0018`, `2200aa1a5b`, `8ca9af8109`) under the
  Orchestrator's 2026-10-14 option-B ruling (A2). #22856 removed every tmux spawn path, pinned by
  `tests/agents/test_spawn_native_only.py`, so a host-unavailable spawn fails closed.
- Typed reason: `host_client.py` defines `HostUnavailableReason` (`host_start_timeout`,
  `epoch_refused`, `gterm_missing`, `other`), `FAIL_CLOSED_HOST_REASONS` (the first three), and
  `HostReasonUnavailableError`. `TerminalHostManager` records the reason for its latest failed
  start or adoption, exposes it in `health_state`, and clears it on successful adoption.
  `storage/terminals.py` reports `epoch_refused` from the durable adoption matrix (1.2's semantic
  subtype). `classify_native_spawn_failure` maps the reason to the bounded code
  `host_unavailable:<reason>`. The spawn path reads the typed field and never parses
  `last_error` or logs.
- Spawn paths: `_runtime_spawn` and `spawn_web_terminal` fail before any terminal row, process,
  tmux session, or project message exists. `WorkspaceOps._fill` rolls the pane back and raises
  `terminal_failed` carrying the diagnostic, and WS terminal create sends the bounded code.
  Every other failure keeps its existing code and settlement.
- Visibility: HTTP list and get and WS `terminal.list` carry `backend` from the stored row.
  gclient leaves a native row unmarked and names a foreign backend (Orchestrator ruling
  2026-10-14, option (a)); sidebar agent rows need no label because every agent spawn is native.
  The web terminal session list renders each row's backend.
- **Choice — restraint rung 1:** no fallback decision module, project-message announcement, or
  locator reason exists; the typed reason on the host manager is the only new state.
- **Granularity:** six acceptance items over one policy, fail-closed spawn with its visibility
  and recovery. Splitting them would let a spawn fail without an identifiable reason.

Each spawn reads the host manager's current typed reason. Once the health loop re-adopts the
host, the next ordinary spawn uses gterm with no operator action, and a failed spawn leaves
nothing to clean up.

Consumers unchanged:
- `src/gobby/agents/resume_executor.py` — no-edit-reason: It reaches terminal spawning through the stable spawn_executor facade and inherits fail-closed spawn.
- `src/gobby/terminals/host_reconcile.py` — no-edit-reason: It reconciles native host inventory and reads no typed host reason.
- `tests/agents/terminal_fixtures.py` — no-edit-reason: Its terminal rows carry a stored backend and need no host reason.
- `tests/agents/test_tmux_integration.py` — no-edit-reason: It tests the spawn-less tmux runtime, which no spawn path reaches.
- `tests/hooks/test_session_start_handlers.py` — no-edit-reason: Its hook-created fixture rows are unchanged by fail-closed spawn.
- `tests/servers/test_attention_native_roster.py` — no-edit-reason: The roster reads the stored backend and creates no rows.
- `tests/servers/test_native_web_proxy.py` — no-edit-reason: Native proxy promotion preserves the existing locator.
- `tests/servers/test_terminal_ws_lease.py` — no-edit-reason: Lease tests consume promoted rows and spawn nothing.
- `tests/storage/test_terminal_bindings.py` — no-edit-reason: Binding tests use the unchanged locator defaults.
- `tests/terminals/fakes.py` — no-edit-reason: Runtime fakes keep their locator behavior and report no host reason.
- `tests/terminals/test_native_runtime.py` — no-edit-reason: It tests native runtime operations through unchanged signatures.
- `tests/terminals/test_tmux_runtime.py` — no-edit-reason: It tests the spawn-less tmux runtime, which fail-closed spawn never reaches.
- `tests/agents/test_run_completion.py` — no-edit-reason: Run completion consumes terminal lifecycle and is unchanged by fail-closed spawn.
- `tests/agents/watchdog/test_close_review_parked_caller.py` — no-edit-reason: Watchdog promotion fixtures keep the unchanged repository signature.
- `tests/agents/test_spawn_native_only.py` — no-edit-reason: It pins that no spawn path reaches tmux, which fail-closed spawn keeps.
- `tests/mcp_proxy/tools/spawn_agent/test_placement.py` — no-edit-reason: It calls `_runtime_spawn` with an adopted native host, which spawns gterm unchanged.
- `tests/servers/test_terminal_ws_attach_honesty.py` — no-edit-reason: It calls `_runtime_spawn` with an adopted native host, which spawns gterm unchanged.
- `tests/terminals/test_web_spawn.py` — no-edit-reason: Its calls reach `spawn_web_terminal` through the unchanged signature; #23213 changed one fixture line only.
- `src/gobby/servers/websocket/server.py` — no-edit-reason: It hosts `TerminalCreateMixin` and dispatches the unchanged handler signature.

**Acceptance:**

- 1.7.1 - For each of `host_start_timeout`, semantic `epoch_refused`, and `gterm_missing`, spawn
  fails closed with a diagnostic code naming that reason; token, pid, protocol, auth, I/O,
  capacity, prepare/commit, and arbitrary native failures keep their existing codes; a healthy
  adopted host spawns gterm. test:
  `tests/agents/test_spawn_executor.py::test_host_unavailable_reasons_fail_closed_with_typed_code`.
- 1.7.2 - Run-scoped and persistent-role spawns, and `WorkspaceOps._fill` for a role-bound pane,
  fail with the same typed diagnostic. test:
  `tests/agents/test_spawn_executor.py::test_every_lifetime_fails_closed_without_host`. test:
  `tests/terminals/test_workspace_ops.py::test_role_bound_pane_fails_closed_without_host`.
- 1.7.3 - A fail-closed spawn creates no terminal row, process, tmux session, or project message;
  `TerminalHostManager` records the typed reason, exposes it in `health_state`, and clears it on
  adoption, and the spawn path never parses `last_error`. test:
  `tests/agents/test_spawn_executor.py::test_fail_closed_spawn_has_no_side_effects`. test:
  `tests/terminals/test_host_manager.py::test_typed_failure_reason_recorded_and_cleared`.
- 1.7.4 - HTTP list and get and WS `terminal.list` rows carry `backend` read from the stored row.
  test: `tests/servers/test_terminals_routes.py::test_terminal_inventory_exposes_backend`. test:
  `tests/servers/test_terminal_ws_list.py::test_terminal_list_exposes_backend`.
- 1.7.5 - gclient renders each terminal row's backend by convention: a native row is unmarked and
  a foreign backend is named (pane chrome, orphan dialog, navigator label); sidebar agent rows
  need no label because every agent spawn is native-only (#22856). The web terminal session list
  renders each row's backend. test:
  `crates/gclient/tests/client_loop.rs::terminal_inventory_renders_each_backend`. test:
  `web/src/components/activity/terminal/__tests__/TerminalSessionList.test.tsx::rendersBackendForEverySession`.
- 1.7.6 - After the health loop re-adopts the host, the next ordinary spawn uses gterm without
  operator action. test:
  `tests/agents/test_spawn_executor.py::test_spawn_recovers_after_host_readoption`.

### 1.8 Retired: Rust tmux runtime parity

`kind: framing`

#23214, `Rust tmux runtime parity for live fallback rows`, closed obsolete on 2026-10-14 under
the Orchestrator's option-B ruling (A2): fail-closed spawn (1.7) leaves no live fallback row, and
tmux rows exist only for hand-started panes (#22856). Nothing in this section is implemented.
The replacement owners are:

- 2.11's write-only `crates/gterminals/src/tmux_write.rs` for Text, Key, Paste, Input, resize,
  and release_size on the row's recorded socket.
- Python's spawn-less `TmuxTerminalRuntime` for snapshots, history, liveness, session presence,
  and termination.
- 2.2 for WS attach, which computes the tmux locator from the row and the host socket.

### 1.9 Durable pane role column and workspace-op argument [category: code]

`kind: deliverable`

Targets:
- `crates/gcore/assets/schema/migrations/456_workspace_pane_role.sql`
- `crates/gcore/src/schema/assets.rs::MIGRATIONS`
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: add the workspace_panes.role column and its check constraint
- `crates/gcore/tests/schema_contract.rs::*` — scope-reason: move the pinned latest asset and root hash to 456
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: regenerate the expected schema identity for 456
- `crates/gcore/src/grant/bundle.rs::*` — scope-reason: move the no-postgres expected identity's latest_version, checksum, and root hash to 456
- `crates/gdaemon/tests/cli_contract.rs::*` — scope-reason: move any pinned schema identity to 456
- `tests/runtime_grants/golden/brokered_datastores.json::*` — scope-reason: re-sign the golden for the 456 identity
- `tests/runtime_grants/golden/direct_datastores.json::*` — scope-reason: re-sign the golden for the 456 identity
- `tests/runtime_grants/golden/old_client_new_grant.json::*` — scope-reason: re-sign the golden for the 456 identity
- `tests/runtime_grants/golden/payload_skew_unknown_field.json::*` — scope-reason: re-sign the golden for the 456 identity
- `tests/runtime_grants/golden/unavailable_datastores.json::*` — scope-reason: re-sign the golden for the 456 identity
- `src/gobby/storage/workspace_panes.py`
- `src/gobby/storage/workspaces.py::*` — scope-reason: move WorkspacePane and _optional_str into workspace_panes.py and thread role through _insert_pane, create_tab and add_pane
- `src/gobby/terminals/workspace_ops.py::WorkspaceOps.tab_create`
- `src/gobby/terminals/workspace_ops.py::WorkspaceOps.pane_split`
- `src/gobby/servers/websocket/workspace_ws.py::_result`
- `tests/storage/test_workspaces.py::*` — scope-reason: cover role persistence, NULL default, and omission when NULL
- `tests/terminals/test_workspace_ops.py::*` — scope-reason: cover role on tab.create and pane.split

Research context:

- PD ruling (A1): this plan owns the column, and #22895 consumes the contract. The contract is a
  nullable `role text` on `workspace_panes`, opaque to this plan: non-empty means `persistent_role`
  (1.6), absent means `run`. #22895 owns which role names a runbook writes.
- Migration slot: 456 is the next free slot on 2026-10-01 (`455_drop_ask_artifacts.sql` is the
  latest). The executor re-checks the catalog immediately before writing it and, if taken,
  renumbers with every identity carrier. Carriers follow commit `bab7aac030`: the SQL asset, the
  `EmbeddedMigration` entry in `assets.rs::MIGRATIONS`, the `workspace_panes.role` entry in
  `catalog.manifest.json` (regenerated by
  `crates/gcore/tests/catalog_manifest_freshness.rs::catalog_manifest_is_fresh_for_embedded_assets`
  with
  `UPDATE_GCORE_SCHEMA_MANIFEST=1`), `schema_contract.rs`, and `schema_expected_identity.json`
  (generated by `scripts/generate_schema_expected_identity.py`), the no-postgres branch of
  `grant/bundle.rs::expected_schema_identity`, the `crates/gdaemon/tests/cli_contract.rs` CLI contract pin, and the five signed
  `tests/runtime_grants/golden/*.json` vectors (re-signed per the
  `tests/runtime_grants/test_golden_vectors.py` docstring). Migration 455 (`30d66635c1`) touched
  the same set. The SQL adds the column with a
  check that a present role is non-empty and within the `workspace_panes_label_byte_limit` ceiling.
  Baseline and applied migrations stay unchanged.
- Storage: `WorkspacePane` (`storage/workspaces.py:188`) gains `role: str | None = None` and
  `from_row` maps it. `_insert_pane`, `create_tab`, and `add_pane` gain a keyword `role` written on
  insert. Every pane read already uses `SELECT *` or `RETURNING *`, so reads need no edit.
- Serialization: `WorkspacePane.to_dict` omits `role` when it is `None`, as `Workspace.to_dict`
  omits `default_project_id`. `workspace_ws._result` serializes `WorkspacePane` through `to_dict`
  as it already does for `Workspace`, so the snapshot path and the event path
  (`workspace_ops.py:664`) share the omission. The golden fixtures `workspace_snapshot.json` and
  `workspace_event.json` keep their bytes, and gclient's `ws_golden.rs::typed_fixture` round-trip
  stays unchanged. gclient tolerates a present `role` key because
  `crates/gclient/src/daemon/workspace.rs` has no `deny_unknown_fields`.
- Ops: `WorkspaceOps.tab_create` and `pane_split` gain keyword `role: str | None = None` and pass it
  to storage. `workspace_ws._SIGNATURES` derives accepted fields from those signatures, so the WS
  ops accept `role` with no dispatch edit; an empty string is refused as `invalid_op` before
  storage. gclient production code sends no role, and the MCP `create_tab`/`split_pane` tools are
  unchanged.
- **Collision:** `.gobby/plans/placed-agent-launch.md` §1.1 edits the same `create_tab`,
  `add_pane`, and `_insert_pane` signatures; its §1.5 guards already landed (#23013). Whichever of
  §1.1 and this leaf lands second rebases and keeps both keyword sets.
- **Choice — restraint rung 6:** one nullable column and one optional argument. No role table,
  enum, or gclient field.

**Decomposition:** `storage/workspaces.py` is 966 lines. Move `WorkspacePane` (188-214) and
`_optional_str` (86-87) into new `src/gobby/storage/workspace_panes.py`, which imports nothing
from `workspaces.py`, and import both back, so existing `gobby.storage.workspaces.WorkspacePane`
importers keep working and the file's net line count drops. `_insert_pane` stays because it
depends on `_free_ref`, `_required`, and `_PARENT_COLUMN`.

Consumers unchanged:
- `src/gobby/mcp_proxy/tools/workspaces/registry.py` — no-edit-reason: Its explicit `create_tab` and `split_pane` parameters omit role, which defaults to NULL.
- `crates/gclient/src/daemon/workspace.rs` — no-edit-reason: `PaneRow` and the op variants carry no role, and serde ignores the absent or extra key.
- `crates/gcore/src/schema/runner_tests.rs` — no-edit-reason: Its pane inserts name explicit columns, so a nullable column changes nothing.

**Acceptance:**

- 1.9.1 - Migration 456 adds nullable `workspace_panes.role` with the non-empty and byte-ceiling
  check, and every schema identity carrier agrees: `cargo test -p gobby-core --lib grant::tests`
  without features, `cargo test -p gobby-core --features postgres --test schema_contract`, and
  `cargo test -p gobby-core --features postgres --test catalog_manifest_freshness` run without
  `UPDATE_GCORE_SCHEMA_MANIFEST` all pass. file: `crates/gcore/assets/schema/migrations/456_workspace_pane_role.sql`.
- 1.9.2 - `create_tab` and `add_pane` persist a supplied role, every pane read returns it, and an
  omitted role stores NULL. test:
  `tests/storage/test_workspaces.py::test_pane_role_persists_and_defaults_null`.
- 1.9.3 - A pane with no role serializes with no `role` key on both the snapshot and event paths,
  and a pane with a role serializes it. test:
  `tests/storage/test_workspaces.py::test_pane_to_dict_omits_null_role`.
- 1.9.4 - The `tab.create` and `pane.split` ops accept an optional role, store it on the new pane,
  and refuse an empty string before storage. test:
  `tests/terminals/test_workspace_ops.py::test_tab_create_and_pane_split_carry_role`.
- 1.9.5 - The canonical workspace golden replies replay unchanged. test:
  `tests/servers/test_terminal_ws_golden.py::test_workspace_emitters_match_golden_replies`.

### 1.10 Input-grant finalization split and durable handoff record [category: code] (depends: 1.4)

`kind: deliverable`

Targets:
- `crates/gterminals/src/leases.rs::*` — scope-reason: preserve only a direct native holder's grant on daemon-shutdown finalization (landed in #23218)
- `crates/gterminals/src/repository.rs::*` — scope-reason: record, compare-and-set, and conditionally clear the native input handoff (landed in #23218)
- `crates/gterminals/src/reconcile.rs::*` — scope-reason: add expire_input_handoffs and the InputRevoke effect (landed in #23218)
- `crates/gterminals/tests/leases.rs::*` — scope-reason: pin the finalization split and the handoff record (landed in #23218)

Research context:

- #23218 executed this section (closed 2026-10-14, landing `6322c1874f`); it replaces the D1
  deferral (A2).
- #23076, `gclient reconnects straight to the host` (closed 2026-10-03), sets the finalization
  split this port keeps. Daemon-shutdown finalization preserves an already-authorized direct
  native holder's input grant. Every other finalization revokes: socket loss, release or
  preemption, proxy and web holders, and host refusal. The adopting daemon revokes a preserved
  grant that is not re-bound within #23076's 30-second window (`INPUT_HANDOFF_REBIND`).
- Handoff persistence — PD ruling (gobby#14972, 2026-10-01, Adversary F1): preservation is
  confirmed only after a durable compare-and-set of `process.native_input_handoff` keyed on native
  backend, live state, machine, host epoch, and physical terminal identity. A failed, raising, or
  cancelled record revokes. `reconcile::expire_input_handoffs` expires a record after the window
  through an `InputRevoke` effect, keeps the record and retries when that revoke fails, and clears
  it conditionally so a rebind or replacement wins. The record lives on the existing process
  JSON, with no new table.
- Production wiring: no production code yet calls `expire_input_handoffs` or implements
  `InputRevoke`; 2.8's settler does (2.8.6), and 2.2 applies ordinary grants and revokes (2.2.7).
- **Granularity:** four acceptance items over one lifecycle owner. Preservation is a registry
  finalize transition confirmed only by the handoff compare-and-set, and expiry revokes through
  the same registry, so neither half closes without the other.

**Acceptance:**

- 1.10.1 - Daemon-shutdown finalization preserves only an already-authorized direct native
  holder's input grant; socket loss, release, preemption, proxy and web holders, and host
  refusal revoke; the adopting daemon revokes a preserved grant not re-bound within #23076's
  bounded window. test:
  `crates/gterminals/tests/leases.rs::shutdown_preserves_only_direct_native_input_grant`.
- 1.10.2 - A grant is preserved only after the handoff record's compare-and-set succeeds on the
  full identity; a failed, raising, or cancelled record revokes the grant. test:
  `crates/gterminals/tests/leases.rs::handoff_preservation_requires_a_durable_identity_cas`.
- 1.10.3 - Expiry clears only the record it read: a rebind or replacement that lands first wins,
  and an identity mismatch leaves the newer record. test:
  `crates/gterminals/tests/leases.rs::handoff_expiry_never_clears_a_rebound_record`.
- 1.10.4 - A failed expiry revoke keeps the record, and the next reconcile pass retries it. test:
  `crates/gterminals/tests/leases.rs::failed_handoff_revoke_keeps_the_record_and_retries`.

## P2: Milestone M2 — terminal WS and proxy relay [category: code] (depends: P1)

`kind: framing`

The 2.1 codec, the 2.4 Native ownership cutover, the 2.5-2.11 Native and tmux write,
settlement, and runtime seams, 2.14's terminal inventory reads and HTTP handlers, and 2.15's
Native lane viewports run without #21558. 2.4.17's activation gate keeps them to isolated
`Native` tests until 2.13 registers the terminal family. 2.2, 2.3, 2.12, and 2.13 ride #21558's
native gdaemon WS accept, auth, subscription, and broadcast transport: 2.2 carries the cross-plan
"blocked by" edge to #21558, and the other three follow 2.2 through heading dependencies. M2 owns
terminal messages and relaying on that transport and does not recreate transport authentication
or the base event envelope.

### 2.1 Backend-neutral terminal WS codec and canonical golden replay [category: code] (depends: 1.4, 1.5)

`kind: deliverable`

Targets:
- `crates/gterminals/src/ws_protocol.rs`
- `crates/gterminals/tests/ws_golden.rs`

Research context:

- Port `encode_message`, `decode_message`, `canonical_json`, fragmentation, proxied-event
  wrapping, cursor parsing, inventory item, and page encoding from
  `src/gobby/terminals/ws_protocol.py`.
- The canonical, read-only corpus is `tests/fixtures/terminal_ws_golden/manifest.json`; the task
  description's `tests/servers/fixtures/...` path does not exist. Python and gclient already replay
  the canonical path.
- **Choice — restraint rung 2:** consume the existing corpus from the Rust test. Do not copy,
  regenerate, normalize, or relocate fixture bytes.

Implement typed message helpers only where typing improves validation; preserve unknown
backend-neutral payload fields and canonical JSON ordering/escaping exactly as the fixtures
require. Enforce safe integers, page ceilings, reassembly bytes/time, queue bounds, lifecycle
reserve, paste bytes, and write sequence capacity before allocation or emission.

**Acceptance:**

- 2.1.1 - Every manifest case is discovered and Rust encode/decode output is byte-identical to
  the canonical fixture; a missing/unread manifest case fails the test. test:
  `crates/gterminals/tests/ws_golden.rs::canonical_terminal_ws_corpus_replays_byte_identically`.
- 2.1.2 - Fragmentation/reassembly preserves the canonical wrapped event and rejects excessive,
  duplicate, missing, expired, or cross-message fragments at the existing limits. test:
  `crates/gterminals/tests/ws_golden.rs::fragment_limits_and_identity_match_python`.
- 2.1.3 - Terminal pages preserve stable cursor ordering and backend on every inventory item,
  enforce safe integers and encoded-page ceiling, and produce the same next cursor as Python.
  test: `crates/gterminals/tests/ws_golden.rs::inventory_pagination_matches_python`.
- 2.1.4 - Paste/write/lifecycle queue constants and failure envelopes match the existing
  backend-neutral WS contract; no gterm/gclient/protocol source or fixture byte changes. behavior:
  `terminal-ws-wire-scope-guard`.

### 2.4 Native ownership cutover: one supervisor, per-start token, action hold, and activation gate [category: code] (depends: 1.2, 1.3, 1.5)

`kind: deliverable`

Targets:
- `crates/gterminals/src/family.rs::*` — scope-reason: TerminalFamily::new takes the startup route latch
- `crates/gterminals/src/lib.rs::*` — scope-reason: declare the hold and write_api modules
- `crates/gterminals/src/hold.rs`
- `crates/gterminals/src/write_api.rs`
- `crates/gdaemon/src/front_door/routes.rs::*` — scope-reason: the terminal_route and terminal_route_latch helpers that carry the startup route latch
- `crates/gdaemon/src/serve.rs::*` — scope-reason: construct Native terminal ownership once at startup and reject a runtime route-mode flip
- `crates/gdaemon/src/lifecycle/backend.rs::BackendChild::spawn`
- `crates/gdaemon/src/lifecycle/backend.rs::BackendChild`
- `crates/gdaemon/src/lifecycle/backend.rs::BackendConfig`
- `crates/gdaemon/src/state_services.rs::*` — scope-reason: Native terminal construction lives in ActiveServices::start; refuse Native without the terminal WS family before HostOptions::from_pool, and latch the route once per process
- `crates/gdaemon/src/state.rs::RuntimeState::run`
- `crates/gdaemon/src/state.rs::RuntimeState::prepare`
- `src/gobby/runner_backend.py::*` — scope-reason: read the write token from the supervisor pipe at startup and hold it in memory
- `src/gobby/runner_init/terminal_wiring.py::init_terminal_wiring`
- `tests/test_runner_backend.py::*` — scope-reason: pin token delivery over the supervisor pipe and its absence from env and argv
- `crates/gdaemon/tests/lifecycle.rs::*` — scope-reason: its Python test children read GOBBY_SUPERVISOR_FD with os.read(fd, 1) and expect it to block until EOF, so with the token line first they now drain to EOF; the token-rotation test is supplementary, with no RED
- `crates/gterminals/tests/host_lifecycle.rs::*` — scope-reason: add route_mode_has_exactly_one_supervisor and remove the five opt-in real-gdaemon tests that 2.13 restores
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `crates/gterminals/tests/family_cutover.rs`

Research context:

- Re-scoped (Orchestrator, 2026-10-15): this section closes on the five ownership gates 2.4.3,
  2.4.8 (token half), 2.4.14, 2.4.15 and 2.4.17, as #23217 carries them. 2.4.7, 2.4.12, 2.4.13,
  2.4.16, the route-side remainders of 2.4.8, 2.4.14 and 2.4.15, and the five #24210 folds move to
  2.6-2.10. Nothing is dropped. The mapping is 16864's (message 1d257c19, 2026-10-15).
- Target notes: `lib.rs` declares `pub mod hold` and `pub mod write_api`. `hold.rs` is a new
  module: the per-terminal action hold (ActionGate) and bounded queue (write.rs and host.rs are
  near the 1,000-line ceiling). `write_api.rs` holds the TokenSlot admission primitive only; 2.6
  adds the write HTTP surface. `lifecycle/backend.rs` mints the per-start write token, delivers it
  on the supervisor pipe, and passes gdaemon's latched route mode to the backend; BackendConfig
  gains the write_token field (from_command_json default) and BackendChild the lease field.
  `state.rs` carries the startup route latch from serve.rs into ActiveServices::start (one
  parameter). `init_terminal_wiring` refuses a route mode that differs from gdaemon's latched one
  as restart-required. `host_lifecycle.rs` gains `route_mode_has_exactly_one_supervisor`
  (TerminalFamily::new takes the route latch). The five opt-in real-gdaemon tests that launch
  with terminal_ws native (`daemon_restart_preserves_live_terminal`,
  `explicit_parent_drain_restarts_with_fresh_epoch`,
  `spawned_identity_survives_health_and_stop_disarms_retry`,
  `explicit_drain_terminates_token_mismatch_host`, `explicit_drain_terminates_foreign_epoch_host`)
  are removed in this section's commit, which names the restoring task (#23219 until
  re-expansion retires it for 2.13's task). 2.13 restores them from base 17ec46c7 when it
  registers the terminal family (2.13.7). There is no `#[ignore]`, no test-quality baseline
  entry and no bypass env var (Orchestrator ruling 2026-10-15). `crates/gdaemon/Cargo.toml`,
  `Cargo.lock` and `version_pins.py` carry the gdaemon 0.0.1 patch bump.
- Route authentication — Orchestrator ruling (2026-10-15). It replaces the mechanism of the PD
  ruling (gobby#14972, 2026-10-01), whose parent-pipe pieces #23457 (fde8c6182d) deleted when it
  made gdaemon the parent; the properties stay the same.
  - gdaemon spawns the Python backend in `lifecycle/backend.rs::BackendChild::spawn`, with a
    liveness pipe whose read end is `GOBBY_SUPERVISOR_FD`.
  - On every backend start, including a respawn, `BackendChild::spawn` mints a random token and
    writes it as the first line of that pipe before holding the write end for EOF.
  - `runner_backend.py` reads that first line at startup, keeps it in memory only, and then keeps
    its EOF watch.
  - The TokenSlot admission primitive accepts only the current backend child's token and refuses
    a missing, wrong or retired one. Registering the route behind it (2.6), and the client's
    per-request read (2.7), belong to the gap amendment.
  - The token never appears in env, argv, a file, or a log.
  - `serve.rs` keeps its other role: startup Native construction and refusal of a runtime
    route-mode flip.
  - Flip vector: because the respawned backend re-reads bootstrap.yaml, gdaemon also passes its
    latched route mode to the backend, and `init_terminal_wiring` refuses a mismatch as
    restart-required, so an edited terminal_ws can never leave zero supervisors.
- Action hold — PD ruling (gobby#14972, 2026-10-01, Adversary F2). This section builds the hold
  primitive (ActionGate) in `hold.rs`. Binding the hold id to the child token, row and lease
  generation with 1.5.2 revalidation (2.6), and 2.12's WS operator input, belong to later
  sections.
  - Acquire: `acquire_action` takes the per-terminal action hold. A second acquire waits.
  - Tokens: every hold carries a token. A reply carrying a superseded token never completes or
    releases a newer hold.
  - Queue: while a hold is active, operations without the hold id queue in arrival order and
    flush in order on release.
  - Uncertainty: TTL expiry, a timeout, client disconnect, cancellation after dispatch, and child
    stop record uncertainty (1.5.3 unresolved evidence) and nothing more. The next queued effect
    is admitted only after the admitted operation completes or is proven unable to produce
    further effects: the pane or terminal was destroyed, the gterm host epoch changed, or the
    host process exited. An operator close or kill of the terminal is the escape hatch because it
    produces that proof. Release never interrupts an admitted step.
  - Recovery: a lost acquire, step, or release reply never replays a non-idempotent uncertain
    write.
  - Constants: no gterm or terminal WS constant governs an action hold or its queue (the nearest
    are `frame_client.py::DELTA_QUEUE_ENTRIES` = 64 and
    `leases.py::LIFECYCLE_PUBLICATION_QUEUE_MAXSIZE` = 256, which size other queues), so the PD
    values apply: hold TTL 10 s; queue bound 128 items or 256 KiB, whichever comes first; queue
    age 30 s, measured on the oldest queued item. At the bound or the age limit, new input is
    refused with an explicit typed error to its sender. Queued items are never discarded, and
    input is never dropped silently.
- Activation gate — Adversary sequencing finding (2026-10-01): until 2.13 registers the terminal
  family, the terminal WS routes stay Python, and `terminal_ws.py::TerminalWsMixin._leases`
  (887-891) requires a Python `TerminalLeaseRegistry`. 2.4 therefore builds and verifies the
  ownership seams in isolated `Native` tests only. Production stays `Proxy` or `Compare` with
  Python owners, and a startup that selects `Native` while the terminal family is unregistered is
  refused before any ownership change. 2.13 lifts the gate in the same change that registers the
  family. No per-operation rollout knob or second supervisor is added.
- **Granularity:** five acceptance items over the ownership boundary: one supervisor and a
  latched route mode, the per-start token, the hold primitive, and the activation gate. They
  close together because the hold and token primitives are only safe behind the startup latch
  and the activation gate.

`Native` constructs Rust ownership and omits Python ownership. Route mode is read once during
startup; a runtime flip is rejected so existing sessions cannot be half-migrated.

Consumers unchanged:
- `src/gobby/runner_init/orchestration.py` — no-edit-reason: It calls `init_terminal_wiring` through the unchanged signature.
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: It reads `MANAGED_BIN_VERSION_PINS` by binary name, and the bump changes only the `gdaemon` value.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: It iterates `MANAGED_BIN_VERSION_PINS` as version floors, and the bump changes only the `gdaemon` value.
- `src/gobby/install/distribution.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: It reads or patches `MANAGED_BIN_VERSION_PINS` by binary name and hard-codes no gdaemon version.
- `tests/cli/test_install_ghook.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/install/test_distribution.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/install/test_version_pins.py` — no-edit-reason: It checks each pin against its crate's `Cargo.toml` version, and the bump changes both together.

**Acceptance:**

- 2.4.3 - Startup has exactly one supervisor in every mode, and a runtime route-mode change is
  rejected with restart-required before altering ownership, including a backend respawn that
  re-reads an edited terminal_ws: the backend refuses a route mode that differs from gdaemon's
  latched one. test:
  `crates/gterminals/tests/family_cutover.rs::cutover_requires_restart_and_has_one_owner`. test:
  `crates/gterminals/tests/host_lifecycle.rs::route_mode_has_exactly_one_supervisor`.
- 2.4.8 - Each backend start mints a fresh token and delivers it only as the first line of the
  supervisor pipe; it never appears in the child's environment or argv, and it rotates on every
  start. The TokenSlot admission primitive refuses a missing, wrong or retired token, and the
  Python holder keeps the token in memory only. test:
  `crates/gterminals/tests/family_cutover.rs::write_route_requires_the_per_start_supervisor_pipe_token`.
  test: `tests/test_runner_backend.py::test_write_token_travels_only_over_the_supervisor_pipe`.
- 2.4.14 - While a hold is active, unheld operations queue in arrival order and flush in order on
  release; at 128 items, 256 KiB, or a 30 s oldest-item age, new input is refused with an
  explicit typed error and no queued item is discarded; a reply carrying a superseded hold token
  neither completes nor releases the newer hold. test:
  `crates/gterminals/tests/family_cutover.rs::route_hold_queue_is_bounded_and_token_scoped`.
- 2.4.15 - TTL expiry, timeout, or disconnect during an admitted host write records uncertainty
  only: the next queued effect is not admitted before completion or proof (pane or terminal
  destroyed, host epoch changed, host process exited, or an operator close or kill), and a
  delayed effect that lands after the uncertainty is persisted is not replayed and releases no
  newer hold. test:
  `crates/gterminals/tests/family_cutover.rs::uncertain_hold_admits_nothing_before_proof`.
- 2.4.17 - A startup that selects `Native` while the terminal family is unregistered is refused
  before any ownership change, and `Proxy` and `Compare` keep the Python host manager, lease
  registry, and write coordinator. test:
  `crates/gterminals/tests/family_cutover.rs::native_activation_refuses_without_the_terminal_family`.

### 2.5 Rust Native write runtime and the host connection opener [category: code] (depends: 2.4)

`kind: deliverable`

Targets:
- `crates/gterminals/src/control.rs::*` — scope-reason: add ControlClient::write_batch
- `crates/gterminals/src/host.rs::*` — scope-reason: move the adoption and connection functions into the new connect submodule and declare it
- `crates/gterminals/src/host/connect.rs`
- `crates/gterminals/src/native_write.rs`
- `crates/gterminals/src/lib.rs::*` — scope-reason: declare the native_write module
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `crates/gterminals/tests/write_runtimes.rs`

Research context:

- No production code implements `write.rs::Runtime` or `BatchRuntime`, and nothing outside tests
  calls `Coordinator::new`. `Runtimes` (write.rs:250) holds `native` and `tmux` as
  `Option<Arc<dyn Runtime>>`, and an absent runtime refuses with `Refusal::RuntimeUnavailable`.
  This section builds the Native runtime, 2.11 builds the tmux runtime, and 2.6 constructs the
  coordinator over both.
- Connections — Orchestrator ruling (2026-10-15): the gterm host accepts several authenticated
  control connections. Its host state keeps a `control_owners` set, `claim_control_owner`
  (`crates/gterminal/src/host/control.rs:209`) adds one, and a disconnect drops only that
  connection and its unprepared reservations. Each Rust component therefore opens its own
  connection, and there is no shared control handle.
- Split: `crates/gterminals/src/host.rs` is 916 lines. This section moves `adopt_or_spawn`,
  `connect`, `authenticate`, `handshake`, `read_pid`, `verify_pid`, and `process_is_host`
  (host.rs:662-822) unchanged from `crates/gterminals/src/host.rs` into the new
  `crates/gterminals/src/host/connect.rs`, declared with `mod connect;` beside `mod epochs;`.
  The new module adds `pub(crate) fn open_control(options) -> ControlClient`: it connects to
  `gterm-control.sock` under the configured socket dir, reads `gterm-control.token` without ever
  creating or rotating it, sends hello at the control protocol version, and verifies the host pid
  as the supervisor's handshake does. 2.8 and 2.9 open their own connections through it.
- `ControlClient` (control.rs, 733 lines) has `write` (512-528) and no `write_batch`. The gterm
  host serves `write_batch` (`crates/gterminal/src/host/control.rs:445`), and Python sends it at
  `host_client.py:584`. `ControlClient::write_batch` emits the bytes of the read-only fixture
  `crates/gterminal/tests/fixtures/wire_golden/control_write_batch.json`; no gterm byte changes.
- `native_write.rs`: `NativeWrite` implements `Runtime` and `BatchRuntime` over its own
  `open_control` connection, reopened after a transport failure. The oracle is
  `native_runtime.py::NativeTerminalRuntime._write` (593-648):
  - Text with submit writes the text without submit, sleeps `TMUX_TEXT_ENTER_DELAY_SECONDS` (1.0,
    `agents/tmux/text_injection.py:16`), and then writes the Enter key.
  - An unavailable host or an error on the first write refuses with `Stage::None`. An error on
    the Enter step refuses with `Stage::Partial`.
  - A connection error after send returns `Delivery::Uncertain`.
  - A single-step host command error is the typed host refusal that the coordinator maps (1.5).
  - A row whose `host_epoch` differs from the connection's hello epoch refuses with `Stage::None`
    before any byte is sent.
  - A batch returns one result per target and preserves an uncertain target.
- Production does not register the runtime here; 2.6 does. `lib.rs` declares
  `pub mod native_write`. `crates/gdaemon/Cargo.toml`, `Cargo.lock`, and `version_pins.py` carry
  the gdaemon 0.0.1 patch bump.
- The tmux write runtime, formerly 2.5.3 and 2.5.4, is 2.11 (Adversary F5, 2026-10-15): it shares
  no code with `NativeWrite` or `open_control` and is tested on its own.
- **Granularity:** three acceptance items over the Native write runtime and the connection opener
  it uses. They close together because the runtime reaches the host only through `open_control`,
  and the split that makes room for the opener moves the adoption code it reuses.

Consumers unchanged:
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: It reads `MANAGED_BIN_VERSION_PINS` by binary name, and the bump changes only the `gdaemon` value.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: It iterates `MANAGED_BIN_VERSION_PINS` as version floors, and the bump changes only the `gdaemon` value.
- `src/gobby/install/distribution.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: It reads or patches `MANAGED_BIN_VERSION_PINS` by binary name and hard-codes no gdaemon version.
- `tests/cli/test_install_ghook.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/install/test_distribution.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/install/test_version_pins.py` — no-edit-reason: It checks each pin against its crate's `Cargo.toml` version, and the bump changes both together.

**Acceptance:**

- 2.5.1 - A Native text write with submit sends the text, waits the Enter delay, and sends Enter.
  An unavailable host or a first-write error refuses with stage none, an Enter-step error refuses
  with stage partial, a connection loss after send is uncertain, and a row epoch that differs from
  the hello epoch refuses before any byte. test:
  `crates/gterminals/tests/write_runtimes.rs::native_write_matches_python_stage_semantics`.
- 2.5.2 - A Native batch emits the `control_write_batch.json` fixture bytes, returns one result
  per target, and preserves an uncertain target. test:
  `crates/gterminals/tests/write_runtimes.rs::native_batch_matches_gterm_write_batch_fixture`.
- 2.5.5 - `open_control` authenticates a connection beside the supervisor's without creating or
  rotating the token file, and the moved adoption code keeps `host_lifecycle.rs` passing. test:
  `crates/gterminals/tests/write_runtimes.rs::open_control_authenticates_an_independent_connection`.

### 2.11 Rust tmux write runtime [category: code] (depends: 2.5)

`kind: deliverable`

Targets:
- `crates/gterminals/src/tmux_write.rs`
- `crates/gterminals/src/lib.rs::*` — scope-reason: declare the tmux_write module
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `crates/gterminals/tests/tmux_write.rs`

Research context:

- No production code implements `write.rs::Runtime` (write.rs:205-216: `write` and an optional
  `batch`). `Runtimes` (write.rs:250) holds `tmux` as `Option<Arc<dyn Runtime>>`, and an absent
  runtime refuses with `Refusal::RuntimeUnavailable`. 2.6 constructs the coordinator over 2.5's
  `NativeWrite` and this section's `TmuxWrite`.
- Orchestrator tmux ruling (2026-10-15, option A): `TmuxWrite` implements `Runtime::write` for
  Text, Key, Paste, and Input, and has inherent `resize` and `release_size` methods, which 2.2's WS
  sizing calls for tmux rows. Every operation runs on the row's recorded tmux socket with the argv
  `tmux_runtime.py::TmuxTerminalRuntime` uses: `write_text` (215-241), `write_key` (243-263),
  `write_input` (265-292), `write_paste` (294-313), `resize` (315-330), and `release_size`
  (332-337).
- Generation check: before every operation, `tmux display-message -p -t <target>` with
  `_GENERATION_FORMAT` (`#{pid}\t#{start_time}`, tmux_runtime.py:51) reads `server_pid` and
  `server_start_time`, which must equal the row's locator (oracle `_same_generation`, 108-124).
  `_require_generation` (126-130) refuses with stage none before the first byte, or stage partial
  after an earlier step.
- Python keeps the spawn-less `TmuxTerminalRuntime` for reads, liveness, and termination. #23214
  (1.8, the complete Rust tmux adapter) closed obsolete on 2026-10-14. Refusing tmux writes would
  break send_keys, wakes, and post-compaction continuation for hand-started panes (#22856), so this
  write-only adapter is the least complete mechanism (restraint rung 2). No tmux spawn path is
  added.
- Order: 2.11 follows 2.5 because both declare a module in `lib.rs` and carry a gdaemon bump
  through the same three files. `lib.rs` declares `pub mod tmux_write`. `crates/gdaemon/Cargo.toml`,
  `Cargo.lock`, and `version_pins.py` carry the gdaemon 0.0.1 patch bump.

Consumers unchanged:
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: It reads `MANAGED_BIN_VERSION_PINS` by binary name, and the bump changes only the `gdaemon` value.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: It iterates `MANAGED_BIN_VERSION_PINS` as version floors, and the bump changes only the `gdaemon` value.
- `src/gobby/install/distribution.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: It reads or patches `MANAGED_BIN_VERSION_PINS` by binary name and hard-codes no gdaemon version.
- `tests/cli/test_install_ghook.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/install/test_distribution.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/install/test_version_pins.py` — no-edit-reason: It checks each pin against its crate's `Cargo.toml` version, and the bump changes both together.

**Acceptance:**

- 2.11.1 - tmux writes run only on the row's recorded socket and refuse before sending when
  `server_pid` or `server_start_time` differs from the locator. test:
  `crates/gterminals/tests/tmux_write.rs::tmux_write_is_socket_and_generation_scoped`.
- 2.11.2 - tmux `resize` and `release_size` run the same argv as Python under the same generation
  check. test:
  `crates/gterminals/tests/tmux_write.rs::tmux_resize_and_release_size_match_python`.

### 2.6 Loopback write route over the Rust coordinator [category: code] (depends: 2.5, 2.11)

`kind: deliverable`

Targets:
- `crates/gterminals/src/write_api.rs`
- `crates/gterminals/src/family.rs::*` — scope-reason: construct the lease registry and the coordinator over the Native and tmux write runtimes
- `crates/gdaemon/src/front_door/terminal_api.rs`
- `crates/gdaemon/src/front_door/mod.rs::*` — scope-reason: declare terminal_api and dispatch its paths from FrontDoor::handle before backend proxying
- `crates/gdaemon/src/state.rs::RuntimeState`
- `crates/gdaemon/src/state.rs::RuntimeState::prepare`
- `crates/gdaemon/src/state.rs::RuntimeState::run`
- `crates/gdaemon/src/state_services.rs::*` — scope-reason: ActiveServices exposes the Native terminal route services it constructs
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `crates/gterminals/tests/write_route.rs`
- `crates/gdaemon/tests/front_door.rs::*` — scope-reason: pin the loopback and current-token gate on the terminal write route

Research context:

- Placement (restraint rung 2): `FrontDoor::handle` (front_door/mod.rs:99-197) already dispatches
  native internal routes before proxying, inside `if let Some(runtime) = &state.runtime`:
  `lease::admin_api::is_route` matches exact paths, and `handle(runtime, request)` serves them.
  The write route follows that pattern at `POST /api/internal/terminals/write`, and 2.9 adds
  `POST /api/internal/terminals/runtime`. gterminals has no HTTP dependency, so the HTTP glue
  lives in the new gdaemon `front_door/terminal_api.rs`, and `write_api.rs` gains the typed serde
  calls, replies, and dispatch beside 2.4's TokenSlot.
- Gate, checked in order:
  - The peer must be loopback (`peer.to_canonical().is_loopback()`, the predicate
    `serve.rs::plaintext_allowed` at serve.rs:363-365), else 403. Loopback peers get plaintext on
    the daemon port.
  - `X-Gobby-Write-Token` must pass `TokenSlot::authorize` for the current backend child, else
    401. The token is never logged.
  - With no backend child or no Native route services, the route answers 503.
  - The body is capped at `MAX_CONTROL_LINE` (2 MiB, control.rs:14) plus a 64 KiB envelope
    allowance, else 413. 2.4's 256 KiB is the held-action queue's byte budget and stays separate
    queue accounting (2.4.14).
  - A serve without a supervised backend has no `RuntimeState`, so it exposes no route.
- Payload limits — Adversary F3: in process, the runtimes accept a paste up to
  `MAX_INPUT_PAYLOAD_BYTES` (1 MiB UTF-8) and an input up to `MAX_RAW_INPUT_PAYLOAD_BYTES`
  (64 KiB) (runtime.py:44-45, enforced at native_runtime.py:583-590 and tmux_runtime.py:266 and
  295), and a Native text or key step only needs its host control line under `MAX_CONTROL_LINE`
  (`NativeTerminalRuntime.preflight_line`, native_runtime.py:281-286). The route keeps those
  limits:
  - Each step payload travels base64 in the body, as it does on the host control line, so a
    step's body bytes never exceed what its host line carries. JSON string escaping would inflate
    control characters up to sixfold. The serde request type in `write_api.rs` decodes each payload
    into the UTF-8 `Step.payload` (write.rs:117) and refuses an undecodable one with stage none.
  - `write_api.rs` refuses a paste over 1 MiB, an input over 64 KiB, or a text or key payload
    whose base64 alone reaches `MAX_CONTROL_LINE` with stage none before the coordinator runs. The
    exact line check stays where it is: `ControlClient` refuses a line over `MAX_CONTROL_LINE` with
    `ControlError::LineTooLong` before writing a byte (control.rs:635-640).
  - The cap admits the largest single step plus the JSON envelope and its companion steps, and
    every production sequence fits. `run_sequence` callers send at most one text step beside keys
    and delays: spawn_executor_support.py:488, interactive_capacity.py:191, recovery.py:269 through
    `_deliver`'s callers at lines 296, 377, and 390, recovery.py:570, and orchestration.py:251
    through `_deliver_wake_action`. Plan approval (plan_approval.py:404) sends single-character
    input steps, two of them for `C-r` followed by a literal (plan_keystrokes.py:454). A sequence
    over the cap is refused whole; no chunking or reassembly mechanism exists.
  - 2.7's client applies the same limits before send, and 2.9's runtime route shares this gate and
    cap.
- Construction: in `Native`, `TerminalFamily` builds `leases::Registry::new(256)` (the port of
  `LIFECYCLE_PUBLICATION_QUEUE_MAXSIZE`) and
  `Coordinator::new(pool, Arc<leases::Registry>, Runtimes { native: NativeWrite, tmux: TmuxWrite })`
  once. `Registry::new` also returns the lifecycle `mpsc::Receiver`, which the family keeps for
  2.3's relay. Before 2.2 nothing claims a Rust lease, because only WS attach does, so nothing
  publishes to it.
- State: `RuntimeState` keeps a clone of the backend's write `TokenSlot` from `prepare`, and
  `run` publishes the Native route services after `ActiveServices::start` and clears them
  before stop.
- Calls: `write`, `run_sequence`, `run_native_wake_batch`, `quarantine`, `retain_unresolved`,
  `observe_resolved`, `observe_operator_input`, and `clear_on_exit` (write.rs:341-686), plus
  `acquire_action` and `release_action` on 2.4's hold. Replies carry 1.5's typed per-step
  outcome, serde-tagged as `Delivered`, `Uncertain` with its detail, `Suppressed`,
  `Quarantined`, or `Refusal` with its stage (`None` or `Partial`).
- Held action, route half — PD ruling (gobby#14972, 2026-10-01, Adversary F2), moved here from
  #23217:
  - `acquire_action` returns a hold id bound to the current child token, terminal row, and lease
    generation.
  - An operation carrying the hold id is admitted after the coordinator re-reads the row and
    lease (1.5.2). A stale generation, lost lease, or rotated token refuses it.
  - Unheld operations queue under 2.4's bounds.
  - Recovery: a lost acquire, step, or release reply never replays a non-idempotent uncertain
    write. The action key and unresolved evidence (1.5.3) decide, and a child restart's new token
    invalidates every old hold.
  - An HTTP disconnect during an admitted write records uncertainty only.
- `crates/gdaemon/Cargo.toml`, `Cargo.lock`, and `version_pins.py` carry the gdaemon 0.0.1 patch
  bump.
- **Granularity:** five acceptance items over one route: its gate, its typed replies, the
  held-action binding, lost-reply recovery, and its payload limits. They close together because
  the binding, the recovery rule, and the payload limits are the route's admission rules, and the
  gate is what makes the route safe to expose.

Consumers unchanged:
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: It reads `MANAGED_BIN_VERSION_PINS` by binary name, and the bump changes only the `gdaemon` value.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: It iterates `MANAGED_BIN_VERSION_PINS` as version floors, and the bump changes only the `gdaemon` value.
- `src/gobby/install/distribution.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: It reads or patches `MANAGED_BIN_VERSION_PINS` by binary name and hard-codes no gdaemon version.
- `tests/cli/test_install_ghook.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/install/test_distribution.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/install/test_version_pins.py` — no-edit-reason: It checks each pin against its crate's `Cargo.toml` version, and the bump changes both together.

**Acceptance:**

- 2.6.1 - A non-loopback peer gets 403; a missing, wrong, or retired token gets 401; without a
  backend child or Native route services the route answers 503; a body over `MAX_CONTROL_LINE`
  plus 64 KiB gets 413; and the token never appears in a log. test:
  `crates/gdaemon/tests/front_door.rs::terminal_write_route_requires_loopback_and_the_current_token`.
- 2.6.2 - Each route call returns 1.5's typed per-step outcome, and a refusal carries its stage.
  test: `crates/gterminals/tests/write_route.rs::route_calls_return_typed_per_step_outcomes`.
- 2.6.3 - A held operation is admitted only after the row and lease revalidate; a stale
  generation, lost lease, or rotated token refuses it. test:
  `crates/gterminals/tests/write_route.rs::held_action_binds_token_row_and_lease_generation`.
- 2.6.4 - A token rotation invalidates every old hold, and a lost acquire, step, or release reply
  or an HTTP disconnect never replays a non-idempotent uncertain write. test:
  `crates/gterminals/tests/write_route.rs::token_rotation_and_lost_replies_never_replay_writes`.
- 2.6.5 - The route admits a 1 MiB paste, a 64 KiB input, and plan approval's two-input `C-r`
  sequence, each carried base64; it refuses a paste or input one byte over its limit, and an
  undecodable payload, with stage none before the coordinator runs; and held-queue accounting
  keeps 2.4's 256 KiB bound. test:
  `crates/gterminals/tests/write_route.rs::route_admits_each_operation_maximum_and_refuses_over_limit_steps`.

### 2.7 Python write client, Native composer ledger, and Native write wiring [category: code] (depends: 2.6)

`kind: deliverable`

Targets:
- `src/gobby/terminals/coordinator_client.py`
- `src/gobby/terminals/coordinated_submit.py`
- `src/gobby/terminals/pane_io.py::*` — scope-reason: move the verified-submit block into coordinated_submit.py and drop submit_coordinated_text from __all__
- `src/gobby/terminals/workspace_writes.py::*` — scope-reason: import submit_coordinated_text from coordinated_submit
- `src/gobby/terminals/host_event_reader.py::*` — scope-reason: add the Native ledger event loop and the task that runs it beside ledger persistence
- `src/gobby/runner_init/terminal_wiring.py::init_terminal_wiring`
- `src/gobby/runner_init/terminal_host_startup.py`
- `src/gobby/runner_lifecycle_subsystems.py::*` — scope-reason: move _start_terminal_host into terminal_host_startup.py and re-bind it by import
- `tests/terminals/test_coordinator_client.py`
- `tests/terminals/test_workspace_ops.py::*` — scope-reason: import _verified_submits from coordinated_submit and patch it there

Research context:

- Callers: in `Native`, Python constructs no `WriteCoordinator` (A1), yet about 30 modules call
  it in-process: attention routes, the plan-approval and session-config WS handlers, session
  send-keys and compaction, compact continuation, `workspace_writes` and `pane_io`, spawn
  support, watchdog recovery, and terminal lifecycle monitoring. `init_terminal_wiring` binds
  `coordinator_client.RustWriteCoordinatorClient` where it binds `WriteCoordinator` today:
  `runner.write_coordinator`, `bind_wake_write_services`, `bind_composer_coordinator`, and the
  `TerminalEffectBridge`.
- Typing — Orchestrator ruling: the client subclasses `WriteCoordinator`. Sixteen modules name
  it in annotations only, with no isinstance check. A Protocol retype of 14 modules was rejected
  because it forces a split of the 899-line `WorkspaceOps` module, and a rename was rejected (87
  constructor sites in 36 files). The client does not call `WriteCoordinator.__init__`, which
  reads `lease_registry.daemon_epoch` while `Native` has no Python lease registry; nothing reads
  `coordinator.lease_registry`. It sets `_registry`, `composer_ledger`, and `_attention_gate`,
  inherits `runtime_for` and `set_attention_gate`, and overrides `write`, `run_sequence`,
  `run_native_wake_batch`, `quarantine`, `retain_unresolved`, `observe_resolved(_async)`,
  `observe_operator_input(_async)`, `clear_on_exit`, `logical_action_lock`, and `lock_held`. A
  reflection test fails when any other public `WriteCoordinator` method lacks an override.
- Transport: the client posts to `http://127.0.0.1:{daemon_port}` from bootstrap and reads
  `runner_backend.supervisor_write_token()` on every request. A missing token raises
  `TerminalWriteError(stage="none")` before send, and the token is never logged. It uses
  `httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(None, connect=2.5))`. A request
  carrying `client_fd` is refused with stage none; nothing produces one (write_coordinator.py:83).
  The attention gate runs locally before a write is forwarded.
- Payload limits — Adversary F3: the client encodes each step payload base64 as 2.6 reads it.
  Before send it raises `InputPayloadTooLargeError` for a paste over `MAX_INPUT_PAYLOAD_BYTES` or
  an input over `MAX_RAW_INPUT_PAYLOAD_BYTES` (runtime.py:44-45), the exception the in-process
  runtimes raise, so `workspace_pane_io.py:224`, which catches it, keeps its behavior. It raises
  `TerminalWriteError(stage="none")` when the serialized body exceeds 2.6's cap of
  `MAX_CONTROL_LINE` (host_client.py:21) plus 64 KiB.
- Sync methods: the only production sync caller is `sync_bridge.py:81`, on the loop thread. Sync
  methods enqueue on a per-terminal ordered send chain through `loop.call_soon_threadsafe` and
  return immediately; the async variants await the same chain.
- Action lock — PD ruling (gobby#14972, 2026-10-01, Adversary F2):
  `logical_action_lock(terminal_id)` returns an `asyncio.Lock` subclass whose async enter takes
  the local lock and then `acquire_action`, and whose exit releases the remote hold and then the
  local lock, so Python-side exclusion is unchanged. Its callers use only `async with`
  (`watchdog/interactive_capacity.py:91`, `watchdog/recovery.py:376`,
  `terminals/composer_lock.py:71`). `lock_held` has no production caller. `run_sequence` and
  `run_native_wake_batch` stay single route calls, and the hold is the only cross-call atomicity
  mechanism.
- Split: `pane_io.py` is 875 lines. This section moves `_HELD_VERIFIED_SUBMIT_LIMIT`,
  `_VerifiedSubmitProgress`, `_verified_submits`, `_held_submit`, `_remember_held`,
  `_forget_held`, and `submit_coordinated_text` (pane_io.py:772-875) from
  `src/gobby/terminals/pane_io.py` into the new `src/gobby/terminals/coordinated_submit.py`, and
  drops `submit_coordinated_text` from pane_io's `__all__` (line 75). There
  `submit_coordinated_text` runs its unchanged ladder inside
  `async with coordinator.logical_action_lock(terminal.id)`. Its only caller,
  `workspace_writes.write_workspace_pane` (call at line 59, import at line 12), holds none, and
  the recovery and wake paths already hold it. `tests/terminals/test_workspace_ops.py` imports
  `_verified_submits` (line 59) and patches it by module string (line 1284).
- Native composer ledger — Orchestrator ruling (2026-10-15): today `WriteCoordinator.write`
  calls `composer_ledger.observe_write` before dispatch (write_coordinator.py:758),
  `hooks/event_handlers/_base.py:246` reads `coordinator.composer_ledger`, and
  `event_reader_loop` needs a `TerminalHostManager`. In `Native` the client keeps a local
  `ComposerLedger`, loaded with `load_ledger(composer_ledger_path())` and bound with
  `bind_composer_ledger` as `terminal_wiring.py:100` does today. It calls `observe_write` for
  every step the route reply reports as dispatched, accepted or uncertain. A reply lost after
  send marks the terminal blocked with `block(terminal_id, "gap")`. The ledger stays in Python in
  this epic.
- Partial refusal — Orchestrator ruling (2026-10-15 22:12 CDT): a `Refusal` with stage `Partial`
  (the text landed, Enter did not) counts as dispatched, and the client records it as
  `observe_write(text, submit=False)`. The text is in the composer, so treating it as undelivered
  would retype and duplicate it, and 2.7.4 requires never typing over a draft.
- Native event reader: `host_event_reader.ledger_event_loop` opens
  `HostClient.open_event_stream(control_socket_path(socket_dir), token, since=<ledger cursor seq>)`.
  `socket_dir` is `expand_socket_dir(TerminalHostConfig.socket_dir)` (host_protocol.py:19), the
  same `terminal_host.socket_dir` Rust reads (host.rs:134). It reads `control_token_path`
  (host_protocol.py:36) and never creates or rotates the token. It calls
  `resume_host(stream.epoch, stream.seq, since, gap)` and then feeds each event to
  `observe_host_event`. On an error it closes the stream and retries after
  `NATIVE_LEDGER_RETRY_SECONDS` = 1.0. The gterm host accepts this event-only connection beside
  the Rust ones (2.5).
- Startup and shutdown: today `runner_lifecycle_subsystems.py:291-297` persists
  `host.composer_ledger`, and in `Native` `_start_terminal_host` returns early (293-295).
  `runner_lifecycle_subsystems.py` is 883 lines, so this section moves `_start_terminal_host`
  (289-313) from `src/gobby/runner_lifecycle_subsystems.py` into the new
  `src/gobby/runner_init/terminal_host_startup.py` as `start_terminal_host`, imported back as
  `_start_terminal_host`; its call site (line 814) and tests
  (`tests/test_runner_lifecycle_subsystems.py`, `tests/test_runner_lifecycle.py:5591`,
  `tests/test_runner_backlog.py:55`) are unchanged. Its `Native` branch sets
  `runner._composer_ledger_task` to `native_ledger_task`, which runs `persist_ledger` and
  `ledger_event_loop` in one task, so the existing cancel at `runner_lifecycle_shutdown.py:736`
  stops both and `persist_ledger` flushes in its finally.
- Comment fix: `terminal_wiring.py:67-68` says a second hello takes gterm control ownership. The
  host accepts several control connections (2.5), so this section corrects the comment.
- **Granularity:** six acceptance items over the Python side of one boundary: the write client
  and its payload limits, the verified-submit move it wraps, and the Native ledger it feeds. They
  close together because a Native write client without the ledger feed could type over an
  operator draft, and one without the limits would send requests the route refuses.

Consumers unchanged:
- `src/gobby/runner_init/orchestration.py` — no-edit-reason: It calls `init_terminal_wiring` through the unchanged signature, and its wake batch already runs under `logical_action_lock`.
- `src/gobby/agents/watchdog/recovery.py` — no-edit-reason: `_attempt_idle_reprompt` already runs its steps under `logical_action_lock`, which the client backs with the route hold.
- `src/gobby/hooks/event_handlers/_base.py` — no-edit-reason: It reads `coordinator.composer_ledger`, which the client sets.
- `src/gobby/runner_lifecycle_shutdown.py` — no-edit-reason: It cancels `runner._composer_ledger_task`, which in `Native` runs ledger persistence and the event loop together.
- `src/gobby/terminals/sync_bridge.py` — no-edit-reason: Its synchronous coordinator call runs on the loop thread, and the client queues it without blocking.
- `src/gobby/terminals/workspace_pane_io.py` — no-edit-reason: It catches `InputPayloadTooLargeError`, which the client raises before send as the in-process runtimes do.
- `tests/test_runner_lifecycle_subsystems.py` — no-edit-reason: It reaches `_start_terminal_host` through the re-bound name.
- `tests/test_runner_lifecycle.py` — no-edit-reason: It reaches `_start_terminal_host` through the re-bound name.
- `tests/test_runner_backlog.py` — no-edit-reason: It reaches `_start_terminal_host` through the re-bound name.

**Acceptance:**

- 2.7.1 - In `Native`, every Python coordinator caller reaches the Rust coordinator through
  `RustWriteCoordinatorClient`, and #22722's submit regressions pass against it: an unreadable
  composer draft is never reported as confirmed success (#23188's contract), and a held verified
  submit retries without a second write, and `submit_coordinated_text` runs its unchanged ladder
  inside `logical_action_lock`. test:
  `tests/terminals/test_coordinator_client.py::test_pane_io_submit_regressions_hold_against_rust_coordinator`.
- 2.7.2 - The client overrides every public `WriteCoordinator` method except the inherited
  `runtime_for` and `set_attention_gate`; sync methods return without blocking the loop and keep
  per-terminal order; and `logical_action_lock` holds the local lock and the remote hold
  together. test:
  `tests/terminals/test_coordinator_client.py::test_client_covers_the_coordinator_surface_without_blocking_the_loop`.
- 2.7.3 - A missing token, or a request carrying `client_fd`, refuses with stage none before
  send, and the token never appears in a log. test:
  `tests/terminals/test_coordinator_client.py::test_client_fails_closed_without_the_supervisor_token`.
- 2.7.4 - In `Native`, an automatic write never types over an operator draft: every dispatched
  step reaches the ledger, a partial refusal records the text without submit, and a reply lost
  after send blocks automatic writes to that terminal with reason gap. test:
  `tests/terminals/test_coordinator_client.py::test_native_automatic_write_never_types_over_an_operator_draft`.
- 2.7.5 - The Native ledger restores from `composer_ledger_path()`, persists until shutdown
  cancels its task, and follows host events from its cursor across a stream error. test:
  `tests/terminals/test_coordinator_client.py::test_native_ledger_restores_persists_and_follows_host_events`.
- 2.7.6 - The client sends a 1 MiB paste base64 in one route call; it raises
  `InputPayloadTooLargeError` for a paste or input one byte over its limit and
  `TerminalWriteError(stage="none")` for a body over 2.6's cap, each before send. test:
  `tests/terminals/test_coordinator_client.py::test_client_enforces_runtime_payload_limits_before_send`.

### 2.8 Native reconcile settler and the host exit proof [category: code] (depends: 2.6, 1.10)

`kind: deliverable`

Targets:
- `crates/gterminals/src/settler.rs`
- `crates/gterminals/src/process.rs`
- `crates/gterminals/src/host/connect.rs`
- `crates/gterminals/src/host/epochs.rs::*` — scope-reason: add record_absent_exit beside record_confirmed_exit, and take the epoch lock in observe through reconcile::lock_host_epochs
- `crates/gterminals/src/reconcile.rs::*` — scope-reason: add lock_host_epochs and fence each dead-epoch effect on the current epoch
- `crates/gterminals/src/control.rs::*` — scope-reason: add ControlClient::revoke_input
- `crates/gterminals/src/family.rs::*` — scope-reason: start the Native settler beside the supervisor
- `crates/gterminals/src/lib.rs::*` — scope-reason: declare the settler and process modules
- `crates/gterminals/Cargo.toml`
- `crates/gdaemon/src/nodes/hooks/ledger.rs::*` — scope-reason: process_start moves to gobby_terminals::process and its two callers import it from there
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `crates/gterminals/tests/settler.rs`
- `crates/gterminals/tests/reconcile.rs::*` — scope-reason: seed host epoch rows for the dead-epoch fence and supersede an epoch between snapshot and apply

Research context:

- No production code calls `reconcile::reconcile` or implements `Effects`; only
  `tests/reconcile.rs:63` does. This section adds the Native settler that does.
- First #24210 fold, verbatim from #23217: "When this task makes Rust reconcile the Native
  settler, its production call site passes dead_host_epochs built from the host supervisor's
  durable exited-epoch record on this machine (#24210 provides the record and its snapshot). It
  takes that set as a snapshot and applies it only after verifying the observed epoch is still the
  machine's current host epoch. If the epoch changed in between, the pass settles no dead-epoch
  rows."
- Epoch fence — Adversary F4: the pass's snapshot check (step 3 below) runs before reconcile's
  per-row transactions, so an epoch observed in between could still be applied as dead.
  - `reconcile.rs` gains `pub(crate) async fn lock_host_epochs(tx, machine)`, which runs exactly
    `SELECT pg_advisory_xact_lock(hashtextextended($1, 24210))` with `machine.to_string()` as its
    text argument. `epochs.rs::observe` calls it in place of its inline statement
    (epochs.rs:96-102), so both sides take one lock. The helper lives in `reconcile.rs` because
    `mod epochs` is private under `host.rs`.
  - In `reconcile::reconcile`, only a row whose `dead_epoch` test passes (reconcile.rs:341-343)
    takes the lock, inside its per-row transaction after `get_for_update`, and re-reads
    `terminal_host_epochs`. The dead-epoch effect (`Live` to `Orphaned`, or `Orphaned` to `Exited`,
    reconcile.rs:367-373) applies only while `inventory.epoch` is still the machine's
    `is_current` epoch and the row's `host_epoch` has `exited_at` set; otherwise the row is left
    unchanged. No `Options` field is added.
  - Lock order is row, then epoch. `observe` takes no row lock, so the two cannot deadlock, and a
    row without a dead-epoch candidate never serializes against `observe`. Host I/O stays outside
    the transaction.
  - The six tests in `tests/reconcile.rs` that put epochs in `dead_host_epochs` (lines 204, 248,
    295, 386, 423, and 478) also seed the observed epoch as `is_current` and each dead epoch with
    `exited_at` set, or the fence would refuse their effects.
- Input handoff expiry — LOW-1, carried from #23219 (Orchestrator, 2026-10-15):
  `reconcile::expire_input_handoffs` (reconcile.rs:419) has no production caller, and
  `InputRevoke` (reconcile.rs:409) has no production implementation (1.10).
  - Each settler pass, after reconcile, calls `expire_input_handoffs(pool, machine, observed
    epoch, &mut deadlines, Instant::now(), &revoker)`, and the settler carries `HandoffDeadlines`
    across passes.
  - The revoker implements `InputRevoke` over the settler's connection through the new
    `ControlClient::revoke_input`, which emits the bytes of the read-only fixture
    `crates/gterminal/tests/fixtures/wire_golden/control_revoke_input.json`.
  - A `ControlError::Host` with code `not_found` maps to `Ok`, so the record clears. Any other
    failure keeps the record for the next pass. The oracle is `input_grants.py` lines 145-150.
  - 2.2's input-grant applier reuses `revoke_input`.
- Pass: `settler.rs` runs one pass after each `HostStatus::Ready` (from
  `HostSupervisor::subscribe`) and on each health interval:
  1. Take `HostSupervisor::epoch_snapshot` (host.rs:392-396).
  2. Over its own `open_control` connection (2.5), read the hello epoch, list the inventory, and
     ping.
  3. If the observed epoch differs from `snapshot.current_epoch`, `dead_host_epochs` is empty.
     Otherwise it is `exited_epochs` minus the observed epoch (the `Options` contract at
     reconcile.rs:57-63).
  4. Call `reconcile::reconcile(pool, machine, Observation::Accepted(&inventory), &options,
     &effects)` with `Options { now, spawn_in_doubt, settle_indeterminate, dead_host_epochs }`.
  `spawn_in_doubt` comes from `terminals.spawn_in_doubt_seconds` through the config pool lookup
  that `HostOptions::from_pool` uses. `settle_indeterminate` is true on the first pass after each
  Ready, as `host_event_reader.py::recover_event_gap` (line 85) does, and false on interval
  passes, the `TerminalHostManager.reconcile` default (line 543).
- Exit proof — fold (a), verbatim (reviewer gobby#16183 on #24210 at 3fa292372e, 2026-10-13):
  "epochs.rs:125-130 demotes a prior current epoch without exited_at. A host that dies while no
  supervisor runs (reboot, daemon down) never enters exited_epochs, so #23217's restart caller
  needs a startup exit proof (e.g. recorded pid+start_time or boot time) or those Live rows never
  settle." On the current tree the demote UPDATE is at epochs.rs:125-129, unchanged since
  3fa292372e.
  - Decision — Orchestrator ruling (2026-10-15): confirmed absence is the proof.
    `adopt_or_spawn` takes its spawn branch only when `connect()` returns none (the control
    socket is missing or refuses, and `gterm.pid` is absent or names no live, non-zombie gterm)
    and no child is held. Before spawning there, it calls the new
    `epochs.rs::record_absent_exit` beside `record_confirmed_exit`, which reads `epoch_snapshot`
    and, when a current epoch exists, calls `observe_exit(machine, current)`. A `PreAdoptionIo`
    "live host is not listening" failure records nothing. In `Proxy` and `Compare` Python owns
    old-epoch rows (`host_reconcile.py:371`).
  - Rejected: migration 469 recording host pid and start time, plus a gcore process module. It
    would bump gcode and ghook, and the gcode version floor would force a split of the 969-line
    `gcode_gateway.py`.
- Process status — fold (b), verbatim: "Python treats a non-int or <=0 pgid as dead. Rust
  reconcile short-circuits only a missing/null pgid, so #23217's production
  Effects::process_status must match."
  - `Effects::process_status` ports `host_reap.py::recorded_process_group_is_alive` (52-62). A
    missing, non-integer, or non-positive pgid is dead. With a numeric start time, a recycled
    pgid (`_pid_recycled`: process start differs by more than 1.0 s) is dead, and a lookup error
    counts as not recycled. `_group_alive` (88-99) treats `killpg(pgid, 0)` success as alive,
    falls back to `kill(pgid, 0)` on ESRCH, and treats EPERM as alive. The port uses rustix's
    `test_kill_process_group` and `test_kill_process` with `process::process_start`.
  - `process_start` (ledger.rs:536-581: macOS `proc_pidinfo` `PROC_PIDTBSDINFO` with
    `pbi_status != 5` and start `pbi_start_tvsec + pbi_start_tvusec / 1e6`; Linux `/proc` stat
    with `btime` and `_SC_CLK_TCK`; none elsewhere) moves unchanged from
    `crates/gdaemon/src/nodes/hooks/ledger.rs` into the new `crates/gterminals/src/process.rs`.
    Its callers (ledger.rs lines 70 and 504) import `gobby_terminals::process::process_start`.
    gdaemon already depends on gobby-terminals (Cargo.toml:32), and `crates/gterminals/Cargo.toml`
    adds `libc = "0.2"`, already in the lockfile.
- Run interruption: `Effects::settle_run` ports the cancel transition the Python host reconciler
  runs, `_AgentRunLifecycleMixin.cancel(run_id, terminal_reason="daemon_stop")` through
  `_execute_terminal_transition` (`_lifecycle.py` 36-110 and 672-699), on the bare
  `LocalAgentRunManager(runner.database)` that `terminal_wiring` builds with no status notifier
  and no credential manager. Inside the reconcile transaction it:
  - takes `pg_advisory_xact_lock_shared(fence)`;
  - updates `agent_runs` to `cancelled` with the terminal reason, clears `pending_terminal_*`,
    `termination_requested_at`, and `pid`, and sets `completed_at` and `updated_at`, where the
    status is queued, pending, or running;
  - skips the terminal update for held ids (`Effects::holds`);
  - normalizes the sandbox count;
  - sets sessions in `LIVE_SESSION_STATUS_ORDER` with that `agent_run_id` to paused.
  Promotion and pending keep 1.3's contract (tests/reconcile.rs:88-118). Running inside the
  reconcile transaction keeps 1.3.5's atomicity, deliberately without Python's nesting guard.
- Fence key: the settler ports `deployment.py::deployment_advisory_key("agent-terminal-transition")`,
  the first 8 bytes of `sha256("gobby:{sha256(resolved home)[:16]}:agent-terminal-transition")`
  read as a signed big-endian integer, and a test pins the vector.
- `Effects::kill` uses `ControlClient::kill` on the settler's connection. `Effects::holds`
  reports no holds here; 2.9 fills it. `lib.rs` declares `pub mod settler` and `pub mod process`,
  and `family.rs` starts the settler beside the supervisor in `Native`. `crates/gdaemon/Cargo.toml`,
  `Cargo.lock`, and `version_pins.py` carry the gdaemon 0.0.1 patch bump.
- **Granularity:** six acceptance items over one owner, the Native settler: its pass and the
  epoch fence it applies under, the exit proof that feeds it, its process status, its run
  interruption, and its input handoff expiry. They close together because a settler without the
  exit proof never settles rows from a host that died while gdaemon was down, one without run
  interruption leaves their runs live, one without the fence can settle a live epoch's rows, and
  one without expiry leaves preserved input grants on the host forever.

Consumers unchanged:
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: It reads `MANAGED_BIN_VERSION_PINS` by binary name, and the bump changes only the `gdaemon` value.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: It iterates `MANAGED_BIN_VERSION_PINS` as version floors, and the bump changes only the `gdaemon` value.
- `src/gobby/install/distribution.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: It reads or patches `MANAGED_BIN_VERSION_PINS` by binary name and hard-codes no gdaemon version.
- `tests/cli/test_install_ghook.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/install/test_distribution.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/install/test_version_pins.py` — no-edit-reason: It checks each pin against its crate's `Cargo.toml` version, and the bump changes both together.

**Acceptance:**

- 2.8.1 - An integration test shows a host restart (old epoch exited, new epoch observed) settles
  the old epoch's absent live rows as orphaned and interrupts their runs. A row on the new epoch
  and rows on another machine stay untouched. test:
  `crates/gterminals/tests/settler.rs::host_restart_settles_only_the_dead_epoch`.
- 2.8.2 - A dead-epoch effect applies only under the epoch lock while the observed epoch is still
  current and the row's epoch has exited: an epoch superseded after the snapshot and before apply
  settles no dead-epoch row, and `observe` takes the same lock. test:
  `crates/gterminals/tests/reconcile.rs::superseded_epoch_between_snapshot_and_apply_settles_no_dead_epoch_rows`.
- 2.8.3 - When adoption confirms that no host is running, the prior current epoch is recorded as
  exited before the spawn, so its live rows settle on the next pass; a live host that is not
  listening records no exit. test:
  `crates/gterminals/tests/settler.rs::confirmed_absence_records_the_prior_epoch_exit`.
- 2.8.4 - Process status matches `recorded_process_group_is_alive` for a missing, non-integer, or
  non-positive pgid, a recycled pgid, ESRCH, and EPERM. test:
  `crates/gterminals/tests/settler.rs::process_status_matches_the_python_reap_oracle`.
- 2.8.5 - Run interruption leaves the same `agent_runs`, sandbox count, and session rows as the
  Python cancel transition with terminal reason daemon_stop, under the same fence key. test:
  `crates/gterminals/tests/settler.rs::run_interruption_matches_the_python_cancel_transition`.
- 2.8.6 - Each settler pass expires input handoff records with the production revoker: after the
  rebind window a record is revoked with `control_revoke_input.json`'s bytes and cleared, a host
  `not_found` also clears it, and any other revoke failure keeps it for the next pass. test:
  `crates/gterminals/tests/settler.rs::settler_expires_input_handoffs_through_the_host_revoke`.

### 2.9 Rust runtime route [category: code] (depends: 2.8)

`kind: deliverable`

Targets:
- `crates/gterminals/src/runtime_api.rs`
- `crates/gterminals/src/settler.rs`
- `crates/gterminals/src/family.rs::*` — scope-reason: construct the runtime route services in Native
- `crates/gterminals/src/lib.rs::*` — scope-reason: declare the runtime_api module
- `crates/gdaemon/src/front_door/terminal_api.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `crates/gterminals/tests/runtime_route.rs`
- `crates/gdaemon/tests/front_door.rs::*` — scope-reason: pin that the runtime route shares the write route's loopback and token gate

Research context:

- Runtime seam — PD ruling (gobby#14972, 2026-10-01, Adversary F5): in `Native` Python
  constructs no `TerminalHostManager` (1.2), so spawn, web-workspace, read, and termination
  callers need a non-supervising runtime. `runtime_api.rs` serves
  `POST /api/internal/terminals/runtime` behind 2.6's gate, including its `MAX_CONTROL_LINE` plus
  64 KiB body cap (Adversary F3), for native rows only. tmux rows keep
  Python's spawn-less `TmuxTerminalRuntime`. The oracle is `native_runtime.py::NativeTerminalRuntime`.
- Operations:
  - `prepare_spawn` inserts the pending row and sends spawn prepare.
  - `commit` sends spawn commit and settles in 1.3.5's transaction through `repository::settle`
    (pub(crate), repository.rs:142). Its reply carries the terminal id, locator, and host epoch
    that 2.10 records.
  - `fail` sends spawn abort and fails the pending row.
  - `reserve_observer` and `release_observer`.
  - `snapshot`, `snapshot_full`, `history`, `is_live`, and `session_present`.
  - `spawn_refusal` (oracle `native_runtime.py:165-172`, `refuse_unavailable_host`).
  - `terminate` (kill), `resize`, and `attach_locator`.
- Single settler — PD ruling: 2.8's settler stays the only settler. The route reports held
  prepared spawns through `Effects::holds`, the analogue of Python's `in_doubt_spawns.held_ids`,
  so a settler pass never settles a spawn the route still holds.
- Connection: the route opens its own connection with `open_control` (2.5) and reopens it after a
  transport failure. A transport failure between prepare and commit settles the attempt through
  1.3.5's failure path.
- `terminal_api.rs` adds the runtime path beside the write path. `lib.rs` declares
  `pub mod runtime_api`, and `family.rs` constructs it in `Native`. `crates/gdaemon/Cargo.toml`,
  `Cargo.lock`, and `version_pins.py` carry the gdaemon 0.0.1 patch bump.
- **Granularity:** four acceptance items over one route. Its Target files are the route module,
  the settler's hold report, the family and lib wiring, the gdaemon HTTP glue, and the version
  bump; they close together because the route without the hold report would let the settler
  settle a spawn the route holds.

Consumers unchanged:
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: It reads `MANAGED_BIN_VERSION_PINS` by binary name, and the bump changes only the `gdaemon` value.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: It iterates `MANAGED_BIN_VERSION_PINS` as version floors, and the bump changes only the `gdaemon` value.
- `src/gobby/install/distribution.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: It reads or patches `MANAGED_BIN_VERSION_PINS` by binary name and hard-codes no gdaemon version.
- `tests/cli/test_install_ghook.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/install/test_distribution.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/install/test_version_pins.py` — no-edit-reason: It checks each pin against its crate's `Cargo.toml` version, and the bump changes both together.

**Acceptance:**

- 2.9.1 - A Native spawn prepares, commits, and settles once with the Rust repository as the only
  settler; a settler pass never settles a held prepared spawn; and a transport failure between
  prepare and commit settles the attempt as failed. test:
  `crates/gterminals/tests/runtime_route.rs::spawn_settles_once_with_one_rust_owner`.
- 2.9.2 - Snapshot, full snapshot, history, liveness, session presence, spawn refusal, and attach
  locator replies match the Python `NativeTerminalRuntime` contract for native rows, and a tmux
  row is refused. test:
  `crates/gterminals/tests/runtime_route.rs::native_reads_match_the_python_runtime_contract`.
- 2.9.3 - Terminate and resize act only on the row's current host epoch and refuse a stale one.
  test: `crates/gterminals/tests/runtime_route.rs::terminate_and_resize_are_epoch_scoped`.
- 2.9.4 - The runtime route applies the write route's loopback, token, availability, and body
  gate. test:
  `crates/gdaemon/tests/front_door.rs::terminal_runtime_route_shares_the_write_route_gate`.

### 2.10 Python runtime client, Native settlement branches, pane binding, and terminal services [category: code] (depends: 2.7, 2.9)

`kind: deliverable`

Targets:
- `src/gobby/terminals/runtime_client.py`
- `src/gobby/runner_init/terminal_wiring.py::init_terminal_wiring`
- `src/gobby/agents/spawn_executor.py::resolve_terminal_services`
- `src/gobby/agents/spawn_executor_runtime.py::_runtime_spawn`
- `src/gobby/agents/lifecycle_reconciliation.py::LifecycleReconciliation.reap_stale_pending`
- `src/gobby/terminals/web_spawn.py::spawn_web_terminal`
- `src/gobby/runner_init/servers.py::init_servers`
- `tests/terminals/test_runtime_client.py`
- `tests/terminals/test_workspace_ops.py::*` — scope-reason: prove the Native workspace spawn writes no Python terminal rows and binds the pane only after Rust settles

Research context:

- Runtime seam — PD ruling (gobby#14972, 2026-10-01, Adversary F5): today
  `init_terminal_wiring` registers `NativeTerminalRuntime(HostManagerControl(...))`, and spawn,
  web-workspace, read, and termination callers reach the host through it. In `Native` it
  registers `runtime_client.RustNativeRuntimeClient` for the native backend and
  `TmuxTerminalRuntime(host_control=None)` for tmux; `host_control` is used only in
  `attach_locator` (tmux_runtime.py:352-359), and 2.2's Rust attach computes the tmux locator
  itself. The client is non-supervising. It implements the `TerminalRuntime` Protocol
  (runtime.py:222-256) and the duck-typed `spawn_refusal` (native_runtime.py:165-172) as
  operations on 2.9's route, and its `write_*` methods raise `TerminalWriteError(stage="none")`
  because every write goes through `RustWriteCoordinatorClient` (2.7). After a commit it calls
  `composer_ledger.record_spawn(terminal_id, epoch)` as `native_runtime.py:392` does.
  `native_runtime.py` (902 lines) is unchanged because the client is a separate implementation.
  The settled host decision 1.7 reads comes through this seam in `Native`.
- Settlement ownership — PD ruling: the 1.3 Rust repository is the single settler of pending and
  prepared rows. In `Native`, `_runtime_spawn` drives the client, and the Rust side creates,
  promotes, or fails the row in the 1.3.5 settlement transaction; `_unplaced_runtime_spawn` and
  `_promote_prepared` (the #23010 path) run only outside `Native`.
  `LifecycleReconciliation.reap_stale_pending` returns without a write in `Native` because Rust
  reconcile (1.3.3, run by 2.8's settler) settles stale pending attempts. `Proxy` and `Compare`
  keep the Python path unchanged.
- Web settler — PD ruling (Adversary F5): `spawn_web_terminal` (web_spawn.py:69-278) is a
  second Python settler: it calls `create_pending`, `record_process`, `fail_pending`,
  `fail_pending_attempt`, `settle_promotion`, and `set_dims` around its runtime calls. In
  `Native` it takes one branch that drives `RustNativeRuntimeClient`, which creates, promotes,
  or fails the row in the 1.3.5 settlement transaction and returns the settled terminal id or a
  typed failure; the branch writes no Python terminal row. `Proxy` and `Compare` keep the
  current body.
- Pane binding — PD ruling: pane binding is workspace layout state. `WorkspaceOps._fill`
  already binds with `set_pane_terminal` only after `spawn_web_terminal` returns a terminal id
  and rolls the pane back on failure (`workspace_ops.py:669-739`). In `Native` that id is the
  one Rust settled, and a Rust settlement failure binds no pane.
- Terminal services (found gap): in `Native`, `init_terminal_wiring` sets
  `runner.terminal_services` to None, so `AgentLifecycleMonitor.__init__`
  (lifecycle_monitor.py:110-117) builds the `build_terminal_services` fallback
  (lifecycle_monitor_terminals.py:13-28): a Python `WriteCoordinator` and
  `TerminalLeaseRegistry` over an empty runtime registry. This section sets
  `runner.terminal_services = TerminalServices(TerminalManager(db), <Native runtime registry>,
  coordinator=<write client>)` in `Native` and sets the agent runner's terminal fields from the
  same objects, so the monitor builds no fallback.
- Workspace event registry (found gap, 2026-10-16): in `Native`, `init_terminal_wiring` sets
  `runner.lease_registry = None` (terminal_wiring.py:70), and `init_servers` hands the websocket
  server that value (servers.py:190 and 330). The workspace family stays Python, so once 2.13
  lifts the gate:
  - `WebSocketServer.start` calls `self.lease_registry.start_lifecycle_publication()`
    (server.py:709), and its stop paths await `shutdown_lifecycle_publication()` (723 and 783),
    so the Python websocket server cannot start.
  - Shutdown awaits `websocket_server.lease_registry.finalize_shutdown_attachments()`
    (runner_lifecycle_shutdown.py:455).
  - `workspace_ws.py` reads `_leases().lifecycle_snapshot()` for its attach key and snapshot
    watermark (248, 290, 307), and `broadcast.py:449` publishes each `workspace_event` through
    `lease_registry.publish_lifecycle`. gclient applies a workspace event only when its
    `daemon_epoch` and `seq` follow the snapshot's watermark
    (`crates/gclient/src/app/workspace_ops.rs:49-51`), so the watermark and the events must come
    from one sequencer.
- Decision (restraint rung 2): in `Native`, `init_terminal_wiring` builds one
  `TerminalLeaseRegistry` as the workspace event sequencer and stores it as
  `runner.workspace_event_registry`. It claims no attachment and owns no terminal lease, because
  Rust owns every lease (A1). `init_servers` hands the websocket server `runner.lease_registry`
  when it is set and `runner.workspace_event_registry` otherwise. `services.lease_registry` stays
  `None`, so the guard at servers.py:366-375 leaves `setup_terminal_lifecycle_broadcasting`
  unwired and Python publishes no `terminal_lifecycle` message; Rust's registry stays the only
  terminal lifecycle sequencer (2.3, 2.14). The class is reused because `publish_lifecycle` and
  `lifecycle_snapshot` already implement the sequence, and 2.15 extends it with the Native viewport
  read.
  - Python's terminal WS handlers (`terminal_ws.py`, `terminal_ws_control.py`,
    `terminal_ws_write.py`, `terminal_sizing.py`, `proxy_relay.py`) and `routes/terminals.py` now
    find a registry, but none is reachable in `Native`: 2.13 claims every terminal WS message type
    and `/api/terminals` for Rust (2.13.1, 2.13.2).
  - `orchestration.py:804` passes `runner.lease_registry.daemon_epoch` to `CommunicationsManager`,
    which falls back to a fresh uuid for `None` (responder.py:193), so it needs no change.
- **Granularity:** five acceptance items over two lifecycle owners, spawn settlement and pane
  binding, plus the terminal services they share and the workspace event registry the Python
  websocket server needs in `Native`. They close together because pane binding reads the terminal
  id Rust settlement returns, both resolve the runtime through the services this section wires,
  and without the registry `Native` cannot serve workspaces at all.

Consumers unchanged:
- `src/gobby/runner_init/orchestration.py` — no-edit-reason: It calls `init_terminal_wiring` through the unchanged signature, and `CommunicationsManager` falls back to a fresh uuid when `runner.lease_registry` is `None`.
- `src/gobby/servers/websocket/server.py` — no-edit-reason: Its start and stop calls run against the workspace event registry that `init_servers` hands it in `Native`.
- `src/gobby/runner_lifecycle_shutdown.py` — no-edit-reason: `finalize_shutdown_attachments` on the workspace event registry finds no attachment and returns.
- `src/gobby/servers/websocket/broadcast.py` — no-edit-reason: It publishes workspace events through the websocket server's registry, which is the workspace event registry in `Native`.
- `src/gobby/runner_broadcasting.py` — no-edit-reason: `setup_terminal_lifecycle_broadcasting` stays unwired in `Native` because `services.lease_registry` is `None`.
- `src/gobby/runner.py` — no-edit-reason: It calls `init_servers` through the unchanged signature.
- `src/gobby/runner_init/__init__.py` — no-edit-reason: It re-exports `init_servers` by name.
- `tests/config/test_restart_config_consumers.py` — no-edit-reason: It runs outside `Native`, where `init_servers` hands the websocket server `runner.lease_registry` as before.
- `tests/runner_init/test_config_runtime_startup.py` — no-edit-reason: Same reason as `test_restart_config_consumers.py`.
- `tests/test_runner_lifecycle.py` — no-edit-reason: Same reason as `test_restart_config_consumers.py`.
- `tests/agents/test_spawn_native_only.py` — no-edit-reason: It runs outside `Native` route mode, where `resolve_terminal_services` is unchanged.
- `tests/mcp_proxy/tools/spawn_agent/test_placement.py` — no-edit-reason: It runs outside `Native` route mode, where `_runtime_spawn` is unchanged.
- `tests/servers/test_terminal_ws_attach_honesty.py` — no-edit-reason: It runs outside `Native` route mode, where `_runtime_spawn` is unchanged.
- `src/gobby/agents/lifecycle_monitor.py` — no-edit-reason: `reap_stale_pending` delegates to the reconciliation method with an unchanged signature and return type, and with `runner.terminal_services` set its fallback is not built.
- `tests/agents/test_lifecycle_monitor.py` — no-edit-reason: It runs outside `Native`, where the reap path is unchanged.
- `tests/agents/test_lifecycle_reconciliation.py` — no-edit-reason: It runs outside `Native`, where the reap path is unchanged.
- `tests/agents/test_spawn_executor_placement_bind.py` — no-edit-reason: It runs outside `Native`, where the reap path is unchanged.
- `tests/terminals/test_in_doubt_kill_truth.py` — no-edit-reason: It runs outside `Native`, where the reap path is unchanged.
- `tests/terminals/test_tmux_runtime.py` — no-edit-reason: It runs outside `Native`, where `spawn_web_terminal` keeps its current body.
- `tests/terminals/test_web_spawn.py` — no-edit-reason: It runs outside `Native`, where `spawn_web_terminal` keeps its current body.

**Acceptance:**

- 2.10.1 - In `Native`, constructing `TerminalHostManager` is refused, and agent spawn,
  web-workspace spawn, snapshot reads, and termination run through `RustNativeRuntimeClient`;
  tmux rows keep the spawn-less `TmuxTerminalRuntime`. test:
  `tests/terminals/test_runtime_client.py::test_native_consumers_use_the_rust_runtime_client`.
- 2.10.2 - In `Native`, agent, retry, placed, and web/workspace spawns and a stale-pending reap
  make zero Python terminal-row writes and the Rust repository settles each row; `Proxy` keeps
  the Python settlement path unchanged. test:
  `tests/terminals/test_runtime_client.py::test_native_settlement_has_one_rust_owner`.
- 2.10.3 - In `Native`, a workspace pane binds only after Rust returns the settled terminal id,
  with no Python terminal-row write; a Rust settlement failure binds no pane. test:
  `tests/terminals/test_workspace_ops.py::test_native_pane_binds_after_rust_settlement`. test:
  `tests/terminals/test_workspace_ops.py::test_native_settlement_failure_binds_no_pane`.
- 2.10.4 - In `Native`, `runner.terminal_services` carries the Rust runtime client and the write
  client, `AgentLifecycleMonitor` builds no fallback coordinator, and a committed spawn records an
  empty composer for its terminal. test:
  `tests/terminals/test_runtime_client.py::test_native_terminal_services_use_the_rust_clients`.
- 2.10.5 - In `Native`, the Python websocket server starts and stops on the workspace event
  registry; a workspace snapshot carries that registry's watermark; a workspace mutation
  publishes a `workspace_event` above it in the same `daemon_epoch`; shutdown finalizes no
  attachment; and Python publishes no `terminal_lifecycle` message. test:
  `tests/terminals/test_runtime_client.py::test_native_websocket_server_sequences_workspace_events_only`.

### 2.14 Terminal inventory reads and HTTP handlers [category: code] (depends: 2.1, 2.10, 1.10)

`kind: deliverable`

Targets:
- `crates/gterminals/src/inventory.rs`
- `crates/gterminals/src/repository.rs::*` — scope-reason: add list_page, the stable (created_at, id) inventory page query
- `crates/gterminals/src/leases.rs::*` — scope-reason: move the shutdown finalizers into the new shutdown child module and add Registry::lifecycle_snapshot
- `crates/gterminals/src/leases/shutdown.rs`
- `crates/gterminals/src/lib.rs::*` — scope-reason: declare the inventory module
- `crates/gdaemon/src/front_door/terminal_api.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `crates/gterminals/tests/inventory.rs`
- `crates/gdaemon/tests/front_door.rs::*` — scope-reason: pin the terminal inventory handlers' statuses

Research context:

- Need (found while converting D2, 2026-10-16): in `Native`, 2.13 claims `GET /api/terminals` and
  `GET /api/terminals/{id}` for Rust (2.13.1: "`Native` serves Rust"), and 2.2.1's
  `terminal.list` pages the same rows. gclient reads `attach`, `command`, and `cwd` from these
  rows (`crates/gclient/src/app/live.rs:455-481`; the request is built at
  `crates/gclient/src/daemon/mod.rs:624`). Left to Python in `Native`, the routes would read an
  empty fallback `TerminalLeaseRegistry` (`routes/terminals.py::_lease_registry`, 57-64) and the
  host manager that 2.10.1 refuses. This section builds those reads and handlers; 2.13 registers
  them, and 2.2 reuses the page query, the watermark, and the attach locator.
- Oracle: `src/gobby/servers/routes/terminals.py`: `list_terminals` (66-100), `_serve_page`
  (102-139), `get_terminal` (141-159), `_parse_states` (164-170), `_tmux_panes` (189-222),
  `_attach` (225-235), `_row_json` (243-256); `src/gobby/storage/terminals.py`:
  `TerminalManager.list_page` (550-596), `attach_locator_for_terminal` (294-323);
  `src/gobby/terminals/foreground.py`: `_process_snapshot` (31-62), `command_name` (65-71),
  `foreground_commands` (74-98), `shell_cwds` (101-119), `shell_pid` (122-135), `process_shell`
  (138-147); `src/gobby/terminals/leases.py::TerminalLeaseRegistry.lifecycle_snapshot` (219-224).
- Reuse (restraint rung 2): `ws_protocol.rs` (#23216) already has `parse_list_cursor`,
  `list_cursor`, `inventory_item`, `Page` with its `snapshot` field and `encode_within`, and
  `TERMINAL_LIST_DEFAULT_PAGE_SIZE`/`TERMINAL_LIST_MAX_PAGE_SIZE` (100/500). Holder info comes from
  `leases::Registry::snapshot` (leases.rs:710). gterminals has no HTTP dependency, so the two
  handlers live in gdaemon's `front_door/terminal_api.rs` beside 2.6's and 2.9's routes and stay
  unregistered until 2.13.
- `repository::list_page` ports `TerminalManager.list_page`: optional project ids, machine, states,
  and backend filters, the `(created_at, id) >` cursor, `ORDER BY created_at, id`, and `LIMIT n+1`
  for `has_more`.
- `Registry::lifecycle_snapshot` returns `daemon_epoch` and `seq` read from `RegistryState.sequence`
  under the state lock, where each event's sequence is committed with its publication permit
  (`Registry::reserve` and `Registry::event`, leases.rs:394-426). The first page carries it and a
  cursor page carries none, as `list_terminals` (82-86) does.
- Split: `crates/gterminals/src/leases.rs` is 861 lines. This section moves
  `Registry::finalize_shutdown` and `Registry::finalize_locked` (leases.rs:624-709) unchanged into
  the new `crates/gterminals/src/leases/shutdown.rs`, declared with `mod shutdown;` as a child
  module that keeps access to the registry's private state, before adding
  `Registry::lifecycle_snapshot`.
- `inventory.rs` holds the row and probe logic:
  - `attach_locator` ports `attach_locator_for_terminal`: none outside `pending` and `live`; a
    native row gets the native locator at the supervisor's current host epoch; a tmux row gets
    `socket_path`, `pane_id`, and integer `server_pid`/`server_start_time` from its locator; both
    name the host frames socket under the socket dir.
  - Commands: one `ps -A -o pid=,tpgid=,comm=` over the whole table (one stale pid must not blank
    the page) under the 2 s budget (`PS_TIMEOUT_SECONDS`), then each shell's foreground group
    leader's command name, as `foreground_commands` does.
  - Working directories: the workspace forbids `unsafe_code` (root `Cargo.toml`), so the
    `proc_pidinfo` call psutil makes on macOS is unavailable. Linux reads `/proc/<pid>/cwd` with
    `std::fs::read_link`; macOS runs one `lsof -a -d cwd -Fn -p <pid,...>` for the page's shell
    pids under the same 2 s budget; other platforms report none. A missing or unreadable shell is
    absent, as in `shell_cwds`.
  - tmux rows: one `tmux -S <socket> list-panes -a -F <format>` per distinct socket under the 2 s
    budget (`TMUX_PANE_PROBE_SECONDS`), keyed as `tmux_locator_key` keys them, supplies command
    and cwd. A failed or slow server contributes nothing and never fails the page.
  - Each row is `inventory_item` with the lease holder, plus `id`, `created_at`, `attach`,
    `command` (falling back to `process_shell`), and `cwd`, as `_row_json` builds it.
- Handlers: `list_terminals` takes `project_id` (required), `states` (default `pending,live`),
  `backend`, `cursor`, and `limit` (clamped to 1..=500, default 100); an invalid cursor answers
  400, and a page over the byte ceiling answers 413 `terminal_page_too_large`. `get_terminal`
  answers 400 for an invalid id and 404 for a missing row or another machine's row.
- `lib.rs` declares `pub mod inventory`. `crates/gdaemon/Cargo.toml`, `Cargo.lock`, and
  `version_pins.py` carry the gdaemon 0.0.1 patch bump.
- **Granularity:** six acceptance items over one read path, terminal inventory: the page query,
  the row and its probes, the watermark, the split that makes room for it, and the two handlers
  that serve them. They close together because the handlers are the only consumer of the row
  builder and both rest on the page query.

Consumers unchanged:
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: It reads `MANAGED_BIN_VERSION_PINS` by binary name, and the bump changes only the `gdaemon` value.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: It iterates `MANAGED_BIN_VERSION_PINS` as version floors, and the bump changes only the `gdaemon` value.
- `src/gobby/install/distribution.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: It reads or patches `MANAGED_BIN_VERSION_PINS` by binary name and hard-codes no gdaemon version.
- `tests/cli/test_install_ghook.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/install/test_distribution.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/install/test_version_pins.py` — no-edit-reason: It checks each pin against its crate's `Cargo.toml` version, and the bump changes both together.

**Acceptance:**

- 2.14.1 - `repository::list_page` returns the same rows, order, and `has_more` as
  `TerminalManager.list_page` for project, machine, state, and backend filters and the
  `(created_at, id)` cursor. test:
  `crates/gterminals/tests/inventory.rs::list_page_matches_the_python_query`.
- 2.14.2 - Each native and tmux row equals Python's `_row_json` for the same row: the inventory
  item with its lease holder, `id`, `created_at`, the attach locator (none outside `pending` and
  `live`), and `command` and `cwd` from the probes, with `command` falling back to the process
  shell. test: `crates/gterminals/tests/inventory.rs::inventory_rows_match_python_row_json`.
- 2.14.3 - The `ps`, cwd, and tmux probes each run under the 2 s budget; a failed or slow probe
  leaves `command` and `cwd` absent for the affected rows without failing the page, and a stale
  shell pid does not blank other rows. test:
  `crates/gterminals/tests/inventory.rs::probes_are_bounded_and_never_fail_the_page`.
- 2.14.4 - The first page carries `Registry::lifecycle_snapshot` (`daemon_epoch` and the last
  committed `seq`), a cursor page carries none, and a page over the byte ceiling refuses. test:
  `crates/gterminals/tests/inventory.rs::first_page_carries_the_lifecycle_watermark`.
- 2.14.5 - The moved shutdown finalizers keep the 1.10 shutdown tests passing unchanged. test:
  `crates/gterminals/tests/leases.rs::shutdown_preserves_only_direct_native_input_grant`.
- 2.14.6 - The handlers answer with Python's statuses: 400 for an invalid cursor or id, 404 for a
  missing or other-machine row, 413 for an over-ceiling page, and `limit` clamps to 1..=500 with
  default 100. test:
  `crates/gdaemon/tests/front_door.rs::terminal_inventory_handlers_match_python_statuses`.

### 2.15 Native lane viewports from the Rust lease registry [category: code] (depends: 2.9, 2.10, 2.14)

`kind: deliverable`

Targets:
- `crates/gterminals/src/leases.rs::*` — scope-reason: declare the viewports child module that holds Registry::viewports
- `crates/gterminals/src/leases/viewports.rs`
- `crates/gterminals/src/runtime_api.rs`
- `crates/gterminals/src/family.rs::*` — scope-reason: hand the Native lease registry to the runtime route
- `src/gobby/terminals/runtime_client.py`
- `src/gobby/runner_init/terminal_wiring.py::init_terminal_wiring`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `crates/gterminals/tests/runtime_route.rs`
- `tests/terminals/test_runtime_client.py`

Research context:

- Need (found gap, 2026-10-16): team-lane launches size the lane from the gclient window showing
  its workspace. `check_runbook_seats_tool` reads the registry from `ctx.lease_registry_resolver`
  and passes `leases.viewports` to `resolve_team_launch_inputs`
  (`runbook_seat_tools.py:77-89`). The resolver returns `services.websocket_server.lease_registry`
  (`http.py:368-370`), which in `Native` is 2.10's workspace event registry. That registry claims
  no attachment, because Rust owns every lease (A1) and 2.2 attaches gclient windows in Rust, so
  its `viewports` is always empty. `team_launch_inputs._lane_viewport` (162-186) needs exactly one
  size per shown pane and raises `_viewport_refusal` otherwise (156-160), so once 2.13 activates
  `Native` every lane launch without explicit `lane_rows` and `lane_columns` refuses.
- Oracle: `src/gobby/terminals/leases.py::TerminalLeaseRegistry.viewports` (595-607) returns the
  distinct `(rows, cols)` of records for the terminal that are not finalized, whose viewer is
  `"gclient"`, and whose viewport is set. Its docstring records that sync tools call it from a
  worker thread.
- Rust state: `leases.rs` `Record` carries `viewer` (`Viewer::Gclient`), `finalized`, and
  `view.viewport`, which `Registry::set_viewport` (747-759) stores as `(rows, cols)`.
  `Registry::viewports(terminal) -> BTreeSet<(u16, u16)>` reads them under the state lock with no
  health check, as `Registry::snapshot` (710-718) does.
- Split: `crates/gterminals/src/leases.rs` is 861 lines. `Registry::viewports` lives in the new
  `crates/gterminals/src/leases/viewports.rs`, declared with `mod viewports;` as a child module
  beside 2.14's `leases/shutdown.rs`, so it keeps access to the registry's private state and
  `leases.rs` grows by one line.
- Route: `runtime_api.rs` (2.9) gains a `viewports` operation that takes `terminal_id` and replies
  with the distinct `[rows, cols]` pairs; an invalid id is refused like 2.9's other operations.
  The route reaches the lease registry because `family.rs` hands it the same
  `Arc<leases::Registry>` that 2.6 builds for the coordinator. No new HTTP path is added, so
  `terminal_api.rs` is unchanged.
- Python: `runtime_client.py` (2.10) gains async `RustNativeRuntimeClient.viewports(terminal_id)`
  and `NativeWorkspaceEventRegistry(TerminalLeaseRegistry)`, which overrides only
  `viewports(terminal_id)` with
  `asyncio.run_coroutine_threadsafe(client.viewports(terminal_id), loop).result(timeout=5.0)`.
  `init_terminal_wiring` builds it in `Native` as `runner.workspace_event_registry`, with the loop
  it already captures for `TerminalEffectBridge` (terminal_wiring.py:147-150), so 2.10's
  workspace event sequencing is unchanged.
- Failure (fail closed): a route refusal, transport failure, or the 5 s timeout cancels the future
  and raises `ValueError` from the cause; so does a call with no captured loop or on the loop's own
  thread, which would otherwise stall the daemon loop. `check_runbook_seats_tool` already maps
  `ValueError` to an unsuccessful result (`runbook_seat_tools.py:107-108`), so a failed read
  resolves no launch inputs and never invents a size.
- Decision (restraint rung 2): the subclass mirrors 2.7's Orchestrator ruling that the write
  client subclasses `WriteCoordinator`, keeping the Python surface its consumers already call, so
  `http.py`, the MCP registries, the seat tool, and `_lane_viewport` stay unchanged. Rejected: a new HTTP viewport route, which would duplicate the
  runtime route's gate; and having Python track gclient viewports itself, which would make a
  second lease owner beside Rust.
- `crates/gdaemon/Cargo.toml`, `Cargo.lock`, and `version_pins.py` carry the gdaemon 0.0.1 patch
  bump.
- **Granularity:** three acceptance items over one read path, the `Native` lane viewport. Its
  Target files are the Rust read, the route operation, the family wiring that reaches the
  registry, the Python client and registry subclass, the wiring that installs it, and the version
  bump; they close together because each half is unobservable without the other.

Consumers unchanged:
- `src/gobby/servers/http.py` — no-edit-reason: Its `lease_registry_resolver` returns the websocket server's registry, which is `NativeWorkspaceEventRegistry` in `Native`.
- `src/gobby/mcp_proxy/registries.py` — no-edit-reason: It forwards `lease_registry_resolver` unchanged.
- `src/gobby/mcp_proxy/tools/agents_context.py` — no-edit-reason: It types the resolver as returning `TerminalLeaseRegistry`, which the subclass satisfies.
- `src/gobby/mcp_proxy/tools/agents_registry.py` — no-edit-reason: Same reason as `registries.py`.
- `src/gobby/mcp_proxy/tools/runbook_seat_tools.py` — no-edit-reason: It passes `leases.viewports` and already maps `ValueError` to an unsuccessful result.
- `src/gobby/agents/team_launch_inputs.py` — no-edit-reason: `_lane_viewport` calls the `Viewports` callable per shown pane and refuses zero or ambiguous sizes unchanged.
- `src/gobby/runner_init/orchestration.py` — no-edit-reason: It calls `init_terminal_wiring` through the unchanged signature.
- `tests/workflows/test_pipeline_input_types.py` — no-edit-reason: It patches the resolver and asserts the tool passes the registry's bound `viewports`, which the subclass keeps.
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: It reads `MANAGED_BIN_VERSION_PINS` by binary name, and the bump changes only the `gdaemon` value.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: It iterates `MANAGED_BIN_VERSION_PINS` as version floors, and the bump changes only the `gdaemon` value.
- `src/gobby/install/distribution.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: It reads or patches `MANAGED_BIN_VERSION_PINS` by binary name and hard-codes no gdaemon version.
- `tests/cli/test_install_ghook.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/install/test_distribution.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/install/test_version_pins.py` — no-edit-reason: It checks each pin against its crate's `Cargo.toml` version, and the bump changes both together.

**Acceptance:**

- 2.15.1 - `Registry::viewports` and the runtime route's `viewports` operation return the distinct
  `(rows, cols)` of the terminal's unfinalized gclient records with a viewport, and exclude web
  viewers, finalized records, records without a viewport, and other terminals' records, matching
  `TerminalLeaseRegistry.viewports`. test:
  `crates/gterminals/tests/runtime_route.rs::viewports_match_the_python_lease_predicate`.
- 2.15.2 - In `Native`, `check_runbook_seats_tool` sizes a lane from the Rust-held gclient
  viewport of each shown pane through `NativeWorkspaceEventRegistry`, and zero or two distinct
  viewports refuse with `_viewport_refusal`. test:
  `tests/terminals/test_runtime_client.py::test_native_lane_viewport_reads_rust_gclient_viewports`.
- 2.15.3 - In `Native`, a route refusal, transport failure, timeout, missing loop, or call on the
  loop's own thread raises `ValueError` from `viewports`, and the seat tool returns an unsuccessful
  result with no resolved launch inputs. test:
  `tests/terminals/test_runtime_client.py::test_native_lane_viewport_read_fails_closed`.

### 2.2 Native terminal WS connection and operation state machine [category: code] (depends: 2.1, 2.10, 1.10, 2.14, 2.15)

`kind: deliverable`

Targets:
- `crates/gterminals/src/ws.rs`
- `crates/gterminals/src/lib.rs::*` — scope-reason: declare the ws module
- `crates/gterminals/src/family.rs::*` — scope-reason: own the WS connection state in the terminal family
- `crates/gterminals/src/control.rs::*` — scope-reason: add ControlClient::grant_input
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `crates/gterminals/tests/ws_operations.rs`

Research context:

- Ordering: 2.2 is blocked by #21558 (Cross-plan edges set at expansion), which supplies gdaemon's
  native WS accept, auth, subscription, and broadcast transport (A2). It depends on 1.10 because
  activating `Native` attach with 1.4's revoke-on-finalize policy alone would break 1.2.5's
  restart I/O invariant; 1.10's grant split and durable handoff record (landed by #23218) close
  that gap, and 2.2.7 uses its `clear_input_handoff`. It depends on 2.10 for the Native write,
  settlement, and runtime seams the operations call, and on 2.14 for `repository::list_page`,
  `Registry::lifecycle_snapshot`, and `inventory::attach_locator`, which `terminal.list` and
  attach reuse (oracle `terminal_ws.py::_handle_terminal_list`, first-page watermark at 344-348).
  It follows 2.15 because both edit `family.rs` and bump gdaemon in the same `Cargo.toml`,
  `Cargo.lock`, and `version_pins.py`; 2.2's `terminal_set_viewport` writes the viewports 2.15
  reads.
- `TerminalWsMixin` is the operation oracle for list, attach, detach, sizing, scroll, input,
  paste, operator write, control, lease-lost fanout, proxy attach, and `terminal_set_theme`.
  #21558 supplies connection
  auth/subscription/broadcast and the outer envelope.
- Runtime resolution remains per terminal row. A terminal id is backend-neutral; native rows use
  the native host runtime. tmux rows exist only for hand-started panes (#22856): their input,
  write, and resize go through 2.11's `tmux_write.rs`; attach computes the locator from the row and
  the host socket as `TmuxTerminalRuntime.attach_locator` does; and history and frames come from
  the gterm host's frames socket, keyed by the pane id.
- `terminal_set_theme` — PD ruling (Adversary F4): the oracle is
  `terminal_ws.py::_handle_terminal_set_theme` and `_declare_holder_theme`. gclient
  `app/theme_sync.rs` already sends it for proxied panes.
- **Choice — restraint rung 2:** implement these messages as one #21558 WS route handler backed by
  the M1 repository/lease/write APIs. Do not introduce a terminal-only listener or socket.
- Input-grant applier — LOW-2, carried from #23219 (Orchestrator, 2026-10-15):
  `leases.rs::HostInput` (leases.rs:28-38: `Unchanged`, `Grant`, `Revoke`, `Preserved`) has no
  production applier, and `ControlClient` (control.rs) has no `grant_input`. `ws.rs` is the
  applier. On `Grant(attachment)` it calls the new `ControlClient::grant_input`, which emits the
  bytes of the read-only fixture `crates/gterminal/tests/fixtures/wire_golden/control_grant_input.json`,
  and then `leases::Registry::confirm_input_grant` (leases.rs:590). On `Revoke` it calls 2.8's
  `ControlClient::revoke_input`. After an ordinary grant or revoke succeeds, it calls
  `repository::clear_input_handoff(tx, terminal, None)` (repository.rs:247), so a record that a
  preserved shutdown grant left never outlives a later ordinary grant or revoke. A failed grant
  or revoke leaves the record for 2.8's expiry pass. The oracle is
  `input_grants.py::sync_host_input_grant` (53). The applier's doc comment in `ws.rs` states this
  duty; `leases.rs` needs no edit.
- `lib.rs` declares `pub mod ws`. `crates/gdaemon/Cargo.toml`, `Cargo.lock`, and `version_pins.py`
  carry the gdaemon 0.0.1 patch bump.
- **Granularity:** seven acceptance items over one connection state machine: list, attach, detach,
  mutating operations, the fault path, theme, and the input-grant applier. They close together
  because every operation runs inside the one connection state that owns its attachments, and the
  applier runs on that state's attach, take, and release transitions.

Implement one connection state owning its attachments and relay. Authorize project access before
lookup, validate each message before mutation, reserve lifecycle capacity before changing a
holder, and finalize all connection attachments exactly once on disconnect or send failure.

Consumers unchanged:
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: It reads `MANAGED_BIN_VERSION_PINS` by binary name, and the bump changes only the `gdaemon` value.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: It iterates `MANAGED_BIN_VERSION_PINS` as version floors, and the bump changes only the `gdaemon` value.
- `src/gobby/install/distribution.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: It reads or patches `MANAGED_BIN_VERSION_PINS` by binary name and hard-codes no gdaemon version.
- `tests/cli/test_install_ghook.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/install/test_distribution.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/install/test_version_pins.py` — no-edit-reason: It checks each pin against its crate's `Cargo.toml` version, and the bump changes both together.

**Acceptance:**

- 2.2.1 - Authenticated `terminal.list` enforces project/machine/state/page filters, stable cursor,
  max page bytes, and backend on every item; unauthorized rows are not distinguishable from
  absent rows. test: `crates/gterminals/tests/ws_operations.rs::list_is_scoped_bounded_and_backend_visible`.
- 2.2.2 - Attach waits for the settled host decision, validates the row's live epoch and locator,
  creates one lease, applies sizing, emits history/live frames, and returns the canonical attach
  envelope for native and tmux. test:
  `crates/gterminals/tests/ws_operations.rs::attach_is_epoch_and_backend_neutral`.
- 2.2.3 - Detach/socket close finalizes each attachment once, re-elects holder/sizing owner, and
  emits ordered lifecycle events without leaking frame reservations. test:
  `crates/gterminals/tests/ws_operations.rs::disconnect_finalizes_all_attachment_state_once`.
- 2.2.4 - Input, paste, scroll, resize, take/release control, and operator write enforce holder,
  generation, byte/sequence limits, and post-lock revalidation before side effects. test:
  `crates/gterminals/tests/ws_operations.rs::mutating_operations_revalidate_before_effect`.
- 2.2.5 - A write fault closes/faults the affected connection through one canonical error path,
  preserves unresolved evidence, and never retries the write on another backend. test:
  `crates/gterminals/tests/ws_operations.rs::write_fault_is_fail_closed`.
- 2.2.6 - `terminal_set_theme` checks socket ownership, native capability, and codec; stores the
  attachment theme; relays only when the sender holds input or no holder exists; replays the
  stored theme on take; refuses an observer; and sends a `terminal_error` without
  `attachment_id` so a pending control reply is untouched. No gclient or wire edit. test:
  `crates/gterminals/tests/ws_operations.rs::set_theme_is_holder_scoped_and_isolated_from_pending_control`.
- 2.2.7 - A `Grant` sends the `control_grant_input.json` bytes and then confirms the grant; after
  an ordinary grant or revoke succeeds, the terminal's input handoff record is cleared with
  `clear_input_handoff(.., None)`, and a failed grant or revoke leaves the record. test:
  `crates/gterminals/tests/ws_operations.rs::ordinary_grant_or_revoke_clears_the_handoff_record`.

### 2.3 Bounded proxy relay and frame/lifecycle fanout [category: code] (depends: 2.2)

`kind: deliverable`

Targets:
- `crates/gterminals/src/relay.rs`
- `crates/gterminals/src/lib.rs::*` — scope-reason: declare the relay module
- `crates/gterminals/src/ws.rs`
- `crates/gterminals/src/family.rs::*` — scope-reason: hand the lease registry's lifecycle receiver to the relay's publisher
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `crates/gterminals/tests/proxy_relay.rs`

Research context:

- Wiring: 2.2's connection state writes frames straight to its socket. 2.3 puts the relay between
  them: `ws.rs` starts one relay task per connection and one pump per attachment, and sends every
  frame and lifecycle event through that relay. The family keeps the lifecycle `mpsc::Receiver`
  that `leases::Registry::new` (leases.rs:337) returns (2.6). That bounded queue stays the only
  lifecycle fanout source: its single consumer hands each event to the subscribed connections'
  reserved lifecycle lanes, as the Python registry's `publish_lifecycle` hands each event to the
  WS broadcaster (`runner_broadcasting.py::observe`). No broker or bus is added.
- `lib.rs` declares `pub mod relay`. `crates/gdaemon/Cargo.toml`, `Cargo.lock`, and
  `version_pins.py` carry the gdaemon 0.0.1 patch bump.

- Port `SocketRelay`, `ProxyHub`, and `_map_host_frame` from
  `src/gobby/servers/websocket/proxy_relay.py`: bounded per-socket frame queue, reserved lifecycle
  lane, timeouts, native history before live pump, one attachment finalizer, and backend-neutral
  event mapping.
- **Choice — restraint rung 6:** one relay task per WS connection and one pump per attachment are
  the minimum ownership units. Do not add a broker, global fanout bus, or persisted frame queue.
- **Failure-boundary check (`edge-case-coverage`):** reserve/attach/pump creation and finalization
  have one owner; any failure after reservation invokes the same finalizer before returning.

Implement strict byte and entry accounting. Frame overflow/timeout drops that socket through the
canonical reason; the reserved lifecycle lane remains deliverable. History is emitted before the
live pump can enqueue output, and finalization is idempotent under simultaneous host/socket close.

Consumers unchanged:
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: It reads `MANAGED_BIN_VERSION_PINS` by binary name, and the bump changes only the `gdaemon` value.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: It iterates `MANAGED_BIN_VERSION_PINS` as version floors, and the bump changes only the `gdaemon` value.
- `src/gobby/install/distribution.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: It reads or patches `MANAGED_BIN_VERSION_PINS` by binary name and hard-codes no gdaemon version.
- `tests/cli/test_install_ghook.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/install/test_distribution.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/install/test_version_pins.py` — no-edit-reason: It checks each pin against its crate's `Cargo.toml` version, and the bump changes both together.

**Acceptance:**

- 2.3.1 - Native history is emitted completely before live frames for an attachment, and tmux
  history/frame mapping produces the same backend-neutral envelopes. test:
  `crates/gterminals/tests/proxy_relay.rs::history_precedes_live_frames_for_each_backend`.
- 2.3.2 - Frame entry/byte saturation and send timeout close only the affected socket with the
  canonical reason, while lifecycle reserve still emits holder loss/finalization. test:
  `crates/gterminals/tests/proxy_relay.rs::frame_backpressure_preserves_lifecycle_lane`.
- 2.3.3 - Host EOF, socket EOF, explicit detach, pump error, and concurrent close all release the
  frame reservation and lease exactly once. test:
  `crates/gterminals/tests/proxy_relay.rs::every_close_race_finalizes_exactly_once`.
- 2.3.4 - Host frames with stale epoch, unknown attachment, invalid sequence, or oversize payload
  are rejected before fanout and cannot affect a new attachment reusing a terminal id. test:
  `crates/gterminals/tests/proxy_relay.rs::stale_or_invalid_host_frames_never_cross_attachment_generation`.

### 2.12 WS operator input under the route action hold [category: code] (depends: 2.2, 2.3)

`kind: deliverable`

Targets:
- `crates/gterminals/src/ws.rs`
- `crates/gterminals/src/write_api.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `crates/gterminals/tests/family_cutover.rs`
- `tests/terminals/test_coordinator_client.py`

Research context:

- Rust WS operator input joins 2.4's route-hold queue under 2.4's tokens, uncertainty, proof, and
  constants (PD ruling gobby#14972, 2026-10-01, Adversary F2). `write_api.rs` admits a WS operator
  input through the same hold admission 2.6 uses for route calls, so a Python-held action
  (`acquire_action` over 2.6's route) and WS operator input share one per-terminal queue; `ws.rs`
  sends input, paste, and operator writes through that admission instead of straight to the
  coordinator.
- Cancellation: before dispatch, nothing is emitted. After dispatch, no queued WS frame is admitted
  until the host effect completes or 2.4's proof arrives (pane or terminal destroyed, host epoch
  changed, host process exited, or an operator close or kill).
- Python paths under test: `submit_coordinated_text`, the held-submit retry, and
  `_attempt_idle_reprompt` drive 2.7's `RustWriteCoordinatorClient` with an action hold;
  `tests/terminals/test_coordinator_client.py` (created by 2.7) runs them against concurrent Rust
  operator input.
- Ordering: 2.12 follows 2.2 (it edits `ws.rs`) and 2.3 (every WS frame passes the relay).
  `crates/gdaemon/Cargo.toml`, `Cargo.lock`, and `version_pins.py` carry the gdaemon 0.0.1 patch
  bump.

Consumers unchanged:
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: It reads `MANAGED_BIN_VERSION_PINS` by binary name, and the bump changes only the `gdaemon` value.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: It iterates `MANAGED_BIN_VERSION_PINS` as version floors, and the bump changes only the `gdaemon` value.
- `src/gobby/install/distribution.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: It reads or patches `MANAGED_BIN_VERSION_PINS` by binary name and hard-codes no gdaemon version.
- `tests/cli/test_install_ghook.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/install/test_distribution.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/install/test_version_pins.py` — no-edit-reason: It checks each pin against its crate's `Cargo.toml` version, and the bump changes both together.

**Acceptance:**

- 2.12.1 - While a Python action hold is active, Rust WS operator input joins 2.4's queue in
  order and flushes in order on release; at the 2.4 bound or age limit the operator receives an
  explicit error frame and no queued frame is discarded. test:
  `crates/gterminals/tests/family_cutover.rs::operator_input_queues_behind_a_held_python_action`.
- 2.12.2 - Cancellation before dispatch emits nothing; cancellation after dispatch admits no
  queued WS frame until the host effect completes or 2.4's proof arrives. A lost acquire, step, or
  release reply, a child-token rotation, a lease loss, and TTL expiry during a pending host write
  never replay a non-idempotent uncertain write. test:
  `crates/gterminals/tests/family_cutover.rs::action_hold_release_is_effect_safe`.
- 2.12.3 - The real `submit_coordinated_text`, held-submit retry, and `_attempt_idle_reprompt`
  paths keep their steps contiguous against concurrent Rust operator input, cancellation, and
  TTL expiry. test:
  `tests/terminals/test_coordinator_client.py::test_real_submit_retry_and_reprompt_paths_hold_against_operator_input`.

### 2.13 Terminal family registration, Native activation, and end-to-end parity [category: code] (depends: 2.12)

`kind: deliverable`

Targets:
- `crates/gterminals/src/family.rs::*` — scope-reason: register terminal HTTP and WS dispatch as one family
- `crates/gdaemon/src/front_door/routes.rs::*` — scope-reason: give TerminalFamily its terminal HTTP and WS routes beside 2.6's write and 2.9's runtime routes
- `crates/gdaemon/src/websocket/transport.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `crates/gdaemon/tests/front_door.rs::*` — scope-reason: pin one-owner terminal family dispatch and Compare's no-duplicate-effect rule
- `tests/terminals/acceptance/test_native_lifecycle.py::*` — scope-reason: add gdaemon native-family restart, fail-closed, and attach-and-control acceptance
- `crates/gterminals/tests/host_lifecycle.rs::*` — scope-reason: restore the five real-gdaemon tests from base 17ec46c7

Research context:

- #21544 requires one family routing-table change. #21558 owns the listener and auth transport;
  this section registers terminal HTTP/WS routes and switches their family backend.
  `crates/gdaemon/src/front_door/routes.rs::TerminalFamily` (name `terminal_ws`, routes.rs:42-54)
  claims no prefix and serves an empty router today.
- HTTP half: `TerminalFamily` claims `/api/terminals`, and its router sends `GET /api/terminals`
  and `GET /api/terminals/{id}` to 2.14's handlers. Both must be claimed: under a `Native` family,
  an unclaimed method or path is proxied to Python
  (`routes.rs::unclaimed_methods_and_paths_under_a_native_family_are_proxied`), whose route would
  read an empty lease registry in `Native`.
- WS half: #21558's gated implementation (native-ws-transport.md, D1 targets) creates
  `crates/gdaemon/src/websocket/transport.rs`, the native application dispatch that forwards
  application frames to the Python bridge. In `Native`, 2.13 claims the terminal message types
  there for 2.2's handler ahead of the bridge; in `Proxy` and `Compare` they still reach Python.
  2.2 is blocked by #21558, so the dispatch exists before 2.13 starts.
- Tests: routing, proxying, and Compare live in gdaemon (`RouteTable`, `compare`), which
  gterminals tests cannot reach, so 2.13.1 and 2.13.2 run in `crates/gdaemon/tests/front_door.rs`.
- Activation gate: #23217's in-review `terminal_route_latch` (routes.rs, lane commit
  `073de0c9c0`) derives `WsRoutes::Registered` from a non-empty `TerminalFamily.prefixes()`, and
  `RouteLatch::new` refuses `Native` while the family is unregistered (2.4.17). Giving
  `TerminalFamily` its routes therefore lifts the gate in the same change, with no other switch.
  2.4.17's `native_activation_refuses_without_the_terminal_family` builds the latch directly and
  keeps passing.
- Live `Compare` dual execution is unsafe for input/control/write. The canonical corpus and a
  captured, non-mutating decode/encode comparison supply parity without duplicating effects.
- **Choice — restraint rung 2:** use the existing family route switch and Python proxy as the
  rollback. Do not add per-operation rollout flags. The supervisor owner changes only at process
  start, so a route change requires the existing announced restart/cutover procedure.
- Fail-closed run (2.13.4): 1.7 landed fail-closed spawn on typed host reasons under #22856
  (Orchestrator ruling 2026-10-14, option B), so the isolated `Native` run proves that contract
  through the Rust family: with the host unavailable, every lifetime fails with 1.7's typed code
  and leaves no terminal row, process, tmux session, or project message, and the next spawn after
  re-adoption runs on gterm. It replaces the bounded tmux fallback cases the 2026-10-01 draft
  listed.
- Restored tests — carried from #23219 (Orchestrator, 2026-10-15): 2.4 removes five opt-in
  real-gdaemon tests from `crates/gterminals/tests/host_lifecycle.rs`
  (`daemon_restart_preserves_live_terminal`, `explicit_parent_drain_restarts_with_fresh_epoch`,
  `spawned_identity_survives_health_and_stop_disarms_retry`,
  `explicit_drain_terminates_token_mismatch_host`, and
  `explicit_drain_terminates_foreign_epoch_host`). 2.13 restores them from base 17ec46c7. At that
  base, `isolated_daemon` (host_lifecycle.rs:728) reads `GOBBY_TERMINALS_TEST_GDAEMON` and
  `GOBBY_TERMINALS_TEST_GTERM` with `std::env::var(..)?` (730-731), so a plain run errors. 2.13 replaces
  each `?` with an explicit opt-in check: an unset variable prints which variable is missing and
  returns before any setup, and a set pair runs the test in full. There is no `#[ignore]`, no
  test-quality baseline entry, and no bypass variable. E1 runs them with both variables set.
- `crates/gdaemon/Cargo.toml`, `Cargo.lock`, and `version_pins.py` carry the gdaemon 0.0.1 patch
  bump.
- **Granularity:** seven acceptance items over one change: registering the terminal family. Its
  dispatch, Compare safety, restart and fail-closed runs, parity gate, activation smoke, and the
  restored real-gdaemon tests all prove the same registration, and none can pass before it lands.

Register `/api/terminals` and terminal WS messages as one terminal family. `Proxy` keeps Python
ownership. `Compare` proxies effects and compares only normalized non-mutating output.
Registering the family lifts 2.4's `Native` activation gate in the same change.

Consumers unchanged:
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: It reads `MANAGED_BIN_VERSION_PINS` by binary name, and the bump changes only the `gdaemon` value.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: It iterates `MANAGED_BIN_VERSION_PINS` as version floors, and the bump changes only the `gdaemon` value.
- `src/gobby/install/distribution.py` — no-edit-reason: Same reason as `install_setup_gclient.py`.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: It reads or patches `MANAGED_BIN_VERSION_PINS` by binary name and hard-codes no gdaemon version.
- `tests/cli/test_install_ghook.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/install/test_distribution.py` — no-edit-reason: Same reason as `test_grok_installer.py`.
- `tests/install/test_version_pins.py` — no-edit-reason: It checks each pin against its crate's `Cargo.toml` version, and the bump changes both together.

**Acceptance:**

- 2.13.1 - One terminal-family registration owns terminal HTTP and WS dispatch; `Proxy` delegates,
  `Native` serves both `/api/terminals` GETs and the terminal message types from Rust, and no
  request is split between owners. test:
  `crates/gdaemon/tests/front_door.rs::terminal_family_routes_as_one_unit`.
- 2.13.2 - `Compare` never executes input, write, resize, control, spawn, terminate, or drain
  twice; it compares only canonical serialization of non-mutating captured results. test:
  `crates/gdaemon/tests/front_door.rs::compare_mode_never_duplicates_terminal_effects`.
- 2.13.3 - An isolated native-mode run spawns an agent on gterm, preserves pid/epoch/terminal and
  I/O across ordinary gdaemon restart, and drains only with explicit `--terminals`. test:
  `tests/terminals/acceptance/test_native_lifecycle.py::test_gdaemon_native_family_restart_preserves_agent_terminal`.
- 2.13.4 - An isolated `Native` run with the gterm host unavailable fails run-scoped and
  persistent-role spawns with 1.7's typed code and creates no terminal row, process, tmux session,
  or project message; after the host is re-adopted, the next spawn runs on gterm without operator
  action. test:
  `tests/terminals/acceptance/test_native_lifecycle.py::test_native_family_host_unavailable_fails_closed_and_recovers`.
- 2.13.5 - Python and Rust golden replay plus end-to-end gclient attach/render/input all pass with
  no change to gterm, no production/runtime change under `crates/gclient/src/`, and no protocol or
  canonical fixture-byte change. The sole allowed gclient edit remains
  `crates/gclient/tests/client_loop.rs`. behavior: `terminal-family-cutover-parity-gate`.
- 2.13.6 - The change that registers the terminal family lifts 2.4's activation gate, and an
  isolated `Native` run attaches, takes control, writes, and releases through the Rust family
  end to end. test:
  `tests/terminals/acceptance/test_native_lifecycle.py::test_native_activation_attach_and_control_smoke`.
- 2.13.7 - The five tests 2.4 removed are restored from base 17ec46c7. With
  `GOBBY_TERMINALS_TEST_GDAEMON` and `GOBBY_TERMINALS_TEST_GTERM` set, each passes; with either
  unset, each names the missing variable and returns before setup instead of erroring. No
  `#[ignore]` or baseline entry is added.
  test: `crates/gterminals/tests/host_lifecycle.rs::daemon_restart_preserves_live_terminal`.
  test: `crates/gterminals/tests/host_lifecycle.rs::explicit_parent_drain_restarts_with_fresh_epoch`.
  test: `crates/gterminals/tests/host_lifecycle.rs::spawned_identity_survives_health_and_stop_disarms_retry`.
  test: `crates/gterminals/tests/host_lifecycle.rs::explicit_drain_terminates_token_mismatch_host`.
  test: `crates/gterminals/tests/host_lifecycle.rs::explicit_drain_terminates_foreign_epoch_host`.

## E1 Verification

`kind: verification`

Executors run focused verification after each leaf and the complete matrix before native cutover:

1. `cargo fmt --all -- --check` and the repository Rust lint/type gate for every changed crate.
2. `cargo nextest run -p gobby-terminals`, including the `tmux_write.rs` contract (2.11), the
   `inventory.rs` reads (2.14), and one `host_lifecycle.rs` run with `GOBBY_TERMINALS_TEST_GDAEMON`
   and `GOBBY_TERMINALS_TEST_GTERM` set to freshly built gdaemon and gterm binaries, so the five
   restored real-gdaemon tests execute (2.13.7); focused `-p gobby-daemon` integration tests for
   family composition, shutdown, and the `front_door.rs` route and handler tests (2.6, 2.9, 2.13,
   2.14).
3. With isolated `DATABASE_URL` and `GOBBY_TEST_PROTECT=1`, run the changed Python test files only:
   backend ingress/selection, spawn executor, agent definitions, terminal routes/WS, host shutdown,
   workspace storage and ops, the coordinator and runtime clients (including the `Native` lane
   viewport read, 2.15), and native lifecycle acceptance. Never run the full pytest suite.
4. Run `tests/servers/test_terminal_ws_golden.py`,
   `crates/gclient/tests/ws_golden.rs`, and `crates/gterminals/tests/ws_golden.rs` against the same
   unchanged `tests/fixtures/terminal_ws_golden/manifest.json`.
5. Run the M1 restart test before #21558 is available; it must exercise the Rust owner lifecycle
   directly and prove that M1 has no terminal-WS dependency.
6. After #21558, run the isolated native route cutover with real gdaemon/gterm/gclient and the
   fail-closed host-unavailable simulations (2.13.4): each typed host reason refuses the spawn
   with no row, process, or message, and the next spawn after re-adoption uses gterm. Capture host
   pid/epoch, terminal id/backend, typed host reason, message id, and pre/post-restart I/O as
   evidence.
7. Audit `git diff` for the implementation commits: no change under `crates/gterminal/`, no
   production/runtime change under `crates/gclient/src/`, no gclient edit except
   `crates/gclient/tests/client_loop.rs`, and no change under
   `tests/fixtures/terminal_ws_golden/`; no protocol change, no production backend-authoring
   field, and every terminal inventory producer emits backend.
8. Re-run a bounded source-agnostic consumer sweep for backend selection and inventory fields.
   This is the whole-plan `edge-case-coverage` / `acceptance-observability` check: every producer,
   special value, error subtype, failure boundary, tmux write operation, and display consumer
   named above must have a passing test. In particular, no separately invoked hook leaves a
   process, pending/live row, lease, or lifecycle publication without its paired settlement.

## V1 Plan Changelog

`kind: framing`

- 2026-09-22: Initial decision-complete two-milestone draft with verified Stage 0 closure,
  #22722/restart-8 gate, M2-only #21558 disposition, epoch adoption, and bounded visible fallback.
- 2026-09-22: Clean base validation after full target, derived-carrier, size-split, and consumer-coverage sweep.
- 2026-09-22: Revision round 2 accepted enhancer opportunities 1–6: exhaustive fallback subtypes,
  durable role lifetime, mailbox reuse, gclient scope, Rust tmux parity, and post-announcement cleanup.
- 2026-10-01: Refreshed against 0.5.0 at `7f402248e9`. #22722 landed and restart #8 is recorded
  complete, so the A2 gate is satisfied evidence. Ask is retired (#23055) and tmux discovery
  removed (#23029); their targets and consumers are dropped. Per the PD ruling the role column
  becomes M1 leaf 1.9, which 1.6 and 1.7 depend on, replacing the #22691 dependency. 1.4 ports
  #23076's input-grant split (1.4.6) with #23076 as an external blocker. 1.5 ports the
  coordinator and keeps #22722's submit layer as the single Python implementation; 2.4 binds the
  Python callers to the Rust coordinator in `Native` over a route authenticated by a per-start
  token on the parent pipe, with multi-step actions atomic under the Rust lock (PD rulings).
  Line counts and moved symbols updated; 1.6 keeps the `_terminal_contract.py` split, 1.2 keeps
  the `stop_host_on_shutdown` removal, and 1.9 moves `WorkspacePane` into `workspace_panes.py`.
- 2026-10-01: Enhancer run `5685d4f1`; E1–E7 applied per PD dispositions (gobby#14972): the
  1.9 split moves only `WorkspacePane` and `_optional_str`, 1.6 rejects legacy
  `terminal_backend` bodies in `_load_agent_body`, the 2.4 client reads the runner's child token
  per request and fails closed, 1.9.1 runs the manifest freshness test, the CLI pin is
  `cli_contract.rs` alone, the Collision note reflects #23013, and 2.4 acceptance is ordered.
- 2026-10-01: Adversary round on `00aa0c3`, F1-F5 applied per PD rulings (gobby#14972). 1.4
  ports #23076's durable handoff record (1.4.7-1.4.9). 2.4 replaces the single-sequence
  ruling with a route-held action hold (2.4.9-2.4.11), and adds the non-supervising
  `RustNativeRuntimeClient` seam with one Rust settler (2.4.12-2.4.13). 2.2 ports
  `terminal_set_theme` (2.2.6). A2 cites #23188 for the unreadable-composer branch. D1 and D2
  type the #23076 and #21558 prerequisites under plan-coverage Deferrals.
- 2026-10-01: Adversary round on `03d1ed4`, three blocking findings applied per PD rulings
  (gobby#14972). 2.4 gains `spawn_web_terminal`'s Native branch with no Python row writes and
  pane binding after Rust settlement (2.4.16). The action hold records uncertainty only and
  admits the next effect on completion or proof, with tokens and the PD constants (2.4.14,
  2.4.15). D1 and D2 take placeholder refs and full leaf specs: D1 holds 1.4.6-1.4.9, and D2
  holds 2.2, 2.3, and the WS half of 2.4. M2 keeps 2.1 and the non-WS 2.4 cutover, and A2's
  stage-automation edges are removed. 1.4 and 2.4 record Granularity decisions.
- 2026-10-01: Adversary re-review of `42f8dca`, activation-order finding applied. 2.4 verifies
  the Native seams in isolated tests and refuses a production `Native` start until D2
  registers the family (2.4.17). D2 lifts that gate with an attach-and-control smoke (2.4.18)
  and waits on D1 as well as #21558, wired by the PD at expansion.
- 2026-10-01: V1 consensus. The Adversary (gobby#14579) accepted every other repair at
  `42f8dca`, the PD (gobby#14972) accepted its activation-order finding, and this revision
  applies it. No blocking finding remains open; the Adversary derives M1 from this revision.
- 2026-10-15: Plan Writer gobby#16490 withdraws the M1 stamped from the 2026-10-01 consensus
  before the gap amendment that Orchestrator gobby#15977 assigned for the items moved out of
  #23217.
- 2026-10-15: Gap amendment (Plan Writer gobby#16490, assigned by Orchestrator gobby#15977). 2.4
  now mirrors #23217 and closes on 2.4.3, 2.4.8 (token half), 2.4.14, 2.4.15, and 2.4.17, with
  route authentication on the supervisor pipe now that #23457 made gdaemon the backend's parent.
  The items moved out of #23217 land in six new deliverables: 2.5 Rust write runtimes and the
  host connection opener, 2.6 the loopback write route, 2.7 the Python write client with the
  Native composer ledger, 2.8 the Native settler and the host exit proof, 2.9 the runtime route,
  and 2.10 the Python runtime client, settlement branches, pane binding, and terminal services.
  ID map: 2.4.7 moves to 2.7.1; 2.4.12 to 2.10.1; 2.4.13 to 2.10.2, with its Rust half in 2.8.5
  and 2.9.1; 2.4.16 to 2.10.3; the route halves of 2.4.8, 2.4.14, and 2.4.15 to 2.6.1, 2.6.3,
  and 2.6.4; and the #24210 folds to 2.8.1-2.8.4, with the first fold's text in 2.8's research.
  New items: 2.7.4 (in `Native` an automatic write never types over an operator draft) and
  2.10.4 (`Native` terminal services use the Rust clients). Orchestrator rulings applied: the
  composer ledger stays in the Python write client; each Rust component opens its own control
  connection; confirmed absence is the host exit proof, with no migration 469; and tmux option A
  replaces 1.8, which #23214 closed as obsolete, so D2 and leaf 2.2 drop the 1.8 edge. 1.6's
  consumer row follows the f2ec9007f7 rename (#23779), and its Targets drop the symbols that
  #23212 and #22856 already deleted. D2 carries #23219's restored tests and two LOW findings.
- 2026-10-15: Orchestrator gobby#15977 ruled 2.7's partial-refusal reading in (22:12 CDT), and
  2.7 now records it as a ruling. The ruling also accepted the three validator-forced edits to 1.6,
  1.8, and the 2.4 and D2 leaf targets; the adversary decides the remaining consumer-coverage
  warnings.
- 2026-10-16: Repair of Adversary gobby#16983's F1-F6 and two LOW findings raised at `121294e`
  (Plan Writer gobby#16490), under Orchestrator gobby#15977's no-deferral ruling.
  - F1 (no deferral): D1 and D2 are removed. D1 becomes the landed P1 section 1.10, which #23218
    executed (`6322c1874f`). D2's leaves become the P2 deliverables 2.2, 2.3, 2.12, and 2.13, with
    every acceptance obligation kept. A2 records #23219 as superseded and retired at expansion. The
    new "Cross-plan edges set at expansion" section lists 2.2 blocked by #21558, 2.4 blocking
    node-halves 2.2 (#24371) on `serve.rs::run`, and 2.6 between #23880 and node-halves 1.5
    (#24368) on `FrontDoor::handle`; the Orchestrator sets or flips those directions.
  - F2 (consumer inventory): consumer rows were added to 1.2, 1.6, and 1.7, and the 20
    `MANAGED_BIN_VERSION_PINS` rows to every section that bumps gdaemon: 2.4, 2.5, 2.11, 2.6, 2.8,
    2.9, 2.14, 2.15, 2.2, 2.3, 2.12, and 2.13.
  - F3 (write-route payload parity): 2.6's body cap is `MAX_CONTROL_LINE` plus 64 KiB, payloads
    travel base64, and the route and 2.7's client apply the runtimes' per-step limits (2.6.5,
    2.7.6); 2.9's runtime route shares the gate and cap. The planned one-payload-step rule was
    dropped because plan approval sends `C-r` and a literal as two input steps.
  - F4 (settler epoch fence): 2.8 applies a dead-epoch effect only after
    `reconcile::lock_host_epochs`, shared with `epochs::observe`, confirms the observed epoch is
    still current; 2.8.2 now tests a superseded epoch in `tests/reconcile.rs`.
  - F5: tmux writes leave 2.5 for the new 2.11, with a dedicated `tmux_write.rs` contract.
  - F6: 1.8 becomes framing that records #23214's obsolete disposition and the owners of each
    former tmux duty.
  - LOW-1 becomes 2.8.6, settler handoff expiry over `ControlClient::revoke_input`. LOW-2 becomes
    2.2.7, clearing the handoff record after an ordinary grant or revoke.
  - ID map: 1.4.6-1.4.9 to 1.10.1-1.10.4; 2.5.3 and 2.5.4 to 2.11.1 and 2.11.2, with 2.5 keeping
    2.5.1, 2.5.2, and 2.5.5; 2.4.9-2.4.11 to 2.12.1-2.12.3; 2.4.1, 2.4.2, 2.4.4, 2.4.5, 2.4.6, and
    2.4.18 to 2.13.1-2.13.6, with 2.13.4 rewritten to the fail-closed policy; 2.4 keeps 2.4.3,
    2.4.8, 2.4.14, 2.4.15, and 2.4.17. New items: 2.2.7, 2.6.5, 2.7.6, 2.8.6, 2.10.5,
    2.13.7 (the five restored real-gdaemon tests), 2.14.1-2.14.6, and 2.15.1-2.15.3.
  - Self-found and repaired in the same pass:
    - A1, 1.7, and E1 still described the bounded tmux fallback that the Orchestrator's 2026-10-14
      option-B ruling replaced; 1.7 now carries #23213's landed fail-closed contract (1.7.1-1.7.6).
    - In `Native`, `/api/terminals` would read an empty Python registry; the new 2.14 builds the
      Rust inventory reads and handlers, and 2.13.1 and 2.13.2's tests move to
      `crates/gdaemon/tests/front_door.rs`.
    - 2.13 now targets `websocket/transport.rs` to claim the terminal WS message types; 2.3 gains
      its `ws.rs` and family wiring; and each new module's `lib.rs` declaration is targeted.
    - In `Native`, the Python websocket server had no lease registry and could not start; 2.10
      gives it a workspace event registry (2.10.5).
    - In `Native`, every team-lane launch would refuse for lack of gclient viewports; the new 2.15
      reads them from the Rust lease registry.
    - `FrontDoor::handle` line references follow #23880.
