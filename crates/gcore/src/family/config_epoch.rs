//! The process's configuration epoch: the active configuration revision and its values.

use std::sync::Arc;

use serde_json::{Map, Value};
use tokio::sync::watch;

/// One published revision of the active configuration.
#[derive(Clone, Debug, PartialEq)]
pub struct ConfigEpoch {
    /// The active-state revision. Zero means nothing has been published yet.
    pub revision: i64,
    /// The active values of that revision.
    pub active_values: Arc<Map<String, Value>>,
}

impl ConfigEpoch {
    /// The epoch before any revision is published: revision zero and no values.
    pub fn unpublished() -> Self {
        Self {
            revision: 0,
            active_values: Arc::new(Map::new()),
        }
    }
}

/// The one publisher of the process's configuration epoch.
///
/// Revisions only move forward. A publish whose revision is not greater than the current
/// one is dropped, and the comparison and the swap happen under the channel's own lock, so
/// a slow read of an older revision that finishes after a newer publish never reverts the
/// epoch. There is no reset: the configuration store numbers every write, a restore
/// included, with a revision above every earlier one, so a restore arrives as an ordinary
/// newer revision. A publisher starts at the revision of the epoch it was built with.
pub struct ConfigEpochPublisher {
    tx: watch::Sender<ConfigEpoch>,
}

impl ConfigEpochPublisher {
    /// Publishes `epoch` when its revision is greater than the current one.
    pub fn publish(&self, epoch: ConfigEpoch) {
        let _ = (&self.tx, epoch);
    }
}

/// The process's configuration channel, seeded with `initial`: the shared publisher and a
/// receiver that sees each published revision.
pub fn config_channel(
    initial: ConfigEpoch,
) -> (Arc<ConfigEpochPublisher>, watch::Receiver<ConfigEpoch>) {
    let (tx, rx) = watch::channel(initial);
    (Arc::new(ConfigEpochPublisher { tx }), rx)
}
