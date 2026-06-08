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

fn test_symbol(
    file_path: &str,
    name: &str,
    kind: &str,
    line_start: usize,
    signature: &str,
) -> Symbol {
    Symbol {
        id: test_component_id(file_path, name, kind),
        project_id: "project-1".to_string(),
        file_path: file_path.to_string(),
        name: name.to_string(),
        qualified_name: name.to_string(),
        kind: kind.to_string(),
        language: "rust".to_string(),
        byte_start: 0,
        byte_end: 0,
        line_start,
        line_end: line_start,
        signature: Some(signature.to_string()),
        docstring: None,
        parent_symbol_id: None,
        content_hash: String::new(),
        summary: None,
        created_at: String::new(),
        updated_at: String::new(),
    }
}

fn test_component_id(file_path: &str, name: &str, kind: &str) -> String {
    Symbol::make_id("project-1", file_path, name, kind, 0)
}

fn hotspots_frontmatter(hotspots: &str) -> serde_yaml::Value {
    let yaml = hotspots
        .strip_prefix("---\n")
        .and_then(|content| content.split_once("---\n\n"))
        .map(|(yaml, _)| yaml)
        .expect("frontmatter block");
    serde_yaml::from_str(yaml).expect("parse frontmatter")
}
