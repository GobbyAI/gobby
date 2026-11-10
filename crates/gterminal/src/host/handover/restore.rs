//! Restore from a handover as one transaction (Decision 12). Stage verifies
//! the state file and builds everything on close-on-exec duplicates, so an
//! error leaves the carried descriptors untouched. Commit takes ownership and
//! does no fallible I/O. A Stage failure ends in `fallback`.

use std::collections::{HashMap, HashSet};
use std::io;
use std::os::fd::{FromRawFd, OwnedFd, RawFd};
use std::panic::{catch_unwind, resume_unwind, AssertUnwindSafe};
use std::path::{Path, PathBuf};
use std::sync::Arc;

use tracing::{info, warn};

use super::{
    decode_snapshot, monotonic_now_ns, CarriedHost, CarriedObserverBind, CarriedPane,
    HandoverState, RestoreFailure, UpgradeOutcome, UpgradeRecord,
};
use crate::host::events::HostEvents;
use crate::host::spawn::PreparedChild;
use crate::host::state::{
    CommitState, HostState, Identity, Inner, ObserverBind, Reservation, TerminalSlot,
};
use crate::pane::{ChildExit, GhosttyPaneTerminal, PaneRuntime};

/// A restore that staged successfully and has not committed yet.
pub(crate) struct Staged {
    pub(crate) host: CarriedHost,
    pub(crate) control: tokio::net::UnixListener,
    pub(crate) frames: tokio::net::UnixListener,
    pub(crate) commit: PendingCommit,
}

/// What Commit needs beyond the host state: the carried exits, the original
/// descriptors to close, and the state file to delete.
pub(crate) struct PendingCommit {
    path: PathBuf,
    exits: HashMap<Identity, Option<ChildExit>>,
    originals: Vec<RawFd>,
    outcome: UpgradeOutcome,
}

/// Verifies `state`, read from `path`, and builds every pane, listener, and
/// registry record on duplicates. Nothing reads, reaps, or accepts yet.
/// `resumed_from` names the failure a fallback takes over from.
pub(crate) fn stage(
    state: HandoverState,
    path: &Path,
    pid_file: &Path,
    scrollback_limits: crate::ghostty::ScrollbackLimits,
    event_queue_bytes: usize,
    resumed_from: Option<RestoreFailure>,
) -> io::Result<Staged> {
    let fallback = resumed_from.is_some();
    verify(&state, pid_file)?;
    let control = listener(state.control_listener_fd)?;
    let frames = listener(state.frames_listener_fd)?;
    let mut originals = vec![state.control_listener_fd, state.frames_listener_fd];
    let mut terminals = HashMap::new();
    let mut reservations = HashMap::new();
    let mut exits = HashMap::new();
    let mut host_ids = HashSet::new();
    for pane in &state.panes {
        if !host_ids.insert(pane.host_terminal_id.clone()) {
            return Err(invalid(format!(
                "duplicate carried pane {}",
                pane.host_terminal_id
            )));
        }
    }
    originals.extend(state.panes.iter().map(|pane| pane.master_fd));
    let lanes = std::thread::available_parallelism().map_or(1, usize::from);
    let staged_panes = stage_panes(state.panes, lanes, scrollback_limits, fallback)?;
    for (slot, reservation, exit) in staged_panes {
        if let Some(reservation) = reservation {
            reservations.insert(reservation.id.clone(), reservation);
        }
        exits.insert(slot.identity.clone(), exit);
        terminals.insert(slot.identity.clone(), slot);
    }
    let events = HostEvents::restore(
        state.host_epoch.clone(),
        event_queue_bytes,
        state.events.cursor,
        state.events.ring,
    )?;
    Ok(Staged {
        host: CarriedHost {
            host_epoch: state.host_epoch,
            generation: state.generation,
            upgrade: UpgradeRecord {
                attempt: state.attempt,
                outcome: None,
            },
            events,
            inner: Inner::restored(
                terminals,
                state.next_host_id,
                state.next_attachment,
                reservations,
            ),
        },
        control,
        frames,
        commit: PendingCommit {
            path: path.to_path_buf(),
            exits,
            originals,
            outcome: resumed_from.map_or(UpgradeOutcome::Succeeded, UpgradeOutcome::Fallback),
        },
    })
}

