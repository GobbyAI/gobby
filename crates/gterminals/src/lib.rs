//! Terminal route family: daemon-side clients for the gterm host wire.
//!
//! The gterm host stays the PTY owner. This crate speaks its two existing
//! sockets unchanged: newline-delimited JSON control ([`control`]) and
//! length-prefixed bincode frames ([`frames`]). The wire contract, not the
//! gterm implementation, is the seam: the frame types here mirror gterm's and
//! are pinned to its golden corpus by `tests/protocol_contract.rs`.
//!
//! `RouteFamily` is defined in gdaemon, which links this crate, so the family
//! identity is exported as constants for gdaemon's routing table to use.

pub mod control;
pub mod frames;

/// The family key in bootstrap `front_door.routes`.
pub const FAMILY_NAME: &str = "terminal_ws";

/// Path prefixes the terminal family claims, matched on whole segments.
pub const ROUTE_PREFIXES: &[&str] = &["/api/terminals"];
