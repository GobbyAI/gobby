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
    }];
    let mut generate = None;
    let mut progress = CodewikiProgress::silent();

    let docs = build_module_docs(
        &files,
        &[],
        CodewikiGraphAvailability::Available,
        &mut generate,
        &mut progress,
    );
    let parent = docs
        .iter()
        .find(|doc| doc.module == "src/commands")
        .expect("physical parent module is documented");
    let rendered = render_module_doc(parent);

    assert!(rendered.contains("[[code/files/src/commands/mod.rs|src/commands/mod.rs]]"));
    assert!(rendered.contains("[[code/modules/src/commands/codewiki|src/commands/codewiki]]"));
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
    }
}
