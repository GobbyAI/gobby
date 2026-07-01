use super::super::{ProseRegister, RelationshipFacts};
use super::*;

#[test]
fn with_register_none_is_borrowed_and_unchanged() {
    let out = with_register(FILE_SYSTEM, None);
    assert_eq!(out.as_ref(), FILE_SYSTEM);
    assert!(matches!(out, std::borrow::Cow::Borrowed(_)));
}

#[test]
fn with_register_appends_grounded_voice() {
    for register in [
        ProseRegister::Newcomer,
        ProseRegister::Maintainer,
        ProseRegister::Agent,
    ] {
        let out = with_register(FILE_SYSTEM, Some(register));
        assert!(out.starts_with(FILE_SYSTEM), "base prompt preserved");
        assert!(
            out.contains("file:line") && out.contains("never invent"),
            "grounding reaffirmed for {register:?}"
        );
        assert!(
            out.contains("time or effort estimates"),
            "no-time-estimate guardrail present for {register:?}"
        );
    }
    let newcomer = with_register(FILE_SYSTEM, Some(ProseRegister::Newcomer));
    assert!(newcomer.contains("plain language"));
    assert!(newcomer.contains("problem this code solves"));
}

#[test]
fn prose_depth_standard_defers_to_default() {
    use crate::commands::codewiki::ProseDepth;
    assert_eq!(ProseDepth::Standard.max_tokens(), None);
    let brief = ProseDepth::Brief.max_tokens().expect("brief pins a budget");
    let deep = ProseDepth::Deep.max_tokens().expect("deep pins a budget");
    assert!(deep > brief, "deep raises the budget above brief");
}

fn oversized_child(name: &str) -> ChildSummary {
    let citation_wall = (0..2_000)
        .map(|line| format!("[src/lib.rs:{line}]"))
        .collect::<Vec<_>>()
        .join("\n");
    ChildSummary {
        name: name.to_string(),
        summary: format!("One real sentence.\n{citation_wall}"),
    }
}

#[test]
fn aggregate_prompts_bound_each_child_summary() {
    let children = (0..3)
        .map(|index| oversized_child(&format!("src/module_{index}")))
        .collect::<Vec<_>>();

    for prompt in [
        module_prompt(
            "src",
            &children,
            &children,
            &[],
            &[],
            &RelationshipFacts::default(),
        ),
        architecture_prompt("src", &children, &children, &[], &[]),
        repo_prompt(&children, &children, &[]),
    ] {
        for line in prompt.lines().filter(|line| line.starts_with("| src/")) {
            assert!(
                line.chars().count()
                    <= CHILD_SUMMARY_EXCERPT_MAX_CHARS + "| src/module_0 |  |".chars().count(),
                "child summary line stays bounded: {} chars",
                line.chars().count()
            );
            assert!(line.contains('…'), "oversized excerpt is marked truncated");
        }
    }
}

#[test]
fn short_summaries_pass_through_untruncated() {
    let child = ChildSummary {
        name: "src/lib.rs".to_string(),
        summary: "Concise healthy summary.".to_string(),
    };
    let prompt = module_prompt(
        "src",
        &[child],
        &[],
        &[],
        &[],
        &RelationshipFacts::default(),
    );
    assert!(prompt.contains("| src/lib.rs | Concise healthy summary. |\n"));
}

#[test]
fn summary_excerpt_includes_ellipsis_inside_hard_cap() {
    let exact = "a".repeat(CHILD_SUMMARY_EXCERPT_MAX_CHARS);
    let oversized = format!("{exact}b");

    assert_eq!(
        summary_excerpt(&exact).chars().count(),
        CHILD_SUMMARY_EXCERPT_MAX_CHARS
    );
    let excerpt = summary_excerpt(&oversized);
    assert_eq!(excerpt.chars().count(), CHILD_SUMMARY_EXCERPT_MAX_CHARS);
    assert!(excerpt.ends_with('…'));
}

#[test]
fn excerpt_flattens_multiline_summaries_to_one_line() {
    let child = ChildSummary {
        name: "src/lib.rs".to_string(),
        summary: "First line.\nSecond line of the same paragraph.".to_string(),
    };
    let prompt = module_prompt(
        "src",
        &[child],
        &[],
        &[],
        &[],
        &RelationshipFacts::default(),
    );
    assert!(prompt.contains("| src/lib.rs | First line. Second line of the same paragraph. |\n"));
}

#[test]
fn aggregate_prompts_request_tables_for_enumerable_facts() {
    let child = ChildSummary {
        name: "src/cli.rs".to_string(),
        summary: "Defines commands and config keys.".to_string(),
    };
    let prompt = repo_prompt(&[child], &[], &[]);

    assert!(MODULE_SYSTEM.contains("compact Markdown tables"));
    // The repo overview leads with narrative and only then permits compact
    // tables — it must never become an index of commands/flags/symbols.
    assert!(REPO_SYSTEM.contains("lead with narrative"));
    assert!(REPO_SYSTEM.contains("compact Markdown tables"));
    assert!(ARCHITECTURE_SYSTEM.contains("public API symbols"));
    assert!(prompt.contains("Table guidance:\n"));
    assert!(prompt.contains("configuration keys, environment variables, or public API symbols"));
    assert!(prompt.contains("| Module | Summary |\n| --- | --- |\n"));
}

