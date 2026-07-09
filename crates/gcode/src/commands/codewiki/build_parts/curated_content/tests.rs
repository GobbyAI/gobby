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

/// Drive `curated_flow_diagram` with a scripted composer. Each response is
/// consumed in order; an exhausted script returns `None` (generation skipped).
#[allow(clippy::too_many_arguments)]
fn compose_flow(
    responses: &[&str],
    member_modules: &[String],
    member_files: &[String],
    modules: &[ModuleDoc],
    files: &[FileDoc],
    graph_edges: &[CodewikiGraphEdge],
) -> Option<String> {
    let mut responses: Vec<String> = responses.iter().map(|s| s.to_string()).collect();
    let mut generator = move |_prompt: &str, system: &str, _tier: PromptTier| {
        assert_eq!(system, prompts::FLOW_DIAGRAM_SYSTEM);
        (!responses.is_empty()).then(|| responses.remove(0))
    };
    let mut generate: Option<&mut TextGenerator<'_>> = Some(&mut generator);
    curated_flow_diagram(
        member_modules,
        member_files,
        &module_lookup(modules),
        &file_lookup(files),
        &BTreeMap::new(),
        graph_edges,
        &mut generate,
    )
}

#[test]
fn composes_flow_when_a_data_flow_is_documented() {
    let modules = [
        module_doc(
            "walker",
            "Discovers candidate files. Pipeline: walker -> parser.",
        ),
        module_doc("parser", "Extracts the AST via tree-sitter."),
    ];
    let member_modules = vec!["walker".to_string(), "parser".to_string()];

    let section = compose_flow(
        &["flowchart LR\n    s0 --> s1\n"],
        &member_modules,
        &[],
        &modules,
        &[],
        &[],
    )
    .expect("flow drawn for two documented members");

    assert!(section.contains("## Conceptual flow"), "{section}");
    assert!(section.contains("flowchart LR"), "{section}");
    // Node labels come from the evidence (member name + grounded role), not
    // from whatever the model wrote.
    assert!(
        section.contains("s0[\"walker — Discovers candidate files\"]"),
        "{section}"
    );
    assert!(
        section.contains("s1[\"parser — Extracts the AST via tree-sitter\"]"),
        "{section}"
    );
    assert!(section.contains("s0 --> s1"), "{section}");
    // The caption states the evidence contract.
    assert!(
        section.contains("composed by the model from supplied evidence only"),
        "{section}"
    );
    assert!(
        section.contains("verified against that evidence"),
        "{section}"
    );
}

#[test]
fn suppressed_without_any_evidenced_edges() {
    // Two grounded members but no `A -> B` chain and no code-index edges:
    // any diagram would fabricate a flow, so the composer is never invoked.
    let modules = [
        module_doc("walker", "Discovers candidate files. Walks the tree."),
        module_doc("parser", "Extracts the AST via tree-sitter."),
    ];
    let member_modules = vec!["walker".to_string(), "parser".to_string()];

    let flow = compose_flow(
        &["flowchart LR\n    s0 --> s1\n"],
        &member_modules,
        &[],
        &modules,
        &[],
        &[],
    );

    assert!(flow.is_none(), "{flow:?}");
}

#[test]
fn unevidenced_arrow_to_an_unchained_member_is_rejected() {
    // The documented chain names walker and parser only; the model also draws
    // an arrow into chunker, which matches no evidence edge — rejected on the
    // repair attempt and dropped deterministically.
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

    let overdrawn = "flowchart LR\n    s0 --> s1\n    s1 --> s2\n";
    let section = compose_flow(
        &[overdrawn, overdrawn],
        &member_modules,
        &[],
        &modules,
        &[],
        &[],
    )
    .expect("flow drawn for the documented pair");

    assert!(section.contains("s0[\"walker"), "{section}");
    assert!(section.contains("s1[\"parser"), "{section}");
    assert!(!section.contains("chunker"), "{section}");
    assert!(section.contains("s0 --> s1"), "{section}");
    assert!(!section.contains("s1 --> s2"), "{section}");
}

#[test]
fn documented_chain_supplies_edges_in_documented_order() {
    // Members arrive indexer-first, but the documented pipeline evidences
    // walker (s1) -> parser (s2) -> chunker (s3) -> indexer (s0); the model
    // may draw exactly that chain and nothing else.
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

    let section = compose_flow(
        &["flowchart LR\n    s1 --> s2\n    s2 --> s3\n    s3 --> s0\n"],
        &member_modules,
        &[],
        &modules,
        &[],
        &[],
    )
    .expect("flow drawn");

    assert!(
        section.contains("s1[\"walker — Discovers files\"]"),
        "{section}"
    );
    assert!(section.contains("s1 --> s2"), "{section}");
    assert!(section.contains("s2 --> s3"), "{section}");
    assert!(section.contains("s3 --> s0"), "{section}");
}

