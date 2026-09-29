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
binary restores every pane from the state file, decodes each pane's complete
terminal snapshot (both screens and their scrollback), and resumes serving the
same sockets under the same `host_epoch`.
Shells and agents never see their PTY close. The daemon triggers the upgrade
when the installed gterm differs from the running host, holds its terminal rows
and client attaches steady through the handover window, and reconnects the web
terminal relay without finalizing browser attachments. gclient reconnects
straight to the host, so a daemon that is down during the upgrade does not
block native terminals.

## Decision Record
`kind: framing`

Status: the Orchestrator accepted Decisions 1 and 2 on 2026-09-28. Josh answered
the two product choices through the Assistant on 2026-09-28: automatic trigger
(`d3_automatic`, Decision 5) and accepting alternate-screen scrollback loss
(`d4_accept`). Adopting ghostty's snapshot codec (Decision 3) removes that
loss, so `d4_accept` is recorded and no longer needed (Decision 4). Decisions
6 and 9-13 come from the enhancement pass the Orchestrator ruled on and from
the Adversary's review. None of this is plan approval.

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
3. **Display state travels as a ghostty terminal snapshot.** The vendored
   libghostty-vt ships a snapshot codec
   (`crates/gterminal/vendor/libghostty-vt/include/ghostty/vt/snapshot.h`,
   bound in `crates/gterminal/src/ghostty/bindings/generated_08.rs`):
   `ghostty_snapshot_encode_alloc` writes a CRC-protected record stream with
   the terminal-wide state, a SCREEN and a HISTORY group for every screen, and
   any unfinished VT parser input, and the snapshot decoder returns a new
   terminal carrying all of it. The old host encodes each pane's terminal; the
   new host decodes it into the pane's terminal. The decoder's
   `GHOSTTY_SNAPSHOT_DECODER_OPT_MAX_CONTINUATION_BYTES` keeps continuation
   tracking on for the decoded terminal. Encoding an unfinished parser state
   requires tracking to have been enabled before that input arrived, so every
   pane terminal enables it at creation. After restore every pane gets
   `nudge_child_redraw_after_handoff` (a resize bounce that sends SIGWINCH),
   so full-screen programs repaint from their own state. Rejected: formatting
   the active screen as VT with every extra and replaying it
   (`ghostty_formatter_terminal_new`). The formatter addresses only the
   active screen, so it loses the primary screen under an alternate-screen
   program; it is also more code than the codec.
4. **No alternate-screen loss.** The snapshot carries both screens, so a pane
   showing an alternate-screen program keeps its primary-screen scrollback
   across an upgrade. Josh's `d4_accept` answer covered a loss that no longer
   happens; nothing is documented as a limitation.
5. **Trigger: automatic, daemon-initiated.** `TerminalHostManager` compares the
   running host's `binary_sha256` (the hash of the pinned image the host runs
   from, Decision 6) with the installed `~/.gobby/bin/gterm` at adoption and on
   each health tick, and sends the new
   control verb `host_upgrade` when they differ. There is no new CLI verb.
   `gobby stop --terminals` and `gobby restart --terminals` keep their meaning:
   drain and end every native terminal. A host started from a binary that
   predates this plan lacks the `host_upgrade` capability; the daemon logs one
   warning naming `gobby restart --terminals` as the one-time way to move onto
   an upgradable host and never retries.
6. **Hosts run from pinned, content-addressed images; rollback is one-shot.**
   Paths under `~/.gobby/bin/` are replaced by new-inode promotion at any time,
   so a path names no fixed binary. `fexecve` would pin by fd but does not
   exist on macOS (absent from the SDK's `unistd.h`). Instead every host runs
   from `<socket_dir>/gterm-images/gterm-<sha256>` (directory 0700, image
   0700): a hard link to the source file, or a copy when the link fails across
   devices, created under a temporary name, hashed, verified, and renamed into
   place. Promotion never writes a pinned name, so its bytes cannot change
   while it exists. A cold-start host whose `current_exe` is not a pinned
   image pins it and re-execs itself from the pin before binding anything,
   which makes `binary_sha256` the exact identity of the running image. An
   upgrade pins the candidate the same way and probes and execs that pin, never
   the installed path. The state file names the old host's own pin as
   `previous_image`. If `execve` itself fails, the old host rolls back in
   process and keeps serving. If the new image fails or panics during restore,
   it execs `previous_image` once with `--resume-fallback`; a fallback that
   cannot restore exits, and the daemon's existing host-death path recovers.
   Pinned images have exactly one deleter, the process that owns the sockets,
   and it deletes only at two points where no attempt is in flight:
   - a cold-start host prunes every other image only after
     `prepare_socket_path`, bind, and the pidfile write have succeeded, so a
     losing second start (the live host answers, `host_busy`) prunes nothing;
   - a restored host prunes every other image at restore commit, after it no
     longer needs `previous_image`.
   `--probe-resume` exits before pinning and never pins or prunes. A refused or
   rolled-back attempt removes the candidate pin only when its hash differs
   from the running pin, so a same-image upgrade never deletes the image the
   host runs from. This replaces an installer-kept `gterm.previous`, which a
   second promotion would overwrite under a still-running older host.
7. **Herdr handoff primitives: which are used.** Exec-in-place uses the
   imported `PtyIoActor` primitives `begin_handoff` (quiesce reads and drain
   queued writes), `rollback_handoff` (undo a quiesce and reopen user writes
   after the actor acknowledges), the actor's `initially_quiesced` start, and
   `nudge_child_redraw_after_handoff` (post-restore repaint), through
   `PaneRuntimeIo` in `crates/gterminal/src/pane/runtime.rs`. Restore adds one
   primitive, `PtyIoActor::resume_restored` (Decision 12). It does not use
   `duplicate_for_handoff`, `release_after_commit`, or
   `PaneRuntime::assume_handoff_ownership`: those need a live actor in another
   process. Restore makes its own close-on-exec duplicates with
   `fcntl(F_DUPFD_CLOEXEC)` (Decision 12).
8. **Clients reconnect without depending on the daemon.** gclient's direct
   frame source sees EOF and first reconnects straight to the host. The
   frames socket authenticates with the local token file, a user attach needs
   no daemon reservation, and the host keeps the pane's input grant across
   the exec, so gclient reattaches the same terminal and rebinds the same
   holder with no daemon involved. That host-local recovery is tracked apart
   from daemon reconnect attempts, so a daemon that keeps failing to reconnect
   cannot cancel it. It falls back to today's daemon re-attach only on an
   epoch change, `terminal_gone`, or when its reconnect budget runs out. The
   daemon still answers its own attaches with the transient `host_not_ready`
   while the window is open. The web terminal relay lives in the daemon and
   reconnects its host frame stream under the same browser `attachment_id`
   instead of finalizing it.
9. **One host-wide mutation gate, and upgrades are serialized.** `HostState`
   owns `upgrade_lock: tokio::sync::Mutex<()>` and
   `mutation_gate: tokio::sync::RwLock<()>`.
   - `host_upgrade` takes `upgrade_lock` with `try_lock` at entry (else
     `upgrade_in_progress`) and holds it until the attempt reaches exec or
     rollback has recovered.
   - Request-driven mutators take the gate with `try_read`. Failure maps to
     `host_upgrading`, so no request ever queues behind the upgrade. The
     upgrade is the only writer: it takes `try_write` in a bounded retry (at
     most 500 ms), then answers `host_busy` if a mutator still holds a read
     guard. `upgrading` is set only while the write guard is held, and the
     write guard is held until `execve` or until rollback has recovered.
   - Background mutators of carried state wait on the gate with
     `read().await` instead: the exit watcher's slot removal and
     `terminal_exited` emission, `expire_prepared` (called from the ungated
     `list`), and each tick of the status ticker in `host/mod.rs::run`, which
     rewrites slot titles through `broadcast_frames`. Each takes the gate
     before the `inner` lock. With the write guard held they cannot run, so
     slots, titles, and the event log stay frozen from quiesce through exec
     or rollback, not only during capture. These are all the producers into
     the shared event log (`native_ops` exits and `write.rs` input activity,
     which is request-driven and refused).
   - Admission separates committed panes from pending work. `host_busy` is
     answered for an unconsumed (`!prepared`) reservation or a prepared
     reservation whose slot is not `Committed`. A committed pane's prepared
     reservation is carried, not refused. Both checks run at entry and again
     under the write guard.
   - Gated with `try_read`: `spawn`, `spawn_commit`, `kill`, `resize`,
     `write`, `write_batch` (each delayed operation holds its own guard across
     its `sleep_until`), `frame_input`, `grant_input`, `revoke_input`,
     `reserve_observer`, `release_observer`, `host_shutdown`, and
     `declare_terminal_theme` (it changes the carried `latest_theme`).
   - Exempt, because attachments are not carried and close at exec:
     `set_viewport`, `set_scroll`, and `bind_attachment`. Connection-close
     cleanup is also ungated: it removes only unconsumed reservations, which
     the recheck under the write guard has proved absent, and capture itself
     turns a `Bound` observer into `Entitled`.
10. **One bounded attempt, on a clock that survives exec.** `host_upgrade`
    fixes `deadline_monotonic_ns` = `CLOCK_MONOTONIC` now + 15 s when it takes
    `upgrade_lock`, so `remaining_ms` is defined in every non-idle phase,
    `probing` included. The probe gets at most 5 s of it. The value stays
    valid across `execve` because the process is the same. The old host, the
    restored image, and a fallback image all honor it, and the daemon works
    only from the `remaining_ms` the host reports, never from wall clocks.
    - Soft bound: a soft cutoff sits 3 s before the deadline. Every pre-exec
      phase checks it and rolls back when it passes, which leaves a rollback
      reserve that covers two 1 s rollback acknowledgements and cleanup. A
      recoverable timeout is always the soft cutoff; reaching the hard
      deadline is always termination.
    - Hard bound: when it takes `upgrade_lock` the host sets SIGALRM to
      `SIG_DFL` and arms `alarm()` for the budget, rounded up to whole
      seconds, so a live host can never report a non-idle phase past its
      deadline, `probing` included. Every return to `idle` before
      acceptance records its outcome, then clears the alarm while it still
      holds `upgrade_lock`, and releases `upgrade_lock` last, so a next
      attempt can never arm an alarm that this one then cancels. The
      pending alarm survives `execve`. It is cleared with `alarm(0)` only once
      recovery is established and the attempt leaves its critical section:
      at restore commit, or, after an in-process rollback, immediately after
      the write guard is released. Every rollback operation performed while
      the gate is held, cleanup included, runs under the alarm. A wedged quiesce, capture,
      `fsync`, rollback, restore, or fallback is therefore ended by the
      default SIGALRM action, and the existing host-death recovery runs.
    - A rollback that cannot resume every pane is terminal: the host raises
      SIGALRM itself and ends exactly like a wedge. It never reopens the gate
      over panes that refuse input.
    - Arming when the attempt begins, not just before exec, is deliberate: a host wedged
      while holding the gate would refuse all input forever while the daemon
      declared it dead. Process death keeps host and daemon consistent at the
      same cost as any host crash.
    - The daemon's window is the reported `remaining_ms` plus one health
      interval, so it never declares death while the host can still commit,
      roll back, or be killed by its alarm.
11. **Reaping is frozen across the exec, and exits are reported exactly
    once.**
    - Each pane's child waiter (`crates/gterminal/src/pane/runtime.rs`)
      today blocks in `child.wait()` or `waitpid(pid, 0)` on a blocking
      thread, so it can reap a child after the snapshot and before exec,
      losing the status. The waiter becomes
      `waitid(P_PID, pid, WEXITED | WNOWAIT)` (present in the macOS SDK's
      `sys/wait.h`), which sees the exit without reaping, followed by a
      per-pane reap lock. Unfrozen, it reaps with `waitpid(pid, WNOHANG)`
      and records `ChildExit`. Frozen, it leaves the zombie in place.
    - The handover freezes every pane under that lock before capture.
      Rollback unfreezes and reaps any exit that arrived while frozen.
    - A `ChildExit` recorded before the freeze can still wake the exit
      watcher `spawn_commit` starts (`crates/gterminal/src/host/native_ops.rs`)
      at any time. The watcher takes the mutation gate with `read().await`
      (Decision 9), then the `inner` lock, then the events lock, and removes
      the slot and emits `terminal_exited` in that one critical section.
      While the upgrade holds the write guard it waits, so it cannot drop a
      carried PTY or advance the event cursor between capture and exec.
      The watcher is the only owner of a committed pane's removal:
      `expire_prepared` today also removes committed slots whose child has
      exited, without emitting `terminal_exited`, and could do so just
      before the upgrade takes the write guard, leaving the watcher pending
      with no slot and no event to carry. `expire_prepared` therefore removes
      only prepared slots past their commit deadline, and waits on the gate
      like the watcher. A pane is therefore either gone, with its event in
      the carried replay ring, or present with its recorded `exit` carried.
    - The restored image starts one watcher per carried slot, so exactly one
      `terminal_exited` with the real status crosses the boundary.
    - Windows hosts keep their current waiter and do not advertise
      `host_upgrade`.
12. **Restore is one transaction: Stage, then Commit.** The restored image
    takes no ownership of a carried descriptor, and consumes no state
    outside the checkpoint, until everything that can fail has succeeded.
    - Stage (fallible, reversible):
      - verify the state file;
      - check each carried fd with `fcntl(F_GETFD)` and `fstat`;
      - make a close-on-exec duplicate of each carried fd;
      - decode every snapshot into a new terminal and restore the pane
        wrapper state around it (1.1);
      - build every PTY actor on its duplicate master with
        `initially_quiesced`, which covers the actor's own `fcntl`, wake
        pipe, and thread creation;
      - build both listeners on duplicates;
      - build the event log, slots, and carried reservations.
    - Nothing reads, reaps, or accepts during Stage. Staged runtimes keep
      `preserve_processes_on_drop` set, so unwinding closes only
      duplicates. The originals stay open and inheritable.
    - A Stage error or panic execs `previous_image` with the unchanged state
      file and original fds. Under `--resume-fallback`, or when that exec
      fails, the process exits with status 70.
    - Commit (no fallible I/O):
      - clear `preserve_processes_on_drop`;
      - close the originals;
      - start the waiters and exit watchers;
      - resume every actor with `PtyIoActor::resume_restored`, a new actor
        primitive that enqueues a `ResumeRestored` control command and then
        reopens `user_writes.accepting`, without waiting for a reply. The
        runner must apply `ResumeRestored` before any data command queued
        after it, so no user write is written and no output is read before
        the actor is `Running`, and writes accepted after the gate reopens
        are written in order. A bare rollback command is not enough: only
        `rollback_handoff`'s caller reopens `accepting`, and only after the
        acknowledgement;
      - start the accept loops and ticker;
      - `alarm(0)`;
      - delete the state file, prune pinned images (Decision 6), and nudge
        every pane.
    - A `resume_restored` that finds the actor gone aborts the process,
      which ends in host-death recovery.
13. **The host owns the attempt record; the daemon names the attempt.** The
    daemon mints `attempt_id` and sends it with `host_upgrade`, so it can key
    its window to the attempt before any reply arrives. Ping reports
    `upgrade: {attempt_id, phase, candidate_sha256, remaining_ms,
    last_outcome}`. Values:
    - `phase` is one of `idle`, `probing`, `quiescing`, `capturing`, `exec`,
      or `rolling_back`.
    - `last_outcome` is `{attempt_id, outcome, candidate_sha256, reason}`,
      with `outcome` one of `succeeded`, `refused`, `deferred`, `aborted`,
      `rolled_back`, or `fallback`. `deferred` means the post-probe
      admission recheck failed; the candidate is not at fault.

    The state file carries the attempt, so the restored image reports
    `succeeded` and a fallback image reports `fallback`, each for the same
    `attempt_id`.
    - `candidate_sha256` is the hash of the pin the host actually probed,
      which is authoritative even when the installed file changed after the
      daemon hashed it.
    - The daemon opens a provisional window before it awaits the reply, and
      opens a window from any ping whose `phase` is not `idle` when none is
      open. That covers a lost acceptance reply, a lost connection, and a
      daemon restart mid-upgrade. A window's deadline is fixed when it
      opens; a reported `remaining_ms` can only shorten it.
    - The daemon closes the window on a `last_outcome` for its
      `attempt_id`, on a refusal reply, or at its deadline after one fresh
      check of the host (2.1).
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
  Reservations in `Inner` carry a control connection id. An unconsumed
  reservation is removed when its connection closes
  (`state.rs::on_control_disconnect` removes only `!prepared` records), but
  `spawn` marks its reservation `prepared` and `spawn_commit` leaves it in
  place, so every committed native pane keeps a prepared reservation record
  until its slot is removed (`native_ops.rs`). That record backs `list`,
  observer rebinding, and spawn idempotency.
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
  `socket_dir` defaults to `~/.gobby` (`src/gobby/config/terminal_host.py`),
  the same volume as `~/.gobby/bin/` in the default layout.
- Child reaping: `PaneRuntime::spawn_command_builder` runs `child.wait()` on a
  `spawn_blocking` thread and `PaneRuntime::from_master_fd` runs
  `libc::waitpid(pid, &mut status, 0)` in a loop, both in
  `crates/gterminal/src/pane/runtime.rs`; neither can be paused.
- Mutation ordering: `control.rs::is_mutating` covers `spawn`, `kill`,
  `resize`, `write`, and `write_batch` per control connection only.
  `HostState::frame_input` (`crates/gterminal/src/host/write.rs`) is called
  from the frames socket (`crates/gterminal/src/host/frames.rs`), which sends
  any error code back with `refuse_input`. `write_batch` schedules delayed
  operations that `sleep_until` their due time after releasing the state
  lock. A closed pane writer maps to `terminal_gone`.
- PTY actors (`crates/gterminal/src/pty/actor/unix.rs`): `PtyIoActor::spawn_inner`
  sets close-on-exec and non-blocking on the master, creates a wake pipe, and
  spawns a thread, each of which can fail; `ActorState` is `Running`,
  `Quiesced`, or `Released`, and `PtyIoActorConfig::initially_quiesced`
  starts an actor quiesced. `begin_handoff(timeout)` blocks in a
  `recv_timeout` for the actor's acknowledgement (default drain 2 s), and
  `rollback_handoff` blocks up to 1 s.
- `native_ops.rs::spawn_commit` spawns a task per pane that waits for the
  child's exit notification, removes the `TerminalSlot` (dropping its
  runtime and master), and emits `terminal_exited`.
