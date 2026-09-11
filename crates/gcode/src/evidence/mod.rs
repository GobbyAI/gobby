//! Deterministic, model-independent evidence bound to exact Git objects.

use std::fmt;
use std::sync::Arc;

use crate::codewiki_facts::{
    CodewikiFacts, ContentFact, FileFact, GraphBounds as FactsGraphBounds, GraphEdge,
    GraphEdgeKind, GraphOutcome, GraphScopeMode, GrepOutcome, GrepQuery, ScopeSelector,
    ScopedGraph, SearchQuery, SymbolFact,
};

mod contracts;
mod graph;
mod read;
mod search;
mod snapshot;

pub use contracts::*;
pub use snapshot::Snapshot;

pub type Result<T> = std::result::Result<T, EvidenceError>;

/// Validate the schema and selector shape without touching Git or external services.
pub fn validate_request_shape(request: &EvidenceRequest) -> Result<()> {
    if request.schema_version != EVIDENCE_SCHEMA_VERSION {
        return Err(EvidenceError::UnsupportedSchema {
            found: request.schema_version,
        });
    }
    if request.max_bytes == 0 {
        return Err(EvidenceError::InvalidSelector {
            detail: "max_bytes must be positive".to_string(),
        });
    }
    match &request.operation {
        EvidenceOperation::Search { search } => search::validate_selector(search),
        EvidenceOperation::Read { read } => read::validate_selector(read),
        EvidenceOperation::Graph { graph } => graph::validate_selector(graph),
    }
}

#[derive(Debug)]
pub enum EvidenceError {
    BindingMismatch {
        detail: String,
    },
    ContinuationMismatch,
    Contract {
        detail: String,
    },
    ExcludedPath {
        path: String,
        reason: ExclusionReason,
    },
    FactMismatch {
        path: String,
        expected: String,
        found: String,
    },
    Git {
        operation: String,
        message: String,
    },
    GraphUnavailable {
        reason: String,
    },
    IndexIncomplete {
        detail: String,
    },
    IndexUnavailable {
        reason: String,
    },
    InvalidObjectId {
        oid: String,
    },
    InvalidSelector {
        detail: String,
    },
    InventoryIncomplete,
    InventoryMismatch {
        detail: String,
    },
    MissingGitObject {
        oid: String,
        detail: String,
    },
    NarrowingRequired {
        evidence_id: String,
        item_bytes: usize,
        max_bytes: usize,
    },
    PathNotTracked {
        path: String,
    },
    SemanticFailure {
        reason: String,
    },
    SemanticIdentityMismatch {
        expected: Box<HybridIdentity>,
        found: Box<HybridIdentity>,
    },
    SemanticIdentityRequired,
    StaleRange {
        path: String,
        detail: String,
    },
    UnsupportedSchema {
        found: u32,
    },
    UnsafePath {
        path: String,
    },
}

impl EvidenceError {
    pub fn code(&self) -> &'static str {
        match self {
            Self::BindingMismatch { .. } => "snapshot_binding_mismatch",
            Self::ContinuationMismatch => "continuation_mismatch",
            Self::Contract { .. } => "contract_error",
            Self::ExcludedPath { .. } => "excluded_path",
            Self::FactMismatch { .. } => "fact_snapshot_mismatch",
            Self::Git { .. } => "git_error",
            Self::GraphUnavailable { .. } => "graph_unavailable",
            Self::IndexIncomplete { .. } => "index_incomplete",
            Self::IndexUnavailable { .. } => "index_unavailable",
            Self::InvalidObjectId { .. } => "invalid_object_id",
            Self::InvalidSelector { .. } => "invalid_selector",
            Self::InventoryIncomplete => "inventory_incomplete",
            Self::InventoryMismatch { .. } => "inventory_mismatch",
            Self::MissingGitObject { .. } => "missing_git_object",
            Self::NarrowingRequired { .. } => "narrowing_required",
            Self::PathNotTracked { .. } => "path_not_tracked",
            Self::SemanticFailure { .. } => "semantic_failure",
            Self::SemanticIdentityMismatch { .. } => "semantic_identity_mismatch",
            Self::SemanticIdentityRequired => "semantic_identity_required",
            Self::StaleRange { .. } => "stale_range",
            Self::UnsupportedSchema { .. } => "unsupported_schema",
            Self::UnsafePath { .. } => "unsafe_path",
        }
    }
}

