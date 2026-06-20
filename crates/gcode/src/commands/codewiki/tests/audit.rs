//! Tests for the deterministic codewiki audit pages (#889): deprecation markers
//! and badge, plus dead-code candidates. Fixtures are built directly (symbols,
//! Call edges, and a tiny tempdir source file for the scan) so the assertions
//! are decoupled from the real workspace.

use super::support::{test_component_id, test_symbol};
use super::*;

use std::collections::HashSet;
use std::fs;
use std::path::Path;

/// Write a source file under `root` (creating parent dirs) so the deprecation /
/// test-gating scan can read it.
fn write_source(root: &Path, rel: &str, contents: &str) {
    let path = root.join(rel);
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).expect("create source dir");
    }
    fs::write(&path, contents).expect("write source file");
}

/// A minimal `CodewikiInput` with the given symbols/edges/availability and no
/// leading chunks.
fn input(
    symbols: Vec<Symbol>,
    edges: Vec<CodewikiGraphEdge>,
    availability: CodewikiGraphAvailability,
) -> CodewikiInput {
    CodewikiInput {
        files: symbols.iter().map(|s| s.file_path.clone()).collect(),
        graph_edges: edges,
        graph_availability: availability,
        symbols,
        leading_chunks: std::collections::BTreeMap::new(),
    }
}

#[test]
fn deprecation_attribute_and_doc_comment_are_detected_badged_and_listed() {
    let dir = tempfile::tempdir().expect("tempdir");
    let root = dir.path();
    // `attr_dep` has a `#[deprecated(note = ...)]`; `doc_dep` has a `///
    // DEPRECATED ...` doc-comment. `keep` is a plain fn that must NOT be
    // flagged.
    let src = "\
/// A live helper.
pub fn keep() {}

#[deprecated(note = \"use keep instead\")]
pub fn attr_dep() {}

/// DEPRECATED: superseded by keep.
pub fn doc_dep() {}
";
    let file = "src/lib.rs";
    write_source(root, file, src);

    let keep = test_symbol(file, "keep", "function", 2, "pub fn keep()");
    let attr_dep = test_symbol(file, "attr_dep", "function", 5, "pub fn attr_dep()");
    let doc_dep = test_symbol(file, "doc_dep", "function", 8, "pub fn doc_dep()");

    let input = input(
        vec![keep.clone(), attr_dep.clone(), doc_dep.clone()],
        Vec::new(),
        CodewikiGraphAvailability::Available,
    );
    let audit = build_audit_context(root, &input, None);

    // Both deprecated symbols are in the index; `keep` is not.
    assert!(
        audit.deprecations.contains_key(&attr_dep.id),
        "attribute-deprecated symbol must be detected"
    );
    assert!(
        audit.deprecations.contains_key(&doc_dep.id),
        "doc-comment-deprecated symbol must be detected"
    );
    assert!(
        !audit.deprecations.contains_key(&keep.id),
        "a plain fn must not be flagged deprecated"
    );
    // The attribute reason carries the note.
    assert_eq!(
        audit.deprecations.get(&attr_dep.id).map(String::as_str),
        Some("use keep instead")
    );

    // The file doc stamps `SymbolDoc.deprecation` and the rendered page shows
    // the badge.
    let file_doc = build_file_doc(
        file,
        "src".to_string(),
        vec![keep.clone(), attr_dep.clone(), doc_dep.clone()],
        None,
        &RelationshipFacts::default(),
        Some(&audit.deprecations),
        Some(&audit.tests),
        &mut None,
        &mut None,
        &mut None,
        AiDepth::Sections,
        &mut CodewikiProgress::silent(),
        FileDocPosition { index: 1, total: 1 },
    );
    let attr_doc = file_doc
        .symbols
        .iter()
        .find(|s| s.symbol.name == "attr_dep")
        .expect("attr_dep symbol doc");
    assert_eq!(
        attr_doc.deprecation.as_deref(),
        Some("use keep instead"),
        "the file doc must stamp the deprecation reason"
    );
    let keep_doc = file_doc
        .symbols
        .iter()
        .find(|s| s.symbol.name == "keep")
        .expect("keep symbol doc");
    assert!(
        keep_doc.deprecation.is_none(),
        "a non-deprecated symbol must not be stamped"
    );

    let rendered = render_file_doc(&file_doc);
    assert!(
        rendered.contains("⚠️ **deprecated**"),
        "the rendered file page must show a deprecation badge; page:\n{rendered}"
    );

    // The aggregate deprecations page lists both deprecated symbols and not the
    // live one.
    let deprecations_doc = build_deprecations_doc(&input, &audit.deprecations);
    assert_eq!(deprecations_doc.symbols.len(), 2);
    let page = render_deprecations_doc(&deprecations_doc);
    assert!(
        page.contains("attr_dep"),
        "page must list attr_dep:\n{page}"
    );
    assert!(page.contains("doc_dep"), "page must list doc_dep:\n{page}");
    assert!(
        page.contains("use keep instead"),
        "page must show the recorded reason:\n{page}"
    );
    assert!(!page.contains("No deprecated symbols found."));
    assert!(deprecations_doc.degraded_sources.is_empty());
}

