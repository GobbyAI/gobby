Plan artifact: `.gobby/plans/daemon-side-gterm-adoption-and-terminal-ws.md`

# Daemon-side gterm adoption and terminal WS

**Plan ID:** daemon-side-gterm-adoption-and-terminal-ws

## A1 Planning authority and decision record

`kind: framing`

Director brief, verbatim:

'Scope the plan as two milestones: M1 gterm adoption by epoch (host_manager, leases, host_reconcile: a daemon restart never kills an agent terminal; no dependency on #21558), M2 terminal WS plus proxy relay (after #21558 Native WS transport in gdaemon). Implementation starts after #22722 lands. gclient/gterm is the primary and default backend for every agent; tmux is a bounded fallback with the fallback, visibility and recovery criteria the task carries.'

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
  construct `TerminalHostManager`, `TerminalLeaseRegistry`, or `WriteCoordinator`. This avoids
  a second supervisor and adds no terminal-specific mode knob.
- **Restart semantics — restraint rung 1 (remove a contradictory option):** ordinary stop or
  restart closes daemon clients and leaves the gterm host and its terminals alive. Remove the
  `terminals.stop_host_on_shutdown` override because it contradicts the required invariant.
  Only the existing explicit `gobby stop --terminals` / restart-with-terminals intent may drain
  the host.
- **Backend choice — restraint rung 1 (remove authoring surface):** remove the user/config
  backend choice. Every producer supplies an internal terminal lifetime, not a backend; native
  gterm is always attempted first. An agent definition containing `terminal_backend` is rejected
  both when stored and when a legacy row is resolved.
- **Fallback model — restraint rung 6 (minimum new code):** use one closed three-value typed
  reason set: `host_start_timeout`, `epoch_refused`, and `gterm_missing`. `epoch_refused` is
  emitted only after a reachable host completes authenticated hello, protocol validation, ping,
  and pid-identity validation but its coherent returned epoch is explicitly rejected by the
  durable adoption matrix. Token, pid, protocol, authentication, pre/post-adoption I/O, and all
  later runtime failures remain distinct fail-closed results. No generic native-error fallback
  exists.
- **Lifetime producer — restraint rung 2 (reuse):** a non-user-settable lifetime enum has only
  `run` and `persistent_role`. Every bare/public `SpawnRequest` shape materializes omission as
  `run`. Leaf 1.9 adds nullable `workspace_panes.role`; the exact role-bound producer is
  `WorkspaceOps._fill`: a non-empty durable `WorkspacePane.role` emits `persistent_role`, while a
  missing/empty role emits `run`. Persistent roles fail rather than enter tmux.
