Plan artifact: `.gobby/plans/gterm-host-handover.md`

# gterm Host Upgrade Without Killing Native Terminals

**Plan ID:** gterm-host-handover

## Overview
`kind: framing`

Task #22648 (gterm host upgrade without killing native terminals: detach panes
across a host replacement or snapshot them). The gterm host outlives the daemon
(#22002): an ordinary `gobby restart` or cutover adopts the running host and
every native pane survives. Replacing the gterm binary does not: the host owns
every pane's PTY master and is the parent of every shell, so the only way to run
a new gterm today is `gobby stop --terminals` or `gobby restart --terminals`,
which sends SIGHUP to every pane's process group and ends every agent and shell
inside a native terminal.

This plan upgrades the running host in place. The old host quiesces its panes,
writes a handover state file, keeps the PTY masters and listener sockets open
across `execve`, and execs the new gterm binary in the same process. The new
binary restores every pane from the state file, replays each pane's screen and
scrollback, and resumes serving the same sockets under the same `host_epoch`.
Shells and agents never see their PTY close. The daemon triggers the upgrade
when the installed gterm differs from the running host, holds its terminal rows
and client attaches steady through the handover window, and reconnects the web
terminal relay without finalizing browser attachments.

## Decision Record
`kind: framing`

Status: Writer recommendations sent to the Orchestrator for Josh on 2026-09-28
(message 02fb1eb2). Replace this line with the confirmation before approval.

1. **Mechanism: exec-in-place.** The running host process calls `execve` on the
   new gterm binary after clearing close-on-exec on each committed pane's PTY
   master fd and on both listener sockets. The PID is unchanged, so the host
   stays the parent of every pane child: `waitpid` in the restored pane runtime
   keeps working and exit statuses are not lost; the pidfile stays valid; the
   socket paths are never unlinked or rebound. Rejected alternatives:
   - **fd passing to a new host process (SCM_RIGHTS).** The new process is not
     the children's parent. `PaneRuntime::from_master_fd`
     (`crates/gterminal/src/pane/runtime.rs`) reaps with
     `libc::waitpid(pid, …)`, which returns `ECHILD` in a non-parent: the exit
     status is lost and `child_wait_completed` is set immediately, so every
     pane would report exited. Fixing that needs a reaper protocol between two
     hosts plus socket handoff and pidfile arbitration. No SCM_RIGHTS code
     exists in the crates today.
   - **Per-pane holder process.** A small process per pane owns the PTY master
     and the child; the host connects to holders. It adds a long-lived process
     and an IPC protocol per pane, and the holder binary itself can never be
     upgraded without killing its pane, which moves the problem instead of
     solving it.
   - **Snapshot and respawn.** Serializing screen state and relaunching the
     command still kills the shell or agent process and loses its in-memory
     state; it does not meet the task's goal. Snapshot is therefore rejected as
     the mechanism and adopted only as the display-state carrier (Decision 3).
2. **`host_epoch` identifies PTY-ownership continuity and is preserved.** The
   epoch is minted once when a host starts from nothing
   (`crates/gterminal/src/host/mod.rs::run`) and carried unchanged through
   every upgrade. `host_terminal_id` values and the host's `next_host_id`
   counter are carried too. Consequences: terminals rows, `locator_key`
   (`native:{epoch}:{host_terminal_id}`, `src/gobby/storage/terminals.py`),
   attach locators, and `reconcile_host_inventory` need no change. Ping gains
   `binary_version`, `binary_sha256`, and `generation` (upgrade count since the
   epoch began) for observability and the staleness check. Rejected: rotating
   the epoch per binary, which would rewrite every native row's `host_epoch`
   and `locator_key` in the same window the host is unavailable, and would
   make `handle_host_death` and `native_attach_locator` treat surviving panes
   as orphans.
3. **Display state travels as replayable VT.** ghostty terminal state is not
   serializable as memory, but its formatter
   (`ghostty_formatter_terminal_new`, wrapped by
   `Terminal::read_formatted_selection` in
   `crates/gterminal/src/ghostty/terminal_api.rs`) emits VT with extras for
   cursor, style, hyperlink, protection, kitty keyboard, charsets, palette,
   modes, scrolling region, tabstops, pwd, and keyboard modes. The old host
   formats each pane's full screen and history with every extra enabled, plus
   the parser continuation (`ghostty_terminal_continuation_alloc`, which
   requires `GHOSTTY_TERMINAL_OPT_CONTINUATION_MAX_BYTES` to be set on the
   terminal before input arrives); the new host feeds both into a fresh
   terminal of the same size. After restore every pane gets
   `nudge_child_redraw_after_handoff` (a resize bounce that sends SIGWINCH), so
   full-screen programs repaint from their own state.
4. **Alt-screen limitation is accepted.** The formatter addresses only the
   active screen (`GhosttyPointTag` values ACTIVE, VIEWPORT, SCREEN, HISTORY
   all refer to it). A pane showing the alternate screen at upgrade time
   carries its alternate screen and modes; its primary-screen scrollback
   underneath is lost, and the program repaints on SIGWINCH. Recorded as a
   documented limitation, not deferred work.
5. **Trigger: automatic, daemon-initiated.** `TerminalHostManager` compares the
   running host's `binary_sha256` (computed once at host start from its own
   executable, before any promotion can replace the path) with the installed
   `~/.gobby/bin/gterm` at adoption and on each health tick, and sends the new
   control verb `host_upgrade` when they differ. There is no new CLI verb.
   `gobby stop --terminals` and `gobby restart --terminals` keep their meaning:
   drain and end every native terminal. A host started from a binary that
   predates this plan lacks the `host_upgrade` capability; the daemon logs one
   warning naming `gobby restart --terminals` as the one-time way to move onto
   an upgradable host and never retries.
