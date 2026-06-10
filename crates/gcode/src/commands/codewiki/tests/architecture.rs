use super::super::test_utils::{test_component_id, test_symbol};
use super::*;

#[test]
fn codewiki_architecture_overview_page_uses_subsystems_and_degradation_metadata() {
    let input = CodewikiInput {
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

    let docs = generate_hierarchical_docs(&input, None);
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
fn repo_structural_fallback_omits_marker_wall_but_generated_text_stays_grounded() {
    let fallback_docs = generate_hierarchical_docs(&repo_marker_input(), None);
    let fallback_repo = rendered_doc(&fallback_docs, "code/repo.md");
    let fallback_overview = markdown_section(fallback_repo, "## Overview");

    assert!(fallback_overview.contains("Repository code documentation covers 6 files"));
    assert_eq!(inline_marker_count(fallback_overview), 0);

    let mut generator = |_prompt: &str, system: &str| {
        if system == prompts::REPO_SYSTEM {
            Some("Generated repository overview.".to_string())
        } else {
            None
        }
    };
    let generated_docs = generate_hierarchical_docs(&repo_marker_input(), Some(&mut generator));
    let generated_repo = rendered_doc(&generated_docs, "code/repo.md");
    let generated_overview = markdown_section(generated_repo, "## Overview");

    assert!(generated_overview.contains("Generated repository overview."));
    assert_eq!(inline_marker_count(generated_overview), 6);
}

#[test]
fn architecture_prompt_formats_component_labels_with_raw_ids() {
    let handle_id = test_component_id("src/api/handler.rs", "handle", "function");
    let route_id = test_component_id("src/api/router.rs", "route", "function");
    let input = CodewikiInput {
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
        let mut generator = |prompt: &str, system: &str| {
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

fn repo_marker_input() -> CodewikiInput {
    let files = [
        "alpha.rs",
        "beta.rs",
        "gamma.rs",
        "delta.rs",
        "epsilon.rs",
        "zeta.rs",
    ];
    CodewikiInput {
        files: files.iter().map(|file| (*file).to_string()).collect(),
        graph_edges: Vec::new(),
        graph_availability: CodewikiGraphAvailability::Available,
        symbols: files
            .iter()
            .enumerate()
            .map(|(index, file)| {
                test_symbol(
                    file,
                    &format!("item_{index}"),
                    "function",
                    1,
                    "pub fn item()",
                )
            })
            .collect(),
    }
}

fn rendered_doc<'a>(docs: &'a [(String, String)], path: &str) -> &'a str {
    docs.iter()
        .find(|(doc_path, _)| doc_path == path)
        .map(|(_, content)| content.as_str())
        .expect("rendered doc")
}

fn markdown_section<'a>(rendered: &'a str, heading: &str) -> &'a str {
    let (_, after_heading) = rendered.split_once(heading).expect("section heading");
    after_heading
        .split_once("\n## ")
        .map(|(section, _)| section)
        .unwrap_or(after_heading)
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
