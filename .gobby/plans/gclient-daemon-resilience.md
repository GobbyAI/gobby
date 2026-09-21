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

The 2026-09-21 split-right freeze is a separate trigger whose root cause remains
**unproven**. The old gclient connection stopped making progress, a fresh gclient
connection worked against the same daemon, and no sessions were lost. The leading,
falsifiable hypothesis is one old-connection lane stuck in
`HostClient._roundtrip` (`src/gobby/terminals/host_client.py`, observed symbol range
317-353) while `TerminalLeaseRegistry.take_control` or `release_control`
(`src/gobby/terminals/leases.py`, observed ranges 421-478) held the terminal lease
lock across the gterm grant callback; the serial connection loop in
`WebSocketServer._handle_connection` (`src/gobby/servers/websocket/server.py`,
observed range 327-405) then stopped reading later messages. P1 prevents that lane
from parking the gclient loop, D2 makes other lanes independent and captures a stuck
stack, D3 bounds the host wait, and D3b removes the lock from the external wait. V1
contains the diagnostic that must either capture this chain or refute it.

gterm needs no change. `crates/gterminal` never dials the daemon; PTY delivery is
`try_send`, frames come off a 30 ms ticker, `input_activity` fan-out drops a slow
subscriber, and grants persist in memory.

Intended outcome: a slow, erroring or restarting daemon degrades only the action
that needed it. Rendering, direct-pane typing, frame ingest, scrolling and reconnect
never stop. The status line says when the daemon is slow. Daemon-side, one slow
terminal handler no longer stalls the other panes on the same window or the other
connections on the daemon.

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
4. gterm is not changed and its binary is not rebuilt, which also sidesteps the
   `--features vt-engine --bin gterm` trap (memory `b59e4ce9`).
5. Keystrokes stay off the daemon (memory `b59e4ce9`). Nothing here optimises the
   daemon keystroke path.
6. Josh, 2026-09-21: P1 is the vehicle for reducing gclient's blocking dependency
on the daemon. The split-right incident is evidence for the priority, not a proven
root cause; the plan must leave a stack-level diagnostic for the next occurrence.

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
`live_attach.rs`, `control.rs` and `actions.rs`, and P4 touches `terminal_ws.py`.
The coordinator gobby#14018 holds the epic until then.

Clean cutover. No implementation leaf promotes a live binary by itself. After all
P1-P4 commits and focused validation are green, announce the cutover with one
`global` `gobby-agents:send_message` and wait until no spawned worker or close
validator is live. Rebuild the coherent `gcode`/`gdaemon`/`ghook` set whenever its
Rust inputs changed and promote it only through
`src/gobby/install/bin_set_coherence.py::promote_workspace_binary_set`; then build
gclient with `cargo build --release -p gobby-client` and promote it separately
through
`src/gobby/cli/install_setup_gclient.py::install_gclient_from_submodule`, because
`promote_workspace_binary_set` rejects gclient (memory `8303e661`). Restart the
Python daemon from the main checkout and restart each validation gclient so it execs
the new inode. gterm stays untouched in the planned work. If implementation evidence
forces a gterm change, use the same announced quiet window, build with
`cargo build --release -p gobby-terminal --features vt-engine --bin gterm`, promote
through `src/gobby/cli/install_setup_gterm.py::install_gterm_from_submodule`, and
rerun the direct-pane checks. **Restraint rung 2:** reuse the repository's native
promotion functions and batch one cutover; add no installer or promotion path.

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
and what consumed the direct-connection reserve during the 04:58 exhaustion; any
change to gterm, `PROTOCOL_VERSION`, the lease model, or the daemon keystroke path
for tmux, web and proxied panes beyond the per-key SELECT removal; re-adding an
event-loop lag watchdog; reducing `REQUEST_DEADLINE`.

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

