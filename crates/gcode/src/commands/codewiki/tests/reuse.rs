use gobby_core::config::AiRouting;

use super::support::*;
use super::*;

fn reuse_project() -> (tempfile::TempDir, CodewikiInput) {
    let project = tempfile::tempdir().expect("project tempdir");
    std::fs::create_dir_all(project.path().join("src/nested")).expect("source dirs");
    std::fs::write(project.path().join("src/lib.rs"), "pub struct Client;\n").expect("write lib");
    std::fs::write(
        project.path().join("src/nested/api.rs"),
        "pub fn serve() {}\n",
    )
    .expect("write api");
    let input = CodewikiInput {
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
    };
    (project, input)
}

#[test]
fn unchanged_sources_are_reused_without_any_generation_call() {
    let (project, input) = reuse_project();
    let out_dir = project.path().join("codewiki");

    let mut first_generator = |_prompt: &str, system: &str, _tier: PromptTier| {
        if system == prompts::CURATED_NAVIGATION_SYSTEM {
            Some(test_curated_navigation_json())
        } else if system == prompts::CONCEPT_PAGE_SYSTEM {
            Some(test_concept_handbook_body())
        } else if system == prompts::NARRATIVE_PAGE_SYSTEM {
            Some(test_narrative_handbook_body())
        } else {
            Some("Generated prose.".to_string())
        }
    };
    let mut progress = CodewikiProgress::silent();
    let first = collect_docs(
        &input,
        GenerateDocsOptions {
            generate: Some(&mut first_generator),
            ai_depth: AiDepth::Symbols,
            progress: Some(&mut progress),
            ..Default::default()
        },
    );
    write_incremental_doc_set_with_snapshot(
        project.path(),
        &out_dir,
        &first,
        None,
        "symbols",
        DocPruneScope::unscoped(),
    )
    .expect("first write");

    let mut calls = 0_usize;
    let mut counting_generator = |_prompt: &str, _system: &str, _tier: PromptTier| {
        calls += 1;
        Some("Second-run prose.".to_string())
    };
    let mut plan = ReusePlan::load(project.path(), &out_dir, "symbols").expect("reuse plan loads");
    let reuse = Some(&mut plan);
    let mut progress = CodewikiProgress::silent();
    let second = collect_docs(
        &input,
        GenerateDocsOptions {
            generate: Some(&mut counting_generator),
            ai_depth: AiDepth::Symbols,
            reuse,
            progress: Some(&mut progress),
            ..Default::default()
        },
    );
    assert_eq!(calls, 0, "unchanged sources must make zero LLM calls");

    // Reused docs carry the on-disk pages verbatim, so a rewrite is lossless.
    let repo = second
        .iter()
        .find(|doc| doc.path == "code/repo.md")
        .expect("repo doc is emitted");
    let on_disk = std::fs::read_to_string(out_dir.join("code/repo.md")).expect("repo on disk");
    assert_eq!(repo.content, on_disk);
    assert!(repo.content.contains("Generated prose."));

    let changed = write_incremental_doc_set_with_snapshot(
        project.path(),
        &out_dir,
        &second,
        None,
        "symbols",
        DocPruneScope::unscoped(),
    )
    .expect("second write");
    assert!(
        changed.iter().all(|path| {
            !path.starts_with("code/files/")
                && !path.starts_with("code/modules/")
                && path != "code/repo.md"
                && path != "code/_architecture.md"
        }),
        "reused docs must not be rewritten: {changed:?}"
    );
}

#[test]
fn reconciled_stage_reuses_unchanged_page_and_bypasses_since_hash_shortcut() {
    let (project, input) = reuse_project();
    let live_out = project.path().join("codewiki");
    let ai_settings = AiGenerationSettings::default();
    let ai_outcome = CodewikiAiOutcome::default();
    let snapshot = CodewikiIndexSnapshot::default();
    let initial_fingerprint = PublicationFingerprint::from_run(
        project.path(),
        &input.files,
        "symbols",
        &ai_settings,
        ai_outcome,
        ai_outcome,
        &[],
        None,
        &snapshot,
    )
    .expect("initial fingerprint");
    let first_publication =
        CodewikiPublication::prepare(&live_out, &initial_fingerprint).expect("prepare first stage");
    let stage_out = first_publication.stage_out().to_path_buf();

    let mut first_generator = |_prompt: &str, system: &str, _tier: PromptTier| {
        if system == prompts::CURATED_NAVIGATION_SYSTEM {
            Some(test_curated_navigation_json())
        } else if system == prompts::CONCEPT_PAGE_SYSTEM {
            Some(test_concept_handbook_body())
        } else if system == prompts::NARRATIVE_PAGE_SYSTEM {
            Some(test_narrative_handbook_body())
        } else {
            Some("Initial generated prose.".to_string())
        }
    };
    let mut sink = DocSink::open(project.path(), &stage_out, "symbols").expect("first sink");
    generate_hierarchical_docs(
        &input,
        GenerateDocsOptions {
            generate: Some(&mut first_generator),
            ai_depth: AiDepth::Symbols,
            ..Default::default()
        },
        &mut |doc| sink.persist(&doc).map(|_| ()),
    )
    .expect("generate initial stage");
    sink.finish(None).expect("finish initial stage");

    let changed_page_path = stage_out.join("code/files/src/lib.rs.md");
    let unchanged_page_path = stage_out.join("code/files/src/nested/api.rs.md");
    let changed_page_before = std::fs::read(&changed_page_path).expect("changed page before");
    let unchanged_page_before = std::fs::read(&unchanged_page_path).expect("unchanged page before");
    drop(first_publication);

    std::fs::write(
        project.path().join("src/lib.rs"),
        "pub struct Client { pub id: u64 }\n",
    )
    .expect("change source");
    let since_changed = Some(std::collections::BTreeSet::new());
    let changed_fingerprint = PublicationFingerprint::from_run(
        project.path(),
        &input.files,
        "symbols",
        &ai_settings,
        ai_outcome,
        ai_outcome,
        &[],
        since_changed.as_ref(),
        &snapshot,
    )
    .expect("changed fingerprint");
    let publication = CodewikiPublication::prepare(&live_out, &changed_fingerprint)
        .expect("reconcile changed stage");
    assert!(publication.requires_full_hash_scan());
    assert_eq!(
        std::fs::read(&changed_page_path).expect("changed page after reconcile"),
        changed_page_before,
        "reconciliation must retain the changed page's staged bytes before reuse checks"
    );
    assert_eq!(
        std::fs::read(&unchanged_page_path).expect("unchanged page after reconcile"),
        unchanged_page_before,
        "reconciliation must retain the unchanged page's staged bytes"
    );

    let effective_since = if publication.requires_full_hash_scan() {
        None
    } else {
        since_changed
    };
    let mut systems = Vec::new();
    let mut second_generator = |_prompt: &str, system: &str, _tier: PromptTier| {
        systems.push(system.to_string());
        if system == prompts::CURATED_NAVIGATION_SYSTEM {
            Some(test_curated_navigation_json())
        } else if system == prompts::CONCEPT_PAGE_SYSTEM {
            Some(test_concept_handbook_body())
        } else if system == prompts::NARRATIVE_PAGE_SYSTEM {
            Some(test_narrative_handbook_body())
        } else {
            Some("Regenerated prose.".to_string())
        }
    };
    let mut reuse = ReusePlan::load_with_since(
        project.path(),
        publication.stage_out(),
        "symbols",
        effective_since.clone(),
    )
    .expect("load reconciled reuse plan");
    let mut sink = DocSink::open(project.path(), publication.stage_out(), "symbols")
        .expect("reconciled sink")
        .with_since(effective_since);
    generate_hierarchical_docs(
        &input,
        GenerateDocsOptions {
            generate: Some(&mut second_generator),
            ai_depth: AiDepth::Symbols,
            reuse: Some(&mut reuse),
            ..Default::default()
        },
        &mut |doc| sink.persist(&doc).map(|_| ()),
    )
    .expect("generate reconciled stage");
    sink.finish(None).expect("finish reconciled stage");

    assert_eq!(
        systems
            .iter()
            .filter(|system| system.as_str() == prompts::SYMBOL_SYSTEM)
            .count(),
        1,
        "only the changed source's symbol should regenerate"
    );
    assert_eq!(
        systems
            .iter()
            .filter(|system| system.as_str() == prompts::FILE_SYSTEM)
            .count(),
        1,
        "only the changed source's file page should regenerate"
    );
    assert_ne!(
        std::fs::read(&changed_page_path).expect("changed page after generation"),
        changed_page_before,
        "full hashing must regenerate a source changed outside the --since set"
    );
    assert_eq!(
        std::fs::read(&unchanged_page_path).expect("unchanged page after generation"),
        unchanged_page_before,
        "the unchanged page must reuse its staged bytes without an LLM call"
    );
}