6. **Rollback is two-sided.** The gterm installer keeps the replaced binary as
   `~/.gobby/bin/gterm.previous`, and the state file names it. Before exec the
   old host runs the new binary's preflight probe
   (`gterm host --probe-resume <format_version>`); a refusal answers the verb
   with `upgrade_refused` and nothing is disturbed. If `execve` itself fails,
   the old host rolls back in process (`rollback_handoff` on every pane,
   close-on-exec restored) and keeps serving. If the new binary fails or panics
   during restore, it execs `gterm.previous` with the same state file.
7. **Herdr handoff primitives: which are used.** Exec-in-place uses the
   imported `PtyIoActor` primitives `begin_handoff` (quiesce and drain queued
   writes), `rollback_handoff` (execve failure), and
   `nudge_child_redraw_after_handoff` (post-restore repaint), through
   `PaneRuntimeIo` in `crates/gterminal/src/pane/runtime.rs`. It does not use
   `duplicate_for_handoff`, `release_after_commit`, or
   `PaneRuntime::assume_handoff_ownership`: the fds are not duplicated into
   another process, and the process that owns them never drops them.
8. **Clients reconnect through existing paths.** gclient's direct frame
   sources see EOF and run their existing recovery (detach and re-attach via
   the daemon); the daemon answers attach with the existing transient
   `host_not_ready` while the handover window is open, which gclient already
   defers and retries. The web terminal relay reconnects its host frame
   stream under the same browser `attachment_id` instead of finalizing it.

## As-Is Facts
`kind: framing`

Observed on `0.5.0` at `840e3cfdc9`:

- `crates/gterminal/src/host/mod.rs::run` binds both listener sockets with
  `tokio::net::UnixListener::bind` (close-on-exec set by default), writes the
  pidfile only after both binds succeed, mints `host_epoch` with
  `uuid::Uuid::new_v4()`, and on exit removes both socket files and the
  pidfile, then sleeps `shutdown_grace_ms`.
- Control verbs (`crates/gterminal/src/host/control.rs::dispatch`): `ping`,
  `list`, `host_shutdown`, `reserve_observer`, `release_observer`, `spawn`
  (refused `host_draining` while draining), `spawn_commit`, `kill`, `resize`,
  `write`, `write_batch`, `grant_input`, `revoke_input`, `snapshot`,
  `subscribe_events`. `PROTOCOL_VERSION` is 1; `HOST_CAPABILITIES` is
  `["terminal_theme"]`.
- `HostState::begin_shutdown` sends SIGHUP to every live native pgid and
  SIGKILL after the grace period. That is the only path that replaces a host
  today.
- Per-pane state lives in `TerminalSlot` (`crates/gterminal/src/host/state.rs`):
  identity (`terminal_id`, `spawn_key`), `host_terminal_id`, pgid, start time,
  title, size, sequence and observation fields, byte counters, observer bind,
  `input_grant` (a daemon attachment id; survives control disconnects),
  locator, and `child: Option<PreparedChild>` holding the `PaneRuntime`.
  Reservations in `Inner` are scoped to a control connection id.
- `HostEvents` (`crates/gterminal/src/host/events.rs`) keeps an epoch, a
  sequence cursor, and a bounded replay ring; `subscribe(since)` reports
  `gap: true` when the cursor is no longer replayable.
- `PaneRuntime` drop shuts the PTY I/O down and ends the pane's processes
  unless `preserve_processes_on_drop` is set; `execve` runs no destructors, so
  the fds stay open and no process is signalled.
- Daemon: `TerminalHostManager._health_loop`
  (`src/gobby/terminals/host_manager.py`) pings each interval; on failure it
  reconnects once if the pid is still a live gterm, and a failed reconnect
  calls `handle_host_death`, which marks live native rows orphaned, interrupts
  their agent runs, and calls `ensure_restart`. `_try_adopt` requires the
  pidfile, `ping.host_pid`, and `is_live_gterm` to agree
  (`src/gobby/terminals/host_identity.py::pid_matches_ping`).
- `TerminalWsMixin._resolve_attach_locator`
  (`src/gobby/servers/websocket/terminal_ws.py`) waits on
  `wait_startup_settled` and answers `host_not_ready` when the host is not
  settled. gclient's `attach_refusal_is_transient`
  (`crates/gclient/src/app/live_attach.rs`) treats `host_not_ready`,
  `host_open_timeout`, and `proxy_start_timeout` as transient and defers the
  pane's attach; frame `Eof`, `Io`, and `Protocol` errors enter
  `begin_proxy_recovery`.
- `ProxyHub._pump` (`src/gobby/servers/websocket/proxy_relay.py`) finalizes a
  browser attachment with `proxy_frame_eof` on host frame EOF and `host_loss`
  on other errors; the web reducer then marks that attachment non-live.
