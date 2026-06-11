use super::support::*;
use super::*;

#[test]
fn clusters_modules_from_graph() {
    let input = CodewikiInput {
        files: vec![
            "src/api/handler.rs".to_string(),
            "src/domain/service.rs".to_string(),
            "tests/domain/service_test.rs".to_string(),
            "vendor/generated/client.rs".to_string(),
        ],
        graph_edges: vec![CodewikiGraphEdge::call(
            test_component_id("src/api/handler.rs", "handle", "function"),
            test_component_id("src/domain/service.rs", "Service", "class"),
        )],
        graph_availability: CodewikiGraphAvailability::Available,
        symbols: vec![
            test_symbol(
                "src/api/handler.rs",
                "handle",
                "function",
                1,
                "pub fn handle()",
            ),
            test_symbol(
                "src/domain/service.rs",
                "Service",
                "class",
                1,
                "pub struct Service;",
            ),
            test_symbol_with_qualified(
                "src/domain/service.rs",
                "new",
                "Service::new",
                "function",
                3,
                "pub fn new() -> Self",
            ),
            test_symbol(
                "tests/domain/service_test.rs",
                "service_test",
                "function",
                1,
                "fn service_test()",
            ),
            test_symbol(
                "vendor/generated/client.rs",
                "GeneratedClient",
                "class",
                1,
                "pub struct GeneratedClient;",
            ),
        ],
    };

    let docs = generate_hierarchical_docs(&input, None);
    let docs_by_path = docs.into_iter().collect::<BTreeMap<_, _>>();

    let module = docs_by_path
        .get("code/modules/src.md")
        .expect("graph-connected files cluster under common module");
    assert!(module.contains("[[code/files/src/api/handler.rs|src/api/handler.rs]]"));
    assert!(module.contains("[[code/files/src/domain/service.rs|src/domain/service.rs]]"));
    assert!(module.contains(&test_component_id(
        "src/api/handler.rs",
        "handle",
        "function"
    )));
    assert!(module.contains(&test_component_id(
        "src/domain/service.rs",
        "Service",
        "class"
    )));
    assert!(!docs_by_path.contains_key("code/files/tests/domain/service_test.rs.md"));
    assert!(!docs_by_path.contains_key("code/files/vendor/generated/client.rs.md"));
}

#[test]
fn file_root_detection_breaks_parent_cycles() {
    let mut parents = HashMap::from([
        ("c.rs".to_string(), "b.rs".to_string()),
        ("b.rs".to_string(), "a.rs".to_string()),
        ("a.rs".to_string(), "b.rs".to_string()),
    ]);

    let root = find_file_root(&mut parents, "c.rs");

    assert_eq!(root, "a.rs");
    assert_eq!(parents.get("a.rs").map(String::as_str), Some("a.rs"));
    assert_eq!(parents.get("b.rs").map(String::as_str), Some("a.rs"));
    assert_eq!(parents.get("c.rs").map(String::as_str), Some("a.rs"));
}

#[test]
fn common_module_for_empty_files_is_root() {
    assert_eq!(common_module_for_files(&[]), "");
}

#[test]
fn module_depth_counts_only_non_empty_segments() {
    assert_eq!(module_depth(""), 0);
    assert_eq!(module_depth("/"), 0);
    assert_eq!(module_depth("src"), 1);
    assert_eq!(module_depth("src/commands/"), 2);
}

#[test]
fn core_file_filter_excludes_specs_mocks_and_test_prefixes() {
    for file in [
        "src/test_parser.rs",
        "src/parser_spec.rs",
        "src/parser.spec.rs",
        "src/__mocks__/client.rs",
        "src/mocks/client.rs",
    ] {
        assert!(!is_core_file(file), "{file} should be filtered out");
    }

    assert!(is_core_file("src/parser.rs"));
}

#[test]
fn core_file_filter_excludes_hidden_metadata_paths() {
    for file in [
        ".gobby/wiki/code/files/crates/gcode/src/cli.rs.md",
        ".gobby/plans/goal.md",
        ".github/workflows/ci.yml",
        ".claude/settings.json",
        ".gitignore",
    ] {
        assert!(!is_core_file(file), "{file} should be filtered out");
    }

    assert!(is_core_file("docs/guides/codewiki.md"));
}

