use super::*;
use crate::config::{CodeVectorSettings, Context};
use crate::models::{CallRelation, ImportRelation, ProjectionProvenance, SOURCE_SYSTEM_GCODE};
use gobby_core::falkor::Row;
use serde_json::json;

fn test_context(falkordb: Option<crate::config::FalkorConfig>) -> Context {
    Context {
        database_url: "postgresql://localhost/nonexistent".to_string(),
        project_root: std::path::PathBuf::from("/tmp/project"),
        project_id: "project-1".to_string(),
        quiet: true,
        falkordb,
        qdrant: None,
        embedding: None,
        code_vectors: CodeVectorSettings::default(),
        daemon_url: None,
        index_scope: crate::config::ProjectIndexScope::Single,
    }
}

#[test]
fn code_edges_carry_provenance() {
    let metadata = extracted_code_edge_metadata("src/lib.rs", 42, Some("caller-1"));

    assert_eq!(metadata.provenance, ProjectionProvenance::Extracted);
    assert_eq!(metadata.confidence, Some(1.0));
    assert_eq!(metadata.source_system, SOURCE_SYSTEM_GCODE);
    assert_eq!(metadata.source_file_path.as_deref(), Some("src/lib.rs"));
    assert_eq!(metadata.source_line, Some(42));
    assert_eq!(metadata.source_symbol_id.as_deref(), Some("caller-1"));
}

#[test]
fn read_apis_return_node_link_payloads_with_link_metadata() {
    let mut payload = GraphPayload::default();
    payload.push_node(GraphNode::new("src/lib.rs", "src/lib.rs", "file"));

    let link_row = Row::from([
        ("source".to_string(), json!("src/lib.rs")),
        ("target".to_string(), json!("symbol-1")),
        ("type".to_string(), json!("DEFINES")),
        ("line".to_string(), json!(12)),
        ("provenance".to_string(), json!("EXTRACTED")),
        ("confidence".to_string(), json!(1.0)),
        ("source_system".to_string(), json!("gcode")),
        ("source_file_path".to_string(), json!("src/lib.rs")),
        ("source_line".to_string(), json!(12)),
        ("source_symbol_id".to_string(), json!("symbol-1")),
    ]);
    payload
        .links
        .push(GraphLink::from_row(&link_row).expect("link row has endpoints"));

    let encoded = serde_json::to_value(&payload).expect("payload serializes");

    assert_eq!(encoded["nodes"][0]["id"], "src/lib.rs");
    assert_eq!(encoded["nodes"][0]["type"], "file");
    assert_eq!(encoded["links"][0]["source"], "src/lib.rs");
    assert_eq!(encoded["links"][0]["target"], "symbol-1");
    assert_eq!(encoded["links"][0]["type"], "DEFINES");
    assert_eq!(encoded["links"][0]["metadata"]["provenance"], "EXTRACTED");
    assert_eq!(encoded["links"][0]["metadata"]["source_system"], "gcode");
}

#[test]
fn graph_read_guard_stays_strict_but_public_reads_degrade_without_service() {
    let ctx = test_context(None);

    let guard_error = require_graph_reads(&ctx).expect_err("missing FalkorDB must fail");
    assert!(matches!(
        guard_error.downcast_ref::<GraphReadError>(),
        Some(GraphReadError::NotConfigured)
    ));

    assert_eq!(
        project_overview_graph(&ctx, 10).expect("overview degrades"),
        GraphPayload::default()
    );
    assert_eq!(
        file_graph(&ctx, "src/lib.rs").expect("file graph degrades"),
        GraphPayload::default()
    );
    assert_eq!(
        symbol_neighbors(&ctx, "symbol-1", 10).expect("neighbors degrade"),
        GraphPayload::default()
    );
    assert_eq!(
        blast_radius_graph(
            &ctx,
            GraphBlastRadiusTarget::SymbolId("symbol-1".to_string()),
            2,
            10
        )
        .expect("blast graph degrades"),
        GraphPayload::default()
    );
    assert_eq!(count_callers(&ctx, "symbol-1").expect("count degrades"), 0);
    assert_eq!(count_usages(&ctx, "symbol-1").expect("count degrades"), 0);
    assert!(
        find_callers(&ctx, "symbol-1", 0, 10)
            .expect("callers degrade")
            .is_empty()
    );
    assert!(
        find_usages(&ctx, "symbol-1", 0, 10)
            .expect("usages degrade")
            .is_empty()
    );
    assert!(
        find_caller_ids(&ctx, "symbol-1", 10)
            .expect("caller ids degrade")
            .is_empty()
    );
    assert!(
        find_usage_ids(&ctx, "symbol-1", 10)
            .expect("usage ids degrade")
            .is_empty()
    );
    assert!(
        find_callers_batch(&ctx, &["symbol-1".to_string()], 10)
            .expect("caller batch degrades")
            .is_empty()
    );
    assert!(
        find_caller_ids_batch(&ctx, &["symbol-1".to_string()], 10)
            .expect("caller id batch degrades")
            .is_empty()
    );
    assert!(
        find_callees_batch(&ctx, &["symbol-1".to_string()], 10)
            .expect("callee batch degrades")
            .is_empty()
    );
    assert!(
        find_callee_ids_batch(&ctx, &["symbol-1".to_string()], 10)
            .expect("callee id batch degrades")
            .is_empty()
    );
    assert!(
        get_imports(&ctx, "src/lib.rs")
            .expect("imports degrade")
            .is_empty()
    );
    assert!(
        blast_radius(&ctx, "symbol-1", 2)
            .expect("blast degrades")
            .is_empty()
    );
}

