use std::collections::{BTreeMap, BTreeSet};
use std::process::Command;

use pulldown_cmark::{Event, HeadingLevel, Parser, Tag, TagEnd};
use serde_json::Value;
use syn::{Expr, ExprLit, Item, Lit};

fn pinned_contract() -> Value {
    serde_json::from_str(include_str!("../contract/gcode.contract.json")).expect("pinned contract")
}

fn shared_graph_schema_doc() -> &'static str {
    include_str!("../../../docs/contracts/shared-graph-schema.md")
}

fn code_graph_writer() -> String {
    [
        include_str!("../src/graph/code_graph/write.rs"),
        include_str!("../src/graph/code_graph/write/mutation.rs"),
        include_str!("../src/graph/code_graph/write/deletion.rs"),
    ]
    .join("\n")
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
    let writer = parse_cypher_snippets(&code_graph_writer());

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

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum SchemaSection {
    Labels,
    Relationships,
}

#[derive(Debug, Default)]
struct CypherSnippets {
    node_merges: BTreeMap<String, Vec<BTreeSet<String>>>,
    relationship_shapes: BTreeSet<RelationshipShape>,
}

fn schema_node_identities(docs: &str) -> BTreeMap<String, BTreeSet<String>> {
    let mut identities = BTreeMap::new();
    let mut active_label = None::<String>;

    for span in markdown_section_code_spans(docs, SchemaSection::Labels) {
        if let Some(props) = identity_props(&span) {
            let label = active_label
                .take()
                .unwrap_or_else(|| panic!("identity props {props:?} missing active label"));
            identities.insert(label, props);
        } else {
            active_label = Some(span);
        }
    }

    identities
}

fn schema_relationship_shapes(docs: &str) -> BTreeSet<RelationshipShape> {
    let mut shapes = BTreeSet::new();
    let mut active_relationship = None::<String>;

    for span in markdown_section_code_spans(docs, SchemaSection::Relationships) {
        if let Some((source, target)) = span.split_once(" -> ") {
            let name = active_relationship
                .as_ref()
                .unwrap_or_else(|| panic!("relationship span `{span}` missing active name"));
            shapes.insert(RelationshipShape {
                name: name.clone(),
                source: source.to_string(),
                target: target.to_string(),
            });
        } else {
            active_relationship = Some(span);
        }
    }

    shapes
}

fn markdown_section_code_spans(docs: &str, target: SchemaSection) -> Vec<String> {
    let mut active_section = None;
    let mut heading_level = None;
    let mut heading_text = String::new();
    let mut spans = Vec::new();

    for event in Parser::new(docs) {
        match event {
            Event::Start(Tag::Heading { level, .. }) => {
                heading_level = Some(level);
                heading_text.clear();
            }
            Event::End(TagEnd::Heading(_)) => {
                active_section = if heading_level == Some(HeadingLevel::H2) {
                    section_from_heading(&heading_text)
                } else {
                    active_section
                };
                heading_level = None;
            }
            Event::Text(text) | Event::Code(text) if heading_level.is_some() => {
                heading_text.push_str(&text);
            }
            Event::Code(code) if active_section == Some(target) => spans.push(code.to_string()),
            _ => {}
        }
    }

    spans
}

fn section_from_heading(heading: &str) -> Option<SchemaSection> {
    match heading.trim() {
        "Labels" => Some(SchemaSection::Labels),
        "Relationships" => Some(SchemaSection::Relationships),
        _ => None,
    }
}

fn parse_cypher_snippets(writer: &str) -> CypherSnippets {
    let syntax = syn::parse_file(writer).expect("code graph writer must parse as Rust");
    let mut snippets = CypherSnippets::default();
    let mut aliases = BTreeMap::new();

    for item in syntax.items {
        let Item::Const(item) = item else {
            continue;
        };
        let Expr::Lit(ExprLit {
            lit: Lit::Str(cypher),
            ..
        }) = item.expr.as_ref()
        else {
            continue;
        };
        parse_cypher_text(&cypher.value(), &mut snippets, &mut aliases);
    }

    snippets
}

fn parse_cypher_text(
    cypher: &str,
    snippets: &mut CypherSnippets,
    aliases: &mut BTreeMap<String, String>,
) {
    aliases.clear();
    for line in cypher.lines().map(str::trim) {
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

#[test]
fn markdown_schema_parser_reads_code_spans_from_target_sections() {
    let docs = r#"
# Contract

## Labels

`CodeFile`
: Identity is
`(project, path)`.

## Relationships

`IMPORTS`
: `CodeFile -> CodeModule`.

## Consumer Contract

`ignored`
"#;

    assert_eq!(
        schema_node_identities(docs)["CodeFile"],
        BTreeSet::from(["project".to_string(), "path".to_string()])
    );
    assert!(
        schema_relationship_shapes(docs).contains(&RelationshipShape {
            name: "IMPORTS".to_string(),
            source: "CodeFile".to_string(),
            target: "CodeModule".to_string(),
        })
    );
}

#[test]
fn cypher_parser_reads_rust_string_constants() {
    let writer = r#"
const TEST_CYPHER: &str = "MERGE (f:CodeFile {path: $path, project: $project})
MERGE (m:CodeModule {name: $name, project: $project})
MERGE (f)-[r:IMPORTS]->(m)";
"#;

    let snippets = parse_cypher_snippets(writer);

    assert!(
        snippets.node_merges["CodeFile"]
            .contains(&BTreeSet::from(
                ["path".to_string(), "project".to_string(),]
            ))
    );
    assert!(snippets.relationship_shapes.contains(&RelationshipShape {
        name: "IMPORTS".to_string(),
        source: "CodeFile".to_string(),
        target: "CodeModule".to_string(),
    }));
}