### A1 Job plumbing, focus hints and geometry [category: code] (depends: R1, A0)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live_loop.rs::*` — scope-reason: `run_live_loop` gains the outcome branch and loses its post-select awaits; `resize_live_workspace` becomes a sync stage
- `crates/gclient/src/app/live_loop/jobs.rs`
- `crates/gclient/src/app/live_loop/jobs/tests.rs`
- `crates/gclient/src/app/live_loop/jobs_apply.rs`
- `crates/gclient/src/app/live_loop/workspace_actions.rs::*` — scope-reason: `send_focus_hints_if_changed` becomes a coalesced job and `issue_workspace_op` is added beside `send_workspace_op`
- `crates/gclient/src/app/run_loop.rs::propagate_geometry`
- `crates/gclient/src/frame_source.rs::UnixSocketFrameSource::send_input`
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
- A1.6 - A direct source accepts `SetViewport` through `send_input`. symbol:
  `UnixSocketFrameSource::send_input`.

### A2 Typed input and control path [category: code] (depends: A1)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live_loop/control.rs::*` — scope-reason: every daemon-awaiting function becomes a sync issue or a job outcome apply
- `crates/gclient/src/app/pane.rs::Pane`
- `crates/gclient/src/app/pane.rs::Pane::new`
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
Option<UnboundedSender<(u64, Value)>>` and `release_pending: bool`; a per-pane writer
task drains the queue sequentially and reports one `Write` outcome per message, so
typed order on proxied and tmux panes holds and `client_write_seq` is still assigned
on the loop. Release-before-take on a focus move: `start_control_request` drains
every `release_pending` pane into the spawned control task ahead of the take.

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

### A3 Attach, recovery and open-unresolved as jobs [category: code] (depends: A2)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live_attach.rs::*` — scope-reason: the handshake bodies move into the job module and the remaining functions become sync plan/apply state functions
- `crates/gclient/src/app/live_attach/handshake.rs`
- `crates/gclient/src/app/live_loop.rs::*` — scope-reason: the frame branch shrinks to classify plus issue; `FrameRecovery`, `deferred_input` and the inner select are deleted
- `crates/gclient/src/app/attach.rs::Pane::begin_attaching`
- `crates/gclient/src/app/live_workspace.rs::open_unresolved_terminals`
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
sets `status_message = "attaching (direct|proxy)…"`. `open_unresolved_terminals`
issues `Opened` jobs. C3's direct-first retry later lands inside `recover_job`.

Frame recovery also becomes observable. The issue side emits one structured
`tracing::warn!` when a real frame error starts recovery; the apply side emits one
`tracing::warn!` named `frame_recovery_gave_up` when detach reaches its deadline,
attach is refused or errors, or a stale/tombstoned result is discarded without
installing a replacement source. Both records carry `pane_id`, `terminal_id`, old
`attachment_id` when present, transport, daemon generation, recovery stage, error
class and elapsed milliseconds. `Refused` and `Backpressure` input outcomes are not
frame failures and remain excluded. A successful recovery emits one
`tracing::info!` completion with the replacement transport so the two warnings can
be closed in `gclient.log`. The job/apply seam owns these records; there is no new
logger, telemetry transport or retry counter. **Restraint rung 2:** reuse gclient's
existing tracing file subscriber and the A3 outcome instead of adding a diagnostics
subsystem.

`crates/gclient/src/app/live_loop.rs` is at 868 production lines: the frame branch
shrinks to classify plus issue, and `FrameRecovery`, `deferred_input` and the inner
input-only select are deleted; the recovery body is a move out of `live_loop.rs`
and `live_attach.rs` into `crates/gclient/src/app/live_attach/handshake.rs`. The
render tick calls `plan_attaches` instead of `attach_ready_panes().await`; an inline
`attach_ready_panes()` (plan, await job, apply) stays for `reconcile_subscribe_first`
and the existing tests.

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
2 s. `open_unresolved_terminals` (`live_workspace.rs` ~79-100) awaits per terminal.
Rejected: awaiting the attach inside the branch with a timeout (still parks the
loop). Planned verification: `cargo nextest run -p gobby-client --test loop_liveness
--test frame_delivery --test reconciliation --test frame_source_live`.

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
pane, terminal, attachment, generation, stage, error class and elapsed time, while a
successful replacement closes the attempt. test:
`crates/gclient/tests/loop_liveness.rs::frame_errors_and_recovery_giveups_are_logged_with_pane_context`.

