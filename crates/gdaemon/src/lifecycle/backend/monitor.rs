//! Lease liveness through the pre-release phase of a lease lifetime.
//!
//! This lives apart from `backend.rs` to keep that file under the 1,000-line ceiling.

use super::{BackendSupervisor, SupervisorExit};
use std::{future::Future, pin::pin};
use tokio::time::{MissedTickBehavior, interval};

impl BackendSupervisor {
    /// Runs `work` while the lease keeps its heartbeat, so a database session that ends during
    /// the worker drain or the pre-release hook publishes Lost at once and the fenced job host
    /// aborts its running job. `exit` is the lifetime's exit so far (None for an error or while
    /// it is still running); a lease already lost is not monitored again.
    ///
    /// Each heartbeat is awaited to its end and never dropped part way, since a cancelled
    /// heartbeat discards the session and publishes Lost. The monitor fails closed: any
    /// heartbeat error, including a lease lock wait that fails at its bound, publishes Lost
    /// before `work` runs to its end unmonitored.
    pub(super) async fn heartbeat_through<T>(
        &self,
        exit: Option<SupervisorExit>,
        work: impl Future<Output = T>,
    ) -> T {
        let interval_is_zero = self.config.heartbeat_interval.is_zero();
        if self.lease.is_none() || interval_is_zero || exit == Some(SupervisorExit::LeaseLost) {
            return work.await;
        }
        let mut work = pin!(work);
        let mut heartbeats = interval(self.config.heartbeat_interval);
        heartbeats.set_missed_tick_behavior(MissedTickBehavior::Delay);
        loop {
            tokio::select! { biased;
                output = &mut work => return output,
                _ = heartbeats.tick() => {
                    if let Err(error) = self.heartbeat().await {
                        if let Some(loss) = &self.loss {
                            loss.publish();
                        }
                        tracing::warn!(
                            target: "gobby.lifecycle",
                            "backend lease lost before release: {error:#}"
                        );
                        return work.await;
                    }
                }
            }
        }
    }
}
