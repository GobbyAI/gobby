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
    let first = generate_hierarchical_docs_with_progress(
        &input,
        Some(&mut first_generator),
        AiDepth::Symbols,
        &mut progress,
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
    let mut reuse = Some(&mut plan);
    let mut progress = CodewikiProgress::silent();
    let second = generate_hierarchical_docs_with_reuse(
        &input,
        Some(&mut counting_generator),
        AiDepth::Symbols,
        &mut reuse,
        &mut progress,
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
    let first = generate_hierarchical_docs_with_progress(
        &input,
        Some(&mut first_generator),
        AiDepth::Symbols,
        &mut progress,
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
    let mut reuse = Some(&mut plan);
    let mut progress = CodewikiProgress::silent();
    let second = generate_hierarchical_docs_with_reuse(
        &input,
        Some(&mut second_generator),
        AiDepth::Symbols,
        &mut reuse,
        &mut progress,
    );

    assert!(calls > 0, "stale render metadata must not reuse old pages");
    assert!(
        second
            .iter()
            .any(|doc| doc.path == "code/repo.md" && doc.content.contains("Regenerated prose."))
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
    let first = generate_hierarchical_docs_with_progress(
        &input,
        Some(&mut first_generator),
        AiDepth::Sections,
        &mut progress,
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
    let mut reuse = Some(&mut plan);
    let mut progress = CodewikiProgress::silent();
    let second = generate_hierarchical_docs_with_reuse(
        &input,
        Some(&mut second_generator),
        AiDepth::Sections,
        &mut reuse,
        &mut progress,
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
    let degraded = generate_hierarchical_docs_with_progress(
        &input,
        Some(&mut failing_generator),
        AiDepth::Sections,
        &mut progress,
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
    let mut reuse = Some(&mut plan);
    let mut progress = CodewikiProgress::silent();
    let repaired = generate_hierarchical_docs_with_reuse(
        &input,
        Some(&mut repairing_generator),
        AiDepth::Sections,
        &mut reuse,
        &mut progress,
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
    let mut generate = Some::<&mut TextGenerator<'_>>(&mut first_generator);
    let mut progress = CodewikiProgress::silent();
    let mut sink = DocSink::open(project.path(), &out_dir, "symbols").expect("sink opens");
    let doc_scope = DocPruneScope::unscoped();
    let mut emit = |doc: BuiltDoc| -> anyhow::Result<()> {
        if doc.path.starts_with("code/modules/") {
            anyhow::bail!("simulated kill before module docs");
        }
        sink.persist(&doc)?;
        Ok(())
    };
    let interrupted = generate_hierarchical_docs_core(
        &input,
        None,
        None,
        None,
        None,
        &mut generate,
        &mut None,
        &mut None,
        AiDepth::Symbols,
        VerifyScope::All,
        CodewikiAiOutcome::default(),
        &mut None,
        &mut progress,
        &doc_scope,
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

    // Run 2 resumes: persisted file docs are reused without symbol or file
    // generation calls, and only the missing aggregates are generated.
    let mut systems = Vec::new();
    let mut second_generator = |_prompt: &str, system: &str, _tier: PromptTier| {
        systems.push(system.to_string());
        Some("Recovered prose.".to_string())
    };
    let mut generate = Some::<&mut TextGenerator<'_>>(&mut second_generator);
    let mut plan = ReusePlan::load(project.path(), &out_dir, "symbols").expect("reuse plan loads");
    let mut reuse = Some(&mut plan);
    let mut progress = CodewikiProgress::silent();
    let mut sink = DocSink::open(project.path(), &out_dir, "symbols").expect("sink reopens");
    let doc_scope = DocPruneScope::unscoped();
    let mut emit = |doc: BuiltDoc| -> anyhow::Result<()> {
        sink.persist(&doc)?;
        Ok(())
    };
    generate_hierarchical_docs_core(
        &input,
        None,
        None,
        None,
        None,
        &mut generate,
        &mut None,
        &mut None,
        AiDepth::Symbols,
        VerifyScope::All,
        CodewikiAiOutcome::default(),
        &mut reuse,
        &mut progress,
        &doc_scope,
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
    let mut first = generate_hierarchical_docs_with_progress(
        &input,
        Some(&mut first_generator),
        AiDepth::Sections,
        &mut progress,
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
    let mut reuse = Some(&mut plan);
    let mut progress = CodewikiProgress::silent();
    let second = generate_hierarchical_docs_with_reuse(
        &input,
        Some(&mut second_generator),
        AiDepth::Sections,
        &mut reuse,
        &mut progress,
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
    let mut reuse = Some(&mut plan);
    let mut progress = CodewikiProgress::silent();
    let third = generate_hierarchical_docs_with_reuse(
        &input,
        Some(&mut third_generator),
        AiDepth::Sections,
        &mut reuse,
        &mut progress,
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
    let first = generate_hierarchical_docs_with_progress(
        &input,
        Some(&mut first_generator),
        AiDepth::Sections,
        &mut progress,
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
    let mut reuse = Some(&mut plan);
    let mut progress = CodewikiProgress::silent();
    let second = generate_hierarchical_docs_with_reuse(
        &input,
        Some(&mut second_generator),
        AiDepth::Sections,
        &mut reuse,
        &mut progress,
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