impl fmt::Display for EvidenceError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::BindingMismatch { detail }
            | Self::Contract { detail }
            | Self::IndexIncomplete { detail }
            | Self::InventoryMismatch { detail } => formatter.write_str(detail),
            Self::ContinuationMismatch => {
                formatter.write_str("continuation does not match request")
            }
            Self::ExcludedPath { path, reason } => {
                write!(formatter, "{path} is excluded: {reason:?}")
            }
            Self::FactMismatch {
                path,
                expected,
                found,
            } => write!(
                formatter,
                "indexed fact for {path} has hash {found}, expected {expected}"
            ),
            Self::Git { operation, message } => write!(formatter, "{operation} failed: {message}"),
            Self::GraphUnavailable { reason } => write!(formatter, "graph unavailable: {reason}"),
            Self::IndexUnavailable { reason } => write!(formatter, "index unavailable: {reason}"),
            Self::InvalidObjectId { oid } => {
                write!(formatter, "invalid exact Git object ID: {oid}")
            }
            Self::InvalidSelector { detail } => write!(formatter, "invalid selector: {detail}"),
            Self::InventoryIncomplete => formatter.write_str("snapshot inventory is incomplete"),
            Self::MissingGitObject { oid, detail } => {
                write!(formatter, "missing Git object {oid}: {detail}")
            }
            Self::NarrowingRequired {
                evidence_id,
                item_bytes,
                max_bytes,
            } => write!(
                formatter,
                "evidence item {evidence_id} requires {item_bytes} bytes, above max_bytes {max_bytes}"
            ),
            Self::PathNotTracked { path } => {
                write!(formatter, "path is absent from snapshot: {path}")
            }
            Self::SemanticFailure { reason } => {
                write!(formatter, "semantic search failed: {reason}")
            }
            Self::SemanticIdentityMismatch { expected, found } => write!(
                formatter,
                "semantic identity changed: expected {expected:?}, found {found:?}"
            ),
            Self::SemanticIdentityRequired => {
                formatter.write_str("hybrid search requires a verified semantic identity")
            }
            Self::StaleRange { path, detail } => {
                write!(formatter, "stale range for {path}: {detail}")
            }
            Self::UnsupportedSchema { found } => {
                write!(formatter, "unsupported evidence schema version {found}")
            }
            Self::UnsafePath { path } => write!(formatter, "unsafe repository path: {path}"),
        }
    }
}

impl std::error::Error for EvidenceError {}

#[derive(Clone, Debug)]
pub struct FactPage<T> {
    pub items: Vec<T>,
    pub truncated: bool,
}

pub trait EvidenceFacts {
    fn project_id(&self) -> &str;
    fn files(&self) -> anyhow::Result<Vec<FileFact>>;
    fn search_symbols(&self, query: &SearchQuery) -> anyhow::Result<FactPage<SymbolFact>>;
    fn search_content(&self, query: &SearchQuery) -> anyhow::Result<FactPage<ContentFact>>;
    fn grep(&self, query: &GrepQuery) -> anyhow::Result<GrepOutcome>;
    fn symbols_for_file(&self, path: &str) -> anyhow::Result<Vec<SymbolFact>>;
    fn symbol_by_id(&self, id: &str) -> anyhow::Result<Option<SymbolFact>>;
    fn graph_edges(
        &self,
        seed: &ScopeSelector,
        kind: GraphEdgeKind,
        bounds: FactsGraphBounds,
        mode: GraphScopeMode,
    ) -> anyhow::Result<ScopedGraph>;
}

impl EvidenceFacts for CodewikiFacts {
    fn project_id(&self) -> &str {
        self.project_id()
    }

    fn files(&self) -> anyhow::Result<Vec<FileFact>> {
        self.scoped_files(&ScopeSelector::all())
    }

    fn search_symbols(&self, query: &SearchQuery) -> anyhow::Result<FactPage<SymbolFact>> {
        let mut overfetch = query.clone();
        overfetch.limit = query.limit.saturating_add(1);
        let mut items = self
            .search_with(&overfetch)?
            .into_iter()
            .map(|hit| hit.symbol)
            .collect::<Vec<_>>();
        let truncated = items.len() > query.limit;
        items.truncate(query.limit);
        Ok(FactPage { items, truncated })
    }

    fn search_content(&self, query: &SearchQuery) -> anyhow::Result<FactPage<ContentFact>> {
        let mut overfetch = query.clone();
        overfetch.limit = query.limit.saturating_add(1);
        let mut items = self.search_content_with(&overfetch)?;
        let truncated = items.len() > query.limit;
        items.truncate(query.limit);
        Ok(FactPage { items, truncated })
    }

