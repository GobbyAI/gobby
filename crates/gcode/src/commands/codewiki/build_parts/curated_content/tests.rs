use super::*;

fn module_doc(name: &str, summary: &str) -> ModuleDoc {
    ModuleDoc {
        module: name.to_string(),
        summary: summary.to_string(),
        source_spans: Vec::new(),
        direct_files: Vec::new(),
        child_modules: Vec::new(),
        degraded: false,
        degraded_sources: Vec::new(),
        verify_notes: Vec::new(),
        reused_page: None,
    }
}

fn file_doc(path: &str, summary: &str) -> FileDoc {
    FileDoc {
        path: path.to_string(),
        module: String::new(),
        summary: summary.to_string(),
        body: String::new(),
        source_spans: Vec::new(),
        symbols: Vec::new(),
        component_ids: Vec::new(),
        degraded: false,
        degraded_sources: Vec::new(),
        verify_notes: Vec::new(),
        reused_page: None,
    }
}

fn module_lookup(docs: &[ModuleDoc]) -> BTreeMap<&str, &ModuleDoc> {
    docs.iter().map(|doc| (doc.module.as_str(), doc)).collect()
}

fn file_lookup(docs: &[FileDoc]) -> BTreeMap<&str, &FileDoc> {
    docs.iter().map(|doc| (doc.path.as_str(), doc)).collect()
}

#[test]
fn verifier_evidence_preserves_curated_members_and_symbols() {
    let members = [prompts::PageEvidenceRow {
        name: "walker".to_string(),
        kind: "module".to_string(),
        citation: "[src/walker.rs:10-12]".to_string(),
        summary: "Discovers candidate files.".to_string(),
    }];
    let symbols = [prompts::PageEvidenceRow {
        name: "parse_plan".to_string(),
        kind: "function".to_string(),
        citation: "[src/plan.rs:42]".to_string(),
        summary: "Parses the navigation JSON.".to_string(),
    }];

    let rows = verifier_evidence_rows(members.iter().chain(symbols.iter()));

    assert_eq!(rows.len(), 2);
    assert_eq!(rows[0].name, "walker");
    assert_eq!(rows[0].kind, "module");
    assert_eq!(rows[0].component_label, "[src/walker.rs:10-12]");
    assert_eq!((rows[0].line_start, rows[0].line_end), (10, 12));
    assert_eq!(rows[0].purpose, "Discovers candidate files.");
    assert_eq!(rows[1].name, "parse_plan");
    assert_eq!(rows[1].component_label, "[src/plan.rs:42]");
    assert_eq!((rows[1].line_start, rows[1].line_end), (42, 42));
}

#[test]
fn has_required_curated_sections_matches_exact_h2_titles_only() {
    let valid = "\
## Purpose

## How it works

## Key components

## Failure modes

## How to change it

## What to read next
";
    assert!(has_required_curated_sections(
        CuratedPageKind::Concept,
        valid
    ));

    let false_subheading = valid.replacen("## Purpose", "### Purpose", 1);
    assert!(!has_required_curated_sections(
        CuratedPageKind::Concept,
        &false_subheading
    ));

    let false_prefix = valid.replacen("## Purpose", "## Purposeful", 1);
    assert!(!has_required_curated_sections(
        CuratedPageKind::Concept,
        &false_prefix
    ));
}

#[test]
fn chains_members_when_a_data_flow_is_documented() {
    let modules = [
        module_doc(
            "walker",
            "Discovers candidate files. Pipeline: walker -> parser.",
        ),
        module_doc("parser", "Extracts the AST via tree-sitter."),
    ];
    let member_modules = vec!["walker".to_string(), "parser".to_string()];

    let flow = curated_flow_diagram(
        &member_modules,
        &[],
        &module_lookup(&modules),
        &file_lookup(&[]),
        &BTreeMap::new(),
    );
    let section = flow.expect("flow drawn for two documented members");

    assert!(section.contains("## Conceptual flow"), "{section}");
    assert!(section.contains("flowchart LR"), "{section}");
    assert!(
        section.contains("walker — Discovers candidate files"),
        "{section}"
    );
    assert!(
        section.contains("parser — Extracts the AST via tree-sitter"),
        "{section}"
    );
    assert!(
        section.contains("ordered by the data flow documented"),
        "{section}"
    );
}

#[test]
fn suppressed_when_no_data_flow_is_documented() {
    // Two grounded members but no `A -> B` chain anywhere in the evidence: a
    // declaration-order diagram would fabricate a flow, so none is emitted.
    let modules = [
        module_doc("walker", "Discovers candidate files. Walks the tree."),
        module_doc("parser", "Extracts the AST via tree-sitter."),
    ];
    let member_modules = vec!["walker".to_string(), "parser".to_string()];

    let flow = curated_flow_diagram(
        &member_modules,
        &[],
        &module_lookup(&modules),
        &file_lookup(&[]),
        &BTreeMap::new(),
    );

    assert!(flow.is_none(), "{flow:?}");
}

