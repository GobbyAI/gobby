use super::*;

#[test]
fn module_docs_include_physical_direct_files_for_ancestor_modules() {
    let files = vec![FileDoc {
        path: "src/commands/mod.rs".to_string(),
        module: "src/commands/codewiki".to_string(),
        summary: "command dispatcher".to_string(),
        body: String::new(),
        source_spans: Vec::new(),
        symbols: Vec::new(),
        component_ids: Vec::new(),
        degraded: false,
        degraded_sources: Vec::new(),
        verify_notes: Vec::new(),
        reused_page: None,
    }];
    let mut generate = None;
    let mut diagram_stats = DiagramStats::default();
    let mut progress = CodewikiProgress::silent();

    let docs = build_module_docs(
        &files,
        &std::collections::BTreeMap::new(),
        &[],
        CodewikiGraphAvailability::Available,
        &mut generate,
        &mut None,
        &mut diagram_stats,
        &mut progress,
        &mut |_| Ok(()),
    )
    .expect("module docs build");
    let parent = docs
        .iter()
        .find(|doc| doc.module == "src/commands")
        .expect("physical parent module is documented");
    let rendered = render_module_doc(parent);

    assert!(rendered.contains("| File | Summary |\n| --- | --- |\n"));
    assert!(rendered.contains("[[code/files/src/commands/mod.rs\\|src/commands/mod.rs]]"));
    assert!(rendered.contains("[[code/modules/src/commands/codewiki\\|src/commands/codewiki]]"));
}

#[test]
fn scoped_run_emits_module_pages_for_synthetic_cluster_link_targets() {
    // Regression: a file page renders `Module: [[code/modules/<file.module>]]`.
    // When clustering assigns a cross-directory synthetic module name (here
    // `src/source_execute`, as `cluster_module_name` does for a call-connected
    // cluster spanning sibling subdirectories) that is not a path prefix of any
    // changed-file scope, a scoped/incremental run's doc-prune filter used to
    // drop that module page while still emitting the file pages that link to it
    // — a dangling link that grew `curated_broken_link_count` every heal. The
    // module page for every direct `file.module` link target must be emitted
    // regardless of the scope filter.
    let files = vec![
        file_doc_with_symbol("src/audit/claims.rs", "src/source_execute", "claims"),
        file_doc_with_symbol("src/ai/chunk.rs", "src/source_execute", "chunk"),
    ];
    let mut generate = None;
    let mut diagram_stats = DiagramStats::default();
    let mut progress = CodewikiProgress::silent();
    let mut emitted = Vec::new();

    // Scope filter mimics an incremental run scoped to the changed files' own
    // directories; the synthetic cluster path `src/source_execute` matches no
    // scope, so before the fix its module page was filtered out even though the
    // in-scope file pages link straight to it.
    let scope_filter = |module: &str| {
        module == "src/audit"
            || module.starts_with("src/audit/")
            || module == "src/ai"
            || module.starts_with("src/ai/")
    };
    assert!(
        !scope_filter("src/source_execute"),
        "the synthetic cluster path must be outside the file-path scope"
    );

    let docs = build_module_docs_with_filter(
        &files,
        &std::collections::BTreeMap::new(),
        &[],
        CodewikiGraphAvailability::Available,
        &mut generate,
        &mut None,
        &mut diagram_stats,
        &mut progress,
        &scope_filter,
        &mut |doc| {
            emitted.push(doc.module.clone());
            Ok(())
        },
    )
    .expect("module docs build");

    assert!(
        docs.iter().any(|doc| doc.module == "src/source_execute"),
        "module page for the direct file->module link target must be built"
    );
    assert!(
        emitted.iter().any(|module| module == "src/source_execute"),
        "the synthetic cluster module page must be emitted, not dropped by scope"
    );
}

