//! Host-local frame recovery across a host-image exec (plan 2.3).
//!
//! A direct pane whose frame source dies can reconnect straight to the host:
//! `Hello` authenticates with the local token file, a user `AttachTerminal`
//! needs no daemon reservation, and the host keeps the pane's `input_grant`
//! across the exec — the only recovery that survives the daemon being away
//! for the whole upgrade window.
//!
//! This set is deliberately separate from `live_attach`'s daemon recoveries:
//! daemon reconnect results and daemon generation changes must not clear it.
//! It is cancelled only when the pane is replaced or closed, or when the host
//! answers with a foreign epoch, and a cancelled or failed host recovery falls
//! through to the daemon path unchanged.

use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;
use std::time::Duration;

use futures_util::stream::{FuturesUnordered, StreamExt};
use tokio::sync::watch;
use tokio::time::Instant;

use crate::app::live_attach::RecoveryFuture;
use crate::app::{PaneId, Workspace};
use crate::daemon::LiveDaemon;
use crate::frame_source::{
    AttachLocator, FrameError, PaneFrameSource, Transport, UnixSocketFrameSource,
};

/// The client's whole budget for a host-local reconnect: the host's own
/// deadline for the exec plus grace. A longer wait only delays the daemon
/// fallback the user ends up needing.
pub(in crate::app) const HOST_RECOVERY_BUDGET: Duration = Duration::from_secs(30);
/// The first retry delay; it doubles up to [`HOST_RECOVERY_RETRY_MAX`].
pub(in crate::app) const HOST_RECOVERY_RETRY_BASE: Duration = Duration::from_millis(100);
pub(in crate::app) const HOST_RECOVERY_RETRY_MAX: Duration = Duration::from_millis(1_000);

/// A finished host-local reconnect, tagged with the pane identity and epoch it
/// was started against so it is applied without consulting the daemon.
pub(in crate::app) struct HostRecovery {
    pane_id: PaneId,
    terminal_id: String,
    epoch: String,
    outcome: HostRecoveryOutcome,
}

pub(in crate::app) enum HostRecoveryOutcome {
    /// The host answered on the same epoch and the stream is back. The
    /// attachment id, lease and grant were never given up.
    Restored(UnixSocketFrameSource),
    /// The pane was closed or replaced, or the host answered a foreign epoch.
    /// Nothing is sent to the host.
    Cancelled,
    /// The host refused or never answered inside the budget.
    Failed,
}

pub(in crate::app) type HostRecoveryFuture =
    Pin<Box<dyn Future<Output = HostRecovery> + Send + 'static>>;

/// A host-local reconnect's cancellation handle. A pane leaving the workspace
/// sets the flag synchronously; the future checks it before every connect, so
/// a pane removed before its first poll never opens a socket at all — a
/// `notify_waiters` wake would be lost in exactly that window (#23076).
pub(in crate::app) struct HostCancel {
    // A watch keeps the cancellation sticky: a subscriber that arrives after
    // the flag is set still observes `true`, which a `Notify` wake does not.
    tx: watch::Sender<bool>,
}

impl HostCancel {
    fn new() -> Self {
        Self {
            tx: watch::channel(false).0,
        }
    }

    fn cancel(&self) {
        // `send_replace`, not `send`: `send` refuses (leaving the value
        // unchanged) whenever the watcher has no receiver, which is exactly
        // the case that matters here — a pane removed before the recovery
        // future was ever polled. The flag must land regardless.
        let _ = self.tx.send_replace(true);
    }

    fn is_cancelled(&self) -> bool {
        *self.tx.borrow()
    }

    async fn cancelled(&self) {
        let mut rx = self.tx.subscribe();
        if *rx.borrow_and_update() {
            return;
        }
        let _ = rx.changed().await;
    }
}

/// The host-local recoveries currently running, one per pane, plus each
/// pane's cancellation handle.
#[derive(Default)]
pub(in crate::app) struct HostRecoveries {
    set: FuturesUnordered<HostRecoveryFuture>,
    cancellations: std::collections::HashMap<PaneId, Arc<HostCancel>>,
}

