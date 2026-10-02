# gclient and gterm resilience under daemon load pressure

**Plan ID:** gclient-daemon-resilience

Plan artifact: `.gobby/plans/gclient-daemon-resilience.md`

## Overview
`kind: framing`

Since the tmux to gclient switch, daemon latency freezes gclient outright. Epic
#22573 (shipped 2026-09-19) removed the per-keystroke daemon round trip for held
direct native panes, but the freeze survives it because the problem is structural.
Three exploration passes over `crates/gclient`, `crates/gterminal`,
`src/gobby/servers/websocket` and `src/gobby/terminals`, plus the logs of
2026-09-20, establish the following.

gclient parks its only loop task on daemon I/O. `run_live_loop` in
`crates/gclient/src/app/live_loop.rs` is one biased `tokio::select!` on one task.
Every daemon `.await` in a branch body, or in the post-select code that runs each
iteration, stops rendering, frame consumption, control-reply delivery, reconnect and
the sidebar future together. Inline daemon awaits remain in the input path for
non-direct panes, in `release_live_control` on every focus move, in every action
chain (spawn shell is four serial round trips), in `send_focus_hints_if_changed` and
`resize_live_workspace` on every iteration, in the roster refetch inside the event
branch, in `attach_ready_panes` on the render tick, and in frame-error recovery,
which polls only input while it awaits detach plus attach. The one non-blocking
pattern that exists is `start_control_request` in `crates/gclient/src/app/live.rs`:
spawn the send, deliver the outcome on a channel, apply it in a select branch.

A slow-but-connected daemon produces no client state change. `daemon_ready` flips
only on an observed disconnect (`observe_daemon_disconnect`). No heartbeat, ping or
RTT measurement exists after launch, so a daemon answering in 1-5 s leaves the
client issuing requests, parking 5 s each, with no banner. `REQUEST_DEADLINE` is 5 s
and `CONTROL_REQUEST_DEADLINE` 2 s (`crates/gclient/src/daemon/live.rs`).

On disconnect the client discards the host grant it still holds.
`observe_daemon_disconnect` and `reconnect_daemon_ws` call `Pane::clear_control` on
every pane. The gterm grant is host memory with no TTL and survives the daemon's
control connection dropping by design (plan `gclient-direct-input.md`, decision 3:
"a daemon restart must not stop typing"). The client never honoured that.

The daemon's terminal handlers are serial per connection and do synchronous
Postgres work on the event loop. `WebSocketServer._handle_connection` awaits
`_handle_message` one message at a time, and the terminal handlers call psycopg
synchronously on the loop thread instead of through `WebSocketServer.run_db`, which
chat and HTTP routes already use. The pool acquire timeout is 5 s and
`_acquire_with_backoff` then `time.sleep`s 0.5/1/2 s between retries; on the loop
thread that is a hard block of the whole daemon. Daemon-to-gterm round trips
(`HostClient._roundtrip`) have no deadline and `TerminalLeaseRegistry.take_control`
holds the per-terminal lock across the host grant call.

The logs tie it together: 324 slow-handler warnings on 2026-09-20 (91 at 1-2 s, 63
at 2-5 s, 8 over 5 s), most often `terminal_list`, then `terminal_attach`,
`workspace_op`, `terminal_detach`, `terminal_take_control`; 48 gclient request
timeouts, all between 04:00 and 06:00 local, on `workspace_op`,
`terminal_take_control` and `terminal_attach`, never `terminal_input`. In that window
the hub pool logged `pool_available: 0, requests_waiting: 54, connections_errors:
150, pool_size: 25` against `pool_max: 64` while five codex spawns and their close
validators ran concurrently. A quiet-window sample at 11:38 (`terminal_take_control`
1.03 s, `workspace_op` 1.21 s) shows the handlers' own cost without contention.

The incident diagnosis is now split by evidence instead of treating three freezes
as one cause:

- **07:44 remains unproven.** An isolated reproduction confirms that an unanswered
  `HostClient._roundtrip` can hold `TerminalLeaseRegistry.lock(terminal_id)`, wedge
  that attachment lane and silently block later same-terminal work. No retained
  evidence proves that 07:44 took this path. D3 and D3b remove the confirmed
  mechanism; their watchdog-free barrier tests do not claim historical attribution.
- **11:58 was a daemon-wide load stall.** The concurrent pool, slow-handler and
  request-timeout evidence above applies to the whole daemon, rather than one
  gclient lane.
- **14:47 was client-side.** A deadline-less attach, roster or websocket await is
  refuted: those requests are bounded by the 5 s `REQUEST_DEADLINE`, or by the 2 s
  control and detach deadlines. A never-returned wait is refuted, the daemon
  websocket lane is refuted by cleanup completing in 225 ms, and #22675's gterm
  `write_batch` lock is a separate bug rather than the causal link. The leading
  chain remains unproven: inline attach of a new terminal, then serial recovery of
  direct panes retired for `RetireReason::Lag`, then a detach reply exceeding 2 s
  and escalating to reconnection of the healthy socket, followed by inline
  reconcile/re-attach of every pane and inline REST. The same shape appeared at
  14:07; the 13:09 slow attach did not freeze. P1 remains the vehicle because it
  removes every daemon await from the gclient loop regardless of which bounded
  steps composed the stall.

gterm does not dial the daemon; PTY delivery is `try_send`, frames come off a 30 ms
ticker, and `input_activity` fan-out drops a slow subscriber. It does need the P3
change that removes daemon grant issuance as a prerequisite for local native input:
the existing frame socket supplies native inventory and grants one attached local
client fallback authority only while no daemon control owner and no current holder
exist. Daemon grants replace that fallback when daemon arbitration returns.

Intended outcome: a slow, absent, erroring or restarting daemon degrades only the
enhancements it owns. gclient and gterm still launch, enumerate local native panes,
attach, render, scroll and type without it. Lease coordination and takeover, layout
and workspace sync, roster, attention and agent relay are unavailable; tmux, web
and proxied panes remain daemon-bound. The status line states that boundary.
Daemon-side, one slow terminal handler no longer stalls the other panes on the same
window or the other connections on the daemon.

**Status at 0.5.0 `7d33430cc2` (2026-09-27 reconciliation).** Work landed outside
this plan after the 2026-09-22 draft; this amendment records it instead of
re-planning it. #22677 closed the detach-deadline repair (X1, `3ef74716ce`). #22747
moved proxy frame recovery beside the loop (`fa3435cf1a`: `FrameRecovery` and the
inner select are deleted and each detach/attach step is a `RecoveryFuture` the loop
polls) and moved the `Lagged` sidebar refetch and terminal relist beside the loop
(`830cc89544`, `2acd8efef5`); the render-tick attach retry issues through
`start_due_attaches` (`ee9bb018b3`). #22755 staged launch and reconnect as startup
jobs (`3f477ce2ac`, `cd49e8d406`). #22709 and #22877 moved the P4 storage work off
the loop with `asyncio.to_thread` and made websocket dispatch concurrent per
terminal chain. Those slices are completed-prerequisite framing records citing
the closed owner tasks and their commits (X1, X2, X3, D1b, D1c, D1d, D2). Still open: the inline daemon awaits left
in `run_live_loop` (focus hints, geometry, input and control, action chains
including #22883's gclient-owned shell adoption, event-driven roster, attention and
open refetches, and the remaining `attach_ready_panes().await` callers), every WARN
logging gap, P2, P3, D1a, D3, D3b, D4 and E1. Sizes in this amendment are raw
`wc -l` counts on 0.5.0 `7d33430cc2`; the validator's production count, which stops
at the first `#[cfg(test)]` for Rust, decides what is near the ceiling.

## Decision Record
`kind: framing`

Josh, 2026-09-20:

1. A held direct native pane keeps typing through a daemon disconnect or restart.
   gclient keeps Held on direct-granted panes, shows the lease as unconfirmed, and
   re-takes on reconnect. Proxied, tmux and web panes still drop to observe.
2. Daemon-side scope is all three: terminal handlers' Postgres work moves onto the
   DatabaseExecutor, websocket messages dispatch concurrently per connection with
   per-attachment ordering, and HostClient round trips get a bounded deadline.
   (Status 2026-09-27: the shipped D1b, D1c and D1d run their storage through
   `asyncio.to_thread` rather than `WebSocketServer.run_db`; both keep psycopg off
   the loop thread, and D1a follows the shipped mechanism. The shipped D2 chains
   key on terminal first, then attachment, with one workspace lane; see D2.)
3. Pool exhaustion gets a diagnosis leaf (attribute the next incident), not a
   capacity redesign.
4. Josh, 2026-09-21, verbatim: "the daemon should enhance gclient/gterm. it should
   still work degraded without it." and "typing does not need to be dependent on
   the daemon at all. gterm/gclient should function degraded absent the daemon."
   Every deliverable is checked against both statements.
5. Without the daemon, gclient uses the already-authenticated local frame socket to
   list and attach native gterm panes. `BindAttachment` claims the empty local
   fallback holder only when gterm has no authenticated daemon control owner and no
   current grant. The first local holder retains input until that frame attachment
   detaches; another local client observes `InputRefused` and cannot take over.
   When the daemon returns, `grant_input` replaces the fallback holder and restores
   normal lease/takeover arbitration. **Restraint rung 2:** extend the existing
   frame protocol, `TerminalSlot.input_grant` and refusal path; add no second socket,
   control service, election protocol or TTL.
6. The daemon enhances native panes with leases and takeover, layout and workspace
   sync, roster, attention and agent relay. Those features degrade when it is down
   or absent. tmux, web and proxied panes remain daemon-bound and unavailable.
   Direct-native `Input` and `Paste` always travel on the bound gterm frame stream;
   D1a's removal of the per-key terminal-row lookup remains the sole optimisation
   to the daemon-mediated tmux/web/proxy input path.
7. Josh, 2026-09-21: P1 is the vehicle for reducing gclient's blocking dependency
   on the daemon. The split-right incident is evidence for the priority, not a
   proven root cause. The plan closes the ordinary WARN gaps around frame failure,
   recovery give-up, unresolved opens, lag, REST and reconnect, but adds no stack
   sampler; the historical root cause remains unproven.
8. Josh, 2026-09-21: add no daemon or client-loop watchdog. The D2 per-lane
   one-shot handler watchdog, Q7b loop phase-label watchdog and B2 idle keepalive
   are removed. D2 keeps ordered concurrent lanes, while D3/D3b use bounded
   operations and test-only barriers for diagnosis. **Restraint rung 1:** these
   timer-driven probes do not need to exist to meet the liveness or diagnostic
   requirements and would add latency/noise.

Plan Writer gobby#14578, 2026-09-27, proposed with the reconciliation and pending
Josh's approval:

9. Attach and frame recovery stay on the `RecoveryFuture` seam #22747 and #22755
   shipped in `crates/gclient/src/app/live_attach.rs` (futures the loop polls;
   the render-tick retry issues through `start_due_attaches`). The remaining
   inline `attach_ready_panes().await` callers mark panes due for that seam
   instead of gaining a second attach path in `jobs.rs`, and C3's direct-first
   retry lands in `begin_proxy_recovery`. **Restraint rung 2:** reuse the shipped
   seam.
10. The shared frame broadcast stays as it is: one
    `broadcast::channel(BROADCAST_CAPACITY)` in
    `crates/gclient/src/daemon/live_connect.rs`, with `ProxyFrameSource::recv`
    skipping other panes' frames. #22747's release-build capture over 13 windows
    showed zero lag, zero recovery and a worst iteration of 24 ms; lag appeared
    only in the debug build. A3.6 logs every `RetireReason::Lag` retirement, so a
    recurrence is visible in `gclient.log`. Rejected: per-attachment routing (a
    channel per attachment plus a router with no measured release-build need)
    and coalesced frame draws (changes every pane's render cadence to fix a
    debug-only symptom). **Restraint rung 1:** the release build does not need
    either.

