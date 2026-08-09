use super::*;

fn module_doc(name: &str, summary: &str) -> ModuleDoc {
    ModuleDoc {
        module: name.to_string(),
        summary: summary.to_string(),
        source_spans: Vec::new(),
        direct_files: Vec::new(),
        child_modules: Vec::new(),
        dependency_diagram: None,
        call_sequence_diagram: None,
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

fn module_link(name: &str) -> ModuleLink {
    ModuleLink {
        module: name.to_string(),
        summary: format!("{name} summary"),
        source_spans: Vec::new(),
    }
}

fn file_link(path: &str) -> FileLink {
    FileLink {
        path: path.to_string(),
        summary: format!("{path} summary"),
        source_spans: Vec::new(),
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
    let mut diagram_stats = DiagramStats::default();
    let mut progress = CodewikiProgress::silent();
    let module_lookup = module_lookup(modules);
    let file_lookup = file_lookup(files);
    let leading_chunks = BTreeMap::new();
    curated_flow_diagram(
        member_modules,
        member_files,
        &mut generate,
        CuratedFlowContext {
            page_path: "code/concepts/test.md",
            module_lookup: &module_lookup,
            file_lookup: &file_lookup,
            leading_chunks: &leading_chunks,
            graph_edges,
            diagram_stats: &mut diagram_stats,
            progress: &mut progress,
        },
    )
}

fn compose_flow_with_observability(
    responses: &[&str],
    member_modules: &[String],
    member_files: &[String],
    modules: &[ModuleDoc],
    files: &[FileDoc],
    graph_edges: &[CodewikiGraphEdge],
) -> (Option<String>, DiagramStats, Vec<String>) {
    let mut responses: Vec<String> = responses.iter().map(|s| s.to_string()).collect();
    let mut generator = move |_prompt: &str, system: &str, _tier: PromptTier| {
        assert_eq!(system, prompts::FLOW_DIAGRAM_SYSTEM);
        (!responses.is_empty()).then(|| responses.remove(0))
    };
    let mut generate: Option<&mut TextGenerator<'_>> = Some(&mut generator);
    let mut diagram_stats = DiagramStats::default();
    let mut progress = CodewikiProgress::capture();
    let module_lookup = module_lookup(modules);
    let file_lookup = file_lookup(files);
    let leading_chunks = BTreeMap::new();
    let flow = curated_flow_diagram(
        member_modules,
        member_files,
        &mut generate,
        CuratedFlowContext {
            page_path: "code/concepts/test.md",
            module_lookup: &module_lookup,
            file_lookup: &file_lookup,
            leading_chunks: &leading_chunks,
            graph_edges,
            diagram_stats: &mut diagram_stats,
            progress: &mut progress,
        },
    );
    (flow, diagram_stats, progress.into_lines())
}

fn mermaid_fence(section: &str) -> &str {
    let start = section.find("```mermaid").expect("mermaid fence");
    let closing = section[start + "```mermaid".len()..]
        .find("```")
        .expect("closing mermaid fence");
    &section[start..start + "```mermaid".len() + closing + "```".len()]
}

fn mermaid_node_count(section: &str) -> usize {
    section
        .lines()
        .filter(|line| line.trim_start().contains("[\"") && !line.contains("-->"))
        .count()
}

#[test]
fn curated_flow_child_evidence_pass_emits_one_final_slot_outcome() {
    let mut root = module_doc("root", "Groups two internal stages.");
    root.child_modules = vec![module_link("root::b"), module_link("root::a")];
    let mut child_a = module_doc("root::a", "Discovers candidate files.");
    child_a.direct_files = vec![file_link("src/a.rs")];
    let mut child_b = module_doc("root::b", "Parses candidate files.");
    child_b.direct_files = vec![file_link("src/b.rs")];
    let modules = [root, child_b, child_a];

    let mut file_a = file_doc("src/a.rs", "Discovers candidates.");
    file_a.module = "root::a".to_string();
    file_a.component_ids = vec!["component_a".to_string()];
    let mut file_b = file_doc("src/b.rs", "Parses candidates.");
    file_b.module = "root::b".to_string();
    file_b.component_ids = vec!["component_b".to_string()];
    let files = [file_a, file_b];
    let member_modules = ["root".to_string()];
    let graph_edges = [CodewikiGraphEdge::call("component_a", "component_b")];

    let (section, stats, progress) = compose_flow_with_observability(
        &["flowchart LR\n    s0 --> s1\n"],
        &member_modules,
        &[],
        &modules,
        &files,
        &graph_edges,
    );
    let section = section.expect("child-level evidence should draw the flow");

    assert!(section.contains("child-level roll-up"), "{section}");
    assert!(is_valid_mermaid(mermaid_fence(&section)), "{section}");
    assert_eq!(stats.emitted, 1);
    assert_eq!(stats.total(), 1);
    assert_eq!(
        progress,
        vec![
            "codewiki: diagram code/concepts/test.md [curated_flow]: emitted (pass 2 child evidence)"
        ]
    );
}

#[test]
fn child_flow_inputs_rank_by_direct_file_count_and_cap_ten() {
    let mut root = module_doc("root", "Groups many internal stages.");
    root.child_modules = (0..12)
        .rev()
        .map(|index| module_link(&format!("root::child_{index:02}")))
        .collect();
    let mut modules = vec![root];
    modules.extend((0..12).map(|index| {
        let mut child = module_doc(&format!("root::child_{index:02}"), "Child stage.");
        child.direct_files = (0..index)
            .map(|file| file_link(&format!("src/child_{index:02}_{file:02}.rs")))
            .collect();
        child
    }));
    let lookup = module_lookup(&modules);

    let inputs = child_flow_inputs(&["root".to_string()], &lookup, &file_lookup(&[]));

    assert_eq!(inputs.modules.len(), 10);
    assert_eq!(
        inputs.modules.first().map(String::as_str),
        Some("root::child_11")
    );
    assert_eq!(
        inputs.modules.last().map(String::as_str),
        Some("root::child_02")
    );
    assert!(inputs.files.is_empty());
}

#[test]
fn curated_flow_sparse_fallback_is_valid_bounded_and_skips_the_generator() {
    let mut root = module_doc("root", "Groups edge-free children.");
    root.child_modules = (0..15)
        .rev()
        .map(|index| module_link(&format!("root::child_{index:02}")))
        .collect();
    let mut modules = vec![root];
    modules.extend(
        (0..15).map(|index| module_doc(&format!("root::child_{index:02}"), "Child stage.")),
    );
    let member_modules = ["root".to_string()];
    let mut generator = |_prompt: &str, _system: &str, _tier: PromptTier| -> Option<String> {
        panic!("sparse evidence must not call the generator")
    };
    let mut generate: Option<&mut TextGenerator<'_>> = Some(&mut generator);
    let mut stats = DiagramStats::default();
    let mut progress = CodewikiProgress::capture();
    let module_lookup = module_lookup(&modules);
    let file_lookup = file_lookup(&[]);
    let leading_chunks = BTreeMap::new();

    let section = curated_flow_diagram(
        &member_modules,
        &[],
        &mut generate,
        CuratedFlowContext {
            page_path: "code/concepts/test.md",
            module_lookup: &module_lookup,
            file_lookup: &file_lookup,
            leading_chunks: &leading_chunks,
            graph_edges: &[],
            diagram_stats: &mut stats,
            progress: &mut progress,
        },
    )
    .expect("edge-free pages should receive a containment map");

    assert!(section.contains(
        "> Structure map — containment from the module tree; no cross-member call/import edges were found in the index. This shows structure, not runtime flow."
    ));
    assert_eq!(mermaid_node_count(&section), 12, "{section}");
    assert!(is_valid_mermaid(mermaid_fence(&section)), "{section}");
    assert_eq!(stats.sparse_evidence, 1);
    assert_eq!(stats.total(), 1);
    assert_eq!(
        progress.into_lines(),
        vec![
            "codewiki: diagram code/concepts/test.md [curated_flow]: sparse_evidence (pass 3 containment fallback)"
        ]
    );
}

#[test]
fn curated_flow_no_generator_fallback_has_reason_aware_caption() {
    let modules = [
        module_doc("walker", "Discovers files. Flow: walker -> parser."),
        module_doc("parser", "Extracts the AST."),
    ];
    let member_modules = ["walker".to_string(), "parser".to_string()];
    let mut generate: Option<&mut TextGenerator<'_>> = None;
    let mut stats = DiagramStats::default();
    let mut progress = CodewikiProgress::capture();
    let module_lookup = module_lookup(&modules);
    let file_lookup = file_lookup(&[]);
    let leading_chunks = BTreeMap::new();

    let section = curated_flow_diagram(
        &member_modules,
        &[],
        &mut generate,
        CuratedFlowContext {
            page_path: "code/concepts/test.md",
            module_lookup: &module_lookup,
            file_lookup: &file_lookup,
            leading_chunks: &leading_chunks,
            graph_edges: &[],
            diagram_stats: &mut stats,
            progress: &mut progress,
        },
    )
    .expect("AI-off pages should receive a containment map");

    assert!(section.contains(
        "> Structure map — containment from the module tree; no diagram generator was available. This shows structure, not runtime flow."
    ));
    assert!(!section.contains("no cross-member call/import edges"));
    assert!(is_valid_mermaid(mermaid_fence(&section)), "{section}");
    assert_eq!(stats.no_generator, 1);
    assert_eq!(stats.total(), 1);
    assert_eq!(
        progress.into_lines(),
        vec![
            "codewiki: diagram code/concepts/test.md [curated_flow]: no_generator (pass 3 containment fallback)"
        ]
    );
}

#[test]
fn curated_flow_rejected_fallback_has_reason_aware_caption_and_one_outcome() {
    let modules = [
        module_doc("walker", "Discovers files. Flow: walker -> parser."),
        module_doc("parser", "Extracts the AST."),
    ];
    let member_modules = ["walker".to_string(), "parser".to_string()];
    let invalid = "flowchart TD\n    ghost --> s0\n";
    let (section, stats, progress) = compose_flow_with_observability(
        &[invalid, invalid],
        &member_modules,
        &[],
        &modules,
        &[],
        &[],
    );
    let section = section.expect("rejected diagrams should receive a containment map");

    assert!(section.contains(
        "> Structure map — containment from the module tree; the generated flow diagram failed verification and was discarded. This shows structure, not runtime flow."
    ));
    assert!(!section.contains("no cross-member call/import edges"));
    assert!(is_valid_mermaid(mermaid_fence(&section)), "{section}");
    assert_eq!(stats.rejected, 1);
    assert_eq!(stats.total(), 1);
    assert_eq!(
        progress,
        vec![
            "codewiki: diagram code/concepts/test.md [curated_flow]: rejected (pass 3 containment fallback)"
        ]
    );
}

#[test]
fn curated_flow_containment_fallback_is_permutation_invariant() {
    let mut root_a = module_doc("root", "Groups edge-free children.");
    root_a.child_modules = vec![module_link("root::zeta"), module_link("root::alpha")];
    root_a.direct_files = vec![file_link("src/z.rs"), file_link("src/a.rs")];
    let mut root_b = module_doc("root", "Groups edge-free children.");
    root_b.child_modules = vec![module_link("root::alpha"), module_link("root::zeta")];
    root_b.direct_files = vec![file_link("src/a.rs"), file_link("src/z.rs")];
    let child_alpha = module_doc("root::alpha", "Alpha child.");
    let child_zeta = module_doc("root::zeta", "Zeta child.");
    let member_modules = ["root".to_string()];

    let first = compose_flow(
        &[],
        &member_modules,
        &[],
        &[root_a, child_zeta, child_alpha],
        &[],
        &[],
    )
    .expect("first containment map");
    let second = compose_flow(
        &[],
        &member_modules,
        &[],
        &[
            root_b,
            module_doc("root::alpha", "Alpha child."),
            module_doc("root::zeta", "Zeta child."),
        ],
        &[],
        &[],
    )
    .expect("second containment map");

    assert_eq!(first, second);
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
fn uses_structure_fallback_without_any_evidenced_edges() {
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

    let flow = flow.expect("edge-free page should receive containment fallback");
    assert!(flow.contains("no cross-member call/import edges were found in the index"));
    assert!(is_valid_mermaid(mermaid_fence(&flow)), "{flow}");
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
fn diagram_outcomes_record_sparse_curated_flow_slots() {
    let modules = [module_doc("walker", "Discovers files.")];
    let member_modules = ["walker".to_string()];
    let mut generate: Option<&mut TextGenerator<'_>> = None;
    let mut diagram_stats = DiagramStats::default();
    let mut progress = CodewikiProgress::capture();
    let module_lookup = module_lookup(&modules);
    let file_lookup = file_lookup(&[]);
    let leading_chunks = BTreeMap::new();
    let flow = curated_flow_diagram(
        &member_modules,
        &[],
        &mut generate,
        CuratedFlowContext {
            page_path: "code/concepts/walker.md",
            module_lookup: &module_lookup,
            file_lookup: &file_lookup,
            leading_chunks: &leading_chunks,
            graph_edges: &[],
            diagram_stats: &mut diagram_stats,
            progress: &mut progress,
        },
    );
    let flow = flow.expect("sparse slot should receive containment fallback");
    assert!(flow.contains("no cross-member call/import edges were found in the index"));
    assert_eq!(diagram_stats.sparse_evidence, 1);
    assert_eq!(diagram_stats.total(), 1);
    assert_eq!(
        progress.into_lines(),
        vec![
            "codewiki: diagram code/concepts/walker.md [curated_flow]: sparse_evidence (pass 3 containment fallback)"
        ]
    );
}

#[test]
fn single_member_gets_containment_fallback() {
    let modules = [module_doc("walker", "Discovers files.")];
    let flow = compose_flow(
        &["flowchart LR\n    s0 --> s1\n"],
        &["walker".to_string()],
        &[],
        &modules,
        &[],
        &[],
    );
    let flow = flow.expect("single-member page should receive containment fallback");
    assert!(flow.contains("n0 --> n1"), "{flow}");
}

#[test]
fn no_generator_uses_structure_fallback() {
    // AI off: documented flow evidence cannot be composed, so the page draws
    // only the deterministic containment structure and names that reason.
    let modules = [
        module_doc("walker", "Discovers files. Flow: walker -> parser."),
        module_doc("parser", "Extracts the AST."),
    ];
    let member_modules = vec!["walker".to_string(), "parser".to_string()];
    let mut generate: Option<&mut TextGenerator<'_>> = None;
    let mut diagram_stats = DiagramStats::default();
    let mut progress = CodewikiProgress::silent();
    let module_lookup = module_lookup(&modules);
    let file_lookup = file_lookup(&[]);
    let leading_chunks = BTreeMap::new();
    let flow = curated_flow_diagram(
        &member_modules,
        &[],
        &mut generate,
        CuratedFlowContext {
            page_path: "code/concepts/test.md",
            module_lookup: &module_lookup,
            file_lookup: &file_lookup,
            leading_chunks: &leading_chunks,
            graph_edges: &[],
            diagram_stats: &mut diagram_stats,
            progress: &mut progress,
        },
    );
    let flow = flow.expect("AI-off page should receive containment fallback");
    assert!(
        flow.contains("no diagram generator was available"),
        "{flow}"
    );
    assert_eq!(diagram_stats.no_generator, 1);
    assert_eq!(diagram_stats.total(), 1);
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
