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

fn command<'a>(contract: &'a Value, name: &str) -> &'a Value {
    contract["commands"]
        .as_array()
        .expect("commands array")
        .iter()
        .find(|cmd| cmd["name"] == Value::String(name.to_string()))
        .unwrap_or_else(|| panic!("command `{name}` missing from contract"))
}

fn output_keys(contract: &Value, name: &str) -> Vec<String> {
    command(contract, name)["json_output_keys"]
        .as_array()
        .expect("json_output_keys array")
        .iter()
        .map(|key| key.as_str().expect("key string").to_string())
        .collect()
}

#[test]
fn contract_is_version_eight_without_codewiki() {
    let contract = serde_json::to_value(gobby_code::contract::contract()).expect("contract json");
    assert_eq!(contract["contract_version"], serde_json::json!(8));
    assert!(
        contract["error_codes"]
            .as_array()
            .expect("error codes")
            .contains(&serde_json::json!("invalid_path_scope"))
    );
    let global_flags = contract["global_flags"]
        .as_array()
        .expect("global flags array");
    assert!(
        global_flags
            .iter()
            .any(|flag| flag["name"] == "--allow-stale")
    );
    assert!(
        !global_flags
            .iter()
            .any(|flag| flag["name"] == "--no-freshness")
    );
    assert_eq!(
        contract["exit_codes"],
        serde_json::json!([
            {
                "code": 0,
                "meaning": "success, including empty result sets"
            },
            {
                "code": 1,
                "meaning": "internal error (unclassified bug); plain `Error:` line on stderr"
            },
            {
                "code": 2,
                "meaning": "usage error or typed error (grant, project_required, invalid_path_scope, capability_unavailable, graph sync contract); one JSON line on stderr"
            },
            {
                "code": 3,
                "meaning": "`index --skip-if-locked` yielded to a concurrent indexer"
            },
            {
                "code": 10,
                "meaning": "embeddings doctor: config missing"
            },
            {
                "code": 11,
                "meaning": "embeddings doctor: config drift"
            },
            {
                "code": 20,
                "meaning": "embeddings doctor: transport failure"
            }
        ])
    );
    assert!(
        contract["commands"]
            .as_array()
            .expect("commands array")
            .iter()
            .all(|command| command["name"] != "codewiki"),
        "retired codewiki command must be absent"
    );
    assert!(
        contract["commands"]
            .as_array()
            .expect("commands array")
            .iter()
            .all(|command| command["name"] != "setup"),
        "standalone setup command must be absent"
    );
}

#[test]
fn contract_declares_tree_paths_and_typed_graph_view_selectors() {
    let contract = serde_json::to_value(gobby_code::contract::contract()).expect("contract json");
    assert_eq!(
        command(&contract, "tree")["positionals"],
        serde_json::json!([{"name": "PATH", "required": false, "repeatable": true}])
    );

    let graph_view = command(&contract, "graph view");
    assert_eq!(graph_view["positionals"], serde_json::json!([]));
    let flags = graph_view["flags"].as_array().expect("graph view flags");
    for expected in ["--file", "--module", "--symbol"] {
        assert!(
            flags.iter().any(|flag| flag["name"] == expected),
            "graph view missing {expected}"
        );
    }
}

#[test]
fn graph_view_payload_keys_are_complete() {
    let contract = serde_json::to_value(gobby_code::contract::contract()).expect("contract json");
    assert_eq!(
        output_keys(&contract, "graph view"),
        [
            "project_id",
            "project_root",
            "view",
            "seed",
            "depth",
            "incoming_truncated",
            "outgoing_truncated",
            "hint",
            "nodes",
            "edges",
            "communities",
            "mermaid",
        ]
    );

    let callees = command(&contract, "callees");
    assert_eq!(callees["daemon_consumed"], serde_json::json!(true));
    assert_eq!(
        output_keys(&contract, "callees"),
        output_keys(&contract, "callers")
    );
    let callees_flags: Vec<String> = callees["flags"]
        .as_array()
        .expect("callees flags")
        .iter()
        .map(|flag| flag["name"].as_str().expect("flag name").to_string())
        .collect();
    assert!(callees_flags.contains(&"--limit".to_string()));
    assert!(callees_flags.contains(&"--offset".to_string()));
    assert!(callees_flags.contains(&"--token-budget".to_string()));
}

#[test]
fn structural_graph_payload_keys_match_serialized_graph_payload() {
    let contract = serde_json::to_value(gobby_code::contract::contract()).expect("contract json");

    for name in [
        "graph overview",
        "graph file",
        "graph neighbors",
        "graph blast-radius",
    ] {
        assert_eq!(output_keys(&contract, name), ["nodes", "links", "center"]);
    }
}