#[test]
fn import_targets_match_exact_path_or_module_components() {
    let files = vec![
        "src/domain/service.rs".to_string(),
        "src/domain/service_extra.rs".to_string(),
        "src/domain_extra/service.rs".to_string(),
        "crates/app/src/domain/mod.rs".to_string(),
        "crates/app/src/application/use_case.rs".to_string(),
    ];

    assert_eq!(
        files_for_import_target(&files, "domain.service"),
        vec!["src/domain/service.rs"]
    );
    assert_eq!(
        files_for_import_target(&files, "domain"),
        vec![
            "src/domain/service.rs",
            "src/domain/service_extra.rs",
            "crates/app/src/domain/mod.rs"
        ]
    );
    assert!(files_for_import_target(&files, "main.service").is_empty());
}

#[test]
fn mermaid_labels_escape_label_metacharacters() {
    let files = vec![
        FileDoc {
            path: "src/api.rs".to_string(),
            module: "src/api[edge]".to_string(),
            summary: String::new(),
            source_spans: Vec::new(),
            symbols: Vec::new(),
            component_ids: vec!["api".to_string()],
            degraded: false,
            reused_page: None,
        },
        FileDoc {
            path: "src/domain.rs".to_string(),
            module: "src/domain{core}|v1".to_string(),
            summary: String::new(),
            source_spans: Vec::new(),
            symbols: Vec::new(),
            component_ids: vec!["domain".to_string()],
            degraded: false,
            reused_page: None,
        },
    ];
    let graph = vec![CodewikiGraphEdge::import("api", "domain")];

    let diagram = render_module_dependency_mermaid("src/api[edge]", &files, &graph)
        .expect("dependency diagram");

    assert!(diagram.contains("src/api&#91;edge&#93;"));
    assert!(diagram.contains("src/domain&#123;core&#125;&#124;v1"));
    assert!(!diagram.contains("src/api[edge]"));
}

#[test]
fn graph_queries_use_requested_edge_limit() {
    let (call_query, _) = codewiki_call_edges_query("project-1", 17);
    let (import_query, _) = codewiki_import_edges_query("project-1", 17);

    assert!(call_query.contains("LIMIT 17"));
    assert!(import_query.contains("LIMIT 17"));
}

#[test]
fn import_edges_drop_non_core_source_files() {
    let file_symbols = BTreeMap::from([
        ("src/api.rs".to_string(), vec!["comp-api".to_string()]),
        ("src/domain.rs".to_string(), vec!["comp-domain".to_string()]),
        (
            "tests/api_test.rs".to_string(),
            vec!["comp-test".to_string()],
        ),
    ]);
    let core_files = vec!["src/api.rs".to_string(), "src/domain.rs".to_string()];
    let pairs = vec![
        ("tests/api_test.rs".to_string(), "domain".to_string()),
        ("src/api.rs".to_string(), "domain".to_string()),
    ];

    let edges = import_edges_from_pairs(&pairs, &core_files, &file_symbols);

    assert_eq!(
        edges,
        vec![CodewikiGraphEdge::import("comp-api", "comp-domain")]
    );
}

#[test]
fn graph_queries_stay_small_and_carry_no_id_lists() {
    // Embedding the core symbol-id/file lists in the Cypher text produced
    // ~633KB payloads on this repo, which intermittently failed at the socket
    // layer; core filtering is client-side now.
    let (call_query, _) = codewiki_call_edges_query("project-1", 5000);
    let (import_query, _) = codewiki_import_edges_query("project-1", 5000);

    assert!(!call_query.contains(" IN ["));
    assert!(!import_query.contains(" IN ["));
    assert!(call_query.len() < 1024);
    assert!(import_query.len() < 1024);
}

#[test]
fn edge_limit_validation_rejects_zero_and_excessive_limits() {
    assert!(validate_edge_limit(1).is_ok());
    assert!(validate_edge_limit(MAX_EDGE_LIMIT).is_ok());
    assert!(validate_edge_limit(0).is_err());

    let error = validate_edge_limit(MAX_EDGE_LIMIT + 1).expect_err("limit above cap fails");
    assert!(error.to_string().contains("codewiki --edge-limit"));
}

