//! The route-family registry: the one list gdaemon composes its route families from.
//!
//! A family is registered by adding one line to [`FAMILIES`]. [`surfaces`] lists the
//! native surfaces the front door claims, and [`selectable`] lists the families the
//! front door may route to for a given pool.

use std::path::Path;
use std::sync::Arc;

use gobby_core::postgres_pool::Pool;
use gobby_core::route_family::RouteFamily;

use super::FrontDoorState;
use super::health::HealthFamily;
use super::routes::TerminalFamily;
#[cfg(unix)]
use super::surfaces::{LeaseAdmin, TerminalWrite};
use super::surfaces::{Machines, NodeChannel, NodeInternal, RuntimeChallenge, WsBridge, WsNative};

/// Every route family, one per line, in alphabetical order.
#[rustfmt::skip]
pub static FAMILIES: &[&dyn RouteFamily<FrontDoorState>] = &[
    &HealthFamily,
    &TerminalFamily,
];

/// The native surfaces the front door claims, in alphabetical order. The routing table
/// appends them, pinned to `Native`, and only their claim rows reach them. Off unix the
/// two surfaces that need the runtime are absent.
#[cfg(unix)]
pub fn surfaces() -> Vec<&'static dyn RouteFamily<FrontDoorState>> {
    vec![
        &LeaseAdmin,
        &Machines,
        &NodeChannel,
        &NodeInternal,
        &RuntimeChallenge,
        &TerminalWrite,
        &WsBridge,
        &WsNative,
    ]
}

/// The native surfaces the front door claims, in alphabetical order (non-unix build).
#[cfg(not(unix))]
pub fn surfaces() -> Vec<&'static dyn RouteFamily<FrontDoorState>> {
    vec![
        &Machines,
        &NodeChannel,
        &NodeInternal,
        &RuntimeChallenge,
        &WsBridge,
        &WsNative,
    ]
}

/// The families the front door may route to, for a pool and a home directory. Families
/// run in `start_rank` order, with registry order inside each rank.
pub fn selectable(
    _pool: Option<&Arc<Pool>>,
    _home: &Path,
) -> Vec<&'static dyn RouteFamily<FrontDoorState>> {
    let mut families = FAMILIES.to_vec();
    families.sort_by_key(|family| family.start_rank());
    families
}