#[test]
fn code_index_edges_evidence_a_flow_without_a_documented_chain() {
    // No documented `A -> B` chain anywhere, but the code index shows a real
    // cross-member call edge — that is evidence enough, and the arrow carries
    // the `calls` label.
    let modules = [
        module_doc("walker", "Discovers candidate files."),
        module_doc("parser", "Extracts the AST."),
    ];
    let mut walker_file = file_doc("src/walker.rs", "Walks the tree.");
    walker_file.module = "walker".to_string();
    walker_file.component_ids = vec!["comp_walker".to_string()];
    let mut parser_file = file_doc("src/parser.rs", "Parses files.");
    parser_file.module = "parser".to_string();
    parser_file.component_ids = vec!["comp_parser".to_string()];
    let files = [walker_file, parser_file];
    let member_modules = vec!["walker".to_string(), "parser".to_string()];
    let graph_edges = [CodewikiGraphEdge::call("comp_walker", "comp_parser")];

    let section = compose_flow(
        &["flowchart LR\n    s0 --> s1\n"],
        &member_modules,
        &[],
        &modules,
        &files,
        &graph_edges,
    )
    .expect("flow drawn from code-index evidence");

    assert!(
        section.contains("s0 -->|\"calls\"| s1"),
        "call edge must carry its label: {section}"
    );
}

#[test]
fn documented_chain_and_graph_edge_do_not_duplicate_stage_pair() {
    let modules = [
        module_doc(
            "walker",
            "Discovers candidate files. Flow: walker -> parser.",
        ),
        module_doc("parser", "Extracts the AST."),
    ];
    let mut walker_file = file_doc("src/walker.rs", "Walks the tree.");
    walker_file.module = "walker".to_string();
    walker_file.component_ids = vec!["comp_walker".to_string()];
    let mut parser_file = file_doc("src/parser.rs", "Parses files.");
    parser_file.module = "parser".to_string();
    parser_file.component_ids = vec!["comp_parser".to_string()];
    let files = [walker_file, parser_file];
    let member_modules = vec!["walker".to_string(), "parser".to_string()];
    let graph_edges = [CodewikiGraphEdge::call("comp_walker", "comp_parser")];

    let section = compose_flow(
        &["flowchart LR\n    s0 --> s1\n"],
        &member_modules,
        &[],
        &modules,
        &files,
        &graph_edges,
    )
    .expect("flow drawn from chain evidence");

    assert!(section.contains("s0 --> s1"), "{section}");
    assert!(
        !section.contains("calls"),
        "same source/target stage edge should not be added twice: {section}"
    );
}

#[test]
fn marks_degraded_when_a_member_summary_is_missing() {
    let modules = [
        module_doc("walker", "Discovers files. Flow: walker -> parser."),
        module_doc("parser", ""),
    ];
    let member_modules = vec!["walker".to_string(), "parser".to_string()];

    let section = compose_flow(
        &["flowchart LR\n    s0 --> s1\n"],
        &member_modules,
        &[],
        &modules,
        &[],
        &[],
    )
    .expect("flow drawn");

    assert!(section.contains("_Degraded:_"), "{section}");
    assert!(
        section.contains("s1[\"parser\"]"),
        "name-only node: {section}"
    );
}

#[test]
fn omitted_for_a_single_member() {
    let modules = [module_doc("walker", "Discovers files.")];
    let flow = compose_flow(
        &["flowchart LR\n    s0 --> s1\n"],
        &["walker".to_string()],
        &[],
        &modules,
        &[],
        &[],
    );
    assert!(flow.is_none());
}

#[test]
fn omitted_when_no_generator_is_available() {
    // AI off: documented evidence exists, but with no composer there is no
    // diagram — deterministic chaining would bypass the LLM-composed contract.
    let modules = [
        module_doc("walker", "Discovers files. Flow: walker -> parser."),
        module_doc("parser", "Extracts the AST."),
    ];
    let member_modules = vec!["walker".to_string(), "parser".to_string()];
    let mut generate: Option<&mut TextGenerator<'_>> = None;
    let flow = curated_flow_diagram(
        &member_modules,
        &[],
        &module_lookup(&modules),
        &file_lookup(&[]),
        &BTreeMap::new(),
        &[],
        &mut generate,
    );
    assert!(flow.is_none());
}

#[test]
fn falls_back_to_files_without_enough_modules() {
    let modules = [module_doc("search", "Coordinates search ranking.")];
    let mut bm25 = file_doc(
        "src/bm25.rs",
        "Runs BM25 keyword search. Flow: bm25 -> rrf.",
    );
    bm25.module = "search".to_string();
    bm25.component_ids = vec!["comp_bm25".to_string()];
    let mut rrf = file_doc("src/rrf.rs", "Fuses ranked results.");
    rrf.module = "search".to_string();
    rrf.component_ids = vec!["comp_rrf".to_string()];
    let files = [bm25, rrf];
    let member_modules = vec!["search".to_string()];
    let member_files = vec!["src/bm25.rs".to_string(), "src/rrf.rs".to_string()];
    let graph_edges = [CodewikiGraphEdge::call("comp_bm25", "comp_rrf")];

    let section = compose_flow(
        &["flowchart LR\n    s1 -->|\"calls\"| s2\n"],
        &member_modules,
        &member_files,
        &modules,
        &files,
        &graph_edges,
    )
    .expect("flow drawn from files");

    assert!(
        section.contains("bm25 — Runs BM25 keyword search"),
        "{section}"
    );
    assert!(section.contains("rrf — Fuses ranked results"), "{section}");
    assert!(
        section.contains("s1 --> s2"),
        "fallback file stages must own their components: {section}"
    );
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
