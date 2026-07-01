//! Bounded batch planning for `sync_file`.
//!
//! `sync_file` historically issued a single fused `UNWIND` covering every
//! symbol, import, and call in a file. For pathological data files (tens of
//! thousands of rows) that request grew to multiple megabytes and FalkorDB
//! never finished it. This module splits the mutation into bounded batches so
//! no single request exceeds a few hundred rows (gobby-cli #678).

use crate::graph::typed_query::TypedQuery;

use super::mutation::{
    SyncFileMutation, add_definitions_query, add_external_calls_query, add_imports_query,
    add_symbol_calls_query, add_unresolved_calls_query, ensure_file_node_query,
};

/// Maximum number of rows per batched graph-sync query.
///
/// Each row is a small scalar map, so 500 keeps a single request in the tens of
/// KB. A typical source file (a few dozen symbols) stays a single batch. Mirrors
/// the vector lifecycle batch size.
pub(super) const GRAPH_SYNC_BATCH_SIZE: usize = 500;

/// Build the ordered, bounded list of write queries for one `sync_file`.
///
/// Order: (1) a header that MERGEs the `CodeFile` with its final `symbol_count`
/// and `sync_token`; then (2) the import, definition, and call adds, each
/// chunked to [`GRAPH_SYNC_BATCH_SIZE`] and carrying the same `sync_token`. The
/// `add_*_query` builders self-MERGE their nodes, so the chunks are
/// order-independent and idempotent — safe to split.
pub(super) fn plan_sync_batches(input: SyncFileMutation<'_>) -> anyhow::Result<Vec<TypedQuery>> {
    let mut queries = Vec::new();

    // (1) Header: MERGE CodeFile + final symbol_count + sync_token.
    queries.push(ensure_file_node_query(
        input.project_id,
        input.file_path,
        input.symbol_count,
        input.sync_token,
    )?);

    // (2) Adds, each chunked. Empty collections yield zero chunks (no query),
    // matching the old fused query's "skip empty segments" behavior.
    for chunk in input.imports.chunks(GRAPH_SYNC_BATCH_SIZE) {
        queries.push(add_imports_query(
            input.project_id,
            chunk,
            input.sync_token,
        )?);
    }
    for chunk in input.symbols.chunks(GRAPH_SYNC_BATCH_SIZE) {
        queries.push(add_definitions_query(
            input.project_id,
            input.file_path,
            chunk,
            input.sync_token,
        )?);
    }
    for chunk in input.calls.symbol.chunks(GRAPH_SYNC_BATCH_SIZE) {
        queries.push(add_symbol_calls_query(
            input.project_id,
            chunk,
            input.sync_token,
        )?);
    }
    for chunk in input.calls.external.chunks(GRAPH_SYNC_BATCH_SIZE) {
        queries.push(add_external_calls_query(
            input.project_id,
            chunk,
            input.sync_token,
        )?);
    }
    for chunk in input.calls.unresolved.chunks(GRAPH_SYNC_BATCH_SIZE) {
        queries.push(add_unresolved_calls_query(
            input.project_id,
            chunk,
            input.sync_token,
        )?);
    }

    Ok(queries)
}

#[cfg(test)]
mod tests {
    use super::super::mutation::{definition_graph_symbols, partition_call_graph_items};
    use super::*;
    use crate::models::Symbol;

    fn test_symbol(i: usize) -> Symbol {
        Symbol {
            id: format!("sym-{i}"),
            project_id: "proj".to_string(),
            file_path: "src/lib.rs".to_string(),
            name: format!("name_{i}"),
            qualified_name: format!("crate::name_{i}"),
            kind: "function".to_string(),
            language: "rust".to_string(),
            byte_start: 0,
            byte_end: 0,
            line_start: i,
            line_end: i,
            signature: None,
            docstring: None,
            parent_symbol_id: None,
            content_hash: String::new(),
            summary: None,
            created_at: String::new(),
            updated_at: String::new(),
        }
    }

    #[test]
    fn plans_header_then_one_definition_batch_per_chunk() {
        let defs: Vec<Symbol> = (0..1200).map(test_symbol).collect();
        let symbols = definition_graph_symbols(&defs);
        let calls = partition_call_graph_items("proj", "src/lib.rs", &[]);
        let queries = plan_sync_batches(SyncFileMutation {
            project_id: "proj",
            file_path: "src/lib.rs",
            symbol_count: defs.len(),
            imports: &[],
            symbols: &symbols,
            calls: &calls,
            sync_token: "tok-1",
        })
        .expect("plan");

        let expected_batches = defs.len().div_ceil(GRAPH_SYNC_BATCH_SIZE);
        assert_eq!(expected_batches, 3);
        // header + 3 definition batches; no imports/calls.
        assert_eq!(queries.len(), 1 + expected_batches);

        // header MERGEs the CodeFile and carries the final symbol_count.
        assert!(queries[0].cypher.contains("MERGE (f:CodeFile"));
        assert_eq!(
            queries[0].params.get("symbol_count").map(String::as_str),
            Some("1200")
        );

        // every query carries the shared sync_token; none carries a symbol_ids list.
        for query in &queries {
            assert!(
                query
                    .params
                    .get("sync_token")
                    .is_some_and(|value| value.contains("tok-1")),
                "missing sync_token in {}",
                query.cypher
            );
            assert!(!query.params.contains_key("symbol_ids"));
        }
        // the trailing batches are all ADD_DEFINITIONS unwinds.
        for query in &queries[1..] {
            assert!(query.cypher.contains("UNWIND $symbols AS symbol"));
        }
    }

    #[test]
    fn small_file_plans_header_and_single_definition_batch() {
        let defs: Vec<Symbol> = (0..3).map(test_symbol).collect();
        let symbols = definition_graph_symbols(&defs);
        let calls = partition_call_graph_items("proj", "src/lib.rs", &[]);
        let queries = plan_sync_batches(SyncFileMutation {
            project_id: "proj",
            file_path: "src/lib.rs",
            symbol_count: defs.len(),
            imports: &[],
            symbols: &symbols,
            calls: &calls,
            sync_token: "tok-2",
        })
        .expect("plan");

        assert_eq!(queries.len(), 2);
        assert!(queries[0].cypher.contains("MERGE (f:CodeFile"));
        assert!(queries[1].cypher.contains("UNWIND $symbols AS symbol"));
    }
}
