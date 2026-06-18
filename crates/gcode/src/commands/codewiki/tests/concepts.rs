use super::support::*;
use super::*;

fn concept_input() -> CodewikiInput {
    CodewikiInput {
        leading_chunks: std::collections::BTreeMap::from([
            (
                "src/lib.rs".to_string(),
                LeadingChunk {
                    content: "pub struct Client;".to_string(),
                    line_start: 1,
                    line_end: 1,
                },
            ),
            (
                "src/search.rs".to_string(),
                LeadingChunk {
                    content: "pub fn query() { /* hybrid search entry point */ }".to_string(),
                    line_start: 4,
                    line_end: 6,
                },
            ),
            (
                "src/db/mod.rs".to_string(),
                LeadingChunk {
                    content: "pub fn connect() { /* open the database */ }".to_string(),
                    line_start: 8,
                    line_end: 10,
                },
            ),
        ]),
        files: vec![
            "src/lib.rs".to_string(),
            "src/search.rs".to_string(),
            "src/db/mod.rs".to_string(),
        ],
        graph_edges: Vec::new(),
        graph_availability: CodewikiGraphAvailability::Available,
        symbols: vec![
            test_symbol("src/lib.rs", "Client", "class", 1, "pub struct Client;"),
            test_symbol("src/search.rs", "query", "function", 4, "pub fn query()"),
            test_symbol(
                "src/db/mod.rs",
                "connect",
                "function",
                8,
                "pub fn connect()",
            ),
        ],
    }
}

fn rendered_doc<'a>(docs: &'a [(String, String)], path: &str) -> &'a str {
    docs.iter()
        .find(|(doc_path, _)| doc_path == path)
        .map(|(_, content)| content.as_str())
        .unwrap_or_else(|| panic!("missing doc {path}"))
}

#[test]
fn curated_navigation_uses_one_structured_aggregate_pass() {
    let mut curated_calls = 0;
    let mut generator = |_prompt: &str, system: &str, tier: PromptTier| {
        if system == prompts::CURATED_NAVIGATION_SYSTEM {
            curated_calls += 1;
            assert_eq!(tier, PromptTier::Aggregate);
            Some(
                r#"{
                  "concept_modules": [
                    {
                      "title": "Query Engine",
                      "summary": "How requests enter the system and resolve into repository answers.",
                      "modules": ["src"],
                      "files": ["src/lib.rs", "src/search.rs"]
                    }
                  ],
                  "sections": [
                    {
                      "title": "Understanding the System",
                      "summary": "Start with query flow, then drill into reference pages.",
                      "concepts": ["Query Engine"]
                    }
                  ],
                  "narrative_pages": [
                    {
                      "slug": "introduction",
                      "title": "Introduction",
                      "summary": "Begin at the query engine and use linked reference pages for implementation detail.",
                      "concepts": ["Query Engine"],
                      "modules": ["src"],
                      "files": ["src/lib.rs"]
                    }
                  ]
                }"#
                .to_string(),
            )
        } else if system == prompts::CONCEPT_PAGE_SYSTEM {
            Some(
                "## Purpose\n\nThe query engine resolves requests into repository answers [src/search.rs:4].\n\n## Key components\n\n| Symbol | Role |\n| --- | --- |\n| query | Runs a hybrid search [src/search.rs:4] |\n\n## Where to start\n\nBegin with `query` [src/search.rs:4].\n"
                    .to_string(),
            )
        } else if system == prompts::NARRATIVE_PAGE_SYSTEM {
            Some(
                "## Why this matters\n\nQuery flow is the spine of the system [src/search.rs:4].\n\n## How it works\n\n1. A request enters and is parsed into a query [src/search.rs:4].\n\n## What to read next\n\nContinue to the architecture chapter.\n"
                    .to_string(),
            )
        } else {
            None
        }
    };

    let docs = generate_hierarchical_docs(&concept_input(), Some(&mut generator));
    // The structure pass still runs exactly once; the per-page content passes
    // use the concept/narrative systems and are not counted here.
    assert_eq!(curated_calls, 1);

    let index = rendered_doc(&docs, "code/concepts/index.md");
    assert!(index.contains("type: code_concept_tree"));
    assert!(index.contains("## Concept Tree"));
    assert!(index.contains("[[code/concepts/query-engine|Query Engine]]"));

    let concept = rendered_doc(&docs, "code/concepts/query-engine.md");
    assert!(concept.contains("type: code_concept"));
    // Content pass: a multi-section body with a table row and a real citation.
    assert!(concept.contains("## Purpose"), "{concept}");
    assert!(
        concept.contains("| query | Runs a hybrid search"),
        "{concept}"
    );
    assert!(concept.contains("src/search.rs:4"), "{concept}");
    // Sparse linking: module roots only, no exhaustive `## Source Files` dump.
    assert!(concept.contains("[[code/modules/src|src]]"), "{concept}");
    assert!(!concept.contains("[[code/files/"), "{concept}");
    assert!(concept.contains("provenance:"));
    // Curated frontmatter is range-free (bounded provenance, commit 5).
    assert!(!concept.contains("ranges:"), "{concept}");

    let narrative = rendered_doc(&docs, "code/narrative/introduction.md");
    assert!(narrative.contains("type: code_narrative"));
    assert!(narrative.contains("## Why this matters"), "{narrative}");
    assert!(narrative.contains("[[code/concepts/query-engine|Query Engine]]"));
    assert!(narrative.contains("[[code/modules/src|src]]"));
}

