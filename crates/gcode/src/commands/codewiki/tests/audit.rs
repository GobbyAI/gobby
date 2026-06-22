//! Tests for the deterministic codewiki deprecation audit page (#889):
//! deprecation markers and badge. Fixtures are built directly (symbols and a
//! tiny tempdir source file for the scan) so the assertions are decoupled from
//! the real workspace.

use super::support::test_symbol;
use super::*;

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
    let audit = build_audit_context(root, &input);

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
