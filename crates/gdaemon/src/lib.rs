//! Library surface of `gdaemon`, shared by the binary and its integration tests.

pub mod front_door;
pub mod heartbeat;
pub mod lease;
pub mod lifecycle;
pub mod node_commands;
pub mod nodes;
mod retention;
pub mod serve;
#[cfg(unix)]
pub mod state;
#[cfg(unix)]
mod state_services;