#[test]
fn clusters_without_falkordb() {
    let input = CodewikiInput {
        files: vec![
            "src/api/handler.rs".to_string(),
            "src/domain/service.rs".to_string(),
            "tests/domain/service_test.rs".to_string(),
        ],
        graph_edges: Vec::new(),
        graph_availability: CodewikiGraphAvailability::Unavailable,
        symbols: vec![
            test_symbol(
                "src/api/handler.rs",
                "handle",
                "function",
                1,
                "pub fn handle()",
            ),
            test_symbol(
                "src/domain/service.rs",
                "Service",
                "class",
                1,
                "pub struct Service;",
            ),
            test_symbol_with_qualified(
                "src/domain/service.rs",
                "new",
                "Service::new",
                "function",
                3,
                "pub fn new() -> Self",
            ),
            test_symbol(
                "tests/domain/service_test.rs",
                "service_test",
                "function",
                1,
                "fn service_test()",
            ),
        ],
    };

    let docs = generate_hierarchical_docs(&input, None);
    let docs_by_path = docs.into_iter().collect::<BTreeMap<_, _>>();

    assert!(docs_by_path.contains_key("code/modules/src/api.md"));
    assert!(docs_by_path.contains_key("code/modules/src/domain.md"));
    assert!(!docs_by_path.contains_key("code/files/tests/domain/service_test.rs.md"));
    assert!(
        docs_by_path
            .get("code/files/src/api/handler.rs.md")
            .expect("handler file doc")
            .contains(&test_component_id(
                "src/api/handler.rs",
                "handle",
                "function"
            ))
    );
    assert!(
        docs_by_path
            .get("code/files/src/domain/service.rs.md")
            .expect("service file doc")
            .contains(&test_component_id(
                "src/domain/service.rs",
                "Service",
                "class"
            ))
    );
    assert!(
        docs_by_path
            .get("code/files/src/domain/service.rs.md")
            .expect("service file doc")
            .contains(&test_component_id(
                "src/domain/service.rs",
                "new",
                "function"
            ))
    );
    assert!(
        !docs_by_path
            .get("code/files/src/domain/service.rs.md")
            .expect("service file doc")
            .contains("src/domain/service.rs::Service::new")
    );
}

#[test]
fn emits_bounded_mermaid() {
    let input = CodewikiInput {
        files: vec![
            "src/api/handler.rs".to_string(),
            "src/domain/service.rs".to_string(),
            "src/storage/repo.rs".to_string(),
            "src/unrelated/tool.rs".to_string(),
        ],
        graph_edges: vec![
            CodewikiGraphEdge::import(
                test_component_id("src/api/handler.rs", "handle", "function"),
                test_component_id("src/domain/service.rs", "Service", "class"),
            ),
            CodewikiGraphEdge::import(
                test_component_id("src/domain/service.rs", "Service", "class"),
                test_component_id("src/storage/repo.rs", "Repo", "class"),
            ),
            CodewikiGraphEdge::import(
                test_component_id("src/unrelated/tool.rs", "Tool", "class"),
                test_component_id("src/storage/repo.rs", "Repo", "class"),
            ),
        ],
        graph_availability: CodewikiGraphAvailability::Available,
        symbols: vec![
            test_symbol(
                "src/api/handler.rs",
                "handle",
                "function",
                1,
                "pub fn handle()",
            ),
            test_symbol(
                "src/domain/service.rs",
                "Service",
                "class",
                1,
                "pub struct Service;",
            ),
            test_symbol(
                "src/storage/repo.rs",
                "Repo",
                "class",
                1,
                "pub struct Repo;",
            ),
            test_symbol(
                "src/unrelated/tool.rs",
                "Tool",
                "class",
                1,
                "pub struct Tool;",
            ),
        ],
    };

    let docs = generate_hierarchical_docs(&input, None);
    let docs_by_path = docs.into_iter().collect::<BTreeMap<_, _>>();
    let rendered = docs_by_path
        .get("code/modules/src/api.md")
        .expect("api module doc");

    assert!(rendered.contains("```mermaid"));
    assert!(rendered.contains("graph LR"));
    assert!(rendered.contains("m_src_api[\"src/api\"] --> m_src_domain[\"src/domain\"]"));
    assert!(rendered.contains("m_src_domain[\"src/domain\"] --> m_src_storage[\"src/storage\"]"));
    assert!(
        !rendered.contains("m_src_unrelated[\"src/unrelated\"] --> m_src_storage[\"src/storage\"]")
    );
}

