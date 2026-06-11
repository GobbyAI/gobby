use super::*;

#[test]
fn module_docs_include_physical_direct_files_for_ancestor_modules() {
    let files = vec![FileDoc {
        path: "src/commands/mod.rs".to_string(),
        module: "src/commands/codewiki".to_string(),
        summary: "command dispatcher".to_string(),
        source_spans: Vec::new(),
        symbols: Vec::new(),
        component_ids: Vec::new(),
        degraded: false,
        reused_page: None,
    }];
    let mut generate = None;
    let mut progress = CodewikiProgress::silent();

    let docs = build_module_docs(
        &files,
        &[],
        CodewikiGraphAvailability::Available,
        &mut generate,
        &mut None,
        &mut progress,
        &mut |_| Ok(()),
    )
    .expect("module docs build");
    let parent = docs
        .iter()
        .find(|doc| doc.module == "src/commands")
        .expect("physical parent module is documented");
    let rendered = render_module_doc(parent);

    assert!(rendered.contains("[[code/files/src/commands/mod.rs|src/commands/mod.rs]]"));
    assert!(rendered.contains("[[code/modules/src/commands/codewiki|src/commands/codewiki]]"));
}

#[test]
fn module_components_only_render_direct_file_symbols() {
    let files = vec![
        file_doc_with_symbol("src/lib.rs", "src", "direct-component"),
        file_doc_with_symbol("src/commands/mod.rs", "src/commands", "child-component"),
        file_doc_with_symbol("src/commands/run.rs", "src/commands", "leaf-component"),
    ];
    let mut generate = None;
    let mut progress = CodewikiProgress::silent();

    let docs = build_module_docs(
        &files,
        &[],
        CodewikiGraphAvailability::Available,
        &mut generate,
        &mut None,
        &mut progress,
        &mut |_| Ok(()),
    )
    .expect("module docs build");
    let parent = docs
        .iter()
        .find(|doc| doc.module == "src")
        .expect("parent module is documented");
    let child = docs
        .iter()
        .find(|doc| doc.module == "src/commands")
        .expect("child module is documented");

    let parent_rendered = render_module_doc(parent);
    let child_rendered = render_module_doc(child);

    assert!(parent_rendered.contains("`direct-component`"));
    assert!(!parent_rendered.contains("child-component"));
    assert!(!parent_rendered.contains("leaf-component"));
    assert!(child_rendered.contains("`child-component`"));
    assert!(child_rendered.contains("`leaf-component`"));
}

#[test]
fn bounded_import_mermaid_notes_partial_graphs() {
    let files = vec![
        file_doc("src/a.rs", "src/a", "a"),
        file_doc("src/b.rs", "src/b", "b"),
        file_doc("src/c.rs", "src/c", "c"),
        file_doc("src/d.rs", "src/d", "d"),
    ];
    let edges = vec![
        CodewikiGraphEdge::import("a", "b"),
        CodewikiGraphEdge::import("b", "c"),
        CodewikiGraphEdge::import("c", "d"),
    ];

    let diagram = render_module_dependency_mermaid("src/a", &files, &edges)
        .expect("dependency diagram renders");

    assert!(diagram.contains("%% Partial import graph: 1 edge(s) omitted by bounds"));
}

fn file_doc(path: &str, module: &str, component_id: &str) -> FileDoc {
    FileDoc {
        path: path.to_string(),
        module: module.to_string(),
        summary: String::new(),
        source_spans: Vec::new(),
        symbols: Vec::new(),
        component_ids: vec![component_id.to_string()],
        degraded: false,
        reused_page: None,
    }
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
        }],
        component_ids: vec![component_id.to_string()],
        degraded: false,
        reused_page: None,
    }
}