#[test]
fn stale_render_version_disables_reuse() {
    let (project, input) = reuse_project();
    let out_dir = project.path().join("codewiki");

    let mut first_generator = |_prompt: &str, system: &str, _tier: PromptTier| {
        if system == prompts::CURATED_NAVIGATION_SYSTEM {
            Some(test_curated_navigation_json())
        } else if system == prompts::CONCEPT_PAGE_SYSTEM {
            Some(test_concept_handbook_body())
        } else if system == prompts::NARRATIVE_PAGE_SYSTEM {
            Some(test_narrative_handbook_body())
        } else {
            Some("Generated prose.".to_string())
        }
    };
    let mut progress = CodewikiProgress::silent();
    let first = collect_docs(
        &input,
        GenerateDocsOptions {
            generate: Some(&mut first_generator),
            ai_depth: AiDepth::Symbols,
            progress: Some(&mut progress),
            ..Default::default()
        },
    );
    write_incremental_doc_set_with_snapshot(
        project.path(),
        &out_dir,
        &first,
        None,
        "symbols",
        DocPruneScope::unscoped(),
    )
    .expect("first write");

    let meta_path = out_dir.join("_meta/codewiki.json");
    let raw_meta = std::fs::read_to_string(&meta_path).expect("read meta");
    let mut meta: serde_json::Value = serde_json::from_str(&raw_meta).expect("parse meta");
    for entry in meta["docs"]
        .as_object_mut()
        .expect("docs object")
        .values_mut()
    {
        entry["render_version"] = serde_json::json!(1);
    }
    std::fs::write(
        &meta_path,
        format!(
            "{}\n",
            serde_json::to_string_pretty(&meta).expect("serialize meta")
        ),
    )
    .expect("write stale meta");

    let mut calls = 0_usize;
    let mut second_generator = |_prompt: &str, system: &str, _tier: PromptTier| {
        calls += 1;
        if system == prompts::CURATED_NAVIGATION_SYSTEM {
            Some(test_curated_navigation_json())
        } else if system == prompts::CONCEPT_PAGE_SYSTEM {
            Some(test_concept_handbook_body())
        } else if system == prompts::NARRATIVE_PAGE_SYSTEM {
            Some(test_narrative_handbook_body())
        } else {
            Some("Regenerated prose.".to_string())
        }
    };
    let mut plan = ReusePlan::load(project.path(), &out_dir, "symbols").expect("reuse plan loads");
    let reuse = Some(&mut plan);
    let mut progress = CodewikiProgress::silent();
    let second = collect_docs(
        &input,
        GenerateDocsOptions {
            generate: Some(&mut second_generator),
            ai_depth: AiDepth::Symbols,
            reuse,
            progress: Some(&mut progress),
            ..Default::default()
        },
    );

    assert!(calls > 0, "stale render metadata must not reuse old pages");
    assert!(
        second
            .iter()
            .any(|doc| doc.path == "code/repo.md" && doc.content.contains("Regenerated prose."))
    );
}

#[test]
fn per_category_render_version_isolates_invalidation() {
    let (project, input) = reuse_project();
    let out_dir = project.path().join("codewiki");

    let mut first_generator = |_prompt: &str, system: &str, _tier: PromptTier| {
        if system == prompts::CURATED_NAVIGATION_SYSTEM {
            Some(test_curated_navigation_json())
        } else if system == prompts::CONCEPT_PAGE_SYSTEM {
            Some(test_concept_handbook_body())
        } else if system == prompts::NARRATIVE_PAGE_SYSTEM {
            Some(test_narrative_handbook_body())
        } else {
            Some("Generated prose.".to_string())
        }
    };
    let mut progress = CodewikiProgress::silent();
    let first = collect_docs(
        &input,
        GenerateDocsOptions {
            generate: Some(&mut first_generator),
            ai_depth: AiDepth::Symbols,
            progress: Some(&mut progress),
            ..Default::default()
        },
    );
    write_incremental_doc_set_with_snapshot(
        project.path(),
        &out_dir,
        &first,
        None,
        "symbols",
        DocPruneScope::unscoped(),
    )
    .expect("first write");

    // Stale only the architecture page's render version. Every other category
    // keeps version 20 and must reuse; only code/_architecture.md regenerates.
    let meta_path = out_dir.join("_meta/codewiki.json");
    let raw_meta = std::fs::read_to_string(&meta_path).expect("read meta");
    let mut meta: serde_json::Value = serde_json::from_str(&raw_meta).expect("parse meta");
    if let Some(arch) = meta["docs"]
        .as_object_mut()
        .expect("docs object")
        .get_mut("code/_architecture.md")
    {
        arch["render_version"] = serde_json::json!(1);
    }
    std::fs::write(
        &meta_path,
        format!(
            "{}\n",
            serde_json::to_string_pretty(&meta).expect("serialize meta")
        ),
    )
    .expect("write stale meta");

    let mut regenerated_paths = Vec::new();
    let mut second_generator = |_prompt: &str, system: &str, _tier: PromptTier| {
        if system == prompts::CURATED_NAVIGATION_SYSTEM {
            Some(test_curated_navigation_json())
        } else if system == prompts::CONCEPT_PAGE_SYSTEM {
            Some(test_concept_handbook_body())
        } else if system == prompts::NARRATIVE_PAGE_SYSTEM {
            Some(test_narrative_handbook_body())
        } else {
            Some("Regenerated prose.".to_string())
        }
    };
    let mut plan = ReusePlan::load(project.path(), &out_dir, "symbols").expect("reuse plan loads");
    let reuse = Some(&mut plan);
    let mut progress = CodewikiProgress::silent();
    let second = collect_docs(
        &input,
        GenerateDocsOptions {
            generate: Some(&mut second_generator),
            ai_depth: AiDepth::Symbols,
            reuse,
            progress: Some(&mut progress),
            ..Default::default()
        },
    );

    // Collect paths whose content changed (regenerated, not reused).
    for doc in &second {
        let prev = first.iter().find(|d| d.path == doc.path);
        if prev.is_none_or(|p| p.content != doc.content) {
            regenerated_paths.push(doc.path.as_str());
        }
    }

    // Architecture must regenerate.
    assert!(
        regenerated_paths.contains(&"code/_architecture.md"),
        "architecture page must regenerate when its render version is stale, got: {regenerated_paths:?}"
    );

    // File docs and module docs must NOT regenerate — their render versions are
    // still valid.
    let file_or_module_regen = regenerated_paths
        .iter()
        .any(|p| p.starts_with("code/files/") || p.starts_with("code/modules/"));
    assert!(
        !file_or_module_regen,
        "file/module pages must reuse when only architecture render version is stale, regenerated: {regenerated_paths:?}"
    );

    // Curated pages must NOT regenerate.
    let curated_regen = regenerated_paths
        .iter()
        .any(|p| p.starts_with("code/concepts/") || p.starts_with("code/narrative/"));
    assert!(
        !curated_regen,
        "curated pages must reuse when only architecture render version is stale, regenerated: {regenerated_paths:?}"
    );
}

#[test]
fn reused_docs_feed_recorded_summaries_into_parent_prompts() {
    let (project, input) = reuse_project();
    let out_dir = project.path().join("codewiki");

    let mut first_generator = |prompt: &str, system: &str, _tier: PromptTier| {
        if system == prompts::CURATED_NAVIGATION_SYSTEM {
            Some(test_curated_navigation_json())
        } else if system == prompts::CONCEPT_PAGE_SYSTEM {
            Some(test_concept_handbook_body())
        } else if system == prompts::NARRATIVE_PAGE_SYSTEM {
            Some(test_narrative_handbook_body())
        } else if system == prompts::MODULE_SYSTEM && prompt.contains("src/nested") {
            Some("Nested module marker prose.".to_string())
        } else {
            Some("Generated prose.".to_string())
        }
    };
    let mut progress = CodewikiProgress::silent();
    let first = collect_docs(
        &input,
        GenerateDocsOptions {
            generate: Some(&mut first_generator),
            ai_depth: AiDepth::Sections,
            progress: Some(&mut progress),
            ..Default::default()
        },
    );
    write_incremental_doc_set_with_snapshot(
        project.path(),
        &out_dir,
        &first,
        None,
        "sections",
        DocPruneScope::unscoped(),
    )
    .expect("first write");

    std::fs::write(
        project.path().join("src/lib.rs"),
        "pub struct Client;\npub fn connect() {}\n",
    )
    .expect("modify lib");

    let mut module_prompts = Vec::new();
    let mut second_generator = |prompt: &str, system: &str, _tier: PromptTier| {
        if system == prompts::MODULE_SYSTEM {
            module_prompts.push(prompt.to_string());
        }
        Some("Regenerated prose.".to_string())
    };
    let mut plan = ReusePlan::load(project.path(), &out_dir, "sections").expect("reuse plan loads");
    let reuse = Some(&mut plan);
    let mut progress = CodewikiProgress::silent();
    let second = collect_docs(
        &input,
        GenerateDocsOptions {
            generate: Some(&mut second_generator),
            ai_depth: AiDepth::Sections,
            reuse,
            progress: Some(&mut progress),
            ..Default::default()
        },
    );
    assert!(!second.is_empty());

    // Only the module containing the changed file regenerates, and its prompt
    // is fed the unchanged sibling's recorded summary instead of a fresh call.
    assert_eq!(
        module_prompts.len(),
        1,
        "unchanged src/nested must not regenerate: {module_prompts:#?}"
    );
    assert!(module_prompts[0].contains("Nested module marker prose."));

    let changed = write_incremental_doc_set_with_snapshot(
        project.path(),
        &out_dir,
        &second,
        None,
        "sections",
        DocPruneScope::unscoped(),
    )
    .expect("second write");
    assert!(changed.contains(&"code/files/src/lib.rs.md".to_string()));
    assert!(changed.contains(&"code/modules/src.md".to_string()));
    assert!(changed.contains(&"code/repo.md".to_string()));
    assert!(!changed.contains(&"code/files/src/nested/api.rs.md".to_string()));
    assert!(!changed.contains(&"code/modules/src/nested.md".to_string()));
}

