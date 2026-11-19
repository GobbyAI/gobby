//! Lifecycle types that route families and their hook points share.

/// Whether a worker spawn is the first of the process or a respawn after an exit.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SpawnKind {
    /// The first spawn of the process.
    FirstStart,
    /// A spawn after the worker exited.
    Respawn,
}