- Frames socket (`crates/gterminal/src/host/frames.rs`): `Hello` carries the
  local token read from the token file (gclient reads it with
  `read_local_cli_token`); `AttachTerminal` without a `reservation_id` makes
  a user attachment (`embed.rs::attach_frame`); `BindAttachment` names the
  daemon attachment id the frame attachment types as, and the slot's
  `input_grant` decides whether input is accepted.
- The vendored libghostty-vt has a complete terminal snapshot codec
  (`snapshot.h`, bindings in `generated_08.rs`); encoding an unfinished
  parser state returns `GHOSTTY_INVALID_VALUE` unless continuation tracking
  was enabled before that input.
- Cargo: the terminal crate is `gobby-terminal` (default features empty; the
  `gterm` binary and the host tests require `vt-engine` and are registered
  as `[[test]]` entries in `crates/gterminal/Cargo.toml`); the client crate
  is `gobby-client`.

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
  check in V2 runs in an announced window.
- Production files stay under 1,000 lines. `host_manager.py` is 913 lines, so
  2.1 moves the new logic into a new module (see 2.1).
- Input typed while the window is open is refused with the typed error
  `host_upgrading`, never queued: queued keystrokes replayed into a restored
  pane after an unpredictable delay are worse than a visible refusal.
- The handover is Unix-only. A Windows host keeps its current behavior and
  does not advertise `host_upgrade`.
- Migration gate: the running gterm 0.1.2 host has no `host_upgrade`, and no
  zero-loss bootstrap onto an upgradable host exists. The first move onto a
  host built from this plan is `gobby restart --terminals`, which ends every
  existing native pane. It is an attended, operator-run step: the daemon
  never runs it and never kills panes to reach an upgradable host (Decision 5
  only warns). The Orchestrator presents that one-time loss to Josh through
  the Assistant before implementation lands or any cutover, and runs it only
  on his answer, in an announced window outside quiet hours. Every later
  gterm upgrade is lossless.

## Failure Modes And Recovery
`kind: framing`

Each mode is owned by the deliverable that implements its recovery and is
pinned by the acceptance item named in brackets.

- **Host busy.** An unconsumed reservation or an uncommitted prepared spawn
  exists (checked at entry and again under the write guard), or a mutator,
  including a delayed batch write, still holds the gate after the bounded
  acquisition. The verb answers `host_busy`; the daemon retries on its next
  health tick. A committed pane's prepared reservation never makes the host
  busy. [1.3.2, 1.3.6, 1.3.8]
- **Host draining, or another upgrade running.** Answers `host_draining` or
  `upgrade_in_progress`; nothing changes. A spawn, reservation, or
  `host_shutdown` that arrives during the probe is caught by the recheck
  under the write guard. [1.3.2]
- **Input or a mutating verb arrives while upgrading.** Control and frame
  callers receive `host_upgrading`; nothing is queued. [1.3.6]
- **Background mutation during the window.** A child exit recorded before the
  freeze, a `list` that would expire a slot, and a ticker tick all wait on
  the gate until exec or rollback. [1.3.9]
- **Probe refuses or times out.** Answers `upgrade_refused` with the probe's
  detail and records `last_outcome: refused`; panes are never quiesced.
  [1.3.3, 2.1.3]
- **Installed binary replaced during the upgrade.** The host probes and
  execs its pin, and reports the pin's hash as `candidate_sha256`; the daemon
  sees the newer installed hash on a later tick. [1.4.2, 2.1.6]
- **Quiesce fails or is late, or the soft cutoff passes.** Any pane whose
  `begin_handoff` fails or times out, or a phase that finds the soft cutoff
  passed, aborts the attempt; `rollback_handoff` goes to every attempted
  pane, including one whose quiesce completes late (actor commands run in
  order), and is retried once per pane. The gate reopens only after every
  pane has acknowledged and accepts input, and the alarm is cleared only
  after the gate is released. A cleanup step that blocks while the gate is
  held is ended by the alarm. [1.3.4, 1.3.12]
- **The admission recheck fails after the probe.** The attempt returns to
  `idle` with `last_outcome: deferred`; the daemon closes its window without
  refusing the candidate and retries on a later tick. [1.3.12, 2.1.9]
- **Rollback cannot resume a pane.** Terminal: the host raises SIGALRM and
  ends like a wedge; the daemon's window expires and `handle_host_death` runs
  once. [1.3.10, 2.1.2]
- **Capture or state write fails.** A snapshot encode error (including
  `GHOSTTY_INVALID_VALUE` for an unavailable continuation), a write, `fsync`,
  or rename error, or an `fcntl` error rolls back in process with the named
  reason. [1.3.4]
- **`execve` fails.** Rolls back in process, removes the state file and a
  candidate pin that differs from the running pin, and records
  `last_outcome: rolled_back`. [1.3.4, 1.4.4]
- **Restore fails during Stage.** Execs `previous_image` once with the
  unchanged state and fds; the fallback records `last_outcome: fallback`.
  [1.2.4]
- **Fallback fails, restore wedges, or the pre-exec phase wedges.** A
  fallback Stage error or failed fallback exec exits 70; a wedge is ended by
  the alarm armed when the attempt began. The kernel closes the masters and the
  children receive SIGHUP; at the daemon's deadline the existing
  `handle_host_death` path orphans the rows, interrupts runs, and starts a
  fresh host. [1.2.6, 2.1.2]
- **A second host starts, or a probe runs, beside a live host.** Neither
  prunes a pinned image; the live host's `previous_image` survives for a
  later fallback. [1.4.4, 1.3.11]
- **Child exits near the boundary.** Exactly one `terminal_exited` with the
  real status, whether the exit was delivered before capture, recorded but
  not delivered, left as a zombie by the freeze, or happened after restore.
  [1.2.3, 1.2.7, 1.3.9]
- **Output during the window.** Reads are paused; the kernel PTY buffer holds
  the bytes and the restored actor reads them. A child that fills the buffer
  blocks on write until restore, bounded by the deadline. [1.2.3]
- **Daemon misses the acceptance reply, restarts, or loses its connection.**
  The provisional window opened before the request keeps rows steady; the
  next ping reports the attempt by the daemon's `attempt_id` and the daemon
  reopens the window from `remaining_ms`; a failed reconnect inside the
  window retries instead of declaring death. [2.1.2, 2.1.6]
- **Daemon down for the whole upgrade.** The host finishes on its own;
  gclient reconnects straight to the host; the next daemon start adopts the
  same epoch and reads `last_outcome`. [1.3.1, 2.3.1]
