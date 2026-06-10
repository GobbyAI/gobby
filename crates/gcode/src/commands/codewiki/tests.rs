use super::io::{
    source_files_from_frontmatter, source_hashes_for_doc, unquote_yaml_string, write_doc,
};
use super::test_utils::{
    test_component_id, test_symbol, test_symbol_range, test_symbol_with_qualified,
};
use super::*;

mod architecture;
#[path = "changes_tests.rs"]
mod changes_tests;

#[test]
fn generates_hierarchical_docs() {
    let out_dir = tempfile::tempdir().expect("tempdir");
    let input = CodewikiInput {
        files: vec!["src/lib.rs".to_string(), "src/nested/api.rs".to_string()],
        graph_edges: Vec::new(),
        graph_availability: CodewikiGraphAvailability::Available,
        symbols: vec![
            test_symbol("src/lib.rs", "Client", "class", 1, "pub struct Client {"),
            test_symbol("src/lib.rs", "connect", "function", 5, "pub fn connect()"),
            test_symbol(
                "src/nested/api.rs",
                "serve",
                "function",
                3,
                "pub fn serve()",
            ),
        ],
    };

    let docs = generate_hierarchical_docs(&input, None);
    write_doc_set(out_dir.path(), &docs).expect("writes docs");

    let repo = std::fs::read_to_string(out_dir.path().join("code/repo.md")).expect("repo doc");
    let module = std::fs::read_to_string(out_dir.path().join("code/modules/src.md"))
        .expect("src module doc");
    let file =
        std::fs::read_to_string(out_dir.path().join("code/files/src/lib.rs.md")).expect("file doc");

    assert!(repo.contains("[[code/modules/src|src]]"));
    assert!(repo.contains("Repository Overview"));
    assert!(module.contains("[[code/files/src/lib.rs|src/lib.rs]]"));
    assert!(file.contains("API Symbols"));
    assert!(file.contains("pub struct Client {"));
    assert!(file.contains("[[code/modules/src|src]]"));
}

#[test]
fn codewiki_unified_vault_emits_code_paths_frontmatter_and_wikilinks() {
    let input = CodewikiInput {
        files: vec!["src/lib.rs".to_string()],
        graph_edges: Vec::new(),
        graph_availability: CodewikiGraphAvailability::Available,
        symbols: vec![test_symbol(
            "src/lib.rs",
            "Client",
            "class",
            1,
            "pub struct Client {",
        )],
    };

    let docs = generate_hierarchical_docs(&input, None);
    let paths = docs
        .iter()
        .map(|(path, _)| path.as_str())
        .collect::<BTreeSet<_>>();

    assert!(paths.contains("code/repo.md"));
    assert!(paths.contains("code/modules/src.md"));
    assert!(paths.contains("code/files/src/lib.rs.md"));

    let repo = docs
        .iter()
        .find(|(path, _)| path == "code/repo.md")
        .map(|(_, content)| content)
        .expect("repo doc");
    let file = docs
        .iter()
        .find(|(path, _)| path == "code/files/src/lib.rs.md")
        .map(|(_, content)| content)
        .expect("file doc");
    let yaml = file
        .strip_prefix("---\n")
        .and_then(|content| content.split_once("---\n\n"))
        .map(|(yaml, _)| yaml)
        .expect("frontmatter block");
    let frontmatter: serde_yaml::Value = serde_yaml::from_str(yaml).expect("parse frontmatter");

    assert!(repo.contains("[[code/modules/src|src]]"));
    assert!(file.contains("[[code/modules/src|src]]"));
    assert_eq!(
        frontmatter
            .get("generated_by")
            .and_then(serde_yaml::Value::as_str),
        Some("gcode-codewiki")
    );
    assert!(frontmatter.get("source").is_none());
    assert!(frontmatter.get("provenance").is_some());
    assert_eq!(
        frontmatter.get("trust").and_then(serde_yaml::Value::as_str),
        Some("generated")
    );
    assert_eq!(
        frontmatter
            .get("freshness")
            .and_then(serde_yaml::Value::as_str),
        Some("indexed")
    );
    assert!(frontmatter.get("source_files").is_none());
}

