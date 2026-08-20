use super::lifecycle::GraphLifecycleTimeouts;
use super::*;
use crate::config::{CodeVectorSettings, Context};
use crate::graph::typed_query::TypedQuery;
use crate::models::{
    CallRelation, CallTargetKind, HeritageKind, ImportRelation, InheritanceRelation,
    ProjectionProvenance, SOURCE_SYSTEM_GCODE, make_external_symbol_id, make_unresolved_callee_id,
};
use gobby_core::falkor::Row;
use serde_json::json;
use std::io::{Read, Write};
use std::net::TcpListener;
use std::thread;

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
        runtime_config_capture_degraded: false,
        indexing: gobby_core::config::IndexingConfig::default(),
        daemon_url: None,
        grant_ai: None,
        index_scope: crate::config::ProjectIndexScope::Single,
    }
}

#[test]
#[serial_test::serial(serial_db)]
fn lifecycle_post_includes_bearer() {
    let home = tempfile::tempdir().expect("create temporary Gobby home");
    std::fs::write(home.path().join("local_cli_token"), "gcode-test-token\n")
        .expect("write local CLI token");

    let home_path = home.path().to_str().expect("temporary home path is UTF-8");
    temp_env::with_vars(
        [
            ("GOBBY_HOME", Some(home_path)),
            (gobby_core::local_token::AGENT_API_TOKEN_ENV, None::<&str>),
        ],
        || {
            let listener = TcpListener::bind("127.0.0.1:0").expect("bind test HTTP listener");
            let address = listener.local_addr().expect("read listener address");
            let handle = thread::spawn(move || {
                let (mut stream, _) = listener.accept().expect("accept lifecycle request");
                let mut buffer = [0_u8; 4096];
                let size = stream.read(&mut buffer).expect("read lifecycle request");
                let request = String::from_utf8_lossy(&buffer[..size]).into_owned();
                let body = r#"{"summary":"cleared"}"#;
                write!(
                stream,
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{body}",
                body.len()
            )
            .expect("write lifecycle response");
                request
            });

            let request = GraphLifecycleRequest {
                project_id: "project-auth-test".to_string(),
                daemon_url: Some(format!("http://{address}")),
                timeouts: GraphLifecycleTimeouts::default(),
            };
            run_lifecycle_action(&request, GraphLifecycleAction::Clear, None)
                .expect("run lifecycle action");
            let raw_request = handle.join().expect("join lifecycle listener");
            assert!(raw_request.lines().any(|line| {
                line.eq_ignore_ascii_case("Authorization: Bearer gcode-test-token")
            }));
        },
    );
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
    assert!(
        shortest_symbol_path(&ctx, "symbol-1", "symbol-2", DEFAULT_SYMBOL_PATH_MAX_DEPTH)
            .expect("path degrades")
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
fn graph_write_skips_unparsed_import_sentinel_modules() {
    let imports = vec![
        ImportRelation {
            file_path: "src/lib.rs".to_string(),
            module_name: "UNPARSED:import maybe".to_string(),
        },
        ImportRelation {
            file_path: "src/lib.rs".to_string(),
            module_name: "crate::dep".to_string(),
        },
    ];

    let import_items = import_graph_items("src/lib.rs", &imports);

    assert_eq!(import_items.len(), 1);
    assert_eq!(import_items[0].target_module, "crate::dep");
}

#[test]
fn imports_query_returns_stable_id() {
    let (query, params) = get_imports_query("project-1", "src/lib.rs", "hash-local");

    assert!(query.contains("m.name AS id"), "{query}");
    assert!(query.contains("m.name AS module_name"), "{query}");
    assert!(
        query.contains("[:IMPORTS {content_hash: $content_hash}]"),
        "{query}"
    );
    assert_eq!(
        params.get("content_hash").map(String::as_str),
        Some("'hash-local'")
    );
}

#[test]
fn external_call_target_resolution_matches_id_name_or_module_member() {
    let (query, params) = resolve_external_call_target_query("project-1", "requests.get");

    assert!(
        query.contains("MATCH (target:ExternalSymbol {project: $project})"),
        "{query}"
    );
    assert!(query.contains("target.id = $input"), "{query}");
    assert!(query.contains("target.name = $input"), "{query}");
    assert!(
        query.contains("target.name = $member AND module = $module"),
        "{query}"
    );
    assert_eq!(
        params.get("project").map(String::as_str),
        Some("'project-1'")
    );
    assert_eq!(
        params.get("input").map(String::as_str),
        Some("'requests.get'")
    );
    assert_eq!(params.get("module").map(String::as_str), Some("'requests'"));
    assert_eq!(params.get("member").map(String::as_str), Some("'get'"));
}

#[test]
fn symbol_path_queries_stay_project_scoped_and_symbol_only() {
    let from_ids = vec!["source-1".to_string()];
    let (edges_query, edge_params) = symbol_callee_edges_query("project-1", &from_ids);

    assert!(
        edges_query.contains(
            "MATCH (source:CodeSymbol {project: $project})-[:CALLS]->(target:CodeSymbol {project: $project})"
        ),
        "{edges_query}"
    );
    assert!(
        edges_query.contains("source.id IN ['source-1']"),
        "{edges_query}"
    );
    assert!(
        edges_query.contains("RETURN DISTINCT source.id AS source_id, target.id AS target_id"),
        "{edges_query}"
    );
    assert_eq!(
        edge_params.get("project").map(String::as_str),
        Some("'project-1'")
    );

    let path_ids = vec!["source-1".to_string(), "target-1".to_string()];
    let (steps_query, step_params) = symbol_path_steps_query("project-1", &path_ids);
    assert!(
        steps_query.contains("MATCH (symbol:CodeSymbol {project: $project})"),
        "{steps_query}"
    );
    assert!(
        steps_query.contains("symbol.id IN ['source-1', 'target-1']"),
        "{steps_query}"
    );
    assert!(
        steps_query.contains("coalesce(symbol.file_path, '') AS file_path"),
        "{steps_query}"
    );
    assert_eq!(
        step_params.get("project").map(String::as_str),
        Some("'project-1'")
    );
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
            "MATCH (s:CodeSymbol {project: $project, file_path: $file_path})-[r:CALLS]->(n {project: $project})"
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
fn content_version_delete_is_project_path_and_hash_scoped() {
    let queries = delete_content_version_queries("project-1", "src/lib.rs", "hash-old")
        .expect("content-version delete queries");
    let combined = queries
        .iter()
        .map(|query| query.cypher.as_str())
        .collect::<Vec<_>>()
        .join("\n");

    assert_eq!(queries.len(), 10);
    assert!(combined.contains("[r:IMPORTS]"), "{combined}");
    assert!(combined.contains("[r:DEFINES]"), "{combined}");
    assert!(combined.contains("[r:CALLS]"), "{combined}");
    assert!(
        combined.contains("[r:INHERITS|EXTENDS|IMPLEMENTS]"),
        "{combined}"
    );
    assert!(
        combined.contains("file_content_hash: $content_hash"),
        "{combined}"
    );
    assert_eq!(
        combined.matches("r.content_hash = $content_hash").count(),
        9
    );
    assert!(!combined.contains("sync_token"), "{combined}");
    for query in &queries {
        assert_eq!(
            query.params.get("project").map(String::as_str),
            Some("'project-1'")
        );
        assert_eq!(
            query.params.get("file_path").map(String::as_str),
            Some("'src/lib.rs'")
        );
        assert_eq!(
            query.params.get("content_hash").map(String::as_str),
            Some("'hash-old'")
        );
    }
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
            .contains("AND NOT ({project: $project})-[:CALLS]->(n)")
            && queries[1]
                .cypher
                .contains("NOT ({project: $project})-[:INHERITS|EXTENDS|IMPLEMENTS]->(n)")
            && queries[1]
                .cypher
                .contains("NOT (n)-[:INHERITS|EXTENDS|IMPLEMENTS]->({project: $project})"),
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
                .contains("NOT (s)-[:CALLS]->({project: $project})")
            && queries[2]
                .cypher
                .contains("NOT ({project: $project})-[:INHERITS|EXTENDS|IMPLEMENTS]->(s)")
            && queries[2]
                .cypher
                .contains("NOT (s)-[:INHERITS|EXTENDS|IMPLEMENTS]->({project: $project})"),
        "{}",
        queries[2].cypher
    );
}

#[test]
fn deleted_file_cleanup_queries_are_project_scoped_and_count_file_nodes() {
    let path_queries = project_file_path_queries("project-1").expect("path queries");
    assert_eq!(path_queries.len(), 2);

    for query in &path_queries {
        assert_eq!(
            query.params.get("project").map(String::as_str),
            Some("'project-1'")
        );
        assert!(
            query.cypher.contains("{project: $project}") && query.cypher.contains(" AS path"),
            "{}",
            query.cypher
        );
    }
    assert!(
        path_queries[0].cypher.contains("MATCH (f:CodeFile")
            && path_queries[0].cypher.contains("f.path IS NOT NULL"),
        "{}",
        path_queries[0].cypher
    );
    assert!(
        path_queries[1].cypher.contains("MATCH (s:CodeSymbol")
            && path_queries[1].cypher.contains("s.file_path IS NOT NULL"),
        "{}",
        path_queries[1].cypher
    );

    let count_query =
        count_file_projection_nodes_query("project-1", "src/stale.rs").expect("count query");
    assert_eq!(
        count_query.params.get("project").map(String::as_str),
        Some("'project-1'")
    );
    assert_eq!(
        count_query.params.get("file_path").map(String::as_str),
        Some("'src/stale.rs'")
    );
    assert!(
        count_query.cypher.contains("n:CodeFile")
            && count_query.cypher.contains("n.path = $file_path")
            && count_query.cypher.contains("n:CodeSymbol")
            && count_query.cypher.contains("n.file_path = $file_path")
            && count_query.cypher.contains("count(n) AS nodes"),
        "{}",
        count_query.cypher
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

#[test]
fn global_prune_scope_discovery_query_reads_distinct_code_projects() {
    let query = project_scopes_query();

    assert!(
        query
            .cypher
            .contains("RETURN DISTINCT n.project AS project")
    );
    assert!(query.cypher.contains("n.project IS NOT NULL"));
    for code_label in [
        "n:CodeFile",
        "n:CodeSymbol",
        "n:CodeModule",
        "n:UnresolvedCallee",
        "n:ExternalSymbol",
    ] {
        assert!(query.cypher.contains(code_label), "missing {code_label}");
    }
    assert!(query.params.is_empty());
}

struct HeritageEnd<'a> {
    id: Option<&'a str>,
    name: &'a str,
    kind: CallTargetKind,
    module: Option<&'a str>,
}

fn heritage_end<'a>(
    id: Option<&'a str>,
    name: &'a str,
    kind: CallTargetKind,
    module: Option<&'a str>,
) -> HeritageEnd<'a> {
    HeritageEnd {
        id,
        name,
        kind,
        module,
    }
}

fn heritage_relation(
    source: HeritageEnd<'_>,
    target: HeritageEnd<'_>,
    heritage_kind: HeritageKind,
    line: usize,
) -> InheritanceRelation {
    InheritanceRelation {
        source_symbol_id: source.id.map(str::to_string),
        source_name: source.name.to_string(),
        source_kind: source.kind,
        source_external_module: source.module.map(str::to_string),
        target_symbol_id: target.id.map(str::to_string),
        target_name: target.name.to_string(),
        target_kind: target.kind,
        target_external_module: target.module.map(str::to_string),
        heritage_kind,
        // planned_heritage always syncs src/owner.rs / hash-1; these carriers
        // stay stale so planning cannot silently read them.
        file_path: "stale/owner.rs".to_string(),
        content_hash: "stale-hash".to_string(),
        line,
    }
}

fn planned_heritage(rows: &[InheritanceRelation]) -> Vec<TypedQuery> {
    planned_heritage_with_token(rows, "tok-1")
}

fn planned_heritage_with_token(rows: &[InheritanceRelation], sync_token: &str) -> Vec<TypedQuery> {
    super::write::plan_test_sync_file("project-1", "src/owner.rs", "hash-1", rows, sync_token)
        .expect("plan heritage sync")
}

fn query_with<'a>(queries: &'a [TypedQuery], needle: &str) -> &'a TypedQuery {
    queries
        .iter()
        .find(|query| query.cypher.contains(needle))
        .unwrap_or_else(|| panic!("missing query containing {needle}"))
}

