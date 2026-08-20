use std::collections::{HashMap, HashSet};

use crate::config::Context;

use crate::db;
use crate::graph::typed_query;
use crate::models::{GraphPathStep, GraphResult};
use crate::visibility;

use super::super::connection::with_optional_core_graph;
use super::super::payload::{row_string_owned, row_usize};
use super::relationship_queries::{
    blast_radius_query, find_callee_ids_batch_query, find_callees_query,
    find_caller_ids_batch_query, find_caller_ids_query, find_callers_query, find_usage_ids_query,
    find_usages_query, get_imports_query, resolve_external_call_target_query,
    symbol_callee_edges_query, symbol_path_steps_query,
};
#[cfg(test)]
use super::relationship_queries::{find_callees_batch_query, find_callers_batch_query};
use super::support::{MAX_GRAPH_LIMIT, row_to_graph_result};
use gobby_core::falkor::GraphClient;

pub const DEFAULT_SYMBOL_PATH_MAX_DEPTH: usize = 8;
pub const MAX_SYMBOL_PATH_DEPTH: usize = 16;
const POST_FILTER_OVERFETCH_FACTOR: usize = 4;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResolvedExternalCallTarget {
    pub id: String,
    pub display_name: String,
}

fn external_call_target_display_name(name: &str, module: &str) -> String {
    if module.is_empty() {
        name.to_string()
    } else {
        format!("{module}.{name}")
    }
}

fn select_external_call_target(
    candidates: Vec<ResolvedExternalCallTarget>,
) -> (Option<ResolvedExternalCallTarget>, Vec<String>) {
    if candidates.len() == 1 {
        return (candidates.into_iter().next(), Vec::new());
    }
    let suggestions = candidates
        .into_iter()
        .map(|candidate| candidate.display_name)
        .collect();
    (None, suggestions)
}

fn graph_fetch_limit(offset: usize, limit: usize) -> usize {
    offset
        .saturating_add(limit)
        .saturating_mul(POST_FILTER_OVERFETCH_FACTOR)
        .min(MAX_GRAPH_LIMIT)
}

fn post_filter_symbol_ids(
    ctx: &Context,
    ids: Vec<String>,
    limit: usize,
) -> anyhow::Result<Vec<String>> {
    if ids.is_empty() {
        return Ok(ids);
    }
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let visible_ids = visibility::visible_symbols_by_ids(&mut conn, ctx, &ids)?
        .into_iter()
        .map(|symbol| symbol.id)
        .collect::<HashSet<_>>();
    Ok(ids
        .into_iter()
        .filter(|id| visible_ids.contains(id))
        .take(limit)
        .collect())
}

fn post_filter_graph_results(
    ctx: &Context,
    results: Vec<GraphResult>,
) -> anyhow::Result<Vec<GraphResult>> {
    if results.is_empty() {
        return Ok(results);
    }
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    visibility::filter_visible_graph_results(&mut conn, ctx, results)
}

pub(crate) fn collect_paged_graph_results(
    offset: usize,
    limit: usize,
    page_size: usize,
    mut fetch_page: impl FnMut(usize, usize) -> anyhow::Result<Vec<GraphResult>>,
    mut filter: impl FnMut(Vec<GraphResult>) -> anyhow::Result<Vec<GraphResult>>,
) -> anyhow::Result<Vec<GraphResult>> {
    if limit == 0 {
        return Ok(Vec::new());
    }
    let mut raw_offset = 0;
    let mut visible_to_skip = offset;
    let mut visible_results = Vec::with_capacity(limit.min(page_size));
    loop {
        let raw_results = fetch_page(raw_offset, page_size)?;
        let raw_count = raw_results.len();
        if raw_count == 0 {
            break;
        }
        for result in filter(raw_results)? {
            if visible_to_skip > 0 {
                visible_to_skip -= 1;
            } else {
                visible_results.push(result);
                if visible_results.len() == limit {
                    return Ok(visible_results);
                }
            }
        }
        raw_offset = raw_offset.saturating_add(raw_count);
        if raw_count < page_size {
            break;
        }
    }
    Ok(visible_results)
}

