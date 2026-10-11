//! Native runtime ownership and backend lifecycle capabilities.

#[cfg(unix)]
pub mod pid_file;

pub mod shutdown_intent;

#[cfg(unix)]
pub mod backend;