### A4 Actions and event-driven refetches as jobs [category: code] (depends: A3, C3)
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

`crates/gclient/src/app/live_loop.rs` is at 868 production lines: the refetch drain
and the new outcome arms are a move into `crates/gclient/src/app/live_loop/jobs_apply.rs`
rather than growth in `live_loop.rs`; `handle_live_event` only sets and drains flags.

**Granularity:** eight production files because the spawn, close, roster and orphan
paths all consume the `Spawned`/`Terminated`/`Roster` outcome family introduced
here; each test in the acceptance list exercises one path end to end.

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

`crates/gclient/src/app/live.rs` (986 lines), `crates/gclient/src/app/live_loop.rs`
(868) and `crates/gclient/src/app/mod.rs` (927) are near the ceiling: the reconcile
body is a move out of `live.rs` and `live_loop.rs` into
`crates/gclient/src/app/live_reconcile.rs`; `mod.rs` gains the module line only.

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

## P2: daemon health and load shedding
`kind: framing`

**Goal:** the client observes daemon responsiveness from the age of its own
in-flight requests, says so on the status line, keeps a half-open socket from
going unnoticed, and stops polling while the daemon is slow. No new protocol
beyond a `request_id` on `ping`. Every P2 production deliverable changes the
gclient binary and therefore uses the shared clean-cutover gate after merge; none
promotes independently.

### B1 Daemon health from in-flight age [category: code] (depends: A3)
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

**Goal:** gterm keeps `TerminalSlot.input_grant` until the daemon replaces it, and
its frame-stream attach and bind need nothing from the daemon: `HostState::attach`
takes a bare `host_terminal_id` for a user attachment and `bind_attachment` stamps
the id on the stream with no uniqueness check. The client already holds
`host_terminal_id` and `frame_socket_path` in `AttachLocator`. So a bound direct
attachment can keep typing through a daemon outage and reconfirm its lease on
reconnect. Every P3 production deliverable changes the gclient binary and therefore
uses the shared clean-cutover gate after merge; none promotes independently.

### C1 Keep control on disconnect for direct-granted panes [category: code] (depends: B3)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/disconnect.rs`
- `crates/gclient/src/app/live_reconcile.rs`
- `crates/gclient/src/app/pane.rs::Pane`
- `crates/gclient/src/app/pane.rs::Pane::new`
- `crates/gclient/src/app/live_loop/control.rs::*` — scope-reason: `send_live_input` tests `direct_input` before `daemon_ready`
- `docs/contracts/gterm-protocols.md`
- `docs/guides/gclient-user-guide.md`
- `crates/gclient/tests/loop_liveness.rs`

`observe_daemon_disconnect` (in `disconnect.rs` after R1) and the reconnect path's
pre-reconcile clear (in `live_reconcile.rs` after A5) call `Pane::clear_control` on
every pane today. New rule: a pane with `Pane::direct_input() == true` keeps
`ControlState::Held`, gets `lease_unconfirmed: bool` set on `Pane`, and shows status
text `Daemon disconnected; typing continues on the host.`; every other pane clears
as today. Keystrokes keep flowing on the bound frame stream because
`Pane::send_host_input` never consults `daemon_ready`; the early return in
`send_live_input` tests `pane.direct_input()` before `daemon_ready`. The contract's
"A surviving grant does not mean typing survives a daemon outage" paragraph is
rewritten to the new contract: a bound direct attachment keeps typing; the lease is
reconfirmed on reconnect; a take by another client during the outage is impossible
because the daemon that issues leases is the thing that is down. The guide's
status-string table and `Daemon restarts and reconnects` section follow.

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

