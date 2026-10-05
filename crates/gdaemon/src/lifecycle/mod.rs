//! Local lifecycle capabilities. These are not yet wired into production ownership.

#[cfg(unix)]
pub mod pid_file;

pub mod shutdown_intent;

#[cfg(unix)]
pub mod backend;
