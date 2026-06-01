//! FalkorDB graph boost: find related symbols to boost in search ranking.
//!
//! Uses callers + usages as the boost set — symbols that are connected
//! to the resolved query symbol in the call graph get a ranking boost via RRF.
//!
//! Source: src/gobby/code_index/searcher.py (_graph_boost method)

use std::collections::{BTreeMap, HashSet};

use crate::config::Context;
use crate::db;
use crate::graph::code_graph;
use crate::search::fts;
use crate::visibility;

/// Get symbol IDs related to query via the call/import graph.
///
/// Returns a ranked list of symbol IDs for use as an RRF source.
/// Returns empty vec when FalkorDB is unavailable (graceful degradation).
pub fn graph_boost(ctx: &Context, query: &str) -> Vec<String> {
    if ctx.falkordb.is_none() {
        return vec![];
    }

    let mut conn = match db::connect_readonly(&ctx.database_url) {
        Ok(conn) => conn,
        Err(_) => return vec![],
    };
    let empty_paths = Vec::new();
    let mut resolved =
        fts::search_symbols_exact_first_visible(&mut conn, query, ctx, None, None, &empty_paths, 1);
    let Some(symbol) = resolved.pop() else {
        return vec![];
    };
    let graph_ctx = visibility::context_for_source_project(ctx, &symbol.project_id);

    let callers = code_graph::find_callers(&graph_ctx, &symbol.id, 0, 10).unwrap_or_default();
    let usages = code_graph::find_usages(&graph_ctx, &symbol.id, 0, 10).unwrap_or_default();

    let mut ids = Vec::new();
    let mut seen = HashSet::new();
    for r in callers.iter().chain(usages.iter()) {
        if !r.id.is_empty() && seen.insert(r.id.clone()) {
            ids.push(r.id.clone());
        }
    }
    ids
}

/// Expand the graph neighborhood of seed symbols found by FTS/semantic search.
///
/// Takes symbol IDs from the top search results and queries FalkorDB for their
/// callees (what they call) and callers (who calls them). Callees are ranked
/// first since they represent implementation details more useful for conceptual
/// queries. Returns deduplicated symbol IDs for use as an RRF source.
pub fn graph_expand(ctx: &Context, seed_ids: &[String]) -> Vec<String> {
    if seed_ids.is_empty() {
        return vec![];
    }

    let mut ids = Vec::new();
    let mut seen = HashSet::new();
    let mut conn = db::connect_readonly(&ctx.database_url).ok();
    let mut by_project: BTreeMap<String, Vec<String>> = BTreeMap::new();
    if let Some(conn) = conn.as_mut()
        && let Ok(symbols) = visibility::visible_symbols_by_ids(conn, ctx, seed_ids)
    {
        for symbol in symbols {
            by_project
                .entry(symbol.project_id)
                .or_default()
                .push(symbol.id);
        }
    }
    if by_project.is_empty() {
        by_project.insert(ctx.project_id.clone(), seed_ids.to_vec());
    }

    for (project_id, ids_for_project) in by_project {
        let graph_ctx = visibility::context_for_source_project(ctx, &project_id);
        // Callees first — "what do these symbols call?" surfaces implementation details.
        let callees =
            code_graph::find_callees_batch(&graph_ctx, &ids_for_project, 30).unwrap_or_default();
        // Callers second — "who calls these symbols?" surfaces broader context.
        let callers =
            code_graph::find_callers_batch(&graph_ctx, &ids_for_project, 30).unwrap_or_default();
        for r in callees.iter().chain(callers.iter()) {
            if r.id.is_empty() || !seen.insert(r.id.clone()) {
                continue;
            }
            ids.push(r.id.clone());
        }
    }
    ids
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn make_ctx_no_falkordb() -> Context {
        Context {
            database_url: "postgresql://localhost/nonexistent".to_string(),
            project_root: PathBuf::from("/nonexistent"),
            project_id: "test".to_string(),
            quiet: true,
            falkordb: None,
            qdrant: None,
            embedding: None,
            code_vectors: crate::config::CodeVectorSettings::default(),
            daemon_url: None,
            index_scope: crate::config::ProjectIndexScope::Single,
        }
    }

    #[test]
    fn test_graph_boost_no_falkordb() {
        let ctx = make_ctx_no_falkordb();
        let result = graph_boost(&ctx, "some_function");
        assert!(result.is_empty());
    }

    #[test]
    fn test_graph_expand_no_falkordb() {
        let ctx = make_ctx_no_falkordb();
        let result = graph_expand(&ctx, &["some_function".to_string()]);
        assert!(result.is_empty());
    }

    #[test]
    fn test_graph_expand_empty_seeds() {
        let ctx = make_ctx_no_falkordb();
        let result = graph_expand(&ctx, &[]);
        assert!(result.is_empty());
    }
}