- C1.1 - A direct-granted pane stays Held with `lease_unconfirmed` when the mock
  daemon closes the socket, and the mock frame host still receives `Input`. test:
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
- `crates/gclient/src/app/pane.rs::Pane`
- `crates/gclient/tests/loop_liveness.rs`

After the reconcile re-attaches panes (new attachment ids from the fresh daemon),
every pane with `lease_unconfirmed` issues `request_control` (take), not only the
focused one that `restore_focused` retakes today. Between the take being issued and
`granted` arriving, keystrokes go to the pane's existing pre-grant `PendingInput`
queue and flush on grant through the existing `apply_control_outcome` path; `held`
by another attachment drops the pane to Observe with take-back exactly as today.
The daemon's grant of the new attachment id replaces the old grant in gterm, so
nothing else is needed host-side. `lease_unconfirmed` clears on `granted`.

**Research context:** Observed: `restore_focused`
(`crates/gclient/src/app/live_loop/projects.rs` ~253-266) retakes only the focused
pane; `request_control`/`start_control_request` (in `live_control.rs` after A2)
issue takes; `Pane::queue_input`/`take_pending_input` (`pane.rs` ~451-468) hold
pre-grant input with `MAX_PENDING_INPUT_BYTES`; `apply_control_outcome` flushes on
grant. Planned verification: `cargo nextest run -p gobby-client --test
loop_liveness`.

**Acceptance:**

- C2.1 - On reconnect a `terminal_take_control` is recorded for every unconfirmed
  direct pane, and input queued in between reaches the host after `granted`. test:
  `crates/gclient/tests/loop_liveness.rs::an_unconfirmed_lease_is_retaken_on_reconnect_and_queued_input_flushes_on_grant`.
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
cancels every lane task and its watchdog, awaits both with
`gather(return_exceptions=True)`, and clears the lane map before connection cleanup;
no task from the dead socket survives to mutate state. The slow-handler warning in
`_handle_message` becomes "later messages in this lane waited".

