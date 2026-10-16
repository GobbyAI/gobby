//! Native WebSocket transport primitives.
//!
//! These modules have no listener, auth, database, or process dependency:
//! sockets arrive from the caller as sinks, so each module can be replayed
//! against the shared Python corpus and in-memory writers.

pub mod broker;
pub mod subscriptions;
