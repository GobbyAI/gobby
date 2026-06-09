use super::test_utils::{test_component_id, test_symbol};
use super::*;

#[test]
fn codewiki_hotspots_page_surfaces_analytics_rankings() {
    let hub_id = test_component_id("src/lib.rs", "Hub", "class");
    let input = CodewikiInput {
        files: vec![
            "src/lib.rs".to_string(),
            "src/a.rs".to_string(),
            "src/b.rs".to_string(),
            "src/c.rs".to_string(),
        ],
        graph_edges: vec![
            CodewikiGraphEdge::call(
                hub_id.clone(),
                test_component_id("src/a.rs", "a", "function"),
            ),
            CodewikiGraphEdge::call(
                hub_id.clone(),
                test_component_id("src/b.rs", "b", "function"),
            ),
            CodewikiGraphEdge::call(
                hub_id.clone(),
                test_component_id("src/c.rs", "c", "function"),
            ),
        ],
        graph_availability: CodewikiGraphAvailability::Available,
        symbols: vec![
            test_symbol("src/lib.rs", "Hub", "class", 1, "pub struct Hub;"),
            test_symbol("src/a.rs", "a", "function", 1, "pub fn a() {}"),
            test_symbol("src/b.rs", "b", "function", 1, "pub fn b() {}"),
            test_symbol("src/c.rs", "c", "function", 1, "pub fn c() {}"),
        ],
    };

    let docs = generate_hierarchical_docs(&input, None);
    let hotspots = docs
        .iter()
        .find(|(path, _)| path == "code/_hotspots.md")
        .map(|(_, content)| content)
        .expect("hotspots doc");
    let frontmatter = hotspots_frontmatter(hotspots);

    assert_eq!(
        frontmatter.get("type").and_then(serde_yaml::Value::as_str),
        Some("code_hotspots")
    );
    assert!(hotspots.contains("# Hotspots"));
    assert!(hotspots.contains("## Hotspots"));
    assert!(hotspots.contains("## God Nodes"));
    assert!(hotspots.contains("## Bridges"));
    assert!(hotspots.contains("[[code/files/src/lib.rs|Hub]]"));
    assert!(hotspots.contains("[[code/files/src/lib.rs|src/lib.rs]]"));
    assert!(hotspots.contains("frequency 3"));
    assert!(hotspots.contains("also listed under Hotspots"));
    assert!(!hotspots.contains("analytics unavailable"));
}

#[test]
fn codewiki_hotspots_page_degrades_when_analytics_unavailable() {
    let input = CodewikiInput {
        files: vec!["src/lib.rs".to_string()],
        graph_edges: Vec::new(),
        graph_availability: CodewikiGraphAvailability::Unavailable,
        symbols: vec![test_symbol(
            "src/lib.rs",
            "Client",
            "class",
            1,
            "pub struct Client;",
        )],
    };

    let docs = generate_hierarchical_docs(&input, None);
    let hotspots = docs
        .iter()
        .find(|(path, _)| path == "code/_hotspots.md")
        .map(|(_, content)| content)
        .expect("hotspots doc");
    let frontmatter = hotspots_frontmatter(hotspots);

    assert_eq!(
        frontmatter
            .get("degraded")
            .and_then(serde_yaml::Value::as_bool),
        Some(true)
    );
    assert!(
        frontmatter["degraded_sources"]
            .as_sequence()
            .expect("degraded sources")
            .iter()
            .any(|value| value.as_str() == Some("graph-analytics-unavailable"))
    );
    assert!(hotspots.contains("analytics unavailable"));
    assert!(!hotspots.contains("frequency 3"));
}

#[test]
fn codewiki_hotspots_page_cross_references_duplicate_bridges() {
    let node = HotspotNode {
        id: "component-1".to_string(),
        kind: "function".to_string(),
        label: "bridge".to_string(),
        wikilink: "[[code/files/src/lib.rs|bridge]]".to_string(),
        file_wikilink: Some("[[code/files/src/lib.rs|src/lib.rs]]".to_string()),
        source_span: None,
    };
    let finding = HotspotFinding {
        node,
        degree: Some(4),
        score: Some(1.0),
        frequency: Some(2),
        weight: Some(3.0),
    };
    let doc = render_hotspots_doc(&HotspotsDoc {
        source_spans: Vec::new(),
        hotspots: vec![finding.clone()],
        god_nodes: Vec::new(),
        bridges: vec![finding],
        degraded_sources: Vec::new(),
    });
    let bridges = doc
        .split("## Bridges")
        .nth(1)
        .expect("bridges section rendered");

    assert!(bridges.contains("also listed under Hotspots"));
    assert!(!bridges.contains("degree 4"));
}

fn hotspots_frontmatter(hotspots: &str) -> serde_yaml::Value {
    let yaml = hotspots
        .strip_prefix("---\n")
        .and_then(|content| content.split_once("---\n\n"))
        .map(|(yaml, _)| yaml)
        .expect("frontmatter block");
    serde_yaml::from_str(yaml).expect("parse frontmatter")
}
