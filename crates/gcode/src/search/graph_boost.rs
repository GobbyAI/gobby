//! Neo4j graph boost: find related symbols to boost in search ranking.
//!
//! Uses callers + usages as the boost set — symbols that are connected
//! to the query term in the call/import graph get a ranking boost via RRF.
//!
//! Source: src/gobby/code_index/searcher.py (_graph_boost method)

use std::collections::HashSet;

use crate::config::Context;
use crate::neo4j;

/// Get symbol IDs related to query via the call/import graph.
///
/// Returns a ranked list of symbol IDs for use as an RRF source.
/// Returns empty vec when Neo4j is unavailable (graceful degradation).
pub fn graph_boost(ctx: &Context, query: &str) -> Vec<String> {
    let callers = neo4j::find_callers(ctx, query, 0, 10).unwrap_or_default();
    let usages = neo4j::find_usages(ctx, query, 0, 10).unwrap_or_default();

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
/// Takes symbol names from the top search results and queries Neo4j for their
/// callees (what they call) and callers (who calls them). Callees are ranked
/// first since they represent implementation details more useful for conceptual
/// queries. Returns deduplicated symbol IDs for use as an RRF source.
pub fn graph_expand(ctx: &Context, seed_names: Vec<String>) -> Vec<String> {
    if seed_names.is_empty() {
        return vec![];
    }

    // Callees first — "what do these symbols call?" surfaces implementation details
    let callees = neo4j::find_callees_batch(ctx, &seed_names, 30).unwrap_or_default();
    // Callers second — "who calls these symbols?" surfaces broader context
    let callers = neo4j::find_callers_batch(ctx, &seed_names, 30).unwrap_or_default();

    let mut ids = Vec::new();
    let mut seen = HashSet::new();
    for r in callees.iter().chain(callers.iter()) {
        if !r.id.is_empty() && seen.insert(r.id.clone()) {
            ids.push(r.id.clone());
        }
    }
    ids
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn make_ctx_no_neo4j() -> Context {
        Context {
            db_path: PathBuf::from("/nonexistent"),
            project_root: PathBuf::from("/nonexistent"),
            project_id: "test".to_string(),
            quiet: true,
            neo4j: None,
            qdrant: None,
            daemon_url: None,
        }
    }

    #[test]
    fn test_graph_boost_no_neo4j() {
        let ctx = make_ctx_no_neo4j();
        let result = graph_boost(&ctx, "some_function");
        assert!(result.is_empty());
    }
}