#[test]
fn degraded_docs_are_never_reused() {
    let (project, input) = reuse_project();
    let out_dir = project.path().join("codewiki");

    let mut failing_generator = |_prompt: &str, _system: &str, _tier: PromptTier| None;
    let mut progress = CodewikiProgress::silent();
    let degraded = collect_docs(
        &input,
        GenerateDocsOptions {
            generate: Some(&mut failing_generator),
            ai_depth: AiDepth::Sections,
            progress: Some(&mut progress),
            ..Default::default()
        },
    );
    write_incremental_doc_set_with_snapshot(
        project.path(),
        &out_dir,
        &degraded,
        None,
        "sections",
        DocPruneScope::unscoped(),
    )
    .expect("degraded write");

    let mut calls = 0_usize;
    let mut repairing_generator = |_prompt: &str, _system: &str, _tier: PromptTier| {
        calls += 1;
        Some("Repaired prose.".to_string())
    };
    let mut plan = ReusePlan::load(project.path(), &out_dir, "sections").expect("reuse plan loads");
    let reuse = Some(&mut plan);
    let mut progress = CodewikiProgress::silent();
    let repaired = collect_docs(
        &input,
        GenerateDocsOptions {
            generate: Some(&mut repairing_generator),
            ai_depth: AiDepth::Sections,
            reuse,
            progress: Some(&mut progress),
            ..Default::default()
        },
    );
    assert!(calls > 0, "degraded docs must regenerate, not reuse");

    let changed = write_incremental_doc_set_with_snapshot(
        project.path(),
        &out_dir,
        &repaired,
        None,
        "sections",
        DocPruneScope::unscoped(),
    )
    .expect("repair write");
    assert!(changed.contains(&"code/modules/src.md".to_string()));
    let on_disk =
        std::fs::read_to_string(out_dir.join("code/modules/src.md")).expect("repaired module");
    assert!(on_disk.contains("Repaired prose."));
}

#[test]
fn reusable_pages_are_rewritten_after_strict_normalization_without_regeneration() {
    let (project, input) = reuse_project();
    let out_dir = project.path().join("codewiki");

    let mut first_generator = |_prompt: &str, system: &str, _tier: PromptTier| {
        if system == prompts::CURATED_NAVIGATION_SYSTEM {
            Some(test_curated_navigation_json())
        } else if system == prompts::CONCEPT_PAGE_SYSTEM {
            Some(test_concept_handbook_body())
        } else if system == prompts::NARRATIVE_PAGE_SYSTEM {
            Some(test_narrative_handbook_body())
        } else {
            Some("Generated prose.".to_string())
        }
    };
    let mut sink = DocSink::open(project.path(), &out_dir, "symbols").expect("sink opens");
    let mut emit = |doc: BuiltDoc| -> anyhow::Result<()> {
        sink.persist(&doc)?;
        Ok(())
    };
    generate_hierarchical_docs(
        &input,
        GenerateDocsOptions {
            generate: Some(&mut first_generator),
            ..Default::default()
        },
        &mut emit,
    )
    .expect("first run");
    sink.finish(None).expect("first run completes");

    let page_path = out_dir.join("code/files/src/lib.rs.md");
    let original = std::fs::read_to_string(&page_path).expect("read normalized page");
    let stale = format!("{original}\n<details>\n<summary>Source</summary>\n\nold\n</details>\n");
    std::fs::write(&page_path, stale).expect("plant old-format reusable page");

    let mut systems = Vec::new();
    let mut second_generator = |_prompt: &str, system: &str, _tier: PromptTier| {
        systems.push(system.to_string());
        Some("Unexpected fresh generation.".to_string())
    };
    let mut plan = ReusePlan::load(project.path(), &out_dir, "symbols").expect("reuse plan loads");
    let mut sink = DocSink::open(project.path(), &out_dir, "symbols").expect("sink reopens");
    let mut emit = |doc: BuiltDoc| -> anyhow::Result<()> {
        sink.persist(&doc)?;
        Ok(())
    };
    generate_hierarchical_docs(
        &input,
        GenerateDocsOptions {
            generate: Some(&mut second_generator),
            reuse: Some(&mut plan),
            ..Default::default()
        },
        &mut emit,
    )
    .expect("second run");
    let changed = sink.finish(None).expect("second run completes");

    assert!(changed.contains(&"code/files/src/lib.rs.md".to_string()));
    assert_eq!(
        std::fs::read_to_string(&page_path).expect("read refreshed page"),
        original
    );
    assert!(
        !systems
            .iter()
            .any(|system| system == prompts::SYMBOL_SYSTEM),
        "normalization refresh must not regenerate symbols: {systems:#?}"
    );
    assert!(!systems.iter().any(|system| system == prompts::FILE_SYSTEM));
}

#[test]
fn interrupted_run_resumes_from_persisted_docs() {
    let (project, input) = reuse_project();
    let out_dir = project.path().join("codewiki");

    // Run 1 dies before any module doc lands: every file doc must already be
    // on disk with a matching meta entry, because the sink flushes per doc.
    let mut first_generator = |_prompt: &str, system: &str, _tier: PromptTier| {
        if system == prompts::CURATED_NAVIGATION_SYSTEM {
            Some(test_curated_navigation_json())
        } else if system == prompts::CONCEPT_PAGE_SYSTEM {
            Some(test_concept_handbook_body())
        } else if system == prompts::NARRATIVE_PAGE_SYSTEM {
            Some(test_narrative_handbook_body())
        } else {
            Some("Generated prose.".to_string())
        }
    };
    let mut sink = DocSink::open(project.path(), &out_dir, "symbols").expect("sink opens");
    let mut emit = |doc: BuiltDoc| -> anyhow::Result<()> {
        if doc.path.starts_with("code/modules/") {
            anyhow::bail!("simulated kill before module docs");
        }
        sink.persist(&doc)?;
        Ok(())
    };
    let interrupted = generate_hierarchical_docs(
        &input,
        GenerateDocsOptions {
            generate: Some(&mut first_generator),
            ..Default::default()
        },
        &mut emit,
    );
    assert!(interrupted.is_err(), "simulated kill propagates");

    assert!(out_dir.join("code/files/src/lib.rs.md").exists());
    assert!(out_dir.join("code/files/src/nested/api.rs.md").exists());
    assert!(!out_dir.join("code/modules/src.md").exists());
    let meta = std::fs::read_to_string(out_dir.join("_meta/codewiki.json")).expect("interim meta");
    let meta: serde_json::Value = serde_json::from_str(&meta).expect("parse interim meta");
    assert!(meta["docs"].get("code/files/src/lib.rs.md").is_some());
    assert!(meta["docs"].get("code/modules/src.md").is_none());

    // A killed run's process exit releases the per-out_dir writer lock
    // (#17732); dropping the interrupted sink stands in for that here.
    drop(sink);

    // Run 2 resumes: persisted file docs are reused without symbol or file
    // generation calls, and only the missing aggregates are generated.
    let mut systems = Vec::new();
    let mut second_generator = |_prompt: &str, system: &str, _tier: PromptTier| {
        systems.push(system.to_string());
        Some("Recovered prose.".to_string())
    };
    let mut plan = ReusePlan::load(project.path(), &out_dir, "symbols").expect("reuse plan loads");
    let mut sink = DocSink::open(project.path(), &out_dir, "symbols").expect("sink reopens");
    let mut emit = |doc: BuiltDoc| -> anyhow::Result<()> {
        sink.persist(&doc)?;
        Ok(())
    };
    generate_hierarchical_docs(
        &input,
        GenerateDocsOptions {
            generate: Some(&mut second_generator),
            reuse: Some(&mut plan),
            ..Default::default()
        },
        &mut emit,
    )
    .expect("resumed run");
    let changed = sink.finish(None).expect("resumed run completes");

    assert!(
        !systems.iter().any(|s| s == prompts::SYMBOL_SYSTEM),
        "persisted file docs must not regenerate symbols: {systems:#?}"
    );
    assert!(!systems.iter().any(|s| s == prompts::FILE_SYSTEM));
    assert!(systems.iter().any(|s| s == prompts::MODULE_SYSTEM));
    assert!(systems.iter().any(|s| s == prompts::REPO_SYSTEM));
    assert!(changed.contains(&"code/modules/src.md".to_string()));
    assert!(changed.contains(&"code/repo.md".to_string()));
    assert!(!changed.contains(&"code/files/src/lib.rs.md".to_string()));
}