fn collect_visible_graph_results(
    ctx: &Context,
    offset: usize,
    limit: usize,
    fetch_page: impl FnMut(usize, usize) -> anyhow::Result<Vec<GraphResult>>,
) -> anyhow::Result<Vec<GraphResult>> {
    collect_paged_graph_results(offset, limit, MAX_GRAPH_LIMIT, fetch_page, |raw_results| {
        post_filter_graph_results(ctx, raw_results)
    })
}

fn count_visible_graph_results(
    ctx: &Context,
    mut fetch_page: impl FnMut(usize, usize) -> anyhow::Result<Vec<GraphResult>>,
) -> anyhow::Result<usize> {
    let mut raw_offset = 0;
    let mut visible_count = 0;
    loop {
        let raw_results = fetch_page(raw_offset, MAX_GRAPH_LIMIT)?;
        let raw_count = raw_results.len();
        if raw_count == 0 {
            break;
        }
        visible_count += post_filter_graph_results(ctx, raw_results)?.len();
        raw_offset = raw_offset.saturating_add(raw_count);
        if raw_count < MAX_GRAPH_LIMIT {
            break;
        }
    }
    Ok(visible_count)
}

pub fn count_callers(ctx: &Context, symbol_id: &str) -> anyhow::Result<usize> {
    count_visible_graph_results(ctx, |raw_offset, raw_limit| {
        with_optional_core_graph(ctx, Vec::new, |client| {
            let (query, params) =
                find_callers_query(&ctx.project_id, symbol_id, raw_offset, raw_limit);
            let rows = client.query(&query, Some(params))?;
            Ok(rows.iter().map(row_to_graph_result).collect())
        })
    })
}

pub fn count_callees(ctx: &Context, symbol_id: &str) -> anyhow::Result<usize> {
    count_visible_graph_results(ctx, |raw_offset, raw_limit| {
        with_optional_core_graph(ctx, Vec::new, |client| {
            let (query, params) =
                find_callees_query(&ctx.project_id, symbol_id, raw_offset, raw_limit);
            let rows = client.query(&query, Some(params))?;
            Ok(rows.iter().map(row_to_graph_result).collect())
        })
    })
}

pub fn count_usages(ctx: &Context, symbol_id: &str) -> anyhow::Result<usize> {
    count_visible_graph_results(ctx, |raw_offset, raw_limit| {
        with_optional_core_graph(ctx, Vec::new, |client| {
            let (query, params) =
                find_usages_query(&ctx.project_id, symbol_id, raw_offset, raw_limit);
            let rows = client.query(&query, Some(params))?;
            Ok(rows.iter().map(row_to_graph_result).collect())
        })
    })
}

pub fn find_callers(
    ctx: &Context,
    symbol_id: &str,
    offset: usize,
    limit: usize,
) -> anyhow::Result<Vec<GraphResult>> {
    collect_visible_graph_results(ctx, offset, limit, |raw_offset, raw_limit| {
        with_optional_core_graph(ctx, Vec::new, |client| {
            let (query, params) =
                find_callers_query(&ctx.project_id, symbol_id, raw_offset, raw_limit);
            let rows = client.query(&query, Some(params))?;
            Ok(rows.iter().map(row_to_graph_result).collect())
        })
    })
}

pub fn find_callees(
    ctx: &Context,
    symbol_id: &str,
    offset: usize,
    limit: usize,
) -> anyhow::Result<Vec<GraphResult>> {
    collect_visible_graph_results(ctx, offset, limit, |raw_offset, raw_limit| {
        with_optional_core_graph(ctx, Vec::new, |client| {
            let (query, params) =
                find_callees_query(&ctx.project_id, symbol_id, raw_offset, raw_limit);
            let rows = client.query(&query, Some(params))?;
            Ok(rows.iter().map(row_to_graph_result).collect())
        })
    })
}