#[test]
fn module_page_drops_component_id_dump_keeps_navigation() {
    let files = vec![
        file_doc_with_symbol("src/lib.rs", "src", "direct-component"),
        file_doc_with_symbol("src/commands/mod.rs", "src/commands", "child-component"),
        file_doc_with_symbol("src/commands/run.rs", "src/commands", "leaf-component"),
    ];
    let mut generate = None;
    let mut diagram_stats = DiagramStats::default();
    let mut progress = CodewikiProgress::silent();

    let docs = build_module_docs(
        &files,
        &std::collections::BTreeMap::new(),
        &[],
        CodewikiGraphAvailability::Available,
        &mut generate,
        &mut None,
        &mut diagram_stats,
        &mut progress,
        &mut |_| Ok(()),
    )
    .expect("module docs build");
    let parent = docs
        .iter()
        .find(|doc| doc.module == "src")
        .expect("parent module is documented");

    let parent_rendered = render_module_doc(parent);
    // The UUID component-id dump is gone from the human module page (#871): no
    // `Component ID` heading/column, no raw component ids.
    assert!(!parent_rendered.contains("Component ID"));
    assert!(!parent_rendered.contains("## Components"));
    assert!(!parent_rendered.contains("direct-component"));
    assert!(!parent_rendered.contains("child-component"));
    // Navigation to direct files and child modules is retained as the module's
    // key components.
    assert!(parent_rendered.contains("## Files"));
    assert!(parent_rendered.contains("[[code/modules/src/commands\\|src/commands]]"));
}

#[test]
fn module_dependency_diagrams_emit_valid_sections_and_observable_empty_slots() {
    let (files, edges) = dependency_fixture(2);
    let mut generate = None;
    let mut diagram_stats = DiagramStats::default();
    let mut progress = CodewikiProgress::silent();

    let docs = build_module_docs(
        &files,
        &std::collections::BTreeMap::new(),
        &edges,
        CodewikiGraphAvailability::Available,
        &mut generate,
        &mut None,
        &mut diagram_stats,
        &mut progress,
        &mut |_| Ok(()),
    )
    .expect("module docs build");
    let core = docs
        .iter()
        .find(|doc| doc.module == "src/core")
        .expect("core module is documented");
    let rendered = render_module_doc(core);
    let fence = mermaid_fence(
        core.dependency_diagram
            .as_deref()
            .expect("dependency diagram"),
    );

    assert!(rendered.contains("## Dependencies\n\n```mermaid\n"));
    assert!(fence.contains("src/core"));
    assert!(fence.contains("src/dep00"));
    assert!(fence.contains("src/dep01"));
    assert!(
        is_valid_mermaid(fence),
        "invalid dependency fence:\n{fence}"
    );
    assert_eq!(diagram_stats.emitted, docs.len());
    assert_eq!(diagram_stats.sparse_evidence, docs.len());
    assert_eq!(diagram_stats.total(), docs.len() * 2);

    let files = vec![file_doc_with_symbol("src/lib.rs", "src", "root")];
    let mut diagram_stats = DiagramStats::default();
    let docs = build_module_docs(
        &files,
        &std::collections::BTreeMap::new(),
        &[],
        CodewikiGraphAvailability::Available,
        &mut generate,
        &mut None,
        &mut diagram_stats,
        &mut progress,
        &mut |_| Ok(()),
    )
    .expect("edge-free module docs build");
    let rendered = render_module_doc(&docs[0]);

    assert!(!rendered.contains("## Dependencies"));
    assert_eq!(diagram_stats.sparse_evidence, 2);
    assert_eq!(diagram_stats.total(), 2);
}

#[test]
fn module_dependency_diagrams_are_bounded_and_permutation_invariant() {
    let (files, edges) = dependency_fixture(25);
    let DiagramOutcome::Emitted(available) = render_module_dependency_mermaid(
        "src/core",
        &files,
        &edges,
        CodewikiGraphAvailability::Available,
    ) else {
        panic!("available dependency diagram was not emitted");
    };
    assert!(available.contains(
        "_Simplified diagram: showing top 20 of 25 module dependency edge(s) within diagram bounds._"
    ));
    assert!(is_valid_mermaid(mermaid_fence(&available)));

    let DiagramOutcome::Emitted(truncated) = render_module_dependency_mermaid(
        "src/core",
        &files,
        &edges,
        CodewikiGraphAvailability::Truncated,
    ) else {
        panic!("truncated dependency diagram was not emitted");
    };
    assert!(truncated.contains(
        "_Simplified diagram: showing top 20 of 25 available module dependency edge(s); source graph was truncated._"
    ));

    let mut reordered_files = files.clone();
    reordered_files.reverse();
    let mut reordered_edges = edges.clone();
    reordered_edges.reverse();
    let DiagramOutcome::Emitted(reordered) = render_module_dependency_mermaid(
        "src/core",
        &reordered_files,
        &reordered_edges,
        CodewikiGraphAvailability::Available,
    ) else {
        panic!("reordered dependency diagram was not emitted");
    };

    assert_eq!(available, reordered);
}