Each running lane arm also owns a one-shot watchdog at the existing
`SLOW_WEBSOCKET_HANDLER_SECONDS` bound (1.0 s). If the handler is still pending, the
watchdog logs the connection id, lane key, message type, request/attachment id,
elapsed time, task name and a formatted await-chain dump: start with
`asyncio.Task.get_stack()`, then follow the task coroutine's stdlib `cr_await` /
`gi_yieldfrom` links so nested awaits such as `_notify_holder` and `_roundtrip` are
visible rather than only the outer handler frame. It observes only: it does not
cancel or retry the handler, and completion cancels and awaits the watchdog.
**Restraint rung 3:** the existing completion-only warning cannot diagnose a handler
that never returns, so use stdlib coroutine introspection and the existing threshold;
add no sampler, dependency or config knob. The dispatcher test drives a fake async websocket
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
no evidence for the 2026-09-21 never-returned-handler hypothesis; the watchdog is
the missing in-flight evidence. Golden fixtures live in
`tests/fixtures/terminal_ws_golden/`. Planned
verification: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test
GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/websocket/test_server.py
tests/servers/test_websocket_server_disconnects.py tests/servers/test_terminal_ws_golden.py
tests/servers/test_workspace_ws.py`.

**Acceptance:**

- D2.1 - Attachment lanes dispatch concurrently while workspace ops stay ordered,
  proven through the real connection loop. test:
  `tests/servers/websocket/test_server.py::test_attachment_lanes_dispatch_concurrently_while_workspace_ops_stay_ordered`.
- D2.2 - Socket close cancels and awaits every in-flight lane and watchdog, runs
  each handler's `finally`, and leaves no dispatcher task. test:
  `tests/servers/test_websocket_server_disconnects.py::test_socket_close_cancels_in_flight_lanes`.
- D2.3 - The dispatcher, its lane keys and the in-flight cap live in their own
  module. file: `src/gobby/servers/websocket/dispatch.py`.
- D2.4 - A lane that exceeds 1.0 s emits one stack dump with lane and request
identity without cancelling the handler; release before the bound emits none. test:
`tests/servers/websocket/test_server.py::test_lane_watchdog_dumps_the_stuck_task_without_cancelling_it`.

### D3 Bounded HostClient round trips [category: code] (depends: D1d)
`kind: deliverable`

Targets:
- `src/gobby/terminals/host_client.py::HostClient._roundtrip`
- `src/gobby/terminals/host_client.py::HostClient.grant_input`
- `src/gobby/terminals/host_client.py::HostClient.revoke_input`
- `src/gobby/terminals/host_client.py::HostClient.connect`
- `src/gobby/terminals/host_process.py`
- `src/gobby/config/terminal_host.py::TerminalHostConfig`
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerated derived carrier for the new config field
- `tests/terminals/test_host_client.py::*` — scope-reason: adds the deadline tests beside the round-trip tests
- `tests/config/test_terminal_host_config.py::*` — scope-reason: adds the new field's default and bounds

Consumers unchanged:
- `src/gobby/terminals/host_control.py` — no-edit-reason: calls `_roundtrip` with the default deadline.
- `tests/terminals/test_wire_golden.py` — no-edit-reason: golden wire shapes carry no deadline field.
- `src/gobby/terminals/input_grants.py` — no-edit-reason: calls `grant_input`/`revoke_input` with unchanged signatures and already maps `HostUnavailableError` to `host_input_granted=False`.
- `src/gobby/terminals/host_event_reader.py` — no-edit-reason: opens its own `HostClient` for the event stream with the default deadline.
- `tests/terminals/host_fakes.py` — no-edit-reason: fakes `grant_input`/`revoke_input` with unchanged signatures.
- `tests/agents/test_spawn_executor.py` — no-edit-reason: calls `HostClient.connect` through fakes; the deadline keyword is optional.
- `tests/e2e/test_terminal_client_stack.py` — no-edit-reason: calls `HostClient.connect` with the default deadline.
- `src/gobby/app_context.py` — no-edit-reason: reads `TerminalHostConfig`; the new field has a default.
- `src/gobby/config/app.py` — no-edit-reason: nests `TerminalHostConfig`; the new field has a default.
- `src/gobby/runner.py` — no-edit-reason: reads `TerminalHostConfig`; the new field has a default.
- `tests/config/test_terminal_host.py` — no-edit-reason: constructs `TerminalHostConfig` with defaults.
- `tests/config/test_terminals.py` — no-edit-reason: constructs `TerminalHostConfig` with defaults.
- `tests/terminals/test_host_shutdown_preservation.py` — no-edit-reason: constructs `TerminalHostConfig` with defaults.
- `tests/test_runner_lifecycle_processes.py` — no-edit-reason: constructs `TerminalHostConfig` with defaults.

`HostClient._roundtrip` gains `deadline_seconds` with a default taken from a new
`TerminalHostConfig.control_timeout_seconds` (5.0), passed at
`HostClient.connect` from the manager's connect site (in `host_process.py` after
D1d); `spawn_commit` keeps `commit_deadline_ms`; `grant_input` and `revoke_input`
pass 1.5 s so they finish inside gclient's 2 s `CONTROL_REQUEST_DEADLINE`. On
timeout an `asyncio.timeout` scope cancels the pending future (so `_begin_request`'s
existing done callback removes it from `_pending`) and raises
`HostUnavailableError("timed out")`, which `sync_host_input_grant` already maps to
`host_input_granted=False` and the client already answers with take-back. D3b
separately removes the terminal lease lock from the external wait; the timeout
remains the bound on the gterm control operation and on the per-terminal host-sync
queue. The config field is a `src/gobby/config/` change, so the runtime config
contract carrier is regenerated in the same commit. **Restraint rung 3:** use
stdlib `asyncio.timeout` around the existing future and its cancellation cleanup;
add no retry framework.

**Research context:** Observed: `HostClient._roundtrip`
(`src/gobby/terminals/host_client.py` ~317-353) does `payload = await future` with
no deadline; `grant_input` (~624-632) and `revoke_input` (~634-644) call it;
`HostClient.connect` (~170-177) is the constructor path used by the manager;
`TerminalHostConfig` (`src/gobby/config/terminal_host.py`) has
`commit_deadline_ms`, `health_interval_seconds` and no control timeout today (the
planning draft's `control_timeout_seconds = 5.0` is a new field, not an existing
one); `take_control` (`src/gobby/terminals/leases.py` ~421-455) holds `self.lock`
across `_notify_holder`; `NativeTerminalRuntime.grant_input` (~697-706) retries once
with a reconnect. Planned verification:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test
GOBBY_TEST_PROTECT=1 uv run pytest tests/terminals/test_host_client.py
tests/config/test_terminal_host_config.py tests/terminals/test_native_runtime.py
tests/servers/test_terminal_ws_lease.py`; `uv run mypy src/`.