pub fn find_usages(
    ctx: &Context,
    symbol_id: &str,
    offset: usize,
    limit: usize,
) -> anyhow::Result<Vec<GraphResult>> {
    collect_visible_graph_results(ctx, offset, limit, |raw_offset, raw_limit| {
        with_optional_core_graph(ctx, Vec::new, |client| {
            let (query, params) =
                find_usages_query(&ctx.project_id, symbol_id, raw_offset, raw_limit);
            let rows = client.query(&query, Some(params))?;
            Ok(rows.iter().map(row_to_graph_result).collect())
        })
    })
}

pub fn find_caller_ids(
    ctx: &Context,
    symbol_id: &str,
    limit: usize,
) -> anyhow::Result<Vec<String>> {
    let fetch_limit = graph_fetch_limit(0, limit);
    let ids = with_optional_core_graph(ctx, Vec::new, |client| {
        let (query, params) = find_caller_ids_query(&ctx.project_id, symbol_id, fetch_limit);
        let rows = client.query(&query, Some(params))?;
        Ok(rows
            .iter()
            .filter_map(|row| row_string_owned(row, &["id"]))
            .collect())
    })?;
    post_filter_symbol_ids(ctx, ids, limit)
}

pub fn find_usage_ids(ctx: &Context, symbol_id: &str, limit: usize) -> anyhow::Result<Vec<String>> {
    let fetch_limit = graph_fetch_limit(0, limit);
    let ids = with_optional_core_graph(ctx, Vec::new, |client| {
        let (query, params) = find_usage_ids_query(&ctx.project_id, symbol_id, fetch_limit);
        let rows = client.query(&query, Some(params))?;
        Ok(rows
            .iter()
            .filter_map(|row| row_string_owned(row, &["id"]))
            .collect())
    })?;
    post_filter_symbol_ids(ctx, ids, limit)
}

#[cfg(test)]
pub fn find_callers_batch(
    ctx: &Context,
    symbol_ids: &[String],
    limit: usize,
) -> anyhow::Result<Vec<GraphResult>> {
    if symbol_ids.is_empty() {
        return Ok(vec![]);
    }
    let fetch_limit = graph_fetch_limit(0, limit);
    let results = with_optional_core_graph(ctx, Vec::new, |client| {
        let (query, params) = find_callers_batch_query(&ctx.project_id, symbol_ids, fetch_limit);
        let rows = client.query(&query, Some(params))?;
        Ok(rows.iter().map(row_to_graph_result).collect())
    })?;
    post_filter_graph_results(ctx, results).map(|results| results.into_iter().take(limit).collect())
}

pub fn find_caller_ids_batch(
    ctx: &Context,
    symbol_ids: &[String],
    limit: usize,
) -> anyhow::Result<Vec<String>> {
    if symbol_ids.is_empty() {
        return Ok(vec![]);
    }
    let fetch_limit = graph_fetch_limit(0, limit);
    let ids = with_optional_core_graph(ctx, Vec::new, |client| {
        let (query, params) = find_caller_ids_batch_query(&ctx.project_id, symbol_ids, fetch_limit);
        let rows = client.query(&query, Some(params))?;
        Ok(rows
            .iter()
            .filter_map(|row| row_string_owned(row, &["id"]))
            .collect())
    })?;
    post_filter_symbol_ids(ctx, ids, limit)
}

#[cfg(test)]
pub fn find_callees_batch(
    ctx: &Context,
    symbol_ids: &[String],
    limit: usize,
) -> anyhow::Result<Vec<GraphResult>> {
    if symbol_ids.is_empty() {
        return Ok(vec![]);
    }
    let fetch_limit = graph_fetch_limit(0, limit);
    let results = with_optional_core_graph(ctx, Vec::new, |client| {
        let (query, params) = find_callees_batch_query(&ctx.project_id, symbol_ids, fetch_limit);
        let rows = client.query(&query, Some(params))?;
        Ok(rows.iter().map(row_to_graph_result).collect())
    })?;
    post_filter_graph_results(ctx, results).map(|results| results.into_iter().take(limit).collect())
}

