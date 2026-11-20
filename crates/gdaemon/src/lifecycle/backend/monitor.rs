//! Lease liveness through a supervised lifetime: the heartbeat's lock bound, the pulse that
//! owns the heartbeat in flight, and lease-scoped work.
//!
//! This lives apart from `backend.rs` to keep that file under the 1,000-line ceiling.

use super::{BackendSupervisor, SupervisorExit, shutdown_signal};
use crate::lease::standby::RECOVERY_TIMEOUT;
use crate::lifecycle::shutdown_intent::ExitReadHold;
use anyhow::Result;
use std::{future::Future, pin::Pin, pin::pin, time::Duration};
use tokio::{
    sync::watch,
    time::{Instant, Interval, MissedTickBehavior, interval, sleep_until},
};

/// Longest a supervised heartbeat waits for the lease lock. While a lifetime is open the only
/// other I/O done under that lock is the status probe's, bounded by `RECOVERY_TIMEOUT`; this is
/// that bound plus one production heartbeat interval, so one slow probe never costs a lifetime
/// and nothing longer is waited for. It is not `BackendConfig::lease_lock_limit`, the startup
/// budget of a lifetime that has admitted nothing.
pub const HEARTBEAT_LOCK_LIMIT: Duration = RECOVERY_TIMEOUT.saturating_add(Duration::from_secs(1));

/// Work that runs only inside a verified lease lifetime.
pub trait LeaseScoped {
    /// Starts the work. Called once, under the lease lock, right after the lifetime's entry
    /// heartbeat verified the lease.
    fn admit(&mut self) -> Result<()>;
    /// Ends the work while the lease is still held (`exit` is None for an error).
    fn end(&mut self, exit: Option<SupervisorExit>) -> impl Future<Output = ()> + Send;
}

impl LeaseScoped for () {
    fn admit(&mut self) -> Result<()> {
        Ok(())
    }

    fn end(&mut self, _: Option<SupervisorExit>) -> impl Future<Output = ()> + Send {
        std::future::ready(())
    }
}

impl BackendSupervisor {
    /// Holds the intent marker's read after each backend exit while `hold.held` is true.
    #[doc(hidden)]
    pub fn hold_exit_reads(&mut self, hold: ExitReadHold) {
        self.exit_read_hold = Some(hold);
    }

    /// One supervised heartbeat. It waits for the lease lock for at most
    /// [`HEARTBEAT_LOCK_LIMIT`]. Every error, a lease lock wait that fails at its bound
    /// included, publishes Lost before it is returned, so a `LeaseLost` exit never leaves
    /// the watch reading Active.
    pub(super) async fn heartbeat(&self) -> Result<()> {
        let Some(lease) = &self.lease else {
            return Ok(());
        };
        let limit = HEARTBEAT_LOCK_LIMIT.min(self.config.lease_lock_limit);
        let beaten = match self.lock_lease(lease, limit).await {
            Ok(mut guard) => guard.heartbeat().await,
            Err(error) => Err(error),
        };
        if beaten.is_err()
            && let Some(loss) = &self.loss
        {
            loss.publish();
        }
        beaten
    }
}

type Beat<'a> = Pin<Box<dyn Future<Output = Result<()>> + Send + 'a>>;

/// The heartbeat of one supervised lifetime. At most one heartbeat is in flight, and it is
/// owned here, not by the select arm that polls it: an arm that loses its select drops only
/// its `next` or `now` wrapper, and the next call, in whatever phase follows, resumes the same
/// heartbeat. A heartbeat is dropped only when the lifetime's own future is.
///
/// A heartbeat in flight must never go unpolled. A heartbeat queued on the lease lock is handed
/// the lock as soon as it is free, polled or not, and then holds it until it is polled again;
/// meanwhile its own bounds do not run, every other user of that lock waits, and the watch reads
/// Active. So from `Pulse::new` to the last `settle`, every await of the supervised lifetime is
/// one of three kinds, and a new await must be made one of them:
/// - a method of this type, or a select with a `next` or `now` arm;
/// - a wait passed to `through`, never awaited bare, however short it looks: every wait that
///   can follow a select which another arm won (a reap, a marker read after an exit), and the
///   marker read after each heartbeat in the wait loop;
/// - a bare await directly after a call here returned the heartbeat's own result, with no
///   select between, so that none is in flight. There are four: the marker snapshot and the
///   spawn after `now`, and the two drains after a heartbeat error.
///
/// The first two kinds are safe by construction: the await itself polls the heartbeat. The
/// third is safe only by where it stands, and nothing here checks that. Before adding a bare
/// await, or a select ahead of one of those four, pass the wait to `through` instead.
pub(super) struct Pulse<'a> {
    supervisor: &'a BackendSupervisor,
    /// None for a zero interval, which `supervise` refuses before any heartbeat.
    ticks: Option<Interval>,
    beat: Option<Beat<'a>>,
}