fn verify(state: &HandoverState, pid_file: &Path) -> io::Result<()> {
    let pid = std::process::id();
    if state.host_pid != pid {
        return Err(invalid(format!(
            "state file names host pid {} but this is {pid}",
            state.host_pid
        )));
    }
    let published = std::fs::read_to_string(pid_file)?;
    if published.trim().parse::<u32>().ok() != Some(pid) {
        return Err(invalid(format!(
            "pidfile {} does not name pid {pid}",
            pid_file.display()
        )));
    }
    if monotonic_now_ns() > state.deadline_monotonic_ns {
        return Err(io::Error::new(
            io::ErrorKind::TimedOut,
            "handover deadline passed before restore",
        ));
    }
    Ok(())
}

/// One pane staged for commit: its slot, its reservation if it had one, and its exit record.
type StagedPane = (TerminalSlot, Option<Reservation>, Option<ChildExit>);

/// Stages carried panes on at most `lanes` threads. Each lane takes a contiguous run of panes, so
/// the first failure in carried order is the one reported, as a serial walk reports it; `lanes = 1`
/// is that serial walk. Every lane is joined before results are read, and a pane that panicked is
/// resumed on the caller at its carried position, so a restore fault still unwinds the restore.
pub(super) fn stage_panes(
    panes: Vec<CarriedPane>,
    lanes: usize,
    scrollback_limits: crate::ghostty::ScrollbackLimits,
    fallback: bool,
) -> io::Result<Vec<StagedPane>> {
    let per_lane = panes.len().div_ceil(lanes.max(1)).max(1);
    let mut runs: Vec<Vec<CarriedPane>> = Vec::new();
    for pane in panes {
        match runs.last_mut() {
            Some(run) if run.len() < per_lane => run.push(pane),
            _ => runs.push(vec![pane]),
        }
    }
    // Lane threads carry the caller's runtime context, which pane restore needs for its fd tasks.
    let runtime = tokio::runtime::Handle::try_current().ok();
    let outcomes: Vec<Vec<std::thread::Result<io::Result<StagedPane>>>> =
        std::thread::scope(|scope| {
            let workers: Vec<_> = runs
                .into_iter()
                .map(|run| {
                    let runtime = runtime.clone();
                    scope.spawn(move || {
                        let _entered = runtime.as_ref().map(tokio::runtime::Handle::enter);
                        run.into_iter()
                            .map(|pane| {
                                catch_unwind(AssertUnwindSafe(|| {
                                    stage_pane(pane, scrollback_limits, fallback)
                                }))
                            })
                            .collect::<Vec<_>>()
                    })
                })
                .collect();
            workers
                .into_iter()
                .map(|worker| {
                    worker
                        .join()
                        .unwrap_or_else(|payload| resume_unwind(payload))
                })
                .collect()
        });
    let mut staged = Vec::with_capacity(outcomes.iter().map(Vec::len).sum());
    for outcome in outcomes.into_iter().flatten() {
        match outcome {
            Ok(pane) => staged.push(pane?),
            Err(payload) => resume_unwind(payload),
        }
    }
    Ok(staged)
}