pub fn find_callee_ids_batch(
    ctx: &Context,
    symbol_ids: &[String],
    limit: usize,
) -> anyhow::Result<Vec<String>> {
    if symbol_ids.is_empty() {
        return Ok(vec![]);
    }
    let fetch_limit = graph_fetch_limit(0, limit);
    let ids = with_optional_core_graph(ctx, Vec::new, |client| {
        let (query, params) = find_callee_ids_batch_query(&ctx.project_id, symbol_ids, fetch_limit);
        let rows = client.query(&query, Some(params))?;
        Ok(rows
            .iter()
            .filter_map(|row| row_string_owned(row, &["id"]))
            .collect())
    })?;
    post_filter_symbol_ids(ctx, ids, limit)
}

pub fn get_imports(ctx: &Context, file_path: &str) -> anyhow::Result<Vec<GraphResult>> {
    with_optional_core_graph(ctx, Vec::new, |client| {
        // IMPORTS edges are tagged per content version; only the local
        // machine's active version of the file may contribute edges.
        let mut conn = db::connect_readonly(&ctx.database_url)?;
        let Some(content_hash) =
            visibility::local_active_content_hash(&mut conn, &ctx.project_id, file_path)?
        else {
            return Ok(Vec::new());
        };
        let (query, params) = get_imports_query(&ctx.project_id, file_path, &content_hash);
        let rows = client.query(&query, Some(params))?;
        Ok(rows.iter().map(row_to_graph_result).collect())
    })
}

pub fn resolve_external_call_target(
    ctx: &Context,
    input: &str,
) -> anyhow::Result<(Option<ResolvedExternalCallTarget>, Vec<String>)> {
    with_optional_core_graph(
        ctx,
        || (None, Vec::new()),
        |client| {
            let (query, params) = resolve_external_call_target_query(&ctx.project_id, input);
            let rows = client.query(&query, Some(params))?;
            let candidates = rows
                .iter()
                .filter_map(|row| {
                    let id = row_string_owned(row, &["id"])?;
                    let name = row_string_owned(row, &["name"]).unwrap_or_else(|| id.clone());
                    let module = row_string_owned(row, &["module"]).unwrap_or_default();
                    Some(ResolvedExternalCallTarget {
                        id,
                        display_name: external_call_target_display_name(&name, &module),
                    })
                })
                .collect();
            Ok(select_external_call_target(candidates))
        },
    )
}

fn symbol_callee_edges(
    client: &mut GraphClient,
    project_id: &str,
    symbol_ids: &[String],
) -> anyhow::Result<Vec<(String, String)>> {
    if symbol_ids.is_empty() {
        return Ok(Vec::new());
    }
    let (query, params) = symbol_callee_edges_query(project_id, symbol_ids);
    let rows = client.query(&query, Some(params))?;
    Ok(rows
        .iter()
        .filter_map(|row| {
            let source_id = row_string_owned(row, &["source_id"])?;
            let target_id = row_string_owned(row, &["target_id"])?;
            Some((source_id, target_id))
        })
        .collect())
}

fn reconstruct_symbol_path(
    from_id: &str,
    to_id: &str,
    parents: &HashMap<String, String>,
) -> Vec<String> {
    let mut path = vec![to_id.to_string()];
    let mut current = to_id.to_string();
    while current != from_id {
        let Some(parent) = parents.get(&current) else {
            return Vec::new();
        };
        path.push(parent.clone());
        current = parent.clone();
    }
    path.reverse();
    path
}

fn symbol_path_steps(
    client: &mut GraphClient,
    project_id: &str,
    symbol_ids: &[String],
) -> anyhow::Result<Vec<GraphPathStep>> {
    if symbol_ids.is_empty() {
        return Ok(Vec::new());
    }
    let (query, params) = symbol_path_steps_query(project_id, symbol_ids);
    let rows = client.query(&query, Some(params))?;
    let mut steps_by_id = HashMap::new();
    for row in rows {
        let Some(id) = row_string_owned(&row, &["symbol_id", "id"]) else {
            continue;
        };
        steps_by_id.insert(
            id.clone(),
            GraphPathStep {
                position: 0,
                name: row_string_owned(&row, &["symbol_name", "name"])
                    .unwrap_or_else(|| id.clone()),
                file_path: row_string_owned(&row, &["file_path", "file"]).unwrap_or_default(),
                line: row_usize(&row, &["line"]).unwrap_or(0),
                id,
            },
        );
    }

    let mut steps = Vec::with_capacity(symbol_ids.len());
    for (position, symbol_id) in symbol_ids.iter().enumerate() {
        let Some(mut step) = steps_by_id.remove(symbol_id) else {
            return Ok(Vec::new());
        };
        step.position = position;
        steps.push(step);
    }
    Ok(steps)
}

