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
fn search_result_token_budget_uses_text_row_estimate() {
    let mut first = symbol("src/lib.rs", "function", "rust").to_brief();
    first.score = 1.0;
    first.sources = Some(vec!["exact".to_string()]);
    let mut second = symbol("src/other.rs", "function", "rust").to_brief();
    second.score = 0.9;
    second.sources = Some(vec!["semantic".to_string()]);
    let budget = token_budget::estimate_tokens(&format_search_result_line(&first));
    let expected_path = first.file_path.clone();

    let trimmed = token_budget::trim_results(
        vec![first, second],
        Some(budget),
        SEARCH_TOKEN_BUDGET_REFINE_HINT,
        format_search_result_line,
    );

    assert_eq!(trimmed.results.len(), 1);
    assert_eq!(trimmed.results[0].file_path, expected_path);
    let hint = trimmed.hint.expect("token budget hint");
    assert!(hint.contains("1 of 2 results"));
    assert!(hint.contains("refine with `--kind`, `--language`, PATH filters"));
}

#[test]
fn literal_query_hint_detects_literal_like_queries() {
    for query in [
        "spawn_ui_server(",
        "config.ui.mode",
        "\"quoted string\"",
        "src/foo.rs",
    ] {
        let hint = literal_query_hint(query).expect("literal hint");
        assert!(hint.contains("gcode grep"));
        assert!(hint.contains("search-content"));
    }
}

#[test]
fn literal_query_hint_skips_natural_language_queries() {
    assert!(literal_query_hint("database connection pool").is_none());
}

#[test]
fn content_snippet_compaction_collapses_whitespace() {
    assert_eq!(
        compact_snippet("  first line\n    second\tline\r\nthird  "),
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