#[test]
fn compact_detail_truncates_on_char_boundaries() {
    let detail = compact_detail(&format!("{} tail", "é".repeat(300)));

    assert!(detail.ends_with("..."));
    assert_eq!(detail.chars().count(), 240);
}

#[test]
fn file_blast_rows_are_deduped_and_limited_after_merge() {
    let rows = vec![
        Row::from([
            ("node_id".to_string(), json!("symbol-2")),
            ("node_name".to_string(), json!("zeta")),
            ("distance".to_string(), json!(2)),
        ]),
        Row::from([
            ("node_id".to_string(), json!("symbol-1")),
            ("node_name".to_string(), json!("alpha")),
            ("distance".to_string(), json!(1)),
        ]),
        Row::from([
            ("node_id".to_string(), json!("symbol-1")),
            ("node_name".to_string(), json!("alpha")),
            ("distance".to_string(), json!(3)),
        ]),
    ];

    let rows = dedupe_limited_blast_rows(rows, 1);

    assert_eq!(rows.len(), 1);
    assert_eq!(
        row_string_owned(&rows[0], &["node_id"]).as_deref(),
        Some("symbol-1")
    );
    assert_eq!(row_usize(&rows[0], &["distance"]), Some(1));
}

#[test]
fn file_calls_query_keeps_node_and_metadata_source_paths_distinct() {
    let (query, _) = file_calls_query("project-1", "src/lib.rs");

    assert!(query.contains("source.file_path AS source_file_path"));
    assert!(query.contains("r.source_file_path AS metadata_source_file_path"));
    assert!(!query.contains("r.source_file_path AS source_file_path"));
}

#[test]
fn graph_write_uses_synced_file_path_for_import_and_call_sources() {
    let imports = vec![ImportRelation {
        file_path: "stale/path.rs".to_string(),
        module_name: "crate::dep".to_string(),
    }];
    let calls = vec![CallRelation::new(
        "caller-1".to_string(),
        "callee".to_string(),
        "stale/path.rs".to_string(),
        7,
    )];

    let import_items = import_graph_items("src/lib.rs", &imports);
    let call_groups = partition_call_graph_items("project-1", "src/lib.rs", &calls);

    assert_eq!(import_items[0].source_file, "src/lib.rs");
    assert_eq!(call_groups.unresolved[0].file_path, "src/lib.rs");
}

#[test]
fn imports_query_returns_stable_id() {
    let (query, _) = get_imports_query("project-1", "src/lib.rs");

    assert!(query.contains("m.name AS id"), "{query}");
    assert!(query.contains("m.name AS module_name"), "{query}");
}

#[test]
fn file_import_blast_radius_traverses_import_edges_undirected() {
    let (query, _) = blast_radius_file_import_query("project-1", "src/lib.rs", 2, 10);

    assert!(query.contains("-[:IMPORTS*1..2]-(m)"), "{query}");
}

#[test]
fn projection_metadata_uses_only_metadata_source_file_path() {
    let row = Row::from([
        ("provenance".to_string(), json!("EXTRACTED")),
        ("source_system".to_string(), json!("gcode")),
        ("source_file_path".to_string(), json!("src/node.rs")),
        (
            "metadata_source_file_path".to_string(),
            json!("src/edge.rs"),
        ),
    ]);

    let metadata = row_to_projection_metadata(&row).expect("metadata");

    assert_eq!(metadata.source_file_path.as_deref(), Some("src/edge.rs"));
}

#[test]
fn projection_metadata_does_not_fallback_to_node_source_file_path() {
    let row = Row::from([
        ("provenance".to_string(), json!("EXTRACTED")),
        ("source_system".to_string(), json!("gcode")),
        ("source_file_path".to_string(), json!("src/node.rs")),
    ]);

    let metadata = row_to_projection_metadata(&row).expect("metadata");

    assert_eq!(metadata.source_file_path, None);
}

