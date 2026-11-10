//! Fleet health watcher, run as `gdaemon fleet-watch`.
//!
//! The watcher is its own OS process. It samples the daemon's public health
//! endpoint, new `daemon.log` lines and a few host signals every 30 seconds,
//! classifies them over a rolling window, and atomically rewrites one state
//! file. Python admission reads that file; it never runs inside this process.

pub mod classify;
pub mod host;
pub mod logtail;
pub mod paths;
pub mod policy;
pub mod policy_source;
pub mod probe;
pub mod state_file;
pub mod watch;

pub use classify::{
    HostSignals, LogSeverity, Probe, Sample, State, Verdict, classify, classify_log_line,
};
pub use policy::Thresholds;
pub use state_file::{StateFile, ThresholdsSource};