- Input grants apply only to direct gclient viewers
  (`src/gobby/terminals/input_grants.py::sync_host_input_grant`) and are
  re-issued by the lease path on every attach.
- gterm installs through `src/gobby/cli/install_setup_gterm.py`, outside the
  stamped coherent set; promotion replaces the name with a new inode, so a
  running host's `current_exe` path resolves to the new binary afterwards.

## Constraints
`kind: framing`

- Josh's gterm rule (memory b59e4ce9): the daemon must not gate native
  terminal operation. The daemon only decides *when* to request an upgrade;
  the handover itself runs entirely inside the host process. No daemon
  watchdog, no daemon input fallback.
- Attach locators resolve the live host epoch at call time (memory e4e0998b);
  preserving the epoch keeps that rule true across an upgrade.
- Boundary with S2.8 #21565 (`.gobby/plans/daemon-side-gterm-adoption-and-terminal-ws.md`):
  that plan ports the Python daemon side to Rust (`crates/gterminals`) using
  the current Python behavior as its parity oracle. This plan changes the
  Python behavior (upgrade trigger, handover window, relay reconnect); the
  Orchestrator records those as parity obligations on #21565. Nothing here
  edits `crates/gterminals` or the S2.8 plan.
- No backward compatibility (0.5.0 has not shipped): the state file carries a
  single `format_version` and the probe accepts exactly the versions a binary
  can restore.
- Only the Orchestrator restarts the daemon or promotes binaries; the live
  check in V1 runs in an announced window.
- Production files stay under 1,000 lines. `host_manager.py` is 913 lines, so
  2.1 moves the new logic into a new module (see 2.1).
- Input typed while the window is open is refused with a typed error, never
  queued: queued keystrokes replayed into a restored pane after an
  unpredictable delay are worse than a visible refusal.

## Failure Modes And Recovery
`kind: framing`

Each mode is owned by the deliverable that implements its recovery and is
pinned by the acceptance item named in brackets.

- **Host busy.** A reservation or uncommitted prepared spawn exists. The verb
  answers `{"ok": false, "error": "host_busy"}`; the daemon retries on its
  next health tick. [1.3.2]
- **Host draining or upgrade already running.** `host_shutdown` accepted, or a
  prior `host_upgrade` in flight. Answers `host_draining` or
  `upgrade_in_progress`; nothing changes. [1.3.2]
- **Probe refuses or times out.** The new binary does not accept the state
  format, is missing, or fails to start. Answers `upgrade_refused` with the
  probe's detail; panes are never quiesced. The daemon records the refusal for
  that installed `binary_sha256` and does not retry until the installed binary
  changes or the daemon restarts. [1.3.3, 2.1.3]
- **Pane quiesce times out.** `begin_handoff` exceeds its timeout on any pane.
  Every pane already quiesced is rolled back, the verb answers
  `upgrade_aborted`, and the host keeps serving. [1.3.4]
- **`execve` fails.** The old image is still running. It restores
  close-on-exec, rolls every pane back, removes the state file, bumps no
  generation, and emits a `host_upgrade_failed` event; the daemon sees the
  unchanged generation on reconnect and records the failure like a refusal.
  [1.3.4, 2.1.3]
- **New binary fails or panics during restore.** A restore error or a panic
  before the restore commits execs `gterm.previous` with the same argv and
  state file; the previous binary restores and emits `host_upgrade_failed`.
  [1.2.4]
- **Both binaries fail.** The process exits; the kernel closes the masters and
  the children receive SIGHUP. After the handover deadline the daemon falls
  through to the existing `handle_host_death` path, which orphans the rows,
  interrupts runs, and starts a fresh host. Logged as an upgrade failure with
  both binary identities. [2.1.2]
- **Child exits during the window.** The child stays a zombie of the same
  process; the restored runtime's `waitpid` collects the real exit status and
  the host emits `terminal_exited` as usual. [1.2.3]
- **Output during the window.** Reads are paused; the kernel PTY buffer holds
  the bytes and the restored runtime reads them. A child that fills the buffer
  blocks on write until restore, bounded by the handover deadline. [1.2.3]
- **Daemon connection drops mid-window.** The daemon keeps rows live and
  answers attaches `host_not_ready` until the host answers ping with the same
  epoch or the deadline passes. [2.1.2]
- **Daemon down during an upgrade it requested.** The host finishes the
  handover on its own; the next daemon start adopts the same epoch through the
  existing `_try_adopt`. [1.3.1]
- **Host predates this plan.** No `host_upgrade` capability; one warning, no
  retry. [2.1.4]
- **Web relay reconnect fails.** Past the deadline, or the epoch changed, the
  relay finalizes the attachment with the existing reason (`proxy_frame_eof`
  or `host_loss`). [2.2.2]

## P1: Host Handover In gterm
`kind: framing`

**Goal:** a running gterm host can replace its own binary while every
committed native pane keeps its process, PTY, identity, and display state.

### 1.1 Replayable pane display state [category: code]
`kind: deliverable`

Targets:
- `crates/gterminal/src/ghostty/terminal_api.rs::*` — scope-reason: add the full-screen handover formatter, the continuation read, and the continuation option setter to `Terminal`
- `crates/gterminal/src/pane/terminal_io.rs::*` — scope-reason: enable continuation tracking when a pane terminal is created and expose handover capture and replay on the pane terminal
- `crates/gterminal/tests/handover_display.rs`

