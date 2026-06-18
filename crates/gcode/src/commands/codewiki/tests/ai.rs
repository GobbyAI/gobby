use gobby_core::ai_types::AiError;

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
    for aggregate in [
        prompts::FILE_SYSTEM,
        prompts::MODULE_SYSTEM,
        prompts::REPO_SYSTEM,
        prompts::ARCHITECTURE_SYSTEM,
    ] {
        let seen = tier_for(aggregate);
        assert!(!seen.is_empty(), "{aggregate} generates");
        assert!(
            seen.iter().all(|tier| *tier == PromptTier::Aggregate),
            "{aggregate} routes to the aggregate tier"
        );
    }
    // File pages now synthesize a multi-section narrative, so the file system
    // prompt joins the aggregate tier; only per-symbol purposes stay standard.
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

    write_incremental_doc_set_with_snapshot(
        project.path(),
        &out_dir,
        &docs,
        None,
        "symbols",
        DocPruneScope::unscoped(),
    )
    .expect("write docs");
    let meta = std::fs::read_to_string(out_dir.join("_meta/codewiki.json")).expect("read meta");
    let meta: serde_json::Value = serde_json::from_str(&meta).expect("parse meta");
    for path in ["code/repo.md", "code/modules/src.md"] {
        assert_eq!(
            meta["docs"][path]["degraded"],
            serde_json::Value::Bool(true),
            "{path} degradation lands in _meta/codewiki.json"
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
