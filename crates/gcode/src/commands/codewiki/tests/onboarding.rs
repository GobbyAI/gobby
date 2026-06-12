use std::collections::BTreeMap;

use super::support::*;
use super::*;

#[test]
fn codewiki_onboarding_ranks_modules_from_graph_analytics() {
    let input = CodewikiInput {
        leading_chunks: std::collections::BTreeMap::new(),
        files: vec![
            "src/main.rs".to_string(),
            "src/lib.rs".to_string(),
            "src/api/handler.rs".to_string(),
            "src/domain/service.rs".to_string(),
            "src/storage/repo.rs".to_string(),
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
            test_symbol("src/main.rs", "main", "function", 1, "fn main()"),
            test_symbol("src/lib.rs", "Client", "class", 1, "pub struct Client;"),
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
        ],
    };

    let docs = generate_hierarchical_docs(&input, None)
        .into_iter()
        .collect::<BTreeMap<_, _>>();
    let onboarding = docs
        .get("code/_onboarding.md")
        .expect("onboarding page renders");

    assert!(onboarding.contains("type: code_onboarding"));
    assert!(!onboarding.contains("degraded: true"));
    assert!(onboarding.contains("[[code/files/src/main.rs|src/main.rs]]"));
    assert!(onboarding.contains("[[code/files/src/lib.rs|src/lib.rs]]"));
    assert!(onboarding.contains("`pub struct Client;`"));
    assert!(onboarding.contains("## Recommended Reading Order"));

    let domain = onboarding
        .find("[[code/modules/src/domain|src/domain]]")
        .expect("domain module is recommended");
    let api = onboarding
        .find("[[code/modules/src/api|src/api]]")
        .expect("api module is recommended");
    let storage = onboarding
        .find("[[code/modules/src/storage|src/storage]]")
        .expect("storage module is recommended");
    assert!(domain < api);
    assert!(domain < storage);
}

#[test]
fn codewiki_onboarding_degrades_to_structural_entry_points_without_graph_analytics() {
    let input = CodewikiInput {
        leading_chunks: std::collections::BTreeMap::new(),
        files: vec!["src/main.rs".to_string(), "src/lib.rs".to_string()],
        graph_edges: Vec::new(),
        graph_availability: CodewikiGraphAvailability::Unavailable,
        symbols: vec![
            test_symbol("src/main.rs", "main", "function", 1, "fn main()"),
            test_symbol("src/lib.rs", "Client", "class", 1, "pub struct Client;"),
        ],
    };

    let docs = generate_hierarchical_docs(&input, None)
        .into_iter()
        .collect::<BTreeMap<_, _>>();
    let onboarding = docs
        .get("code/_onboarding.md")
        .expect("onboarding page renders");

    assert!(onboarding.contains("degraded: true"));
    assert!(onboarding.contains("- graph-analytics-unavailable"));
    assert!(onboarding.contains("## Entry Points"));
    assert!(!onboarding.contains("## Structural Start Points"));
    assert!(!onboarding.contains("## Recommended Reading Order"));
    assert!(onboarding.contains("[[code/files/src/main.rs|src/main.rs]]"));
    assert!(onboarding.contains("[[code/files/src/lib.rs|src/lib.rs]]"));
}

#[test]
fn codewiki_onboarding_available_empty_reading_order_is_not_unavailable() {
    let input = CodewikiInput {
        leading_chunks: std::collections::BTreeMap::new(),
        files: vec!["src/lib.rs".to_string()],
        graph_edges: Vec::new(),
        graph_availability: CodewikiGraphAvailability::Available,
        symbols: vec![test_symbol(
            "src/lib.rs",
            "Client",
            "class",
            1,
            "pub struct Client;",
        )],
    };

    let docs = generate_hierarchical_docs(&input, None)
        .into_iter()
        .collect::<BTreeMap<_, _>>();
    let onboarding = docs
        .get("code/_onboarding.md")
        .expect("onboarding page renders");

    assert!(!onboarding.contains("degraded: true"));
    assert!(!onboarding.contains("- graph-analytics-unavailable"));
}
