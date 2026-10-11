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
    StopTerminals,
    RestartTerminals,
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
        let restart = record.get("intent").and_then(Value::as_str) == Some("restart");
        if !restart {
            match tokio::fs::remove_file(&self.path).await {
                Ok(()) => {}
                Err(error) if error.kind() == io::ErrorKind::NotFound => {}
                Err(error) => return Err(error),
            }
        }
        let drain = record
            .get("details")
            .and_then(|details| details.get("drain_terminals"))
            .and_then(Value::as_bool)
            .unwrap_or(false);
        // Full restart keeps the marker for health clients but retires this
        // supervisor. The CLI starts its successor after the owner exits.
        let restart_backend = restart
            && record
                .get("details")
                .and_then(|details| details.get("restart_scope"))
                .and_then(Value::as_str)
                != Some("supervisor");
        // Python treats maintenance and unknown intents as a stop for this owner.
        Ok(Some(match (restart_backend, drain) {
            (false, false) => Intent::Stop,
            (true, false) => Intent::Restart,
            (false, true) => Intent::StopTerminals,
            (true, true) => Intent::RestartTerminals,
        }))
    }

    async fn read(&self) -> io::Result<Option<Vec<u8>>> {
        match tokio::fs::read(&self.path).await {
            Ok(bytes) => Ok(Some(bytes)),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
            Err(error) => Err(error),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn consumed_stop_removes_marker_and_restart_retains_it() -> io::Result<()> {
        let home = tempfile::tempdir()?;
        let path = home.path().join("shutdown_intent_active.json");
        for (intent, drain, scope, expected) in [
            ("stop", false, None, Intent::Stop),
            ("stop", true, None, Intent::StopTerminals),
            ("restart", false, None, Intent::Restart),
            ("restart", true, None, Intent::RestartTerminals),
            ("restart", false, Some("supervisor"), Intent::Stop),
            ("restart", true, Some("supervisor"), Intent::StopTerminals),
            ("stop", false, Some("supervisor"), Intent::Stop),
            ("stop", true, Some("supervisor"), Intent::StopTerminals),
        ] {
            let mut marker = IntentMarker::new(home.path());
            marker.before_spawn().await?;
            let bytes = serde_json::to_vec(&serde_json::json!({
                "intent": intent, "timestamp": 1000.0,
                "details": {"drain_terminals": drain, "restart_scope": scope}
            }))?;
            tokio::fs::write(&path, &bytes).await?;
            assert_eq!(marker.after_exit(1010.0).await?, Some(expected));
            if intent == "restart" {
                assert_eq!(tokio::fs::read(&path).await?, bytes);
                let mut successor = IntentMarker::new(home.path());
                successor.before_spawn().await?;
                assert_eq!(successor.after_exit(1015.0).await?, None);
                tokio::fs::remove_file(&path).await?;
            } else {
                assert!(
                    !path.exists(),
                    "consumed STOP must remove the active marker"
                );
            }
        }
        Ok(())
    }
}
