use super::support::*;
use super::*;

fn concept_input() -> CodewikiInput {
    CodewikiInput {
        leading_chunks: std::collections::BTreeMap::new(),
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
        } else {
            None
        }
    };

    let docs = generate_hierarchical_docs(&concept_input(), Some(&mut generator));
    assert_eq!(curated_calls, 1);

    let index = rendered_doc(&docs, "code/concepts/index.md");
    assert!(index.contains("type: code_concept_tree"));
    assert!(index.contains("## Concept Tree"));
    assert!(index.contains("[[code/concepts/query-engine|Query Engine]]"));

    let concept = rendered_doc(&docs, "code/concepts/query-engine.md");
    assert!(concept.contains("type: code_concept"));
    assert!(concept.contains("[[code/modules/src|src]]"));
    assert!(concept.contains("[[code/files/src/search.rs|src/search.rs]]"));
    assert!(concept.contains("provenance:"));

    let narrative = rendered_doc(&docs, "code/narrative/introduction.md");
    assert!(narrative.contains("type: code_narrative"));
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
}
