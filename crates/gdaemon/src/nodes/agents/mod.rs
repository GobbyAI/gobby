//! Durable node-local agent ownership.
pub mod agy_gate;
pub mod commands;
pub mod executable;
#[cfg(unix)]
pub mod host;
mod lanes;
pub mod launch;
pub mod provider_command;
pub mod provider_env;
pub mod repair;
pub mod reports;
pub mod sandbox;
pub mod state;
pub mod trust;
pub mod workspace;

use anyhow::{Context, Result};
use state::AgentState;
use std::{
    collections::HashMap,
    fs,
    io::ErrorKind,
    path::{Path, PathBuf},
    sync::Arc,
};
use tokio::sync::{Mutex, Notify, watch};

pub struct NodeAgents {
    pub home: PathBuf,
    pub state: Mutex<AgentState>,
    pub changed: Notify,
    /// Serializes node reports, so a slower report never posts after a newer one.
    pub report_lock: Mutex<()>,
    /// Arrival-ordered turns for the commands that name each run.
    lanes: Arc<lanes::Lanes>,
    /// Runs whose preparation task is still live.
    preparing: std::sync::Mutex<HashMap<String, Preparing>>,
    #[cfg(unix)]
    pub host: host::HostControl,
}

/// A live preparation task. The task holds `busy` until it ends, so a cancel that waits on
/// `busy` reads the run's final phase.
struct Preparing {
    /// Set by a cancel; the task selects on it, which stops an awaited seed child.
    cancel: watch::Sender<bool>,
    busy: Arc<Mutex<()>>,
}

impl NodeAgents {
    pub fn open(home: &Path) -> Result<Self> {
        let path = home.join("node/agents.json");
        let mut state = match fs::read(&path) {
            Ok(bytes) => serde_json::from_slice::<AgentState>(&bytes).with_context(|| {
                format!(
                    "cannot parse node agent state {}; move it aside to reset",
                    path.display()
                )
            })?,
            Err(error) if error.kind() == ErrorKind::NotFound => AgentState::default(),
            Err(error) => {
                return Err(error)
                    .with_context(|| format!("cannot read node agent state {}", path.display()));
            }
        };
        state.recover_after_restart();
        let changed = Notify::new();
        // No handle has escaped yet; recovery is persisted before consumers
        // can acquire the mutex or launch a report.
        state.commit(home, &changed)?;
        Ok(Self {
            home: home.to_owned(),
            state: Mutex::new(state),
            changed,
            report_lock: Mutex::new(()),
            lanes: Arc::default(),
            preparing: Default::default(),
            #[cfg(unix)]
            host: host::HostControl::new(home),
        })
    }
}