#[test]
fn metas_without_recorded_summaries_rewrite_once_to_backfill() {
    let (project, input) = reuse_project();
    let out_dir = project.path().join("codewiki");

    // Simulate a meta written before summaries existed (#681): same pages on
    // disk, healthy entries, but nothing recorded to reuse.
    let mut first_generator = |_prompt: &str, system: &str, _tier: PromptTier| {
        if system == prompts::CURATED_NAVIGATION_SYSTEM {
            Some(test_curated_navigation_json())
        } else if system == prompts::CONCEPT_PAGE_SYSTEM {
            Some(test_concept_handbook_body())
        } else if system == prompts::NARRATIVE_PAGE_SYSTEM {
            Some(test_narrative_handbook_body())
        } else {
            Some("Generated prose.".to_string())
        }
    };
    let mut progress = CodewikiProgress::silent();
    let mut first = collect_docs(
        &input,
        GenerateDocsOptions {
            generate: Some(&mut first_generator),
            ai_depth: AiDepth::Sections,
            progress: Some(&mut progress),
            ..Default::default()
        },
    );
    for doc in &mut first {
        doc.summary = None;
    }
    write_incremental_doc_set_with_snapshot(
        project.path(),
        &out_dir,
        &first,
        None,
        "sections",
        DocPruneScope::unscoped(),
    )
    .expect("legacy-shaped write");

    let mut calls = 0_usize;
    let mut second_generator = |_prompt: &str, system: &str, _tier: PromptTier| {
        calls += 1;
        if system == prompts::CURATED_NAVIGATION_SYSTEM {
            Some(test_curated_navigation_json())
        } else if system == prompts::CONCEPT_PAGE_SYSTEM {
            Some(test_concept_handbook_body())
        } else if system == prompts::NARRATIVE_PAGE_SYSTEM {
            Some(test_narrative_handbook_body())
        } else {
            Some("Backfilled prose.".to_string())
        }
    };
    let mut plan = ReusePlan::load(project.path(), &out_dir, "sections").expect("reuse plan loads");
    let reuse = Some(&mut plan);
    let mut progress = CodewikiProgress::silent();
    let second = collect_docs(
        &input,
        GenerateDocsOptions {
            generate: Some(&mut second_generator),
            ai_depth: AiDepth::Sections,
            reuse,
            progress: Some(&mut progress),
            ..Default::default()
        },
    );
    assert!(calls > 0, "missing summaries cannot be reused");
    let changed = write_incremental_doc_set_with_snapshot(
        project.path(),
        &out_dir,
        &second,
        None,
        "sections",
        DocPruneScope::unscoped(),
    )
    .expect("backfill write");
    // Summary-carrying docs rewrite once so the recorded summary matches the
    // page on disk; from then on the run is fully reusable.
    assert!(changed.contains(&"code/modules/src.md".to_string()));

    let mut third_calls = 0_usize;
    let mut third_generator = |_prompt: &str, _system: &str, _tier: PromptTier| {
        third_calls += 1;
        Some("Third prose.".to_string())
    };
    let mut plan =
        ReusePlan::load(project.path(), &out_dir, "sections").expect("reuse plan reloads");
    let reuse = Some(&mut plan);
    let mut progress = CodewikiProgress::silent();
    let third = collect_docs(
        &input,
        GenerateDocsOptions {
            generate: Some(&mut third_generator),
            ai_depth: AiDepth::Sections,
            reuse,
            progress: Some(&mut progress),
            ..Default::default()
        },
    );
    assert!(!third.is_empty());
    assert_eq!(third_calls, 0, "backfilled metas are fully reusable");
}

#[test]
fn missing_page_on_disk_regenerates_that_doc() {
    let (project, input) = reuse_project();
    let out_dir = project.path().join("codewiki");

    let mut first_generator = |_prompt: &str, system: &str, _tier: PromptTier| {
        if system == prompts::CURATED_NAVIGATION_SYSTEM {
            Some(test_curated_navigation_json())
        } else if system == prompts::CONCEPT_PAGE_SYSTEM {
            Some(test_concept_handbook_body())
        } else if system == prompts::NARRATIVE_PAGE_SYSTEM {
            Some(test_narrative_handbook_body())
        } else {
            Some("Generated prose.".to_string())
        }
    };
    let mut progress = CodewikiProgress::silent();
    let first = collect_docs(
        &input,
        GenerateDocsOptions {
            generate: Some(&mut first_generator),
            ai_depth: AiDepth::Sections,
            progress: Some(&mut progress),
            ..Default::default()
        },
    );
    write_incremental_doc_set_with_snapshot(
        project.path(),
        &out_dir,
        &first,
        None,
        "sections",
        DocPruneScope::unscoped(),
    )
    .expect("first write");

    std::fs::remove_file(out_dir.join("code/modules/src/nested.md")).expect("drop module page");

    let mut module_prompts = Vec::new();
    let mut second_generator = |prompt: &str, system: &str, _tier: PromptTier| {
        if system == prompts::MODULE_SYSTEM {
            module_prompts.push(prompt.to_string());
        }
        Some("Restored prose.".to_string())
    };
    let mut plan = ReusePlan::load(project.path(), &out_dir, "sections").expect("reuse plan loads");
    let reuse = Some(&mut plan);
    let mut progress = CodewikiProgress::silent();
    let second = collect_docs(
        &input,
        GenerateDocsOptions {
            generate: Some(&mut second_generator),
            ai_depth: AiDepth::Sections,
            reuse,
            progress: Some(&mut progress),
            ..Default::default()
        },
    );
    assert_eq!(
        module_prompts.len(),
        1,
        "only the deleted page regenerates: {module_prompts:#?}"
    );
    let changed = write_incremental_doc_set_with_snapshot(
        project.path(),
        &out_dir,
        &second,
        None,
        "sections",
        DocPruneScope::unscoped(),
    )
    .expect("second write");
    assert!(changed.contains(&"code/modules/src/nested.md".to_string()));
    let restored = std::fs::read_to_string(out_dir.join("code/modules/src/nested.md"))
        .expect("restored module page");
    assert!(restored.contains("Restored prose."));
}

#[test]
fn finish_reclaims_on_disk_orphans_absent_from_a_cleared_cache() {
    // A churned narrative slug left on disk after the meta log was deleted to
    // force a clean run must still be reclaimed by `finish` — even though the
    // cache never listed it, so the cache-only prune could never see it (#900).
    let (project, _input) = reuse_project();
    let out_dir = project.path().join("codewiki");

    // Plant a stale page on disk with NO meta entry: exactly the state left by
    // `rm _meta/codewiki.json` before a regen.
    write_doc(
        &out_dir,
        "code/narrative/from-files-to-code-facts.md",
        "stale orphan",
    )
    .expect("plant orphan");
    // A sibling vault file outside the codewiki-owned `code/` tree (e.g. the
    // gwiki research notes) must never be walked or deleted.
    std::fs::create_dir_all(out_dir.join("research")).expect("research dir");
    std::fs::write(out_dir.join("research/notes.md"), "user note").expect("plant vault note");

    // A completed run that produces one healthy page and nothing else.
    let mut sink = DocSink::open(project.path(), &out_dir, "symbols").expect("sink opens");
    sink.persist(&BuiltDoc::healthy(
        "code/narrative/01-introduction.md",
        "fresh chapter".to_string(),
    ))
    .expect("persist fresh page");
    sink.finish(None).expect("run completes");

    assert!(
        !out_dir
            .join("code/narrative/from-files-to-code-facts.md")
            .exists(),
        "cache-independent GC must reclaim the on-disk orphan"
    );
    assert!(
        out_dir.join("code/narrative/01-introduction.md").exists(),
        "the freshly produced page must survive"
    );
    assert!(
        out_dir.join("research/notes.md").exists(),
        "GC must not walk or delete outside the `code/` tree"
    );
}

#[test]
fn aggregate_settings_apply_only_to_aggregate_writer_pages() {
    let settings = AiGenerationSettings {
        prose_depth: "deep".to_string(),
        register: "newcomer".to_string(),
        aggregate_profile: "feature-high".to_string(),
        aggregate_candidates: vec!["claude/sonnet@xhigh".to_string()],
    };
    // Aggregate-writer pages carry the full settings.
    for path in [
        "code/repo.md",
        "code/_architecture.md",
        "code/concepts/index.md",
        "code/narrative/01-introduction.md",
    ] {
        assert_eq!(settings.for_path(path), settings, "{path}");
    }
    // Every other page carries only the run-wide prose depth and register, so
    // aggregate flag changes never invalidate it.
    for path in [
        "code/files/src/lib.rs.md",
        "code/modules/src.md",
        "code/infrastructure.md",
        "code/features.md",
    ] {
        let projected = settings.for_path(path);
        assert_eq!(projected.prose_depth, "deep", "{path}");
        assert_eq!(projected.register, "newcomer", "{path}");
        assert!(projected.aggregate_profile.is_empty(), "{path}");
        assert!(projected.aggregate_candidates.is_empty(), "{path}");
    }
}

/// Like `write_incremental_doc_set_with_snapshot` but recording the run's
/// requested AI generation settings into each doc's meta (#17530).
fn write_docs_with_ai_settings(
    project_root: &std::path::Path,
    out_dir: &std::path::Path,
    docs: &[BuiltDoc],
    ai_mode: &str,
    ai_settings: AiGenerationSettings,
) -> Vec<String> {
    let mut sink = DocSink::open(project_root, out_dir, ai_mode)
        .expect("sink opens")
        .with_ai_settings(ai_settings);
    for doc in docs {
        sink.persist(doc).expect("doc persists");
    }
    sink.finish(None).expect("run completes")
}