fn stage_pane(
    pane: CarriedPane,
    scrollback_limits: crate::ghostty::ScrollbackLimits,
    fallback: bool,
) -> io::Result<(TerminalSlot, Option<Reservation>, Option<ChildExit>)> {
    let fault = |kind| injected_fault(fallback, kind, &pane.host_terminal_id);
    let master = duplicate(pane.master_fd, libc::S_IFCHR)?;
    if fault("panic") {
        panic!("injected restore panic");
    }
    if fault("wedge") {
        loop {
            std::thread::sleep(std::time::Duration::from_secs(60));
        }
    }
    let snapshot = decode_snapshot(if fault("decode") {
        "!"
    } else {
        &pane.snapshot_b64
    })?;
    let terminal = GhosttyPaneTerminal::from_handover(&snapshot, pane.core, scrollback_limits)?;
    if fault("actor") {
        return Err(io::Error::other("injected actor construction failure"));
    }
    let runtime = PaneRuntime::stage_restore(
        terminal,
        (pane.rows, pane.cols, pane.pixel_width, pane.pixel_height),
        master,
        pane.pid,
        pane.reported_cwd,
    )?;
    let identity = Identity {
        terminal_id: pane.terminal_id,
        spawn_key: pane.spawn_key,
    };
    let reservation = pane.reservation.map(|reservation| Reservation {
        id: reservation.id,
        key: reservation.key,
        generation: reservation.generation,
        terminal_id: reservation.terminal_id,
        conn_id: 0,
        identity: reservation.identity.map(|identity| Identity {
            terminal_id: identity.terminal_id,
            spawn_key: identity.spawn_key,
        }),
        prepared: true,
    });
    let slot = TerminalSlot {
        identity,
        host_terminal_id: pane.host_terminal_id,
        commit_state: CommitState::Committed,
        pgid: pane.pgid,
        start_time: pane.start_time,
        title: pane.title,
        rows: pane.rows,
        cols: pane.cols,
        last_seq: pane.last_seq,
        observation_state: pane.observation_state,
        observation_reason: None,
        observation_generation: pane.observation_generation,
        fingerprint: pane.fingerprint,
        reservation_id: pane.reservation_id,
        reserve_key: pane.reserve_key,
        reserve_generation: pane.reserve_generation,
        observer_bind: match pane.observer_bind {
            CarriedObserverBind::None => ObserverBind::None,
            CarriedObserverBind::Reserved {
                reservation_id,
                generation,
            } => ObserverBind::Reserved {
                reservation_id,
                generation,
            },
            CarriedObserverBind::Entitled {
                reservation_id,
                generation,
            } => ObserverBind::Entitled {
                reservation_id,
                generation,
            },
        },
        commit_deadline: None,
        killing: false,
        kill_unproven: false,
        child: Some(PreparedChild::from_restored(
            runtime,
            pane.pid,
            pane.pgid,
            pane.start_time,
        )),
        written_bytes: pane.written_bytes,
        dropped_bytes: pane.dropped_bytes,
        total_bytes: pane.total_bytes,
        truncated: pane.truncated,
        user_attachments: HashSet::new(),
        locator: pane.locator,
        tmux_history_bytes: 0,
        history: None,
        last_frame: None,
        observer_generation: pane.observer_generation,
        consecutive_failures: 0,
    };
    Ok((slot, reservation, pane.exit))
}

fn listener(fd: RawFd) -> io::Result<tokio::net::UnixListener> {
    let listener = std::os::unix::net::UnixListener::from(duplicate(fd, libc::S_IFSOCK)?);
    listener.set_nonblocking(true)?;
    tokio::net::UnixListener::from_std(listener)
}

/// A close-on-exec duplicate of a carried descriptor of file type `kind`.
/// The original stays open and inheritable for a fallback exec.
fn duplicate(fd: RawFd, kind: libc::mode_t) -> io::Result<OwnedFd> {
    // SAFETY: F_GETFD only reads the descriptor flags.
    if unsafe { libc::fcntl(fd, libc::F_GETFD) } < 0 {
        return Err(io::Error::new(
            io::ErrorKind::NotFound,
            format!("carried fd {fd}: {}", io::Error::last_os_error()),
        ));
    }
    // SAFETY: stat is plain data that fstat fills.
    let mut stat: libc::stat = unsafe { std::mem::zeroed() };
    // SAFETY: fd is open (checked above) and stat is a valid out-pointer.
    if unsafe { libc::fstat(fd, &mut stat) } < 0 {
        return Err(io::Error::last_os_error());
    }
    if stat.st_mode & libc::S_IFMT != kind {
        return Err(invalid(format!("carried fd {fd} has the wrong file type")));
    }
    // SAFETY: fd is open; the duplicate is a new descriptor this call owns.
    let duplicate = unsafe { libc::fcntl(fd, libc::F_DUPFD_CLOEXEC, 0) };
    if duplicate < 0 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: duplicate is a fresh descriptor nothing else owns.
    Ok(unsafe { OwnedFd::from_raw_fd(duplicate) })
}

