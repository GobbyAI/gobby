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
  `run`. After #22691 supplies nullable `workspace_panes.role`, the exact role-bound producer is
  `WorkspaceOps._fill`: a non-empty durable `WorkspacePane.role` emits `persistent_role`, while a
  missing/empty role emits `run`. Persistent roles fail rather than enter tmux; #22691 is a
  concrete external dependency of the leaves that consume that field.
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

The live task graph and task rows were checked on 2026-09-22. Exact Stage 0 evidence:

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

**Global implementation gate:** no implementation leaf from either milestone may be claimed or
started until #22722 is closed with its landed commit and Program Director restart #8 is recorded
complete. The implementation ports #22722's final fail-closed WriteCoordinator behavior; it must
not port the pre-#22722 snapshot. Expansion attaches #22722 to the M1 roots so all M2 work inherits
the gate through phase dependencies. Restart #8 is a release precondition checked before the
first implementation claim, not a code workaround.

**#21558 disposition:** #21558 remains the epic blocker during planning. On expansion, stage
automation must first add #21558 as a blocker to every M2 leaf (sections 2.1–2.4), verify that no
M1 leaf (sections 1.1–1.8) carries that edge, and only then remove the epic-level #21558 → #21565
edge through `gobby-tasks`. This add-before-remove ordering prevents an unblocked M2 window and
does not bypass task lifecycle with SQL, REST, the operator CLI, or hand-authored manifest data.
M2 stays in scope; it is not a deferral.

**#22691 role-field dependency — restraint rung 2 (reuse its durable owner):** #22691,
`Runbooks: role-bound multi-agent tabs loaded from gclient`, owns migration 446 and the
`WorkspacePane.role` / `workspace_panes.role` storage and workspace-operation contract. This plan
owns only the terminal-lifetime consumption of that field in `WorkspaceOps._fill` and
`spawn_web_terminal`. On expansion, stage automation attaches #22691 as an external blocker to
the generated 1.6 and 1.7 leaves. No other M1 leaf waits for #22691, and this dependency does not
introduce #21558 into M1.

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
  write and resolves the runtime from the row's backend.
- The task's original golden path is stale. The canonical corpus is
  `tests/fixtures/terminal_ws_golden/manifest.json`, replayed by
  `tests/servers/test_terminal_ws_golden.py` and `crates/gclient/tests/ws_golden.rs`.
- `TerminalManager` already stores `backend`, `host_epoch`, and backend-owned `locator` JSON;
  `ws_protocol.inventory_item`, `/api/terminals`, gclient inventory/pane/orphan rendering, and the
  web terminal session list already carry or display backend.
- The exact current backend-option producer sweep covers agent spawn models/executor, MCP
  factory/implementation/request, HTTP agent spawn, CLI agents, dispatch actions/spawn, scheduler
  execution, ask agents, config, and agent-definition JSON storage/resolution.
- #22691 owns the future durable `workspace_panes.role` column and `WorkspacePane.role` projection;
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
- `crates/gdaemon/src/serve.rs`
- `crates/gdaemon/src/front_door/routes.rs`
- `src/gobby/runner_init/terminal_wiring.py::init_terminal_wiring`
- `src/gobby/terminals/host_manager.py::*` — scope-reason: extract shutdown helpers and remove config-driven drain while preserving the Proxy-mode manager
- `src/gobby/terminals/host_shutdown.py`
- `tests/terminals/test_host_shutdown_preservation.py::*` — scope-reason: replace the contradictory config-opt-in case with the invariant and explicit-drain cases
- `tests/terminals/test_host_manager.py::*` — scope-reason: pin the extracted Python shutdown seam while Proxy mode remains available
- `crates/gterminals/tests/host_lifecycle.rs`

Research context:

- `TerminalHostManager._try_adopt`, `_spawn_candidate`, `_health_loop`, and
  `stop(drain_host=False)` are the behavior oracle. The manager is 909 lines, so it is not edited
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
- **Choice — restraint rung 1:** delete `stop_host_on_shutdown`; it is the only non-explicit path
  that violates restart preservation. Preserve the existing explicit drain intent.
- **Granularity:** the listed targets are one lifecycle leaf because the owner/observer switch, Python
  wiring exclusion, and gdaemon stop behavior must change together. Splitting them creates a
  double-supervisor or no-supervisor interval.

**Decomposition:** `host_manager.py` is 909 lines. Move the existing drain request, host-exit wait,
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
  bidirectional terminal I/O; no ordinary config can request a drain. test:
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
- **Choice — restraint rung 6:** use one in-memory registry owned by the terminal family. Leases
  are daemon-connection state and do not need a database table or distributed lock.
- **Failure-boundary check (`edge-case-coverage`):** a holder change and its lifecycle publication
  are one registry transition; queue reservation happens before the lease mutation so saturation
  cannot publish only half of the transition.

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

- This section is implemented only from the landed #22722 version of
  `src/gobby/terminals/write_coordinator.py`; the current symbols are navigation anchors, not an
  invitation to port the pre-#22722 behavior.
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
- 1.5.5 - Native wake batches and multi-step sequences preserve #22722's composer-readable,
  fail-closed boundaries, stop after the first uncertain/failing step, and report the exact step.
  test: `crates/gterminals/tests/write_coordinator.rs::batch_and_sequence_stop_at_first_uncertain_step`.

### 1.6 Remove backend authoring and make terminal lifetime explicit [category: code]

`kind: deliverable`

Targets:
- `src/gobby/config/terminals.py::TerminalConfig`
- `src/gobby/install/shared/config/config.yaml::default_backend`
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerate the TerminalConfig carrier after removing default_backend and stop_host_on_shutdown
- `src/gobby/storage/definitions/agents.py::parent_body`
- `src/gobby/agents/spawn_models.py::resolve_terminal_backend`
- `src/gobby/agents/spawn_models.py::SpawnRequest`
- `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py::create_spawn_agent_registry`
- `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py::spawn_agent_impl`
- `src/gobby/mcp_proxy/tools/spawn_agent/_terminal_contract.py`
- `src/gobby/mcp_proxy/tools/spawn_agent/_request.py::build_spawn_request`
- `src/gobby/servers/routes/agent_spawn.py::AgentSpawnRequest`
- `src/gobby/servers/routes/agent_spawn.py::create_agent_spawn_router`
- `src/gobby/servers/websocket/terminal_ws_create.py::TerminalCreateMixin._handle_terminal_create`
- `src/gobby/terminals/lifetime.py`
- `src/gobby/terminals/workspace_ops.py::WorkspaceOps._fill`
- `src/gobby/terminals/web_spawn.py::spawn_web_terminal`
- `src/gobby/cli/agents.py::spawn_agent_cmd`
- `src/gobby/dispatch/actions.py::SpawnAgentAction`
- `src/gobby/dispatch/spawn.py::spawn_agent`
- `src/gobby/scheduler/executor.py::CronExecutor._execute_agent_spawn`
- `src/gobby/ask/agents.py::ManagedAskAgents.launch`
- `tests/agents/test_backend_ingress.py::*` — scope-reason: cover every public producer and internal lifetime producer
- `tests/agents/test_local_context_setup.py::*` — scope-reason: remove legacy backend arguments from direct MCP spawn calls
- `tests/agents/test_native_spawn.py::*` — scope-reason: replace configurable backend fixtures with native-first lifetime fixtures
- `tests/agents/test_spawn_executor_droid.py::*` — scope-reason: update direct SpawnRequest construction for the internal lifetime contract
- `tests/agents/test_spawn_executor_providers.py::*` — scope-reason: update provider request fixtures and prove providers cannot choose a backend
- `tests/agents/test_srt_spawn.py::*` — scope-reason: update sandboxed spawn fixtures without exposing backend choice
- `tests/agents/test_verified_review_regressions.py::*` — scope-reason: preserve reviewed spawn regressions under native-first selection
- `tests/ask/native_probe_harness.py::*` — scope-reason: remove obsolete shutdown/backend config overrides from the probe harness
- `tests/ask/test_native_probe_harness.py::*` — scope-reason: update probe expectations for removed config keys
- `tests/ask/test_permissions.py::*` — scope-reason: update direct SpawnRequest and ask-agent fixtures
- `tests/cli/test_agents_coverage.py::*` — scope-reason: remove backend CLI-option coverage
- `tests/cli/test_cli_agents.py::*` — scope-reason: assert the CLI has no backend selector
- `tests/config/test_native_backend_flip.py::*` — scope-reason: replace configurable backend flip cases with fixed native behavior
- `tests/config/test_terminal_config.py::*` — scope-reason: assert removed keys are rejected and regenerate config expectations
- `tests/config/test_terminal_host.py::*` — scope-reason: remove ordinary-shutdown drain configuration expectations
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
- `spawn_executor.py` is already 998 lines. It remains the patchable facade per durable spawn
  architecture; this section removes forwarding code but adds no policy body there. Section 1.7
  places shared terminal selection in a dedicated support module.