#[test]
fn aggregate_profile_change_regenerates_only_aggregate_pages() {
    let (project, input) = reuse_project();
    let out_dir = project.path().join("codewiki");

    let mut first_generator = |_prompt: &str, system: &str, _tier: PromptTier| {
        if system == prompts::CURATED_NAVIGATION_SYSTEM {
            Some(test_curated_navigation_json())
        } else if system == prompts::CONCEPT_PAGE_SYSTEM {
            Some(test_concept_handbook_body())
        } else if system == prompts::NARRATIVE_PAGE_SYSTEM {
            Some(test_narrative_handbook_body())
        } else {
            Some("Generated prose.".to_string())
        }
    };
    let mut progress = CodewikiProgress::silent();
    let first = collect_docs(
        &input,
        GenerateDocsOptions {
            generate: Some(&mut first_generator),
            ai_depth: AiDepth::Symbols,
            progress: Some(&mut progress),
            ..Default::default()
        },
    );
    write_docs_with_ai_settings(
        project.path(),
        &out_dir,
        &first,
        "symbols",
        AiGenerationSettings {
            aggregate_profile: "feature-high".to_string(),
            ..AiGenerationSettings::default()
        },
    );

    // Same sources, different aggregate writer: only aggregate-tier prompts
    // may run — file/module pages must reuse (the bakeoff-arm contract).
    let mut tiers = Vec::new();
    let mut second_generator = |_prompt: &str, system: &str, tier: PromptTier| {
        tiers.push(tier);
        if system == prompts::CURATED_NAVIGATION_SYSTEM {
            Some(test_curated_navigation_json())
        } else if system == prompts::CONCEPT_PAGE_SYSTEM {
            Some(test_concept_handbook_body())
        } else if system == prompts::NARRATIVE_PAGE_SYSTEM {
            Some(test_narrative_handbook_body())
        } else {
            Some("Re-voiced prose.".to_string())
        }
    };
    let repinned = AiGenerationSettings {
        aggregate_profile: "opus-first".to_string(),
        ..AiGenerationSettings::default()
    };
    let mut plan = ReusePlan::load(project.path(), &out_dir, "symbols")
        .expect("reuse plan loads")
        .with_ai_settings(repinned.clone());
    let reuse = Some(&mut plan);
    let mut progress = CodewikiProgress::silent();
    let second = collect_docs(
        &input,
        GenerateDocsOptions {
            generate: Some(&mut second_generator),
            ai_depth: AiDepth::Symbols,
            reuse,
            progress: Some(&mut progress),
            ..Default::default()
        },
    );
    assert!(!second.is_empty());
    assert!(
        !tiers.is_empty(),
        "an aggregate profile change must regenerate the aggregate pages"
    );
    assert!(
        tiers.iter().all(|tier| *tier == PromptTier::Aggregate),
        "file/module pages must reuse across an aggregate profile change: {tiers:?}"
    );

    let changed =
        write_docs_with_ai_settings(project.path(), &out_dir, &second, "symbols", repinned);
    assert!(changed.contains(&"code/repo.md".to_string()));
    assert!(changed.contains(&"code/_architecture.md".to_string()));
    assert!(
        changed
            .iter()
            .all(|path| { !path.starts_with("code/files/") && !path.starts_with("code/modules/") }),
        "reused file/module pages must not be rewritten: {changed:?}"
    );
}

#[test]
fn aggregate_candidate_change_regenerates_only_aggregate_pages() {
    let (project, input) = reuse_project();
    let out_dir = project.path().join("codewiki");

    let mut first_generator = |_prompt: &str, system: &str, _tier: PromptTier| {
        if system == prompts::CURATED_NAVIGATION_SYSTEM {
            Some(test_curated_navigation_json())
        } else if system == prompts::CONCEPT_PAGE_SYSTEM {
            Some(test_concept_handbook_body())
        } else if system == prompts::NARRATIVE_PAGE_SYSTEM {
            Some(test_narrative_handbook_body())
        } else {
            Some("Generated prose.".to_string())
        }
    };
    let mut progress = CodewikiProgress::silent();
    let first = collect_docs(
        &input,
        GenerateDocsOptions {
            generate: Some(&mut first_generator),
            ai_depth: AiDepth::Symbols,
            progress: Some(&mut progress),
            ..Default::default()
        },
    );
    write_docs_with_ai_settings(
        project.path(),
        &out_dir,
        &first,
        "symbols",
        AiGenerationSettings {
            aggregate_candidates: vec!["claude/sonnet@xhigh".to_string()],
            ..AiGenerationSettings::default()
        },
    );

    // Simulate the bakeoff rerun with an existing output dir and a different
    // pinned candidate chain: aggregates auto-invalidate, file/module pages reuse.
    let mut tiers = Vec::new();
    let mut second_generator = |_prompt: &str, system: &str, tier: PromptTier| {
        tiers.push(tier);
        if system == prompts::CURATED_NAVIGATION_SYSTEM {
            Some(test_curated_navigation_json())
        } else if system == prompts::CONCEPT_PAGE_SYSTEM {
            Some(test_concept_handbook_body())
        } else if system == prompts::NARRATIVE_PAGE_SYSTEM {
            Some(test_narrative_handbook_body())
        } else {
            Some("Opus-voiced prose.".to_string())
        }
    };
    let repinned = AiGenerationSettings {
        aggregate_candidates: vec!["claude/opus@xhigh".to_string()],
        ..AiGenerationSettings::default()
    };
    let mut plan = ReusePlan::load(project.path(), &out_dir, "symbols")
        .expect("reuse plan loads")
        .with_ai_settings(repinned.clone());
    let reuse = Some(&mut plan);
    let mut progress = CodewikiProgress::silent();
    let second = collect_docs(
        &input,
        GenerateDocsOptions {
            generate: Some(&mut second_generator),
            ai_depth: AiDepth::Symbols,
            reuse,
            progress: Some(&mut progress),
            ..Default::default()
        },
    );
    assert!(!second.is_empty());
    assert!(
        !tiers.is_empty(),
        "a candidate chain change must regenerate the aggregate pages"
    );
    assert!(
        tiers.iter().all(|tier| *tier == PromptTier::Aggregate),
        "file/module pages must reuse across a candidate chain change: {tiers:?}"
    );

    let changed =
        write_docs_with_ai_settings(project.path(), &out_dir, &second, "symbols", repinned);
    assert!(changed.contains(&"code/repo.md".to_string()));
    assert!(changed.contains(&"code/_architecture.md".to_string()));
    assert!(
        changed
            .iter()
            .all(|path| { !path.starts_with("code/files/") && !path.starts_with("code/modules/") }),
        "reused file/module pages must not be rewritten: {changed:?}"
    );
}

#[test]
fn prose_depth_and_register_changes_regenerate_every_ai_page() {
    let (project, input) = reuse_project();
    let out_dir = project.path().join("codewiki");

    let mut first_generator = |_prompt: &str, system: &str, _tier: PromptTier| {
        if system == prompts::CURATED_NAVIGATION_SYSTEM {
            Some(test_curated_navigation_json())
        } else if system == prompts::CONCEPT_PAGE_SYSTEM {
            Some(test_concept_handbook_body())
        } else if system == prompts::NARRATIVE_PAGE_SYSTEM {
            Some(test_narrative_handbook_body())
        } else {
            Some("Generated prose.".to_string())
        }
    };
    let mut progress = CodewikiProgress::silent();
    let first = collect_docs(
        &input,
        GenerateDocsOptions {
            generate: Some(&mut first_generator),
            ai_depth: AiDepth::Symbols,
            progress: Some(&mut progress),
            ..Default::default()
        },
    );
    // Default-settings write: the meta records no depth/register, exactly like
    // meta written before settings were recorded.
    write_incremental_doc_set_with_snapshot(
        project.path(),
        &out_dir,
        &first,
        None,
        "symbols",
        DocPruneScope::unscoped(),
    )
    .expect("first write");

    // Depth shapes every AI page's budget, so a change regenerates file and
    // module pages too — unlike the aggregate-only settings above.
    let mut deep_calls = 0_usize;
    let mut deep_generator = |_prompt: &str, system: &str, _tier: PromptTier| {
        deep_calls += 1;
        if system == prompts::CURATED_NAVIGATION_SYSTEM {
            Some(test_curated_navigation_json())
        } else if system == prompts::CONCEPT_PAGE_SYSTEM {
            Some(test_concept_handbook_body())
        } else if system == prompts::NARRATIVE_PAGE_SYSTEM {
            Some(test_narrative_handbook_body())
        } else {
            Some("Deepened prose.".to_string())
        }
    };
    let deep = AiGenerationSettings {
        prose_depth: "deep".to_string(),
        ..AiGenerationSettings::default()
    };
    let mut plan = ReusePlan::load(project.path(), &out_dir, "symbols")
        .expect("reuse plan loads")
        .with_ai_settings(deep.clone());
    let reuse = Some(&mut plan);
    let mut progress = CodewikiProgress::silent();
    let deepened = collect_docs(
        &input,
        GenerateDocsOptions {
            generate: Some(&mut deep_generator),
            ai_depth: AiDepth::Symbols,
            reuse,
            progress: Some(&mut progress),
            ..Default::default()
        },
    );
    assert!(deep_calls > 0, "a prose-depth change must regenerate");
    let changed =
        write_docs_with_ai_settings(project.path(), &out_dir, &deepened, "symbols", deep.clone());
    assert!(changed.contains(&"code/files/src/lib.rs.md".to_string()));
    assert!(changed.contains(&"code/modules/src.md".to_string()));
    assert!(changed.contains(&"code/repo.md".to_string()));

    // Same depth, new register: the recorded depth now matches, so only the
    // register difference drives the second full regeneration.
    let mut register_calls = 0_usize;
    let mut register_generator = |_prompt: &str, system: &str, _tier: PromptTier| {
        register_calls += 1;
        if system == prompts::CURATED_NAVIGATION_SYSTEM {
            Some(test_curated_navigation_json())
        } else if system == prompts::CONCEPT_PAGE_SYSTEM {
            Some(test_concept_handbook_body())
        } else if system == prompts::NARRATIVE_PAGE_SYSTEM {
            Some(test_narrative_handbook_body())
        } else {
            Some("Newcomer prose.".to_string())
        }
    };
    let deep_newcomer = AiGenerationSettings {
        register: "newcomer".to_string(),
        ..deep
    };
    let mut plan = ReusePlan::load(project.path(), &out_dir, "symbols")
        .expect("reuse plan reloads")
        .with_ai_settings(deep_newcomer.clone());
    let reuse = Some(&mut plan);
    let mut progress = CodewikiProgress::silent();
    let revoiced = collect_docs(
        &input,
        GenerateDocsOptions {
            generate: Some(&mut register_generator),
            ai_depth: AiDepth::Symbols,
            reuse,
            progress: Some(&mut progress),
            ..Default::default()
        },
    );
    assert!(register_calls > 0, "a register change must regenerate");
    let changed = write_docs_with_ai_settings(
        project.path(),
        &out_dir,
        &revoiced,
        "symbols",
        deep_newcomer,
    );
    assert!(changed.contains(&"code/files/src/lib.rs.md".to_string()));
    assert!(changed.contains(&"code/modules/src.md".to_string()));
    assert!(changed.contains(&"code/repo.md".to_string()));
}