#[test]
fn inline_code_uses_commonmark_backtick_delimiters() {
    assert_eq!(inline_code(""), "``");
    assert_eq!(inline_code("plain"), "`plain`");
    assert_eq!(inline_code("a`b"), "``a`b``");
    assert_eq!(inline_code("a``b"), "```a``b```");
    assert_eq!(inline_code("`edge`"), "`` `edge` ``");
    assert_eq!(inline_code("two\nlines"), "`two lines`");
    assert_eq!(inline_code("two\n\t  lines"), "`two lines`");
    assert_eq!(inline_code("  padded  value  "), "`padded value`");
}

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
        },
        FileDoc {
            path: "src/domain.rs".to_string(),
            module: "src/domain{core}|v1".to_string(),
            summary: String::new(),
            source_spans: Vec::new(),
            symbols: Vec::new(),
            component_ids: vec!["domain".to_string()],
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
    let symbol_ids = vec!["symbol-a".to_string(), "symbol-b".to_string()];
    let files = vec!["src/a.rs".to_string(), "src/b.rs".to_string()];

    let (call_query, _) = codewiki_call_edges_query("project-1", &symbol_ids, 17);
    let (import_query, _) = codewiki_import_edges_query("project-1", &files, 17);

    assert!(call_query.contains("LIMIT 17"));
    assert!(import_query.contains("LIMIT 17"));
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

#[test]
fn frontmatter_provenance_accepts_unquoted_and_escaped_values() {
    let files = source_files_from_frontmatter(
        r#"---
provenance:
  - file: src/plain.rs
  - file: "src/escaped\"quote.rs"
---
"#,
    );

    assert!(files.contains("src/plain.rs"));
    assert!(files.contains("src/escaped\"quote.rs"));
}

#[test]
fn frontmatter_provenance_parse_yaml_with_ranges() {
    let files = source_files_from_frontmatter(
        r#"---
title: "Example"
provenance:
  - file: "src/one:thing.rs"
    ranges:
      - "1-4"
  - file: src/two.rs
---
"#,
    );

    assert!(files.contains("src/one:thing.rs"));
    assert!(files.contains("src/two.rs"));
    assert_eq!(files.len(), 2);
}

#[test]
fn frontmatter_legacy_source_files_are_ignored() {
    let files = source_files_from_frontmatter(
        r#"---
source_files:
- file: src/legacy.rs
sources:
- file: src/also-legacy.rs
---
"#,
    );

    assert!(files.is_empty());
}

#[test]
fn source_hashes_reject_frontmatter_paths_outside_project_root() {
    let tempdir = tempfile::tempdir().expect("tempdir");
    let project_root = tempdir.path().join("project");
    std::fs::create_dir_all(&project_root).expect("project root");
    std::fs::write(tempdir.path().join("outside.rs"), "fn outside() {}").expect("outside file");
    let content = r#"---
provenance:
  - file: ../outside.rs
---
"#;

    let err = source_hashes_for_doc(&project_root, content).expect_err("outside source rejected");

    assert!(
        err.to_string().contains("resolves outside project root"),
        "unexpected error: {err}"
    );
}

#[test]
fn yaml_unquote_translates_common_escapes_and_rejects_incomplete_escape() {
    assert_eq!(
        unquote_yaml_string(r#""line\nquote\"tab\tbackslash\\""#),
        Some("line\nquote\"tab\tbackslash\\".to_string())
    );
    assert_eq!(
        unquote_yaml_string(r#""hex\x21 unicode\u2713 scalar\U0001F680""#),
        Some("hex! unicode\u{2713} scalar\u{1f680}".to_string())
    );
    let incomplete = format!("\"{}\\\"", "src/incomplete");
    assert_eq!(unquote_yaml_string(&incomplete), None);
    assert_eq!(unquote_yaml_string(r#""bad\x1""#), None);
    assert_eq!(unquote_yaml_string(r#""bad\u12xz""#), None);
    assert_eq!(unquote_yaml_string(r#""bad\U00110000""#), None);
}

#[test]
fn frontmatter_serializes_scalars_with_serde_yaml() {
    let source_file = "src/quote\"colon:thing.rs";
    let doc = frontmatter(
        "line\nquote\"tab\tbackslash\\nul\0bell\u{0007}",
        "code_file",
        &[SourceSpan {
            file: source_file.to_string(),
            line_start: 7,
            line_end: 9,
        }],
    );
    let yaml = doc
        .strip_prefix("---\n")
        .and_then(|content| content.strip_suffix("---\n\n"))
        .expect("frontmatter delimiters");
    let parsed: serde_yaml::Value = serde_yaml::from_str(yaml).expect("frontmatter parses");
    let serde_yaml::Value::Mapping(mapping) = parsed else {
        panic!("frontmatter is a YAML mapping");
    };

    assert_eq!(
        mapping
            .get(serde_yaml::Value::String("title".to_string()))
            .and_then(serde_yaml::Value::as_str),
        Some("line\nquote\"tab\tbackslash\\nul\0bell\u{0007}")
    );
    assert_eq!(
        mapping
            .get(serde_yaml::Value::String("type".to_string()))
            .and_then(serde_yaml::Value::as_str),
        Some("code_file")
    );
    assert!(source_files_from_frontmatter(&doc).contains(source_file));
}

#[test]
fn citations_validated_against_spans() {
    let input = CodewikiInput {
        files: vec!["src/lib.rs".to_string()],
        graph_edges: Vec::new(),
        graph_availability: CodewikiGraphAvailability::Available,
        symbols: vec![
            test_symbol_range(
                "src/lib.rs",
                "Client",
                "class",
                10,
                14,
                "pub struct Client {",
            ),
            test_symbol_range(
                "src/lib.rs",
                "connect",
                "function",
                20,
                24,
                "pub fn connect()",
            ),
        ],
    };
    let mut generator = |prompt: &str, _system: &str| {
        if prompt.contains("Client") {
            Some("Builds client state [src/lib.rs:999].".to_string())
        } else if prompt.contains("connect") {
            Some("Opens a connection [src/lib.rs:20].".to_string())
        } else {
            Some("Coordinates the public API [missing.rs:1].".to_string())
        }
    };

    let docs = generate_hierarchical_docs(&input, Some(&mut generator));
    let file_doc = docs
        .iter()
        .find(|(path, _)| path == "code/files/src/lib.rs.md")
        .map(|(_, content)| content)
        .expect("file doc");

    assert!(!file_doc.contains("source:\n"));
    assert!(file_doc.contains("provenance:\n"));
    assert!(source_files_from_frontmatter(file_doc).contains("src/lib.rs"));
    assert!(file_doc.contains("10-14"));
    assert!(file_doc.contains("20-24"));
    assert!(file_doc.contains("[src/lib.rs:10-14]"));
    assert!(file_doc.contains("[src/lib.rs:20]"));
    assert!(!file_doc.contains("src/lib.rs:999"));
    assert!(!file_doc.contains("missing.rs:1"));
}

#[test]
fn incremental_regenerates_only_changed() {
    let project = tempfile::tempdir().expect("project tempdir");
    std::fs::create_dir_all(project.path().join("src/nested")).expect("source dirs");
    std::fs::write(project.path().join("src/lib.rs"), "pub struct Client;\n").expect("write lib");
    std::fs::write(
        project.path().join("src/nested/api.rs"),
        "pub fn serve() {}\n",
    )
    .expect("write api");
    let out_dir = project.path().join("codewiki");

    let input = CodewikiInput {
        files: vec!["src/lib.rs".to_string(), "src/nested/api.rs".to_string()],
        graph_edges: Vec::new(),
        graph_availability: CodewikiGraphAvailability::Available,
        symbols: vec![
            test_symbol("src/lib.rs", "Client", "class", 1, "pub struct Client;"),
            test_symbol(
                "src/nested/api.rs",
                "serve",
                "function",
                1,
                "pub fn serve()",
            ),
        ],
    };

    let first_docs = generate_hierarchical_docs(&input, None);
    let first_written =
        write_incremental_doc_set(project.path(), &out_dir, &first_docs).expect("first write");
    assert!(first_written.contains(&"code/repo.md".to_string()));
    assert!(first_written.contains(&"code/modules/src.md".to_string()));
    assert!(first_written.contains(&"code/files/src/lib.rs.md".to_string()));
    assert!(first_written.contains(&"code/files/src/nested/api.rs.md".to_string()));

    let unchanged_file_doc = out_dir.join("code/files/src/nested/api.rs.md");
    let mut unchanged_content =
        std::fs::read_to_string(&unchanged_file_doc).expect("unchanged doc content");
    unchanged_content.push_str("\n<!-- preserve unchanged doc -->\n");
    std::fs::write(&unchanged_file_doc, unchanged_content).expect("write unchanged marker");

    std::fs::write(
        project.path().join("src/lib.rs"),
        "pub struct Client;\npub fn connect() {}\n",
    )
    .expect("modify lib");
    let changed_docs = generate_hierarchical_docs(&input, None);
    let changed_written = write_incremental_doc_set(project.path(), &out_dir, &changed_docs)
        .expect("incremental write");
    let unchanged_after =
        std::fs::read_to_string(&unchanged_file_doc).expect("unchanged doc after content");

    assert!(unchanged_after.contains("preserve unchanged doc"));
    assert_eq!(
        changed_written,
        vec![
            "code/repo.md".to_string(),
            "code/_onboarding.md".to_string(),
            "code/_architecture.md".to_string(),
            "code/modules/src.md".to_string(),
            "code/files/src/lib.rs.md".to_string()
        ]
    );
    let meta = std::fs::read_to_string(out_dir.join("_meta/codewiki.json")).expect("read meta log");
    let meta: serde_json::Value = serde_json::from_str(&meta).expect("parse meta log");
    let generated_docs = meta["generated_docs"].as_array().expect("generated docs");
    assert_eq!(
        generated_docs,
        &vec![
            serde_json::Value::String("code/repo.md".to_string()),
            serde_json::Value::String("code/_onboarding.md".to_string()),
            serde_json::Value::String("code/_architecture.md".to_string()),
            serde_json::Value::String("code/modules/src.md".to_string()),
            serde_json::Value::String("code/files/src/lib.rs.md".to_string())
        ]
    );

    let reduced_input = CodewikiInput {
        files: vec!["src/lib.rs".to_string()],
        graph_edges: Vec::new(),
        graph_availability: CodewikiGraphAvailability::Available,
        symbols: vec![test_symbol(
            "src/lib.rs",
            "Client",
            "class",
            1,
            "pub struct Client;",
        )],
    };
    let reduced_docs = generate_hierarchical_docs(&reduced_input, None);
    write_incremental_doc_set(project.path(), &out_dir, &reduced_docs).expect("stale docs removed");

    assert!(!unchanged_file_doc.exists());
    let meta =
        std::fs::read_to_string(out_dir.join("_meta/codewiki.json")).expect("read final meta");
    let meta: serde_json::Value = serde_json::from_str(&meta).expect("parse final meta");
    assert!(
        meta["docs"]
            .get("code/files/src/nested/api.rs.md")
            .is_none()
    );
}

#[test]
fn run_summary_serializes_daemon_contract_keys() {
    let summary = CodewikiRunSummary {
        command: "codewiki",
        project_id: "project-1".to_string(),
        project_root: "/repo".to_string(),
        out_dir: "/repo/codewiki".to_string(),
        generated_pages: 3,
        changed_paths: vec!["repo.md".to_string()],
        skipped: 2,
        files: 1,
        modules: 1,
        symbols: 4,
        ai_enabled: false,
    };

    let value = serde_json::to_value(summary).expect("summary json");

    assert_eq!(value["command"], "codewiki");
    assert_eq!(value["project_id"], "project-1");
    assert_eq!(value["project_root"], "/repo");
    assert_eq!(value["changed_paths"][0], "repo.md");
    assert_eq!(value["skipped"], 2);
    assert_eq!(value["ai_enabled"], false);
}

#[test]
fn component_id_uses_stored_symbol_id() {
    let mut symbol = test_symbol("src/lib.rs", "Client", "class", 1, "pub struct Client;");
    symbol.id = "stored-symbol-id".to_string();
    assert_eq!(symbol.id, "stored-symbol-id");
}

#[test]
#[cfg(unix)]
fn write_doc_rejects_symlinked_parent() {
    use std::os::unix::fs::symlink;

    let project = tempfile::tempdir().expect("project tempdir");
    let out_dir = project.path().join("codewiki");
    let outside = tempfile::tempdir().expect("outside tempdir");
    std::fs::create_dir_all(&out_dir).expect("out dir");
    symlink(outside.path(), out_dir.join("linked")).expect("symlink parent");

    let err = write_doc(&out_dir, "linked/escape.md", "escaped")
        .expect_err("symlink parent should be rejected");

    assert!(err.to_string().contains("symlinked codewiki path"));
    assert!(!outside.path().join("escape.md").exists());
}

#[test]
#[cfg(unix)]
fn write_doc_rejects_symlinked_target() {
    use std::os::unix::fs::symlink;

    let project = tempfile::tempdir().expect("project tempdir");
    let out_dir = project.path().join("codewiki");
    let outside = tempfile::tempdir().expect("outside tempdir");
    std::fs::create_dir_all(&out_dir).expect("out dir");
    let outside_target = outside.path().join("target.md");
    symlink(&outside_target, out_dir.join("target.md")).expect("symlink target");

    let err = write_doc(&out_dir, "target.md", "escaped").expect_err("symlink target rejected");

    assert!(err.to_string().contains("symlinked codewiki path"));
    assert!(!outside_target.exists());
}