fn combined_cypher(queries: &[TypedQuery]) -> String {
    queries
        .iter()
        .map(|query| query.cypher.as_str())
        .collect::<Vec<_>>()
        .join("\n")
}

macro_rules! assert_heritage_owner_delete {
    ($combined:expr) => {{
        let combined: &str = $combined;
        assert!(
            combined.contains("[r:INHERITS|EXTENDS|IMPLEMENTS]"),
            "heritage delete must target INHERITS|EXTENDS|IMPLEMENTS:\n{combined}"
        );
        assert!(
            combined.contains(
                "MATCH (s:CodeSymbol {project: $project, file_path: $file_path})-[r:INHERITS|EXTENDS|IMPLEMENTS]->(n {project: $project})"
            ),
            "same-file heritage must anchor on CodeSymbol {{project, file_path}}:\n{combined}"
        );
        assert!(
            combined.contains("MATCH (s:ExternalSymbol {project: $project})-[r:INHERITS|EXTENDS|IMPLEMENTS]->(n {project: $project})"),
            "terminal heritage must start from indexed ExternalSymbol {{project}}:\n{combined}"
        );
        assert!(
            combined.contains("MATCH (s:UnresolvedCallee {project: $project})-[r:INHERITS|EXTENDS|IMPLEMENTS]->(n {project: $project})"),
            "terminal heritage must start from indexed UnresolvedCallee {{project}}:\n{combined}"
        );
        assert!(
            combined.contains("MATCH (s)-[r:INHERITS]->(n)"),
            "cross-file heritage must use a per-type relationship index scan:\n{combined}"
        );
        assert!(
            combined.contains("r.source_file_path = $file_path"),
            "heritage delete must filter r.source_file_path:\n{combined}"
        );
        assert!(
            combined.contains("s.file_path IS NULL OR s.file_path <> $file_path"),
            "cross-file heritage must keep relationship ownership when the Type lives elsewhere:\n{combined}"
        );
        assert!(
            !combined.contains("r.file = $file_path"),
            "heritage delete must not filter the redundant r.file predicate:\n{combined}"
        );
        assert!(
            combined.contains("s.project = $project") && combined.contains("n.project = $project"),
            "edge-index heritage must bind project on both endpoints:\n{combined}"
        );
    }};
}