- **Host predates this plan.** No `host_upgrade` capability; one warning, no
  retry. [2.1.4]
- **Web relay or gclient reconnect cannot complete.** On an epoch change,
  `terminal_gone`, or an exhausted budget, the relay finalizes with today's
  reasons and gclient falls back to today's daemon re-attach. [2.2.2, 2.3.2]

## P1: Host Handover In gterm
`kind: framing`

**Goal:** a running gterm host can replace its own binary while every
committed native pane keeps its process, PTY, identity, and display state.

### 1.1 Pane terminal snapshot and wrapper state encode and decode [category: code]
`kind: deliverable`

Targets:
- `crates/gterminal/src/ghostty/terminal_api.rs::*` — scope-reason: add snapshot encode, snapshot decode into a new Terminal, and the continuation-tracking option setter
- `crates/gterminal/src/pane/terminal_io.rs::*` — scope-reason: enable continuation tracking at pane terminal creation, add encode_handover, and build a pane terminal from a decoded snapshot plus carried wrapper state
- `crates/gterminal/src/pane/terminal.rs::*` — scope-reason: define the carried GhosttyPaneCore wrapper state and restore it into the core
- `crates/gterminal/src/pane/osc.rs::*` — scope-reason: make AgentOscStateTracker, DefaultColorOscTracker, and DefaultColorEventTracker state, including an unfinished OSC sequence, exportable and restorable
- `crates/gterminal/src/pane/kitty_keyboard.rs::*` — scope-reason: make KittyKeyboardTracker state exportable and restorable
- `crates/gterminal/src/pane/cursor.rs::*` — scope-reason: make DecscusrTracker state exportable and restorable
- `crates/gterminal/Cargo.toml::*` — scope-reason: register the vt-engine test target handover_display
- `crates/gterminal/tests/handover_display.rs`

**Granularity:** one leaf. The ghostty snapshot and the wrapper state around
it restore one pane terminal; either alone leaves a pane whose title, colors,
or keyboard mode disagree with its screen.

**Research context:** Decision 3. Add `Terminal::encode_snapshot() ->
Result<Vec<u8>, GhosttyError>` over `ghostty_snapshot_encode_alloc`, and
`Terminal::decode_snapshot(bytes, max_continuation_bytes) -> Result<Terminal,
GhosttyError>` over the decoder (`ghostty_snapshot_decoder_new_buf`, set
`GHOSTTY_SNAPSHOT_DECODER_OPT_MAX_CONTINUATION_BYTES` and continuation
retention, `ghostty_snapshot_decoder_decode`), both in
`crates/gterminal/src/ghostty/terminal_api.rs` with the bindings in
`crates/gterminal/src/ghostty/bindings/generated_08.rs`. Add a setter for
`GHOSTTY_TERMINAL_OPT_CONTINUATION_MAX_BYTES` through `ghostty_terminal_set`.
The pane terminal constructor in `crates/gterminal/src/pane/terminal_io.rs`
enables tracking at 4096 bytes for every pane, before any input.

The snapshot covers only the C terminal. `GhosttyPaneCore`
(`crates/gterminal/src/pane/terminal.rs`) keeps Rust state around it that
`terminal_io.rs::new` resets, and that the host reads: `terminal_title` and
`osc_progress` read `agent_osc_state`, and `native_ops::broadcast_frames`
overwrites the slot title from `runtime.osc_title`. The pane handover
therefore carries a `PaneCoreHandover` record beside the snapshot:
- carried: `host_terminal_theme` (the pane's own theme and appearance),
  `initial_default_foreground`, `initial_default_background`,
  `transient_default_color_owner_pgid`, `child_default_foreground_changed`,
  `child_default_background_changed`, `default_color_tracker`,
  `default_color_event_tracker`, `agent_osc_state` (title, latest title,
  progress, and any unfinished OSC bytes), `kitty_keyboard`, and
  `decscusr_tracker`;
- rebuilt: `render_state`, from the decoded terminal;
- reset: `osc_debug_tracker` (diagnostics only), `cursor_settle_state` (a
  transient settle timer), and the Windows-only fields (Windows hosts do not
  advertise `host_upgrade`).

The pane terminal gains `encode_handover() -> Result<(Vec<u8>,
PaneCoreHandover), …>` and a constructor `from_handover(bytes, core,
scrollback_limit)` that decodes the snapshot, restores the carried fields,
and applies the carried per-pane theme with the carried child color
ownership, so a child's OSC 10/11 override is not overwritten by the host
theme. Kitty graphics and PowerShell settings are applied as the ordinary
constructor applies them. Existing plain and ANSI snapshot reads are
unchanged. `handover_display` is added as a `[[test]]` entry with
`required-features = ["vt-engine"]` beside the existing host tests; each
later leaf registers its own test file with the file.

Verification planned: `cargo test -p gobby-terminal --features vt-engine
--test handover_display`; `cargo clippy -p gobby-terminal --features
vt-engine --all-targets -- -D warnings`.

**Acceptance:**

- 1.1.1 - A pane terminal fed styled text, cursor moves, a scrolling region,
  a changed palette entry, bracketed paste mode, a hyperlink, tab stops, and
  more lines than its height decodes into a terminal whose screen text,
  history text, cursor, and active modes equal the original. test:
  `crates/gterminal/tests/handover_display.rs::snapshot_restores_screen_history_cursor_and_modes`.
- 1.1.2 - Input split inside an escape sequence and inside a UTF-8 character
  completes correctly when the rest arrives after decode. test:
  `crates/gterminal/tests/handover_display.rs::continuation_completes_split_sequences`.
- 1.1.3 - A terminal on the alternate screen decodes with the alternate
  screen active and the primary screen's scrollback intact after the program
  leaves the alternate screen. test:
  `crates/gterminal/tests/handover_display.rs::alternate_screen_keeps_primary_history`.
- 1.1.4 - Every pane terminal is created with continuation tracking enabled,
  and encoding a pane mid-escape succeeds. symbol:
  `Terminal::encode_snapshot`. test:
  `crates/gterminal/tests/handover_display.rs::pane_terminals_track_continuation`.
- 1.1.5 - A pane with an agent OSC title and progress, a child OSC 10/11
  default-color override, a kitty keyboard mode, a cursor style, its own
  theme, and an OSC title sequence split across the encode restores all of
  them: the title and progress read the same, the split title completes, and
  a later host theme update keeps the child's override. test:
  `crates/gterminal/tests/handover_display.rs::pane_wrapper_state_round_trips`.

### 1.2 Handover state file, frozen reaping, and transactional restore [category: code] (depends: 1.1, 1.4)
`kind: deliverable`

Targets:
- `crates/gterminal/src/host/handover.rs`
- `crates/gterminal/src/host/mod.rs::*` — scope-reason: declare the handover module, parse --resume-state, --resume-fallback, and --probe-resume, and branch run() into the Stage/Commit restore without rebinding sockets or rewriting the pidfile
- `crates/gterminal/src/host/events.rs::*` — scope-reason: rebuild HostEvents with the carried epoch, cursor, and replay ring
- `crates/gterminal/src/host/state.rs::*` — scope-reason: construct HostState from staged slots, carried committed reservations, counters, and the carried attempt record
- `crates/gterminal/src/host/spawn.rs::*` — scope-reason: add a production PreparedChild constructor for a restored committed runtime with no gate writer or status reader
- `crates/gterminal/src/host/native_ops.rs::*` — scope-reason: make the exit watcher's slot removal and terminal_exited emission one critical section and the only removal path for a committed pane, restrict expire_prepared to prepared slots past their deadline, and start watchers for restored slots
- `crates/gterminal/src/pane/runtime.rs::*` — scope-reason: add the staged restore constructor on a duplicate master with a quiesced actor, carried exit, and decoded terminal; replace both child waiters with the waitid(WNOWAIT) waiter and per-pane reap lock; freeze/unfreeze and handoff wrappers
- `crates/gterminal/src/pty/actor/unix.rs::*` — scope-reason: add the resume_restored primitive and its ResumeRestored control command, which move the actor to Running and reopen user writes together without waiting
- `crates/gterminal/Cargo.toml::*` — scope-reason: register the vt-engine test target host_handover
- `crates/gterminal/tests/host_handover.rs`
- `crates/gterminal/tests/host_cli_args.rs::*` — scope-reason: cover --resume-state, --resume-fallback, and --probe-resume

**Granularity:** one leaf. The state format, the Stage/Commit restore, and the
reaping freeze are one contract: the format is only testable through
restore, and the freeze exists only so restore can report exits exactly once.
Splitting them leaves a restore that loses exits or a freeze with no reader.

**Research context:** New module `crates/gterminal/src/host/handover.rs`
owns the state format and restore, declared in `host/mod.rs`. State file:
`<socket_dir>/gterm-handover.json`, mode 0600, written to a temporary name,
`fsync`ed, renamed, and the directory `fsync`ed before exec; removed only at
restore commit. JSON (serde_json is already a dependency):

```json
{
  "format_version": 1,
  "host_epoch": "…", "generation": 3, "host_pid": 12345,
  "deadline_monotonic_ns": 81234567890123,
  "attempt": {"attempt_id": "…", "candidate_sha256": "…", "previous_sha256": "…"},
  "previous_image": "/Users/…/.gobby/gterm-images/gterm-<sha256>",
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
    "reservation": {"id": "…", "key": "…", "generation": 1, "terminal_id": "term-17", "identity": {…}},
    "exit": null,
    "snapshot_b64": "…",
    "core": {…}
  }]
}
```

Only committed native panes still present at capture are carried; `exit` is
the `ChildExit` (`{"exit_code", "signal"}`) recorded but not yet delivered,
else null; `core` is the 1.1 `PaneCoreHandover`. Each committed pane's
prepared reservation is carried without its connection id and restored as a
prepared record owned by no live connection, so `list`, observer rebinding,
and spawn idempotency (`fingerprint`) behave as before the upgrade.
Attachments, control owners, and unconsumed reservations are connection-scoped
and are not carried: every connection closes at exec, and admission (1.3)
guarantees no unconsumed reservation exists. An observer bind that was
`Bound` is carried as `Entitled`. tmux-observed slots are not native panes
and are not carried; the daemon re-observes them through the existing
reconcile path.

Reaping (Decision 11): both waiters in `crates/gterminal/src/pane/runtime.rs`
become one Unix waiter, `waitid(P_PID, pid, WEXITED | WNOWAIT)` on a blocking
thread, then the pane's reap lock: unfrozen, reap with `waitpid(pid,
WNOHANG)` and record `ChildExit`; frozen, set `exit_pending` and return.
`PaneRuntime::freeze_reaping` takes the lock and sets frozen;
`unfreeze_reaping` clears it and reaps when `exit_pending` is set. The exit
watcher in `native_ops.rs::spawn_commit` performs slot removal and the
`terminal_exited` emission under the `inner` lock and then the events lock,
the order capture uses (1.3 adds the gate wait in front), and it is the only
path that removes a committed pane: `expire_prepared` keeps its
prepared-deadline expiry and drops its committed-exit branch, which removed
the slot without emitting. The Windows waiter is unchanged.

Restored slots: `TerminalSlot.child` holds a `PreparedChild`, whose
`gate_writer` and `status_reader` are private and whose only runtime-only
constructor is `#[cfg(test)]` (`host/spawn.rs`). Add a production
`PreparedChild::from_restored(runtime, pid, pgid, start_time)` with both
fields `None`, the state a committed child reaches after `spawn_commit`.

Resume primitive (Decision 12): `PtyIoActor::resume_restored` in
`crates/gterminal/src/pty/actor/unix.rs` sends
`PtyIoControlCommand::ResumeRestored` and then sets `user_writes.accepting =
true`, returning an error only when the control channel is closed. The
runner handles `ResumeRestored` like a successful rollback (state
`Running`, reads resume) and needs no reply. User writes travel on the
separate data channel, so the runner must apply a pending `ResumeRestored`
before any data command queued after it; the leaf enforces that order in the
runner loop and 1.2.8 pins it.

Restore (Decision 12), in `run()` with `--resume-state <path>`: skip
`prepare_socket_path`, `bind`, `write_pidfile`, and cold-start pinning.
Stage:
1. Read the state file. Verify `format_version`, that `std::process::id()`
   equals `host_pid` and the pidfile, and that the deadline has not passed.
2. For every carried fd, check `fcntl(F_GETFD)` and `fstat` (socket for
   listeners, character device for masters) and take a
   `fcntl(F_DUPFD_CLOEXEC)` duplicate. The original is left untouched.