#[test]
fn bounded_component_edges_prefers_edges_nearest_seed() {
    let seed_components = BTreeSet::from(["seed".to_string()]);
    let edges = BTreeSet::from([
        ("a1".to_string(), "a2".to_string()),
        ("seed".to_string(), "a2".to_string()),
        ("seed".to_string(), "z1".to_string()),
    ]);

    let bounded = render::bounded_component_edges(&seed_components, &edges, 2, 1);

    assert_eq!(
        bounded,
        BTreeSet::from([("seed".to_string(), "a2".to_string())])
    );
}

#[test]
fn mermaid_degrades_without_falkordb() {
    let input = CodewikiInput {
        files: vec!["src/api/handler.rs".to_string()],
        graph_edges: Vec::new(),
        graph_availability: CodewikiGraphAvailability::Unavailable,
        symbols: vec![test_symbol(
            "src/api/handler.rs",
            "handle",
            "function",
            1,
            "pub fn handle()",
        )],
    };

    let docs = generate_hierarchical_docs(&input, None);
    let docs_by_path = docs.into_iter().collect::<BTreeMap<_, _>>();
    let module = docs_by_path
        .get("code/modules/src/api.md")
        .expect("module doc still renders");
    let file = docs_by_path
        .get("code/files/src/api/handler.rs.md")
        .expect("file doc still renders");

    assert!(module.contains("degraded: graph-unavailable"));
    assert!(file.contains("API Symbols"));
    assert!(file.contains(&test_component_id(
        "src/api/handler.rs",
        "handle",
        "function"
    )));
}

#[test]
fn empty_available_graph_does_not_emit_degradation_marker() {
    let input = CodewikiInput {
        files: vec!["src/api/handler.rs".to_string()],
        graph_edges: Vec::new(),
        graph_availability: CodewikiGraphAvailability::Available,
        symbols: vec![test_symbol(
            "src/api/handler.rs",
            "handle",
            "function",
            1,
            "pub fn handle()",
        )],
    };

    let docs = generate_hierarchical_docs(&input, None);
    let docs_by_path = docs.into_iter().collect::<BTreeMap<_, _>>();
    let module = docs_by_path
        .get("code/modules/src/api.md")
        .expect("module doc still renders");

    assert!(!module.contains("degraded: graph-unavailable"));
}

#[test]
fn truncated_graph_emits_degradation_marker_with_partial_diagram() {
    let input = CodewikiInput {
        files: vec![
            "src/api/handler.rs".to_string(),
            "src/domain/service.rs".to_string(),
        ],
        graph_edges: vec![CodewikiGraphEdge::import(
            test_component_id("src/api/handler.rs", "handle", "function"),
            test_component_id("src/domain/service.rs", "Service", "class"),
        )],
        graph_availability: CodewikiGraphAvailability::Truncated,
        symbols: vec![
            test_symbol(
                "src/api/handler.rs",
                "handle",
                "function",
                1,
                "pub fn handle()",
            ),
            test_symbol(
                "src/domain/service.rs",
                "Service",
                "class",
                1,
                "pub struct Service;",
            ),
        ],
    };

    let docs = generate_hierarchical_docs(&input, None);
    let docs_by_path = docs.into_iter().collect::<BTreeMap<_, _>>();
    let module = docs_by_path
        .get("code/modules/src/api.md")
        .expect("module doc still renders");

    assert!(module.contains("degraded: graph-truncated"));
    assert!(module.contains("```mermaid"));
    assert!(module.contains("m_src_api[\"src/api\"] --> m_src_domain[\"src/domain\"]"));
}
