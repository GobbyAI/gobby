//! Native WebSocket transport.
//!
//! `subscriptions` and `broker` have no listener, auth, database, or process
//! dependency: sockets arrive from the caller as sinks, so each can be
//! replayed against the shared Python corpus and in-memory writers.
//! `transport` terminates public connections and relays each over a private
//! leg to the Python bridge, and `publish` serves the private bind and
//! publish endpoints that prove the backend pair's secret and token.

pub mod broker;
pub mod publish;
pub mod subscriptions;
pub mod transport;
mod transport_support;
