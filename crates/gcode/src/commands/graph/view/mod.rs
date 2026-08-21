//! Shared `gcode graph view` scaffold: clap/dispatch glue, payload, visibility.

mod class_hierarchy;
mod fcg;
mod mcg;
mod render;

use anyhow::Context as _;
use std::collections::HashSet;

use crate::cli::GraphViewArgs;
use crate::codewiki_facts::GraphAvailability;
use crate::config::Context;
use crate::db;
use crate::graph::code_graph::GraphReadError;
use crate::output::Format;
use crate::search::fts::ResolvedGraphSymbol;
use crate::visibility;

use super::reads::{hint_for, hint_for_error, resolve_symbol_with_connection};
use render::{
    NodeKey, ViewEdgeInput, ViewNodeInput, ViewPayload, ViewSeed, build_view_payload,
    node_file_for_kind,
};

#[derive(Clone, Debug, Eq, PartialEq, Hash)]
pub(super) struct VisibleOwnerKey {
    pub path: String,
    pub content_hash: String,
    pub machine_id: String,
}

/// Visibility decisions for one hop's candidate edges.
///
/// `owners` carries the (path, hash, machine) keys of edge owners whose path the
/// hub reports as visible; `visible_paths` is the full set of hub-visible paths
/// among every owner and endpoint file in the hop, so endpoints in files other
/// than the edge owner's can be admitted.
#[derive(Clone, Debug, Default)]
pub(super) struct VisibleFileMap {
    pub owners: HashSet<VisibleOwnerKey>,
    pub visible_paths: HashSet<String>,
    pub overlay_shadowed_paths: HashSet<String>,
}

impl VisibleFileMap {
    fn owner_is_visible(&self, path: &str, content_hash: &str, machine_id: &str) -> bool {
        if self.overlay_shadowed_paths.contains(path) {
            return false;
        }
        self.owners.contains(&VisibleOwnerKey {
            path: path.to_string(),
            content_hash: content_hash.to_string(),
            machine_id: machine_id.to_string(),
        })
    }

