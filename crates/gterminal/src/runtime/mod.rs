//! Terminal runtime plumbing (id, spawn/write/resize registry, title).

mod history_read;
mod id;
mod runtime;
mod runtime_registry;
mod title;

pub use id::TerminalId;
pub use runtime::TerminalRuntime;
