use gobby_core::ai_types::AiError;
use gobby_core::config::AiRouting;

use super::support::*;
use super::*;

fn depth_probe_input() -> CodewikiInput {
    CodewikiInput {
        leading_chunks: std::collections::BTreeMap::new(),
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
    }
}

fn generation_systems_at_depth(ai_depth: AiDepth) -> Vec<String> {
    let input = depth_probe_input();
    let mut systems = Vec::new();
    let mut generator = |_prompt: &str, system: &str, _tier: PromptTier| {
        systems.push(system.to_string());
        None
    };
    let mut progress = CodewikiProgress::silent();
    let docs = generate_hierarchical_docs_with_progress(
        &input,
        Some(&mut generator),
        ai_depth,
        &mut progress,
    );
    assert!(!docs.is_empty());
    systems
}

#[test]
fn ai_depth_sections_skips_symbol_and_file_generation() {
    let systems = generation_systems_at_depth(AiDepth::Sections);
    assert!(!systems.iter().any(|s| s == prompts::SYMBOL_SYSTEM));
    assert!(!systems.iter().any(|s| s == prompts::FILE_SYSTEM));
    assert!(systems.iter().any(|s| s == prompts::MODULE_SYSTEM));
    assert!(systems.iter().any(|s| s == prompts::REPO_SYSTEM));
    assert!(systems.iter().any(|s| s == prompts::ARCHITECTURE_SYSTEM));
}

#[test]
fn ai_depth_files_skips_symbol_generation_only() {
    let systems = generation_systems_at_depth(AiDepth::Files);
    assert!(!systems.iter().any(|s| s == prompts::SYMBOL_SYSTEM));
    assert!(systems.iter().any(|s| s == prompts::FILE_SYSTEM));
    assert!(systems.iter().any(|s| s == prompts::MODULE_SYSTEM));
}

#[test]
fn ai_depth_symbols_generates_symbol_purposes() {
    let systems = generation_systems_at_depth(AiDepth::Symbols);
    assert!(systems.iter().any(|s| s == prompts::SYMBOL_SYSTEM));
    assert!(systems.iter().any(|s| s == prompts::FILE_SYSTEM));
}