impl HostRecoveries {
    pub(in crate::app) fn push(
        &mut self,
        pane_id: PaneId,
        cancel: Arc<HostCancel>,
        future: HostRecoveryFuture,
    ) {
        self.cancellations.insert(pane_id, cancel);
        self.set.push(future);
    }

    pub(in crate::app) fn is_empty(&self) -> bool {
        self.set.is_empty()
    }

    pub(in crate::app) fn clear(&mut self) {
        self.set.clear();
        self.cancellations.clear();
    }

    /// Cancel the recovery for `pane_id`: the pane was closed or replaced, so
    /// its in-flight host connect is dropped instead of attaching.
    pub(in crate::app) fn cancel(&mut self, pane_id: PaneId) {
        if let Some(cancel) = self.cancellations.remove(&pane_id) {
            cancel.cancel();
        }
    }

    /// Cancel every recovery whose pane has left the workspace. A pane
    /// replaced or closed while its host connect was in flight must not come
    /// back and attach (#23076).
    pub(in crate::app) fn retain_existing(&mut self, workspace: &Workspace<LiveDaemon>) {
        let gone: Vec<PaneId> = self
            .cancellations
            .keys()
            .copied()
            .filter(|pane_id| !workspace.panes.contains_key(pane_id))
            .collect();
        for pane_id in gone {
            self.cancel(pane_id);
        }
    }

    pub(in crate::app) async fn next(&mut self) -> Option<HostRecovery> {
        self.set.next().await
    }
}

impl Workspace<LiveDaemon> {
    /// Start a host-local reconnect for a direct pane whose frame source just
    /// died, or `None` when this pane has no direct locator to use.
    ///
    /// Nothing is given up first: the pane keeps its attachment, lease and
    /// control state so the carried grant still types once the stream is back.
    pub(in crate::app) fn begin_host_recovery(
        &mut self,
        pane_id: PaneId,
    ) -> Option<(Arc<HostCancel>, HostRecoveryFuture)> {
        if !self.frame_delivery.allows(Transport::Direct) {
            return None;
        }
        let (locator, cols, rows) = {
            let pane = self.panes.get(&pane_id)?;
            let locator = pane.host_locator()?;
            let (cols, rows) = pane.viewport();
            (locator, cols, rows)
        };
        let gobby_home = self.gobby_home.clone();
        let pane = self.panes.get_mut(&pane_id).expect("pane exists");
        let terminal_id = pane.terminal_id.clone();
        // The failed source is never polled again, whatever state it left.
        let _ = pane.take_frame_source();
        // The recovery owns its pane until it lands, exactly as a daemon
        // recovery does, so a due-attach pass cannot race it.
        pane.fallback_in_flight = true;
        self.host_recovering.insert(pane_id);
        self.host_recovered.remove(&pane_id);
        let epoch = locator.frame_host_epoch.clone();
        let cancel = Arc::new(HostCancel::new());
        let future_cancel = Arc::clone(&cancel);
        let future: HostRecoveryFuture = Box::pin(async move {
            let deadline = Instant::now() + HOST_RECOVERY_BUDGET;
            let mut delay = HOST_RECOVERY_RETRY_BASE;
            let outcome = loop {
                // The pane may have left before this future was ever polled;
                // the flag is set synchronously, so the socket is never opened.
                if future_cancel.is_cancelled() {
                    break HostRecoveryOutcome::Cancelled;
                }
                // The host is mid-exec when the source dies, so the first
                // attempt waits a beat: a pane closed in the same event batch
                // is then removed and the socket is never opened at all, which
                // a later check could not undo (#23076).
                tokio::select! {
                    biased;
                    _ = future_cancel.cancelled() => {
                        break HostRecoveryOutcome::Cancelled;
                    }
                    _ = tokio::time::sleep(delay) => {}
                }
                let attempt = connect_host(gobby_home.as_deref(), &locator, cols, rows);
                tokio::select! {
                    biased;
                    _ = future_cancel.cancelled() => {
                        break HostRecoveryOutcome::Cancelled;
                    }
                    connected = attempt => match connected {
                        Ok(source) => break HostRecoveryOutcome::Restored(source),
                        // A definite failure has no recovery: the host moved
                        // to another image, refused, or spoke a protocol the
                        // client cannot continue on. Retrying only delays the
                        // daemon fallback the user ends up needing.
                        Err(error) if !host_connect_is_transient(&error) => {
                            break HostRecoveryOutcome::Failed;
                        }
                        Err(_) if Instant::now() >= deadline => {
                            break HostRecoveryOutcome::Failed;
                        }
                        Err(_) => {}
                    },
                }
                delay = (delay * 2).min(HOST_RECOVERY_RETRY_MAX);
            };
            HostRecovery {
                pane_id,
                terminal_id,
                epoch,
                outcome,
            }
        });
        Some((cancel, future))
    }