    fn grep(&self, query: &GrepQuery) -> anyhow::Result<GrepOutcome> {
        self.grep_with(query)
    }

    fn symbols_for_file(&self, path: &str) -> anyhow::Result<Vec<SymbolFact>> {
        self.symbols_for_file(&crate::codewiki_facts::FileId::new(path))
    }

    fn symbol_by_id(&self, id: &str) -> anyhow::Result<Option<SymbolFact>> {
        self.symbol_by_id(id)
    }

    fn graph_edges(
        &self,
        seed: &ScopeSelector,
        kind: GraphEdgeKind,
        bounds: FactsGraphBounds,
        mode: GraphScopeMode,
    ) -> anyhow::Result<ScopedGraph> {
        self.scoped_edges(seed, kind, bounds, mode, None)
    }
}

pub trait HybridSearch {
    fn effective_identity(&self) -> std::result::Result<HybridIdentity, String>;
    fn search_symbol_ids(
        &self,
        selector: &SearchSelector,
    ) -> std::result::Result<FactPage<String>, String>;
}

pub struct EvidenceLibrary {
    snapshot: Snapshot,
    facts: Arc<dyn EvidenceFacts>,
    hybrid: Option<Arc<dyn HybridSearch>>,
}

impl EvidenceLibrary {
    pub fn new(snapshot: Snapshot, facts: Arc<dyn EvidenceFacts>) -> Result<Self> {
        if facts.project_id() != snapshot.binding().project_id {
            return Err(EvidenceError::BindingMismatch {
                detail: format!(
                    "fact project {} differs from snapshot project {}",
                    facts.project_id(),
                    snapshot.binding().project_id
                ),
            });
        }
        Ok(Self {
            snapshot,
            facts,
            hybrid: None,
        })
    }

    pub fn with_hybrid(mut self, hybrid: Arc<dyn HybridSearch>) -> Self {
        self.hybrid = Some(hybrid);
        self
    }

    pub fn snapshot(&self) -> &Snapshot {
        &self.snapshot
    }

    pub fn query(&self, request: EvidenceRequest) -> Result<EvidenceResponse> {
        self.validate_request(&request)?;
        let canonical = request.canonical();
        let request_fingerprint = snapshot::canonical_hash(&canonical)?;
        let query_result = match &canonical.operation {
            EvidenceOperation::Search { search } => search::execute(self, search)?,
            EvidenceOperation::Read { read } => read::execute(self, read)?,
            EvidenceOperation::Graph { graph } => graph::execute(self, graph)?,
        };
        self.paginate(
            canonical,
            request.continuation,
            request_fingerprint,
            query_result,
        )
    }

    fn validate_request(&self, request: &EvidenceRequest) -> Result<()> {
        validate_request_shape(request)?;
        if request.binding != *self.snapshot.binding() {
            return Err(EvidenceError::BindingMismatch {
                detail: "request binding differs from verified snapshot".to_string(),
            });
        }
        Ok(())
    }

    fn validate_index_inventory(&self) -> Result<()> {
        let files = self
            .facts
            .files()
            .map_err(|error| EvidenceError::IndexUnavailable {
                reason: format!("{error:#}"),
            })?;
        let indexed = files
            .into_iter()
            .map(|file| (file.path, file.content_hash))
            .collect::<std::collections::BTreeMap<_, _>>();
        for entry in self.snapshot.eligible_entries() {
            match indexed.get(&entry.path) {
                Some(hash) if Some(hash) == entry.content_hash.as_ref() => {}
                Some(hash) => {
                    return Err(EvidenceError::FactMismatch {
                        path: entry.path.clone(),
                        expected: entry.content_hash.clone().unwrap_or_default(),
                        found: hash.clone(),
                    });
                }
                None => {
                    let is_blank = if entry.size_bytes == Some(0) {
                        true
                    } else {
                        let content = self.snapshot.read_blob(&entry.path)?;
                        std::str::from_utf8(&content).is_ok_and(|text| text.trim().is_empty())
                    };
                    if !is_blank {
                        return Err(EvidenceError::IndexIncomplete {
                            detail: format!(
                                "eligible snapshot path is not indexed: {}",
                                entry.path
                            ),
                        });
                    }
                }
            }
        }
        for (path, hash) in indexed {
            self.snapshot.verify_fact(&path, &hash)?;
        }
        Ok(())
    }