3. Decode each snapshot and its wrapper state (1.1 `from_handover`).
4. Build each `PaneRuntime` with the new `PaneRuntime::stage_restore`: its
   actor runs on the duplicate master with `initially_quiesced`,
   `preserve_processes_on_drop` is set, and no waiter or reader starts.
5. Build both listeners from their duplicates
   (`std::os::unix::net::UnixListener::from_raw_fd`, non-blocking,
   `tokio::net::UnixListener::from_std`).
6. Rebuild `HostEvents` with the carried epoch, cursor, and ring (new
   constructor; `subscribe(since)` keeps its gap rule), every
   `TerminalSlot` with `PreparedChild::from_restored`, and every carried
   reservation.

A panic hook installed for Stage and any Stage error call
`execve(previous_image, argv + ["--resume-state", path,
"--resume-fallback"])` with the original fds untouched. Under
`--resume-fallback`, a Stage error exits with status 70, and so does a failed
fallback `execve`.

Commit:
1. Disarm the panic hook and clear `preserve_processes_on_drop`.
2. Close the original fds.
3. Start one waiter and one exit watcher per slot. A slot with a carried
   `exit` records it and its watcher emits `terminal_exited` once without
   waiting.
4. Call `resume_restored` on every actor.
5. Start the accept loops and the ticker.
6. `alarm(0)`.
7. Set the attempt's `last_outcome` to `succeeded`, or to `fallback` under
   `--resume-fallback`.
8. Delete the state file, prune pinned images (1.4), and call
   `nudge_child_redraw_after_handoff` on every pane.

A `resume_restored` error (the actor is gone) aborts the process.

`--probe-resume <n>` is parsed before cold-start pinning; it prints the
supported format versions and exits 0 when `n` is supported, 3 otherwise,
without pinning, pruning, or touching sockets.

Verification planned: `cargo test -p gobby-terminal --features vt-engine
--test host_handover --test host_cli_args --test host_lifecycle`.

**Acceptance:**

- 1.2.1 - A state file written from a live host round-trips: restore rebuilds
  the same epoch, generation, attempt, `next_host_id`, pane identities, sizes,
  grants, locators, committed reservations, wrapper state, and event cursor.
  test:
  `crates/gterminal/tests/host_handover.rs::state_round_trips_host_and_pane_fields`.
- 1.2.2 - Restore adopts the carried listener fds and never unlinks, rebinds,
  or rewrites the sockets or pidfile. test:
  `crates/gterminal/tests/host_handover.rs::restore_adopts_listeners_without_rebinding`.
- 1.2.3 - A child that exits and is delivered before capture, one recorded but
  not delivered, one that exits after the freeze and before exec, and one that
  exits after restore each produce exactly one `terminal_exited` with the real
  status, and output written during the window is delivered after restore.
  test: `crates/gterminal/tests/host_handover.rs::exit_and_output_during_window_survive`.
- 1.2.4 - A decode error, a panic, and an injected actor-construction failure,
  each after at least one pane is staged, exec `previous_image` once with
  `--resume-fallback`; every child pid, queued output, real exit status, and
  both listener fds survive into the fallback, and every pane accepts a
  write and returns its output after the fallback commits. test:
  `crates/gterminal/tests/host_handover.rs::stage_failure_falls_back_with_checkpoint_intact`.
- 1.2.5 - `--probe-resume` accepts exactly the supported format versions and
  leaves the pinned image directory untouched. test:
  `crates/gterminal/tests/host_cli_args.rs::probe_resume_reports_supported_formats`.
- 1.2.6 - A fallback Stage error and a failed fallback `execve` both exit with
  status 70, and a restore that wedges is ended by the pending alarm before
  the daemon's deadline. test:
  `crates/gterminal/tests/host_handover.rs::fallback_failure_and_wedged_restore_end_the_process`.
- 1.2.7 - A rollback that unfreezes a pane whose child exited while frozen
  reaps it and emits one `terminal_exited` with the real status. symbol:
  `PaneRuntime::unfreeze_reaping`. test:
  `crates/gterminal/tests/host_handover.rs::rollback_reaps_exit_seen_while_frozen`.
- 1.2.8 - After a restore commits, every pane accepts a user write and its
  output reaches the terminal; a write sent while the actor is still
  quiesced is refused, and one sent right after `resume_restored` is written
  once, in order. symbol: `PtyIoActor::resume_restored`. test:
  `crates/gterminal/tests/host_handover.rs::restored_panes_accept_input_after_commit`.
- 1.2.9 - After restore, a pane's agent title survives the first ticker
  broadcast, two panes keep their distinct themes, a child OSC 10/11
  override survives a host theme refresh, and a committed observer
  entitlement rebinds. test:
  `crates/gterminal/tests/host_handover.rs::restored_panes_keep_wrapper_state_and_entitlements`.

### 1.3 `host_upgrade` verb, mutation gate, bounded capture, exec, and in-process rollback [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `crates/gterminal/src/host/control.rs::*` — scope-reason: add the host_upgrade verb with the daemon's attempt_id, host_upgrade in HOST_CAPABILITIES on Unix, generation and the upgrade record in ping, and host_upgrading refusals for gated verbs
- `crates/gterminal/src/host/upgrade.rs`
- `crates/gterminal/src/host/mod.rs::*` — scope-reason: declare the upgrade module, and make each status-ticker tick in run() wait on the mutation gate
- `crates/gterminal/src/host/state.rs::*` — scope-reason: own upgrade_lock, the mutation gate, upgrading, and the attempt record; admission that separates committed reservations from pending ones; spawn, reserve_observer, release_observer, and host_shutdown take the gate
- `crates/gterminal/src/host/write.rs::*` — scope-reason: write, write_batch with a guard per delayed operation, frame_input, grant_input, and revoke_input take the gate and refuse host_upgrading
- `crates/gterminal/src/host/native_ops.rs::*` — scope-reason: spawn_commit, kill, and resize take the gate and refuse host_upgrading; the exit watcher and expire_prepared wait on the gate
- `crates/gterminal/src/host/theme.rs::*` — scope-reason: declare_terminal_theme takes the gate and refuses host_upgrading
- `crates/gterminal/tests/control_protocol.rs::*` — scope-reason: cover host_upgrade refusals, host_upgrading refusals, and the upgrade ping field
- `crates/gterminal/tests/host_handover.rs`

**Granularity:** one leaf. The verb, the gate, and the capture/exec sequence
are one admission contract: the verb is unsafe without the gate, and the gate
has no user without the verb. Every acceptance item drives the same verb.

**Research context:** New module `crates/gterminal/src/host/upgrade.rs`,
declared in `host/mod.rs`, owns the attempt. Verb `host_upgrade` with
`{"exe": <path>, "attempt_id": <daemon-minted id>}`:

1. `upgrade_lock.try_lock()`, else `upgrade_in_progress`. Refuse
   `host_draining`, and refuse `host_busy` when an unconsumed (`!prepared`)
   reservation exists or a prepared reservation's slot is not `Committed`
   (`Inner::reservations`, `CommitState`). Fix `deadline_monotonic_ns` (now +
   15 s, Decision 10), set SIGALRM to `SIG_DFL`, arm `alarm()` for the
   budget, and set phase `probing` with the daemon's `attempt_id`. Every
   later return to `idle` before acceptance (steps 2 and 3) does its
   cleanup, records its outcome, calls `alarm(0)` while `upgrade_lock` is
   still held, and releases `upgrade_lock` last.
2. Pin the candidate (1.4 `pin_image(exe)`) and run `<pin> host
   --probe-resume 1` with a timeout of 5 s or the time left before the soft
   cutoff, whichever is smaller. A pin failure, non-zero exit, timeout, or spawn
   error answers `{"ok": false, "error": "upgrade_refused", "detail": …}`,
   removes the candidate pin when its hash differs from the running pin,
   records `last_outcome: refused`, calls `alarm(0)`, and then releases
   `upgrade_lock`.
3. Take the gate's write guard with `try_write` in a retry of at most 500 ms,
   else `host_busy`. Holding it, recheck every predicate from step 1. A
   `try_write` timeout or a failed recheck answers `host_busy` or
   `host_draining`, returns the phase to `idle`, records `last_outcome:
   deferred` with that reason, removes the candidate pin when its hash
   differs from the running pin, calls `alarm(0)`, and then releases
   `upgrade_lock`: every return
   after phase `probing` is set leaves a terminal record, so no leftover
   in-progress record can reopen a daemon window. Otherwise set
   `upgrading`, and answer `{"ok": true, "accepted": true, "attempt_id",
   "candidate_sha256", "remaining_ms", "generation"}` on the control
   connection.
4. Phase `quiescing`. The status ticker, the exit watchers, and
   `expire_prepared` now wait on the gate (Decision 9). On a blocking task,
   give each carried pane its own thread (`std::thread::scope`) that calls
   `freeze_reaping` then `begin_handoff(quiesce_budget)`, and join all.
   `quiesce_budget` is the time left until the soft cutoff minus 1 s,
   because `begin_handoff` itself calls `rollback_handoff` (up to 1 s) when
   its wait times out (`pty/actor/unix.rs`). A pane whose budget is zero or
   less is not quiesced and the attempt rolls back. The worst case therefore
   finishes the quiesce, including its internal rollback, by the soft
   cutoff, leaving the 3 s reserve for step 7 (two 1 s acknowledgements and
   cleanup).
5. Phase `capturing`. Under the `inner` lock then the events lock, encode
   every pane's snapshot and wrapper state (1.1), read each recorded `exit`
   and prepared reservation, and build the state (1.2 format) with
   `generation + 1`, the attempt, and `previous_image` = the running pin.
   Check the soft cutoff, write the temporary file, `fsync`, rename, and
   `fsync` the directory, and check the soft cutoff again before exec.
6. Phase `exec`. If the soft cutoff has passed, roll back instead: no
   `execve` runs after it. Otherwise clear close-on-exec (`fcntl(F_SETFD, 0)`) on each carried
   master fd and both listener fds, then `execve(<pin>, argv +
   ["--resume-state", path])` with the original argv and environment.
   `execve` does not return on success; accepted connections and other fds
   close because they are close-on-exec.
7. Rollback, on any failure in steps 4-6, phase `rolling_back`, with the
   alarm still armed: restore close-on-exec; send `rollback_handoff` to every
   attempted pane, one thread each, retrying once after a timeout (a
   `RollbackHandoff` to an actor already `Running` acknowledges `Ok`, so a
   late first acknowledgement is harmless); call `unfreeze_reaping`.
   - When every pane's `rollback_handoff` returned `Ok`, each actor is
     `Running` and accepts user writes. Then, still under the alarm and the
     write guard: delete the state file and the candidate pin when its hash
     differs from the running pin; record `last_outcome` (`aborted` with
     `quiesce_timeout`, `soft_deadline`, `capture_failed`, or
     `state_write_failed`, or `rolled_back` with `exec_failed` and errno);
     emit `host_upgrade_failed` with the reason; clear `upgrading`; release
     the write guard; then `alarm(0)`; then release `upgrade_lock`.
   - When any pane's rollback failed twice, the attempt is terminal: log the
     panes and `libc::raise(SIGALRM)`. The host ends like a wedge and never
     reopens the gate over a pane that refuses input.

Mutation gate (Decision 9): every request-driven gated verb calls
`mutation_gate.try_read()` and answers `host_upgrading` on failure or when
`upgrading` is set; the guard is held until the verb returns, and each
delayed `write_batch` operation takes its own guard when scheduled and holds
it across its `sleep_until`. The exit watcher, `expire_prepared`, and each
ticker tick in `host/mod.rs::run` take `mutation_gate.read().await` before the
`inner` lock. `frames.rs` forwards `frame_input`'s error code through
`refuse_input`, so the frame client sees `host_upgrading` with no change
there.

Ping: `ping_json` adds `generation` and the `upgrade` record (Decision 13);
`binary_version` and `binary_sha256` come from 1.4. `HOST_CAPABILITIES`
becomes `["terminal_theme", "host_upgrade"]` on Unix; `PROTOCOL_VERSION`
stays 1.

Verification planned: `cargo test -p gobby-terminal --features vt-engine
--test host_handover --test control_protocol`. The end-to-end test runs a
real host from the test build, spawns `sh` panes through the real
reserve, `spawn`, and `spawn_commit` control path, upgrades to the same
binary, and checks the pids; the timing test opens 16 panes filled to the
scrollback limit and asserts the upgrade commits inside the deadline.

