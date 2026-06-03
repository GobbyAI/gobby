//! FalkorDB graph boost: find related symbols to boost in search ranking.
//!
//! Uses callers + usages as the boost set — symbols that are connected
//! to the resolved query symbol in the call graph get a ranking boost via RRF.
//!
//! Source: src/gobby/code_index/searcher.py (_graph_boost method)

use std::collections::{BTreeMap, HashSet};

use crate::config::Context;
use crate::graph::code_graph;
use crate::search::fts;
use crate::visibility;

/// Get symbol IDs related to query via the call/import graph.
///
/// Returns a ranked list of symbol IDs for use as an RRF source.
/// Returns empty vec when FalkorDB is unavailable (graceful degradation).
pub fn graph_boost(ctx: &Context, conn: Option<&mut postgres::Client>, query: &str) -> Vec<String> {
    if ctx.falkordb.is_none() {
        return vec![];
    }

    let Some(conn) = conn else {
        return vec![];
    };
    let mut resolved =
        fts::search_symbols_exact_first_visible(conn, query, ctx, None, None, &[], 1).results;
    let Some(symbol) = resolved.pop() else {
        return vec![];
    };
    let graph_ctx = visibility::context_for_source_project(ctx, &symbol.project_id);

    let callers = code_graph::find_caller_ids(&graph_ctx, &symbol.id, 10).unwrap_or_default();
    let usages = code_graph::find_usage_ids(&graph_ctx, &symbol.id, 10).unwrap_or_default();

    let mut ids = Vec::new();
    let mut seen = HashSet::new();
    for id in callers.into_iter().chain(usages) {
        if !id.is_empty() && seen.insert(id.clone()) {
            ids.push(id);
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
pub fn graph_expand(
    ctx: &Context,
    conn: Option<&mut postgres::Client>,
    seed_ids: &[String],
) -> Vec<String> {
    if seed_ids.is_empty() || ctx.falkordb.is_none() {
        return vec![];
    }

    let mut by_project: BTreeMap<String, Vec<String>> = BTreeMap::new();
    let Some(conn) = conn else {
        return vec![];
    };
    if let Ok(symbols) = visibility::visible_symbols_by_ids(conn, ctx, seed_ids) {
        for symbol in symbols {
            by_project
                .entry(symbol.project_id)
                .or_default()
                .push(symbol.id);
        }
    }

    graph_expand_grouped(ctx, by_project, |graph_ctx, ids_for_project| {
        // Callees first — "what do these symbols call?" surfaces implementation details.
        let callees =
            code_graph::find_callee_ids_batch(graph_ctx, ids_for_project, 30).unwrap_or_default();
        // Callers second — "who calls these symbols?" surfaces broader context.
        let callers =
            code_graph::find_caller_ids_batch(graph_ctx, ids_for_project, 30).unwrap_or_default();
        (callees, callers)
    })
}

fn graph_expand_grouped(
    ctx: &Context,
    by_project: BTreeMap<String, Vec<String>>,
    mut graph_neighbors: impl FnMut(&Context, &[String]) -> (Vec<String>, Vec<String>),
) -> Vec<String> {
    let mut ids = Vec::new();
    let mut seen = HashSet::new();
    for (project_id, ids_for_project) in by_project {
        let graph_ctx = visibility::context_for_source_project(ctx, &project_id);
        let (callees, callers) = graph_neighbors(&graph_ctx, &ids_for_project);
        for id in callees.into_iter().chain(callers) {
            if id.is_empty() || !seen.insert(id.clone()) {
                continue;
            }
            ids.push(id);
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

    fn make_ctx_with_overlay() -> Context {
        Context {
            database_url: "postgresql://localhost/nonexistent".to_string(),
            project_root: PathBuf::from("/overlay"),
            project_id: "overlay".to_string(),
            quiet: true,
            falkordb: Some(crate::config::FalkorConfig {
                host: "127.0.0.1".to_string(),
                port: 16379,
                password: None,
                graph_name: "g".to_string(),
            }),
            qdrant: None,
            embedding: None,
            code_vectors: crate::config::CodeVectorSettings::default(),
            daemon_url: None,
            index_scope: crate::config::ProjectIndexScope::Overlay {
                overlay_project_id: "overlay".to_string(),
                overlay_root: PathBuf::from("/overlay"),
                parent_project_id: "parent".to_string(),
                parent_root: PathBuf::from("/parent"),
            },
        }
    }

    #[test]
    fn test_graph_boost_no_falkordb() {
        let ctx = make_ctx_no_falkordb();
        let result = graph_boost(&ctx, None, "some_function");
        assert!(result.is_empty());
    }

    #[test]
    fn test_graph_expand_no_falkordb() {
        let ctx = make_ctx_no_falkordb();
        let result = graph_expand(&ctx, None, &["some_function".to_string()]);
        assert!(result.is_empty());
    }

    #[test]
    fn test_graph_expand_empty_seeds() {
        let ctx = make_ctx_no_falkordb();
        let result = graph_expand(&ctx, None, &[]);
        assert!(result.is_empty());
    }

    #[test]
    fn graph_expand_grouped_expands_each_project_scope_and_dedupes() {
        let ctx = make_ctx_with_overlay();
        let by_project = BTreeMap::from([
            (
                "overlay".to_string(),
                vec!["overlay-seed-1".to_string(), "overlay-seed-2".to_string()],
            ),
            ("parent".to_string(), vec!["parent-seed".to_string()]),
        ]);
        let mut calls = Vec::new();

        let expanded = graph_expand_grouped(&ctx, by_project, |graph_ctx, ids| {
            calls.push((graph_ctx.project_id.clone(), ids.to_vec()));
            match graph_ctx.project_id.as_str() {
                "overlay" => (
                    vec!["impl-a".to_string(), "shared".to_string()],
                    vec!["caller-a".to_string(), "shared".to_string()],
                ),
                "parent" => (
                    vec!["parent-impl".to_string(), "impl-a".to_string()],
                    vec!["".to_string(), "parent-caller".to_string()],
                ),
                other => panic!("unexpected project {other}"),
            }
        });

        assert_eq!(
            calls,
            vec![
                (
                    "overlay".to_string(),
                    vec!["overlay-seed-1".to_string(), "overlay-seed-2".to_string()]
                ),
                ("parent".to_string(), vec!["parent-seed".to_string()])
            ]
        );
        assert_eq!(
            expanded,
            vec![
                "impl-a".to_string(),
                "shared".to_string(),
                "caller-a".to_string(),
                "parent-impl".to_string(),
                "parent-caller".to_string()
            ]
        );
    }
}
