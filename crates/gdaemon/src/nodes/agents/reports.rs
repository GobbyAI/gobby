//! Compile-only report API for the #24377 acceptance tests.

use crate::{heartbeat::HeartbeatHost, state::NodeServices};
use anyhow::Result;
use std::sync::Arc;

pub async fn report_once(_node: &NodeServices) -> Result<()> {
    todo!("#24377 node reports are not implemented")
}

pub async fn report_loop(_node: Arc<NodeServices>) {
    todo!("#24377 report loop is not implemented")
}

pub fn register_jobs(_host: &mut HeartbeatHost, _node: Arc<NodeServices>) -> Result<()> {
    todo!("#24377 report job is not implemented")
}