#[test]
fn unchanged_generation_settings_still_reuse_without_llm_calls() {
    let (project, input) = reuse_project();
    let out_dir = project.path().join("codewiki");

    let mut first_generator = |_prompt: &str, system: &str, _tier: PromptTier| {
        if system == prompts::CURATED_NAVIGATION_SYSTEM {
            Some(test_curated_navigation_json())
        } else if system == prompts::CONCEPT_PAGE_SYSTEM {
            Some(test_concept_handbook_body())
        } else if system == prompts::NARRATIVE_PAGE_SYSTEM {
            Some(test_narrative_handbook_body())
        } else {
            Some("Generated prose.".to_string())
        }
    };
    let mut progress = CodewikiProgress::silent();
    let first = collect_docs(
        &input,
        GenerateDocsOptions {
            generate: Some(&mut first_generator),
            ai_depth: AiDepth::Symbols,
            progress: Some(&mut progress),
            ..Default::default()
        },
    );
    let pinned = AiGenerationSettings {
        prose_depth: "deep".to_string(),
        register: "maintainer".to_string(),
        aggregate_candidates: vec!["claude/sonnet@xhigh".to_string()],
        ..AiGenerationSettings::default()
    };
    write_docs_with_ai_settings(project.path(), &out_dir, &first, "symbols", pinned.clone());

    let mut calls = 0_usize;
    let mut counting_generator = |_prompt: &str, _system: &str, _tier: PromptTier| {
        calls += 1;
        Some("Second-run prose.".to_string())
    };
    let mut plan = ReusePlan::load(project.path(), &out_dir, "symbols")
        .expect("reuse plan loads")
        .with_ai_settings(pinned);
    let reuse = Some(&mut plan);
    let mut progress = CodewikiProgress::silent();
    let second = collect_docs(
        &input,
        GenerateDocsOptions {
            generate: Some(&mut counting_generator),
            ai_depth: AiDepth::Symbols,
            reuse,
            progress: Some(&mut progress),
            ..Default::default()
        },
    );
    assert!(!second.is_empty());
    assert_eq!(calls, 0, "unchanged settings must make zero LLM calls");
}

#[test]
fn keyed_aggregate_page_reuse_honors_generation_settings() {
    let (project, _input) = reuse_project();
    let out_dir = project.path().join("codewiki");

    // A keyed derived page (architecture with a SystemModel digest) bypasses
    // source-hash reuse entirely — the settings comparison must still apply.
    let pinned = AiGenerationSettings {
        aggregate_candidates: vec!["claude/sonnet@xhigh".to_string()],
        ..AiGenerationSettings::default()
    };
    let mut sink = DocSink::open(project.path(), &out_dir, "symbols")
        .expect("sink opens")
        .with_ai_settings(pinned.clone());
    sink.persist(&BuiltDoc::derived(
        "code/_architecture.md",
        "# Architecture\n\nPinned narrative.\n".to_string(),
        "model-digest".to_string(),
    ))
    .expect("keyed page persists");
    sink.finish(None).expect("run completes");

    let mut same = ReusePlan::load(project.path(), &out_dir, "symbols")
        .expect("reuse plan loads")
        .with_ai_settings(pinned);
    let outcome = same.ai_outcome();
    assert!(
        same.reusable_page_keyed_with_ai_outcome("code/_architecture.md", "model-digest", outcome)
            .is_some(),
        "unchanged settings must reuse the keyed page"
    );

    let repinned = AiGenerationSettings {
        aggregate_candidates: vec!["claude/opus@xhigh".to_string()],
        ..AiGenerationSettings::default()
    };
    let mut changed = ReusePlan::load(project.path(), &out_dir, "symbols")
        .expect("reuse plan reloads")
        .with_ai_settings(repinned);
    let outcome = changed.ai_outcome();
    assert!(
        changed
            .reusable_page_keyed_with_ai_outcome("code/_architecture.md", "model-digest", outcome)
            .is_none(),
        "a candidate change must invalidate the keyed page even when its digest matches"
    );
}

/// Project whose call graph chains three files across sibling subdirectories
/// into one synthetic cross-directory cluster: `src/db/ids.rs` calls
/// `src/graph/write.rs` calls `src/graph/sync_plan.rs`. A direct `src/main.rs`
/// keeps `src` as a single subsystem root so the chain merges. Cluster name
/// derives from member stems: `src/ids_plan` with all three, `src/ids_write`
/// once `sync_plan.rs` leaves.
fn cluster_rename_project() -> (tempfile::TempDir, CodewikiInput) {
    let project = tempfile::tempdir().expect("project tempdir");
    std::fs::create_dir_all(project.path().join("src/db")).expect("db dir");
    std::fs::create_dir_all(project.path().join("src/graph")).expect("graph dir");
    for (path, content) in [
        ("src/main.rs", "fn main() {}\n"),
        ("src/db/ids.rs", "pub fn ids() {}\n"),
        ("src/graph/write.rs", "pub fn write() {}\n"),
        ("src/graph/sync_plan.rs", "pub fn sync_plan() {}\n"),
    ] {
        std::fs::write(project.path().join(path), content).expect("write source");
    }
    let symbols = vec![
        test_symbol("src/main.rs", "main", "function", 1, "fn main()"),
        test_symbol("src/db/ids.rs", "ids", "function", 1, "pub fn ids()"),
        test_symbol(
            "src/graph/write.rs",
            "write",
            "function",
            1,
            "pub fn write()",
        ),
        test_symbol(
            "src/graph/sync_plan.rs",
            "sync_plan",
            "function",
            1,
            "pub fn sync_plan()",
        ),
    ];
    let input = CodewikiInput {
        leading_chunks: std::collections::BTreeMap::new(),
        files: vec![
            "src/main.rs".to_string(),
            "src/db/ids.rs".to_string(),
            "src/graph/write.rs".to_string(),
            "src/graph/sync_plan.rs".to_string(),
        ],
        graph_edges: vec![
            CodewikiGraphEdge::call(
                test_component_id("src/db/ids.rs", "ids", "function"),
                test_component_id("src/graph/write.rs", "write", "function"),
            ),
            CodewikiGraphEdge::call(
                test_component_id("src/graph/write.rs", "write", "function"),
                test_component_id("src/graph/sync_plan.rs", "sync_plan", "function"),
            ),
        ],
        graph_availability: CodewikiGraphAvailability::Available,
        symbols,
    };
    (project, input)
}

/// Every `[[code/modules/...]]` wikilink in the emitted doc set must target an
/// emitted module page — the invariant #17731 saw broken by verbatim reuse.
fn assert_module_links_resolve(docs: &[BuiltDoc]) {
    let paths = docs
        .iter()
        .map(|doc| doc.path.as_str())
        .collect::<std::collections::BTreeSet<_>>();
    for doc in docs {
        let mut rest = doc.content.as_str();
        while let Some(start) = rest.find("[[code/modules/") {
            let after = &rest[start + 2..];
            let end = after.find(['|', ']']).unwrap_or(after.len());
            let target = &after[..end];
            assert!(
                paths.contains(format!("{target}.md").as_str()),
                "doc {} links module page {target} that this run does not emit",
                doc.path
            );
            rest = &after[end..];
        }
    }
}