**Research context:** ghostty terminal memory is not serializable, but the
formatter emits VT that reconstructs it. `Terminal::read_formatted_selection`
(`crates/gterminal/src/ghostty/terminal_api.rs`) already builds
`GhosttyFormatterTerminalOptions` with a selection from
`ghostty_terminal_grid_ref`; today it zeroes every extra. Add a new method
`Terminal::handover_vt` that formats the whole active screen including
history (selection from the first HISTORY point to the last ACTIVE point;
`GhosttyPointTag` values in `crates/gterminal/src/ghostty/bindings/generated_02.rs`)
with `emit` = VT, `unwrap = false`, `trim = false`, and every field of
`GhosttyFormatterTerminalExtra` and `GhosttyFormatterScreenExtra` set true
(palette, modes, scrolling region, tabstops, pwd, keyboard; cursor, style,
hyperlink, protection, kitty keyboard, charsets). Add `Terminal::continuation`
over `ghostty_terminal_continuation_alloc` (bytes that finish an unfinished
escape or UTF-8 sequence; empty at ground) and a setter for
`GHOSTTY_TERMINAL_OPT_CONTINUATION_MAX_BYTES` through `ghostty_terminal_set`.
Continuation tracking must be on before input arrives, so the pane terminal
constructor in `crates/gterminal/src/pane/terminal_io.rs` sets it (4096 bytes)
for every pane; tracking costs only the bounded suffix.

The pane terminal gains `capture_handover() -> HandoverDisplay { vt: Vec<u8>,
continuation: Vec<u8>, alt_screen: bool, cols, rows }` (new) and
`replay_handover(&HandoverDisplay)` (new), which writes `vt` then
`continuation` through the existing VT write path into a fresh terminal of the
same size and scrollback limit. `alt_screen` is read from
`GHOSTTY_TERMINAL_DATA_ACTIVE_SCREEN`; when it is ALTERNATE the captured VT is
the alternate screen and the primary scrollback is not captured (Decision 4).
Existing plain and ANSI snapshot reads (`read_ansi_screen`,
`recent_unwrapped_ansi`) are unchanged.

Verification planned: `cargo test -p gterminal --test handover_display`;
`cargo clippy -p gterminal --all-targets -- -D warnings`.

**Acceptance:**

- 1.1.1 - A pane terminal fed a mix of styled text, cursor moves, a scrolling
  region, a changed palette entry, bracketed paste mode, and more lines than
  its height replays into a fresh terminal whose screen text, history text,
  cursor position, and active modes equal the original. test:
  `crates/gterminal/tests/handover_display.rs::replay_restores_screen_history_cursor_and_modes`.
- 1.1.2 - Input split in the middle of an escape sequence and in the middle of
  a UTF-8 character replays correctly when the rest of the sequence arrives
  after restore. test:
  `crates/gterminal/tests/handover_display.rs::continuation_completes_split_sequences`.
- 1.1.3 - A terminal on the alternate screen captures and restores the
  alternate screen and reports `alt_screen = true`. test:
  `crates/gterminal/tests/handover_display.rs::alternate_screen_is_captured_as_active`.
- 1.1.4 - Every pane terminal is created with continuation tracking enabled.
  symbol: `Terminal::handover_vt`. file:
  `crates/gterminal/src/pane/terminal_io.rs`.

### 1.2 Handover state file and in-process restore [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `crates/gterminal/src/host/handover.rs`
- `crates/gterminal/src/host/mod.rs::*` — scope-reason: parse --resume-state and --probe-resume, and branch run() into restore without rebinding sockets or rewriting the pidfile
- `crates/gterminal/src/host/events.rs::*` — scope-reason: rebuild HostEvents with the carried epoch, cursor, and replay ring
- `crates/gterminal/src/host/state.rs::*` — scope-reason: construct HostState from restored slots and counters and report generation
- `crates/gterminal/src/pane/runtime.rs::*` — scope-reason: add a restore constructor from a carried master fd, child pid, and replayed terminal, and public wrappers for the handoff primitives
- `crates/gterminal/tests/host_handover.rs`
- `crates/gterminal/tests/host_cli_args.rs::*` — scope-reason: cover the --resume-state and --probe-resume arguments

**Research context:** New module `crates/gterminal/src/host/handover.rs` owns
the state format and the restore half. State file:
`<socket_dir>/gterm-handover.json`, mode 0600, written with `fsync` before
exec and removed by the restoring binary only after restore commits. JSON
(serde_json is already a dependency):

```json
{
  "format_version": 1,
  "host_epoch": "…", "generation": 3, "host_pid": 12345,
  "previous_exe": "/Users/…/.gobby/bin/gterm.previous",
  "argv": ["gterm", "host", "…original flags…"],
  "control_listener_fd": 7, "frames_listener_fd": 8,
  "next_host_id": 42, "latest_theme": {…},
  "events": {"cursor": 118, "ring": [ … ]},
  "panes": [{
    "host_terminal_id": "17", "terminal_id": "term-17", "spawn_key": "spawn-17",
    "master_fd": 23, "pid": 50211, "pgid": 50211, "start_time": 1759000000.5,
    "title": "…", "rows": 48, "cols": 160, "pixel_width": 0, "pixel_height": 0,
    "last_seq": 9031, "fingerprint": 0, "observation_state": "live",
    "observation_generation": 1, "observer_generation": 1,
    "written_bytes": 0, "dropped_bytes": 0, "total_bytes": 0, "truncated": false,
    "input_grant": "att-…", "locator": {…}, "reported_cwd": "/…",
    "kitty_keyboard_flags": 0,
    "display": {"vt_b64": "…", "continuation_b64": "…", "alt_screen": false}
  }]
}
```