macro_rules! assert_heritage_merge_keys {
    ($query:expr, $rel:expr) => {{
        let query = $query;
        let rel = $rel;
        assert!(
            query.cypher.contains(&format!(
                "[r:{rel} {{file: row.file_path, line: row.line, content_hash: $content_hash}}]"
            )),
            "heritage MERGE must key {rel} by file, line, and content_hash:\n{}",
            query.cypher
        );
        assert!(
            query
                .params
                .get("sync_token")
                .is_some_and(|value| !value.is_empty()),
            "missing sync_token in {}",
            query.cypher
        );
        assert!(
            query
                .params
                .get("content_hash")
                .is_some_and(|value| value.contains("hash-1")),
            "missing content_hash in {}",
            query.cypher
        );
        assert!(
            !query.cypher.contains("MATCH (source:"),
            "heritage endpoints must MERGE, not MATCH:\n{}",
            query.cypher
        );
        assert!(
            !query.cypher.contains("MATCH (target:"),
            "heritage endpoints must MERGE, not MATCH:\n{}",
            query.cypher
        );
    }};
}

#[test]
fn plan_sync_batches_merges_inheritance_with_content_hash_and_sync_token() {
    let rows = [heritage_relation(
        heritage_end(Some("derived-id"), "Derived", CallTargetKind::Symbol, None),
        heritage_end(Some("base-id"), "Base", CallTargetKind::Symbol, None),
        HeritageKind::Extends,
        4,
    )];
    let queries = planned_heritage(&rows);
    let query = query_with(&queries, "EXTENDS");
    assert_heritage_merge_keys!(query, "EXTENDS");
}

