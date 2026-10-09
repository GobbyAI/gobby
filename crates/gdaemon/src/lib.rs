//! Library surface of `gdaemon`, shared by the binary and its integration tests.

pub mod front_door;
pub mod heartbeat;
pub mod lease;
pub mod lifecycle;
mod retention;
pub mod serve;