fn invalid(message: String) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

/// A test-helper fault (as in `gate.rs`) of `kind` at one pane, set by
/// `GTERM_RESTORE_FAULT` for a first restore and `GTERM_FALLBACK_FAULT` for a
/// fallback, as `<kind>:<host_terminal_id>`.
#[cfg(debug_assertions)]
fn injected_fault(fallback: bool, kind: &str, host_terminal_id: &str) -> bool {
    let var = if fallback {
        "GTERM_FALLBACK_FAULT"
    } else {
        "GTERM_RESTORE_FAULT"
    };
    crate::test_seam::helper_enabled()
        && std::env::var(var).is_ok_and(|value| value == format!("{kind}:{host_terminal_id}"))
}

#[cfg(not(debug_assertions))]
fn injected_fault(_fallback: bool, _kind: &str, _host_terminal_id: &str) -> bool {
    false
}

impl PendingCommit {
    /// Commit steps before the listeners accept: take ownership of every
    /// child, close the originals, start the exit watchers, resume the actors.
    pub(crate) async fn take_ownership(&mut self, state: &Arc<HostState>) {
        let mut watches = Vec::new();
        {
            let mut inner = state.inner.lock().await;
            for slot in inner.terminals.values_mut() {
                let Some(child) = slot.child.as_mut() else {
                    continue;
                };
                let exit = self.exits.remove(&slot.identity).flatten();
                child.runtime.commit_restore(exit);
                if let Some(watch) = child.runtime.child_exit_watch() {
                    watches.push((slot.identity.clone(), slot.host_terminal_id.clone(), watch));
                }
            }
        }
        for fd in self.originals.drain(..) {
            // SAFETY: the originals were inherited for this image to adopt and
            // every one now has an adopted duplicate; nothing else uses them.
            unsafe { libc::close(fd) };
        }
        for (identity, host_terminal_id, watch) in watches {
            state.watch_leader_exit(identity, host_terminal_id, watch, false);
        }
        let inner = state.inner.lock().await;
        for slot in inner.terminals.values() {
            if let Some(child) = &slot.child {
                if let Err(err) = child.runtime.resume_restored() {
                    tracing::error!(
                        host_terminal_id = %slot.host_terminal_id,
                        err = %err,
                        "restored pane actor is gone"
                    );
                    std::process::abort();
                }
            }
        }
    }

    /// Commit steps after the listeners accept: cancel the upgrade alarm,
    /// record the outcome, delete the state file, redraw panes. `run` prunes
    /// the pins afterwards, as it does for a cold start.
    pub(crate) async fn finish(self, state: &Arc<HostState>) {
        // SAFETY: alarm(0) only cancels a pending alarm.
        unsafe { libc::alarm(0) };
        if let Ok(mut upgrade) = state.upgrade.lock() {
            if let Some(record) = upgrade.as_mut() {
                record.outcome = Some(self.outcome);
                info!(
                    attempt_id = %record.attempt.attempt_id,
                    outcome = ?self.outcome,
                    generation = state.generation,
                    "gterm host restored"
                );
            }
        }
        if let Err(err) = std::fs::remove_file(&self.path) {
            warn!(path = %self.path.display(), err = %err, "handover state removal failed");
        }
        let inner = state.inner.lock().await;
        for child in inner
            .terminals
            .values()
            .filter_map(|slot| slot.child.as_ref())
        {
            child.runtime.nudge_child_redraw_after_handoff();
        }
    }
}
