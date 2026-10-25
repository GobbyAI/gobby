//! Wire contract for the node code-index routes.
//!
//! The node and the hub compile against these types. Every route is `POST` with a
//! JSON body; the hub answers reads from the database and never from the node's disk.

pub mod hub;

#[cfg(test)]
mod tests;

use serde::{Deserialize, Serialize};

use crate::models::ContentSearchHit;

/// Operation name the grant names for the whole node family.
pub const NODE_OPERATION: &str = "";
/// Family prefix every node route sits under.
pub const NODE_ROUTE_PREFIX: &str = "";
pub const STATUS_ROUTE: &str = "";
pub const SEARCH_CONTENT_ROUTE: &str = "";
pub const INDEX_PLAN_ROUTE: &str = "";
pub const INDEX_APPLY_ROUTE: &str = "";
/// Every route path in the family. The claims check reads this list.
pub const ROUTE_PATHS: &[&str] = &[];

/// Which project root a node request addresses, and its parent when it is an overlay.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct NodeScope {
    pub root: String,
    pub parent_root: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StatusRequest {
    pub scope: NodeScope,
}

/// Status report. Its fields land with the collect and render split.
#[derive(Debug)]
pub struct StatusReport;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SearchContentRequest {
    pub scope: NodeScope,
    pub query: String,
    pub language: Option<String>,
    pub paths: Vec<String>,
    pub limit: usize,
    pub offset: usize,
}

/// Hub half of a content search: candidates before the node's disk check.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ContentCandidates {
    pub results: Vec<ContentSearchHit>,
    pub db_total: Option<usize>,
    pub hint: Option<String>,
}

/// Body every node route error uses.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RouteError {
    pub code: String,
    pub error: String,
    pub message: String,
}
