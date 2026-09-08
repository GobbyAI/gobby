use std::collections::{BTreeMap, BTreeSet};

use crate::codewiki_facts::{GraphEdge, GraphEdgeKind, GraphScopeMode, ScopeSelector, SymbolFact};

use super::contracts::{
    Completeness, EntitySelector, EvidenceItem, GraphDirection, GraphEndpoint, GraphEvidence,
    GraphOwner, GraphProvenance, GraphQuery, GraphRelation, GraphSelector,
};
use super::read::{source_for_lines, source_for_symbol};
use super::snapshot::{canonical_hash, validate_repo_path};
use super::{
    EvidenceError, EvidenceLibrary, QueryResult, Result, fact_graph_bounds, graph_outcome,
};

const MAX_GRAPH_DEPTH: usize = 16;

pub(super) fn execute(library: &EvidenceLibrary, selector: &GraphSelector) -> Result<QueryResult> {
    validate_selector(selector)?;
    if let Some(source) = &selector.source {
        validate_snapshot_selector(library, source)?;
    }
    if let Some(target) = &selector.target {
        validate_snapshot_selector(library, target)?;
    }
    library.validate_index_inventory()?;
    let direction = effective_direction(selector);
    let (edges, truncated) = match selector.query {
        GraphQuery::Callers => one_hop(
            library,
            required_source(library, selector, true)?,
            GraphEdgeKind::Call,
            GraphDirection::Incoming,
            selector.limit,
        )?,
        GraphQuery::Callees => one_hop(
            library,
            required_source(library, selector, true)?,
            GraphEdgeKind::Call,
            GraphDirection::Outgoing,
            selector.limit,
        )?,
        GraphQuery::Usages => one_hop(
            library,
            required_source(library, selector, true)?,
            GraphEdgeKind::Call,
            GraphDirection::Incoming,
            selector.limit,
        )?,
        GraphQuery::Imports => one_hop(
            library,
            required_source(library, selector, false)?,
            GraphEdgeKind::Import,
            direction,
            selector.limit,
        )?,
        GraphQuery::DirectedPath => directed_path(library, selector)?,
        GraphQuery::ScopedView => scoped_view(library, selector, direction)?,
    };
    let relation_override = (selector.query == GraphQuery::Usages).then_some(GraphRelation::Usage);
    let mut items = edges
        .into_iter()
        .map(|edge| {
            graph_item(library, edge, relation_override, direction).map(EvidenceItem::Graph)
        })
        .collect::<Result<Vec<_>>>()?;
    let overflow = items.len() > selector.limit;
    items.truncate(selector.limit);
    let truncated = truncated || overflow;
    Ok(QueryResult {
        completeness: if truncated {
            Completeness::TruncatedTraversal
        } else if items.is_empty() {
            Completeness::CompleteEmpty
        } else {
            Completeness::Complete
        },
        items,
        lane: format!("graph_{:?}", selector.query).to_lowercase(),
        hybrid: None,
        exclusions: Vec::new(),
        warnings: Vec::new(),
        result_limit: selector.limit,
        graph_depth: Some(selector.depth),
    })
}