#[test]
fn uncalled_free_function_is_a_candidate() {
    let dir = tempfile::tempdir().expect("tempdir");
    let root = dir.path();
    let file = "src/lib.rs";
    write_source(root, file, "pub fn lonely() {}\n");

    let lonely = test_symbol(file, "lonely", "function", 1, "pub fn lonely()");
    let input = input(
        vec![lonely.clone()],
        Vec::new(),
        CodewikiGraphAvailability::Available,
    );
    let audit = build_audit_context(root, &input, None);

    let doc = build_dead_code_doc(&input, &audit);
    assert!(!doc.skipped);
    assert!(!doc.truncated);
    assert_eq!(doc.candidates.len(), 1);
    assert_eq!(doc.candidates[0].name, "lonely");

    let page = render_dead_code_doc(&doc);
    assert!(page.contains("CANDIDATES, not verdicts"));
    assert!(page.contains("lonely"), "candidate must be listed:\n{page}");
}

#[test]
fn entry_points_test_gated_and_trait_impls_are_excluded() {
    let dir = tempfile::tempdir().expect("tempdir");
    let root = dir.path();
    // `main` (entry by name), `handler` (entry via the catalog entry set),
    // `test_helper` (preceded by `#[test]`), an `impl` block symbol, and a
    // `method` symbol must ALL be excluded.
    let file = "src/lib.rs";
    let src = "\
fn main() {}

pub fn handler() {}

#[test]
fn test_helper() {}

impl Foo {}

fn method_like() {}
";
    write_source(root, file, src);

    let main_sym = test_symbol(file, "main", "function", 1, "fn main()");
    let handler = test_symbol(file, "handler", "function", 3, "pub fn handler()");
    let test_helper = test_symbol(file, "test_helper", "function", 6, "fn test_helper()");
    let impl_sym = test_symbol(file, "Foo", "class", 8, "impl Foo");
    let method_sym = test_symbol(file, "method_like", "method", 10, "fn method_like()");

    let symbols = vec![
        main_sym.clone(),
        handler.clone(),
        test_helper.clone(),
        impl_sym.clone(),
        method_sym.clone(),
    ];
    let input = input(symbols, Vec::new(), CodewikiGraphAvailability::Available);

    // Build an audit context whose entry-point set contains `(file, handler)`.
    let mut entry_points = HashSet::new();
    entry_points.insert((file.to_string(), "handler".to_string()));
    let audit = AuditContext {
        project_root: root.to_path_buf(),
        deprecations: std::collections::BTreeMap::new(),
        tests: std::collections::BTreeSet::new(),
        entry_points,
    };

    let doc = build_dead_code_doc(&input, &audit);
    let names = doc
        .candidates
        .iter()
        .map(|c| c.name.as_str())
        .collect::<Vec<_>>();
    assert!(
        names.is_empty(),
        "main, catalog entry point, test-gated, impl, and method must all be excluded; got {names:?}"
    );
}

#[test]
fn symbol_with_inbound_call_edge_is_not_a_candidate() {
    let dir = tempfile::tempdir().expect("tempdir");
    let root = dir.path();
    let file = "src/lib.rs";
    write_source(root, file, "fn caller() {}\nfn callee() {}\n");

    let caller = test_symbol(file, "caller", "function", 1, "fn caller()");
    let callee = test_symbol(file, "callee", "function", 2, "fn callee()");
    // caller -> callee Call edge: callee has an inbound call, so it is NOT a
    // candidate. caller itself has no inbound call, so it remains a candidate.
    let edge = CodewikiGraphEdge::call(
        test_component_id(file, "caller", "function"),
        test_component_id(file, "callee", "function"),
    );
    let input = input(
        vec![caller.clone(), callee.clone()],
        vec![edge],
        CodewikiGraphAvailability::Available,
    );
    let audit = build_audit_context(root, &input, None);

    let doc = build_dead_code_doc(&input, &audit);
    let names = doc
        .candidates
        .iter()
        .map(|c| c.name.as_str())
        .collect::<Vec<_>>();
    assert!(
        !names.contains(&"callee"),
        "a symbol with an inbound Call edge must not be a candidate; got {names:?}"
    );
    assert!(
        names.contains(&"caller"),
        "the uncalled caller should still be a candidate; got {names:?}"
    );
}

