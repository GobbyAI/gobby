//! Backend and lease states that the daemon publishes and families read.

use std::time::Duration;

/// What the backend supervisor is doing with the worker process.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum BackendStatus {
    /// No worker runs because the daemon holds no lease.
    Standby,
    /// A worker with this pid is starting or running.
    Starting { pid: u32 },
    /// The worker exited and the supervisor waits this long before the next spawn.
    Backoff { delay: Duration },
    /// The supervisor refuses to start a worker, for this reason.
    Refused { reason: String },
    /// The supervisor stopped.
    Stopped,
}

/// Whether the daemon holds the hub lease.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LeaseState {
    /// Waiting to acquire the lease.
    Standby,
    /// The lease is held.
    Active,
    /// The lease was held and has been lost.
    Lost,
}