#[test]
fn file_prompt_lists_symbols_as_markdown_table() {
    let symbol = SymbolSummary {
        name: "run|cli".to_string(),
        kind: "function".to_string(),
        component_id: "component|id".to_string(),
        component_label: "run [function]".to_string(),
        line_start: 7,
        line_end: 9,
        purpose: "Handles command dispatch.".to_string(),
    };

    let prompt = file_prompt("src/cli.rs", &[symbol], &[], &RelationshipFacts::default());

    assert!(prompt.contains("| Symbol | Kind | Component | Component ID | Lines | Purpose |"));
    assert!(prompt.contains("| run\\|cli | function | run [function] | component\\|id | 7-9 | Handles command dispatch. |"));
}

#[test]
fn file_prompt_includes_cross_file_relationships() {
    use super::super::SourceSpan;
    use super::super::relationship_facts::SymbolRelation;

    let relationships = RelationshipFacts {
        inbound_calls: vec![SymbolRelation {
            other_name: "outer::caller".to_string(),
            other_kind: "function".to_string(),
            local_name: Some("local_fn".to_string()),
            span: SourceSpan {
                file: "src/other.rs".to_string(),
                line_start: 5,
                line_end: 8,
            },
        }],
        outbound_calls: Vec::new(),
        imports: Vec::new(),
    };

    let prompt = file_prompt("src/cli.rs", &[], &[], &relationships);

    assert!(prompt.contains("Cross-file relationships"), "{prompt}");
    assert!(
        prompt.contains("Called by (external symbols that call into this file):"),
        "{prompt}"
    );
    assert!(prompt.contains("[src/other.rs:5-8]"), "{prompt}");
}

#[test]
fn module_prompt_includes_cross_module_relationships() {
    use super::super::SourceSpan;
    use super::super::relationship_facts::SymbolRelation;

    let relationships = RelationshipFacts {
        inbound_calls: vec![SymbolRelation {
            other_name: "sibling::caller".to_string(),
            other_kind: "function".to_string(),
            local_name: Some("module_fn".to_string()),
            span: SourceSpan {
                file: "src/other_mod/api.rs".to_string(),
                line_start: 12,
                line_end: 20,
            },
        }],
        outbound_calls: Vec::new(),
        imports: Vec::new(),
    };
    let child = ChildSummary {
        name: "src/search/fts.rs".to_string(),
        summary: "FTS layer.".to_string(),
    };

    let prompt = module_prompt("src/search", &[child], &[], &[], &[], &relationships);

    assert!(prompt.contains("Cross-file relationships"), "{prompt}");
    assert!(
        prompt.contains("Called by (external symbols that call into this file):"),
        "{prompt}"
    );
    assert!(prompt.contains("[src/other_mod/api.rs:12-20]"), "{prompt}");
}

#[test]
fn verify_prompt_includes_symbols_as_authoritative_evidence() {
    let symbol = SymbolSummary {
        name: "run|cli".to_string(),
        kind: "function".to_string(),
        component_id: "component|id".to_string(),
        component_label: "run [function]".to_string(),
        line_start: 7,
        line_end: 9,
        purpose: "Handles command dispatch.".to_string(),
    };
    let blocks = vec!["The run symbol handles command dispatch.".to_string()];

    let prompt = verify_prompt(&blocks, &[symbol], &[], &RelationshipFacts::default());

    assert!(prompt.contains("Symbols:\n"));
    assert!(prompt.contains("| Symbol | Kind | Component | Component ID | Lines | Purpose |"));
    assert!(prompt.contains("| run\\|cli | function | run [function] | component\\|id | 7-9 | Handles command dispatch. |"));
}

#[test]
fn verify_prompt_without_symbols_stays_source_only() {
    let blocks = vec!["Grounded claim.".to_string()];

    let prompt = verify_prompt(&blocks, &[], &[], &RelationshipFacts::default());

    assert!(
        prompt.starts_with("Audit each numbered draft block against the source excerpts below.")
    );
    assert!(!prompt.contains("Symbols:"));
    assert!(!prompt.contains("No indexed symbols"));
    assert!(prompt.contains("Source excerpts:\n- No source excerpts.\n"));
}

fn excerpt(path: &str, content: &str) -> SourceExcerpt {
    SourceExcerpt {
        path: path.to_string(),
        line_start: 1,
        line_end: 40,
        excerpt: content.to_string(),
    }
}

#[test]
fn aggregate_prompts_embed_bounded_source_excerpts() {
    let oversized = "x".repeat(SOURCE_EXCERPT_MAX_CHARS * 3);
    let sources = (0..MAX_PROMPT_SOURCE_EXCERPTS + 3)
        .map(|index| excerpt(&format!("src/file_{index}.rs"), &oversized))
        .collect::<Vec<_>>();

    let prompt = module_prompt(
        "src",
        &[],
        &[],
        &[],
        &sources,
        &RelationshipFacts::default(),
    );

    let headers = prompt
        .lines()
        .filter(|line| line.starts_with("--- src/file_"))
        .count();
    assert_eq!(
        headers, MAX_PROMPT_SOURCE_EXCERPTS,
        "excerpt count is capped"
    );
    assert!(prompt.contains("--- src/file_0.rs (lines 1-40)"));
    for chunk in prompt.split("--- ").skip(1) {
        assert!(
            chunk.chars().count() <= SOURCE_EXCERPT_MAX_CHARS + 120,
            "each excerpt block stays bounded: {} chars",
            chunk.chars().count()
        );
    }
}

