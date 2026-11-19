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

/// Every route family, one per line, in alphabetical order.
#[rustfmt::skip]
pub static FAMILIES: &[&dyn RouteFamily<FrontDoorState>] = &[
    &HealthFamily,
    &TerminalFamily,
];

/// The native surfaces the front door claims.
pub fn surfaces() -> Vec<&'static dyn RouteFamily<FrontDoorState>> {
    Vec::new()
}

/// The families the front door may route to, for a pool and a home directory.
pub fn selectable(
    _pool: Option<&Arc<Pool>>,
    _home: &Path,
) -> Vec<&'static dyn RouteFamily<FrontDoorState>> {
    Vec::new()
}