#[test]
fn heritage_merge_recovers_when_owner_syncs_before_provider() {
    let rows = [
        heritage_relation(
            heritage_end(Some("derived-id"), "Derived", CallTargetKind::Symbol, None),
            heritage_end(Some("base-id"), "Base", CallTargetKind::Symbol, None),
            HeritageKind::Extends,
            4,
        ),
        heritage_relation(
            heritage_end(Some("type-id"), "ExtType", CallTargetKind::Symbol, None),
            heritage_end(Some("trait-id"), "LocalTrait", CallTargetKind::Symbol, None),
            HeritageKind::Implements,
            8,
        ),
    ];
    let queries = planned_heritage(&rows);
    for (needle, rel) in [("EXTENDS", "EXTENDS"), ("IMPLEMENTS", "IMPLEMENTS")] {
        let query = query_with(&queries, needle);
        assert!(
            query
                .cypher
                .contains("MERGE (source:CodeSymbol {id: row.source_id, project: $project})")
        );
        assert!(
            query
                .cypher
                .contains("ON CREATE SET source.name = row.source_name")
        );
        assert!(
            query
                .cypher
                .contains("MERGE (target:CodeSymbol {id: row.target_id, project: $project})")
        );
        assert!(
            query
                .cypher
                .contains("ON CREATE SET target.name = row.target_name")
        );
        assert_heritage_merge_keys!(query, rel);
    }
}

