use super::*;

fn symbol(file_path: &str, kind: &str, language: &str) -> Symbol {
    Symbol {
        id: "sym-1".to_string(),
        project_id: "proj".to_string(),
        file_path: file_path.to_string(),
        name: "outline".to_string(),
        qualified_name: "outline".to_string(),
        kind: kind.to_string(),
        language: language.to_string(),
        byte_start: 0,
        byte_end: 10,
        line_start: 1,
        line_end: 2,
        signature: None,
        docstring: None,
        parent_symbol_id: None,
        file_content_hash: String::new(),
        content_hash: String::new(),
        summary: None,
        created_at: String::new(),
        updated_at: String::new(),
    }
}

#[test]
fn symbol_filter_rejects_language_kind_path_and_missing_disk_file() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let src = tmp.path().join("src");
    std::fs::create_dir_all(&src).expect("create src");
    std::fs::write(src.join("lib.rs"), "fn outline() {}").expect("write file");
    let pattern = glob::Pattern::new("src/*.rs").expect("glob");
    let rust_fn = symbol("src/lib.rs", "function", "rust");
    let ctx = Context {
        database_url: "postgresql://localhost/gobby-test".to_string(),
        project_root: tmp.path().to_path_buf(),
        project_id: "proj".to_string(),
        quiet: true,
        falkordb: None,
        qdrant: None,
        embedding: None,
        code_vectors: crate::config::CodeVectorSettings::default(),
        runtime_config_capture_degraded: false,
        indexing: gobby_core::config::IndexingConfig::default(),
        daemon_url: None,
        grant_ai: None,
        index_scope: crate::config::ProjectIndexScope::Single,
    };

    let rust_glob = std::slice::from_ref(&pattern);
    assert!(symbol_matches_local_filters(
        &ctx,
        &rust_fn,
        Some("function"),
        Some("rust"),
        rust_glob,
    ));
    assert!(!symbol_matches_local_filters(
        &ctx,
        &rust_fn,
        Some("class"),
        Some("rust"),
        rust_glob,
    ));
    assert!(!symbol_matches_local_filters(
        &ctx,
        &rust_fn,
        Some("function"),
        Some("python"),
        rust_glob,
    ));
    let py_pattern = glob::Pattern::new("src/*.py").expect("glob");
    assert!(!symbol_matches_local_filters(
        &ctx,
        &rust_fn,
        Some("function"),
        Some("rust"),
        std::slice::from_ref(&py_pattern),
    ));
    assert!(!symbol_matches_local_filters(
        &ctx,
        &symbol("src/missing.rs", "function", "rust"),
        Some("function"),
        Some("rust"),
        rust_glob,
    ));
}

#[test]
fn exact_tier_prefers_case_sensitive_match() {
    assert_eq!(
        exact_tier("outline", &symbol("src/lib.rs", "function", "rust")),
        0
    );

    let mut case_variant = symbol("src/lib.rs", "function", "rust");
    case_variant.name = "Outline".to_string();
    case_variant.qualified_name = "Outline".to_string();
    assert_eq!(exact_tier("outline", &case_variant), 1);

    case_variant.name = "outline_helper".to_string();
    case_variant.qualified_name = "outline_helper".to_string();
    assert_eq!(exact_tier("outline", &case_variant), 2);
}

#[test]
fn final_score_preserves_display_tier_before_rrf_score() {
    let exact = symbol("src/lib.rs", "function", "rust");
    let mut fuzzy = symbol("src/other.rs", "function", "rust");
    fuzzy.name = "outline_helper".to_string();
    fuzzy.qualified_name = "outline_helper".to_string();

    assert!(final_rank_score("outline", &exact, 0.01) > final_rank_score("outline", &fuzzy, 0.08));
}

#[test]
fn combines_fetch_cap_and_path_post_filter_hints() {
    let hint = token_budget::combine_hints(
        Some(filtered_fetch_cap_hint()),
        Some(path_filter_post_filter_hint()),
    )
    .expect("hint");

    assert!(hint.contains("fetch cap"));
    assert!(hint.contains("post-filtered"));
}

#[test]
fn snake_case_query_hint_routes_to_symbol_and_word_grep_with_shell_safe_paths() {
    let paths = vec!["src/user guides".to_string(), "docs/owner's.md".to_string()];
    let hint = search_lane_hint(
        "context_handoff",
        &paths,
        Some("function"),
        Some("rust"),
        false,
        true,
    )
    .expect("identifier hint");

    assert!(hint.contains(
        "`gcode search-symbol context_handoff 'src/user guides' 'docs/owner'\"'\"'s.md' \
         --kind function --language rust`"
    ));
    assert!(hint.contains(
        "`gcode grep -w context_handoff 'src/user guides' 'docs/owner'\"'\"'s.md' -m 50`"
    ));

    let constant_hint = search_lane_hint("RUNTIME_CONFIG", &[], None, None, false, true)
        .expect("constant-style identifier hint");
    assert!(constant_hint.contains("gcode grep -w RUNTIME_CONFIG"));
}

