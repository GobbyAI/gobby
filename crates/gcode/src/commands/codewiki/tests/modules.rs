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
    let mut progress = CodewikiProgress::silent();

    let docs = build_module_docs(
        &files,
        &std::collections::BTreeMap::new(),
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

    assert!(rendered.contains("| File | Summary |\n| --- | --- |\n"));
    assert!(rendered.contains("[[code/files/src/commands/mod.rs\\|src/commands/mod.rs]]"));
    assert!(rendered.contains("[[code/modules/src/commands/codewiki\\|src/commands/codewiki]]"));
}

#[test]
fn module_page_drops_component_id_dump_keeps_navigation() {
    let files = vec![
        file_doc_with_symbol("src/lib.rs", "src", "direct-component"),
        file_doc_with_symbol("src/commands/mod.rs", "src/commands", "child-component"),
        file_doc_with_symbol("src/commands/run.rs", "src/commands", "leaf-component"),
    ];
    let mut generate = None;
    let mut progress = CodewikiProgress::silent();

    let docs = build_module_docs(
        &files,
        &std::collections::BTreeMap::new(),
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
        }],
        component_ids: vec![component_id.to_string()],
        degraded: false,
        degraded_sources: Vec::new(),
        verify_notes: Vec::new(),
        reused_page: None,
    }
}
