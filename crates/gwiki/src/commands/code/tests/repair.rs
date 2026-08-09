use std::collections::BTreeMap;

use super::super::io::write_codewiki_meta;
use super::support::*;
use super::*;

/// Resolver fake so [`reanchor_citations`] can be exercised with no index, no
/// snapshot, and — by construction — no generator. `current` lists spans that
/// are still accurate; `moved` maps a stale `(file, old_start)` to the symbol's
/// new span.
struct FakeResolver {
    current: Vec<(String, usize, usize)>,
    moved: BTreeMap<(String, usize), (usize, usize)>,
}

impl CitationResolver for FakeResolver {
    fn is_current(&self, file: &str, line_start: usize, line_end: usize) -> bool {
        self.current
            .iter()
            .any(|(f, s, e)| f == file && *s <= line_start && line_end <= *e)
    }

    fn resolve(&self, file: &str, line_start: usize) -> Option<(usize, usize)> {
        self.moved.get(&(file.to_string(), line_start)).copied()
    }
}

fn snapshot_of(symbols: &[(&str, &str, &str, usize)]) -> CodewikiIndexSnapshot {
    let mut map = BTreeMap::new();
    for (index, (file, qualified_name, kind, line_start)) in symbols.iter().enumerate() {
        map.insert(
            format!("id-{index}"),
            CodewikiSymbolSnapshot {
                file_path: file.to_string(),
                name: qualified_name.to_string(),
                qualified_name: qualified_name.to_string(),
                kind: kind.to_string(),
                line_start: *line_start,
            },
        );
    }
    CodewikiIndexSnapshot {
        files: BTreeMap::new(),
        symbols: map,
        graph_neighborhoods: None,
        degraded_sources: Vec::new(),
    }
}

#[test]
fn reanchor_primitive_repairs_moves_keeps_current_and_flags_unresolved_without_a_generator() {
    let resolver = FakeResolver {
        // `[a.rs:5]` still falls inside a current symbol span, so it is accurate.
        current: vec![("a.rs".to_string(), 5, 5)],
        // The `b.rs` symbol cited at line 1 now lives at 9-11.
        moved: BTreeMap::from([(("b.rs".to_string(), 1), (9, 11))]),
    };

    let text = "keep [a.rs:5], move [b.rs:1], drop [c.rs:7-8], marker [3]";
    let result = reanchor_citations(text, &resolver);

    assert_eq!(result.repaired, 1, "only the b.rs citation re-anchors");
    assert_eq!(result.unresolved, 1, "the c.rs citation cannot be resolved");
    assert_eq!(
        result.text, "keep [a.rs:5], move [b.rs:9-11], drop [c.rs:7-8], marker [3]",
        "accurate citation and `[3]` reference marker stay verbatim"
    );
}

#[test]
fn repair_reanchors_moved_citation_and_counts_unresolved() {
    let dir = tempfile::tempdir().expect("tempdir");
    let out_dir = dir.path().to_path_buf();

    // A page generated when `foo` was at 10-12 and `bar` at 30-31, citing both
    // plus a `[2]` reference marker that must be left alone. No generator is
    // available anywhere in this test — repair takes no generator seam.
    let doc_path = "code/files/src/lib.rs.md";
    let page = "# src/lib.rs\n\n\
        The foo helper validates input. [src/lib.rs:10-12]\n\n\
        The bar helper formats output. [src/lib.rs:30-31]\n\n\
        See note [2] for details.\n";
    write_doc(&out_dir, doc_path, page).expect("write page");

    let snapshot = snapshot_of(&[
        ("src/lib.rs", "foo", "function", 10),
        ("src/lib.rs", "bar", "function", 30),
    ]);
    let meta = CodewikiMeta {
        docs: BTreeMap::from([(doc_path.to_string(), CodewikiDocMeta::default())]),
        index_snapshot: Some(snapshot),
        ..Default::default()
    };
    write_codewiki_meta(&out_dir, &meta).expect("write meta");

    // Current index: `foo` moved to 20-22; `bar` was deleted.
    let symbols = vec![test_symbol_range(
        "src/lib.rs",
        "foo",
        "function",
        20,
        22,
        "fn foo()",
    )];

    let summary = repair_citations(&out_dir, &symbols).expect("repair");

    assert_eq!(summary.pages_scanned, 1);
    assert_eq!(summary.pages_repaired, 1);
    assert_eq!(
        summary.citations_repaired, 1,
        "foo re-anchors 10-12 -> 20-22"
    );
    assert_eq!(summary.citations_unresolved, 1, "bar was deleted");

    let repaired = std::fs::read_to_string(out_dir.join(doc_path)).expect("read repaired page");
    assert!(
        repaired.contains("[src/lib.rs:20-22]"),
        "foo citation re-anchored to its current span"
    );
    assert!(
        !repaired.contains("[src/lib.rs:10-12]"),
        "stale foo citation is gone"
    );
    assert!(
        repaired.contains("[src/lib.rs:30-31]"),
        "unresolved bar citation is left in place for a human to investigate"
    );
    assert!(
        repaired.contains("See note [2]"),
        "reference marker is not mistaken for a citation"
    );
}

#[test]
fn repair_leaves_accurate_pages_untouched() {
    let dir = tempfile::tempdir().expect("tempdir");
    let out_dir = dir.path().to_path_buf();

    let doc_path = "code/files/src/lib.rs.md";
    let page = "# src/lib.rs\n\nThe foo helper validates input. [src/lib.rs:20-22]\n";
    write_doc(&out_dir, doc_path, page).expect("write page");

    let snapshot = snapshot_of(&[("src/lib.rs", "foo", "function", 20)]);
    let meta = CodewikiMeta {
        docs: BTreeMap::from([(doc_path.to_string(), CodewikiDocMeta::default())]),
        index_snapshot: Some(snapshot),
        ..Default::default()
    };
    write_codewiki_meta(&out_dir, &meta).expect("write meta");

    // The citation already matches the current symbol span, so nothing moves.
    let symbols = vec![test_symbol_range(
        "src/lib.rs",
        "foo",
        "function",
        20,
        22,
        "fn foo()",
    )];

    let summary = repair_citations(&out_dir, &symbols).expect("repair");

    assert_eq!(summary.pages_scanned, 1);
    assert_eq!(
        summary.pages_repaired, 0,
        "an accurate page is not rewritten"
    );
    assert_eq!(summary.citations_repaired, 0);
    assert_eq!(summary.citations_unresolved, 0);

    let after = std::fs::read_to_string(out_dir.join(doc_path)).expect("read page");
    assert_eq!(after, page, "accurate page is byte-for-byte unchanged");
}
