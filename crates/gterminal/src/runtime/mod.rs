//! Terminal runtime plumbing (id, spawn/write/resize registry, title).

#[cfg(test)]
mod history_read;
mod id;
// reason: the crate module is `runtime`; this inner file owns TerminalRuntime.
#[allow(clippy::module_inception)]
mod runtime;
#[cfg(test)]
mod runtime_registry;
mod title;

pub use crate::pane::{PaneRuntime, PaneShellConfig, ShellMode};
pub use id::TerminalId;
pub use runtime::TerminalRuntime;
