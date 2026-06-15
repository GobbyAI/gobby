use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

use gobby_core::degradation::DegradationKind;

use crate::graph::{
    self, MENTIONS_TARGET_REL, SUPPORTS_REL, WIKI_DOC_LABEL, WIKI_LINKS_TO_REL, WIKI_SOURCE_LABEL,
    WIKI_TARGET_LABEL, WikiGraphDocument, WikiGraphFacts, WikiGraphLink, WikiGraphLinkTarget,
    WikiGraphSource, graph_write_statements,
};
use crate::search::SearchScope;

use super::boost::partial_graph_degradation;
use super::code_edges::{
    code_call_edges_query, code_doc_source_path, code_edge_query_params, code_import_edges_query,
    record_code_edge_truncation, remaining_code_edge_limit, truncate_to_limit,
    truncation_component,
};
use super::query::scope_params;
use super::wiki_facts::{resolve_graph_target, slug_target_map};
use super::{
    CODE_CALL_EDGE_TRUNCATION_COMPONENT, CODE_IMPORT_EDGE_TRUNCATION_COMPONENT,
    CODE_TOTAL_EDGE_TRUNCATION_COMPONENT, FALKORDB_GRAPH_NAME, MAX_TOTAL_CODE_EDGES,
};

#[test]
fn falkordb_graph_name_is_wiki_owned() {
    assert_eq!(FALKORDB_GRAPH_NAME, "gobby_wiki");
    assert_ne!(FALKORDB_GRAPH_NAME, "gobby_code");
}

#[test]
fn graph_resolution_keeps_unresolved_targets_and_skips_external() {
    let scope = SearchScope::topic("rust");
    let documents = vec![WikiGraphDocument {
        scope: scope.clone(),
        path: PathBuf::from("knowledge/topics/rust.md"),
        title: Some("Rust Async".to_string()),
    }];
    let document_paths = documents
        .iter()
        .map(|document| document.path.clone())
        .collect::<BTreeSet<_>>();
    let slug_targets = slug_target_map(&documents);
    assert_eq!(
        resolve_graph_target(
            "Rust Async",
            Path::new("knowledge/topics/source.md"),
            &document_paths,
            &slug_targets
        ),
        Some(graph::WikiGraphLinkTarget::Resolved(PathBuf::from(
            "knowledge/topics/rust.md"
        )))
    );
    assert_eq!(
        resolve_graph_target(
            "Missing Page",
            Path::new("knowledge/topics/source.md"),
            &document_paths,
            &slug_targets
        ),
        Some(graph::WikiGraphLinkTarget::Unresolved(
            "Missing Page".to_string()
        ))
    );
    assert!(
        resolve_graph_target(
            "https://example.test",
            Path::new("knowledge/topics/source.md"),
            &document_paths,
            &slug_targets
        )
        .is_none()
    );
}

#[test]
fn graph_scope_params_are_cypher_string_literals() {
    let params = scope_params(&SearchScope::topic("rust'async")).expect("scoped params");

    assert_eq!(
        params.get("scope_kind").map(String::as_str),
        Some("'topic'")
    );
    assert_eq!(
        params.get("scope_id").map(String::as_str),
        Some("'rust\\'async'")
    );
}

#[test]
fn graph_scope_params_omit_global_scope_filters() {
    assert!(scope_params(&SearchScope::global()).is_none());
}

#[test]
fn ask_unified_graph_code_doc_source_path_maps_to_code_file() {
    assert_eq!(
        code_doc_source_path(Path::new("code/files/crates/gwiki/src/lib.rs.md")),
        Some("crates/gwiki/src/lib.rs".to_string())
    );
    assert_eq!(code_doc_source_path(Path::new("wiki/notes.md")), None);
}

#[test]
fn cypher_string_literal_escapes_like_gcode() {
    assert_eq!(
        cypher_string_literal("a\"b\\c'd\n\r\t\u{0008}\u{000C}\u{001F}"),
        "'a\\\"b\\\\c\\'d\\n\\r\\t\\b\\f\\u001F'"
    );
}

#[test]
fn partial_graph_degradation_reports_capped_components() {
    let degradation =
        partial_graph_degradation(&["documents>10".to_string(), "links>20".to_string()])
            .expect("partial data degradation");

    let DegradationKind::PartialData { component, message } = degradation else {
        panic!("expected partial data degradation");
    };
    assert_eq!(component, "gwiki_graph");
    assert!(message.contains("documents>10"));
    assert!(message.contains("links>20"));
}

#[test]
fn code_edge_query_params_use_sentinel_limit_and_parameterized_queries() {
    let call_query = code_call_edges_query();
    let import_query = code_import_edges_query();

    assert!(call_query.contains("LIMIT $limit"));
    assert!(import_query.contains("LIMIT $limit"));
    assert!(
        call_query.contains(
            "ORDER BY source.file_path, source.name, target.file_path, target.name, r.line"
        )
    );
    assert!(import_query.contains("ORDER BY file.path, module.name"));
    assert!(!call_query.contains("LIMIT 200"));
    assert!(!import_query.contains("LIMIT 200"));

    let params = code_edge_query_params("project-1", "src/lib.rs", 7).expect("params");

    assert_eq!(params.get("limit").map(String::as_str), Some("8"));
}

