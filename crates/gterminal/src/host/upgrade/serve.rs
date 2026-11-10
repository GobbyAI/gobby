//! Serving the recorded outcome. A drain waits, up to `SERVE_GRACE`, for a
//! client to read it. A reply counts as having served it only once the reply
//! write completes, and only while the reply's record is still current.

use std::sync::atomic::Ordering;
use std::sync::{Arc, PoisonError};
use std::time::Duration;

use serde_json::Value;
use tokio::sync::MutexGuard;

use super::{Attempts, SERVE_GRACE};
use crate::host::backpressure::OnWritten;
use crate::host::handover::UpgradeRecord;
use crate::host::state::HostState;

/// Waits for the attempt in flight to settle. When an outcome is recorded, it
/// gives a client up to `SERVE_GRACE` to read it, then runs `teardown` while
/// `upgrade_lock` is held. The returned guard keeps the lock to shutdown, and the
/// second value is what `teardown` returned.
pub(crate) async fn drain_endpoint<R>(
    state: &HostState,
    teardown: impl FnOnce() -> R,
) -> (MutexGuard<'_, ()>, R) {
    let settled = state.attempts.settled().await;
    let recorded = state
        .upgrade
        .lock()
        .unwrap_or_else(PoisonError::into_inner)
        .as_ref()
        .is_some_and(|record| record.outcome.is_some());
    if recorded {
        state.attempts.await_served(SERVE_GRACE).await;
    }
    let torn_down = teardown();
    (settled, torn_down)
}

impl Attempts {
    /// Records that a client read the recorded outcome. Called under the
    /// `upgrade` lock, from `mark_served`.
    pub(crate) fn mark_outcome_served(&self) {
        self.served.store(true, Ordering::SeqCst);
        self.read.notify_waiters();
    }

    /// Waits up to `grace` for a client to read the recorded outcome; a read
    /// that already happened returns at once.
    async fn await_served(&self, grace: Duration) {
        let served = async {
            loop {
                // Create the waiter before checking, so a read between the check
                // and the wait is not missed.
                let read = self.read.notified();
                if self.served.load(Ordering::SeqCst) {
                    return;
                }
                read.await;
            }
        };
        let _ = tokio::time::timeout(grace, served).await;
    }
}

impl HostState {
    /// The write hook for a reply that carries a recorded outcome: the outcome
    /// counts as served only once the reply is written.
    pub(in crate::host) fn served_reply(self: &Arc<Self>, reply: &Value) -> Option<OnWritten> {
        let record_seq = reply["upgrade"]["last_outcome"]["record_seq"].as_u64()?;
        let state = Arc::clone(self);
        Some(OnWritten(Box::new(move || mark_served(&state, record_seq))))
    }

    /// Replaces the recorded outcome. The sequence bump and the `served` reset
    /// happen under the `upgrade` lock, so a reply built from an older record
    /// cannot mark this one, even when both records carry the same attempt id.
    pub(super) fn replace_record(&self, record: UpgradeRecord) {
        let mut upgrade = self.upgrade.lock().unwrap_or_else(PoisonError::into_inner);
        self.attempts.record_seq.fetch_add(1, Ordering::SeqCst);
        self.attempts.served.store(false, Ordering::SeqCst);
        *upgrade = Some(record);
    }
}

/// Marks the recorded outcome served while the reply's record is still the
/// current one. A reply for an older record must not mark a newer one.
fn mark_served(state: &HostState, record_seq: u64) {
    let _upgrade = state.upgrade.lock().unwrap_or_else(PoisonError::into_inner);
    if state.attempts.record_seq.load(Ordering::SeqCst) == record_seq {
        state.attempts.mark_outcome_served();
    }
}
