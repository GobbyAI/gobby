//! Applies each configuration epoch's `logging.*` keys to the routed layer.
//!
//! Every epoch the channel carries is applied, whatever its revision: the publisher
//! decides which epoch is current, and a restore can lower the stored revision. An epoch
//! is the whole stored configuration, so a key it does not carry returns to the settings
//! routing activated with. A rejected setting is reported on stderr, not through the
//! routes, so the floor it leaves in force cannot hide it.

use std::path::{Path, PathBuf};
use std::sync::Arc;

use gobby_core::family::ConfigEpoch;
use tokio::sync::watch;
use tokio::task::JoinHandle;

use super::layer::RouteHandle;
use super::settings::{from_values, resolve_dir};
use super::sink::{active_routes, raw_stderr};

/// Applies the current epoch of `rx` now, then spawns the task that applies each later
/// one, to the process's activated routed layer. An epoch applied before activation
/// changes nothing; the next one after activation is applied.
pub fn spawn_applier(mut rx: watch::Receiver<ConfigEpoch>) -> JoinHandle<()> {
    apply_current(&mut rx);
    tokio::spawn(follow(rx))
}

/// Applies the current epoch of `rx` to the process's activated routed layer.
pub fn apply_current(rx: &mut watch::Receiver<ConfigEpoch>) {
    apply_latest(rx, active_routes, resolve_configured_dir);
}

/// Applies each epoch `rx` sees after the current one to the process's activated routed
/// layer, until the publisher is gone.
pub async fn follow(rx: watch::Receiver<ConfigEpoch>) {
    follow_with(rx, active_routes, resolve_configured_dir).await;
}

/// Applies every epoch `rx` sees to the layer behind `handle`, including an epoch whose
/// revision is lower than the last one applied. The configured directory is used as
/// given.
#[cfg(test)]
pub(crate) fn spawn_applier_to(
    handle: RouteHandle,
    mut rx: watch::Receiver<ConfigEpoch>,
) -> JoinHandle<()> {
    let routes = move || Some(handle.clone());
    let resolve = |dir: &Path| Some(dir.to_path_buf());
    apply_latest(&mut rx, &routes, resolve);
    tokio::spawn(follow_with(rx, routes, resolve))
}

/// Applies each change `rx` sees until the publisher is gone.
async fn follow_with(
    mut rx: watch::Receiver<ConfigEpoch>,
    routes: impl Fn() -> Option<RouteHandle>,
    resolve: impl Fn(&Path) -> Option<PathBuf>,
) {
    while rx.changed().await.is_ok() {
        apply_latest(&mut rx, &routes, &resolve);
    }
}

/// Applies the latest epoch of `rx` to the layer `routes` returns, when there is one.
fn apply_latest(
    rx: &mut watch::Receiver<ConfigEpoch>,
    routes: impl Fn() -> Option<RouteHandle>,
    resolve: impl Fn(&Path) -> Option<PathBuf>,
) {
    let values = Arc::clone(&rx.borrow_and_update().active_values);
    let Some(handle) = routes() else {
        return;
    };
    let current = handle.settings();
    let defaults = handle.defaults();
    let (mut next, warnings) = from_values(&values, &current, &defaults);
    // The activation directory is already resolved; a configured one resolves here.
    if next.dir != defaults.dir {
        next.dir = resolve(&next.dir).unwrap_or(current.dir);
    }
    handle.replace_settings(next);
    for warning in warnings {
        raw_stderr(&format!("gdaemon logging: {warning}"));
    }
}

/// The directory a configured `logging.dir` names in this process: `GOBBY_LOGGING_DIR`
/// first, and Python's default under the Gobby home. None keeps the current directory,
/// when the Gobby home cannot be determined.
fn resolve_configured_dir(dir: &Path) -> Option<PathBuf> {
    let home = gobby_core::gobby_home().ok()?;
    Some(resolve_dir(
        &|name| std::env::var(name).ok(),
        Some(dir),
        &home,
    ))
}
