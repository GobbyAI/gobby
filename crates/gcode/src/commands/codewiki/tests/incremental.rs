use super::support::*;
use super::*;

fn generate_docs_for_scope(input: &CodewikiInput, doc_scope: &DocPruneScope) -> Vec<BuiltDoc> {
    let mut docs = Vec::new();
    let mut generate = None::<&mut TextGenerator<'_>>;
    let mut progress = CodewikiProgress::silent();
    generate_hierarchical_docs_core(
        input,
        None,
        None,
        None,
        None,
        &mut generate,
        &mut None,
        AiDepth::Symbols,
        &mut None,
        &mut progress,
        doc_scope,
        &mut |doc| {
            docs.push(doc);
            Ok(())
        },
    )
    .expect("generate docs for scope");
    docs
}

#[test]
fn incremental_write_always_rewrites_docs_without_provenance() {
    let project = tempfile::tempdir().expect("project dir");
    std::fs::create_dir_all(project.path().join("src")).expect("source dir");
    std::fs::write(project.path().join("src/lib.rs"), "pub struct Client;\n").expect("write lib");
    let out_dir = project.path().join("codewiki");

    let provenance_doc =
        "---\ntitle: Lib\nprovenance:\n- file: src/lib.rs\n  ranges:\n  - '1'\n---\n# Lib\n"
            .to_string();
    let first = vec![
        ("code/_special.md".to_string(), "# Special v1\n".to_string()),
        (
            "code/files/src/lib.rs.md".to_string(),
            provenance_doc.clone(),
        ),
    ];
    write_incremental_doc_set(project.path(), &out_dir, &first).expect("first write");

    let second = vec![
        ("code/_special.md".to_string(), "# Special v2\n".to_string()),
        ("code/files/src/lib.rs.md".to_string(), provenance_doc),
    ];
    let written =
        write_incremental_doc_set(project.path(), &out_dir, &second).expect("second write");

    // No provenance => always rewritten; matching non-empty hashes => preserved.
    assert_eq!(written, vec!["code/_special.md".to_string()]);
    let special =
        std::fs::read_to_string(out_dir.join("code/_special.md")).expect("special content");
    assert!(special.contains("Special v2"));
}