    /// Apply a finished host-local reconnect by pane identity and epoch, never
    /// by daemon generation. Returns the daemon recovery to run when the host
    /// could not be reached and the daemon path takes over.
    pub(in crate::app) fn apply_host_recovery(
        &mut self,
        recovery: HostRecovery,
    ) -> Option<RecoveryFuture> {
        let HostRecovery {
            pane_id,
            terminal_id,
            epoch,
            outcome,
        } = recovery;
        self.host_recovering.remove(&pane_id);
        let live = self.panes.get(&pane_id).is_some_and(|pane| {
            pane.terminal_id == terminal_id && pane.expected_host_epoch == epoch
        });
        match outcome {
            HostRecoveryOutcome::Restored(source) if live => {
                let pane = self.panes.get_mut(&pane_id).expect("pane exists");
                // Only the stream is new: the attachment id, lease and grant
                // were never given up. The daemon outage may have cleared the
                // pane's control, so the carried grant is put back here.
                pane.install_frame_source(PaneFrameSource::Direct(source));
                pane.live = true;
                pane.restore_host_control();
                self.host_recovered.insert(pane_id);
                let generation = self.daemon.generation();
                self.attached_generation.insert(pane_id, generation);
                None
            }
            HostRecoveryOutcome::Restored(_) => {
                // A different terminal image answered: not ours.
                if let Some(pane) = self.panes.get_mut(&pane_id) {
                    pane.fallback_in_flight = false;
                }
                None
            }
            HostRecoveryOutcome::Cancelled => {
                // The pane was closed or replaced: drop the slots with it.
                self.host_recovered.remove(&pane_id);
                None
            }
            HostRecoveryOutcome::Failed => {
                // An epoch change, an unreachable host, or an exhausted
                // budget: the daemon path takes over unchanged.
                if let Some(pane) = self.panes.get_mut(&pane_id) {
                    pane.fallback_in_flight = false;
                }
                self.begin_daemon_recovery(pane_id)
            }
        }
    }

    /// Whether this pane is attached straight to its host, so the daemon's
    /// reconcile pass leaves the attachment alone.
    pub(in crate::app) fn pane_host_attached(&self, pane_id: PaneId) -> bool {
        self.host_recovered.contains(&pane_id)
    }
}

/// Whether a failed host connect is worth retrying inside the budget. A host
/// mid-exec refuses or drops the connect (`Io`, `Eof`); the listener fd
/// survives the exec, so the next attempt can still land. An epoch change, a
/// protocol error or a refused attach is terminal for this recovery: the
/// daemon path takes over at once.
fn host_connect_is_transient(error: &FrameError) -> bool {
    matches!(
        error,
        FrameError::Io(_) | FrameError::Eof | FrameError::Lag | FrameError::Cancelled
    )
}

async fn connect_host(
    gobby_home: Option<&std::path::Path>,
    locator: &AttachLocator,
    cols: u16,
    rows: u16,
) -> Result<UnixSocketFrameSource, FrameError> {
    match gobby_home {
        Some(home) => UnixSocketFrameSource::from_gobby_home(home, locator, cols, rows).await,
        None => UnixSocketFrameSource::from_env(locator, cols, rows).await,
    }
}