#[test]
fn truncation_components_name_capped_call_and_import_queries() {
    assert_eq!(
        truncation_component(CODE_CALL_EDGE_TRUNCATION_COMPONENT, 7),
        "code_call_edges>7"
    );
    assert_eq!(
        truncation_component(CODE_IMPORT_EDGE_TRUNCATION_COMPONENT, 9),
        "code_import_edges>9"
    );
}

#[test]
fn code_edge_query_limit_respects_total_remaining_cap() {
    assert_eq!(
        remaining_code_edge_limit(200, MAX_TOTAL_CODE_EDGES),
        Some(200)
    );
    assert_eq!(remaining_code_edge_limit(200, 17), Some(17));
    assert_eq!(remaining_code_edge_limit(200, 0), None);

    let mut components = BTreeSet::new();
    record_code_edge_truncation(
        &mut components,
        CODE_CALL_EDGE_TRUNCATION_COMPONENT,
        200,
        17,
    );
    assert!(components.contains(&truncation_component(
        CODE_TOTAL_EDGE_TRUNCATION_COMPONENT,
        MAX_TOTAL_CODE_EDGES
    )));
}

#[test]
fn truncate_to_limit_handles_sentinel_rows_and_zero_limit() {
    let mut rows = vec![1, 2, 3];
    assert!(truncate_to_limit(&mut rows, 2));
    assert_eq!(rows, vec![1, 2]);

    let mut rows = vec![1];
    assert!(truncate_to_limit(&mut rows, 0));
    assert!(rows.is_empty());

    let mut rows = vec![1, 2];
    assert!(!truncate_to_limit(&mut rows, 2));
    assert_eq!(rows, vec![1, 2]);
}

#[test]
fn graph_write_uses_wiki_labels_and_relationships() {
    let facts = WikiGraphFacts {
        documents: vec![
            WikiGraphDocument {
                scope: SearchScope::topic("rust"),
                path: PathBuf::from("wiki/a.md"),
                title: Some("A".to_string()),
            },
            WikiGraphDocument {
                scope: SearchScope::topic("rust"),
                path: PathBuf::from("wiki/b.md"),
                title: Some("B".to_string()),
            },
        ],
        links: vec![
            WikiGraphLink {
                scope: SearchScope::topic("rust"),
                source_path: PathBuf::from("wiki/a.md"),
                raw_target: "wiki/b.md".to_string(),
                target: WikiGraphLinkTarget::Resolved(PathBuf::from("wiki/b.md")),
            },
            WikiGraphLink {
                scope: SearchScope::topic("rust"),
                source_path: PathBuf::from("wiki/a.md"),
                raw_target: "Missing Page".to_string(),
                target: WikiGraphLinkTarget::Unresolved("Missing Page".to_string()),
            },
        ],
        sources: vec![WikiGraphSource {
            scope: SearchScope::topic("rust"),
            source_path: PathBuf::from("raw/a.md"),
            document_path: PathBuf::from("wiki/a.md"),
        }],
        code_edges: Vec::new(),
    };
    let joined = graph_write_statements(&facts)
        .into_iter()
        .map(|statement| statement.cypher)
        .collect::<Vec<_>>()
        .join("\n");
    for token in [
        WIKI_DOC_LABEL,
        WIKI_SOURCE_LABEL,
        WIKI_TARGET_LABEL,
        WIKI_LINKS_TO_REL,
        MENTIONS_TARGET_REL,
        SUPPORTS_REL,
    ] {
        assert!(joined.contains(token), "{token} missing from {joined}");
    }
    assert!(!joined.contains("CodeSymbol"));
    assert!(!joined.contains("gobby_code"));
}

fn cypher_string_literal(value: &str) -> String {
    format!("'{}'", escape_string_contents(value))
}

fn escape_string_contents(value: &str) -> String {
    let mut escaped = String::with_capacity(value.len());
    for ch in value.chars() {
        match ch {
            '\\' => escaped.push_str(r"\\"),
            '\'' => escaped.push_str(r"\'"),
            '"' => escaped.push_str("\\\""),
            '\n' => escaped.push_str(r"\n"),
            '\r' => escaped.push_str(r"\r"),
            '\t' => escaped.push_str(r"\t"),
            '\u{0008}' => escaped.push_str(r"\b"),
            '\u{000C}' => escaped.push_str(r"\f"),
            ch if ch.is_control() => escaped.push_str(&format!(r"\u{:04X}", ch as u32)),
            ch => escaped.push(ch),
        }
    }
    escaped
}