Coordinator decision (gobby#14018, 2026-09-20): the near-ceiling gclient files are
decomposed first in one behaviour-neutral refactor leaf (R1) so that every later
deliverable edits a file with headroom; the planning draft's per-leaf splits fold
into it.

## Constraints
`kind: framing`

Scheduling. The prerequisites this epic waited on have landed. #22635 and #22637
(gclient key encoding and scrollback copy, both also touching gterm) closed on
2026-09-20. #22617 (the fourteen dirty gclient files in the task-22616 worktree)
and epic #22627 (post-reboot daemon terminal handler fixes) closed on 2026-09-21.
P1-P3 rewrite `live_loop.rs`, `live.rs`, `live_attach.rs`, `control.rs`,
`actions.rs` and the gterm frame/grant seam on top of that landed code, and P4
touches `terminal_ws.py`. Cross-plan: #22904 placed-agent launch (section 1.5)
splits `src/gobby/terminals/native_runtime.py` into `NativeHostProbeMixin`, and D3
and E1 here split the same module. The Program Director serializes those
implementation edits, and the second to land rebases onto the first.

Shared clean-cutover gate. No implementation leaf promotes a live binary by itself.
After all P1-P4 commits and focused validation are green, announce the cutover with
one `global` `gobby-agents:send_message` and wait until no spawned worker or close
validator is live. Entrypoints on 0.5.0 `8965cc963f`:

- Release versioning (Josh's rule, memory `8802df5d`): every crate binary this
  epic releases gets a +0.0.1 patch bump in its `Cargo.toml`, with the matching
  `Cargo.lock` update, in the commit that ships the release. A crate is bumped
  only when it is actually released. Here that is `gobby-client` (P1-P3),
  `gobby-terminal` (P3), and the `gcode`/`gdaemon`/`ghook` crates when a gcore
  input such as the runtime-config carrier changes (D3).
- Coherent trio: `gobby cutover` (`src/gobby/cli/cutover.py::run_cutover`) builds
  `gcode`/`gdaemon`/`ghook`, proves the start half (worktree guard, installed set,
  schema identity, read-only `gdaemon schema plan`), promotes through
  `src/gobby/install/bin_set_coherence.py::promote_workspace_binary_set` and
  restarts the daemon from the main checkout. It refuses uncommitted schema
  inputs, and this epic never passes `--allow-dirty`. Any change to the gcore
  runtime-config carrier counts as an input change to the trio.
- gclient: `cargo build --release -p gobby-client`, then promote it separately
  through `src/gobby/cli/install_setup_gclient.py::install_gclient_from_submodule`,
  because `promote_workspace_binary_set` rejects gclient (memory `8303e661`).
  Restart each validation gclient so it execs the new inode.
- gterm (P3): `cargo build --release -p gobby-terminal --features vt-engine --bin
  gterm`, promote it through
  `src/gobby/cli/install_setup_gterm.py::install_gterm_from_submodule`, and restart
  it before relaunching the validation gclient.
- Read the installed identity stamp and hashes from `~/.gobby/bin/`, never from
  `target/release/`.

**Restraint rung 2:** reuse `gobby cutover` and the repository's native promotion
functions and batch one cutover; add no installer or promotion path.

Implementer traps for P1 (recorded from the research pass):

- Never `abort()` a job. Dropping a control future after its write started
  tombstones the attachment (`LiveDaemon::request`, `ControlScopeIndeterminate`).
  Jobs are never cancelled; stale outcomes are dropped at apply time.
- A dropped stale outcome must still settle the ledger and clear a matching
  `in_flight_write` or `control_request`. An `Err` outcome from an older
  generation is a no-op: no toast, no `UncertainReadOnly`.
- The outcome branch sits under `control_rx` and above `input` in the biased
  select, never above the exit signal (same reasoning as #22507).
- Issue structs are `Send + 'static` (daemon clone, ids, prebuilt JSON, `PathBuf`);
  `Chrome` is never captured. Placement ops are computed from `Chrome` at apply
  time. Add `assert_send::<JobOutcome>()` at compile time.
- `crates/gclient/src/app/mod.rs` gains only `mod` and `pub use` lines;
  `crates/gclient/src/daemon/mod.rs` gains only a `mod` line. New loop tests go in
  `crates/gclient/tests/loop_liveness.rs`, never in `client_loop.rs`.
- Direct `SetViewport` through `send_input` reports `Backpressure` on the status
  line, not as a toast.
- Load the `rust` skill before editing; `crates/AGENTS.md` sets the
  `<module>/tests.rs` convention and the 1,000-line ceiling. gclient module
  placement follows memory `d71c4299`; loop-test ordering follows memory
  `679bf344` (wait for the mock-visible request before asserting state).
- Render characterisations in `crates/gclient/tests/parity/chrome.rs` and
  `crates/gclient/tests/fixtures/screens/*.txt` change only if status-line text
  changes; regenerate with `GOBBY_UPDATE_SCREENS=1` in the same commit (memory
  `f5164d15`).
- `docs/guides/gclient-user-guide.md` must track code (memory `392cc53f`).

Out of scope: the Postgres capacity model (`docs/contracts/database-concurrency-v1.json`)
and what consumed the direct-connection reserve during the 04:58 exhaustion;
changing `PROTOCOL_VERSION`; changing the daemon keystroke path for tmux, web and
proxied panes beyond D1a's per-key terminal-row SELECT removal; the D2 per-lane
one-shot handler watchdog; the Q7b loop phase-label watchdog; re-adding an
event-loop lag watchdog; reducing `REQUEST_DEADLINE`; and duplicating #22677's
detach-deadline repair.

## X1 Detach-deadline repair (completed prerequisite)
`kind: framing`

Task #22677 owned the detach-deadline implementation and its five validation
criteria. It closed with `3ef74716ce` (merge `8868e754bb`), so A3's precondition is
met. A3 moves that repaired state machine into the job seam without redefining its
contract. #22677 is a completed foreign prerequisite recorded here as evidence,
not a deferral of this plan. **Restraint rung 2:** reuse the delivered repair
instead of duplicating its tests or a manifest leaf.

## X2 P1 slices shipped by #22747 (completed prerequisite)
`kind: framing`

#22747 (closed 2026-09-25) landed the frame-recovery slice of A3 and the `Lagged`
slice of A4. `fa3435cf1a` deletes `FrameRecovery` and the inner input-only select;
each recovery detach and attach step is a `RecoveryFuture` the loop polls
(`begin_frame_recovery`, `apply_frame_recovery` and `begin_recovery_attach` in
`crates/gclient/src/app/live_attach.rs`). `830cc89544` and `2acd8efef5` run the
`Lagged` arm's sidebar refetch and terminal relist beside the loop behind a seq
guard (`crates/gclient/src/app/live/relist.rs`). `ee9bb018b3` issues the render-tick
attach retry through `start_due_attaches`, covered by
`crates/gclient/tests/client_loop.rs::a_due_attach_retry_does_not_hold_the_loop`.
That shipped code delivers the former A3.5; A3 and A4 keep only their residual
obligations.

## X3 Staged launch and reconnect shipped by #22755 (completed prerequisite)
`kind: framing`

#22755 (closed 2026-09-26) staged launch and reconnect as futures the loop polls:
`3f477ce2ac` and `cd49e8d406` add `connect`, `attach` and `roster` in
`crates/gclient/src/app/live_loop/startup.rs` and make `handle_reconnect_outcome`
in `crates/gclient/src/app/live_loop/reconnect.rs` sync.
`crates/gclient/tests/startup_latency.rs::the_first_frame_is_drawn_before_the_daemon_answers`
and `reconnect_attach_wait_keeps_menus_responsive` cover the launch and reconnect
waits. That shipped code delivers the former A5.1 and A5.3. A5's planned
`live_reconcile.rs` is superseded by that seam; A5 keeps the stale-generation
drop, the final audit and the reconnect WARN bracket.

## P1: gclient loop never awaits the daemon
`kind: framing`

**Goal:** a select branch body and the post-select code never await the daemon.
Daemon I/O is issued as a spawned job; its outcome comes back on a channel and is
applied to `&mut Workspace` on the loop through one staleness gate. This
generalises `start_control_request`, which already does exactly this for
take-control. `LiveDaemon` is `Clone` and both frame source types are `Send`, so a
task can own the daemon handle and hand back a connected source. Ordering is
guaranteed only inside one task, or by issuing the next job from the previous
outcome. Every P1 production deliverable changes the gclient binary and therefore
uses the shared clean-cutover gate above after merge; none promotes independently.

### R1 Decompose the near-ceiling gclient files [category: refactor]
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live_loop.rs::*` — scope-reason: the exit, resize and suspend signal helpers move out and three `mod` lines come in
- `crates/gclient/src/app/live_loop/signals.rs`
- `crates/gclient/src/app/live_loop/workspace_actions.rs::*` — scope-reason: the local-tab adoption group moves out
- `crates/gclient/src/app/live_loop/local_adoption.rs`
- `crates/gclient/src/app/live_loop/actions.rs::*` — scope-reason: the terminal lifecycle actions move out
- `crates/gclient/src/app/live_loop/lifecycle.rs`
- `crates/gclient/src/app/live_loop/projects.rs::*` — scope-reason: imports `close_live_pane`, `spawn_live_shell` and `terminate_live_terminal` from the lifecycle module
- `crates/gclient/src/app/mod.rs::*` — scope-reason: `observe_daemon_disconnect`, `retire_indeterminate_control` and `submit_expired_detaches` move out and one `mod` line comes in
- `crates/gclient/src/app/disconnect.rs`

Consumers unchanged:
- `crates/gclient/tests/client_loop.rs` — no-edit-reason: calls `observe_daemon_disconnect` and `retire_indeterminate_control` as `Workspace` methods, unchanged.
- `crates/gclient/tests/splash.rs` — no-edit-reason: calls `observe_daemon_disconnect` as a `Workspace` method, unchanged.
- `crates/gclient/tests/parity/status.rs` — no-edit-reason: calls `observe_daemon_disconnect` as a `Workspace` method, unchanged.

Pure moves, no behaviour change, so every later deliverable edits a file with
headroom under the 1,000-line ceiling. Rewritten 2026-09-27: the 2026-09-20 R1
split the app and daemon `live` modules, and other work has since done both (the
app module is 605 raw lines with its sidebar and relist modules beside it; the
daemon module is 477 with its connect and reader modules), so those moves and the
planned roster, events and API modules are dropped. Raw counts on 0.5.0 `7d33430cc2`: `crates/gclient/src/app/live_loop.rs`
976, `crates/gclient/src/app/live_loop/workspace_actions.rs` 932,
`crates/gclient/src/app/mod.rs` 856, `crates/gclient/src/app/live_loop/actions.rs`
852.

Move out of `crates/gclient/src/app/live_loop.rs`: `ExitSignals`, `ResizeSignal`
(both cfg variants), `install_exit_signals`, `install_resize_signal`,
`install_suspend_signal`, `recv_exit_signal`, `recv_resize_signal` and
`recv_suspend_signal` into new `crates/gclient/src/app/live_loop/signals.rs`;
`SuspendSignal` stays in `suspend.rs`. `live_loop.rs` gains the `mod signals;`,
`mod local_adoption;` and `mod lifecycle;` lines and imports the helpers.

Move out of `crates/gclient/src/app/live_loop/workspace_actions.rs`: the local-tab
adoption group #22883 added (`LocalSplit`, `first_local_slot`, `local_split_steps`,
`rollback_adopted_tab`, `LocalAdoption`, `adopt_local_tab`,
`spawn_in_adopted_local_tab`) into new
`crates/gclient/src/app/live_loop/local_adoption.rs`; `spawn_owned_live_shell`
stays and imports `spawn_in_adopted_local_tab`.

Move out of `crates/gclient/src/app/live_loop/actions.rs`: the terminal lifecycle
actions `daemon_pane_is_adopted`, `close_live_pane`, `close_live_tab`,
`spawn_live_terminal`, `open_empty_tab`, `spawn_live_shell`,
`finish_live_shell_spawn` and `terminate_live_terminal` into new
`crates/gclient/src/app/live_loop/lifecycle.rs` (`close_slot` already moved to
`projection.rs`, and `group_target` no longer exists); `projects.rs` imports the
three it calls from `super::lifecycle`.

Move out of `crates/gclient/src/app/mod.rs`: `retire_indeterminate_control`,
`observe_daemon_disconnect` and `submit_expired_detaches` into new
`crates/gclient/src/app/disconnect.rs` as an `impl<D: Daemon> Workspace<D>` block;
`mod.rs` gains the `mod disconnect;` line and nothing else.

**Granularity:** one refactor leaf rather than one per file: every move is
behaviour-neutral, the crate must compile as a whole, and one green
`cargo nextest run -p gobby-client` run is the whole verification.

**Cutover:** R1 changes gclient and does not promote independently; after all
leaves pass, it participates in the single shared clean-cutover gate in
Constraints.

**Research context:** Observed with `gcode outline` on 0.5.0 `7d33430cc2`: the
signal types sit at `live_loop.rs` ~115-194 and their helpers at ~682-743; the
adoption group at `workspace_actions.rs` ~117-423; the lifecycle actions at
`actions.rs` ~530-652 and ~741-852; the three `Workspace` methods at `mod.rs`
~746-763 and ~781-805 (`arm_indeterminate_reattach` at ~765-779 stays: `live.rs`
calls it). Callers of the moved methods use method syntax (`live.rs`,
`live_loop.rs`, `reconnect.rs`, `run_loop.rs`, `control.rs`), so only `use` lines
change. Approach: cut and paste with `pub(super)`/`pub(crate)` visibility adjusted;
no signature changes. Planned verification: `cargo fmt -p gobby-client`,
`cargo clippy -p gobby-client --all-targets`, `cargo nextest run -p gobby-client`,
then `wc -l` of the four source files, each under 850; screen goldens unchanged.

**Acceptance:**

- R1.1 - The exit, resize and suspend signal helpers live in their own module.
  file: `crates/gclient/src/app/live_loop/signals.rs`.
- R1.2 - The local-tab adoption group lives in its own module. file:
  `crates/gclient/src/app/live_loop/local_adoption.rs`.
- R1.3 - `live_loop.rs`, `workspace_actions.rs`, `actions.rs` and `mod.rs` each end
  under 850 raw lines. behavior: "each of the four moved-from files is under 850
  lines" in `crates/gclient/src/app/live_loop.rs`.
- R1.4 - The disconnect and control-retirement methods live in their own module.
  file: `crates/gclient/src/app/disconnect.rs`.
- R1.5 - The terminal lifecycle actions live in their own module. file:
  `crates/gclient/src/app/live_loop/lifecycle.rs`.
- R1.7 - No behaviour change: the full gclient suite and the screen goldens pass
  unchanged. behavior: "cargo nextest run -p gobby-client passes with no golden
  regeneration" in `crates/gclient/tests/screens.rs`.

### A0 Loop-liveness test scaffolding [category: test]
`kind: deliverable`

Targets:
- `crates/gclient/tests/mock_daemon/mod.rs::*` — scope-reason: the hold threads through `MockState`, `MockDaemon`, `serve_websocket` and `websocket_reply`
- `crates/gclient/tests/loop_liveness.rs`

Add to the mock daemon a `hold_ws(kind, filter) -> Arc<Notify>` that records the
request, computes the reply and the events-before-reply exactly as
`websocket_reply` and `websocket_events_before_reply` do today, and releases them
only on `notified()`; add `replies(kind) -> usize`. New
`crates/gclient/tests/loop_liveness.rs` carries the shared probe used by every
later P1-P3 test: a proxy pane fed `terminal_frame` events every ~16 ms; while a
hold is outstanding the probe asserts `frames_rendered` advances, `chrome.ticker`
advances, a key typed into another pane reaches its source, and the held outcome
applies after release. The file header documents the wait-for-mock-visible-request
rule from memory `679bf344`.

A0 closes with passing hold and probe infrastructure only. The current inline
`terminal_attach` path is not required to satisfy a green liveness assertion in
this section; A3 owns that assertion after it moves attach work into a job. The
probe records loop progress directly and adds no phase label or watchdog.
**Restraint rung 1:** do not add a behavior promise before the deliverable that
owns the production change; reuse `hold_ws` and the shared probe there.

**Research context:** Observed: `crates/gclient/tests/mock_daemon/mod.rs`
(`MockDaemon::start_at`, `enqueue`, `enqueue_with_events`,
`finalize_next_proxy_attach_before_reply`, `pause_websocket_reads`, `requests`,
`websocket_handshakes`; `serve_websocket` at ~744, `websocket_reply` at ~871) already
supports queued replies, paused reads, a REST `enqueue_held` (~209) and a
`terminal_attach`-only `hold_attach` (~523), but has no per-kind websocket hold.
`hold_ws` generalises `hold_attach` (which becomes a `hold_ws("terminal_attach", ..)`
call), and REST holds reuse `enqueue_held`. `crates/gclient/tests/client_loop.rs` is
13,787 lines; no new tests go there. Reusable: `ScriptedFrameSource` and
`ProxyFrameSource` from `crates/gclient/src/frame_source.rs` for the probe pane.
Approach: a `Notify` per hold, stored on `MockState`, consulted by
`serve_websocket` before writing the reply. Planned verification: `cargo nextest run
-p gobby-client --test loop_liveness`.

**Acceptance:**

- A0.1 - `hold_ws` keeps a matching websocket request pending until released and
  `replies` counts the released reply. file: `crates/gclient/tests/mock_daemon/mod.rs`.
- A0.2 - The liveness probe is a reusable helper and its baseline test passes.
  test: `crates/gclient/tests/loop_liveness.rs::held_websocket_request_stays_pending_until_released`.

### A1 Job plumbing, focus hints and geometry [category: code] (depends: R1, A0)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live_loop.rs::*` — scope-reason: `run_live_loop` gains the outcome branch and loses its post-select awaits; `resize_live_workspace` becomes a sync stage
- `crates/gclient/src/app/live_loop/jobs.rs`
- `crates/gclient/src/app/live_loop/jobs/tests.rs`
- `crates/gclient/src/app/live_loop/jobs_apply.rs`
- `crates/gclient/src/app/live_loop/workspace_actions.rs::*` — scope-reason: the focus-hint group moves out to the focus-hints module
- `crates/gclient/src/app/live_loop/focus_hints.rs`
- `crates/gclient/src/app/run_loop.rs::propagate_geometry`
- `crates/gclient/src/frame_source.rs::*` — scope-reason: keep direct SetViewport/input delivery nonblocking while the same file is decomposed and extended by P3
- `crates/gclient/tests/loop_liveness.rs`

Consumers unchanged:
- `crates/gclient/tests/client_loop.rs` — no-edit-reason: drives `propagate_geometry` and `run_live_loop` through the scripted loop API; both keep their signatures.

Mechanism, in new `crates/gclient/src/app/live_loop/jobs.rs`:

```rust
struct JobTag { id: u64, generation: Generation, key: JobKey }
enum JobKey { Loop, Pane(PaneId), Attachment(String), Terminal(String),
              Geometry(PaneId), Scroll(PaneId), FocusHints, Roster, Attention, Tab(String) }
struct JobOutcome { tag: JobTag, result: JobResult }
enum JobResult { WorkspaceOp{..}, Resized{..}, /* later deliverables add variants */ }
fn spawn_job(tx: &UnboundedSender<JobOutcome>, tag: JobTag,
             fut: impl Future<Output = JobResult> + Send + 'static);
struct Coalescer<K, T> { /* offer(k, v) -> Option<T>; settle(k) -> Option<T> */ }
struct JobLedger { in_flight: HashMap<JobKey, u64>, coalesced: Coalescer<..>, .. }
```

Typed enums, not boxed appliers. Every outcome passes one gate in new
`crates/gclient/src/app/live_loop/jobs_apply.rs` (which needs `&mut Chrome`): settle
the ledger key; drop if `tag.generation != daemon.generation()` but still clear a
matching `in_flight_write` or `control_request`; then per-result seq or request-id
checks. One `mpsc::unbounded_channel::<JobOutcome>()` is consumed by a select branch
placed directly under the existing `control_rx` branch. Not `JoinSet`: jobs are
never aborted. `JobLedger` lives on the loop; `forget_generation(old)` on reconnect
frees slots whose outcomes will be dropped. `job_rx.close()` at loop exit.

Settlement is identity-checked. `JobLedger::settle(tag)` removes
`in_flight[tag.key]` and releases that key's coalesced successor only while the
entry still equals `tag.id` for `tag.generation`. After `forget_generation(old)`
frees a key and a fresh job takes it, a late old-generation outcome settles
nothing, so the newer job, its in-flight marker and its one coalesced follow-up
stay intact. Two kinds of result bypass the stale-generation drop. A2's
`WriteAbandoned` and `WriteUnconfirmed` reports carry their own generation and
apply to their pane by id as status text only, never as a state change of the
current generation, so delivery uncertainty survives a reconnect. Committed
mutation results take A4b's disposal path.

**Granularity:** seven acceptance items, one seam: the job types, the gate, the
identity-checked ledger and the first converted paths must compile and be tested
together, because a job seam without its gate or settlement rule would apply
stale outcomes.

`crates/gclient/src/app/live_loop.rs` is at 976 raw lines before R1: the channel types,
ledger, coalescer and `spawn_job` move into `crates/gclient/src/app/live_loop/jobs.rs`
and the apply match into `crates/gclient/src/app/live_loop/jobs_apply.rs` instead of
growing `live_loop.rs`; `run_live_loop` gains the branch and loses its post-select
awaits.

`send_focus_hints_if_changed` becomes a coalesced job with key `FocusHints`, memo
updated on success only; it issues through a new sync `issue_workspace_op(workspace,
ledger, op, OpIntent)` in `jobs.rs`, and `send_workspace_op` stays until A2 retires
it. `crates/gclient/src/app/live_loop/workspace_actions.rs` is at 932 raw lines:
move `ShownFocus`, `shown_focus`, `stored_focus` and `send_focus_hints_if_changed`
into new `crates/gclient/src/app/live_loop/focus_hints.rs`, where the hint becomes
the coalesced job. Geometry: `Workspace::stage_geometry` beside
`propagate_geometry` in `run_loop.rs` is sync: it sets the viewport, and for direct
sources enqueues `SetViewport` through `UnixSocketFrameSource::send_input`, whose
allow-list widens from `BindAttachment | Input | Paste` to include `SetViewport`; for
proxy sources it returns the message for the job. Key `Geometry(pane)`, latest wins;
the job sends viewport then resize so per-pane order holds. A direct `SetViewport`
that hits backpressure reports `Backpressure` on the status line, not a toast.

**Cutover:** A1 changes gclient and does not promote independently; after all
leaves pass, it participates in the single shared clean-cutover gate in
Constraints.

**Research context:** Observed: `run_live_loop`
(`crates/gclient/src/app/live_loop.rs`, ~197-680) is the biased select; the
post-select code awaits `send_focus_hints_if_changed`
(`crates/gclient/src/app/live_loop/workspace_actions.rs`, which awaits
`send_workspace_op` → `LiveDaemon::workspace_op`) and `resize_live_workspace`
(`live_loop.rs` ~931-976, awaits per-pane resize) every iteration. `propagate_geometry`
(`crates/gclient/src/app/run_loop.rs` ~392-445) is the scripted-loop geometry path
and stays. `UnixSocketFrameSource::send_input` (`crates/gclient/src/frame_source.rs`
~662) refuses anything but `BindAttachment | Input | Paste` with
`FrameError::Protocol`. `start_control_request` (`crates/gclient/src/app/live.rs`
~236) is the pattern being generalised: spawn, channel, apply in branch.
`LiveDaemon` is `Clone` (`crates/gclient/src/daemon/live.rs`). Rejected: a `JoinSet`
(aborting a control future after its write started tombstones the attachment);
boxed `FnOnce(&mut Workspace, &mut Chrome)` appliers (untestable, capture `Chrome`).
Planned verification: `cargo nextest run -p gobby-client --test loop_liveness`,
`cargo nextest run -p gobby-client` for the unit test, `cargo clippy -p gobby-client
--all-targets`.

**Acceptance:**

- A1.1 - The job types, ledger, coalescer and `spawn_job` exist and are `Send`.
  file: `crates/gclient/src/app/live_loop/jobs.rs`.
- A1.2 - `run_live_loop` consumes job outcomes in a branch under `control_rx` and its
  post-select code contains no daemon await. symbol: `run_live_loop`.
- A1.3 - A held focus-hint op never stalls frames or ticks. test:
  `crates/gclient/tests/loop_liveness.rs::a_held_focus_hint_op_never_stalls_frames_or_ticks`.
- A1.4 - A resize burst sends at most one resize per pane in flight and the latest
  geometry wins. test:
  `crates/gclient/tests/loop_liveness.rs::resize_burst_sends_at_most_one_resize_per_pane_in_flight`.
- A1.5 - The coalescer keeps one in flight and the latest pending. test:
  `crates/gclient/src/app/live_loop/jobs/tests.rs::coalescer_keeps_one_in_flight_and_latest_pending`.
- A1.6 - A direct source accepts `SetViewport`; when its bounded writer is full,
  the latest viewport is refused with `Backpressure`, the status line reports it,
  and frames, ticks and input on another pane keep progressing. test:
  `crates/gclient/tests/loop_liveness.rs::direct_set_viewport_backpressure_is_visible_and_never_stalls_the_loop`.
- A1.7 - An old-generation job is held, `forget_generation` runs, the same key is
  issued anew, and then the old result lands before the new one. The old result
  settles nothing, and the new result clears the current marker and releases
  exactly one coalesced follow-up. test:
  `crates/gclient/tests/loop_liveness.rs::a_late_old_generation_outcome_never_settles_the_reissued_job`.

### A2 Typed input and control path [category: code] (depends: A1)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live_loop/control.rs::*` — scope-reason: every daemon-awaiting function becomes a sync issue or a job outcome apply
- `crates/gclient/src/app/pane.rs::*` — scope-reason: add the bounded writer, queue accounting and release state across Pane construction and input helpers
- `crates/gclient/src/app/live.rs::*` — scope-reason: the control-request machinery moves out to the new control module
- `crates/gclient/src/app/live_control.rs`
- `crates/gclient/src/app/mod.rs::*` — scope-reason: gains the `mod live_control;` declaration only
- `crates/gclient/src/app/live_loop/workspace_actions.rs::*` — scope-reason: the small op senders move out to the daemon-ops module and `send_workspace_op` is deleted
- `crates/gclient/src/app/live_loop/daemon_ops.rs`
- `crates/gclient/src/app/attention.rs::submit_response`
- `crates/gclient/src/app/live_loop/actions.rs::*` — scope-reason: the remaining callers of the control, scroll and op senders drop their awaits
- `crates/gclient/src/app/live_loop/lifecycle.rs`
- `crates/gclient/src/app/live_loop/projects.rs::*` — scope-reason: callers of the release and op senders drop their awaits
- `crates/gclient/src/app/live_loop/jobs.rs`
- `crates/gclient/src/app/live_loop/jobs_apply.rs`
- `crates/gclient/src/frame_source.rs::*` — scope-reason: `UnixSocketFrameSource::send_input` hands back its per-write `done` receiver so a direct pane's release barrier can await the last accepted write
- `crates/gclient/tests/loop_liveness.rs`

Consumers unchanged:
- `crates/gclient/src/app/scripted_input.rs` — no-edit-reason: mentions `send_live_write` only in a doc comment; the scripted path has its own writer.
- `crates/gclient/src/frame_source/proxy.rs` — no-edit-reason: `ProxyFrameSource::send_input` (~195) keeps the unchanged `FrameSource::send_input` trait signature; the direct write receipt is read through an inherent method on the direct source in `frame_source.rs`, not through the trait.
- `crates/gclient/tests/client_loop.rs` — no-edit-reason: asserts write bodies by `client_write_seq`, which is still assigned on the loop.

The functions in `crates/gclient/src/app/live_loop/control.rs` become sync:
`send_live_write` pushes `(client_write_seq, Value)` to the pane's writer;
`release_live_control` sets the pane's pending-release flag or issues `Notified`;
`retire_live_control` issues `Notified{detach}`; `set_live_scroll_offset` issues key
`Scroll(pane)` for proxy sources and calls `send_input` for direct ones;
`apply_control_outcome` and `apply_live_write_outcome` stay sync appliers;
`move_live_focus` and `send_live_input` are sync. `Pane` gains `writer:
Option<Sender<QueuedWrite>>`, queued-byte accounting and `release_pending: bool`;
a per-pane writer task drains the queue sequentially and reports one `Write`
outcome per message, so typed order on proxied and tmux panes holds and
`client_write_seq` is still assigned on the loop. The channel is nonblocking and
bounded at 256 messages and 1 MiB of queued payload per pane, reusing
`PASTE_MAX_BYTES` as the byte ceiling. `try_send` rejects the newest input with
`Backpressure` when either cap would be exceeded; it never drops, coalesces or
reorders already accepted `Input` or `Paste` messages. A successful later enqueue
clears the one-shot queue-full status. **Restraint rung 2:** reuse Tokio's bounded
channel, the existing payload ceiling and the existing backpressure surface; add no
queue framework or retry policy.

Ordering of accepted input against release, close and generation change. The
pane's writer queue is the one ordering seam; the loop only enqueues and applies
outcomes, and every wait below runs in the writer task or the spawned control task.

- Barrier. The queue carries `QueuedWrite::Write{generation, client_write_seq,
  body}` and `QueuedWrite::Barrier{kind: Release | Close, done: watch::Sender<bool>}`. The
  256-message and 1 MiB caps count `Write` items only, and the channel is sized one
  above the message cap. `release_pending` admits at most one outstanding barrier
  per pane, so a barrier enqueue never meets `Backpressure`.
- Barrier ownership. The writer task owns each barrier and publishes its
  completion once on a `tokio::sync::watch` channel. Every waiter holds its own
  cloned receiver, so one completion serves any number of dependent takes. A
  repeated release while `release_pending` is set enqueues nothing and reuses the
  pending barrier. A close while a release barrier is pending also enqueues
  nothing: it sets `close_after_release` on the pane, and the close's detach or
  kill is issued from that barrier's completion, after the release. A close with
  no barrier pending enqueues its own `Close` barrier in the reserved slot. A
  pane's `Release` barrier first awaits the completion of that pane's own
  in-flight take, if any, so a release never reaches the daemon ahead of the take
  it undoes.
- Focus move. `release_live_control` sets `release_pending`, marks the pane
  Releasing so any new input for it is refused locally with the status
  `Focus moved; input not sent.`, and enqueues a `Release` barrier. The writer
  sends every earlier accepted `Write` first, then the daemon release, then
  resolves `done`. For a direct pane, whose input goes to the frame stream's own
  writer task, the barrier first awaits the `done` receiver of the last accepted
  frame write (`UnixSocketFrameSource::send_input` already creates one per write;
  the pane keeps the latest instead of dropping it, reading it through an
  inherent method on the direct source so the `FrameSource::send_input` trait,
  its scripted wrapper and `ProxyFrameSource::send_input` stay unchanged). That receipt proves only a
  local flush to the frame socket. It does not prove gterm read the bytes, the PTY
  consumed them, or any order against the daemon's revoke on the control socket,
  so the direct barrier is local flush ordering only.
- Take. `start_control_request` hands the spawned control task a cloned
  completion receiver for every Releasing pane, and the task awaits them before it writes the
  take. The wire order is: the old pane's accepted bytes, its release, the new
  pane's take. The loop never awaits.
- Failure. Outcomes keep what gclient actually knows, in three classes:
  - Known unsent. Writes still queued, never handed to a socket, behind a failed
    write, a `Close` barrier or a generation change. The writer reports them as
    one `WriteAbandoned{generation, messages, bytes}` outcome with the status
    `Input not sent: N bytes.`. These counts are exact, because gclient never
    submitted them.
  - Delivery unconfirmed. A write handed to the socket whose reply never arrived:
    a daemon `terminal_input` that errored or timed out after the request write
    began, or a connection lost with the reply outstanding. The daemon may or may
    not have written it to the PTY. The writer reports
    `WriteUnconfirmed{generation, messages}` with the status `Input delivery
    unconfirmed.`, with no delivered or refused byte count.
  - Refused. A typed refusal reply that correlates to its request (the daemon's
    `terminal_input` error for that request) is known not written. It is
    reported as `Input refused: <reason>.` with that request's size.
  A direct pane's frame socket has no per-write reply. Once a flushed write
  exists, gterm arbitrates: it accepts bytes it reads while the pane's grant
  holds, and answers bytes it reads after the revoke with the typed `InputRefused`.
  That message is `InputRefused { code }` only
  (`crates/gterminal/src/protocol/wire_types.rs` ~543 at `8965cc963f`), with no
  write id or byte count. The pane therefore reports it as one
  `WriteUnconfirmed` for the pane, beside the existing observe flip. It does not
  say which flushed writes the refusal covers and gives no byte count. This plan adds no ack protocol to
  the frame socket. After the release barrier, no write for the old pane is
  enqueued or flushed, and gclient never replays a known-unsent or unconfirmed
  write under any authority or generation. Recovery is the user's retype.
- Close. Pane and tab close enqueue a `Close` barrier, and the close's detach or
  kill is issued from the barrier's `done`. Accepted bytes are flushed or reported
  before the pane leaves.
- Stale generation. Each `Write` carries the attachment generation it was accepted
  under. When the pane's generation changes (re-attach, recovery, reconnect), the
  old writer is closed. Its still-queued writes are reported as `WriteAbandoned`
  and any write in flight as `WriteUnconfirmed`, both for the old generation.
  Neither is re-sent under the new attachment id; a fresh writer serves the new
  generation.

`crates/gclient/src/app/live.rs` is at 605 raw lines and `crates/gclient/src/app/mod.rs`
at 856 (about 812 after R1): the control-request machinery (`request_control`, `awaiting_control`,
`start_control_request`, `ControlOutcome`, `PendingControl`) is a move out of
`live.rs` and `mod.rs` into new `crates/gclient/src/app/live_control.rs`, where the
release drain is added; `mod.rs` gains the `mod live_control;` line only.
`crates/gclient/src/app/live_loop/actions.rs` is at 852 raw lines: R1's move of the
lifecycle actions into `crates/gclient/src/app/live_loop/lifecycle.rs` leaves this
deliverable only dropping the awaits that remain in `actions.rs`.

`crates/gclient/src/app/live_loop/workspace_actions.rs` is at 932 raw lines: the
small ops (`place_live_terminal`, `swap_live_slots`, `close_daemon_pane`,
`close_daemon_tab`, `rename_daemon_target`, `apply_daemon_menu_action`,
`resize_daemon_split`, `move_daemon_tab`, `move_active_daemon_tab`,
`move_focused_pane_to_tab`) move into new
`crates/gclient/src/app/live_loop/daemon_ops.rs` and issue `WorkspaceOp` jobs there;
the refusal toast moves to the `WorkspaceOp` outcome apply in `jobs_apply.rs`.
`submit_response` in `attention.rs` issues a `Responded` job.

**Granularity:** fourteen production files in one leaf because the sync conversion of
`control.rs` changes the signature of every caller and the crate must compile in
one commit; the callers cannot be separate leaves. The release barrier needs the
writer, the control task and the frame write receipt together, or a focus move
could revoke authority with bytes still queued.

**Cutover:** A2 changes gclient and does not promote independently; after all
leaves pass, it participates in the single shared clean-cutover gate in
Constraints.

**Research context:** Observed: `send_live_input` (`control.rs` ~223-262) returns
early when `!workspace.daemon_ready()` before checking `pane.writable()`; C1 later
reorders that test. `move_live_focus` (~58-70) awaits `release_live_control` on the
previous pane. `release_live_control` (~181-215) and `set_live_scroll_offset`
(~333-355) await the daemon. `Pane` (`crates/gclient/src/app/pane.rs` ~133-215)
already carries the pre-grant `PendingInput` queue and `in_flight_write`.
`submit_response` (`crates/gclient/src/app/attention.rs` ~115-142) awaits
`LiveDaemon::respond`. Callers dropping awaits: `handle_live_action` and
`apply_live_menu_action` in `actions.rs`, `close_live_terminal` and `focus_terminal`
in `projects.rs`, `close_live_pane`/`close_live_tab` in `lifecycle.rs`. Rejected: a
`Workspace::pending_releases` queue (adds a field to `mod.rs`; the per-pane flag
carries the same fact). Planned verification: `cargo nextest run -p gobby-client
--test loop_liveness`, `cargo clippy -p gobby-client --all-targets`.

**Acceptance:**

- A2.1 - No function in `control.rs` awaits the daemon; writes go through the pane
  writer and releases through the pending flag. file:
  `crates/gclient/src/app/live_loop/control.rs`.
- A2.2 - Typing into a tmux pane while `terminal_input` is held keeps frames
  flowing. test:
  `crates/gclient/tests/loop_liveness.rs::typing_into_a_tmux_pane_while_terminal_input_is_held_keeps_frames_flowing`.
- A2.3 - A focus move writes release before take on the wire. test:
  `crates/gclient/tests/loop_liveness.rs::a_focus_move_writes_release_before_take_on_the_wire`.
- A2.4 - A write error from a stale generation does not mark the pane read-only.
  test:
  `crates/gclient/tests/loop_liveness.rs::a_write_error_from_a_stale_generation_does_not_mark_the_pane_read_only`.
- A2.5 - The control-request machinery lives in its own module, and its control
  task awaits every pending release barrier before writing a take. file:
  `crates/gclient/src/app/live_control.rs`.
- A2.6 - `send_workspace_op` no longer exists; every op sender is sync and the
  refusal toast is raised at apply. file:
  `crates/gclient/src/app/live_loop/daemon_ops.rs`.
- A2.7 - Holding an attention-response request never stalls rendering; the
  response outcome applies exactly once after release. test:
  `crates/gclient/tests/loop_liveness.rs::a_held_attention_response_never_stalls_frames_or_applies_twice`.
- A2.8 - Flooding a pane while `terminal_input` is held keeps its queue at or
  below 256 messages and 1 MiB, rejects only the newest over-cap messages with
  `Backpressure`, preserves accepted FIFO order after release, and leaves another
  direct pane's input plus rendering live. test:
  `crates/gclient/tests/loop_liveness.rs::a_held_terminal_input_flood_is_bounded_ordered_and_nonblocking`.
- A2.9 - With `terminal_input` held on pane A, bytes typed into A and then a focus
  move to B produce, on the wire, A's accepted writes, then A's release, then B's
  take. If the held write then errors after its request was written, A reports
  `WriteUnconfirmed` for it and `WriteAbandoned` with the exact count for the
  writes queued behind it. No A write is sent after the release or under B's
  authority, and nothing is replayed. Frames and ticks advance throughout. test:
  `crates/gclient/tests/loop_liveness.rs::held_input_then_focus_move_delivers_or_reports_before_release`.
- A2.10 - Closing a pane with queued writes flushes or reports them before the
  detach, and a generation change reports the old generation's queued writes as
  abandoned and its in-flight write as unconfirmed, without sending either under
  the new attachment id. test:
  `crates/gclient/tests/loop_liveness.rs::close_and_generation_change_never_replay_queued_input`.
- A2.11 - On a direct pane, the frame writer's receipt resolves while the mock
  gterm delays reading the socket until after it processes the revoke. The
  release barrier resolves on the local receipt alone. The mock's typed
  `InputRefused` is reported as `WriteUnconfirmed` with no byte count, and
  nothing is replayed. In a second run the mock reads before the revoke, and
  the bytes are accepted with no refusal status. test:
  `crates/gclient/tests/loop_liveness.rs::direct_flush_receipt_orders_locally_and_late_consumption_is_unconfirmed`.
- A2.12 - A daemon `terminal_input` whose request is written and whose reply is
  never delivered (the mock drops the connection with the reply outstanding)
  is reported as `WriteUnconfirmed`, and writes queued behind it are reported
  as `WriteAbandoned` with their exact count. After reconnect the new generation
  sends none of them. A correlated typed refusal is reported as refused with its
  request's size. test:
  `crates/gclient/tests/loop_liveness.rs::indeterminate_daemon_write_is_unconfirmed_and_never_replayed`.
- A2.13 - With a held writer, focus moves A to B to C. Each take waits on its own
  receiver for every earlier release, each release follows its pane's accepted
  bytes and that pane's own take, and exactly one daemon release per pane is
  sent. test:
  `crates/gclient/tests/loop_liveness.rs::focus_a_b_c_with_a_held_writer_orders_every_release_before_the_next_take`.
- A2.14 - Closing A while its release barrier is pending sends one release and
  then the close's detach from the same completion. The barrier slot never
  overfills and the detach never precedes the release. test:
  `crates/gclient/tests/loop_liveness.rs::closing_a_pane_while_its_release_is_pending_detaches_after_the_release`.
- A2.15 - `WriteAbandoned` and `WriteUnconfirmed` reports from the old generation
  still show their status after a reconnect. test:
  `crates/gclient/tests/loop_liveness.rs::old_generation_write_uncertainty_is_reported_after_reconnect`.

### A3 Attach and recovery as jobs [category: code] (depends: A2)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live_attach.rs::*` — scope-reason: attach and recovery futures record their start, emit the WARN records, and drop and detach a stale-generation result
- `crates/gclient/src/app/live.rs::*` — scope-reason: the `created` arm of `apply_live_event` marks the new pane due instead of awaiting `attach_ready_panes`
- `crates/gclient/src/app/attach.rs::Pane::begin_attaching`
- `crates/gclient/tests/loop_liveness.rs`

Consumers unchanged:
- `crates/gclient/tests/frame_delivery.rs` — no-edit-reason: exercises the inline attach path, unchanged.
- `crates/gclient/tests/reconciliation.rs` — no-edit-reason: exercises the inline attach path, unchanged.
- `crates/gclient/tests/workspace.rs` — no-edit-reason: calls `recv_live_frame`, whose signature is unchanged.

Status 2026-09-27: A3's frame-recovery slice shipped with #22747 (X2), and the
render-tick retry issues through `start_due_attaches`. By Decision 9 attach and
recovery stay on that `RecoveryFuture` seam in
`crates/gclient/src/app/live_attach.rs`; the planned `live_attach/handshake.rs`
and a second attach path in `jobs.rs` are dropped. A3 keeps four obligations.

The `created` event no longer awaits `attach_ready_panes()` inside
`apply_live_event` (`live.rs` ~356): it marks the new pane due, and the next
`start_due_attaches` pass issues it. The Q7b liveness case belongs here: a
`created` event starts a new terminal's `terminal_attach`, its reply is held for
three seconds of paused Tokio time, and already attached direct panes keep
consuming frames, advancing ticks and delivering input for the whole interval.
The remaining action-chain callers (`lifecycle.rs`, `local_adoption.rs`,
`workspace_actions.rs`, `terminal_location.rs`) convert in A4b, `projects.rs` in
A4a, and
`open_live_terminal`'s (`live.rs` ~143) in A3b. `reconcile_subscribe_first` and the
existing tests keep the inline `attach_ready_panes()`.

Every attach result passes the gate: pane exists, `Attaching{request_id,
generation}` matches, attachment not tombstoned; a superseded result that carries
a source is dropped and its attachment detached so the daemon does not hold a
dangling attachment. `Pane::begin_attaching` sets `status_message = "attaching
(direct|proxy)…"`.

A3's X1 precondition is met: #22677 closed as `completed`. The repaired detach
state machine stays #22677's; A3 changes none of its transitions.

Frame recovery becomes observable. Each attach and recovery future records its
start instant. One structured `tracing::warn!` fires when a real frame error
starts recovery, including `RetireReason::Lag`; one when the direct connect/attach
phase times out; and one named `frame_recovery_gave_up` when detach reaches its
deadline, attach is refused or errors, or a stale or tombstoned result is
discarded without installing a replacement source. These records carry
`pane_id`, `terminal_id`, old `attachment_id` when present, transport, daemon
generation, recovery stage, error class and `elapsed_ms`. `Refused` and
`Backpressure` input outcomes are not frame failures and stay excluded. A
successful recovery emits one structured completion with elapsed time and the
replacement transport, so the attempt closes in `gclient.log`. The records sit
where `begin_frame_recovery`, `apply_frame_recovery` and `start_due_attaches`
already start and apply each step; there is no new logger, telemetry transport,
retry counter or watchdog. **Restraint rung 2:** reuse gclient's tracing
subscriber and the shipped seam.

**Cutover:** A3 changes gclient and does not promote independently; after all
leaves pass, it participates in the single shared clean-cutover gate in
Constraints.

**Research context:** Observed on 0.5.0 `7d33430cc2`: `live_attach.rs` defines
`RecoveryFuture` (~50), `begin_frame_recovery` (~175), `apply_frame_recovery`
(~253) and `start_due_attaches` (~441); `attach_ready_panes` (~63) still awaits
`request_direct_source` then `begin_live_proxy_attach` per pane. Inline callers:
`live.rs` ~143 and ~356, `actions.rs` ~811, `workspace_actions.rs` ~416 and ~527,
`projects.rs` ~49, `terminal_location.rs` ~146. No WARN exists for frame
failure, recovery give-up or direct-handshake timeout; `daemon/live.rs` ~473
("daemon request timed out") and `live_sidebar.rs` ~323 are the only request
WARNs. `AttachState` and `Pane::begin_attaching` live in
`crates/gclient/src/app/attach.rs`; `DETACH_DEADLINE` is 2 s. Rejected: awaiting
the attach inside the branch with a timeout (still parks the loop); a
`handshake.rs` job module (duplicates the shipped seam). Planned verification:
`cargo nextest run -p gobby-client --test loop_liveness --test frame_delivery
--test reconciliation --test frame_source_live`.

**Acceptance:**

- A3.1 - The `created` event path marks the pane due for `start_due_attaches`
  instead of awaiting `attach_ready_panes`. symbol: `apply_live_event`.
- A3.2 - A held attach on one pane keeps the other pane rendering. test:
  `crates/gclient/tests/loop_liveness.rs::a_held_attach_on_one_pane_keeps_the_other_pane_rendering`.
- A3.3 - An attach outcome from a previous generation is dropped and its attachment
  detached. test:
  `crates/gclient/tests/loop_liveness.rs::an_attach_outcome_from_a_previous_generation_is_dropped_and_detached`.
- A3.4 - Recovery detaches then attaches while input still routes. test:
  `crates/gclient/tests/loop_liveness.rs::recovery_detaches_then_attaches_while_input_still_routes`.
- A3.6 - A frame error and every recovery give-up leave one structured record with
  pane, terminal, attachment, generation, stage, error class and `elapsed_ms`, while
  a successful replacement closes the attempt. The same test covers a
  direct-handshake timeout and a `RetireReason::Lag` retirement. test:
  `crates/gclient/tests/loop_liveness.rs::frame_errors_and_recovery_giveups_are_logged_with_pane_context`.
- A3.7 - A newly created terminal whose `terminal_attach` reply takes three
  seconds does not stop frames, ticks or input on existing direct panes. test:
  `crates/gclient/tests/loop_liveness.rs::a_slow_new_terminal_attach_never_stalls_streaming_direct_panes`.

### A3b Open unresolved terminals as jobs [category: code] (depends: A3)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live_workspace.rs::*` — scope-reason: retains the inline helper and adds a sync planner for missing-terminal open jobs
- `crates/gclient/src/app/live.rs::*` — scope-reason: the `DaemonEvent::Workspace` arm of `apply_live_event` stops awaiting `open_unresolved_terminals`
- `crates/gclient/src/app/live_loop.rs::*` — scope-reason: `run_live_loop` routes `Opened` outcomes
- `crates/gclient/src/app/live_loop/reconnect.rs::*` — scope-reason: `handle_live_event` (~54) plans missing opens
- `crates/gclient/src/app/live_loop/unresolved.rs`
- `crates/gclient/src/app/live_loop/jobs.rs`
- `crates/gclient/src/app/live_loop/jobs_apply.rs`
- `crates/gclient/tests/loop_liveness.rs`

The existing async `open_unresolved_terminals` stays for inline reconcile and test
callers. A sync `plan_unresolved_opens` sibling snapshots the missing terminal ids;
the live loop calls it after a workspace event and issues one keyed `Opened` job per
terminal. `apply_live_event` no longer awaits an open. A repeated event coalesces on
`Terminal(id)`, and an outcome applies only when that terminal is still unresolved
in the current daemon generation. Replace the current discarded
`open_live_terminal` error with one structured WARN at the job/apply seam carrying
`terminal_id`, daemon generation, error class and `elapsed_ms`; a stale-generation
drop is a distinct outcome. **Restraint rung 2:** reuse the `Opened` job result and
tracing subscriber; add no retry counter or monitoring task.

`crates/gclient/src/app/live_loop.rs` is at 976 raw lines before R1: move unresolved-job issuance and outcome routing into new
`crates/gclient/src/app/live_loop/unresolved.rs`; `live_loop.rs` gains only the
module declaration and bounded calls.

**Cutover:** A3b changes gclient and does not promote independently; after all
leaves pass, it participates in the single shared clean-cutover gate in
Constraints.

**Research context:** Observed: `open_unresolved_terminals`
(`crates/gclient/src/app/live_workspace.rs` ~105) collects missing terminals and
awaits `open_live_terminal` serially. Its two current callers are `drain_receiver`
and the `DaemonEvent::Workspace` arm in `apply_live_event` (both in
`crates/gclient/src/app/live.rs`). The former is an
inline reconcile path and keeps the async helper; only the live-event path enters
the nonblocking job seam. Planned verification: `cargo nextest run -p gobby-client
--test loop_liveness --test reconciliation --test workspace`.

**Acceptance:**

- A3b.1 - A held unresolved-terminal open keeps frames, ticks and direct input
  progressing, installs the terminal once after release, and coalesces repeated
  workspace events. test:
  `crates/gclient/tests/loop_liveness.rs::a_held_open_unresolved_terminal_job_is_coalesced_and_nonblocking`.
- A3b.2 - The inline reconcile helper remains available while the live workspace
  event path issues `Opened` jobs without awaiting. symbol:
  `open_unresolved_terminals`.
- A3b.3 - Unresolved-job issuance and apply helpers live outside the near-ceiling
  loop module. file: `crates/gclient/src/app/live_loop/unresolved.rs`.
- A3b.4 - A failed unresolved-terminal open emits one WARN with terminal,
  generation, error class and `elapsed_ms`; the error is no longer discarded.
  test:
  `crates/gclient/tests/loop_liveness.rs::an_unresolved_terminal_open_failure_is_logged_with_elapsed_time`.

### A4a Event-driven refetches and roster jobs [category: code] (depends: A3b)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live/relist.rs::*` — scope-reason: `fetch_roster` gains the sync `issue_roster_fetch` sibling beside the shipped relist job
- `crates/gclient/src/app/live.rs::*` — scope-reason: `apply_live_event` becomes sync and sets refetch flags
- `crates/gclient/src/app/live_sidebar.rs::*` — scope-reason: `fetch_attention` issues a coalesced job and each failed REST component logs a WARN
- `crates/gclient/src/app/live_loop.rs::*` — scope-reason: `run_live_loop` routes the new outcomes and its `Lagged` arm (~419) drops its await on `apply_live_event`
- `crates/gclient/src/app/live_loop/reconnect.rs::*` — scope-reason: `handle_live_event` (~54-75) drops its await on the now-sync `apply_live_event` and drains the refetch flags
- `crates/gclient/src/app/live_loop/projects.rs::*` — scope-reason: `focus_project` issues a scoped roster job and `restore_focused` runs when it lands
- `crates/gclient/src/app/live_loop/jobs.rs`
- `crates/gclient/src/app/live_loop/jobs_apply.rs`
- `crates/gclient/tests/loop_liveness.rs`

Consumers unchanged:
- `crates/gclient/tests/client_loop.rs` — no-edit-reason: calls `fetch_roster` and `drain_live_events` inline, unchanged.
- `crates/gclient/tests/reconciliation.rs` — no-edit-reason: calls `fetch_roster` inline, unchanged.
- `crates/gclient/tests/workspace.rs` — no-edit-reason: calls `fetch_roster` inline, unchanged.
- `crates/gclient/tests/daemon_live.rs` — no-edit-reason: calls `drain_live_events`, unchanged.

`apply_live_event` (`crates/gclient/src/app/live.rs`) becomes sync and sets
`pending_refetch { roster, attention, sidebar }` flags that the loop drains into
coalesced `Roster` and `Attention` jobs (`Lagged` keeps the relist and sidebar
refetch #22747 shipped beside the loop, X2, and sets the attention flag);
`handle_live_event` in `crates/gclient/src/app/live_loop/reconnect.rs` (~54-75)
drops its await on the now-sync `apply_live_event` and does the drain. `fetch_roster` gains a sync
`issue_roster_fetch` sibling in `crates/gclient/src/app/live/relist.rs`, beside the
shipped `start_relist`, used by the loop paths, while the inline async
`fetch_roster` stays for `reconcile_subscribe_first` and the tests. `focus_project`
selects immediately and issues a scoped `Roster`; `restore_focused` runs when it
lands if the project is still selected.

Every sidebar/REST job records its start instant. The existing `optional` DEBUG
becomes one WARN per failed REST component with operation, project, error class and
`elapsed_ms`; the aggregate outcome still applies any successful rows. Both the
websocket receiver's `RecvError::Lagged(skipped)` branch and a decoded
`DaemonEvent::Lagged` emit one WARN with source, skipped count when available,
generation and `elapsed_ms` before scheduling refetch jobs. **Restraint rung 2:**
reuse the job outcome and existing tracing subscriber; add no lag watchdog or
metrics pipeline.

`crates/gclient/src/app/live_loop.rs` is at 976 raw lines before R1 and
`crates/gclient/src/app/live_loop/projects.rs` at 867: this is a split, not
growth. The refetch drain and the new outcome arms move into
`crates/gclient/src/app/live_loop/jobs_apply.rs`; `handle_live_event` only sets and
drains flags, and `focus_project` only swaps its await for an issue call.

**Granularity:** one outcome, event-driven data refresh without awaits. It
introduces the `Roster` and `Attention` outcome family that A4b's spawn chain
reuses.

**Cutover:** A4a changes gclient and does not promote independently; after all
leaves pass, it participates in the single shared clean-cutover gate in
Constraints.

**Research context:** Observed on 0.5.0 `7d33430cc2`: `apply_live_event` (`live.rs`
~361) awaits `fetch_roster` (~374, ~429, ~448), `fetch_attention` (~457) and
`open_unresolved_terminals` (~490) on roster-changing events; `focus_project`
(`projects.rs` ~34-55) awaits a scoped roster. `GIT_REFRESH_INTERVAL`/
`ROSTER_REFRESH_INTERVAL` polls live in `crates/gclient/src/app/sidebar_model.rs`;
their request functions live in `live_sidebar.rs` and B3 gates them. Planned
verification: `cargo nextest run -p gobby-client --test loop_liveness`.
**Restraint rung 2:** extend the shared A1 job ledger and existing mock-daemon hold
points; add no second action queue.

**Acceptance:**

- A4a.1 - A roster for a project no longer focused is dropped (was A4.3). test:
  `crates/gclient/tests/loop_liveness.rs::a_roster_for_a_project_no_longer_focused_is_dropped`.
- A4a.2 - `apply_live_event` is sync and sets refetch flags instead of awaiting
  (was A4.5). symbol: `apply_live_event`.
- A4a.3 - Failed REST components and both `Lagged` entry paths emit WARN records
  with operation/source, generation, error class or skipped count, and
  `elapsed_ms`, while successful partial rows still apply (was A4.8). test:
  `crates/gclient/tests/loop_liveness.rs::rest_failures_and_lagged_events_warn_with_elapsed_time`.

### A4b Lifecycle and shell-adoption actions as chained jobs [category: code] (depends: A4a)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live_loop/lifecycle.rs`
- `crates/gclient/src/app/live_loop/local_adoption.rs`
- `crates/gclient/src/app/live_loop/workspace_actions.rs::*` — scope-reason: `spawn_owned_live_shell` issues its create, adoption and attach steps as chained jobs
- `crates/gclient/src/app/live_loop/terminal_location.rs::*` — scope-reason: the location move marks its pane due instead of awaiting the attach
- `crates/gclient/src/app/live_loop/jobs.rs`
- `crates/gclient/src/app/live_loop/jobs_apply.rs`
- `crates/gclient/tests/loop_liveness.rs`

Spawn shell (`lifecycle.rs`): the task does `terminal_create` only; the `Spawned`
outcome registers the pending spawn, computes the placement op from current `Chrome`
and issues it plus A4a's `Roster` job. #22883's gclient-owned shell path runs the
same way: `spawn_owned_live_shell` and the adoption chain (`adopt_local_tab`,
`spawn_in_adopted_local_tab`, `rollback_adopted_tab`) issue each `workspace_op` step
(`TabCreate`, `PaneSplit`, `PaneResize`, `TabMove`, rollback `TabClose`) from the
previous step's outcome, and a refused step issues the rollback. Every action chain
ends by marking its pane due for `start_due_attaches` (Decision 9) instead of
awaiting `attach_ready_panes`; `terminal_location.rs` does the same. `TabClose`
after every kill: a `CloseTabPlan` join in the ledger.

Committed stale results. A1's gate drops stale outcomes for UI state, but a
`Spawned` result or an adoption-chain `WorkspaceOp` success reports a mutation the
daemon already committed. When such an outcome is stale, or its target tab or
project is gone, the gate records it in `JobLedger.committed_orphans` with its
captured identity (the created `terminal_id`, or the adopted tab id), separately
from UI apply. Disposal runs once an orphan and an authoritative current model
both exist. An orphan recorded after the current generation has already
installed its workspace model (the staged reconnect `attach`, X3) is disposed at
once against that model. An orphan recorded before that install waits in
`committed_orphans`, and the install drains it. A same-generation target
removal disposes at once. Either completion order reaches exactly one
disposal:
- A created terminal the model already places in a live tab is kept.
- An unplaced created terminal is killed by one current-generation
  `terminal_kill` job.
- An adopted tab still present runs the chain's existing `rollback_adopted_tab`
  `TabClose` as one current-generation job; an absent tab needs nothing.

Disposal outcomes are owned. Each disposal is a ledger job keyed by the captured
identity, and the entry leaves `committed_orphans` only when that job settles
with a success or a not-found refusal. A refused, failed or unanswered
`terminal_kill` or `TabClose` keeps its entry, marked with the error. It raises
one status toast naming the terminal or tab and is evaluated again against the
next installed model. A terminal that model already places, or a tab it no
longer holds, clears the entry without another job. Re-evaluation happens only
on a model install, so a failing disposal never loops.

A request whose reply never arrived is uncertain. gclient cannot attribute a
daemon terminal to it, because `terminal_create` carries only a client
`request_id` (`crates/gclient/src/daemon/live_connect.rs` ~244). It is neither
replayed nor compensated, and the reconnect model is authoritative. No job is
cancelled.

`crates/gclient/src/app/live_loop/workspace_actions.rs` is at 932 raw lines: R1's
move of the adoption group into `crates/gclient/src/app/live_loop/local_adoption.rs`
leaves this deliverable converting `spawn_owned_live_shell` in place and the
adoption chain in the new module.

**Granularity:** one outcome, user-initiated terminal lifecycle actions as
chained jobs. Spawn, adoption, close and location move share the `Spawned`,
`Terminated` and `WorkspaceOp` chaining this leaf adds, and each test exercises one
path end to end.

**Cutover:** A4b changes gclient and does not promote independently; after all
leaves pass, it participates in the single shared clean-cutover gate in
Constraints.

**Research context:** Observed on 0.5.0 `7d33430cc2`: `spawn_live_shell`
(`actions.rs` ~763-826 before R1) awaits `terminal_create`, placement op, roster and
focus serially; `spawn_owned_live_shell` (`workspace_actions.rs` ~426-536) and the
adoption chain (~149-423) await up to five `workspace_op` round trips plus roster
and attach; `close_live_tab` (~584-652 before R1) kills every pane then sends
`TabClose`. Incident mapping: split-right runs the create/placement/roster chain
owned here, while its focus change uses A2's release/take job and a resulting attach
uses A3. Holding any one reply may stall that action's outcome, but no step awaits
inside `run_live_loop`. Planned verification: `cargo nextest run -p gobby-client
--test loop_liveness`.

**Acceptance:**

- A4b.1 - A held `terminal_create` keeps the window interactive and places the pane
  after the create lands (was A4.1). test:
  `crates/gclient/tests/loop_liveness.rs::a_held_terminal_create_keeps_the_window_interactive_and_places_after_create`.
- A4b.2 - Closing a tab issues `TabClose` only after every kill settles (was A4.2).
  test:
  `crates/gclient/tests/loop_liveness.rs::closing_a_tab_issues_tab_close_only_after_every_kill_settles`.
- A4b.3 - Two new tabs before the first create lands place both (was A4.4). test:
  `crates/gclient/tests/loop_liveness.rs::two_new_tabs_before_the_first_create_lands_place_both`.
- A4b.4 - Splitting right while the old connection's release/take or terminal-create
  reply is held keeps direct input, frame ingest and rendering live; the placement
  is applied only after its own job settles (was A4.6). test:
  `crates/gclient/tests/loop_liveness.rs::split_right_with_a_held_control_or_create_reply_keeps_the_window_live`.
- A4b.5 - A held `TabCreate` in the gclient-owned shell adoption chain keeps frames,
  ticks and direct input live; the chain resumes from each outcome, and a refused
  step rolls back the adopted tab (was A4.9). test:
  `crates/gclient/tests/loop_liveness.rs::a_held_adoption_step_keeps_the_window_live_and_rolls_back_on_refusal`.
- A4b.6 - A `terminal_create` reply that lands after the loop's generation changed
  kills the unplaced terminal exactly once after the new workspace installs and
  places no pane. A create whose connection is lost before its reply is neither
  replayed nor compensated. test:
  `crates/gclient/tests/loop_liveness.rs::a_stale_committed_create_is_compensated_once_and_an_unanswered_create_is_not_replayed`.
- A4b.7 - An adoption `TabCreate` reply that lands after its target project closed
  runs one rollback `TabClose` for the captured tab. test:
  `crates/gclient/tests/loop_liveness.rs::a_stale_committed_adoption_step_rolls_back_its_captured_tab`.
- A4b.8 - A stale create reply is disposed exactly once in both orders: arriving
  after the new generation's model is installed, it is killed at once; arriving
  before, it is killed when that model installs. A disposal kill whose reply is
  lost keeps its entry and raises one toast. The next install re-evaluates the
  entry and clears it once the terminal is gone. test:
  `crates/gclient/tests/loop_liveness.rs::a_stale_create_is_disposed_once_in_either_order_and_a_failed_disposal_stays_owned`.

### A4c Orphan inventory and destroy fan-out as jobs [category: code] (depends: A4b)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live_loop/orphans.rs::*` — scope-reason: `fetch_orphans` becomes a job and the destroy dialog's kills fan out with a `DestroySummary` reducer
- `crates/gclient/src/app/live_loop/jobs.rs`
- `crates/gclient/src/app/live_loop/jobs_apply.rs`
- `crates/gclient/tests/loop_liveness.rs`

`fetch_orphans` becomes a job, and the destroy dialog's kills fan out concurrently
with a `DestroySummary` reducer that keeps one result per requested orphan. The
dependency on A4b orders the shared `jobs.rs` and `jobs_apply.rs` edits; A4c
consumes no A4b interface.

**Cutover:** A4c changes gclient and does not promote independently; after all
leaves pass, it participates in the single shared clean-cutover gate in
Constraints.

**Research context:** Observed on 0.5.0 `7d33430cc2`: `fetch_orphans`
(`orphans.rs` ~90-113) and the destroy dialog kill orphans one at a time. Planned
verification: `cargo nextest run -p gobby-client --test loop_liveness`.

**Acceptance:**

- A4c.1 - Holding orphan inventory leaves the dialog and window live; after release,
  orphan kills fan out concurrently and `DestroySummary` preserves one result per
  requested orphan regardless of completion order (was A4.7). test:
  `crates/gclient/tests/loop_liveness.rs::held_orphan_fetch_and_kill_fanout_are_nonblocking_and_complete`.

### A5 Launch and reconnect audit and WARN bracket [category: code] (depends: A4c)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live_loop/reconnect.rs::*` — scope-reason: the reconnect job records its start, emits the start and finish WARN, and drops a stale-generation outcome
- `crates/gclient/src/app/live_loop/startup.rs::*` — scope-reason: the staged roster outcome passes the same generation gate
- `crates/gclient/tests/loop_liveness.rs`

Consumers unchanged:
- `crates/gclient/tests/startup_latency.rs` — no-edit-reason: covers the shipped staged launch and reconnect jobs (X3), unchanged.
- `crates/gclient/tests/reconciliation.rs` — no-edit-reason: calls `reconnect_daemon_ws` inline, unchanged signature.
- `crates/gclient/tests/ws_golden.rs` — no-edit-reason: calls `reconnect_daemon_ws` inline, unchanged signature.

Status 2026-09-27: #22755 shipped the launch and reconnect reconcile as staged
futures the loop polls (X3): `connect`, `attach` and `roster` in
`crates/gclient/src/app/live_loop/startup.rs`, and a sync `handle_reconnect_outcome`
in `crates/gclient/src/app/live_loop/reconnect.rs`. The planned
`live_reconcile.rs` is superseded. A5 keeps three obligations.

A reconnect or roster outcome whose generation is no longer current is dropped,
and a newer attempt's outcome applies.

The reconnect job records one WARN when reconnect/reconcile starts and one terminal
WARN when it finishes, carrying reason, old/new generation, outcome and
`elapsed_ms`; joined callers do not duplicate either record. A successful finish
remains WARN in this diagnostic release so every freeze-relevant reconnect bracket
is present in `gclient.log`. **Restraint rung 2:** log at the single reconnect job
owner rather than adding per-caller logging or a watchdog.

The final audit maps every daemon-touching `run_live_loop` branch or post-select
helper to a held-request case. It includes the #22745 case: with a REST request
held (`enqueue_held` on `GET /api/agents/runs`), a chrome-only prefix chord such as
`prefix+b` applies immediately and exactly once, where the #22745 capture showed
three chords applying only after the daemon answered.

**Cutover:** A5 changes gclient and does not promote independently; after all
leaves pass, it participates in the single shared clean-cutover gate in
Constraints.

**Research context:** Observed on 0.5.0 `7d33430cc2`: `run_live_loop`
(`live_loop.rs` ~518) applies `handle_reconnect_outcome`; `reconnect.rs` exposes
`begin_reconnect`, `await_reconnect_job` and `handle_reconnect_outcome` (~77);
`startup.rs` holds the staged `StartupFuture`s; `reconnect_daemon_ws` (`live.rs`
~507) and `reconcile_subscribe_first` (~284) stay inline for tests; no reconnect
WARN exists. `ReconnectSupervisor` (`crates/gclient/src/app/run_loop.rs`) schedules
attempts. Planned verification: `cargo nextest run -p gobby-client --test
loop_liveness --test startup_latency --test reconciliation --test ws_golden`.

**Acceptance:**

- A5.2 - A reconcile from a stale generation is dropped. test:
  `crates/gclient/tests/loop_liveness.rs::a_reconcile_from_a_stale_generation_is_dropped`.
- A5.4 - A bounded final audit enumerates launch/reconcile, control, input,
  daemon-event, frame-recovery, sidebar, resize/tick attach, focus-hint, geometry,
  attention, unresolved-open, roster/orphan, lifecycle-action and shell-adoption
  paths; every daemon-touching `run_live_loop` branch or post-select helper maps to
  a held-request case that advances frames, ticks and unaffected direct input. test:
  `crates/gclient/tests/loop_liveness.rs::every_run_live_loop_daemon_path_is_issued_without_awaiting`.
- A5.5 - Each reconnect/reconcile attempt emits exactly one start and one finish
  WARN with reason, generations, outcome and `elapsed_ms`, including success,
  timeout and transport-loss cases. test:
  `crates/gclient/tests/loop_liveness.rs::reconnect_attempts_warn_once_at_start_and_finish_with_reason`.
- A5.6 - With `GET /api/agents/runs` held, a prefix chord toggles the sidebar
  immediately and applies once. test:
  `crates/gclient/tests/loop_liveness.rs::a_chrome_chord_applies_while_a_rest_request_is_held`.

## P2: daemon health and load shedding
`kind: framing`

**Goal:** the client observes daemon responsiveness from the age of its own
in-flight requests, says so on the status line, and stops polling while the daemon
is slow. Every P2 production deliverable changes the gclient binary and therefore
uses the shared clean-cutover gate after merge; none promotes independently.

### B1 Daemon health from in-flight age [category: code] (depends: A5)
`kind: deliverable`

Targets:
- `crates/gclient/src/daemon/live.rs::*` — scope-reason: `LiveState` waiters gain `issued_at`, set by `register_waiter`, and `oldest_inflight_age` reads them
- `crates/gclient/src/daemon/live_reader.rs::*` — scope-reason: `handle_inbound` and `fail_waiters` take the reply sender from the new timestamped waiter entry
- `crates/gclient/src/app/live_loop/health.rs`
- `crates/gclient/src/app/live_loop/health/tests.rs`
- `crates/gclient/src/app/live_loop.rs::*` — scope-reason: the `WorkspaceView` impl gains `daemon_health` and the render tick derives it
- `crates/gclient/src/ui/chrome.rs::WorkspaceView`
- `crates/gclient/src/ui/status.rs::render_status_line`
- `docs/guides/gclient-user-guide.md`
- `crates/gclient/tests/loop_liveness.rs`

Consumers unchanged:
- `crates/gclient/src/ui/chrome_render.rs` — no-edit-reason: `render_navigation_chrome` is generic over `WorkspaceView`; the new trait method has a default.
- `crates/gclient/src/app/live_loop/mouse/pointer.rs` — no-edit-reason: `pane_scrollbar` is generic over `WorkspaceView`; the new trait method has a default.
- `crates/gclient/tests/parity/status.rs` — no-edit-reason: renders the scripted workspace, whose health is `Ready`, so the status line is unchanged.

Add `issued_at: Instant` to the waiter entries in `LiveState` (`requests`, `writes`,
`controls`) and `LiveDaemon::oldest_inflight_age() -> Option<Duration>`. On the
render tick derive `DaemonHealth::{Ready, Slow(Duration), Unreachable}`: `Slow`
once the oldest in-flight request is older than `SLOW_DAEMON_THRESHOLD` (1 s),
`Unreachable` on disconnect (today's `daemon_ready == false`). `WorkspaceView` gains
`fn daemon_health(&self) -> DaemonHealth` with a `Ready` default; the
`Workspace<LiveDaemon>` impl in `live_loop.rs` computes it. `render_status_line`
shows `Daemon slow (2.3 s)` in the palette's warning colour as the healthy-transport
alternative to the existing red `× Daemon unreachable` branch, which keeps its
separately styled `· retrying in N s` countdown and agent counts; this is UI text, so the executor loads the `impeccable`
skill and `.impeccable.md` before touching it. The guide's status-line section names
the new state.

`crates/gclient/src/app/live_loop.rs` is at 976 raw lines before R1;
`crates/gclient/src/daemon/live.rs` (477) and `crates/gclient/src/ui/chrome.rs` (771)
have headroom since the `daemon/live.rs` split: the enum, threshold and
derivation are a move into new `crates/gclient/src/app/live_loop/health.rs` rather
than growth in `crates/gclient/src/daemon/live.rs`, `crates/gclient/src/app/live_loop.rs`
or `crates/gclient/src/ui/chrome.rs`, which gain only the field, the impl method and
the trait method.

**Cutover:** B1 changes gclient and does not promote independently; after all
leaves pass, it participates in the single shared clean-cutover gate in
Constraints.

**Research context:** Observed: `LiveState` (`crates/gclient/src/daemon/live.rs`
~48-67) keeps `requests`, `writes` and `controls` maps of `ReplySender` keyed by
request id, filled by `LiveDaemon::register_waiter` (~364-419) and cleared by
`remove_waiter` (~421-438); nothing records issue time. `daemon_ready` is read for
the status line through `WorkspaceView::daemon_ready` (`crates/gclient/src/ui/chrome.rs`
~42-57, implemented for `Workspace<LiveDaemon>` in `live_loop.rs` ~52-90);
`render_status_line` (`crates/gclient/src/ui/status.rs` ~342 onward) renders
`× Daemon unreachable` in `p.red` with a separately styled `· retrying in N s`
countdown and the agent counts when `!ws.daemon_ready()` (~393-400; the status
test at ~741 asserts ` × Daemon unreachable · retrying in 3 s`). Protocol-level
`Ping`/`Pong` frames are discarded in `crates/gclient/src/daemon/live_reader.rs`
`decode_frame` and stay so. Rejected: an idle keepalive; passive health derives
only from organic in-flight requests, while transport loss follows the existing
disconnect path. Planned verification: `cargo
nextest run -p gobby-client --test loop_liveness`; screen goldens unchanged
(`GOBBY_UPDATE_SCREENS=1` only if a fixture renders a slow daemon).

**Acceptance:**

- B1.1 - Every waiter records `issued_at` and `oldest_inflight_age` reports the
  oldest. symbol: `LiveState`.
- B1.2 - `DaemonHealth` derives `Slow` past the threshold and `Unreachable` on
  disconnect. test:
  `crates/gclient/src/app/live_loop/health/tests.rs::health_is_slow_once_the_oldest_in_flight_request_passes_the_threshold`.
- B1.3 - The status line shows `Daemon slow` after 1 s of a held `workspace_op` and
  clears when the reply lands. test:
  `crates/gclient/tests/loop_liveness.rs::a_slow_workspace_op_reply_shows_daemon_slow_until_it_lands`.
- B1.4 - The guide's status-line section describes the slow state. behavior:
  "Daemon slow" in `docs/guides/gclient-user-guide.md`.

### B3 Shed load while slow [category: code] (depends: B1)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live_sidebar.rs::*` — scope-reason: the git and roster poll functions gate on `DaemonHealth`
- `docs/guides/gclient-user-guide.md`
- `crates/gclient/tests/loop_liveness.rs`

The sidebar's `GIT_REFRESH_INTERVAL` (10 s) and `ROSTER_REFRESH_INTERVAL` (15 s)
polls (`request_git_refresh_if_due`, `request_roster_refresh_if_due`, in
`live_sidebar.rs`) do not fire while `DaemonHealth` is `Slow` or
`Unreachable`; they resume on `Ready`. Attach retries keep their backoff
(`ATTACH_RETRY_BASE`/`ATTACH_RETRY_MAX` in `crates/gclient/src/app/attach.rs`).
Focus-hint and geometry sends are coalesced by A1 and need no gating.

The gate is a health check inside the two poll functions themselves, which live
in `crates/gclient/src/app/live_sidebar.rs` and derive `DaemonHealth`
from `LiveDaemon::oldest_inflight_age` and `daemon_ready` through the B1 helper;
the loop tick is untouched.

**Cutover:** B3 changes gclient and does not promote independently; after all
leaves pass, it participates in the single shared clean-cutover gate in
Constraints.

**Research context:** Observed: `GIT_REFRESH_INTERVAL` and `ROSTER_REFRESH_INTERVAL`
are constants in `crates/gclient/src/app/sidebar_model.rs` (~20-24);
`request_git_refresh_if_due` and `request_roster_refresh_if_due` (`live.rs` ~376-393
before R1) compare `git_refreshed_at`/`roster_refreshed_at` on `Workspace` and set
`pending_sidebar`; the sidebar fetch runs as `SidebarFetchFuture` from the tick. The
sidebar's git status request hits `/api/source-control/status`, which the mock
daemon records. Planned verification: `cargo nextest run -p gobby-client --test
loop_liveness`.

**Acceptance:**

- B3.1 - No sidebar `/api/source-control/status` or roster refresh request is
  recorded while the daemon is slow, and polling resumes on `Ready`. test:
  `crates/gclient/tests/loop_liveness.rs::sidebar_polls_pause_while_the_daemon_is_slow_and_resume_on_ready`.
- B3.2 - The guide's status-line section says polling pauses while slow. behavior:
  "pauses" in `docs/guides/gclient-user-guide.md`.

## P3: direct panes keep their host grant across daemon disconnects
`kind: framing`

**Goal:** gclient and gterm launch, discover local native panes, attach, render and
type with the daemon down or absent. The existing authenticated frame socket owns
that degraded path. gterm grants the first attached local client fallback authority
only when no daemon control owner and no holder exist; daemon grants replace it when
lease arbitration returns. Existing daemon grants survive disconnect until replaced
or explicitly revoked. Every P3 production deliverable participates in the shared
clean-cutover gate; none promotes independently.

### C0 Native inventory and fallback input authority in gterm [category: code] (depends: B3, A5)
`kind: deliverable`

Targets:
- `crates/gterminal/src/protocol/wire_types.rs::*` — scope-reason: append the authenticated native-inventory request/response shapes without renumbering existing messages
- `crates/gterminal/src/host/frames.rs::handle_connection`
- `crates/gterminal/src/host/state.rs::*` — scope-reason: expose native inventory and track which frame attachment owns a local fallback grant
- `crates/gterminal/src/host/write.rs::HostState::grant_input`
- `crates/gterminal/src/host/write.rs::HostState::revoke_input`
- `crates/gterminal/src/host/write.rs::HostState::frame_input`
- `crates/gterminal/src/host/control.rs::dispatch`
- `crates/gterminal/tests/frame_protocol.rs::*` — scope-reason: add daemonless inventory, first-holder and refusal cases to the frame protocol suite
- `crates/gterminal/tests/control_protocol.rs::*` — scope-reason: add daemon takeover/revoke cases through the real control protocol
- `crates/gterminal/tests/wire_golden.rs::*` — scope-reason: cover the appended inventory request and response variants
- `docs/contracts/gterm-protocols.md`

Consumers unchanged:
- `crates/gterminal/src/host/mod.rs` — no-edit-reason: The accept loop keeps the same `frames::handle_connection(stream, state)` signature at current line 111.
- `crates/gclient/src/app/workspace_panes.rs` — no-edit-reason: `record_source_message` matches `ServerMessage` with a catch-all arm (~87), so the appended `NativeTerminals` variant compiles unchanged.
- `crates/gclient/src/frame_source/proxy.rs` — no-edit-reason: its `ServerMessage` mapping (~151) and `ClientMessage` mapping (~184) end in catch-all arms.
- `crates/gterminal/src/host/embed.rs` — no-edit-reason: its only `ServerMessage` match (~340) ends in a catch-all arm.
- `crates/gterminal/src/protocol/wire.rs` — no-edit-reason: the codec is serde-derived from `wire_types.rs`, so the appended variants need no hand-written arm.

Append `ListNativeTerminals` to `ClientMessage` and `NativeTerminals` to
`ServerMessage`. The response carries `host_epoch` plus committed live native rows
with stable `terminal_id`, `host_terminal_id`, title, rows and columns; it excludes
tmux/observer slots (`TerminalSlot.locator.is_some()`). The request is available
only after the existing `Hello` authenticates `local_cli_token` on the owner-only
frame socket. Existing message tags keep their order and `PROTOCOL_VERSION` does not
change because only the newly paired gclient requests the appended response.

Extend `TerminalSlot` with the authenticated frame-connection id that owns local
fallback input, separate from the attachment id currently bound on that stream.
`HostState::bind_attachment` atomically records the bound attachment id and, when
`Inner.control_owners` and both authority slots are empty, installs that frame
connection as the fallback holder. Rebinding a new attachment id on the same
owning stream preserves fallback input; a second local connection cannot replace
it and receives the existing `InputRefused{input_not_granted}` on `Input`/`Paste`,
so degraded mode has no takeover path.

When the last daemon control owner disconnects, a daemon grant whose attachment is
bound on a live frame stream transfers atomically to that stream's fallback owner;
this is the host half of C1's restart continuity. An arriving control owner blocks
new fallback claims but does not clear the existing one. Its next `grant_input`
installs the daemon attachment id and clears the fallback owner atomically, so
normal lease/takeover arbitration resumes without an input gap. Detaching the
owning frame connection clears only its fallback; detaching another connection
cannot. Matching or unconditional `revoke_input` clears the daemon grant and any
fallback owned by that same attachment/stream, while unrelated revoke/detach work
cannot clear the holder. `frame_input` admits exactly the daemon grant or the one
fallback connection. **Restraint rung 2:** reuse the current local token, frame
socket, `input_grant` comparison and refusal surface; add one provenance field, no
new socket, TTL, election or background task.

Control-connection fence. `is_mutating` (`host/control.rs` ~327) excludes
`grant_input` and `revoke_input`, so `handle_connection` dispatches them as
independent tasks. A request the daemon has given up on can therefore still land
after a newer one. `TerminalSlot` gains `grant_conn: Option<u64>`, the control
connection id that last changed its input authority, and `dispatch` passes its
`conn_id` to both verbs. A request from a connection id lower than `grant_conn`
changes nothing and answers `{"ok": false, "error": "stale_grant_connection"}`.
Any other request applies and records its `conn_id`. `HostState::alloc_conn` is a
monotonic `fetch_add`, so a later control connection always carries a larger id.
D3 retires the daemon's control connection whenever a grant, revoke or mutating
request is abandoned by timeout or by cancellation after its bytes may have been
written, so a request issued after an abandonment always travels on a newer
connection. A late effect from the retired connection then either lands before
the newer connection's first authority change, which overwrites it, or after it,
and is refused. Newest authority wins in both orders without a watchdog or a
cross-connection ordering assumption. The refusal reaches only the retired
connection, whose waiter is already gone. Frame-side fallback claims and detaches
leave `grant_conn` unchanged. Wire-enum sweep: `frames::handle_connection` holds
the only exhaustive `ClientMessage` match and is a Target; every other match on
either enum ends in a catch-all arm (Consumers unchanged).

**Granularity:** inventory and fallback authority land together because they are the
two server halves of one authenticated daemonless frame-session contract and the
wire enums, frame dispatcher and host state must compile atomically. The
connection fence is a second provenance field on the same authority slot, changed
by the same `grant_input` and `revoke_input` edits. The gclient
consumer is split into C0b/C0c.

**Cutover:** C0 changes gterm and does not promote independently. After all leaves
pass, build with `cargo build --release -p gobby-terminal --features vt-engine --bin
gterm`, promote through `install_gterm_from_submodule` inside the single announced
quiet window, restart gterm and relaunch the validation gclient.

**Research context:** Observed: `ClientMessage` and `ServerMessage` live in
`crates/gterminal/src/protocol/wire_types.rs`; `frames::handle_connection` already
authenticates `local_cli_token`, handles `AttachTerminal`/`BindAttachment`, and
routes input to `HostState::frame_input`. `HostState::list_json` already builds host
inventory; `TerminalSlot.locator.is_none()` is the same native test used by input
admission. `Inner.control_owners` is populated by authenticated control `hello` and
cleared by `on_control_disconnect`. `bind_attachment` currently stores the client
id without granting it, while `grant_input`/`revoke_input` own
`TerminalSlot.input_grant`. `detach` already has both frame attachment and slot.
Planned verification: `cargo nextest run -p gobby-terminal --test frame_protocol
--test control_protocol --test wire_golden`; `cargo clippy -p gobby-terminal
--all-targets --features vt-engine`.

**Acceptance:**

- C0.1 - An authenticated frame client lists only committed live native terminals
  with stable terminal/host ids and host epoch; unauthenticated and tmux inventory
  is unavailable. test:
  `crates/gterminal/tests/frame_protocol.rs::daemonless_native_inventory_is_authenticated_and_excludes_tmux`.
- C0.2 - With no control owner or grant, the first bound local attachment can type
  and paste; a second attachment cannot replace it and receives
  `input_not_granted`. test:
  `crates/gterminal/tests/frame_protocol.rs::first_local_attachment_holds_fallback_input_until_detach`.
- C0.3 - Detaching clears its local fallback, while detaching another frame stream
  cannot clear either the fallback or a daemon grant. test:
  `crates/gterminal/tests/frame_protocol.rs::frame_detach_clears_only_its_local_fallback_grant`.
- C0.4 - A control owner disables new fallback authority; its `grant_input`
  replaces an existing fallback, and matching/unconditional revoke clears the
  resulting holder. test:
  `crates/gterminal/tests/control_protocol.rs::daemon_grant_replaces_local_fallback_and_restores_arbitration`.
- C0.5 - The protocol contract and wire goldens describe the authenticated native
  inventory and local-fallback/daemon-takeover rules. behavior: "local fallback
  holder" in `docs/contracts/gterm-protocols.md`.
- C0.6 - Last-control-owner disconnect transfers a bound daemon grant to that same
  frame connection; rebinding it to the fresh reconnect attachment id preserves
  input until the next daemon grant replaces the fallback. test:
  `crates/gterminal/tests/control_protocol.rs::disconnect_and_rebind_transfer_the_daemon_grant_without_an_input_gap`.
- C0.7 - Over two real control connections, a `grant_input` sent on the older
  connection after the newer one revoked or granted the same terminal is refused
  `stale_grant_connection` and leaves the newer holder in place. The same late
  request before any newer authority change applies and is then replaced. test:
  `crates/gterminal/tests/control_protocol.rs::a_late_grant_from_a_retired_connection_cannot_overwrite_newer_authority`.

### C0b Read native inventory through the existing frame socket [category: code] (depends: C0)
`kind: deliverable`

Targets:
- `crates/gclient/src/frame_source.rs::*` — scope-reason: declare the native-host submodule and move the shared direct hello/read/write helpers needed by inventory out of the near-ceiling file
- `crates/gclient/src/frame_source/native_host.rs`
- `crates/gclient/tests/frame_source_live.rs::*` — scope-reason: add a real-gterm daemonless inventory/attach/input integration case

New `crates/gclient/src/frame_source/native_host.rs` owns
`NativeHost::connect(socket, local_token, viewport)`, `list_native()` and
`attach(row, client_attachment_id)`. It reuses the existing length-prefixed codec,
`Hello`, local token and `UnixSocketFrameSource` attach path. Inventory has a 2 s
aggregate local-host deadline covering connect, hello and list; a missing or
unresponsive gterm returns a typed local-host-unavailable result and never touches
the daemon. Move only the shared direct handshake helpers needed by this module out
of the 950-line `frame_source.rs`; no second codec or connection pool.
**Restraint rung 2:** reuse the frame transport and direct-source constructor.

**Cutover:** C0b changes gclient and does not promote independently; after all
leaves pass it participates in the shared quiet-window build and separate gclient
promotion.

**Research context:** Observed: `UnixSocketFrameSource::connect` opens the socket
and `connect_stream` performs `Hello` then `AttachTerminal`; `frame_source.rs` is
950 lines and must split rather than grow. `crates/gclient/tests/frame_source_live.rs`
already builds a real test gterm, spawns a native terminal, reads frames and proves
grant/refusal behavior. Planned verification: `cargo nextest run -p gobby-client
--test frame_source_live`; `cargo clippy -p gobby-client --all-targets`.

**Acceptance:**

- C0b.1 - `NativeHost` uses the existing authenticated frame codec and returns
  typed native inventory without any daemon request. file:
  `crates/gclient/src/frame_source/native_host.rs`.
- C0b.2 - Against a real gterm with no lasting control connection, gclient lists a
  native terminal, attaches it and receives frames within one 2 s aggregate local
  deadline. test:
  `crates/gclient/tests/frame_source_live.rs::native_inventory_and_attach_work_without_a_daemon`.
- C0b.3 - Missing, refused and timed-out local-host probes return typed outcomes and
  leave no reader/writer task or socket alive. test:
  `crates/gclient/tests/frame_source_live.rs::native_inventory_failure_cleans_up_the_frame_connection`.

### C0c Launch, render and type native panes with no daemon [category: code] (depends: C0b, A5)
`kind: deliverable`

Targets:
- `crates/gclient/src/startup.rs::*` — scope-reason: an absent default daemon token becomes a degraded capability while an explicit bad token path still errors
- `crates/gclient/src/views/mod.rs::run_ready`
- `crates/gclient/src/app/native_degraded.rs`
- `crates/gclient/src/app/live_loop.rs::*` — scope-reason: one native-inventory job slot beside `startup_job` and one select arm that hands its outcome to `native_degraded.rs`
- `crates/gclient/src/app/mod.rs::*` — scope-reason: declare the module and keep daemonless workspace state in the extracted implementation
- `crates/gclient/src/app/pane.rs::*` — scope-reason: record local fallback authority and clear its provisional state on typed refusal
- `crates/gclient/src/app/live_loop/actions.rs::*` — scope-reason: delegate daemonless tab projection to the extracted native-degraded module
- `crates/gclient/src/app/live_loop/control.rs::*` — scope-reason: test direct input before daemon readiness for daemonless panes, and move focus locally without a daemon release while the daemon is absent
- `crates/gclient/src/app/live.rs::*` — scope-reason: `install_live_rows` adopts daemonless native panes by terminal id without duplication
- `crates/gclient/tests/startup.rs::*` — scope-reason: add missing-token and daemonless launch cases beside the existing unreachable-daemon test
- `crates/gclient/tests/loop_liveness.rs`
- `docs/guides/gclient-user-guide.md`

Consumers unchanged:
- `crates/gclient/tests/client_loop.rs` — no-edit-reason: `live_entry_connects_before_running` still validates invalid-URL construction before the new degraded attachment path; its direct `run_ready` call keeps the same signature.

`resolve_probe_env_at` treats a missing default daemon token as `token=None`; an
explicit `--token-file` that is missing, unreadable or empty remains an error.
`run_ready` already enters the loop without waiting on the daemon (X3): it builds
`LiveDaemon::unconnected` and `run_live_loop` stages `connect`, `attach` and
`roster` as `startup_job` futures. C0c adds nothing to that chain. `run_ready`
passes the optional local token to the workspace, and `run_live_loop` starts one
native-inventory job beside `startup_job` at loop entry, independent of the daemon
stages. When a valid owner-only
`local_cli_token` is available, that job opens one short-lived authenticated
`NativeHost` connection, sends `Hello` plus `ListNativeTerminals`, records the
inventory and closes that connection without attaching a terminal. For every
returned row it opens a fresh authenticated frame connection, sends
`AttachTerminal` and `BindAttachment`, and installs exactly that connection as the
row's direct source. This is required because gterm's frame server stores one
`attachment_id` and one frame mailbox per connection; another `AttachTerminal` on
the same connection replaces them. New `native_degraded.rs` installs one direct
pane and one local tab per row, keyed by stable gterm `terminal_id`. A missing gterm
or missing default token opens an empty window with `Daemon unavailable; no local
native host.` rather than exiting; an explicit bad token-file path remains an
error. **Restraint rung 2:** reuse the existing authenticated frame connection for
each stream and use only a short-lived extra connection for inventory; add no
connection pool or multiplexing layer.

Each local pane gets a fresh client attachment id, binds on the frame stream, starts
Held with `lease_unconfirmed`, and sends `Input`/`Paste` directly even while
`daemon_ready == false`. An `InputRefused` clears provisional local authority and
shows take-back unavailable; it never retries or falls back through the daemon.
`sync_live_chrome` projects the flat local tabs only while no daemon workspace model
exists. When the staged startup `attach` (X3) or a reconnect later delivers the
daemon workspace, `install_live_rows` reuses panes by terminal id, preserves their
live direct source, replaces the flat tabs with daemon layout and leaves every
adopted holder Held with `lease_unconfirmed` set. C0c issues no retake; C2 owns
reconfirming those leases. Rows with no matching local inventory use the normal
daemon attach path. **Restraint rung 2:** reuse `Pane`, local tabs,
`InputRefused`, terminal ids and A5 reconcile; add no offline workspace database or
synthetic daemon model.

Offline focus. Today `move_live_focus` (`live_loop/control.rs` ~54) returns false
while `!daemon_ready()`, so focus cannot change without the daemon. While the
daemon is absent it instead moves focus locally and never calls
`release_live_control`, because no daemon release or take can complete. The
previous pane keeps `Held`, `lease_unconfirmed` and its host authority (a daemon
id-grant or C0 fallback) on its own frame stream, and A2's release barrier is not
entered. Keyboard and mouse focus reach the target pane the same way. Input then
goes to that pane's direct stream; a pane without direct authority refuses input
locally with the status `Daemon unavailable; input not sent.`. Explicit take-back,
takeover and release actions answer `Daemon unavailable; control changes wait for
reconnect.` and change no pane state. When the daemon returns, C2 retakes every
`lease_unconfirmed` pane, and ordinary focus release resumes on the next focus
move.

Native result disposal. The native-inventory job is a loop-owned future beside
`startup_job`, not a detached task. Loop exit therefore drops it and closes every
frame connection it holds, and gterm detaches each one, clearing only that
connection's fallback (C0.3). One idempotent rule in `native_degraded.rs` applies
its outcome in either completion order:
- If the loop has exited, nothing is installed.
- If a daemon workspace model is already installed (daemon-first order, including
  a daemon that has since disconnected), every source in the result is closed and
  nothing is installed, because the daemon attach path owns those terminals.
- Otherwise a row installs only when no pane for its `terminal_id` holds a live
  direct source; a row whose pane already has one closes its new source.
- A row whose terminal exited before `AttachTerminal` is skipped.

No rejected source stays open, so no extra authenticated attachment or fallback
authority survives. The native-first, daemon-later order is C0c.4's adoption.
**Restraint rung 2:** one disposal rule at the single apply point; no second
reconciliation system.

To preserve the production ceiling, move all daemonless workspace state and local
tab projection out of near-ceiling `crates/gclient/src/app/mod.rs` and
`crates/gclient/src/app/live_loop/actions.rs` into new
`crates/gclient/src/app/native_degraded.rs`; the existing files gain only the module
declaration and bounded calls. `crates/gclient/src/app/live_loop.rs` (976 raw lines
before R1) is a split too: the inventory job, its outcome apply and the pane
installation live in `native_degraded.rs`, and `live_loop.rs` gains only the job
slot and one select arm on top of R1's reduced size.

Without the daemon, the status/help text explicitly marks leases/takeover, layout
and workspace sync, roster, attention and agent relay unavailable; tmux, web and
proxied panes are not listed. Native render, scroll, copy and input remain available.

**Granularity:** nine production files form one launch-to-loop state transition:
startup capability, local inventory installation, tab projection and direct input
must land together so the new launch path never renders a pane it cannot type into.
The transport client is independently closeable and therefore split into C0b.

**Cutover:** C0c changes gclient and does not promote independently; after all
leaves pass it participates in the shared quiet-window build and separate gclient
promotion.

**Research context:** Observed on 0.5.0 `8965cc963f`: `prepare_at` already converts
an unreachable health probe into a notice, but `resolve_probe_env_at`
(`crates/gclient/src/startup.rs` ~419-448) still errors when the token file is
missing or empty. `run_ready` (`crates/gclient/src/views/mod.rs` ~40-85) builds
`LiveDaemon::unconnected(&daemon_url, token.unwrap_or_default())`, sets up the
workspace and chrome, and calls `run_live_loop`; `run_live_loop`
(`crates/gclient/src/app/live_loop.rs` ~222-227) starts `startup::connect`, and
its `startup_job` arm (~311-366) chains `attach` and `roster`. A down-daemon
`LiveDaemon` is already valid. `send_live_input` returns before testing direct input
when `daemon_ready` is false. `sync_live_chrome` currently projects only a daemon
workspace model. `ensure_live_pane` and `install_live_row` already reuse a pane by
terminal id, which is the reconnect merge seam. `gterminal`'s
`frames::handle_connection` authenticates `Hello.local_token` against the same
owner-only local token, then keeps only one `attachment_id` and `out_rx` per
connection; a second `AttachTerminal` replaces both. Planned verification: `cargo
nextest run -p gobby-client --test startup --test frame_source_live --test
loop_liveness --test reconciliation`; docs link check.

**Acceptance:**

- C0c.1 - A missing default daemon token produces an empty degraded Ready window,
  an unreachable daemon with a valid local token can use the native host, and an
  explicitly requested bad token file remains a startup error.
  test: `crates/gclient/tests/startup.rs::missing_default_daemon_token_starts_in_native_degraded_mode`.
- C0c.2 - With the daemon absent and a real gterm present, gclient launches, lists
  native panes only, attaches, renders advancing frames and delivers typed bytes to
  the PTY. test:
  `crates/gclient/tests/loop_liveness.rs::daemonless_launch_attaches_renders_and_types_native_panes`.
- C0c.3 - A competing local gclient that receives `input_not_granted` becomes
  read-only without daemon fallback, while the first holder keeps typing. test:
  `crates/gclient/tests/loop_liveness.rs::daemonless_second_client_cannot_take_over_the_local_holder`.
- C0c.4 - When the daemon connects after degraded launch, matching terminal ids
  preserve one direct source, daemon layout replaces local tabs, the adopted holder
  stays Held with `lease_unconfirmed` set, and no duplicate pane appears. test:
  `crates/gclient/tests/loop_liveness.rs::daemon_reconcile_adopts_daemonless_native_panes_without_duplication`.
- C0c.5 - Without the daemon, the UI states the unavailable enhancements and never
  lists tmux, web or proxied panes. behavior: "Native degraded mode" in
  `docs/guides/gclient-user-guide.md`.
- C0c.6 - With two native terminals and no daemon, gclient opens one independently
  attached direct pane per inventory row; frames advance and typed bytes reach the
  correct PTY on both streams. test:
  `crates/gclient/tests/loop_liveness.rs::daemonless_launch_attaches_and_types_multiple_native_panes_independently`.
- C0c.7 - With the daemon absent, focus moves A to B to A by keyboard and by mouse
  without any daemon release or take, both panes stay Held, and typed bytes reach
  both PTYs. An explicit take-back answers the reconnect status. test:
  `crates/gclient/tests/loop_liveness.rs::daemonless_focus_moves_between_direct_panes_and_types_into_both_ptys`.
- C0c.8 - A native inventory result, or a native attach result, held until after
  the daemon workspace installs is disposed: every frame connection it holds is
  closed and no duplicate pane appears. The same holds when the terminal was
  removed first, and when the loop exits while the job holds sources. test:
  `crates/gclient/tests/loop_liveness.rs::a_late_native_result_after_the_daemon_workspace_is_disposed_and_closes_its_sources`.

### C1 Keep control on disconnect for direct-granted panes [category: code] (depends: C0c)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/disconnect.rs`
- `crates/gclient/src/app/live.rs::*` — scope-reason: `reconnect_daemon_ws` keeps direct-granted panes Held instead of clearing every pane before reconcile
- `crates/gclient/src/app/pane.rs::*` — scope-reason: preserve direct control and record unconfirmed lease state across construction, disconnect and input admission
- `crates/gclient/src/app/live_loop/control.rs::*` — scope-reason: `send_live_input` tests `direct_input` before `daemon_ready`
- `docs/contracts/gterm-protocols.md`
- `docs/guides/gclient-user-guide.md`
- `crates/gclient/tests/loop_liveness.rs`

`observe_daemon_disconnect` (in `disconnect.rs` after R1) and the reconnect path's
pre-reconcile clear (`reconnect_daemon_ws` in `live.rs`) call `Pane::clear_control` on
every pane today. New rule: a pane with `Pane::direct_input() == true`, whether its
authority came from a daemon grant or C0's local fallback, keeps
`ControlState::Held`, gets `lease_unconfirmed: bool` set on `Pane`, and shows status
text `Daemon disconnected; typing continues on the host.`; every other pane clears
as today. Keystrokes keep flowing on the bound frame stream because
`Pane::send_host_input` never consults `daemon_ready`; the early return in
`send_live_input` tests `pane.direct_input()` before `daemon_ready`. The contract's
"A surviving grant does not mean typing survives a daemon outage" paragraph is
rewritten to the new contract: a bound direct attachment keeps typing; the lease is
reconfirmed on reconnect; D3b's `ws_close` cleanup finalizes daemon-side attachment
and lease state while C0 transfers the matching host grant to the bound local frame
connection. Its owning frame detach clears that fallback. Daemon-side explicit
release, detach, takeover, terminal removal and non-direct cleanup still revoke or
replace host authority. A daemon lease takeover is unavailable during the outage,
and gterm refuses a second local fallback holder. The guide's
status-string table and `Daemon restarts and reconnects` section follow.

**Cutover:** C1 changes gclient and does not promote independently; after all
leaves pass, it participates in the single shared clean-cutover gate in
Constraints.

**Research context:** Observed: `observe_daemon_disconnect` sets `daemon_ready =
false` and calls `pane.clear_control(error.to_string())` for every pane;
`reconnect_daemon_ws` clears with `"Daemon disconnected."` before reconciling;
`Pane::clear_control` (`crates/gclient/src/app/attach.rs` ~177-183) drops to
`Observe`; `Pane::direct_input` (`crates/gclient/src/app/pane.rs` ~441-443) is true
for a bound direct source with a grant; `send_live_input`
(`crates/gclient/src/app/live_loop/control.rs` ~223-237) returns early on
`!daemon_ready()` before `pane.writable()`. gterm's grant is host memory with no
TTL (`crates/gterminal/src/host/state.rs`, read-only here). The contract passage is
`docs/contracts/gterm-protocols.md` ~113-117. Memories `b59e4ce9` and `392cc53f` are
updated at close through the post-task memory review. Planned verification:
`cargo nextest run -p gobby-client --test loop_liveness`; docs link check.

**Acceptance:**

- C1.1 - A daemon-granted or local-fallback direct pane stays Held with
  `lease_unconfirmed` when the mock daemon closes the socket, and the mock frame
  host still receives `Input`. test:
  `crates/gclient/tests/loop_liveness.rs::a_direct_granted_pane_keeps_typing_through_a_daemon_disconnect`.
- C1.2 - A proxied pane drops to Observe on the same disconnect. test:
  `crates/gclient/tests/loop_liveness.rs::a_proxied_pane_drops_to_observe_on_daemon_disconnect`.
- C1.3 - The protocol contract states the new outage contract. behavior: "keeps
  typing" in `docs/contracts/gterm-protocols.md`.
- C1.4 - The guide's status-string table carries the unconfirmed-lease text.
  behavior: "typing continues on the host" in `docs/guides/gclient-user-guide.md`.

### C2 Reconfirm the lease on reconnect [category: code] (depends: C1)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live_attach.rs::*` — scope-reason: `finish_direct_attach` (~108) and `install_direct_source` (~125) offer every `lease_unconfirmed` pane to the per-pane retake schedule
- `crates/gclient/src/app/live.rs::*` — scope-reason: `install_live_rows` offers each adopted `lease_unconfirmed` pane to the per-pane retake schedule
- `crates/gclient/src/app/live_loop/control.rs::*` — scope-reason: `apply_control_outcome` clears `lease_unconfirmed` only on a confirmed host grant and schedules the bounded retry
- `crates/gclient/src/app/live_control.rs`
- `crates/gclient/src/app/pane.rs::*` — scope-reason: apply rebind/take outcomes to unconfirmed direct panes without dropping their live source
- `crates/gclient/src/frame_source.rs::*` — scope-reason: rebind the preserved direct frame stream to the fresh daemon attachment id before issuing the reconnect take
- `crates/gclient/tests/loop_liveness.rs`

After the reconcile re-attaches panes (new attachment ids from the fresh daemon),
every pane with `lease_unconfirmed` first sends the existing `BindAttachment` on
its preserved direct frame stream with the fresh daemon attachment id, then issues
`request_control` (take), not only the focused pane that `restore_focused` retakes
today. C0 keys fallback authority by that frame connection, so input continues on
the direct stream while rebind and take are pending; it is not diverted to a daemon
write or the pre-grant queue. A `granted` result with a confirmed host grant
atomically replaces the fallback with the fresh attachment id and clears
`lease_unconfirmed`. A `held`
result means the daemon granted another attachment, so this stream receives the
typed host refusal, drops to Observe and offers take-back exactly as today. A
rebind or take transport error follows the retry rule below; the single host
authority still prevents concurrent writes. **Restraint rung 2:** reuse `BindAttachment`, the preserved frame stream and
the existing take outcome; add no handoff protocol or second source.

Scheduling. Today `request_control` (`live.rs` ~210-218) writes one
`pending_control` slot and `start_control_request` consumes one request, so
offering several panes would overwrite all but the last. In `live_control.rs`
(A2's move), `pending_control` becomes a per-pane map holding the newest wish per
pane, and `start_control_request` starts every ready entry. Each pane keeps its
own `control_request` sequence, ordinary focused takes use the same map, and A2's
release-barrier dependency applies per take. The retake entry points are
`finish_direct_attach` and `install_direct_source` in `live_attach.rs` and C0c's
`install_live_rows` adoption; each offers its pane when it is `lease_unconfirmed`.

Confirmation and retry. `granted: true` confirms only the daemon lease, so
`apply_control_outcome` clears `lease_unconfirmed` only when the reply also
carries `host_input_granted: true`. The pane stays Held and unconfirmed after a
take that answers `host_input_granted: false`, or after a rebind or take
transport error while the daemon socket stays healthy. It is re-offered to the map
on the first render tick after the reconnect supervisor's existing backoff
interval, for at most three attempts. That take is the holder's repeated take,
which `TerminalLeaseRegistry.take_control` already treats as a retry after a
failed grant. The reconnect supervisor does not own this retry, because its
episode settles at `handshake_complete` (`run_loop.rs` ~621-650). After the third
failure the pane drops to Observe with take-back offered and the status `Lease not
reconfirmed; take back to type.`. It never claims typing authority that the host
has not confirmed. **Restraint rung 2:** reuse the take outcome, the repeated-take
retry and the existing backoff constants; add no retry framework.

**Cutover:** C2 changes gclient and does not promote independently; after all
leaves pass, it participates in the single shared clean-cutover gate in
Constraints.

**Research context:** Observed: `restore_focused`
(`crates/gclient/src/app/live_loop/projects.rs` ~253-266) retakes only the focused
pane; `request_control`/`start_control_request` (in `live_control.rs` after A2)
issue takes; C0c's adoption and C1's disconnect both leave panes Held with
`lease_unconfirmed`, and C2 is the one retake for either source;
`Pane::queue_input`/`take_pending_input` (`pane.rs` ~451-468) hold
pre-grant input with `MAX_PENDING_INPUT_BYTES`; `apply_control_outcome` flushes on
grant. Planned verification: `cargo nextest run -p gobby-client --test
loop_liveness`.

**Acceptance:**

- C2.1 - On reconnect every unconfirmed direct pane rebinds its preserved frame
  stream before `terminal_take_control`; bytes typed before the reply continue to
  reach the host, and `granted` clears the unconfirmed state without duplicating the
  source. test:
  `crates/gclient/tests/loop_liveness.rs::an_unconfirmed_direct_stream_rebinds_and_keeps_typing_until_retake_grants`.
- C2.2 - `lease_unconfirmed` clears on grant and a `held` reply drops the pane to
  Observe with take-back. symbol: `Pane`.
- C2.3 - After a daemonless launch (C0c), the daemon's arrival retakes every
  adopted unconfirmed holder: each rebinds its preserved stream and issues one take,
  typing continues until the grant, and no duplicate source appears. test:
  `crates/gclient/tests/loop_liveness.rs::daemon_arrival_after_degraded_launch_retakes_every_adopted_holder`.
- C2.4 - Each of three unconfirmed panes issues its own take. One take fails while
  the daemon socket stays healthy; it is retried at most three times and then
  drops that pane to Observe with take-back, while the other two confirm. test:
  `crates/gclient/tests/loop_liveness.rs::every_unconfirmed_pane_retakes_and_one_failed_take_retries_boundedly`.
- C2.5 - `granted: true` with `host_input_granted: false` keeps `lease_unconfirmed`
  set and claims no typing authority until a repeated take confirms the host
  grant. test:
  `crates/gclient/tests/loop_liveness.rs::a_lease_grant_without_a_host_grant_does_not_confirm_typing`.

### C3 Direct re-attach before proxy fallback [category: code] (depends: C2)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live_attach.rs::*` — scope-reason: `begin_proxy_recovery` retries the held direct locator before the daemon proxy path
- `crates/gclient/src/app/pane.rs::*` — scope-reason: mark a directly re-attached pane `lease_unconfirmed` with the provisional status
- `crates/gclient/tests/frame_source_live.rs::*` — scope-reason: adds the direct-reconnect case beside the existing direct-transport tests
- `crates/gclient/tests/loop_liveness.rs`
- `docs/contracts/gterm-protocols.md`
- `docs/guides/gclient-user-guide.md`

`begin_frame_recovery` (shipped by #22747, X2) sends every frame error to the
daemon proxy path through `begin_proxy_recovery` today. For a
direct native pane it first retries the direct path with the locator already held:
connect `frame_socket_path`, `AttachTerminal{host_terminal_id}`,
`BindAttachment(attachment_id)`. Two authority cases follow:
- Surviving daemon grant. It is keyed by attachment id, so the rebound stream
  types again at once.
- Lost C0 fallback. The failed frame connection owned it, and its detach cleared
  it. The replacement stream holds authority only if gterm's first-holder rule
  admits it (no control owner, no grant and no other local holder); otherwise it
  receives `InputRefused`.

gclient cannot tell these apart before input. After a direct re-attach, the pane
therefore shows `Reconnected; input authority unconfirmed.`, sets
`lease_unconfirmed` and claims nothing. An `InputRefused` drops it to Observe with
take-back, as C0c specifies. Once the daemon is ready, `install_direct_source`
offers it to C2's per-pane schedule, and the retake grants the fresh attachment
id on the new stream. Only a connect failure or a gterm refusal (`not_found`, `capacity`) falls
through to the daemon path with the existing `Pane::defer_attach` backoff. The
contract's frame-protocol section and the guide's reconnect section state the order.

**Cutover:** C3 changes gclient and does not promote independently; after all
leaves pass, it participates in the single shared clean-cutover gate in
Constraints.

**Research context:** Observed: `UnixSocketFrameSource::connect` and
`from_stream` (`crates/gclient/src/frame_source.rs` ~437-587) open the frames socket
from an `AttachLocator` (`frame_socket_path`, `host_terminal_id`, ~37-43);
`recover_proxy_source` (pre-A3) always asks the daemon; the existing test
`granted_direct_input_echoes_and_revoke_refuses` in
`crates/gclient/tests/frame_source_live.rs` runs a real mock frame host. Planned
verification: `cargo nextest run -p gobby-client --test frame_source_live --test
loop_liveness`; docs link check.

**Acceptance:**

- C3.1 - A direct source error followed by a successful direct reconnect records
  `AttachTerminal` plus `BindAttachment` and no `terminal_attach` daemon request.
  test:
  `crates/gclient/tests/frame_source_live.rs::a_direct_source_error_reattaches_directly_before_asking_the_daemon`.
- C3.2 - The contract and guide state direct-first recovery. behavior: "direct
  re-attach" in `docs/contracts/gterm-protocols.md`.
- C3.3 - With the daemon absent, a fallback holder's stream fails and the
  replacement stream reclaims fallback, so typed bytes reach the PTY. If a peer
  local client claimed first, the replacement is refused and drops to Observe.
  test:
  `crates/gclient/tests/loop_liveness.rs::a_lost_fallback_stream_reclaims_only_when_no_peer_holds_input`.
- C3.4 - A fallback stream lost after the daemon has returned, but before C2's
  retake grants, cannot reclaim fallback. Its input is refused until the retake
  grants the fresh attachment id; after that, bytes reach the PTY. test:
  `crates/gclient/tests/loop_liveness.rs::a_fallback_stream_lost_during_daemon_return_types_only_after_the_retake`.

## P4: daemon terminal handlers off the event loop and off the serial lane
`kind: framing`

**Goal:** a pool wait or the pool backoff sleep occupies an executor worker, never
the loop; one slow terminal handler no longer delays the other attachments on the
same connection; daemon-to-gterm round trips are bounded; the next pool exhaustion
is attributable. `crates/gclient`'s `attach_refusal_is_transient` is an allowlist,
so no client change follows from any new refusal code. `src/gobby/agents/spawn_executor.py`
is at 962 lines and gains nothing here.

### D1a Terminal attach, sizing, scroll, create and kill handlers on the executor [category: code]
`kind: deliverable`

Targets:
- `src/gobby/servers/websocket/terminal_ws_write.py`
- `src/gobby/servers/websocket/terminal_ws.py::*` — scope-reason: move the input, paste and operator-write path into terminal_ws_write.py, then route the attach row lookup to the executor and drop the scroll-offset lookup
- `src/gobby/servers/websocket/terminal_sizing.py::TerminalSizingMixin._apply_terminal_sizing`
- `src/gobby/servers/websocket/terminal_ws_create.py::TerminalCreateMixin._handle_terminal_create`
- `src/gobby/servers/websocket/terminal_ws_create.py::TerminalCreateMixin._handle_terminal_kill`
- `tests/servers/test_terminal_ws_attach_honesty.py::*` — scope-reason: adds the executor-routing assertion beside the attach tests
- `tests/servers/test_terminal_ws_golden.py::*` — scope-reason: adds the no-storage scroll assertion; reply shapes are unchanged
- `tests/servers/test_terminal_ws_create.py::*` — scope-reason: adds the executor-routing assertion for create and kill
- `tests/servers/test_terminal_ws_resize.py::*` — scope-reason: adds the one-hop sizing assertion
- `tests/servers/test_terminal_ws_lease.py::*` — scope-reason: keeps the sizing-patch regression beside the shared handler diagnostic added by D3b
- `tests/servers/test_terminal_ws_input.py::*` — scope-reason: the no-direct-write source assertion reads terminal_ws_write.py with terminal_ws.py

Consumers unchanged:
- `src/gobby/servers/websocket/server.py` — no-edit-reason: dispatches these handlers by message type with unchanged signatures.
- `src/gobby/servers/websocket/proxy_relay.py` — no-edit-reason: awaits `_apply_terminal_sizing` with the same signature.
- `src/gobby/servers/websocket/terminal_ws_control.py` — no-edit-reason: awaits `_apply_terminal_sizing` with the same signature.
- `src/gobby/adapters/acp_client_requests.py` — no-edit-reason: calls `_handle_terminal_create` with the same signature.
- `tests/servers/test_terminal_list_watermark.py` — no-edit-reason: calls the create and kill handlers with unchanged signatures.
- `tests/servers/test_terminal_ws_kill.py` — no-edit-reason: calls `_handle_terminal_kill` with an unchanged signature.
- `tests/terminals/test_backend_selection.py` — no-edit-reason: calls `_handle_terminal_create` with an unchanged signature.

Route every synchronous storage call in these handlers onto a worker thread with
`asyncio.to_thread`, the mechanism `terminal_list` and the shipped D1b-D1d handlers
use, one hop per handler (bundle get plus set into one function so a handler makes
one hop, not two): the attach `manager.get`;
`_apply_terminal_sizing` (get plus `set_dims` as one function), reached from
take/release/resize/detach and lease finalize; `terminal_set_scroll_offset` deletes
its row lookup and reads `backend` from the attach snapshot `record.terminal` (#22557
already stores it); create and kill. Operator writes already pass the attach
snapshot's row (`_deliver_operator_write`), so no keystroke calls the write
coordinator's `_require()`.

**Decomposition:** `terminal_ws.py` is 896 lines on 0.5.0 (#23080 grew it from 829). Before
the executor edits, move the input, paste and operator-write path into new
`src/gobby/servers/websocket/terminal_ws_write.py` as `TerminalWriteMixin`:
`_handle_terminal_input`, `_handle_terminal_paste`, `_handle_operator_write`,
`_deliver_operator_write`, `_write_outcome`, `_wait_joined_write`, `WRITE_FAULT_NAME` and
`write_handler_faulted`. `TerminalWsMixin` subclasses `TerminalWriteMixin`, so `server.py`
and the message dispatch keep their bases, and `terminal_ws.py` re-exports `WRITE_FAULT_NAME`.
The moved path is about 190 lines and none of it is a D1a handler. #21565 targets no symbol in
`terminal_ws.py` or the new module, so the two plans share no split target.

**Research context:** Observed on 0.5.0 `7d33430cc2`: `terminal_list` runs its
storage and process reads through `asyncio.to_thread` (`terminal_ws.py` ~343-354),
as do `WorkspaceOps`, `WriteCoordinator` and host reconcile after #22709 and
#22877. `_handle_terminal_attach` (`terminal_ws.py` ~195) calls `manager.get`
synchronously; `_apply_terminal_sizing` (`terminal_sizing.py` ~70, ~105) does get
plus `set_dims`; `_handle_terminal_set_scroll_offset` (~482) does one `manager.get`
per scroll event;
`_handle_terminal_create`/`_handle_terminal_kill` (`terminal_ws_create.py` ~58-198)
call storage inline. #22932 (`8f291d7af2`) deleted `tmux.py` and `test_tmux_mixin.py`;
every backend now attaches through `TerminalWsMixin`, so the former D1a.5 tmux row
pass-through has no target. Rejected: changing the pool module (D4 owns
diagnosis; capacity is out of scope); `WebSocketServer.run_db` (its sibling
handlers use `asyncio.to_thread`, and two off-loop mechanisms in one handler family
buy nothing). Planned verification:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test
GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/test_terminal_ws_attach_honesty.py
tests/servers/test_terminal_ws_golden.py tests/servers/test_terminal_ws_create.py
tests/servers/test_terminal_ws_resize.py tests/servers/test_terminal_ws_lease.py
tests/servers/test_terminal_ws_input.py`; `uv run ruff format`, `uv run ruff check src/`,
`uv run mypy src/`.

**Acceptance:**

- D1a.1 - Attach resolves its row on the executor. test:
  `tests/servers/test_terminal_ws_attach_honesty.py::test_attach_resolves_the_row_on_the_db_executor`.
- D1a.2 - Sizing runs get and `set_dims` as one executor hop. test:
  `tests/servers/test_terminal_ws_resize.py::test_sizing_runs_get_and_set_dims_as_one_executor_hop`.
- D1a.3 - A scroll-offset message makes no storage call. test:
  `tests/servers/test_terminal_ws_golden.py::test_scroll_offset_makes_no_storage_call`.
- D1a.4 - Create and kill run their storage on the executor. test:
  `tests/servers/test_terminal_ws_create.py::test_create_and_kill_run_storage_on_the_executor`.

### D1b WorkspaceOps runs its storage on the executor (completed prerequisite)
`kind: framing`

Shipped by #22709 (closed 2026-09-24): `c62987bcb4`, `32b8a43e37`, `762639ad44` and
`df7b196618` run every `WorkspaceOps` storage call through `asyncio.to_thread`
(Decision 2 status) and add `src/gobby/terminals/workspace_contract.py`, so the
former D1b.1 obligation and D1b.2 are delivered without an injected runner. D1b.3
was not delivered: `src/gobby/terminals/workspace_ops.py` is 1,004 raw lines on
0.5.0 `7d33430cc2`, over the ceiling, and no `workspace_ops_types.py` exists. That
decomposition belongs to #22883's `workspace_pane_access.py` extraction, which had
not landed at `7d33430cc2`; no remaining deliverable here targets
`workspace_ops.py`, so D1b.3 is dropped from this plan rather than repeated.

### D1c WriteCoordinator persists on the executor (completed prerequisite)
`kind: framing`

Shipped by #22877 (closed 2026-09-26): `599c3a3592` and `7e32d8ac81` run
`WriteCoordinator`'s `_require`, `_persist` and store reads through
`asyncio.to_thread` with the per-terminal lock held across the awaits, delivering
the former D1c.1. D1c.2 (an injected runner installed by `init_terminal_wiring`)
is superseded: `asyncio.to_thread` needs no runner.

### D1d Host reconcile row writes on the executor (completed prerequisite)
`kind: framing`

Shipped by #22877 (closed 2026-09-26): `src/gobby/terminals/host_reconcile.py` runs
its row lookups and mutations through `asyncio.to_thread`, delivering the former
D1d.1. D1d.2's `host_process.py` split did not ship
(`src/gobby/terminals/host_manager.py` is 909 raw lines on 0.5.0 `7d33430cc2`); it
moves to D3, which already targets `host_process.py` for the connect-time
deadline, as D3.5.

### D2 Concurrent dispatch per connection, ordered per lane (completed prerequisite)
`kind: framing`

Shipped by #22709 (closed 2026-09-24): `7866ac13ab`, `6654f28507`, `0db7486584`,
`2b23452270` and `df7b196618`. In `src/gobby/servers/websocket/server.py`, the
message types in `_OFF_LOOP_MESSAGE_TYPES` (attach, detach, input, paste, release,
resize, take_control, workspace_op) run as tasks chained per `_off_loop_chain_key`:
one lane for `workspace_op`, otherwise one per terminal id, falling back to the
attachment id. Everything else stays on the connection's read loop, today's
ordered default lane. On close, `_cancel_off_loop` cancels and awaits in-flight
chains, except durable workspace mutations, which complete by design (the #22709
bounce). That delivers the former D2.1 and D2.2 with three recorded divergences
from the planned design: chains key on terminal before attachment, so two
attachments to one terminal stay ordered; there is no semaphore cap, because only
eight message types leave the read loop and A2's bounded per-pane queue bounds
gclient's input (**Restraint rung 1**); and the dispatcher stays in `server.py`
(795 raw lines), so D2.3's `dispatch.py` is dropped. Task #22663 criterion 1(a) is
met by this shipped code; there is no in-flight watchdog.

### D3 Bounded HostClient round trips [category: code] (depends: C0)
`kind: deliverable`

Targets:
- `src/gobby/terminals/host_client.py::*` — scope-reason: adds `HostRequestAbandoned` and `HostClient._retire_generation`, gives `HostConnectionLost` a `request_written` flag, and changes `_begin_request`, `_close_generation`, `_roundtrip`, `_mutating_roundtrip`, `grant_input`, `revoke_input`, `connect` and `reconnect`
- `src/gobby/terminals/host_process.py`
- `src/gobby/terminals/host_manager.py::*` — scope-reason: the host process helpers move out to the new host-process module
- `tests/terminals/test_host_manager.py::*` — scope-reason: patches of the moved helpers follow them into the host-process module
- `src/gobby/terminals/native_runtime.py::*` — scope-reason: grant/revoke/reconnect orchestration moves to the new helper module and the runtime methods become delegates; the `resize`, `kill` and `terminate` reconnect retries and the `_write` and `write_batch` error mapping stop treating an abandoned or written request as unsent
- `src/gobby/terminals/native_input_grants.py`
- `src/gobby/config/terminal_host.py::TerminalHostConfig`
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerated derived carrier for the new config field
- `tests/terminals/test_host_client.py::*` — scope-reason: adds the deadline tests beside the round-trip tests
- `tests/terminals/test_native_runtime.py::*` — scope-reason: adds the aggregate EOF/reconnect/retry deadline, pending cleanup and lease-lock release test
- `tests/config/test_terminal_host_config.py::*` — scope-reason: adds the new field's default and bounds

Consumers unchanged:
- `src/gobby/terminals/host_control.py` — no-edit-reason: calls `_roundtrip` with the default deadline.
- `tests/terminals/test_wire_golden.py` — no-edit-reason: golden wire shapes carry no deadline field.
- `src/gobby/terminals/input_grants.py` — no-edit-reason: calls `grant_input`/`revoke_input` with unchanged signatures and already maps `HostUnavailableError` to `host_input_granted=False`.
- `src/gobby/terminals/host_event_reader.py` — no-edit-reason: opens its own `HostClient` for the event stream with the default deadline.
- `tests/terminals/host_fakes.py` — no-edit-reason: fakes `grant_input`/`revoke_input` with unchanged signatures.
- `tests/agents/test_spawn_executor.py` — no-edit-reason: calls `HostClient.connect` through fakes; the deadline keyword is optional.
- `tests/agents/test_native_spawn.py` — no-edit-reason: its reconnect fake keeps the unchanged optional-deadline interface.
- `src/gobby/app_context.py` — no-edit-reason: reads `TerminalHostConfig`; the new field has a default.
- `src/gobby/runner_init/terminal_wiring.py` — no-edit-reason: reads `TerminalHostConfig`; the new field has a default.
- `src/gobby/config/app.py` — no-edit-reason: nests `TerminalHostConfig`; the new field has a default.
- `src/gobby/runner.py` — no-edit-reason: reads `TerminalHostConfig`; the new field has a default.
- `tests/config/test_terminal_host.py` — no-edit-reason: constructs `TerminalHostConfig` with defaults.
- `tests/config/test_terminals.py` — no-edit-reason: constructs `TerminalHostConfig` with defaults.
- `tests/terminals/test_host_shutdown_preservation.py` — no-edit-reason: constructs `TerminalHostConfig` with defaults.
- `tests/test_runner_lifecycle_processes.py` — no-edit-reason: constructs `TerminalHostConfig` with defaults.

`HostClient._roundtrip` accepts one absolute event-loop deadline, defaulted from a
new `TerminalHostConfig.control_timeout_seconds` (5.0) stored by
`HostClient.connect`; its timeout scope begins before the lifecycle/writer lock and
`_begin_request`, so lock acquisition, write/drain and reply wait share one budget.
`spawn_commit` keeps `commit_deadline_ms`.

`_mutating_roundtrip` starts the same deadline scope before it waits for
`_operation_lock`, so time spent waiting on the serializer counts against the
caller's budget.

Abandonment retires the connection. The effect-bearing requests are
`grant_input`, `revoke_input`, `spawn_commit` (which calls `_roundtrip`
directly), and every `_mutating_roundtrip` verb (`spawn`, `kill`, `resize`,
`write`, `write_batch`). `_begin_request`
records, for every such request, its connection generation and whether its
bytes reached `writer.write`. This generalizes the per-task written flag
`spawn_commit` keeps today (`_commit_write_states`). A request is abandoned when its deadline expires or
its caller task is cancelled after that point. `_roundtrip` handles both the
same way: it retires the request's recorded generation, then raises. A deadline raises
`HostRequestAbandoned("timed out")`, a new `HostUnavailableError` subclass, so
`input_grants.py` keeps mapping it to `host_input_granted=False`. A cancellation
re-raises `CancelledError`. A request abandoned before it was written, for
example while waiting on `_operation_lock` or the lifecycle lock, never reached
the host and leaves the connection in place. D3b cancels an in-flight observer
before it starts recovery. The recovery grant or revoke therefore always travels
on a newer connection with a larger host connection id. Once it changes the
terminal's authority, C0's fence refuses the abandoned request if it lands
later.

Retirement fences the generation before any await. The new
`_retire_generation(message, expected_generation)` is synchronous and
generation-specific. If `_generation` no longer equals `expected_generation`,
that connection is already retired and a newer one may be installed, so it
changes nothing. Otherwise it advances `_generation`, sets `closed`, fails the
old generation's pending futures, cancels its reader task, calls
`writer.close()` and posts the event-stream sentinel. It then returns the
captured reader task and writer. `_close_generation` passes the current
generation, and an abandoned request passes the generation `_begin_request`
recorded for it. Cleanup (the cancelled reader and `writer.wait_closed()`)
runs as a detached task bounded by `control_timeout_seconds`. It is a genuinely
bounded wait: `asyncio.wait` over both awaitables with that timeout, which
returns at the bound even if the reader ignores cancellation. A
`wait_for(gather(...))` would not, because it waits for cooperative
cancellation. That task is kept in a strong-reference set until it
completes. It touches only the captured objects, so a reconnect that has
already installed a new reader, writer and generation is never affected by a
stale cleanup. `_close_generation` becomes retirement plus that detached
cleanup and no longer awaits the cleanup. As a result, neither `reconnect`
replacing a live connection nor an abandoned request spends any of its caller's
budget on the old socket. `close()` keeps its signature and calls
`_close_generation` as today.

The next runtime call finds `HostClient.closed` set and reconnects through
`_reconnect_epoch` and `HostClient.reconnect` within its own deadline. The new
host connection brings a fresh operation ledger, `next_seq` reset to 1 and a
larger host connection id. No sequence the abandoned request consumed is reused,
so the host never answers `operation_conflict` for it.

No replay. The abandoned request is never re-sent. Four wrappers keep one
reconnect-and-retry only for a transport failure inside the caller's live
budget (an `_ensure` failure, connection loss, or EOF), and they re-raise
`HostRequestAbandoned` without reconnecting or retrying:
- the grant/revoke wrapper in `native_input_grants.py`;
- `NativeTerminalRuntime.resize`;
- `NativeTerminalRuntime.kill`;
- `NativeTerminalRuntime.terminate`. It calls `self._client.kill` directly
  (~725-747) and does not delegate to `kill`. The same contract applies to
  #22904's stale-epoch branch there when it lands.

Write uncertainty lives in `NativeTerminalRuntime._write` (~638-694), which
`write_paste` and `write_text` call. Today every `HostUnavailableError` from
its first host write becomes `TerminalWriteError(stage="none")`. A connection
loss after the bytes were written has the same uncertainty as a timeout.
`_roundtrip` therefore raises such a loss as `HostConnectionLost` carrying
`request_written=True` from the same written flag, as `CommitTransportError`
does for commits. In `_write`, `HostRequestAbandoned` and a written
`HostConnectionLost` become `IndeterminateWrite` on each host write. The
text+submit path makes two host writes: text, then the `enter` key. On that
path, an abandonment or written loss of either write is `IndeterminateWrite`.
`stage="none"` is kept only for a first write that never reached
`writer.write`. `stage="partial"` is kept only for a refusal, or a pre-write
loss, of the `enter` write after the text was delivered.
`NativeTerminalRuntime.write_batch` (~512-606) has the same gap. Its batch
round trip catches `HostCommandError`, which includes `HostUnavailableError`,
as `stage="none"` for every target. `HostRequestAbandoned` and a written
`HostConnectionLost` from that round trip become `IndeterminateWrite` for every
target in the batch instead.
`spawn_commit` keeps its commit deadline
and its `CommitTransportError(request_written=...)` contract. Its cancellation
still carries `request_written`, and its abandonment also retires the
connection. Reads (`ping`, `list_terminals`, `list_inventory`, `snapshot`) keep
the connection on timeout.

`NativeTerminalRuntime.grant_input` and `revoke_input` each compute one absolute
1.5 s deadline before `_ensure`. That same deadline covers ensure, first round trip,
an EOF from that attempt, reconnect, the second grant/revoke attempt, writer
lock/drain and reply. `_reconnect_epoch`,
`HostClient.reconnect`, `grant_input`, `revoke_input` and `_roundtrip` receive only
the remaining time/absolute deadline; no phase or retry resets it. Expiry cancels
the active pending future, lets `_begin_request`'s existing done callback remove it
from `_pending`, and raises `HostUnavailableError("timed out")`. Thus the complete
operation stays below gclient's 2 s `CONTROL_REQUEST_DEADLINE`, including when the
first attempt ends in EOF and reconnect plus the second attempt consume the rest.
`sync_host_input_grant` already maps that error to `host_input_granted=False`, so
its caller exits the observer await and releases `TerminalLeaseRegistry.lock` no
later than the same original 1.5 s deadline.

`src/gobby/terminals/host_manager.py` is at 909 raw lines and D1d's split did not
ship with #22877: move the host process helpers (`_spawn_candidate`,
`_publish_spawned_client`, `_spawn_host_process`, `_wait_for_client`, `_connect`,
`_handshake`, `_process_alive`, `_reap_process`, `_close_client`, `_drain_host`,
`_interrupt`) into new `src/gobby/terminals/host_process.py` as module functions
taking the manager, so the connect-time deadline lands there and `host_manager.py`
ends under 850 lines.

`src/gobby/terminals/native_runtime.py` is at 854 raw lines: move the grant/revoke and
reconnect deadline orchestration into new
`src/gobby/terminals/native_input_grants.py`; the three targeted runtime methods
delegate with the runtime/client/terminal inputs and do not grow the near-ceiling
module.

**Granularity:** ten acceptance items and six production files, one behavior:
bounded host round trips and their abandonment recovery. The deadline, connection
retirement, the aggregate grant budget, the config field and the two module moves
land together, because a deadline without retirement reuses sequences and the
moves carry the deadline code.

D3b separately removes the lease lock from the external wait. The config field is
a `src/gobby/config/` change, so the runtime config contract carrier is regenerated
in the same commit. That gcore carrier dirties the coherent
`gcode`/`gdaemon`/`ghook` trio. D3 does not promote independently: after every leaf
passes, one batched global quiet-window gate rebuilds and promotes the trio through
`promote_workspace_binary_set`, promotes gclient separately through
`install_gclient_from_submodule`, restarts from the main checkout, and reads the
installed identity and hashes from `~/.gobby/bin/`. **Restraint rung 3:** use stdlib
absolute timeout scopes and the existing pending-future cleanup; add no retry or
deadline framework.

**Research context:** Observed: `HostClient._roundtrip`
(`src/gobby/terminals/host_client.py` ~317-353) does `payload = await future` with
no deadline; `grant_input` (~624-632) and `revoke_input` (~634-644) call it;
`HostClient.connect` (~170-177) is the constructor path used by the manager;
`TerminalHostConfig` (`src/gobby/config/terminal_host.py`) has
`commit_deadline_ms`, `health_interval_seconds` and no control timeout today (the
planning draft's `control_timeout_seconds = 5.0` is a new field, not an existing
one); `take_control` (`src/gobby/terminals/leases.py` ~421-455) holds `self.lock`
across `_notify_holder`; `NativeTerminalRuntime.grant_input` (~697-706) retries once
with a reconnect and `revoke_input` (~713-722) does the same;
`_reconnect_epoch` (~846-851) has no timeout argument. Planned verification:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test
GOBBY_TEST_PROTECT=1 uv run pytest tests/terminals/test_host_client.py
tests/config/test_terminal_host_config.py tests/terminals/test_native_runtime.py
tests/servers/test_terminal_ws_lease.py`; `uv run mypy src/`.

**Acceptance:**

- D3.1 - A round trip past its deadline raises `HostUnavailableError("timed out")`
  and drops the pending future. test:
  `tests/terminals/test_host_client.py::test_roundtrip_times_out_with_host_unavailable`.
- D3.2 - Grant and revoke use one absolute 1.5 s budget across ensure, a first
  attempt ending in EOF, reconnect and the second attempt. With the retry stalled,
  wall time stays below 2 s, every HostClient pending entry is removed, and a
  competing acquisition of the same `TerminalLeaseRegistry.lock(terminal_id)`
  succeeds by that original deadline. test:
  `tests/terminals/test_native_runtime.py::test_eof_reconnect_and_retry_share_one_deadline_and_release_the_lease_lock`.
- D3.3 - `control_timeout_seconds` defaults to 5.0 and is exported to the runtime
  config contract. test:
  `tests/config/test_terminal_host_config.py::test_control_timeout_seconds_default`.
- D3.4 - Validation treats the regenerated gcore carrier as an input change to the
  coherent trio and records the batched installed identity/hashes from
  `~/.gobby/bin/` plus separate gclient promotion. file:
  `crates/gcore/assets/config/runtime_config_contract.json`.
- D3.5 - The host process helpers live in their own module and `host_manager.py` is
  under 850 lines. file: `src/gobby/terminals/host_process.py`.
- D3.6 - A mutating request that times out, including time spent waiting for
  `_operation_lock`, retires the connection. The next mutation runs on a new
  connection from `operation_seq` 1 with no `operation_conflict`, and the
  timed-out request is not re-sent. test:
  `tests/terminals/test_host_client.py::test_mutating_timeout_retires_the_connection_and_never_reuses_its_sequence`.
- D3.7 - A grant whose fake-host reply is withheld past its deadline retires the
  connection, and the following revoke is issued on a new connection. test:
  `tests/terminals/test_host_client.py::test_grant_timeout_retires_the_connection_before_the_next_authority_change`.
- D3.8 - A grant is written and the fake host parks it at a barrier before
  applying it. The fake host enforces C0's per-slot connection fence, whose
  real-host rule C0.7 proves. The caller is cancelled before its deadline. That
  cancellation retires the connection, and the recovery revoke is issued on a
  newer connection. Releasing the parked grant afterwards leaves the revoke in
  force. A request cancelled before it is written leaves the connection in
  place. test:
  `tests/terminals/test_host_client.py::test_cancelling_a_written_grant_retires_the_connection_so_newer_authority_survives`.
- D3.9 - In this test the old writer's `wait_closed` never returns and its
  reader ignores cancellation. Retirement still returns without awaiting either,
  and the detached cleanup ends at its bound. The grant's aggregate budget
  holds, and a reconnect installed while the stale cleanup runs keeps its
  reader, writer and generation after that cleanup ends. It also covers an old
  request's timeout or cancellation handler that runs after another caller
  has reconnected: its retirement names the old generation and leaves the new
  connection open.
  test:
  `tests/terminals/test_host_client.py::test_retirement_fences_before_cleanup_and_stale_cleanup_leaves_the_new_connection`.
- D3.10 - A timed-out grant, revoke, resize, kill or `terminate` is not re-sent
  after the runtime reconnects. In `_write`, a timed-out host write, or one
  whose connection is lost after its bytes were written, returns
  `IndeterminateWrite`. That includes the text+submit path when the loss hits
  either the text or the `enter` write. A loss before the first write stays
  `TerminalWriteError(stage="none")`. An EOF inside the budget still gets its
  one retry. A timed-out `write_batch` reports `IndeterminateWrite` for every
  target. test:
  `tests/terminals/test_native_runtime.py::test_abandoned_requests_are_never_replayed_and_timed_out_writes_are_indeterminate`.

### D3b Host grant reconciliation never holds the terminal lease lock [category: code] (depends: D1a, D3, C2)
`kind: deliverable`

Targets:
- `src/gobby/terminals/leases.py::*` — scope-reason: `HolderChange`, holder-sync cells/tasks, `_notify_holder`, `take_control`, `release_control` and `finalize` move every host observer await outside the lease lock
- `tests/terminals/test_lease_authority.py::*` — scope-reason: adds transition, race and cancellation coverage for the two lock domains
- `tests/servers/test_terminal_ws_lease.py::*` — scope-reason: adds the unanswered real HostClient grant diagnostic through the shipped per-terminal chain (#22709)
- `tests/e2e/test_terminal_client_stack.py::*` — scope-reason: adds the real WebSocketServer, HostClient and gterm daemon-restart continuity test

Consumers unchanged:
- `src/gobby/terminals/input_grants.py` — no-edit-reason: `sync_host_input_grant` keeps the same runtime/terminal/holder signature and error mapping.

Every holder mutation stays atomic under `lock(terminal_id)`, captures a
`HolderChange` with the resulting lease generation and host-grant disposition, then
releases that lock before starting `_notify_holder`. Host notifications serialize
under a separate per-terminal holder-sync lock. After acquiring that lock,
`_notify_holder` briefly reacquires the lease lock to compare the captured
generation, holder and disposition with the current lease, skips a stale change,
releases the lease lock, and only then awaits the gterm observer.

The disposition is explicit. Repeated take, first take/takeover, explicit release,
explicit detach, terminal removal and cleanup of a non-direct holder synchronize
the resulting holder and therefore revoke or replace an old grant as appropriate.
`finalize(reason="ws_close")` for the current direct-native holder still finalizes
the attachment, clears daemon-side holder/write/sizing state and bumps the
generation, but records `PreserveCurrentDirectGrant` and makes no revoke call. That
is the only preservation case. A later C2 take replaces the surviving grant with
the new attachment id. The lease's latest generation retains this disposition so a
cancellation recovery cannot turn the preserve transition into a revoke.

If an older sync already owns the holder-sync lock when a newer lease mutation
commits, the newer sync queues behind it and is therefore the last host effect; if
the newer mutation wins the sync lock first, the older generation is skipped. The
four existing observer awaits at observed lines 437, 446, 470 and 494 are removed
from their lease-lock scopes.

The lane directly awaits `_sync_committed_holder(change)` outside the lease lock.
If lane cancellation interrupts that await, the wrapper synchronously creates a
registry-owned `reconcile_latest_holder(terminal_id)` task before re-raising
`CancelledError`; the task is kept in a strong-reference set until its done callback
removes it. `reconcile_latest_holder` acquires the holder-sync lock first, then
snapshots the current generation, holder and disposition under the lease lock,
releases only the lease lock, and applies or preserves that latest host state while
still owning the holder-sync lock. If a newer normal sync wins the holder-sync lock
first, recovery snapshots the newer state after it; if recovery wins first, a later
mutation queues and writes last. No pre-lock snapshot can be applied after a newer
host effect. No lease mutation, write admission or sizing decision waits on gterm.

Reply snapshot. Today `take_control` (`leases.py` ~432-466) reads
`lease.generation` and calls `_reelect_sizing` after awaiting `_notify_holder`,
which is safe only because the lease lock is still held. Each mutation now
captures its whole reply under the lease lock at commit: the resulting
generation, `displaced_attachment_id` and `_reelect_sizing(terminal_id, lease)`.
No reply field or sizing change is therefore read from a lease that another take,
release or finalize may already have changed. After the unlocked observer
returns, the caller reacquires the lease lock briefly. If the lease generation
and holder still equal the captured ones, the reply carries the observer's
`host_input_granted`. If a newer mutation superseded them, the reply carries the
captured generation and `host_input_granted=False`, so the superseded caller
never claims typing authority; the newer mutation's own reply and host sync
govern. The repeated-take branch captures the same way.
**Restraint rung 2:** reuse the registry's existing per-terminal lock-cell pattern
and holder observer; introduce one separate sync lock plus cancellation-recovery
task set because the external effect cannot safely share the lease-state lock.

The real unanswered-grant test uses a test-only barrier after `HostClient` registers
the request and before the fake host replies. While that request is visibly pending,
it asserts `lock_held(terminal_id) == false` and completes another connection lane
and same-terminal lease-state acquisition. Releasing the barrier or allowing D3's
single deadline to expire clears the pending entry. It adds no production phase
label, sampler or watchdog and makes no claim about the historical incidents; their
root cause remains unproven.

**Research context:** gcode verified `TerminalLeaseRegistry.take_control`
(`src/gobby/terminals/leases.py` 421-455), `release_control` (457-478) and
`finalize` (480-511). All four `_notify_holder` calls currently run inside
`lock(terminal_id)`, including the cleanup path the incident report did not name.
`HolderChange` (153-158) currently has no generation. The observer installed by
`src/gobby/runner_init/terminal_wiring.py::init_terminal_wiring` calls
`sync_host_input_grant`, which awaits `NativeTerminalRuntime.grant_input` or revoke;
the grant reaches `HostClient._roundtrip`. The repair-lesson whole-plan sweep
therefore includes repeated take, takeover, release, finalize and cancellation,
not only the two reported line locations. Planned verification:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test
GOBBY_TEST_PROTECT=1 uv run pytest tests/terminals/test_lease_authority.py
tests/servers/test_terminal_ws_lease.py tests/terminals/test_host_client.py` plus the
isolated native backend case in `tests/e2e/test_terminal_client_stack.py`;
`uv run mypy src/`.

**Acceptance:**

- D3b.1 - The transition matrix sees `lock_held(terminal_id) == false`; direct
  `ws_close` preserves the current host grant, while repeated/first take, takeover,
  explicit release, detach, terminal removal and non-direct cleanup replace or
  revoke it. test:
  `tests/terminals/test_lease_authority.py::test_holder_transition_matrix_applies_preserve_and_revoke_policies_outside_the_lease_lock`.
- D3b.2 - A stalled older grant cannot overwrite a newer takeover or final revoke;
after the stall releases, the last host notification matches the latest generation.
test:
`tests/terminals/test_lease_authority.py::test_stalled_holder_sync_converges_to_the_latest_generation`.
- D3b.3 - With a real WebSocketServer, HostClient and gterm, restarting the isolated
  daemon finalizes its websocket state without revoking the direct grant; `Input`
  typed after cleanup starts and while the reconnect take is paused still reaches
  the PTY, then C2 replaces the grant and an explicit detach refuses later input.
  test:
  `tests/e2e/test_terminal_client_stack.py::test_direct_native_input_survives_daemon_restart_until_lease_retake`.
- D3b.4 - A test barrier holds an actual `HostClient` grant after request
  registration; while it is pending, `lock_held(terminal_id) == false`, another
  connection lane and same-terminal lease-state acquisition complete, and D3's
  deadline clears the pending request without any production watchdog. test:
  `tests/servers/test_terminal_ws_lease.py::test_unanswered_host_grant_is_bounded_and_lock_free`.
- D3b.5 - In the barrier race where the newer normal sync wins the holder-sync lock
  before recovery, recovery snapshots only after acquiring that lock and the final
  host state still matches the newest generation. test:
  `tests/terminals/test_lease_authority.py::test_reconcile_latest_holder_snapshots_under_the_sync_lock_after_newer_sync_wins`.
- D3b.6 - An older take's observer is held while a newer takeover or finalize
  commits. The older reply carries its own captured generation and sizing with
  `host_input_granted=False`, sizing is never recomputed outside the lease lock,
  and the final host state matches the newer generation. test:
  `tests/terminals/test_lease_authority.py::test_a_superseded_take_reply_keeps_its_captured_generation_and_claims_no_host_grant`.
### D4 Pool exhaustion diagnosis [category: code]
`kind: deliverable`

Targets:
- `src/gobby/storage/hub/postgres_pool.py::_acquire_with_backoff`
- `src/gobby/telemetry/logging.py::setup_file_logging`
- `tests/storage/hub/test_postgres_pool_backoff.py::*` — scope-reason: adds the census test beside the backoff tests
- `tests/telemetry/test_logging.py::*` — scope-reason: adds the psycopg pool logger routing test

Consumers unchanged:
- `src/gobby/runner_init/storage.py` — no-edit-reason: calls `setup_file_logging` with an unchanged signature.
- `src/gobby/telemetry/__init__.py` — no-edit-reason: re-exports `setup_file_logging`, unchanged.
- `tests/sessions/test_parser_error_log.py` — no-edit-reason: calls `setup_file_logging` with an unchanged signature.
- `tests/telemetry/test_health_metrics.py` — no-edit-reason: calls `setup_file_logging` with an unchanged signature.

Two small additions so the next incident is attributable. First,
`setup_file_logging` lets the `psycopg.pool` logger's WARNING lines (the "error
connecting" warning psycopg_pool emits when the pool cannot grow) reach
`daemon.log`, beside the existing `websockets` logger levels. Those records
interpolate the raw exception (`psycopg_pool/pool.py` ~673:
`logger.warning("error connecting in %r: %s", self.name, ex)`, and other warning
branches do the same), which can carry the DSN. `setup_file_logging` has no
sanitizer, and `_PrimarySurfaceFilter` only routes namespaces. So the route
installs one filter on the `psycopg.pool` logger that rewrites each record before
any handler formats it. The rewritten record has a fixed message, the pool name,
the exception class and its `sqlstate` when present. It keeps no original
`args`, no exception text and no `exc_info`. This is a guard on the newly enabled
sink, not a general redaction framework. Second, `_acquire_with_backoff`'s
final failure branch may take one census:

- Rate limit. A module-level `threading.Lock` guards a monotonic
  `_last_census_at` and a `_census_in_flight` flag. A failing caller takes the
  lock without blocking; if the lock is busy, a census is in flight, or less than
  60 seconds have passed, it skips. Otherwise it stamps `_last_census_at`, sets
  `_census_in_flight`, releases the lock, and starts the census on one daemon
  `threading.Thread`, which clears the flag in `finally`. At most one census
  runs at a time, and at most one starts per minute.
- Bounds. The failing caller never waits on the census: it starts the thread and
  re-raises at once, so a stalled census adds no latency to any request. The
  census opens one direct connection from the pool's conninfo with
  `connect_timeout=2`, `options="-c statement_timeout=2000"`, and libpq TCP
  keepalives (`keepalives_idle=5`, `keepalives_interval=2`,
  `keepalives_count=2`). It runs one
  `SELECT application_name, state, count(*) FROM pg_stat_activity GROUP BY 1, 2`,
  and closes the connection in `finally`. Each bound covers one failure mode:
  - `connect_timeout` bounds the client-side connect at 2 seconds.
  - `statement_timeout` is enforced by the server. It bounds query execution at
    2 seconds and does nothing for a reply lost on the network.
  - The keepalives end a lost reply from a dead peer or a broken path in about
    9 seconds.
  A live server that holds the connection open without replying is not bounded
  in time. That stall occupies only the one census thread, and later failures
  skip the census while `_census_in_flight` is set, so nothing accumulates. The
  census stays disabled until that thread ends; it does not recover by itself
  while the connection is held.
- Result. Success logs the rows beside `pool_stats`. Failure logs one WARN with
  the exception class and `sqlstate` only, never the exception text or the DSN
  (the #22962 redaction precedent). Capacity exhaustion is claimed only for
  SQLSTATE `53300` (`too_many_connections`). An auth failure (`28P01`,
  `28000`), `57P03`, a statement timeout (`57014`) or a failure with no SQLSTATE
  (network, server down) is logged as that reason with no capacity claim.
- Original error. The census runs inside its own `try`/`except Exception`, and
  the function still re-raises the original acquisition `PoolTimeout` unchanged.
  A census failure never replaces or chains onto it.

**Research context:** Observed: `_acquire_with_backoff`
(`src/gobby/storage/hub/postgres_pool.py` ~159-188) retries over
`POOL_TIMEOUT_RETRY_BACKOFF_SECONDS` with `time.sleep` and logs
`PostgreSQL hub pool acquisition failed after %d retries: pool_stats=%s` on final
failure; logger levels are set in `setup_file_logging`
(`src/gobby/telemetry/logging.py` ~464-498, the `websockets` loop at ~493). The
planning draft placed the logger wiring in `src/gobby/config/logging.py`; that
module holds only settings, so no config carrier is involved. Status 2026-09-27:
#22864 (`af072a49d0`) added per-query timing to `postgres_pool.py` and touches
neither `_acquire_with_backoff`'s failure branch (~160) nor the `psycopg.pool`
logger, so D4 is still open. Planned verification:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test
GOBBY_TEST_PROTECT=1 uv run pytest tests/storage/hub/test_postgres_pool_backoff.py
tests/telemetry/test_logging.py`.

**Acceptance:**

- D4.1 - The final acquire failure logs a `pg_stat_activity` census, and concurrent
  failures within a minute run at most one census. test:
  `tests/storage/hub/test_postgres_pool_backoff.py::test_final_acquire_failure_logs_a_rate_limited_activity_census`.
- D4.3 - A census refused with SQLSTATE `53300` logs capacity exhaustion; an auth,
  network or server-down refusal logs its class and SQLSTATE with no capacity
  claim, and no log line carries the exception text or DSN. test:
  `tests/storage/hub/test_postgres_pool_backoff.py::test_census_failure_reason_is_typed_and_claims_capacity_only_for_53300`.
- D4.4 - The caller receives the original `PoolTimeout` object at once, whether the
  census succeeds, fails or stalls. While a stalled census is in flight, a later
  failure starts no second census, and the census connection carries
  `connect_timeout`, `statement_timeout` and the keepalive parameters. test:
  `tests/storage/hub/test_postgres_pool_backoff.py::test_census_is_bounded_and_preserves_the_original_acquisition_error`.
- D4.2 - `psycopg.pool` warnings reach the daemon log as sanitized records. A
  warning whose exception text carries a sentinel credential and DSN writes the
  pool name, exception class and SQLSTATE to `daemon.log`, and the sentinel
  never appears. test:
  `tests/telemetry/test_logging.py::test_psycopg_pool_warnings_reach_the_daemon_log`.

### E1 Drop the undrained host event subscription [category: code] (depends: D3)
`kind: deliverable`

Targets:
- `src/gobby/terminals/native_runtime.py::*` — scope-reason: `reserve_observer` drops the subscription and `_subscribed` goes away; the spawn-failure helpers move out
- `src/gobby/terminals/native_spawn_failure.py`
- `tests/terminals/test_native_runtime.py::*` — scope-reason: adds the no-subscribe assertion
- `tests/terminals/acceptance/conftest.py::*` — scope-reason: the fake runtime drops its `_subscribed` mirror
- `tests/terminals/test_runtime_contract.py::*` — scope-reason: the contract fake drops its `_subscribed` mirror

`NativeTerminalRuntime.reserve_observer` subscribes the main control client to host
events once, but the consumer (`connect_event_stream` in
`src/gobby/terminals/host_event_reader.py`) reads a separate connection; nothing
calls `_next_event_payload` on the main client, so its unbounded `_event_queue`
grows by one entry per accepted direct keystroke for the life of the host
generation. Remove that subscribe and `_subscribed`.

`src/gobby/terminals/native_runtime.py` is at 854 raw lines: the spawn-failure helpers
(`HostEpochMismatch`, `_bounded_spawn_code`, `_host_error_detail`,
`classify_native_spawn_failure`, `_mark_host_error_stage`) move into new
`src/gobby/terminals/native_spawn_failure.py` and are re-exported from
`native_runtime.py` so `src/gobby/agents/spawn_executor.py`,
`src/gobby/terminals/web_spawn.py` and the acceptance tests keep their imports.

**Research context:** Observed: `reserve_observer`
(`src/gobby/terminals/native_runtime.py` ~338-355) calls `self._client.subscribe_events()`
when `not self._subscribed`; `HostClient.subscribe_events` (~646-650) and
`_next_event_payload` (~274-278) with `_event_queue` (~157, ~233) in
`src/gobby/terminals/host_client.py`; `connect_event_stream`
(`src/gobby/terminals/host_event_reader.py` ~31-38) opens its own connection.
`_subscribed` is mirrored by fakes in `tests/terminals/acceptance/conftest.py` and
`tests/terminals/test_runtime_contract.py`. Planned verification:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test
GOBBY_TEST_PROTECT=1 uv run pytest tests/terminals/test_native_runtime.py
tests/terminals/test_runtime_contract.py tests/terminals/acceptance
tests/agents/test_native_spawn.py`.

**Acceptance:**

- E1.1 - `reserve_observer` sends no `subscribe_events`. test:
  `tests/terminals/test_native_runtime.py::test_reserve_observer_sends_no_subscribe_events`.
- E1.2 - The spawn-failure helpers live in their own module and are re-exported.
  file: `src/gobby/terminals/native_spawn_failure.py`.

## Q1: Verification
`kind: verification`

gclient unit: `cargo fmt -p gobby-client`, `cargo clippy -p gobby-client
--all-targets`, `cargo nextest run -p gobby-client` (the tests named in P1-P3).
Render characterisations in `crates/gclient/tests/parity/chrome.rs` and
`crates/gclient/tests/fixtures/screens/*.txt` change only if status-line text
changes (B1, C1); regenerate with `GOBBY_UPDATE_SCREENS=1` in the same commit.

Watchdog-free host-grant verification: run
`tests/servers/test_terminal_ws_lease.py::test_unanswered_host_grant_is_bounded_and_lock_free`
with the isolated hub. Its test-only barrier stops the fake host after the real
`HostClient` request is registered. While `_pending` is non-empty, the lease lock
must be free and another connection lane plus same-terminal lease-state acquisition
must complete; the one D3 deadline then clears `_pending`. This proves the repaired
invariant without a production watchdog or phase label. The 07:44 and 14:47
historical root causes remain unproven. **Restraint rung 2:** reuse the real
handler/lease/HostClient seam and a test barrier; add no runtime diagnostic task.

Clean-cutover window: send one `global` announcement and wait for no live spawned
worker or close validator. D3's gcore runtime-config carrier dirties the coherent
set, so rebuild all three of `gcode`/`gdaemon`/`ghook` and promote them together
through `promote_workspace_binary_set`. Build and promote gclient separately through
`install_gclient_from_submodule` (never pass gclient to the coherent-set function),
restart the Python daemon from the main checkout, then restart the validation
gclient so it execs the new inode. P3 changes gterm, so build it with
`--features vt-engine --bin gterm` and promote it through
`install_gterm_from_submodule` inside this same announced window. Read installed
identity/hash evidence from `~/.gobby/bin/`, not `target/release/`.

Daemonless live path: stop or omit the isolated daemon before launch but keep a
valid owner-only `local_cli_token`, while a real promoted gterm owns two native
PTYs. Launch gclient and verify one independently attached direct pane opens per
inventory row, both render advancing output, and bytes typed into each pane reach
the correct PTY. Confirm the UI names unavailable lease/takeover, layout/workspace,
roster, attention and relay features and offers no tmux, web or proxy panes. Launch
a second gclient and verify its input is refused while the first keeps typing.
Start the isolated daemon, then verify the same panes are adopted by terminal id
without duplication, their frame streams rebind to daemon attachments, typing
continues before the take replies, and daemon grants replace the fallbacks. Test the
missing-default-token path separately with no native-host authentication attempt:
gclient opens an empty degraded window and states that no local native host is
available. **Restraint rung 2:** preserve the shared token contract and vary only
daemon availability; add no second daemonless credential.

gclient live, the freeze: with the promoted binaries and a direct pane held, run
`kill -STOP <daemon pid>` at t=0, immediately focus another project to issue its
scoped `terminal_list` roster request, and keep the daemon stopped for at least 6 s
after that request. The held request must be observable as `Daemon slow` by t=1 s;
if it is not, the request did not issue and the check restarts. It reaches
`REQUEST_DEADLINE` at t=5 s, at which point the status becomes
`× Daemon unreachable · retrying in N s`, a reconnect attempt is scheduled, and the direct pane stays
Held with an unconfirmed lease. Throughout t=0-6 s, keystrokes appear immediately,
frames and ticks advance, and proxied input does not claim success. Run
`kill -CONT <daemon pid>` only after t=6 s and verify reconnect clears the banner
and reconfirms the direct lease without take-back. Then run
the announced `uv run gobby restart --wait` while typing in a direct pane: typing
never stops; after reconnect the pane shows Held again with the lease reconfirmed; a
proxied pane drops to observe and comes back on take. Trigger one mock frame error
and one forced recovery give-up. Retain structured WARN evidence for frame-error
start, direct-handshake timeout, recovery give-up and success, unresolved-open
failure, REST component failure, both Lagged entry paths, and reconnect start/finish
including timeout and success from `gclient.log`.

Daemon: `uv run ruff format src/ && uv run ruff check src/ && uv run mypy src/`,
the pytest targets in P4 with the isolated hub
(`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test
GOBBY_TEST_PROTECT=1`), and `uv run gobby test-types audit tests/ --baseline
.gobby/test-types-baseline.json --fail-on-new`. After the announced restart, the
next day's `daemon.log` should show slow-handler warnings attributed to lanes, and
`gclient.log` no `daemon request timed out` lines outside genuine outages. Baseline
to compare against: 324 slow-handler warnings and 48 gclient timeouts on
2026-09-20.

Docs: relative link and anchor check over the edited guide, README index and
protocol contract (no `gobby docs` CLI exists). Never run the full pytest suite.

## V1 Plan Changelog
`kind: verification`

- Program Director narrative review of `f788377a52` (2026-09-27), BOUNCE, five
  repairs:
  1. C0c no longer asserts C2's retake. C0c.4 keeps adoption without duplicates
     and leaves holders `lease_unconfirmed`, and new C2.3 owns the retake after a
     degraded launch. A sweep found no other acceptance item naming a later leaf.
  2. A2 orders accepted input through the pane writer: `Release` and `Close`
     barriers, a control task that awaits release barriers before the take, a
     direct-pane barrier on the last frame write receipt, and `WriteAbandoned`
     reporting for failures and stale generations. `frame_source.rs` is a new
     target, and A2.9 and A2.10 are new.
  3. A4 is split into A4a (event refetches and roster jobs), A4b (lifecycle and
     adoption chains) and A4c (orphan fan-out), chained to order the shared job
     files. All nine items are kept: A4.3, A4.5 and A4.8 become A4a.1-3; A4.1,
     A4.2, A4.4, A4.6 and A4.9 become A4b.1-5; A4.7 becomes A4c.1. A5 depends on
     A4c.
  4. D4 makes a capacity claim only for SQLSTATE `53300`, bounds the census
     connect and SELECT, rate-limits it under a non-blocking lock, logs failures
     by class and SQLSTATE only, and preserves the original `PoolTimeout`. D4.3
     and D4.4 are new.
  5. C0c describes the current staged startup (X3), with a native-inventory job
     beside `startup_job`, and adds `live_loop.rs` as a target. The cutover gate
     uses `gobby cutover` for the trio and applies Josh's +0.0.1 crate
     patch-and-lock rule (memory `8802df5d`).
- Program Director re-review of `dd85262f3b` (2026-09-27), one bounded correction:
  1. A2 keeps delivery uncertainty. It separates known-unsent queued writes
     (`WriteAbandoned`, exact counts) from in-flight writes with no reply
     (`WriteUnconfirmed`, no byte count) and correlated typed refusals. The
     direct barrier is local flush ordering only, gterm's uncorrelated
     `InputRefused` is reported as unconfirmed, the frame socket gets no new ack
     protocol, and nothing is replayed. A2.11 (direct flush against delayed
     consumption and revoke) and A2.12 (indeterminate daemon write) are new.
  2. D4 moves the census off the caller path onto one in-flight-guarded daemon
     thread. It names each bound separately: the client connect deadline, the
     server statement timeout, and keepalives for a lost reply. A live but
     silent server stays unbounded in time and is contained to one thread. D4.4
     is restated to match.
- Plan Adversary round on `207548db2e` (gobby#14579, 2026-09-27), GDR-14 to
  GDR-26, all accepted:
  1. GDR-14: C0c moves focus locally without a daemon release while the daemon
     is absent, and refuses explicit control changes truthfully (C0c.7).
  2. GDR-15: C0c's native job is loop-owned, and one disposal rule closes every
     source of a late or stale native result (C0c.8).
  3. GDR-16: C0 adds a per-slot control-connection fence (C0.7), and D3 counts
     serializer wait and retires the connection on every effect-bearing timeout
     (D3.6, D3.7, now depends on C0).
  4. GDR-17: A4b records committed stale `Spawned` and adoption results and
     compensates them against the next installed model; unanswered requests are
     not replayed (A4b.6, A4b.7).
  5. GDR-18: new Targets `write.rs::frame_input`, `control.rs::dispatch`,
     `reconnect.rs::handle_live_event` (A3b, A4a) and `live_reader.rs` (B1). C2
     names `finish_direct_attach` and `install_direct_source`. A2 keeps the
     `FrameSource::send_input` trait. The wire-enum sweep is recorded in C0.
  6. GDR-19: X1, X2, X3, D1b, D1c, D1d and D2 become completed-prerequisite
     framing with their commit evidence, and the Scheduling paragraph records
     the closed prerequisites and the #22904 `native_runtime.py` serialization.
  7. GDR-20: C2 schedules takes per pane, confirms only on a host grant, and
     owns a bounded retry (C2.4, C2.5).
  8. GDR-21: C3 separates a surviving id-grant from a lost fallback and claims
     no authority before the host answers (C3.3, C3.4).
  9. GDR-22: A2 gives each barrier one owner with cloned receivers and folds
     close-during-release into the pending barrier (A2.13 to A2.15).
  10. GDR-23: D3b captures the whole reply under the lease lock and reports a
      superseded observer as `host_input_granted=False` (D3b.6).
  11. GDR-24: A1 settles only a matching `JobTag.id`, and write-uncertainty
      reports bypass the stale drop (A1.7).
  12. GDR-25: D4 sanitizes `psycopg.pool` records before `daemon.log` (D4.2).
  13. GDR-26: B1's status research matches the current `× Daemon unreachable`
      branch.
- Plan Adversary recheck on `1029c2f91d` (gobby#14579, 2026-09-27) accepts
  GDR-14, GDR-15 and GDR-18 to GDR-26. It found residuals in GDR-16 and GDR-17,
  both accepted:
  1. GDR-16: D3 retires on cancellation as well as timeout once a request may
     have been written. `_retire_generation` fences synchronously, and cleanup
     is detached, bounded and isolated from a newer connection.
     `HostRequestAbandoned` is never replayed by the grant/revoke, resize or
     kill wrappers. A timed-out write, or one whose connection is lost after
     it was written, is indeterminate (D3.8 to D3.10).
  2. GDR-17: A4b disposes an orphan at once when the current model is already
     installed, and otherwise at install. A failed disposal keeps its entry,
     raises a toast and is re-evaluated on the next install (A4b.8).
  3. Follow-up on `2643406ce5`, also accepted. Retirement is
     generation-specific (`expected_generation` compare), and cleanup uses a
     genuinely bounded `asyncio.wait`. The no-replay sweep names `terminate`,
     `_write`'s two-write text+submit path and `write_batch` (D3.9, D3.10).
- 2026-09-27: Plan Adversary consensus (gobby#14579) on `6002c063ce`. GDR-14 to
  GDR-26 are all resolved. No blocking findings and no proportionality
  objection remain. The Adversary derives and applies M1 from the committed
  bytes and validates expansion from them. The Program Director reviews the
  plan, presents it to Josh and gates expansion on Josh's approval. No
  expansion happens before that approval.

- 2026-09-21: made the split-right freeze hypothesis testable; added ordered lanes,
  lock-free host-grant reconciliation, frame-recovery logging, and the shared
  native-binary clean-cutover gate.
- 2026-09-21: resolved GDR-R1-F01 through F11 with corrected ordering, restart-grant
  continuity, aggregate deadlines, lock ordering, path-complete tests and bounded input.
- 2026-09-21: round 2 adds daemonless native launch/render/input and local fallback
  authority, removes both proposed watchdogs, tightens the EOF/retry deadline and
  lock-release proof, references #22677, closes WARN gaps, and corrects diagnosis.
- 2026-09-21: round 3 moves slow-attach liveness into A3, types #22677 as its
  prerequisite, gives each daemonless native pane its own frame connection, and
  retains the local token for daemonless host authentication.
- 2026-09-22: applied round 3's three blocking findings by removing the idle
  keepalive and recording C0/C0c's unchanged consumers; by the orchestrator's
  decision, there is no round 4.
- 2026-09-27: reconciliation against 0.5.0 `7d33430cc2` (Plan Writer gobby#14578,
  task #22663). Shipped work becomes typed deferrals: X2 (#22747: A3.5 and the
  Lagged relist and sidebar refetch), X3 (#22755: A5.1, A5.3), D1b and D2
  (#22709), D1c and D1d (#22877); X1's #22677 is closed. R1 is rewritten for the
  current near-ceiling files (`live_loop.rs` signals, `workspace_actions.rs` local
  adoption, `actions.rs` lifecycle, `app/mod.rs` disconnect) and drops the
  superseded `live.rs` and `daemon/live.rs` moves. A3, A3b, A4, A5, B3, C0c, C1
  and C3 are retargeted off the never-created `live_events.rs`, `live_roster.rs`,
  `live_reconcile.rs` and `handshake.rs`; A4 absorbs #22883's gclient-owned shell
  adoption chain; A1 and A2 move the focus-hint and small-op groups out of
  `workspace_actions.rs`; D3 absorbs D1d's `host_process.py` split. Decisions 9
  (attach stays on the `RecoveryFuture` seam) and 10 (shared broadcast unchanged)
  are proposed pending Josh's approval; #22745's chrome-only input case joins A5;
  D1a moves to `asyncio.to_thread`. Authored on worktree base `1eb8fbb67c`, behind
  0.5.0; code evidence measured on the main checkout at `7d33430cc2`.

Round 1 adversarial review (plan-adversary-taskless run 47e9dd2e, 2026-09-21 10:1x): needs_review, 11 blocking findings, 2 candidates dismissed. The coordinator (the assistant gobby#14069, under delegated planning-lane authority from the orchestrator gobby#14018) accepted all 11.

GDR-R1-F01 (C1 creation order), F02 (the direct grant is revoked on ws_close across restart), F03 (aggregate host-grant deadline), F04 (reconcile_latest_holder lock order), F05 (A3 granularity decision), F06 (P1 path acceptance parity), F07 (Decision Record direct-input authority boundary), F08 (freeze-hypothesis falsification order), F09 (SIGSTOP health timing), F10 (D3 binary cutover self-contained), F11 (bounded proxied-input queue).

F02 resolution direction: keep direct typing uninterrupted across a daemon restart (Josh 2026-09-21: 'seriously reduce blocking dependency on the daemon in gclient'). ws_close finalize must not revoke the current direct host grant; explicit release, detach, takeover and terminal removal still revoke. The revision goes to the planner under #22663.

```json plan-review-round
{"evidence_id":"4406027e-ff69-44c5-8820-35a005a20a7f","plan_hash":"04c1c42eb897f6f2d63000b09244f7ab10bfac11ee839bf3a4c8bf82a039dcee","round_number":1,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"c6ff1b2bfd0589ec6a75bb75c21893a61975cc90b2529ae6c07c3e05e51a1f7a","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":2,"emitted_findings":11,"total":13},"evidence_id":"4406027e-ff69-44c5-8820-35a005a20a7f","lanes":[{"candidate_count":8,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":1,"lane_id":"repository_blast_radius","status":"delegated-verified"},{"candidate_count":4,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":22,"manifest_digest":"8c306493dde84d775551d311d0c522b34eff15a2ed296253145d5cc21c32be9f","status":"valid"},"source_digest":"342770e1fc2598507eef652557787071dcb9076fc031e2d761fd0c5e20701996","version":1},"findings":[{"category":"bad-sequencing","check_key":"live-reconcile-creation-order","description":"C1 targets crates/gclient/src/app/live_reconcile.rs, but that file is created by A5. The current graph is C1→C2→C3→A4→A5, so C1 runs before its target exists and P1 is unnecessarily serialized behind P2/P3.","finding_id":"GDR-R1-F01","fix":"Remove C3 from A4's dependencies so A5 follows A4 and creates live_reconcile.rs; then make C1 depend on B3 and A5, leaving C2 and C3 after C1. Update the affected headings and manifest edges.","location":".gobby/plans/gclient-daemon-resilience.md:596,672-699,889-904","prevention":"Topologically walk every new-file target and verify its creating deliverable precedes every consumer; justify each cross-phase edge with the exact produced interface.","principle":"Each deliverable must be executable against the repository state produced by its declared predecessors, and dependency edges must encode real prerequisites.","root_cause":"A4 was made dependent on C3 even though it consumes no C3 interface, which places C1 before A5 even though C1 targets the file A5 creates.","section_id":"C1","severity":"blocking"},{"category":"unhandled-edge","check_key":"restart-grant-survival","description":"A graceful daemon restart revokes the direct gterm grant before gclient can re-take it. The next direct Input is refused, so the plan cannot satisfy 'typing never stops' while D3b.3 requires socket-close finalize to revoke.","finding_id":"GDR-R1-F02","fix":"Choose and specify one coherent contract. To preserve uninterrupted direct typing, make ws_close finalize daemon-side state without revoking the current direct host grant, while explicit release, detach, takeover, terminal removal, and non-direct cleanup still revoke; revise D3b.3 and add a real WebSocketServer/HostClient/gterm restart test that types after cleanup starts and before C2 completes. Otherwise narrow C1/Q1 to admit interruption and take-back.","location":".gobby/plans/gclient-daemon-resilience.md:902-914,1401-1441; src/gobby/servers/websocket/tmux.py:110-122; src/gobby/terminals/leases.py:480-529; src/gobby/terminals/input_grants.py:49-79","prevention":"Trace disconnect behavior end to end through client, websocket cleanup, lease finalization, host reconciliation, and gterm admission before promising continuity.","principle":"A claimed outage invariant must hold across both client state and authoritative server cleanup transitions.","root_cause":"C1 reasons only about gclient retaining Held, while current websocket cleanup finalizes the attachment and synchronizes holder=None to gterm as revoke; D3b.3 explicitly preserves that revoke.","section_id":"C1","severity":"blocking"},{"category":"unhandled-edge","check_key":"host-grant-aggregate-deadline","description":"grant_input/revoke_input can wait 1.5 seconds, reconnect without a bound, then wait another 1.5 seconds. The operation can exceed gclient's 2-second CONTROL_REQUEST_DEADLINE and block D3b's per-terminal host-sync lane longer than the plan claims.","finding_id":"GDR-R1-F03","fix":"Target NativeTerminalRuntime.grant_input/revoke_input and apply one absolute sub-2-second budget across ensure, first attempt, reconnect, retry, writer lock/drain, and reply wait, passing only remaining time downstream. Add a test where both attempts stall and assert total elapsed time remains below the client deadline and all HostClient pending entries clear.","location":".gobby/plans/gclient-daemon-resilience.md:1335-1364,1368-1375,1442-1446; src/gobby/terminals/native_runtime.py:702-722,846-851","prevention":"For every timeout claim, enumerate all awaited phases and retries and test wall-clock time through the highest-level operation.","principle":"A deadline promised to an upstream caller must bound the complete operation, including reconnect and retry, not each individual attempt.","root_cause":"D3 adds a 1.5-second HostClient round-trip timeout but leaves NativeTerminalRuntime's reconnect-and-retry behavior outside that budget.","section_id":"D3","severity":"blocking"},{"category":"unhandled-edge","check_key":"reconcile-latest-lock-order","description":"A newer mutation can commit and win the holder-sync lock, apply the new holder, and release it; the recovery task can then acquire the lock and apply its older pre-lock snapshot last.","finding_id":"GDR-R1-F04","fix":"Specify that reconcile_latest_holder acquires the per-terminal holder-sync lock first, then snapshots the current generation/holder under the lease lock, releases only the lease lock, and performs the host await. Add a barrier-driven race test where the newer normal sync wins first and final host state still matches the newest generation.","location":".gobby/plans/gclient-daemon-resilience.md:1401-1413,1435-1441","prevention":"For every two-lock recovery path, enumerate both lock-acquisition races and prove the final external write corresponds to the newest committed generation.","principle":"Recovery that promises latest-state convergence must linearize its snapshot with the serializer for the external effect.","root_cause":"reconcile_latest_holder snapshots generation/holder under the lease lock before acquiring the holder-sync lock.","section_id":"D3b","severity":"blocking"},{"category":"gobby-format","check_key":"granularity-decision","description":"A3 exceeds the plan-coverage granularity threshold but lacks the required decision; unlike A2 and A4, it does not explain why all targeted production changes must land together.","finding_id":"GDR-R1-F05","fix":"Add a bounded Granularity paragraph proving the seven-file change is one compile/runtime seam, or split open_unresolved job conversion and/or recovery logging into independently closeable deliverables with explicit dependencies and acceptance.","location":".gobby/plans/gclient-daemon-resilience.md:507-594","prevention":"Count distinct production files for every deliverable after target expansion and add the required bounded Granularity decision before review.","principle":"A deliverable touching more than six distinct hand-maintained production files must record why the work is inseparable or be split into independently closable leaves.","root_cause":"A3 combines handshake/recovery, loop integration, unresolved-terminal opening, outcome application, and logging across seven production files without a Granularity decision.","section_id":"A3","severity":"blocking"},{"category":"traceability","check_key":"p1-path-acceptance-parity","description":"Acceptance does not directly cover SetViewport backpressure status, attention-response job conversion, open_unresolved_terminals job conversion, or orphan fetch/kill fan-out with DestroySummary. Existing tests can pass while those paths remain blocking or behavior regresses.","finding_id":"GDR-R1-F06","fix":"Add named held-request liveness/behavior tests for each uncovered path and a bounded final audit proving every run_live_loop branch and post-select helper issues daemon work without awaiting it.","location":".gobby/plans/gclient-daemon-resilience.md:374-383,403-417,464-468,503-505,535-536,578-594,617-628,654-670","prevention":"Build a requirement-to-acceptance matrix for every run_live_loop branch and post-select helper before declaring P1 complete.","principle":"Every behavior and changed path needed for the primary goal must have artifact-backed acceptance that can fail if that path still awaits the daemon or loses its promised outcome.","root_cause":"Implementation prose and Targets were expanded without matching acceptance for several newly converted paths.","section_id":"A1","severity":"blocking"},{"category":"traceability","check_key":"direct-input-authority-boundary","description":"The Decision Record does not preserve b59e4ce9/be35449d precisely: the daemon still owns leases, layout, and workspaces; tmux/web/proxy input remains daemon-mediated; only direct-native keystrokes bypass it. D1a's per-key SELECT removal also contradicts 'Nothing here optimises the daemon keystroke path.'","finding_id":"GDR-R1-F07","fix":"Rewrite the decision boundary verbatim in architectural terms and name D1a's existing per-key lookup removal as the sole daemon-input-path optimization in scope.","location":".gobby/plans/gclient-daemon-resilience.md:91-102,168-172","prevention":"Transcribe owner decisions as an explicit matrix of transport, authority, and in-scope exceptions.","principle":"A plan must state owner decisions at their exact authority boundary and must not contradict its own scoped exception.","root_cause":"The shorthand 'Keystrokes stay off the daemon' omits that only direct-native Input/Paste bypass it, while the next sentence denies optimization of a daemon keystroke path that D1a explicitly optimizes.","section_id":"Decision Record","severity":"blocking"},{"category":"weak-testability","check_key":"freeze-hypothesis-falsification-order","description":"The final D3b test can show an unanswered HostClient wait and prove lock-free bounded recovery, but it cannot prove or refute the original claim that the wait occurred while the lease lock was held.","finding_id":"GDR-R1-F08","fix":"Add an explicit checkpoint after D2/D3 but before D3b that captures the watchdog chain and lock-held=true against the pre-D3b behavior; retain that evidence, then run the final D3b test for lock-held=false and bounded progress. Rephrase Q1 so a mismatch blocks D3b/cutover, not expansion, and do not claim proof of the historical incident.","location":".gobby/plans/gclient-daemon-resilience.md:1377-1446,1545-1555","prevention":"Place causal checkpoints before the mutation that removes the hypothesized condition, and distinguish historical attribution from final regression proof.","principle":"A diagnostic can falsify a historical mechanism only if it observes the mechanism before the repair removes the relevant state.","root_cause":"Q1 assigns falsification to the post-D3b lock-free test and says a mismatch blocks expansion even though the test exists only after expansion and implementation.","section_id":"D3b","severity":"blocking"},{"category":"weak-testability","check_key":"sigstop-health-timing","description":"The five-second SIGSTOP run may produce neither Daemon slow nor Daemon unreachable, so it cannot deterministically validate B1/B2/C1.","finding_id":"GDR-R1-F09","fix":"Issue and observe a known daemon request immediately before/during SIGSTOP and hold it past REQUEST_DEADLINE, or keep SIGSTOP active for more than the 15-second idle interval plus the 5-second request deadline. Record exact timing and expected status transitions.","location":".gobby/plans/gclient-daemon-resilience.md:792-839,1568-1576","prevention":"For timing validations, state the triggering request, start condition, timeout budget, and expected transition timestamps.","principle":"A live validation must force the event it claims to observe and allow enough time for every configured interval and deadline.","root_cause":"The validation stops the daemon for 5 seconds while idle keepalive starts only after 15 seconds; direct typing intentionally sends no daemon request.","section_id":"B2","severity":"blocking"},{"category":"traceability","check_key":"binary-cutover-self-contained","description":"D3 is not decision-complete about cutover: its gcore carrier change must force a coherent gcode/gdaemon/ghook rebuild and promotion in addition to the separately promoted gclient, but neither D3 nor its acceptance says so.","finding_id":"GDR-R1-F10","fix":"State in D3 that the gcore carrier dirties the coherent trio; after all leaves pass, one global quiet-window announcement must rebuild/promote gcode/gdaemon/ghook through promote_workspace_binary_set, promote gclient separately, restart from the main checkout, and read identity/hashes from ~/.gobby/bin. Add the same explicit shared-gate reference to every binary-touching deliverable without promoting per leaf.","location":".gobby/plans/gclient-daemon-resilience.md:122-138,1308-1317,1355-1364,1557-1566","prevention":"For every binary-touching deliverable, name the binaries dirtied by each target and point to the exact batched announcement/build/promotion/installed-hash gate.","principle":"A binary-affecting deliverable must state its complete cutover obligation, including generated crate inputs and the installed artifact set, while shared promotion remains batched.","root_cause":"D3 changes crates/gcore/assets/config/runtime_config_contract.json but the shared gate conditionally says to rebuild the coherent trio only when 'Rust inputs' changed, leaving the JSON carrier and D3's required installed set ambiguous.","section_id":"D3","severity":"blocking"},{"category":"unhandled-edge","check_key":"proxied-input-queue-bound","description":"When the daemon stalls, gclient remains responsive only by accumulating an unbounded per-keystroke queue, creating a memory-exhaustion failure path. The existing pre-grant input path is bounded, so this is a regression in resilience.","finding_id":"GDR-R1-F11","fix":"Use a bounded, nonblocking per-pane queue with an explicit byte/message cap and defined Backpressure/refusal/coalescing policy that preserves ordering. Add a held terminal_input flood test proving rendering/direct input remain live and queued memory stays within the cap.","location":".gobby/plans/gclient-daemon-resilience.md:442-453,486-505","prevention":"Audit every async queue introduced for capacity, overload policy, and a sustained-stall test.","principle":"Removing a blocking await must not replace bounded backpressure with unbounded memory growth under the same stalled dependency.","root_cause":"A2 uses a per-pane UnboundedSender for every proxied/tmux write and defines no byte/message cap or overload behavior.","section_id":"A2","severity":"blocking"}],"reviewer_session":"e7d3ec52-52ab-40cf-abe4-0935bd7cf4e0","round":1,"verdict":"needs_review"},"session_id":"26de7dbf-1f31-455c-ade4-993cc7c42cf6"}
```

- 2026-10-01: Renewed consensus required. R5 bounced `748cd5151a`. `terminal_ws.py` is
  896 lines on 0.5.0, so D1a now moves the input, paste and operator-write path into
  `terminal_ws_write.py`. #22932 deleted `tmux.py`, so D1a.5 and its tmux targets are
  dropped. The stale M1 is withdrawn per the PD ruling on #23106 (memory `f5577ae0`) for the
  Adversary to derive afresh.
- 2026-10-01: Renewed consensus. The Adversary (gobby#14579) independently reviewed
  `03faf9c7d9` against `0.5.0` `8fd2bfd3f2`: the D1a split into `terminal_ws_write.py`, the
  `WRITE_FAULT_NAME` re-export, the source-reading test target, and the #22932 tmux
  deletion hold, and the whole-M1 withdrawal keeps the earlier narrative. No blocker or
  proportionality objection remains. R5 (gobby#14944) packet: LAND on content. The
  Adversary derives the fresh M1 from these bytes.

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Decompose the near-ceiling gclient files
  category: refactor
  task_type: chore
  depends_on: []
  validation_criteria: 'R1.1: The exit, resize and suspend signal helpers live in
    their own module. file: `crates/gclient/src/app/live_loop/signals.rs`.

    R1.2: The local-tab adoption group lives in its own module. file: `crates/gclient/src/app/live_loop/local_adoption.rs`.

    R1.3: `live_loop.rs`, `workspace_actions.rs`, `actions.rs` and `mod.rs` each end
    under 850 raw lines. behavior: "each of the four moved-from files is under 850
    lines" in `crates/gclient/src/app/live_loop.rs`.

    R1.4: The disconnect and control-retirement methods live in their own module.
    file: `crates/gclient/src/app/disconnect.rs`.

    R1.5: The terminal lifecycle actions live in their own module. file: `crates/gclient/src/app/live_loop/lifecycle.rs`.

    R1.7: No behaviour change: the full gclient suite and the screen goldens pass
    unchanged. behavior: "cargo nextest run -p gobby-client passes with no golden
    regeneration" in `crates/gclient/tests/screens.rs`.'
  labels:
  - covers:gclient-daemon-resilience:R1:R1.1
  - covers:gclient-daemon-resilience:R1:R1.2
  - covers:gclient-daemon-resilience:R1:R1.3
  - covers:gclient-daemon-resilience:R1:R1.4
  - covers:gclient-daemon-resilience:R1:R1.5
  - covers:gclient-daemon-resilience:R1:R1.7
  tdd: false
  source_section: R1
  assigned_agent: backend-developer
- title: Loop-liveness test scaffolding
  category: test
  task_type: chore
  depends_on: []
  validation_criteria: 'A0.1: `hold_ws` keeps a matching websocket request pending
    until released and `replies` counts the released reply. file: `crates/gclient/tests/mock_daemon/mod.rs`.

    A0.2: The liveness probe is a reusable helper and its baseline test passes. test:
    `crates/gclient/tests/loop_liveness.rs::held_websocket_request_stays_pending_until_released`.'
  labels:
  - covers:gclient-daemon-resilience:A0:A0.1
  - covers:gclient-daemon-resilience:A0:A0.2
  tdd: false
  source_section: A0
  assigned_agent: backend-developer
- title: Job plumbing, focus hints and geometry
  category: code
  task_type: bug
  depends_on:
  - R1
  - A0
  validation_criteria: 'A1.1: The job types, ledger, coalescer and `spawn_job` exist
    and are `Send`. file: `crates/gclient/src/app/live_loop/jobs.rs`.

    A1.2: `run_live_loop` consumes job outcomes in a branch under `control_rx` and
    its post-select code contains no daemon await. symbol: `run_live_loop`.

    A1.3: A held focus-hint op never stalls frames or ticks. test: `crates/gclient/tests/loop_liveness.rs::a_held_focus_hint_op_never_stalls_frames_or_ticks`.

    A1.4: A resize burst sends at most one resize per pane in flight and the latest
    geometry wins. test: `crates/gclient/tests/loop_liveness.rs::resize_burst_sends_at_most_one_resize_per_pane_in_flight`.

    A1.5: The coalescer keeps one in flight and the latest pending. test: `crates/gclient/src/app/live_loop/jobs/tests.rs::coalescer_keeps_one_in_flight_and_latest_pending`.

    A1.6: A direct source accepts `SetViewport`; when its bounded writer is full,
    the latest viewport is refused with `Backpressure`, the status line reports it,
    and frames, ticks and input on another pane keep progressing. test: `crates/gclient/tests/loop_liveness.rs::direct_set_viewport_backpressure_is_visible_and_never_stalls_the_loop`.

    A1.7: An old-generation job is held, `forget_generation` runs, the same key is
    issued anew, and then the old result lands before the new one. The old result
    settles nothing, and the new result clears the current marker and releases exactly
    one coalesced follow-up. test: `crates/gclient/tests/loop_liveness.rs::a_late_old_generation_outcome_never_settles_the_reissued_job`.'
  labels:
  - covers:gclient-daemon-resilience:A1:A1.1
  - covers:gclient-daemon-resilience:A1:A1.2
  - covers:gclient-daemon-resilience:A1:A1.3
  - covers:gclient-daemon-resilience:A1:A1.4
  - covers:gclient-daemon-resilience:A1:A1.5
  - covers:gclient-daemon-resilience:A1:A1.6
  - covers:gclient-daemon-resilience:A1:A1.7
  tdd: true
  source_section: A1
  implementation_domain: backend
- title: Typed input and control path
  category: code
  task_type: bug
  depends_on:
  - A1
  validation_criteria: 'A2.1: No function in `control.rs` awaits the daemon; writes
    go through the pane writer and releases through the pending flag. file: `crates/gclient/src/app/live_loop/control.rs`.

    A2.2: Typing into a tmux pane while `terminal_input` is held keeps frames flowing.
    test: `crates/gclient/tests/loop_liveness.rs::typing_into_a_tmux_pane_while_terminal_input_is_held_keeps_frames_flowing`.

    A2.3: A focus move writes release before take on the wire. test: `crates/gclient/tests/loop_liveness.rs::a_focus_move_writes_release_before_take_on_the_wire`.

    A2.4: A write error from a stale generation does not mark the pane read-only.
    test: `crates/gclient/tests/loop_liveness.rs::a_write_error_from_a_stale_generation_does_not_mark_the_pane_read_only`.

    A2.5: The control-request machinery lives in its own module, and its control task
    awaits every pending release barrier before writing a take. file: `crates/gclient/src/app/live_control.rs`.

    A2.6: `send_workspace_op` no longer exists; every op sender is sync and the refusal
    toast is raised at apply. file: `crates/gclient/src/app/live_loop/daemon_ops.rs`.

    A2.7: Holding an attention-response request never stalls rendering; the response
    outcome applies exactly once after release. test: `crates/gclient/tests/loop_liveness.rs::a_held_attention_response_never_stalls_frames_or_applies_twice`.

    A2.8: Flooding a pane while `terminal_input` is held keeps its queue at or below
    256 messages and 1 MiB, rejects only the newest over-cap messages with `Backpressure`,
    preserves accepted FIFO order after release, and leaves another direct pane''s
    input plus rendering live. test: `crates/gclient/tests/loop_liveness.rs::a_held_terminal_input_flood_is_bounded_ordered_and_nonblocking`.

    A2.9: With `terminal_input` held on pane A, bytes typed into A and then a focus
    move to B produce, on the wire, A''s accepted writes, then A''s release, then
    B''s take. If the held write then errors after its request was written, A reports
    `WriteUnconfirmed` for it and `WriteAbandoned` with the exact count for the writes
    queued behind it. No A write is sent after the release or under B''s authority,
    and nothing is replayed. Frames and ticks advance throughout. test: `crates/gclient/tests/loop_liveness.rs::held_input_then_focus_move_delivers_or_reports_before_release`.

    A2.10: Closing a pane with queued writes flushes or reports them before the detach,
    and a generation change reports the old generation''s queued writes as abandoned
    and its in-flight write as unconfirmed, without sending either under the new attachment
    id. test: `crates/gclient/tests/loop_liveness.rs::close_and_generation_change_never_replay_queued_input`.

    A2.11: On a direct pane, the frame writer''s receipt resolves while the mock gterm
    delays reading the socket until after it processes the revoke. The release barrier
    resolves on the local receipt alone. The mock''s typed `InputRefused` is reported
    as `WriteUnconfirmed` with no byte count, and nothing is replayed. In a second
    run the mock reads before the revoke, and the bytes are accepted with no refusal
    status. test: `crates/gclient/tests/loop_liveness.rs::direct_flush_receipt_orders_locally_and_late_consumption_is_unconfirmed`.

    A2.12: A daemon `terminal_input` whose request is written and whose reply is never
    delivered (the mock drops the connection with the reply outstanding) is reported
    as `WriteUnconfirmed`, and writes queued behind it are reported as `WriteAbandoned`
    with their exact count. After reconnect the new generation sends none of them.
    A correlated typed refusal is reported as refused with its request''s size. test:
    `crates/gclient/tests/loop_liveness.rs::indeterminate_daemon_write_is_unconfirmed_and_never_replayed`.

    A2.13: With a held writer, focus moves A to B to C. Each take waits on its own
    receiver for every earlier release, each release follows its pane''s accepted
    bytes and that pane''s own take, and exactly one daemon release per pane is sent.
    test: `crates/gclient/tests/loop_liveness.rs::focus_a_b_c_with_a_held_writer_orders_every_release_before_the_next_take`.

    A2.14: Closing A while its release barrier is pending sends one release and then
    the close''s detach from the same completion. The barrier slot never overfills
    and the detach never precedes the release. test: `crates/gclient/tests/loop_liveness.rs::closing_a_pane_while_its_release_is_pending_detaches_after_the_release`.

    A2.15: `WriteAbandoned` and `WriteUnconfirmed` reports from the old generation
    still show their status after a reconnect. test: `crates/gclient/tests/loop_liveness.rs::old_generation_write_uncertainty_is_reported_after_reconnect`.'
  labels:
  - covers:gclient-daemon-resilience:A2:A2.1
  - covers:gclient-daemon-resilience:A2:A2.2
  - covers:gclient-daemon-resilience:A2:A2.3
  - covers:gclient-daemon-resilience:A2:A2.4
  - covers:gclient-daemon-resilience:A2:A2.5
  - covers:gclient-daemon-resilience:A2:A2.6
  - covers:gclient-daemon-resilience:A2:A2.7
  - covers:gclient-daemon-resilience:A2:A2.8
  - covers:gclient-daemon-resilience:A2:A2.9
  - covers:gclient-daemon-resilience:A2:A2.10
  - covers:gclient-daemon-resilience:A2:A2.11
  - covers:gclient-daemon-resilience:A2:A2.12
  - covers:gclient-daemon-resilience:A2:A2.13
  - covers:gclient-daemon-resilience:A2:A2.14
  - covers:gclient-daemon-resilience:A2:A2.15
  tdd: true
  source_section: A2
  implementation_domain: backend
- title: Attach and recovery as jobs
  category: code
  task_type: bug
  depends_on:
  - A2
  validation_criteria: 'A3.1: The `created` event path marks the pane due for `start_due_attaches`
    instead of awaiting `attach_ready_panes`. symbol: `apply_live_event`.

    A3.2: A held attach on one pane keeps the other pane rendering. test: `crates/gclient/tests/loop_liveness.rs::a_held_attach_on_one_pane_keeps_the_other_pane_rendering`.

    A3.3: An attach outcome from a previous generation is dropped and its attachment
    detached. test: `crates/gclient/tests/loop_liveness.rs::an_attach_outcome_from_a_previous_generation_is_dropped_and_detached`.

    A3.4: Recovery detaches then attaches while input still routes. test: `crates/gclient/tests/loop_liveness.rs::recovery_detaches_then_attaches_while_input_still_routes`.

    A3.6: A frame error and every recovery give-up leave one structured record with
    pane, terminal, attachment, generation, stage, error class and `elapsed_ms`, while
    a successful replacement closes the attempt. The same test covers a direct-handshake
    timeout and a `RetireReason::Lag` retirement. test: `crates/gclient/tests/loop_liveness.rs::frame_errors_and_recovery_giveups_are_logged_with_pane_context`.

    A3.7: A newly created terminal whose `terminal_attach` reply takes three seconds
    does not stop frames, ticks or input on existing direct panes. test: `crates/gclient/tests/loop_liveness.rs::a_slow_new_terminal_attach_never_stalls_streaming_direct_panes`.'
  labels:
  - covers:gclient-daemon-resilience:A3:A3.1
  - covers:gclient-daemon-resilience:A3:A3.2
  - covers:gclient-daemon-resilience:A3:A3.3
  - covers:gclient-daemon-resilience:A3:A3.4
  - covers:gclient-daemon-resilience:A3:A3.6
  - covers:gclient-daemon-resilience:A3:A3.7
  tdd: true
  source_section: A3
  implementation_domain: backend
- title: Open unresolved terminals as jobs
  category: code
  task_type: bug
  depends_on:
  - A3
  validation_criteria: 'A3b.1: A held unresolved-terminal open keeps frames, ticks
    and direct input progressing, installs the terminal once after release, and coalesces
    repeated workspace events. test: `crates/gclient/tests/loop_liveness.rs::a_held_open_unresolved_terminal_job_is_coalesced_and_nonblocking`.

    A3b.2: The inline reconcile helper remains available while the live workspace
    event path issues `Opened` jobs without awaiting. symbol: `open_unresolved_terminals`.

    A3b.3: Unresolved-job issuance and apply helpers live outside the near-ceiling
    loop module. file: `crates/gclient/src/app/live_loop/unresolved.rs`.

    A3b.4: A failed unresolved-terminal open emits one WARN with terminal, generation,
    error class and `elapsed_ms`; the error is no longer discarded. test: `crates/gclient/tests/loop_liveness.rs::an_unresolved_terminal_open_failure_is_logged_with_elapsed_time`.'
  labels:
  - covers:gclient-daemon-resilience:A3b:A3b.1
  - covers:gclient-daemon-resilience:A3b:A3b.2
  - covers:gclient-daemon-resilience:A3b:A3b.3
  - covers:gclient-daemon-resilience:A3b:A3b.4
  tdd: true
  source_section: A3b
  implementation_domain: backend
- title: Event-driven refetches and roster jobs
  category: code
  task_type: bug
  depends_on:
  - A3b
  validation_criteria: 'A4a.1: A roster for a project no longer focused is dropped
    (was A4.3). test: `crates/gclient/tests/loop_liveness.rs::a_roster_for_a_project_no_longer_focused_is_dropped`.

    A4a.2: `apply_live_event` is sync and sets refetch flags instead of awaiting (was
    A4.5). symbol: `apply_live_event`.

    A4a.3: Failed REST components and both `Lagged` entry paths emit WARN records
    with operation/source, generation, error class or skipped count, and `elapsed_ms`,
    while successful partial rows still apply (was A4.8). test: `crates/gclient/tests/loop_liveness.rs::rest_failures_and_lagged_events_warn_with_elapsed_time`.'
  labels:
  - covers:gclient-daemon-resilience:A4a:A4a.1
  - covers:gclient-daemon-resilience:A4a:A4a.2
  - covers:gclient-daemon-resilience:A4a:A4a.3
  tdd: true
  source_section: A4a
  implementation_domain: backend
- title: Lifecycle and shell-adoption actions as chained jobs
  category: code
  task_type: bug
  depends_on:
  - A4a
  validation_criteria: 'A4b.1: A held `terminal_create` keeps the window interactive
    and places the pane after the create lands (was A4.1). test: `crates/gclient/tests/loop_liveness.rs::a_held_terminal_create_keeps_the_window_interactive_and_places_after_create`.

    A4b.2: Closing a tab issues `TabClose` only after every kill settles (was A4.2).
    test: `crates/gclient/tests/loop_liveness.rs::closing_a_tab_issues_tab_close_only_after_every_kill_settles`.

    A4b.3: Two new tabs before the first create lands place both (was A4.4). test:
    `crates/gclient/tests/loop_liveness.rs::two_new_tabs_before_the_first_create_lands_place_both`.

    A4b.4: Splitting right while the old connection''s release/take or terminal-create
    reply is held keeps direct input, frame ingest and rendering live; the placement
    is applied only after its own job settles (was A4.6). test: `crates/gclient/tests/loop_liveness.rs::split_right_with_a_held_control_or_create_reply_keeps_the_window_live`.

    A4b.5: A held `TabCreate` in the gclient-owned shell adoption chain keeps frames,
    ticks and direct input live; the chain resumes from each outcome, and a refused
    step rolls back the adopted tab (was A4.9). test: `crates/gclient/tests/loop_liveness.rs::a_held_adoption_step_keeps_the_window_live_and_rolls_back_on_refusal`.

    A4b.6: A `terminal_create` reply that lands after the loop''s generation changed
    kills the unplaced terminal exactly once after the new workspace installs and
    places no pane. A create whose connection is lost before its reply is neither
    replayed nor compensated. test: `crates/gclient/tests/loop_liveness.rs::a_stale_committed_create_is_compensated_once_and_an_unanswered_create_is_not_replayed`.

    A4b.7: An adoption `TabCreate` reply that lands after its target project closed
    runs one rollback `TabClose` for the captured tab. test: `crates/gclient/tests/loop_liveness.rs::a_stale_committed_adoption_step_rolls_back_its_captured_tab`.

    A4b.8: A stale create reply is disposed exactly once in both orders: arriving
    after the new generation''s model is installed, it is killed at once; arriving
    before, it is killed when that model installs. A disposal kill whose reply is
    lost keeps its entry and raises one toast. The next install re-evaluates the entry
    and clears it once the terminal is gone. test: `crates/gclient/tests/loop_liveness.rs::a_stale_create_is_disposed_once_in_either_order_and_a_failed_disposal_stays_owned`.'
  labels:
  - covers:gclient-daemon-resilience:A4b:A4b.1
  - covers:gclient-daemon-resilience:A4b:A4b.2
  - covers:gclient-daemon-resilience:A4b:A4b.3
  - covers:gclient-daemon-resilience:A4b:A4b.4
  - covers:gclient-daemon-resilience:A4b:A4b.5
  - covers:gclient-daemon-resilience:A4b:A4b.6
  - covers:gclient-daemon-resilience:A4b:A4b.7
  - covers:gclient-daemon-resilience:A4b:A4b.8
  tdd: true
  source_section: A4b
  implementation_domain: backend
- title: Orphan inventory and destroy fan-out as jobs
  category: code
  task_type: bug
  depends_on:
  - A4b
  validation_criteria: 'A4c.1: Holding orphan inventory leaves the dialog and window
    live; after release, orphan kills fan out concurrently and `DestroySummary` preserves
    one result per requested orphan regardless of completion order (was A4.7). test:
    `crates/gclient/tests/loop_liveness.rs::held_orphan_fetch_and_kill_fanout_are_nonblocking_and_complete`.'
  labels:
  - covers:gclient-daemon-resilience:A4c:A4c.1
  tdd: true
  source_section: A4c
  implementation_domain: backend
- title: Launch and reconnect audit and WARN bracket
  category: code
  task_type: bug
  depends_on:
  - A4c
  validation_criteria: 'A5.2: A reconcile from a stale generation is dropped. test:
    `crates/gclient/tests/loop_liveness.rs::a_reconcile_from_a_stale_generation_is_dropped`.

    A5.4: A bounded final audit enumerates launch/reconcile, control, input, daemon-event,
    frame-recovery, sidebar, resize/tick attach, focus-hint, geometry, attention,
    unresolved-open, roster/orphan, lifecycle-action and shell-adoption paths; every
    daemon-touching `run_live_loop` branch or post-select helper maps to a held-request
    case that advances frames, ticks and unaffected direct input. test: `crates/gclient/tests/loop_liveness.rs::every_run_live_loop_daemon_path_is_issued_without_awaiting`.

    A5.5: Each reconnect/reconcile attempt emits exactly one start and one finish
    WARN with reason, generations, outcome and `elapsed_ms`, including success, timeout
    and transport-loss cases. test: `crates/gclient/tests/loop_liveness.rs::reconnect_attempts_warn_once_at_start_and_finish_with_reason`.

    A5.6: With `GET /api/agents/runs` held, a prefix chord toggles the sidebar immediately
    and applies once. test: `crates/gclient/tests/loop_liveness.rs::a_chrome_chord_applies_while_a_rest_request_is_held`.'
  labels:
  - covers:gclient-daemon-resilience:A5:A5.2
  - covers:gclient-daemon-resilience:A5:A5.4
  - covers:gclient-daemon-resilience:A5:A5.5
  - covers:gclient-daemon-resilience:A5:A5.6
  tdd: true
  source_section: A5
  implementation_domain: backend
- title: Daemon health from in-flight age
  category: code
  task_type: feature
  depends_on:
  - A5
  validation_criteria: 'B1.1: Every waiter records `issued_at` and `oldest_inflight_age`
    reports the oldest. symbol: `LiveState`.

    B1.2: `DaemonHealth` derives `Slow` past the threshold and `Unreachable` on disconnect.
    test: `crates/gclient/src/app/live_loop/health/tests.rs::health_is_slow_once_the_oldest_in_flight_request_passes_the_threshold`.

    B1.3: The status line shows `Daemon slow` after 1 s of a held `workspace_op` and
    clears when the reply lands. test: `crates/gclient/tests/loop_liveness.rs::a_slow_workspace_op_reply_shows_daemon_slow_until_it_lands`.

    B1.4: The guide''s status-line section describes the slow state. behavior: "Daemon
    slow" in `docs/guides/gclient-user-guide.md`.'
  labels:
  - covers:gclient-daemon-resilience:B1:B1.1
  - covers:gclient-daemon-resilience:B1:B1.2
  - covers:gclient-daemon-resilience:B1:B1.3
  - covers:gclient-daemon-resilience:B1:B1.4
  tdd: true
  source_section: B1
  implementation_domain: backend
- title: Shed load while slow
  category: code
  task_type: bug
  depends_on:
  - B1
  validation_criteria: 'B3.1: No sidebar `/api/source-control/status` or roster refresh
    request is recorded while the daemon is slow, and polling resumes on `Ready`.
    test: `crates/gclient/tests/loop_liveness.rs::sidebar_polls_pause_while_the_daemon_is_slow_and_resume_on_ready`.

    B3.2: The guide''s status-line section says polling pauses while slow. behavior:
    "pauses" in `docs/guides/gclient-user-guide.md`.'
  labels:
  - covers:gclient-daemon-resilience:B3:B3.1
  - covers:gclient-daemon-resilience:B3:B3.2
  tdd: true
  source_section: B3
  implementation_domain: backend
- title: Native inventory and fallback input authority in gterm
  category: code
  task_type: feature
  depends_on:
  - B3
  - A5
  validation_criteria: 'C0.1: An authenticated frame client lists only committed live
    native terminals with stable terminal/host ids and host epoch; unauthenticated
    and tmux inventory is unavailable. test: `crates/gterminal/tests/frame_protocol.rs::daemonless_native_inventory_is_authenticated_and_excludes_tmux`.

    C0.2: With no control owner or grant, the first bound local attachment can type
    and paste; a second attachment cannot replace it and receives `input_not_granted`.
    test: `crates/gterminal/tests/frame_protocol.rs::first_local_attachment_holds_fallback_input_until_detach`.

    C0.3: Detaching clears its local fallback, while detaching another frame stream
    cannot clear either the fallback or a daemon grant. test: `crates/gterminal/tests/frame_protocol.rs::frame_detach_clears_only_its_local_fallback_grant`.

    C0.4: A control owner disables new fallback authority; its `grant_input` replaces
    an existing fallback, and matching/unconditional revoke clears the resulting holder.
    test: `crates/gterminal/tests/control_protocol.rs::daemon_grant_replaces_local_fallback_and_restores_arbitration`.

    C0.5: The protocol contract and wire goldens describe the authenticated native
    inventory and local-fallback/daemon-takeover rules. behavior: "local fallback
    holder" in `docs/contracts/gterm-protocols.md`.

    C0.6: Last-control-owner disconnect transfers a bound daemon grant to that same
    frame connection; rebinding it to the fresh reconnect attachment id preserves
    input until the next daemon grant replaces the fallback. test: `crates/gterminal/tests/control_protocol.rs::disconnect_and_rebind_transfer_the_daemon_grant_without_an_input_gap`.

    C0.7: Over two real control connections, a `grant_input` sent on the older connection
    after the newer one revoked or granted the same terminal is refused `stale_grant_connection`
    and leaves the newer holder in place. The same late request before any newer authority
    change applies and is then replaced. test: `crates/gterminal/tests/control_protocol.rs::a_late_grant_from_a_retired_connection_cannot_overwrite_newer_authority`.'
  labels:
  - covers:gclient-daemon-resilience:C0:C0.1
  - covers:gclient-daemon-resilience:C0:C0.2
  - covers:gclient-daemon-resilience:C0:C0.3
  - covers:gclient-daemon-resilience:C0:C0.4
  - covers:gclient-daemon-resilience:C0:C0.5
  - covers:gclient-daemon-resilience:C0:C0.6
  - covers:gclient-daemon-resilience:C0:C0.7
  tdd: true
  source_section: C0
  implementation_domain: backend
- title: Read native inventory through the existing frame socket
  category: code
  task_type: feature
  depends_on:
  - C0
  validation_criteria: 'C0b.1: `NativeHost` uses the existing authenticated frame
    codec and returns typed native inventory without any daemon request. file: `crates/gclient/src/frame_source/native_host.rs`.

    C0b.2: Against a real gterm with no lasting control connection, gclient lists
    a native terminal, attaches it and receives frames within one 2 s aggregate local
    deadline. test: `crates/gclient/tests/frame_source_live.rs::native_inventory_and_attach_work_without_a_daemon`.

    C0b.3: Missing, refused and timed-out local-host probes return typed outcomes
    and leave no reader/writer task or socket alive. test: `crates/gclient/tests/frame_source_live.rs::native_inventory_failure_cleans_up_the_frame_connection`.'
  labels:
  - covers:gclient-daemon-resilience:C0b:C0b.1
  - covers:gclient-daemon-resilience:C0b:C0b.2
  - covers:gclient-daemon-resilience:C0b:C0b.3
  tdd: true
  source_section: C0b
  implementation_domain: backend
- title: Launch, render and type native panes with no daemon
  category: code
  task_type: feature
  depends_on:
  - C0b
  - A5
  validation_criteria: 'C0c.1: A missing default daemon token produces an empty degraded
    Ready window, an unreachable daemon with a valid local token can use the native
    host, and an explicitly requested bad token file remains a startup error. test:
    `crates/gclient/tests/startup.rs::missing_default_daemon_token_starts_in_native_degraded_mode`.

    C0c.2: With the daemon absent and a real gterm present, gclient launches, lists
    native panes only, attaches, renders advancing frames and delivers typed bytes
    to the PTY. test: `crates/gclient/tests/loop_liveness.rs::daemonless_launch_attaches_renders_and_types_native_panes`.

    C0c.3: A competing local gclient that receives `input_not_granted` becomes read-only
    without daemon fallback, while the first holder keeps typing. test: `crates/gclient/tests/loop_liveness.rs::daemonless_second_client_cannot_take_over_the_local_holder`.

    C0c.4: When the daemon connects after degraded launch, matching terminal ids preserve
    one direct source, daemon layout replaces local tabs, the adopted holder stays
    Held with `lease_unconfirmed` set, and no duplicate pane appears. test: `crates/gclient/tests/loop_liveness.rs::daemon_reconcile_adopts_daemonless_native_panes_without_duplication`.

    C0c.5: Without the daemon, the UI states the unavailable enhancements and never
    lists tmux, web or proxied panes. behavior: "Native degraded mode" in `docs/guides/gclient-user-guide.md`.

    C0c.6: With two native terminals and no daemon, gclient opens one independently
    attached direct pane per inventory row; frames advance and typed bytes reach the
    correct PTY on both streams. test: `crates/gclient/tests/loop_liveness.rs::daemonless_launch_attaches_and_types_multiple_native_panes_independently`.

    C0c.7: With the daemon absent, focus moves A to B to A by keyboard and by mouse
    without any daemon release or take, both panes stay Held, and typed bytes reach
    both PTYs. An explicit take-back answers the reconnect status. test: `crates/gclient/tests/loop_liveness.rs::daemonless_focus_moves_between_direct_panes_and_types_into_both_ptys`.

    C0c.8: A native inventory result, or a native attach result, held until after
    the daemon workspace installs is disposed: every frame connection it holds is
    closed and no duplicate pane appears. The same holds when the terminal was removed
    first, and when the loop exits while the job holds sources. test: `crates/gclient/tests/loop_liveness.rs::a_late_native_result_after_the_daemon_workspace_is_disposed_and_closes_its_sources`.'
  labels:
  - covers:gclient-daemon-resilience:C0c:C0c.1
  - covers:gclient-daemon-resilience:C0c:C0c.2
  - covers:gclient-daemon-resilience:C0c:C0c.3
  - covers:gclient-daemon-resilience:C0c:C0c.4
  - covers:gclient-daemon-resilience:C0c:C0c.5
  - covers:gclient-daemon-resilience:C0c:C0c.6
  - covers:gclient-daemon-resilience:C0c:C0c.7
  - covers:gclient-daemon-resilience:C0c:C0c.8
  tdd: true
  source_section: C0c
  implementation_domain: backend
- title: Keep control on disconnect for direct-granted panes
  category: code
  task_type: bug
  depends_on:
  - C0c
  validation_criteria: 'C1.1: A daemon-granted or local-fallback direct pane stays
    Held with `lease_unconfirmed` when the mock daemon closes the socket, and the
    mock frame host still receives `Input`. test: `crates/gclient/tests/loop_liveness.rs::a_direct_granted_pane_keeps_typing_through_a_daemon_disconnect`.

    C1.2: A proxied pane drops to Observe on the same disconnect. test: `crates/gclient/tests/loop_liveness.rs::a_proxied_pane_drops_to_observe_on_daemon_disconnect`.

    C1.3: The protocol contract states the new outage contract. behavior: "keeps typing"
    in `docs/contracts/gterm-protocols.md`.

    C1.4: The guide''s status-string table carries the unconfirmed-lease text. behavior:
    "typing continues on the host" in `docs/guides/gclient-user-guide.md`.'
  labels:
  - covers:gclient-daemon-resilience:C1:C1.1
  - covers:gclient-daemon-resilience:C1:C1.2
  - covers:gclient-daemon-resilience:C1:C1.3
  - covers:gclient-daemon-resilience:C1:C1.4
  tdd: true
  source_section: C1
  implementation_domain: backend
- title: Reconfirm the lease on reconnect
  category: code
  task_type: bug
  depends_on:
  - C1
  validation_criteria: 'C2.1: On reconnect every unconfirmed direct pane rebinds its
    preserved frame stream before `terminal_take_control`; bytes typed before the
    reply continue to reach the host, and `granted` clears the unconfirmed state without
    duplicating the source. test: `crates/gclient/tests/loop_liveness.rs::an_unconfirmed_direct_stream_rebinds_and_keeps_typing_until_retake_grants`.

    C2.2: `lease_unconfirmed` clears on grant and a `held` reply drops the pane to
    Observe with take-back. symbol: `Pane`.

    C2.3: After a daemonless launch (C0c), the daemon''s arrival retakes every adopted
    unconfirmed holder: each rebinds its preserved stream and issues one take, typing
    continues until the grant, and no duplicate source appears. test: `crates/gclient/tests/loop_liveness.rs::daemon_arrival_after_degraded_launch_retakes_every_adopted_holder`.

    C2.4: Each of three unconfirmed panes issues its own take. One take fails while
    the daemon socket stays healthy; it is retried at most three times and then drops
    that pane to Observe with take-back, while the other two confirm. test: `crates/gclient/tests/loop_liveness.rs::every_unconfirmed_pane_retakes_and_one_failed_take_retries_boundedly`.

    C2.5: `granted: true` with `host_input_granted: false` keeps `lease_unconfirmed`
    set and claims no typing authority until a repeated take confirms the host grant.
    test: `crates/gclient/tests/loop_liveness.rs::a_lease_grant_without_a_host_grant_does_not_confirm_typing`.'
  labels:
  - covers:gclient-daemon-resilience:C2:C2.1
  - covers:gclient-daemon-resilience:C2:C2.2
  - covers:gclient-daemon-resilience:C2:C2.3
  - covers:gclient-daemon-resilience:C2:C2.4
  - covers:gclient-daemon-resilience:C2:C2.5
  tdd: true
  source_section: C2
  implementation_domain: backend
- title: Direct re-attach before proxy fallback
  category: code
  task_type: bug
  depends_on:
  - C2
  validation_criteria: 'C3.1: A direct source error followed by a successful direct
    reconnect records `AttachTerminal` plus `BindAttachment` and no `terminal_attach`
    daemon request. test: `crates/gclient/tests/frame_source_live.rs::a_direct_source_error_reattaches_directly_before_asking_the_daemon`.

    C3.2: The contract and guide state direct-first recovery. behavior: "direct re-attach"
    in `docs/contracts/gterm-protocols.md`.

    C3.3: With the daemon absent, a fallback holder''s stream fails and the replacement
    stream reclaims fallback, so typed bytes reach the PTY. If a peer local client
    claimed first, the replacement is refused and drops to Observe. test: `crates/gclient/tests/loop_liveness.rs::a_lost_fallback_stream_reclaims_only_when_no_peer_holds_input`.

    C3.4: A fallback stream lost after the daemon has returned, but before C2''s retake
    grants, cannot reclaim fallback. Its input is refused until the retake grants
    the fresh attachment id; after that, bytes reach the PTY. test: `crates/gclient/tests/loop_liveness.rs::a_fallback_stream_lost_during_daemon_return_types_only_after_the_retake`.'
  labels:
  - covers:gclient-daemon-resilience:C3:C3.1
  - covers:gclient-daemon-resilience:C3:C3.2
  - covers:gclient-daemon-resilience:C3:C3.3
  - covers:gclient-daemon-resilience:C3:C3.4
  tdd: true
  source_section: C3
  implementation_domain: backend
- title: Terminal attach, sizing, scroll, create and kill handlers on the executor
  category: code
  task_type: bug
  depends_on: []
  validation_criteria: 'D1a.1: Attach resolves its row on the executor. test: `tests/servers/test_terminal_ws_attach_honesty.py::test_attach_resolves_the_row_on_the_db_executor`.

    D1a.2: Sizing runs get and `set_dims` as one executor hop. test: `tests/servers/test_terminal_ws_resize.py::test_sizing_runs_get_and_set_dims_as_one_executor_hop`.

    D1a.3: A scroll-offset message makes no storage call. test: `tests/servers/test_terminal_ws_golden.py::test_scroll_offset_makes_no_storage_call`.

    D1a.4: Create and kill run their storage on the executor. test: `tests/servers/test_terminal_ws_create.py::test_create_and_kill_run_storage_on_the_executor`.'
  labels:
  - covers:gclient-daemon-resilience:D1a:D1a.1
  - covers:gclient-daemon-resilience:D1a:D1a.2
  - covers:gclient-daemon-resilience:D1a:D1a.3
  - covers:gclient-daemon-resilience:D1a:D1a.4
  tdd: true
  source_section: D1a
  implementation_domain: backend
- title: Bounded HostClient round trips
  category: code
  task_type: bug
  depends_on:
  - C0
  validation_criteria: 'D3.1: A round trip past its deadline raises `HostUnavailableError("timed
    out")` and drops the pending future. test: `tests/terminals/test_host_client.py::test_roundtrip_times_out_with_host_unavailable`.

    D3.2: Grant and revoke use one absolute 1.5 s budget across ensure, a first attempt
    ending in EOF, reconnect and the second attempt. With the retry stalled, wall
    time stays below 2 s, every HostClient pending entry is removed, and a competing
    acquisition of the same `TerminalLeaseRegistry.lock(terminal_id)` succeeds by
    that original deadline. test: `tests/terminals/test_native_runtime.py::test_eof_reconnect_and_retry_share_one_deadline_and_release_the_lease_lock`.

    D3.3: `control_timeout_seconds` defaults to 5.0 and is exported to the runtime
    config contract. test: `tests/config/test_terminal_host_config.py::test_control_timeout_seconds_default`.

    D3.4: Validation treats the regenerated gcore carrier as an input change to the
    coherent trio and records the batched installed identity/hashes from `~/.gobby/bin/`
    plus separate gclient promotion. file: `crates/gcore/assets/config/runtime_config_contract.json`.

    D3.5: The host process helpers live in their own module and `host_manager.py`
    is under 850 lines. file: `src/gobby/terminals/host_process.py`.

    D3.6: A mutating request that times out, including time spent waiting for `_operation_lock`,
    retires the connection. The next mutation runs on a new connection from `operation_seq`
    1 with no `operation_conflict`, and the timed-out request is not re-sent. test:
    `tests/terminals/test_host_client.py::test_mutating_timeout_retires_the_connection_and_never_reuses_its_sequence`.

    D3.7: A grant whose fake-host reply is withheld past its deadline retires the
    connection, and the following revoke is issued on a new connection. test: `tests/terminals/test_host_client.py::test_grant_timeout_retires_the_connection_before_the_next_authority_change`.

    D3.8: A grant is written and the fake host parks it at a barrier before applying
    it. The fake host enforces C0''s per-slot connection fence, whose real-host rule
    C0.7 proves. The caller is cancelled before its deadline. That cancellation retires
    the connection, and the recovery revoke is issued on a newer connection. Releasing
    the parked grant afterwards leaves the revoke in force. A request cancelled before
    it is written leaves the connection in place. test: `tests/terminals/test_host_client.py::test_cancelling_a_written_grant_retires_the_connection_so_newer_authority_survives`.

    D3.9: In this test the old writer''s `wait_closed` never returns and its reader
    ignores cancellation. Retirement still returns without awaiting either, and the
    detached cleanup ends at its bound. The grant''s aggregate budget holds, and a
    reconnect installed while the stale cleanup runs keeps its reader, writer and
    generation after that cleanup ends. It also covers an old request''s timeout or
    cancellation handler that runs after another caller has reconnected: its retirement
    names the old generation and leaves the new connection open. test: `tests/terminals/test_host_client.py::test_retirement_fences_before_cleanup_and_stale_cleanup_leaves_the_new_connection`.

    D3.10: A timed-out grant, revoke, resize, kill or `terminate` is not re-sent after
    the runtime reconnects. In `_write`, a timed-out host write, or one whose connection
    is lost after its bytes were written, returns `IndeterminateWrite`. That includes
    the text+submit path when the loss hits either the text or the `enter` write.
    A loss before the first write stays `TerminalWriteError(stage="none")`. An EOF
    inside the budget still gets its one retry. A timed-out `write_batch` reports
    `IndeterminateWrite` for every target. test: `tests/terminals/test_native_runtime.py::test_abandoned_requests_are_never_replayed_and_timed_out_writes_are_indeterminate`.'
  labels:
  - covers:gclient-daemon-resilience:D3:D3.1
  - covers:gclient-daemon-resilience:D3:D3.2
  - covers:gclient-daemon-resilience:D3:D3.3
  - covers:gclient-daemon-resilience:D3:D3.4
  - covers:gclient-daemon-resilience:D3:D3.5
  - covers:gclient-daemon-resilience:D3:D3.6
  - covers:gclient-daemon-resilience:D3:D3.7
  - covers:gclient-daemon-resilience:D3:D3.8
  - covers:gclient-daemon-resilience:D3:D3.9
  - covers:gclient-daemon-resilience:D3:D3.10
  tdd: true
  source_section: D3
  implementation_domain: backend
- title: Host grant reconciliation never holds the terminal lease lock
  category: code
  task_type: bug
  depends_on:
  - D1a
  - D3
  - C2
  validation_criteria: 'D3b.1: The transition matrix sees `lock_held(terminal_id)
    == false`; direct `ws_close` preserves the current host grant, while repeated/first
    take, takeover, explicit release, detach, terminal removal and non-direct cleanup
    replace or revoke it. test: `tests/terminals/test_lease_authority.py::test_holder_transition_matrix_applies_preserve_and_revoke_policies_outside_the_lease_lock`.

    D3b.2: A stalled older grant cannot overwrite a newer takeover or final revoke;
    after the stall releases, the last host notification matches the latest generation.
    test: `tests/terminals/test_lease_authority.py::test_stalled_holder_sync_converges_to_the_latest_generation`.

    D3b.3: With a real WebSocketServer, HostClient and gterm, restarting the isolated
    daemon finalizes its websocket state without revoking the direct grant; `Input`
    typed after cleanup starts and while the reconnect take is paused still reaches
    the PTY, then C2 replaces the grant and an explicit detach refuses later input.
    test: `tests/e2e/test_terminal_client_stack.py::test_direct_native_input_survives_daemon_restart_until_lease_retake`.

    D3b.4: A test barrier holds an actual `HostClient` grant after request registration;
    while it is pending, `lock_held(terminal_id) == false`, another connection lane
    and same-terminal lease-state acquisition complete, and D3''s deadline clears
    the pending request without any production watchdog. test: `tests/servers/test_terminal_ws_lease.py::test_unanswered_host_grant_is_bounded_and_lock_free`.

    D3b.5: In the barrier race where the newer normal sync wins the holder-sync lock
    before recovery, recovery snapshots only after acquiring that lock and the final
    host state still matches the newest generation. test: `tests/terminals/test_lease_authority.py::test_reconcile_latest_holder_snapshots_under_the_sync_lock_after_newer_sync_wins`.

    D3b.6: An older take''s observer is held while a newer takeover or finalize commits.
    The older reply carries its own captured generation and sizing with `host_input_granted=False`,
    sizing is never recomputed outside the lease lock, and the final host state matches
    the newer generation. test: `tests/terminals/test_lease_authority.py::test_a_superseded_take_reply_keeps_its_captured_generation_and_claims_no_host_grant`.'
  labels:
  - covers:gclient-daemon-resilience:D3b:D3b.1
  - covers:gclient-daemon-resilience:D3b:D3b.2
  - covers:gclient-daemon-resilience:D3b:D3b.3
  - covers:gclient-daemon-resilience:D3b:D3b.4
  - covers:gclient-daemon-resilience:D3b:D3b.5
  - covers:gclient-daemon-resilience:D3b:D3b.6
  tdd: true
  source_section: D3b
  implementation_domain: backend
- title: Pool exhaustion diagnosis
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: 'D4.1: The final acquire failure logs a `pg_stat_activity`
    census, and concurrent failures within a minute run at most one census. test:
    `tests/storage/hub/test_postgres_pool_backoff.py::test_final_acquire_failure_logs_a_rate_limited_activity_census`.

    D4.3: A census refused with SQLSTATE `53300` logs capacity exhaustion; an auth,
    network or server-down refusal logs its class and SQLSTATE with no capacity claim,
    and no log line carries the exception text or DSN. test: `tests/storage/hub/test_postgres_pool_backoff.py::test_census_failure_reason_is_typed_and_claims_capacity_only_for_53300`.

    D4.4: The caller receives the original `PoolTimeout` object at once, whether the
    census succeeds, fails or stalls. While a stalled census is in flight, a later
    failure starts no second census, and the census connection carries `connect_timeout`,
    `statement_timeout` and the keepalive parameters. test: `tests/storage/hub/test_postgres_pool_backoff.py::test_census_is_bounded_and_preserves_the_original_acquisition_error`.

    D4.2: `psycopg.pool` warnings reach the daemon log as sanitized records. A warning
    whose exception text carries a sentinel credential and DSN writes the pool name,
    exception class and SQLSTATE to `daemon.log`, and the sentinel never appears.
    test: `tests/telemetry/test_logging.py::test_psycopg_pool_warnings_reach_the_daemon_log`.'
  labels:
  - covers:gclient-daemon-resilience:D4:D4.1
  - covers:gclient-daemon-resilience:D4:D4.3
  - covers:gclient-daemon-resilience:D4:D4.4
  - covers:gclient-daemon-resilience:D4:D4.2
  tdd: true
  source_section: D4
  implementation_domain: backend
- title: Drop the undrained host event subscription
  category: code
  task_type: bug
  depends_on:
  - D3
  validation_criteria: 'E1.1: `reserve_observer` sends no `subscribe_events`. test:
    `tests/terminals/test_native_runtime.py::test_reserve_observer_sends_no_subscribe_events`.

    E1.2: The spawn-failure helpers live in their own module and are re-exported.
    file: `src/gobby/terminals/native_spawn_failure.py`.'
  labels:
  - covers:gclient-daemon-resilience:E1:E1.1
  - covers:gclient-daemon-resilience:E1:E1.2
  tdd: true
  source_section: E1
  implementation_domain: backend
```