**Acceptance:**

- 1.3.1 - Upgrading a live host keeps the host pid, every pane's child pid,
  `host_epoch`, and `host_terminal_id`s, increments `generation`, reports
  `last_outcome: succeeded` for the daemon's `attempt_id`, and the panes
  accept input and produce output afterwards. test:
  `crates/gterminal/tests/host_handover.rs::upgrade_keeps_pids_epoch_and_panes`.
- 1.3.2 - The verb refuses `host_busy`, `host_draining`, and
  `upgrade_in_progress` without changing state; two simultaneous upgrades
  yield one attempt; a spawn, reservation, or `host_shutdown` issued during
  the probe is caught by the recheck. test:
  `crates/gterminal/tests/control_protocol.rs::host_upgrade_admission_is_serialized_and_rechecked`.
- 1.3.3 - A probe that exits non-zero or times out answers `upgrade_refused`,
  records `last_outcome: refused`, and quiesces no pane. test:
  `crates/gterminal/tests/host_handover.rs::probe_refusal_leaves_panes_untouched`.
- 1.3.4 - A quiesce timeout, a late quiesce completion, a first rollback
  timeout followed by a late acknowledgement, a snapshot encode error, a
  write or `fsync` error, the soft cutoff passed during capture, and an
  `execve` failure each roll back, keep serving on the same sockets, record
  the matching `last_outcome`, clear the alarm, and reopen the gate only
  after every pane accepts a write and returns its output. test:
  `crates/gterminal/tests/host_handover.rs::every_pre_exec_failure_rolls_back`.
- 1.3.5 - `ping` reports `generation` and the `upgrade` record, with
  `remaining_ms` defined in `probing`, and `HOST_CAPABILITIES` contains
  `host_upgrade`. test:
  `crates/gterminal/tests/control_protocol.rs::ping_reports_generation_and_attempt`.
- 1.3.6 - A `write_batch` with a pending delayed operation makes `host_upgrade`
  answer `host_busy`; after acceptance, control writes, frame input, and theme
  declarations receive `host_upgrading`, and no refused input is written after
  restore. test:
  `crates/gterminal/tests/control_protocol.rs::mutation_gate_blocks_and_refuses_during_upgrade`.
- 1.3.7 - Sixteen panes at the scrollback limit upgrade inside the deadline.
  test: `crates/gterminal/tests/host_handover.rs::many_full_panes_upgrade_within_deadline`.
- 1.3.8 - A pane created through reserve, `spawn`, and `spawn_commit`, kept
  live, and a second one whose creating connection has closed both upgrade
  successfully; a pending unconsumed reservation still yields `host_busy`.
  test:
  `crates/gterminal/tests/host_handover.rs::committed_panes_are_admitted_and_pending_reservations_are_busy`.
- 1.3.9 - A child exit recorded before the freeze whose watcher is released
  after capture and before exec, a `list` that runs just before the write
  guard is taken while that watcher is pending, a `list` call during the
  window, and a ticker tick during the window neither remove a carried slot
  without its event, close its master, nor advance the event cursor before
  exec; a subscriber resuming from the carried cursor sees exactly one
  `terminal_exited`. test:
  `crates/gterminal/tests/host_handover.rs::background_mutators_wait_through_exec`.
- 1.3.10 - A pane whose rollback fails twice ends the host through SIGALRM
  without reopening the gate. test:
  `crates/gterminal/tests/host_handover.rs::persistent_rollback_failure_ends_the_host`.
- 1.3.11 - After a live host has seen a losing cold start, a probe, and a
  same-image refusal and rollback, a later upgrade whose restore fails still
  finds `previous_image` and falls back. test:
  `crates/gterminal/tests/host_handover.rs::fallback_image_survives_other_starts_and_same_image_attempts`.
- 1.3.12 - A `try_write` timeout and a recheck that finds a new pending
  reservation or draining after the probe each return the attempt to `idle`
  with `last_outcome: deferred`; an attempt that starts right after a
  refused or deferred one keeps its own alarm armed (the earlier attempt's
  `alarm(0)` runs before it can take `upgrade_lock`); a rollback whose
  cleanup blocks while the
  write guard is held is ended by the alarm; and after a recovered rollback
  the alarm is cleared only once the write guard is released. test:
  `crates/gterminal/tests/host_handover.rs::pre_accept_returns_and_rollback_cleanup_are_bounded`.
- 1.3.13 - A pane whose quiesce acknowledgement never arrives makes the
  attempt give up on it before the soft cutoff, run no `execve`, and recover
  every pane (writes accepted, output returned, alarm cleared) before the
  hard deadline. test:
  `crates/gterminal/tests/host_handover.rs::stalled_quiesce_recovers_within_hard_budget`.

### 1.4 Pinned host images and binary identity [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `crates/gterminal/src/host/image.rs`
- `crates/gterminal/src/host/mod.rs::*` — scope-reason: declare the image module, pin current_exe and re-exec from the pin at cold start before binding anything, and prune only after this process owns the sockets
- `crates/gterminal/src/host/state.rs::*` — scope-reason: record the running pin's binary_sha256 and binary_version
- `crates/gterminal/src/host/control.rs::*` — scope-reason: report binary_version and binary_sha256 in ping
- `crates/gterminal/Cargo.toml::*` — scope-reason: register the vt-engine test target host_image
- `crates/gterminal/tests/host_image.rs`

**Research context:** New module `crates/gterminal/src/host/image.rs`
(Decision 6), declared in `host/mod.rs`. `pin_image(src) -> PinnedImage {
path, sha256 }`:
- Create `<socket_dir>/gterm-images/` with mode 0700 if it is missing.
  Refuse a directory not owned by the current user or writable by others.
- Hard-link `src` to a temporary name there (`std::fs::hard_link`). When the
  link fails with a cross-device or unsupported error, fall back to
  `std::fs::copy`.
- Hash the temporary file with SHA-256, set mode 0700, and rename it to
  `gterm-<sha256>`.
- When that name already exists, verify its hash and mode and reuse it. A
  mismatch replaces it through the same temporary-name rename.

Other functions:
- `PinnedImage::verify()` re-hashes the pin before any exec and refuses a
  mismatch.
- `is_pinned(path)` is true for a path directly under that directory whose
  name matches its content hash.
- `prune_images(keep)` removes every other `gterm-*` entry. Its only
  callers are the two owner points in Decision 6.
- `remove_candidate(candidate, running)` removes the candidate pin only when
  the hashes differ.

Cold start in `run()` (`crates/gterminal/src/host/mod.rs`): before
`prepare_socket_path`, when
`std::env::current_exe()` is not pinned, pin it and `execve` the pin with the
same argv and environment. A pinned host records its own `PinnedImage` for
`binary_sha256` and `previous_image` (1.2). Only after `prepare_socket_path`,
bind, and the pidfile write succeed does it call `prune_images` with its own
pin; a start that loses to a live host exits without pruning. The daemon
keeps launching the installed path; the host pins itself. A pin failure at
cold start is fatal with the error named, like a bind failure.

Binary identity: `HostState::new` records the pin as `binary_sha256` with
`binary_version` (`CARGO_PKG_VERSION`), and `ping_json` reports both.

Verification planned: `cargo test -p gobby-terminal --features vt-engine
--test host_image`.

**Acceptance:**

- 1.4.1 - `pin_image` creates a 0700 image under a 0700 user-owned directory
  whose name is its SHA-256, reuses a matching existing pin, replaces a
  mismatched one, falls back to copy across devices, and refuses an unsafe
  directory. test:
  `crates/gterminal/tests/host_image.rs::pin_image_is_content_addressed_and_private`.
- 1.4.2 - Replacing the source path by rename after pinning leaves the pin's
  bytes and hash unchanged, and two promotions in a row leave both earlier
  pins intact until pruned. test:
  `crates/gterminal/tests/host_image.rs::pins_survive_promotion_of_the_source`.
- 1.4.3 - A cold-start host launched from an unpinned path re-execs from its
  pin before binding, and `ping.binary_sha256` equals the pin's hash. test:
  `crates/gterminal/tests/host_image.rs::cold_start_runs_from_pin`.
- 1.4.4 - With a live host A, a second cold start B that loses the socket
  check leaves A's pin and every other pin in place, and `remove_candidate`
  keeps a candidate equal to the running pin. The probe's side of this is
  1.2.5. test:
  `crates/gterminal/tests/host_image.rs::only_the_socket_owner_prunes`.

## P2: Daemon And Client Behavior During An Upgrade
`kind: framing`

**Goal:** the daemon starts the upgrade, keeps terminal rows and agent runs
untouched through the window, and every client re-attaches to the same
terminals.

### 2.1 Upgrade trigger and handover window in TerminalHostManager [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `src/gobby/terminals/host_upgrade.py`
- `src/gobby/terminals/host_manager.py::*` — scope-reason: call the upgrade coordinator from adoption and the health loop, and hold host death on both the failed-ping and failed-reconnect paths, restart, and reconcile during an open window
- `src/gobby/terminals/host_client.py::*` — scope-reason: add the host_upgrade request with the daemon-minted attempt_id and parse binary identity, generation, and the upgrade record from ping
- `src/gobby/terminals/host_control.py::*` — scope-reason: parse binary identity, generation, and the upgrade record in the handshake client's PingResult
- `tests/terminals/test_host_upgrade.py`

**Granularity:** one leaf. The trigger, the window, and its close rules are
one contract over the host's attempt record: every acceptance item drives
`HostUpgradeCoordinator` through the same health-loop call sites, and a
trigger without the window would let the health loop kill panes mid-upgrade.

**Research context:** `src/gobby/terminals/host_manager.py` is 913 lines, so
the new logic goes in a new module `src/gobby/terminals/host_upgrade.py`
(split out of `src/gobby/terminals/host_manager.py`; the manager gains only
call sites and one window check). `HostUpgradeCoordinator`
(new) holds:
- the installed gterm path (the one `_spawn_host_process` launches);
- a cache of the installed file's `sha256`, keyed by `(st_ino, st_mtime_ns)`;
- a refused set of candidate hashes;
- the open window: `attempt_id`, the `sha_at_start` it saw, a
  `time.monotonic()` deadline, and whether the host has confirmed the
  attempt.

Trigger: after `_try_adopt` succeeds and after every healthy ping in
`_health_loop`, call `coordinator.observe(client, ping, hello)`. Cases, in
order:
- The host lacks the `host_upgrade` capability: log one warning per host pid
  naming `gobby restart --terminals`, and nothing more.
- `ping.upgrade.phase` is not `idle`: open the window for that
  `attempt_id` if none is open (a daemon that restarted mid-upgrade), with
  deadline = now + min(`remaining_ms`, 15 s) + one health interval, or
  confirm the open one. A window's deadline is fixed when it opens: a later
  `remaining_ms` from a ping or the acceptance reply can only shorten it, to
  now + `remaining_ms` + one health interval, and never extends it, so
  in-progress pings reporting `remaining_ms = 0` cannot keep a window open.
- A window is open: evaluate its close rules below.
- `ping.binary_sha256` equals the installed hash, or the installed hash is in
  the refused set: do nothing.
- Otherwise mint `attempt_id` (`uuid4().hex`) and open a provisional window
  for it before sending anything, with deadline = now + the request timeout
  + the host's 15 s budget + one health interval. That deadline bounds any
  attempt the host could accept from this request. Then send
  `host_upgrade{exe, attempt_id}`:
  - `host_busy`, `upgrade_in_progress`, and `host_draining` close the window
    quietly, and the next tick re-observes with a fresh `attempt_id`;
  - `upgrade_refused` closes the window and adds the reported
    `candidate_sha256` (or the installed hash when the host sent none) to the
    refused set;
  - `accepted` confirms the window and may shorten its deadline from
    `remaining_ms`;
  - a lost reply or a connection error leaves the window open.

Window: while it is open, `wait_startup_settled` returns False, so
`TerminalWsMixin._resolve_attach_locator` answers the existing transient
`host_not_ready`. In `_health_loop` both paths that call `handle_host_death`
today (a failed ping from a dead pid, and a failed reconnect to a live pid)
check the window first and retry on the next tick instead, and `reconcile`
is skipped. A ping answered by the old host mid-quiesce shows an in-progress
phase and keeps the window open.

The window closes when a ping reports `last_outcome.attempt_id` equal to the
window's attempt:
- `succeeded` with `binary_sha256 == last_outcome.candidate_sha256`: log both
  identities and reconcile once, which changes no row because epoch and ids
  are unchanged.