#[test]
fn module_call_sequence_emits_only_for_depth_two_chains() {
    let files = call_sequence_files(3);
    let chain = vec![
        CodewikiGraphEdge::call("component-00", "component-01"),
        CodewikiGraphEdge::call("component-01", "component-02"),
    ];
    let DiagramOutcome::Emitted(diagram) = render_module_call_sequence(
        "src/core",
        &files,
        &chain,
        CodewikiGraphAvailability::Available,
    ) else {
        panic!("depth-two call chain was not emitted");
    };
    let fence = mermaid_fence(&diagram);

    assert!(diagram.contains(
        "_Static call sequence — indexed call edges ordered by call depth…; not a recorded execution trace._"
    ));
    assert!(fence.contains("sequenceDiagram"));
    assert!(fence.contains("m_component_00->>m_component_01: calls"));
    assert!(fence.contains("m_component_01->>m_component_02: calls"));
    assert!(
        fence.find("m_component_00->>m_component_01: calls")
            < fence.find("m_component_01->>m_component_02: calls")
    );
    assert!(
        is_valid_mermaid(fence),
        "invalid call-sequence fence:\n{fence}"
    );

    let mut generate = None;
    let mut diagram_stats = DiagramStats::default();
    let mut progress = CodewikiProgress::silent();
    let docs = build_module_docs(
        &files,
        &std::collections::BTreeMap::new(),
        &chain,
        CodewikiGraphAvailability::Available,
        &mut generate,
        &mut None,
        &mut diagram_stats,
        &mut progress,
        &mut |_| Ok(()),
    )
    .expect("module docs build");
    let core = docs
        .iter()
        .find(|doc| doc.module == "src/core")
        .expect("core module is documented");
    assert!(core.call_sequence_diagram.is_some());
    assert!(render_module_doc(core).contains("## Call sequence\n\n_Static call sequence"));

    let flat_star = vec![
        CodewikiGraphEdge::call("component-00", "component-01"),
        CodewikiGraphEdge::call("component-00", "component-02"),
    ];
    assert_eq!(
        render_module_call_sequence(
            "src/core",
            &files,
            &flat_star,
            CodewikiGraphAvailability::Available,
        ),
        DiagramOutcome::SparseEvidence
    );

    let mut cycle = chain;
    cycle.push(CodewikiGraphEdge::call("component-02", "component-00"));
    assert!(matches!(
        render_module_call_sequence(
            "src/core",
            &files,
            &cycle,
            CodewikiGraphAvailability::Available,
        ),
        DiagramOutcome::Emitted(_)
    ));
}

#[test]
fn module_call_sequence_is_bounded_and_permutation_invariant() {
    let files = call_sequence_files(21);
    let mut edges = Vec::new();
    for index in 1..=10 {
        edges.push(CodewikiGraphEdge::call(
            "component-00",
            format!("component-{index:02}"),
        ));
        edges.push(CodewikiGraphEdge::call(
            format!("component-{index:02}"),
            format!("component-{:02}", index + 10),
        ));
    }

    let DiagramOutcome::Emitted(available) = render_module_call_sequence(
        "src/core",
        &files,
        &edges,
        CodewikiGraphAvailability::Available,
    ) else {
        panic!("bounded call sequence was not emitted");
    };
    let fence = mermaid_fence(&available);
    assert!(available.contains("_Simplified diagram:"));
    assert!(
        fence
            .lines()
            .filter(|line| line.trim_start().starts_with("participant "))
            .count()
            <= 8
    );
    assert!(fence.lines().filter(|line| line.contains("->>")).count() <= 12);
    assert!(is_valid_mermaid(fence));

    let DiagramOutcome::Emitted(truncated) = render_module_call_sequence(
        "src/core",
        &files,
        &edges,
        CodewikiGraphAvailability::Truncated,
    ) else {
        panic!("truncated call sequence was not emitted");
    };
    assert!(truncated.contains("source graph was truncated"));

    let mut reordered_files = files.clone();
    reordered_files.reverse();
    let mut reordered_edges = edges.clone();
    reordered_edges.reverse();
    let DiagramOutcome::Emitted(reordered) = render_module_call_sequence(
        "src/core",
        &reordered_files,
        &reordered_edges,
        CodewikiGraphAvailability::Available,
    ) else {
        panic!("reordered call sequence was not emitted");
    };

    assert_eq!(available, reordered);
}

