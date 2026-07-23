use super::support::*;
use super::*;

fn generate_docs_for_scope(input: &CodewikiInput, doc_scope: &DocPruneScope) -> Vec<BuiltDoc> {
    let mut docs = Vec::new();
    generate_hierarchical_docs(
        input,
        GenerateDocsOptions {
            doc_scope: Some(doc_scope),
            ..Default::default()
        },
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
        collect_docs(
            &input,
            GenerateDocsOptions {
                generate: generator,
                ai_depth: AiDepth::Symbols,
                progress: Some(&mut progress),
                ..Default::default()
            },
        )
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

    let first_docs = collect_doc_pairs(&input, GenerateDocsOptions::default());
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
    let changed_docs = collect_doc_pairs(&input, GenerateDocsOptions::default());
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
            "code/narrative/01-overview.md".to_string(),
            "code/narrative/02-architecture.md".to_string(),
            "code/narrative/03-capabilities.md".to_string(),
            "code/narrative/04-workflows.md".to_string(),
            "code/narrative/05-getting-started.md".to_string(),
            "code/narrative/06-operations.md".to_string(),
            "code/narrative/07-data-model.md".to_string(),
            "code/narrative/08-cli-api.md".to_string(),
            "code/narrative/09-troubleshooting.md".to_string(),
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
            serde_json::Value::String("code/narrative/01-overview.md".to_string()),
            serde_json::Value::String("code/narrative/02-architecture.md".to_string()),
            serde_json::Value::String("code/narrative/03-capabilities.md".to_string()),
            serde_json::Value::String("code/narrative/04-workflows.md".to_string()),
            serde_json::Value::String("code/narrative/05-getting-started.md".to_string()),
            serde_json::Value::String("code/narrative/06-operations.md".to_string()),
            serde_json::Value::String("code/narrative/07-data-model.md".to_string()),
            serde_json::Value::String("code/narrative/08-cli-api.md".to_string()),
            serde_json::Value::String("code/narrative/09-troubleshooting.md".to_string()),
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
    let reduced_docs = collect_doc_pairs(&reduced_input, GenerateDocsOptions::default());
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
    let mut first_docs = collect_doc_pairs(&input, GenerateDocsOptions::default())
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
    let first_docs = collect_doc_pairs(&input, GenerateDocsOptions::default())
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

fn scoped_input_for(file: &str, symbol: &str) -> CodewikiInput {
    CodewikiInput {
        leading_chunks: std::collections::BTreeMap::new(),
        files: vec![file.to_string()],
        graph_edges: Vec::new(),
        graph_availability: CodewikiGraphAvailability::Available,
        symbols: vec![test_symbol(file, symbol, "class", 1, "pub struct S;")],
    }
}

fn scoped_write(
    project_root: &std::path::Path,
    out_dir: &std::path::Path,
    file: &str,
    symbol: &str,
    scope: &str,
) -> Vec<String> {
    let doc_scope = DocPruneScope::from_scopes(&[scope.to_string()]);
    let docs = generate_docs_for_scope(&scoped_input_for(file, symbol), &doc_scope);
    write_incremental_doc_set_with_snapshot(project_root, out_dir, &docs, None, "off", doc_scope)
        .expect("scoped write")
}

#[test]
fn scoped_write_synthesizes_missing_ancestor_and_repo_stubs() {
    let project = tempfile::tempdir().expect("project tempdir");
    std::fs::create_dir_all(project.path().join("src/nested")).expect("source dirs");
    std::fs::write(
        project.path().join("src/nested/leaf.rs"),
        "pub struct Leaf;\n",
    )
    .expect("write leaf");
    let out_dir = project.path().join("codewiki");

    // A scoped run into an empty vault (#17639): the nested module page links
    // `Parent: [[code/modules/src]]` and file pages/`src` link `code/repo`,
    // none of which the scope filter lets the run generate directly.
    let changed = scoped_write(
        project.path(),
        &out_dir,
        "src/nested/leaf.rs",
        "Leaf",
        "src/nested",
    );
    assert!(changed.contains(&"code/modules/src.md".to_string()));
    assert!(changed.contains(&"code/repo.md".to_string()));

    let nested = std::fs::read_to_string(out_dir.join("code/modules/src/nested.md"))
        .expect("nested module page");
    assert!(nested.contains("Parent: [[code/modules/src|src]]"));
    assert!(!nested.contains("stub: true"));

    let ancestor =
        std::fs::read_to_string(out_dir.join("code/modules/src.md")).expect("ancestor stub");
    assert!(ancestor.contains("stub: true"));
    assert!(ancestor.contains("Parent: [[code/repo|Repository Overview]]"));
    assert!(ancestor.contains("[[code/modules/src/nested|src/nested]]"));

    let repo = std::fs::read_to_string(out_dir.join("code/repo.md")).expect("repo stub");
    assert!(repo.contains("stub: true"));
    assert!(repo.contains("# Repository Overview"));
    assert!(repo.contains("[[code/modules/src|src]]"));
    assert!(
        !repo.contains("code/narrative/"),
        "a repo stub must not link narrative chapters that only a full run generates"
    );
}

#[test]
fn ancestor_stubs_refresh_as_new_scoped_children_appear() {
    let project = tempfile::tempdir().expect("project tempdir");
    std::fs::create_dir_all(project.path().join("src/nested")).expect("nested dir");
    std::fs::create_dir_all(project.path().join("src/other")).expect("other dir");
    std::fs::write(
        project.path().join("src/nested/leaf.rs"),
        "pub struct Leaf;\n",
    )
    .expect("write leaf");
    std::fs::write(
        project.path().join("src/other/thing.rs"),
        "pub struct Thing;\n",
    )
    .expect("write thing");
    let out_dir = project.path().join("codewiki");

    scoped_write(
        project.path(),
        &out_dir,
        "src/nested/leaf.rs",
        "Leaf",
        "src/nested",
    );
    let changed = scoped_write(
        project.path(),
        &out_dir,
        "src/other/thing.rs",
        "Thing",
        "src/other",
    );
    assert!(
        changed.contains(&"code/modules/src.md".to_string()),
        "the ancestor stub must be re-synthesized when a sibling scope lands"
    );

    let ancestor =
        std::fs::read_to_string(out_dir.join("code/modules/src.md")).expect("ancestor stub");
    assert!(ancestor.contains("stub: true"));
    assert!(ancestor.contains("[[code/modules/src/nested|src/nested]]"));
    assert!(ancestor.contains("[[code/modules/src/other|src/other]]"));

    // An unchanged repeat leaves the stubs alone: same disk state, same key.
    let repeat = scoped_write(
        project.path(),
        &out_dir,
        "src/other/thing.rs",
        "Thing",
        "src/other",
    );
    assert!(!repeat.contains(&"code/modules/src.md".to_string()));
    assert!(!repeat.contains(&"code/repo.md".to_string()));
}

#[test]
fn full_run_replaces_stubs_and_scoped_runs_never_replace_real_pages() {
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

    scoped_write(
        project.path(),
        &out_dir,
        "src/nested/leaf.rs",
        "leaf",
        "src/nested",
    );
    assert!(
        std::fs::read_to_string(out_dir.join("code/modules/src.md"))
            .expect("ancestor stub")
            .contains("stub: true")
    );

    // A full run replaces the stub ancestors with real generated pages.
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
    let full_docs = collect_doc_pairs(&input, GenerateDocsOptions::default())
        .into_iter()
        .map(|(path, content)| BuiltDoc::healthy(path, content))
        .collect::<Vec<_>>();
    write_incremental_doc_set_with_snapshot(
        project.path(),
        &out_dir,
        &full_docs,
        None,
        "off",
        DocPruneScope::unscoped(),
    )
    .expect("full write");
    let ancestor =
        std::fs::read_to_string(out_dir.join("code/modules/src.md")).expect("real ancestor");
    assert!(!ancestor.contains("stub: true"));
    assert!(ancestor.contains("src/sibling.rs"));
    let repo = std::fs::read_to_string(out_dir.join("code/repo.md")).expect("real repo");
    assert!(!repo.contains("stub: true"));

    // A later scoped run must not demote the real pages back to stubs.
    let repo_before = repo.clone();
    let ancestor_before = ancestor.clone();
    scoped_write(
        project.path(),
        &out_dir,
        "src/nested/leaf.rs",
        "leaf",
        "src/nested",
    );
    assert_eq!(
        std::fs::read_to_string(out_dir.join("code/modules/src.md")).expect("ancestor after"),
        ancestor_before
    );
    assert_eq!(
        std::fs::read_to_string(out_dir.join("code/repo.md")).expect("repo after"),
        repo_before
    );
}

fn compare_meta(
    commit: Option<&str>,
    dirty: Option<bool>,
    docs: serde_json::Value,
) -> serde_json::Value {
    serde_json::json!({
        "docs": docs,
        "generated_docs": [],
        "commit": commit,
        "commit_dirty": dirty,
        "ai_mode": ""
    })
}

fn compare_doc(
    commit: Option<&str>,
    dirty: Option<bool>,
    source_hashes: serde_json::Value,
) -> serde_json::Value {
    serde_json::json!({
        "source_hashes": source_hashes,
        "commit": commit,
        "commit_dirty": dirty
    })
}

fn compare_git(root: &std::path::Path, args: &[&str]) -> std::process::Output {
    std::process::Command::new("git")
        .arg("-C")
        .arg(root)
        .args(args)
        .output()
        .expect("run git")
}

fn compare_git_ok(root: &std::path::Path, args: &[&str]) -> String {
    let output = compare_git(root, args);
    assert!(
        output.status.success(),
        "git {args:?} failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout)
        .expect("git output is UTF-8")
        .trim()
        .to_string()
}

fn committed_compare_repo(
    baseline_raw: Option<String>,
    current_raw: String,
) -> (tempfile::TempDir, String) {
    committed_compare_repo_at("_meta/codewiki.json", baseline_raw, current_raw)
}

fn committed_compare_repo_at(
    baseline_meta_path: &str,
    baseline_raw: Option<String>,
    current_raw: String,
) -> (tempfile::TempDir, String) {
    let project = tempfile::tempdir().expect("project");
    let root = project.path();
    compare_git_ok(root, &["init", "-q"]);
    compare_git_ok(root, &["config", "user.email", "test@example.com"]);
    compare_git_ok(root, &["config", "user.name", "Test"]);
    std::fs::write(root.join("README.md"), "compare fixture\n").expect("write fixture marker");
    if let Some(raw) = baseline_raw {
        let baseline_meta = root.join(baseline_meta_path);
        std::fs::create_dir_all(baseline_meta.parent().expect("baseline metadata parent"))
            .expect("baseline metadata dir");
        std::fs::write(baseline_meta, raw).expect("write baseline metadata");
    }
    compare_git_ok(root, &["add", "."]);
    compare_git_ok(root, &["commit", "-q", "-m", "baseline"]);
    let baseline_ref = compare_git_ok(root, &["rev-parse", "HEAD"]);

    let meta_dir = root.join("wiki/_meta");
    std::fs::create_dir_all(&meta_dir).expect("current metadata dir");
    std::fs::write(meta_dir.join("codewiki.json"), current_raw).expect("write current metadata");
    compare_git_ok(root, &["add", "."]);
    compare_git_ok(root, &["commit", "-q", "--allow-empty", "-m", "current"]);
    (project, baseline_ref)
}

fn committed_compare_case(
    baseline: &serde_json::Value,
    current: &serde_json::Value,
) -> (tempfile::TempDir, String) {
    committed_compare_repo(
        Some(serde_json::to_string_pretty(baseline).expect("serialize baseline") + "\n"),
        serde_json::to_string_pretty(current).expect("serialize current") + "\n",
    )
}

#[test]
fn compare_to_matches_path_sorted_json_goldens_without_writing_pages() {
    let clean_base_doc = compare_doc(
        Some("base-page"),
        Some(false),
        serde_json::json!({"src/a.rs": "hash-a"}),
    );
    let cases = [
        (
            "no-change",
            compare_meta(
                Some("base-run"),
                Some(false),
                serde_json::json!({"code/a.md": clean_base_doc.clone()}),
            ),
            compare_meta(
                Some("current-run"),
                Some(false),
                serde_json::json!({"code/a.md": clean_base_doc}),
            ),
            serde_json::json!({
                "base": {"commit": "base-run", "dirty": false},
                "current": {"commit": "current-run", "dirty": false},
                "added": [],
                "removed": [],
                "changed": []
            }),
        ),
        (
            "added",
            compare_meta(Some("base-run"), Some(false), serde_json::json!({})),
            compare_meta(
                Some("current-run"),
                Some(false),
                serde_json::json!({
                    "code/z.md": compare_doc(
                        Some("current-page"),
                        Some(false),
                        serde_json::json!({"src/z.rs": "hash-z"})
                    ),
                    "code/a.md": compare_doc(
                        Some("current-page"),
                        Some(false),
                        serde_json::json!({"src/a.rs": "hash-a"})
                    )
                }),
            ),
            serde_json::json!({
                "base": {"commit": "base-run", "dirty": false},
                "current": {"commit": "current-run", "dirty": false},
                "added": [
                    {
                        "path": "code/a.md",
                        "commit": "current-page",
                        "dirty": false,
                        "source_hashes": {"src/a.rs": "hash-a"}
                    },
                    {
                        "path": "code/z.md",
                        "commit": "current-page",
                        "dirty": false,
                        "source_hashes": {"src/z.rs": "hash-z"}
                    }
                ],
                "removed": [],
                "changed": []
            }),
        ),
        (
            "removed",
            compare_meta(
                Some("base-run"),
                Some(false),
                serde_json::json!({
                    "code/old.md": compare_doc(
                        Some("base-page"),
                        Some(false),
                        serde_json::json!({"src/old.rs": "old-hash"})
                    )
                }),
            ),
            compare_meta(Some("current-run"), Some(false), serde_json::json!({})),
            serde_json::json!({
                "base": {"commit": "base-run", "dirty": false},
                "current": {"commit": "current-run", "dirty": false},
                "added": [],
                "removed": [
                    {
                        "path": "code/old.md",
                        "commit": "base-page",
                        "dirty": false,
                        "source_hashes": {"src/old.rs": "old-hash"}
                    }
                ],
                "changed": []
            }),
        ),
        (
            "changed",
            compare_meta(
                Some("base-run"),
                Some(false),
                serde_json::json!({
                    "code/a.md": compare_doc(
                        Some("base-page"),
                        Some(false),
                        serde_json::json!({"src/a.rs": "old-hash"})
                    )
                }),
            ),
            compare_meta(
                Some("current-run"),
                Some(false),
                serde_json::json!({
                    "code/a.md": compare_doc(
                        Some("current-page"),
                        Some(false),
                        serde_json::json!({"src/a.rs": "new-hash"})
                    )
                }),
            ),
            serde_json::json!({
                "base": {"commit": "base-run", "dirty": false},
                "current": {"commit": "current-run", "dirty": false},
                "added": [],
                "removed": [],
                "changed": [{
                    "path": "code/a.md",
                    "base": {
                        "commit": "base-page",
                        "dirty": false,
                        "source_hashes": {"src/a.rs": "old-hash"}
                    },
                    "current": {
                        "commit": "current-page",
                        "dirty": false,
                        "source_hashes": {"src/a.rs": "new-hash"}
                    }
                }]
            }),
        ),
        (
            "unstamped",
            compare_meta(Some("base-run"), Some(false), serde_json::json!({})),
            compare_meta(
                None,
                None,
                serde_json::json!({
                    "code/legacy.md": compare_doc(
                        None,
                        None,
                        serde_json::json!({"src/legacy.rs": "legacy-hash"})
                    )
                }),
            ),
            serde_json::json!({
                "base": {"commit": "base-run", "dirty": false},
                "current": {"commit": null, "dirty": null},
                "added": [{
                    "path": "code/legacy.md",
                    "commit": null,
                    "dirty": null,
                    "source_hashes": {"src/legacy.rs": "legacy-hash"}
                }],
                "removed": [],
                "changed": []
            }),
        ),
        (
            "dirty",
            compare_meta(Some("base-run"), Some(false), serde_json::json!({})),
            compare_meta(
                Some("current-run"),
                Some(true),
                serde_json::json!({
                    "code/dirty.md": compare_doc(
                        Some("current-page"),
                        Some(true),
                        serde_json::json!({"src/dirty.rs": "dirty-hash"})
                    )
                }),
            ),
            serde_json::json!({
                "base": {"commit": "base-run", "dirty": false},
                "current": {"commit": "current-run", "dirty": true},
                "added": [{
                    "path": "code/dirty.md",
                    "commit": "current-page",
                    "dirty": true,
                    "source_hashes": {"src/dirty.rs": "dirty-hash"}
                }],
                "removed": [],
                "changed": []
            }),
        ),
    ];

    for (name, baseline, current, expected) in cases {
        let (project, baseline_ref) = committed_compare_case(&baseline, &current);
        let current_path = project.path().join("wiki/_meta/codewiki.json");
        let before = std::fs::read(&current_path).expect("read current metadata before compare");
        let target = format!("{baseline_ref}:_meta/codewiki.json");
        let summary = compare_to(project.path(), Some("wiki"), &target).expect("compare succeeds");
        assert_eq!(
            serde_json::to_value(summary).expect("serialize compare summary"),
            expected,
            "{name} golden mismatch"
        );
        assert_eq!(
            std::fs::read(&current_path).expect("read current metadata after compare"),
            before,
            "{name} compare must not write metadata"
        );
        assert!(
            compare_git_ok(project.path(), &["status", "--porcelain"]).is_empty(),
            "{name} compare must leave the repository clean"
        );
    }
}

#[test]
fn compare_to_defaults_to_output_relative_baseline_metadata() {
    let current = compare_meta(Some("same-run"), Some(false), serde_json::json!({}));
    let raw = serde_json::to_string_pretty(&current).expect("serialize metadata") + "\n";
    let (project, baseline_ref) =
        committed_compare_repo_at("wiki/_meta/codewiki.json", Some(raw.clone()), raw);

    let summary =
        compare_to(project.path(), Some("wiki"), &baseline_ref).expect("default compare succeeds");
    let value = serde_json::to_value(summary).expect("serialize compare summary");
    assert_eq!(value["added"], serde_json::json!([]));
    assert_eq!(value["removed"], serde_json::json!([]));
    assert_eq!(value["changed"], serde_json::json!([]));
    assert!(
        compare_git_ok(project.path(), &["status", "--porcelain"]).is_empty(),
        "default compare must leave the repository clean"
    );
}

#[test]
fn compare_to_distinguishes_bad_ref_and_invalid_baseline_metadata() {
    let current = compare_meta(Some("current-run"), Some(false), serde_json::json!({}));
    let current_raw = serde_json::to_string_pretty(&current).expect("serialize current") + "\n";

    let (valid_project, baseline_ref) =
        committed_compare_repo(Some(current_raw.clone()), current_raw.clone());
    let valid_target = format!("{baseline_ref}:_meta/codewiki.json");
    compare_to(valid_project.path(), Some("wiki"), &valid_target).expect("no-change succeeds");
    let bad_ref = compare_to(
        valid_project.path(),
        Some("wiki"),
        "does-not-exist:_meta/codewiki.json",
    )
    .expect_err("bad ref fails");
    assert!(
        bad_ref
            .to_string()
            .contains("compare ref 'does-not-exist' does not resolve to a commit"),
        "unexpected bad-ref error: {bad_ref:#}"
    );

    let (absent_project, absent_ref) = committed_compare_repo(None, current_raw.clone());
    let absent_target = format!("{absent_ref}:_meta/codewiki.json");
    let absent = compare_to(absent_project.path(), Some("wiki"), &absent_target)
        .expect_err("absent baseline fails");
    assert!(
        absent
            .to_string()
            .contains("baseline metadata is absent at ref"),
        "unexpected absent-baseline error: {absent:#}"
    );

    let (malformed_project, malformed_ref) =
        committed_compare_repo(Some("{malformed".to_string()), current_raw.clone());
    let malformed_target = format!("{malformed_ref}:_meta/codewiki.json");
    let malformed = compare_to(malformed_project.path(), Some("wiki"), &malformed_target)
        .expect_err("malformed baseline fails");
    assert!(
        malformed
            .to_string()
            .contains("baseline metadata is malformed at ref"),
        "unexpected malformed-baseline error: {malformed:#}"
    );

    let (malformed_current_project, valid_ref) =
        committed_compare_repo(Some(current_raw.clone()), "{malformed".to_string());
    let valid_target = format!("{valid_ref}:_meta/codewiki.json");
    let malformed_current = compare_to(
        malformed_current_project.path(),
        Some("wiki"),
        &valid_target,
    )
    .expect_err("malformed current metadata fails");
    assert!(
        malformed_current
            .to_string()
            .contains("current metadata is malformed at"),
        "unexpected malformed-current error: {malformed_current:#}"
    );

    for invalid_path in ["", "/_meta/codewiki.json", "../_meta/codewiki.json"] {
        let invalid_target = format!("{baseline_ref}:{invalid_path}");
        let invalid = compare_to(valid_project.path(), Some("wiki"), &invalid_target)
            .expect_err("invalid explicit metadata path fails");
        assert!(
            invalid
                .to_string()
                .contains("metadata path must be repository-relative"),
            "unexpected invalid-path error for {invalid_path:?}: {invalid:#}"
        );
    }
}