#[test]
fn drops_members_outside_the_documented_chain() {
    // The documented chain names walker and parser only; chunker must not be
    // drawn — an edge to it would be fabricated.
    let modules = [
        module_doc("walker", "Discovers files. Flow: walker -> parser."),
        module_doc("parser", "Extracts the AST."),
        module_doc("chunker", "Splits content for search."),
    ];
    let member_modules = vec![
        "walker".to_string(),
        "parser".to_string(),
        "chunker".to_string(),
    ];

    let flow = curated_flow_diagram(
        &member_modules,
        &[],
        &module_lookup(&modules),
        &file_lookup(&[]),
        &BTreeMap::new(),
    );
    let section = flow.expect("flow drawn for the documented pair");

    assert!(section.contains("s0[\"walker"), "{section}");
    assert!(section.contains("s1[\"parser"), "{section}");
    assert!(!section.contains("chunker"), "{section}");
    assert!(section.contains("s0 --> s1"), "{section}");
    assert!(!section.contains("s1 --> s2"), "{section}");
}

#[test]
fn orders_by_documented_data_flow_when_present() {
    let modules = [
        module_doc(
            "indexer",
            "Writes hub rows. Pipeline: walker -> parser -> chunker -> indexer.",
        ),
        module_doc("walker", "Discovers files."),
        module_doc("parser", "Extracts the AST."),
        module_doc("chunker", "Splits content for search."),
    ];
    let member_modules = vec![
        "indexer".to_string(),
        "walker".to_string(),
        "parser".to_string(),
        "chunker".to_string(),
    ];

    let flow = curated_flow_diagram(
        &member_modules,
        &[],
        &module_lookup(&modules),
        &file_lookup(&[]),
        &BTreeMap::new(),
    );
    let section = flow.expect("flow drawn");

    assert!(
        section.contains("ordered by the data flow documented"),
        "{section}"
    );
    assert!(
        section.contains("s0[\"walker — Discovers files\"]"),
        "{section}"
    );
    assert!(section.contains("s3[\"indexer"), "indexer last: {section}");
}

#[test]
fn marks_degraded_when_a_member_summary_is_missing() {
    let modules = [
        module_doc("walker", "Discovers files. Flow: walker -> parser."),
        module_doc("parser", ""),
    ];
    let member_modules = vec!["walker".to_string(), "parser".to_string()];

    let flow = curated_flow_diagram(
        &member_modules,
        &[],
        &module_lookup(&modules),
        &file_lookup(&[]),
        &BTreeMap::new(),
    );
    let section = flow.expect("flow drawn");

    assert!(section.contains("_Degraded:_"), "{section}");
    assert!(
        section.contains("s1[\"parser\"]"),
        "name-only node: {section}"
    );
}

#[test]
fn omitted_for_a_single_member() {
    let modules = [module_doc("walker", "Discovers files.")];
    let flow = curated_flow_diagram(
        &["walker".to_string()],
        &[],
        &module_lookup(&modules),
        &file_lookup(&[]),
        &BTreeMap::new(),
    );
    assert!(flow.is_none());
}

#[test]
fn falls_back_to_files_without_enough_modules() {
    let files = [
        file_doc(
            "src/bm25.rs",
            "Runs BM25 keyword search. Flow: bm25 -> rrf.",
        ),
        file_doc("src/rrf.rs", "Fuses ranked results."),
    ];
    let member_files = vec!["src/bm25.rs".to_string(), "src/rrf.rs".to_string()];

    let flow = curated_flow_diagram(
        &[],
        &member_files,
        &module_lookup(&[]),
        &file_lookup(&files),
        &BTreeMap::new(),
    );
    let section = flow.expect("flow drawn from files");

    assert!(
        section.contains("bm25 — Runs BM25 keyword search"),
        "{section}"
    );
    assert!(section.contains("rrf — Fuses ranked results"), "{section}");
}

#[test]
fn role_phrase_keeps_short_clause_whole() {
    assert_eq!(
        role_phrase("Discovers candidate files. Walks the tree."),
        Some("Discovers candidate files".to_string())
    );
    // Commas inside a clause that fits the cap survive untouched.
    assert_eq!(
        role_phrase("Runs BM25, RRF, and graph boost."),
        Some("Runs BM25, RRF, and graph boost".to_string())
    );
}

#[test]
fn role_phrase_clips_overlong_clause_at_a_comma_not_mid_thought() {
    let summary = "Coordinates the build pipeline stages, resolving symbols and emitting page bodies \
for every indexed module.";
    assert_eq!(
        role_phrase(summary),
        Some("Coordinates the build pipeline stages".to_string())
    );
}

#[test]
fn role_phrase_drops_overlong_clause_without_a_boundary() {
    // Eleven words, no comma: truncating would leave a mid-thought fragment,
    // so the stage falls back to name-only (degraded) instead.
    let summary =
        "Coordinates every build pipeline stage while resolving symbols across all modules";
    assert_eq!(role_phrase(summary), None);
    assert_eq!(role_phrase(""), None);
}