#[test]
fn heritage_merge_keeps_parallel_same_type_facts() {
    let rows = [
        heritage_relation(
            heritage_end(Some("derived-id"), "Derived", CallTargetKind::Symbol, None),
            heritage_end(Some("base-id"), "Base", CallTargetKind::Symbol, None),
            HeritageKind::Extends,
            10,
        ),
        heritage_relation(
            heritage_end(Some("derived-id"), "Derived", CallTargetKind::Symbol, None),
            heritage_end(Some("base-id"), "Base", CallTargetKind::Symbol, None),
            HeritageKind::Extends,
            20,
        ),
    ];
    let first = planned_heritage_with_token(&rows, "tok-1");
    let second = planned_heritage_with_token(&rows, "tok-2");
    assert_ne!(
        first
            .iter()
            .find_map(|query| query.params.get("sync_token").cloned()),
        second
            .iter()
            .find_map(|query| query.params.get("sync_token").cloned()),
        "idempotency comparison must use distinct sync tokens"
    );
    for queries in [&first, &second] {
        let query = query_with(queries, "EXTENDS");
        let rows_param = query.params.get("rows").expect("heritage UNWIND rows");
        assert!(
            rows_param.contains("line: 10") && rows_param.contains("line: 20"),
            "parallel same-type facts must remain two relationships: {rows_param}"
        );
        assert_heritage_merge_keys!(query, "EXTENDS");
    }
}

#[test]
fn heritage_merge_external_and_unresolved_sources() {
    let rows = [
        heritage_relation(
            heritage_end(
                None,
                "ExternalType",
                CallTargetKind::External,
                Some("external_crate"),
            ),
            heritage_end(Some("trait-id"), "LocalTrait", CallTargetKind::Symbol, None),
            HeritageKind::Implements,
            3,
        ),
        heritage_relation(
            heritage_end(None, "MissingType", CallTargetKind::Unresolved, None),
            heritage_end(Some("trait-id"), "LocalTrait", CallTargetKind::Symbol, None),
            HeritageKind::Implements,
            5,
        ),
    ];
    let queries = planned_heritage(&rows);
    let external = query_with(&queries, "ExternalSymbol");
    assert!(
        external
            .cypher
            .contains("MERGE (source:ExternalSymbol {id: row.source_id, project: $project})")
    );
    assert!(
        external
            .cypher
            .contains("MERGE (target:CodeSymbol {id: row.target_id, project: $project})")
    );
    assert_heritage_merge_keys!(external, "IMPLEMENTS");
    let unresolved = query_with(&queries, "UnresolvedCallee");
    assert!(
        unresolved
            .cypher
            .contains("MERGE (source:UnresolvedCallee {id: row.source_id, project: $project})")
    );
    assert_heritage_merge_keys!(unresolved, "IMPLEMENTS");
    let external_id = make_external_symbol_id("project-1", "ExternalType", Some("external_crate"));
    let unresolved_id = make_unresolved_callee_id("project-1", "MissingType");
    assert!(
        external
            .params
            .get("rows")
            .is_some_and(|rows| rows.contains(&external_id)),
        "external source id missing from {:?}",
        external.params.get("rows")
    );
    assert!(
        unresolved
            .params
            .get("rows")
            .is_some_and(|rows| rows.contains(&unresolved_id)),
        "unresolved source id missing from {:?}",
        unresolved.params.get("rows")
    );
}

#[test]
fn heritage_merge_external_and_unresolved_targets() {
    let rows = [
        heritage_relation(
            heritage_end(Some("derived-id"), "Derived", CallTargetKind::Symbol, None),
            heritage_end(None, "Display", CallTargetKind::External, Some("std::fmt")),
            HeritageKind::Implements,
            2,
        ),
        heritage_relation(
            heritage_end(Some("derived-id"), "Derived", CallTargetKind::Symbol, None),
            heritage_end(None, "UnknownBase", CallTargetKind::Unresolved, None),
            HeritageKind::Extends,
            3,
        ),
    ];
    let queries = planned_heritage(&rows);
    let external = query_with(&queries, "ExternalSymbol");
    assert!(
        external
            .cypher
            .contains("MERGE (source:CodeSymbol {id: row.source_id, project: $project})")
    );
    assert!(
        external
            .cypher
            .contains("MERGE (target:ExternalSymbol {id: row.target_id, project: $project})")
    );
    assert_heritage_merge_keys!(external, "IMPLEMENTS");
    let unresolved = query_with(&queries, "[r:EXTENDS");
    assert!(
        unresolved
            .cypher
            .contains("MERGE (target:UnresolvedCallee {id: row.target_id, project: $project})")
    );
    assert_heritage_merge_keys!(unresolved, "EXTENDS");
    let external_id = make_external_symbol_id("project-1", "Display", Some("std::fmt"));
    let unresolved_id = make_unresolved_callee_id("project-1", "UnknownBase");
    assert!(
        external.params.get("rows").is_some_and(|rows| {
            rows.contains(&external_id) && rows.contains("file_path: 'src/owner.rs'")
        }),
        "external target rows missing owner file or id: {:?}",
        external.params.get("rows")
    );
    assert!(
        unresolved
            .params
            .get("rows")
            .is_some_and(|rows| rows.contains(&unresolved_id) && rows.contains("line: 3")),
        "unresolved target rows missing: {:?}",
        unresolved.params.get("rows")
    );
}

