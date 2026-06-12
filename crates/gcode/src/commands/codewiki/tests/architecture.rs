use super::support::*;
use super::*;

#[test]
fn codewiki_architecture_overview_page_uses_subsystems_and_degradation_metadata() {
    let input = CodewikiInput {
        leading_chunks: std::collections::BTreeMap::new(),
        files: vec![
            "src/api/handler.rs".to_string(),
            "src/api/router.rs".to_string(),
            "src/domain/service.rs".to_string(),
            "src/storage/repo.rs".to_string(),
        ],
        graph_edges: vec![
            CodewikiGraphEdge::call(
                test_component_id("src/api/handler.rs", "handle", "function"),
                test_component_id("src/api/router.rs", "route", "function"),
            ),
            CodewikiGraphEdge::import(
                test_component_id("src/api/handler.rs", "handle", "function"),
                test_component_id("src/domain/service.rs", "Service", "class"),
            ),
            CodewikiGraphEdge::import(
                test_component_id("src/domain/service.rs", "Service", "class"),
                test_component_id("src/storage/repo.rs", "Repo", "class"),
            ),
        ],
        graph_availability: CodewikiGraphAvailability::Truncated,
        symbols: vec![
            test_symbol(
                "src/api/handler.rs",
                "handle",
                "function",
                1,
                "pub fn handle()",
            ),
            test_symbol(
                "src/api/router.rs",
                "route",
                "function",
                1,
                "pub fn route()",
            ),
            test_symbol(
                "src/domain/service.rs",
                "Service",
                "class",
                1,
                "pub struct Service;",
            ),
            test_symbol(
                "src/storage/repo.rs",
                "Repo",
                "class",
                1,
                "pub struct Repo;",
            ),
        ],
    };

    // A generator that attempts and fails marks model degradation; a missing
    // generator (AI off) is structural by intent and records nothing.
    let mut failing_generator = |_prompt: &str, _system: &str, _tier: PromptTier| None;
    let docs = generate_hierarchical_docs(&input, Some(&mut failing_generator));
    let docs_by_path = docs.into_iter().collect::<BTreeMap<_, _>>();
    let rendered = docs_by_path
        .get("code/_architecture.md")
        .expect("architecture overview doc");

    assert!(rendered.contains("generated_by: gcode-codewiki"));
    assert!(rendered.contains("type: code_architecture"));
    assert!(rendered.contains("degraded: true"));
    assert!(rendered.contains("degraded_sources:"));
    assert!(rendered.contains("- model-unavailable"));
    assert!(rendered.contains("- graph-truncated"));
    assert!(rendered.contains("[[code/modules/src/api|src/api]]"));
    assert!(rendered.contains("[[code/modules/src/domain|src/domain]]"));
    assert!(rendered.contains("[[code/modules/src/storage|src/storage]]"));
    assert!(rendered.contains("`src/api` contains 2 direct files and 0 child modules."));
    assert!(!rendered.contains("src/api/handler.rs:1"));
    assert!(!rendered.contains("src/api/router.rs:1"));
    for line in rendered
        .lines()
        .filter(|line| line.starts_with("- [[code/modules/"))
    {
        assert_eq!(inline_marker_count(line), 0);
    }
    assert!(rendered.contains("```mermaid"));
    assert!(rendered.contains("m_src_api[\"src/api\"] --> m_src_domain[\"src/domain\"]"));
    assert!(rendered.contains("m_src_domain[\"src/domain\"] --> m_src_storage[\"src/storage\"]"));
}

#[test]
fn architecture_prompt_formats_component_labels_with_raw_ids() {
    let handle_id = test_component_id("src/api/handler.rs", "handle", "function");
    let route_id = test_component_id("src/api/router.rs", "route", "function");
    let input = CodewikiInput {
        leading_chunks: std::collections::BTreeMap::new(),
        files: vec![
            "src/api/handler.rs".to_string(),
            "src/api/router.rs".to_string(),
        ],
        graph_edges: Vec::new(),
        graph_availability: CodewikiGraphAvailability::Available,
        symbols: vec![
            test_symbol(
                "src/api/handler.rs",
                "handle",
                "function",
                1,
                "pub fn handle()",
            ),
            test_symbol(
                "src/api/router.rs",
                "route",
                "function",
                1,
                "pub fn route()",
            ),
        ],
    };
    let mut architecture_prompts = Vec::new();
    {
        let mut generator = |prompt: &str, system: &str, _tier: PromptTier| {
            if system == prompts::ARCHITECTURE_SYSTEM {
                architecture_prompts.push(prompt.to_string());
            }
            Some("Generated summary [src/api/handler.rs:1].".to_string())
        };

        let _docs = generate_hierarchical_docs(&input, Some(&mut generator));
    }

    let prompt = architecture_prompts
        .iter()
        .find(|prompt| prompt.contains("Subsystem: src/api"))
        .expect("src/api architecture prompt");

    assert!(prompt.contains(&format!("- handle [function] ({handle_id})")));
    assert!(prompt.contains(&format!("- route [function] ({route_id})")));
    assert!(!prompt.contains(&format!("- {handle_id}\n")));
    assert!(!prompt.contains(&format!("- {route_id}\n")));
}

