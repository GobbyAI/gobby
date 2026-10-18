//! Durable node-local agent ownership.
pub mod state;

use anyhow::{Context, Result};
use state::AgentState;
use std::{
    fs,
    io::ErrorKind,
    path::{Path, PathBuf},
};
use tokio::sync::{Mutex, Notify};

pub struct NodeAgents {
    pub home: PathBuf,
    pub state: Mutex<AgentState>,
    pub changed: Notify,
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
        })
    }
}