#[test]
fn heritage_local_import_is_not_projected() {
    let rows = [heritage_relation(
        heritage_end(Some("derived-id"), "Derived", CallTargetKind::Symbol, None),
        heritage_end(
            None,
            "Helper",
            CallTargetKind::LocalImport,
            Some("pkg/helper.py"),
        ),
        HeritageKind::Inherits,
        9,
    )];
    let queries = planned_heritage(&rows);
    assert!(
        queries.iter().all(|query| {
            !query.cypher.contains("INHERITS")
                && !query.cypher.contains("EXTENDS")
                && !query.cypher.contains("IMPLEMENTS")
        }),
        "LocalImport heritage must wait for promotion"
    );
}

#[test]
fn sync_graph_file_projects_inheritance_facts() {
    let inheritance = heritage_relation(
        heritage_end(Some("derived-id"), "Derived", CallTargetKind::Symbol, None),
        heritage_end(Some("base-id"), "Base", CallTargetKind::Symbol, None),
        HeritageKind::Extends,
        4,
    );
    let facts = crate::db::GraphFileFacts {
        file_path: "src/owner.rs".to_string(),
        content_hash: "hash-1".to_string(),
        imports: Vec::new(),
        definitions: Vec::new(),
        calls: Vec::new(),
        inheritance: vec![inheritance.clone()],
    };
    assert_eq!(facts.inheritance.len(), 1);

    fn assert_active_imports_reader<C: postgres::GenericClient>(
        _reader: fn(&mut C, &str) -> anyhow::Result<Vec<crate::models::ImportRelation>>,
    ) {
    }
    assert_active_imports_reader::<postgres::Client>(crate::db::read_active_imports);

    let queries = planned_heritage(&facts.inheritance);
    let query = query_with(&queries, "EXTENDS");
    assert_heritage_merge_keys!(query, "EXTENDS");

    let mut outcome = crate::index::indexer::IndexOutcome {
        project_id: "project-1".to_string(),
        indexed_file_paths: vec!["src/owner.rs".to_string()],
        ..crate::index::indexer::IndexOutcome::default()
    };
    outcome.record_promotion_owners(["src/derived.rs".to_string()]);
    assert_eq!(
        outcome.graph_file_paths,
        vec!["src/owner.rs".to_string(), "src/derived.rs".to_string(),]
    );
    assert_eq!(outcome.vector_file_paths, vec!["src/owner.rs".to_string()]);
}

#[test]
fn delete_queries_include_inheritance_rels() {
    let file = combined_cypher(
        &delete_file_graph_queries("project-1", "src/impl.rs", &[]).expect("file delete"),
    );
    let stale = combined_cypher(
        &delete_stale_file_graph_queries("project-1", "src/impl.rs", "hash-1", "tok-1")
            .expect("stale delete"),
    );
    let content = combined_cypher(
        &delete_content_version_queries("project-1", "src/impl.rs", "hash-old")
            .expect("content-version delete"),
    );
    assert_heritage_owner_delete!(&file);
    assert_heritage_owner_delete!(&stale);
    assert_heritage_owner_delete!(&content);
}

#[test]
fn rebuild_projects_promoted_inheritance_edge() {
    let rows = [heritage_relation(
        heritage_end(Some("derived-id"), "Derived", CallTargetKind::Symbol, None),
        heritage_end(Some("base-id"), "Base", CallTargetKind::Symbol, None),
        HeritageKind::Extends,
        4,
    )];
    let queries = planned_heritage(&rows);
    let query = query_with(&queries, "EXTENDS");
    assert!(
        query
            .cypher
            .contains("MERGE (source:CodeSymbol {id: row.source_id, project: $project})")
    );
    assert!(
        query
            .cypher
            .contains("MERGE (target:CodeSymbol {id: row.target_id, project: $project})")
    );
    assert_heritage_merge_keys!(query, "EXTENDS");
}