fn validate_selector(selector: &GraphSelector) -> Result<()> {
    if selector.limit == 0 {
        return Err(EvidenceError::InvalidSelector {
            detail: "graph limit must be positive".to_string(),
        });
    }
    if selector.depth == 0 || selector.depth > MAX_GRAPH_DEPTH {
        return Err(EvidenceError::InvalidSelector {
            detail: format!("graph depth must be in 1..={MAX_GRAPH_DEPTH}"),
        });
    }
    if let Some(source) = &selector.source {
        validate_entity_selector(source)?;
    }
    if let Some(target) = &selector.target {
        validate_entity_selector(target)?;
    }
    let fixed_direction = match selector.query {
        GraphQuery::Callers | GraphQuery::Usages => Some(GraphDirection::Incoming),
        GraphQuery::Callees | GraphQuery::Imports | GraphQuery::DirectedPath => {
            Some(GraphDirection::Outgoing)
        }
        GraphQuery::ScopedView => None,
    };
    if let (Some(requested), Some(required)) = (selector.direction, fixed_direction)
        && requested != required
    {
        return Err(EvidenceError::InvalidSelector {
            detail: format!("{:?} requires {required:?} direction", selector.query).to_lowercase(),
        });
    }
    match selector.query {
        GraphQuery::DirectedPath => {
            if !matches!(
                selector.source,
                Some(EntitySelector::SymbolId { .. } | EntitySelector::Symbol { .. })
            ) || !matches!(
                selector.target,
                Some(EntitySelector::SymbolId { .. } | EntitySelector::Symbol { .. })
            ) {
                return Err(EvidenceError::InvalidSelector {
                    detail: "directed_path requires source and target symbols".to_string(),
                });
            }
        }
        GraphQuery::Callers | GraphQuery::Callees | GraphQuery::Usages => {
            if !matches!(
                selector.source,
                Some(EntitySelector::SymbolId { .. } | EntitySelector::Symbol { .. })
            ) {
                return Err(EvidenceError::InvalidSelector {
                    detail: "operation requires an exact symbol source".to_string(),
                });
            }
        }
        GraphQuery::Imports => {
            if !matches!(selector.source, Some(EntitySelector::Path { .. })) {
                return Err(EvidenceError::InvalidSelector {
                    detail: "imports requires a path source".to_string(),
                });
            }
        }
        _ if selector.target.is_some() => {
            return Err(EvidenceError::InvalidSelector {
                detail: "target is valid only for directed_path".to_string(),
            });
        }
        _ if selector.source.is_none() => {
            return Err(EvidenceError::InvalidSelector {
                detail: "graph query requires a source selector".to_string(),
            });
        }
        _ => {}
    }
    let allowed = match selector.query {
        GraphQuery::Callers | GraphQuery::Callees => &[GraphRelation::Call][..],
        GraphQuery::Usages => &[GraphRelation::Usage, GraphRelation::Call][..],
        GraphQuery::Imports => &[GraphRelation::Import][..],
        GraphQuery::DirectedPath => &[GraphRelation::Call][..],
        GraphQuery::ScopedView => &[
            GraphRelation::Call,
            GraphRelation::Import,
            GraphRelation::Inheritance,
        ][..],
    };
    if selector
        .relations
        .iter()
        .any(|relation| !allowed.contains(relation))
    {
        return Err(EvidenceError::InvalidSelector {
            detail: "relation filter is incompatible with graph operation".to_string(),
        });
    }
    Ok(())
}

fn validate_entity_selector(selector: &EntitySelector) -> Result<()> {
    match selector {
        EntitySelector::SymbolId { id } if id.trim().is_empty() => {
            Err(EvidenceError::InvalidSelector {
                detail: "symbol ID must not be empty".to_string(),
            })
        }
        EntitySelector::Symbol {
            path,
            qualified_name,
        } => {
            validate_repo_path(path)?;
            if qualified_name.trim().is_empty() {
                return Err(EvidenceError::InvalidSelector {
                    detail: "qualified symbol name must not be empty".to_string(),
                });
            }
            Ok(())
        }
        EntitySelector::Path { path } => validate_repo_path(path),
        EntitySelector::SymbolId { .. } => Ok(()),
    }
}

fn validate_snapshot_selector(library: &EvidenceLibrary, selector: &EntitySelector) -> Result<()> {
    let path = match selector {
        EntitySelector::Symbol { path, .. } | EntitySelector::Path { path } => path,
        EntitySelector::SymbolId { .. } => return Ok(()),
    };
    let entry = library.snapshot.entry(path)?;
    if let Some(reason) = entry.exclusion {
        return Err(EvidenceError::ExcludedPath {
            path: path.clone(),
            reason,
        });
    }
    Ok(())
}

fn effective_direction(selector: &GraphSelector) -> GraphDirection {
    selector.direction.unwrap_or(match selector.query {
        GraphQuery::Callers | GraphQuery::Usages => GraphDirection::Incoming,
        GraphQuery::ScopedView => GraphDirection::Both,
        GraphQuery::Callees | GraphQuery::Imports | GraphQuery::DirectedPath => {
            GraphDirection::Outgoing
        }
    })
}

#[derive(Clone)]
struct ResolvedSelector {
    scope: ScopeSelector,
    symbol_ids: BTreeSet<String>,
}

fn required_source(
    library: &EvidenceLibrary,
    selector: &GraphSelector,
    require_symbol: bool,
) -> Result<ResolvedSelector> {
    let source = selector
        .source
        .as_ref()
        .ok_or_else(|| EvidenceError::InvalidSelector {
            detail: "graph source is required".to_string(),
        })?;
    let resolved = resolve_selector(library, source)?;
    if require_symbol && resolved.symbol_ids.is_empty() {
        return Err(EvidenceError::InvalidSelector {
            detail: "operation requires an exact symbol source".to_string(),
        });
    }
    Ok(resolved)
}