    fn path_is_visible(&self, path: &str) -> bool {
        self.visible_paths.contains(path) || self.owners.iter().any(|owner| owner.path == path)
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum CandidateEndpointKind {
    File,
    Symbol,
    Module,
    External,
    Unresolved,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(super) struct CandidateEndpoint {
    pub kind: CandidateEndpointKind,
    pub id: String,
    pub name: Option<String>,
    pub file: Option<String>,
    pub content_hash: Option<String>,
    pub machine_id: Option<String>,
}

impl CandidateEndpoint {
    fn key(&self) -> NodeKey {
        match self.kind {
            CandidateEndpointKind::Symbol => NodeKey::symbol(&self.id),
            CandidateEndpointKind::File => NodeKey::file(&self.id),
            CandidateEndpointKind::Module => NodeKey::module(&self.id),
            CandidateEndpointKind::External => NodeKey::external(&self.id),
            CandidateEndpointKind::Unresolved => NodeKey::unresolved(&self.id),
        }
    }

    /// Payload node for this endpoint. `file` follows the `nodes[].file`
    /// contract: declaring file for files and symbols, the unique provider for
    /// modules, and `null` for external and unresolved terminals.
    fn node(&self) -> ViewNodeInput {
        let key = self.key();
        let kind = key.kind;
        ViewNodeInput {
            key,
            name: self.name.clone().unwrap_or_else(|| self.id.clone()),
            kind: kind.key_prefix().to_string(),
            file: node_file_for_kind(kind, self.file.clone(), self.file.as_slice()),
            community: None,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(super) struct ViewEdgeCandidate {
    pub source: CandidateEndpoint,
    pub target: CandidateEndpoint,
    pub rel: String,
    pub owner_path: String,
    pub owner_hash: String,
    pub owner_machine: String,
    pub overlay_shadowed: bool,
    pub hop: usize,
}

#[allow(dead_code)]
#[derive(Clone, Debug, Eq, PartialEq)]
pub(super) struct VisibleBound {
    pub edges: Vec<ViewEdgeCandidate>,
    pub truncated: bool,
    pub hop_cut: bool,
}

pub(super) fn take_visible_before_bound(
    candidates: impl IntoIterator<Item = ViewEdgeCandidate>,
    visible: &VisibleFileMap,
    edge_limit: Option<usize>,
    hop_limit: Option<usize>,
) -> VisibleBound {
    let visible_edges = candidates
        .into_iter()
        .filter(|edge| edge_is_visible(edge, visible))
        .collect::<Vec<_>>();
    let hop_cut = hop_limit.is_some_and(|limit| visible_edges.iter().any(|edge| edge.hop > limit));
    let after_hop = match hop_limit {
        Some(limit) => visible_edges
            .into_iter()
            .filter(|edge| edge.hop <= limit)
            .collect::<Vec<_>>(),
        None => visible_edges,
    };
    match edge_limit {
        Some(limit) => {
            let truncated = after_hop.len() > limit;
            VisibleBound {
                edges: after_hop.into_iter().take(limit).collect(),
                truncated,
                hop_cut,
            }
        }
        None => VisibleBound {
            edges: after_hop,
            truncated: false,
            hop_cut,
        },
    }
}

fn edge_is_visible(edge: &ViewEdgeCandidate, visible: &VisibleFileMap) -> bool {
    if edge.overlay_shadowed {
        return false;
    }
    if !visible.owner_is_visible(&edge.owner_path, &edge.owner_hash, &edge.owner_machine) {
        return false;
    }
    endpoint_is_visible(&edge.source, visible) && endpoint_is_visible(&edge.target, visible)
}

fn endpoint_is_visible(endpoint: &CandidateEndpoint, visible: &VisibleFileMap) -> bool {
    match endpoint.kind {
        CandidateEndpointKind::External | CandidateEndpointKind::Unresolved => true,
        CandidateEndpointKind::File
        | CandidateEndpointKind::Symbol
        | CandidateEndpointKind::Module => {
            let Some(file) = endpoint.file.as_deref() else {
                return true;
            };
            if visible.overlay_shadowed_paths.contains(file) {
                return false;
            }
            match (
                endpoint.content_hash.as_deref(),
                endpoint.machine_id.as_deref(),
            ) {
                (Some(hash), Some(machine)) => visible.owner_is_visible(file, hash, machine),
                _ => visible.path_is_visible(file),
            }
        }
    }
}

/// Every non-empty owner and endpoint path referenced by `candidates`, deduplicated.
pub(super) fn candidate_paths(candidates: &[ViewEdgeCandidate]) -> Vec<String> {
    let mut paths = HashSet::new();
    for edge in candidates {
        if !edge.owner_path.is_empty() {
            paths.insert(edge.owner_path.clone());
        }
        for file in [&edge.source.file, &edge.target.file].into_iter().flatten() {
            paths.insert(file.clone());
        }
    }
    paths.into_iter().collect()
}

/// Pure half of the visibility builder: owners whose path is hub-visible plus the
/// visible path set itself, so endpoint files outside the owner set still admit.
pub(super) fn visible_map_from(
    candidates: &[ViewEdgeCandidate],
    visible_paths: HashSet<String>,
    machine_id: &str,
) -> VisibleFileMap {
    let owners = candidates
        .iter()
        .filter(|edge| visible_paths.contains(&edge.owner_path))
        .map(|edge| VisibleOwnerKey {
            path: edge.owner_path.clone(),
            content_hash: edge.owner_hash.clone(),
            machine_id: machine_id.to_string(),
        })
        .collect();
    VisibleFileMap {
        owners,
        visible_paths,
        overlay_shadowed_paths: HashSet::new(),
    }
}

pub(super) fn visible_map_for_candidates(
    ctx: &Context,
    candidates: &[ViewEdgeCandidate],
) -> anyhow::Result<VisibleFileMap> {
    let paths = candidate_paths(candidates);
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let visible_paths = visibility::visible_graph_paths(&mut conn, ctx, &paths)?;
    Ok(visible_map_from(
        candidates,
        visible_paths,
        &local_machine_id(),
    ))
}

pub(super) fn local_machine_id() -> String {
    visibility::local_machine_uuid_or_invisible()
        .map(|id| id.to_string())
        .unwrap_or_default()
}

pub(super) fn non_empty(value: &str) -> Option<String> {
    if value.is_empty() {
        None
    } else {
        Some(value.to_string())
    }
}

pub(super) fn endpoint_kind_from_label(label: &str) -> CandidateEndpointKind {
    match label {
        "external" => CandidateEndpointKind::External,
        "unresolved" => CandidateEndpointKind::Unresolved,
        "file" => CandidateEndpointKind::File,
        "module" => CandidateEndpointKind::Module,
        _ => CandidateEndpointKind::Symbol,
    }
}

/// Seed descriptor and walk endpoint for a resolved symbol; both carry the
/// symbol's declaring file so the payload's `seed.file` and seed node `file`
/// agree with the `nodes[].file` contract.
fn symbol_seed(symbol: &ResolvedGraphSymbol) -> (ViewSeed, CandidateEndpoint) {
    (
        ViewSeed {
            id: symbol.id.clone(),
            name: symbol.display_name.clone(),
            kind: "symbol".to_string(),
            file: symbol.file_path.clone(),
        },
        CandidateEndpoint {
            kind: CandidateEndpointKind::Symbol,
            id: symbol.id.clone(),
            name: Some(symbol.display_name.clone()),
            file: symbol.file_path.clone(),
            content_hash: None,
            machine_id: None,
        },
    )
}

#[derive(Debug)]
pub(super) enum SeedLookupError {
    Infrastructure(anyhow::Error),
    Missing {
        input: String,
        suggestions: Vec<String>,
    },
}

impl std::fmt::Display for SeedLookupError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Infrastructure(error) => write!(f, "{error:#}"),
            Self::Missing { input, suggestions } if suggestions.is_empty() => {
                write!(f, "No symbol matching '{input}' found")
            }
            Self::Missing { input, suggestions } => write!(
                f,
                "Ambiguous symbol '{input}'. Refine the query. Matches: {}",
                suggestions.join(", ")
            ),
        }
    }
}

impl std::error::Error for SeedLookupError {}

pub(super) fn classify_seed_lookup(
    input: &str,
    lookup: anyhow::Result<(Option<ResolvedGraphSymbol>, Vec<String>)>,
) -> Result<ResolvedGraphSymbol, SeedLookupError> {
    match lookup {
        Err(error) => Err(SeedLookupError::Infrastructure(error)),
        Ok((Some(symbol), _)) => Ok(symbol),
        Ok((None, suggestions)) => Err(SeedLookupError::Missing {
            input: input.to_string(),
            suggestions,
        }),
    }
}

fn resolve_view_seed(ctx: &Context, input: &str) -> anyhow::Result<ResolvedGraphSymbol> {
    let mut conn =
        db::connect_readonly(&ctx.database_url).map_err(SeedLookupError::Infrastructure)?;
    classify_seed_lookup(
        input,
        resolve_symbol_with_connection(&mut conn, &ctx.project_id, input),
    )
    .map_err(anyhow::Error::from)
}

pub(super) fn hint_for_availability(
    ctx: &Context,
    availability: &GraphAvailability,
) -> Option<String> {
    match availability {
        GraphAvailability::Available => None,
        GraphAvailability::Unavailable { reason } if reason == "FalkorDB is not configured" => {
            hint_for(ctx)
        }
        GraphAvailability::Unavailable { reason } => hint_for_error(
            ctx,
            &anyhow::Error::new(GraphReadError::Unreachable {
                message: reason.clone(),
            }),
        ),
    }
}

fn empty_view_payload(
    ctx: &Context,
    args: &GraphViewArgs,
    seed: ViewSeed,
    hint: Option<String>,
) -> anyhow::Result<ViewPayload> {
    build_view_payload(
        ctx.project_id.clone(),
        ctx.project_root.display().to_string(),
        args.view,
        seed,
        args.effective_depth(),
        false,
        false,
        hint,
        Vec::<ViewNodeInput>::new(),
        Vec::<ViewEdgeInput>::new(),
        Vec::new(),
    )
}

pub(crate) fn run(ctx: &Context, args: &GraphViewArgs, format: Format) -> anyhow::Result<()> {
    match args.view {
        crate::cli::GraphViewKind::Mcg => mcg::run(ctx, args, format),
        crate::cli::GraphViewKind::Fcg => {
            let symbol = resolve_view_seed(ctx, &args.seed).context("resolve graph view seed")?;
            fcg::run(ctx, args, &symbol, format)
        }
        crate::cli::GraphViewKind::ClassHierarchy => {
            let symbol = resolve_view_seed(ctx, &args.seed).context("resolve graph view seed")?;
            class_hierarchy::run(ctx, args, &symbol, format)
        }
    }
}

#[cfg(test)]
#[path = "tests.rs"]
mod tests;
