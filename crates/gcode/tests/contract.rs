use std::collections::{BTreeMap, BTreeSet};
use std::process::Command;

use serde_json::Value;

fn pinned_contract() -> Value {
    serde_json::from_str(include_str!("../contract/gcode.contract.json")).expect("pinned contract")
}

fn shared_graph_schema_doc() -> &'static str {
    include_str!("../../../docs/contracts/shared-graph-schema.md")
}

fn code_graph_writer() -> &'static str {
    include_str!("../src/graph/code_graph/write.rs")
}

#[test]
fn contract_builder_matches_pinned_json() {
    let actual = serde_json::to_value(gobby_code::contract::contract()).expect("contract json");
    assert_eq!(actual, pinned_contract());
}

#[test]
fn contract_command_emits_pinned_json() {
    let output = Command::new(env!("CARGO_BIN_EXE_gcode"))
        .args(["contract", "--format", "json"])
        .output()
        .expect("run gcode contract");

    assert!(
        output.status.success(),
        "gcode contract failed\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );

    let actual: Value = serde_json::from_slice(&output.stdout).expect("contract stdout json");
    assert_eq!(actual, pinned_contract());
}

#[test]
fn code_graph_writer_matches_shared_schema_contract() {
    let docs = schema_node_identities(shared_graph_schema_doc());
    let relationships = schema_relationship_shapes(shared_graph_schema_doc());
    let writer = parse_cypher_snippets(code_graph_writer());

    assert!(!docs.is_empty(), "schema docs must declare node identities");
    assert!(
        !relationships.is_empty(),
        "schema docs must declare relationship endpoints"
    );

    for (label, identity_props) in docs {
        let Some(merges) = writer.node_merges.get(&label) else {
            panic!("{label} missing from gcode writer");
        };
        assert!(
            merges
                .iter()
                .any(|merge_props| identity_props.iter().all(|prop| merge_props.contains(prop))),
            "{label} writer merge missing identity props {identity_props:?}"
        );
    }

    for shape in relationships {
        assert!(
            writer.relationship_shapes.contains(&shape),
            "writer missing relationship shape {shape:?}"
        );
    }
}

#[derive(Debug, PartialEq, Eq, PartialOrd, Ord)]
struct RelationshipShape {
    name: String,
    source: String,
    target: String,
}

#[derive(Debug, Default)]
struct CypherSnippets {
    node_merges: BTreeMap<String, Vec<BTreeSet<String>>>,
    relationship_shapes: BTreeSet<RelationshipShape>,
}

fn schema_node_identities(docs: &str) -> BTreeMap<String, BTreeSet<String>> {
    let mut identities = BTreeMap::new();
    let mut in_labels = false;
    let mut active_label = None::<String>;
    let mut text = String::new();

    for line in docs.lines() {
        let trimmed = line.trim();
        if trimmed == "## Labels" {
            in_labels = true;
            continue;
        }
        if in_labels && trimmed.starts_with("## ") {
            break;
        }
        if !in_labels {
            continue;
        }
        if let Some(label) = single_backtick_token(trimmed) {
            active_label = Some(label.to_string());
            text.clear();
            continue;
        }
        if active_label.is_some() {
            text.push(' ');
            text.push_str(trimmed);
            if let Some(props) = identity_props(&text) {
                identities.insert(active_label.take().expect("active label"), props);
                text.clear();
            }
        }
    }

    identities
}

fn schema_relationship_shapes(docs: &str) -> BTreeSet<RelationshipShape> {
    let mut shapes = BTreeSet::new();
    let mut in_relationships = false;
    let mut active_relationship = None::<String>;
    let mut text = String::new();

    for line in docs.lines() {
        let trimmed = line.trim();
        if trimmed == "## Relationships" {
            in_relationships = true;
            continue;
        }
        if in_relationships && trimmed.starts_with("## ") {
            break;
        }
        if !in_relationships {
            continue;
        }
        if let Some(name) = single_backtick_token(trimmed) {
            collect_relationship_shapes(&mut shapes, active_relationship.take(), &text);
            active_relationship = Some(name.to_string());
            text.clear();
            continue;
        }
        if active_relationship.is_some() {
            text.push(' ');
            text.push_str(trimmed);
        }
    }
    collect_relationship_shapes(&mut shapes, active_relationship, &text);

    shapes
}

fn collect_relationship_shapes(
    shapes: &mut BTreeSet<RelationshipShape>,
    relationship: Option<String>,
    text: &str,
) {
    let Some(name) = relationship else {
        return;
    };
    for span in backtick_spans(text) {
        let Some((source, target)) = span.split_once(" -> ") else {
            continue;
        };
        shapes.insert(RelationshipShape {
            name: name.clone(),
            source: source.to_string(),
            target: target.to_string(),
        });
    }
}

fn parse_cypher_snippets(writer: &str) -> CypherSnippets {
    let mut snippets = CypherSnippets::default();
    let mut aliases = BTreeMap::new();

    for line in writer.lines().map(str::trim) {
        if line.starts_with("const ") {
            aliases.clear();
        }
        if let Some((alias, label, props)) = parse_node_merge(line) {
            aliases.insert(alias, label.clone());
            snippets.node_merges.entry(label).or_default().push(props);
            continue;
        }
        if let Some((source_alias, relationship, target_alias)) = parse_relationship_merge(line) {
            let source = aliases
                .get(&source_alias)
                .unwrap_or_else(|| panic!("missing label for alias {source_alias}"))
                .clone();
            let target = aliases
                .get(&target_alias)
                .unwrap_or_else(|| panic!("missing label for alias {target_alias}"))
                .clone();
            snippets.relationship_shapes.insert(RelationshipShape {
                name: relationship,
                source,
                target,
            });
        }
    }

    snippets
}

fn single_backtick_token(text: &str) -> Option<&str> {
    text.strip_prefix('`')?.strip_suffix('`')
}

fn identity_props(text: &str) -> Option<BTreeSet<String>> {
    let start = text.find('(')?;
    let end = text[start..].find(')')? + start;
    Some(
        text[start + 1..end]
            .split(',')
            .map(str::trim)
            .filter(|prop| !prop.is_empty())
            .map(str::to_string)
            .collect(),
    )
}

fn backtick_spans(text: &str) -> Vec<String> {
    let mut spans = Vec::new();
    let mut rest = text;
    while let Some(start) = rest.find('`') {
        let after_start = &rest[start + 1..];
        let Some(end) = after_start.find('`') else {
            break;
        };
        spans.push(after_start[..end].to_string());
        rest = &after_start[end + 1..];
    }
    spans
}

fn parse_node_merge(line: &str) -> Option<(String, String, BTreeSet<String>)> {
    let node = line.strip_prefix("MERGE (")?.split_once(')')?.0;
    let (alias, rest) = node.split_once(':')?;
    let label_end = rest.find([' ', '{']).unwrap_or(rest.len());
    let label = rest[..label_end].to_string();
    let props = node
        .split_once('{')
        .and_then(|(_, rest)| rest.split_once('}'))
        .map(|(props, _)| {
            props
                .split(',')
                .filter_map(|prop| {
                    prop.split_once(':')
                        .map(|(name, _)| name.trim().to_string())
                })
                .collect()
        })
        .unwrap_or_default();
    Some((alias.to_string(), label, props))
}

fn parse_relationship_merge(line: &str) -> Option<(String, String, String)> {
    let rest = line.strip_prefix("MERGE (")?;
    let (source_alias, rest) = rest.split_once(")-[")?;
    let (relationship_part, rest) = rest.split_once("]->(")?;
    let relationship = relationship_part
        .split_once(':')?
        .1
        .split([' ', '{', ']'])
        .next()?
        .to_string();
    let target_alias = rest.split_once(')')?.0.to_string();
    Some((source_alias.to_string(), relationship, target_alias))
}
