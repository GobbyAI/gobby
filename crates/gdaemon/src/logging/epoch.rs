//! Applies each configuration epoch's `logging.*` keys to the routed layer.
//!
//! Every epoch the channel carries is applied, whatever its revision: the publisher
//! decides which epoch is current, and a restore can lower the stored revision.

use std::path::{Path, PathBuf};
use std::sync::Arc;

use gobby_core::family::ConfigEpoch;
use tokio::sync::watch;
use tokio::task::JoinHandle;

use super::layer::RouteHandle;
use super::settings::{from_values, resolve_dir};
use super::sink::active_routes;

/// Applies every epoch `rx` sees to the process's activated routed layer. An epoch that
/// arrives before activation is not applied; the next one after activation is.
pub fn spawn_applier(rx: watch::Receiver<ConfigEpoch>) -> JoinHandle<()> {
    tokio::spawn(apply_each(rx, active_routes, resolve_configured_dir))
}

/// Applies every epoch `rx` sees to the layer behind `handle`, including an epoch whose
/// revision is lower than the last one applied. The configured directory is used as
/// given.
#[cfg(test)]
pub(crate) fn spawn_applier_to(
    handle: RouteHandle,
    rx: watch::Receiver<ConfigEpoch>,
) -> JoinHandle<()> {
    tokio::spawn(apply_each(
        rx,
        move || Some(handle.clone()),
        |dir: &Path| Some(dir.to_path_buf()),
    ))
}

/// Applies the current epoch, then each change, until the publisher is gone.
async fn apply_each(
    mut rx: watch::Receiver<ConfigEpoch>,
    routes: impl Fn() -> Option<RouteHandle>,
    resolve: impl Fn(&Path) -> Option<PathBuf>,
) {
    loop {
        let values = Arc::clone(&rx.borrow_and_update().active_values);
        if let Some(handle) = routes() {
            let current = handle.settings();
            let (mut next, warnings) = from_values(&values, &current);
            next.dir = resolve(&next.dir).unwrap_or(current.dir);
            handle.replace_settings(next);
            for warning in warnings {
                tracing::warn!(target: "gobby.daemon", "{warning}");
            }
        }
        if rx.changed().await.is_err() {
            return;
        }
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