#[test]
fn curated_navigation_falls_back_to_structural_concepts_without_ai() {
    let docs = generate_hierarchical_docs(&concept_input(), None);
    let repo = rendered_doc(&docs, "code/repo.md");
    let index = rendered_doc(&docs, "code/concepts/index.md");
    let introduction = rendered_doc(&docs, "code/narrative/introduction.md");

    assert!(repo.contains("[[code/concepts/index|Concept tree and narrative tours]]"));
    assert!(index.contains("## Concept Tree"));
    assert!(index.contains("[[code/narrative/introduction|Introduction]]"));
    assert!(introduction.contains("type: code_narrative"));
    assert!(introduction.contains("provenance:"));
    // --ai off still yields a structural multi-section body, not a bare summary.
    assert!(introduction.contains("## Key components"), "{introduction}");
}

#[test]
fn repo_leads_with_start_here_and_demotes_reference_appendix() {
    let docs = generate_hierarchical_docs(&concept_input(), None);
    let repo = rendered_doc(&docs, "code/repo.md");

    let start_here = repo.find("## Start here").expect("start-here section");
    let overview = repo.find("## Overview").expect("overview section");
    let appendix = repo
        .find("## Reference appendix")
        .expect("reference appendix");
    // Start here leads; the module/file reference is demoted below it.
    assert!(start_here < overview, "{repo}");
    assert!(overview < appendix, "{repo}");

    // The guided tour entry point is the first link a reader sees.
    assert!(
        repo.contains("[[code/narrative/introduction|Introduction]]"),
        "{repo}"
    );

    // Module/file tables stay reachable, but under the appendix (level-3).
    let modules = repo.find("### Modules").expect("modules table heading");
    assert!(appendix < modules, "{repo}");
    assert!(repo.contains("| Module | Summary |"), "{repo}");

    // Concept tree lists the guided tour above the concept catalog.
    let index = rendered_doc(&docs, "code/concepts/index.md");
    let tours = index
        .find("## Start here — guided tour")
        .expect("guided tour");
    let tree = index.find("## Concept Tree").expect("concept tree");
    assert!(tours < tree, "{index}");
}