- **Choice — restraint rung 1:** delete the config field, CLI option, MCP parameter, HTTP field,
  dispatch action field, and forwarding arguments. A one-value backend knob does not need to
  exist.
- **Choice — restraint rung 6:** add one internal `TerminalLifetime` discriminator to
  `SpawnRequest`, materialized as `run` by the model when omitted. Put the enum and the narrow
  role-to-lifetime helper in new `src/gobby/terminals/lifetime.py`; it is never exposed in agent
  definitions or public request schemas. #22691 owns nullable `workspace_panes.role` and its
  `WorkspacePane.role` projection. After that dependency lands, `WorkspaceOps._fill` is the exact
  role-bound producer: it passes `persistent_role` to `spawn_web_terminal` for a non-empty durable
  role and `run` for a missing/empty role. Every existing agent/public producer shape resolves to
  `run` without signature churn in unrelated provider code.
- **Granularity:** the listed production targets exceed the target heuristic but remain one atomic
  trust-boundary leaf. Leaving any producer able to emit or accept backend metadata would violate
  “not selectable” and silently reintroduce the option. Focused tests enumerate every producer
  rather than hiding the sweep.

**Decomposition:** `_implementation.py` is already 962 lines. Move agent-definition terminal
contract validation and internal lifetime derivation into new `_terminal_contract.py`; keep
`spawn_agent_impl` as the orchestrating caller and delete its backend-resolution body. This is a
real split, not a wrapper added beside unchanged code, and keeps both production modules below the
1,000-line ceiling.

`workspace_ops.py` is currently 964 lines. Move the new shared lifetime enum/derivation out of
`workspace_ops.py` into new `src/gobby/terminals/lifetime.py`; `WorkspaceOps._fill` only reads the
durable role and calls that helper before `spawn_web_terminal`. #22691 separately moves existing
pane-I/O code, so this plan neither duplicates that owned split nor grows the near-ceiling module.

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
- `src/gobby/ask/stage_runtime.py` — no-edit-reason: It calls ManagedAskAgents.launch through the unchanged signature.
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
- `tests/ask/test_pipeline.py` — no-edit-reason: Ask pipeline uses the unchanged launch signature and supplies no backend.

**Acceptance:**

- 1.6.1 - `TerminalConfig`, installed config, and the regenerated runtime contract have neither
  `default_backend` nor `stop_host_on_shutdown`; config containing either is rejected as unknown,
  while the explicit shutdown-intent `drain_terminals` path remains. test:
  `tests/terminals/test_backend_selection.py::test_default_backend_config_is_removed`.