#[test]
fn literal_query_hint_routes_to_fixed_string_grep() {
    for query in [
        "spawn_ui_server(",
        "config.ui.mode",
        "\"quoted string\"",
        "src/foo.rs",
    ] {
        let hint = search_lane_hint(query, &[], None, None, false, true).expect("literal hint");
        assert!(hint.contains("gcode grep -F"));
        assert!(!hint.contains("gcode grep \"pattern\""));
    }
}

#[test]
fn empty_or_content_only_symbol_search_redirects_to_search_content() {
    let markdown_paths = vec!["src/lib.rs".to_string(), "docs/user guide.md".to_string()];
    let path_hint = search_lane_hint(
        "Context pressure",
        &markdown_paths,
        None,
        Some("markdown"),
        false,
        true,
    )
    .expect("content-only path hint");
    assert!(path_hint.contains(
        "`gcode search-content 'Context pressure' src/lib.rs 'docs/user guide.md' --language markdown`"
    ));

    let empty_hint = search_lane_hint(
        "natural language concept",
        &["docs".to_string()],
        None,
        None,
        true,
        true,
    )
    .expect("empty symbol-search hint");
    assert!(empty_hint.contains("`gcode search-content 'natural language concept' docs`"));
}

#[test]
fn natural_language_symbol_query_stays_in_hybrid_lane_when_results_exist() {
    assert!(search_lane_hint("database connection pool", &[], None, None, false, true,).is_none());
}

#[test]
fn content_snippet_compaction_collapses_whitespace() {
    assert_eq!(
        output::compact_snippet("  first line\n    second\tline\r\nthird  "),
        "first line second line third"
    );
}

#[test]
fn outage_degrades_with_warning() {
    let semantic = semantic_lane_from_grant_outage();
    let SemanticLane::Degraded(warning) = &semantic else {
        panic!("daemon outage must degrade the semantic lane, got {semantic:?}");
    };
    assert_eq!(warning.lane, crate::models::SearchWarningLane::Semantic);
    assert_eq!(
        warning.cause,
        crate::models::SearchWarningCause::DaemonUnreachable
    );

    let assembled = assemble_hybrid_sources(
        vec!["exact-1".to_string()],
        vec!["lex-1".to_string()],
        semantic,
        vec!["graph-1".to_string()],
        Vec::new(),
    );
    let names: Vec<&str> = assembled.sources.iter().map(|(name, _)| *name).collect();
    assert!(
        names.contains(&"fts"),
        "lexical lane must survive: {names:?}"
    );
    assert!(
        names.contains(&"graph"),
        "graph lane must survive: {names:?}"
    );
    assert!(
        !names.contains(&"semantic"),
        "silent empty semantic source is forbidden: {names:?}"
    );
    assert_eq!(assembled.warnings.len(), 1);
    assert_eq!(
        assembled.warnings[0].lane,
        crate::models::SearchWarningLane::Semantic
    );

    let payload = serde_json::to_value(&crate::models::PagedResponse {
        project_id: "proj".to_string(),
        total: 1,
        offset: 0,
        limit: 10,
        results: Vec::<crate::models::SearchResult>::new(),
        next_offset: None,
        budget_exceeded: false,
        hint: None,
        warnings: assembled.warnings.clone(),
    })
    .expect("json");
    let warnings = payload["warnings"].as_array().expect("JSON warnings field");
    assert_eq!(warnings.len(), 1);
    assert_eq!(warnings[0]["lane"], "semantic");
    assert_eq!(warnings[0]["cause"], "daemon_unreachable");

    #[cfg(feature = "ai")]
    {
        let explicit = gobby_core::ai::require_modality_ready(
            &gobby_core::grant::GrantCapabilities {
                postgres: gobby_core::grant::PostgresCapability::Unavailable {},
                falkordb: gobby_core::grant::FalkorCapability::Unavailable {},
                qdrant: gobby_core::grant::QdrantCapability::Unavailable {},
                embed: gobby_core::grant::AiCapability::Daemon {},
                text_generate: gobby_core::grant::AiCapability::Daemon {},
                tool_chat: gobby_core::grant::AiCapability::Daemon {},
                vision_extract: gobby_core::grant::AiCapability::Daemon {},
                audio_transcribe: gobby_core::grant::AiCapability::Daemon {},
                broker_operations: Vec::new(),
            },
            false,
            gobby_core::config::AiCapability::TextGenerate,
        )
        .expect_err("explicit AI must fail typed on daemon outage");
        match explicit {
            gobby_core::ai_types::AiError::CapabilityUnavailable { capability, .. } => {
                assert_eq!(capability, "text_generate");
            }
            other => panic!("expected typed capability error, got {other:?}"),
        }
    }
}
