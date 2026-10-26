//! Wire contract for the node code-index routes.
//!
//! The node and the hub compile against these types. Every route is `POST` with a
//! JSON body; the hub answers reads from the database and never from the node's disk.

pub mod hub;
pub(crate) mod split;

#[cfg(test)]
mod index_tests;
#[cfg(test)]
mod tests;

use std::path::PathBuf;

use serde::{Deserialize, Serialize};

use crate::bm25_health::Bm25Health;
use crate::index::indexer::{IndexOutcome, UnsupportedFileType};
use crate::models::{ContentSearchHit, IndexedProject};

/// Operation name the grant names for the whole node family.
pub const NODE_OPERATION: &str = "node_code_index";
/// Family prefix every node route sits under.
pub const NODE_ROUTE_PREFIX: &str = "/api/code-index/node";
pub const STATUS_ROUTE: &str = "/api/code-index/node/status";
pub const SEARCH_CONTENT_ROUTE: &str = "/api/code-index/node/search-content";
pub const INDEX_PLAN_ROUTE: &str = "/api/code-index/node/index/plan";
pub const INDEX_APPLY_ROUTE: &str = "/api/code-index/node/index/apply";
/// Every route path in the family. The claims check reads this list.
pub const ROUTE_PATHS: &[&str] = &[
    STATUS_ROUTE,
    SEARCH_CONTENT_ROUTE,
    INDEX_PLAN_ROUTE,
    INDEX_APPLY_ROUTE,
];

/// Which project root a node request addresses, and its parent when it is an overlay.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct NodeScope {
    pub root: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub parent_root: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StatusRequest {
    pub scope: NodeScope,
}

/// Status of a node's project. The JSON object and the text status both render from it.
#[derive(Debug, Serialize, Deserialize)]
#[serde(untagged)]
pub enum StatusReport {
    /// The project has indexed state on the machine.
    Indexed {
        #[serde(flatten)]
        project: Box<IndexedProject>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        overlay: Option<OverlayStatus>,
        code_index: Bm25Health,
    },
    /// The project has no indexed state on the machine.
    Unindexed {
        project_id: String,
        indexed: bool,
        code_index: Bm25Health,
    },
}

/// Overlay shape of an indexed project. Present only for an overlay index.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OverlayStatus {
    pub overlay_project_id: String,
    pub overlay_root: PathBuf,
    pub parent_project_id: String,
    pub parent_root: PathBuf,
    #[serde(default, skip_serializing_if = "is_zero")]
    pub tombstones: usize,
}

fn is_zero(value: &usize) -> bool {
    *value == 0
}

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

/// One file's identity as the node reports it: its content hash and, for a clean tracked
/// file, the Git blob id.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FileHash {
    pub path: String,
    pub content_hash: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub git_blob: Option<String>,
}

/// Facts the node parsed for one file, for the hub's database half to write.
/// Stub shape: the implementation replaces this with the `Parsed` and `ContentOnly` payloads.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ParsedFileFacts {
    pub path: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct IndexPlanRequest {
    pub scope: NodeScope,
    pub full: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub path_filter: Option<String>,
    pub files: Vec<FileHash>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct IndexPlanResponse {
    /// Paths the node must parse: stale, and not adoptable from existing facts.
    pub parse: Vec<String>,
    /// Used by the overlay split (1.5).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub parent_indexed: Option<bool>,
}

/// Everything the hub needs to finish a run once every file's facts are written.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FinalBatch {
    pub present: Vec<FileHash>,
    pub indexed_paths: Vec<String>,
    pub eligible_files: usize,
    pub scanned_files: usize,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub unsupported_file_types: Vec<UnsupportedFileType>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct IndexApplyRequest {
    pub scope: NodeScope,
    pub full: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub path_filter: Option<String>,
    pub files: Vec<ParsedFileFacts>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub final_batch: Option<FinalBatch>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct IndexApplyResponse {
    /// Paths whose facts the node must parse again. A non-empty list skips the post-pass.
    pub reparse: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub outcome: Option<IndexOutcome>,
}
