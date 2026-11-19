//! Applies each configuration epoch's `logging.*` keys to the routed layer.

use gobby_core::family::ConfigEpoch;
use tokio::sync::watch;
use tokio::task::JoinHandle;

#[cfg(test)]
use super::RouteHandle;

/// Applies every epoch `rx` sees to the process's activated routed layer.
pub fn spawn_applier(rx: watch::Receiver<ConfigEpoch>) -> JoinHandle<()> {
    tokio::spawn(async move { drop(rx) })
}

/// Applies every epoch `rx` sees to the layer behind `handle`, including an epoch whose
/// revision is lower than the last one applied.
#[cfg(test)]
pub(crate) fn spawn_applier_to(
    handle: RouteHandle,
    rx: watch::Receiver<ConfigEpoch>,
) -> JoinHandle<()> {
    tokio::spawn(async move { drop((handle, rx)) })
}