#[test]
fn delete_preserves_current_symbols() {
    let current_ids = vec!["symbol-current".to_string()];
    let queries =
        delete_file_graph_queries("project-1", "src/lib.rs", &current_ids).expect("queries");

    let combined = queries
        .iter()
        .map(|query| query.cypher.as_str())
        .collect::<Vec<_>>()
        .join("\n");

    assert!(
        combined.contains(
            "MATCH (s:CodeSymbol {project: $project, file_path: $file_path})-[r:CALLS]->()"
        ),
        "{combined}"
    );
    assert!(
        combined.contains("WHERE NOT s.id IN $symbol_ids"),
        "{combined}"
    );
    assert!(
            !combined.contains(
                "MATCH (s:CodeSymbol {project: $project, file_path: $file_path})\n                DETACH DELETE s"
            ),
            "{combined}"
        );

    let stale_symbol_cleanup = queries
        .iter()
        .find(|query| query.cypher.contains("WHERE NOT s.id IN $symbol_ids"))
        .expect("stale symbol cleanup query");
    assert_eq!(
        stale_symbol_cleanup
            .params
            .get("symbol_ids")
            .map(String::as_str),
        Some("['symbol-current']")
    );
}

#[test]
fn cleanup_orphans_is_project_scoped() {
    let queries = cleanup_orphans_queries("project-1").expect("queries");
    assert_eq!(queries.len(), 3);

    for query in &queries {
        assert_eq!(
            query.params.get("project").map(String::as_str),
            Some("'project-1'")
        );
        assert!(
            query.cypher.contains("{project: $project}"),
            "{}",
            query.cypher
        );
    }

    assert!(
        queries[0]
            .cypher
            .contains("MATCH (m:CodeModule {project: $project})"),
        "{}",
        queries[0].cypher
    );
    assert!(
        queries[1]
            .cypher
            .contains("AND NOT ({project: $project})-[:CALLS]->(n)"),
        "{}",
        queries[1].cypher
    );
    assert!(
        queries[2]
            .cypher
            .contains("MATCH (s:CodeSymbol {project: $project})")
            && queries[2].cypher.contains("s.file_path IS NULL")
            && queries[2]
                .cypher
                .contains("NOT (:CodeFile {project: $project})-[:DEFINES]->(s)")
            && queries[2]
                .cypher
                .contains("NOT ({project: $project})-[:CALLS]->(s)")
            && queries[2]
                .cypher
                .contains("NOT (s)-[:CALLS]->({project: $project})"),
        "{}",
        queries[2].cypher
    );
}

#[test]
fn delete_file_node_is_project_and_path_scoped() {
    let query = delete_file_node_query("project-1", "src/lib.rs").expect("query");

    assert!(
        query
            .cypher
            .contains("MATCH (f:CodeFile {path: $file_path, project: $project})"),
        "{}",
        query.cypher
    );
    assert!(query.cypher.contains("DETACH DELETE f"), "{}", query.cypher);
    assert_eq!(
        query.params.get("project").map(String::as_str),
        Some("'project-1'")
    );
    assert_eq!(
        query.params.get("file_path").map(String::as_str),
        Some("'src/lib.rs'")
    );
}

#[test]
fn clear_project_is_project_scoped() {
    let query = clear_project_query("project-1").expect("query");

    assert!(query.cypher.contains("MATCH (n {project: $project})"));
    assert!(query.cypher.contains("n:CodeFile"));
    assert!(query.cypher.contains("n:CodeSymbol"));
    assert_eq!(
        query.params.get("project").map(String::as_str),
        Some("'project-1'")
    );
}

#[test]
fn clear_project_targets_only_code_index_labels() {
    let query = clear_project_query("project-1").expect("query");

    for code_label in [
        "n:CodeFile",
        "n:CodeSymbol",
        "n:CodeModule",
        "n:UnresolvedCallee",
        "n:ExternalSymbol",
    ] {
        assert!(query.cypher.contains(code_label), "missing {code_label}");
    }

    for memory_label in [
        "Memory",
        "MemoryNode",
        "MemoryGraph",
        "Entity",
        "Observation",
        "Relationship",
        "RELATES_TO_CODE",
    ] {
        assert!(
            !query.cypher.contains(memory_label),
            "code graph clear must not target memory label {memory_label}"
        );
    }
}

#[test]
fn clear_all_code_index_targets_only_code_index_labels() {
    let query = clear_all_code_index_query().expect("query");

    assert!(query.cypher.contains("MATCH (n)"));
    assert!(query.cypher.contains("n:CodeFile"));
    assert!(query.cypher.contains("n:CodeSymbol"));
    assert!(query.cypher.contains("n:CodeModule"));
    assert!(query.cypher.contains("n:UnresolvedCallee"));
    assert!(query.cypher.contains("n:ExternalSymbol"));
    assert!(!query.cypher.contains("config_store"));
    assert!(!query.cypher.contains("MATCH (n {project: $project})"));
    assert!(query.params.is_empty());
}
