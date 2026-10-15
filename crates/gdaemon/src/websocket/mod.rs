//! Native WebSocket transport primitives.
//!
//! Each module here is pure protocol logic with no socket, auth, database, or
//! process dependency, so it can be replayed against the shared Python corpus.

pub mod subscriptions;
