//! The process's configuration epoch: the active configuration revision and its values.

use std::future::Future;
use std::sync::Arc;

use serde_json::{Map, Value};
use tokio::sync::{Mutex, watch};

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
/// The only way to publish is [`reload`](Self::reload): it takes the publisher's lock,
/// then reads the stored configuration, then publishes what it read, and holds the lock
/// until subscribers can see it. Every reader (the poller and the configuration write
/// path) goes through it, so the last publish is always the latest read, whatever its
/// revision number. The stored revision can move backwards (a restore to an older
/// revision, or a deleted row read as revision zero), so revision numbers never decide
/// what is published.
pub struct ConfigEpochPublisher {
    tx: watch::Sender<ConfigEpoch>,
    reads: Mutex<()>,
}

impl ConfigEpochPublisher {
    /// Takes the publisher's lock, runs `read` (a fresh read of the stored configuration,
    /// started only once the lock is held), and publishes its epoch when it differs from
    /// the current one, including a lower revision or the same revision with other values.
    /// Returns whether subscribers were notified. A failed read publishes nothing.
    pub async fn reload<E, F, Fut>(&self, read: F) -> Result<bool, E>
    where
        F: FnOnce() -> Fut,
        Fut: Future<Output = Result<ConfigEpoch, E>>,
    {
        let _reading = self.reads.lock().await;
        let epoch = read().await?;
        Ok(self.tx.send_if_modified(|current| {
            if *current == epoch {
                false
            } else {
                *current = epoch;
                true
            }
        }))
    }
}

/// The process's configuration channel, seeded with `initial`: the shared publisher and a
/// receiver that sees each published epoch.
pub fn config_channel(
    initial: ConfigEpoch,
) -> (Arc<ConfigEpochPublisher>, watch::Receiver<ConfigEpoch>) {
    let (tx, rx) = watch::channel(initial);
    (
        Arc::new(ConfigEpochPublisher {
            tx,
            reads: Mutex::new(()),
        }),
        rx,
    )
}