#[test]
fn rebuild_drops_stale_inheritance_after_derived_reindex() {
    let queries =
        delete_stale_file_graph_queries("project-1", "src/derived.py", "hash-new", "tok-2")
            .expect("stale heritage delete");
    let combined = combined_cypher(&queries);
    assert_heritage_owner_delete!(&combined);
    let heritage = query_with(&queries, "[r:INHERITS|EXTENDS|IMPLEMENTS]");
    assert!(
        heritage.cypher.contains("r.content_hash = $content_hash"),
        "{}",
        heritage.cypher
    );
    assert!(
        heritage
            .cypher
            .contains("r.sync_token IS NULL OR r.sync_token <> $sync_token"),
        "stale heritage delete must drop missing and mismatched tokens:\n{}",
        heritage.cypher
    );
}

#[test]
fn cleanup_keeps_heritage_only_terminals() {
    let queries = cleanup_orphans_queries("project-1").expect("cleanup");
    let external = &queries[1].cypher;
    let detached = &queries[2].cypher;
    assert!(
        external.contains("n:UnresolvedCallee OR n:ExternalSymbol")
            && external.contains("NOT ({project: $project})-[:INHERITS|EXTENDS|IMPLEMENTS]->(n)")
            && external.contains("NOT (n)-[:INHERITS|EXTENDS|IMPLEMENTS]->({project: $project})"),
        "{external}"
    );
    assert!(
        detached.contains("MATCH (s:CodeSymbol {project: $project})")
            && detached.contains("NOT ({project: $project})-[:INHERITS|EXTENDS|IMPLEMENTS]->(s)")
            && detached.contains("NOT (s)-[:INHERITS|EXTENDS|IMPLEMENTS]->({project: $project})"),
        "{detached}"
    );
}

#[test]
fn delete_cross_file_rust_impl_uses_relationship_owner() {
    let combined = combined_cypher(
        &delete_file_graph_queries("project-1", "src/impl.rs", &["type-id".to_string()])
            .expect("impl-file delete"),
    );
    assert_heritage_owner_delete!(&combined);
    assert!(
        combined.contains("s.file_path IS NULL OR s.file_path <> $file_path"),
        "cross-file impl ownership lives on the relationship, not the Type symbol file:\n{combined}"
    );
}

#[test]
fn promotion_projects_owning_file_without_rebuild() {
    let promotion = include_str!("../../index/indexer/local_imports.rs");
    let types = include_str!("../../index/indexer/types.rs");
    let index = include_str!("../../commands/index.rs");
    let projection = include_str!("../../projection/sync.rs");
    assert!(
        promotion.contains("dirty_graph_sync_for_file")
            && promotion.contains("&original.file_path"),
        "promotion must dirty the owning derived/impl file"
    );
    assert!(
        types.contains("record_promotion_owners") && types.contains("graph_file_paths"),
        "promotion owners must join the graph sync set"
    );
    assert!(
        index.contains("&outcome.graph_file_paths"),
        "index must graph-sync promoted owners without a rebuild"
    );
    assert!(
        !promotion.contains("rebuild_project_graph") && !promotion.contains("graph rebuild"),
        "provider-later promotion must not require gcode graph rebuild"
    );
    let projection_sync = projection
        .split("fn sync_graph_file(")
        .nth(1)
        .and_then(|rest| rest.split("\nstruct ").next())
        .expect("projection/sync.rs::sync_graph_file");
    assert!(
        projection_sync.contains("&facts.inheritance"),
        "graph-syncing the owning file must project the promoted heritage edge"
    );
}

#[test]
fn delete_external_source_impl_uses_relationship_owner() {
    let combined = combined_cypher(
        &delete_file_graph_queries("project-1", "src/impl.rs", &[])
            .expect("external-source delete"),
    );
    assert_heritage_owner_delete!(&combined);
    let cleanup = combined_cypher(&cleanup_orphans_queries("project-1").expect("cleanup"));
    assert!(
        cleanup.contains("NOT (n)-[:INHERITS|EXTENDS|IMPLEMENTS]->({project: $project})"),
        "heritage-only ExternalSymbol/UnresolvedCallee sources must survive cleanup:\n{cleanup}"
    );
}

#[test]
fn heritage_delete_binds_project_on_both_endpoints() {
    for combined in [
        combined_cypher(&delete_file_graph_queries("project-1", "src/lib.rs", &[]).expect("file")),
        combined_cypher(
            &delete_stale_file_graph_queries("project-1", "src/lib.rs", "hash-1", "tok-1")
                .expect("stale"),
        ),
        combined_cypher(
            &delete_content_version_queries("project-1", "src/lib.rs", "hash-old")
                .expect("content"),
        ),
    ] {
        assert_heritage_owner_delete!(&combined);
        let heritage = combined
            .lines()
            .filter(|line| line.contains("INHERITS|EXTENDS|IMPLEMENTS"))
            .collect::<Vec<_>>()
            .join("\n");
        assert!(
            heritage.contains("{project: $project}")
                && (heritage.matches("{project: $project}").count() >= 2
                    || (heritage.contains("s.project = $project")
                        && heritage.contains("n.project = $project"))),
            "same-path heritage in another project must not be deleted:\n{heritage}"
        );
    }
}

