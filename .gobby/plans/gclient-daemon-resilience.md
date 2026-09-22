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

## Decision Record
`kind: framing`

Josh, 2026-09-20:

1. A held direct native pane keeps typing through a daemon disconnect or restart.
   gclient keeps Held on direct-granted panes, shows the lease as unconfirmed, and
   re-takes on reconnect. Proxied, tmux and web panes still drop to observe.
2. Daemon-side scope is all three: terminal handlers' Postgres work moves onto the
   DatabaseExecutor, websocket messages dispatch concurrently per connection with
   per-attachment ordering, and HostClient round trips get a bounded deadline.
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
8. Josh, 2026-09-21: add no daemon or client-loop watchdog. D2 keeps ordered
   concurrent lanes, while D3/D3b use bounded operations and test-only barriers for
   diagnosis. **Restraint rung 1:** watchdogs do not need to exist to meet the
   liveness or diagnostic requirements and would add latency/noise.

Coordinator decision (gobby#14018, 2026-09-20): the near-ceiling gclient files are
decomposed first in one behaviour-neutral refactor leaf (R1) so that every later
deliverable edits a file with headroom; the planning draft's per-leaf splits fold
into it.

## Constraints
`kind: framing`

Scheduling. This epic runs after tasks #22635 and #22637 (gclient key encoding and
scrollback copy, both also touching gterm), #22617 (the fourteen dirty gclient
files in the task-22616 worktree) and epic #22627 (post-reboot daemon terminal
handler fixes) have landed, because P1-P3 rewrite `live_loop.rs`, `live.rs`,
`live_attach.rs`, `control.rs`, `actions.rs` and the gterm frame/grant seam, and P4
touches `terminal_ws.py`. The coordinator gobby#14018 holds the epic until then.

Shared clean-cutover gate. No implementation leaf promotes a live binary by itself.
After all P1-P4 commits and focused validation are green, announce the cutover with
one `global` `gobby-agents:send_message` and wait until no spawned worker or close
validator is live. Any change to the gcore runtime-config carrier counts as an
input change to the coherent `gcode`/`gdaemon`/`ghook` trio. Rebuild that complete
trio and promote it only through
`src/gobby/install/bin_set_coherence.py::promote_workspace_binary_set`; then build
gclient with `cargo build --release -p gobby-client` and promote it separately
through
`src/gobby/cli/install_setup_gclient.py::install_gclient_from_submodule`, because
`promote_workspace_binary_set` rejects gclient (memory `8303e661`). Restart the
Python daemon from the main checkout and restart each validation gclient so it execs
the new inode. P3 changes gterm: build it with `cargo build --release -p
gobby-terminal --features vt-engine --bin gterm`, promote it through
`src/gobby/cli/install_setup_gterm.py::install_gterm_from_submodule`, and restart it
before relaunching the validation gclient. Read the installed identity stamp and
hashes from `~/.gobby/bin/`, never from `target/release/`. **Restraint rung 2:**
reuse the repository's native promotion functions and batch one cutover; add no
installer or promotion path.

Detach-deadline escalation is owned and implemented by task #22677; this plan does
not create a second deliverable or manifest leaf for it. Its exact code surfaces are
`crates/gclient/src/app/live_attach.rs::recover_proxy_source`,
`crates/gclient/src/app/attach.rs::Pane::take_expired_detach_generation`,
`crates/gclient/src/app/mod.rs::Workspace::submit_expired_detaches` and
`crates/gclient/src/daemon/live.rs::LiveDaemon::reconnect`, with gclient loop tests
covering each deadline/error exit. The task clears `fallback_in_flight`, restores
`direct_available` on successful recovery or the next attach pass, hands the pane
back to the attach job with backoff, and reconnects only for real transport loss.
Its test matrix covers a late healthy-socket reply, reply error, nested result
error, success and real transport loss. **Restraint rung 2:** reuse #22677's owned
repair and acceptance instead of duplicating it in this plan.

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
- `crates/gclient/src/app/live.rs::*` — scope-reason: the roster, sidebar and event-handling impl blocks move out; only the control-request and reconcile core stays
- `crates/gclient/src/app/live_roster.rs`
- `crates/gclient/src/app/live_sidebar.rs`
- `crates/gclient/src/app/live_events.rs`
- `crates/gclient/src/app/mod.rs::*` — scope-reason: `observe_daemon_disconnect`, `retire_indeterminate_control` and `submit_expired_detaches` move out and four `mod` lines come in
- `crates/gclient/src/app/disconnect.rs`
- `crates/gclient/src/app/live_loop/actions.rs::*` — scope-reason: `close_slot`, `close_live_pane`, `close_live_tab`, `group_target`, `spawn_live_terminal`, `spawn_live_shell` and `terminate_live_terminal` move out
- `crates/gclient/src/app/live_loop/lifecycle.rs`
- `crates/gclient/src/app/live_loop.rs::*` — scope-reason: gains the `mod lifecycle;` declaration only
- `crates/gclient/src/app/live_loop/projects.rs::*` — scope-reason: `close_live_terminal` re-imports `close_live_pane` from the lifecycle module
- `crates/gclient/src/daemon/live.rs::*` — scope-reason: the `impl Daemon for LiveDaemon` block moves out; the connection, waiter and reconnect machinery stays
- `crates/gclient/src/daemon/live_api.rs`
- `crates/gclient/src/daemon/mod.rs::*` — scope-reason: gains the `mod live_api;` declaration only

Consumers unchanged:
- `crates/gclient/tests/client_loop.rs` — no-edit-reason: exercises the moved functions only through the public loop API and by test name.
- `crates/gclient/tests/reconciliation.rs` — no-edit-reason: calls `fetch_roster` and `reconnect_daemon_ws` as `Workspace` methods, unchanged.
- `crates/gclient/tests/workspace.rs` — no-edit-reason: calls `fetch_roster` as a `Workspace` method, unchanged.
- `crates/gclient/tests/ws_golden.rs` — no-edit-reason: calls `reconnect_daemon_ws` as a `Workspace` method, unchanged.

Pure moves, no behaviour change, so that every later deliverable edits a file with
headroom under the 1,000-line ceiling. Measured on the current checkout (Rust
counts stop at the first `#[cfg(test)]`): `crates/gclient/src/app/live.rs` 986,
`crates/gclient/src/app/mod.rs` 927, `crates/gclient/src/app/live_loop/actions.rs`
979, `crates/gclient/src/daemon/live.rs` 985, `crates/gclient/src/daemon/mod.rs`
987, `crates/gclient/src/app/live_loop.rs` 868.

Move out of `crates/gclient/src/app/live.rs`: the roster impl (`ensure_live_pane`,
`install_live_row`, `open_live_terminal`, `install_live_rows`, `fetch_roster`,
`fetch_attention`, `install_attention`, `RosterSnapshot`, `row_command`,
`row_address`, `row_is_external`, `row_has_direct_locator`) into new
`crates/gclient/src/app/live_roster.rs`; the sidebar impl (`fetch_sidebar_rows`,
`checked_out_projects`, `request_focused_sessions`, `start_sidebar_refetch`,
`apply_sidebar_fetch`, `flush_sidebar_refetches`, `request_git_refresh_if_due`,
`request_roster_refresh_if_due`, `SidebarFetch`, `SidebarFetchFuture`,
`SidebarRequest`, `SidebarRequest::run`) into new
`crates/gclient/src/app/live_sidebar.rs`; the event impl (`drain_live_events`,
`drain_receiver`, `apply_live_event`, `note_sidebar_message`, `accept_lifecycle`,
`advance_lifecycle`, `is_cursor_error`, `optional`) into new
`crates/gclient/src/app/live_events.rs`. `live.rs` keeps `live`, `daemon`,
`select_project`, `set_frame_delivery`, `project_id`, `daemon_ready`,
`daemon_error`, `request_control`, `awaiting_control`, `start_control_request`,
`ControlOutcome`, `reconcile_subscribe_first` and `reconnect_daemon_ws`.

Move out of `crates/gclient/src/app/mod.rs`: the generic disconnect and
control-retirement methods `observe_daemon_disconnect`,
`retire_indeterminate_control` and `submit_expired_detaches` into new
`crates/gclient/src/app/disconnect.rs` as an `impl<D: Daemon> Workspace<D>` block;
`mod.rs` gains the `mod` lines for the four new app modules and nothing else.

Move out of `crates/gclient/src/app/live_loop/actions.rs`: the terminal lifecycle
actions `close_slot`, `close_live_pane`, `close_live_tab`, `group_target`,
`spawn_live_terminal`, `spawn_live_shell` and `terminate_live_terminal` into new
`crates/gclient/src/app/live_loop/lifecycle.rs`; `crates/gclient/src/app/live_loop.rs`
gains the `mod lifecycle;` line and `crates/gclient/src/app/live_loop/projects.rs`
imports `close_live_pane` from `super::lifecycle` in `close_live_terminal`.

Move out of `crates/gclient/src/daemon/live.rs`: the `impl Daemon for LiveDaemon`
block (`list_terminals` through `workspace_op`) into new
`crates/gclient/src/daemon/live_api.rs`; `crates/gclient/src/daemon/mod.rs` gains
the `mod live_api;` line only.

**Granularity:** one refactor leaf rather than one per file: every move is
behaviour-neutral, the crate must compile as a whole, and one green
`cargo nextest run -p gobby-client` run is the whole verification. Splitting it
would create per-file chores with no independent outcome.

**Cutover:** R1 changes gclient and does not promote independently; after all
leaves pass, it participates in the single shared clean-cutover gate in
Constraints.