- `succeeded` with any other hash: identity failure. Log the expected and
  observed hashes and refuse the candidate.
- `fallback`, `rolled_back`, `aborted`, or `refused`: add
  `last_outcome.candidate_sha256` to the refused set and log the reason.
- `deferred`: close quietly without refusing the candidate; a later tick
  retries.

At the deadline without such a ping, the daemon makes one fresh check: it
reconnects if needed, then `hello` and `ping` against the same host pid
identity and epoch. A ping seen before the deadline, including the idle ping
that led to the request, does not count. A fresh ping carrying the attempt's
outcome closes the window by the rules above. A fresh ping that is `idle`
with no outcome for the attempt means the request never ran; close the
window quietly and log it. A failed fresh check falls through to the
existing `handle_host_death` once, and once the window is closed every later
failed ping takes the ordinary host-death path. A fresh ping that still
reports a non-idle phase for the attempt past the deadline is a failed check:
the host armed its alarm when the attempt began (Decision 10), so a
conforming host cannot answer that way, and the daemon treats it exactly
like a failed check and runs `handle_host_death` once. The coordinator
records every attempt whose window closed, by any path, as finished and
never opens a window for a finished `attempt_id` again, whatever a later
ping reports. The refused set is in memory: a daemon restart or a
newly installed binary with a different hash tries again once.

Terminals rows: no schema change and no row writes. `host_epoch`,
`locator_key`, `state`, and `agent_run_id` stay as they are through a
successful upgrade; only the deadline fall-through changes rows, through the
existing `handle_host_death`.

Consumers unchanged:
- `src/gobby/terminals/host_identity.py` — no-edit-reason: the pid, pidfile, and gterm process identity are unchanged by exec-in-place, so `pid_matches_ping` keeps passing.

Verification planned: `DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest
tests/terminals/test_host_upgrade.py tests/terminals/test_host_manager.py -q`.

**Acceptance:**

- 2.1.1 - A stale adopted host (installed hash differs from `binary_sha256`)
  receives exactly one `host_upgrade` with the installed path and a fresh
  `attempt_id`; a current host receives none. test:
  `tests/terminals/test_host_upgrade.py::test_stale_host_gets_one_upgrade_request`.
- 2.1.2 - During an open window failed pings and failed reconnects do not
  call `handle_host_death`, attaches answer `host_not_ready`, no terminals
  row changes, and a healthy ping reporting an in-progress phase keeps the
  window open; past the deadline with failing pings the existing host-death
  path runs once. test:
  `tests/terminals/test_host_upgrade.py::test_window_holds_rows_until_terminal_outcome`.
- 2.1.3 - `refused`, `rolled_back`, `aborted`, `fallback`, and an identity
  failure each suppress retries for the reported candidate hash until the
  installed hash changes; a forced restore failure yields exactly one upgrade
  request. test:
  `tests/terminals/test_host_upgrade.py::test_failed_candidates_are_not_retried`.
- 2.1.4 - A host without the `host_upgrade` capability is never sent the verb
  and produces one warning naming `gobby restart --terminals`. test:
  `tests/terminals/test_host_upgrade.py::test_pre_handover_host_is_left_alone`.
- 2.1.5 - A successful upgrade closes the window on `succeeded` with the
  candidate hash, and the following reconcile leaves every native row's
  `host_epoch`, `locator_key`, and `state` unchanged. test:
  `tests/terminals/test_host_upgrade.py::test_successful_upgrade_changes_no_rows`.
- 2.1.6 - A lost acceptance reply, a daemon restart during the window, and a
  promotion between the daemon's hash and the host's pin each end with the
  window keyed to the daemon's `attempt_id` and the host's
  `candidate_sha256`. test:
  `tests/terminals/test_host_upgrade.py::test_window_follows_host_attempt_record`.
- 2.1.7 - A lost acceptance reply followed by two failed reconnects while
  the host restores calls no `handle_host_death`, and the first healthy ping
  reporting `succeeded` for the attempt closes the window with no row
  change. test:
  `tests/terminals/test_host_upgrade.py::test_lost_ack_and_failed_reconnects_keep_rows`.
- 2.1.8 - A request the host never ran closes the provisional window at its
  deadline only after a fresh post-deadline `hello` and `ping` to the same
  host answers `idle` with no outcome for the attempt; an idle ping seen
  before the deadline does not close it, and a failed fresh check calls
  `handle_host_death` once. test:
  `tests/terminals/test_host_upgrade.py::test_unrun_request_closes_only_on_fresh_check`.
- 2.1.9 - In-progress pings that keep reporting `remaining_ms = 0` do not
  extend the window past its fixed deadline; a fresh post-deadline ping
  still reporting `probing` or another non-idle phase for the attempt runs
  `handle_host_death` once, and no later ping reopens a window for that
  `attempt_id`; a `deferred` outcome closes the window without adding the
  candidate to the refused set. test:
  `tests/terminals/test_host_upgrade.py::test_window_deadline_is_fixed_and_deferred_is_not_refused`.

### 2.2 Web terminal relay reconnects across an upgrade [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/servers/websocket/proxy_relay.py::*` — scope-reason: ProxyAttachment carries a reopen callable and the host generation; ProxyHub._pump reconnects on host frame EOF after an upgrade, rechecks the live record before swapping, and updates the generation
- `src/gobby/servers/websocket/terminal_ws.py::*` — scope-reason: _start_proxy_attach passes a reopen callable that re-resolves the locator, reopens the frame source with the handed_off ownership rule, and redeclares the remembered theme when the lease allows it
- `tests/servers/test_native_web_proxy.py::*` — scope-reason: cover relay reconnect, cancellation cleanup, consecutive upgrades, themes, fallback, and finalization

**Research context:** `ProxyHub._pump`
(`src/gobby/servers/websocket/proxy_relay.py`) turns host frame EOF
(`FrameProtocolError`) into `finalize_attachment(…, "proxy_frame_eof")`,
which pops the record, cancels its pump, and the browser applies as a dead
attachment. `TerminalWsMixin._start_proxy_attach`
(`src/gobby/servers/websocket/terminal_ws.py`) opens the frame with
`open_proxy_frame(locator)` after `_resolve_attach_locator`, then
`start_proxy` performs `handshake` and `attach_terminal`; a `handed_off` flag
and a shielded `finally` close an opened frame that was never handed to the
hub. Theme: `_handle_terminal_set_theme` remembers the browser's theme and
`_declare_holder_theme` declares it only for the input holder.

`ProxyAttachment` gains `reopen: Callable[[], Awaitable[Any]] | None` and
`host_generation: int | None`, both set by `_start_proxy_attach`. The reopen
closure calls `_resolve_attach_locator(row)` and `open_proxy_frame`, then
`handshake` and `attach_terminal`, with the existing timeouts, and follows the
same `handed_off` plus shielded `finally` rule: a frame it opened is closed on
any failure or cancellation until the hub installs it.

In `_pump`, on `FrameProtocolError`, when `record.reopen` is set and the host
manager has an open upgrade window or reports a generation above
`record.host_generation`:
1. Wait for the window to close, bounded by its deadline.
2. Reconnect only when the host answers with the same epoch and a generation
   above `record.host_generation`. That covers `succeeded` and a verified
   `fallback`, both of which closed the old frames. Otherwise finalize with
   today's reasons.
3. Call `reopen`.
4. Recheck that `record` is still the live entry for its `attachment_id`
   (not finalized by a detach or WebSocket close during any await). If not,
   close the new frame and return.
5. Swap `record.frame`, set `record.host_generation` to the host's
   generation, and close the old frame.
6. Redeclare the remembered theme through `_declare_holder_theme` with the
   same binding when this attachment holds the lease; an observer declares
   nothing.
7. Emit the native history exactly as the pump does at start
   (`_emit_native_history`), and continue the loop with the same
   `attachment_id` and message sequence.

A reopen that fails, a changed epoch (`host_epoch_stale`), or an EOF with no
window and no generation change finalizes with today's reasons. The browser
sees a repaint, no `terminal_attachment_finalized`.

Verification planned: `DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest
tests/servers/test_native_web_proxy.py -q`.

**Acceptance:**

- 2.2.1 - Host frame EOF during an upgrade window reconnects the relay under
  the same browser `attachment_id`, replays history, continues the message
  sequence, and emits no `terminal_attachment_finalized`; a verified fallback
  reconnects the same way. test:
  `tests/servers/test_native_web_proxy.py::test_relay_reconnects_across_host_upgrade`.
- 2.2.2 - A failed reopen, a changed epoch, or EOF with no upgrade finalizes
  with the existing reasons. test:
  `tests/servers/test_native_web_proxy.py::test_relay_finalizes_when_reconnect_is_not_possible`.
- 2.2.3 - A detach or WebSocket close at each await of the reconnect leaves
  no open frame and no live record; two consecutive upgrades reconnect, and
  a later unrelated EOF finalizes as `proxy_frame_eof`. test:
  `tests/servers/test_native_web_proxy.py::test_relay_reconnect_settles_on_cancel_and_tracks_generation`.
- 2.2.4 - After a reconnect the holder's remembered theme is redeclared with
  the same binding and an observer's is not. test:
  `tests/servers/test_native_web_proxy.py::test_relay_reconnect_redeclares_holder_theme_only`.