#[test]
fn import_edge_does_not_count_as_an_inbound_call() {
    let dir = tempfile::tempdir().expect("tempdir");
    let root = dir.path();
    let file = "src/lib.rs";
    write_source(root, file, "fn imported() {}\n");

    let imported = test_symbol(file, "imported", "function", 1, "fn imported()");
    // An Import edge targeting the symbol id must NOT shield it from being a
    // candidate — only Call edges count.
    let edge = CodewikiGraphEdge::import(
        "some/other/file".to_string(),
        test_component_id(file, "imported", "function"),
    );
    let input = input(
        vec![imported.clone()],
        vec![edge],
        CodewikiGraphAvailability::Available,
    );
    let audit = build_audit_context(root, &input, None);

    let doc = build_dead_code_doc(&input, &audit);
    assert_eq!(
        doc.candidates.len(),
        1,
        "an import edge must not count as an inbound call"
    );
}

#[test]
fn unavailable_graph_renders_skip_note_and_never_degrades() {
    let dir = tempfile::tempdir().expect("tempdir");
    let root = dir.path();
    let file = "src/lib.rs";
    write_source(root, file, "fn lonely() {}\n");

    let lonely = test_symbol(file, "lonely", "function", 1, "fn lonely()");
    let input = input(
        vec![lonely],
        Vec::new(),
        CodewikiGraphAvailability::Unavailable,
    );
    let audit = build_audit_context(root, &input, None);

    let doc = build_dead_code_doc(&input, &audit);
    assert!(doc.skipped, "unavailable graph must set skipped");
    assert!(
        doc.candidates.is_empty(),
        "no candidates are computed when the graph is unavailable"
    );
    assert!(
        doc.degraded_sources.is_empty(),
        "the dead-code page must never be degraded"
    );

    let page = render_dead_code_doc(&doc);
    assert!(
        page.contains(
            "Dead-code analysis needs the code graph, which was unavailable for this run."
        ),
        "skip note must render:\n{page}"
    );
    assert!(
        !page.contains("lonely"),
        "no candidate may be listed when the graph is unavailable:\n{page}"
    );
    // Non-degrading frontmatter: the page declares no degraded sources.
    assert!(
        !page.contains("degraded: true"),
        "frontmatter must not mark the page degraded:\n{page}"
    );
}

#[test]
fn truncated_graph_adds_incomplete_caveat() {
    let dir = tempfile::tempdir().expect("tempdir");
    let root = dir.path();
    let file = "src/lib.rs";
    write_source(root, file, "fn lonely() {}\n");

    let lonely = test_symbol(file, "lonely", "function", 1, "fn lonely()");
    let input = input(
        vec![lonely],
        Vec::new(),
        CodewikiGraphAvailability::Truncated,
    );
    let audit = build_audit_context(root, &input, None);

    let doc = build_dead_code_doc(&input, &audit);
    assert!(!doc.skipped);
    assert!(doc.truncated);
    let page = render_dead_code_doc(&doc);
    assert!(
        page.contains("truncated"),
        "a truncated graph must add an incomplete caveat:\n{page}"
    );
}

#[test]
fn empty_deprecations_page_renders_cleanly_and_does_not_degrade() {
    let input = input(Vec::new(), Vec::new(), CodewikiGraphAvailability::Available);
    let deprecations = std::collections::BTreeMap::new();
    let doc = build_deprecations_doc(&input, &deprecations);
    assert!(doc.symbols.is_empty());
    assert!(doc.degraded_sources.is_empty());

    let page = render_deprecations_doc(&doc);
    assert!(
        page.contains("No deprecated symbols found."),
        "empty deprecations page must render a clear line:\n{page}"
    );
    assert!(
        !page.contains("degraded: true"),
        "the deprecations page must never be degraded:\n{page}"
    );
}