fn grep_flag_names(contract: &Value) -> Vec<String> {
    command(contract, "grep")["flags"]
        .as_array()
        .expect("grep flags array")
        .iter()
        .map(|flag| flag["name"].as_str().expect("flag name").to_string())
        .collect()
}

#[test]
fn grep_flags_include_files_with_matches_and_noop_switches() {
    let contract = serde_json::to_value(gobby_code::contract::contract()).expect("contract json");
    let flags = grep_flag_names(&contract);
    for expected in [
        "--files-with-matches",
        "--extended-regexp",
        "--line-number",
        "--recursive",
        "-R",
        "--limit",
        "--offset",
        "--token-budget",
    ] {
        assert!(
            flags.contains(&expected.to_string()),
            "grep contract missing {expected}; flags={flags:?}"
        );
    }
}

#[test]
fn daemon_query_surface_is_consumed_with_keys() {
    let contract = serde_json::to_value(gobby_code::contract::contract()).expect("contract json");

    // Every query surface promoted in v2 must be daemon_consumed and declare
    // its JSON keys, so the daemon conformance check can see the stable shape.
    for name in [
        "outline",
        "symbol",
        "symbol-at",
        "symbols",
        "tree",
        "imports",
        "blast-radius",
        "search-text",
        "search-content",
    ] {
        assert_eq!(
            command(&contract, name)["daemon_consumed"],
            serde_json::json!(true),
            "{name} must be daemon_consumed in v2"
        );
        assert!(
            !output_keys(&contract, name).is_empty(),
            "{name} must declare json_output_keys"
        );
    }

    for expected in ["id", "name", "kind", "line_start", "line_end", "signature"] {
        assert!(output_keys(&contract, "outline").contains(&expected.to_string()));
    }
    for expected in ["file_path", "language", "symbol_count"] {
        assert!(output_keys(&contract, "tree").contains(&expected.to_string()));
    }

    // symbol = record + source; symbol-at adds lookup; symbols pages source-bearing
    // records and reports every missing requested ID. All three carry `summary`,
    // never `docstring`.
    let symbols = output_keys(&contract, "symbols");
    assert!(symbols.contains(&"summary".to_string()));
    assert!(!symbols.contains(&"docstring".to_string()));
    assert!(symbols.contains(&"source".to_string()));
    assert!(symbols.contains(&"missing_ids".to_string()));
    assert!(output_keys(&contract, "symbol").contains(&"source".to_string()));
    assert!(output_keys(&contract, "symbol-at").contains(&"lookup".to_string()));

    // imports + blast-radius share the paged graph envelope, distinct from the
    // callers/usages `graph_read_keys` surface.
    for name in ["imports", "blast-radius"] {
        let keys = output_keys(&contract, name);
        for expected in [
            "project_id",
            "total",
            "offset",
            "limit",
            "next_offset",
            "budget_exceeded",
            "results",
            "relation",
            "distance",
        ] {
            assert!(
                keys.contains(&expected.to_string()),
                "{name} paged envelope missing {expected}"
            );
        }
    }

    for name in [
        "search",
        "search-symbol",
        "search-text",
        "search-content",
        "grep",
        "outline",
        "symbols",
        "kinds",
        "tree",
        "callers",
        "callees",
        "usages",
        "imports",
        "blast-radius",
        "repo-outline",
    ] {
        let keys = output_keys(&contract, name);
        assert!(keys.contains(&"next_offset".to_string()), "{name}");
        assert!(keys.contains(&"budget_exceeded".to_string()), "{name}");
        let flags = command(&contract, name)["flags"]
            .as_array()
            .expect("collection flags");
        for expected in ["--limit", "--offset", "--token-budget"] {
            assert!(
                flags.iter().any(|flag| flag["name"] == expected),
                "{name} missing {expected}"
            );
        }
    }

    for name in [
        "graph overview",
        "graph file",
        "graph view",
        "graph rebuild",
    ] {
        let keys = output_keys(&contract, name);
        assert!(!keys.contains(&"next_offset".to_string()), "{name}");
        assert!(!keys.contains(&"budget_exceeded".to_string()), "{name}");
    }
}

#[test]
fn invalidate_declares_project_id_flag() {
    let contract = serde_json::to_value(gobby_code::contract::contract()).expect("contract json");
    let invalidate = command(&contract, "invalidate");

    let has_flag = invalidate["flags"]
        .as_array()
        .expect("flags array")
        .iter()
        .any(|flag| flag["name"] == Value::String("--project-id".to_string()));

    assert!(has_flag, "invalidate must declare the --project-id flag");
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