#[test]
fn cluster_dissolve_restamps_module_links_on_reused_file_pages() {
    let (project, input) = cluster_rename_project();
    let out_dir = project.path().join("codewiki");

    let mut first_generator =
        |_prompt: &str, _system: &str, _tier: PromptTier| Some("First prose.".to_string());
    let mut progress = CodewikiProgress::silent();
    let first = collect_docs(
        &input,
        GenerateDocsOptions {
            generate: Some(&mut first_generator),
            ai_depth: AiDepth::Symbols,
            progress: Some(&mut progress),
            ..Default::default()
        },
    );
    assert!(
        first
            .iter()
            .any(|doc| doc.path == "code/modules/src/ids_plan.md"),
        "first run must emit the synthetic cluster module page"
    );
    write_incremental_doc_set_with_snapshot(
        project.path(),
        &out_dir,
        &first,
        None,
        "symbols",
        DocPruneScope::unscoped(),
    )
    .expect("first write");
    let ids_page_before = std::fs::read_to_string(out_dir.join("code/files/src/db/ids.rs.md"))
        .expect("ids.rs page on disk");
    assert!(ids_page_before.contains("code/modules/src/ids_plan|"));

    // `sync_plan.rs` is deleted: the cluster keeps `ids.rs` and `write.rs` but
    // its purpose-derived name changes. `ids.rs` (sources and neighbors
    // untouched — its only edge is to `write.rs`) stays reusable and must be
    // re-stamped, not left linking the dissolved module.
    std::fs::remove_file(project.path().join("src/graph/sync_plan.rs")).expect("delete source");
    let mut second_input = input;
    second_input
        .files
        .retain(|file| file != "src/graph/sync_plan.rs");
    second_input
        .symbols
        .retain(|symbol| symbol.file_path != "src/graph/sync_plan.rs");
    second_input.graph_edges.truncate(1);

    let mut second_generator =
        |_prompt: &str, _system: &str, _tier: PromptTier| Some("Second prose.".to_string());
    let mut plan = ReusePlan::load(project.path(), &out_dir, "symbols").expect("reuse plan loads");
    let reuse = Some(&mut plan);
    let mut progress = CodewikiProgress::silent();
    let second = collect_docs(
        &second_input,
        GenerateDocsOptions {
            generate: Some(&mut second_generator),
            ai_depth: AiDepth::Symbols,
            reuse,
            progress: Some(&mut progress),
            ..Default::default()
        },
    );

    // Reuse held (byte-identical page except the re-stamped, equal-length
    // module link) — no regeneration for the unchanged file.
    let ids_doc = second
        .iter()
        .find(|doc| doc.path == "code/files/src/db/ids.rs.md")
        .expect("ids.rs doc emitted");
    let expected = ids_page_before.replace(
        "code/modules/src/ids_plan|src/ids_plan",
        "code/modules/src/ids_write|src/ids_write",
    );
    assert_ne!(expected, ids_page_before, "link swap must apply");
    assert_eq!(ids_doc.content, expected);
    assert_module_links_resolve(&second);

    let changed = write_incremental_doc_set_with_snapshot(
        project.path(),
        &out_dir,
        &second,
        None,
        "symbols",
        DocPruneScope::unscoped(),
    )
    .expect("second write");
    assert!(changed.contains(&"code/files/src/db/ids.rs.md".to_string()));
    assert!(
        !out_dir.join("code/modules/src/ids_plan.md").exists(),
        "dissolved cluster module page must be pruned"
    );
    assert!(out_dir.join("code/modules/src/ids_write.md").exists());
    let ids_page_after = std::fs::read_to_string(out_dir.join("code/files/src/db/ids.rs.md"))
        .expect("ids.rs page after heal");
    assert!(!ids_page_after.contains("ids_plan"));
}

#[test]
fn child_cluster_rename_regenerates_parent_module_page() {
    let (project, input) = cluster_rename_project();
    let out_dir = project.path().join("codewiki");

    let mut first_generator =
        |_prompt: &str, _system: &str, _tier: PromptTier| Some("First prose.".to_string());
    let mut progress = CodewikiProgress::silent();
    let first = collect_docs(
        &input,
        GenerateDocsOptions {
            generate: Some(&mut first_generator),
            ai_depth: AiDepth::Symbols,
            progress: Some(&mut progress),
            ..Default::default()
        },
    );
    write_incremental_doc_set_with_snapshot(
        project.path(),
        &out_dir,
        &first,
        None,
        "symbols",
        DocPruneScope::unscoped(),
    )
    .expect("first write");
    let src_page_before = std::fs::read_to_string(out_dir.join("code/modules/src.md"))
        .expect("src module page on disk");
    assert!(src_page_before.contains("code/modules/src/ids_plan|"));

    // Only the call edge between `write.rs` and `sync_plan.rs` disappears: no
    // file content changes, so `src.md`'s member-file span hashes all still
    // match. The cluster splits and renames, and the parent's Child Modules
    // links are stale — hash-based reuse alone would ship them verbatim.
    let mut second_input = input;
    second_input.graph_edges.truncate(1);

    let mut second_generator =
        |_prompt: &str, _system: &str, _tier: PromptTier| Some("Second prose.".to_string());
    let mut plan = ReusePlan::load(project.path(), &out_dir, "symbols").expect("reuse plan loads");
    let reuse = Some(&mut plan);
    let mut progress = CodewikiProgress::silent();
    let second = collect_docs(
        &second_input,
        GenerateDocsOptions {
            generate: Some(&mut second_generator),
            ai_depth: AiDepth::Symbols,
            reuse,
            progress: Some(&mut progress),
            ..Default::default()
        },
    );

    let src_doc = second
        .iter()
        .find(|doc| doc.path == "code/modules/src.md")
        .expect("src module doc emitted");
    assert!(
        src_doc.content.contains("code/modules/src/ids_write|"),
        "parent page must link the renamed child cluster"
    );
    assert!(
        !src_doc.content.contains("ids_plan"),
        "parent page must not keep the stale child link"
    );
    assert_module_links_resolve(&second);

    // The regenerated parent must land on disk: its member-file span hashes
    // all match, so only the child-link invalidation key forces the write.
    write_incremental_doc_set_with_snapshot(
        project.path(),
        &out_dir,
        &second,
        None,
        "symbols",
        DocPruneScope::unscoped(),
    )
    .expect("second write");
    let src_page_after = std::fs::read_to_string(out_dir.join("code/modules/src.md"))
        .expect("src module page after heal");
    assert!(src_page_after.contains("code/modules/src/ids_write|"));
    assert!(!src_page_after.contains("ids_plan"));
    assert!(
        !out_dir.join("code/modules/src/ids_plan.md").exists(),
        "renamed cluster's old module page must be pruned"
    );
}

#[test]
fn cluster_rename_rewrites_stale_ownership_module_links() {
    let (project, input) = cluster_rename_project();
    // Declared owners make the ownership page's Modules section emit module
    // links; blame data is irrelevant to the rename scenario.
    std::fs::write(project.path().join("CODEOWNERS"), "* @gobby-owners\n").expect("codeowners");
    let out_dir = project.path().join("codewiki");

    let mut first_generator =
        |_prompt: &str, _system: &str, _tier: PromptTier| Some("First prose.".to_string());
    let mut progress = CodewikiProgress::silent();
    let mut first_meta = OwnershipMeta::default();
    let first = collect_docs(
        &input,
        GenerateDocsOptions {
            ownership: Some((project.path(), &mut first_meta)),
            generate: Some(&mut first_generator),
            ai_depth: AiDepth::Symbols,
            progress: Some(&mut progress),
            ..Default::default()
        },
    );
    write_incremental_doc_set_with_snapshot(
        project.path(),
        &out_dir,
        &first,
        None,
        "symbols",
        DocPruneScope::unscoped(),
    )
    .expect("first write");
    let ownership_before = std::fs::read_to_string(out_dir.join("code/_ownership.md"))
        .expect("ownership page on disk");
    assert!(ownership_before.contains("code/modules/src/ids_plan|"));

    // Only the write.rs -> sync_plan.rs call edge disappears: no file content
    // changes, so every provenance hash the ownership page records still
    // matches. The cluster splits and renames; only the module-link
    // invalidation key (#18190) forces the stale page to be rewritten.
    let mut second_input = input;
    second_input.graph_edges.truncate(1);

    let mut second_generator =
        |_prompt: &str, _system: &str, _tier: PromptTier| Some("Second prose.".to_string());
    let mut plan = ReusePlan::load(project.path(), &out_dir, "symbols").expect("reuse plan loads");
    let reuse = Some(&mut plan);
    let mut progress = CodewikiProgress::silent();
    let mut second_meta = OwnershipMeta::default();
    let second = collect_docs(
        &second_input,
        GenerateDocsOptions {
            ownership: Some((project.path(), &mut second_meta)),
            generate: Some(&mut second_generator),
            ai_depth: AiDepth::Symbols,
            reuse,
            progress: Some(&mut progress),
            ..Default::default()
        },
    );
    assert_module_links_resolve(&second);

    write_incremental_doc_set_with_snapshot(
        project.path(),
        &out_dir,
        &second,
        None,
        "symbols",
        DocPruneScope::unscoped(),
    )
    .expect("second write");
    let ownership_after = std::fs::read_to_string(out_dir.join("code/_ownership.md"))
        .expect("ownership page after rename");
    assert!(ownership_after.contains("code/modules/src/ids_write|"));
    assert!(
        !ownership_after.contains("ids_plan"),
        "stale ownership page retained with a dangling module link"
    );
}