**Acceptance:**

- D3.1 - A round trip past its deadline raises `HostUnavailableError("timed out")`
  and drops the pending future. test:
  `tests/terminals/test_host_client.py::test_roundtrip_times_out_with_host_unavailable`.
- D3.2 - Grant and revoke use a 1.5 s deadline. test:
  `tests/terminals/test_host_client.py::test_grant_and_revoke_use_the_control_deadline`.
- D3.3 - `control_timeout_seconds` defaults to 5.0 and is exported to the runtime
  config contract. test:
  `tests/config/test_terminal_host_config.py::test_control_timeout_seconds_default`.

### D3b Host grant reconciliation never holds the terminal lease lock [category: code] (depends: D2, D3)
`kind: deliverable`

Targets:
- `src/gobby/terminals/leases.py::*` — scope-reason: `HolderChange`, holder-sync cells/tasks, `_notify_holder`, `take_control`, `release_control` and `finalize` move every host observer await outside the lease lock
- `tests/terminals/test_lease_authority.py::*` — scope-reason: adds transition, race and cancellation coverage for the two lock domains
- `tests/servers/test_terminal_ws_lease.py::*` — scope-reason: adds the unanswered real HostClient grant diagnostic shared with D2

Consumers unchanged:
- `src/gobby/terminals/input_grants.py` — no-edit-reason: `sync_host_input_grant` keeps the same runtime/terminal/holder signature and error mapping.

Every holder mutation stays atomic under `lock(terminal_id)`, captures a
`HolderChange` with the resulting lease generation, then releases that lock before
starting `_notify_holder`. Host notifications serialize under a separate
per-terminal holder-sync lock. After acquiring that lock, `_notify_holder` briefly
reacquires the lease lock to compare the captured generation and holder with the
current lease, skips a stale change, releases the lease lock, and only then awaits
the gterm observer. If an older sync already owns the holder-sync lock when a newer
lease mutation commits, the newer sync queues behind it and is therefore the last
host effect; if the newer mutation wins the sync lock first, the older generation is
skipped. Repeated take, first take/takeover, release and finalize all use this one
seam; the four existing observer awaits at observed lines 437, 446, 470 and 494 are
removed from their lease-lock scopes.