**Research context:** Observed: `live.rs` holds `impl Workspace<LiveDaemon>` blocks
whose functions gcode indexes as bare names (`fetch_roster`, `apply_live_event`);
`mod.rs` holds `impl Workspace` (scripted harness, lines ~191-720) and
`impl<D: Daemon> Workspace<D>` (~723-760); `daemon/live.rs` holds `LiveState`,
`LiveInner`, `LiveDaemon` and the trait impl at ~601-909; `daemon/mod.rs` declares
`mod live; mod live_reader; mod live_workspace; mod projects; mod rest; mod
workspace; mod ws;`; `live_loop.rs` declares `mod actions; mod control; pub(super)
mod menu; …; mod workspace_actions;`. Approach: `git mv`-style cut and paste with
`pub(super)`/`pub(crate)` visibility adjusted; no signature changes. Rejected:
splitting inside each later deliverable (the draft's A4 pattern), because four
later deliverables touch `live.rs` and each would carry its own split. Planned
verification: `cargo fmt -p gobby-client`, `cargo clippy -p gobby-client
--all-targets`, `cargo nextest run -p gobby-client`, then `wc -l` of every listed
file under 850 except `daemon/mod.rs` and `mod.rs`, which only shrink; screen goldens
unchanged.

**Acceptance:**

- R1.1 - The roster, sidebar and event impls live in their own modules and `live.rs`
  keeps only the control and reconcile core. file: `crates/gclient/src/app/live_roster.rs`.
- R1.2 - The sidebar refresh functions live in their own module. file:
  `crates/gclient/src/app/live_sidebar.rs`.
- R1.3 - The daemon event application lives in its own module. file:
  `crates/gclient/src/app/live_events.rs`.
- R1.4 - The disconnect and control-retirement methods live in their own module.
  file: `crates/gclient/src/app/disconnect.rs`.
- R1.5 - The terminal lifecycle actions live in their own module. file:
  `crates/gclient/src/app/live_loop/lifecycle.rs`.
- R1.6 - The `Daemon` trait impl for `LiveDaemon` lives in its own module. file:
  `crates/gclient/src/daemon/live_api.rs`.
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

The baseline matrix includes the Q7b case: a `created` event starts a new
terminal's `terminal_attach`, its reply is held for three seconds of paused Tokio
time, and already attached direct panes continue consuming frames and input for the
whole interval. This test proves the current inline path stalls before A3 and turns
green when attach is issued as a job. It records loop progress directly; it adds no
phase label or watchdog. **Restraint rung 2:** reuse `hold_ws` and the shared
liveness probe instead of adding a runtime monitor.

**Research context:** Observed: `crates/gclient/tests/mock_daemon/mod.rs`
(`MockDaemon::start_at`, `enqueue`, `enqueue_with_events`,
`finalize_next_proxy_attach_before_reply`, `pause_websocket_reads`, `requests`,
`websocket_handshakes`; `serve_websocket` at ~615, `websocket_reply` at ~734) already
supports queued replies and paused reads but has no per-kind hold that keeps the
request pending while the loop runs. `crates/gclient/tests/client_loop.rs` is
10,903 lines; no new tests go there. Reusable: `ScriptedFrameSource` and
`ProxyFrameSource` from `crates/gclient/src/frame_source.rs` for the probe pane.
Approach: a `Notify` per hold, stored on `MockState`, consulted by
`serve_websocket` before writing the reply. Planned verification: `cargo nextest run
-p gobby-client --test loop_liveness`.

**Acceptance:**

- A0.1 - `hold_ws` keeps a matching websocket request pending until released and
  `replies` counts the released reply. file: `crates/gclient/tests/mock_daemon/mod.rs`.
- A0.2 - The liveness probe is a reusable helper and its baseline test passes.
  test: `crates/gclient/tests/loop_liveness.rs::held_websocket_request_stays_pending_until_released`.
- A0.3 - A newly created terminal whose `terminal_attach` reply takes three
  seconds does not stop frames, ticks or input on existing direct panes. test:
  `crates/gclient/tests/loop_liveness.rs::a_slow_new_terminal_attach_never_stalls_streaming_direct_panes`.

### A1 Job plumbing, focus hints and geometry [category: code] (depends: R1, A0)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live_loop.rs::*` — scope-reason: `run_live_loop` gains the outcome branch and loses its post-select awaits; `resize_live_workspace` becomes a sync stage
- `crates/gclient/src/app/live_loop/jobs.rs`
- `crates/gclient/src/app/live_loop/jobs/tests.rs`
- `crates/gclient/src/app/live_loop/jobs_apply.rs`
- `crates/gclient/src/app/live_loop/workspace_actions.rs::*` — scope-reason: `send_focus_hints_if_changed` becomes a coalesced job and `issue_workspace_op` is added beside `send_workspace_op`
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

`crates/gclient/src/app/live_loop.rs` is at 868 production lines: the channel types,
ledger, coalescer and `spawn_job` move into `crates/gclient/src/app/live_loop/jobs.rs`
and the apply match into `crates/gclient/src/app/live_loop/jobs_apply.rs` instead of
growing `live_loop.rs`; `run_live_loop` gains the branch and loses its post-select
awaits.

`send_focus_hints_if_changed` becomes a coalesced job with key `FocusHints`, memo
updated on success only; it issues through a new sync `issue_workspace_op(workspace,
ledger, op, OpIntent)` in `workspace_actions.rs` beside the existing
`send_workspace_op`, which A2 retires. Geometry: `Workspace::stage_geometry` beside
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
(`crates/gclient/src/app/live_loop.rs`, ~176-481) is the biased select; the
post-select code awaits `send_focus_hints_if_changed`
(`crates/gclient/src/app/live_loop/workspace_actions.rs`, which awaits
`send_workspace_op` → `LiveDaemon::workspace_op`) and `resize_live_workspace`
(`live_loop.rs` ~809-854, awaits per-pane resize) every iteration. `propagate_geometry`
(`crates/gclient/src/app/run_loop.rs` ~392-445) is the scripted-loop geometry path
and stays. `UnixSocketFrameSource::send_input` (`crates/gclient/src/frame_source.rs`
~662) refuses anything but `BindAttachment | Input | Paste` with
`FrameError::Protocol`. `start_control_request` (`crates/gclient/src/app/live.rs`
~425-471) is the pattern being generalised: spawn, channel, apply in branch.
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

### A2 Typed input and control path [category: code] (depends: A1)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live_loop/control.rs::*` — scope-reason: every daemon-awaiting function becomes a sync issue or a job outcome apply
- `crates/gclient/src/app/pane.rs::*` — scope-reason: add the bounded writer, queue accounting and release state across Pane construction and input helpers
- `crates/gclient/src/app/live.rs::*` — scope-reason: the control-request machinery moves out to the new control module
- `crates/gclient/src/app/live_control.rs`
- `crates/gclient/src/app/mod.rs::*` — scope-reason: gains the `mod live_control;` declaration only
- `crates/gclient/src/app/live_loop/workspace_actions.rs::*` — scope-reason: every op sender becomes a sync issue with the refusal toast moved to apply; `send_workspace_op` is deleted
- `crates/gclient/src/app/attention.rs::submit_response`
- `crates/gclient/src/app/live_loop/actions.rs::*` — scope-reason: the remaining callers of the control, scroll and op senders drop their awaits
- `crates/gclient/src/app/live_loop/lifecycle.rs`
- `crates/gclient/src/app/live_loop/projects.rs::*` — scope-reason: callers of the release and op senders drop their awaits
- `crates/gclient/src/app/live_loop/jobs.rs`
- `crates/gclient/src/app/live_loop/jobs_apply.rs`
- `crates/gclient/tests/loop_liveness.rs`

Consumers unchanged:
- `crates/gclient/src/app/scripted_input.rs` — no-edit-reason: mentions `send_live_write` only in a doc comment; the scripted path has its own writer.
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
clears the one-shot queue-full status. Release-before-take on a focus move:
`start_control_request` drains every `release_pending` pane into the spawned control
task ahead of the take. **Restraint rung 2:** reuse Tokio's bounded channel, the
existing payload ceiling and the existing backpressure surface; add no queue
framework or retry policy.

`crates/gclient/src/app/live.rs` is at 986 lines and `crates/gclient/src/app/mod.rs`
at 927: the control-request machinery (`request_control`, `awaiting_control`,
`start_control_request`, `ControlOutcome`, `PendingControl`) is a move out of
`live.rs` and `mod.rs` into new `crates/gclient/src/app/live_control.rs`, where the
release drain is added; `mod.rs` gains the `mod live_control;` line only.
`crates/gclient/src/app/live_loop/actions.rs` is at 979 lines: R1's move of the
lifecycle actions into `crates/gclient/src/app/live_loop/lifecycle.rs` leaves this
deliverable only dropping the awaits that remain in `actions.rs`.

The small ops in `workspace_actions.rs` (`move_daemon_tab`, `resize_daemon_split`,
`swap_live_slots`, `rename_daemon_target`, `close_daemon_pane`, `close_daemon_tab`,
`place_live_terminal`, `apply_daemon_menu_action`) issue `WorkspaceOp` jobs; the
refusal toast moves to the `WorkspaceOp` outcome apply in `jobs_apply.rs`.
`submit_response` in `attention.rs` issues a `Responded` job.

**Granularity:** nine production files in one leaf because the sync conversion of
`control.rs` changes the signature of every caller and the crate must compile in
one commit; the callers cannot be separate leaves.

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
- A2.5 - The control-request machinery lives in its own module and drains pending
  releases ahead of a take. file: `crates/gclient/src/app/live_control.rs`.
- A2.6 - `send_workspace_op` no longer exists; every op sender is sync and the
  refusal toast is raised at apply. file:
  `crates/gclient/src/app/live_loop/workspace_actions.rs`.
- A2.7 - Holding an attention-response request never stalls rendering; the
  response outcome applies exactly once after release. test:
  `crates/gclient/tests/loop_liveness.rs::a_held_attention_response_never_stalls_frames_or_applies_twice`.
- A2.8 - Flooding a pane while `terminal_input` is held keeps its queue at or
  below 256 messages and 1 MiB, rejects only the newest over-cap messages with
  `Backpressure`, preserves accepted FIFO order after release, and leaves another
  direct pane's input plus rendering live. test:
  `crates/gclient/tests/loop_liveness.rs::a_held_terminal_input_flood_is_bounded_ordered_and_nonblocking`.

### A3 Attach and recovery as jobs [category: code] (depends: A2)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live_attach.rs::*` — scope-reason: the handshake bodies move into the job module and the remaining functions become sync plan/apply state functions
- `crates/gclient/src/app/live_attach/handshake.rs`
- `crates/gclient/src/app/live_loop.rs::*` — scope-reason: the frame branch shrinks to classify plus issue; `FrameRecovery`, `deferred_input` and the inner select are deleted
- `crates/gclient/src/app/attach.rs::Pane::begin_attaching`
- `crates/gclient/src/app/live_loop/jobs.rs`
- `crates/gclient/src/app/live_loop/jobs_apply.rs`
- `crates/gclient/tests/loop_liveness.rs`

Consumers unchanged:
- `crates/gclient/tests/frame_delivery.rs` — no-edit-reason: exercises the inline attach path, unchanged.
- `crates/gclient/tests/reconciliation.rs` — no-edit-reason: exercises the inline attach path, unchanged.
- `crates/gclient/tests/workspace.rs` — no-edit-reason: calls `recv_live_frame`, whose signature is unchanged.

New `crates/gclient/src/app/live_attach/handshake.rs` holds `attach_job(daemon,
terminal_id, request_id, AttachPlan) -> AttachResult` (today's
`request_direct_source` + `connect_direct_reply` + proxy fallback, minus `&self`;
returns a concrete `UnixSocketFrameSource` or `ProxyFrameSource`) and `recover_job`
(detach racing `AttachmentFinalized` under the 2 s deadline, then attach, then
viewport, all in one task). `live_attach.rs` keeps the sync state functions and
gains `plan_attaches(now) -> Vec<AttachIssue>` and `apply_attach_result(..)` with the
gate: pane exists, `Attaching{request_id, generation}` matches, attachment not
tombstoned; a superseded result that carries a source is dropped and its attachment
detached so the daemon does not hold a dangling attachment. `Pane::begin_attaching`
sets `status_message = "attaching (direct|proxy)…"`. C3's direct-first retry later
lands inside `recover_job`.

A3 starts from #22677's repaired detach state machine and moves it without changing
its ownership contract: deadline or reply errors return the pane to the attach job
with backoff, restore `fallback_in_flight`/`direct_available` as #22677 specifies,
and never request a reconnect for a healthy socket. #22677 remains the only task
that implements and validates those transitions.

Frame recovery also becomes observable. Each attach/recovery issue records its
start instant. The issue side emits one structured `tracing::warn!` when a real
frame error starts recovery, including `RetireReason::Lag`; the handshake emits a
WARN when the direct connect/attach phase times out; and the apply side emits one
WARN named `frame_recovery_gave_up` when detach reaches its deadline, attach is
refused or errors, or a stale/tombstoned result is discarded without installing a
replacement source. These records carry `pane_id`, `terminal_id`, old
`attachment_id` when present, transport, daemon generation, recovery stage, error
class and `elapsed_ms`. `Refused` and `Backpressure` input outcomes are not frame
failures and remain excluded. A successful recovery emits one structured
completion with elapsed time and the replacement transport so the attempt closes
in `gclient.log`. The job/apply seam owns these records; there is no new logger,
telemetry transport, retry counter or watchdog. **Restraint rung 2:** reuse
gclient's tracing subscriber and the A3 outcome.

`crates/gclient/src/app/live_loop.rs` is at 868 production lines: the frame branch
shrinks to classify plus issue, and `FrameRecovery`, `deferred_input` and the inner
input-only select are deleted; the recovery body is a move out of `live_loop.rs`
and `live_attach.rs` into `crates/gclient/src/app/live_attach/handshake.rs`. The
render tick calls `plan_attaches` instead of `attach_ready_panes().await`; an inline
`attach_ready_panes()` (plan, await job, apply) stays for `reconcile_subscribe_first`
and the existing tests.

**Granularity:** A3 owns one attach/recovery state machine across six production
files. The independently issuable `open_unresolved_terminals` path is split into
A3b, so it can be implemented, tested and closed without coupling its event trigger
to frame recovery. The handshake and its logging stay together because the same
job/apply seam owns start, terminal outcome and stale-result disposal.

**Cutover:** A3 changes gclient and does not promote independently; after all
leaves pass, it participates in the single shared clean-cutover gate in
Constraints.

**Research context:** Observed: `attach_ready_panes` (`live_attach.rs` ~23-65)
awaits `request_direct_source` then `begin_live_proxy_attach` per pane on the render
tick; `recover_live_frame_error` (~174-203) routes every non-finalized error to
`recover_proxy_source` (~205-284), which awaits detach and attach while
`run_live_loop`'s `FrameRecovery` (`live_loop.rs` ~483-486) polls only input.
The detach-deadline branch in `recover_proxy_source` returns `Ok(())` without a log,
and the defer/refusal branches in `begin_live_proxy_attach` (~344-425) update pane
state without recording the recovery outcome; those observed ranges were verified
with gcode before this amendment. The 2026-09-21 freeze therefore left no frame-side
evidence. A3 removes the await from the loop and adds the start/give-up/completion
records at the job/apply boundary.
`AttachState` and `Pane::begin_attaching`/`install_attachment`/`begin_detaching`/
`retire_attachment` live in `crates/gclient/src/app/attach.rs`; `DETACH_DEADLINE` is
2 s. Rejected: awaiting the attach inside the branch with a timeout (still parks
the loop). Planned verification: `cargo nextest run -p gobby-client --test
loop_liveness --test frame_delivery --test reconciliation --test frame_source_live`.

**Acceptance:**

- A3.1 - `attach_job` and `recover_job` run the whole handshake in one spawned task
  and return a concrete source. file: `crates/gclient/src/app/live_attach/handshake.rs`.
- A3.2 - A held attach on one pane keeps the other pane rendering. test:
  `crates/gclient/tests/loop_liveness.rs::a_held_attach_on_one_pane_keeps_the_other_pane_rendering`.
- A3.3 - An attach outcome from a previous generation is dropped and its attachment
  detached. test:
  `crates/gclient/tests/loop_liveness.rs::an_attach_outcome_from_a_previous_generation_is_dropped_and_detached`.
- A3.4 - Recovery detaches then attaches while input still routes. test:
  `crates/gclient/tests/loop_liveness.rs::recovery_detaches_then_attaches_while_input_still_routes`.
- A3.5 - `FrameRecovery` and the inner input-only select no longer exist. symbol:
  `run_live_loop`.
- A3.6 - A frame error and every recovery give-up leave one structured record with
  pane, terminal, attachment, generation, stage, error class and `elapsed_ms`, while
  a successful replacement closes the attempt. The same test covers a
  direct-handshake timeout and a `RetireReason::Lag` retirement. test:
  `crates/gclient/tests/loop_liveness.rs::frame_errors_and_recovery_giveups_are_logged_with_pane_context`.

### A3b Open unresolved terminals as jobs [category: code] (depends: A3)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live_workspace.rs::*` — scope-reason: retains the inline helper and adds a sync planner for missing-terminal open jobs
- `crates/gclient/src/app/live_events.rs`
- `crates/gclient/src/app/live_loop.rs::*` — scope-reason: `handle_live_event` plans missing opens and routes `Opened` outcomes
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

`crates/gclient/src/app/live_loop.rs` is at 879 production lines after the current
checkout's prior edits: move unresolved-job issuance and outcome routing into new
`crates/gclient/src/app/live_loop/unresolved.rs`; `live_loop.rs` gains only the
module declaration and bounded calls.

**Cutover:** A3b changes gclient and does not promote independently; after all
leaves pass, it participates in the single shared clean-cutover gate in
Constraints.

**Research context:** Observed: `open_unresolved_terminals`
(`crates/gclient/src/app/live_workspace.rs` ~79-100) collects missing terminals and
awaits `open_live_terminal` serially. Its two current callers are `drain_receiver`
and the `DaemonEvent::Workspace` arm in `apply_live_event` (both in
`crates/gclient/src/app/live.rs` before R1, then `live_events.rs`). The former is an
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

### A4 Actions and event-driven refetches as jobs [category: code] (depends: A3b)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live_roster.rs`
- `crates/gclient/src/app/live_events.rs`
- `crates/gclient/src/app/live_sidebar.rs`
- `crates/gclient/src/app/live_loop.rs::*` — scope-reason: `handle_live_event` drains refetch flags and `run_live_loop` routes the new outcomes
- `crates/gclient/src/app/live_loop/lifecycle.rs`
- `crates/gclient/src/app/live_loop/projects.rs::*` — scope-reason: `focus_project` issues a scoped roster job and `restore_focused` runs when it lands
- `crates/gclient/src/app/live_loop/orphans.rs::*` — scope-reason: `fetch_orphans` becomes a job and the destroy dialog's kills fan out with a `DestroySummary` reducer
- `crates/gclient/src/app/live_loop/jobs.rs`
- `crates/gclient/src/app/live_loop/jobs_apply.rs`
- `crates/gclient/tests/loop_liveness.rs`

Consumers unchanged:
- `crates/gclient/tests/client_loop.rs` — no-edit-reason: calls `fetch_roster` and `drain_live_events` inline, unchanged.
- `crates/gclient/tests/reconciliation.rs` — no-edit-reason: calls `fetch_roster` inline, unchanged.
- `crates/gclient/tests/workspace.rs` — no-edit-reason: calls `fetch_roster` inline, unchanged.
- `crates/gclient/tests/daemon_live.rs` — no-edit-reason: calls `drain_live_events`, unchanged.

`apply_live_event` (in `live_events.rs` after R1) becomes sync and sets
`pending_refetch { roster, attention, sidebar }` flags that the loop drains into
coalesced `Roster` and `Attention` jobs (`Lagged` sets all three); `handle_live_event`
in `live_loop.rs` does the drain. `fetch_roster` gains a sync `issue_roster_fetch`
sibling in `live_roster.rs` used by the loop paths, while the inline async
`fetch_roster` stays for `reconcile_subscribe_first` and the tests. Spawn shell
(`lifecycle.rs`): the task does `terminal_create` only; the `Spawned` outcome
registers the pending spawn, computes the placement op from current `Chrome` and
issues it plus a `Roster` job. `TabClose` after every kill: a `CloseTabPlan` join in
the ledger. `focus_project` selects immediately and issues a scoped `Roster`;
`restore_focused` runs when it lands if the project is still selected. Orphans:
`fetch_orphans` job, kill fan-out with a `DestroySummary` reducer.

Every sidebar/REST job records its start instant. The existing `optional` DEBUG
becomes one WARN per failed REST component with operation, project, error class and
`elapsed_ms`; the aggregate outcome still applies any successful rows. Both the
websocket receiver's `RecvError::Lagged(skipped)` branch and a decoded
`DaemonEvent::Lagged` emit one WARN with source, skipped count when available,
generation and `elapsed_ms` before scheduling refetch jobs. **Restraint rung 2:**
reuse the job outcome and existing tracing subscriber; add no lag watchdog or
metrics pipeline.

`crates/gclient/src/app/live_loop.rs` is at 868 production lines: the refetch drain
and the new outcome arms are a move into `crates/gclient/src/app/live_loop/jobs_apply.rs`
rather than growth in `live_loop.rs`; `handle_live_event` only sets and drains flags.

**Granularity:** eight production files because the spawn, close, roster and orphan
paths all consume the `Spawned`/`Terminated`/`Roster` outcome family introduced
here; each test in the acceptance list exercises one path end to end.

**Cutover:** A4 changes gclient and does not promote independently; after all
leaves pass, it participates in the single shared clean-cutover gate in
Constraints.

**Research context:** Observed: `apply_live_event` (`live.rs` ~550-682 before R1)
awaits `fetch_roster` on `Lagged` and on roster-changing events; `spawn_live_shell`
(`actions.rs` ~927-953 before R1) awaits `terminal_create`, placement op, roster and
focus serially; `close_live_tab` (~764-805 before R1) kills every pane then sends
`TabClose`; `focus_project` (`projects.rs` ~34-55) awaits a scoped roster;
`fetch_orphans` (`orphans.rs` ~90-113) and the destroy dialog kill orphans one at a
time. `GIT_REFRESH_INTERVAL`/`ROSTER_REFRESH_INTERVAL` polls live in
`crates/gclient/src/app/sidebar_model.rs`; their request functions live in
`live_sidebar.rs` after R1 and B3 gates them. Planned verification: `cargo nextest
run -p gobby-client --test loop_liveness`. Incident mapping: split-right runs the
create/placement/roster chain owned here, while its focus change uses A2's
release/take job and a resulting attach uses A3. Holding any one reply may stall
that action's outcome, but no step awaits inside `run_live_loop`. **Restraint rung
2:** extend the shared A1 job ledger and existing mock-daemon hold points; add no
second action queue.

**Acceptance:**

- A4.1 - A held `terminal_create` keeps the window interactive and places the pane
  after the create lands. test:
  `crates/gclient/tests/loop_liveness.rs::a_held_terminal_create_keeps_the_window_interactive_and_places_after_create`.
- A4.2 - Closing a tab issues `TabClose` only after every kill settles. test:
  `crates/gclient/tests/loop_liveness.rs::closing_a_tab_issues_tab_close_only_after_every_kill_settles`.
- A4.3 - A roster for a project no longer focused is dropped. test:
  `crates/gclient/tests/loop_liveness.rs::a_roster_for_a_project_no_longer_focused_is_dropped`.
- A4.4 - Two new tabs before the first create lands place both. test:
  `crates/gclient/tests/loop_liveness.rs::two_new_tabs_before_the_first_create_lands_place_both`.
- A4.5 - `apply_live_event` is sync and sets refetch flags instead of awaiting.
  file: `crates/gclient/src/app/live_events.rs`.
- A4.6 - Splitting right while the old connection's release/take or terminal-create
reply is held keeps direct input, frame ingest and rendering live; the placement is
applied only after its own job settles. test:
`crates/gclient/tests/loop_liveness.rs::split_right_with_a_held_control_or_create_reply_keeps_the_window_live`.
- A4.7 - Holding orphan inventory leaves the dialog and window live; after release,
  orphan kills fan out concurrently and `DestroySummary` preserves one result per
  requested orphan regardless of completion order. test:
  `crates/gclient/tests/loop_liveness.rs::held_orphan_fetch_and_kill_fanout_are_nonblocking_and_complete`.
- A4.8 - Failed REST components and both `Lagged` entry paths emit WARN records
  with operation/source, generation, error class or skipped count, and
  `elapsed_ms`, while successful partial rows still apply. test:
  `crates/gclient/tests/loop_liveness.rs::rest_failures_and_lagged_events_warn_with_elapsed_time`.

### A5 Launch and reconnect reconcile as a job [category: code] (depends: A4)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live_reconcile.rs`
- `crates/gclient/src/app/live.rs::*` — scope-reason: `reconnect_daemon_ws` delegates to the reconcile job
- `crates/gclient/src/app/live_loop.rs::*` — scope-reason: `run_live_loop`, `handle_reconnect_outcome` and `reconcile_ready` issue and apply the reconcile job
- `crates/gclient/src/app/mod.rs::*` — scope-reason: gains the `mod live_reconcile;` declaration only
- `crates/gclient/tests/loop_liveness.rs`

Consumers unchanged:
- `crates/gclient/tests/client_loop.rs` — no-edit-reason: calls `reconnect_daemon_ws` inline; it keeps its signature and delegates to the job.
- `crates/gclient/tests/reconciliation.rs` — no-edit-reason: calls `reconnect_daemon_ws` inline, unchanged signature.
- `crates/gclient/tests/ws_golden.rs` — no-edit-reason: calls `reconnect_daemon_ws` inline, unchanged signature.

New `crates/gclient/src/app/live_reconcile.rs`: `reconcile_job` does
`attach_workspace → list_terminals pages → attention_roster → projects +
SidebarRequest::run` and returns a `ReconcileFetch` including the event receiver;
the apply installs the rows, drains the receiver (sync after A4), completes the
handshake, restores focus and plans attaches; an error applies
`observe_daemon_disconnect` plus `handshake_failed`. `reconcile_subscribe_first`
stays inline for its direct callers; `reconnect_daemon_ws` keeps its signature and
delegates. `ReconnectSupervisor` is unchanged.

The job records one WARN when reconnect/reconcile starts and one terminal WARN when
it finishes, carrying reason, old/new generation, outcome and `elapsed_ms`; joined
callers do not duplicate either record. A successful finish remains WARN in this
diagnostic release so every freeze-relevant reconnect bracket is present in
`gclient.log`. **Restraint rung 2:** log at the single reconcile job owner rather
than adding per-caller logging or a watchdog.

`crates/gclient/src/app/live.rs` (986 lines), `crates/gclient/src/app/live_loop.rs`
(868) and `crates/gclient/src/app/mod.rs` (927) are near the ceiling: the reconcile
body is a move out of `live.rs` and `live_loop.rs` into
`crates/gclient/src/app/live_reconcile.rs`; `mod.rs` gains the module line only.

**Cutover:** A5 changes gclient and does not promote independently; after all
leaves pass, it participates in the single shared clean-cutover gate in
Constraints.

**Research context:** Observed: `reconnect_daemon_ws` (`live.rs` ~735-743) clears
control on every pane then awaits `reconcile_subscribe_first`;
`handle_reconnect_outcome` (`live_loop.rs` ~621-656) and `reconcile_ready` (~508-511)
await the reconcile on the loop; `ReconnectSupervisor`
(`crates/gclient/src/app/run_loop.rs` ~500-639) schedules attempts. Planned
verification: `cargo nextest run -p gobby-client --test loop_liveness --test
reconciliation --test ws_golden`.

**Acceptance:**

- A5.1 - Launch against a slow daemon draws before the first roster lands. test:
  `crates/gclient/tests/loop_liveness.rs::launch_against_a_slow_daemon_draws_before_the_first_roster_lands`.
- A5.2 - A reconcile from a stale generation is dropped. test:
  `crates/gclient/tests/loop_liveness.rs::a_reconcile_from_a_stale_generation_is_dropped`.
- A5.3 - The reconcile runs as one job and applies through the staleness gate.
  file: `crates/gclient/src/app/live_reconcile.rs`.
- A5.4 - A bounded final audit enumerates launch/reconcile, control, input,
  daemon-event, frame-recovery, sidebar, resize/tick attach, focus-hint, geometry,
  attention, unresolved-open, roster/orphan and lifecycle-action paths; every
  daemon-touching `run_live_loop` branch or post-select helper maps to a held-request
  case that advances frames, ticks and unaffected direct input. test:
  `crates/gclient/tests/loop_liveness.rs::every_run_live_loop_daemon_path_is_issued_without_awaiting`.
- A5.5 - Each reconnect/reconcile attempt emits exactly one start and one finish
  WARN with reason, generations, outcome and `elapsed_ms`, including success,
  timeout and transport-loss cases. test:
  `crates/gclient/tests/loop_liveness.rs::reconnect_attempts_warn_once_at_start_and_finish_with_reason`.

## P2: daemon health and load shedding
`kind: framing`

**Goal:** the client observes daemon responsiveness from the age of its own
in-flight requests, says so on the status line, keeps a half-open socket from
going unnoticed, and stops polling while the daemon is slow. No new protocol
beyond a `request_id` on `ping`. Every P2 production deliverable changes the
gclient binary and therefore uses the shared clean-cutover gate after merge; none
promotes independently.

### B1 Daemon health from in-flight age [category: code] (depends: A5)
`kind: deliverable`

Targets:
- `crates/gclient/src/daemon/live.rs::*` — scope-reason: `LiveState` waiters gain `issued_at`, set by `register_waiter`, and `oldest_inflight_age` reads them
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
shows `│ Daemon slow (2.3s)` in the palette's warning colour beside the existing
red `Daemon unreachable.`; this is UI text, so the executor loads the `impeccable`
skill and `.impeccable.md` before touching it. The guide's status-line section names
the new state.

`crates/gclient/src/daemon/live.rs` is at 985 lines, `crates/gclient/src/app/live_loop.rs`
at 868 and `crates/gclient/src/ui/chrome.rs` at 886: the enum, threshold and
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
`render_status_line` (`crates/gclient/src/ui/status.rs` ~341-406) pushes
`" │ Daemon unreachable."` in `p.red` when `!ws.daemon_ready()`. Protocol-level
`Ping`/`Pong` frames are discarded in `crates/gclient/src/daemon/live_reader.rs`
`decode_frame` and stay so. Rejected: a heartbeat before B2 (the client already
knows every in-flight age; it just never looks). Planned verification: `cargo
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

### B2 Idle keepalive [category: code] (depends: B1)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live_loop/health.rs`
- `crates/gclient/src/app/live_loop.rs::*` — scope-reason: `run_live_loop` ticks the keepalive
- `src/gobby/servers/websocket/handlers/core.py::HandlerMixin._handle_ping`
- `tests/servers/websocket/test_server.py::*` — scope-reason: adds the pong echo test beside the existing dispatch tests
- `docs/guides/gclient-user-guide.md`
- `crates/gclient/tests/loop_liveness.rs`

Consumers unchanged:
- `crates/gclient/src/daemon/live_reader.rs` — no-edit-reason: `handle_inbound` routes any reply by `route_key`, so a `pong` carrying `request_id` already reaches its waiter.

When no request has been in flight for 15 s, the loop sends
`{"type":"ping","request_id":...}` through `LiveDaemon::request`; the daemon's
`_handle_ping` (zero DB) echoes `request_id` in the `pong` so it routes as a normal
reply through `RouteKey::Request`. A pong later than `REQUEST_DEADLINE` is treated
as a disconnect (`observe_daemon_disconnect`) so a half-open socket after
sleep/wake or a daemon SIGSTOP is noticed within 20 s instead of never. The
keepalive state (last activity, in-flight ping) lives in `health.rs`; `run_live_loop`
only ticks it. The guide's `Daemon restarts and reconnects` section names the
keepalive.

`crates/gclient/src/app/live_loop.rs` is at 868 production lines: the keepalive is
a move into `crates/gclient/src/app/live_loop/health.rs`, not growth in
`live_loop.rs`.

**Cutover:** B2 changes gclient and the running Python daemon and does not promote
or restart either independently; after all leaves pass, it participates in the
single shared clean-cutover gate in Constraints.

**Research context:** Observed: `_handle_ping` (`src/gobby/servers/websocket/handlers/core.py`
~154-171) replies `{"type": "pong", "latency": ...}` with no `request_id`;
`LiveDaemon::request` (`crates/gclient/src/daemon/live.rs` ~440-487) correlates on
`route_key(&message)` (`crates/gclient/src/daemon/ws.rs` ~172) and `handle_inbound`
(`live_reader.rs` ~260-364) resolves `RouteKey::Request(id)` for any kind, so no
reader change is needed. Planned verification: `cargo nextest run -p gobby-client
--test loop_liveness`; `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test
GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/websocket/test_server.py`.

**Acceptance:**

- B2.1 - After 15 s idle the client sends a `ping` with a `request_id`; an
  unanswered ping past `REQUEST_DEADLINE` produces a disconnect and a reconnect
  attempt. test:
  `crates/gclient/tests/loop_liveness.rs::an_unanswered_keepalive_ping_counts_as_a_disconnect`.
- B2.2 - The daemon's `pong` echoes `request_id`. test:
  `tests/servers/websocket/test_server.py::test_pong_echoes_request_id`.
- B2.3 - The guide's reconnect section describes the keepalive. behavior:
  "keepalive" in `docs/guides/gclient-user-guide.md`.

### B3 Shed load while slow [category: code] (depends: B2)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live_sidebar.rs`
- `docs/guides/gclient-user-guide.md`
- `crates/gclient/tests/loop_liveness.rs`

The sidebar's `GIT_REFRESH_INTERVAL` (10 s) and `ROSTER_REFRESH_INTERVAL` (15 s)
polls (`request_git_refresh_if_due`, `request_roster_refresh_if_due`, in
`live_sidebar.rs` after R1) do not fire while `DaemonHealth` is `Slow` or
`Unreachable`; they resume on `Ready`. Attach retries keep their backoff
(`ATTACH_RETRY_BASE`/`ATTACH_RETRY_MAX` in `crates/gclient/src/app/attach.rs`).
Focus-hint and geometry sends are coalesced by A1 and need no gating.

The gate is a health check inside the two poll functions themselves, which live
in `crates/gclient/src/app/live_sidebar.rs` after R1 and derive `DaemonHealth`
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
- `crates/gterminal/tests/frame_protocol.rs::*` — scope-reason: add daemonless inventory, first-holder and refusal cases to the frame protocol suite
- `crates/gterminal/tests/control_protocol.rs::*` — scope-reason: add daemon takeover/revoke cases through the real control protocol
- `crates/gterminal/tests/wire_golden.rs::*` — scope-reason: cover the appended inventory request and response variants
- `docs/contracts/gterm-protocols.md`

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

**Granularity:** inventory and fallback authority land together because they are the
two server halves of one authenticated daemonless frame-session contract and the
wire enums, frame dispatcher and host state must compile atomically. The gclient
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
- `crates/gclient/src/app/mod.rs::*` — scope-reason: declare the module and keep daemonless workspace state in the extracted implementation
- `crates/gclient/src/app/pane.rs::*` — scope-reason: record local fallback authority and clear its provisional state on typed refusal
- `crates/gclient/src/app/live_loop/actions.rs::*` — scope-reason: delegate daemonless tab projection to the extracted native-degraded module
- `crates/gclient/src/app/live_loop/control.rs::*` — scope-reason: test direct input before daemon readiness for daemonless panes
- `crates/gclient/src/app/live_roster.rs`
- `crates/gclient/tests/startup.rs::*` — scope-reason: add missing-token and daemonless launch cases beside the existing unreachable-daemon test
- `crates/gclient/tests/loop_liveness.rs`
- `docs/guides/gclient-user-guide.md`

`resolve_probe_env_at` treats a missing default daemon token as `token=None`; an
explicit `--token-file` that is missing, unreadable or empty remains an error.
`run_ready` constructs `LiveDaemon` with the available token but does not await
`attach_live_workspace` before opening the terminal. It asks `NativeHost` for local
inventory, and new `native_degraded.rs` installs one direct pane and one local tab
per returned native row, keyed by stable gterm `terminal_id`. A missing gterm opens
an empty window with `Daemon unavailable; no local native host.` rather than exiting.

Each local pane gets a fresh client attachment id, binds on the frame stream, starts
Held with `lease_unconfirmed`, and sends `Input`/`Paste` directly even while
`daemon_ready == false`. An `InputRefused` clears provisional local authority and
shows take-back unavailable; it never retries or falls back through the daemon.
`sync_live_chrome` projects the flat local tabs only while no daemon workspace model
exists. When A5 later reconciles, `install_live_rows` reuses panes by terminal id,
preserves their live direct source, replaces the flat tabs with daemon layout and
lets C2 retake every unconfirmed holder. Rows with no matching local inventory use
the normal daemon attach path. **Restraint rung 2:** reuse `Pane`, local tabs,
`InputRefused`, terminal ids and A5 reconcile; add no offline workspace database or
synthetic daemon model.

To preserve the production ceiling, move all daemonless workspace state and local
tab projection out of near-ceiling `crates/gclient/src/app/mod.rs` and
`crates/gclient/src/app/live_loop/actions.rs` into new
`crates/gclient/src/app/native_degraded.rs`; the existing files gain only the module
declaration and bounded calls.

Without the daemon, the status/help text explicitly marks leases/takeover, layout
and workspace sync, roster, attention and agent relay unavailable; tmux, web and
proxied panes are not listed. Native render, scroll, copy and input remain available.

**Granularity:** eight production files form one launch-to-loop state transition:
startup capability, local inventory installation, tab projection and direct input
must land together so the new launch path never renders a pane it cannot type into.
The transport client is independently closeable and therefore split into C0b.

**Cutover:** C0c changes gclient and does not promote independently; after all
leaves pass it participates in the shared quiet-window build and separate gclient
promotion.

**Research context:** Observed: `prepare_at` already converts an unreachable health
probe into a notice, but `resolve_probe_env_at` still requires the daemon token;
`run_ready` calls `LiveDaemon::connect_or_wait` then awaits
`attach_live_workspace` before constructing the terminal. A down-daemon
`LiveDaemon` is already valid. `send_live_input` returns before testing direct input
when `daemon_ready` is false. `sync_live_chrome` currently projects only a daemon
workspace model. `ensure_live_pane` and `install_live_row` already reuse a pane by
terminal id, which is the reconnect merge seam. Planned verification: `cargo
nextest run -p gobby-client --test startup --test frame_source_live --test
loop_liveness --test reconciliation`; docs link check.

**Acceptance:**

- C0c.1 - A missing default daemon token and an unreachable daemon still produce a
  Ready session; an explicitly requested bad token file remains a startup error.
  test: `crates/gclient/tests/startup.rs::missing_default_daemon_token_starts_in_native_degraded_mode`.
- C0c.2 - With the daemon absent and a real gterm present, gclient launches, lists
  native panes only, attaches, renders advancing frames and delivers typed bytes to
  the PTY. test:
  `crates/gclient/tests/loop_liveness.rs::daemonless_launch_attaches_renders_and_types_native_panes`.
- C0c.3 - A competing local gclient that receives `input_not_granted` becomes
  read-only without daemon fallback, while the first holder keeps typing. test:
  `crates/gclient/tests/loop_liveness.rs::daemonless_second_client_cannot_take_over_the_local_holder`.
- C0c.4 - When the daemon connects after degraded launch, matching terminal ids
  preserve one direct source, daemon layout replaces local tabs, C2 retakes the
  unconfirmed holder, and no duplicate pane appears. test:
  `crates/gclient/tests/loop_liveness.rs::daemon_reconcile_adopts_daemonless_native_panes_without_duplication`.
- C0c.5 - Without the daemon, the UI states the unavailable enhancements and never
  lists tmux, web or proxied panes. behavior: "Native degraded mode" in
  `docs/guides/gclient-user-guide.md`.

### C1 Keep control on disconnect for direct-granted panes [category: code] (depends: C0c)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/disconnect.rs`
- `crates/gclient/src/app/live_reconcile.rs`
- `crates/gclient/src/app/pane.rs::*` — scope-reason: preserve direct control and record unconfirmed lease state across construction, disconnect and input admission
- `crates/gclient/src/app/live_loop/control.rs::*` — scope-reason: `send_live_input` tests `direct_input` before `daemon_ready`
- `docs/contracts/gterm-protocols.md`
- `docs/guides/gclient-user-guide.md`
- `crates/gclient/tests/loop_liveness.rs`

`observe_daemon_disconnect` (in `disconnect.rs` after R1) and the reconnect path's
pre-reconcile clear (in `live_reconcile.rs` after A5) call `Pane::clear_control` on
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
- `crates/gclient/src/app/live_attach.rs::*` — scope-reason: `apply_attach_result` issues a take for every pane whose lease is unconfirmed
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
write or the pre-grant queue. The daemon's `granted` result atomically replaces the
fallback with the fresh attachment id and clears `lease_unconfirmed`. A `held`
result means the daemon granted another attachment, so this stream receives the
typed host refusal, drops to Observe and offers take-back exactly as today. A
rebind or take transport error leaves the pane Held but unconfirmed while the
reconnect supervisor retries; the single host authority still prevents concurrent
writes. **Restraint rung 2:** reuse `BindAttachment`, the preserved frame stream and
the existing take outcome; add no handoff protocol or second source.

**Cutover:** C2 changes gclient and does not promote independently; after all
leaves pass, it participates in the single shared clean-cutover gate in
Constraints.

**Research context:** Observed: `restore_focused`
(`crates/gclient/src/app/live_loop/projects.rs` ~253-266) retakes only the focused
pane; `request_control`/`start_control_request` (in `live_control.rs` after A2)
issue takes; `Pane::queue_input`/`take_pending_input` (`pane.rs` ~451-468) hold
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

### C3 Direct re-attach before proxy fallback [category: code] (depends: C2)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live_attach/handshake.rs`
- `crates/gclient/tests/frame_source_live.rs::*` — scope-reason: adds the direct-reconnect case beside the existing direct-transport tests
- `docs/contracts/gterm-protocols.md`
- `docs/guides/gclient-user-guide.md`

`recover_job` (A3) sends every frame error to the daemon proxy path today. For a
direct native pane it first retries the direct path with the locator already held:
connect `frame_socket_path`, `AttachTerminal{host_terminal_id}`,
`BindAttachment(attachment_id)`; typing resumes because the grant is keyed by that
id. Only a connect failure or a gterm refusal (`not_found`, `capacity`) falls
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

## P4: daemon terminal handlers off the event loop and off the serial lane
`kind: framing`

**Goal:** a pool wait or the pool backoff sleep occupies an executor worker, never
the loop; one slow terminal handler no longer delays the other attachments on the
same connection; daemon-to-gterm round trips are bounded; the next pool exhaustion
is attributable. `crates/gclient`'s `attach_refusal_is_transient` is an allowlist,
so no client change follows from any new refusal code. `src/gobby/agents/spawn_executor.py`
is at 982 lines and gains nothing here.

### D1a Terminal attach, sizing, scroll, create and kill handlers on the executor [category: code]
`kind: deliverable`

Targets:
- `src/gobby/servers/websocket/terminal_ws.py::TerminalWsMixin._handle_terminal_attach`
- `src/gobby/servers/websocket/terminal_ws.py::TerminalWsMixin._handle_terminal_set_scroll_offset`
- `src/gobby/servers/websocket/terminal_sizing.py::TerminalSizingMixin._apply_terminal_sizing`
- `src/gobby/servers/websocket/terminal_ws_create.py::TerminalCreateMixin._handle_terminal_create`
- `src/gobby/servers/websocket/terminal_ws_create.py::TerminalCreateMixin._handle_terminal_kill`
- `src/gobby/servers/websocket/tmux.py::TmuxMixin._handle_terminal_attach`
- `tests/servers/test_terminal_ws_attach_honesty.py::*` — scope-reason: adds the executor-routing assertion beside the attach tests
- `tests/servers/test_terminal_ws_golden.py::*` — scope-reason: adds the no-storage scroll assertion; reply shapes are unchanged
- `tests/servers/test_terminal_ws_create.py::*` — scope-reason: adds the executor-routing assertion for create and kill
- `tests/servers/test_terminal_ws_resize.py::*` — scope-reason: adds the one-hop sizing assertion
- `tests/servers/test_tmux_mixin.py::*` — scope-reason: adds the row pass-through assertion
- `tests/servers/test_terminal_ws_lease.py::*` — scope-reason: keeps the sizing-patch regression beside the shared handler diagnostic added by D2 and D3b

Consumers unchanged:
- `src/gobby/servers/websocket/proxy_relay.py` — no-edit-reason: awaits `_apply_terminal_sizing` with the same signature.
- `src/gobby/servers/websocket/terminal_ws_control.py` — no-edit-reason: awaits `_apply_terminal_sizing` with the same signature.
- `src/gobby/adapters/acp_client_requests.py` — no-edit-reason: calls `_handle_terminal_create` with the same signature.
- `tests/servers/test_terminal_list_watermark.py` — no-edit-reason: calls the create and kill handlers with unchanged signatures.
- `tests/servers/test_terminal_ws_kill.py` — no-edit-reason: calls `_handle_terminal_kill` with an unchanged signature.
- `tests/terminals/test_backend_selection.py` — no-edit-reason: calls `_handle_terminal_create` with an unchanged signature.
- `tests/servers/test_terminal_ws_input.py` — no-edit-reason: calls `_handle_terminal_attach` with an unchanged signature.

Route every synchronous storage call in these handlers through
`WebSocketServer.run_db`, one executor call per handler (bundle get plus set into
one function so a handler makes one hop, not two): the attach `manager.get`;
`_apply_terminal_sizing` (get plus `set_dims` as one function), reached from
take/release/resize/detach and lease finalize; `terminal_set_scroll_offset` deletes
its SELECT and reads `backend` from the attach snapshot `record.terminal` (#22557
already stores it); create and kill; the tmux attach passes `terminal=row` (the row
is already fetched in the handler) so tmux keystrokes stop doing `_require()` per
key in the write coordinator.

**Research context:** Observed: `WebSocketServer.run_db`
(`src/gobby/servers/websocket/server.py` ~312-316) runs work on the bounded
`DatabaseExecutor`; `terminal_list` uses it since #22543. `_handle_terminal_attach`
(`terminal_ws.py` ~156-249) calls `manager.get` synchronously; `_apply_terminal_sizing`
(`terminal_sizing.py` ~64-105) does get plus `set_dims`;
`_handle_terminal_set_scroll_offset` (~441-468) does one SELECT per scroll event;
`_handle_terminal_create`/`_handle_terminal_kill` (`terminal_ws_create.py` ~58-198)
call storage inline; `TmuxMixin._handle_terminal_attach` (`tmux.py` ~161-227) attaches
the bridge without the row it fetched. Rejected: changing the pool module (D4 owns
diagnosis; capacity is out of scope). Planned verification:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test
GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/test_terminal_ws_attach_honesty.py
tests/servers/test_terminal_ws_golden.py tests/servers/test_terminal_ws_create.py
tests/servers/test_terminal_ws_resize.py tests/servers/test_terminal_ws_lease.py
tests/servers/test_tmux_mixin.py`; `uv run ruff format`, `uv run ruff check src/`,
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
- D1a.5 - The tmux attach passes the fetched row to the bridge. test:
  `tests/servers/test_tmux_mixin.py::test_tmux_attach_passes_the_fetched_row_to_the_bridge`.

### D1b WorkspaceOps runs its storage on the executor [category: code] (depends: D1a)
`kind: deliverable`

Targets:
- `src/gobby/terminals/workspace_ops.py::*` — scope-reason: every `self._workspaces.*` call and the resolve, sweep, list and mutation helpers go through the injected runner
- `src/gobby/terminals/workspace_ops_types.py`
- `src/gobby/servers/websocket/server.py::WebSocketServer.configure_terminals`
- `tests/terminals/test_workspace_ops.py::*` — scope-reason: adds executor-routing assertions to the existing op tests
- `tests/servers/test_workspace_ws.py::*` — scope-reason: adds the no-loop-thread-storage assertion for `workspace_attach`
- `tests/servers/test_terminal_ws_lease.py::*` — scope-reason: keeps the configure-terminals regression beside the shared handler diagnostic added by D2 and D3b

Consumers unchanged:
- `src/gobby/mcp_proxy/registries.py` — no-edit-reason: constructs `WorkspaceOps` without a runner; the runner is an optional keyword with a direct-call fallback.
- `src/gobby/mcp_proxy/tools/workspaces/registry.py` — no-edit-reason: constructs `WorkspaceOps` without a runner; same fallback.
- `src/gobby/servers/websocket/workspace_ws.py` — no-edit-reason: awaits the op methods, whose signatures are unchanged.
- `tests/mcp_proxy/test_workspaces_registry.py` — no-edit-reason: constructs `WorkspaceOps` without a runner; same fallback.
- `src/gobby/runner_init/servers.py` — no-edit-reason: calls `configure_terminals` with an unchanged signature.
- `tests/servers/test_native_web_proxy.py` — no-edit-reason: calls `configure_terminals` with an unchanged signature.
- `tests/servers/test_terminal_ws_input.py` — no-edit-reason: calls `configure_terminals` with an unchanged signature.
- `tests/servers/websocket/test_servers_websocket_auth.py` — no-edit-reason: calls `configure_terminals` with an unchanged signature.
- `tests/terminals/test_composition_roots.py` — no-edit-reason: calls `configure_terminals` with an unchanged signature.

`WorkspaceOps` takes an optional `run_db` at construction (injected from
`WebSocketServer.configure_terminals`, where it is built) and wraps its resolve,
sweep, list and mutation storage calls in it; the shell spawn inside `tab_create`
and `pane_split` stays async as today. `workspace_attach` today issues 5-8 queries
on the loop thread.

`src/gobby/terminals/workspace_ops.py` is at 958 lines: the module-level types and
helpers (`WorkspaceOpError`, `WorkspaceEvent`, `PaneWrite`, `PaneOutputWait`,
`WorkspaceSnapshot`, `_ShellSpawn`, `storage_errors`, `_workspace_of`, `_tab_of`,
`_pane_of`, `_require_local`, `_pane_ref`, `_identity_env`) move into new
`src/gobby/terminals/workspace_ops_types.py` and are re-exported from
`workspace_ops.py` so existing importers keep working.

**Research context:** Observed: `WorkspaceOps.__init__`
(`src/gobby/terminals/workspace_ops.py` ~250-265) takes `workspaces`, `terminals`,
`registry`, `coordinator`, `sessions`, `publish`; `storage_errors` (~173-192) wraps
storage exceptions; `_resolve`, `_enter`, `_sweep`, `_fill`, `_kill` (~658-894) call
storage synchronously; `PANE_SPAWN_TIMEOUT_SECONDS` is 30 s inside `workspace_op`.
The only production construction site is `configure_terminals`
(`src/gobby/servers/websocket/server.py` ~252-259). Planned verification:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test
GOBBY_TEST_PROTECT=1 uv run pytest tests/terminals/test_workspace_ops.py
tests/servers/test_workspace_ws.py tests/mcp_proxy/test_workspaces_registry.py`;
`uv run ruff check src/`, `uv run mypy src/`.

**Acceptance:**

- D1b.1 - Every `WorkspaceOps` storage call runs through the injected runner. test:
  `tests/terminals/test_workspace_ops.py::test_workspace_ops_run_storage_calls_on_the_executor`.
- D1b.2 - `workspace_attach` makes no loop-thread storage call. test:
  `tests/servers/test_workspace_ws.py::test_workspace_attach_makes_no_loop_thread_storage_call`.
- D1b.3 - The module types live in their own file and `workspace_ops.py` is under
  850 lines. file: `src/gobby/terminals/workspace_ops_types.py`.

### D1c WriteCoordinator persists on the executor [category: code] (depends: D1b)
`kind: deliverable`

Targets:
- `src/gobby/terminals/write_coordinator.py::*` — scope-reason: `_require`, `_persist`, `_clear` and their callers in the locked sequence await the injected runner
- `src/gobby/runner_init/terminal_wiring.py::init_terminal_wiring`
- `tests/terminals/test_write_coordinator.py::*` — scope-reason: adds the executor assertion to the locked-sequence tests

Consumers unchanged:
- `src/gobby/runner_init/orchestration.py` — no-edit-reason: calls `init_terminal_wiring` with an unchanged signature.
- `tests/terminals/test_composition_roots.py` — no-edit-reason: calls `init_terminal_wiring` with an unchanged signature.

The non-operator write path (`_require`, `_persist`, `_clear`, reached from
`_write_locked`, `run_sequence` and `run_native_wake_batch`) runs its
`UnresolvedWriteStore` calls on the executor through a runner injected with a
`set_db_runner(run_db)` setter (the `set_attention_gate` pattern), called from
`init_terminal_wiring`; the per-terminal asyncio lock stays held across the awaits,
semantics unchanged. Automatic writes (agent wake, nudges, `pane_send_text`) do up
to six transactions under that lock today, all on the loop thread.

**Research context:** Observed: `WriteCoordinator` (`src/gobby/terminals/write_coordinator.py`
~123-602) with `_write_locked` (~408-448), `_persist` (~504-518), `_clear` (~520-521),
`_require` (~523-527), `set_attention_gate` (~146-147); the store protocol is
`UnresolvedWriteStore` (~39-61), implemented by `src/gobby/storage/terminals.py`.
The planning draft cited a `terminal_settlement.py`; no such module exists and the
settlement calls are the coordinator's own `_persist`/`_clear`/`_require`.
`init_terminal_wiring` (`src/gobby/runner_init/terminal_wiring.py` ~33-118) builds
the coordinator and the host manager. Rejected: a constructor keyword (thirty-plus
construction sites in tests). Planned verification:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test
GOBBY_TEST_PROTECT=1 uv run pytest tests/terminals/test_write_coordinator.py
tests/terminals/test_composition_roots.py tests/terminals/test_write_input.py
tests/terminals/test_write_outcomes.py`.

**Acceptance:**

- D1c.1 - Non-operator writes persist on the executor while the terminal lock is
  held. test:
  `tests/terminals/test_write_coordinator.py::test_non_operator_writes_persist_on_the_executor_under_the_terminal_lock`.
- D1c.2 - `init_terminal_wiring` installs the runner on the coordinator. symbol:
  `init_terminal_wiring`.

### D1d Host reconcile row writes on the executor [category: code] (depends: D1c)
`kind: deliverable`

Targets:
- `src/gobby/terminals/host_manager.py::*` — scope-reason: `reconcile` runs its row writes on the injected runner and the host process helpers move out
- `src/gobby/terminals/host_process.py`
- `src/gobby/terminals/host_reconcile.py::reconcile_host_inventory`
- `src/gobby/runner_init/terminal_wiring.py::init_terminal_wiring`
- `tests/terminals/test_host_manager.py::*` — scope-reason: adds the executor assertion to the reconcile tests

Consumers unchanged:
- `tests/agents/test_native_spawn.py` — no-edit-reason: calls `reconcile_host_inventory` with the same keyword signature.
- `src/gobby/runner_init/orchestration.py` — no-edit-reason: calls `init_terminal_wiring` with an unchanged signature.
- `tests/terminals/test_composition_roots.py` — no-edit-reason: calls `init_terminal_wiring` with an unchanged signature.

The host health reconcile ticks every 5 s on the loop and writes rows
synchronously. `reconcile_host_inventory` gains an optional `run_db` keyword and
applies its `SupportsIdentityLookup` mutations through it when given;
`TerminalHostManager.reconcile` passes the runner injected by `init_terminal_wiring`.

`src/gobby/terminals/host_manager.py` is at 909 lines: the host process helpers
(`_spawn_host_process`, `_spawn_candidate`, `_publish_spawned_client`,
`_wait_for_client`, `_connect`, `_handshake`, `_process_alive`, `_reap_process`,
`_close_client`, `_drain_host`, `_interrupt`) move into new
`src/gobby/terminals/host_process.py` as module functions taking the manager, so
D3's connect-time deadline lands there.

**Research context:** Observed: `TerminalHostManager.reconcile`
(`src/gobby/terminals/host_manager.py` ~468-512) awaits `reconcile_host_inventory`
(`src/gobby/terminals/host_reconcile.py` ~92-199), which takes `terminal_manager:
SupportsIdentityLookup` and calls `promote_to_live`, `fail_pending_attempt`,
`mark_exited`, `mark_orphaned`, `merge_process_reap_record` synchronously;
`_connect` (~680-683) builds `HostClient.connect(control_socket_path(...))`. Planned
verification: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test
GOBBY_TEST_PROTECT=1 uv run pytest tests/terminals/test_host_manager.py
tests/agents/test_native_spawn.py`.

**Acceptance:**

- D1d.1 - Reconcile row writes run on the executor. test:
  `tests/terminals/test_host_manager.py::test_reconcile_row_writes_run_on_the_executor`.
- D1d.2 - The host process helpers live in their own module and
  `host_manager.py` is under 850 lines. file: `src/gobby/terminals/host_process.py`.

### D2 Concurrent dispatch per connection, ordered per lane [category: code] (depends: D1b, B2)
`kind: deliverable`

Targets:
- `src/gobby/servers/websocket/server.py::WebSocketServer._handle_connection`
- `src/gobby/servers/websocket/server.py::WebSocketServer._handle_message`
- `src/gobby/servers/websocket/dispatch.py`
- `tests/servers/websocket/test_server.py::*` — scope-reason: adds the lane dispatcher test beside the existing dispatch tests
- `tests/servers/test_websocket_server_disconnects.py::*` — scope-reason: adds socket-close cancellation of in-flight lanes
- `tests/servers/test_terminal_ws_lease.py::*` — scope-reason: adds the held-host-grant diagnostic through the real terminal handler

Consumers unchanged:
- `src/gobby/servers/_app_ui.py` — no-edit-reason: calls `handle_connection`, whose signature is unchanged.
- `tests/servers/test_ws_asgi_endpoint.py` — no-edit-reason: calls `handle_connection`, whose signature is unchanged.
- `tests/mcp_proxy/test_workspaces_registry.py` — no-edit-reason: calls `_handle_message` directly with an unchanged signature.
- `tests/servers/test_native_web_proxy.py` — no-edit-reason: calls `_handle_message` directly with an unchanged signature.
- `tests/servers/test_terminal_ws_input.py` — no-edit-reason: calls `_handle_message` directly with an unchanged signature.
- `tests/servers/test_terminals_routes.py` — no-edit-reason: calls `_handle_message` directly with an unchanged signature.

In `_handle_connection` the inline `await self._handle_message` becomes a
per-connection dispatcher in new `src/gobby/servers/websocket/dispatch.py`: each
message gets a lane key, messages in one lane run in order, lanes run concurrently,
and total in-flight per connection is capped by a semaphore (16) so a flood queues
instead of spawning unbounded tasks. Lanes: `attachment:<id>` for any message
carrying `attachment_id`; `workspace` (one per connection) for `workspace_*`,
because splits and closes on one tab must stay ordered; `request:<id>` (fully
concurrent) for read-only `terminal_list`; `default` (one ordered lane, today's
behaviour) for chat, voice, `tool_call` and anything unclassified. Socket close
cancels every lane task, awaits them with `gather(return_exceptions=True)`, and
clears the lane map before connection cleanup; no task from the dead socket survives
to mutate state. The existing completion-only slow-handler warning in
`_handle_message` becomes "later messages in this lane waited". There is no
per-lane timeout, sampler or one-shot watchdog. **Restraint rung 2:** reuse tasks,
the semaphore and the existing completion warning; add only the dispatcher needed
for ordering and concurrency. The dispatcher test drives a fake async websocket
iterator through `handle_connection` and proves an attachment-A handler that
sleeps 1 s does not delay a `terminal_take_control` on attachment B or a
`workspace_op` on the same connection, while two `workspace_op`s stay ordered.

**Research context:** Observed: `_handle_connection`
(`src/gobby/servers/websocket/server.py` ~327-405) runs `async for message in
websocket: await self._handle_message(...)`; `_handle_message` (~411-506) pins the
runtime bundle in a contextvar per message and logs `websocket handler %s took
%.2fs; later messages on this connection waited` past
`SLOW_WEBSOCKET_HANDLER_SECONDS`; the runtime-bundle contextvar must be pinned per
lane task. That warning runs only after `_handle_message` returns and therefore left
no evidence for the 2026-09-21 never-returned-handler hypothesis. That hypothesis
remains unproven rather than gaining a runtime watchdog. Golden fixtures live in
`tests/fixtures/terminal_ws_golden/`. Planned
verification: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test
GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/websocket/test_server.py
tests/servers/test_websocket_server_disconnects.py tests/servers/test_terminal_ws_golden.py
tests/servers/test_workspace_ws.py`.

**Acceptance:**

- D2.1 - Attachment lanes dispatch concurrently while workspace ops stay ordered,
  proven through the real connection loop. test:
  `tests/servers/websocket/test_server.py::test_attachment_lanes_dispatch_concurrently_while_workspace_ops_stay_ordered`.
- D2.2 - Socket close cancels and awaits every in-flight lane, runs each handler's
  `finally`, and leaves no dispatcher task. test:
  `tests/servers/test_websocket_server_disconnects.py::test_socket_close_cancels_in_flight_lanes`.
- D2.3 - The dispatcher, its lane keys and the in-flight cap live in their own
  module. file: `src/gobby/servers/websocket/dispatch.py`.
### D3 Bounded HostClient round trips [category: code] (depends: D1d)
`kind: deliverable`

Targets:
- `src/gobby/terminals/host_client.py::HostClient._roundtrip`
- `src/gobby/terminals/host_client.py::HostClient.grant_input`
- `src/gobby/terminals/host_client.py::HostClient.revoke_input`
- `src/gobby/terminals/host_client.py::HostClient.connect`
- `src/gobby/terminals/host_client.py::HostClient.reconnect`
- `src/gobby/terminals/host_process.py`
- `src/gobby/terminals/native_runtime.py::*` — scope-reason: grant/revoke/reconnect orchestration moves to the new helper module and the runtime methods become delegates
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

`src/gobby/terminals/native_runtime.py` is at 851 lines: move the grant/revoke and
reconnect deadline orchestration into new
`src/gobby/terminals/native_input_grants.py`; the three targeted runtime methods
delegate with the runtime/client/terminal inputs and do not grow the near-ceiling
module.

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

### D3b Host grant reconciliation never holds the terminal lease lock [category: code] (depends: D2, D3, C2)
`kind: deliverable`

Targets:
- `src/gobby/terminals/leases.py::*` — scope-reason: `HolderChange`, holder-sync cells/tasks, `_notify_holder`, `take_control`, `release_control` and `finalize` move every host observer await outside the lease lock
- `tests/terminals/test_lease_authority.py::*` — scope-reason: adds transition, race and cancellation coverage for the two lock domains
- `tests/servers/test_terminal_ws_lease.py::*` — scope-reason: adds the unanswered real HostClient grant diagnostic shared with D2
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

Two small additions so the next incident is attributable: `setup_file_logging`
lets the `psycopg.pool` logger's WARNING lines (the "error connecting" text
psycopg_pool emits when the pool cannot grow) reach `daemon.log`, beside the
existing `websockets` logger levels; and `_acquire_with_backoff`'s final failure
branch takes a rate-limited (once per minute) census over a one-off direct
connection with `connect_timeout=2`: `application_name, state, count` from
`pg_stat_activity`, logged beside `pool_stats`. If that connection is refused, that
is logged too, which by itself proves Postgres is at `max_connections`.

**Research context:** Observed: `_acquire_with_backoff`
(`src/gobby/storage/hub/postgres_pool.py` ~159-188) retries over
`POOL_TIMEOUT_RETRY_BACKOFF_SECONDS` with `time.sleep` and logs
`PostgreSQL hub pool acquisition failed after %d retries: pool_stats=%s` on final
failure; logger levels are set in `setup_file_logging`
(`src/gobby/telemetry/logging.py` ~464-498, the `websockets` loop at ~493). The
planning draft placed the logger wiring in `src/gobby/config/logging.py`; that
module holds only settings, so no config carrier is involved. Planned verification:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test
GOBBY_TEST_PROTECT=1 uv run pytest tests/storage/hub/test_postgres_pool_backoff.py
tests/telemetry/test_logging.py`.

**Acceptance:**

- D4.1 - The final acquire failure logs a rate-limited `pg_stat_activity` census or
  the refused direct connection. test:
  `tests/storage/hub/test_postgres_pool_backoff.py::test_final_acquire_failure_logs_a_rate_limited_activity_census`.
- D4.2 - `psycopg.pool` warnings reach the daemon log. test:
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

`src/gobby/terminals/native_runtime.py` is at 850 lines: the spawn-failure helpers
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

Daemonless live path: stop the isolated daemon before launch and remove the default
daemon token, while a real promoted gterm owns one native PTY. Launch gclient and
verify it opens, lists only that native pane, attaches, renders advancing output and
delivers typed bytes. Confirm the UI names unavailable lease/takeover,
layout/workspace, roster, attention and relay features and offers no tmux, web or
proxy panes. Launch a second gclient and verify its input is refused while the first
keeps typing. Start the isolated daemon, then verify the same pane is adopted by
terminal id without duplication, its frame stream rebinds to the daemon attachment,
typing continues before the take reply, and the daemon grant replaces the fallback.

gclient live, the freeze: with the promoted binaries and a direct pane held, run
`kill -STOP <daemon pid>` at t=0, immediately focus another project to issue its
scoped `terminal_list` roster request, and keep the daemon stopped for at least 6 s
after that request. The held request must be observable as `Daemon slow` by t=1 s;
if it is not, the request did not issue and the check restarts. It reaches
`REQUEST_DEADLINE` at t=5 s, at which point the status becomes
`Daemon unreachable.`, a reconnect attempt is scheduled, and the direct pane stays
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

- 2026-09-21: made the split-right freeze hypothesis testable; added ordered lanes,
  lock-free host-grant reconciliation, frame-recovery logging, and the shared
  native-binary clean-cutover gate.
- 2026-09-21: resolved GDR-R1-F01 through F11 with corrected ordering, restart-grant
  continuity, aggregate deadlines, lock ordering, path-complete tests and bounded input.
- 2026-09-21: round 2 adds daemonless native launch/render/input and local fallback
  authority, removes both proposed watchdogs, tightens the EOF/retry deadline and
  lock-release proof, references #22677, closes WARN gaps, and corrects diagnosis.

Round 1 adversarial review (plan-adversary-taskless run 47e9dd2e, 2026-09-21 10:1x): needs_review, 11 blocking findings, 2 candidates dismissed. The coordinator (the assistant gobby#14069, under delegated planning-lane authority from the orchestrator gobby#14018) accepted all 11.

GDR-R1-F01 (C1 creation order), F02 (the direct grant is revoked on ws_close across restart), F03 (aggregate host-grant deadline), F04 (reconcile_latest_holder lock order), F05 (A3 granularity decision), F06 (P1 path acceptance parity), F07 (Decision Record direct-input authority boundary), F08 (freeze-hypothesis falsification order), F09 (SIGSTOP health timing), F10 (D3 binary cutover self-contained), F11 (bounded proxied-input queue).

F02 resolution direction: keep direct typing uninterrupted across a daemon restart (Josh 2026-09-21: 'seriously reduce blocking dependency on the daemon in gclient'). ws_close finalize must not revoke the current direct host grant; explicit release, detach, takeover and terminal removal still revoke. The revision goes to the planner under #22663.

```json plan-review-round
{"evidence_id":"4406027e-ff69-44c5-8820-35a005a20a7f","plan_hash":"04c1c42eb897f6f2d63000b09244f7ab10bfac11ee839bf3a4c8bf82a039dcee","round_number":1,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"c6ff1b2bfd0589ec6a75bb75c21893a61975cc90b2529ae6c07c3e05e51a1f7a","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":2,"emitted_findings":11,"total":13},"evidence_id":"4406027e-ff69-44c5-8820-35a005a20a7f","lanes":[{"candidate_count":8,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":1,"lane_id":"repository_blast_radius","status":"delegated-verified"},{"candidate_count":4,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":22,"manifest_digest":"8c306493dde84d775551d311d0c522b34eff15a2ed296253145d5cc21c32be9f","status":"valid"},"source_digest":"342770e1fc2598507eef652557787071dcb9076fc031e2d761fd0c5e20701996","version":1},"findings":[{"category":"bad-sequencing","check_key":"live-reconcile-creation-order","description":"C1 targets crates/gclient/src/app/live_reconcile.rs, but that file is created by A5. The current graph is C1→C2→C3→A4→A5, so C1 runs before its target exists and P1 is unnecessarily serialized behind P2/P3.","finding_id":"GDR-R1-F01","fix":"Remove C3 from A4's dependencies so A5 follows A4 and creates live_reconcile.rs; then make C1 depend on B3 and A5, leaving C2 and C3 after C1. Update the affected headings and manifest edges.","location":".gobby/plans/gclient-daemon-resilience.md:596,672-699,889-904","prevention":"Topologically walk every new-file target and verify its creating deliverable precedes every consumer; justify each cross-phase edge with the exact produced interface.","principle":"Each deliverable must be executable against the repository state produced by its declared predecessors, and dependency edges must encode real prerequisites.","root_cause":"A4 was made dependent on C3 even though it consumes no C3 interface, which places C1 before A5 even though C1 targets the file A5 creates.","section_id":"C1","severity":"blocking"},{"category":"unhandled-edge","check_key":"restart-grant-survival","description":"A graceful daemon restart revokes the direct gterm grant before gclient can re-take it. The next direct Input is refused, so the plan cannot satisfy 'typing never stops' while D3b.3 requires socket-close finalize to revoke.","finding_id":"GDR-R1-F02","fix":"Choose and specify one coherent contract. To preserve uninterrupted direct typing, make ws_close finalize daemon-side state without revoking the current direct host grant, while explicit release, detach, takeover, terminal removal, and non-direct cleanup still revoke; revise D3b.3 and add a real WebSocketServer/HostClient/gterm restart test that types after cleanup starts and before C2 completes. Otherwise narrow C1/Q1 to admit interruption and take-back.","location":".gobby/plans/gclient-daemon-resilience.md:902-914,1401-1441; src/gobby/servers/websocket/tmux.py:110-122; src/gobby/terminals/leases.py:480-529; src/gobby/terminals/input_grants.py:49-79","prevention":"Trace disconnect behavior end to end through client, websocket cleanup, lease finalization, host reconciliation, and gterm admission before promising continuity.","principle":"A claimed outage invariant must hold across both client state and authoritative server cleanup transitions.","root_cause":"C1 reasons only about gclient retaining Held, while current websocket cleanup finalizes the attachment and synchronizes holder=None to gterm as revoke; D3b.3 explicitly preserves that revoke.","section_id":"C1","severity":"blocking"},{"category":"unhandled-edge","check_key":"host-grant-aggregate-deadline","description":"grant_input/revoke_input can wait 1.5 seconds, reconnect without a bound, then wait another 1.5 seconds. The operation can exceed gclient's 2-second CONTROL_REQUEST_DEADLINE and block D3b's per-terminal host-sync lane longer than the plan claims.","finding_id":"GDR-R1-F03","fix":"Target NativeTerminalRuntime.grant_input/revoke_input and apply one absolute sub-2-second budget across ensure, first attempt, reconnect, retry, writer lock/drain, and reply wait, passing only remaining time downstream. Add a test where both attempts stall and assert total elapsed time remains below the client deadline and all HostClient pending entries clear.","location":".gobby/plans/gclient-daemon-resilience.md:1335-1364,1368-1375,1442-1446; src/gobby/terminals/native_runtime.py:702-722,846-851","prevention":"For every timeout claim, enumerate all awaited phases and retries and test wall-clock time through the highest-level operation.","principle":"A deadline promised to an upstream caller must bound the complete operation, including reconnect and retry, not each individual attempt.","root_cause":"D3 adds a 1.5-second HostClient round-trip timeout but leaves NativeTerminalRuntime's reconnect-and-retry behavior outside that budget.","section_id":"D3","severity":"blocking"},{"category":"unhandled-edge","check_key":"reconcile-latest-lock-order","description":"A newer mutation can commit and win the holder-sync lock, apply the new holder, and release it; the recovery task can then acquire the lock and apply its older pre-lock snapshot last.","finding_id":"GDR-R1-F04","fix":"Specify that reconcile_latest_holder acquires the per-terminal holder-sync lock first, then snapshots the current generation/holder under the lease lock, releases only the lease lock, and performs the host await. Add a barrier-driven race test where the newer normal sync wins first and final host state still matches the newest generation.","location":".gobby/plans/gclient-daemon-resilience.md:1401-1413,1435-1441","prevention":"For every two-lock recovery path, enumerate both lock-acquisition races and prove the final external write corresponds to the newest committed generation.","principle":"Recovery that promises latest-state convergence must linearize its snapshot with the serializer for the external effect.","root_cause":"reconcile_latest_holder snapshots generation/holder under the lease lock before acquiring the holder-sync lock.","section_id":"D3b","severity":"blocking"},{"category":"gobby-format","check_key":"granularity-decision","description":"A3 exceeds the plan-coverage granularity threshold but lacks the required decision; unlike A2 and A4, it does not explain why all targeted production changes must land together.","finding_id":"GDR-R1-F05","fix":"Add a bounded Granularity paragraph proving the seven-file change is one compile/runtime seam, or split open_unresolved job conversion and/or recovery logging into independently closeable deliverables with explicit dependencies and acceptance.","location":".gobby/plans/gclient-daemon-resilience.md:507-594","prevention":"Count distinct production files for every deliverable after target expansion and add the required bounded Granularity decision before review.","principle":"A deliverable touching more than six distinct hand-maintained production files must record why the work is inseparable or be split into independently closable leaves.","root_cause":"A3 combines handshake/recovery, loop integration, unresolved-terminal opening, outcome application, and logging across seven production files without a Granularity decision.","section_id":"A3","severity":"blocking"},{"category":"traceability","check_key":"p1-path-acceptance-parity","description":"Acceptance does not directly cover SetViewport backpressure status, attention-response job conversion, open_unresolved_terminals job conversion, or orphan fetch/kill fan-out with DestroySummary. Existing tests can pass while those paths remain blocking or behavior regresses.","finding_id":"GDR-R1-F06","fix":"Add named held-request liveness/behavior tests for each uncovered path and a bounded final audit proving every run_live_loop branch and post-select helper issues daemon work without awaiting it.","location":".gobby/plans/gclient-daemon-resilience.md:374-383,403-417,464-468,503-505,535-536,578-594,617-628,654-670","prevention":"Build a requirement-to-acceptance matrix for every run_live_loop branch and post-select helper before declaring P1 complete.","principle":"Every behavior and changed path needed for the primary goal must have artifact-backed acceptance that can fail if that path still awaits the daemon or loses its promised outcome.","root_cause":"Implementation prose and Targets were expanded without matching acceptance for several newly converted paths.","section_id":"A1","severity":"blocking"},{"category":"traceability","check_key":"direct-input-authority-boundary","description":"The Decision Record does not preserve b59e4ce9/be35449d precisely: the daemon still owns leases, layout, and workspaces; tmux/web/proxy input remains daemon-mediated; only direct-native keystrokes bypass it. D1a's per-key SELECT removal also contradicts 'Nothing here optimises the daemon keystroke path.'","finding_id":"GDR-R1-F07","fix":"Rewrite the decision boundary verbatim in architectural terms and name D1a's existing per-key lookup removal as the sole daemon-input-path optimization in scope.","location":".gobby/plans/gclient-daemon-resilience.md:91-102,168-172","prevention":"Transcribe owner decisions as an explicit matrix of transport, authority, and in-scope exceptions.","principle":"A plan must state owner decisions at their exact authority boundary and must not contradict its own scoped exception.","root_cause":"The shorthand 'Keystrokes stay off the daemon' omits that only direct-native Input/Paste bypass it, while the next sentence denies optimization of a daemon keystroke path that D1a explicitly optimizes.","section_id":"Decision Record","severity":"blocking"},{"category":"weak-testability","check_key":"freeze-hypothesis-falsification-order","description":"The final D3b test can show an unanswered HostClient wait and prove lock-free bounded recovery, but it cannot prove or refute the original claim that the wait occurred while the lease lock was held.","finding_id":"GDR-R1-F08","fix":"Add an explicit checkpoint after D2/D3 but before D3b that captures the watchdog chain and lock-held=true against the pre-D3b behavior; retain that evidence, then run the final D3b test for lock-held=false and bounded progress. Rephrase Q1 so a mismatch blocks D3b/cutover, not expansion, and do not claim proof of the historical incident.","location":".gobby/plans/gclient-daemon-resilience.md:1377-1446,1545-1555","prevention":"Place causal checkpoints before the mutation that removes the hypothesized condition, and distinguish historical attribution from final regression proof.","principle":"A diagnostic can falsify a historical mechanism only if it observes the mechanism before the repair removes the relevant state.","root_cause":"Q1 assigns falsification to the post-D3b lock-free test and says a mismatch blocks expansion even though the test exists only after expansion and implementation.","section_id":"D3b","severity":"blocking"},{"category":"weak-testability","check_key":"sigstop-health-timing","description":"The five-second SIGSTOP run may produce neither Daemon slow nor Daemon unreachable, so it cannot deterministically validate B1/B2/C1.","finding_id":"GDR-R1-F09","fix":"Issue and observe a known daemon request immediately before/during SIGSTOP and hold it past REQUEST_DEADLINE, or keep SIGSTOP active for more than the 15-second idle interval plus the 5-second request deadline. Record exact timing and expected status transitions.","location":".gobby/plans/gclient-daemon-resilience.md:792-839,1568-1576","prevention":"For timing validations, state the triggering request, start condition, timeout budget, and expected transition timestamps.","principle":"A live validation must force the event it claims to observe and allow enough time for every configured interval and deadline.","root_cause":"The validation stops the daemon for 5 seconds while idle keepalive starts only after 15 seconds; direct typing intentionally sends no daemon request.","section_id":"B2","severity":"blocking"},{"category":"traceability","check_key":"binary-cutover-self-contained","description":"D3 is not decision-complete about cutover: its gcore carrier change must force a coherent gcode/gdaemon/ghook rebuild and promotion in addition to the separately promoted gclient, but neither D3 nor its acceptance says so.","finding_id":"GDR-R1-F10","fix":"State in D3 that the gcore carrier dirties the coherent trio; after all leaves pass, one global quiet-window announcement must rebuild/promote gcode/gdaemon/ghook through promote_workspace_binary_set, promote gclient separately, restart from the main checkout, and read identity/hashes from ~/.gobby/bin. Add the same explicit shared-gate reference to every binary-touching deliverable without promoting per leaf.","location":".gobby/plans/gclient-daemon-resilience.md:122-138,1308-1317,1355-1364,1557-1566","prevention":"For every binary-touching deliverable, name the binaries dirtied by each target and point to the exact batched announcement/build/promotion/installed-hash gate.","principle":"A binary-affecting deliverable must state its complete cutover obligation, including generated crate inputs and the installed artifact set, while shared promotion remains batched.","root_cause":"D3 changes crates/gcore/assets/config/runtime_config_contract.json but the shared gate conditionally says to rebuild the coherent trio only when 'Rust inputs' changed, leaving the JSON carrier and D3's required installed set ambiguous.","section_id":"D3","severity":"blocking"},{"category":"unhandled-edge","check_key":"proxied-input-queue-bound","description":"When the daemon stalls, gclient remains responsive only by accumulating an unbounded per-keystroke queue, creating a memory-exhaustion failure path. The existing pre-grant input path is bounded, so this is a regression in resilience.","finding_id":"GDR-R1-F11","fix":"Use a bounded, nonblocking per-pane queue with an explicit byte/message cap and defined Backpressure/refusal/coalescing policy that preserves ordering. Add a held terminal_input flood test proving rendering/direct input remain live and queued memory stays within the cap.","location":".gobby/plans/gclient-daemon-resilience.md:442-453,486-505","prevention":"Audit every async queue introduced for capacity, overload policy, and a sustained-stall test.","principle":"Removing a blocking await must not replace bounded backpressure with unbounded memory growth under the same stalled dependency.","root_cause":"A2 uses a per-pane UnboundedSender for every proxied/tmux write and defines no byte/message cap or overload behavior.","section_id":"A2","severity":"blocking"}],"reviewer_session":"e7d3ec52-52ab-40cf-abe4-0935bd7cf4e0","round":1,"verdict":"needs_review"},"session_id":"26de7dbf-1f31-455c-ade4-993cc7c42cf6"}
```
