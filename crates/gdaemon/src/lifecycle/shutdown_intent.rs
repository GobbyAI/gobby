//! Non-consuming Python shutdown marker observations for a backend generation.

use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{
    collections::HashSet,
    io,
    path::{Path, PathBuf},
};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Intent {
    Stop,
    Restart,
}

pub struct IntentMarker {
    path: PathBuf,
    before_spawn: Option<Vec<u8>>,
    consumed: HashSet<[u8; 32]>,
}

impl IntentMarker {
    pub fn new(home: &Path) -> Self {
        Self {
            path: home.join("shutdown_intent_active.json"),
            before_spawn: None,
            consumed: HashSet::new(),
        }
    }

    /// Snapshot bytes, keeping the marker in place for planned-shutdown hook guards.
    pub async fn before_spawn(&mut self) -> io::Result<()> {
        self.before_spawn = self.read().await?;
        Ok(())
    }

    pub async fn after_exit(&mut self, now: f64) -> io::Result<Option<Intent>> {
        let Some(bytes) = self.read().await? else {
            return Ok(None);
        };
        if self.before_spawn.as_ref() == Some(&bytes) {
            return Ok(None);
        }
        let digest: [u8; 32] = Sha256::digest(&bytes).into();
        if self.consumed.contains(&digest) {
            return Ok(None);
        }
        let Some(record) = serde_json::from_slice::<Value>(&bytes)
            .ok()
            .filter(Value::is_object)
        else {
            return Ok(None);
        };
        let timestamp = record.get("timestamp").and_then(|value| {
            value
                .as_f64()
                .or_else(|| value.as_str()?.trim().parse().ok())
        });
        let Some(timestamp) = timestamp.filter(|value| value.is_finite()) else {
            return Ok(None);
        };
        let age = now - timestamp;
        if !now.is_finite() || !(0.0..120.0).contains(&age) {
            return Ok(None);
        }
        self.consumed.insert(digest);
        Ok(Some(
            if record.get("intent").and_then(Value::as_str) == Some("restart") {
                Intent::Restart
            } else {
                // Python treats maintenance and unknown intents as a stop for this owner.
                Intent::Stop
            },
        ))
    }

    async fn read(&self) -> io::Result<Option<Vec<u8>>> {
        match tokio::fs::read(&self.path).await {
            Ok(bytes) => Ok(Some(bytes)),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
            Err(error) => Err(error),
        }
    }
}
