//! Hub-side read entry points. A hub read answers from the database for the verified
//! principal and never opens a path on the hub's own disk.

use crate::config::Context;
use crate::node::{
    ContentCandidates, NodeScope, RouteError, SearchContentRequest, StatusReport, StatusRequest,
};

/// The verified principal a hub read runs as.
#[derive(Debug, Clone)]
pub struct HubScope {
    pub database_url: String,
    pub machine_id: String,
    pub project_id: String,
    pub overlay_project_id: Option<String>,
}

impl HubScope {
    /// Builds the read context for a node request, after the overlay claims check.
    pub fn context(&self, _scope: &NodeScope) -> Result<Context, RouteError> {
        todo!("1.3: claims check, then Context::for_machine")
    }
}

/// Status of the node's project, read for the principal's machine.
pub fn status(_scope: &HubScope, _request: StatusRequest) -> Result<StatusReport, RouteError> {
    todo!("1.3: read-only connection, then collect")
}

/// Content candidates for the node's project, read for the principal's machine.
pub fn search_content(
    _scope: &HubScope,
    _request: SearchContentRequest,
) -> Result<ContentCandidates, RouteError> {
    todo!("1.3: read-only connection, then content_candidates")
}