Only committed native panes with a live child are carried. Attachments,
control owners, and reservations are connection-scoped and are not carried:
every connection closes at exec. `observer_bind` becomes `Entitled` for a pane
that was `Bound` (its attachment is gone), matching what a control disconnect
does today. tmux-observed slots are not native panes and are not carried; the
daemon re-observes them through the existing reconcile path.

Restore path in `run()` (`crates/gterminal/src/host/mod.rs`): with
`--resume-state <path>`, skip `prepare_socket_path`, `bind`, and
`write_pidfile`; read the state; verify `format_version`, that
`std::process::id()` equals `host_pid` and the pidfile; wrap the listener fds
with `std::os::unix::net::UnixListener::from_raw_fd`, set non-blocking, and
convert with `tokio::net::UnixListener::from_std`; set close-on-exec again on
every carried fd; rebuild `HostEvents` with the carried epoch, cursor, and
ring (new constructor; `subscribe(since)` keeps its gap rule); rebuild each
`TerminalSlot` with its carried fields and a `PreparedChild` whose runtime
comes from a new `PaneRuntime::restore` constructor. `PaneRuntime::restore`
is `PaneRuntime::from_master_fd` plus a replayed terminal (1.1) instead of an
empty one: the process is still the child's parent, so the existing
`waitpid(pid)` reaper collects real exit status. After every pane is rebuilt,
restore commits: delete the state file, start the accept loops and ticker,
then call `nudge_child_redraw_after_handoff` on every pane. Any restore error,
and any panic before commit (a panic hook installed for the restore window),
calls `execve(previous_exe, argv + ["--resume-state", path])`; the previous
binary restores from the same file. `--probe-resume <n>` prints the
supported format versions and exits 0 when `n` is supported, 3 otherwise,
without touching sockets.

Verification planned: `cargo test -p gterminal --test host_handover`;
`cargo test -p gterminal --test host_lifecycle`.

**Acceptance:**

- 1.2.1 - A state file written from a live host round-trips: restore rebuilds
  the same epoch, generation, `next_host_id`, pane identities, sizes, grants,
  locators, and event cursor. test:
  `crates/gterminal/tests/host_handover.rs::state_round_trips_host_and_pane_fields`.
- 1.2.2 - Restore adopts the carried listener fds and never unlinks, rebinds,
  or rewrites the sockets or pidfile. test:
  `crates/gterminal/tests/host_handover.rs::restore_adopts_listeners_without_rebinding`.
- 1.2.3 - A pane whose child exits while the host is between images reports
  its real exit status after restore, and output written during the window is
  delivered after restore. test:
  `crates/gterminal/tests/host_handover.rs::exit_and_output_during_window_survive`.
- 1.2.4 - A restore error and a panic before commit both exec the
  `previous_exe` named in the state file with the same state. test:
  `crates/gterminal/tests/host_handover.rs::restore_failure_execs_previous_binary`.
- 1.2.5 - `--probe-resume` accepts exactly the supported format versions. test:
  `crates/gterminal/tests/host_cli_args.rs::probe_resume_reports_supported_formats`.

### 1.3 `host_upgrade` verb, exec, and in-process rollback [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `crates/gterminal/src/host/control.rs::*` — scope-reason: add the host_upgrade verb to dispatch and host_upgrade to HOST_CAPABILITIES
- `crates/gterminal/src/host/upgrade.rs`
- `crates/gterminal/src/host/state.rs::*` — scope-reason: ping reports binary_version, binary_sha256, and generation; capture binary identity once at start
- `crates/gterminal/tests/control_protocol.rs::*` — scope-reason: cover host_upgrade refusals and the ping binary identity fields
- `crates/gterminal/tests/host_handover.rs`

**Research context:** New module `crates/gterminal/src/host/upgrade.rs`
owns the capture-and-exec half. Verb `host_upgrade` with `{"exe": <path>}`
(the daemon passes the installed gterm path). Sequence under one
`upgrading` flag on `HostState`:

1. Refuse `host_draining` when `draining` is set, `upgrade_in_progress` when
   `upgrading` is set, and `host_busy` when any reservation or uncommitted
   prepared spawn exists (`Inner::reservations`, `CommitState`). Refusals
   return immediately and change nothing.
2. Run `<exe> host --probe-resume 1` with a 5-second timeout. A non-zero exit,
   timeout, or spawn error answers `{"ok": false, "error": "upgrade_refused",
   "detail": …}`.
3. Answer `{"ok": true, "accepted": true, "deadline_ms": 10000,
   "generation": <current>}` on the control connection and flush it, then
   refuse new `spawn` and mutating verbs (`upgrading`).
4. For every carried pane call `begin_handoff` (quiesce reads and drain queued
   writes) with a 2-second timeout. A timeout rolls back every quiesced pane
   with `rollback_handoff`, clears `upgrading`, and emits
   `host_upgrade_failed{reason: "quiesce_timeout"}`.