    fn paginate(
        &self,
        request: EvidenceRequest,
        continuation: Option<String>,
        request_fingerprint: String,
        mut result: QueryResult,
    ) -> Result<EvidenceResponse> {
        result.items.sort_by_key(EvidenceItem::canonical_key);
        result
            .items
            .dedup_by(|left, right| left.evidence_id() == right.evidence_id());
        let total_items = result.items.len();
        let offset = continuation
            .as_deref()
            .map(|token| parse_continuation(token, &request_fingerprint))
            .transpose()?
            .unwrap_or(0);
        if offset > total_items {
            return Err(EvidenceError::ContinuationMismatch);
        }
        let sizes = result
            .items
            .iter()
            .map(|item| {
                serde_json::to_vec(item)
                    .map(|bytes| bytes.len())
                    .map_err(|error| EvidenceError::Contract {
                        detail: error.to_string(),
                    })
            })
            .collect::<Result<Vec<_>>>()?;
        if let Some((index, item_bytes)) = sizes
            .iter()
            .enumerate()
            .skip(offset)
            .find(|(_, bytes)| **bytes > request.max_bytes)
        {
            return Err(EvidenceError::NarrowingRequired {
                evidence_id: result.items[index].evidence_id().to_string(),
                item_bytes: *item_bytes,
                max_bytes: request.max_bytes,
            });
        }
        let mut used: usize = 0;
        let mut end = offset;
        while end < total_items && used.saturating_add(sizes[end]) <= request.max_bytes {
            used += sizes[end];
            end += 1;
        }
        let items = result.items[offset..end].to_vec();
        let next = (end < total_items).then(|| continuation_token(&request_fingerprint, end));
        let source_complete = matches!(
            result.completeness,
            Completeness::Complete | Completeness::CompleteEmpty | Completeness::ExcludedScope
        );
        let complete = source_complete && next.is_none();
        let completeness = if next.is_some() {
            Completeness::Paginated
        } else {
            result.completeness
        };
        let max_bytes = request.max_bytes;
        Ok(EvidenceResponse {
            request,
            request_fingerprint,
            binding: self.snapshot.binding().clone(),
            contract: ContractIdentity {
                name: "gcode-evidence".to_string(),
                schema_version: EVIDENCE_SCHEMA_VERSION,
                tool: "gobby-code".to_string(),
                tool_version: env!("CARGO_PKG_VERSION").to_string(),
                lane: result.lane,
                hybrid: result.hybrid,
            },
            items,
            complete,
            completeness,
            bounds: AppliedBounds {
                max_bytes,
                serialized_item_bytes: used,
                returned_items: end.saturating_sub(offset),
                total_items,
                result_limit: result.result_limit,
                graph_depth: result.graph_depth,
            },
            exclusions: result.exclusions,
            warnings: result.warnings,
            continuation: next,
        })
    }
}

struct QueryResult {
    items: Vec<EvidenceItem>,
    completeness: Completeness,
    lane: String,
    hybrid: Option<HybridIdentity>,
    exclusions: Vec<InventoryEntry>,
    warnings: Vec<EvidenceWarning>,
    result_limit: usize,
    graph_depth: Option<usize>,
}

fn continuation_token(fingerprint: &str, offset: usize) -> String {
    format!("evidence-v1:{fingerprint}:{offset}")
}

fn parse_continuation(token: &str, fingerprint: &str) -> Result<usize> {
    let prefix = format!("evidence-v1:{fingerprint}:");
    token
        .strip_prefix(&prefix)
        .and_then(|offset| offset.parse().ok())
        .ok_or(EvidenceError::ContinuationMismatch)
}

fn fact_graph_bounds(direction: GraphDirection, limit: usize) -> FactsGraphBounds {
    match direction {
        GraphDirection::Incoming => FactsGraphBounds {
            incoming_limit: limit,
            outgoing_limit: 0,
        },
        GraphDirection::Outgoing => FactsGraphBounds {
            incoming_limit: 0,
            outgoing_limit: limit,
        },
        GraphDirection::Both => FactsGraphBounds::symmetric(limit),
    }
}

fn graph_outcome(scoped: ScopedGraph) -> Result<(Vec<GraphEdge>, bool)> {
    match scoped.outcome {
        GraphOutcome::Available(items) => Ok((items, false)),
        GraphOutcome::Truncated(items) => Ok((items, true)),
        GraphOutcome::Empty => Ok((Vec::new(), false)),
        GraphOutcome::Unavailable { reason } => Err(EvidenceError::GraphUnavailable { reason }),
    }
}

#[cfg(test)]
mod tests;