- 1.6.2 - Agent-definition create, update, sync/upsert, and legacy resolution all reject a top-level
  `terminal_backend` with one stable validation error. test:
  `tests/agents/test_backend_ingress.py::test_every_agent_definition_write_and_read_rejects_backend`.
- 1.6.3 - MCP, HTTP, CLI, dispatch, scheduler, and ask-agent paths expose no backend selector and
  forward no backend string; literal legacy MCP/HTTP/dispatch payloads are rejected. test:
  `tests/agents/test_backend_ingress.py::test_every_public_spawn_ingress_rejects_backend`.
- 1.6.4 - Every bare/public agent producer shape materializes
  `SpawnRequest.terminal_lifetime` as literal `run`; omission has the observable run result in the
  model rather than leaking a public `auto`/backend sentinel. test:
  `tests/agents/test_backend_ingress.py::test_all_spawn_request_producers_emit_run_lifetime`.
- 1.6.5 - After external dependency #22691 lands, `WorkspaceOps._fill` reads the durable
  `WorkspacePane.role`: a non-empty role is the only `persistent_role` producer, while an empty or
  absent role emits `run`. Neither value changes native-first selection; the discriminator only
  governs whether fallback is legal. test:
  `tests/terminals/test_workspace_ops.py::test_role_bound_pane_is_persistent_and_bare_pane_is_run`.
- 1.6.6 - A post-change literal sweep has no production `terminal_backend` authoring field or
  `default_backend`; remaining backend occurrences are row/runtime observation or the internal
  selected result. behavior: `backend-option-consumer-sweep`.

### 1.7 Bounded tmux fallback, durable visibility, and automatic recovery [category: code] (depends: 1.2, 1.3, 1.6)

`kind: deliverable`

Targets:
- `src/gobby/agents/spawn_executor_terminal.py`
- `src/gobby/agents/spawn_executor.py::resolve_terminal_services`
- `src/gobby/agents/spawn_executor.py::_runtime_spawn`
- `src/gobby/storage/terminals.py::TerminalManager.create_pending`
- `src/gobby/storage/terminals.py::TerminalManager.promote_to_live`
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
  `spawn_executor_terminal.py` keeps the 998-line facade below the production ceiling and avoids
  turning the existing general support module into another terminal subsystem.
- `TerminalManager` already persists `backend` and `locator`; no migration is required. HTTP and
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

**Decomposition:** move `resolve_terminal_services`, `_default_backend`, and the new asynchronous
fallback decision/announcement flow from the 998-line facade into new
`spawn_executor_terminal.py`. `spawn_executor.py` re-exports the established patch targets and
retains only the call from `_runtime_spawn`; its net line count decreases.

Move the current service resolver into the support module and make it async so it can await the
2.5-second native readiness decision. Only the three typed host outcomes are eligible, with
`epoch_refused` restricted to the semantic subtype defined in 1.2. All token, pid, protocol, auth,
I/O, preparation, commit, capacity, write, and post-adoption failures remain fail closed. For an
eligible run, enqueue one project message containing run/session id, reason, `backend=tmux`, and
attempt status, then create the tmux pending row with the same reason and launch. Promote to live
before any surface may describe the terminal as active. Do not cache the fallback decision.