The lane directly awaits `_sync_committed_holder(change)` outside the lease lock so
the D2 await-chain dump reaches `_notify_holder`, `sync_host_input_grant` and
`HostClient._roundtrip`. If lane cancellation interrupts that await, the wrapper
synchronously creates a registry-owned `reconcile_latest_holder(terminal_id)` task
before re-raising `CancelledError`; the task is kept in a strong-reference set until
its done callback removes it. It snapshots the current generation/holder under the
lease lock, releases it, then uses the same holder-sync lock to apply the latest
host state. Socket-close `finalize` therefore converges to revoke even when the
cancelled take had already written its grant. No lease mutation, write admission or
sizing decision waits on gterm. **Restraint rung 2:** reuse the registry's existing
per-terminal lock-cell pattern and holder observer; introduce one separate sync
lock plus cancellation-recovery task set because the external effect cannot safely
share the lease-state lock.

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
tests/servers/test_terminal_ws_lease.py tests/terminals/test_host_client.py`;
`uv run mypy src/`.

**Acceptance:**

- D3b.1 - The holder observer sees `lock_held(terminal_id) == false` for repeated
take, first take/takeover, release and finalize. test:
`tests/terminals/test_lease_authority.py::test_every_holder_transition_notifies_outside_the_lease_lock`.
- D3b.2 - A stalled older grant cannot overwrite a newer takeover or final revoke;
after the stall releases, the last host notification matches the latest generation.
test:
`tests/terminals/test_lease_authority.py::test_stalled_holder_sync_converges_to_the_latest_generation`.
- D3b.3 - Cancelling the websocket lane after the lease mutation does not cancel
host reconciliation, and socket-close finalize still revokes. test:
`tests/terminals/test_lease_authority.py::test_cancelled_take_keeps_committed_host_sync_and_finalize_revokes`.
- D3b.4 - An actual `HostClient` grant whose writer never receives a reply produces
the D2 watchdog stack containing `_roundtrip`, times out by 1.5 s, clears its
pending request, and does not block another connection lane or the lease-state lock.
test:
`tests/servers/test_terminal_ws_lease.py::test_unanswered_host_grant_is_diagnosed_bounded_and_lock_free`.

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

### E1 Drop the undrained host event subscription [category: code]
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

Freeze hypothesis, isolated and falsifiable: run
`tests/servers/test_terminal_ws_lease.py::test_unanswered_host_grant_is_diagnosed_bounded_and_lock_free`
with the isolated hub. Its unanswered gterm control writer must make the D2 watchdog
capture a stack containing `TerminalLeaseRegistry._notify_holder`,
`sync_host_input_grant` and `HostClient._roundtrip`, while another connection lane
and the lease-state lock still progress; D3 then ends the host wait by 1.5 s and
clears `_pending`. That result proves the proposed chain is reproducible, not that it
caused the historical 2026-09-21 freeze. A stack without that chain refutes the
hypothesis and blocks expansion until this premise is corrected from the captured
evidence. **Restraint rung 2:** use the real handler/lease/HostClient test seam and
the D2 watchdog; add no production fault-injection switch.

Clean-cutover window: send one `global` announcement, wait for no live spawned
worker or close validator, rebuild all changed native binaries, and promote any
rebuilt coherent `gcode`/`gdaemon`/`ghook` set through
`promote_workspace_binary_set`. Promote rebuilt gclient through
`install_gclient_from_submodule` (never pass gclient to the coherent-set function),
restart the Python daemon from the main checkout, then restart the validation
gclient so it execs the new inode. If implementation changed gterm, build it with
`--features vt-engine --bin gterm` and promote it through
`install_gterm_from_submodule` inside this same announced window. Read installed
identity/hash evidence from `~/.gobby/bin/`, not `target/release/`.

gclient live, the freeze: with the promoted binaries and a direct pane held, run
`kill -STOP <daemon pid>` for 5 s while typing: keystrokes appear immediately, the
status line shows `Daemon slow` then `Daemon unreachable.`, the window keeps
rendering, no pane changes state; `kill -CONT` recovers without take-back. Then run
the announced `uv run gobby restart --wait` while typing in a direct pane: typing
never stops; after reconnect the pane shows Held again with the lease reconfirmed; a
proxied pane drops to observe and comes back on take. Trigger one mock frame error
and one forced recovery give-up and retain the corresponding structured
`frame_recovery_*` records from `gclient.log`.

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

- 2026-09-21: made the split-right freeze hypothesis falsifiable; added lane stack
watchdogs, lock-free host-grant reconciliation, frame-recovery logging, and the
shared native-binary clean-cutover gate.
