mod common;

use std::path::PathBuf;

use gobby_wiki::output::{
    AskAiOutput, AskCodeCitationOutput, AskCodeEdgeOutput, AskOutput, AskRelatedPageOutput,
    AskSynthesisOutput, SearchResultOutput,
};
use serde_json::Value;

fn pinned_contract() -> Value {
    serde_json::from_str(include_str!("../contract/gwiki.contract.json")).expect("pinned contract")
}

#[test]
fn contract_builder_matches_pinned_json() {
    let actual = serde_json::to_value(gobby_wiki::contract::contract()).expect("contract json");
    assert_eq!(actual, pinned_contract());
}

#[test]
fn contract_command_emits_pinned_json() {
    let output = common::gwiki_command()
        .args(["contract", "--format", "json"])
        .output()
        .expect("run gwiki contract");

    assert!(
        output.status.success(),
        "gwiki contract failed\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );

    let actual: Value = serde_json::from_slice(&output.stdout).expect("contract stdout json");
    assert_eq!(actual, pinned_contract());
}

#[test]
fn compile_contract_tracks_compile_json_payload_keys() {
    let contract = gobby_wiki::contract::contract();
    let compile = contract
        .commands
        .iter()
        .find(|command| command.name == "compile")
        .expect("compile command contract");

    let expected_keys = vec![
        "command",
        "scope",
        "status",
        "target_kind",
        "outline",
        "daemon_synthesis_available",
        "article_path",
        "source_paths",
        "index_path",
        "handoff_id",
        "page_writes",
        "prompt",
    ];

    assert_eq!(compile.json_output_keys, expected_keys);
}

fn command<'a>(contract: &'a Value, name: &str) -> &'a Value {
    contract["commands"]
        .as_array()
        .expect("commands array")
        .iter()
        .find(|command| command["name"] == name)
        .unwrap_or_else(|| panic!("{name} command contract"))
}

#[test]
fn ask_contract_json_keys_exist_in_serialized_output_shape() {
    let contract = gobby_wiki::contract::contract();
    let ask = contract
        .commands
        .iter()
        .find(|command| command.name == "ask")
        .expect("ask command contract");
    let output = AskOutput {
        command: "ask",
        scope: gobby_wiki::ScopeIdentity::project("project-1"),
        query: "Where is request handling wired?".to_string(),
        status: "answered",
        degraded: false,
        degraded_sources: Vec::new(),
        hits: vec![SearchResultOutput {
            title: Some("Request handler".to_string()),
            fusion_key: "project:project-1:wiki/code/files/src/handler.rs.md".to_string(),
            wiki_page: PathBuf::from("wiki/code/files/src/handler.rs.md"),
            source_path: PathBuf::from("src/handler.rs"),
            snippet: "fn handle() calls route().".to_string(),
            score: 0.95,
            sources: vec!["bm25".to_string(), "graph".to_string()],
            explanations: Vec::new(),
        }],
        related_pages: vec![AskRelatedPageOutput {
            title: Some("Request handler".to_string()),
            path: PathBuf::from("wiki/code/files/src/handler.rs.md"),
            score: 0.95,
        }],
        sources: vec!["src/handler.rs".to_string()],
        code_edges: vec![AskCodeEdgeOutput {
            source: "src/handler.rs:handle".to_string(),
            target: "src/router.rs:route".to_string(),
            kind: "calls".to_string(),
            direction: "outgoing".to_string(),
            line: Some(42),
            provenance: "gcode_falkor".to_string(),
        }],
        code_citations: vec![AskCodeCitationOutput {
            file: "src/handler.rs".to_string(),
            line: Some(42),
            symbol: Some("handle".to_string()),
        }],
        gaps: Vec::new(),
        stale_candidates: Vec::new(),
        suggested_questions: vec!["Where is route defined?".to_string()],
        warnings: Vec::new(),
        ai: Some(AskAiOutput {
            requested: true,
            requested_mode: "optional",
            route: "daemon",
            status: "available",
            model: Some("test-model".to_string()),
            error: None,
        }),
        synthesis: Some(AskSynthesisOutput {
            answer: "The handler calls the router.".to_string(),
            model: Some("test-model".to_string()),
        }),
    };
    let serialized = serde_json::to_value(output).expect("ask output json");
    let object = serialized.as_object().expect("ask output object");
    let missing = ask
        .json_output_keys
        .iter()
        .filter(|key| !object.contains_key(**key))
        .copied()
        .collect::<Vec<_>>();

    assert!(
        missing.is_empty(),
        "ask contract advertises keys missing from serialized AskOutput: {missing:?}"
    );
}

#[test]
fn parity_contract_tracks_code_grounding_and_dependency_classification() {
    let contract = pinned_contract();
    assert_eq!(contract["contract_version"], 3);

    let ask = command(&contract, "ask");
    assert_eq!(ask["hard_dependencies"], serde_json::json!(["PostgreSQL"]));
    assert_eq!(
        ask["optional_dependencies"],
        serde_json::json!([
            "model synthesis",
            "code graph",
            "Qdrant+embeddings",
            "FalkorDB"
        ])
    );
    assert_eq!(ask["multimodal"], "none");
    assert_eq!(
        ask["degradation"],
        serde_json::json!({
            "output_shape": "model off emits an extractive citation-list answer; signal loss falls back to wiki-only grounding",
            "metadata_keys": ["degraded", "degraded_sources[]"]
        })
    );
    assert_eq!(
        ask["json_output_keys"],
        serde_json::json!([
            "command",
            "scope",
            "query",
            "status",
            "degraded",
            "degraded_sources",
            "hits",
            "related_pages",
            "sources",
            "code_edges",
            "code_citations",
            "gaps",
            "stale_candidates",
            "suggested_questions",
            "warnings",
            "ai",
            "synthesis"
        ])
    );

    let graph_context = command(&contract, "graph-context");

    assert_eq!(
        graph_context["hard_dependencies"],
        serde_json::json!(["PostgreSQL"])
    );
    assert_eq!(
        graph_context["optional_dependencies"],
        serde_json::json!(["FalkorDB", "shared code graph"])
    );
    assert_eq!(graph_context["multimodal"], "none");
    assert_eq!(
        graph_context["degradation"],
        serde_json::json!({
            "output_shape": "wiki-link-only neighborhood",
            "metadata_keys": [
                "warnings[]",
                "degradation.degraded",
                "degradation.degraded_sources[]"
            ]
        })
    );

    assert_eq!(
        graph_context["json_output_keys"],
        serde_json::json!([
            "command",
            "scope",
            "context",
            "source_bundle",
            "code_edges",
            "code_citations",
            "trust",
            "freshness",
            "audit",
            "warnings",
            "degradation"
        ])
    );

    let research = command(&contract, "research");
    assert_eq!(
        research["hard_dependencies"],
        serde_json::json!(["PostgreSQL"])
    );
    assert_eq!(
        research["optional_dependencies"],
        serde_json::json!([
            "model multi-step synthesis loop",
            "code graph/index",
            "Qdrant+embeddings"
        ])
    );
    assert_eq!(research["multimodal"], "none");
    assert_eq!(
        research["degradation"],
        serde_json::json!({
            "output_shape": "model off emits a retrieval-only research scaffold with candidate sources and citations but no synthesized notes; code graph/index off emits docs-only output",
            "metadata_keys": ["accepted_notes[].degradation", "report.degradation"]
        })
    );
}
