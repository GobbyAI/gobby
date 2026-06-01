use std::fs::{self, OpenOptions};
use std::io::{ErrorKind, Write};
use std::path::{Path, PathBuf};
use std::thread;
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};

use crate::WikiError;

const EVENT_LOG_LOCK_TIMEOUT: Duration = Duration::from_secs(5);

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SessionEvent {
    pub session_id: String,
    pub dispatch_id: Option<String>,
    pub kind: String,
    pub message: String,
    pub timestamp_ms: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EventMonitor {
    path: PathBuf,
}

impl EventMonitor {
    pub fn for_vault(vault_root: &Path) -> Self {
        Self {
            path: vault_root.join(".gwiki").join("session-events.jsonl"),
        }
    }

    pub fn append_event(&self, event: &SessionEvent) -> Result<(), WikiError> {
        if let Some(parent) = self.path.parent() {
            fs::create_dir_all(parent).map_err(|error| WikiError::Io {
                action: "create session event directory",
                path: Some(parent.to_path_buf()),
                source: error,
            })?;
        }

        let mut line = serde_json::to_vec(event).map_err(|error| WikiError::Json {
            action: "serialize session event",
            path: Some(self.path.clone()),
            source: error,
        })?;
        line.push(b'\n');
        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)
            .map_err(|error| WikiError::Io {
                action: "open session event log",
                path: Some(self.path.clone()),
                source: error,
            })?;
        lock_event_log(&file, &self.path)?;
        file.write_all(&line).map_err(|error| WikiError::Io {
            action: "append session event",
            path: Some(self.path.clone()),
            source: error,
        })?;
        file.flush().map_err(|error| WikiError::Io {
            action: "flush session event log",
            path: Some(self.path.clone()),
            source: error,
        })?;
        file.sync_all().map_err(|error| WikiError::Io {
            action: "sync session event log",
            path: Some(self.path.clone()),
            source: error,
        })
    }
}

fn lock_event_log(file: &fs::File, path: &Path) -> Result<(), WikiError> {
    let started = Instant::now();
    loop {
        match fs4::FileExt::try_lock(file) {
            Ok(()) => return Ok(()),
            Err(fs4::TryLockError::WouldBlock) => {
                let elapsed = started.elapsed();
                if elapsed >= EVENT_LOG_LOCK_TIMEOUT {
                    return Err(WikiError::Io {
                        action: "lock session event log",
                        path: Some(path.to_path_buf()),
                        source: std::io::Error::new(
                            ErrorKind::TimedOut,
                            format!("timed out after {}ms", EVENT_LOG_LOCK_TIMEOUT.as_millis()),
                        ),
                    });
                }
                thread::sleep(Duration::from_millis(25).min(EVENT_LOG_LOCK_TIMEOUT - elapsed));
            }
            Err(error) => {
                return Err(WikiError::Io {
                    action: "lock session event log",
                    path: Some(path.to_path_buf()),
                    source: error.into(),
                });
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use serde_json::Value;

    use super::*;

    #[test]
    fn events_append_jsonl() {
        let temp = tempfile::tempdir().expect("tempdir");
        let monitor = EventMonitor::for_vault(temp.path());

        monitor
            .append_event(&SessionEvent {
                session_id: "research-1".to_string(),
                dispatch_id: Some("dispatch-1".to_string()),
                kind: "worker_started".to_string(),
                message: "worker 1 started".to_string(),
                timestamp_ms: 10,
            })
            .expect("first event appended");
        monitor
            .append_event(&SessionEvent {
                session_id: "research-1".to_string(),
                dispatch_id: Some("dispatch-1".to_string()),
                kind: "note_accepted".to_string(),
                message: "accepted source note".to_string(),
                timestamp_ms: 20,
            })
            .expect("second event appended");

        let log_path = temp.path().join(".gwiki/session-events.jsonl");
        let contents = std::fs::read_to_string(log_path).expect("jsonl exists");
        let lines = contents.lines().collect::<Vec<_>>();

        assert_eq!(lines.len(), 2);
        let first: Value = serde_json::from_str(lines[0]).expect("first event is json");
        let second: Value = serde_json::from_str(lines[1]).expect("second event is json");
        assert_eq!(first["kind"], "worker_started");
        assert_eq!(second["kind"], "note_accepted");
        assert_eq!(second["dispatch_id"], "dispatch-1");
    }
}