- **Role column — PD ruling (gobby#14972, 2026-10-01):** this plan owns the column as one M1
  leaf (1.9): a gcore migration, `WorkspacePane.role`, and an optional `role` argument on the
  `tab.create` and `pane.split` workspace ops. #22895, `Plan runbooks as existing pipelines with
  placed agent launch`, consumes that contract and adds no second column or op argument.
- **Fallback persistence — restraint rung 2 (reuse):** keep `terminals.backend = 'tmux'` and
  store the reason as `locator.fallback_reason` on the same terminal row. No migration or
  derived fallback table is needed because `locator` is the existing backend-owned JSON carrier.
- **Fallback announcement — restraint rung 2 (reuse):** call the existing, unchanged
  `MailboxService.send(target='project')` before tmux launch. The message describes an attempt,
  not an active terminal, until pending-to-live promotion succeeds. A failed enqueue fails the
  spawn; later failures use the existing attempt-scoped terminal settlement and spawn-key kill
  paths, without a mailbox/database transaction subsystem.
- **Tmux runtime parity — restraint rung 6 (minimum new code):** gcode found no Rust tmux runtime
  owner; Rust tmux references are gclient display/direct-attach consumers only. Add one focused
  `gobby-terminals` adapter mirroring the existing Python `TmuxTerminalRuntime` contract. Do not
  add a generic runtime framework or change gclient production code.
- **Recovery — restraint rung 1 (no migration daemon):** backend selection is evaluated anew
  per spawn. A recovered host automatically serves new spawns; live tmux terminals are never
  mutated. Their next restart or handoff creates a new native terminal and then ends the old
  run-scoped tmux terminal through the ordinary lifecycle.
- **Inventory visibility — restraint rung 2 (reuse):** preserve the existing `backend` field in
  HTTP and WS inventory and pin the existing gclient and web renderers with contract tests. Add
  no new badge system or client setting.
- **Adoption wait — restraint rung 6 (minimum code):** use a fixed 2.5-second startup/adoption
  decision deadline, matching the existing terminal attach startup wait. It is a constant, not a
  new config knob. Health retries continue after a degraded decision.
- **Cutover proof — restraint rung 2 (reuse):** the canonical fixture corpus and isolated
  restart/fallback integration tests are the comparison mechanism. Do not dual-execute live
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
write and Enter`. Its chosen contract is the oracle for 1.5 and 2.4.7.

**#21558 disposition — PD ruling (gobby#14972, 2026-10-01):** #21558 blocks the epic during
planning. Its WS transport gates only the work D2 holds: 2.2, 2.3, and the WS half of 2.4. No
deliverable leaf waits on it. Expansion apply creates the D2 deferral task from its placeholder
`task_ref`; the PD then wires that task's blocked-by edges to #21558 and to the created D1 task,
and removes the epic-level #21558 → #21565 edge through `gobby-tasks`.

**Role column ownership:** leaf 1.9 owns `workspace_panes.role`, `WorkspacePane.role`, and the
optional `role` op argument (A1 ruling). 1.6 and 1.7 depend on 1.9 inside M1, so no external
role blocker remains and #21558 stays out of M1.

**#23076 lease dependency — PD ruling:** #23076, `gclient reconnects straight to the host`, is
open and owns the input-grant finalization split and its durable handoff record. 1.4 ports the
registry as landed on 0.5.0, and D1 holds the grant split and handoff record (1.4.6-1.4.9).
Expansion apply creates the D1 deferral task; the PD wires its blocked-by edge to #23076.

## A3 Scope, invariants, and observed evidence

`kind: framing`

In scope:

- A workspace-private `gobby-terminals` crate over the #21557 gcore async pool/transaction seam.
- Epoch-authenticated gterm control/event/frame clients, host adoption/supervision, terminal rows,
  reconciliation, attachment leases, and the post-#22722 write coordinator.
- The current agent-spawn ingress cleanup and bounded tmux fallback policy, before the agent
  family is absorbed by #21564. The fixed policy is a required contract of that later
  `TerminalRuntime` seam.
- After #21558, the native terminal WS handler, proxy relay, family routing, and cutover proof.

Out of scope:

- Any change under `crates/gterminal/`; any production/runtime change under
  `crates/gclient/src/`. The only allowed gclient edit is the named
  `crates/gclient/tests/client_loop.rs` backend-visibility contract update.
- Any change to the three existing wire protocols or to canonical fixture bytes.
- Live migration of an existing tmux terminal, a selectable backend in agent definitions, a
  sticky fallback mode, an operator recovery action, or a new terminal configuration knob.
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
- A repository-wide Rust tmux sweep found no runtime adapter. The complete current behavior oracle
  is `src/gobby/terminals/tmux_runtime.py::TmuxTerminalRuntime`: per-row socket routing, raw input
  and paste/write, resize, bounded snapshot/history, live attach locator, pane/session liveness,
  and termination.

## P1: Milestone M1 — gterm adoption by epoch

`kind: framing`

M1 has no dependency on #21558. It establishes the Rust terminal core, epoch-preserving daemon
restart behavior, current-agent backend policy, and bounded fallback. It may merge while the
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

**Acceptance:**

- 1.2.1 - Same-epoch startup adopts the existing gterm pid and epoch without spawning, rotating
  credentials, or rewriting sockets; two concurrent callers share one adoption/spawn attempt.
  test: `crates/gterminals/tests/host_lifecycle.rs::same_epoch_adopts_singleflight`.
- 1.2.2 - Only a reachable, authenticated, protocol-compatible, pid-verified host whose coherent
  epoch the durable adoption matrix explicitly rejects yields `epoch_refused`; it stays alive,
  health retry remains armed, and no second host is spawned or killed. test:
  `crates/gterminals/tests/host_lifecycle.rs::semantic_epoch_refusal_is_non_destructive`.
- 1.2.3 - An exhaustive subtype matrix proves token mismatch, pid mismatch, protocol mismatch,
  authentication failure, pre-adoption I/O, and post-adoption I/O never map to `epoch_refused` or
  fallback; confirmed absence alone may launch once, a missing executable yields `gterm_missing`,
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
- #23076, `gclient reconnects straight to the host`, is open. Its input-grant finalization split
  and durable handoff record are unlanded (`input_grants.py` is its new module; landed
  `leases.py` has no handoff path). This leaf ports the registry as landed on 0.5.0; D1 holds
  the grant split and handoff record (1.4.6-1.4.9).
- **Choice — restraint rung 6:** use one in-memory registry owned by the terminal family. Leases
  are daemon-connection state and do not need a database table or distributed lock. The only
  durable lease fact is D1's preserved-handoff record.
- **Failure-boundary check (`edge-case-coverage`):** a holder change and its lifecycle publication
  are one registry transition; queue reservation happens before the lease mutation so saturation
  cannot publish only half of the transition.
- **Granularity:** five acceptance items, one production Target, and one state machine, the
  registry. The durable handoff expiry is a second lifecycle owner (repository CAS and reconcile
  retry) and waits on #23076, so it moves to D1 with 1.4.6-1.4.9.

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
  the failing step, and typed refusals. 2.4 binds the Python callers to it in `Native` mode.
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
- `src/gobby/install/shared/config/config.yaml::default_backend`
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerate the TerminalConfig carrier after removing default_backend
- `src/gobby/storage/definitions/agents.py::parent_body`
- `src/gobby/agents/spawn_models.py::resolve_terminal_backend`
- `src/gobby/agents/spawn_models.py::SpawnRequest`
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
- `src/gobby/agents/spawn_executor.py::_default_backend`
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
  `request.terminal_backend` and `default_backend` reads from `resolve_terminal_services` and the
  uncalled `_default_backend`; 1.7 moves the resolver into a support module.
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
- `tests/mcp_proxy/tools/spawn_agent/test_project_context.py` — no-edit-reason: It builds the registry and passes no backend argument.
- `tests/mcp_proxy/tools/spawn_agent/test_project_scope.py` — no-edit-reason: It builds the registry and passes no backend argument.
- `tests/terminals/test_web_spawn.py` — no-edit-reason: Its calls omit the new lifetime keyword, which defaults to run.

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
  absent role emits `run`. Neither value changes native-first selection; the discriminator only
  governs whether fallback is legal. test:
  `tests/terminals/test_workspace_ops.py::test_role_bound_pane_is_persistent_and_bare_pane_is_run`.
- 1.6.6 - A post-change literal sweep has no production `terminal_backend` authoring field or
  `default_backend`; remaining backend occurrences are row/runtime observation or the internal
  selected result. behavior: `backend-option-consumer-sweep`.

### 1.7 Bounded tmux fallback, durable visibility, and automatic recovery [category: code] (depends: 1.2, 1.3, 1.6, 1.9)

`kind: deliverable`

Targets:
- `src/gobby/agents/spawn_executor_terminal.py`
- `src/gobby/agents/spawn_executor.py::resolve_terminal_services`
- `src/gobby/agents/spawn_executor_runtime.py::_runtime_spawn`
- `src/gobby/terminals/web_spawn.py::spawn_web_terminal`
- `tests/agents/test_spawn_executor.py::*` — scope-reason: cover the complete fallback matrix, message ordering, and recovery
- `tests/servers/test_terminals_routes.py::*` — scope-reason: pin backend and fallback reason on HTTP inventory rows
- `tests/servers/test_terminal_ws_list.py::*` — scope-reason: pin backend on WS inventory rows and fallback lifecycle events
- `tests/terminals/test_workspace_ops.py::*` — scope-reason: prove role-bound workspace panes refuse fallback through the shared lifetime contract
- `web/src/components/activity/terminal/__tests__/TerminalSessionList.test.tsx::*` — scope-reason: pin visible gterm/tmux labels
- `crates/gclient/tests/client_loop.rs::*` — scope-reason: pin backend rendering on pane, sidebar, and orphan inventory flows

Research context:

- The facade/support split is established repository architecture: callers keep patching
  `gobby.agents.spawn_executor`, while helpers live in focused support modules. A dedicated
  `spawn_executor_terminal.py` keeps the async fallback decision and announcement in one module
  beside `spawn_executor_runtime.py` and avoids turning the existing general support module into
  another terminal subsystem.
- `TerminalManager` already persists `backend` and `locator`; no migration is required.
  `create_pending` inserts no locator, and `promote_to_live` stores a tmux locator as given
  (`storage/terminals.py:419-420`), so the fallback flow adds `fallback_reason` to the tmux
  locator it promotes and `storage/terminals.py` needs no edit. HTTP and
  WS inventory already emit backend. gclient parses it for live panes/orphans, and web derives
  `backendLabel` as `gterm` or `tmux`.
- `MailboxService.send(target='project')` is a read-only dependency whose signature and semantics
  remain unchanged; it is deliberately absent from Targets. `spawn_web_terminal` is the existing
  workspace-pane pending/prepare/commit/promotion owner and receives the internal lifetime from
  1.6.
- **Choice — restraint rung 2:** reuse the unchanged mailbox call, terminal locator JSON, and the
  existing display fields. Add no fallback table, alert channel, transaction subsystem, or UI
  control.
- **Failure-boundary check (`edge-case-coverage`):** decide a typed reason, reject persistent
  lifetime, enqueue an “attempting tmux fallback” project message, then create/launch tmux. If
  enqueue fails, no row or process exists. If pending-row creation, prepare, commit, or promotion
  fails after enqueue, the existing attempt-generation CAS settles the row to exited and the
  spawn-key termination path removes any process/session before returning. The announcement never
  claims activation before pending-to-live promotion.
- **Sentinel check (`acceptance-observability`):** the three typed reasons, a non-eligible native
  error, `run`, and `persistent_role` each have an observable result test.
- **Granularity:** the listed targets are one behavior leaf because fallback selection, row evidence,
  message-before-launch, recovery, and inventory visibility are one policy. Splitting them would
  permit a fallback that is silent or unidentifiable.

**Placement:** move `resolve_terminal_services` and the new asynchronous fallback
decision/announcement flow from the 612-line facade into new `spawn_executor_terminal.py`.
`spawn_executor.py` re-exports the established patch target, and
`spawn_executor_runtime.py::_runtime_spawn` awaits the moved resolver.

Move the current service resolver into the support module and make it async so it can await the
2.5-second native readiness decision. Only the three typed host outcomes are eligible, with
`epoch_refused` restricted to the semantic subtype defined in 1.2. All token, pid, protocol, auth,
I/O, preparation, commit, capacity, write, and post-adoption failures remain fail closed. For an
eligible run, enqueue one project message containing run/session id, reason, `backend=tmux`, and
attempt status, then create the tmux pending row and launch. Promote to live with the reason in
the tmux locator before any surface may describe the terminal as active. Do not cache the fallback decision.

Consumers unchanged:
- `src/gobby/agents/resume_executor.py` — no-edit-reason: It reaches terminal spawning through the stable spawn_executor facade and inherits the new resolver.
- `src/gobby/terminals/host_reconcile.py` — no-edit-reason: It reconciles native host inventory only; tmux fallback rows never pass through it.
- `src/gobby/storage/terminals.py` — no-edit-reason: `promote_to_live` stores the tmux locator as given, so the reason needs no repository change.
- `tests/agents/terminal_fixtures.py` — no-edit-reason: Existing terminal rows omit optional fallback reason and retain their behavior.
- `tests/agents/test_tmux_integration.py` — no-edit-reason: Explicit tmux runtime integration is not the agent fallback-selection path.
- `tests/hooks/test_session_start_handlers.py` — no-edit-reason: Hook-created fixture rows omit optional fallback evidence.
- `tests/servers/test_attention_native_roster.py` — no-edit-reason: Roster reads terminal backend but does not create fallback rows.
- `tests/servers/test_native_web_proxy.py` — no-edit-reason: Native proxy promotion preserves the existing locator.
- `tests/servers/test_terminal_ws_lease.py` — no-edit-reason: Lease tests consume promoted rows and do not author fallback selection.
- `tests/storage/test_terminal_bindings.py` — no-edit-reason: Binding tests use existing optional locator defaults.
- `tests/storage/test_terminals.py` — no-edit-reason: Repository tests keep valid no-reason row cases; focused fallback tests cover the new key.
- `tests/terminals/fakes.py` — no-edit-reason: Fakes keep backward-compatible optional locator behavior.
- `tests/terminals/test_native_runtime.py` — no-edit-reason: Native runtime tests neither select nor persist tmux fallback.
- `tests/terminals/test_tmux_runtime.py` — no-edit-reason: Runtime tests do not exercise the native-first fallback decision.
- `tests/agents/test_run_completion.py` — no-edit-reason: Run completion consumes terminal lifecycle and is unchanged by optional fallback evidence.
- `tests/agents/watchdog/test_close_review_parked_caller.py` — no-edit-reason: Watchdog promotion fixtures retain the compatible repository signature.
- `tests/terminals/test_web_spawn.py` — no-edit-reason: Its native-host fixtures never reach the fallback decision.

**Acceptance:**

- 1.7.1 - A healthy/adopted host always selects gterm. Exactly `host_start_timeout`, semantic
  `epoch_refused`, and `gterm_missing` may select tmux for `run`; separate fixtures prove token,
  pid, protocol, auth, pre/post-adoption I/O, capacity, child prepare/commit, database, write, and
  arbitrary native subtypes fail closed. test:
  `tests/agents/test_spawn_executor.py::test_tmux_fallback_subtype_matrix_is_exhaustive`.
- 1.7.2 - `persistent_role` plus any eligible reason fails before message, row creation, or tmux
  launch and returns a diagnostic naming native unavailability and the reason. This holds for a
  direct internal request and for `WorkspaceOps._fill` when 1.9's durable `WorkspacePane.role`
  is non-empty. test:
  `tests/agents/test_spawn_executor.py::test_persistent_role_refuses_tmux_fallback`. test:
  `tests/terminals/test_workspace_ops.py::test_role_bound_pane_refuses_tmux_fallback`.
- 1.7.3 - For a run fallback, the project-scoped message is durably enqueued before row/process
  creation and says “attempting” until activation; enqueue failure creates nothing. An injected
  failure after enqueue but before pending-row creation leaves no row/process, while failures
  after pending-row creation at prepare, commit, or promotion terminate the spawn key and
  attempt-CAS the pending row to exited, so no pending/live row or process survives. The attempt
  message and any resulting live row carry the same stable reason and `backend=tmux`. test:
  `tests/agents/test_spawn_executor.py::test_fallback_announcement_is_attempt_until_activation`.
  test:
  `tests/agents/test_spawn_executor.py::test_fallback_failure_after_pending_row_settles_and_kills`.
- 1.7.4 - The live tmux row carries `backend=tmux` and `locator.fallback_reason` after promotion; HTTP/WS list
  output contains backend for native and tmux rows and never derives it from logs. test:
  `tests/servers/test_terminals_routes.py::test_terminal_inventory_exposes_backend_and_fallback_reason`.
- 1.7.5 - Every terminal-list surface identifies the backend: HTTP list/get, WS
  `terminal.list`, gclient live pane/sidebar/orphan rows, and the web terminal session list. test:
  `crates/gclient/tests/client_loop.rs::terminal_inventory_renders_each_backend` and
  test: `web/src/components/activity/terminal/__tests__/TerminalSessionList.test.tsx::rendersBackendForEverySession`.
- 1.7.6 - Host recovery changes no live tmux row. The next ordinary spawn uses gterm without
  operator action; restart/handoff creates a new native terminal before the old run-scoped tmux
  terminal exits, while an in-place live migration is impossible. test:
  `tests/agents/test_spawn_executor.py::test_recovery_is_new_spawn_and_handoff_only`.

### 1.8 Rust tmux runtime parity for live fallback rows [category: code] (depends: 1.3, 1.5)

`kind: deliverable`

Targets:
- `crates/gterminals/src/family.rs`
- `crates/gterminals/src/tmux_runtime.rs`
- `crates/gterminals/tests/tmux_runtime.rs`

Research context:

- A gcode Rust sweep found no daemon-side tmux runtime adapter. Existing Rust tmux code is limited
  to gclient display/direct-attach consumption and does not own terminal operations. The new
  `tmux_runtime.rs` path is therefore a focused adapter, not a duplicate of an existing Rust
  owner; its name mirrors the complete Python oracle at
  `src/gobby/terminals/tmux_runtime.py::TmuxTerminalRuntime`.
- The Python oracle supplies the exact required operations: per-row configured socket selection,
  `write_text`/`write_key`/raw `write_input`/bracketed `write_paste`, manual-window resize,
  bounded/full snapshots with history bounds, live attach locator, pane-process liveness distinct
  from session presence, and termination on the row's own socket.
- Memory evidence requires `attach_locator` to resolve `frame_host_epoch` at call time from the
  terminal row with fallback to the live host manager epoch and derive the frames socket from the
  live host-manager directory. It also requires `is_live` to use `pane_dead`, while cleanup checks
  session presence separately because remain-on-exit panes can be dead but capturable.
- **Choice — restraint rung 6:** add this one adapter because no existing Rust owner provides the
  complete contract. Reuse the installed tmux executable and current row locator; add no tmux
  protocol, cache, runtime registry hierarchy, or dependency.

Implement the adapter only for durable `backend=tmux` rows already selected by 1.7. Every command
uses the row's recorded socket/pane/server identity, never the process default socket. Register it
with the terminal family so Native mode can operate existing tmux rows without changing their
backend or migrating them.

**Acceptance:**

- 1.8.1 - Raw input, literal text, named keys, and bracketed paste preserve current byte/size and
  delivered/indeterminate/partial-failure semantics on the row's own tmux socket, including SGR
  mouse input used for tmux copy-mode scrolling. test:
  `crates/gterminals/tests/tmux_runtime.rs::input_and_write_match_python_on_recorded_socket`.
- 1.8.2 - Resize uses manual window sizing; bounded and full snapshots preserve text, history
  bounds, and truncation metadata for a live or remain-on-exit row. test:
  `crates/gterminals/tests/tmux_runtime.rs::resize_and_snapshot_preserve_tmux_history_contract`.
- 1.8.3 - Attach locator preserves socket path, pane id, server pid/start time, and resolves the
  live frame host epoch/socket directory at call time rather than caching either generation.
  test: `crates/gterminals/tests/tmux_runtime.rs::attach_locator_uses_live_host_generation`.
- 1.8.4 - Pane-process liveness and tmux-session presence are distinct; termination addresses the
  recorded socket and failed termination leaves the row eligible for existing orphan retry rather
  than reporting success. test:
  `crates/gterminals/tests/tmux_runtime.rs::liveness_presence_and_termination_are_socket_scoped`.
- 1.8.5 - Native terminal-family mode routes every live tmux row operation through this adapter
  and preserves its backend, locator, and lifecycle; no gclient production/runtime or protocol
  change is introduced. behavior: `native-family-live-tmux-runtime-parity`.

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

## P2: Milestone M2 — terminal WS and proxy relay [category: code] (depends: P1)

`kind: framing`

M2's deliverables run without #21558: the 2.1 codec and the 2.4 Native ownership cutover. D2
holds the work that rides #21558's native gdaemon WS accept/auth/subscription/broadcast
transport (2.2, 2.3, and the WS half of 2.4) at leaf-spec fidelity. M2 and D2 own terminal
messages and relaying on that transport and do not recreate transport authentication or the
base event envelope.

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

### 2.4 Native ownership cutover: write route, action hold, runtime seam, and settlement [category: code] (depends: 1.2, 1.3, 1.5)

`kind: deliverable`

Targets:
- `crates/gterminals/src/family.rs`
- `crates/gdaemon/src/front_door/routes.rs::*` — scope-reason: register the loopback write and runtime routes
- `crates/gdaemon/src/serve.rs::*` — scope-reason: construct Native terminal ownership once at startup, reject a runtime route-mode flip, and read the per-start write token in watch_parent_fd
- `src/gobby/runner_front_door.py::FrontDoorChild._popen`
- `tests/test_runner_front_door.py::*` — scope-reason: pin token delivery over the parent pipe and its absence from env and argv
- `src/gobby/runner_init/terminal_wiring.py::init_terminal_wiring`
- `crates/gterminals/src/write_api.rs`
- `crates/gterminals/src/write.rs`
- `crates/gterminals/src/runtime_api.rs`
- `src/gobby/terminals/coordinator_client.py`
- `src/gobby/terminals/runtime_client.py`
- `src/gobby/terminals/pane_io.py::submit_coordinated_text`
- `src/gobby/agents/spawn_executor.py::resolve_terminal_services`
- `src/gobby/agents/spawn_executor_runtime.py::_runtime_spawn`
- `src/gobby/agents/lifecycle_reconciliation.py::LifecycleReconciliation.reap_stale_pending`
- `src/gobby/terminals/web_spawn.py::spawn_web_terminal`
- `tests/terminals/test_workspace_ops.py::*` — scope-reason: prove the Native workspace spawn writes no Python terminal rows and binds the pane only after Rust settles
- `tests/terminals/test_coordinator_client.py`
- `tests/terminals/test_runtime_client.py`
- `crates/gterminals/tests/family_cutover.rs`

Research context:

- In `Native`, Python constructs no `WriteCoordinator` (A1), yet about 30 Python modules call it
  in-process: attention routes, the plan-approval and session-config WS handlers, session
  send-keys and compaction, compact continuation, `workspace_writes` and `pane_io`, spawn
  support, watchdog recovery, and terminal lifecycle monitoring. `init_terminal_wiring` binds
  `coordinator_client.RustWriteCoordinatorClient` where it binds `WriteCoordinator` today, so
  those callers keep one attribute and one call surface: `write`, `run_sequence`,
  `run_native_wake_batch`, `quarantine`, `retain_unresolved`, `observe_resolved(_async)`,
  `observe_operator_input(_async)`, `clear_on_exit`, `logical_action_lock`, `lock_held`,
  `runtime_for`, and `set_attention_gate`.
- Transport: the terminal family registers a loopback-only write route on gdaemon
  (`write_api.rs`) that runs each coordinator operation under the Rust per-terminal lock and
  returns 1.5's typed per-step outcome. `runtime_for` resolves the read runtime from the row's
  backend through the registry, which in `Native` holds the runtime client below, and the
  attention gate runs in the client before a write is forwarded.
- Runtime seam — PD ruling (gobby#14972, 2026-10-01, Adversary F5): today
  `init_terminal_wiring` (33-114) registers `NativeTerminalRuntime(HostManagerControl(...))`,
  and spawn, web-workspace, read, and termination callers reach the host through it. In
  `Native`, Python constructs no `TerminalHostManager` (1.2), so `init_terminal_wiring` registers
  `runtime_client.RustNativeRuntimeClient` for the native and tmux backends instead. It is
  non-supervising and implements the `TerminalRuntime` surface the callers use (`prepare_spawn`,
  `commit`, `reserve`, snapshot and history reads, liveness, host readiness, `terminate`) as
  operations on the token-authenticated route (`runtime_api.rs`). It has no write methods;
  every write goes through `RustWriteCoordinatorClient`. `native_runtime.py` (902 lines) is
  unchanged because the client is a separate implementation. The settled host decision 1.7
  reads comes through this seam in `Native`.
- Settlement ownership — PD ruling: the 1.3 Rust repository is the single settler of pending
  and prepared rows. In `Native`, `_runtime_spawn` drives the client, and the Rust side creates,
  promotes, or fails the row in the 1.3.5 settlement transaction; `_unplaced_runtime_spawn` and
  `_promote_prepared` (the #23010 path) run only outside `Native`.
  `LifecycleReconciliation.reap_stale_pending` returns without a write in `Native` because Rust
  reconcile (1.3.3) settles stale pending attempts. `Proxy` and `Compare` keep the Python path
  unchanged.
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
- Route authentication — PD ruling (gobby#14972, 2026-10-01): no Python-to-gdaemon credential
  exists, and the runner spawns gdaemon (`runner_front_door.py::FrontDoorChild._popen`, which
  passes `GOBBY_PARENT_FD`; the child watches it in `serve.rs::watch_parent_fd`). On every child
  start, `_popen` mints a random token and writes it as the first line of that pipe;
  `watch_parent_fd` reads the line and then keeps its EOF watch. gdaemon holds the token in
  memory only, and the write route rejects any request without it. The token never appears in
  env, argv, a file, or a log, and it rotates on every start, including a respawn;
  `init_terminal_wiring` hands `RustWriteCoordinatorClient` the runner, and the client reads
  `runner.front_door_child` and its token on every request because the child is attached after
  wiring (`runner.py:481`); a missing child or token fails closed. A serve without `GOBBY_PARENT_FD` registers no write route.
- Multi-step atomicity — PD ruling (gobby#14972, 2026-10-01, Adversary F2). This replaces the
  earlier single-`run_sequence` ruling, which cannot cover the composer ladder:
  `submit_coordinated_text` and `CoordinatorPaneIO._dispatch` write, wait, send Enter, poll the
  composer, and retry Enter across separate calls, and
  `watchdog/recovery.py::_attempt_idle_reprompt` clears, observes, and reprompts under
  `logical_action_lock`. The write route carries a held logical action:
  - Acquire: `acquire_action` takes the Rust per-terminal action hold and returns a hold id bound
    to the current child token, terminal row, and lease generation. A second acquire waits.
  - Admit and revalidate: an operation carrying the hold id runs under the hold after the
    coordinator re-reads the row and lease (1.5.2). A stale generation, lost lease, or rotated
    token refuses it.
  - Tokens: every hold carries a token. A reply carrying a superseded token never completes or
    releases a newer hold.
  - Queue: while a hold is active, route operations without the hold id queue in arrival order
    and flush in order on release. D2 adds Rust WS operator input to the same queue.
  - Uncertainty: TTL expiry, a timeout, client disconnect, cancellation after dispatch, and
    child stop record uncertainty (1.5.3 unresolved evidence) and nothing more. The next queued
    effect is admitted only after the admitted operation completes or is proven unable to
    produce further effects: the pane or terminal was destroyed, the gterm host epoch changed,
    or the host process exited. An operator close or kill of the terminal is the escape hatch
    because it produces that proof. Release never interrupts an admitted step.
  - Recovery: a lost acquire, step, or release reply never replays a non-idempotent uncertain
    write. The action key and unresolved evidence (1.5.3) decide, and a child restart's new
    token invalidates every old hold.
  - Constants: no gterm or terminal WS constant governs an action hold or its queue (the
    nearest are `frame_client.py::DELTA_QUEUE_ENTRIES` = 64 and
    `leases.py::LIFECYCLE_PUBLICATION_QUEUE_MAXSIZE` = 256, which size other queues), so the PD
    values apply: hold TTL 10 s; queue bound 128 items or 256 KiB, whichever comes first; queue
    age 30 s, measured on the oldest queued item. At the bound or the age limit, new input is
    refused with an explicit typed error to its sender. Queued items are never discarded, and
    input is never dropped silently.
  - Python: `RustWriteCoordinatorClient.logical_action_lock(terminal_id)` returns an async
    context that holds a local `asyncio.Lock` and the remote hold together, so Python-side
    exclusion is unchanged. `submit_coordinated_text` runs its ladder inside
    `logical_action_lock`; its only caller, `workspace_writes.write_workspace_pane`, holds none,
    and the recovery and wake paths already hold it. `run_sequence` and `run_native_wake_batch`
    stay as they are, and the hold is the only cross-call atomicity mechanism. The composer
    ladder changes only by that lock wrapping.
- Activation gate — Adversary sequencing finding (2026-10-01): until D2 registers the terminal
  family, the terminal WS routes stay Python, and `terminal_ws.py::TerminalWsMixin._leases`
  (887-891) requires a Python `TerminalLeaseRegistry`, which neither Rust client replaces. 2.4
  therefore builds and verifies the owner, runtime, and write seams in isolated `Native` tests
  only. Production stays `Proxy` or `Compare` with Python owners, and a startup that selects
  `Native` while the terminal family is unregistered is refused before any ownership change.
  D2 lifts the gate in the same change that registers the family. No per-operation rollout knob
  or second supervisor is added.
- **Granularity:** nine acceptance items over one atomic Native ownership cutover. Supervisor
  exclusion, the write route with its action hold, the runtime seam, and single Rust settlement
  close together: exclusion without the write client strands the Python write callers,
  exclusion without the runtime seam strands spawn, read, and termination, and the seam without
  Rust settlement leaves two settlers. The action hold is the write route's admission rule, so
  it is not independently closeable from the route. Exclusion without WS routing would strand
  terminal WS clients, so the activation gate keeps production on Python owners until D2.

`Native` constructs Rust ownership and omits Python ownership. Route mode is read once during
startup; a runtime flip is rejected so existing sessions cannot be half-migrated.

Consumers unchanged:
- `src/gobby/runner_init/orchestration.py` — no-edit-reason: It calls `init_terminal_wiring` through the unchanged signature, and its wake batch already runs under `logical_action_lock`.
- `src/gobby/agents/watchdog/recovery.py` — no-edit-reason: `_attempt_idle_reprompt` already runs its steps under `logical_action_lock`, which the client backs with the route hold.
- `src/gobby/terminals/workspace_writes.py` — no-edit-reason: It calls `submit_coordinated_text` with the unchanged signature and holds no action lock.
- `src/gobby/terminals/native_runtime.py` — no-edit-reason: `Proxy` and `Compare` keep `NativeTerminalRuntime`; `Native` registers the separate runtime client.
- `src/gobby/agents/lifecycle_monitor.py` — no-edit-reason: `reap_stale_pending` delegates to the reconciliation method with an unchanged signature and return type.
- `tests/agents/test_lifecycle_monitor.py` — no-edit-reason: It runs outside `Native`, where the reap path is unchanged.
- `tests/agents/test_lifecycle_reconciliation.py` — no-edit-reason: It runs outside `Native`, where the reap path is unchanged.
- `tests/agents/test_spawn_executor_placement_bind.py` — no-edit-reason: It runs outside `Native`, where the reap path is unchanged.
- `tests/terminals/test_in_doubt_kill_truth.py` — no-edit-reason: It runs outside `Native`, where the reap path is unchanged.
- `tests/terminals/test_tmux_runtime.py` — no-edit-reason: It runs outside `Native`, where `spawn_web_terminal` keeps its current body.
- `tests/terminals/test_web_spawn.py` — no-edit-reason: It runs outside `Native`, where `spawn_web_terminal` keeps its current body.

**Acceptance:**

- 2.4.3 - Startup has exactly one supervisor in every mode, and a runtime route-mode change is
  rejected with restart-required before altering ownership. test:
  `crates/gterminals/tests/family_cutover.rs::cutover_requires_restart_and_has_one_owner`.
- 2.4.7 - In `Native`, every Python coordinator caller reaches the Rust coordinator through
  `RustWriteCoordinatorClient`, and #22722's submit regressions pass against it: an unreadable
  composer draft is never reported as confirmed success (#23188's contract), and a held verified
  submit retries without a second write, and `submit_coordinated_text` runs its unchanged ladder
  inside `logical_action_lock`. test:
  `tests/terminals/test_coordinator_client.py::test_pane_io_submit_regressions_hold_against_rust_coordinator`.
- 2.4.8 - The write route refuses a missing or wrong token; the token never appears in the
  child's environment or argv and rotates on every start; a serve without `GOBBY_PARENT_FD`
  exposes no write route; a write issued before the child is attached, or after it stops, is
  refused. test:
  `crates/gterminals/tests/family_cutover.rs::write_route_requires_the_per_start_parent_pipe_token`.
  test: `tests/test_runner_front_door.py::test_write_token_travels_only_over_the_parent_pipe`.
- 2.4.12 - In `Native`, constructing `TerminalHostManager` is refused, and agent spawn,
  web-workspace spawn, snapshot reads, and termination run through `RustNativeRuntimeClient`.
  test: `tests/terminals/test_runtime_client.py::test_native_consumers_use_the_rust_runtime_client`.
- 2.4.13 - In `Native`, agent, retry, placed, and web/workspace spawns and a stale-pending reap
  make zero Python terminal-row writes and the Rust repository settles each row; `Proxy` keeps
  the Python settlement path unchanged. test:
  `tests/terminals/test_runtime_client.py::test_native_settlement_has_one_rust_owner`.
- 2.4.14 - While a hold is active, unheld route operations queue in arrival order and flush in
  order on release; at 128 items, 256 KiB, or a 30 s oldest-item age, new input is refused with
  an explicit typed error and no queued item is discarded; a reply carrying a superseded hold
  token neither completes nor releases the newer hold. test:
  `crates/gterminals/tests/family_cutover.rs::route_hold_queue_is_bounded_and_token_scoped`.
- 2.4.15 - TTL expiry, timeout, or disconnect during an admitted host write records uncertainty
  only: the next queued effect is not admitted before completion or proof (pane or terminal
  destroyed, host epoch changed, host process exited, or an operator close or kill), and a
  delayed effect that lands after the uncertainty is persisted is not replayed and releases no
  newer hold. test:
  `crates/gterminals/tests/family_cutover.rs::uncertain_hold_admits_nothing_before_proof`.
- 2.4.16 - In `Native`, a workspace pane binds only after Rust returns the settled terminal id,
  with no Python terminal-row write; a Rust settlement failure binds no pane. test:
  `tests/terminals/test_workspace_ops.py::test_native_pane_binds_after_rust_settlement`. test:
  `tests/terminals/test_workspace_ops.py::test_native_settlement_failure_binds_no_pane`.
- 2.4.17 - A startup that selects `Native` while the terminal family is unregistered is refused
  before any ownership change, and `Proxy` and `Compare` keep the Python host manager, lease
  registry, and write coordinator. test:
  `crates/gterminals/tests/family_cutover.rs::native_activation_refuses_without_the_terminal_family`.

## D1 #23076 input-grant finalization and durable handoff (depends: 1.4)

`kind: deferred`

#23076, `gclient reconnects straight to the host`, is open and owns the input-grant
finalization split and the durable handoff record (A2). This section is the leaf spec to expand
by hand once #23076 lands; the PD wires its blocked-by edge to #23076 at expansion.

**Leaf D1 — Input-grant finalization split and durable handoff record (depends: 1.4)**

Leaf targets:
- `crates/gterminals/src/leases.rs`
- `crates/gterminals/src/repository.rs`
- `crates/gterminals/src/reconcile.rs`
- `crates/gterminals/tests/leases.rs`

Research context:

- #23076, `gclient reconnects straight to the host`, sets the finalization split this port
  keeps. Daemon-shutdown finalization preserves an already-authorized direct native holder's
  input grant. Every other finalization revokes: socket loss, release or preemption, proxy and
  web holders, and host refusal. The adopting daemon revokes a preserved grant that is not
  re-bound within #23076's bounded window. The registry ports the landed semantic and its window
  constant (A2).
- Handoff persistence — PD ruling (gobby#14972, 2026-10-01, Adversary F1): #23076's working
  code (`leases.py::TerminalLeaseRegistry.finalize`, `input_grants.py::sync_host_input_grant`
  and `expire_native_input_handoffs`, `TerminalManager.record_native_input_handoff`, and
  `TerminalHostManager.reconcile`) confirms preservation only after a durable compare-and-set of
  `process.native_input_handoff` keyed on native backend, live state, machine, host epoch, and
  physical terminal identity. A failed, raising, or cancelled record revokes. Host reconcile
  expires a record after #23076's 30-second window through an authenticated, attachment-guarded
  revoke, keeps the record and retries when that revoke fails, and clears it conditionally so a
  rebind or replacement wins. 1.4 ports this through the 1.3 repository and reconcile on the
  existing process JSON, with no new table.
- **Granularity:** one leaf. Preservation is a registry finalize transition confirmed only by
  the handoff compare-and-set, and expiry revokes through the same registry, so neither half
  closes without the other.

Deferred acceptance:

- 1.4.6 - Daemon-shutdown finalization preserves only an already-authorized direct native
  holder's input grant; socket loss, release, preemption, proxy and web holders, and host
  refusal revoke; the adopting daemon revokes a preserved grant not re-bound within #23076's
  bounded window. test:
  `crates/gterminals/tests/leases.rs::shutdown_preserves_only_direct_native_input_grant`.
- 1.4.7 - A grant is preserved only after the handoff record's compare-and-set succeeds on the
  full identity; a failed, raising, or cancelled record revokes the grant. test:
  `crates/gterminals/tests/leases.rs::handoff_preservation_requires_a_durable_identity_cas`.
- 1.4.8 - Expiry clears only the record it read: a rebind or replacement that lands first
  wins, and an identity mismatch leaves the newer record. test:
  `crates/gterminals/tests/leases.rs::handoff_expiry_never_clears_a_rebound_record`.
- 1.4.9 - A failed expiry revoke keeps the record, and the next reconcile pass retries it.
  test: `crates/gterminals/tests/leases.rs::failed_handoff_revoke_keeps_the_record_and_retries`.

```yaml
deferral:
  task_ref: "TBD-after-23076"
  reason: "External prerequisite: the ported finalization split and handoff record are #23076's unlanded working code."
  owner: "program-director"
  original_acceptance_items:
    - 1.4.6
    - 1.4.7
    - 1.4.8
    - 1.4.9
```

## D2 #21558 terminal WS operations, relay, and family cutover (depends: 2.1, 2.4, 1.8)

`kind: deferred`

#21558 supplies gdaemon's native WS accept, auth, subscription, and broadcast transport (A2,
P2). The three leaf specs below ride that transport; expand them by hand, in order, once
#21558 lands. D2 also waits on D1: activating `Native` with 1.4's landed revoke-on-finalize
policy would break 1.2.5's restart I/O invariant. The parser cannot name a deferred section in
`(depends: ...)`, so at expansion the PD wires the D2 task's blocked-by edges to #21558 and to
the D1 task named in `deferral_task_map`, and hand expansion lands D1 before the 2.4-WS leaf.

**Leaf 2.2 — Native terminal WS connection and operation state machine (depends: 2.1, 1.8)**

Leaf targets:
- `crates/gterminals/src/ws.rs`
- `crates/gterminals/src/family.rs`
- `crates/gterminals/tests/ws_operations.rs`

Research context:

- `TerminalWsMixin` is the operation oracle for list, attach, detach, sizing, scroll, input,
  paste, operator write, control, lease-lost fanout, proxy attach, and `terminal_set_theme`.
  #21558 supplies connection
  auth/subscription/broadcast and the outer envelope.
- Runtime resolution remains per terminal row. A terminal id is backend-neutral; native rows use
  the native host runtime and live tmux rows use 1.8's complete Rust adapter for input/write,
  resize, snapshot/history, attach locator, liveness, and termination.
- `terminal_set_theme` — PD ruling (Adversary F4): the oracle is
  `terminal_ws.py::_handle_terminal_set_theme` and `_declare_holder_theme`. gclient
  `app/theme_sync.rs` already sends it for proxied panes.
- **Choice — restraint rung 2:** implement these messages as one #21558 WS route handler backed by
  the M1 repository/lease/write APIs. Do not introduce a terminal-only listener or socket.

Implement one connection state owning its attachments and relay. Authorize project access before
lookup, validate each message before mutation, reserve lifecycle capacity before changing a
holder, and finalize all connection attachments exactly once on disconnect or send failure.

Deferred acceptance:

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
  preserves unresolved evidence, and does not degrade into tmux fallback. test:
  `crates/gterminals/tests/ws_operations.rs::write_fault_is_fail_closed_not_fallback`.
- 2.2.6 - `terminal_set_theme` checks socket ownership, native capability, and codec; stores the
  attachment theme; relays only when the sender holds input or no holder exists; replays the
  stored theme on take; refuses an observer; and sends a `terminal_error` without
  `attachment_id` so a pending control reply is untouched. No gclient or wire edit. test:
  `crates/gterminals/tests/ws_operations.rs::set_theme_is_holder_scoped_and_isolated_from_pending_control`.

**Leaf 2.3 — Bounded proxy relay and frame/lifecycle fanout (depends: 2.2)**

Leaf targets:
- `crates/gterminals/src/relay.rs`
- `crates/gterminals/tests/proxy_relay.rs`

Research context:

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

Deferred acceptance:

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

**Leaf 2.4-WS — Terminal family registration, WS action-hold queueing, and end-to-end parity (depends: 2.3)**

Leaf targets:
- `crates/gterminals/src/family.rs`
- `crates/gdaemon/src/front_door/routes.rs::*` — scope-reason: route terminal HTTP and WS as one family beside 2.4's write and runtime routes
- `crates/gterminals/src/write_api.rs`
- `crates/gterminals/tests/family_cutover.rs`
- `tests/terminals/test_coordinator_client.py`
- `tests/terminals/acceptance/test_native_lifecycle.py::*` — scope-reason: add gdaemon native-family restart, fallback, and explicit-drain acceptance

Research context:

- #21544 requires one family routing-table change. #21558 owns the listener and auth transport;
  this leaf registers terminal HTTP/WS routes and switches their family backend.
- Live `Compare` dual execution is unsafe for input/control/write. The canonical corpus and a
  captured, non-mutating decode/encode comparison supply parity without duplicating effects.
- **Choice — restraint rung 2:** use the existing family route switch and Python proxy as the
  rollback. Do not add per-operation rollout flags. The supervisor owner changes only at process
  start, so a route change requires the existing announced restart/cutover procedure.
- Rust WS operator input joins 2.4's route-hold queue under 2.4's tokens, uncertainty, proof,
  and constants.

Register `/api/terminals` and terminal WS messages as one terminal family. `Proxy` keeps Python
ownership. `Compare` proxies effects and compares only normalized non-mutating output.
Registering the family lifts 2.4's `Native` activation gate in the same change.

Deferred acceptance:

- 2.4.1 - One terminal-family registration owns terminal HTTP and WS dispatch; `Proxy` delegates,
  `Native` serves Rust, and no request is split between owners. test:
  `crates/gterminals/tests/family_cutover.rs::terminal_family_routes_as_one_unit`.
- 2.4.2 - `Compare` never executes input, write, resize, control, spawn, terminate, or drain twice;
  it compares only canonical serialization of non-mutating captured results. test:
  `crates/gterminals/tests/family_cutover.rs::compare_mode_never_duplicates_terminal_effects`.
- 2.4.4 - An isolated native-mode run spawns an agent on gterm, preserves pid/epoch/terminal and
  I/O across ordinary gdaemon restart, and drains only with explicit `--terminals`. test:
  `tests/terminals/acceptance/test_native_lifecycle.py::test_gdaemon_native_family_restart_preserves_agent_terminal`.
- 2.4.5 - Isolated fallback cases prove all three allowed reasons, project message visibility,
  row evidence, persistent-role refusal, automatic recovery for new spawns, and handoff-only
  replacement of an existing tmux run. test:
  `tests/terminals/acceptance/test_native_lifecycle.py::test_bounded_tmux_fallback_visibility_and_recovery`.
- 2.4.6 - Python and Rust golden replay plus end-to-end gclient attach/render/input all pass with
  no change to gterm, no production/runtime change under `crates/gclient/src/`, and no protocol or
  canonical fixture-byte change. The sole allowed gclient edit remains
  `crates/gclient/tests/client_loop.rs`. behavior: `terminal-family-cutover-parity-gate`.
- 2.4.9 - While a Python action hold is active, Rust WS operator input joins 2.4's queue in
  order and flushes in order on release; at the 2.4 bound or age limit the operator receives an
  explicit error frame and no queued frame is discarded. test:
  `crates/gterminals/tests/family_cutover.rs::operator_input_queues_behind_a_held_python_action`.
- 2.4.10 - Cancellation before dispatch emits nothing; cancellation after dispatch admits no
  queued WS frame until the host effect completes or 2.4's proof arrives. A lost acquire, step, or release
  reply, a child-token rotation, a lease loss, and TTL expiry during a pending host write never
  replay a non-idempotent uncertain write. test:
  `crates/gterminals/tests/family_cutover.rs::action_hold_release_is_effect_safe`.
- 2.4.11 - The real `submit_coordinated_text`, held-submit retry, and `_attempt_idle_reprompt`
  paths keep their steps contiguous against concurrent Rust operator input, cancellation, and
  TTL expiry. test:
  `tests/terminals/test_coordinator_client.py::test_real_submit_retry_and_reprompt_paths_hold_against_operator_input`.
- 2.4.18 - The change that registers the terminal family lifts 2.4's activation gate, and an
  isolated `Native` run attaches, takes control, writes, and releases through the Rust family
  end to end. test:
  `tests/terminals/acceptance/test_native_lifecycle.py::test_native_activation_attach_and_control_smoke`.

```yaml
deferral:
  task_ref: "TBD-after-21558"
  reason: "External prerequisite: terminal WS operations, relay, family registration, and native-mode runs ride #21558's transport."
  owner: "program-director"
  original_acceptance_items:
    - 2.2.1
    - 2.2.2
    - 2.2.3
    - 2.2.4
    - 2.2.5
    - 2.2.6
    - 2.3.1
    - 2.3.2
    - 2.3.3
    - 2.3.4
    - 2.4.1
    - 2.4.2
    - 2.4.4
    - 2.4.5
    - 2.4.6
    - 2.4.9
    - 2.4.10
    - 2.4.11
    - 2.4.18
```

## E1 Verification

`kind: verification`

Executors run focused verification after each leaf and the complete matrix before native cutover:

1. `cargo fmt --all -- --check` and the repository Rust lint/type gate for every changed crate.
2. `cargo nextest run -p gobby-terminals`, including the live-tmux adapter contract; focused
   `-p gobby-daemon` integration tests for family composition and shutdown.
3. With isolated `DATABASE_URL` and `GOBBY_TEST_PROTECT=1`, run the changed Python test files only:
   backend ingress/selection, spawn executor, agent definitions, terminal routes/WS, host shutdown,
   workspace storage and ops, the coordinator client, and native lifecycle acceptance. Never run the full pytest suite.
4. Run `tests/servers/test_terminal_ws_golden.py`,
   `crates/gclient/tests/ws_golden.rs`, and `crates/gterminals/tests/ws_golden.rs` against the same
   unchanged `tests/fixtures/terminal_ws_golden/manifest.json`.
5. Run the M1 restart test before #21558 is available; it must exercise the Rust owner lifecycle
   directly and prove that M1 has no terminal-WS dependency.
6. After #21558, run the isolated native route cutover with real gdaemon/gterm/gclient and the
   three fallback simulations. Capture host pid/epoch, terminal id/backend/reason, message id,
   and pre/post-restart I/O as evidence.
7. Audit `git diff` for the implementation commits: no change under `crates/gterminal/`, no
   production/runtime change under `crates/gclient/src/`, no gclient edit except
   `crates/gclient/tests/client_loop.rs`, and no change under
   `tests/fixtures/terminal_ws_golden/`; no protocol change, no production backend-authoring
   field, and every terminal inventory producer emits backend.
8. Re-run a bounded source-agnostic consumer sweep for backend selection and inventory fields.
   This is the whole-plan `edge-case-coverage` / `acceptance-observability` check: every producer,
   special value, error subtype, failure boundary, tmux runtime operation, and display consumer
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

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Workspace-private terminal crate and unchanged host-wire clients
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '1.1.1: `gobby-terminals` is a workspace member, is `publish
    = false`, inherits workspace version/edition/lints, exports the terminal family
    API, and is linked by gdaemon. file: `crates/gterminals/Cargo.toml`.

    1.1.2: The control client round-trips hello, ping, inventory, spawn prepare/commit/abort,
    terminate, write, resize, observer reservation, and event subscription over the
    existing newline JSON contract without changing gterm. test: `crates/gterminals/tests/protocol_contract.rs::control_json_lines_match_gterm`.

    1.1.3: The event client rejects a changed epoch or non-monotonic sequence, resumes
    from the last committed cursor, and reports an unreplayable cursor as a reconcile
    requirement rather than silently skipping events. test: `crates/gterminals/tests/protocol_contract.rs::event_epoch_and_cursor_are_fail_closed`.

    1.1.4: The frame client enforces the existing length/queue ceilings and reproduces
    bincode attach, input, output, history, lifecycle, and close frames byte-for-byte.
    test: `crates/gterminals/tests/protocol_contract.rs::frame_bytes_match_gterm`.

    1.1.5: The implementation diff contains no change under `crates/gterminal/`, no
    production/runtime change under `crates/gclient/src/`, and no protocol or fixture-byte
    change. The only allowed gclient edit in the whole plan is the named `crates/gclient/tests/client_loop.rs`
    backend-visibility contract update. behavior: `gterm-gclient-protocol-scope-guard`.'
  labels:
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.1:1.1.1
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.1:1.1.2
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.1:1.1.3
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.1:1.1.4
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.1:1.1.5
  tdd: true
  source_section: '1.1'
  implementation_domain: backend
- title: Epoch adoption, single-owner supervision, and restart preservation
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  - '1.6'
  validation_criteria: '1.2.1: Same-epoch startup adopts the existing gterm pid and
    epoch without spawning, rotating credentials, or rewriting sockets; two concurrent
    callers share one adoption/spawn attempt. test: `crates/gterminals/tests/host_lifecycle.rs::same_epoch_adopts_singleflight`.

    1.2.2: Only a reachable, authenticated, protocol-compatible, pid-verified host
    whose coherent epoch the durable adoption matrix explicitly rejects yields `epoch_refused`;
    it stays alive, health retry remains armed, and no second host is spawned or killed.
    test: `crates/gterminals/tests/host_lifecycle.rs::semantic_epoch_refusal_is_non_destructive`.

    1.2.3: An exhaustive subtype matrix proves token mismatch, pid mismatch, protocol
    mismatch, authentication failure, pre-adoption I/O, and post-adoption I/O never
    map to `epoch_refused` or fallback; confirmed absence alone may launch once, a
    missing executable yields `gterm_missing`, and the 2.5-second ready deadline yields
    `host_start_timeout`. test: `crates/gterminals/tests/host_lifecycle.rs::adoption_result_subtypes_are_exhaustive`.

    1.2.4: In `Proxy` and `Compare`, Rust performs observation only and Python is
    the active supervisor; in `Native`, Rust is the active supervisor and Python constructs
    none of its host, lease, or write owners. test: `crates/gterminals/tests/host_lifecycle.rs::route_mode_has_exactly_one_supervisor`.

    1.2.5: Ordinary gdaemon stop/start/restart preserves the host pid, epoch, terminal
    id, and bidirectional terminal I/O; `TerminalConfig` has no `stop_host_on_shutdown`,
    config containing it is rejected as unknown, and no ordinary config can request
    a drain. test: `crates/gterminals/tests/host_lifecycle.rs::daemon_restart_preserves_live_terminal`.

    1.2.6: Only the explicit terminals-drain shutdown intent terminates the host and
    its native terminals; the next start creates a fresh epoch. test: `tests/terminals/test_host_shutdown_preservation.py::test_explicit_terminal_drain_is_the_only_host_shutdown_path`.'
  labels:
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.2:1.2.1
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.2:1.2.2
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.2:1.2.3
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.2:1.2.4
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.2:1.2.5
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.2:1.2.6
  tdd: true
  source_section: '1.2'
  implementation_domain: backend
- title: Terminal repository and epoch reconciliation
  category: code
  task_type: feature
  depends_on:
  - '1.2'
  validation_criteria: '1.3.1: Row decoding/encoding preserves backend, machine/project/session/run
    identity, host_epoch, locator, dimensions, lifecycle state, pending identity,
    and post-#22722 unresolved write/quarantine fields. test: `crates/gterminals/tests/reconcile.rs::terminal_row_round_trips_all_runtime_fields`.

    1.3.2: Inventory entries matching durable id and spawn key promote pending rows
    to live in the observed epoch; replaying the same inventory is idempotent. test:
    `crates/gterminals/tests/reconcile.rs::matching_inventory_promotes_once`.

    1.3.3: Missing inventory settles stale pending attempts and marks only same-machine,
    same-epoch live rows exited/orphaned according to the existing evidence rules;
    it never mutates another machine or a row rebound to a new epoch. test: `crates/gterminals/tests/reconcile.rs::absence_is_scoped_to_observed_machine_and_epoch`.

    1.3.4: Duplicate/foreign durable identities are killed or rejected exactly as
    the Python oracle specifies, while a reachable refused-adoption host is not reconciled
    at all. test: `crates/gterminals/tests/reconcile.rs::identity_conflicts_fail_closed_without_touching_refused_host`.

    1.3.5: Prepared-process settlement and its terminal/run evidence are atomic: injected
    failure at every former boundary leaves either the complete pre-state or complete
    post-state, never an untracked child or live row without evidence. test: `crates/gterminals/tests/reconcile.rs::spawn_settlement_is_atomic_across_failure_boundaries`.'
  labels:
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.3:1.3.1
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.3:1.3.2
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.3:1.3.3
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.3:1.3.4
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.3:1.3.5
  tdd: true
  source_section: '1.3'
  implementation_domain: backend
- title: Attachment leases and ordered lifecycle publication
  category: code
  task_type: feature
  depends_on:
  - '1.3'
  validation_criteria: '1.4.1: Attach/detach/take/release/finalize permits one controller,
    increments generation, and returns the same deterministic control outcomes as
    Python. test: `crates/gterminals/tests/leases.rs::control_holder_transitions_are_deterministic`.

    1.4.2: Web wins the sizing tie over gclient, remaining viewers are re-elected
    on detach, and viewport/scroll offsets are isolated by attachment. test: `crates/gterminals/tests/leases.rs::sizing_and_scroll_are_attachment_scoped`.

    1.4.3: Write admission rejects stale attachment generation, duplicate-conflicting
    payloads, and non-holder input while replaying an identical completed write idempotently.
    test: `crates/gterminals/tests/leases.rs::write_admission_is_generation_and_payload_bound`.

    1.4.4: Every lifecycle event carries the current daemon epoch and strictly increasing
    sequence; restart begins a new epoch and clients can unambiguously discard the
    old stream. test: `crates/gterminals/tests/leases.rs::lifecycle_order_is_epoch_scoped`.

    1.4.5: A full/failed lifecycle queue leaves the lease state unchanged, closes
    the affected connection through one fault path, and never drops a holder-loss
    event silently. test: `crates/gterminals/tests/leases.rs::publication_failure_is_atomic_and_fail_closed`.'
  labels:
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.4:1.4.1
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.4:1.4.2
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.4:1.4.3
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.4:1.4.4
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.4:1.4.5
  tdd: true
  source_section: '1.4'
  implementation_domain: backend
- title: Post-#22722 fail-closed write coordinator
  category: code
  task_type: feature
  depends_on:
  - '1.3'
  - '1.4'
  validation_criteria: '1.5.1: Native and tmux rows dispatch through their own runtime;
    unavailable/unknown backend fails closed and no global/config default can redirect
    a row. test: `crates/gterminals/tests/write_coordinator.rs::runtime_is_resolved_from_terminal_row`.

    1.5.2: The coordinator serializes per terminal, revalidates row/lease generation
    after lock acquisition, and refuses stale controllers before bytes are emitted.
    test: `crates/gterminals/tests/write_coordinator.rs::stale_lease_never_emits_bytes`.

    1.5.3: Same action key plus same payload replays the recorded outcome; same key
    plus different payload conflicts; timeout/ambiguous dispatch persists unresolved
    evidence and does not retry. test: `crates/gterminals/tests/write_coordinator.rs::idempotency_and_uncertainty_are_fail_closed`.

    1.5.4: Successful confirmed writes clear only their matching unresolved item;
    a new uncertain write replaces/preserves its own evidence atomically; operator
    input releases automatic quarantine without erasing unrelated evidence; terminal
    exit clears all terminal-local state. test: `crates/gterminals/tests/write_coordinator.rs::each_evidence_mutation_path_is_explicit`.

    1.5.5: Native wake batches and multi-step sequences preserve #22722''s fail-closed
    boundaries, stop after the first uncertain/failing step, and report the exact
    step with its accepted, uncertain, or typed-refusal outcome. test: `crates/gterminals/tests/write_coordinator.rs::batch_and_sequence_stop_at_first_uncertain_step`.'
  labels:
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.5:1.5.1
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.5:1.5.2
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.5:1.5.3
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.5:1.5.4
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.5:1.5.5
  tdd: true
  source_section: '1.5'
  implementation_domain: backend
- title: Remove backend authoring and make terminal lifetime explicit
  category: code
  task_type: feature
  depends_on:
  - '1.9'
  validation_criteria: '1.6.1: `TerminalConfig`, installed config, and the regenerated
    runtime contract have no `default_backend`; config containing it is rejected as
    unknown. test: `tests/terminals/test_backend_selection.py::test_default_backend_config_is_removed`.

    1.6.2: Agent-definition create, update, sync/upsert, and legacy resolution all
    reject a top-level `terminal_backend` with one stable validation error. test:
    `tests/agents/test_backend_ingress.py::test_every_agent_definition_write_and_read_rejects_backend`.

    1.6.3: MCP, HTTP, CLI, dispatch, and scheduler paths expose no backend selector
    and forward no backend string; literal legacy MCP/HTTP/dispatch payloads are rejected.
    test: `tests/agents/test_backend_ingress.py::test_every_public_spawn_ingress_rejects_backend`.

    1.6.4: Every bare/public agent producer shape materializes `SpawnRequest.terminal_lifetime`
    as literal `run`; omission has the observable run result in the model rather than
    leaking a public `auto`/backend sentinel. test: `tests/agents/test_backend_ingress.py::test_all_spawn_request_producers_emit_run_lifetime`.

    1.6.5: `WorkspaceOps._fill` reads the durable `WorkspacePane.role` that 1.9 adds:
    a non-empty role is the only `persistent_role` producer, while an empty or absent
    role emits `run`. Neither value changes native-first selection; the discriminator
    only governs whether fallback is legal. test: `tests/terminals/test_workspace_ops.py::test_role_bound_pane_is_persistent_and_bare_pane_is_run`.

    1.6.6: A post-change literal sweep has no production `terminal_backend` authoring
    field or `default_backend`; remaining backend occurrences are row/runtime observation
    or the internal selected result. behavior: `backend-option-consumer-sweep`.'
  labels:
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.6:1.6.1
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.6:1.6.2
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.6:1.6.3
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.6:1.6.4
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.6:1.6.5
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.6:1.6.6
  tdd: true
  source_section: '1.6'
  implementation_domain: backend
- title: Bounded tmux fallback, durable visibility, and automatic recovery
  category: code
  task_type: feature
  depends_on:
  - '1.2'
  - '1.3'
  - '1.6'
  - '1.9'
  validation_criteria: "1.7.1: A healthy/adopted host always selects gterm. Exactly\
    \ `host_start_timeout`, semantic `epoch_refused`, and `gterm_missing` may select\
    \ tmux for `run`; separate fixtures prove token, pid, protocol, auth, pre/post-adoption\
    \ I/O, capacity, child prepare/commit, database, write, and arbitrary native subtypes\
    \ fail closed. test: `tests/agents/test_spawn_executor.py::test_tmux_fallback_subtype_matrix_is_exhaustive`.\n\
    1.7.2: `persistent_role` plus any eligible reason fails before message, row creation,\
    \ or tmux launch and returns a diagnostic naming native unavailability and the\
    \ reason. This holds for a direct internal request and for `WorkspaceOps._fill`\
    \ when 1.9's durable `WorkspacePane.role` is non-empty. test: `tests/agents/test_spawn_executor.py::test_persistent_role_refuses_tmux_fallback`.\
    \ test: `tests/terminals/test_workspace_ops.py::test_role_bound_pane_refuses_tmux_fallback`.\n\
    1.7.3: For a run fallback, the project-scoped message is durably enqueued before\
    \ row/process creation and says \u201Cattempting\u201D until activation; enqueue\
    \ failure creates nothing. An injected failure after enqueue but before pending-row\
    \ creation leaves no row/process, while failures after pending-row creation at\
    \ prepare, commit, or promotion terminate the spawn key and attempt-CAS the pending\
    \ row to exited, so no pending/live row or process survives. The attempt message\
    \ and any resulting live row carry the same stable reason and `backend=tmux`.\
    \ test: `tests/agents/test_spawn_executor.py::test_fallback_announcement_is_attempt_until_activation`.\
    \ test: `tests/agents/test_spawn_executor.py::test_fallback_failure_after_pending_row_settles_and_kills`.\n\
    1.7.4: The live tmux row carries `backend=tmux` and `locator.fallback_reason`\
    \ after promotion; HTTP/WS list output contains backend for native and tmux rows\
    \ and never derives it from logs. test: `tests/servers/test_terminals_routes.py::test_terminal_inventory_exposes_backend_and_fallback_reason`.\n\
    1.7.5: Every terminal-list surface identifies the backend: HTTP list/get, WS `terminal.list`,\
    \ gclient live pane/sidebar/orphan rows, and the web terminal session list. test:\
    \ `crates/gclient/tests/client_loop.rs::terminal_inventory_renders_each_backend`\
    \ and test: `web/src/components/activity/terminal/__tests__/TerminalSessionList.test.tsx::rendersBackendForEverySession`.\n\
    1.7.6: Host recovery changes no live tmux row. The next ordinary spawn uses gterm\
    \ without operator action; restart/handoff creates a new native terminal before\
    \ the old run-scoped tmux terminal exits, while an in-place live migration is\
    \ impossible. test: `tests/agents/test_spawn_executor.py::test_recovery_is_new_spawn_and_handoff_only`."
  labels:
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.7:1.7.1
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.7:1.7.2
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.7:1.7.3
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.7:1.7.4
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.7:1.7.5
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.7:1.7.6
  tdd: true
  source_section: '1.7'
  implementation_domain: backend
- title: Rust tmux runtime parity for live fallback rows
  category: code
  task_type: feature
  depends_on:
  - '1.3'
  - '1.5'
  validation_criteria: '1.8.1: Raw input, literal text, named keys, and bracketed
    paste preserve current byte/size and delivered/indeterminate/partial-failure semantics
    on the row''s own tmux socket, including SGR mouse input used for tmux copy-mode
    scrolling. test: `crates/gterminals/tests/tmux_runtime.rs::input_and_write_match_python_on_recorded_socket`.

    1.8.2: Resize uses manual window sizing; bounded and full snapshots preserve text,
    history bounds, and truncation metadata for a live or remain-on-exit row. test:
    `crates/gterminals/tests/tmux_runtime.rs::resize_and_snapshot_preserve_tmux_history_contract`.

    1.8.3: Attach locator preserves socket path, pane id, server pid/start time, and
    resolves the live frame host epoch/socket directory at call time rather than caching
    either generation. test: `crates/gterminals/tests/tmux_runtime.rs::attach_locator_uses_live_host_generation`.

    1.8.4: Pane-process liveness and tmux-session presence are distinct; termination
    addresses the recorded socket and failed termination leaves the row eligible for
    existing orphan retry rather than reporting success. test: `crates/gterminals/tests/tmux_runtime.rs::liveness_presence_and_termination_are_socket_scoped`.

    1.8.5: Native terminal-family mode routes every live tmux row operation through
    this adapter and preserves its backend, locator, and lifecycle; no gclient production/runtime
    or protocol change is introduced. behavior: `native-family-live-tmux-runtime-parity`.'
  labels:
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.8:1.8.1
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.8:1.8.2
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.8:1.8.3
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.8:1.8.4
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.8:1.8.5
  tdd: true
  source_section: '1.8'
  implementation_domain: backend
- title: Durable pane role column and workspace-op argument
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '1.9.1: Migration 456 adds nullable `workspace_panes.role`
    with the non-empty and byte-ceiling check, and every schema identity carrier agrees:
    `cargo test -p gobby-core --lib grant::tests` without features, `cargo test -p
    gobby-core --features postgres --test schema_contract`, and `cargo test -p gobby-core
    --features postgres --test catalog_manifest_freshness` run without `UPDATE_GCORE_SCHEMA_MANIFEST`
    all pass. file: `crates/gcore/assets/schema/migrations/456_workspace_pane_role.sql`.

    1.9.2: `create_tab` and `add_pane` persist a supplied role, every pane read returns
    it, and an omitted role stores NULL. test: `tests/storage/test_workspaces.py::test_pane_role_persists_and_defaults_null`.

    1.9.3: A pane with no role serializes with no `role` key on both the snapshot
    and event paths, and a pane with a role serializes it. test: `tests/storage/test_workspaces.py::test_pane_to_dict_omits_null_role`.

    1.9.4: The `tab.create` and `pane.split` ops accept an optional role, store it
    on the new pane, and refuse an empty string before storage. test: `tests/terminals/test_workspace_ops.py::test_tab_create_and_pane_split_carry_role`.

    1.9.5: The canonical workspace golden replies replay unchanged. test: `tests/servers/test_terminal_ws_golden.py::test_workspace_emitters_match_golden_replies`.'
  labels:
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.9:1.9.1
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.9:1.9.2
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.9:1.9.3
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.9:1.9.4
  - covers:daemon-side-gterm-adoption-and-terminal-ws:1.9:1.9.5
  tdd: true
  source_section: '1.9'
  implementation_domain: backend
- title: Backend-neutral terminal WS codec and canonical golden replay
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  - '1.2'
  - '1.3'
  - '1.4'
  - '1.5'
  - '1.6'
  - '1.7'
  - '1.8'
  - '1.9'
  validation_criteria: '2.1.1: Every manifest case is discovered and Rust encode/decode
    output is byte-identical to the canonical fixture; a missing/unread manifest case
    fails the test. test: `crates/gterminals/tests/ws_golden.rs::canonical_terminal_ws_corpus_replays_byte_identically`.

    2.1.2: Fragmentation/reassembly preserves the canonical wrapped event and rejects
    excessive, duplicate, missing, expired, or cross-message fragments at the existing
    limits. test: `crates/gterminals/tests/ws_golden.rs::fragment_limits_and_identity_match_python`.

    2.1.3: Terminal pages preserve stable cursor ordering and backend on every inventory
    item, enforce safe integers and encoded-page ceiling, and produce the same next
    cursor as Python. test: `crates/gterminals/tests/ws_golden.rs::inventory_pagination_matches_python`.

    2.1.4: Paste/write/lifecycle queue constants and failure envelopes match the existing
    backend-neutral WS contract; no gterm/gclient/protocol source or fixture byte
    changes. behavior: `terminal-ws-wire-scope-guard`.'
  labels:
  - covers:daemon-side-gterm-adoption-and-terminal-ws:2.1:2.1.1
  - covers:daemon-side-gterm-adoption-and-terminal-ws:2.1:2.1.2
  - covers:daemon-side-gterm-adoption-and-terminal-ws:2.1:2.1.3
  - covers:daemon-side-gterm-adoption-and-terminal-ws:2.1:2.1.4
  tdd: true
  source_section: '2.1'
  implementation_domain: backend
- title: 'Native ownership cutover: write route, action hold, runtime seam, and settlement'
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  - '1.2'
  - '1.3'
  - '1.4'
  - '1.5'
  - '1.6'
  - '1.7'
  - '1.8'
  - '1.9'
  validation_criteria: '2.4.3: Startup has exactly one supervisor in every mode, and
    a runtime route-mode change is rejected with restart-required before altering
    ownership. test: `crates/gterminals/tests/family_cutover.rs::cutover_requires_restart_and_has_one_owner`.

    2.4.7: In `Native`, every Python coordinator caller reaches the Rust coordinator
    through `RustWriteCoordinatorClient`, and #22722''s submit regressions pass against
    it: an unreadable composer draft is never reported as confirmed success (#23188''s
    contract), and a held verified submit retries without a second write, and `submit_coordinated_text`
    runs its unchanged ladder inside `logical_action_lock`. test: `tests/terminals/test_coordinator_client.py::test_pane_io_submit_regressions_hold_against_rust_coordinator`.

    2.4.8: The write route refuses a missing or wrong token; the token never appears
    in the child''s environment or argv and rotates on every start; a serve without
    `GOBBY_PARENT_FD` exposes no write route; a write issued before the child is attached,
    or after it stops, is refused. test: `crates/gterminals/tests/family_cutover.rs::write_route_requires_the_per_start_parent_pipe_token`.
    test: `tests/test_runner_front_door.py::test_write_token_travels_only_over_the_parent_pipe`.

    2.4.12: In `Native`, constructing `TerminalHostManager` is refused, and agent
    spawn, web-workspace spawn, snapshot reads, and termination run through `RustNativeRuntimeClient`.
    test: `tests/terminals/test_runtime_client.py::test_native_consumers_use_the_rust_runtime_client`.

    2.4.13: In `Native`, agent, retry, placed, and web/workspace spawns and a stale-pending
    reap make zero Python terminal-row writes and the Rust repository settles each
    row; `Proxy` keeps the Python settlement path unchanged. test: `tests/terminals/test_runtime_client.py::test_native_settlement_has_one_rust_owner`.

    2.4.14: While a hold is active, unheld route operations queue in arrival order
    and flush in order on release; at 128 items, 256 KiB, or a 30 s oldest-item age,
    new input is refused with an explicit typed error and no queued item is discarded;
    a reply carrying a superseded hold token neither completes nor releases the newer
    hold. test: `crates/gterminals/tests/family_cutover.rs::route_hold_queue_is_bounded_and_token_scoped`.

    2.4.15: TTL expiry, timeout, or disconnect during an admitted host write records
    uncertainty only: the next queued effect is not admitted before completion or
    proof (pane or terminal destroyed, host epoch changed, host process exited, or
    an operator close or kill), and a delayed effect that lands after the uncertainty
    is persisted is not replayed and releases no newer hold. test: `crates/gterminals/tests/family_cutover.rs::uncertain_hold_admits_nothing_before_proof`.

    2.4.16: In `Native`, a workspace pane binds only after Rust returns the settled
    terminal id, with no Python terminal-row write; a Rust settlement failure binds
    no pane. test: `tests/terminals/test_workspace_ops.py::test_native_pane_binds_after_rust_settlement`.
    test: `tests/terminals/test_workspace_ops.py::test_native_settlement_failure_binds_no_pane`.

    2.4.17: A startup that selects `Native` while the terminal family is unregistered
    is refused before any ownership change, and `Proxy` and `Compare` keep the Python
    host manager, lease registry, and write coordinator. test: `crates/gterminals/tests/family_cutover.rs::native_activation_refuses_without_the_terminal_family`.'
  labels:
  - covers:daemon-side-gterm-adoption-and-terminal-ws:2.4:2.4.3
  - covers:daemon-side-gterm-adoption-and-terminal-ws:2.4:2.4.7
  - covers:daemon-side-gterm-adoption-and-terminal-ws:2.4:2.4.8
  - covers:daemon-side-gterm-adoption-and-terminal-ws:2.4:2.4.12
  - covers:daemon-side-gterm-adoption-and-terminal-ws:2.4:2.4.13
  - covers:daemon-side-gterm-adoption-and-terminal-ws:2.4:2.4.14
  - covers:daemon-side-gterm-adoption-and-terminal-ws:2.4:2.4.15
  - covers:daemon-side-gterm-adoption-and-terminal-ws:2.4:2.4.16
  - covers:daemon-side-gterm-adoption-and-terminal-ws:2.4:2.4.17
  tdd: true
  source_section: '2.4'
  implementation_domain: backend
```
