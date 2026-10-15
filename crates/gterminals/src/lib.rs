//! Terminal route family: daemon-side clients for the gterm host wire.
//!
//! The gterm host stays the PTY owner. This crate speaks its two existing
//! sockets unchanged: newline-delimited JSON control ([`control`]) and
//! length-prefixed bincode frames ([`frames`]). The wire contract, not the
//! gterm implementation, is the seam: the frame types here mirror gterm's and
//! are pinned to its golden corpus by `tests/protocol_contract.rs`.
//!
//! [`ws_protocol`] is the backend-neutral terminal WS codec that browsers and
//! gclient speak, pinned to the shared terminal WS corpus by
//! `tests/ws_golden.rs`.
//!
//! `RouteFamily` is defined in gcore (`gobby_core::route_family`); this crate
//! exports the family identity as constants for gdaemon's routing table to use.

pub mod control;
pub mod family;
pub mod frames;
pub mod host;
pub mod leases;
pub mod reconcile;
pub mod repository;
pub mod write;
pub mod ws_protocol;

/// The family key in bootstrap `front_door.routes`.
pub const FAMILY_NAME: &str = "terminal_ws";

/// Path prefixes the terminal family claims, matched on whole segments.
pub const ROUTE_PREFIXES: &[&str] = &["/api/terminals"];