Consumers unchanged:
- `src/gobby/agents/resume_executor.py` — no-edit-reason: It reaches terminal spawning through the stable spawn_executor facade and inherits the new resolver.
- `src/gobby/terminals/host_reconcile.py` — no-edit-reason: Promotion signature remains compatible; host reconciliation preserves the locator already on the pending row.
- `src/gobby/terminals/tmux_discovery.py` — no-edit-reason: External tmux discovery is not an agent fallback and supplies its existing locator unchanged.
- `tests/agents/terminal_fixtures.py` — no-edit-reason: Existing terminal rows omit optional fallback reason and retain their behavior.
- `tests/agents/test_tmux_integration.py` — no-edit-reason: Explicit tmux runtime integration is not the agent fallback-selection path.
- `tests/hooks/test_session_start_handlers.py` — no-edit-reason: Hook-created fixture rows omit optional fallback evidence.
- `tests/servers/test_attention_native_roster.py` — no-edit-reason: Roster reads terminal backend but does not create fallback rows.
- `tests/servers/test_native_web_proxy.py` — no-edit-reason: Native proxy promotion preserves the existing locator.
- `tests/servers/test_terminal_ws_lease.py` — no-edit-reason: Lease tests consume promoted rows and do not author fallback selection.
- `tests/storage/test_terminal_bindings.py` — no-edit-reason: Binding tests use existing optional locator defaults.
- `tests/storage/test_terminals.py` — no-edit-reason: Repository tests keep valid no-reason row cases; focused fallback tests cover the new key.
- `tests/storage/test_workspaces.py` — no-edit-reason: Workspace terminal fixtures omit optional fallback evidence.
- `tests/terminals/fakes.py` — no-edit-reason: Fakes keep backward-compatible optional locator behavior.
- `tests/terminals/test_native_runtime.py` — no-edit-reason: Native runtime tests neither select nor persist tmux fallback.
- `tests/terminals/test_tmux_discovery.py` — no-edit-reason: Discovered external tmux sessions have no agent fallback reason by design.
- `tests/terminals/test_tmux_runtime.py` — no-edit-reason: Runtime tests do not exercise the native-first fallback decision.
- `tests/agents/test_run_completion.py` — no-edit-reason: Run completion consumes terminal lifecycle and is unchanged by optional fallback evidence.
- `tests/agents/watchdog/test_close_review_parked_caller.py` — no-edit-reason: Watchdog promotion fixtures retain the compatible repository signature.

**Acceptance:**

- 1.7.1 - A healthy/adopted host always selects gterm. Exactly `host_start_timeout`, semantic
  `epoch_refused`, and `gterm_missing` may select tmux for `run`; separate fixtures prove token,
  pid, protocol, auth, pre/post-adoption I/O, capacity, child prepare/commit, database, write, and
  arbitrary native subtypes fail closed. test:
  `tests/agents/test_spawn_executor.py::test_tmux_fallback_subtype_matrix_is_exhaustive`.
- 1.7.2 - `persistent_role` plus any eligible reason fails before message, row creation, or tmux
  launch and returns a diagnostic naming native unavailability and the reason. This holds for a
  direct internal request and for `WorkspaceOps._fill` when #22691's durable `WorkspacePane.role`
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
- 1.7.4 - Backend and fallback reason survive pending-to-live promotion on the row; HTTP/WS list
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

## P2: Milestone M2 — terminal WS and proxy relay [category: code] (depends: P1)

`kind: framing`

Every M2 leaf is blocked by #21558 after expansion. M2 begins only when #21558's native gdaemon
WS accept/auth/subscription/broadcast transport is available. M2 owns terminal messages and
relaying on that transport; it does not recreate transport authentication or the base event
envelope.

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

### 2.2 Native terminal WS connection and operation state machine [category: code] (depends: 2.1, 1.8)

`kind: deliverable`

Targets:
- `crates/gterminals/src/ws.rs`
- `crates/gterminals/src/family.rs`
- `crates/gterminals/tests/ws_operations.rs`

Research context:

- `TerminalWsMixin` is the operation oracle for list, attach, detach, sizing, scroll, input,
  paste, operator write, control, lease-lost fanout, and proxy attach. #21558 supplies connection
  auth/subscription/broadcast and the outer envelope.
- Runtime resolution remains per terminal row. A terminal id is backend-neutral; native rows use
  the native host runtime and live tmux rows use 1.8's complete Rust adapter for input/write,
  resize, snapshot/history, attach locator, liveness, and termination.
- **Choice — restraint rung 2:** implement these messages as one #21558 WS route handler backed by
  the M1 repository/lease/write APIs. Do not introduce a terminal-only listener or socket.