fn resolve_selector(
    library: &EvidenceLibrary,
    selector: &EntitySelector,
) -> Result<ResolvedSelector> {
    match selector {
        EntitySelector::SymbolId { id } => {
            let symbol = exact_symbol_by_id(library, id)?;
            Ok(ResolvedSelector {
                scope: ScopeSelector::symbols([symbol.id.clone()]),
                symbol_ids: BTreeSet::from([symbol.id]),
            })
        }
        EntitySelector::Symbol {
            path,
            qualified_name,
        } => {
            validate_repo_path(path)?;
            let matches = library
                .facts
                .symbols_for_file(path)
                .map_err(|error| EvidenceError::IndexUnavailable {
                    reason: format!("{error:#}"),
                })?
                .into_iter()
                .filter(|symbol| symbol.qualified_name == *qualified_name)
                .collect::<Vec<_>>();
            if matches.len() != 1 {
                return Err(EvidenceError::InvalidSelector {
                    detail: format!(
                        "expected one exact symbol {qualified_name:?} in {path}, found {}",
                        matches.len()
                    ),
                });
            }
            library
                .snapshot
                .verify_fact(path, &matches[0].file_content_hash)?;
            Ok(ResolvedSelector {
                scope: ScopeSelector::symbols([matches[0].id.clone()]),
                symbol_ids: BTreeSet::from([matches[0].id.clone()]),
            })
        }
        EntitySelector::Path { path } => {
            validate_repo_path(path)?;
            library.snapshot.entry(path)?;
            Ok(ResolvedSelector {
                scope: ScopeSelector::paths([path.clone()]),
                symbol_ids: BTreeSet::new(),
            })
        }
    }
}

fn exact_symbol_by_id(library: &EvidenceLibrary, id: &str) -> Result<SymbolFact> {
    let symbol = library
        .facts
        .symbol_by_id(id)
        .map_err(|error| EvidenceError::IndexUnavailable {
            reason: format!("{error:#}"),
        })?
        .ok_or_else(|| EvidenceError::InvalidSelector {
            detail: format!("symbol ID is not visible: {id}"),
        })?;
    library
        .snapshot
        .verify_fact(&symbol.file_path, &symbol.file_content_hash)?;
    Ok(symbol)
}

fn one_hop(
    library: &EvidenceLibrary,
    source: ResolvedSelector,
    kind: GraphEdgeKind,
    direction: GraphDirection,
    limit: usize,
) -> Result<(Vec<GraphEdge>, bool)> {
    let scoped = library
        .facts
        .graph_edges(
            &source.scope,
            kind,
            fact_graph_bounds(direction, limit),
            GraphScopeMode::Incident,
        )
        .map_err(|error| EvidenceError::GraphUnavailable {
            reason: format!("{error:#}"),
        })?;
    let direction_truncated = scoped.incoming_truncated || scoped.outgoing_truncated;
    let (edges, outcome_truncated) = graph_outcome(scoped)?;
    Ok((edges, direction_truncated || outcome_truncated))
}

fn scoped_view(
    library: &EvidenceLibrary,
    selector: &GraphSelector,
    direction: GraphDirection,
) -> Result<(Vec<GraphEdge>, bool)> {
    let source = required_source(library, selector, false)?;
    let relations = if selector.relations.is_empty() {
        vec![
            GraphRelation::Call,
            GraphRelation::Import,
            GraphRelation::Inheritance,
        ]
    } else {
        selector.relations.clone()
    };
    let mut edges = Vec::new();
    let mut truncated = false;
    for relation in relations {
        let kind = relation_kind(relation)?;
        let (mut relation_edges, relation_truncated) = walk_scoped_relation(
            library,
            source.clone(),
            kind,
            direction,
            selector.depth,
            selector.limit,
        )?;
        edges.append(&mut relation_edges);
        truncated |= relation_truncated;
    }
    edges.sort_by(edge_key);
    edges.dedup_by(|left, right| edge_key(left, right).is_eq());
    if edges.len() > selector.limit {
        edges.truncate(selector.limit);
        truncated = true;
    }
    Ok((edges, truncated))
}

