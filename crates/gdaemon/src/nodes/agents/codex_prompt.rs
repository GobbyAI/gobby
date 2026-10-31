use std::sync::Arc;

use crate::state::NodeServices;

// Compile-only entry point for the tests-first prompt delivery contract (#24379).
pub async fn deliver(_node: Arc<NodeServices>, _run_id: String, _prompt: String) {}
