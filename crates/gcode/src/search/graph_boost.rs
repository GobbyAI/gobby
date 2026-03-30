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
    let callers = neo4j::find_callers(ctx, query, 10).unwrap_or_default();
    let usages = neo4j::find_usages(ctx, query, 10).unwrap_or_default();

    let mut ids = Vec::new();
    let mut seen = HashSet::new();
    for r in callers.iter().chain(usages.iter()) {
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
        }
    }

    #[test]
    fn test_graph_boost_no_neo4j() {
        let ctx = make_ctx_no_neo4j();
        let result = graph_boost(&ctx, "some_function");
        assert!(result.is_empty());
    }
}
