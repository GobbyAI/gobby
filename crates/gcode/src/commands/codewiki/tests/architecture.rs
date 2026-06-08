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
    assert!(rendered.contains("```mermaid"));
    assert!(rendered.contains("m_src_api[\"src/api\"] --> m_src_domain[\"src/domain\"]"));
    assert!(rendered.contains("m_src_domain[\"src/domain\"] --> m_src_storage[\"src/storage\"]"));
}