pub fn shortest_symbol_path(
    ctx: &Context,
    from_id: &str,
    to_id: &str,
    max_depth: usize,
) -> anyhow::Result<Vec<GraphPathStep>> {
    let max_depth = max_depth.clamp(1, MAX_SYMBOL_PATH_DEPTH);
    with_optional_core_graph(ctx, Vec::new, |client| {
        if from_id == to_id {
            return symbol_path_steps(client, &ctx.project_id, &[from_id.to_string()]);
        }

        let mut visited = HashSet::from([from_id.to_string()]);
        let mut parents = HashMap::new();
        let mut frontier = vec![from_id.to_string()];

        for _ in 0..max_depth {
            let edges = symbol_callee_edges(client, &ctx.project_id, &frontier)?;
            // Routes must not pass through content versions that are not
            // active on this machine; drop invisible targets before they can
            // enter the frontier or the parent map.
            let mut candidates = Vec::new();
            let mut seen_candidates = HashSet::new();
            for (_, target_id) in &edges {
                if !visited.contains(target_id) && seen_candidates.insert(target_id.clone()) {
                    candidates.push(target_id.clone());
                }
            }
            let candidate_count = candidates.len();
            let visible_targets: HashSet<String> =
                post_filter_symbol_ids(ctx, candidates, candidate_count)?
                    .into_iter()
                    .collect();
            let mut next_frontier = Vec::new();
            for (source_id, target_id) in edges {
                if !visible_targets.contains(&target_id) {
                    continue;
                }
                if !visited.insert(target_id.clone()) {
                    continue;
                }
                parents.insert(target_id.clone(), source_id);
                if target_id == to_id {
                    let symbol_ids = reconstruct_symbol_path(from_id, to_id, &parents);
                    return symbol_path_steps(client, &ctx.project_id, &symbol_ids);
                }
                next_frontier.push(target_id);
            }
            if next_frontier.is_empty() {
                break;
            }
            frontier = next_frontier;
        }

        Ok(Vec::new())
    })
}

pub fn blast_radius(
    ctx: &Context,
    symbol_id: &str,
    depth: usize,
) -> anyhow::Result<Vec<GraphResult>> {
    let results = with_optional_core_graph(ctx, Vec::new, |client| {
        let query = blast_radius_query(depth, MAX_GRAPH_LIMIT);
        let params = typed_query::string_params(&[("project", &ctx.project_id), ("id", symbol_id)]);
        let rows = client.query(&query, Some(params))?;
        Ok(rows.iter().map(row_to_graph_result).collect())
    })?;
    post_filter_graph_results(ctx, results)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn target(id: &str, display_name: &str) -> ResolvedExternalCallTarget {
        ResolvedExternalCallTarget {
            id: id.to_string(),
            display_name: display_name.to_string(),
        }
    }

    #[test]
    fn external_call_target_display_uses_module_when_present() {
        assert_eq!(
            external_call_target_display_name("get", "requests"),
            "requests.get"
        );
        assert_eq!(external_call_target_display_name("get", ""), "get");
    }

    #[test]
    fn select_external_call_target_resolves_single_candidate() {
        let (resolved, suggestions) =
            select_external_call_target(vec![target("external-1", "requests.get")]);

        assert!(suggestions.is_empty());
        let resolved = resolved.expect("single external target resolves");
        assert_eq!(resolved.id, "external-1");
        assert_eq!(resolved.display_name, "requests.get");
    }

    #[test]
    fn select_external_call_target_reports_ambiguous_candidates() {
        let (resolved, suggestions) = select_external_call_target(vec![
            target("external-1", "requests.get"),
            target("external-2", "httpx.get"),
        ]);

        assert!(resolved.is_none());
        assert_eq!(suggestions, ["requests.get", "httpx.get"]);
    }
}