5. Capture every pane (1.1 `capture_handover`) and the host fields (1.2 state
   format) with `generation + 1`, write and `fsync` the state file.
6. Clear close-on-exec (`fcntl(F_SETFD, 0)`) on each carried master fd and
   both listener fds, then `execve(exe, argv + ["--resume-state", path])`
   with the original argv from `std::env::args` and the current environment.
   `execve` does not return on success; all threads are replaced and every
   other fd (accepted connections, gate and status pipes) closes because it
   is close-on-exec.
7. If `execve` returns, restore close-on-exec, `rollback_handoff` every pane,
   delete the state file, clear `upgrading`, and emit
   `host_upgrade_failed{reason: "exec_failed", errno}`.

Binary identity: `HostState::new` computes `binary_sha256` of
`std::env::current_exe()` once at start and stores it with `binary_version`
(`CARGO_PKG_VERSION`); a restored host computes its own at restore. Computing
it later would read the promoted file and report a stale host as current.
`ping_json` adds `binary_version`, `binary_sha256`, and `generation`.
`HOST_CAPABILITIES` becomes `["terminal_theme", "host_upgrade"]`;
`PROTOCOL_VERSION` stays 1 (additive fields and verb).

Verification planned: `cargo test -p gterminal --test host_handover
--test control_protocol`. The end-to-end test runs a real host from the test
build, spawns `sh` panes, upgrades to the same binary path, and checks the
pids.

**Acceptance:**

- 1.3.1 - Upgrading a live host to a binary that accepts the probe keeps the
  host pid, every pane's child pid and shell, `host_epoch`, and
  `host_terminal_id`s, increments `generation`, and the panes accept input and
  produce output afterwards. test:
  `crates/gterminal/tests/host_handover.rs::upgrade_keeps_pids_epoch_and_panes`.
- 1.3.2 - The verb refuses `host_busy`, `host_draining`, and
  `upgrade_in_progress` without changing any state. test:
  `crates/gterminal/tests/control_protocol.rs::host_upgrade_refusals_change_nothing`.
- 1.3.3 - A probe that exits non-zero or times out answers `upgrade_refused`
  and no pane is quiesced. test:
  `crates/gterminal/tests/host_handover.rs::probe_refusal_leaves_panes_untouched`.
- 1.3.4 - A quiesce timeout and an `execve` failure both roll every pane back,
  keep serving on the same sockets, and emit `host_upgrade_failed`. test:
  `crates/gterminal/tests/host_handover.rs::exec_failure_rolls_back_in_process`.
- 1.3.5 - `ping` reports `binary_version`, `binary_sha256` of the executable
  the host started from, and `generation`, and `HOST_CAPABILITIES` contains
  `host_upgrade`. test:
  `crates/gterminal/tests/control_protocol.rs::ping_reports_binary_identity_and_generation`.

### 1.4 Installer keeps the previous gterm [category: code]
`kind: deliverable`

Targets:
- `src/gobby/cli/install_setup_gterm.py::*` — scope-reason: hard-link the installed gterm to gterm.previous before promoting a replacement
- `tests/cli/test_install_setup_gterm.py::*` — scope-reason: cover retention of the previous gterm on promotion

**Research context:** `src/gobby/cli/install_setup_gterm.py` installs gterm
from the workspace build or a published release, outside the stamped coherent
set, replacing the name with a new inode (copy to a dotfile, `mv -f`). After
the move the old inode has no name, so the rollback in 1.2 has nothing to
exec. Before each promotion that replaces an existing `~/.gobby/bin/gterm`,
create `~/.gobby/bin/gterm.previous` as a hard link to the current file
(replacing any older `gterm.previous` the same new-inode way); a first install
creates none. The hard link keeps the signed bytes intact, so macOS accepts
the exec. `gterm.previous` is not added to `MANAGED_NATIVE_BINARY_NAMES`
(`src/gobby/cli/install_setup.py`): it is a rollback artifact, never a
command.

Verification planned: `DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest
tests/cli/test_install_setup_gterm.py -q`.

**Acceptance:**

- 1.4.1 - Promoting over an existing gterm leaves `gterm.previous` with the
  old bytes and inode, and `gterm` with the new. test:
  `tests/cli/test_install_setup_gterm.py::test_promotion_keeps_previous_gterm`.
- 1.4.2 - A first install creates no `gterm.previous`. test:
  `tests/cli/test_install_setup_gterm.py::test_first_install_has_no_previous`.

## P2: Daemon And Client Behavior During An Upgrade
`kind: framing`

**Goal:** the daemon starts the upgrade, keeps terminal rows and agent runs
untouched through the window, and every client re-attaches to the same
terminals.

### 2.1 Upgrade trigger and handover window in TerminalHostManager [category: code] (depends: 1.3, 1.4)
`kind: deliverable`

Targets:
- `src/gobby/terminals/host_upgrade.py`
- `src/gobby/terminals/host_manager.py::*` — scope-reason: call the upgrade coordinator from adoption and the health loop, and hold host death and restart during an open handover window
- `src/gobby/terminals/host_client.py::*` — scope-reason: add the host_upgrade request and parse binary identity and generation from ping
- `src/gobby/terminals/host_control.py::*` — scope-reason: parse binary identity and generation in the handshake client's PingResult
- `tests/terminals/test_host_upgrade.py`

