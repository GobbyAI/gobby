pub mod fake_hub;

use anyhow::{Context, Result};
use gobby_core::bootstrap::parse_hub_database_bootstrap;
use gobby_daemon::{lifecycle::pid_file::PidFileClaim, state::NodeServices};
use std::sync::Arc;
use tempfile::TempDir;

pub const NODE_KEY: &str = "node-receipt-test-key";

pub struct NodeHome {
    pub home: TempDir,
    pub node: Arc<NodeServices>,
    _claim: PidFileClaim,
}

impl NodeHome {
    pub fn new(hub: &fake_hub::FakeHub) -> Result<Self> {
        let home = tempfile::tempdir()?;
        let path = home.path().join("bootstrap.yaml");
        let contents = format!(
            "datastore_mode: remote\nhub_daemon_url: {}\napi_key: {NODE_KEY}\n",
            hub.origin
        );
        std::fs::write(&path, &contents)?;
        let bootstrap = parse_hub_database_bootstrap(&contents)?.context("node bootstrap")?;
        let (node, claim) =
            NodeServices::prepare(&bootstrap, &path, home.path())?.context("PID claim")?;
        Ok(Self {
            home,
            node,
            _claim: claim,
        })
    }
}