#[test]
fn architecture_page_renders_layered_narrative_and_child_module_levels() {
    let input = CodewikiInput {
        leading_chunks: std::collections::BTreeMap::new(),
        files: vec![
            "src/api/handler.rs".to_string(),
            "src/domain/service.rs".to_string(),
            "src/storage/repo.rs".to_string(),
            "src/storage/backend/disk.rs".to_string(),
        ],
        graph_edges: vec![
            CodewikiGraphEdge::import(
                test_component_id("src/api/handler.rs", "handle", "function"),
                test_component_id("src/domain/service.rs", "Service", "class"),
            ),
            CodewikiGraphEdge::import(
                test_component_id("src/domain/service.rs", "Service", "class"),
                test_component_id("src/storage/repo.rs", "Repo", "class"),
            ),
        ],
        graph_availability: CodewikiGraphAvailability::Available,
        symbols: vec![
            test_symbol(
                "src/api/handler.rs",
                "handle",
                "function",
                1,
                "pub fn handle()",
            ),
            test_symbol(
                "src/domain/service.rs",
                "Service",
                "class",
                1,
                "pub struct Service;",
            ),
            test_symbol(
                "src/storage/repo.rs",
                "Repo",
                "class",
                1,
                "pub struct Repo;",
            ),
            test_symbol(
                "src/storage/backend/disk.rs",
                "Disk",
                "class",
                1,
                "pub struct Disk;",
            ),
        ],
    };

    let mut narrative_prompts = Vec::new();
    let mut generator = |prompt: &str, system: &str, _tier: PromptTier| {
        if system == prompts::ARCHITECTURE_NARRATIVE_SYSTEM {
            narrative_prompts.push(prompt.to_string());
            Some(
                "The api layer sits atop domain, which persists through storage \
                 [src/api/handler.rs:1]."
                    .to_string(),
            )
        } else if system == prompts::ARCHITECTURE_SYSTEM {
            Some("Owns one layer of the system [src/api/handler.rs:1].".to_string())
        } else {
            None
        }
    };
    let docs = generate_hierarchical_docs(&input, Some(&mut generator));
    let docs_by_path = docs.into_iter().collect::<BTreeMap<_, _>>();
    let rendered = docs_by_path
        .get("code/_architecture.md")
        .expect("architecture overview doc");

    assert!(rendered.contains("## Overview"), "{rendered}");
    assert!(
        rendered.contains("The api layer sits atop domain"),
        "{rendered}"
    );
    assert!(rendered.contains("## Subsystem Map"), "{rendered}");
    assert!(
        rendered.contains("m_src_api[\"src/api\"] --> m_src_domain[\"src/domain\"]"),
        "{rendered}"
    );
    // One module level is enumerated below each subsystem.
    assert!(
        rendered.contains("  - [[code/modules/src/storage/backend|src/storage/backend]]"),
        "{rendered}"
    );

    let narrative_prompt = narrative_prompts
        .first()
        .expect("narrative prompt captured");
    assert!(
        narrative_prompt.contains("- src/api -> src/domain"),
        "{narrative_prompt}"
    );
    assert!(
        narrative_prompt.contains("- src/domain -> src/storage"),
        "{narrative_prompt}"
    );
}

fn inline_marker_count(text: &str) -> usize {
    text.split_whitespace()
        .filter(|token| {
            token
                .strip_prefix('[')
                .and_then(|value| value.strip_suffix(']'))
                .is_some_and(|value| value.chars().all(|ch| ch.is_ascii_digit()))
        })
        .count()
}