### 2.3 gclient reconnects straight to the host [category: code]
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live_attach.rs::*` — scope-reason: retain the locator and daemon attachment id before recovery drops the source, run a host-local reconnect before the daemon re-attach, and apply its result by pane identity and epoch instead of daemon generation
- `crates/gclient/src/app/live_loop/host_recovery.rs`
- `crates/gclient/src/app/live_loop.rs::*` — scope-reason: declare the host_recovery module and poll its set beside the existing recovery futures; the set and its cancellation rules live in the new module
- `crates/gclient/src/app/pane.rs::*` — scope-reason: rebind the same holder after a host-local reconnect through the existing BindAttachment send
- `crates/gclient/tests/host_upgrade_recovery.rs`

**Research context:** Decision 8. Today `begin_frame_recovery`
(`crates/gclient/src/app/live_attach.rs`) sends frame `Eof` to
`begin_proxy_recovery`, which drops the direct source, and
`request_direct_source` then sends `terminal_attach` through the daemon. The
host needs no daemon for a reconnect: `Hello` authenticates with the local
token file, a user `AttachTerminal` needs no reservation, and the host keeps
the pane's `input_grant` across the exec (1.2). Two consumers would cancel a
host-local recovery today: `live_loop.rs::run_live_loop` clears every
`RecoveryFuture` and calls `abandon_frame_recoveries` on every daemon
reconnect result, success or failure, and `apply_frame_recovery` rejects a
result whose daemon generation changed.

Change:
- Before anything is dropped, `begin_proxy_recovery` copies the pane's
  `AttachLocator` (`host_socket`, `frame_host_epoch`, `host_terminal_id`) and
  its daemon `attachment_id`. It keeps the pane's attachment, lease, and
  control state; it does not call `begin_detaching` or send
  `terminal_detach` before local success or a deliberate fallback.
- It then reconnects through the same `UnixSocketFrameSource` direct-connect
  path `connect_direct_reply` uses, retrying with backoff from 100 ms to 1 s
  for a fixed client budget of 30 s. That covers the host's 15 s deadline
  plus grace without any new frame-protocol field. Connects made during the
  exec wait in the listener backlog, because the listener fd survives the
  exec.
- The host-local recovery lives in its own set, keyed by pane id,
  `host_terminal_id`, and `frame_host_epoch`, in the new module
  `crates/gclient/src/app/live_loop/host_recovery.rs` (split out of
  `crates/gclient/src/app/live_loop.rs`, which is 984 lines; the loop gains
  only the module declaration and one select arm that polls the set). Daemon reconnect
  results and daemon generation changes do not clear it. It is cancelled
  only when the pane is replaced or closed, or when the host answers with a
  different epoch.
- Its result is applied by pane identity and epoch, not by daemon
  generation. The reconnect is accepted only when the handshake's host epoch
  equals `frame_host_epoch`. It then re-attaches the same `host_terminal_id`
  and rebinds the same holder with `BindAttachment`, as the first direct
  attach does (`pane.rs`).
- An epoch change, `terminal_gone`, or an exhausted budget falls through to
  today's daemon path unchanged (`request_direct_source`,
  `attach_refusal_is_transient`, `defer_pane_attach`).
- No input is routed through the daemon.

The tests build on the fakes in `crates/gclient/tests/client_loop.rs`.

Verification planned: `cargo test -p gobby-client --test
host_upgrade_recovery`; `cargo clippy -p gobby-client --all-targets -- -D
warnings`.

**Acceptance:**

- 2.3.1 - With the daemon disconnected across the host's exec, a pane whose
  frame source hits EOF reconnects straight to the host, keeps the same
  terminal and epoch, never retires, and its input is accepted through the
  carried grant. test:
  `crates/gclient/tests/host_upgrade_recovery.rs::pane_reconnects_to_host_without_daemon`.
- 2.3.2 - An epoch change, `terminal_gone`, or an exhausted budget falls back
  to the daemon re-attach, which defers on `host_not_ready` and succeeds.
  test:
  `crates/gclient/tests/host_upgrade_recovery.rs::host_local_failure_falls_back_to_daemon_attach`.
- 2.3.3 - Repeated failed daemon reconnect attempts and a daemon generation
  change overlapping the host restore do not cancel the host-local recovery,
  which succeeds; closing the pane during the recovery cancels it and sends
  nothing to the host. test:
  `crates/gclient/tests/host_upgrade_recovery.rs::host_local_recovery_survives_daemon_attempts`.

### 2.4 Operator documentation [category: docs] (depends: 2.1)
`kind: deliverable`

Targets:
- `docs/guides/cli-commands.md`
- `src/gobby/install/shared/skills/gobby/references/admin/daemon.md`

**Research context:** `docs/guides/cli-commands.md` documents `gobby stop
--terminals` and `gobby restart --terminals` as the way to end native
terminals; `references/admin/daemon.md` is the operator lifecycle reference.
Neither says how a new gterm reaches a running host. Add:
- a newly installed gterm is applied automatically by the daemon without
  ending terminals, and scrollback on both screens is preserved;
- full-screen programs repaint once;
- input typed during the window is refused with `host_upgrading`;
- gclient reconnects to the host even while the daemon is down;
- `--terminals` still ends every native terminal;
- a host started before this change needs one `gobby restart --terminals` to
  become upgradable;
- hosts run from pinned images under `<socket_dir>/gterm-images/`, and a
  failed upgrade falls back to the pinned previous image once.

**Acceptance:**

- 2.4.1 - The CLI guide describes automatic gterm upgrade and when
  `--terminals` is still needed. behavior: "gterm upgrade" in
  `docs/guides/cli-commands.md`.
- 2.4.2 - The daemon lifecycle reference names the pinned image directory and
  the one-shot fallback. behavior: "gterm-images" in
  `src/gobby/install/shared/skills/gobby/references/admin/daemon.md`.

## V1: Plan Changelog
`kind: framing`

- 2026-09-28: First draft (a7a0446a1d): exec-in-place gterm host upgrade that
  keeps `host_epoch`, detached native panes, and daemon and gclient behavior
  across the host replacement. One enhancer pass; the Program Director
  disposed its edits and the Writer applied them (851fcc0bfa).
- 2026-09-28: Adversary review by `send_message` dialogue (memory 55b8c14e).
  H1-H6 resolved and the ghostty snapshot codec adopted (54738923c5); H1-H11
  resolved (0b75bbc8fb), including committed-pane reservations carried across
  the upgrade, the host-wide mutation gate, pinned images pruned only by the
  socket owner, the daemon-minted attempt record, and the
  `live_loop/host_recovery.rs` split; residuals on expiry ownership, the fixed
  window deadline with its fresh post-deadline check, and the rollback bound
  (d8111099dc); quiesce bounded by the soft cutoff with expired attempts
  settled (e0f42fcf43); every pre-accept exit clears the alarm while holding
  `upgrade_lock` (ce7459bad3).
- 2026-09-28: Adversary consensus on ce7459bad3 (plan sha256
  `dd36bfc26cba1ab10d8d317c192e35b462c39e1d93130511cb15697c780978e0`) with no
  outstanding findings; requirements traceability, runtime invariants, and
  repository blast radius complete. The approval presentation keeps two
  stated losses: the attended first move off gterm 0.1.2 ends existing native
  panes, and a timeout or failed rollback falls back to a host failure.
  M1 pending from the Adversary; expansion waits for PD review and Josh's
  approval.

## V2: Verification
`kind: verification`

Each leaf runs its own "Verification planned" commands, which touch only the
test targets that exist once it lands. After the final leaf and before
landing, run the combined set:

```bash
cargo test -p gobby-terminal --features vt-engine --test handover_display --test host_handover --test host_image --test control_protocol --test host_cli_args --test host_lifecycle
cargo test -p gobby-client --test host_upgrade_recovery
cargo clippy -p gobby-terminal --features vt-engine --all-targets -- -D warnings
cargo clippy -p gobby-client --all-targets -- -D warnings
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/terminals/test_host_upgrade.py tests/terminals/test_host_manager.py tests/servers/test_native_web_proxy.py -q
uv run ruff check src/ && uv run mypy src/
uv run gobby plans validate .gobby/plans/gterm-host-handover.md -p /Users/josh/Projects/gobby
```

Live check, in a window the Orchestrator announces: with two native panes open
(one shell running `sleep 600` and printing its pid, one running `vim`), the
Orchestrator installs a rebuilt gterm; within one health interval the daemon
log shows the upgrade with both binary identities; `ps` shows the host
running from `<socket_dir>/gterm-images/gterm-<new sha256>` and the older pin
is gone; the gterm pid, both child
pids, `host_epoch`, and every terminals row are unchanged; the shell pane
shows its prior scrollback; `vim` repaints, and after quitting it the pane's
earlier scrollback is intact; gclient and a web terminal both keep typing
into the same panes. Do not run the full pytest suite.

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Pane terminal snapshot and wrapper state encode and decode
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '1.1.1: A pane terminal fed styled text, cursor moves, a scrolling
    region, a changed palette entry, bracketed paste mode, a hyperlink, tab stops,
    and more lines than its height decodes into a terminal whose screen text, history
    text, cursor, and active modes equal the original. test: `crates/gterminal/tests/handover_display.rs::snapshot_restores_screen_history_cursor_and_modes`.

    1.1.2: Input split inside an escape sequence and inside a UTF-8 character completes
    correctly when the rest arrives after decode. test: `crates/gterminal/tests/handover_display.rs::continuation_completes_split_sequences`.

    1.1.3: A terminal on the alternate screen decodes with the alternate screen active
    and the primary screen''s scrollback intact after the program leaves the alternate
    screen. test: `crates/gterminal/tests/handover_display.rs::alternate_screen_keeps_primary_history`.

    1.1.4: Every pane terminal is created with continuation tracking enabled, and
    encoding a pane mid-escape succeeds. symbol: `Terminal::encode_snapshot`. test:
    `crates/gterminal/tests/handover_display.rs::pane_terminals_track_continuation`.

    1.1.5: A pane with an agent OSC title and progress, a child OSC 10/11 default-color
    override, a kitty keyboard mode, a cursor style, its own theme, and an OSC title
    sequence split across the encode restores all of them: the title and progress
    read the same, the split title completes, and a later host theme update keeps
    the child''s override. test: `crates/gterminal/tests/handover_display.rs::pane_wrapper_state_round_trips`.'
  labels:
  - covers:gterm-host-handover:1.1:1.1.1
  - covers:gterm-host-handover:1.1:1.1.2
  - covers:gterm-host-handover:1.1:1.1.3
  - covers:gterm-host-handover:1.1:1.1.4
  - covers:gterm-host-handover:1.1:1.1.5
  tdd: true
  source_section: '1.1'
  implementation_domain: backend
- title: Handover state file, frozen reaping, and transactional restore
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  - '1.4'
  validation_criteria: '1.2.1: A state file written from a live host round-trips:
    restore rebuilds the same epoch, generation, attempt, `next_host_id`, pane identities,
    sizes, grants, locators, committed reservations, wrapper state, and event cursor.
    test: `crates/gterminal/tests/host_handover.rs::state_round_trips_host_and_pane_fields`.

    1.2.2: Restore adopts the carried listener fds and never unlinks, rebinds, or
    rewrites the sockets or pidfile. test: `crates/gterminal/tests/host_handover.rs::restore_adopts_listeners_without_rebinding`.

    1.2.3: A child that exits and is delivered before capture, one recorded but not
    delivered, one that exits after the freeze and before exec, and one that exits
    after restore each produce exactly one `terminal_exited` with the real status,
    and output written during the window is delivered after restore. test: `crates/gterminal/tests/host_handover.rs::exit_and_output_during_window_survive`.

    1.2.4: A decode error, a panic, and an injected actor-construction failure, each
    after at least one pane is staged, exec `previous_image` once with `--resume-fallback`;
    every child pid, queued output, real exit status, and both listener fds survive
    into the fallback, and every pane accepts a write and returns its output after
    the fallback commits. test: `crates/gterminal/tests/host_handover.rs::stage_failure_falls_back_with_checkpoint_intact`.

    1.2.5: `--probe-resume` accepts exactly the supported format versions and leaves
    the pinned image directory untouched. test: `crates/gterminal/tests/host_cli_args.rs::probe_resume_reports_supported_formats`.

    1.2.6: A fallback Stage error and a failed fallback `execve` both exit with status
    70, and a restore that wedges is ended by the pending alarm before the daemon''s
    deadline. test: `crates/gterminal/tests/host_handover.rs::fallback_failure_and_wedged_restore_end_the_process`.

    1.2.7: A rollback that unfreezes a pane whose child exited while frozen reaps
    it and emits one `terminal_exited` with the real status. symbol: `PaneRuntime::unfreeze_reaping`.
    test: `crates/gterminal/tests/host_handover.rs::rollback_reaps_exit_seen_while_frozen`.

    1.2.8: After a restore commits, every pane accepts a user write and its output
    reaches the terminal; a write sent while the actor is still quiesced is refused,
    and one sent right after `resume_restored` is written once, in order. symbol:
    `PtyIoActor::resume_restored`. test: `crates/gterminal/tests/host_handover.rs::restored_panes_accept_input_after_commit`.

    1.2.9: After restore, a pane''s agent title survives the first ticker broadcast,
    two panes keep their distinct themes, a child OSC 10/11 override survives a host
    theme refresh, and a committed observer entitlement rebinds. test: `crates/gterminal/tests/host_handover.rs::restored_panes_keep_wrapper_state_and_entitlements`.'
  labels:
  - covers:gterm-host-handover:1.2:1.2.1
  - covers:gterm-host-handover:1.2:1.2.2
  - covers:gterm-host-handover:1.2:1.2.3
  - covers:gterm-host-handover:1.2:1.2.4
  - covers:gterm-host-handover:1.2:1.2.5
  - covers:gterm-host-handover:1.2:1.2.6
  - covers:gterm-host-handover:1.2:1.2.7
  - covers:gterm-host-handover:1.2:1.2.8
  - covers:gterm-host-handover:1.2:1.2.9
  tdd: true
  source_section: '1.2'
  implementation_domain: backend
- title: '`host_upgrade` verb, mutation gate, bounded capture, exec, and in-process
    rollback'
  category: code
  task_type: feature
  depends_on:
  - '1.2'
  validation_criteria: '1.3.1: Upgrading a live host keeps the host pid, every pane''s
    child pid, `host_epoch`, and `host_terminal_id`s, increments `generation`, reports
    `last_outcome: succeeded` for the daemon''s `attempt_id`, and the panes accept
    input and produce output afterwards. test: `crates/gterminal/tests/host_handover.rs::upgrade_keeps_pids_epoch_and_panes`.

    1.3.2: The verb refuses `host_busy`, `host_draining`, and `upgrade_in_progress`
    without changing state; two simultaneous upgrades yield one attempt; a spawn,
    reservation, or `host_shutdown` issued during the probe is caught by the recheck.
    test: `crates/gterminal/tests/control_protocol.rs::host_upgrade_admission_is_serialized_and_rechecked`.

    1.3.3: A probe that exits non-zero or times out answers `upgrade_refused`, records
    `last_outcome: refused`, and quiesces no pane. test: `crates/gterminal/tests/host_handover.rs::probe_refusal_leaves_panes_untouched`.

    1.3.4: A quiesce timeout, a late quiesce completion, a first rollback timeout
    followed by a late acknowledgement, a snapshot encode error, a write or `fsync`
    error, the soft cutoff passed during capture, and an `execve` failure each roll
    back, keep serving on the same sockets, record the matching `last_outcome`, clear
    the alarm, and reopen the gate only after every pane accepts a write and returns
    its output. test: `crates/gterminal/tests/host_handover.rs::every_pre_exec_failure_rolls_back`.

    1.3.5: `ping` reports `generation` and the `upgrade` record, with `remaining_ms`
    defined in `probing`, and `HOST_CAPABILITIES` contains `host_upgrade`. test: `crates/gterminal/tests/control_protocol.rs::ping_reports_generation_and_attempt`.

    1.3.6: A `write_batch` with a pending delayed operation makes `host_upgrade` answer
    `host_busy`; after acceptance, control writes, frame input, and theme declarations
    receive `host_upgrading`, and no refused input is written after restore. test:
    `crates/gterminal/tests/control_protocol.rs::mutation_gate_blocks_and_refuses_during_upgrade`.

    1.3.7: Sixteen panes at the scrollback limit upgrade inside the deadline. test:
    `crates/gterminal/tests/host_handover.rs::many_full_panes_upgrade_within_deadline`.

    1.3.8: A pane created through reserve, `spawn`, and `spawn_commit`, kept live,
    and a second one whose creating connection has closed both upgrade successfully;
    a pending unconsumed reservation still yields `host_busy`. test: `crates/gterminal/tests/host_handover.rs::committed_panes_are_admitted_and_pending_reservations_are_busy`.

    1.3.9: A child exit recorded before the freeze whose watcher is released after
    capture and before exec, a `list` that runs just before the write guard is taken
    while that watcher is pending, a `list` call during the window, and a ticker tick
    during the window neither remove a carried slot without its event, close its master,
    nor advance the event cursor before exec; a subscriber resuming from the carried
    cursor sees exactly one `terminal_exited`. test: `crates/gterminal/tests/host_handover.rs::background_mutators_wait_through_exec`.

    1.3.10: A pane whose rollback fails twice ends the host through SIGALRM without
    reopening the gate. test: `crates/gterminal/tests/host_handover.rs::persistent_rollback_failure_ends_the_host`.

    1.3.11: After a live host has seen a losing cold start, a probe, and a same-image
    refusal and rollback, a later upgrade whose restore fails still finds `previous_image`
    and falls back. test: `crates/gterminal/tests/host_handover.rs::fallback_image_survives_other_starts_and_same_image_attempts`.

    1.3.12: A `try_write` timeout and a recheck that finds a new pending reservation
    or draining after the probe each return the attempt to `idle` with `last_outcome:
    deferred`; an attempt that starts right after a refused or deferred one keeps
    its own alarm armed (the earlier attempt''s `alarm(0)` runs before it can take
    `upgrade_lock`); a rollback whose cleanup blocks while the write guard is held
    is ended by the alarm; and after a recovered rollback the alarm is cleared only
    once the write guard is released. test: `crates/gterminal/tests/host_handover.rs::pre_accept_returns_and_rollback_cleanup_are_bounded`.

    1.3.13: A pane whose quiesce acknowledgement never arrives makes the attempt give
    up on it before the soft cutoff, run no `execve`, and recover every pane (writes
    accepted, output returned, alarm cleared) before the hard deadline. test: `crates/gterminal/tests/host_handover.rs::stalled_quiesce_recovers_within_hard_budget`.'
  labels:
  - covers:gterm-host-handover:1.3:1.3.1
  - covers:gterm-host-handover:1.3:1.3.2
  - covers:gterm-host-handover:1.3:1.3.3
  - covers:gterm-host-handover:1.3:1.3.4
  - covers:gterm-host-handover:1.3:1.3.5
  - covers:gterm-host-handover:1.3:1.3.6
  - covers:gterm-host-handover:1.3:1.3.7
  - covers:gterm-host-handover:1.3:1.3.8
  - covers:gterm-host-handover:1.3:1.3.9
  - covers:gterm-host-handover:1.3:1.3.10
  - covers:gterm-host-handover:1.3:1.3.11
  - covers:gterm-host-handover:1.3:1.3.12
  - covers:gterm-host-handover:1.3:1.3.13
  tdd: true
  source_section: '1.3'
  implementation_domain: backend
- title: Pinned host images and binary identity
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '1.4.1: `pin_image` creates a 0700 image under a 0700 user-owned
    directory whose name is its SHA-256, reuses a matching existing pin, replaces
    a mismatched one, falls back to copy across devices, and refuses an unsafe directory.
    test: `crates/gterminal/tests/host_image.rs::pin_image_is_content_addressed_and_private`.

    1.4.2: Replacing the source path by rename after pinning leaves the pin''s bytes
    and hash unchanged, and two promotions in a row leave both earlier pins intact
    until pruned. test: `crates/gterminal/tests/host_image.rs::pins_survive_promotion_of_the_source`.

    1.4.3: A cold-start host launched from an unpinned path re-execs from its pin
    before binding, and `ping.binary_sha256` equals the pin''s hash. test: `crates/gterminal/tests/host_image.rs::cold_start_runs_from_pin`.

    1.4.4: With a live host A, a second cold start B that loses the socket check leaves
    A''s pin and every other pin in place, and `remove_candidate` keeps a candidate
    equal to the running pin. The probe''s side of this is 1.2.5. test: `crates/gterminal/tests/host_image.rs::only_the_socket_owner_prunes`.'
  labels:
  - covers:gterm-host-handover:1.4:1.4.1
  - covers:gterm-host-handover:1.4:1.4.2
  - covers:gterm-host-handover:1.4:1.4.3
  - covers:gterm-host-handover:1.4:1.4.4
  tdd: true
  source_section: '1.4'
  implementation_domain: backend
- title: Upgrade trigger and handover window in TerminalHostManager
  category: code
  task_type: feature
  depends_on:
  - '1.3'
  validation_criteria: '2.1.1: A stale adopted host (installed hash differs from `binary_sha256`)
    receives exactly one `host_upgrade` with the installed path and a fresh `attempt_id`;
    a current host receives none. test: `tests/terminals/test_host_upgrade.py::test_stale_host_gets_one_upgrade_request`.

    2.1.2: During an open window failed pings and failed reconnects do not call `handle_host_death`,
    attaches answer `host_not_ready`, no terminals row changes, and a healthy ping
    reporting an in-progress phase keeps the window open; past the deadline with failing
    pings the existing host-death path runs once. test: `tests/terminals/test_host_upgrade.py::test_window_holds_rows_until_terminal_outcome`.

    2.1.3: `refused`, `rolled_back`, `aborted`, `fallback`, and an identity failure
    each suppress retries for the reported candidate hash until the installed hash
    changes; a forced restore failure yields exactly one upgrade request. test: `tests/terminals/test_host_upgrade.py::test_failed_candidates_are_not_retried`.

    2.1.4: A host without the `host_upgrade` capability is never sent the verb and
    produces one warning naming `gobby restart --terminals`. test: `tests/terminals/test_host_upgrade.py::test_pre_handover_host_is_left_alone`.

    2.1.5: A successful upgrade closes the window on `succeeded` with the candidate
    hash, and the following reconcile leaves every native row''s `host_epoch`, `locator_key`,
    and `state` unchanged. test: `tests/terminals/test_host_upgrade.py::test_successful_upgrade_changes_no_rows`.

    2.1.6: A lost acceptance reply, a daemon restart during the window, and a promotion
    between the daemon''s hash and the host''s pin each end with the window keyed
    to the daemon''s `attempt_id` and the host''s `candidate_sha256`. test: `tests/terminals/test_host_upgrade.py::test_window_follows_host_attempt_record`.

    2.1.7: A lost acceptance reply followed by two failed reconnects while the host
    restores calls no `handle_host_death`, and the first healthy ping reporting `succeeded`
    for the attempt closes the window with no row change. test: `tests/terminals/test_host_upgrade.py::test_lost_ack_and_failed_reconnects_keep_rows`.

    2.1.8: A request the host never ran closes the provisional window at its deadline
    only after a fresh post-deadline `hello` and `ping` to the same host answers `idle`
    with no outcome for the attempt; an idle ping seen before the deadline does not
    close it, and a failed fresh check calls `handle_host_death` once. test: `tests/terminals/test_host_upgrade.py::test_unrun_request_closes_only_on_fresh_check`.

    2.1.9: In-progress pings that keep reporting `remaining_ms = 0` do not extend
    the window past its fixed deadline; a fresh post-deadline ping still reporting
    `probing` or another non-idle phase for the attempt runs `handle_host_death` once,
    and no later ping reopens a window for that `attempt_id`; a `deferred` outcome
    closes the window without adding the candidate to the refused set. test: `tests/terminals/test_host_upgrade.py::test_window_deadline_is_fixed_and_deferred_is_not_refused`.'
  labels:
  - covers:gterm-host-handover:2.1:2.1.1
  - covers:gterm-host-handover:2.1:2.1.2
  - covers:gterm-host-handover:2.1:2.1.3
  - covers:gterm-host-handover:2.1:2.1.4
  - covers:gterm-host-handover:2.1:2.1.5
  - covers:gterm-host-handover:2.1:2.1.6
  - covers:gterm-host-handover:2.1:2.1.7
  - covers:gterm-host-handover:2.1:2.1.8
  - covers:gterm-host-handover:2.1:2.1.9
  tdd: true
  source_section: '2.1'
  implementation_domain: backend
- title: Web terminal relay reconnects across an upgrade
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: '2.2.1: Host frame EOF during an upgrade window reconnects
    the relay under the same browser `attachment_id`, replays history, continues the
    message sequence, and emits no `terminal_attachment_finalized`; a verified fallback
    reconnects the same way. test: `tests/servers/test_native_web_proxy.py::test_relay_reconnects_across_host_upgrade`.

    2.2.2: A failed reopen, a changed epoch, or EOF with no upgrade finalizes with
    the existing reasons. test: `tests/servers/test_native_web_proxy.py::test_relay_finalizes_when_reconnect_is_not_possible`.

    2.2.3: A detach or WebSocket close at each await of the reconnect leaves no open
    frame and no live record; two consecutive upgrades reconnect, and a later unrelated
    EOF finalizes as `proxy_frame_eof`. test: `tests/servers/test_native_web_proxy.py::test_relay_reconnect_settles_on_cancel_and_tracks_generation`.

    2.2.4: After a reconnect the holder''s remembered theme is redeclared with the
    same binding and an observer''s is not. test: `tests/servers/test_native_web_proxy.py::test_relay_reconnect_redeclares_holder_theme_only`.'
  labels:
  - covers:gterm-host-handover:2.2:2.2.1
  - covers:gterm-host-handover:2.2:2.2.2
  - covers:gterm-host-handover:2.2:2.2.3
  - covers:gterm-host-handover:2.2:2.2.4
  tdd: true
  source_section: '2.2'
  implementation_domain: backend
- title: gclient reconnects straight to the host
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '2.3.1: With the daemon disconnected across the host''s exec,
    a pane whose frame source hits EOF reconnects straight to the host, keeps the
    same terminal and epoch, never retires, and its input is accepted through the
    carried grant. test: `crates/gclient/tests/host_upgrade_recovery.rs::pane_reconnects_to_host_without_daemon`.

    2.3.2: An epoch change, `terminal_gone`, or an exhausted budget falls back to
    the daemon re-attach, which defers on `host_not_ready` and succeeds. test: `crates/gclient/tests/host_upgrade_recovery.rs::host_local_failure_falls_back_to_daemon_attach`.

    2.3.3: Repeated failed daemon reconnect attempts and a daemon generation change
    overlapping the host restore do not cancel the host-local recovery, which succeeds;
    closing the pane during the recovery cancels it and sends nothing to the host.
    test: `crates/gclient/tests/host_upgrade_recovery.rs::host_local_recovery_survives_daemon_attempts`.'
  labels:
  - covers:gterm-host-handover:2.3:2.3.1
  - covers:gterm-host-handover:2.3:2.3.2
  - covers:gterm-host-handover:2.3:2.3.3
  tdd: true
  source_section: '2.3'
  implementation_domain: backend
- title: Operator documentation
  category: docs
  task_type: chore
  depends_on:
  - '2.1'
  validation_criteria: '2.4.1: The CLI guide describes automatic gterm upgrade and
    when `--terminals` is still needed. behavior: "gterm upgrade" in `docs/guides/cli-commands.md`.

    2.4.2: The daemon lifecycle reference names the pinned image directory and the
    one-shot fallback. behavior: "gterm-images" in `src/gobby/install/shared/skills/gobby/references/admin/daemon.md`.'
  labels:
  - covers:gterm-host-handover:2.4:2.4.1
  - covers:gterm-host-handover:2.4:2.4.2
  tdd: false
  source_section: '2.4'
  assigned_agent: tech-writer
```
