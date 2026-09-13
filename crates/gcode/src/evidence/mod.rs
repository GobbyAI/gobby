//! Deterministic, model-independent evidence from the caller's live index.

use std::fmt;
use std::sync::Arc;

use crate::codewiki_facts::{
    CodewikiFacts, ContentFact, FileFact, GraphBounds as FactsGraphBounds, GraphEdge,
    GraphEdgeKind, GraphOutcome, GraphScopeMode, GrepOutcome, GrepQuery, ScopeSelector,
    ScopedGraph, SearchQuery, SymbolFact,
};

mod contracts;
mod graph;
mod provenance;
mod read;
mod search;
mod source;

pub use contracts::*;

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
    for oid in [&request.binding.commit_oid, &request.binding.tree_oid] {
        if !matches!(oid.len(), 40 | 64)
            || !oid
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        {
            return Err(EvidenceError::InvalidSelector {
                detail: "repository provenance requires full lowercase Git object ids".to_string(),
            });
        }
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
    IndexUnavailable {
        reason: String,
    },
    InvalidSelector {
        detail: String,
    },
    NarrowingRequired {
        evidence_id: String,
        item_bytes: usize,
        max_bytes: usize,
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
            Self::BindingMismatch { .. } => "repository_binding_mismatch",
            Self::ContinuationMismatch => "continuation_mismatch",
            Self::Contract { .. } => "contract_error",
            Self::FactMismatch { .. } => "fact_index_mismatch",
            Self::Git { .. } => "git_error",
            Self::GraphUnavailable { .. } => "graph_unavailable",
            Self::IndexUnavailable { .. } => "index_unavailable",
            Self::InvalidSelector { .. } => "invalid_selector",
            Self::NarrowingRequired { .. } => "narrowing_required",
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
            Self::BindingMismatch { detail } | Self::Contract { detail } => {
                formatter.write_str(detail)
            }
            Self::ContinuationMismatch => {
                formatter.write_str("continuation does not match request")
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
            Self::InvalidSelector { detail } => write!(formatter, "invalid selector: {detail}"),
            Self::NarrowingRequired {
                evidence_id,
                item_bytes,
                max_bytes,
            } => write!(
                formatter,
                "evidence item {evidence_id} requires {item_bytes} bytes, above max_bytes {max_bytes}"
            ),
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
    repository_root: std::path::PathBuf,
    binding: RepositoryBinding,
    files: std::collections::BTreeMap<String, FileFact>,
    facts: Arc<dyn EvidenceFacts>,
    hybrid: Option<Arc<dyn HybridSearch>>,
}

impl EvidenceLibrary {
    pub fn new(
        repository_root: &std::path::Path,
        binding: RepositoryBinding,
        facts: Arc<dyn EvidenceFacts>,
    ) -> Result<Self> {
        if facts.project_id() != binding.project_id {
            return Err(EvidenceError::BindingMismatch {
                detail: "fact project differs from caller project".to_string(),
            });
        }
        let repository_root =
            repository_root
                .canonicalize()
                .map_err(|error| EvidenceError::IndexUnavailable {
                    reason: error.to_string(),
                })?;
        let files = facts
            .files()
            .map_err(|error| EvidenceError::IndexUnavailable {
                reason: format!("{error:#}"),
            })?
            .into_iter()
            .map(|file| (file.path.clone(), file))
            .collect();
        Ok(Self {
            repository_root,
            binding,
            files,
            facts,
            hybrid: None,
        })
    }

    pub fn with_hybrid(mut self, hybrid: Arc<dyn HybridSearch>) -> Self {
        self.hybrid = Some(hybrid);
        self
    }

    pub fn query(&self, request: EvidenceRequest) -> Result<EvidenceResponse> {
        self.validate_request(&request)?;
        let canonical = request.canonical();
        let request_fingerprint = source::canonical_hash(&canonical)?;
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
        if request.binding != self.binding {
            return Err(EvidenceError::BindingMismatch {
                detail: "request binding differs from caller binding".to_string(),
            });
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
            observation: None,
            request,
            request_fingerprint,
            binding: self.binding.clone(),
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