**Research context:** `src/gobby/terminals/host_manager.py` is 913 lines, so
the new logic goes in a new module `src/gobby/terminals/host_upgrade.py`
(split out of `host_manager.py`; the manager gains only call sites and one
window check). `HostUpgradeCoordinator` (new) holds: the installed gterm path
(the one `_spawn_host_process` launches), a cache of its `sha256` keyed by
`(st_ino, st_mtime_ns)`, a set of `binary_sha256` values that were refused or
failed, and the open window (`deadline`, `generation_at_start`).

Trigger: after `_try_adopt` succeeds and after every healthy ping in
`_health_loop`, call `coordinator.maybe_upgrade(client, ping, hello)`. It does
nothing when the host lacks the `host_upgrade` capability (log one warning
per host pid naming `gobby restart --terminals`), when `ping.binary_sha256`
equals the installed file's hash, or when the installed hash is in the
refused set. Otherwise it sends `host_upgrade{exe}`; `host_busy` returns
quietly (next tick retries); `upgrade_refused` adds the installed hash to the
refused set and logs the detail; `accepted` opens the window with the host's
`deadline_ms` plus one health interval.

Window: while a window is open, `wait_startup_settled` returns False
immediately, so `TerminalWsMixin._resolve_attach_locator` answers the
existing transient `host_not_ready` with no change of its own, a
failed ping or reconnect in `_health_loop` retries on the next tick instead of
calling `handle_host_death`, and `reconcile` is skipped. The window closes
when a ping answers with the same `host_epoch` and a `generation` greater
than `generation_at_start` (success: log both binary identities, reconcile
once, which changes no row because epoch and ids are unchanged), with the
same generation (in-process rollback: add the installed hash to the refused
set), or at the deadline (fall through to the existing `handle_host_death`).
The refused set is in memory: a daemon restart or a newly installed binary
tries again once.

Terminals rows: no schema change and no row writes. `host_epoch`,
`locator_key`, `state`, and `agent_run_id` stay as they are through a
successful upgrade; only the deadline fall-through changes rows, through the
existing `handle_host_death`.

gclient needs no code change: direct frame EOF runs `begin_proxy_recovery`;
the re-attach reaches `TerminalWsMixin._resolve_attach_locator`, which answers
`host_not_ready` while the window is open; `attach_refusal_is_transient`
defers and retries; after the window the attach resolves the same epoch and
succeeds, and the lease path re-issues the input grant. 2.3 pins this.

Consumers unchanged:
- `src/gobby/terminals/host_identity.py` — no-edit-reason: the pid, pidfile, and gterm process identity are unchanged by exec-in-place, so `pid_matches_ping` keeps passing.

Verification planned: `DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest
tests/terminals/test_host_upgrade.py tests/terminals/test_host_manager.py -q`.

**Acceptance:**

- 2.1.1 - A stale adopted host (installed hash differs from `binary_sha256`)
  receives exactly one `host_upgrade` with the installed path; a current host
  receives none. test:
  `tests/terminals/test_host_upgrade.py::test_stale_host_gets_one_upgrade_request`.
- 2.1.2 - During an open window failed pings do not call `handle_host_death`,
  attaches answer `host_not_ready`, and no terminals row changes; past the
  deadline the existing host-death path runs once. test:
  `tests/terminals/test_host_upgrade.py::test_window_holds_rows_until_deadline`.
- 2.1.3 - `upgrade_refused` and an unchanged generation after the window both
  suppress retries for that installed hash until it changes. test:
  `tests/terminals/test_host_upgrade.py::test_refused_or_rolled_back_hash_is_not_retried`.
- 2.1.4 - A host without the `host_upgrade` capability is never sent the verb
  and produces one warning naming `gobby restart --terminals`. test:
  `tests/terminals/test_host_upgrade.py::test_pre_handover_host_is_left_alone`.
- 2.1.5 - A successful upgrade closes the window on the higher generation and
  the following reconcile leaves every native row's `host_epoch`,
  `locator_key`, and `state` unchanged. test:
  `tests/terminals/test_host_upgrade.py::test_successful_upgrade_changes_no_rows`.

### 2.2 Web terminal relay reconnects across an upgrade [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/servers/websocket/proxy_relay.py::*` — scope-reason: ProxyAttachment carries a reopen callable and the host generation at open; ProxyHub._pump reconnects on host frame EOF during or after an upgrade
- `src/gobby/servers/websocket/terminal_ws.py::*` — scope-reason: _start_proxy_attach passes a reopen callable that re-resolves the locator and reopens the frame source
- `tests/servers/test_native_web_proxy.py::*` — scope-reason: cover relay reconnect and finalization across a host upgrade

**Research context:** `ProxyHub._pump`
(`src/gobby/servers/websocket/proxy_relay.py`) turns host frame EOF
(`FrameProtocolError`) into `finalize_attachment(…, "proxy_frame_eof")`, which
the browser applies as a dead attachment. `TerminalWsMixin._start_proxy_attach`
(`src/gobby/servers/websocket/terminal_ws.py`) opens the frame with
`open_proxy_frame(locator)` after `_resolve_attach_locator`, then
`start_proxy` performs `handshake` and `attach_terminal`.

