//! Shared `gcode graph view` scaffold: clap/dispatch glue, payload, visibility.

#[allow(dead_code)]
mod class_hierarchy;
#[allow(dead_code)]
mod fcg;
#[allow(dead_code)]
mod mcg;
mod render;

use anyhow::Context as _;
use std::collections::HashSet;

use crate::cli::GraphViewArgs;
use crate::codewiki_facts::{CodewikiFacts, GraphAvailability};
use crate::config::Context;
use crate::db;
use crate::graph::code_graph::GraphReadError;
use crate::output::Format;
use crate::search::fts::ResolvedGraphSymbol;

use super::reads::{hint_for, hint_for_error, resolve_symbol_with_connection};
use render::{ViewEdgeInput, ViewNodeInput, ViewPayload, ViewSeed, build_view_payload, print_view};

#[allow(dead_code)]
#[derive(Clone, Debug, Eq, PartialEq, Hash)]
pub(super) struct VisibleOwnerKey {
    pub path: String,
    pub content_hash: String,
    pub machine_id: String,
}

#[allow(dead_code)]
#[derive(Clone, Debug, Default)]
pub(super) struct VisibleFileMap {
    pub owners: HashSet<VisibleOwnerKey>,
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
}

#[allow(dead_code)]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum CandidateEndpointKind {
    File,
    Symbol,
    Module,
    External,
    Unresolved,
}

#[allow(dead_code)]
#[derive(Clone, Debug, Eq, PartialEq)]
pub(super) struct CandidateEndpoint {
    pub kind: CandidateEndpointKind,
    pub id: String,
    pub file: Option<String>,
    pub content_hash: Option<String>,
    pub machine_id: Option<String>,
}

#[allow(dead_code)]
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

#[allow(dead_code)]
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
                _ => visible.owners.iter().any(|owner| owner.path == file),
            }
        }
    }
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
    let symbol = resolve_view_seed(ctx, &args.seed).context("resolve graph view seed")?;
    let facts = CodewikiFacts::from_context(ctx.clone());
    let hint = hint_for_availability(ctx, &facts.graph_availability());
    let seed = ViewSeed {
        id: symbol.id,
        name: symbol.display_name,
        kind: "symbol".to_string(),
        file: None,
    };
    let payload = empty_view_payload(ctx, args, seed, hint)?;
    print_view(&payload, format)
}

#[cfg(test)]
#[path = "tests.rs"]
mod tests;