fn walk_scoped_relation(
    library: &EvidenceLibrary,
    source: ResolvedSelector,
    kind: GraphEdgeKind,
    direction: GraphDirection,
    depth: usize,
    limit: usize,
) -> Result<(Vec<GraphEdge>, bool)> {
    let mut frontier = source.scope;
    let mut visited = TraversalFrontier {
        symbols: source.symbol_ids,
        ..TraversalFrontier::default()
    };
    let mut edges = Vec::new();
    let mut truncated = false;
    for hop in 0..depth {
        let (hop_edges, hop_truncated) = one_hop(
            library,
            ResolvedSelector {
                scope: frontier,
                symbol_ids: BTreeSet::new(),
            },
            kind,
            direction,
            limit.saturating_add(1),
        )?;
        truncated |= hop_truncated;
        let mut next = TraversalFrontier::default();
        for edge in &hop_edges {
            if matches!(direction, GraphDirection::Incoming | GraphDirection::Both) {
                add_frontier_endpoint(kind, true, edge, &mut visited, &mut next);
            }
            if matches!(direction, GraphDirection::Outgoing | GraphDirection::Both) {
                add_frontier_endpoint(kind, false, edge, &mut visited, &mut next);
            }
        }
        edges.extend(hop_edges);
        edges.sort_by(edge_key);
        edges.dedup_by(|left, right| edge_key(left, right).is_eq());
        if edges.len() > limit {
            truncated = true;
            break;
        }
        if next.is_empty() {
            break;
        }
        if hop + 1 == depth {
            truncated = true;
            break;
        }
        frontier = if kind == GraphEdgeKind::Import {
            ScopeSelector::endpoints(next.files, next.modules)
        } else {
            ScopeSelector::symbols(next.symbols)
        };
    }
    Ok((edges, truncated))
}

#[derive(Default)]
struct TraversalFrontier {
    symbols: BTreeSet<String>,
    files: BTreeSet<String>,
    modules: BTreeSet<String>,
}

impl TraversalFrontier {
    fn is_empty(&self) -> bool {
        self.symbols.is_empty() && self.files.is_empty() && self.modules.is_empty()
    }
}

fn add_frontier_endpoint(
    kind: GraphEdgeKind,
    source: bool,
    edge: &GraphEdge,
    visited: &mut TraversalFrontier,
    next: &mut TraversalFrontier,
) {
    if kind == GraphEdgeKind::Import {
        let (value, visited, next) = if source {
            (&edge.source, &mut visited.files, &mut next.files)
        } else {
            (&edge.target, &mut visited.modules, &mut next.modules)
        };
        if visited.insert(value.clone()) {
            next.insert(value.clone());
        }
    } else {
        let value = if source { &edge.source } else { &edge.target };
        if visited.symbols.insert(value.clone()) {
            next.symbols.insert(value.clone());
        }
    }
}

fn directed_path(
    library: &EvidenceLibrary,
    selector: &GraphSelector,
) -> Result<(Vec<GraphEdge>, bool)> {
    let source = required_source(library, selector, true)?;
    let target_selector =
        selector
            .target
            .as_ref()
            .ok_or_else(|| EvidenceError::InvalidSelector {
                detail: "directed_path target is required".to_string(),
            })?;
    let target = resolve_selector(library, target_selector)?;
    if target.symbol_ids.len() != 1 || source.symbol_ids.len() != 1 {
        return Err(EvidenceError::InvalidSelector {
            detail: "directed_path endpoints must each resolve to one symbol".to_string(),
        });
    }
    let source_id = source.symbol_ids.iter().next().cloned().unwrap_or_default();
    let target_id = target.symbol_ids.iter().next().cloned().unwrap_or_default();
    if source_id == target_id {
        return Ok((Vec::new(), false));
    }
    let mut frontier = BTreeSet::from([source_id.clone()]);
    let mut visited = frontier.clone();
    let mut parent = BTreeMap::<String, GraphEdge>::new();
    let mut truncated = false;
    let mut found = false;
    for _ in 0..selector.depth {
        let scope = ScopeSelector::symbols(frontier.iter().cloned());
        let scoped = library
            .facts
            .graph_edges(
                &scope,
                GraphEdgeKind::Call,
                fact_graph_bounds(GraphDirection::Outgoing, selector.limit.saturating_add(1)),
                GraphScopeMode::Incident,
            )
            .map_err(|error| EvidenceError::GraphUnavailable {
                reason: format!("{error:#}"),
            })?;
        truncated |= scoped.outgoing_truncated;
        let (mut edges, outcome_truncated) = graph_outcome(scoped)?;
        truncated |= outcome_truncated;
        edges.sort_by(edge_key);
        let mut next = BTreeSet::new();
        for edge in edges {
            if !frontier.contains(&edge.source) || visited.contains(&edge.target) {
                continue;
            }
            visited.insert(edge.target.clone());
            next.insert(edge.target.clone());
            parent.insert(edge.target.clone(), edge);
            if next.contains(&target_id) {
                found = true;
                break;
            }
        }
        if found || next.is_empty() {
            frontier = next;
            break;
        }
        frontier = next;
    }
    if !found {
        truncated |= !frontier.is_empty();
        return Ok((Vec::new(), truncated));
    }
    let mut path = Vec::new();
    let mut cursor = target_id;
    while cursor != source_id {
        let edge = parent
            .get(&cursor)
            .cloned()
            .ok_or_else(|| EvidenceError::GraphUnavailable {
                reason: "directed path reconstruction lost a predecessor".to_string(),
            })?;
        cursor.clone_from(&edge.source);
        path.push(edge);
    }
    path.reverse();
    Ok((path, truncated))
}