impl<'a> Pulse<'a> {
    pub(super) fn new(supervisor: &'a BackendSupervisor) -> Self {
        let period = supervisor.config.heartbeat_interval;
        let ticks = (!period.is_zero()).then(|| {
            let mut ticks = interval(period);
            ticks.set_missed_tick_behavior(MissedTickBehavior::Delay);
            ticks
        });
        Self {
            supervisor,
            ticks,
            beat: None,
        }
    }

    /// The result of the heartbeat in flight, or of one started at the next tick. Cancel-safe:
    /// a tick that is not reached is not consumed, and the heartbeat is stored before its first
    /// poll and stays stored until it ends.
    pub(super) async fn next(&mut self) -> Result<()> {
        if self.beat.is_none() {
            match &mut self.ticks {
                Some(ticks) => {
                    ticks.tick().await;
                }
                None => std::future::pending::<()>().await,
            }
            self.beat = Some(Box::pin(self.supervisor.heartbeat()));
        }
        self.settle().await
    }

    /// The result of the heartbeat in flight, or of one started at once. Cancel-safe.
    pub(super) async fn now(&mut self) -> Result<()> {
        if self.beat.is_none() {
            self.beat = Some(Box::pin(self.supervisor.heartbeat()));
        }
        self.settle().await
    }

    /// Awaits the heartbeat in flight to its end; Ok when there is none. Cancel-safe.
    pub(super) async fn settle(&mut self) -> Result<()> {
        let Some(beat) = self.beat.as_mut() else {
            return Ok(());
        };
        let beaten = beat.await;
        self.beat = None;
        beaten
    }

    /// Runs `work` beside the heartbeat: the one in flight is polled to its end and the next
    /// starts at its tick. So a database session that ends during a worker drain, a reap, a
    /// marker read (after an exit, or in the wait loop) or the pre-release end publishes Lost
    /// at once and the fenced job host aborts its running job, and a heartbeat handed the
    /// lease lock during `work` gives it back. `work` is polled first and its output is
    /// returned as soon as it ends, whatever the heartbeat does. `exit` is the lifetime's exit
    /// so far (None for an error or while it is still running); a lease already lost is not
    /// monitored again, and no heartbeat is in flight then. A heartbeat error leaves `work` to
    /// run to its end unmonitored. A heartbeat still in flight when `work` ends stays in the
    /// pulse.
    pub(super) async fn through<T>(
        &mut self,
        exit: Option<SupervisorExit>,
        work: impl Future<Output = T>,
    ) -> T {
        if self.ticks.is_none()
            || self.supervisor.lease.is_none()
            || exit == Some(SupervisorExit::LeaseLost)
        {
            return work.await;
        }
        let mut work = pin!(work);
        loop {
            tokio::select! { biased;
                output = &mut work => return output,
                beaten = self.next() => {
                    if let Err(error) = beaten {
                        tracing::warn!(target: "gobby.lifecycle", "backend lease lost before release: {error:#}");
                        return work.await;
                    }
                }
            }
        }
    }

    /// Sleeps out a crash backoff while heartbeating. Returns the exit when a shutdown or a lost
    /// lease ends the wait first, and None when the delay has passed.
    pub(super) async fn back_off(
        &mut self,
        delay: Duration,
        shutdown: &mut watch::Receiver<bool>,
    ) -> Option<SupervisorExit> {
        let deadline = Instant::now() + delay;
        loop {
            tokio::select! { biased;
                _ = shutdown_signal(shutdown) => return Some(SupervisorExit::Stopped),
                _ = sleep_until(deadline) => return None,
                beaten = self.next() => {
                    if let Err(error) = beaten {
                        tracing::warn!(target: "gobby.lifecycle", "backend lease lost: {error:#}");
                        return Some(SupervisorExit::LeaseLost);
                    }
                }
            }
        }
    }
}
