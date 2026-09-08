use serde::{Deserialize, Serialize};

pub const EVIDENCE_SCHEMA_VERSION: u32 = 1;
pub const DEFAULT_MAX_BYTES: usize = 16_384;
pub const DEFAULT_GRAPH_DEPTH: usize = 2;
pub const DEFAULT_RESULT_LIMIT: usize = 1_000;

fn default_max_bytes() -> usize {
    DEFAULT_MAX_BYTES
}

fn default_graph_depth() -> usize {
    DEFAULT_GRAPH_DEPTH
}

fn default_result_limit() -> usize {
    DEFAULT_RESULT_LIMIT
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SnapshotBinding {
    pub project_id: String,
    pub commit_oid: String,
    pub tree_oid: String,
    pub inventory_digest: String,
    pub commit: CommitBinding,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CommitBinding {
    pub parent_oids: Vec<String>,
    pub comparison_parent_oid: String,
    pub comparison_kind: ComparisonKind,
    pub changed_paths_digest: String,
    pub changed_paths: Vec<ChangedPath>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ComparisonKind {
    FirstParent,
    EmptyTree,
}

#[derive(Clone, Debug, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ChangedPath {
    pub status: ChangeStatus,
    pub similarity: Option<u8>,
    pub old_path: Option<String>,
    pub new_path: Option<String>,
    pub old_exclusion: Option<ExclusionReason>,
    pub new_exclusion: Option<ExclusionReason>,
    pub old_mode: Option<String>,
    pub new_mode: Option<String>,
    pub old_blob_oid: Option<String>,
    pub new_blob_oid: Option<String>,
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ChangeStatus {
    Added,
    Copied,
    Deleted,
    Modified,
    Renamed,
    TypeChanged,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SnapshotInventory {
    pub schema_version: u32,
    pub complete: bool,
    pub digest: String,
    pub entries: Vec<InventoryEntry>,
}

#[derive(Clone, Debug, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InventoryEntry {
    pub path: String,
    pub mode: String,
    pub kind: TrackedFileKind,
    pub object_oid: String,
    pub blob_oid: Option<String>,
    pub size_bytes: Option<u64>,
    pub content_hash: Option<String>,
    pub language: Option<String>,
    pub exclusion: Option<ExclusionReason>,
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TrackedFileKind {
    File,
    Executable,
    Symlink,
    Gitlink,
    Unsupported,
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ExclusionReason {
    Binary,
    Gitlink,
    Oversized,
    Symlink,
    UnsafePath,
    UnsupportedEncoding,
    UnsupportedObject,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct EvidenceRequest {
    pub schema_version: u32,
    pub binding: SnapshotBinding,
    #[serde(flatten)]
    pub operation: EvidenceOperation,
    #[serde(default = "default_max_bytes")]
    pub max_bytes: usize,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub continuation: Option<String>,
}

impl EvidenceRequest {
    pub fn canonical(&self) -> Self {
        let mut canonical = self.clone();
        canonical.continuation = None;
        canonical
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(tag = "operation", rename_all = "snake_case", deny_unknown_fields)]
pub enum EvidenceOperation {
    Search { search: SearchSelector },
    Read { read: ReadSelector },
    Graph { graph: GraphSelector },
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SearchSelector {
    pub lane: SearchLane,
    pub query: String,
    #[serde(default)]
    pub paths: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub language: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub kind: Option<String>,
    #[serde(default = "default_result_limit")]
    pub limit: usize,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub hybrid_identity: Option<HybridIdentity>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SearchLane {
    Symbol,
    Literal,
    Regex,
    Content,
    LexicalSymbol,
    Hybrid,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HybridIdentity {
    pub endpoint: String,
    pub model: String,
    pub dimension: usize,
    pub index_id: String,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum ReadSelector {
    Range {
        path: String,
        start_line: usize,
        end_line: usize,
    },
    Symbol {
        path: String,
        qualified_name: String,
    },
    CommitMetadata,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GraphSelector {
    pub query: GraphQuery,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source: Option<EntitySelector>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub target: Option<EntitySelector>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub direction: Option<GraphDirection>,
    #[serde(default = "default_graph_depth")]
    pub depth: usize,
    #[serde(default)]
    pub relations: Vec<GraphRelation>,
    #[serde(default = "default_result_limit")]
    pub limit: usize,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum GraphQuery {
    Callers,
    Callees,
    Usages,
    Imports,
    DirectedPath,
    ScopedView,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum GraphDirection {
    Incoming,
    Outgoing,
    Both,
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum GraphRelation {
    Call,
    Import,
    Inheritance,
    Usage,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum EntitySelector {
    SymbolId {
        id: String,
    },
    Symbol {
        path: String,
        qualified_name: String,
    },
    Path {
        path: String,
    },
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(tag = "item_type", rename_all = "snake_case", deny_unknown_fields)]
pub enum EvidenceItem {
    Source(SourceEvidence),
    Graph(GraphEvidence),
    CommitMetadata(CommitMetadataEvidence),
}

impl EvidenceItem {
    pub fn evidence_id(&self) -> &str {
        match self {
            Self::Source(item) => &item.evidence_id,
            Self::Graph(item) => &item.evidence_id,
            Self::CommitMetadata(item) => &item.evidence_id,
        }
    }

    pub(crate) fn canonical_key(&self) -> String {
        match self {
            Self::Source(item) => format!(
                "0\0{}\0{:020}\0{:020}\0{}",
                item.path, item.byte_start, item.byte_end, item.evidence_id
            ),
            Self::Graph(item) => format!(
                "1\0{}\0{:020}\0{:?}\0{}\0{}\0{}",
                item.source.path,
                item.source.byte_start,
                item.relation,
                item.from.id,
                item.to.id,
                item.evidence_id
            ),
            Self::CommitMetadata(item) => {
                let path = item
                    .changed_path
                    .as_ref()
                    .and_then(|record| record.new_path.as_ref().or(record.old_path.as_ref()))
                    .map(String::as_str)
                    .unwrap_or("");
                format!("2\0{path}\0{}", item.evidence_id)
            }
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SourceEvidence {
    pub evidence_id: String,
    pub path: String,
    pub blob_oid: String,
    pub content_hash: String,
    pub excerpt_hash: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub qualified_name: Option<String>,
    pub line_start: usize,
    pub line_end: usize,
    pub byte_start: usize,
    pub byte_end: usize,
    pub excerpt: String,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GraphEvidence {
    pub evidence_id: String,
    pub source: SourceEvidence,
    pub relation: GraphRelation,
    pub direction: GraphDirection,
    pub from: GraphEndpoint,
    pub to: GraphEndpoint,
    pub owner: GraphOwner,
    pub provenance: GraphProvenance,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GraphEndpoint {
    pub id: String,
    pub name: String,
    pub kind: String,
    pub path: String,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GraphOwner {
    pub path: String,
    pub content_hash: String,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum GraphProvenance {
    Extracted,
    Inferred,
    Unresolved,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CommitMetadataEvidence {
    pub evidence_id: String,
    pub commit_oid: String,
    pub parent_oids: Vec<String>,
    pub comparison_parent_oid: String,
    pub comparison_kind: ComparisonKind,
    pub changed_paths_digest: String,
    pub changed_path_count: usize,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub changed_path: Option<ChangedPath>,
    pub record_hash: String,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EvidenceResponse {
    pub request: EvidenceRequest,
    pub request_fingerprint: String,
    pub binding: SnapshotBinding,
    pub contract: ContractIdentity,
    pub items: Vec<EvidenceItem>,
    pub complete: bool,
    pub completeness: Completeness,
    pub bounds: AppliedBounds,
    pub exclusions: Vec<InventoryEntry>,
    pub warnings: Vec<EvidenceWarning>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub continuation: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ContractIdentity {
    pub name: String,
    pub schema_version: u32,
    pub tool: String,
    pub tool_version: String,
    pub lane: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub hybrid: Option<HybridIdentity>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Completeness {
    Complete,
    CompleteEmpty,
    ExcludedScope,
    Paginated,
    TruncatedIndex,
    TruncatedTraversal,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AppliedBounds {
    pub max_bytes: usize,
    pub serialized_item_bytes: usize,
    pub returned_items: usize,
    pub total_items: usize,
    pub result_limit: usize,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub graph_depth: Option<usize>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EvidenceWarning {
    pub code: String,
    pub message: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub path: Option<String>,
}