#[test]
fn prompts_without_excerpts_state_their_absence() {
    let prompt = repo_prompt(&[], &[], &[]);
    assert!(prompt.contains("Source excerpts:\n- No source excerpts.\n"));
}

#[test]
fn content_file_prompt_carries_leading_content() {
    let prompt = content_file_prompt(
        "README.md",
        &excerpt("README.md", "# Project\n\nWhat this repo does."),
    );
    assert!(prompt.contains("File: README.md"));
    assert!(prompt.contains("--- README.md (lines 1-40)"));
    assert!(prompt.contains("What this repo does."));
}

#[test]
fn architecture_narrative_prompt_lists_subsystems_and_edges() {
    let subsystems = vec![
        ChildSummary {
            name: "crates/gcore".to_string(),
            summary: "Shared foundation library.".to_string(),
        },
        ChildSummary {
            name: "crates/gcode".to_string(),
            summary: "Code search CLI.".to_string(),
        },
    ];
    let edges = vec![("crates/gcode".to_string(), "crates/gcore".to_string())];

    let prompt = architecture_narrative_prompt(&subsystems, &edges);

    assert!(prompt.contains("- crates/gcore: Shared foundation library."));
    assert!(prompt.contains("- crates/gcode -> crates/gcore"));

    let empty = architecture_narrative_prompt(&subsystems, &[]);
    assert!(empty.contains("- No cross-subsystem dependency edges.\n"));
}

fn evidence(name: &str, kind: &str, citation: &str, summary: &str) -> PageEvidenceRow {
    PageEvidenceRow {
        name: name.to_string(),
        kind: kind.to_string(),
        citation: citation.to_string(),
        summary: summary.to_string(),
    }
}

#[test]
fn concept_page_prompt_embeds_evidence_anchors_and_extra_excerpts() {
    let members = vec![evidence(
        "code/modules/src/search",
        "module",
        "src/search.rs:1-120",
        "Hybrid search entry point.",
    )];
    let symbols = vec![evidence(
        "query",
        "function",
        "src/search.rs:4-40",
        "Runs a hybrid search.",
    )];
    let sources = (0..6)
        .map(|index| excerpt(&format!("src/file_{index}.rs"), "fn demo() {}"))
        .collect::<Vec<_>>();

    let prompt = concept_page_prompt(
        "Search",
        "Hybrid search over the index.",
        &members,
        &symbols,
        &sources,
    );

    assert!(prompt.contains("src/search.rs:1-120"), "{prompt}");
    assert!(
        prompt.contains("| query | function | src/search.rs:4-40 |"),
        "{prompt}"
    );
    // Per-page budget is CONCEPT_PAGE_SOURCE_EXCERPTS (8), so a 5th excerpt
    // - which the shared aggregate cap of 4 would drop - is still present.
    assert!(prompt.contains("src/file_4.rs"), "{prompt}");
}

#[test]
fn narrative_page_prompt_grounds_with_members_and_symbols() {
    let members = vec![evidence(
        "code/modules/src",
        "module",
        "src/lib.rs:1-50",
        "Crate root.",
    )];
    let symbols = vec![evidence(
        "Client",
        "struct",
        "src/lib.rs:1-1",
        "Public client.",
    )];

    let prompt = narrative_page_prompt("Introduction", "Start here.", &members, &symbols, &[]);

    assert!(prompt.contains("src/lib.rs:1-50"), "{prompt}");
    assert!(
        prompt.contains("| Client | struct | src/lib.rs:1-1 |"),
        "{prompt}"
    );
    assert!(prompt.contains("- No source excerpts.\n"), "{prompt}");
}

#[test]
fn curated_page_systems_demand_grounded_multi_section_output() {
    for heading in [
        "## Purpose",
        "## How it works",
        "## Key components",
        "## Failure modes",
        "## How to change it",
        "## What to read next",
    ] {
        assert!(CONCEPT_PAGE_SYSTEM.contains(heading), "{heading}");
    }
    for heading in [
        "## Why this matters",
        "## How it works",
        "## Key components",
        "## Failure modes",
        "## How to change it",
        "## What to read next",
    ] {
        assert!(NARRATIVE_PAGE_SYSTEM.contains(heading), "{heading}");
    }
    for system in [CONCEPT_PAGE_SYSTEM, NARRATIVE_PAGE_SYSTEM] {
        assert!(system.contains("file:line"), "{system}");
        assert!(system.contains("at most six rows"), "{system}");
        assert!(system.contains("exhaustive member dump"), "{system}");
        assert!(system.contains("No markdown fences."), "{system}");
    }
}