#[test]
fn degraded_doc_is_rewritten_once_generation_succeeds() {
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
    let file_doc = "code/files/src/lib.rs.md".to_string();
    let build = |generator: Option<&mut TextGenerator<'_>>| {
        let mut progress = CodewikiProgress::silent();
        generate_hierarchical_docs_with_progress(&input, generator, AiDepth::Symbols, &mut progress)
    };

    // Run 1: every generation fails, so the docs land degraded.
    let mut failing = |_prompt: &str, _system: &str, _tier: PromptTier| None;
    let degraded_docs = build(Some(&mut failing));
    write_incremental_doc_set_with_snapshot(
        project.path(),
        &out_dir,
        &degraded_docs,
        None,
        "symbols",
        DocPruneScope::unscoped(),
    )
    .expect("degraded write");

    // Run 2: generation succeeds and sources are unchanged — the recorded
    // degradation must force a rewrite where hash equality alone would skip.
    let mut succeeding = |_prompt: &str, _system: &str, _tier: PromptTier| {
        Some("Healthy generated prose.".to_string())
    };
    let healthy_docs = build(Some(&mut succeeding));
    let repaired = write_incremental_doc_set_with_snapshot(
        project.path(),
        &out_dir,
        &healthy_docs,
        None,
        "symbols",
        DocPruneScope::unscoped(),
    )
    .expect("repair write");
    assert!(repaired.contains(&file_doc), "degraded doc is repaired");
    let on_disk = std::fs::read_to_string(out_dir.join(&file_doc)).expect("repaired content");
    assert!(on_disk.contains("Healthy generated prose."));

    // Run 3: healthy and unchanged — skipped again.
    let skipped = write_incremental_doc_set_with_snapshot(
        project.path(),
        &out_dir,
        &healthy_docs,
        None,
        "symbols",
        DocPruneScope::unscoped(),
    )
    .expect("healthy rewrite");
    assert!(!skipped.contains(&file_doc), "healthy unchanged doc skips");

    // Run 4: a later failed run must not displace healthy prose for
    // unchanged sources.
    let mut failing_again = |_prompt: &str, _system: &str, _tier: PromptTier| None;
    let degraded_again = build(Some(&mut failing_again));
    let preserved = write_incremental_doc_set_with_snapshot(
        project.path(),
        &out_dir,
        &degraded_again,
        None,
        "symbols",
        DocPruneScope::unscoped(),
    )
    .expect("failed rerun write");
    assert!(!preserved.contains(&file_doc), "healthy doc is preserved");
    let on_disk = std::fs::read_to_string(out_dir.join(&file_doc)).expect("preserved content");
    assert!(on_disk.contains("Healthy generated prose."));
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
    // _hotspots.md carries no provenance frontmatter, so it is always
    // rewritten (empty source-hash sets cannot prove the doc unchanged).
    // Docs are listed in build order — leaves before the aggregates that
    // consume them — because each one is persisted as soon as it is built.
    assert_eq!(
        changed_written,
        vec![
            "code/files/src/lib.rs.md".to_string(),
            "code/modules/src.md".to_string(),
            "code/concepts/index.md".to_string(),
            "code/concepts/src.md".to_string(),
            "code/narrative/01-introduction.md".to_string(),
            "code/narrative/02-architecture.md".to_string(),
            "code/narrative/03-indexing-pipeline.md".to_string(),
            "code/narrative/04-search-rrf.md".to_string(),
            "code/narrative/05-codewiki-generation.md".to_string(),
            "code/narrative/06-gwiki-vault.md".to_string(),
            "code/narrative/07-graph-vector-storage.md".to_string(),
            "code/narrative/08-cli-contracts.md".to_string(),
            "code/narrative/09-failure-modes.md".to_string(),
            "code/narrative/10-contributor-guide.md".to_string(),
            "code/repo.md".to_string(),
            "code/_architecture.md".to_string(),
            "code/_onboarding.md".to_string(),
            "code/_hotspots.md".to_string()
        ]
    );
    let meta = std::fs::read_to_string(out_dir.join("_meta/codewiki.json")).expect("read meta log");
    let meta: serde_json::Value = serde_json::from_str(&meta).expect("parse meta log");
    let generated_docs = meta["generated_docs"].as_array().expect("generated docs");
    assert_eq!(
        generated_docs,
        &vec![
            serde_json::Value::String("code/files/src/lib.rs.md".to_string()),
            serde_json::Value::String("code/modules/src.md".to_string()),
            serde_json::Value::String("code/concepts/index.md".to_string()),
            serde_json::Value::String("code/concepts/src.md".to_string()),
            serde_json::Value::String("code/narrative/01-introduction.md".to_string()),
            serde_json::Value::String("code/narrative/02-architecture.md".to_string()),
            serde_json::Value::String("code/narrative/03-indexing-pipeline.md".to_string()),
            serde_json::Value::String("code/narrative/04-search-rrf.md".to_string()),
            serde_json::Value::String("code/narrative/05-codewiki-generation.md".to_string()),
            serde_json::Value::String("code/narrative/06-gwiki-vault.md".to_string()),
            serde_json::Value::String("code/narrative/07-graph-vector-storage.md".to_string()),
            serde_json::Value::String("code/narrative/08-cli-contracts.md".to_string()),
            serde_json::Value::String("code/narrative/09-failure-modes.md".to_string()),
            serde_json::Value::String("code/narrative/10-contributor-guide.md".to_string()),
            serde_json::Value::String("code/repo.md".to_string()),
            serde_json::Value::String("code/_architecture.md".to_string()),
            serde_json::Value::String("code/_onboarding.md".to_string()),
            serde_json::Value::String("code/_hotspots.md".to_string())
        ]
    );

    let reduced_input = CodewikiInput {
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
fn scoped_incremental_write_preserves_out_of_scope_docs_and_meta() {
    let project = tempfile::tempdir().expect("project tempdir");
    std::fs::create_dir_all(project.path().join("src")).expect("source dir");
    std::fs::create_dir_all(project.path().join("tools")).expect("tools dir");
    std::fs::write(project.path().join("src/lib.rs"), "pub struct Client;\n").expect("write lib");
    std::fs::write(project.path().join("src/old.rs"), "pub struct OldClient;\n")
        .expect("write old");
    std::fs::write(
        project.path().join("tools/helper.rs"),
        "pub fn helper() {}\n",
    )
    .expect("write helper");
    let out_dir = project.path().join("codewiki");

    let input = CodewikiInput {
        leading_chunks: std::collections::BTreeMap::new(),
        files: vec![
            "src/lib.rs".to_string(),
            "src/old.rs".to_string(),
            "tools/helper.rs".to_string(),
        ],
        graph_edges: Vec::new(),
        graph_availability: CodewikiGraphAvailability::Available,
        symbols: vec![
            test_symbol("src/lib.rs", "Client", "class", 1, "pub struct Client;"),
            test_symbol(
                "src/old.rs",
                "OldClient",
                "class",
                1,
                "pub struct OldClient;",
            ),
            test_symbol(
                "tools/helper.rs",
                "helper",
                "function",
                1,
                "pub fn helper()",
            ),
        ],
    };
    let mut first_docs = generate_hierarchical_docs(&input, None)
        .into_iter()
        .map(|(path, content)| BuiltDoc::healthy(path, content))
        .collect::<Vec<_>>();
    first_docs.push(BuiltDoc::healthy(
        "code/_changes.md",
        "changes before scoped run\n".to_string(),
    ));
    first_docs.push(BuiltDoc::healthy(
        "code/_ownership.md",
        "ownership before scoped run\n".to_string(),
    ));
    write_incremental_doc_set_with_snapshot(
        project.path(),
        &out_dir,
        &first_docs,
        None,
        "off",
        DocPruneScope::unscoped(),
    )
    .expect("first write");

    let out_of_scope_file_doc = out_dir.join("code/files/tools/helper.rs.md");
    let out_of_scope_module_doc = out_dir.join("code/modules/tools.md");
    let stale_in_scope_file_doc = out_dir.join("code/files/src/old.rs.md");
    assert!(out_of_scope_file_doc.exists());
    assert!(out_of_scope_module_doc.exists());
    assert!(stale_in_scope_file_doc.exists());
    let global_paths = [
        "code/repo.md",
        "code/_architecture.md",
        "code/_onboarding.md",
        "code/_hotspots.md",
        "code/_changes.md",
        "code/_ownership.md",
    ];
    let global_before = global_paths
        .iter()
        .map(|path| {
            (
                *path,
                std::fs::read_to_string(out_dir.join(path)).expect("global doc before"),
            )
        })
        .collect::<Vec<_>>();

    let scoped_input = CodewikiInput {
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
    let doc_scope = DocPruneScope::from_scopes(&["src".to_string()]);
    let scoped_docs = generate_docs_for_scope(&scoped_input, &doc_scope);
    write_incremental_doc_set_with_snapshot(
        project.path(),
        &out_dir,
        &scoped_docs,
        None,
        "off",
        doc_scope,
    )
    .expect("scoped write");

    assert!(out_of_scope_file_doc.exists());
    assert!(out_of_scope_module_doc.exists());
    assert!(!stale_in_scope_file_doc.exists());
    for (path, before) in global_before {
        let after = std::fs::read_to_string(out_dir.join(path)).expect("global doc after");
        assert_eq!(after, before, "{path} changed during scoped write");
    }
    let meta = std::fs::read_to_string(out_dir.join("_meta/codewiki.json")).expect("read meta");
    let meta: serde_json::Value = serde_json::from_str(&meta).expect("parse meta");
    assert!(meta["docs"].get("code/files/tools/helper.rs.md").is_some());
    assert!(meta["docs"].get("code/modules/tools.md").is_some());
    assert!(meta["docs"].get("code/files/src/old.rs.md").is_none());
    for path in global_paths {
        assert!(meta["docs"].get(path).is_some(), "{path} meta was pruned");
    }
    let generated_docs = meta["generated_docs"].as_array().expect("generated docs");
    for path in global_paths {
        assert!(
            !generated_docs.contains(&serde_json::Value::String(path.to_string())),
            "{path} was regenerated during scoped write"
        );
    }
}

#[test]
fn scoped_incremental_write_preserves_partial_ancestor_module() {
    let project = tempfile::tempdir().expect("project tempdir");
    std::fs::create_dir_all(project.path().join("src/nested")).expect("source dirs");
    std::fs::write(
        project.path().join("src/sibling.rs"),
        "pub struct Sibling;\n",
    )
    .expect("write sibling");
    std::fs::write(
        project.path().join("src/nested/leaf.rs"),
        "pub fn leaf() {}\n",
    )
    .expect("write leaf");
    let out_dir = project.path().join("codewiki");

    let input = CodewikiInput {
        leading_chunks: std::collections::BTreeMap::new(),
        files: vec![
            "src/sibling.rs".to_string(),
            "src/nested/leaf.rs".to_string(),
        ],
        graph_edges: Vec::new(),
        graph_availability: CodewikiGraphAvailability::Available,
        symbols: vec![
            test_symbol(
                "src/sibling.rs",
                "Sibling",
                "class",
                1,
                "pub struct Sibling;",
            ),
            test_symbol("src/nested/leaf.rs", "leaf", "function", 1, "pub fn leaf()"),
        ],
    };
    let first_docs = generate_hierarchical_docs(&input, None)
        .into_iter()
        .map(|(path, content)| BuiltDoc::healthy(path, content))
        .collect::<Vec<_>>();
    let snapshot = build_codewiki_index_snapshot(project.path(), &input).expect("snapshot");
    write_incremental_doc_set_with_snapshot(
        project.path(),
        &out_dir,
        &first_docs,
        Some(snapshot),
        "off",
        DocPruneScope::unscoped(),
    )
    .expect("first write");

    let ancestor_module_path = out_dir.join("code/modules/src.md");
    let ancestor_before = std::fs::read_to_string(&ancestor_module_path).expect("ancestor before");
    assert!(ancestor_before.contains("src/sibling.rs"));

    std::fs::write(
        project.path().join("src/nested/leaf.rs"),
        "pub fn leaf() {}\npub fn changed() {}\n",
    )
    .expect("modify leaf");
    let scoped_input = CodewikiInput {
        leading_chunks: std::collections::BTreeMap::new(),
        files: vec!["src/nested/leaf.rs".to_string()],
        graph_edges: Vec::new(),
        graph_availability: CodewikiGraphAvailability::Available,
        symbols: vec![test_symbol(
            "src/nested/leaf.rs",
            "leaf",
            "function",
            1,
            "pub fn leaf()",
        )],
    };
    let doc_scope = DocPruneScope::from_scopes(&["src/nested".to_string()]);
    let scoped_docs = generate_docs_for_scope(&scoped_input, &doc_scope);
    assert!(
        scoped_docs
            .iter()
            .any(|doc| doc.path == "code/modules/src/nested.md")
    );
    assert!(
        !scoped_docs
            .iter()
            .any(|doc| doc.path == "code/modules/src.md")
    );

    let changed_paths = write_incremental_doc_set_with_snapshot(
        project.path(),
        &out_dir,
        &scoped_docs,
        None,
        "off",
        doc_scope,
    )
    .expect("scoped write");
    assert!(!changed_paths.contains(&"code/modules/src.md".to_string()));

    let ancestor_after = std::fs::read_to_string(&ancestor_module_path).expect("ancestor after");
    assert_eq!(ancestor_after, ancestor_before);
    assert!(ancestor_after.contains("src/sibling.rs"));

    let meta = std::fs::read_to_string(out_dir.join("_meta/codewiki.json")).expect("read meta");
    let meta: serde_json::Value = serde_json::from_str(&meta).expect("parse meta");
    assert!(meta["docs"].get("code/modules/src.md").is_some());
    assert!(
        meta["index_snapshot"]["files"]
            .get("src/sibling.rs")
            .is_some(),
        "scoped write must preserve the previous full index snapshot"
    );
    let generated_docs = meta["generated_docs"].as_array().expect("generated docs");
    assert!(
        !generated_docs.contains(&serde_json::Value::String(
            "code/modules/src.md".to_string()
        )),
        "ancestor module was regenerated from partial scoped input"
    );
}