Implement one connection state owning its attachments and relay. Authorize project access before
lookup, validate each message before mutation, reserve lifecycle capacity before changing a
holder, and finalize all connection attachments exactly once on disconnect or send failure.

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
  preserves unresolved evidence, and does not degrade into tmux fallback. test:
  `crates/gterminals/tests/ws_operations.rs::write_fault_is_fail_closed_not_fallback`.

### 2.3 Bounded proxy relay and frame/lifecycle fanout [category: code] (depends: 2.2)

`kind: deliverable`

Targets:
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

### 2.4 Route-family cutover, Python-owner exclusion, and end-to-end parity [category: code] (depends: 2.2, 2.3)

`kind: deliverable`

Targets:
- `crates/gterminals/src/family.rs`
- `crates/gdaemon/src/front_door/routes.rs`
- `crates/gdaemon/src/serve.rs`
- `src/gobby/runner_init/terminal_wiring.py::init_terminal_wiring`
- `crates/gterminals/tests/family_cutover.rs`
- `tests/terminals/acceptance/test_native_lifecycle.py::*` — scope-reason: add gdaemon native-family restart, fallback, and explicit-drain acceptance

Research context:

- #21544 requires one family routing-table change. #21558 owns the listener and auth transport;
  this leaf registers terminal HTTP/WS routes and switches their family backend.
- Live `Compare` dual execution is unsafe for input/control/write. The canonical corpus and a
  captured, non-mutating decode/encode comparison supply parity without duplicating effects.
- **Choice — restraint rung 2:** use the existing family route switch and Python proxy as the
  rollback. Do not add per-operation rollout flags. The supervisor owner changes only at process
  start, so a route change requires the existing announced restart/cutover procedure.
- **Granularity:** six production/test targets cover one atomic owner-and-route cutover. Routing
  without supervisor exclusion creates two owners; exclusion without routing strands clients.

Register `/api/terminals` and terminal WS messages as one terminal family. `Proxy` keeps Python
ownership. `Compare` proxies effects and compares only normalized non-mutating output. `Native`
constructs Rust ownership and omits Python ownership. Route mode is read once during startup; a
runtime flip is rejected so existing sessions cannot be half-migrated.

Consumers unchanged:
- `src/gobby/runner_init/orchestration.py` — no-edit-reason: It calls `init_terminal_wiring` through the unchanged signature; the wiring function owns route-mode selection.

**Acceptance:**

- 2.4.1 - One terminal-family registration owns terminal HTTP and WS dispatch; `Proxy` delegates,
  `Native` serves Rust, and no request is split between owners. test:
  `crates/gterminals/tests/family_cutover.rs::terminal_family_routes_as_one_unit`.
- 2.4.2 - `Compare` never executes input, write, resize, control, spawn, terminate, or drain twice;
  it compares only canonical serialization of non-mutating captured results. test:
  `crates/gterminals/tests/family_cutover.rs::compare_mode_never_duplicates_terminal_effects`.
- 2.4.3 - Startup has exactly one supervisor in every mode, and a runtime route-mode change is
  rejected with restart-required before altering ownership. test:
  `crates/gterminals/tests/family_cutover.rs::cutover_requires_restart_and_has_one_owner`.
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

## E1 Verification

`kind: verification`

Executors run focused verification after each leaf and the complete matrix before native cutover:

1. `cargo fmt --all -- --check` and the repository Rust lint/type gate for every changed crate.
2. `cargo nextest run -p gobby-terminals`, including the live-tmux adapter contract; focused
   `-p gobby-daemon` integration tests for family composition and shutdown.
3. With isolated `DATABASE_URL` and `GOBBY_TEST_PROTECT=1`, run the changed Python test files only:
   backend ingress/selection, spawn executor, agent definitions, terminal routes/WS, host shutdown,
   and native lifecycle acceptance. Never run the full pytest suite.
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
