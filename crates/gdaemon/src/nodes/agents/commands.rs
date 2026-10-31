//! Compile-only command entry point for #24376's tests-first contract.
use super::{NodeAgents, launch::LaunchContext};
use serde_json::Value;
use std::sync::Arc;

pub async fn dispatch(
    _agents: Arc<NodeAgents>,
    _context: Arc<LaunchContext>,
    _frame: Value,
) -> Value {
    todo!("#24376 command dispatch has not been implemented")
}