`ProxyAttachment` gains `reopen: Callable[[], Awaitable[Any]] | None` and
`host_generation: int | None`, both set by `_start_proxy_attach` (the reopen
closure calls `_resolve_attach_locator(row)` and `open_proxy_frame`, then
`handshake` and `attach_terminal`, with the existing timeouts). In `_pump`, on
`FrameProtocolError`, when `record.reopen` is set and the host manager has an
open upgrade window or reports a generation different from
`record.host_generation`, wait for the window to close (bounded by its
deadline), call `reopen`, swap `record.frame`, close the old frame, emit the
native history exactly as the pump does at start (`_emit_native_history`),
and continue the loop with the same `attachment_id` and message sequence. A
reopen that fails, a changed epoch (`host_epoch_stale`), or a closed window
with an unchanged generation finalizes with today's reasons. The browser sees
a repaint, no `terminal_attachment_finalized`.

Verification planned: `DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest
tests/servers/test_native_web_proxy.py -q`.

**Acceptance:**

- 2.2.1 - Host frame EOF during an upgrade window reconnects the relay under
  the same browser `attachment_id`, replays history, continues the message
  sequence, and emits no `terminal_attachment_finalized`. test:
  `tests/servers/test_native_web_proxy.py::test_relay_reconnects_across_host_upgrade`.
- 2.2.2 - A failed reopen, a changed epoch, or EOF with no upgrade finalizes
  with the existing reasons. test:
  `tests/servers/test_native_web_proxy.py::test_relay_finalizes_when_reconnect_is_not_possible`.

### 2.3 gclient recovery across an upgrade [category: test] (depends: 2.1)
`kind: deliverable`

Targets:
- `crates/gclient/tests/host_upgrade_recovery.rs`

**Research context:** gclient's existing recovery already covers the upgrade
(Decision 8): `begin_frame_recovery` (`crates/gclient/src/app/live_attach.rs`)
sends frame `Eof` to `begin_proxy_recovery`, which detaches and re-attaches
through the daemon; `attach_refusal_is_transient` defers on
`host_not_ready`; `defer_pane_attach` retries with backoff. No test pins that
sequence against a host upgrade. Build it on the fakes in
`crates/gclient/tests/client_loop.rs` (`queue_error`, scripted daemon
replies): frame source EOF, first re-attach refused `host_not_ready`, second
re-attach succeeds with the same `frame_host_epoch`; assert the pane stays
attached to the same `terminal_id`, never retires, and shows no
`HostEpochChanged` error.

Verification planned: `cargo test -p gclient --test host_upgrade_recovery`.

**Acceptance:**

- 2.3.1 - A pane whose frame source hits EOF and whose first re-attach is
  refused `host_not_ready` re-attaches to the same terminal under the same
  epoch without retiring. test:
  `crates/gclient/tests/host_upgrade_recovery.rs::pane_reattaches_after_host_upgrade`.

### 2.4 Operator documentation [category: docs] (depends: 2.1)
`kind: deliverable`

Targets:
- `docs/guides/cli-commands.md`
- `src/gobby/install/shared/skills/gobby/references/admin/daemon.md`

**Research context:** `docs/guides/cli-commands.md` documents `gobby stop
--terminals` and `gobby restart --terminals` as the way to end native
terminals; `references/admin/daemon.md` is the operator lifecycle reference.
Neither says how a new gterm reaches a running host. Add: a newly installed
gterm is applied automatically by the daemon without ending terminals; full-
screen programs repaint once; a pane showing an alternate screen loses the
primary scrollback beneath it; input typed during the window is refused;
`--terminals` still ends every native terminal; a host started before this
change needs one `gobby restart --terminals` to become upgradable;
`~/.gobby/bin/gterm.previous` is the rollback binary.

**Acceptance:**

- 2.4.1 - The CLI guide describes automatic gterm upgrade and when
  `--terminals` is still needed. behavior: "gterm upgrade" in
  `docs/guides/cli-commands.md`.
- 2.4.2 - The daemon lifecycle reference names the rollback binary and the
  alternate-screen limitation. behavior: "gterm.previous" in
  `src/gobby/install/shared/skills/gobby/references/admin/daemon.md`.

## V1: Verification
`kind: verification`

After each leaf and before landing:

```bash
cargo test -p gterminal --test handover_display --test host_handover --test control_protocol --test host_cli_args --test host_lifecycle
cargo test -p gclient --test host_upgrade_recovery
cargo clippy -p gterminal -p gclient --all-targets -- -D warnings
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/terminals/test_host_upgrade.py tests/terminals/test_host_manager.py tests/servers/test_native_web_proxy.py tests/cli/test_install_setup_gterm.py -q
uv run ruff check src/ && uv run mypy src/
uv run gobby plans validate .gobby/plans/gterm-host-handover.md -p /Users/josh/Projects/gobby
```

Live check, in a window the Orchestrator announces: with two native panes open
(one shell running `sleep 600` and printing its pid, one running `vim`), the
Orchestrator installs a rebuilt gterm; within one health interval the daemon
log shows the upgrade with both binary identities; the gterm pid, both child
pids, `host_epoch`, and every terminals row are unchanged; the shell pane
shows its prior scrollback; `vim` repaints; gclient and a web terminal both
keep typing into the same panes. Do not run the full pytest suite.