fn merge_node_property_keys(cypher: &str) -> Vec<(String, Vec<String>)> {
    let mut out = Vec::new();
    let mut rest = cypher;
    while let Some(start) = rest.find("MERGE (") {
        let after = &rest[start + "MERGE (".len()..];
        let Some(colon) = after.find(':') else {
            break;
        };
        let alias = &after[..colon];
        if !alias
            .chars()
            .all(|character| character.is_ascii_alphanumeric() || character == '_')
        {
            rest = &after[colon + 1..];
            continue;
        }
        let after_colon = &after[colon + 1..];
        let Some(brace) = after_colon.find('{') else {
            rest = after_colon;
            continue;
        };
        let label = after_colon[..brace].trim();
        if !label
            .chars()
            .all(|character| character.is_ascii_alphanumeric() || character == '_')
        {
            rest = &after_colon[brace + 1..];
            continue;
        }
        let after_brace = &after_colon[brace + 1..];
        let Some(end) = after_brace.find('}') else {
            break;
        };
        let keys = after_brace[..end]
            .split(',')
            .filter_map(|part| {
                let key = part.split(':').next()?.trim();
                (!key.is_empty()).then(|| key.to_string())
            })
            .collect();
        out.push((label.to_string(), keys));
        rest = &after_brace[end + 1..];
    }
    out
}

#[test]
fn project_indexes_cover_every_merge_key() {
    let indexed = PROJECT_INDEXED_PROPERTIES
        .iter()
        .map(|(label, properties)| {
            (
                *label,
                properties
                    .iter()
                    .copied()
                    .collect::<std::collections::BTreeSet<_>>(),
            )
        })
        .collect::<std::collections::BTreeMap<_, _>>();
    for (label, keys) in merge_node_property_keys(include_str!("write/mutation.rs")) {
        let Some(indexed_keys) = indexed.get(label.as_str()) else {
            panic!("MERGE label {label} is missing from PROJECT_INDEXED_PROPERTIES");
        };
        for key in keys {
            assert!(
                indexed_keys.contains(key.as_str()),
                "MERGE :{label}({key}) is not indexed"
            );
        }
    }
}

#[test]
fn stale_sweeps_anchor_on_indexed_file_path() {
    let combined = combined_cypher(
        &delete_stale_file_graph_queries("project-1", "src/lib.rs", "hash-1", "tok-1")
            .expect("stale delete"),
    );
    assert!(
        combined.contains(
            "MATCH (s:CodeSymbol {project: $project, file_path: $file_path})-[r:CALLS]->(n {project: $project})"
        ),
        "stale CALLS must start from CodeSymbol {{project, file_path}}:\n{combined}"
    );
    assert!(
        !combined.contains("CodeSymbol {project: $project})-["),
        "no stale relationship sweep may match CodeSymbol by project alone:\n{combined}"
    );
}

#[test]
fn file_delete_sweeps_anchor_on_indexed_file_path() {
    let combined = combined_cypher(
        &delete_file_graph_queries("project-1", "src/lib.rs", &[]).expect("file delete"),
    );
    assert!(
        combined.contains(
            "MATCH (s:CodeSymbol {project: $project, file_path: $file_path})-[r:CALLS]->(n {project: $project})"
        ),
        "file-delete CALLS must start from CodeSymbol {{project, file_path}}:\n{combined}"
    );
    assert!(
        !combined.contains("CodeSymbol {project: $project})-["),
        "no file-delete relationship sweep may match CodeSymbol by project alone:\n{combined}"
    );
}

#[test]
fn mutation_edges_keep_file_and_source_file_path_in_parity() {
    let source = include_str!("write/mutation.rs");
    for block in source.split("const ADD_") {
        if !block.contains("{file:") {
            continue;
        }
        assert!(
            block.contains("r.source_file_path ="),
            "a mutation that sets r.file must also set r.source_file_path:\n{block}"
        );
        if block.contains("file: call.file_path") {
            assert!(
                block.contains("r.source_file_path = call.file_path"),
                "CALLS r.file and r.source_file_path must share call.file_path:\n{block}"
            );
        }
        if block.contains("file: row.file_path") {
            assert!(
                block.contains("r.source_file_path = row.file_path"),
                "heritage r.file and r.source_file_path must share row.file_path:\n{block}"
            );
        }
    }
}