#[test]
fn module_diagram_slots_record_dependency_and_suppressed_call_sequence() {
    let files = vec![
        file_doc_with_symbol("src/core.rs", "src/core", "root"),
        file_doc_with_symbol("src/dep-a.rs", "src/dep-a", "dep-a"),
        file_doc_with_symbol("src/dep-b.rs", "src/dep-b", "dep-b"),
    ];
    let edges = vec![
        CodewikiGraphEdge::call("root", "dep-a"),
        CodewikiGraphEdge::call("root", "dep-b"),
    ];
    let mut generate = None;
    let mut diagram_stats = DiagramStats::default();
    let mut progress = CodewikiProgress::capture();

    let docs = build_module_docs(
        &files,
        &std::collections::BTreeMap::new(),
        &edges,
        CodewikiGraphAvailability::Available,
        &mut generate,
        &mut None,
        &mut diagram_stats,
        &mut progress,
        &mut |_| Ok(()),
    )
    .expect("module docs build");
    let core = docs
        .iter()
        .find(|doc| doc.module == "src/core")
        .expect("core module is documented");

    assert!(core.dependency_diagram.is_some());
    assert!(core.call_sequence_diagram.is_none());
    assert_eq!(diagram_stats.total(), docs.len() * 2);
    assert_eq!(diagram_stats.recorded_slots_len(), docs.len() * 2);
    let lines = progress.into_lines();
    assert!(lines.iter().any(|line| {
        line == "codewiki: diagram code/modules/src/core.md [module_dependency]: emitted"
    }));
    assert!(lines.iter().any(|line| {
        line == "codewiki: diagram code/modules/src/core.md [module_call_sequence]: sparse_evidence"
    }));
}

fn dependency_fixture(edge_count: usize) -> (Vec<FileDoc>, Vec<CodewikiGraphEdge>) {
    let mut files = vec![file_doc_with_symbol("src/core.rs", "src/core", "root")];
    let mut edges = Vec::new();
    for index in 0..edge_count {
        let component = format!("dep-{index:02}");
        files.push(file_doc_with_symbol(
            &format!("src/dep{index:02}.rs"),
            &format!("src/dep{index:02}"),
            &component,
        ));
        edges.push(if index % 2 == 0 {
            CodewikiGraphEdge::import("root", component)
        } else {
            CodewikiGraphEdge::call("root", component)
        });
    }
    (files, edges)
}

fn call_sequence_files(component_count: usize) -> Vec<FileDoc> {
    (0..component_count)
        .map(|index| {
            file_doc_with_symbol(
                &format!("src/core/component_{index:02}.rs"),
                "src/core",
                &format!("component-{index:02}"),
            )
        })
        .collect()
}

fn mermaid_fence(diagram: &str) -> &str {
    let start = diagram.find("```mermaid").expect("mermaid fence");
    &diagram[start..]
}

fn file_doc_with_symbol(path: &str, module: &str, component_id: &str) -> FileDoc {
    let symbol = Symbol {
        id: format!("{component_id}-symbol"),
        project_id: "project".to_string(),
        file_path: path.to_string(),
        name: component_id.to_string(),
        qualified_name: component_id.to_string(),
        kind: "function".to_string(),
        language: "rust".to_string(),
        byte_start: 0,
        byte_end: 1,
        line_start: 1,
        line_end: 1,
        signature: None,
        docstring: None,
        parent_symbol_id: None,
        content_hash: String::new(),
        summary: None,
        created_at: String::new(),
        updated_at: String::new(),
    };
    FileDoc {
        path: path.to_string(),
        module: module.to_string(),
        summary: String::new(),
        body: String::new(),
        source_spans: Vec::new(),
        symbols: vec![SymbolDoc {
            purpose: String::new(),
            component_id: component_id.to_string(),
            component_label: component_id.to_string(),
            source_span: SourceSpan {
                file: path.to_string(),
                line_start: 1,
                line_end: 1,
            },
            symbol,
            deprecation: None,
            is_test: false,
        }],
        component_ids: vec![component_id.to_string()],
        degraded: false,
        degraded_sources: Vec::new(),
        verify_notes: Vec::new(),
        reused_page: None,
    }
}