#[test]
fn guided_tour_spine_numbers_chapters_with_callout_and_reciprocal_nav() {
    let docs = generate_hierarchical_docs(&concept_input(), None);

    // Front page and concept index both lead with the numbered guided tour,
    // the new-to-this-codebase callout, and the ask/search pointer.
    for path in ["code/repo.md", "code/concepts/index.md"] {
        let doc = rendered_doc(&docs, path);
        assert!(doc.contains("## Start here — guided tour"), "{path}: {doc}");
        assert!(
            doc.contains(
                "New to this codebase? Begin with [[code/narrative/introduction|Introduction]]."
            ),
            "{path}: {doc}"
        );
        assert!(
            doc.contains("1. [[code/narrative/introduction|Introduction]]"),
            "{path}: {doc}"
        );
        assert!(
            doc.contains("2. [[code/narrative/architecture|Architecture]]"),
            "{path}: {doc}"
        );
        assert!(
            doc.contains("3. [[code/narrative/data-flow|Data Flow]]"),
            "{path}: {doc}"
        );
        assert!(doc.contains("`gwiki ask"), "{path}: {doc}");
        assert!(doc.contains("`gwiki search"), "{path}: {doc}");
    }

    // Sequential reciprocal chapter nav along the spine.
    let intro = rendered_doc(&docs, "code/narrative/introduction.md");
    assert!(!intro.contains("← Previous"), "{intro}");
    assert!(
        intro.contains("Next →: [[code/narrative/architecture|Architecture]]"),
        "{intro}"
    );

    let arch = rendered_doc(&docs, "code/narrative/architecture.md");
    assert!(arch.contains("## Continue the tour"), "{arch}");
    assert!(
        arch.contains("← Previous: [[code/narrative/introduction|Introduction]]"),
        "{arch}"
    );
    assert!(
        arch.contains("Next →: [[code/narrative/data-flow|Data Flow]]"),
        "{arch}"
    );

    let data_flow = rendered_doc(&docs, "code/narrative/data-flow.md");
    assert!(
        data_flow.contains("← Previous: [[code/narrative/architecture|Architecture]]"),
        "{data_flow}"
    );
}

#[test]
fn verify_pass_records_notes_without_stripping_curated_page() {
    // Generator: the structure pass plus a concept body carrying one planted,
    // unsupported "Fabricated" block among grounded blocks.
    let mut generator = |_prompt: &str, system: &str, _tier: PromptTier| {
        if system == prompts::CURATED_NAVIGATION_SYSTEM {
            Some(
                r#"{
                  "concept_modules": [
                    {
                      "title": "Query Engine",
                      "summary": "How requests enter the system and resolve into repository answers.",
                      "modules": ["src"],
                      "files": ["src/lib.rs", "src/search.rs"]
                    }
                  ],
                  "sections": [
                    {"title": "Understanding the System", "summary": "Start with query flow.", "concepts": ["Query Engine"]}
                  ],
                  "narrative_pages": [
                    {"slug": "introduction", "title": "Introduction", "summary": "Begin at the query engine.", "concepts": ["Query Engine"], "modules": ["src"], "files": ["src/lib.rs"]}
                  ]
                }"#
                .to_string(),
            )
        } else if system == prompts::CONCEPT_PAGE_SYSTEM {
            Some(
                "## Purpose\n\nThe query engine resolves requests into repository answers [src/search.rs:4].\n\nFabricated: the engine secretly trains a neural ranker each night [src/search.rs:4].\n\n## Where to start\n\nBegin with `query` [src/search.rs:4].\n"
                    .to_string(),
            )
        } else {
            // Narrative chapters: keep a clean, fully-supported body.
            Some(
                "## Why this matters\n\nQuery flow is the spine of the system [src/search.rs:4].\n\n## What to read next\n\nContinue to the architecture chapter.\n"
                    .to_string(),
            )
        }
    };

    // Verifier: flag the numbered block that carries the planted sentinel; any
    // page without it is fully supported (`[]`).
    let mut verifier = |prompt: &str, _system: &str| {
        for line in prompt.lines() {
            let trimmed = line.trim_start();
            if trimmed.starts_with('[') && trimmed.contains("Fabricated") {
                let id = trimmed[1..].split(']').next().unwrap_or_default().trim();
                return Some(format!(
                    r#"[{{"id":{id},"reason":"Fabricated claim lacks evidence."}}]"#
                ));
            }
        }
        Some("[]".to_string())
    };

    let docs = generate_hierarchical_docs_with_verify(
        &concept_input(),
        Some(&mut generator),
        Some(&mut verifier),
        AiDepth::Files,
    );

    let concept = docs
        .iter()
        .find(|doc| doc.path == "code/concepts/query-engine.md")
        .map(|doc| doc.content.as_str())
        .expect("concept page");

    // The grounded block and planted block both survive; verifier findings are
    // frontmatter-only audit metadata.
    assert!(
        concept.contains("resolves requests into repository answers"),
        "{concept}"
    );
    assert!(concept.contains("Fabricated"), "{concept}");
    assert!(concept.contains("verify_notes:"), "{concept}");
    assert!(
        concept.contains("reason: Fabricated claim lacks evidence."),
        "{concept}"
    );
    assert!(!concept.contains("degraded: true"), "{concept}");
}