/// Stamps a top-level `degraded: true` into a page's frontmatter without
/// touching `_meta/codewiki.json`, simulating manifest/page skew (#18291).
fn stamp_page_degraded(path: &std::path::Path) {
    let page = std::fs::read_to_string(path).expect("page to stamp");
    let stamped = page.replacen("---\n", "---\ndegraded: true\n", 1);
    assert_ne!(page, stamped, "no frontmatter fence in {}", path.display());
    std::fs::write(path, stamped).expect("stamp degraded frontmatter");
}

#[test]
fn on_disk_degraded_file_page_regenerates_while_healthy_pages_reuse() {
    let (project, input) = reuse_project();
    let out_dir = project.path().join("codewiki");

    let mut first_generator = |_prompt: &str, system: &str, _tier: PromptTier| {
        if system == prompts::CURATED_NAVIGATION_SYSTEM {
            Some(test_curated_navigation_json())
        } else if system == prompts::CONCEPT_PAGE_SYSTEM {
            Some(test_concept_handbook_body())
        } else if system == prompts::NARRATIVE_PAGE_SYSTEM {
            Some(test_narrative_handbook_body())
        } else {
            Some("Generated prose.".to_string())
        }
    };
    let mut progress = CodewikiProgress::silent();
    let first = collect_docs(
        &input,
        GenerateDocsOptions {
            generate: Some(&mut first_generator),
            ai_depth: AiDepth::Symbols,
            progress: Some(&mut progress),
            ..Default::default()
        },
    );
    write_incremental_doc_set_with_snapshot(
        project.path(),
        &out_dir,
        &first,
        None,
        "symbols",
        DocPruneScope::unscoped(),
    )
    .expect("first write");

    // The manifest still records the page as healthy; only the page itself
    // carries the degraded stamp (manifest/page skew, #18291).
    stamp_page_degraded(&out_dir.join("code/files/src/lib.rs.md"));

    let mut calls = 0_usize;
    let mut counting_generator = |_prompt: &str, _system: &str, _tier: PromptTier| {
        calls += 1;
        Some("Second-run prose.".to_string())
    };
    let mut plan = ReusePlan::load(project.path(), &out_dir, "symbols").expect("reuse plan loads");
    let reuse = Some(&mut plan);
    let mut progress = CodewikiProgress::silent();
    let second = collect_docs(
        &input,
        GenerateDocsOptions {
            generate: Some(&mut counting_generator),
            ai_depth: AiDepth::Symbols,
            reuse,
            progress: Some(&mut progress),
            ..Default::default()
        },
    );
    assert!(calls > 0, "the degraded page must regenerate");

    let lib_doc = second
        .iter()
        .find(|doc| doc.path == "code/files/src/lib.rs.md")
        .expect("lib file doc is emitted");
    assert!(
        lib_doc.content.contains("Second-run prose."),
        "on-disk degraded page must not satisfy reuse:\n{}",
        lib_doc.content
    );
    let api_doc = second
        .iter()
        .find(|doc| doc.path == "code/files/src/nested/api.rs.md")
        .expect("api file doc is emitted");
    assert!(
        api_doc.content.contains("Generated prose."),
        "healthy unchanged pages still reuse:\n{}",
        api_doc.content
    );

    write_incremental_doc_set_with_snapshot(
        project.path(),
        &out_dir,
        &second,
        None,
        "symbols",
        DocPruneScope::unscoped(),
    )
    .expect("second write");
    let healed = std::fs::read_to_string(out_dir.join("code/files/src/lib.rs.md"))
        .expect("lib page after regeneration");
    assert!(
        !healed.contains("\ndegraded: true"),
        "regenerated page sheds the degraded stamp:\n{healed}"
    );
}

#[test]
fn on_disk_degraded_ownership_page_regenerates_despite_healthy_meta() {
    let (project, input) = reuse_project();
    // Declared owners make the ownership page emit module links. The tempdir
    // is not a git repository, so the built page records `degraded: true`
    // (git_blame_unavailable) while its BuiltDoc — and therefore its manifest
    // entry — claims the page is healthy: exactly the manifest/page skew the
    // #17531 bakeoff hit (#18291).
    std::fs::write(project.path().join("CODEOWNERS"), "* @gobby-owners\n").expect("codeowners");
    let out_dir = project.path().join("codewiki");

    let mut generator =
        |_prompt: &str, _system: &str, _tier: PromptTier| Some("Ownership-run prose.".to_string());
    let mut progress = CodewikiProgress::silent();
    let mut first_meta = OwnershipMeta::default();
    let first = collect_docs(
        &input,
        GenerateDocsOptions {
            ownership: Some((project.path(), &mut first_meta)),
            generate: Some(&mut generator),
            ai_depth: AiDepth::Symbols,
            progress: Some(&mut progress),
            ..Default::default()
        },
    );
    write_incremental_doc_set_with_snapshot(
        project.path(),
        &out_dir,
        &first,
        None,
        "symbols",
        DocPruneScope::unscoped(),
    )
    .expect("first write");

    let on_disk = std::fs::read_to_string(out_dir.join("code/_ownership.md"))
        .expect("ownership page on disk");
    assert!(
        on_disk.contains("\ndegraded: true"),
        "harness precondition: the built ownership page is degraded:\n{on_disk}"
    );
    let meta = std::fs::read_to_string(out_dir.join("_meta/codewiki.json")).expect("read meta");
    let meta: serde_json::Value = serde_json::from_str(&meta).expect("parse meta");
    assert!(
        meta["docs"]["code/_ownership.md"].get("degraded").is_none(),
        "harness precondition: the manifest entry claims the page is healthy"
    );

    // The degraded page must never satisfy reuse, no matter what the manifest
    // entry claims — the next run rewrites it.
    let mut plan = ReusePlan::load(project.path(), &out_dir, "symbols").expect("reuse plan loads");
    let mut progress = CodewikiProgress::silent();
    let mut second_meta = OwnershipMeta::default();
    let second = collect_docs(
        &input,
        GenerateDocsOptions {
            ownership: Some((project.path(), &mut second_meta)),
            generate: Some(&mut generator),
            ai_depth: AiDepth::Symbols,
            reuse: Some(&mut plan),
            progress: Some(&mut progress),
            ..Default::default()
        },
    );
    let changed = write_incremental_doc_set_with_snapshot(
        project.path(),
        &out_dir,
        &second,
        None,
        "symbols",
        DocPruneScope::unscoped(),
    )
    .expect("second write");
    assert!(
        changed.iter().any(|path| path == "code/_ownership.md"),
        "degraded ownership page must be rewritten, not reused: {changed:?}"
    );
}

#[test]
fn outcome_degraded_page_records_degraded_meta_and_never_reuses() {
    let project = tempfile::tempdir().expect("project tempdir");
    let out_dir = project.path().join("codewiki");
    let doc = BuiltDoc::derived(
        "code/test.md",
        "---\ntitle: Test\ntype: note\n---\n\nBody.\n".to_string(),
        "key-1".to_string(),
    );
    // The builder reports the doc healthy; only the run-level write outcome
    // degrades it (e.g. model-unavailable) — the bakeoff `_ownership.md` case.
    let degraded_outcome = CodewikiAiOutcome::generated(AiRouting::Daemon, false).for_doc(true);

    let mut sink = DocSink::open(project.path(), &out_dir, "off").expect("sink opens");
    assert!(
        sink.persist_with_ai_outcome(&doc, degraded_outcome)
            .expect("first persist")
    );
    assert_eq!(sink.degraded_docs(), ["code/test.md"]);
    sink.finish(None).expect("first finish");

    let meta = std::fs::read_to_string(out_dir.join("_meta/codewiki.json")).expect("read meta");
    let meta: serde_json::Value = serde_json::from_str(&meta).expect("parse meta");
    assert_eq!(
        meta["docs"]["code/test.md"]["degraded"],
        serde_json::Value::Bool(true),
        "outcome degradation lands in _meta/codewiki.json"
    );

    // A later run under the same degraded outcome must rewrite, never reuse:
    // repairing degraded pages is the whole point of a heal re-run (#687).
    let mut sink = DocSink::open(project.path(), &out_dir, "off").expect("sink reopens");
    assert!(
        sink.persist_with_ai_outcome(&doc, degraded_outcome)
            .expect("second persist"),
        "a degraded page is regenerated, never reused"
    );
}

#[test]
fn page_frontmatter_blocks_reuse_reads_top_level_health_keys() {
    assert!(page_frontmatter_blocks_reuse(
        "---\ntitle: T\ndegraded: true\n---\n\nBody.\n"
    ));
    assert!(page_frontmatter_blocks_reuse(
        "---\ntitle: T\nai_generation_status: degraded\n---\n\nBody.\n"
    ));
    assert!(!page_frontmatter_blocks_reuse(
        "---\ntitle: T\ndegraded: false\nai_generation_status: generated\n---\n\nBody.\n"
    ));
    assert!(!page_frontmatter_blocks_reuse(
        "---\ntitle: T\nai_generation_status: skipped\n---\n\nBody.\n"
    ));
    // Nested keys belong to other structures, not the page's own health.
    assert!(!page_frontmatter_blocks_reuse(
        "---\ntitle: T\nprovenance:\n  - degraded: true\n---\n\nBody.\n"
    ));
    // No frontmatter: nothing to block on.
    assert!(!page_frontmatter_blocks_reuse("Body only.\n"));
}