fn graph_item(
    library: &EvidenceLibrary,
    edge: GraphEdge,
    relation_override: Option<GraphRelation>,
    direction: GraphDirection,
) -> Result<GraphEvidence> {
    library
        .snapshot
        .verify_fact(&edge.owner_path, &edge.owner_hash)?;
    let source = owner_source(library, &edge)?;
    let relation = relation_override.unwrap_or_else(|| relation_from_edge(&edge));
    let provenance = if edge.source_kind == "unresolved" || edge.target_kind == "unresolved" {
        GraphProvenance::Unresolved
    } else {
        match edge.provenance.to_ascii_lowercase().as_str() {
            "extracted" => GraphProvenance::Extracted,
            "inferred" => GraphProvenance::Inferred,
            _ => GraphProvenance::Unresolved,
        }
    };
    let from = GraphEndpoint {
        id: edge.source.clone(),
        name: edge.source_name.clone(),
        kind: edge.source_kind.clone(),
        path: edge.source_file.clone(),
    };
    let to = GraphEndpoint {
        id: edge.target.clone(),
        name: edge.target_name.clone(),
        kind: edge.target_kind.clone(),
        path: edge.target_file.clone(),
    };
    let owner = GraphOwner {
        path: edge.owner_path,
        content_hash: edge.owner_hash,
    };
    let evidence_id = format!(
        "graph:{}",
        canonical_hash(&(
            library.snapshot.binding(),
            &source.evidence_id,
            relation,
            direction,
            &from,
            &to,
            &owner,
            provenance,
        ))?
    );
    Ok(GraphEvidence {
        evidence_id,
        source,
        relation,
        direction,
        from,
        to,
        owner,
        provenance,
    })
}

fn owner_source(library: &EvidenceLibrary, edge: &GraphEdge) -> Result<super::SourceEvidence> {
    if let Some(symbol) = library.facts.symbol_by_id(&edge.source).map_err(|error| {
        EvidenceError::IndexUnavailable {
            reason: format!("{error:#}"),
        }
    })? && symbol.file_path == edge.owner_path
        && symbol.file_content_hash == edge.owner_hash
    {
        return source_for_symbol(library, &symbol);
    }
    let bytes = library.snapshot.read_blob(&edge.owner_path)?;
    let text = std::str::from_utf8(&bytes).map_err(|_| EvidenceError::FactMismatch {
        path: edge.owner_path.clone(),
        expected: "UTF-8 owner blob".to_string(),
        found: "non-UTF-8".to_string(),
    })?;
    let line = text
        .lines()
        .position(|line| line.contains(&edge.target_name) || line.contains(&edge.target))
        .map(|index| index + 1)
        .unwrap_or(1);
    source_for_lines(library, &edge.owner_path, line, line, None)
}

fn relation_kind(relation: GraphRelation) -> Result<GraphEdgeKind> {
    match relation {
        GraphRelation::Call | GraphRelation::Usage => Ok(GraphEdgeKind::Call),
        GraphRelation::Import => Ok(GraphEdgeKind::Import),
        GraphRelation::Inheritance => Ok(GraphEdgeKind::Inheritance),
    }
}

fn relation_from_edge(edge: &GraphEdge) -> GraphRelation {
    match edge.kind {
        GraphEdgeKind::Call => GraphRelation::Call,
        GraphEdgeKind::Import => GraphRelation::Import,
        GraphEdgeKind::Inheritance => GraphRelation::Inheritance,
    }
}

fn edge_key(left: &GraphEdge, right: &GraphEdge) -> std::cmp::Ordering {
    (
        &left.owner_path,
        &left.source,
        &left.target,
        &left.rel,
        &left.owner_hash,
    )
        .cmp(&(
            &right.owner_path,
            &right.source,
            &right.target,
            &right.rel,
            &right.owner_hash,
        ))
}