#[test]
fn non_code_files_get_content_derived_purpose_from_leading_chunk() {
    let mut leading_chunks = std::collections::BTreeMap::new();
    leading_chunks.insert(
        "README.md".to_string(),
        LeadingChunk {
            content: "# Demo\n\nA workspace of small CLI tools.".to_string(),
            line_start: 1,
            line_end: 3,
        },
    );
    let input = CodewikiInput {
        leading_chunks,
        files: vec!["README.md".to_string(), "src/lib.rs".to_string()],
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

    let mut content_prompts = Vec::new();
    let mut generator = |prompt: &str, system: &str, _tier: PromptTier| {
        if system == prompts::CONTENT_FILE_SYSTEM {
            content_prompts.push(prompt.to_string());
            Some("Introduces the workspace of small CLI tools.".to_string())
        } else {
            None
        }
    };
    let docs = generate_hierarchical_docs(&input, Some(&mut generator));
    let docs_by_path = docs.into_iter().collect::<BTreeMap<_, _>>();
    let readme = docs_by_path
        .get("code/files/README.md.md")
        .expect("readme file doc");

    let prompt = content_prompts.first().expect("content prompt captured");
    assert!(prompt.contains("File: README.md"), "{prompt}");
    assert!(
        prompt.contains("A workspace of small CLI tools."),
        "{prompt}"
    );
    assert!(
        readme.contains("Introduces the workspace of small CLI tools."),
        "{readme}"
    );
    // The content-derived purpose grounds in the leading chunk's line range.
    assert!(readme.contains("[README.md:1-3]"), "{readme}");
    assert!(!readme.contains("has no indexed API symbols"), "{readme}");
}

#[test]
fn repo_front_page_drops_no_symbol_filler_for_root_files() {
    let input = CodewikiInput {
        leading_chunks: std::collections::BTreeMap::new(),
        files: vec!["README.md".to_string(), "src/lib.rs".to_string()],
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

    let docs = generate_hierarchical_docs(&input, None);
    let docs_by_path = docs.into_iter().collect::<BTreeMap<_, _>>();
    let repo = docs_by_path.get("code/repo.md").expect("repo doc");

    assert!(
        repo.lines()
            .any(|line| line.trim() == "| [[code/files/README.md\\|README.md]] |  |"),
        "filler-only root files keep an empty summary cell: {repo}"
    );
    assert!(!repo.contains("has no indexed API symbols"), "{repo}");
}

#[test]
fn aggregate_docs_use_heavier_prompt_tier_than_symbol_docs() {
    let input = depth_probe_input();
    let mut tiers = Vec::new();
    let mut generator = |_prompt: &str, system: &str, tier: PromptTier| {
        tiers.push((system.to_string(), tier));
        None
    };
    let mut progress = CodewikiProgress::silent();
    let docs = generate_hierarchical_docs_with_progress(
        &input,
        Some(&mut generator),
        AiDepth::Symbols,
        &mut progress,
    );
    assert!(!docs.is_empty());

    let tier_for = |target: &str| {
        tiers
            .iter()
            .filter(|(system, _)| system == target)
            .map(|(_, tier)| *tier)
            .collect::<Vec<_>>()
    };
    // Top-level repo-wide synthesis routes to the opus-first aggregate tier (#904).
    for aggregate in [prompts::REPO_SYSTEM, prompts::ARCHITECTURE_SYSTEM] {
        let seen = tier_for(aggregate);
        assert!(!seen.is_empty(), "{aggregate} generates");
        assert!(
            seen.iter().all(|tier| *tier == PromptTier::Aggregate),
            "{aggregate} routes to the opus-first aggregate tier"
        );
    }
    // Module docs and file-body narratives are mid-level per-unit synthesis, so
    // they route to the sonnet Module tier (#904); only per-symbol purposes stay
    // on the standard tier.
    for module in [prompts::FILE_SYSTEM, prompts::MODULE_SYSTEM] {
        let seen = tier_for(module);
        assert!(!seen.is_empty(), "{module} generates");
        assert!(
            seen.iter().all(|tier| *tier == PromptTier::Module),
            "{module} routes to the module tier"
        );
    }
    let symbol_tiers = tier_for(prompts::SYMBOL_SYSTEM);
    assert!(!symbol_tiers.is_empty(), "symbol purposes generate");
    assert!(
        symbol_tiers
            .iter()
            .all(|tier| *tier == PromptTier::Standard),
        "symbol purposes stay on the standard tier"
    );
}

#[test]
fn ai_mode_change_invalidates_unchanged_docs() {
    let project = tempfile::tempdir().expect("project tempdir");
    std::fs::create_dir_all(project.path().join("src")).expect("source dirs");
    std::fs::write(project.path().join("src/lib.rs"), "pub struct Client;\n").expect("write lib");
    let out_dir = project.path().join("codewiki");
    let input = CodewikiInput {
        leading_chunks: std::collections::BTreeMap::new(),
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
    let docs = generate_hierarchical_docs(&input, None)
        .into_iter()
        .map(|(path, content)| BuiltDoc::healthy(path, content))
        .collect::<Vec<_>>();
    let file_doc = "code/files/src/lib.rs.md".to_string();

    write_incremental_doc_set_with_snapshot(
        project.path(),
        &out_dir,
        &docs,
        None,
        "off",
        DocPruneScope::unscoped(),
    )
    .expect("first write");
    let rewritten = write_incremental_doc_set_with_snapshot(
        project.path(),
        &out_dir,
        &docs,
        None,
        "sections",
        DocPruneScope::unscoped(),
    )
    .expect("mode change write");
    assert!(rewritten.contains(&file_doc));

    let same_mode = write_incremental_doc_set_with_snapshot(
        project.path(),
        &out_dir,
        &docs,
        None,
        "sections",
        DocPruneScope::unscoped(),
    )
    .expect("same mode write");
    assert!(!same_mode.contains(&file_doc));
}

fn doc<'a>(docs: &'a [BuiltDoc], path: &str) -> &'a BuiltDoc {
    docs.iter()
        .find(|doc| doc.path == path)
        .unwrap_or_else(|| panic!("doc {path} is generated"))
}

#[test]
fn generation_failure_records_degradation_in_frontmatter_and_meta() {
    let project = tempfile::tempdir().expect("project tempdir");
    std::fs::create_dir_all(project.path().join("src/nested")).expect("source dirs");
    std::fs::write(project.path().join("src/lib.rs"), "pub struct Client;\n").expect("write lib");
    std::fs::write(
        project.path().join("src/nested/api.rs"),
        "pub fn serve() {}\n",
    )
    .expect("write api");
    let out_dir = project.path().join("codewiki");

    let mut failing_generator = |_prompt: &str, _system: &str, _tier: PromptTier| None;
    let mut progress = CodewikiProgress::silent();
    let docs = generate_hierarchical_docs_with_progress(
        &depth_probe_input(),
        Some(&mut failing_generator),
        AiDepth::Symbols,
        &mut progress,
    );

    for path in ["code/repo.md", "code/modules/src.md"] {
        let built = doc(&docs, path);
        assert!(built.degraded, "{path} records the failed generation");
        assert!(built.content.contains("degraded: true"), "{path}");
        assert!(built.content.contains("- model-unavailable"), "{path}");
    }
    assert!(!doc(&docs, "code/_onboarding.md").degraded);

    let mut sink = DocSink::open(project.path(), &out_dir, "symbols")
        .expect("sink opens")
        .with_ai_outcome(CodewikiAiOutcome::generated(AiRouting::Daemon, false));
    for doc in &docs {
        sink.persist(doc).expect("doc persists");
    }
    sink.finish(None).expect("sink finishes");
    let meta = std::fs::read_to_string(out_dir.join("_meta/codewiki.json")).expect("read meta");
    let meta: serde_json::Value = serde_json::from_str(&meta).expect("parse meta");
    for path in [
        "code/repo.md",
        "code/modules/src.md",
        "code/files/src/lib.rs.md",
    ] {
        assert_eq!(
            meta["docs"][path]["degraded"],
            serde_json::Value::Bool(true),
            "{path} degradation lands in _meta/codewiki.json"
        );
        assert_eq!(meta["docs"][path]["ai_route"], "daemon", "{path} route");
        assert_eq!(meta["docs"][path]["ai_fallback"], false, "{path} fallback");
        assert_eq!(
            meta["docs"][path]["ai_generation_status"], "degraded",
            "{path} status"
        );
    }
    assert!(
        meta["docs"]["code/_onboarding.md"]
            .get("degraded")
            .is_none()
    );
}

#[test]
fn ast_only_generation_records_no_degradation() {
    let mut progress = CodewikiProgress::silent();
    let docs = generate_hierarchical_docs_with_progress(
        &depth_probe_input(),
        None,
        AiDepth::Symbols,
        &mut progress,
    );

    for built in &docs {
        assert!(!built.degraded, "{} is structural by intent", built.path);
        assert!(
            !built.content.contains("- model-unavailable"),
            "{} must not claim model degradation when AI is off",
            built.path
        );
    }
}

#[test]
fn transient_generation_failure_retries_to_healthy_doc() {
    let mut calls = 0_usize;
    let mut flaky_transport = || {
        calls += 1;
        if calls == 1 {
            Err(AiError::TransportFailure {
                status: None,
                body: None,
                source: "connection reset".to_string(),
            })
        } else {
            Ok("Generated prose.".to_string())
        }
    };
    let mut generator = |_prompt: &str, system: &str, _tier: PromptTier| {
        if system == prompts::CURATED_NAVIGATION_SYSTEM {
            Some(test_curated_navigation_json())
        } else if system == prompts::CONCEPT_PAGE_SYSTEM {
            generate_with_bounded_retry(&mut flaky_transport)
                .ok()
                .map(|_| test_concept_handbook_body())
        } else if system == prompts::NARRATIVE_PAGE_SYSTEM {
            generate_with_bounded_retry(&mut flaky_transport)
                .ok()
                .map(|_| test_narrative_handbook_body())
        } else {
            generate_with_bounded_retry(&mut flaky_transport).ok()
        }
    };

    let mut progress = CodewikiProgress::silent();
    let docs = generate_hierarchical_docs_with_progress(
        &depth_probe_input(),
        Some(&mut generator),
        AiDepth::Sections,
        &mut progress,
    );

    let repo = doc(&docs, "code/repo.md");
    assert!(!repo.degraded, "retried generation produces a healthy doc");
    assert!(repo.content.contains("Generated prose."));
    assert!(docs.iter().all(|doc| !doc.degraded));
}

#[test]
fn ai_route_outcomes_render_frontmatter_body_notes_and_meta() {
    struct Case {
        name: &'static str,
        outcome: CodewikiAiOutcome,
        degraded: bool,
        route: &'static str,
        fallback: bool,
        status: &'static str,
        note: Option<&'static str>,
    }

    let cases = [
        Case {
            name: "off",
            outcome: CodewikiAiOutcome::skipped(AiRouting::Off, false),
            degraded: false,
            route: "off",
            fallback: false,
            status: "skipped",
            note: None,
        },
        Case {
            name: "daemon_generated",
            outcome: CodewikiAiOutcome::generated(AiRouting::Daemon, false),
            degraded: false,
            route: "daemon",
            fallback: false,
            status: "generated",
            note: None,
        },
        Case {
            name: "auto_fallback_direct",
            outcome: CodewikiAiOutcome::generated(AiRouting::Direct, true),
            degraded: false,
            route: "direct",
            fallback: true,
            status: "generated",
            note: Some("Direct route"),
        },
        Case {
            name: "auto_fallback_off",
            outcome: CodewikiAiOutcome::skipped(AiRouting::Off, true),
            degraded: false,
            route: "off",
            fallback: true,
            status: "skipped",
            note: Some("structural documentation only"),
        },
        Case {
            name: "direct_no_generator",
            outcome: CodewikiAiOutcome::skipped(AiRouting::Direct, false),
            degraded: false,
            route: "direct",
            fallback: false,
            status: "skipped",
            note: Some("structural documentation only"),
        },
        Case {
            name: "daemon_failed",
            outcome: CodewikiAiOutcome::generated(AiRouting::Daemon, false),
            degraded: true,
            route: "daemon",
            fallback: false,
            status: "degraded",
            note: Some("generation failed"),
        },
    ];

    for case in cases {
        let project = tempfile::tempdir().expect("project tempdir");
        std::fs::create_dir_all(project.path().join("src")).expect("source dir");
        std::fs::write(project.path().join("src/lib.rs"), "pub struct Client;\n")
            .expect("write source");
        let out_dir = project.path().join(format!("out-{}", case.name));
        let mut doc = BuiltDoc::healthy(
            "code/repo.md",
            format!(
                "{}# Repo\n\nStructural body.\n",
                frontmatter_with_degradation_without_ranges(
                    "Repo",
                    "repo",
                    &[SourceSpan {
                        file: "src/lib.rs".to_string(),
                        line_start: 1,
                        line_end: 1,
                    }],
                    &[],
                )
            ),
        );
        doc.degraded = case.degraded;

        let mut sink = DocSink::open(project.path(), &out_dir, "sections")
            .expect("sink opens")
            .with_ai_outcome(case.outcome);
        sink.persist(&doc).expect("doc persisted");
        sink.finish(None).expect("sink finishes");

        let markdown = std::fs::read_to_string(out_dir.join("code/repo.md")).expect("read page");
        let frontmatter = parse_yaml_frontmatter(&markdown);
        assert_eq!(
            frontmatter.get("type").and_then(serde_yaml::Value::as_str),
            Some("repo"),
            "{} type",
            case.name
        );
        assert_eq!(
            frontmatter
                .get("ai_route")
                .and_then(serde_yaml::Value::as_str),
            Some(case.route),
            "{} route",
            case.name
        );
        assert_eq!(
            frontmatter
                .get("ai_fallback")
                .and_then(serde_yaml::Value::as_bool),
            Some(case.fallback),
            "{} fallback",
            case.name
        );
        assert_eq!(
            frontmatter
                .get("ai_generation_status")
                .and_then(serde_yaml::Value::as_str),
            Some(case.status),
            "{} status",
            case.name
        );
        match case.note {
            Some(note) => assert!(markdown.contains(note), "{} note missing", case.name),
            None => assert!(
                !markdown.contains("codewiki-ai-notice:start"),
                "{} unexpected AI body note",
                case.name
            ),
        }

        let meta = std::fs::read_to_string(out_dir.join("_meta/codewiki.json")).expect("read meta");
        let meta: serde_json::Value = serde_json::from_str(&meta).expect("parse meta");
        let doc_meta = &meta["docs"]["code/repo.md"];
        assert_eq!(doc_meta["ai_route"], case.route, "{} meta route", case.name);
        assert_eq!(
            doc_meta["ai_fallback"], case.fallback,
            "{} meta fallback",
            case.name
        );
        assert_eq!(
            doc_meta["ai_generation_status"], case.status,
            "{} meta status",
            case.name
        );
    }
}

#[test]
fn ai_frontmatter_schema_is_present_on_representative_page_kinds() {
    let project = tempfile::tempdir().expect("project tempdir");
    std::fs::create_dir_all(project.path().join("src/nested")).expect("source dirs");
    std::fs::write(project.path().join("src/lib.rs"), "pub struct Client;\n").expect("write lib");
    std::fs::write(
        project.path().join("src/nested/api.rs"),
        "pub fn serve() {}\n",
    )
    .expect("write api");
    let out_dir = project.path().join("codewiki");
    let mut progress = CodewikiProgress::silent();
    let docs = generate_hierarchical_docs_with_progress(
        &depth_probe_input(),
        None,
        AiDepth::Files,
        &mut progress,
    );

    let mut sink = DocSink::open(project.path(), &out_dir, "files")
        .expect("sink opens")
        .with_ai_outcome(CodewikiAiOutcome::generated(AiRouting::Daemon, false));
    for doc in &docs {
        sink.persist(doc).expect("doc persists");
    }
    sink.finish(None).expect("sink finishes");

    for (path, expected_type) in [
        ("code/repo.md", "code_repo"),
        ("code/modules/src.md", "code_module"),
        ("code/files/src/lib.rs.md", "code_file"),
        ("code/_architecture.md", "code_architecture"),
        ("code/_onboarding.md", "code_onboarding"),
    ] {
        let markdown = std::fs::read_to_string(out_dir.join(path)).expect("read page");
        let frontmatter = parse_yaml_frontmatter(&markdown);
        assert_eq!(
            frontmatter.get("type").and_then(serde_yaml::Value::as_str),
            Some(expected_type),
            "{path} type"
        );
        assert_eq!(
            frontmatter
                .get("ai_route")
                .and_then(serde_yaml::Value::as_str),
            Some("daemon"),
            "{path} route"
        );
        assert_eq!(
            frontmatter
                .get("ai_fallback")
                .and_then(serde_yaml::Value::as_bool),
            Some(false),
            "{path} fallback"
        );
        assert_eq!(
            frontmatter
                .get("ai_generation_status")
                .and_then(serde_yaml::Value::as_str),
            Some("generated"),
            "{path} status"
        );
    }
}

fn parse_yaml_frontmatter(markdown: &str) -> serde_yaml::Value {
    let yaml = markdown
        .strip_prefix("---\n")
        .and_then(|body| body.split_once("\n---\n"))
        .map(|(yaml, _)| yaml)
        .expect("frontmatter block");
    serde_yaml::from_str(yaml).expect("parse frontmatter")
}
